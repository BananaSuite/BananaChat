"""Workers: register volunteer worker PCs, watch them, enable, disable and remove them.

Registering one issues a lasting token whose worker reads the prompts routed to it, so it needs a
recent confirmation (:mod:`._reauth`), as creating an administrator does."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from bananachat import db, security
from bananachat.db import users
from bananachat.db import worker_remote as remote_store
from bananachat.db import workers as store
from bananachat.security import admin_required
from bananachat.services import remote, worker_commands, worker_remote
from bananachat.web import _tunnels

from . import _reauth, bp


def _audit(action: str, target: str, details=None) -> None:
    users.audit(security.current_user(), f"admin.workers.{action}", target, details, security.client_ip())


def _keys() -> list[dict]:
    now = time.time()
    return [{"id": row["id"], "label": row["label"], "created_by": row["created_by_name"],
             "created_at": row["created_at"], "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
             "active": row["revoked_at"] is None and float(row["expires_at"]) > now,
             "revoked": row["revoked_at"] is not None} for row in remote_store.keys()]


def _sessions(limit: int = 20) -> list[dict]:
    return [{"id": row["id"], "worker": row["worker_name"], "kind": row["kind"],
             "target": "terminal" if row["kind"] == "shell" else f"{row['target_host']}:{row['target_port']}",
             "status": row["status"], "by": row["created_by_name"] or ("connect helper" if row["access_key_id"] else ""),
             "created_at": row["created_at"], "error": row["error"] or ""}
            for row in remote_store.recent_tunnels(limit)]


def _page_data() -> dict:
    config = current_app.config["BC"]
    return {
        "workers": remote.overview(),
        "enabled": config.workers_enabled,
        "remote_enabled": worker_remote.enabled(),
        "keys": _keys(),
        "sessions": _sessions(),
        "urls": {
            "data": url_for("admin.workers_data"),
            "register": url_for("admin.workers_register"),
            "state": url_for("admin.workers_state", worker_id="WORKER_ID"),
            "delete": url_for("admin.workers_delete", worker_id="WORKER_ID"),
            "detail": url_for("admin.worker_detail", worker_id="WORKER_ID"),
            "key_create": url_for("admin.workers_key_create"),
            "key_revoke": url_for("admin.workers_key_revoke", key_id="KEY_ID"),
        },
    }


def _render(new_token=None, new_name=None, status=200):
    config = current_app.config["BC"]
    data = _page_data()
    return render_template(
        "admin/workers.html", section="workers", workers=data["workers"], page_data=data,
        workers_enabled=config.workers_enabled, claim_timeout=config.worker_claim_timeout,
        server_url=request.host_url.rstrip("/"), new_token=new_token, new_name=new_name,
        job_counts=store.job_counts(), remote_enabled=data["remote_enabled"], keys=data["keys"],
        sessions=data["sessions"], subnet=config.worker_subnet,
        helper_url=url_for("static", filename="downloads/bananachat_connect.py"),
    ), status


@bp.get("/workers", endpoint="workers")
@admin_required
def workers():
    return _render()


@bp.get("/workers/data", endpoint="workers_data")
@admin_required
def workers_data():
    response = jsonify({"workers": remote.overview(), "enabled": current_app.config["BC"].workers_enabled,
                        "keys": _keys(), "sessions": _sessions()})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/workers", endpoint="workers_register")
@admin_required
def workers_register():
    payload = request.get_json(silent=True) if request.is_json else None
    fields = payload if isinstance(payload, dict) else {}
    name = fields.get("name") if payload is not None else request.form.get("name")
    problem = _reauth.refusal(fields.get("reauth_secret") if payload is not None else None)
    if problem:
        if security.wants_json():
            return security.json_error(problem, 403, "reauth_required")
        flash(problem, "error")
        return redirect(url_for("admin.workers"))
    try:
        with db.transaction():
            worker_id, token = remote.register_worker(name)
            _audit("register", worker_id, {"name": remote.valid_name(name)})
    except remote.InvalidRequest as error:
        if security.wants_json():
            return security.json_error(str(error), 400, "invalid_name")
        flash(str(error), "error")
        return redirect(url_for("admin.workers"))
    if security.wants_json():
        # The token is shown once, by the page, and never stored anywhere readable.
        response = jsonify({"worker": remote.describe(store.get(worker_id)), "token": token})
        response.status_code = 201
        response.headers["Cache-Control"] = "no-store"
        return response
    return _render(new_token=token, new_name=remote.valid_name(name), status=201)


@bp.post("/workers/<worker_id>/state", endpoint="workers_state")
@admin_required
def workers_state(worker_id):
    raw = _payload().get("enabled")
    enabled = raw in (True, 1, "1", "true", "on")
    worker = store.get(worker_id)
    if worker is None or not remote.set_enabled(worker_id, enabled):
        abort(404)
    _audit("enable" if enabled else "disable", worker_id, {"name": worker["name"]})
    message = f"Worker “{worker['name']}” {'enabled' if enabled else 'disabled'}."
    if security.wants_json():
        return jsonify({"ok": True, "message": message, "worker": remote.describe(store.get(worker_id))})
    flash(message, "success")
    return redirect(url_for("admin.workers"))


@bp.post("/workers/<worker_id>/delete", endpoint="workers_delete")
@admin_required
def workers_delete(worker_id):
    worker = store.get(worker_id)
    if worker is None or not remote.delete_worker(worker_id):
        abort(404)
    _audit("delete", worker_id, {"name": worker["name"]})
    message = f"Worker “{worker['name']}” removed. Its token no longer works."
    if security.wants_json():
        return jsonify({"ok": True, "message": message})
    flash(message, "success")
    return redirect(url_for("admin.workers"))


# ----- one worker: remote access, models and commands ----------------------------------------------------------

ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _worker_or_404(worker_id: str):
    if not ID_RE.fullmatch(worker_id or ""):
        abort(404)
    worker = store.get(worker_id)
    if worker is None:
        abort(404)
    return worker


def _json(value, status=200):
    response = jsonify(value)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _payload() -> dict:
    payload = request.get_json(silent=True) if request.is_json else None
    return payload if isinstance(payload, dict) else dict(request.form)


def _detail(worker_id: str) -> dict:
    worker = store.get(worker_id)
    config = current_app.config["BC"]
    remote_view = next((item for item in worker_remote.overview() if item["id"] == worker_id), None) \
        if worker_remote.enabled() else None
    return {"worker": remote.describe(worker), "remote": remote_view,
            "remote_enabled": worker_remote.enabled(), "subnet": config.worker_subnet,
            "system": worker_remote.system_info(remote_store.remote_row(worker_id)),
            "manages_models": worker_commands.supports(remote_store.remote_row(worker_id)),
            "commands": worker_commands.overview(worker_id, 50)}


@bp.get("/workers/<worker_id>", endpoint="worker_detail")
@admin_required
def worker_detail(worker_id):
    worker = _worker_or_404(worker_id)
    data = _detail(worker_id)
    data["urls"] = {
        "data": url_for("admin.worker_detail_data", worker_id=worker_id),
        "arm": url_for("admin.workers_remote_arm", worker_id=worker_id),
        "session": url_for("admin.workers_remote_session", worker_id=worker_id),
        "close": url_for("admin.workers_tunnel_close", tunnel_id="TUNNEL_ID"),
        "terminal": url_for("admin.worker_terminal", worker_id=worker_id),
        "vnc": url_for("admin.worker_vnc", worker_id=worker_id),
        "command": url_for("admin.workers_command_create", worker_id=worker_id),
        "cancel": url_for("admin.workers_command_cancel", command_id="COMMAND_ID"),
        "retry": url_for("admin.workers_command_retry", command_id="COMMAND_ID"),
    }
    data["reauth"] = _reauth.needed()
    return render_template("admin/worker_detail.html", section="workers", worker=worker, data=data,
                           page_data=data, helper_url=url_for("static", filename="downloads/bananachat_connect.py"),
                           server_url=request.host_url.rstrip("/"))


@bp.get("/workers/<worker_id>/data", endpoint="worker_detail_data")
@admin_required
def worker_detail_data(worker_id):
    _worker_or_404(worker_id)
    return _json(_detail(worker_id))


@bp.post("/workers/<worker_id>/remote/arm", endpoint="workers_remote_arm")
@admin_required
def workers_remote_arm(worker_id):
    """Ask the worker to listen for sessions now (it hears it in its next heartbeat); grants no access."""
    _worker_or_404(worker_id)
    if not worker_remote.enabled():
        return security.json_error("Remote access to workers is off on this server.", 409, "remote_disabled")
    worker_remote.arm(worker_id)
    return _json({"ok": True})


@bp.post("/workers/<worker_id>/remote/sessions", endpoint="workers_remote_session")
@admin_required
def workers_remote_session(worker_id):
    """Open a session to a service of the worker for this browser (terminal, VNC, or a TCP port)."""
    worker = _worker_or_404(worker_id)
    fields = _payload()
    problem = _reauth.refusal(fields.get("reauth_secret"))
    if problem:
        return security.json_error(problem, 403, "reauth_required")
    if not security.allow("worker-remote-session", 30, 300, key=security.current_user()["id"]):
        return security.json_error("Too many sessions opened. Wait a few minutes.", 429, "rate_limited")
    kind = str(fields.get("kind") or "")
    host = str(fields.get("host") or "127.0.0.1")
    try:
        port = int(fields.get("port") or worker_remote.DEFAULT_PORTS.get(kind, 0))
    except (TypeError, ValueError):
        return security.json_error("Choose a port from 1 to 65535.", 400, "invalid_port")
    try:
        session = worker_remote.create_session(worker_id, kind, host, port, created_by=security.current_user()["id"],
                                               client_ip=security.client_ip())
    except worker_remote.RemoteError as error:
        return security.json_error(str(error), 409, "remote_unavailable")
    _audit("remote_session", worker_id, {"name": worker["name"], "kind": session["kind"], "host": session["host"],
                                         "port": session["port"], "session": session["id"]})
    # A WebSocket rule builds an absolute ws:// address; the page picks ws or wss from its own address.
    session["ws"] = urlsplit(url_for("admin.workers_tunnel_ws", tunnel_id=session["id"])).path
    return _json({"session": session}, 201)


@bp.get("/workers/tunnels/<tunnel_id>/ws", endpoint="workers_tunnel_ws", websocket=True)
@admin_required
@security.long_request
def workers_tunnel_ws(tunnel_id):
    """The browser's end of a session (WebSocket): only from this site's pages, only by its creator."""
    if not worker_remote.enabled():
        abort(404)
    if not _tunnels.same_origin():
        return _tunnels.error("Sessions open only from this site's pages.", 403, "cross_origin")
    if not ID_RE.fullmatch(tunnel_id or "") or worker_remote.session_for_client(
            tunnel_id, user_id=security.current_user()["id"]) is None:
        return _tunnels.error("Session not found or expired.", 404, "not_found")
    return _tunnels.client_bridge(tunnel_id, status_messages=request.args.get("status") == "1",
                                  auth_session=security.current_session_hash())


@bp.post("/workers/tunnels/<tunnel_id>/close", endpoint="workers_tunnel_close")
@admin_required
def workers_tunnel_close(tunnel_id):
    if not ID_RE.fullmatch(tunnel_id or ""):
        abort(404)
    row = remote_store.tunnel(tunnel_id)
    if row is None:
        abort(404)
    worker_remote.ended(tunnel_id, ok=True, error="Ended by an administrator.")
    _audit("remote_session_close", row["worker_id"], {"session": tunnel_id})
    if security.wants_json():
        return _json({"ok": True})
    flash("Session ended.", "success")
    return redirect(url_for("admin.worker_detail", worker_id=row["worker_id"]))


def _viewer(template: str, worker_id: str, **values):
    """A page that opens a WebSocket: its policy also allows this site's ws:// and wss:// address."""
    worker = _worker_or_404(worker_id)
    if not worker_remote.enabled():
        flash("Remote access to workers is off on this server (BC_WORKER_REMOTE_ACCESS).", "error")
        return redirect(url_for("admin.worker_detail", worker_id=worker_id))
    response = current_app.make_response(render_template(
        template, section="workers", worker=worker, page_data={
            "session": url_for("admin.workers_remote_session", worker_id=worker_id),
            "close": url_for("admin.workers_tunnel_close", tunnel_id="TUNNEL_ID"),
            "back": url_for("admin.worker_detail", worker_id=worker_id), "reauth": _reauth.needed(),
            "name": worker["name"], **values}))
    from bananachat.app import _security_headers

    _security_headers(response)
    host = request.host
    policy = response.headers["Content-Security-Policy"].replace(
        "connect-src 'self'", f"connect-src 'self' wss://{host} ws://{host}")
    response.headers["Content-Security-Policy"] = policy
    return response


