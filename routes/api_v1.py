"""OpenAI-compatible API endpoint at /v1/chat/completions.

Authentication: Bearer token (API token).
Credit system: 1 credit = 1000 tokens.
"""

import json
import secrets
import time

from flask import request, jsonify, Response, Blueprint

import db
from services.http_capacity import limit_inference
import config
from services import (
    comfyui, ollama, queue as q, model_access, image_generation, inference,
)
from logger import log_action

api_v1 = Blueprint("api_v1", __name__, url_prefix="/v1")


def _get_token_user():
    """Authenticate via Bearer token. Returns (token_row, user_row) or (None, None)."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None, None
    raw_token = auth[7:].strip()
    if not raw_token:
        return None, None
    token_hash = db.hash_token(raw_token)
    token_row = db.get_token_by_hash(token_hash)
    if not token_row:
        return None, None
    # Auto-unsuspend if timed suspension has elapsed (mirrors web UI behavior)
    db.check_suspension_expired(token_row["user_id"])
    user_row = db.get_user_by_id(token_row["user_id"])
    if not user_row or user_row["suspended"]:
        return None, None
    return token_row, user_row


def _error(msg, code=400):
    return jsonify({"error": {"message": msg, "type": "invalid_request_error"}}), code


@api_v1.route("/models", methods=["GET"])
def list_models():
    token_row, user_row = _get_token_user()
    if not token_row:
        return _error("Invalid or missing API token", 401)
    models = model_access.list_accessible_models(
        user_row, "api", include_image=True
    )
    data = [
        {
            "id": m["ollama_name"],
            "object": "model",
            "created": 0,
            "owned_by": "local",
        }
        for m in models
    ]
    data.insert(0, {"id": "auto", "object": "model", "created": 0, "owned_by": "local"})
    return jsonify({"object": "list", "data": data})


@api_v1.route("/images/generations", methods=["POST"])
@limit_inference
def image_generations():
    """Generate one base64 image through the configured ComfyUI backend."""
    token_row, user_row = _get_token_user()
    if not token_row:
        return _error("Invalid or missing API token", 401)
    db.touch_token(token_row["id"])
    if not comfyui.is_enabled():
        return _error("Image generation is not configured", 503)

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not all(isinstance(body.get(key, default), str) for key, default in (
        ("prompt", ""), ("model", "auto"), ("response_format", "b64_json"), ("size", "1024x1024"),
    )):
        return _error("Use a JSON object with text prompt, model, size and response_format fields")
    prompt = body.get("prompt", "").strip()
    model_name = body.get("model", "auto").strip() or "auto"
    response_format = body.get("response_format", "b64_json")
    size = body.get("size", "1024x1024")
    image_count = body.get("n", 1)
    if type(image_count) is not int:
        return _error("n must be an integer")
    if not prompt:
        return _error("prompt is required")
    if len(prompt) > 10_000:
        return _error("prompt is too long (maximum 10,000 characters)")
    if image_count != 1:
        return _error("Image generation currently supports n=1")
    if response_format != "b64_json":
        return _error("Only response_format='b64_json' is supported")

    is_admin = user_row["role"] == "admin"
    if not image_generation.check_rate_limit(user_row):
        return _error("Image generation rate limit exceeded", 429)

    reservation_id = None
    try:
        model = image_generation.resolve_model(user_row, model_name)
        if is_admin:
            priority = q.PRIORITY_ADMIN
        else:
            reservation_id, is_slow = db.reserve_image_credits(
                user_row["id"], config.IMAGE_CREDITS_PER_GENERATION,
                model_id=model["id"], token_id=token_row["id"],
            )
            priority = q.PRIORITY_SLOW if is_slow else q.PRIORITY_API
        def authorize_image():
            current_token, current_user = _get_token_user()
            if not current_token:
                raise image_generation.ImageGenerationError("This API token can no longer generate images", 403)
            current_model = image_generation.resolve_model(current_user, model["ollama_name"])
            if current_model["id"] != model["id"] or current_model["backend_model_name"] != model["backend_model_name"]:
                raise image_generation.ImageGenerationError("The model configuration changed. Please retry.", 503)
        generation_kwargs = {"size": size, "owner_key": None if is_admin else user_row["id"] + ":api", "authorization_check": authorize_image}
        if reservation_id is not None:
            generation_kwargs["reservation_heartbeat"] = lambda: (
                db.touch_image_credit_reservation(
                    reservation_id, user_row["id"]
                )
            )
        result = image_generation.generate(
            model, prompt, priority, **generation_kwargs
        )
        with db.get_db_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if reservation_id is not None:
                db.finalize_image_credit_reservation(reservation_id, user_row["id"], connection=conn)
            if is_admin:
                db.deduct_credits(user_row["id"], 0, image_generation.billable_tokens(result),
                    model_id=model["id"], token_id=token_row["id"], request_type="api", connection=conn)
            db.record_request_metric(request_type="image", model_id=model["id"], user_id=user_row["id"],
                tokens_in=result["tokens_in"], tokens_out=result["tokens_out"], duration_ms=result["duration_ms"],
                queue_wait_ms=result["wait_ms"], status="ok", connection=conn)
            conn.commit()
    except db.InsufficientImageCreditsError as exc:
        if reservation_id is not None:
            db.refund_image_credit_reservation(
                reservation_id, user_row["id"]
            )
        return _error(str(exc), 429)
    except image_generation.ImageGenerationError as exc:
        if reservation_id is not None:
            db.refund_image_credit_reservation(
                reservation_id, user_row["id"]
            )
        return _error(str(exc), exc.status_code)
    except db.ImageCreditReservationError:
        return _error("Image generation billing could not be completed", 500)

    log_action(
        "image_generate", request, user=user_row, model=model["ollama_name"]
    )
    return jsonify({
        "created": int(time.time()),
        "model": model["ollama_name"],
        "data": [
            {"b64_json": image["b64_json"]} for image in result["images"]
        ],
        "usage": {
            "prompt_tokens": result["tokens_in"],
            "completion_tokens": result["tokens_out"],
            "total_tokens": result["tokens_in"] + result["tokens_out"],
        },
    })


@api_v1.route("/chat/completions", methods=["POST"])
@limit_inference
def chat_completions():
    # Tokens belonging to one account share its allowance across workers.
    # Browser cookies and shared client IPs do not identify API ownership.
    token_row, user_row = _get_token_user()
    _rpm_settings = db.get_site_settings() or {}
    _api_rpm = _rpm_settings.get("api_rpm")
    if _api_rpm is None:
        _api_rpm = 60  # default
    if _api_rpm > 0:
        if token_row:
            _rpm_key = f"api_rpm:user:{user_row['id']}"
        else:
            from helpers._rate_limiting import _current_client_key
            _rpm_key = f"api_rpm:invalid:{_current_client_key()}"
        _allowed = db.check_rate_limit(_rpm_key, _api_rpm, 60)
        if not _allowed:
            return _error("Rate limit exceeded", 429)

    if not token_row:
        return _error("Invalid or missing API token", 401)

    db.touch_token(token_row["id"])

    body = request.get_json(silent=True)
    try:
        model_name, messages = inference.validate_request(body)
    except ValueError as error:
        return _error(str(error))
    stream = body.get("stream", False)

    is_admin = user_row["role"] == "admin"

    if model_name == "auto":
        if user_row["role"] != "admin" and not model_access.list_accessible_models(
            user_row, "api"
        ):
            return _error("No models are available to your account", 403)
        auto, auto_err = ollama.select_auto_model(user=user_row, surface="api")
        if not auto:
            return _error(auto_err, 503)
        model_name = auto["ollama_name"]
        backend_model_name = auto["backend_model_name"]
        model_id = auto["id"]
        model_row = auto
    else:
        model_row = db.get_model_by_ollama_name(model_name)
        if not model_row:
            return _error(f"Model '{model_name}' not found", 404)
        if not model_access.can_user_access_model(user_row, model_row, "api"):
            return _error(f"Model '{model_name}' is not available to your account", 403)
        if not model_access.is_ollama_text_model(model_row):
            return _error(
                f"Model '{model_name}' is not an available Ollama text model",
                400,
            )
        model_id = model_row["id"]
        backend_model_name = model_row["backend_model_name"]

    options = inference.options_for(model_row, body)

    # Prepend model system prompt if no system message in request
    model_sys_prompt = db.get_model_system_prompt(model_row)
    if model_sys_prompt and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": model_sys_prompt}] + list(messages)

    if not is_admin:
        ok, is_slow, reg_used, slow_used, reg_limit, slow_limit = db.check_credits_available(
            user_row["id"], role=user_row["role"]
        )
        if not ok:
            return _error(
                f"Credit limit reached ({reg_limit} regular + {slow_limit} slow credits/day). "
                "Try again tomorrow.",
                429,
            )
        priority = q.PRIORITY_SLOW if is_slow else q.PRIORITY_API
    else:
        priority = q.PRIORITY_ADMIN
        is_slow = False

    if stream:
        return _stream_response(
            user_row, token_row, model_name, model_id, messages, priority, is_slow,
            options, backend_model_name=backend_model_name,
        )
    else:
        return _blocking_response(
            user_row, token_row, model_name, model_id, messages, priority, is_slow,
            options, backend_model_name=backend_model_name,
        )


def _blocking_response(user_row, token_row, model_name, model_id, messages, priority, is_slow, options=None, backend_model_name=None):
    log_action("api_request", request, user=user_row, model=model_name, stream="false")
    try:
        work = inference.TextInference(user_row, model_id, backend_model_name or model_name,
            messages, options, priority, token_id=token_row["id"])
        content, tokens_in, tokens_out = work.collect()
    except q.QueueFullError as error:
        return (*_error(str(error), 503), {"Retry-After": "5"})
    except TimeoutError:
        return _error("Inference timed out. Please retry.", 504)
    except (inference.InferenceError, q.QueueCancelledError) as error:
        return _error(str(error), 503)
    return jsonify({
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion", "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": work.finish_reason}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                  "total_tokens": tokens_in + tokens_out},
    })


def _stream_response(user_row, token_row, model_name, model_id, messages, priority, is_slow, options=None, backend_model_name=None):
    log_action("api_request", request, user=user_row, model=model_name, stream="true")
    try:
        work = inference.TextInference(user_row, model_id, backend_model_name or model_name,
            messages, options, priority, token_id=token_row["id"])
    except q.QueueFullError as error:
        return (*_error(str(error), 503), {"Retry-After": "5"})
    completion_id = "chatcmpl-" + secrets.token_hex(12)

    def generate():
        iterator = work.stream()
        try:
            for chunk, done, usage in iterator:
                payload = {
                    "id": completion_id, "object": "chat.completion.chunk", "model": model_name,
                    "choices": [{"index": 0,
                        "delta": {"role": "assistant", "content": chunk} if chunk else {},
                        "finish_reason": usage.get("finish_reason", "stop") if done else None}],
                }
                if done:
                    payload["usage"] = {"prompt_tokens": work.tokens_in, "completion_tokens": work.tokens_out,
                                        "total_tokens": work.tokens_in + work.tokens_out}
                yield "data: " + json.dumps(payload) + "\n\n"
            yield "data: [DONE]\n\n"
        except (inference.InferenceError, q.QueueCancelledError, TimeoutError) as error:
            payload = {"error": {"message": str(error), "type": "server_error"}}
            yield "data: " + json.dumps(payload) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            iterator.close()
            work.close()

    response = Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"})
    response.call_on_close(work.close)
    return response


def register_api_v1(app):
    app.register_blueprint(api_v1)
