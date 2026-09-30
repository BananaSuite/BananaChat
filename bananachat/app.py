"""Application factory."""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import timedelta

from flask import Flask, g, render_template, request, session
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from bananachat import PRODUCT_NAME, __version__, db
from bananachat.config import Config, load_config, log_warnings
from bananachat.db import settings as site_settings
from bananachat.db import users
from bananachat.i18n import LANGUAGE_NAMES, browser_catalog, negotiate, translate
from bananachat import security
from bananachat.logs import configure_logging, record_exception

DEFAULT_BODY_LIMIT = 2 * 1024 * 1024

log = logging.getLogger("bananachat")


def create_app(config: Config | None = None, *, testing: bool = False) -> Flask:
    config = config or load_config()
    configure_logging(config)
    log_warnings(config)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        BC=config,
        TESTING=testing,
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_NAME=config.session_cookie_name,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config.secure_cookies,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=config.session_days),
        MAX_CONTENT_LENGTH=DEFAULT_BODY_LIMIT,
        MAX_FORM_MEMORY_SIZE=DEFAULT_BODY_LIMIT,
        JSON_SORT_KEYS=False,
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(hours=12),
    )
    if not config.secret_key:
        raise RuntimeError("A secret key is required.")
    if config.proxy_mode:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.proxy_hops, x_proto=1, x_host=1)

    db.configure(config.database_path)
    db.init_db()
    _announce_setup(config)

    _install_hooks(app)
    _install_template_helpers(app)
    _install_error_handlers(app)

    from bananachat.web import register_blueprints
    from bananachat.services import housekeeping  # noqa: F401  (registers core background jobs)
    from bananachat.services import limits  # noqa: F401  (registers the limit jobs)
    register_blueprints(app)
    # Never hand a connection opened here to forked worker processes.
    db.close_thread_connection()
    return app


def start_background_services(app: Flask) -> None:
    """Run periodic jobs in this process if it wins leadership (see services.background)."""
    from bananachat.services import background
    background.start(app)


def _announce_setup(config: Config) -> None:
    """Tell the operator how to create the first administrator."""
    if site_settings.get().get("setup_done"):
        return
    import os
    if os.environ.get("BC_SETUP_TOKEN"):
        log.warning("Setup is not complete. Open the site and use BC_SETUP_TOKEN from the configuration file.")
    else:
        log.warning("Setup is not complete. Open the site and enter this installation token: %s", config.setup_token)


# ----- request lifecycle ----------------------------------------------------

def _install_hooks(app: Flask) -> None:
    @app.before_request
    def prepare_request():
        g.request_id = secrets.token_hex(4)
        g.csp_nonce = secrets.token_urlsafe(16)
        view = app.view_functions.get(request.endpoint)
        limit = getattr(view, "body_limit", None)
        if callable(limit):
            limit = limit(app.config["BC"])
        if limit:
            request.max_content_length = limit
            request.max_form_memory_size = limit

        if request.endpoint == "static":
            return None
        g.settings = site_settings.get()
        security.load_current_user()
        g.lang = _language()

        if not g.settings.get("setup_done") and request.endpoint not in ("auth.setup", "core.health",
                                                                           "core.status_report", "core.set_language"):
            from flask import redirect, url_for
            if security.wants_json():
                return security.json_error("Initial setup has not been completed.", 503, "setup_required")
            return redirect(url_for("auth.setup"))

        security.check_csrf()
        # Maintenance mode and inference outages never close the site: pages
        # stay reachable and show a banner; only starting new answers is
        # refused (see services.status).
        return None

    @app.after_request
    def secure_response(response):
        _security_headers(response)
        return response

    @app.teardown_request
    def release_database(_error=None):
        db.release_thread_connection()


def _language() -> str:
    user = security.current_user()
    if user is not None:
        preference = users.get_preferences(user["id"]).get("interface_language")
        if preference in LANGUAGE_NAMES:
            return preference
    chosen = session.get("language")
    if chosen in LANGUAGE_NAMES:
        return chosen
    from flask import current_app
    return negotiate(request.headers.get("Accept-Language"), current_app.config["BC"].default_language)


def _security_headers(response) -> None:
    nonce = getattr(g, "csp_nonce", "")
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; "
        "manifest-src 'self'; worker-src 'self' blob:"
    )
    headers = response.headers
    headers.setdefault("Content-Security-Policy", csp)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    microphone = "(self)" if request.endpoint == "chat.session" else "()"
    headers.setdefault("Permissions-Policy", f"camera=(), microphone={microphone}, geolocation=(), payment=()")
    if request.is_secure:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if response.mimetype == "text/html" and getattr(g, "user", None) is not None:
        headers.setdefault("Cache-Control", "private, no-store")


