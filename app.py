# SPDX-FileCopyrightText: 2026 Luca Zani and all contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""BananaChat: Flask application entry point.

Started by Luca Zani on 2026-07-09, from BananaWiki; see NOTICE.
"""

import secrets
import time
import uuid
from datetime import timedelta

from flask import (
    Flask, request, redirect, url_for, session, g,
    render_template, jsonify, flash,
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import db
from logger import log_request
from helpers import get_current_user, time_ago, format_datetime, get_safe_next_url, plural
from routes import register_all_routes
from i18n import (
    SUPPORTED_LANGUAGES, browser_strings, get_language, translate,
)

app = Flask(
    __name__,
    template_folder="app/templates",
    static_folder="app/static",
)

app.jinja_env.globals["source_code_url"] = config.SOURCE_CODE_URL
app.jinja_env.globals["display_name"] = config.DISPLAY_NAME

app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_NAME=config.SESSION_COOKIE_NAME,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=config.SECURE_COOKIES,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    WTF_CSRF_TIME_LIMIT=3600,
    MAX_CONTENT_LENGTH=config.MAX_BACKUP_UPLOAD_MB * 1024 * 1024,
)


# This hook must be registered before Flask-WTF. CSRF validation parses
# multipart form data, so setting the limit in the main lifecycle hook is late.
@app.before_request
def enforce_chat_upload_limit():
    if request.endpoint == "chat_send":
        request.max_content_length = config.CHAT_MAX_REQUEST_BYTES

if config.PROXY_MODE:
    # BC_PROXY_HOPS controls how many reverse-proxy hops ProxyFix trusts.
    # Default (2): Cloudflare edge → nginx → Gunicorn.  Cloudflare sets
    # X-Forwarded-For: <real_client_ip>; nginx appends the Cloudflare edge IP
    # via $proxy_add_x_forwarded_for, producing "real_client_ip, cf_edge_ip".
    # With x_for=1 ProxyFix would pick the Cloudflare edge IP, breaking
    # rate-limiting and audit logs.  x_for=2 surfaces the real client IP.
    #
    # Set BC_PROXY_HOPS=1 for deployments with only nginx in front (e.g.
    # onion-only, where there is no Cloudflare hop).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.PROXY_HOPS, x_proto=1, x_host=1)

csrf = CSRFProtect(app)

# Exempt Bearer-token API blueprints from CSRF. These use Authorization
# headers, not session cookies, so CSRF does not apply.
from routes.api_v1    import api_v1    as _api_v1_blueprint    # noqa: E402
from routes.worker_api import worker_api as _worker_api_blueprint  # noqa: E402
csrf.exempt(_api_v1_blueprint)
csrf.exempt(_worker_api_blueprint)

db.init_db()


@app.before_request
def before_request_hook():
    g._request_id = str(uuid.uuid4())[:8]
    g._request_start = time.monotonic()
    # CSP nonce for inline scripts
    g._csp_nonce = secrets.token_hex(16)

    settings = db.get_site_settings()
    g._site_settings = settings

    if settings and not settings.get("setup_done"):
        if request.endpoint not in ("setup", "static", "set_language", "health"):
            return redirect(url_for("setup"))

    from services.ollama import is_inference_server_down
    if is_inference_server_down() and config.INFERENCE_OUTAGE_MODE == "shutdown":
        user = get_current_user()
        if not user or user["role"] != "admin":
            if request.endpoint not in ("admin_access", "login", "static", "setup", "health", "set_language"):
                if request.path.startswith("/v1/"):
                    return jsonify({"error": {"message": "Inference server unavailable. Service is temporarily offline.", "type": "server_error"}}), 503
                if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                    return jsonify({"error": "Inference server unavailable. Service is temporarily offline."}), 503
                return render_template("errors/outage.html"), 503

    if settings and settings.get("maintenance_mode"):
        user = get_current_user()
        if not user or user["role"] != "admin":
            if request.endpoint not in ("admin_access", "login", "static", "setup", "health", "set_language"):
                if request.path.startswith("/v1/"):
                    # OpenAI-compatible format for API clients
                    return jsonify({"error": {"message": "Service temporarily unavailable (maintenance mode)", "type": "server_error"}}), 503
                if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                    # Simple format for internal AJAX endpoints
                    return jsonify({"error": "Service is temporarily unavailable (maintenance mode)"}), 503
                return render_template(
                    "errors/maintenance.html",
                    message=settings.get("maintenance_message", ""),
                ), 503


@app.after_request
def after_request_hook(response):
    try:
        user = get_current_user()
        log_request(request, user=user)
    except Exception:
        pass
    nonce = getattr(g, "_csp_nonce", "")
    csp_parts = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        "base-uri 'self'",
    ]
    response.headers.setdefault("Content-Security-Policy", "; ".join(csp_parts))
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    microphone = "(self)" if request.endpoint == "chat_session" else "()"
    response.headers.setdefault(
        "Permissions-Policy", f"camera=(), microphone={microphone}, geolocation=()"
    )
    return response


@app.context_processor
def inject_globals():
    user = None
    settings = None
    music_active = False
    music_show_nav = False
    music_tracks = []
    music_playback_mode = "sequential"
    user_accessibility = None
    try:
        user = get_current_user()
        settings = getattr(g, "_site_settings", None) or db.get_site_settings()
        if user:
            user_accessibility = db.get_user_accessibility(user["id"])
            preferred_language = user_accessibility.get("interface_language")
            if preferred_language in SUPPORTED_LANGUAGES:
                g._language_override = preferred_language
        if user and settings:
            music_settings = db.get_music_settings()
            music_enabled = bool(music_settings.get("music_enabled"))
            music_visible = bool(music_settings.get("music_visible"))
            music_show_nav = music_enabled and music_visible
            if music_enabled:
                music_active = db.is_user_music_opted_in(user["id"])
                if music_active:
                    music_tracks = [dict(t) for t in db.list_music_tracks()]
                    music_playback_mode = music_settings.get("music_playback_mode", "sequential")
    except Exception:
        pass
    settings = settings or {}
    site_theme_mode = settings.get("default_theme_mode", "dark")
    if site_theme_mode not in ("dark", "light"):
        site_theme_mode = "dark"
    user_theme_mode = (user_accessibility or {}).get("theme_mode", "default")
    effective_theme_mode = (
        user_theme_mode if user_theme_mode in ("dark", "light") else site_theme_mode
    )
    prefix = "light_" if effective_theme_mode == "light" else ""
    defaults = {
        "dark": {
            "primary": "#e6be32", "secondary": "#1d1d1d", "accent": "#cda624",
            "text": "#ededed", "sidebar": "#181818", "bg": "#141414",
        },
        "light": {
            "primary": "#8a6500", "secondary": "#ffffff", "accent": "#6f5000",
            "text": "#202124", "sidebar": "#f4f1e8", "bg": "#faf9f5",
        },
    }
    fallback = defaults[effective_theme_mode]
    theme_palette = {
        "primary": settings.get(f"{prefix}primary_color", fallback["primary"]),
        "secondary": settings.get(f"{prefix}secondary_color", fallback["secondary"]),
        "accent": settings.get(f"{prefix}accent_color", fallback["accent"]),
        "text": settings.get(f"{prefix}text_color", fallback["text"]),
        "sidebar": settings.get(f"{prefix}sidebar_color", fallback["sidebar"]),
        "bg": settings.get(f"{prefix}bg_color", fallback["bg"]),
    }
    return {
        "current_user": user,
        "settings": settings,
        "site_name": settings.get("site_name", "BananaChat"),
        "current_language": get_language(),
        "supported_languages": SUPPORTED_LANGUAGES,
        "t": translate,
        "browser_i18n": browser_strings(),
        "csp_nonce": getattr(g, "_csp_nonce", ""),
        "music_active": music_active,
        "music_show_nav": music_show_nav,
        "music_tracks": music_tracks,
        "music_playback_mode": music_playback_mode,
        "image_backend_enabled": config.IMAGE_BACKEND == "comfyui",
        "user_accessibility": user_accessibility,
        "effective_theme_mode": effective_theme_mode,
        "theme_palette": theme_palette,
    }


app.jinja_env.globals["time_ago"] = time_ago
app.jinja_env.globals["format_datetime"] = format_datetime
app.jinja_env.globals["plural"] = plural


@app.route("/")
def index():
    # Show maintenance/outage page on root instead of redirecting
    from services.ollama import is_inference_server_down
    settings = getattr(g, "_site_settings", None) or db.get_site_settings()
    if is_inference_server_down() and config.INFERENCE_OUTAGE_MODE == "shutdown":
        user = get_current_user()
        if not user or user["role"] != "admin":
            return render_template("errors/outage.html"), 503
    if settings and settings.get("maintenance_mode"):
        user = get_current_user()
        if not user or user["role"] != "admin":
            return render_template(
                "errors/maintenance.html",
                message=settings.get("maintenance_message", ""),
            ), 503
    user = get_current_user()
    if user:
        return redirect(url_for("chat_index"))
    return redirect(url_for("login"))


@app.route("/admin-access", methods=["GET", "POST"])
def admin_access():
    """Dedicated admin login path accessible during maintenance and outages."""
    from helpers._auth import get_current_user as _get_user
    user = _get_user()
    if user and user["role"] == "admin":
        return redirect(url_for("chat_index"))
    if request.method == "POST":
        from helpers._passwords import check_password_hash
        from helpers._constants import _get_dummy_hash
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.remote_addr or "unknown"
        # This path stays reachable during maintenance and outages, so it needs
        # the same throttle and attempt record as /login; otherwise it is an
        # unmetered channel for guessing the administrator password.
        if db.check_login_rate_limit(ip, max_attempts=10, window_seconds=60):
            flash("Too many failed attempts. Please wait a minute.", "error")
            return render_template("auth/admin_access.html"), 429
        user = db.get_user_by_username(username)
        if not user:
            check_password_hash(_get_dummy_hash(), password)
            db.record_login_attempt(ip)
            flash("Invalid credentials.", "error")
            return render_template("auth/admin_access.html"), 401
        if not check_password_hash(user["password"], password):
            db.record_login_attempt(ip)
            flash("Invalid credentials.", "error")
            return render_template("auth/admin_access.html"), 401
        if user["role"] != "admin":
            flash("Admin access only.", "error")
            return render_template("auth/admin_access.html"), 403
        if user.get("suspended"):
            flash("Account suspended.", "error")
            return render_template("auth/admin_access.html"), 403
        db.clear_login_attempts(ip)
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["session_version"] = user.get("session_version", 0)
        return redirect(url_for("chat_index"))
    return render_template("auth/admin_access.html")


@app.route("/language/<language>")
def set_language(language):
    """Switch interface language and return to the current in-app page."""
    if language in SUPPORTED_LANGUAGES:
        session["language"] = language
        session.modified = True
        user = get_current_user()
        if user:
            prefs = db.get_user_accessibility(user["id"])
            prefs["interface_language"] = language
            db.save_user_accessibility(user["id"], prefs)
    next_url = get_safe_next_url(request.args.get("next", ""))
    return redirect(next_url or url_for("index"))


@app.route("/health")
@app.route("/healthz")
def health():
    """Health check for load balancers and orchestration systems."""
    db_ok = False
    try:
        result = db.integrity_check()
        db_ok = result.lower() == "ok"
    except Exception:
        pass
    status = 200 if db_ok else 503
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
    }), status


register_all_routes(app)


if __name__ == "__main__":
    import os as _os
    from services.ollama import start_background_sync, stop_background_sync
    start_background_sync()
    try:
        app.run(host=config.HOST, port=config.PORT, debug=_os.environ.get("BC_DEBUG", "0") == "1")
    finally:
        stop_background_sync()
