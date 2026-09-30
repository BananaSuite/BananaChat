"""OpenAI-style chat completions, shared by the public API and the playground.

``parse_chat_request()`` validates an OpenAI ``chat/completions`` body and
turns it into :class:`ChatParams` (Ollama-shaped messages plus options).
:class:`CompletionRun` selects the model, checks the reasoning effort and the
limits (``services.limits.admit``: the pool's 5-hour and weekly tokens and the
model's own limits), runs the request through ``services.inference.generate``
and charges it exactly once, also when the client goes away in the middle of a
streamed answer.

Problems are raised as :class:`CompletionError` with an HTTP status; the web
layer turns them into its own error format.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
import time
from dataclasses import dataclass, field

from flask import current_app

from bananachat.db import catalog, credits, tokens, users
from bananachat.services import api_usage, inference, limits, model_lifecycle, queue
from bananachat.services.access import AccessContext
from bananachat.services.upstream import MAX_CONTEXT_IMAGE_BYTES, Cancelled, CancelToken

log = logging.getLogger("bananachat.completions")

ROLES = ("system", "user", "assistant")
MAX_MESSAGES = 100
MAX_STOP_SEQUENCES = 4
MAX_STOP_CHARS = 200
MAX_MODEL_NAME = 300
IMAGE_SIGNATURES = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda data: data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,(.*)$", re.IGNORECASE | re.DOTALL)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\[[0-9a-f:]+\](?::\d+)?", re.IGNORECASE)
# OpenAI's reasoning_effort values (and "max") to effort levels.
EFFORTS = {"none": "off", "minimal": "low", "low": "low", "medium": "medium", "high": "high", "max": "max"}
_UNREACHABLE = ("could not be reached", "did not answer in time", "stopped responding", "connection to the backend")


class CompletionError(Exception):
    """A request that cannot be served. ``status`` is the HTTP status to return."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request", *,
                 retry_after: int | None = None, param: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.param = param


def sanitize_backend_message(text: str) -> str:
    """Strip addresses and URLs from a backend error before showing it to a client."""
    text = _URL.sub("[backend]", str(text or ""))
    text = _ADDRESS.sub("[backend]", text)
    text = "".join(char if char.isprintable() else " " for char in text)
    text = " ".join(text.split())
    return text[:300] or "The model failed."


# ----- request parsing -------------------------------------------------------

@dataclass
class ChatParams:
    model: str = "auto"
    messages: list = field(default_factory=list)
    stream: bool = False
    include_usage: bool = False
    max_tokens: int | None = None
    overrides: dict = field(default_factory=dict)
    stop: list = field(default_factory=list)
    image_count: int = 0
    effort: str | None = None  # reasoning effort level (``off`` ... ``max``) or None for the default

    @property
    def has_system(self) -> bool:
        return any(message["role"] == "system" for message in self.messages)


def _bad(message: str, param: str | None = None, code: str = "invalid_value") -> CompletionError:
    return CompletionError(message, 400, code, param=param)


def _number(body: dict, name: str, low: float, high: float) -> float | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise _bad(f"'{name}' must be a number between {low:g} and {high:g}.", name)
    return float(value)


def _integer(body: dict, name: str, low: int, high: int | None = None) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        limit = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise _bad(f"'{name}' must be an integer {limit}.", name)
    return value