@bp.get("/workers/<worker_id>/terminal", endpoint="worker_terminal")
@admin_required
def worker_terminal(worker_id):
    return _viewer("admin/worker_terminal.html", worker_id)


@bp.get("/workers/<worker_id>/vnc", endpoint="worker_vnc")
@admin_required
def worker_vnc(worker_id):
    try:
        port = int(request.args.get("port") or 5900)
    except ValueError:
        port = 5900
    return _viewer("admin/worker_vnc.html", worker_id, port=port if 1 <= port <= 65535 else 5900)


# ----- access keys for the connect helper ----------------------------------------------------------------------

@bp.post("/workers/keys", endpoint="workers_key_create")
@admin_required
def workers_key_create():
    fields = _payload()
    problem = _reauth.refusal(fields.get("reauth_secret"))
    if problem:
        if security.wants_json():
            return security.json_error(problem, 403, "reauth_required")
        flash(problem, "error")
        return redirect(url_for("admin.workers") + "#remote-access")
    try:
        hours = int(fields.get("hours") or worker_remote.KEY_HOURS)
        key_id, token = worker_remote.create_key(security.current_user(), fields.get("label") or "", hours)
    except (TypeError, ValueError, worker_remote.RemoteError) as error:
        message = str(error) if isinstance(error, worker_remote.RemoteError) else "Choose a number of hours."
        if security.wants_json():
            return security.json_error(message, 400, "invalid_key")
        flash(message, "error")
        return redirect(url_for("admin.workers") + "#remote-access")
    _audit("access_key_create", key_id, {"label": fields.get("label") or "", "hours": hours})
    return _json({"key": {"id": key_id, "token": token, "hours": hours}}, 201)


