"""Sign in with BananaWiki Hosting: the flow, the first sign-in's connect page, the account link and the
administrators' recovery sign-in.

* ``/login/hosting/start`` and ``/login/hosting/callback``: the OAuth flow (see services.hosting_signin).
  A linked account signs in without this site's two-step code: BananaWiki Hosting asked for its own.
  A hosting administrator acting as an account (``act``) signs in to that account only when it is
  already linked and the site allows it; the session shows who is acting, refuses the account's
  sensitive actions and ends when acting ends. Acting never links or creates an account.
* ``/login/hosting/start?create=1`` (``hosting`` mode): "Create an account" on the sign-in page. The portal
  shows its sign-up form first (``prompt=create``); back here, the new portal account gets its account at
  once, named like it, under the sign-up policy (the connect page takes over when that cannot be: the name
  is taken, an invitation is needed). Never while acting.
* ``/login/hosting/connect``: the first sign-in of a portal account here. It creates a new account
  (``/login/hosting/create``, under the sign-up policy, with an unusable random password and
  ``password_set`` 0: it can set one later, after confirming with BananaWiki Hosting) or connects
  one that exists, proven with its password (``/login/hosting/connect-existing``) and its two-step code
  (``/login/hosting/connect/code``). Nothing is ever matched by user name or email.
* ``/account/hosting/link`` and ``/account/hosting/unlink``: from the account page, with the password
  (or a recent confirmation). Disconnecting needs a password of the account's own: without one nothing
  else signs it in.
* ``/account/hosting/confirm``: a fresh sign-in at BananaWiki Hosting confirms the signed-in, linked
  person instead of the password (services.hosting_signin), for ten minutes.
* ``/login/recovery``: in ``hosting`` mode, administrators can still sign in with their password (and
  two-step code). Not linked from the sign-in page, rate-limited, every attempt audited.

Every route answers 404 while the mode in force is ``local`` (the recovery page: unless it is ``hosting``).
"""

from __future__ import annotations

import functools
import hmac
import logging
import secrets
import sqlite3
import time

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from bananachat import PRODUCT_NAME, db, security
from bananachat.db import hosting_links, invites, users
from bananachat.db import settings as site_settings
from bananachat.i18n import translate
from bananachat.services import hosting_signin, two_step
from bananachat.services.hosting_signin import OAuthError
from bananachat.web import auth

bp = Blueprint("hosting", __name__)
log = logging.getLogger("bananachat.hosting")

PENDING_KEY = "hosting_pending"
PENDING_SECONDS = 15 * 60
# A connection whose password was right waits here for the account's two-step code.
CONNECT_CODE_KEY = "hosting_connect_code"
# Audit entries for hosting sign-ins of ordinary accounts, per account (administrators' are always audited).
SIGN_IN_AUDITS, SIGN_IN_AUDIT_SECONDS = 10, 900
RECOVERY_ATTEMPTS, RECOVERY_WINDOW = 10, 900


def _t(key, **params):
    return translate(g.lang, key, **params)


@bp.app_context_processor
def _template_helpers():
    return {"signin_mode": hosting_signin.effective_mode, "hosting_signup_open": hosting_signin.portal_signup_open}


