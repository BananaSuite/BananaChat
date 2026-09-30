"""Text generation shared by the chat, the playground and the public API.

``generate()`` takes a :class:`TextRequest` and yields events:

* :class:`Queued` - the request is waiting (position in line);
* :class:`Started` - a model began answering (again after a fallback);
* :class:`Delta` - a piece of the answer (``thinking=True`` for reasoning);
* :class:`Finished` - always last: state ``completed``, ``stopped`` or ``failed``.

Requests with ``tools`` (the agents) pass Ollama function definitions; the
model's tool calls arrive in ``Finished.tool_calls`` (unvalidated: the caller
checks them). Such requests always run on the local/compute Ollama server,
never on volunteer worker PCs.

It handles admission (``services.queue``), the generation deadline,
routing to a remote worker or the local Ollama server, falling back to the
next candidate model when one fails before producing any output, the
response size limit and token accounting (estimated when the backend reports
none). Callers persist results and charge credits.

Queue problems are raised before any event: ``queue.QueueFull``,
``queue.QueueTimeout`` and ``upstream.Cancelled`` (cancelled while waiting).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace

from flask import current_app

from bananachat.db import catalog
from bananachat.db.credits import estimate_tokens
from bananachat.services import model_lifecycle, ollama, queue, remote, supervisor
from bananachat.services.access import AccessContext, is_text_model, usable_models
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError

log = logging.getLogger("bananachat.inference")

# Accepted user sampling overrides: name -> (type, minimum, maximum).
OPTION_LIMITS = {
    "temperature": (float, 0.0, 2.0),
    "top_p": (float, 0.0, 1.0),
    "top_k": (int, 1, 200),
    "repeat_penalty": (float, 0.5, 2.5),
    "num_ctx": (int, 512, 1_048_576),
    "seed": (int, 0, 2**31 - 1),
    "presence_penalty": (float, -2.0, 2.0),
    "frequency_penalty": (float, -2.0, 2.0),
}


class ModelUnavailable(RuntimeError):
    """No usable model (or the requested one) is available. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


# ----- events -----------------------------------------------------------------

@dataclass
class Queued:
    position: int


@dataclass
class Started:
    model: object
    via_worker: bool
    wait_ms: int
    notice: str = ""


@dataclass
class Delta:
    text: str
    thinking: bool = False


@dataclass
class Finished:
    state: str  # completed | stopped | failed
    model: object
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = False
    error: str = ""
    wait_ms: int = 0
    duration_ms: int = 0
    via_worker: bool = False
    truncated: bool = False
    tool_calls: list = field(default_factory=list)


MAX_TOOL_CALLS = 32


@dataclass
class TextRequest:
    user: object
    model: object
    messages: list
    options: dict = field(default_factory=dict)
    request_type: str = "chat"
    priority: int = queue.PRIORITY_CHAT
    owner_key: str | None = None
    fallbacks: list = field(default_factory=list)
    think: bool | str | None = None  # Ollama's think: see think_for()
    # The resolved reasoning effort level (services.limits.resolve_effort), for backends that take levels
    # directly (the Claude pool) rather than Ollama's think.
    effort: str | None = None
    max_response_bytes: int | None = None
    # Called once admitted, before inference: return an error message to refuse
    # (e.g. the user was suspended or ran out of credits while waiting).
    authorize: object = None
    # Ollama function definitions (agents only); see the module docstring.
    tools: list | None = None
    # ``prepare(model) -> (messages, options)`` rebuilds the request for a
    # fallback model, so it gets its own system prompt and generation defaults.
    prepare: object = None


# ----- model selection ---------------------------------------------------------

_running_cache = {"at": 0.0, "names": set()}
_running_lock = threading.Lock()


def running_models() -> set[str]:
    """Names currently loaded by Ollama (cached for ten seconds)."""
    now = time.monotonic()
    with _running_lock:
        if now - _running_cache["at"] < 10:
            return set(_running_cache["names"])
    try:
        names = {model.get("name") or model.get("model") for model in ollama.list_running()}
    except (UpstreamError, OSError):
        names = set()
    with _running_lock:
        _running_cache.update(at=now, names=names)
    return set(names)