@bp.post("/workers/keys/<key_id>/revoke", endpoint="workers_key_revoke")
@admin_required
def workers_key_revoke(key_id):
    if not ID_RE.fullmatch(key_id or "") or not worker_remote.revoke_key(key_id):
        abort(404)
    _audit("access_key_revoke", key_id)
    if security.wants_json():
        return _json({"ok": True})
    flash("Access key revoked; its sessions were ended.", "success")
    return redirect(url_for("admin.workers") + "#remote-access")


# ----- model commands ------------------------------------------------------------------------------------------

@bp.post("/workers/<worker_id>/commands", endpoint="workers_command_create")
@admin_required
def workers_command_create(worker_id):
    worker = _worker_or_404(worker_id)
    fields = _payload()
    kind = str(fields.get("kind") or "pull")
    model = str(fields.get("model") or "")
    try:
        command_id = worker_commands.queue(worker_id, kind, model, security.current_user()["id"])
    except worker_commands.CommandError as error:
        if security.wants_json():
            return security.json_error(str(error), 409, "command_refused")
        flash(str(error), "error")
        return redirect(url_for("admin.worker_detail", worker_id=worker_id))
    _audit(f"model_{kind}", worker_id, {"name": worker["name"], "model": model, "command": command_id})
    message = f"{'Download of' if kind == 'pull' else 'Removal of'} {model} queued on {worker['name']}."
    if security.wants_json():
        return _json({"ok": True, "id": command_id, "message": message}, 201)
    flash(message, "success")
    return redirect(url_for("admin.worker_detail", worker_id=worker_id))


