"""Worker API (``/worker/v1``) used by the volunteer worker daemon (``worker/``).

Wire protocol (unchanged since the first worker release, so deployed daemons
keep working):

* Every request carries ``Authorization: Bearer <worker token>``; the server
  stores only the token's SHA-256 digest. Errors are ``{"error": "<text>"}``.
* ``POST /heartbeat`` ``{status, gpu_name, gpu_util, ollama_version,
  capabilities: {models: [...]}, activity_state[, platform, job_id]}`` ->
  ``{"ok": true, "job_stop": bool}``. It also renews the lease of the job the
  worker is running; reporting ``gaming`` (or ``busy``/``offline``) returns
  jobs it has not started answering to the pool.
* ``GET /jobs/poll[?models=a,b]`` long-polls (at most 25 s) and answers
  ``{job_id, model, messages, options, priority, first_token_timeout,
  lease_seconds, generation_timeout}`` or ``204`` when there is nothing.
* ``POST /jobs/<id>/chunk`` ``{seq, content, done}`` -> ``{ok, stop}``; chunks
  are accepted strictly in order (retries of accepted ones are acknowledged).
* ``POST /jobs/<id>/complete`` ``{tokens_in, tokens_out, finish_reason}``
  (after the ``done`` chunk) -> ``{ok, stop}``.
* ``POST /jobs/<id>/fail`` ``{error[, requeue]}`` -> ``{ok, stop}``;
  ``requeue: true`` or ``error: "deferred"`` before any chunk gives the job to
  another worker.

With ``BC_WORKERS_ENABLED`` off every endpoint answers 503.
"""

from __future__ import annotations

import re

from flask import Blueprint, current_app, g, jsonify, request

from bananachat import security
from bananachat.services import remote

bp = Blueprint("worker_api", __name__, url_prefix="/worker/v1")

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _error(message: str, status: int):
    response = jsonify({"error": message})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.before_request
def _authenticate():
    if not current_app.config["BC"].workers_enabled:
        response = _error("Remote workers are disabled on this server.", 503)
        response.headers["Retry-After"] = "300"
        return response
    worker = remote.authenticate(request.headers.get("Authorization"))
    if worker is None:
        # Count only failures, so a working daemon is never throttled.
        if not security.allow("worker-auth", 30, 300):
            response = _error("Too many failed attempts. Try again later.", 429)
            response.headers["Retry-After"] = "300"
            return response
        return _error("Unauthorized", 401)
    if worker["status"] == "disabled":
        return _error("This worker has been disabled by an administrator", 403)
    g.worker = worker
    return None


@bp.after_request
def _no_store(response):
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _body():
    return request.get_json(silent=True, force=True)


def _job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id or ""):
        raise remote.JobNotFound()
    return job_id


@bp.errorhandler(remote.InvalidRequest)
def _invalid(error):
    return _error(str(error) or "Invalid request.", 400)


@bp.errorhandler(remote.JobNotFound)
def _missing(_error_value):
    return _error("Job not found", 404)


@bp.errorhandler(remote.WorkerDisabled)
def _disabled(_error_value):
    return _error("This worker has been disabled by an administrator", 403)


@bp.post("/heartbeat", endpoint="heartbeat")
@security.body_limit(256 * 1024)
def heartbeat():
    return jsonify(remote.heartbeat(g.worker, _body()))


@bp.get("/jobs/poll", endpoint="poll")
def poll():
    models = None
    text = request.args.get("models", "").strip()
    if text:
        models = remote.parse_models([name.strip() for name in text.split(",") if name.strip()])
    job = remote.poll(g.worker, models, wait=remote.LONG_POLL_SECONDS)
    if job is None:
        return "", 204
    return jsonify(job)


@bp.post("/jobs/<job_id>/chunk", endpoint="chunk")
@security.body_limit(96 * 1024)
def chunk(job_id):
    return jsonify(remote.submit_chunk(g.worker, _job_id(job_id), _body()))


@bp.post("/jobs/<job_id>/complete", endpoint="complete")
@security.body_limit(4096)
def complete(job_id):
    return jsonify(remote.complete(g.worker, _job_id(job_id), _body()))


@bp.post("/jobs/<job_id>/fail", endpoint="fail")
@security.body_limit(4096)
def fail(job_id):
    return jsonify(remote.fail(g.worker, _job_id(job_id), _body()))