def _backend_name(model) -> str | None:
    if model is None:
        return None
    return model["backend_model_name"] or model["ollama_name"]


@dataclass
class Selection:
    model: object
    fallbacks: list
    notice: str = ""
    # Why the requested model was replaced: "deprecated" or "retired" (chat shows its own notice).
    reason: str = ""
    requested: object = None


def candidates(context: AccessContext, surface: str, *, vision: bool = False) -> list:
    """Usable text models, loaded ones first; deprecated models only after every other."""
    models = [model for model in usable_models(context, surface, kind="text")
              if (not vision or model["supports_vision"]) and not model["retired_at"]]
    loaded = running_models()
    return sorted(models, key=lambda model: (bool(model["deprecated_at"]),
                                             model["ollama_name"] not in loaded and
                                             (model["backend_model_name"] or model["ollama_name"]) not in loaded,
                                             model["sort_order"]))


def _replaced(context: AccessContext, model, surface: str, vision: bool, strict: bool) -> Selection | None:
    """A deprecated or retired model's replacement (chat), or the API's refusal of a retired model."""
    if not (model["retired_at"] or (model["deprecated_at"] and not strict)):
        return None
    replacement = model_lifecycle.replacement_for(model, context, surface)
    if replacement is not None and vision and not replacement["supports_vision"]:
        replacement = None
    if strict:
        suffix = f"; use '{replacement['ollama_name']}' instead" if replacement is not None else ""
        raise ModelUnavailable(f"The model '{model['ollama_name']}' was retired{suffix}.", 404)
    reason = "retired" if model["retired_at"] else "deprecated"
    if replacement is None:
        if reason == "deprecated" and context.can_use(model, surface) and is_text_model(model):
            return None  # nothing to move to yet: keep answering with it
        options = candidates(context, surface, vision=vision)
        if not options:
            raise ModelUnavailable("No model that can handle this request is available right now.")
        return Selection(options[0], options[1:3], reason=reason, requested=model)
    others = [item for item in candidates(context, surface, vision=vision) if item["id"] != replacement["id"]]
    return Selection(replacement, others[:2], reason=reason, requested=model)


def select_model(context: AccessContext, requested: str | None, *, surface: str, vision: bool = False,
                 strict: bool = False) -> Selection:
    """Resolve a requested model name (or ``auto``) to a usable text model.

    With ``strict`` (API), an unknown, forbidden or unavailable name is an
    error; otherwise (chat) the best available model is used instead, with a
    notice explaining the switch.
    """
    requested = (requested or "auto").strip()
    if requested and requested != "auto":
        model = catalog.get_by_name(requested)
        if model is not None and model["enrollment"] == "ignored":
            model = None  # an ignored model is never offered
        replaced = _replaced(context, model, surface, vision, strict) if model is not None else None
        if replaced is not None:
            return replaced
        if model is None:
            if strict:
                raise ModelUnavailable(f"The model '{requested}' does not exist.", 404)
        elif not context.can_use(model, surface):
            raise ModelUnavailable(f"You do not have access to '{requested}'.", 403)
        elif not is_text_model(model):
            if strict:
                raise ModelUnavailable(f"The model '{requested}' is not available right now.", 503)
        elif vision and not model["supports_vision"]:
            if strict:
                raise ModelUnavailable(f"The model '{requested}' cannot read images.", 400)
        else:
            return Selection(model, [])
        options = candidates(context, surface, vision=vision)
        if not options:
            raise ModelUnavailable("No model that can handle this request is available right now.")
        name = model["display_name"] if model else requested
        return Selection(options[0], options[1:3], notice=f"{name} is not available, so {options[0]['display_name']} "
                                                          "is answering instead.")
    options = candidates(context, surface, vision=vision)
    if not options:
        raise ModelUnavailable("No model is available right now. Ask an administrator to add or enable one.")
    return Selection(options[0], options[1:3])


# ----- reasoning effort ----------------------------------------------------------