@bp.post("/workers/commands", endpoint="workers_command_bulk")
@admin_required
def workers_command_bulk():
    """Download one model to several workers (every online one that manages its models, or those chosen)."""
    fields = request.form
    model = (fields.get("model") or "").strip()
    chosen = fields.getlist("workers")
    if not chosen:
        now = time.time()
        chosen = [row["id"] for row in store.list_all() if row["status"] != "disabled" and row["last_heartbeat"]
                  and float(row["last_heartbeat"]) >= now - remote.HEARTBEAT_FRESH_SECONDS
                  and worker_commands.supports(remote_store.remote_row(row["id"]))]
    if not chosen:
        flash("No online worker can download models from here.", "error")
        return redirect(url_for("admin.workers"))
    result = worker_commands.queue_many(chosen, "pull", model, security.current_user()["id"])
    _audit("model_pull_bulk", model, {"workers": len(chosen), "queued": len(result["queued"])})
    if result["queued"]:
        flash(f"Download of {model} queued on {len(result['queued'])} worker(s).", "success")
    for problem in result["errors"][:5]:
        flash(problem, "error")
    return redirect(url_for("admin.workers"))


def _command_or_404(command_id: str):
    if not ID_RE.fullmatch(command_id or ""):
        abort(404)
    row = remote_store.command(command_id)
    if row is None:
        abort(404)
    return row


@bp.post("/workers/commands/<command_id>/cancel", endpoint="workers_command_cancel")
@admin_required
def workers_command_cancel(command_id):
    row = _command_or_404(command_id)
    done = worker_commands.cancel(command_id)
    if done:
        _audit("model_command_cancel", row["worker_id"], {"command": command_id, "model": row["model_name"]})
    if security.wants_json():
        return _json({"ok": done})
    flash("Cancelled." if done else "That command had already finished.", "success" if done else "info")
    return redirect(url_for("admin.worker_detail", worker_id=row["worker_id"]))


@bp.post("/workers/commands/<command_id>/retry", endpoint="workers_command_retry")
@admin_required
def workers_command_retry(command_id):
    row = _command_or_404(command_id)
    try:
        new_id = worker_commands.retry(command_id, security.current_user()["id"])
    except worker_commands.CommandError as error:
        if security.wants_json():
            return security.json_error(str(error), 409, "command_refused")
        flash(str(error), "error")
        return redirect(url_for("admin.worker_detail", worker_id=row["worker_id"]))
    _audit("model_command_retry", row["worker_id"], {"command": new_id, "model": row["model_name"]})
    if security.wants_json():
        return _json({"ok": True, "id": new_id})
    flash("Queued again.", "success")
    return redirect(url_for("admin.worker_detail", worker_id=row["worker_id"]))
