"""Connect helper API (``/remote/v1``): native SSH, VNC and RDP clients reach worker PCs through tunnels.

The helper (``bananachat_connect.py``, downloadable from Administration →
Workers) authenticates with an administrator's access key
(``Authorization: Bearer bca_...``, created with a recent confirmation, expiring
after at most 30 days, revocable). Like the worker API it is a bearer
blueprint: no cookies, no CSRF token.

* ``GET /workers`` -> ``{"workers": [{id, name, slug, address, online, remote}], "subnet"}``
* ``POST /sessions`` ``{"host", "port"}`` (or ``{"worker", "kind", "host", "port"}``) -> ``{"session": {...}}``
  ``host`` is a worker's address in ``BC_WORKER_SUBNET``, ``<name>.bc``, or an
  address in a network a worker shares.
* ``GET /sessions/<id>/ws`` -> the helper's end of the session (a WebSocket; text
  messages report its status, binary ones carry the connection's bytes).

Off (404) unless ``BC_WORKERS_ENABLED`` and ``BC_WORKER_REMOTE_ACCESS`` are on.
"""

from __future__ import annotations

import re

from flask import Blueprint, current_app, g, jsonify, request

from bananachat import security
from bananachat.db import users
from bananachat.services import worker_remote
from bananachat.web import _tunnels

bp = Blueprint("remote_api", __name__, url_prefix="/remote/v1")
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _error(message: str, status: int, code: str = "error"):
    return _tunnels.error(message, status, code)


@bp.before_request
def _authenticate():
    if not worker_remote.enabled():
        return _error("Remote access to workers is off on this server.", 404, "remote_disabled")
    key = worker_remote.authenticate_key(request.headers.get("Authorization"))
    if key is None:
        if not security.allow("remote-auth", 30, 300):
            response = _error("Too many failed attempts. Try again later.", 429, "rate_limited")
            response.headers["Retry-After"] = "300"
            return response
        return _error("The access key is missing, wrong, expired or revoked.", 401, "unauthorized")
    g.access_key = key
    return None


@bp.after_request
def _no_store(response):
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@bp.get("/workers", endpoint="workers")
def workers():
    worker_remote.arm(None)  # the helper is in use: every worker listens for sessions for a while
    items = [{key: item[key] for key in ("id", "name", "slug", "address", "online")} |
             {"remote": {key: item["remote"].get(key) for key in ("enabled", "shell", "services", "subnets",
                                                                  "subnet_ports")}}
             for item in worker_remote.overview()]
    return jsonify({"workers": items, "subnet": current_app.config["BC"].worker_subnet})


@bp.post("/sessions", endpoint="sessions")
@security.body_limit(4096)
def sessions():
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return _error("Expected a JSON object.", 400, "invalid_request")
    if not security.allow("remote-session", 120, 300, key=g.access_key["id"]):
        return _error("Too many sessions opened. Wait a few minutes.", 429, "rate_limited")
    port = body.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return _error("Give the port as a number.", 400, "invalid_request")
    try:
        if body.get("worker"):
            worker_id = str(body["worker"])
            kind = str(body.get("kind") or worker_remote.kind_for_port(port))
            host = str(body.get("host") or "127.0.0.1")
        else:
            worker_id, kind, host, port = worker_remote.resolve(str(body.get("host") or ""), port)
        session = worker_remote.create_session(worker_id, kind, host, port, access_key_id=g.access_key["id"],
                                               client_ip=security.client_ip())
    except worker_remote.RemoteError as error:
        return _error(str(error), 409, "remote_unavailable")
    owner = users.get(g.access_key["created_by"]) if g.access_key["created_by"] else None
    users.audit(owner, "admin.workers.remote_session", session["worker_id"],
                {"name": session["worker"], "kind": session["kind"], "host": session["host"], "port": session["port"],
                 "session": session["id"], "access_key": g.access_key["id"]}, security.client_ip())
    response = jsonify({"session": session})
    response.status_code = 201
    return response


@bp.get("/sessions/<tunnel_id>/ws", endpoint="session_ws", websocket=True)
@security.long_request
def session_ws(tunnel_id):
    if not ID_RE.fullmatch(tunnel_id or "") or worker_remote.session_for_client(
            tunnel_id, access_key_id=g.access_key["id"]) is None:
        return _error("Session not found or expired.", 404, "not_found")
    return _tunnels.client_bridge(tunnel_id, status_messages=True)