def _enabled(view):
    """404 while sign-in with BananaWiki Hosting is not in force."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if hosting_signin.effective_mode() == "local":
            abort(404)
        return view(*args, **kwargs)
    return wrapper


def _printable(text: str) -> str:
    """A short, single-line excerpt of what the portal sent, for the log."""
    return "".join(char for char in text[:40] if char.isprintable())


def _fail(error: OAuthError, endpoint: str = "auth.login", **values):
    log.warning("Sign-in with BananaWiki Hosting failed: %s", error)
    flash(_t(error.key), "error")
    return redirect(url_for(endpoint, **values))


# ----- the flow -----------------------------------------------------------------

@bp.get("/login/hosting/start", endpoint="start")
@_enabled
def start():
    if security.current_user() is not None:
        return redirect(url_for("core.index"))
    next_url = security.safe_next_url(request.args.get("next")) or ""
    invite = (request.args.get("invite") or "").strip()[:32]
    # "Create an account" on the sign-in page, which only BananaWiki Hosting signs in to.
    create = request.args.get("create") == "1" and hosting_signin.effective_mode() == "hosting"
    return redirect(hosting_signin.authorize_url("login", next_url=next_url, invite=invite, create=create))


@bp.get("/login/hosting/callback", endpoint="callback")
@_enabled
def callback():
    if not security.allow("hosting-callback", current_app.config["BC"].login_attempts_per_minute, 60):
        return security.too_many_requests()
    state = request.args.get("state", "")
    try:
        entry = hosting_signin.consume_state(state)
    except OAuthError as error:
        return _fail(error, "account.index" if security.current_user() else "auth.login")
    if entry["purpose"] == "link":
        return _link_callback(entry, state)
    if entry["purpose"] == "confirm":
        return _confirm_callback(entry, state)
    try:
        if request.args.get("error"):
            raise OAuthError("hosting.error_cancelled", "the portal answered " + _printable(request.args["error"]))
        profile = hosting_signin.complete(request.args.get("code", ""), state)
    except OAuthError as error:
        return _fail(error)
    next_url = security.safe_next_url(entry.get("next")) or ""
    user = hosting_signin.linked_user(profile.sub)
    if profile.act is not None:
        session.pop(PENDING_KEY, None)
        return _acting_sign_in(profile, user, next_url)
    if user is not None:
        session.pop(PENDING_KEY, None)
        return _finish(user, next_url)
    session.pop(CONNECT_CODE_KEY, None)
    pending = {"sub": profile.sub, "username": profile.username, "next": next_url,
               "invite": str(entry.get("invite") or "")[:32], "at": time.time()}
    session[PENDING_KEY] = pending
    if entry.get("create"):
        # The portal account was just made for this site: nothing to choose, its account here follows.
        return _create_within_budgets(pending, (profile.username or "")[:64], pending["invite"])
    return redirect(url_for("hosting.connect"))


def _finish(user, next_url: str, *, code_given: str | None = None):
    """Sign *user* in after BananaWiki Hosting vouched for it.

    No two-step code is asked here: BananaWiki Hosting asked for its own. *code_given* names the code given
    when an existing account was connected with its password (for the administrators' audit entry).
    """
    with db.transaction():
        fresh = users.get(user["id"])
        if fresh is None:
            flash(_t("hosting.error_expired"), "error")
            return redirect(url_for("auth.login"))
        if users.is_suspended(fresh):
            flash(_t("auth.suspended"), "error")
            return redirect(url_for("auth.login"))
        auth.start_session(fresh, "hosting" + (f"+{code_given}" if code_given else ""))
    _audit_sign_in(fresh)
    response = redirect(auth.landing(fresh, next_url or None))
    security.set_device_cookie(response, fresh)
    return response


def _acting_sign_in(profile, user, next_url: str):
    """A hosting administrator acting as the account: signed in to an existing link only, if the site allows it.

    The session ends when acting ends; it gets no device cookie (the browser is the administrator's).
    """
    admin_name = profile.act.get("name") or profile.act["sub"]
    details = {"acting_admin": admin_name, "acting_admin_sub": profile.act["sub"],
               "until": db.timestamp(profile.act_until)}
    if not hosting_signin.acting_allowed():
        users.audit(None, "auth.hosting_acting_refused", user["id"] if user else profile.sub,
                    {**details, "reason": "not allowed"}, security.client_ip())
        return _fail(OAuthError("hosting.error_acting_off", f"{admin_name} acting refused by the site setting"))
    if user is None:
        users.audit(None, "auth.hosting_acting_refused", profile.sub, {**details, "reason": "not linked"},
                    security.client_ip())
        return _fail(OAuthError("hosting.error_acting_unknown", f"{admin_name} acting as an account not linked here"))
    with db.transaction():
        fresh = users.get(user["id"])
        if fresh is None:
            flash(_t("hosting.error_expired"), "error")
            return redirect(url_for("auth.login"))
        if users.is_suspended(fresh):
            flash(_t("auth.suspended"), "error")
            return redirect(url_for("auth.login"))
        users.touch_login(fresh["id"])
        security.login(fresh, acting={**profile.act, "until": profile.act_until})
    # Recorded under the administrator's name (users.audit reads it from the session for the next requests).
    users.audit(fresh, "auth.hosting_acting_login", fresh["id"], details, security.client_ip(),
                acting={"acting_sub": profile.act["sub"], "acting_name": profile.act.get("name") or ""})
    log.info("%s (BananaWiki Hosting) signed in as %s", admin_name, fresh["username"])
    return redirect(next_url or url_for("core.index"))


def _audit_sign_in(user) -> None:
    """Administrators' sign-ins are audited by auth.start_session; others' here, a few per account at a time."""
    if user["role"] == "admin":
        return
    if security.allow("hosting-sign-in-audit", SIGN_IN_AUDITS, SIGN_IN_AUDIT_SECONDS, key="user:" + user["id"]):
        users.audit(user, "auth.hosting_login", user["id"], ip_address=security.client_ip())
    else:
        log.info("User %s signed in with BananaWiki Hosting without an audit entry: more than %d in %d seconds",
                 user["id"], SIGN_IN_AUDITS, SIGN_IN_AUDIT_SECONDS)


# ----- the first sign-in: create an account or connect one ------------------------

def _pending():
    pending = session.get(PENDING_KEY)
    if not isinstance(pending, dict) or not isinstance(pending.get("sub"), str):
        return None
    try:
        age = time.time() - float(pending.get("at", 0))
    except (TypeError, ValueError):
        return None
    return pending if 0 <= age <= PENDING_SECONDS else None


def _pending_or_restart():
    """``(pending, None)`` while this browser has a first sign-in waiting, else ``(None, response)``."""
    if security.current_user() is not None:
        return None, redirect(url_for("core.index"))
    pending = _pending()
    if pending is None:
        session.pop(PENDING_KEY, None)
        return None, _fail(OAuthError("hosting.error_expired", "no pending first sign-in"))
    return pending, None


def _connect_page(pending, status: int = 200, *, create_error: str = "", existing_error: str = "",
                  username: str | None = None, invite: str | None = None, existing_username: str = ""):
    signup_mode = g.settings.get("signup_mode") or "invite"
    portal_name = pending.get("username") or ""
    suggestion = hosting_signin.suggest_username(portal_name) if signup_mode != "disabled" else ""
    renamed = bool(portal_name) and suggestion != portal_name and hosting_signin.name_problem(portal_name) is not None
    return render_template(
        "auth/hosting_connect.html", pending=pending, portal_name=portal_name, signup_mode=signup_mode,
        username=suggestion if username is None else username, renamed=renamed and username is None,
        invite=pending.get("invite", "") if invite is None else invite, create_error=create_error,
        existing_error=existing_error, existing_username=existing_username,
    ), status


@bp.get("/login/hosting/connect", endpoint="connect")
@_enabled
def connect():
    pending, refusal = _pending_or_restart()
    if refusal is not None:
        return refusal
    session.pop(CONNECT_CODE_KEY, None)  # back from the code step: the choice starts again
    return _connect_page(pending)


@bp.post("/login/hosting/create", endpoint="create")
@_enabled
def create():
    """Create a new account for the portal account, under the sign-up policy (and its invitations)."""
    pending, refusal = _pending_or_restart()
    if refusal is not None:
        return refusal
    username = (request.form.get("username") or "").strip()[:64]
    code = (request.form.get("invite_code") or "").strip()[:32]
    if not security.form_looks_human():
        return _connect_page(pending, 400, create_error=_t("auth.form_expired"), username=username, invite=code)
    return _create_within_budgets(pending, username, code)


def _create_within_budgets(pending, username: str, code: str):
    """:func:`_create`, counted like /signup: accounts made and failed attempts per address, the same budgets."""
    config = current_app.config["BC"]
    address = security.client_key()
    attempt = users.reserve([("signup:" + address, config.signups_per_hour),
                             ("signup-fail:" + address, config.signup_failures_per_hour)], auth.SIGNUP_WINDOW)
    if attempt is None:
        return security.too_many_requests()
    created = []
    try:
        return _create(pending, username, code, created)
    finally:
        users.release(attempt[1:] if created else attempt[:1])


def _create(pending, username: str, code: str, created: list):
    def fail(key, status=400, **params):
        message = _t(key, **params)
        if key in ("auth.username_rules", "auth.username_taken"):
            message = _t("hosting.name_unavailable", reason=message,
                         suggestion=hosting_signin.suggest_username(username))
        return _connect_page(pending, status, create_error=message, username=username, invite=code)

    if not users.USERNAME_RE.match(username):
        return fail("auth.username_rules")
    password_hash = security.hash_password(secrets.token_urlsafe(32))  # never told to anyone
    try:
        with db.transaction():
            # The sign-up policy as it is under the write lock (hashing ran outside it).
            mode = site_settings.get().get("signup_mode") or "invite"
            if mode == "disabled":
                raise auth.Refused("auth.signup_closed", 403)
            role, invite = "user", None
            if mode == "invite":
                if not code:
                    raise auth.Refused("auth.invite_required", 400)
                invite = invites.find_usable(code)
                if invite is None:
                    raise ValueError
                role = invite["assigned_role"] or "user"
            if hosting_signin.linked_user(pending["sub"]) is not None:
                raise OAuthError("hosting.error_account_taken")
            if users.get_by_username(username):
                raise LookupError
            user_id = users.create(username, password_hash, role=role,
                                   invite_code=invite["code"].upper() if invite else None, password_set=False)
            hosting_links.create(user_id, pending["sub"], pending.get("username") or "")
            if mode == "invite":
                invites.consume(code, user_id)
    except auth.Refused as refusal:
        return fail(refusal.key, refusal.status)
    except LookupError:
        return fail("auth.username_taken", 409)
    except OAuthError as error:
        return _connect_page(pending, 409, create_error=_t(error.key), username=username, invite=code)
    except (ValueError, sqlite3.IntegrityError):
        return fail("auth.invite_invalid")
    created.append(user_id)
    user = users.get(user_id)
    users.audit(user, "auth.hosting_link", user_id, {"via": "new account", "portal_username": pending.get("username")},
                security.client_ip())
    log.info("New account %s through BananaWiki Hosting", username)
    flash(_t("auth.welcome", site=g.settings.get("site_name") or PRODUCT_NAME), "success")
    return _finish(user, security.safe_next_url(pending.get("next")) or "")


def _link_existing(pending, user) -> None:
    """Connect the portal account to *user*, whose password (and code) were just proven; else OAuthError."""
    with db.transaction():
        fresh = users.get(user["id"])
        if fresh is None or fresh["password"] != user["password"] or users.is_suspended(fresh):
            raise OAuthError("hosting.error_expired", "the account changed while it was being connected")
        current = hosting_links.link_of(user["id"])
        if current is not None:
            if current["account_id"] != pending["sub"]:
                raise OAuthError("hosting.error_local_linked")
            return  # another tab finished first
        if hosting_signin.linked_user(pending["sub"]) is not None:
            raise OAuthError("hosting.error_account_taken")
        hosting_links.create(user["id"], pending["sub"], pending.get("username") or "")
    users.audit(user, "auth.hosting_link", user["id"], {"via": "existing account",
                                                         "portal_username": pending.get("username")},
                security.client_ip())


@bp.post("/login/hosting/connect-existing", endpoint="connect_existing")
@_enabled
def connect_existing():
    """Connect an account that exists here: its user name and password, then its two-step code when it has one.

    Any account can be connected (not only one named like the portal account), and only with its password.
    """
    pending, refusal = _pending_or_restart()
    if refusal is not None:
        return refusal
    session.pop(CONNECT_CODE_KEY, None)
    username = (request.form.get("username") or "").strip()[:64]
    if not security.form_looks_human():
        return _connect_page(pending, 400, existing_error=_t("auth.form_expired"), existing_username=username)
    try:
        user = auth.check_password(username, request.form.get("password") or "")
    except auth.Refused as refusal:
        return _connect_page(pending, refusal.status, existing_error=_t(refusal.key), existing_username=username)
    current = hosting_links.link_of(user["id"])
    if current is not None and current["account_id"] != pending["sub"]:
        return _connect_page(pending, 409, existing_error=_t("hosting.error_local_linked"),
                             existing_username=username)
    if two_step.enabled(user["id"]):
        session[CONNECT_CODE_KEY] = {"sub": pending["sub"], "user": user["id"], "at": time.time(),
                                     "fingerprint": two_step.fingerprint(user)}
        return redirect(url_for("hosting.connect_code"))
    try:
        _link_existing(pending, user)
    except OAuthError as error:
        return _connect_page(pending, 409, existing_error=_t(error.key), existing_username=username)
    session.pop(PENDING_KEY, None)
    flash(_t("hosting.linked"), "success")
    return _finish(user, security.safe_next_url(pending.get("next")) or "", code_given="password")


def _password_passed(pending):
    """The account whose password this browser gave in this connection, while it owes its two-step code."""
    state = session.get(CONNECT_CODE_KEY)
    if not isinstance(state, dict) or state.get("sub") != pending["sub"]:
        return None
    try:
        age = time.time() - float(state.get("at", 0))
    except (TypeError, ValueError):
        return None
    user = users.get(str(state.get("user") or ""))
    if user is None or not 0 <= age <= auth.TWO_STEP_PENDING_SECONDS or not two_step.enabled(user["id"]):
        return None
    if not hmac.compare_digest(str(state.get("fingerprint", "")), two_step.fingerprint(user)):
        return None
    return user


@bp.route("/login/hosting/connect/code", methods=["GET", "POST"], endpoint="connect_code")
@_enabled
def connect_code():
    """The second step of connecting an existing account: the link is made only once its code is accepted."""
    pending, refusal = _pending_or_restart()
    if refusal is not None:
        return refusal
    user = _password_passed(pending)
    if user is None:
        session.pop(CONNECT_CODE_KEY, None)
        flash(_t("two_step.sign_in_again"), "info")
        return redirect(url_for("hosting.connect"))
    if request.method == "GET":
        return render_template("auth/hosting_code.html", username=user["username"])
    if not auth.allow_attempt(user, security.device_cookie_nonce(user)):
        return security.too_many_requests()
    try:
        method = two_step.check_code(user, request.form.get("code"))
    except two_step.Refused as refusal:
        flash(_t(refusal.key), "error")
        return render_template("auth/hosting_code.html", username=user["username"]), refusal.status
    session.pop(CONNECT_CODE_KEY, None)
    if method == "recovery_code":
        users.audit(user, "auth.two_step_recovery_code_used", user["id"], ip_address=security.client_ip())
    try:
        _link_existing(pending, user)
    except OAuthError as error:
        return _connect_page(pending, 409, existing_error=_t(error.key), existing_username=user["username"])
    session.pop(PENDING_KEY, None)
    flash(_t("hosting.linked"), "success")
    return _finish(user, security.safe_next_url(pending.get("next")) or "", code_given=method)


# ----- the account page -----------------------------------------------------------

def _account(anchor: str = "hosting"):
    return redirect(url_for("account.index", _anchor=anchor))


def _confirmed(user) -> bool:
    """The password typed with the form, or a confirmation with BananaWiki Hosting in the last ten minutes."""
    if hosting_signin.confirmed_recently():
        return True
    confirmed = security.confirm_password(user, request.form.get("password") or "")
    if not confirmed:
        flash(_t("account.password_wrong" if confirmed is False else "auth.too_many_attempts"), "error")
    return bool(confirmed)


# ----- confirming with BananaWiki Hosting -------------------------------------------------

@bp.post("/account/hosting/confirm", endpoint="confirm")
@security.login_required
@_enabled
@security.rate_limit("account-hosting-confirm", 20, 900, per_user=True)
def confirm():
    """Start a confirmation: a fresh sign-in at BananaWiki Hosting stands for the password for ten minutes."""
    user = security.current_user()
    next_url = security.safe_next_url(request.form.get("next")) or url_for("account.index", _anchor="security")
    if security.acting() is not None:
        flash(_t("hosting.acting_refused"), "error")
        return redirect(next_url)
    if hosting_links.link_of(user["id"]) is None:
        flash(_t("hosting.not_linked"), "info")
        return redirect(next_url)
    return redirect(hosting_signin.authorize_url("confirm", next_url=next_url, user_id=user["id"],
                                                 session_hash=security.current_session_hash() or ""))


def _confirm_callback(entry, state: str):
    user = security.current_user()
    next_url = security.safe_next_url(entry.get("next")) or url_for("account.index", _anchor="security")
    try:
        session_hash = security.current_session_hash() or ""
        if (user is None or not hmac.compare_digest(str(entry.get("uid") or ""), user["id"])
                or not session_hash or not hmac.compare_digest(str(entry.get("sid") or ""), session_hash)):
            raise OAuthError("hosting.error_expired", "a confirmation finished in another session")
        if security.acting() is not None:
            raise OAuthError("hosting.acting_refused", "a confirmation in an acting session")
        if request.args.get("error"):
            raise OAuthError("hosting.error_cancelled", "the portal answered " + _printable(request.args["error"]))
        profile = hosting_signin.complete(request.args.get("code", ""), state)
        link = hosting_links.link_of(user["id"])
        problem = hosting_signin.confirmation_problem(profile, link["account_id"] if link else "",
                                                      float(entry.get("at") or 0))
        if problem:
            raise OAuthError(problem, f"confirmation refused: {problem.split('.')[-1]}")
    except OAuthError as error:
        if user is None:
            return _fail(error)
        log.warning("Confirming with BananaWiki Hosting failed: %s", error)
        flash(_t(error.key), "error")
        return redirect(next_url)
    users.mark_session_hosting_confirmed(security.current_session_hash())
    users.audit(user, "account.hosting_confirm", user["id"], {"auth_time": profile.auth_time},
                security.client_ip())
    flash(_t("hosting.confirmed"), "success")
    return redirect(next_url)


@bp.post("/account/hosting/link", endpoint="link")
@security.login_required
@_enabled
@security.rate_limit("account-hosting", 20, 900, per_user=True)
def link():
    """Connect the signed-in account: its password first, then the portal."""
    user = security.current_user()
    if security.acting() is not None:
        flash(_t("hosting.acting_refused"), "error")
        return _account()
    if hosting_links.link_of(user["id"]) is not None:
        flash(_t("hosting.already_linked"), "info")
        return _account()
    if not _confirmed(user):
        return _account()
    return redirect(hosting_signin.authorize_url("link", user_id=user["id"]))


def _link_callback(entry, state: str):
    user = security.current_user()
    try:
        if user is None or not hmac.compare_digest(str(entry.get("uid") or ""), user["id"]):
            raise OAuthError("hosting.error_expired", "a link flow finished in another session")
        if security.acting() is not None:
            raise OAuthError("hosting.acting_refused", "a link flow finished in an acting session")
        if request.args.get("error"):
            raise OAuthError("hosting.error_cancelled", "the portal answered " + _printable(request.args["error"]))
        profile = hosting_signin.complete(request.args.get("code", ""), state)
        if profile.act is not None:
            raise OAuthError("hosting.error_acting_link", "an acting administrator tried to link an account")
        account_id, portal_username = profile.sub, profile.username
        with db.transaction():
            if hosting_links.link_of(user["id"]) is not None:
                raise OAuthError("hosting.error_local_linked")
            if hosting_signin.linked_user(account_id) is not None:
                raise OAuthError("hosting.error_account_taken")
            hosting_links.create(user["id"], account_id, portal_username)
    except OAuthError as error:
        return _fail(error, "account.index" if user else "auth.login", **({"_anchor": "hosting"} if user else {}))
    users.audit(user, "auth.hosting_link", user["id"], {"via": "account page", "portal_username": portal_username},
                security.client_ip())
    flash(_t("hosting.linked"), "success")
    return _account()


@bp.post("/account/hosting/unlink", endpoint="unlink")
@security.login_required
@_enabled
@security.rate_limit("account-hosting", 20, 900, per_user=True)
def unlink():
    """Disconnect the signed-in account, with its password; never in ``hosting`` mode or without a password of
    its own (no other way in)."""
    user = security.current_user()
    if security.acting() is not None:
        flash(_t("hosting.acting_refused"), "error")
        return _account()
    if hosting_signin.effective_mode() == "hosting":
        flash(_t("hosting.unlink_refused"), "error")
        return _account()
    if not users.has_password(user):
        flash(_t("hosting.unlink_no_password"), "error")
        return _account()
    if hosting_links.link_of(user["id"]) is None:
        flash(_t("hosting.not_linked"), "info")
        return _account()
    if not _confirmed(user):
        return _account()
    removed = hosting_links.remove(user["id"])
    if removed is not None:
        users.audit(user, "account.hosting_unlink", user["id"], {"portal_username": removed["portal_username"]},
                    security.client_ip())
    flash(_t("hosting.unlinked"), "success")
    return _account()


# ----- recovery ------------------------------------------------------------------

@bp.route("/login/recovery", methods=["GET", "POST"], endpoint="recovery")
@security.rate_limit("login-recovery", RECOVERY_ATTEMPTS, RECOVERY_WINDOW)
def recovery():
    """Administrators' password sign-in while only BananaWiki Hosting signs in (not linked from anywhere)."""
    if hosting_signin.effective_mode() != "hosting":
        abort(404)
    if security.current_user() is not None:
        return redirect(url_for("core.index"))
    if request.method == "GET":
        return render_template("auth/recovery.html")
    username = (request.form.get("username") or "").strip()[:64]
    if not security.form_looks_human():
        flash(_t("auth.form_expired"), "error")
        return render_template("auth/recovery.html"), 400
    try:
        # Accounts that are not administrators read as a wrong password, and count as one.
        user = auth.check_password(username, request.form.get("password") or "",
                                   accept=lambda account: account["role"] == "admin")
    except auth.Refused as refusal:
        users.audit(None, "auth.recovery_login_failed", username, {"reason": refusal.key.split(".")[-1]},
                    security.client_ip())
        flash(_t(refusal.key), "error")
        return render_template("auth/recovery.html"), refusal.status
    users.audit(user, "auth.recovery_login", user["id"], {"two_step": two_step.enabled(user["id"])},
                security.client_ip())
    with db.transaction():
        fresh = users.get(user["id"])
        if fresh is None or fresh["password"] != user["password"] or fresh["role"] != "admin":
            flash(_t("auth.invalid_credentials"), "error")
            return render_template("auth/recovery.html"), 401
        if two_step.enabled(fresh["id"]):
            auth.await_code(fresh, None, "recovery")
            return redirect(url_for("auth.two_step"))
        auth.start_session(fresh, "recovery")
    response = redirect(auth.landing(fresh, None))
    security.set_device_cookie(response, fresh)
    return response
