"""Authentication, CSRF protection, throttling and request helpers.

Login state lives in the ``auth_sessions`` table; the signed session cookie
only carries a random session token (plus the CSRF token and language). A
session ends on logout, password change, suspension or expiry, and users can
end their other sessions from their account page.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from bananachat import db
from bananachat.db import users

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Blueprints authenticated by bearer tokens, never by cookies: CSRF cannot apply.
BEARER_BLUEPRINTS = frozenset({"api_v1", "worker_api"})
PASSWORD_MIN, PASSWORD_MAX = 8, 1024


# ----- passwords ------------------------------------------------------------

def _method() -> str:
    configured = current_app.config["BC"].password_hash_method
    if configured and configured != "auto":
        return configured
    try:
        hashlib.scrypt(b"x", salt=b"y", n=2, r=1, p=1)
        return "scrypt"
    except (AttributeError, ValueError):
        return "pbkdf2:sha256:600000"


def hash_password(password: str) -> str:
    return generate_password_hash(password, method=_method())


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return check_password_hash(stored_hash, password)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when a hash uses another algorithm than new hashes (e.g. pbkdf2 vs scrypt)."""
    return not (stored_hash or "").startswith(_method().split(":")[0] + ":")


_DUMMY: dict[str, str] = {}


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check when the account does not exist."""
    method = _method()
    if method not in _DUMMY:
        _DUMMY[method] = generate_password_hash(secrets.token_hex(16), method=method)
    verify_password(_DUMMY[method], password)


def password_problem(password: str, confirmation: str | None = None) -> str | None:
    """A translation key describing what is wrong with a new password, or None."""
    if not PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX:
        return "auth.password_length"
    if confirmation is not None and password != confirmation:
        return "auth.password_mismatch"
    return None


# ----- current user ---------------------------------------------------------

def load_current_user() -> None:
    """Resolve ``g.user`` from the session cookie (called before each request)."""
    g.user = None
    g.auth_session = None
    token = session.get("sid")
    if token:
        row, user = users.load_session(token)
        if row is None:
            session.pop("sid", None)
        elif _usable(user):
            users.refresh_session(row, current_app.config["BC"].session_days)
            g.user, g.auth_session = user, row
            return
        else:
            users.end_session(token)
            session.pop("sid", None)
        return
    legacy_id = session.get("user_id")
    if legacy_id:
        # Cookies issued before server-side sessions: upgrade while the
        # account's session version matches. Check and create atomically so
        # revocation cannot occur between reading the version and upgrading.
        # The previous release read a missing version as 0.
        version = session.get("session_version", 0)
        for key in ("user_id", "session_version"):
            session.pop(key, None)
        with db.transaction():
            user = users.get(legacy_id)
            if user and _usable(user) and version == (user["session_version"] or 0):
                login(user, remember_language=False)


def _usable(user) -> bool:
    if user is None:
        return False
    if users.is_suspended(user):
        return False
    if user["suspended"]:
        users.lift_expired_suspension(user)
    return True


def current_user():
    return getattr(g, "user", None)


def is_admin() -> bool:
    user = current_user()
    return bool(user and user["role"] == "admin")


def login(user, *, remember_language: bool = True) -> None:
    """Start a fresh session for *user* (prevents session fixation)."""
    language = session.get("language")
    session.clear()
    if language and remember_language:
        session["language"] = language
    session.permanent = True
    config = current_app.config["BC"]
    token = users.create_session(user["id"], config.session_days,
                                 user_agent=request.headers.get("User-Agent", ""), ip_address=client_ip())
    session["sid"] = token
    g.user = user
    g.auth_session = None


def logout() -> None:
    token = session.get("sid")
    if token:
        users.end_session(token)
    language = session.get("language")
    session.clear()
    if language:
        session["language"] = language
    g.user = None


def current_session_hash() -> str | None:
    token = session.get("sid")
    return users.session_hash(token) if token else None


# ----- responses ------------------------------------------------------------

def wants_json() -> bool:
    if request.path.startswith(("/v1/", "/worker/v1/")) or request.headers.get("X-Requested-With"):
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]


def json_error(message: str, status: int = 400, code: str = "bad_request", **extra):
    response = jsonify({"error": {"code": code, "message": message, **extra}})
    response.status_code = status
    return response


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if wants_json():
                return json_error("Your session has ended. Sign in again.", 401, "auth_required")
            target = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login", next=target if target != "/" else None))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if wants_json():
                return json_error("Your session has ended. Sign in again.", 401, "auth_required")
            return redirect(url_for("auth.login", next=request.path))
        if user["role"] != "admin":
            if wants_json():
                return json_error("Administrator access is required.", 403, "forbidden")
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def safe_next_url(target: str | None) -> str | None:
    """Accept only same-site relative paths as redirect targets."""
    if not target or len(target) > 2000:
        return None
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    if any(ord(char) < 32 for char in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


# ----- CSRF -----------------------------------------------------------------

def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def error_language() -> str:
    """The language for error messages: English for API clients, else the page language."""
    if request.blueprint in BEARER_BLUEPRINTS:
        return "en"
    return getattr(g, "lang", None) or "en"


def check_csrf() -> None:
    """Reject unsafe cookie-authenticated requests without the session's token."""
    if request.method in SAFE_METHODS or request.blueprint in BEARER_BLUEPRINTS:
        return
    if request.routing_exception is not None:
        return  # no view runs: the 404/405 (or redirect) answers the request
    view = current_app.view_functions.get(request.endpoint)
    if view is not None and getattr(view, "csrf_exempt", False):
        return
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        from bananachat.i18n import translate

        abort(403, description=translate(error_language(), "errors.cross_site"))
    expected = session.get("csrf")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    if (not isinstance(expected, str) or not expected or not expected.isascii()
            or not isinstance(sent, str) or not sent.isascii() or not hmac.compare_digest(sent, expected)):
        raise CSRFError()


