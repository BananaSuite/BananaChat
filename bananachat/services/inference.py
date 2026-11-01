"""Text generation shared by the chat, the playground and the public API.

``generate()`` takes a :class:`TextRequest` and yields events:

* :class:`Queued` - the request is waiting (position in line);
* :class:`Started` - a model began answering (again after a fallback);
* :class:`Delta` - a piece of the answer (``thinking=True`` for reasoning);
* :class:`Finished` - always last: state ``completed``, ``stopped`` or ``failed``.

Queue problems are raised before any event: ``queue.QueueFull``, ``queue.QueueTimeout`` and
``upstream.Cancelled`` (cancelled while waiting); also after one, when the last fallback is of the other lane
and gets no place there. Requests with ``tools`` (cloud sessions) get the model's tool
calls, unvalidated, in ``Finished.tool_calls`` and never run on worker PCs.

Callers save the result and charge the usage. An answer that does not complete is charged by
:func:`interrupted_usage`: the provider's counts when it sent them, else estimates of the prompt and of what was
produced. ``Finished.billable`` marks an interrupted external or subscription request that costs allowance even
without output. Queueing, fallbacks and failure attribution: docs/architecture.md, section Inference.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field, replace

from flask import current_app

from bananachat.db import catalog
from bananachat.db.credits import IMAGE_TOKEN_ESTIMATE, estimate_tokens
from bananachat.services import health, model_lifecycle, ollama, queue, remote, supervisor
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
    error_status: int | None = None
    error_code: str = ""
    retry_after: int | None = None
    # The provider reported token usage for this attempt (also for an interrupted answer).
    usage_reported: bool = False
    # The attempt has a cost even if nothing was produced: the provider reported usage, or an external API
    # request was interrupted after it was sent (see interrupted_usage).
    billable: bool = False
    # Requests with tools: the provider's own record of a completed turn, to be sent back unchanged with the
    # tool results (Claude's signed thinking blocks, see external_api._replay); None for other backends.
    replay: dict | None = None


def continues(last, event) -> bool:
    """Whether *event* only extends *last* for a reader that has read neither (services.event_pump)."""
    if isinstance(last, Delta) and isinstance(event, Delta):
        return last.thinking == event.thinking
    return isinstance(last, Queued) and isinstance(event, Queued)


def joined(events: list):
    """The one event that stands for a run of events that :func:`continues`."""
    if isinstance(events[0], Delta):
        return Delta("".join(event.text for event in events), events[0].thinking)
    return events[-1]


MAX_TOOL_CALLS = 32
# Reasoning an external provider does not stream is charged at this rate for the time before the first output
# when it did not count the output itself. Providers generate 50-150 tokens per second, so this stays below their
# bill.
HIDDEN_REASONING_TOKENS_PER_SECOND = 20


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
    # directly (external API providers) rather than Ollama's think.
    effort: str | None = None
    max_response_bytes: int | None = None
    # Called with the fresh model before each attempt: return an error message
    # to refuse (e.g. access was revoked or limits were spent while waiting).
    authorize: object = None
    # Ollama function definitions (agents only); see the module docstring.
    tools: list | None = None
    # ``prepare(model) -> (messages, options)`` rebuilds the request for a
    # fallback model, so it gets its own system prompt and generation defaults.
    prepare: object = None
    # Token counts the provider reported before the end of the current attempt
    # (``prompt``/``completion``), so an interrupted answer is charged with them.
    reported: dict = field(default_factory=dict)
    # The current attempt (see interrupted_usage): ``external``, ``reasoning`` (the provider was asked to reason),
    # monotonic times ``opened`` (sent), ``output`` (first output) and ``ended``, and ``interrupted``.
    attempt: dict = field(default_factory=dict)


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
    return model["ollama_name"] if model["backend"] == "external" else model["backend_model_name"] or model["ollama_name"]


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
    config = current_app.config["BC"]
    if not config.ollama_is_local and config.inference_outage_mode == "shutdown" and health.inference_down():
        models = [model for model in models if model["backend"] != "ollama"]
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
_OLLAMA_LEVELS = {"low": "low", "medium": "medium", "high": "high", "extra": "high", "max": "high"}


def think_for(model, level: str | None):
    """Ollama's ``think`` value for a reasoning effort level (``services.limits.resolve_effort``).

    The one place that maps levels: no level keeps the model's own default
    (thinking on for reasoning models), ``off`` is false, ``on`` true, and a
    named level is passed as a name to models that take named levels (``extra``
    and ``max`` as ``high``) and as true to models that only switch thinking on.
    External API providers receive the resolved level separately without this Ollama mapping.
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
    # Hosted models wait in a lane of their own: a model server or worker that went away cannot hold them up.
    slot = queue.Slot(request.priority, owner_key=request.owner_key, model=_backend_name(request.model),
                      lane=queue.lane_for(request.model))
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
    queue_deadline = time.monotonic() + max(0.0, config.queue_timeout - wait_ms / 1000)
    model = request.model
    limit = request.max_response_bytes or config.chat_max_response_bytes
    attempts = [request.model, *request.fallbacks]
    last_error = ""
    last_error_status = last_retry_after = None
    last_error_code = ""
    tried = request.model
    failed_before = False  # an attempt of this request already failed: later ones say nothing about their model
    for index, model in enumerate(attempts):
        # Register a fallback before checking its fresh deletion marker. A
        # concurrent delete then either sees this request or prevents it from
        # starting; it cannot miss the switch between authorization and launch.
        if index:
            lane = queue.lane_for(model)
            if lane == slot.lane and slot.running:
                slot.set_model(_backend_name(model))
            else:
                # A fallback of the other kind (a local model after a hosted one) waits for a place in its own
                # lane, within what is left of the queue timeout: never past BC_MAX_CONCURRENT, the free-memory
                # check or the account's place there.
                try:
                    slot.move(lane, _backend_name(model))
                    for _position in slot.positions(timeout=max(0.0, queue_deadline - time.monotonic()),
                                                    cancel=cancel):
                        pass
                except (queue.QueueFull, queue.QueueTimeout) as error:
                    log.info("Fallback %s got no place in the %s lane: %s", model["ollama_name"], lane, error)
                    if index + 1 < len(attempts):
                        continue
                    raise
                wait_ms += slot.wait_ms
        # Selection can predate a queue wait or another attempt. Never run a
        # model that has since been removed, disabled or withdrawn.
        fresh = catalog.get(model["id"])
        if fresh is None or fresh["missing_at"] or not is_text_model(fresh):
            if index:
                log.info("Skipping fallback %s: no longer available", model["ollama_name"])
                continue
            yield Finished("failed", model, error=f"The model '{model['ollama_name']}' is no longer available "
                           "on the model server.", wait_ms=wait_ms)
            return
        model = fresh
        if index and not model["is_rolled_out"]:
            continue
        if request.authorize is not None:
            problem = request.authorize(model)
            if problem:
                last_error = problem
                last_error_status = last_retry_after = None
                last_error_code = ""
                tried = model
                if index:
                    continue
                yield Finished("failed", model, error=problem, wait_ms=wait_ms)
                return
        if request.prepare is not None:
            messages, options = request.prepare(model)
            request = replace(request, messages=messages, options=options)
        via_worker = not request.tools and model["backend"] == "ollama" and remote.should_route(request.user, model,
                                                                request_type=request.request_type,
                                                                think=request.think)
        notice = "" if index == 0 else f"{tried['display_name']} failed, so " \
                                       f"{model['display_name']} is answering instead."
        tried = model
        request.reported.clear()
        request.attempt.clear()
        request.attempt["external"] = model["backend"] in ("external", "claude", "codex")
        yield Started(model, via_worker, wait_ms, notice)
        produced = 0
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        truncated = False
        stream = None
        try:
            cancel.check()
            request.attempt["opened"] = time.monotonic()
            stream = _open_stream(request, model, via_worker, cancel, config)
            for chunk in stream:
                if not chunk.done:
                    for key, value in (("prompt", chunk.prompt_tokens), ("completion", chunk.completion_tokens)):
                        if value is not None:
                            request.reported[key] = value
                if request.tools and chunk.tool_calls:
                    tool_calls.extend(chunk.tool_calls[: max(0, MAX_TOOL_CALLS - len(tool_calls))])
                    produced += 1
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
                        request.attempt.setdefault("output", time.monotonic())
                        yield Delta(text, thinking)
                    if truncated:
                        break
                if truncated:
                    cancel.cancel("limit")
                    break
                if chunk.done:
                    request.attempt.setdefault("ended", time.monotonic())
                    prompt, completion = chunk.prompt_tokens, chunk.completion_tokens
                    estimated = prompt is None or completion is None
                    if estimated:
                        # No usage from the backend: the estimates of an interrupted answer (the counts reported
                        # before the end, else the prompt, the output and the reasoning it did not stream).
                        guessed_prompt, guessed_completion = interrupted_usage(request, "".join(text_parts))
                        prompt = guessed_prompt if prompt is None else prompt
                        completion = guessed_completion if completion is None else completion
                    if not via_worker:
                        model_lifecycle.record_success(model)
                    yield Finished("completed", model, chunk.finish_reason or "stop", prompt, completion, estimated,
                                   wait_ms=wait_ms, duration_ms=_elapsed(started), via_worker=via_worker,
                                   tool_calls=tool_calls, usage_reported=not estimated, billable=True,
                                   replay=chunk.extra.get("replay") if request.tools else None)
                    return
        except (Cancelled, GeneratorExit) as interruption:
            request.attempt.update(interrupted=True, ended=time.monotonic())
            if isinstance(interruption, GeneratorExit):
                raise  # the caller stopped reading (an API client went away): it charges interrupted_usage()
        except (UpstreamError, OSError) as error:
            sent = isinstance(error, UpstreamError) and error.sent
            if cancel.cancelled or sent:
                # A prompt the backend read and then refused to answer costs as much as one interrupted.
                request.attempt.update(interrupted=True, ended=time.monotonic())
            last_error = str(error) or "The model failed."
            capacity_unavailable = isinstance(error, UpstreamError) and error.kind == "capacity"
            # External API failures carry a provider_* code that the chat and the API explain.
            provider_code = error.code if isinstance(error, UpstreamError) and model["backend"] == "external" \
                and (error.code or "").startswith("provider_") else ""
            last_error_status = 503 if capacity_unavailable else None
            last_error_code = provider_code or ("provider_capacity_unavailable" if capacity_unavailable else "")
            last_retry_after = (error.retry_after or 30) if capacity_unavailable else None
            if capacity_unavailable:
                log.info("Generation with %s is waiting for provider capacity", model["ollama_name"])
            else:
                detail = getattr(error, "detail", "")
                log.warning("Generation with %s failed: %s%s", model["ollama_name"], last_error,
                            f" Provider message: {detail}" if detail else "")
            if not via_worker and not cancel.cancelled and not capacity_unavailable and not failed_before \
                    and not _request_fault(error, request):
                model_lifecycle.record_failure(model, error, config)
            failed_before = True
            if produced == 0 and not cancel.cancelled and not sent and index + 1 < len(attempts):
                continue
            finished = _partial(request, model, text_parts, "failed", last_error, wait_ms, started, via_worker)
            if produced == 0:
                finished.error_status = last_error_status
                finished.error_code = last_error_code
                finished.retry_after = last_retry_after
            yield finished
            return
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001 - cleanup cannot replace a committed outcome or backend error
                    log.exception("Closing generation with %s failed", model["ollama_name"])

        if cancel.cancelled:
            request.attempt.setdefault("ended", time.monotonic())
            request.attempt["interrupted"] = True
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
                   duration_ms=_elapsed(started), error_status=last_error_status, error_code=last_error_code,
                   retry_after=last_retry_after)