# Ollama's ``think`` takes true/false, or "low"/"medium"/"high" for models with named levels.
_OLLAMA_LEVELS = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def think_for(model, level: str | None):
    """Ollama's ``think`` value for a reasoning effort level (``services.limits.resolve_effort``).

    The one place that maps levels: no level keeps the model's own default
    (thinking on for reasoning models), ``off`` is false, ``on`` true, and a
    named level is passed as a name to models that take named levels (``max``
    as ``high``) and as true to models that only switch thinking on.
    """
    from bananachat.services import limits

    if level is None:
        return True if model["is_reasoning"] else None
    if level == "off":
        return False
    if level != "on" and limits.named_levels(model):
        return _OLLAMA_LEVELS.get(level, "medium")
    return True


def compatible_fallbacks(fallbacks: list, think) -> list:
    """Fallback models that accept the same ``think`` value: named levels need models with named levels,
    true or false models that reason; no value suits every model."""
    from bananachat.services import limits

    if think is None:
        return list(fallbacks)
    if isinstance(think, str):
        return [model for model in fallbacks if limits.named_levels(model)]
    return [model for model in fallbacks if model["is_reasoning"] or limits.supported_efforts(model)]


def build_options(model, overrides: dict | None = None, *, max_tokens: int | None = None, config=None) -> dict:
    """Administrator options for *model*, then validated user overrides, then limits."""
    config = config or current_app.config["BC"]
    options = catalog.options(model)
    for name, value in (overrides or {}).items():
        limits = OPTION_LIMITS.get(name)
        if limits is None or value is None or value == "" or isinstance(value, bool):
            continue
        kind, low, high = limits
        try:
            value = kind(value)
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            options[name] = value
    context_cap = min(config.max_num_ctx, model["num_ctx"] or config.max_num_ctx)
    if "num_ctx" in options:
        options["num_ctx"] = min(int(options["num_ctx"]), context_cap)
    limit = config.max_output_tokens
    if max_tokens:
        limit = min(limit, max(1, int(max_tokens)))
    options["num_predict"] = limit
    return options


# ----- generation --------------------------------------------------------------

def generate(request: TextRequest, cancel: CancelToken):
    """Run *request*; see the module docstring for the event protocol."""
    config = current_app.config["BC"]
    slot = queue.Slot(request.priority, owner_key=request.owner_key, model=_backend_name(request.model))
    with slot:
        for position in slot.positions(timeout=config.queue_timeout, cancel=cancel):
            yield Queued(position)
        deadline_handle = supervisor.cancel_at(time.monotonic() + config.generation_timeout, cancel)
        try:
            yield from _run_admitted(request, cancel, slot, config)
        finally:
            supervisor.clear_deadline(deadline_handle)


