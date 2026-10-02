"""First-run setup, sign-in, sign-up and sign-out."""

from __future__ import annotations

import hmac
import logging
import sqlite3

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from bananachat import PRODUCT_NAME, db, security
from bananachat.db import invites, settings as site_settings, users
from bananachat.i18n import translate

bp = Blueprint("auth", __name__)
log = logging.getLogger("bananachat.auth")

FAILURE_WINDOW = 15 * 60
FAILURES_PER_ADDRESS = 20
FAILURES_PER_ACCOUNT = 10


def _t(key, **params):
    return translate(g.lang, key, **params)


def _render(template, status=200, **context):
    return render_template(template, **context), status


# ----- first administrator --------------------------------------------------

@bp.route("/setup", methods=["GET", "POST"])
@security.rate_limit("setup", 10, 300)
def setup():
    if g.settings.get("setup_done"):
        return redirect(url_for("core.index"))
    if request.method == "GET":
        return _render("auth/setup.html")

    config = current_app.config["BC"]
    supplied = (request.form.get("setup_token") or "").strip()
    if not hmac.compare_digest(supplied.encode(), config.setup_token.encode()):
        flash(_t("auth.setup_token_invalid"), "error")
        return _render("auth/setup.html", 403)
    if not security.form_looks_human():
        flash(_t("auth.form_expired"), "error")
        return _render("auth/setup.html", 400)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    site_name = " ".join((request.form.get("site_name") or "").split())[:100] or PRODUCT_NAME
    problem = None if users.USERNAME_RE.match(username) else "auth.username_rules"
    problem = problem or security.password_problem(password, request.form.get("confirm_password"))
    if problem:
        flash(_t(problem), "error")
        return _render("auth/setup.html", 400)

    try:
        with db.transaction():
            current = site_settings.get()
            if current.get("setup_done") or db.scalar("SELECT COUNT(*) FROM users", default=0):
                raise ValueError("already set up")
            user_id = users.create(username, security.hash_password(password), role="admin")
            site_settings.update(setup_done=1, site_name=site_name)
    except ValueError:
        return redirect(url_for("auth.login"))
    user = users.get(user_id)
    users.touch_login(user_id)
    security.login(user)
    users.audit(user, "setup.complete", target=site_name, ip_address=security.client_ip())
    log.info("Initial administrator %s created", username)
    flash(_t("auth.setup_done", site=site_name), "success")
    return redirect(url_for("core.index"))


# ----- sign in ----------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
@security.rate_limit("login", 30, 60)
def login():
    if security.current_user() is not None:
        return redirect(security.safe_next_url(request.args.get("next")) or url_for("core.index"))
    next_url = security.safe_next_url(request.values.get("next"))
    if request.method == "GET":
        return _render("auth/login.html", next_url=next_url)

    if not security.form_looks_human():
        flash(_t("auth.form_expired"), "error")
        return _render("auth/login.html", 400, next_url=next_url)

    username = (request.form.get("username") or "").strip()[:64]
    password = request.form.get("password") or ""
    address_key = "login-fail-ip:" + security.client_key()
    account_key = "login-fail-user:" + username.lower()
    if (users.count_hits(address_key, FAILURE_WINDOW) >= FAILURES_PER_ADDRESS
            or users.count_hits(account_key, FAILURE_WINDOW) >= FAILURES_PER_ACCOUNT):
        flash(_t("auth.too_many_attempts"), "error")
        return _render("auth/login.html", 429, next_url=next_url)

    user = users.get_by_username(username)
    if user is None:
        security.burn_password_check(password)
        valid = False
    else:
        valid = security.verify_password(user["password"], password)

    def invalid_credentials():
        users.hit(address_key, 10**6, FAILURE_WINDOW)
        users.hit(account_key, 10**6, FAILURE_WINDOW)
        log.info("Failed sign-in from %s", security.client_ip())
        flash(_t("auth.invalid_credentials"), "error")
        return _render("auth/login.html", 401, next_url=next_url)

    if not valid:
        return invalid_credentials()
    # Password hashing stays outside the write lock. Recheck the account under
    # that lock before any writes or session issuance so a concurrent password
    # reset, suspension or session revocation cannot be undone by this login.
    rehashed = security.hash_password(password) if security.needs_rehash(user["password"]) else None
    with db.transaction():
        fresh = users.get(user["id"])
        if (fresh is None or fresh["password"] != user["password"]
                or fresh["session_version"] != user["session_version"]):
            return invalid_credentials()
        if users.is_suspended(fresh):
            flash(_t("auth.suspended"), "error")
            return _render("auth/login.html", 403, next_url=next_url)
        users.clear_hits(account_key)
        if rehashed:
            db.execute("UPDATE users SET password=? WHERE id=?", (rehashed, fresh["id"]))
        users.touch_login(fresh["id"])
        security.login(fresh)
        if fresh["role"] == "admin":
            users.audit(fresh, "auth.admin_login", ip_address=security.client_ip())
    return redirect(next_url or url_for("core.index"))


