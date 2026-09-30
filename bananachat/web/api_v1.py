"""OpenAI-compatible API under ``/v1`` (bearer ``bc-...`` tokens).

Endpoints: ``GET /v1/models``, ``GET /v1/models/<id>``,
``POST /v1/chat/completions`` and, when image generation is enabled,
``POST /v1/images/generations``. See ``docs/api.md``.

Every error, including 404/405/413 and failures raised outside the views
(maintenance mode, outages), uses the OpenAI envelope
``{"error": {"message", "type", "param", "code"}}``.
"""

from __future__ import annotations

import json
import logging
import secrets
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context

from bananachat import db, security
from bananachat.db import tokens, users
from bananachat.services import inference, limits, status
from bananachat.services.access import AccessContext, usable_models
from bananachat.services.completions import CompletionError, CompletionRun, parse_chat_request
from bananachat.services.images import ImageError, ImageJob

bp = Blueprint("api_v1", __name__, url_prefix="/v1")
log = logging.getLogger("bananachat.api")

BODY_LIMIT = 4 * 1024 * 1024
INVALID_TOKEN_LIMIT = 30  # failed authentications per address and minute
ROUTES = {"models": ("GET",), "chat/completions": ("POST",), "images/generations": ("POST",)}
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


# ----- errors ---------------------------------------------------------------------

