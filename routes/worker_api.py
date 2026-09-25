"""Worker node API: served at /worker/v1/…

Personal-PC daemons (Windows or Linux) authenticate with a per-worker
Bearer token, long-poll for inference jobs, stream chunks back, and
periodically report their heartbeat.  All requests are outbound from the
PC to this server, so NAT traversal is not required.

Authentication
--------------
Every request must carry:
    Authorization: Bearer <worker_token>

The token is created once in the Admin → Workers panel and written into the
worker's configuration file.  Only its SHA-256 hash is stored here.
"""

import json
import logging
import math
import time

from flask import Blueprint, request, jsonify

import db

_logger = logging.getLogger("bananachat.worker_api")

worker_api = Blueprint(
    "worker_api", __name__,
    url_prefix="/worker/v1",
)

# Maximum seconds a /jobs/poll request will block waiting for work.
# Slightly under a typical 30-s reverse-proxy read timeout.
_LONG_POLL_TIMEOUT = 28
_LONG_POLL_SLEEP   = 1.0   # seconds between DB checks while waiting


def _auth_worker():
    """Verify the Bearer token and return the worker row, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    raw = auth[7:].strip()
    if not raw:
        return None
    return db.get_worker_by_token(raw)


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# Heartbeat: workers call this every ~10 s to stay "online"

@worker_api.route("/heartbeat", methods=["POST"])
def heartbeat():
    worker = _auth_worker()
    if not worker:
        return _err("Unauthorized", 401)

    if worker["status"] == "disabled":
        return _err("This worker has been disabled by an administrator", 403)

    request.max_content_length = 256 * 1024
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Expected a JSON object")
    if body.get("status", "online") not in {"online", "busy", "offline"}:
        return _err("Invalid worker status")
    for name in ("gpu_name", "ollama_version"):
        if body.get(name) is not None and (not isinstance(body[name], str) or len(body[name]) > 512):
            return _err("Invalid worker description")
    caps = body.get("capabilities")
    if caps is not None and (not isinstance(caps, dict) or not isinstance(caps.get("models", []), list)
            or len(caps.get("models", [])) > 512
            or any(not isinstance(model, str) or len(model) > 512 for model in caps.get("models", []))):
        return _err("Invalid worker model inventory")
    if body.get("activity_state") not in {None, "idle", "light", "active", "gaming"}:
        return _err("Invalid activity state")
    gpu = body.get("gpu_util")
    if gpu is not None and (isinstance(gpu, bool) or not isinstance(gpu, (int, float)) or not math.isfinite(gpu) or not 0 <= gpu <= 100):
        return _err("Invalid GPU utilization")

    db.update_worker_heartbeat(
        worker["id"],
        status=body.get("status", "online"),
        gpu_name=body.get("gpu_name"),
        ollama_version=body.get("ollama_version"),
        capabilities=body.get("capabilities"),         # {"models": [...]}
        activity_state=body.get("activity_state"),     # idle/light/active/gaming
        gpu_util=body.get("gpu_util"),
    )
    return jsonify({"ok": True})


# Job polling: long-poll; returns the job or 204 after timeout

@worker_api.route("/jobs/poll", methods=["GET"])
def poll_job():
    """Block until a job is available, then atomically claim and return it.

    The worker should call this in a tight loop.  On 204 it should loop
    again immediately (or after a short sleep) so the next poll window
    starts promptly.

    Query parameters
    ----------------
    models : comma-separated list of model names the caller has available.
             Only matching jobs are returned.  Omit to accept any model.
    """
    worker = _auth_worker()
    if not worker:
        return _err("Unauthorized", 401)

    if worker["status"] == "disabled":
        return _err("This worker has been disabled by an administrator", 403)

    models_param = request.args.get("models", "").strip()
    available_models = (
        [m.strip() for m in models_param.split(",") if m.strip()]
        if models_param else None
    )

    if available_models and (len(available_models) > 512 or any(len(model) > 512 for model in available_models)):
        return _err("Invalid worker model inventory")

    # Heartbeat at poll start so the admin panel shows the worker as active
    # even between jobs.
    db.update_worker_heartbeat(
        worker["id"],
        status="online",
        gpu_name=worker["gpu_name"],
        ollama_version=worker["ollama_version"],
    )

    deadline = time.monotonic() + _LONG_POLL_TIMEOUT
    while time.monotonic() < deadline:
        # Re-check disabled status on every iteration (admin may have disabled
        # the worker mid-poll).
        current = db.get_worker_by_id(worker["id"])
        if not current or current["status"] == "disabled":
            return _err("Worker disabled", 403)

        job = db.claim_next_worker_job(worker["id"], available_models)
        if job:
            _logger.info(
                "Job %s dispatched to worker %s (%s)",
                job["id"][:8], worker["name"], worker["id"][:8],
            )
            return jsonify({
                "job_id":   job["id"],
                "model":    job["model_name"],
                "messages": json.loads(job["messages"]),
                "options":  json.loads(job["options"]) if job.get("options") else None,
                "priority": job["priority"],
            })
        time.sleep(_LONG_POLL_SLEEP)

    return "", 204  # No Content: nothing arrived in this poll window


# Chunk relay: worker POSTs each generated token batch here

@worker_api.route("/jobs/<job_id>/chunk", methods=["POST"])
def submit_chunk(job_id):
    """Accept one streaming chunk from the worker and buffer it in SQLite.

    The server's dispatcher.stream_from_worker() polls for these chunks and
    relays them to the waiting browser SSE connection.

    Request body (JSON)
    -------------------
    seq     : int: monotonically increasing chunk index (0-based)
    content : str: text delta (may be empty for the final done=true chunk)
    done    : bool: true only on the very last chunk of the response
    """
    worker = _auth_worker()
    if not worker:
        return _err("Unauthorized", 401)

    job = db.get_worker_job(job_id)
    if not job or job.get("worker_id") != worker["id"]:
        return _err("Job not found", 404)

    request.max_content_length = 64 * 1024
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Expected a JSON object")
    try:
        accepted = db.add_worker_chunk(job_id, body.get("seq"), body.get("content", ""), body.get("done", False), worker_id=worker["id"])
    except ValueError as error:
        db.finish_worker_job(job_id, error="Worker submitted an invalid chunk", worker_id=worker["id"])
        return _err(str(error))
    return jsonify({"ok": accepted, "stop": not accepted})


# Job completion / failure reporting

@worker_api.route("/jobs/<job_id>/complete", methods=["POST"])
def complete_job(job_id):
    """Worker signals successful completion with final token counts."""
    worker = _auth_worker()
    if not worker:
        return _err("Unauthorized", 401)

    job = db.get_worker_job(job_id)
    if not job or job.get("worker_id") != worker["id"]:
        return _err("Job not found", 404)

    request.max_content_length = 4096
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Expected a JSON object")
    try:
        accepted = db.finish_worker_job(job_id, tokens_in=body.get("tokens_in", 0),
            tokens_out=body.get("tokens_out", 0), worker_id=worker["id"], finish_reason=body.get("finish_reason", "stop"))
    except ValueError as error:
        return _err(str(error))
    return jsonify({"ok": accepted, "stop": not accepted})


@worker_api.route("/jobs/<job_id>/fail", methods=["POST"])
def fail_job(job_id):
    """Worker signals a failure (Ollama error, model not found, etc.)."""
    worker = _auth_worker()
    if not worker:
        return _err("Unauthorized", 401)

    job = db.get_worker_job(job_id)
    if not job or job.get("worker_id") != worker["id"]:
        return _err("Job not found", 404)

    request.max_content_length = 4096
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("error", "Worker error"), str):
        return _err("Expected an error message")
    accepted = db.finish_worker_job(job_id, error=body.get("error") or "Worker error", worker_id=worker["id"])
    return jsonify({"ok": accepted, "stop": True})


# Blueprint registration helper

def register_worker_api(app):
    # These routes authenticate with Bearer tokens, never ambient cookies.
    csrf = app.extensions.get("csrf")
    if csrf:
        csrf.exempt(worker_api)
    app.register_blueprint(worker_api)