def _flag(body: dict, name: str) -> bool:
    value = body.get(name, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _bad(f"'{name}' must be true or false.", name)
    return value


def _decode_image(url, config, param: str) -> str:
    if not isinstance(url, str):
        raise _bad("'image_url.url' must be a string.", param)
    match = _DATA_URL.match(url.strip())
    if match is None:
        raise _bad("Images must be data: URLs with base64 PNG, JPEG or WebP data; remote URLs are not fetched.",
                   param, "unsupported_image")
    declared, encoded = match.group(1).lower(), "".join(match.group(2).split())
    if len(encoded) > (config.chat_max_image_bytes * 4) // 3 + 4:
        raise _bad(f"An image is larger than {config.chat_max_image_bytes // (1024 * 1024)} MB.", param,
                   "image_too_large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _bad("An image is not valid base64 data.", param, "invalid_image") from None
    if not data or len(data) > config.chat_max_image_bytes:
        raise _bad("An image is empty or too large.", param, "invalid_image")
    if not IMAGE_SIGNATURES[declared](data) and not any(check(data) for check in IMAGE_SIGNATURES.values()):
        raise _bad("An image is not a PNG, JPEG or WebP file.", param, "invalid_image")
    # The model server decodes the image in full: refuse decompression bombs (only the header is read here).
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise _bad("An image is not a PNG, JPEG or WebP file.", param, "invalid_image") from None
    if width * height > config.chat_max_image_pixels:
        raise _bad(f"An image has more than {config.chat_max_image_pixels:,} pixels.", param, "image_too_large")
    return encoded


def _content(message: dict, role: str, index: int, config, images: list) -> str:
    content = message.get("content")
    param = f"messages[{index}].content"
    if isinstance(content, str):
        return content
    if content is None and role == "assistant":
        return ""
    if not isinstance(content, list):
        raise _bad("'content' must be a string or an array of content parts.", param)
    texts = []
    for position, part in enumerate(content):
        part_param = f"{param}[{position}]"
        if not isinstance(part, dict):
            raise _bad("Content parts must be objects.", part_param)
        kind = part.get("type")
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise _bad("A text part needs a 'text' string.", part_param)
            texts.append(text)
        elif kind == "image_url":
            if role != "user":
                raise _bad("Only user messages can contain images.", part_param)
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if len(images) >= config.chat_max_context_images:
                raise _bad(f"A request can contain at most {config.chat_max_context_images} images.", part_param,
                           "too_many_images")
            images.append(_decode_image(url, config, part_param))
        else:
            raise _bad(f"Content parts of type '{kind}' are not supported.", part_param, "unsupported_content")
    return "\n".join(texts)


def parse_chat_request(body, config=None) -> ChatParams:
    """Validate an OpenAI ``chat/completions`` body. Raises :class:`CompletionError`."""
    config = config or current_app.config["BC"]
    if not isinstance(body, dict):
        raise _bad("The request body must be a JSON object.", code="invalid_body")
    for name in ("tools", "functions"):
        if body.get(name):
            raise _bad("Tool and function calls are not supported.", name, "unsupported_parameter")
    if body.get("tool_choice") not in (None, "none"):
        raise _bad("Tool and function calls are not supported.", "tool_choice", "unsupported_parameter")
    if body.get("logprobs"):
        raise _bad("'logprobs' is not supported.", "logprobs", "unsupported_parameter")
    response_format = body.get("response_format")
    if response_format is not None and not (isinstance(response_format, dict)
                                            and response_format.get("type") in (None, "text")):
        raise _bad("Only the 'text' response format is supported.", "response_format", "unsupported_parameter")
    n = body.get("n")
    if n is not None and (isinstance(n, bool) or n != 1):
        raise _bad("Only one choice ('n': 1) is supported.", "n", "unsupported_parameter")

    model = body.get("model", "auto")
    if model is None:
        model = "auto"
    if not isinstance(model, str) or not model.strip() or len(model) > MAX_MODEL_NAME:
        raise _bad("'model' must be a model id from /v1/models or 'auto'.", "model")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _bad("'messages' must be a non-empty array.", "messages")
    if len(raw_messages) > MAX_MESSAGES:
        raise _bad(f"A request can contain at most {MAX_MESSAGES} messages.", "messages", "too_many_messages")
    messages, images_total, characters, image_bytes = [], 0, 0, 0
    for index, message in enumerate(raw_messages):
        param = f"messages[{index}]"
        if not isinstance(message, dict):
            raise _bad("Each message must be an object.", param)
        role = message.get("role")
        if role in ("tool", "function") or message.get("tool_calls") or message.get("function_call"):
            raise _bad("Tool and function calls are not supported.", param, "unsupported_parameter")
        if role == "developer":
            role = "system"
        if role not in ROLES:
            raise _bad("Message roles must be system, user or assistant.", f"{param}.role")
        images: list = []
        content = _content(message, role, index, config, images)
        images_total += len(images)
        if images_total > config.chat_max_context_images:
            raise _bad(f"A request can contain at most {config.chat_max_context_images} images.", param,
                       "too_many_images")
        image_bytes += sum(len(encoded) * 3 // 4 for encoded in images)
        if image_bytes > MAX_CONTEXT_IMAGE_BYTES:
            raise _bad(f"The images of a request may total at most {MAX_CONTEXT_IMAGE_BYTES // 2**20} MB.", param,
                       "images_too_large")
        characters += len(content)
        entry = {"role": role, "content": content}
        if images:
            entry["images"] = images
        messages.append(entry)
    if characters > config.chat_max_context_chars:
        raise _bad(f"The messages are too long (at most {config.chat_max_context_chars:,} characters).",
                   "messages", "context_too_long")

    stream = _flag(body, "stream")
    stream_options = body.get("stream_options")
    if stream_options is not None and not isinstance(stream_options, dict):
        raise _bad("'stream_options' must be an object.", "stream_options")
    include_usage = bool(stream_options and stream_options.get("include_usage") is True)

    limits = [value for value in (_integer(body, "max_completion_tokens", 1), _integer(body, "max_tokens", 1))
              if value is not None]
    max_tokens = min(limits) if limits else None

    overrides = {}
    for name, low, high in (("temperature", 0, 2), ("top_p", 0, 1), ("presence_penalty", -2, 2),
                            ("frequency_penalty", -2, 2)):
        value = _number(body, name, low, high)
        if value is not None:
            overrides[name] = value
    seed = body.get("seed")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise _bad("'seed' must be an integer.", "seed")
        overrides["seed"] = seed % 2**31

    stop = body.get("stop")
    if stop is None:
        stop = []
    elif isinstance(stop, str):
        stop = [stop]
    if not isinstance(stop, list) or len(stop) > MAX_STOP_SEQUENCES or not all(
            isinstance(item, str) and 0 < len(item) <= MAX_STOP_CHARS for item in stop):
        raise _bad(f"'stop' must be a string or up to {MAX_STOP_SEQUENCES} non-empty strings "
                   f"of at most {MAX_STOP_CHARS} characters.", "stop")

    effort = body.get("reasoning_effort")
    if effort is not None:
        if effort not in EFFORTS:
            raise _bad("'reasoning_effort' must be none, minimal, low, medium, high or max.", "reasoning_effort")
        effort = EFFORTS[effort]

    return ChatParams(model=model.strip(), messages=messages, stream=stream, include_usage=include_usage,
                      max_tokens=max_tokens, overrides=overrides, stop=stop, image_count=images_total, effort=effort)


# ----- running -------------------------------------------------------------

def _queue_full(error: Exception) -> CompletionError:
    stats = queue.stats()
    if stats["running"] + stats["waiting"] >= stats["max_depth"]:
        return CompletionError(str(error), 503, "queue_full", retry_after=5)
    return CompletionError(str(error), 429, "too_many_requests", retry_after=10)


def refused(refusal: limits.Refusal, lang: str = "en") -> CompletionError:
    """A limit refusal (``services.limits.admit``) as an API error (English unless *lang* says otherwise)."""
    return CompletionError(refusal.message(lang), refusal.status, refusal.code, retry_after=refusal.retry_after)


def credits_exhausted(budget: credits.Budget) -> CompletionError:
    """``insufficient_quota``: the 5-hour tokens (back when the window ends) or the weekly ones."""
    return refused(limits.pool_refusal(budget))


def effort_locked(error: limits.EffortLocked) -> CompletionError:
    return CompletionError(
        f"The reasoning effort '{error.requested}' is locked for {error.model_name or 'this model'}; the highest "
        f"level you can use is '{error.allowed}'. Ask for more on your account page.", 403,
        "reasoning_effort_locked", param="reasoning_effort")


class CompletionRun:
    """One chat completion from admission to charging.

    Iterate :meth:`events` (``Queued``, ``Started``, ``Delta`` and, on success,
    a final ``Finished``). A request that fails raises :class:`CompletionError`
    instead of yielding ``Finished``. Closing the iterator early (the client
    disconnected) cancels the generation and charges what was produced.

    Limit refusals are worded in *lang* (the playground passes the reader's language; the API keeps English).
    """

    def __init__(self, user, params: ChatParams, *, request_type: str, token_id=None, lang: str = "en"):
        self.user = user
        self.lang = lang
        self.params = params
        self.request_type = request_type
        self.token_id = token_id
        self.cancel = CancelToken()
        self.refusal: CompletionError | None = None
        self.model = None
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.finished: inference.Finished | None = None
        self.usage: tuple[int, int, bool] = (0, 0, False)
        self.tokens_counted = 0.0  # tokens counted against the pool (× the model's weight)
        self.effort: str | None = None
        self.created = int(time.time())
        self._began = time.monotonic()
        self._admitted = False
        self._settled = False
        self._iterator = None
        self._buffer: list = []

        context = AccessContext.load(user)
        try:
            selection = inference.select_model(context, params.model, surface="api", vision=params.image_count > 0,
                                               strict=True)
        except inference.ModelUnavailable as error:
            status, message = error.status, str(error)
            requested = catalog.get_by_name(params.model) if params.model != "auto" else None
            if status == 503 and requested is not None and requested["backend"] == "comfyui":
                # /v1/models lists image models too: naming one here is a client error, not an outage
                # (a 503 makes SDKs retry a request that can never succeed).
                status, message = 400, f"The model '{params.model}' generates images; use /v1/images/generations."
            code = {404: "model_not_found", 403: "model_not_allowed", 400: "model_not_suitable"}.get(
                status, "model_unavailable")
            raise CompletionError(message, status, code, param="model",
                                  retry_after=30 if status == 503 else None) from None
        with limits.snapshot():  # admission, auto and the fallbacks read each limit setting once
            selection = limits.prefer_usable(user, selection, params.model, pool="api", candidates=lambda: (
                inference.candidates(context, "api", vision=params.image_count > 0)))
            model = selection.model
            try:
                self.effort = limits.resolve_effort(user, model, params.effort)
            except limits.EffortLocked as error:
                raise effort_locked(error) from None
            think = inference.think_for(model, self.effort)
            admission = limits.admit(user, "api", model)
            if not admission.allowed:
                raise refused(admission.refusal, lang)
            fallbacks = limits.usable_fallbacks(user, "api", selection.fallbacks, think=think, effort=self.effort)
        messages, options = self._for_model(model)
        self.model = model
        self.request = inference.TextRequest(
            user=user, model=model, messages=messages, options=options, request_type=request_type,
            priority=queue.priority_for(user, slow=admission.slow, api=True),
            owner_key=f"user:{user['id']}:api", fallbacks=fallbacks, think=think, effort=self.effort,
            authorize=self._authorize, prepare=self._for_model)

    def _for_model(self, model) -> tuple[list, dict]:
        """The client's messages with *model*'s system prompt (unless they sent one) and its option defaults."""
        params = self.params
        messages = list(params.messages)
        if not params.has_system and (model["system_prompt"] or "").strip():
            messages.insert(0, {"role": "system", "content": model["system_prompt"]})
        options = inference.build_options(model, params.overrides, max_tokens=params.max_tokens)
        if params.stop:
            options["stop"] = list(params.stop)
        return messages, options

    # admission check, run once the request leaves the queue -----------------
    def _authorize(self) -> str | None:
        user = users.get(self.user["id"])
        if user is None or users.is_suspended(user):
            self.refusal = CompletionError("This account is suspended.", 403, "account_suspended")
        elif self.token_id is not None and not _token_active(user["id"], self.token_id):
            self.refusal = CompletionError("The API token was revoked.", 401, "invalid_api_key")
        elif not (admission := self._admit_again(user)).allowed:
            self.refusal = refused(admission.refusal, self.lang)
        return self.refusal.message if self.refusal else None

    def _admit_again(self, user):
        with limits.snapshot(fresh=True):  # after the queue wait: read everything again
            return limits.admit(user, "api", self.model, take_rate=False)

    # iteration -------------------------------------------------------------
    def events(self):
        """Events not yet consumed, including those read by :meth:`prime`."""
        while self._buffer:
            yield self._buffer.pop(0)
        if self._iterator is None:
            self._iterator = self._run()
        yield from self._iterator

    def prime(self, *, until: tuple) -> None:
        """Read events until one of the types in *until* (errors surface here, before any response)."""
        if self._iterator is None:
            self._iterator = self._run()
        for event in self._iterator:
            self._buffer.append(event)
            if isinstance(event, until):
                return

    def close(self) -> None:
        if self._iterator is not None:
            self._iterator.close()

    def _run(self):
        source = inference.generate(self.request, self.cancel)
        try:
            try:
                for event in source:
                    if isinstance(event, inference.Started):
                        self._admitted = True
                        self.model = event.model
                    elif isinstance(event, inference.Delta):
                        (self.reasoning_parts if event.thinking else self.text_parts).append(event.text)
                    elif isinstance(event, inference.Finished):
                        self.finished = event
                        self.model = event.model or self.model
                        self._settle(event)
                        problem = self._problem(event)
                        if problem is not None:
                            raise problem
                    yield event
            except queue.QueueFull as error:
                raise _queue_full(error) from None
            except queue.QueueTimeout as error:
                raise CompletionError(str(error), 503, "queue_timeout", retry_after=10) from None
            except Cancelled:
                raise CompletionError("The request was cancelled.", 503, "cancelled") from None
        finally:
            if self.finished is None:
                self.cancel.cancel("disconnected")
            source.close()
            if not self._settled:
                self._settle(None)

    def _problem(self, finished: inference.Finished) -> CompletionError | None:
        if finished.state == "completed" or finished.truncated:
            return None
        if self.refusal is not None:
            return self.refusal
        error = finished.error or "The model stopped before finishing its answer."
        if "took too long" in error:
            return CompletionError(error, 504, "timeout")
        if not self.text_parts and not self.reasoning_parts and model_lifecycle.gone(finished.model, error):
            # Deleted (or removed on the server) after the request chose it: the same answer as an unknown model.
            return CompletionError(f"The model '{finished.model['ollama_name']}' is no longer available on the "
                                   "model server.", 404, "model_not_found", param="model")
        if not self.text_parts and not self.reasoning_parts and any(marker in error.lower() for marker in _UNREACHABLE):
            return CompletionError("The inference server is unreachable: " + sanitize_backend_message(error), 503,
                                   "backend_unavailable", retry_after=30)
        return CompletionError("The model could not answer: " + sanitize_backend_message(error), 502,
                               "upstream_error")

    # accounting ------------------------------------------------------------
    @property
    def produced(self) -> str:
        return "".join(self.reasoning_parts) + "".join(self.text_parts)

    @property
    def finish_reason(self) -> str:
        finished = self.finished
        if finished is None:
            return "stop"
        if finished.truncated:
            return "length"
        return finished.finish_reason if finished.finish_reason in ("stop", "length") else "stop"

    def _settle(self, finished: inference.Finished | None) -> None:
        """Charge the request once: exact usage when known, an estimate for interrupted answers."""
        if self._settled:
            return
        self._settled = True
        if not self._admitted or self.model is None:
            return
        produced = self.produced
        completed = finished is not None and finished.state == "completed"
        if completed:
            prompt, completion, estimated = finished.prompt_tokens, finished.completion_tokens, finished.usage_estimated
        else:
            prompt = sum(credits.estimate_tokens(str(m.get("content", ""))) for m in self.request.messages)
            completion, estimated = credits.estimate_tokens(produced), True
        self.usage = (prompt, completion, estimated)
        # Metrics durations include the queue wait (as for chat and images).
        duration = finished.duration_ms + finished.wait_ms if finished is not None \
            else int((time.monotonic() - self._began) * 1000)
        wait = finished.wait_ms if finished is not None else 0
        status = "ok" if completed or (finished is not None and finished.truncated) else (
            "stopped" if produced else "error")
        try:
            if not completed and not produced:
                # Nothing was produced: record the failure, charge nothing.
                api_usage.record_metric(request_type=self.request_type, user_id=self.user["id"],
                                        model_id=self.model["id"], duration_ms=duration, queue_wait_ms=wait,
                                        status="error")
                self.usage = (0, 0, False)
                return
            self.tokens_counted = api_usage.charge_text(
                user_id=self.user["id"], request_type=self.request_type, model_id=self.model["id"],
                token_id=self.token_id, prompt_tokens=prompt, completion_tokens=completion, usage_estimated=estimated,
                duration_ms=duration, queue_wait_ms=wait, status=status)
        except Exception:  # noqa: BLE001 - never lose the answer because accounting failed
            log.exception("Recording usage for a %s request failed", self.request_type)



def _token_active(user_id: str, token_id) -> bool:
    return any(row["id"] == token_id for row in tokens.list_for(user_id))