def error_type(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


def api_error(message: str, status: int = 400, code: str | None = None, *, param: str | None = None,
              retry_after: int | None = None):
    response = jsonify({"error": {"message": message, "type": error_type(status), "param": param,
                                  "code": code}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    return response


def _from_exception(error):
    return api_error(error.message, error.status, error.code, param=error.param, retry_after=error.retry_after)


@bp.after_request
def normalize_errors(response):
    """Give errors produced outside this module (413, maintenance, 500...) the OpenAI envelope.

    Authenticated responses also carry the account's request-rate state
    (``x-ratelimit-*-requests``, the tightest rule) and its 5-hour token window
    (``x-ratelimit-*-tokens``).
    """
    response.headers.setdefault("Cache-Control", "no-store")
    decision = getattr(g, "api_rate", None)
    if decision is not None:
        for name, value in decision.headers().items():
            response.headers[name] = value
    user = getattr(g, "api_user", None) or getattr(g, "api_rate_user", None)
    if user is not None:
        try:
            for name, value in limits.token_headers(user).items():
                response.headers[name] = value
        except Exception:  # noqa: BLE001 - headers are informative; never fail the response for them
            log.warning("Could not compute the token-window headers", exc_info=True)
    if response.status_code < 400 or response.is_streamed or not response.is_json:
        return response
    data = response.get_json(silent=True)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and "type" not in error:
        message = error.get("message") or "The request failed."
        envelope = {"error": {"message": message, "type": error_type(response.status_code), "param": None,
                              "code": error.get("code")}}
        response.set_data(json.dumps(envelope))
    return response


@bp.route("/", defaults={"path": ""}, methods=ALL_METHODS)
@bp.route("/<path:path>", methods=ALL_METHODS)
def unknown(path):
    """Catch-all so unknown paths and wrong methods get the API's error format."""
    path = path.strip("/")
    allowed = ROUTES.get(path) or (("GET",) if path.startswith("models/") else None)
    # A supported method here means the path itself is wrong (e.g. a trailing slash).
    if allowed and request.method not in allowed and not (request.method == "HEAD" and "GET" in allowed):
        response = api_error(f"Use {' or '.join(allowed)} for /v1/{path}.", 405, "method_not_allowed")
        response.headers["Allow"] = ", ".join(allowed)
        return response
    return api_error(f"Unknown endpoint: {request.method} /v1/{path}.", 404, "unknown_endpoint")


# ----- authentication and throttling ----------------------------------------------

def authenticated(view):
    """Resolve the bearer token to ``g.api_token``/``g.api_user`` and apply the account's request rate."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
        token = user = None
        if scheme.lower() == "bearer" and raw.strip():
            token, user = tokens.authenticate(raw.strip())
        if token is None:
            if not users.hit(f"api-auth-fail:{security.client_key()}", INVALID_TOKEN_LIMIT, 60):
                return api_error("Too many requests with invalid API tokens. Wait a minute.", 429,
                                 "rate_limit_exceeded", retry_after=60)
            message = "Invalid API token." if raw.strip() else \
                "Missing API token. Send 'Authorization: Bearer bc-...'."
            response = api_error(message, 401, "invalid_api_key")
            response.headers["WWW-Authenticate"] = 'Bearer realm="api"'
            return response
        if users.is_suspended(user):
            return api_error("This account is suspended.", 403, "account_suspended")
        if user["suspended"] and users.lift_expired_suspension(user):
            user = users.get(user["id"])
        decision = limits.check_rate(user, "api")
        g.api_rate, g.api_rate_user = decision, user
        if not decision.allowed:
            rate = limits.rule_text("en", decision.rule)
            return api_error(f"Rate limit reached: at most {rate}. Retry in {decision.retry_after} s.", 429,
                             "rate_limit_exceeded", retry_after=decision.retry_after)
        tokens.touch(token["id"])
        g.api_token, g.api_user = token, user
        return view(*args, **kwargs)
    return wrapper


def json_body() -> dict:
    """The request body as a JSON object (strict: 415 for other types, 400 for invalid JSON)."""
    if not request.is_json:
        raise CompletionError("Send a JSON body with 'Content-Type: application/json'.", 415,
                              "unsupported_media_type")

    def reject(constant):
        raise ValueError(f"{constant} is not valid JSON")

    try:
        body = json.loads(request.get_data(cache=False) or b"", parse_constant=reject)
    except ValueError:
        raise CompletionError("The request body is not valid JSON.", 400, "invalid_json") from None
    if not isinstance(body, dict):
        raise CompletionError("The request body must be a JSON object.", 400, "invalid_body")
    return body


# ----- models -----------------------------------------------------------------------

def _model_object(model) -> dict:
    created = db.parse_timestamp(model["created_at"])
    return {"id": model["ollama_name"], "object": "model", "created": int(created.timestamp()) if created else 0,
            "owned_by": "bananachat"}


def _usable(user) -> list:
    config = current_app.config["BC"]
    # Administrators may name a model waiting for review, so it is listed for them (never chosen by auto).
    return usable_models(AccessContext.load(user), "api", kind="any", images_enabled=config.images_enabled,
                         unreviewed=True)


@bp.get("/models")
@authenticated
def list_models():
    data = [{"id": "auto", "object": "model", "created": 0, "owned_by": "bananachat"}]
    data.extend(_model_object(model) for model in _usable(g.api_user))
    return jsonify({"object": "list", "data": data})


@bp.get("/models/<path:model_id>")
@authenticated
def get_model(model_id):
    if model_id == "auto":
        return jsonify({"id": "auto", "object": "model", "created": 0, "owned_by": "bananachat"})
    for model in _usable(g.api_user):
        if model["ollama_name"] == model_id:
            return jsonify(_model_object(model))
    return api_error(f"The model '{model_id}' does not exist or is not available to you.", 404, "model_not_found",
                     param="model")


# ----- chat completions -------------------------------------------------------------

@bp.post("/chat/completions")
@security.body_limit(BODY_LIMIT)
@authenticated
@security.long_request
def chat_completions():
    blocked = status.guard(g.api_user, openai=True)  # maintenance or AI-server outage
    if blocked is not None:
        return blocked
    run = None
    try:
        params = parse_chat_request(json_body())
        run = CompletionRun(g.api_user, params, request_type="api", token_id=g.api_token["id"])
        # Wait for admission and the first output, so queue and model problems are plain HTTP errors.
        run.prime(until=(inference.Delta, inference.Finished))
        if params.stream:
            response = Response(stream_with_context(_stream(run, params)), mimetype="text/event-stream")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Accel-Buffering"] = "no"
            run = None  # the stream owns it now
            return response
        for _event in run.events():
            pass
        return jsonify(_completion(run))
    except CompletionError as error:
        return _from_exception(error)
    finally:
        if run is not None:
            run.close()


def _completion(run: CompletionRun) -> dict:
    message = {"role": "assistant", "content": "".join(run.text_parts)}
    if run.reasoning_parts:
        message["reasoning_content"] = "".join(run.reasoning_parts)
    prompt, completion, _estimated = run.usage
    return {"id": "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion", "created": run.created,
            "model": run.model["ollama_name"],
            "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": run.finish_reason}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


def _sse(payload) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _stream(run: CompletionRun, params):
    completion_id = "chatcmpl-" + secrets.token_hex(12)

    def chunk(delta, finish_reason=None):
        return _sse({"id": completion_id, "object": "chat.completion.chunk", "created": run.created,
                     "model": run.model["ollama_name"],
                     "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}]})

    try:
        yield chunk({"role": "assistant", "content": ""})
        for event in run.events():
            if isinstance(event, inference.Delta):
                yield chunk({"reasoning_content": event.text} if event.thinking else {"content": event.text})
            elif isinstance(event, inference.Finished):
                yield chunk({}, run.finish_reason)
        if params.include_usage:
            prompt, completion, _estimated = run.usage
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": run.created,
                        "model": run.model["ollama_name"], "choices": [],
                        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                  "total_tokens": prompt + completion}})
    except CompletionError as error:
        yield _sse({"error": {"message": error.message, "type": error_type(error.status), "param": error.param,
                              "code": error.code}})
    finally:
        run.close()
    yield "data: [DONE]\n\n"


# ----- images -----------------------------------------------------------------------

@bp.post("/images/generations")
@security.body_limit(BODY_LIMIT)
@authenticated
@security.long_request
def image_generations():
    blocked = status.guard(g.api_user, openai=True)
    if blocked is not None:
        return blocked
    try:
        body = json_body()
    except CompletionError as error:
        return _from_exception(error)
    count = body.get("n", 1)
    if count is not None and (isinstance(count, bool) or count != 1):
        return api_error("Only one image per request ('n': 1) is supported.", 400, "unsupported_parameter", param="n")
    if body.get("response_format") not in (None, "b64_json"):
        return api_error("Only 'response_format': 'b64_json' is supported.", 400, "unsupported_parameter",
                         param="response_format")
    job = None
    try:
        job = ImageJob(g.api_user, prompt=body.get("prompt"), model=body.get("model", "auto"), size=body.get("size"),
                       token_id=g.api_token["id"])
        result = job.run()
    except ImageError as error:
        return _from_exception(error)
    finally:
        if job is not None:
            job.close()
    tokens_out = int(result.tokens)
    return jsonify({
        "created": result.created, "model": result.model["ollama_name"],
        "data": [{"b64_json": result.b64_json}],
        "output_format": result.mime_type.split("/", 1)[1], "size": f"{result.width}x{result.height}",
        "usage": {"input_tokens": 0, "output_tokens": tokens_out, "total_tokens": tokens_out},
    })