def _run_admitted(request: TextRequest, cancel: CancelToken, slot, config):
    started = time.monotonic()
    wait_ms = slot.wait_ms
    model = request.model
    if request.authorize is not None:
        problem = request.authorize()
        if problem:
            yield Finished("failed", model, error=problem, wait_ms=wait_ms)
            return

    limit = request.max_response_bytes or config.chat_max_response_bytes
    attempts = [request.model, *request.fallbacks]
    last_error = ""
    tried = request.model
    for index, model in enumerate(attempts):
        if index:
            # Fallbacks were chosen when the request was made: skip one that became failing, missing, disabled
            # or was removed while the request waited.
            fresh = catalog.get(model["id"])
            if fresh is None or not fresh["is_rolled_out"] or fresh["missing_at"] or not is_text_model(fresh):
                log.info("Skipping fallback %s: no longer available", model["ollama_name"])
                continue
            model = fresh
        if index and request.prepare is not None:
            messages, options = request.prepare(model)
            request = replace(request, messages=messages, options=options)
        if index:
            slot.set_model(_backend_name(model))
        via_worker = not request.tools and model["backend"] == "ollama" and remote.should_route(request.user, model,
                                                                request_type=request.request_type,
                                                                think=request.think)
        notice = "" if index == 0 else f"{tried['display_name']} failed, so " \
                                       f"{model['display_name']} is answering instead."
        tried = model
        yield Started(model, via_worker, wait_ms, notice)
        produced = 0
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        truncated = False
        stream = None
        try:
            stream = _open_stream(request, model, via_worker, cancel, config)
            for chunk in stream:
                if request.tools and chunk.tool_calls:
                    tool_calls.extend(chunk.tool_calls[: max(0, MAX_TOOL_CALLS - len(tool_calls))])
                    produced += 1
                if chunk.done:
                    prompt, completion = chunk.prompt_tokens, chunk.completion_tokens
                    estimated = prompt is None or completion is None
                    if prompt is None:
                        prompt = sum(estimate_tokens(str(m.get("content", ""))) for m in request.messages)
                    if completion is None:
                        completion = estimate_tokens("".join(text_parts))
                    if not via_worker:
                        model_lifecycle.record_success(model)
                    yield Finished("completed", model, chunk.finish_reason or "stop", prompt, completion, estimated,
                                   wait_ms=wait_ms, duration_ms=_elapsed(started), via_worker=via_worker,
                                   tool_calls=tool_calls)
                    return
                for text, thinking in ((chunk.thinking, True), (chunk.content, False)):
                    if not text:
                        continue
                    size = len(text.encode("utf-8"))
                    if produced + size > limit:
                        text = text.encode("utf-8")[: max(0, limit - produced)].decode("utf-8", "ignore")
                        truncated = True
                    produced += len(text.encode("utf-8"))
                    if text:
                        text_parts.append(text)
                        yield Delta(text, thinking)
                    if truncated:
                        break
                if truncated:
                    cancel.cancel("limit")
                    break
        except Cancelled:
            pass
        except (UpstreamError, OSError) as error:
            last_error = str(error) or "The model failed."
            log.warning("Generation with %s failed: %s", model["ollama_name"], last_error)
            if not via_worker and not cancel.cancelled:
                model_lifecycle.record_failure(model, error, config)
            if produced == 0 and not cancel.cancelled and index + 1 < len(attempts):
                continue
            yield _partial(request, model, text_parts, "failed", last_error, wait_ms, started, via_worker)
            return
        finally:
            if stream is not None and hasattr(stream, "close"):
                stream.close()

        if truncated:
            finished = _partial(request, model, text_parts, "stopped", "", wait_ms, started, via_worker)
            finished.finish_reason, finished.truncated = "length", True
            yield finished
            return
        reason = cancel.reason if cancel.cancelled else ""
        if reason == "stopped":
            yield _partial(request, model, text_parts, "stopped", "", wait_ms, started, via_worker)
        elif reason == "deadline":
            yield _partial(request, model, text_parts, "failed" if not text_parts else "stopped",
                           "The answer took too long and was stopped.", wait_ms, started, via_worker)
        elif reason:
            yield _partial(request, model, text_parts, "failed", "The request was interrupted.", wait_ms, started,
                           via_worker)
        else:
            yield _partial(request, model, text_parts, "failed", "The model stopped before finishing its answer.",
                           wait_ms, started, via_worker)
        return
    yield Finished("failed", tried, error=last_error or "No model could answer.", wait_ms=wait_ms,
                   duration_ms=_elapsed(started))


def _open_stream(request, model, via_worker, cancel, config):
    name = model["backend_model_name"] or model["ollama_name"]
    if model["backend"] == "claude":
        # Claude models are served by the pooled subscription accounts, never by Ollama or a worker PC.
        from bananachat.services import claude_pool

        return claude_pool.stream_chunks(name, request.messages, options=request.options, think=request.think,
                                         effort=request.effort, cancel=cancel)
    if via_worker:
        return remote.stream(name, request.messages, request.options, cancel=cancel, priority=request.priority,
                             think=request.think,
                             first_token_timeout=config.first_token_timeout,
                             total_timeout=config.generation_timeout)
    if request.tools:
        return ollama.chat_stream(name, request.messages, options=request.options, think=request.think,
                                  cancel=cancel, config=config, tools=request.tools)
    return ollama.chat_stream(name, request.messages, options=request.options, think=request.think, cancel=cancel,
                              config=config)


def _partial(request, model, text_parts, state, error, wait_ms, started, via_worker) -> Finished:
    completion = estimate_tokens("".join(text_parts))
    prompt = sum(estimate_tokens(str(message.get("content", ""))) for message in request.messages)
    return Finished(state, model, "stop", prompt, completion, True, error, wait_ms, _elapsed(started), via_worker)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