# The model server's words for input it cannot read (Ollama: "failed to process inputs: image: unknown format").
_INPUT_FAULTS = ("process input", "invalid input", "input length", "input too long", "prompt is too long",
                 "prompt too long", "context length", "exceeds the context")
_IMAGE_FAULTS = ("image", "unsupported format", "unknown format")


def _request_fault(error, request) -> bool:
    """Whether a failure follows from the request rather than the model (see the module docstring)."""
    status = getattr(error, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return True
    if getattr(error, "kind", None) is not None:
        return False  # a timeout, an unreachable server or missing capacity says nothing about the request
    return input_fault(str(error), images=any(item.get("images") for item in request.messages))


def input_fault(message: str, *, images: bool) -> bool:
    """Whether a model server's error message says it could not read the request (its input, or an image the
    request carried)."""
    message = message.lower()
    return any(marker in message for marker in _INPUT_FAULTS) or (
        images and any(marker in message for marker in _IMAGE_FAULTS))


def _provider_effort(request, model):
    from bananachat.db import users
    from bananachat.services import limits

    user = users.get(request.user["id"])
    if user is None or users.is_suspended(user):
        raise UpstreamError("This account cannot start a provider request.")
    try:
        return limits.resolve_effort(user, model, request.effort)
    except limits.EffortLocked as error:
        raise UpstreamError(str(error)) from None


def _open_stream(request, model, via_worker, cancel, config):
    name = model["backend_model_name"] or model["ollama_name"]
    if model["backend"] == "external":
        from bananachat.services import external_providers

        effort = _provider_effort(request, model)
        request.attempt["reasoning"] = bool(model["is_reasoning"]) and effort != "off"
        return external_providers.stream(model, request.messages, options=request.options, effort=effort,
                                         tools=request.tools, cancel=cancel)
    if model["backend"] in ("claude", "codex"):
        # Subscription models are served by the pooled accounts, never by Ollama or a worker PC.
        from bananachat.services import subscription_pool

        effort = _provider_effort(request, model)
        request.attempt["reasoning"] = bool(model["is_reasoning"]) and effort != "off"
        return subscription_pool.stream_chunks(model["backend"], name, request.messages, options=request.options,
                                               think=request.think, effort=effort, cancel=cancel,
                                               user_id=request.user.get("id") if request.user is not None else None)
    if model["backend"] != "ollama":
        # Only Ollama models reach a model server or a worker PC; no other backend ever does.
        raise UpstreamError(f"The model '{model['ollama_name']}' is not available right now.")
    if via_worker:
        return remote.stream(name, request.messages, request.options, cancel=cancel, priority=request.priority,
                             think=request.think,
                             first_token_timeout=config.first_token_timeout,
                             total_timeout=config.generation_timeout,
                             user_id=request.user.get("id") if request.user is not None else None)
    if request.tools:
        return ollama.chat_stream(name, request.messages, options=request.options, think=request.think,
                                  cancel=cancel, config=config, tools=request.tools)
    return ollama.chat_stream(name, request.messages, options=request.options, think=request.think, cancel=cancel,
                              config=config)


def _partial(request, model, text_parts, state, error, wait_ms, started, via_worker) -> Finished:
    """An answer that did not complete: provider-reported counts where known, estimates otherwise."""
    prompt, completion = interrupted_usage(request, "".join(text_parts))
    return Finished(state, model, "stop", prompt, completion, True, error, wait_ms, _elapsed(started), via_worker,
                    usage_reported=bool(request.reported), billable=billable(request))


def estimate_prompt(messages) -> int:
    """Estimated input tokens of *messages*: their text and :data:`IMAGE_TOKEN_ESTIMATE` per image."""
    return sum(estimate_tokens(str(message.get("content", ""))) + IMAGE_TOKEN_ESTIMATE * len(message.get("images") or ())
               for message in messages)


def billable(request, *, interrupted: bool | None = None) -> bool:
    """Whether the current attempt costs something even with no output (see the module docstring); with
    *interrupted*, as if it were (or were not) interrupted now."""
    attempt = request.attempt
    if interrupted is None:
        interrupted = bool(attempt.get("interrupted"))
    return bool(request.reported) or bool(interrupted and attempt.get("external") and attempt.get("opened") is not None)


def _hidden_reasoning(request, visible: int) -> int:
    """Reasoning tokens an external attempt that was asked to reason probably spent without streaming them (the
    time before its first output; until now while it has none)."""
    attempt = request.attempt
    if not (attempt.get("reasoning") and attempt.get("opened") is not None):
        return 0
    silent = (attempt.get("output") or attempt.get("ended") or time.monotonic()) - attempt["opened"]
    hidden = math.ceil(max(0.0, silent) * HIDDEN_REASONING_TOKENS_PER_SECOND)
    limit = (request.options or {}).get("num_predict")
    return min(hidden, max(0, int(limit) - visible)) if limit else hidden


def interrupted_usage(request, produced: str) -> tuple[int, int]:
    """Tokens to charge for an interrupted answer (or one whose backend reported no usage): the provider's counts
    when it sent them, else estimates of the prompt and of what was produced plus the reasoning it did not stream
    (see the module docstring)."""
    reported = request.reported
    prompt = reported.get("prompt")
    if prompt is None:
        prompt = estimate_prompt(request.messages)
    visible = estimate_tokens(produced)
    if reported.get("completion") is not None:
        return prompt, max(visible, reported["completion"])
    return prompt, visible + _hidden_reasoning(request, visible)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
