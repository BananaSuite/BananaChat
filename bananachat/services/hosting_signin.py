"""Sign in with BananaWiki Hosting: an OAuth 2.0 client (authorization code with PKCE) of the hosting portal.

The contract is the portal's (PROTOCOL.md of the BananaSuite projects); the
pages are in :mod:`bananachat.web.hosting`. The rules:

* Configured by ``BC_HOSTING_OAUTH_URL``, ``…_CLIENT_ID`` and ``…_CLIENT_SECRET``
  (``config.load_config`` checks them). The endpoints derive from the URL
  (``/oauth/authorize``, ``/oauth/token``, ``/oauth/userinfo``).
* The sign-in mode (``site_settings.signin_mode``) is ``local``, ``both`` or
  ``hosting``; without a working configuration it is always ``local``.
* A flow starts with a random ``state`` kept in the session cookie (at most
  :data:`MAX_STATES`, each for :data:`STATE_TTL_SECONDS`, used once) and a PKCE
  S256 challenge whose verifier is derived from the state with the server's
  secret key, so nothing secret is stored in the cookie.
* The code is exchanged with HTTP Basic client authentication, the profile read
  once with the Bearer token, and the token dropped: it is never stored or
  logged. Server-to-server calls go through :mod:`bananachat.services.upstream`
  (no proxies, no redirects, timeouts, bounded bodies) to the configured
  portal's origin only.
* Accounts are found by the portal's stable account id (``sub``) in
  ``hosting_links`` only, never by user name or email.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode, urlsplit

from flask import current_app, g, session, url_for

from bananachat.db import hosting_links, users
from bananachat.db import settings as site_settings
from bananachat.services import upstream

log = logging.getLogger("bananachat.hosting")

MODES = ("local", "both", "hosting")
PURPOSES = ("login", "link")
SCOPE = "profile"
STATE_KEY = "hosting_oauth_states"
STATE_TTL_SECONDS = 10 * 60
MAX_STATES = 5
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10
SUB_MAX = 64
PORTAL_NAME_MAX = 200


class OAuthError(Exception):
    """A failed step; ``key`` is the translation key shown to the person, the message is for the log."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key


# ----- configuration and mode -------------------------------------------------

def _config():
    return current_app.config["BC"]


def configured() -> bool:
    return _config().hosting_oauth_configured


def stored_mode(settings: dict | None = None) -> str:
    """The mode the administrators chose (whether or not the portal is configured)."""
    if settings is None:
        settings = getattr(g, "settings", None) or site_settings.get()
    mode = settings.get("signin_mode")
    return mode if mode in MODES else "local"


def effective_mode(settings: dict | None = None) -> str:
    """The mode in force: always ``local`` without a working configuration."""
    return stored_mode(settings) if configured() else "local"


def portal_url() -> str:
    return _config().hosting_oauth_url


def portal_origin() -> str:
    """``scheme://host[:port]`` of the portal, for the pages whose forms continue there."""
    parts = urlsplit(portal_url())
    return f"{parts.scheme}://{parts.netloc}"


def endpoint(name: str) -> str:
    return f"{portal_url()}/oauth/{name}"


def callback_url() -> str:
    """The redirect URI to register on the portal; it must match it exactly."""
    return url_for("hosting.callback", _external=True)


# ----- state and PKCE ---------------------------------------------------------

