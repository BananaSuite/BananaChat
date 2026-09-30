"""The Images page (only when ``BC_IMAGE_BACKEND=comfyui``).

``POST /images/generate`` takes ``{"prompt", "model", "size"}`` and runs the
same service as ``POST /v1/images/generations`` (same limits, same charge).
With ``Accept: text/event-stream`` it streams progress events and ends with
the image; otherwise it answers with JSON once the image is ready.
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, abort, current_app, g, jsonify, render_template, request, \
    stream_with_context, url_for

from bananachat import security
from bananachat.i18n import translate
from bananachat.services import images, limits, status
from bananachat.services.images import ImageError, ImageJob

bp = Blueprint("images", __name__)


def _enabled() -> bool:
    return current_app.config["BC"].images_enabled


@bp.get("/images", endpoint="index")
@security.login_required
def index():
    if not _enabled():
        abort(404)
    from bananachat.web.developer import credit_summary

    config = current_app.config["BC"]
    models = images.image_models(g.user, config)
    data = {"generateUrl": url_for("images.generate"), "cost": config.image_tokens_per_generation,
            "rpm": config.image_generation_rpm}
    return render_template("images/index.html", models=models, sizes=images.SIZE_PRESETS,
                           default_size=images.DEFAULT_SIZE, summary=credit_summary(g.user),
                           cost=config.image_tokens_per_generation, rpm=config.image_generation_rpm, data=data,
                           paused=status.inference_block() is not None)


def _error(error: ImageError):
    response = security.json_error(error.message, error.status, error.code)
    if error.retry_after:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


def _event(payload: dict) -> str:
    return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"


def _result(result: images.ImageResult) -> dict:
    return {"b64_json": result.b64_json, "mime_type": result.mime_type, "width": result.width,
            "height": result.height, "tokens": result.tokens, "created": result.created,
            "model": {"id": result.model["ollama_name"], "name": result.model["display_name"]},
            "duration_ms": result.duration_ms}


@bp.post("/images/generate", endpoint="generate")
@security.login_required
@security.long_request
def generate():
    if not _enabled():
        abort(404)
    blocked = status.guard()  # maintenance or AI-server outage
    if blocked is not None:
        return blocked
    # Like the playground, the Images page shares the API's request rate.
    decision = limits.check_rate(g.user, "api")
    if not decision.allowed:
        response = security.json_error(
            translate(g.lang, "developer.rate_limited", rate=limits.rule_text(g.lang, decision.rule),
                      seconds=decision.retry_after), 429, "rate_limited")
        response.headers["Retry-After"] = str(decision.retry_after)
        return response
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return security.json_error("Send a JSON object.", 400, "bad_request")
    job = None
    try:
        job = ImageJob(g.user, prompt=body.get("prompt"), model=body.get("model") or "auto", size=body.get("size"),
                       lang=g.lang)
        job.prime()  # rate limit, credits and queue admission errors become plain HTTP errors
    except ImageError as error:
        if job is not None:
            job.close()
        return _error(error)
    if "text/event-stream" in (request.headers.get("Accept") or ""):
        response = Response(stream_with_context(_events(job)), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response
    try:
        result = job.run()
    except ImageError as error:
        return _error(error)
    finally:
        job.close()
    response = jsonify(_result(result))
    response.headers["Cache-Control"] = "no-store"
    return response


def _events(job: ImageJob):
    try:
        for event in job.events():
            if isinstance(event, images.Queued):
                yield _event({"type": "queued", "position": event.position})
            elif isinstance(event, images.Admitted):
                yield _event({"type": "started"})
            elif isinstance(event, images.Progress):
                yield _event({"type": "progress", "state": event.state})
            elif isinstance(event, images.ImageResult):
                yield _event({"type": "done", "image": _result(event)})
    except ImageError as error:
        yield _event({"type": "error", "message": error.message, "code": error.code})
    finally:
        job.close()
