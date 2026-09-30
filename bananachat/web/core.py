"""Home redirect, health and status checks, language switch, source link, robots."""

from __future__ import annotations

import time

from flask import Blueprint, current_app, jsonify, redirect, request, session, url_for

from bananachat import db, security
from bananachat.db import users
from bananachat.i18n import LANGUAGE_NAMES

bp = Blueprint("core", __name__)


@bp.get("/")
def index():
    if security.current_user() is not None:
        return redirect(url_for("chat.index"))
    return redirect(url_for("auth.login"))


@bp.get("/health")
@bp.get("/healthz")
def health():
    """Readiness probe for the lifecycle manager and load balancers (cheap, unauthenticated)."""
    ok = db.ping()
    response = jsonify({"status": "ok" if ok else "degraded", "database": "ok" if ok else "error"})
    response.status_code = 200 if ok else 503
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/status")
def status_report():
    """Service status for monitors and the pages' live banner (public, always 200 while the app runs).

    ``status`` is ``ok``, ``degraded``, ``maintenance`` or ``outage``;
    ``accepting_requests`` tells whether new answers can start. Signed-in
    users also get the notices that apply to them (with the administrator's
    messages), which the interface uses to update its banner without a reload.
    """
    from bananachat.services import status

    public = status.notices(None)
    payload = {
        "status": status.overall(),
        "accepting_requests": not any(notice.blocks_inference for notice in public),
        "database": "ok" if db.ping() else "error",
        "checked_at": int(time.time()),
    }
    user = security.current_user()
    if user is not None:
        mine = status.notices(user)
        payload["notices"] = [notice.to_dict() for notice in mine]
        payload["can_send"] = status.inference_block(user) is None
        if request.args.get("banner"):
            from flask import render_template
            payload["banner_html"] = render_template("partials/status_banner.html")
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/language/<language>", methods=["GET", "POST"])
def set_language(language):
    if language in LANGUAGE_NAMES:
        session["language"] = language
        user = security.current_user()
        if user is not None and request.method == "POST":
            prefs = users.get_preferences(user["id"])
            prefs["interface_language"] = language
            users.save_preferences(user["id"], prefs)
    target = security.safe_next_url(request.values.get("next")) or url_for("core.index")
    return redirect(target)


@bp.get("/source")
def source():
    """Where to obtain the corresponding source code (AGPL-3.0 section 13)."""
    return redirect(current_app.config["BC"].source_url)


@bp.get("/robots.txt")
def robots():
    return current_app.response_class("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@bp.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/favicon.png"))


@bp.app_template_global()
def language_switch_url(language: str) -> str:
    return url_for("core.set_language", language=language, next=request.full_path if request.query_string
                   else request.path)


@bp.app_template_global()
def current_path() -> str:
    return request.path


@bp.app_template_global()
def has_endpoint(name: str) -> bool:
    return name in current_app.view_functions

