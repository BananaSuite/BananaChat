"""Bounded, metered text inference shared by the API and playground."""

import logging
import threading
import time

import config
import db
from db._inference_usage import record_usage
from services import chat_routing, dispatcher, model_access, ollama, queue as q

_logger = logging.getLogger("bananachat.inference")


class InferenceError(RuntimeError):
    """A safe, actionable error that may be returned to a caller."""


def validate_request(body):
    if not isinstance(body, dict):
        raise ValueError("The request body must be a JSON object.")
    model, messages = body.get("model"), body.get("messages")
    if not isinstance(model, str) or not model or len(model) > 512:
        raise ValueError("model must be a nonempty model identifier.")
    if not isinstance(messages, list) or not 1 <= len(messages) <= config.CHAT_MAX_HISTORY_MESSAGES:
        raise ValueError(f"messages must contain between 1 and {config.CHAT_MAX_HISTORY_MESSAGES} text messages.")
    total = 0
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)):
            raise ValueError("Each message needs a system, user or assistant role and text content.")
        if set(message) - {"role", "content", "name"}:
            raise ValueError("This endpoint supports text messages. Use the chat attachment interface for files and images.")
        total += len(message["content"])
    if total > config.CHAT_MAX_CONTEXT_CHARS:
        raise ValueError(f"Message context exceeds {config.CHAT_MAX_CONTEXT_CHARS} characters.")
    if not isinstance(body.get("stream", False), bool):
        raise ValueError("stream must be true or false.")
    if body.get("tools") or body.get("functions"):
        raise ValueError("Tool calling is not supported by this text endpoint.")
    for key in ("max_tokens", "max_completion_tokens"):
        if key in body and (isinstance(body[key], bool) or not isinstance(body[key], int) or body[key] < 1):
            raise ValueError(key + " must be a positive integer.")
    return model, [{"role": item["role"], "content": item["content"]} for item in messages]


def options_for(model, body):
    options = chat_routing.options_for(model, body)
    requested = body.get("max_completion_tokens", body.get("max_tokens"))
    if requested is not None:
        options["num_predict"] = min(requested, config.MAX_OUTPUT_TOKENS)
    return options


class TextInference:
    """Reserve capacity before HTTP acceptance; meter before announcing success."""
    def __init__(self, user, model_id, model_name, messages, options, priority,
                 *, token_id=None, request_type="api"):
        self.user, self.model_id, self.model_name = dict(user), model_id, model_name
        self.messages, self.options = messages, options
        self.token_id, self.request_type = token_id, request_type
        self.stop_event = threading.Event()
        self.slot = q.acquire(priority=priority, timeout=120, stop_ev=self.stop_event,
                              owner_key=None if user["role"] == "admin" else str(user["id"]) + ":api")
        self.started = time.monotonic()
        self.tokens_in = self.tokens_out = self.output_chars = self.output_bytes = 0
        self.finish_reason = "stop"
        self._record_attempted = False
        self._backend_completed = False
        self._iterator = None
        self._claimed = False

    def close(self):
        self.stop_event.set()
        self.slot.close()

    def _check(self):
        self.slot.check()
        if time.monotonic() - self.started > config.GENERATION_TIMEOUT:
            raise InferenceError("Inference reached its time limit. Please retry with a shorter request.")

    def _authorize(self):
        db.check_suspension_expired(self.user["id"])
        user = db.get_user_by_id(self.user["id"])
        model = db.get_model_by_id(self.model_id)
        if not user or user["suspended"] or not model or not model_access.can_user_access_model(user, model, "api") or not model_access.is_ollama_text_model(model):
            raise InferenceError("This model is no longer available to your account.")
        if self.token_id is not None:
            with db.get_db_context() as conn:
                token = conn.execute("SELECT revoked FROM api_tokens WHERE id=? AND user_id=?", (self.token_id, user["id"])).fetchone()
            if not token or token["revoked"]:
                raise InferenceError("The API token is no longer active.")
        if not db.check_credits_available(user["id"], role=user["role"])[0]:
            raise InferenceError("Daily credit limit reached. Try again tomorrow.")
        self.user = dict(user)
        self.model_name = model["backend_model_name"]

    def _record(self, status):
        if self._record_attempted or not self._claimed:
            return
        self._record_attempted = True
        estimated = not self._backend_completed and self.output_chars > 0
        if estimated:
            self.tokens_out = max(1, (self.output_chars + 3) // 4)
            self.tokens_in = sum(len(message["content"]) for message in self.messages) // 4
        record_usage(
            self.user["id"], self.model_id, self.token_id, self.request_type,
            self.tokens_in, self.tokens_out, int((time.monotonic() - self.started) * 1000),
            self.slot.wait_ms, status, usage_estimated=estimated,
        )

    def stream(self):
        status = "error"
        try:
            with self.slot:
                self._claimed = True
                try:
                    self._authorize()
                    remote = dispatcher.should_use_worker(self.user["role"] == "admin", self.model_name)
                    if remote:
                        job = dispatcher.create_worker_job(self.model_name, self.messages, self.options,
                            q.PRIORITY_ADMIN if self.user["role"] == "admin" else q.PRIORITY_API)
                        iterator = dispatcher.stream_from_worker(job, stop_ev=self.stop_event)
                    else:
                        iterator = ollama.generate_chat_stream(self.model_name, self.messages, options=self.options)
                    self._iterator = iter(iterator)
                    for chunk, done, usage in self._iterator:
                        self._check()
                        if not isinstance(chunk, str):
                            raise InferenceError("The inference server returned invalid text.")
                        self.output_bytes += len(chunk.encode("utf-8"))
                        self.output_chars += len(chunk)
                        if self.output_bytes > config.CHAT_MAX_RESPONSE_BYTES:
                            raise InferenceError("Response exceeded its size limit. Please request a shorter response.")
                        if chunk:
                            yield chunk, False, {}
                        if done:
                            for key in ("prompt_tokens", "completion_tokens"):
                                value = usage.get(key, 0)
                                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
                                    raise InferenceError("The inference server returned invalid usage.")
                            self.tokens_in = usage.get("prompt_tokens", 0)
                            self.tokens_out = usage.get("completion_tokens", 0)
                            self._backend_completed = True
                            self.finish_reason = usage.get("finish_reason", "stop")
                            if self.finish_reason not in {"stop", "length"}:
                                self.finish_reason = "stop"
                            status = "ok"
                            self._record(status)
                            yield "", True, {"prompt_tokens": self.tokens_in, "completion_tokens": self.tokens_out,
                                             "finish_reason": self.finish_reason}
                            return
                    raise InferenceError("The inference server ended the response before completing it.")
                except GeneratorExit:
                    status = "interrupted"
                    raise
                finally:
                    try:
                        close = getattr(self._iterator, "close", None)
                        if close:
                            close()
                    finally:
                        self._record(status)
        except (InferenceError, q.QueueCancelledError, TimeoutError):
            raise
        except Exception as error:
            _logger.exception("Text inference failed")
            raise InferenceError("The inference service could not complete this request. Please retry.") from error
        finally:
            self.close()

    def collect(self):
        pieces = []
        for chunk, _, _ in self.stream():
            if chunk:
                pieces.append(chunk)
        return "".join(pieces), self.tokens_in, self.tokens_out
