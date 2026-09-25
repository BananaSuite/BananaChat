"""BananaChat error handlers."""

import html
import os
import sqlite3
import sqlite_runtime
import traceback
import private_logs
from datetime import datetime, timezone

from flask import render_template, request, jsonify, redirect, url_for, flash, abort
from flask_wtf.csrf import CSRFError

import config
import db
from helpers import _safe_referrer, get_current_user
from logger import get_logger


_MINIMAL_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{status} | {heading}</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#0f0f0f;color:#e0e0e0;margin:0;
         display:flex;align-items:center;justify-content:center;min-height:100vh;}}
    .card{{background:#1a1a1a;border-radius:12px;padding:2.5rem 2rem;max-width:480px;
           width:90%;text-align:center;border:1px solid #333;}}
    h1{{font-size:1.5rem;margin:0 0 .75rem;color:#fff;}}
    p{{color:#aaa;line-height:1.5;margin:0 0 1.25rem;}}
    a{{display:inline-block;padding:.6rem 1.1rem;background:#facc15;color:#000;
       border-radius:8px;text-decoration:none;font-weight:600;}}
  </style>
</head>
<body>
  <div class="card">
    <h1>{status}: {heading}</h1>
    <p>{message}</p>
    <a href="/">Return home</a>
  </div>
</body>
</html>"""


def _dump_traceback(exc):
    try:
        location = f"{request.method} {request.path}"
        location = location.replace("\r", "").replace("\n", "")[:2048]
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=40))
        private_logs.append(os.path.join(config.INSTANCE_DIR, "errors.log"),
                            f"\n{datetime.now(timezone.utc).isoformat()} {location}\n{details}")
    except Exception:
        pass


def _wants_json():
    if request.path.startswith(("/v1/", "/admin/api/")):
        return True
    accept = request.accept_mimetypes
    if accept:
        try:
            best = accept.best_match(["application/json", "text/html"])
            return best == "application/json"
        except (ValueError, TypeError):
            pass
    return False


def _json_error(status_code, message):
    return jsonify({"error": message, "status": status_code}), status_code


def _safe_render(template_name, status_code, heading, message, **ctx):
    try:
        return render_template(template_name, **ctx), status_code
    except Exception:
        html = _MINIMAL_ERROR_HTML.format(status=status_code, heading=heading, message=message)
        return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


def register_error_handlers(app):

    @app.errorhandler(sqlite3.DatabaseError)
    def database_error(error):
        if not sqlite_runtime.is_unavailable(error):
            return internal_error(error)
        get_logger().error("Database unavailable; preserving storage for operator recovery", exc_info=error)
        message = "The service is temporarily unavailable. Please try again later."
        if _wants_json():
            response = jsonify({"error": message, "status": 503})
        else:
            response = app.make_response(_MINIMAL_ERROR_HTML.format(status=503, heading="Service unavailable", message=message))
        response.status_code = 503
        response.headers.update({"Retry-After": "30", "Cache-Control": "no-store"})
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf(e):
        if _wants_json():
            return _json_error(400, "CSRF token invalid. Refresh the page and try again.")
        flash("Your session expired. Please try again.", "error")
        return redirect(_safe_referrer() or url_for("login"))

    @app.errorhandler(400)
    def bad_request(e):
        if _wants_json():
            return _json_error(400, getattr(e, "description", "Bad request."))
        return _safe_render("errors/400.html", 400, "Bad Request", "The request could not be understood.")

    @app.errorhandler(403)
    def forbidden(e):
        if _wants_json():
            return _json_error(403, "Permission denied.")
        return _safe_render("errors/403.html", 403, "Forbidden", "You don't have permission to access this.")

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return _json_error(404, "Not found.")
        return _safe_render("errors/404.html", 404, "Not Found", "The page you were looking for doesn't exist.")

    @app.errorhandler(405)
    def method_not_allowed(e):
        if _wants_json():
            return _json_error(405, "Method not allowed.")
        return _safe_render("errors/405.html", 405, "Method Not Allowed", "This request method is not allowed.")

    @app.errorhandler(413)
    def request_too_large(e):
        if _wants_json():
            return _json_error(413, "Request too large. Check the maximum allowed upload size.")
        from flask import flash, redirect
        flash("The uploaded file is too large.", "error")
        referrer = _safe_referrer()
        if referrer:
            return redirect(referrer)
        return _safe_render("errors/400.html", 413, "File Too Large",
                            "The uploaded file exceeds the maximum allowed size.")

    @app.errorhandler(429)
    def too_many_requests(e):
        if _wants_json():
            return _json_error(429, "Too many requests. Please slow down.")
        return _safe_render("errors/429.html", 429, "Too Many Requests",
                            "You've made too many requests. Please wait a moment.")

    @app.errorhandler(500)
    def internal_error(e):
        original = getattr(e, "original_exception", None) or e
        _dump_traceback(original)
        try:
            get_logger().error("500 on %s %s: %s", request.method, request.path, original, exc_info=True)
        except Exception:
            pass
        if _wants_json():
            return _json_error(500, "Internal server error.")
        return _safe_render("errors/500.html", 500, "Internal Server Error",
                            "Something went wrong on our end. Please try again.")

    @app.route("/admin/error-log")
    def admin_error_log():
        user = get_current_user()
        if not user or user["role"] != "admin" or db.is_suspension_active(user):
            abort(404)
        log_path = os.path.join(config.INSTANCE_DIR, "errors.log")
        try:
            content = private_logs.tail(log_path)
        except FileNotFoundError:
            content = "No errors logged yet."
        except OSError:
            abort(404)
        return f"<pre>{html.escape(content)}</pre>", 200, {"Cache-Control": "private, no-store"}