def _verifier(state: str) -> str:
    """The PKCE code verifier (43 base64url characters), derived from the state with the server's secret key."""
    digest = hmac.new(_config().secret_key.encode("utf-8"), b"hosting-oauth-pkce:" + state.encode("ascii"),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _live_states() -> dict:
    stored = session.get(STATE_KEY)
    now = time.time()
    states = {}
    if isinstance(stored, dict):
        for key, entry in stored.items():
            try:
                age = now - float(entry.get("at", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(key, str) and 0 <= age <= STATE_TTL_SECONDS:
                states[key] = entry
    return states


def authorize_url(purpose: str, *, next_url: str = "", user_id: str = "", invite: str = "") -> str:
    """Remember a new state in the session and build the portal's authorize URL."""
    if purpose not in PURPOSES:
        raise ValueError("Unknown purpose.")
    states = _live_states()
    while len(states) >= MAX_STATES:
        states.pop(min(states, key=lambda key: float(states[key]["at"])))
    state = secrets.token_urlsafe(24)
    states[state] = {"purpose": purpose, "next": next_url or "", "uid": user_id or "", "invite": invite or "",
                     "at": time.time()}
    session[STATE_KEY] = states
    query = urlencode({"response_type": "code", "client_id": _config().hosting_oauth_client_id,
                       "redirect_uri": callback_url(), "state": state, "scope": SCOPE,
                       "code_challenge": challenge(_verifier(state)), "code_challenge_method": "S256"})
    return f"{endpoint('authorize')}?{query}"


def consume_state(state: str) -> dict:
    """The flow *state* belongs to, forgotten now (a state works once); OAuthError when unknown or expired."""
    states = _live_states()
    entry = states.pop(state, None) if isinstance(state, str) and 0 < len(state) <= 128 else None
    session[STATE_KEY] = states
    if not isinstance(entry, dict) or entry.get("purpose") not in PURPOSES:
        raise OAuthError("hosting.error_expired", "unknown, used or expired state")
    return entry


# ----- talking to the portal ----------------------------------------------------

def _request(method: str, path: str, *, headers: dict, body: bytes | None = None) -> tuple[int, object]:
    """One server-to-server call to the portal: ``(HTTP status, parsed JSON or None)``.

    Tests replace this function. Only the portal's origin is ever called, without
    proxies or redirects, and the answer is read up to :data:`MAX_RESPONSE_BYTES`.
    """
    try:
        with upstream.open_request(method, portal_url(), path, body=body, headers=headers,
                                   connect_timeout=TIMEOUT_SECONDS, first_byte_timeout=TIMEOUT_SECONDS,
                                   read_timeout=TIMEOUT_SECONDS, total_timeout=TIMEOUT_SECONDS * 2,
                                   max_bytes=MAX_RESPONSE_BYTES) as response:
            status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except upstream.UpstreamError as error:
        if error.status is None:
            raise OAuthError("hosting.error_portal", f"{path}: {error.kind or 'failed'}") from None
        # The portal's error (``invalid_grant``…), for the log only.
        return error.status, {"error": (error.code or str(error))[:60]}
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OAuthError("hosting.error_portal", f"{path}: the answer is too large")
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return status, None


def _client_auth() -> str:
    config = _config()
    raw = f"{config.hosting_oauth_client_id}:{config.hosting_oauth_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def exchange_code(code: str, state: str) -> str:
    """Trade an authorization code for an access token (kept in memory only)."""
    if not isinstance(code, str) or not 0 < len(code) <= 2048:
        raise OAuthError("hosting.error_portal", "callback without a usable code")
    body = urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url(),
                      "code_verifier": _verifier(state)}).encode("ascii")
    status, data = _request("POST", "/oauth/token", body=body,
                            headers={"Authorization": _client_auth(), "Accept": "application/json",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    token = data.get("access_token") if status == 200 and isinstance(data, dict) else None
    if not isinstance(token, str) or not 0 < len(token) <= 4096 or not token.isascii() \
            or any(ord(char) < 33 or ord(char) == 127 for char in token):
        error = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), str) else ""
        raise OAuthError("hosting.error_portal", f"the token endpoint answered {status} {error[:40]}".strip())
    token_type = data.get("token_type")
    if token_type is not None and (not isinstance(token_type, str) or token_type.lower() != "bearer"):
        raise OAuthError("hosting.error_portal", "the token endpoint issued a token that is not a Bearer token")
    return token


def fetch_profile(token: str) -> tuple[str, str]:
    """``(portal account id, portal username)`` for an access token; the token is only sent in the header."""
    status, data = _request("GET", "/oauth/userinfo",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status == 403:
        raise OAuthError("hosting.error_portal_account", "userinfo answered 403")
    if status != 200 or not isinstance(data, dict):
        raise OAuthError("hosting.error_portal", f"userinfo answered {status}")
    sub, name = data.get("sub"), data.get("preferred_username") or data.get("username") or ""
    if (not isinstance(sub, str) or not 0 < len(sub) <= SUB_MAX or not sub.isprintable()
            or sub != sub.strip()):
        raise OAuthError("hosting.error_portal", "userinfo without a valid sub")
    name = name.strip()[:PORTAL_NAME_MAX] if isinstance(name, str) and name.isprintable() else ""
    return sub, name


def complete(code: str, state: str) -> tuple[str, str]:
    """Exchange the code and read the profile; the access token does not outlive this call."""
    token = exchange_code(code, state)
    try:
        return fetch_profile(token)
    finally:
        del token


# ----- account names ------------------------------------------------------------

def name_problem(username: str) -> str | None:
    """Why *username* cannot be used for a new account (a translation key), or None."""
    if not users.USERNAME_RE.match(username or ""):
        return "auth.username_rules"
    return "auth.username_taken" if users.get_by_username(username) else None


def suggest_username(portal_username: str) -> str:
    """A free name for a new account: the portal's when it can be used, else the closest free one."""
    base = "".join(char if char.isascii() and (char.isalnum() or char in "_.-") else "_"
                   for char in (portal_username or "").strip())[:28].strip("_.-") or "member"
    if len(base) < 3:
        base = (base + "_member")[:28]
    if name_problem(base) is None:
        return base
    for number in range(2, 100):
        candidate = f"{base}{number}"
        if name_problem(candidate) is None:
            return candidate
    while True:
        candidate = f"{base[:24]}_{secrets.token_hex(3)}"
        if name_problem(candidate) is None:
            return candidate


# ----- links ----------------------------------------------------------------------

def link_of(user_id: str):
    return hosting_links.link_of(user_id)


def linked_user(account_id: str):
    return hosting_links.linked_user(account_id)
