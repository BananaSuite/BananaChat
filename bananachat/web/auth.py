"""First-run setup, sign-in (with the two-step verification code), sign-up and sign-out."""

from __future__ import annotations

import hmac
import logging
import sqlite3
import time

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from bananachat import PRODUCT_NAME, db, security
from bananachat.db import invites, settings as site_settings, users
from bananachat.i18n import translate
from bananachat.services import hosting_signin, two_step

bp = Blueprint("auth", __name__)
log = logging.getLogger("bananachat.auth")

# Failed sign-ins and sign-ups are counted per client address (an IPv4 address or
# an IPv6 /64) and per account; the limits are BC_LOGIN_* and BC_SIGNUP* settings.
FAILURE_WINDOW = 15 * 60
SIGNUP_WINDOW = 3600
# A browser known to the account (its device cookie) has budgets of its own,
# but its address still counts it, against limits this many times higher: a
# client cannot multiply them by collecting device cookies.
KNOWN_BROWSER_ADDRESS_FACTOR = 10
# Accounts with two-step verification answer a code after the password. The
# sign-in waits in the session cookie for a few minutes, bound to the password
# and the two-step set-up (wrong codes are counted by two_step.check_code).
TWO_STEP_PENDING_KEY = "two_step_pending"
TWO_STEP_PENDING_SECONDS = 5 * 60
# A sign-in with a recovery code is never refused for this, but renewing the codes (confirmed with one of them)
# hands out ten more and the audit log is kept for good: past this many in the window, an account's further
# recovery-code sign-ins go to the application log instead of the audit log (as with sign-outs, MC1-3).
RECOVERY_CODE_AUDITS, RECOVERY_CODE_AUDIT_SECONDS = 10, 900


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
    users.mark_session_verified(security.current_session_hash())
    users.audit(user, "setup.complete", target=site_name, ip_address=security.client_ip())
    log.info("Initial administrator %s created", username)
    flash(_t("auth.setup_done", site=site_name), "success")
    return redirect(url_for("core.index"))


# ----- sign in ----------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if security.current_user() is not None:
        return redirect(security.safe_next_url(request.args.get("next")) or url_for("core.index"))
    next_url = security.safe_next_url(request.values.get("next"))
    if request.method == "GET":
        return _render("auth/login.html", next_url=next_url)
    if hosting_signin.effective_mode() == "hosting":
        # Only BananaWiki Hosting signs in; administrators have the recovery page (web.hosting).
        flash(_t("hosting.password_disabled"), "error")
        return _render("auth/login.html", 403, next_url=next_url)

    username = (request.form.get("username") or "").strip()[:64]
    password = request.form.get("password") or ""
    user = users.get_by_username(username)
    device = security.device_cookie_nonce(user) if user else None
    # A browser that has signed in to this account before is counted apart from
    # everyone else at its address (a school or office NAT, a VPN exit), so
    # strangers there can neither lock it out nor use up its request rate.
    if not allow_attempt(user, device):
        return security.too_many_requests()

    if not security.form_looks_human():
        flash(_t("auth.form_expired"), "error")
        return _render("auth/login.html", 400, next_url=next_url)

    account_key, limits = _failure_limits(user, username, device)
    # The attempt is counted before the password is checked, atomically with the
    # limit check, so parallel guesses cannot all pass it. It stays counted as a
    # failure unless the sign-in succeeds.
    attempt = users.reserve(limits, FAILURE_WINDOW)
    if attempt is None:
        flash(_t("auth.too_many_attempts"), "error")
        return _render("auth/login.html", 429, next_url=next_url)

    if user is None:
        security.burn_password_check(password)
        valid = False
    else:
        valid = security.verify_password(user["password"], password)

    def invalid_credentials():
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
        users.release(attempt)
        if users.is_suspended(fresh):
            flash(_t("auth.suspended"), "error")
            return _render("auth/login.html", 403, next_url=next_url)
        users.clear_hits(account_key)
        if rehashed:
            db.execute("UPDATE users SET password=? WHERE id=?", (rehashed, fresh["id"]))
            fresh = users.get(fresh["id"])
        if two_step.enabled(fresh["id"]):
            # The password was right; the session starts once a code is accepted.
            await_code(fresh, next_url)
            return redirect(url_for("auth.two_step"))
        start_session(fresh, "password")
    response = redirect(landing(fresh, next_url))
    # Signed with the current password hash (rehashed above if needed).
    security.set_device_cookie(response, fresh)
    return response


def _failure_limits(user, username: str, device: str | None):
    """``(account key, limits)`` that count a failed password for *username* (see :func:`login`)."""
    config = current_app.config["BC"]
    account_key = (f"login-fail-device:{user['id']}:{device}" if device
                   else "login-fail-user:" + username.lower())
    limits = [(account_key, config.login_failures_per_account)]
    if device:
        limits.append(("login-fail-ip-known:" + security.client_key(),
                       config.login_failures_per_address * KNOWN_BROWSER_ADDRESS_FACTOR))
    else:
        limits.append(("login-fail-ip:" + security.client_key(), config.login_failures_per_address))
    return account_key, limits