@bp.route("/admin-access", methods=["GET", "POST"])
def admin_access():
    """Former administrator sign-in path; sign-in now works during maintenance for everyone."""
    return redirect(url_for("auth.login", next=url_for("admin.dashboard")), 307 if request.method == "POST" else 302)


# ----- sign up ----------------------------------------------------------------

@bp.route("/signup", methods=["GET", "POST"])
@security.rate_limit("signup", 10, 3600)
def signup():
    mode = g.settings.get("signup_mode") or "invite"
    if security.current_user() is not None:
        return redirect(url_for("core.index"))
    if mode == "disabled":
        flash(_t("auth.signup_closed"), "info")
        return redirect(url_for("auth.login"))
    if request.method == "GET":
        return _render("auth/signup.html", signup_mode=mode, invite=request.args.get("invite", "")[:32])

    def fail(key, status=400, **params):
        flash(_t(key, **params), "error")
        return _render("auth/signup.html", status, signup_mode=mode, invite=request.form.get("invite_code", "")[:32])

    if not security.form_looks_human():
        return fail("auth.form_expired")
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    code = (request.form.get("invite_code") or "").strip()
    if not users.USERNAME_RE.match(username):
        return fail("auth.username_rules")
    problem = security.password_problem(password, request.form.get("confirm_password"))
    if problem:
        return fail(problem)
    if mode == "invite" and not code:
        return fail("auth.invite_required")

    password_hash = security.hash_password(password)
    try:
        with db.transaction():
            role, invite = "user", None
            if mode == "invite":
                # Checked first, so visitors without an invitation cannot probe usernames.
                invite = invites.find_usable(code)
                if invite is None:
                    raise ValueError
                role = invite["assigned_role"] or "user"
            if users.get_by_username(username):
                raise LookupError
            # Only an invitation actually used is recorded (open sign-up ignores the field).
            user_id = users.create(username, password_hash, role=role,
                                   invite_code=invite["code"].upper() if invite else None)
            if mode == "invite":
                invites.consume(code, user_id)
    except LookupError:
        return fail("auth.username_taken", 409)
    except (ValueError, sqlite3.IntegrityError):
        return fail("auth.invite_invalid")
    user = users.get(user_id)
    users.touch_login(user_id)
    security.login(user)
    log.info("New account %s", username)
    flash(_t("auth.welcome", site=g.settings.get("site_name") or PRODUCT_NAME), "success")
    return redirect(url_for("core.index"))


# ----- sign out -------------------------------------------------------------

@bp.route("/logout", methods=["GET", "POST"])
def logout():
    if request.method == "GET":
        # Signing out changes state, so it needs the CSRF-protected form.
        if security.current_user() is None:
            return redirect(url_for("auth.login"))
        return _render("auth/logout.html")
    security.logout()
    flash(_t("auth.signed_out"), "info")
    return redirect(url_for("auth.login"))