class CSRFError(Exception):
    pass


def csrf_exempt(view):
    view.csrf_exempt = True
    return view


def body_limit(max_bytes):
    """Allow a larger (or smaller) request body for one view.

    *max_bytes* is a number or a function of the app's ``Config`` returning one.
    """
    def decorate(view):
        view.body_limit = max_bytes
        return view
    return decorate


# Threads per process that long requests may never take, so health checks,
# status polls and ordinary pages are answered even while every other thread
# is streaming an answer.
RESERVED_THREADS = 4


class _LongRequests:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0


def long_request(view):
    """Cap concurrent streaming/generating requests below the thread count.

    Each such request holds a server thread until its response is closed
    (while it waits in the inference queue and while it streams). Over the
    cap the view answers 503 at once instead of starving short requests.
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        app = current_app._get_current_object()
        state = app.extensions.setdefault("bananachat.long_requests", _LongRequests())
        limit = max(2, app.config["BC"].http_threads - RESERVED_THREADS)
        with state.lock:
            if state.active >= limit:
                busy = True
            else:
                busy = False
                state.active += 1
        if busy:
            from bananachat.i18n import translate

            language = getattr(g, "lang", None) or "en"
            response = json_error(translate(language, "chat.error_server_busy"), 503, "server_busy")
            response.headers["Retry-After"] = "5"
            return response
        released = []

        def release():
            with state.lock:
                if not released:
                    released.append(True)
                    state.active -= 1

        try:
            response = app.make_response(view(*args, **kwargs))
        except BaseException:
            release()
            raise
        if response.is_streamed:
            response.call_on_close(release)
        else:
            release()
        return response
    return wrapper


def long_requests_active() -> int:
    state = current_app.extensions.get("bananachat.long_requests")
    return state.active if state else 0


# ----- client identity and throttling ---------------------------------------

def client_ip() -> str:
    return request.remote_addr or "unknown"


def client_key() -> str:
    """Throttling key: the IPv4 address or the IPv6 /64 network."""
    address = client_ip()
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{parsed}/64", strict=False))
    return str(parsed)


def allow(bucket: str, limit: int, window: int, *, key: str | None = None) -> bool:
    return users.hit(f"{bucket}:{key or client_key()}", limit, window)


def rate_limit(bucket: str, limit: int, window: int, *, per_user: bool = False):
    """Throttle a view by client address (or by account with ``per_user``)."""
    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            key = None
            if per_user and current_user() is not None:
                key = "user:" + current_user()["id"]
            if request.method not in SAFE_METHODS and not allow(bucket, limit, window, key=key):
                return too_many_requests()
            return view(*args, **kwargs)
        return wrapper
    return decorate


def too_many_requests(retry_after: int = 60):
    if wants_json():
        response = json_error("Too many requests. Please wait and try again.", 429, "rate_limited")
    else:
        from flask import render_template
        response = current_app.make_response((render_template("errors/error.html", code=429), 429))
    response.headers["Retry-After"] = str(retry_after)
    return response


# ----- bot protection for anonymous forms -----------------------------------

def form_token() -> str:
    """A signed timestamp; forms submitted implausibly fast are rejected."""
    issued = str(int(time.time()))
    signature = hmac.new(current_app.config["SECRET_KEY"].encode(), b"form:" + issued.encode(),
                         hashlib.sha256).hexdigest()[:24]
    return f"{issued}.{signature}"


def form_looks_human() -> bool:
    if request.form.get("website"):  # honeypot field, hidden from people
        return False
    if current_app.config.get("TESTING"):
        return True
    issued, _, signature = (request.form.get("_form_time") or "").partition(".")
    # Bound and validate before signing or converting attacker-controlled text.
    # str.isdigit also accepts characters int() cannot parse, while compare_digest
    # only accepts ASCII strings.
    if (not 1 <= len(issued) <= 12 or not issued.isascii() or not issued.isdecimal()
            or len(signature) != 24 or not signature.isascii()):
        return False
    expected = hmac.new(current_app.config["SECRET_KEY"].encode(), b"form:" + issued.encode(),
                        hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(signature, expected):
        return False
    age = time.time() - int(issued)
    return current_app.config["BC"].min_form_seconds <= age <= 6 * 3600