class Refused(Exception):
    """A password check failed; ``key`` is the translation key, ``status`` the HTTP status."""

    def __init__(self, key: str, status: int):
        super().__init__(key)
        self.key = key
        self.status = status


def check_password(username: str, password: str, *, accept=None):
    """The account *username* when *password* is its own, counted and limited like a sign-in, else :class:`Refused`.

    For the forms that prove an account with its password outside the sign-in form (connecting an
    account to BananaWiki Hosting, the administrators' recovery sign-in). *accept* (a function of the
    account) can turn a right password away; it then counts and reads as a wrong one, so the answer
    tells nothing about the account. A suspended account is refused after a right password, as at sign-in.
    """
    username = (username or "").strip()[:64]
    user = users.get_by_username(username) if username else None
    device = security.device_cookie_nonce(user) if user else None
    if not allow_attempt(user, device):
        raise Refused("auth.too_many_attempts", 429)
    account_key, limits = _failure_limits(user, username, device)
    attempt = users.reserve(limits, FAILURE_WINDOW)
    if attempt is None:
        raise Refused("auth.too_many_attempts", 429)
    if user is None:
        security.burn_password_check(password or "")
        valid = False
    else:
        valid = security.verify_password(user["password"], password or "")
    if not valid or (accept is not None and not accept(user)):
        log.info("Failed password check from %s", security.client_ip())
        raise Refused("auth.invalid_credentials", 401)
    users.release(attempt)
    users.clear_hits(account_key)
    if users.is_suspended(user):
        raise Refused("auth.suspended", 403)
    return user


def allow_attempt(user, device: str | None) -> bool:
    """Count a sign-in request: per known browser and its address, or per address."""
    config = current_app.config["BC"]
    if not device:
        return security.allow("login", config.login_attempts_per_minute, 60)
    return (security.allow("login", config.login_attempts_per_minute, 60, key=f"device:{user['id']}:{device}")
            and security.allow("login-known", config.login_attempts_per_minute * KNOWN_BROWSER_ADDRESS_FACTOR, 60))


def start_session(user, method: str) -> None:
    """Sign *user* in, inside the caller's transaction; the session counts as just confirmed."""
    users.touch_login(user["id"])
    security.login(user)
    users.mark_session_verified(security.current_session_hash())
    if user["role"] == "admin":
        users.audit(user, "auth.admin_login", details={"method": method}, ip_address=security.client_ip())


def landing(user, next_url: str | None) -> str:
    """Where a completed sign-in goes: the two-step set-up first when the site requires it of *user*."""
    if two_step.missing(user):
        flash(_t("two_step.required_notice"), "info")
        return url_for("account.two_step")
    return next_url or url_for("core.index")


# ----- two-step verification ------------------------------------------------

# How the first step was passed: the password form, BananaWiki Hosting, or the administrators' recovery page.
FIRST_STEPS = ("password", "hosting", "recovery")


def await_code(user, next_url: str | None, first_step: str = "password") -> None:
    """Keep the sign-in pending until :func:`two_step` accepts a code."""
    language = session.get("language")
    session.clear()
    if language:
        session["language"] = language
    session[TWO_STEP_PENDING_KEY] = {"user": user["id"], "at": time.time(), "via": first_step,
                                     "fingerprint": two_step.fingerprint(user), "next": next_url or ""}


def _pending_sign_in():
    """``(account, pending sign-in)`` while this browser owes a code, else None.

    It lapses after a few minutes, and when the password or the two-step
    set-up changed since the password was checked.
    """
    pending = session.get(TWO_STEP_PENDING_KEY)
    if not isinstance(pending, dict):
        return None
    try:
        started = float(pending.get("at", 0))
    except (TypeError, ValueError):
        return None
    if not 0 <= time.time() - started <= TWO_STEP_PENDING_SECONDS:
        return None
    user = users.get(str(pending.get("user") or ""))
    if user is None or not hmac.compare_digest(str(pending.get("fingerprint", "")), two_step.fingerprint(user)):
        return None
    return user, pending


