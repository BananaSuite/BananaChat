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

Optional parts added later (older daemons neither send nor read them):

* the heartbeat may carry ``remote`` (what the owner allows administrators to
  reach), ``system`` (memory, disk, features) and get back ``remote:
  {armed}`` and ``commands`` (model downloads and removals to run);
* ``GET /remote/poll`` long-polls for remote sessions while the worker is armed;
  ``GET /tunnel/<id>`` is the worker's WebSocket for one session;
* ``POST /commands/<id>`` ``{status, percent, detail, completed, total, error}``
  reports a command's progress -> ``{ok, cancel}``.

With ``BC_WORKERS_ENABLED`` off every endpoint answers 503.
"""

from __future__ import annotations

import re

from flask import Blueprint, current_app, g, jsonify, request

from bananachat import db, security
from bananachat.services import remote, tunnel_broker, websocket, worker_commands, worker_remote

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


# ----- remote access and model commands ----------------------------------------------------------------------

@bp.get("/remote/poll", endpoint="remote_poll")
@security.long_request
def remote_poll():
    if not worker_remote.enabled():
        return _error("Remote access to workers is off on this server.", 404)
    sessions = worker_remote.poll(g.worker, wait=worker_remote.LONG_POLL_SECONDS)
    if not sessions:
        return "", 204
    return jsonify({"sessions": sessions})


@bp.get("/tunnel/<tunnel_id>", endpoint="tunnel", websocket=True)
@security.long_request  # holds a thread for the whole session: counted with the answers
def tunnel(tunnel_id):
    """The worker's end of a remote session (a WebSocket)."""
    if not worker_remote.enabled():
        return _error("Remote access to workers is off on this server.", 404)
    if not JOB_ID_RE.fullmatch(tunnel_id or "") or worker_remote.session_for_worker(g.worker, tunnel_id) is None:
        return _error("Session not found", 404)
    if not websocket.requested(request.environ):
        return _error("Expected a WebSocket.", 400)
    config = current_app.config["BC"]
    if not tunnel_broker.take_slot(config.worker_tunnels_per_process, config.http_threads):
        response = _error("The server is relaying as many sessions as it can; try again shortly.", 503)
        response.headers["Retry-After"] = "5"
        return response
    try:
        try:
            broker = tunnel_broker.connect(current_app, "worker", tunnel_id)
        except tunnel_broker.BrokerUnavailable as error:
            worker_remote.ended(tunnel_id, ok=False, error=str(error))
            return _error(str(error), 503)
        app = current_app._get_current_object()
        try:
            ws = websocket.accept(request.environ)
        except (ValueError, OSError) as error:
            tunnel_broker._close(broker)
            return _error(str(error) or "Invalid WebSocket request.", 400)
        db.release_thread_connection()  # the session can last hours: hold no database connection
        if not tunnel_broker.wait_paired(broker):
            ws.close(1011, "the administrator did not connect")
            tunnel_broker._close(broker)
            with app.app_context():
                worker_remote.ended(tunnel_id, ok=False, error="The other side did not connect.")
                db.release_thread_connection()
            return websocket.finished()
        with app.app_context():
            worker_remote.opened(tunnel_id)
            db.release_thread_connection()
        result = tunnel_broker.bridge(ws, broker)
        with app.app_context():
            worker_remote.ended(tunnel_id, ok=True, error=result["reason"] or None, up=result["down"],
                                down=result["up"])
            db.release_thread_connection()
        return websocket.finished()
    finally:
        tunnel_broker.release_slot()


@bp.post("/commands/<command_id>", endpoint="command_report")
@security.body_limit(8192)
def command_report(command_id):
    if not JOB_ID_RE.fullmatch(command_id or ""):
        return _error("Command not found", 404)
    try:
        return jsonify(worker_commands.report(g.worker, command_id, _body()))
    except LookupError:
        return _error("Command not found", 404)
    except ValueError as error:
        return _error(str(error), 400)