# ----- templates ------------------------------------------------------------

def _install_template_helpers(app: Flask) -> None:
    from bananachat import formatting

    app.jinja_env.globals.update(
        product_name=PRODUCT_NAME,
        product_version=__version__,
        csrf_token=security.csrf_token,
        form_token=security.form_token,
        language_names=LANGUAGE_NAMES,
    )
    app.jinja_env.filters.update(
        timeago=formatting.timeago,
        datetime=formatting.datetime_text,
        number=formatting.number,
        credits=formatting.credits,
        tokens=formatting.tokens,
        compact=formatting.compact,
        token_input=formatting.token_input,
        time_text=formatting.time_text,
        time_left=formatting.time_left,
        filesize=formatting.filesize,
    )

    @app.context_processor
    def inject():
        config = app.config["BC"]
        settings = getattr(g, "settings", None) or {}
        language = getattr(g, "lang", config.default_language)
        user = getattr(g, "user", None)
        prefs = users.get_preferences(user["id"]) if user is not None else dict(users.PREFERENCE_DEFAULTS)
        theme = prefs["theme_mode"] if prefs["theme_mode"] in ("dark", "light") else (
            settings.get("default_theme_mode") if settings.get("default_theme_mode") in ("dark", "light") else "dark")
        palette = site_settings.palette(settings, theme)
        overrides = {"bg": prefs["custom_bg"], "text": prefs["custom_text"], "primary": prefs["custom_primary"],
                     "secondary": prefs["custom_secondary"], "accent": prefs["custom_accent"],
                     "sidebar": prefs["custom_sidebar"]}
        palette.update({key: value for key, value in overrides.items() if value})

        def t(key, **params):
            return translate(language, key, **params)

        return {
            "config": config,
            "settings": settings,
            "site_name": settings.get("site_name") or PRODUCT_NAME,
            "current_user": user,
            "is_admin": bool(user and user["role"] == "admin"),
            "lang": language,
            "t": t,
            "js_strings": browser_catalog(language),
            "csp_nonce": getattr(g, "csp_nonce", ""),
            "prefs": prefs,
            "theme": theme,
            "palette": palette,
            "source_url": config.source_url,
            "images_enabled": config.images_enabled,
            "status_notices": _status_notices(),
        }


def _status_notices():
    from bananachat.services import status
    try:
        return status.notices()
    except Exception:  # noqa: BLE001 - a status problem must never break page rendering
        log.warning("Could not compute the service status", exc_info=True)
        return []


# ----- errors ---------------------------------------------------------------

def _install_error_handlers(app: Flask) -> None:
    def respond(code: int, message: str | None = None):
        if security.wants_json():
            codes = {400: "bad_request", 401: "auth_required", 403: "forbidden", 404: "not_found",
                     405: "method_not_allowed", 413: "too_large", 415: "unsupported_media_type",
                     429: "rate_limited", 500: "server_error", 503: "unavailable"}
            text = message or _default_message(code)
            return security.json_error(text, code, codes.get(code, "error"))
        try:
            return render_template("errors/error.html", code=code, message=message), code
        except Exception:  # the error page itself failed (e.g. database down)
            log.exception("Rendering the error page failed")
            return f"<!doctype html><title>{code}</title><h1>{code}</h1>", code

    @app.errorhandler(security.CSRFError)
    def csrf_failed(_error):
        return respond(400, translate(security.error_language(), "errors.csrf_expired"))

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return respond(413)

    @app.errorhandler(HTTPException)
    def http_error(error):
        if error.code is None or error.code < 400:
            return error
        message = error.description if error.code in (400, 403, 409, 415) and error.description and \
            not error.description.startswith(("The browser", "You don't", "The server")) else None
        return respond(error.code, message)

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        if db.is_unavailable(error):
            log.error("Database unavailable: %s", error)
            response = respond(503, translate(security.error_language(), "errors.storage_unavailable"))
            response = app.make_response(response)
            response.headers["Retry-After"] = "30"
            return response
        record_exception(app.config["BC"], error)
        return respond(500)

    @app.errorhandler(Exception)
    def unexpected(error):
        record_exception(app.config["BC"], error)
        return respond(500)


ERROR_KEYS = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "not_allowed", 415: "bad_request",
              413: "too_large", 429: "rate_limited", 500: "server_error", 503: "unavailable"}


def _default_message(code: int) -> str:
    key = ERROR_KEYS.get(code)
    return translate(security.error_language(), f"errors.{key}_text") if key else "Error."