@bp.route("/login/two-step", methods=["GET", "POST"], endpoint="two_step")
def two_step_code():
    if security.current_user() is not None:
        session.pop(TWO_STEP_PENDING_KEY, None)
        return redirect(url_for("core.index"))
    pending = _pending_sign_in()
    if pending is None:
        session.pop(TWO_STEP_PENDING_KEY, None)
        flash(_t("two_step.sign_in_again"), "info")
        return redirect(url_for("auth.login"))
    user, state = pending
    if request.method == "GET":
        return _render("auth/two_step.html")

    if not allow_attempt(user, security.device_cookie_nonce(user)):
        return security.too_many_requests()
    try:
        method = two_step.check_code(user, request.form.get("code"))
    except two_step.Refused as refusal:
        if refusal.status == 401:
            log.info("Wrong two-step code from %s", security.client_ip())
        flash(_t(refusal.key), "error")
        return _render("auth/two_step.html", refusal.status)
    with db.transaction():
        fresh = users.get(user["id"])
        session.pop(TWO_STEP_PENDING_KEY, None)
        if fresh is None or fresh["password"] != user["password"]:
            flash(_t("two_step.sign_in_again"), "info")
            return redirect(url_for("auth.login"))
        if users.is_suspended(fresh):
            flash(_t("auth.suspended"), "error")
            return redirect(url_for("auth.login"))
        first_step = state.get("via") if state.get("via") in FIRST_STEPS else "password"
        if (first_step == "recovery" and fresh["role"] != "admin") or (
                first_step == "password" and hosting_signin.effective_mode() == "hosting"):
            # The recovery page is for administrators only, and passwords stop with hosting-only sign-in (web.hosting).
            flash(_t("two_step.sign_in_again"), "info")
            return redirect(url_for("auth.login"))
        start_session(fresh, f"{first_step}+{method}")
    if method == "recovery_code":
        if security.allow("auth-recovery-code-audit", RECOVERY_CODE_AUDITS, RECOVERY_CODE_AUDIT_SECONDS,
                          key="user:" + fresh["id"]):
            users.audit(fresh, "auth.two_step_recovery_code_used", fresh["id"], ip_address=security.client_ip())
        else:
            log.info("User %s signed in with a recovery code without an audit entry: more than %d in %d seconds",
                     fresh["id"], RECOVERY_CODE_AUDITS, RECOVERY_CODE_AUDIT_SECONDS)
        left = (two_step.state(fresh["id"]) or {}).get("codes_left", 0)
        flash(_t("two_step.recovery_used", count=left), "warning")
    response = redirect(landing(fresh, security.safe_next_url(state.get("next"))))
    # Issued only now: a browser becomes "known" to the account after both steps.
    security.set_device_cookie(response, fresh)
    return response


@bp.route("/admin-access", methods=["GET", "POST"])
def admin_access():
    """Former administrator sign-in path; sign-in now works during maintenance for everyone."""
    return redirect(url_for("auth.login", next=url_for("admin.dashboard")), 307 if request.method == "POST" else 302)


# ----- sign up ----------------------------------------------------------------

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    mode = g.settings.get("signup_mode") or "invite"
    if security.current_user() is not None:
        return redirect(url_for("core.index"))
    if mode == "disabled":
        flash(_t("auth.signup_closed"), "info")
        return redirect(url_for("auth.login"))
    if hosting_signin.effective_mode() == "hosting":
        # New accounts are made on the connect page after signing in with BananaWiki Hosting.
        return redirect(url_for("hosting.start", invite=request.values.get("invite", "")[:32] or None))
    if request.method == "GET":
        return _render("auth/signup.html", signup_mode=mode, invite=request.args.get("invite", "")[:32])

    # Accounts created and failed attempts (mistyped invitations, taken names)
    # have separate per-address limits, so a class signing up together is not
    # stopped by a few typos and invitation guessing stays slow. Both are
    # reserved atomically; the one that does not apply is released afterwards.
    config = current_app.config["BC"]
    address = security.client_key()
    attempt = users.reserve([("signup:" + address, config.signups_per_hour),
                             ("signup-fail:" + address, config.signup_failures_per_hour)], SIGNUP_WINDOW)
    if attempt is None:
        return security.too_many_requests()
    created = []
    try:
        return _signup(mode, created)
    finally:
        users.release(attempt[1:] if created else attempt[:1])


def _signup(mode: str, created: list):
    """Create the account; appends to *created* once it exists."""
    # The field the message is about gets the focus and is marked invalid.
    fields = {"auth.username_rules": "username", "auth.username_taken": "username",
              "auth.password_length": "password", "auth.password_mismatch": "password",
              "auth.invite_required": "invite_code", "auth.invite_invalid": "invite_code"}

    def fail(key, status=400, **params):
        flash(_t(key, **params), "error")
        field = fields.get(key)
        if field == "invite_code" and mode != "invite":
            field = None
        return _render("auth/signup.html", status, signup_mode=mode, invite=request.form.get("invite_code", "")[:32],
                       error_field=field)

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
            # Hashing runs outside the write lock. The administrator may have
            # closed registration or required invitations while it was running.
            # Authorize the account using the policy protected by this lock.
            mode = site_settings.get().get("signup_mode") or "invite"
            if mode == "disabled":
                flash(_t("auth.signup_closed"), "info")
                return redirect(url_for("auth.login"))
            role, invite = "user", None
            if mode == "invite":
                if not code:
                    raise PermissionError
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
    except PermissionError:
        return fail("auth.invite_required")
    except (ValueError, sqlite3.IntegrityError):
        return fail("auth.invite_invalid")
    created.append(user_id)
    user = users.get(user_id)
    users.touch_login(user_id)
    security.login(user)
    users.mark_session_verified(security.current_session_hash())
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
