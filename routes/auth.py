"""Authentication routes: login, signup, logout, setup."""

import sqlite3

from flask import render_template, request, redirect, url_for, session, flash

import config
import db
from bootstrap import authorize_setup, setup_authorized
from helpers import (
    get_current_user, rate_limit, _is_valid_username, get_safe_next_url,
    MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, generate_form_token, check_bot_protection,
    _get_dummy_hash,
)
from helpers._passwords import generate_password_hash, check_password_hash
from logger import log_action


def _clear_auth_session():
    language = session.get("language")
    session.clear()
    if language in {"en", "it"}:
        session["language"] = language


def register_auth_routes(app):
    app.jinja_env.globals["setup_authorized"] = setup_authorized


    @app.route("/setup", methods=["GET", "POST"])
    @rate_limit(5, 60)
    def setup():
        settings = db.get_site_settings()
        if settings and settings.get("setup_done"):
            return redirect(url_for("index"))

        if request.method == "GET" and request.args.get("setup_token") and authorize_setup():
            return redirect(url_for("setup"))

        if request.method == "POST":
            if not authorize_setup():
                flash("Enter the installation token to create the first administrator.", "error")
                return render_template("auth/setup.html", bot_form_token=generate_form_token()), 403
            blocked, reason = check_bot_protection(request)
            if blocked:
                flash(
                    "Page expired: please reload and try again."
                    if reason == "expired_token"
                    else "Submission rejected. Please try again.",
                    "error",
                )
                return render_template("auth/setup.html", bot_form_token=generate_form_token()), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            site_name = request.form.get("site_name", config.DISPLAY_NAME).strip()[:100] or config.DISPLAY_NAME

            errors = []
            if not username or not _is_valid_username(username):
                errors.append("Username must contain only letters, digits, underscores and hyphens.")
            if len(password) < MIN_PASSWORD_LENGTH:
                errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if len(password) > MAX_PASSWORD_LENGTH:
                errors.append("Password is too long.")
            if password != confirm:
                errors.append("Passwords do not match.")

            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("auth/setup.html", bot_form_token=generate_form_token()), 400

            try:
                uid = db.complete_initial_setup(username, generate_password_hash(password), site_name)
                _clear_auth_session()
                session["user_id"] = uid
                db.update_last_login(uid)
                log_action("setup_complete", request, user=username, user_id=uid)
                flash(f"Setup complete. Welcome to {config.DISPLAY_NAME}!", "success")
                return redirect(url_for("index"))
            except ValueError:
                return redirect(url_for("login"))
            except Exception:
                app.logger.exception("Initial administrator setup failed")
                flash("Setup failed. Check the server log and try again.", "error")
                return render_template("auth/setup.html", bot_form_token=generate_form_token()), 500

        return render_template("auth/setup.html", bot_form_token=generate_form_token())

    @app.route("/login", methods=["GET", "POST"])
    @rate_limit(20, 60)
    def login():
        settings = db.get_site_settings()
        if not settings or not settings.get("setup_done"):
            return redirect(url_for("setup"))

        if get_current_user():
            return redirect(url_for("index"))

        if request.method == "POST":
            blocked, reason = check_bot_protection(request)
            if blocked:
                flash(
                    "Page expired: please reload and try again."
                    if reason == "expired_token"
                    else "Submission rejected. Please try again.",
                    "error",
                )
                return render_template("auth/login.html", bot_form_token=generate_form_token()), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            ip = request.remote_addr or "unknown"

            if db.check_login_rate_limit(ip, max_attempts=10, window_seconds=60):
                flash("Too many failed attempts. Please wait a minute.", "error")
                return render_template("auth/login.html", bot_form_token=generate_form_token()), 429

            user = db.get_user_by_username(username)

            # Constant-time check even for unknown users
            dummy = _get_dummy_hash()
            stored_hash = user["password"] if user else dummy
            password_ok = check_password_hash(stored_hash, password)

            if not user or not password_ok:
                db.record_login_attempt(ip)
                log_action("login_failed", request, username=username)
                flash("Invalid username or password.", "error")
                return render_template("auth/login.html", bot_form_token=generate_form_token()), 401

            if user["suspended"]:
                if not db.check_suspension_expired(user["id"]):
                    flash("Your account has been suspended.", "error")
                    return render_template("auth/login.html", bot_form_token=generate_form_token()), 403

            db.clear_login_attempts(ip)
            _clear_auth_session()
            session["user_id"] = user["id"]
            session["session_version"] = user.get("session_version", 0)
            session.permanent = True
            db.update_last_login(user["id"])
            log_action("login_success", request, user=user, user_id=user["id"])

            next_url = get_safe_next_url(request.form.get("next") or request.args.get("next"))
            return redirect(next_url or url_for("index"))

        next_param = get_safe_next_url(request.args.get("next"))
        return render_template(
            "auth/login.html",
            bot_form_token=generate_form_token(),
            next=next_param or "",
        )

    @app.route("/signup", methods=["GET", "POST"])
    @rate_limit(10, 60)
    def signup():
        settings = db.get_site_settings()
        if not settings or not settings.get("setup_done"):
            return redirect(url_for("setup"))

        if get_current_user():
            return redirect(url_for("index"))

        signup_mode = settings.get("signup_mode", "invite")
        if signup_mode == "disabled":
            flash("Signups are currently disabled.", "error")
            return redirect(url_for("login"))

        if settings.get("maintenance_mode"):
            flash("The system is in maintenance mode.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            blocked, reason = check_bot_protection(request)
            if blocked:
                flash(
                    "Page expired: please reload and try again."
                    if reason == "expired_token"
                    else "Submission rejected. Please try again.",
                    "error",
                )
                return render_template(
                    "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
                ), 400

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            invite_code = request.form.get("invite_code", "").strip()

            errors = []
            if not username or not _is_valid_username(username):
                errors.append("Username must contain only letters, digits, underscores and hyphens.")
            if len(username) > 32:
                errors.append("Username must be 32 characters or fewer.")
            if len(password) < MIN_PASSWORD_LENGTH:
                errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if len(password) > MAX_PASSWORD_LENGTH:
                errors.append("Password is too long.")
            if password != confirm:
                errors.append("Passwords do not match.")

            invite_row = None
            if signup_mode == "invite":
                if not invite_code:
                    errors.append("An invite code is required.")
                else:
                    invite_row = db.validate_invite_code(invite_code)
                    if not invite_row:
                        errors.append("Invalid or expired invite code.")

            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template(
                    "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
                ), 400

            if db.get_user_by_username(username):
                flash("Username already taken.", "error")
                return render_template(
                    "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
                ), 409

            try:
                uid = db.create_signup_user(
                    username,
                    generate_password_hash(password),
                    invite_code=invite_code or None,
                )
                _clear_auth_session()
                session["user_id"] = uid
                session["session_version"] = 0
                session.permanent = True
                db.update_last_login(uid)
                log_action("signup_success", request, user=username, user_id=uid)
                flash(f"Welcome to {config.DISPLAY_NAME}!", "success")
                return redirect(url_for("index"))
            except (ValueError, sqlite3.IntegrityError):
                flash("The username or invite is no longer available. Please try again.", "error")
                return render_template(
                    "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
                ), 400
            except Exception:
                app.logger.exception("Account signup failed")
                flash("Signup failed. Please try again later.", "error")
                return render_template(
                    "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
                ), 500

        return render_template(
            "auth/signup.html", signup_mode=signup_mode, bot_form_token=generate_form_token()
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        user = get_current_user()
        if user:
            log_action("logout", request, user=user, user_id=user["id"])
        _clear_auth_session()
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))
