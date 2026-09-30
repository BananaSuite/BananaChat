"""Overview: users, models, queue, inference health, today's usage, pending work."""

from __future__ import annotations

from flask import current_app, flash, jsonify, redirect, render_template, url_for

from bananachat import db
from bananachat.db import admin_overview, users
from bananachat.db import metrics as metrics_db
from bananachat.security import admin_required
from bananachat.services import health, model_recovery, ollama, queue
from bananachat.services import status as service_status
from bananachat.services.upstream import UpstreamError

from . import bp
from ._helpers import audit


@bp.get("/", endpoint="dashboard")
@admin_required
def dashboard():
    start, _end = db.day_bounds()
    config = current_app.config["BC"]
    model_recovery.refresh(config)
    return render_template(
        "admin/dashboard.html", section="dashboard",
        user_counts=admin_overview.user_counts(),
        model_counts=admin_overview.model_counts(),
        pending=admin_overview.pending_counts(),
        queue_stats=queue.stats(),
        health_state=health.status(),
        service_state=service_status.overall(),
        notices=service_status.notices(None),
        ollama_is_local=config.ollama_is_local,
        inference_server=ollama.describe_server(config),
        today=metrics_db.summary(start),
        snapshot=metrics_db.latest_snapshot(),
        recent_audit=users.list_audit(limit=8),
        recovery=model_recovery.status(),
        page_data={"inference_url": url_for("admin.inference_status")},
    )


@bp.get("/api/inference", endpoint="inference_status")
@admin_required
def inference_status():
    """Ollama version and running models, fetched by the overview page (fails gracefully)."""
    result = {"reachable": False, "version": "", "running": [], "error": "", "down": health.inference_down()}
    try:
        result["version"] = ollama.version(timeout=4)
        result["running"] = [_running(item) for item in ollama.list_running()]
        result["reachable"] = True
    except (UpstreamError, OSError, ValueError) as error:
        result["error"] = str(error)[:300]
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


def _running(item: dict) -> dict:
    size = item.get("size") if isinstance(item.get("size"), (int, float)) else None
    vram = item.get("size_vram") if isinstance(item.get("size_vram"), (int, float)) else None
    return {"name": str(item.get("name") or item.get("model") or "")[:300], "size": size, "size_vram": vram,
            "expires_at": str(item.get("expires_at") or "")[:64]}


@bp.post("/database-check", endpoint="database_check")
@admin_required
def database_check():
    result = db.integrity_check()
    audit("database_check", "database", {"result": result[:200]})
    if result == "ok":
        flash("Database check passed: no corruption found.", "success")
    else:
        flash(f"Database check reported a problem: {result[:300]}. Restore a backup or contact support.", "error")
    return redirect(url_for("admin.dashboard"))
