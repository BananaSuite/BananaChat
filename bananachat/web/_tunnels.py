"""The administrator's end of a worker tunnel (browser terminal, VNC viewer or connect helper), as a WebSocket.

Shared by ``web.admin.workers`` (cookie sessions, same-origin only) and
``web.remote_api`` (the connect helper's access keys).
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

from bananachat import db
from bananachat.db import worker_remote as store
from bananachat.services import background, tunnel_broker, websocket, worker_remote


def error(message: str, status: int, code: str = "tunnel_error"):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def same_origin() -> bool:
    """A browser's WebSocket carries the site's cookies even when another site opens it: accept only our pages."""
    origin = request.headers.get("Origin", "")
    if not origin or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    try:
        return urlsplit(origin).netloc.lower() == request.host.lower()
    except ValueError:
        return False


def _status(ws, state: str, message: str = "") -> None:
    ws.send_text(json.dumps({"type": "status", "state": state, "message": message}))


def _closed_by_administrator(tunnel_id: str):
    """A ``stopping`` callback: True once the session was ended from the page, or whoever opened it lost the right
    to (checked every few seconds)."""
    app = current_app._get_current_object()
    seen = {"at": 0.0, "closed": False}

    def check() -> bool:
        if background.stopping():
            return True
        if time.monotonic() - seen["at"] < 5:
            return seen["closed"]
        seen["at"] = time.monotonic()
        with app.app_context():
            try:
                row = store.tunnel(tunnel_id)
                seen["closed"] = row is None or row["status"] not in store.LIVE_TUNNELS
                if not seen["closed"] and not worker_remote.still_allowed(row):
                    # Demoted, suspended or deleted, or the access key was revoked or expired: the session ends.
                    worker_remote.ended(tunnel_id, ok=False, error="The administrator's access was withdrawn.")
                    seen["closed"] = True
            finally:
                db.release_thread_connection()
        return seen["closed"]

    return check


def client_bridge(tunnel_id: str, *, status_messages: bool):
    """Accept the WebSocket and relay it to the worker's end of the session."""
    config = current_app.config["BC"]
    if not websocket.requested(request.environ):
        return error("Expected a WebSocket.", 400)
    if not tunnel_broker.take_slot(config.worker_tunnels_per_process, config.http_threads):
        response = error("The server is relaying as many sessions as it can; try again shortly.", 503, "busy")
        response.headers["Retry-After"] = "5"
        return response
    app = current_app._get_current_object()
    try:
        try:
            broker = tunnel_broker.connect(app, "client", tunnel_id)
        except tunnel_broker.BrokerUnavailable as problem:
            worker_remote.ended(tunnel_id, ok=False, error=str(problem))
            return error(str(problem), 503, "relay_unavailable")
        try:
            ws = websocket.accept(request.environ)
        except (ValueError, OSError) as problem:
            tunnel_broker._close(broker)
            return error(str(problem) or "Invalid WebSocket request.", 400)
        db.release_thread_connection()  # sessions can last hours: hold no database connection
        if status_messages:
            try:
                _status(ws, "waiting", "Waiting for the worker to connect…")
            except websocket.WebSocketError:
                pass  # the browser left: the relay below gives up on its own
        if not tunnel_broker.wait_paired(broker):
            if status_messages:
                try:
                    _status(ws, "failed", "The worker did not connect. Is it online, and has its owner allowed this?")
                except websocket.WebSocketError:
                    pass
            ws.close(1011, "the worker did not connect")
            tunnel_broker._close(broker)
            with app.app_context():
                worker_remote.ended(tunnel_id, ok=False, error="The worker did not connect.")
                db.release_thread_connection()
            return websocket.finished()
        with app.app_context():
            worker_remote.opened(tunnel_id)
            db.release_thread_connection()
        if status_messages:
            try:
                _status(ws, "connected")
            except websocket.WebSocketError:
                pass  # the browser left: the bridge below notices and ends at once
        result = tunnel_broker.bridge(ws, broker, stopping=_closed_by_administrator(tunnel_id))
        with app.app_context():
            worker_remote.ended(tunnel_id, ok=True, up=result["up"], down=result["down"])
            db.release_thread_connection()
        return websocket.finished()
    finally:
        tunnel_broker.release_slot()
