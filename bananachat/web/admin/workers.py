"""Workers: register volunteer worker PCs, watch them, enable, disable and remove them."""

from __future__ import annotations

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from bananachat import db, security
from bananachat.db import users
from bananachat.db import workers as store
from bananachat.security import admin_required
from bananachat.services import remote

from . import bp


def _audit(action: str, target: str, details=None) -> None:
    users.audit(security.current_user(), f"admin.workers.{action}", target, details, security.client_ip())


def _page_data() -> dict:
    return {
        "workers": remote.overview(),
        "enabled": current_app.config["BC"].workers_enabled,
        "urls": {
            "data": url_for("admin.workers_data"),
            "register": url_for("admin.workers_register"),
            "state": url_for("admin.workers_state", worker_id="WORKER_ID"),
            "delete": url_for("admin.workers_delete", worker_id="WORKER_ID"),
        },
    }


def _render(new_token=None, new_name=None, status=200):
    config = current_app.config["BC"]
    data = _page_data()
    return render_template(
        "admin/workers.html", section="workers", workers=data["workers"], page_data=data,
        workers_enabled=config.workers_enabled, claim_timeout=config.worker_claim_timeout,
        server_url=request.host_url.rstrip("/"), new_token=new_token, new_name=new_name,
        job_counts=store.job_counts(),
    ), status


@bp.get("/workers", endpoint="workers")
@admin_required
def workers():
    return _render()


@bp.get("/workers/data", endpoint="workers_data")
@admin_required
def workers_data():
    response = jsonify({"workers": remote.overview(), "enabled": current_app.config["BC"].workers_enabled})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/workers", endpoint="workers_register")
@admin_required
def workers_register():
    payload = request.get_json(silent=True) if request.is_json else None
    name = (payload or {}).get("name") if payload is not None else request.form.get("name")
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
    payload = request.get_json(silent=True) if request.is_json else None
    raw = (payload or {}).get("enabled") if payload is not None else request.form.get("enabled")
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
