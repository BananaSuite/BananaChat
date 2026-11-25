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
* A ``confirm`` flow re-verifies a signed-in, linked person before a sensitive
  action: it asks the portal for a fresh sign-in (``prompt=login``,
  ``max_age=0``) and is accepted only for the linked ``sub`` with an
  ``auth_time`` no older than the start of the flow (:data:`AUTH_TIME_SKEW`
  seconds of clock difference allowed). A portal that sends no ``auth_time``
  never confirms anything.
* A profile with ``act`` comes from a hosting administrator acting as the
  account (:class:`Profile`): it signs in only to an account that is already
  linked, never links or creates one, and its session ends at
  ``act_expires_at`` (at most :data:`ACT_MAX_SECONDS` from now).
* "Create an account" (``hosting`` mode only) starts a ``login`` flow with
  ``prompt=create``: the portal shows its sign-up form first, and the state
  remembers the flow was a sign-up, so an unknown ``sub`` gets its account here
  at once (web.hosting). The sign-in page shows it only while the portal's
  metadata says sign-up is ``open`` or ``invite`` (:func:`portal_signup_open`,
  remembered :data:`SIGNUP_STATE_SECONDS`; no answer hides it).
* The administrators' linking tool can look accounts up on the portal
  (:func:`lookup`) with a portal API token stored encrypted in the settings
  (:func:`store_lookup_token`); without one, it takes portal account ids only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app, g, session, url_for

from bananachat.db import hosting_links, users
from bananachat.db import settings as site_settings
from bananachat.services import upstream

log = logging.getLogger("bananachat.hosting")

MODES = ("local", "both", "hosting")
PURPOSES = ("login", "link", "confirm")
SCOPE = "profile"
STATE_KEY = "hosting_oauth_states"
STATE_TTL_SECONDS = 10 * 60
MAX_STATES = 5
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 10
SUB_MAX = 64
PORTAL_NAME_MAX = 200
# A confirmation accepts a portal sign-in this many seconds older than the start of the flow (clock skew).
AUTH_TIME_SKEW = 60
# A recent confirmation (password, code or BananaWiki Hosting) covers the session's next actions this long.
CONFIRM_WINDOW_SECONDS = 10 * 60
# An acting administrator's session never lasts longer than this, whatever the portal says.
ACT_MAX_SECONDS = 12 * 3600
SETTING_ALLOW_ACTING = "hosting_allow_acting"
# The linking tool's account lookup (A3 of the BananaSuite sign-in contract).
LOOKUP_PATH = "/api/v1/admin/accounts/lookup"
LOOKUP_BATCH = 500
LOOKUP_MAX_RESPONSE_BYTES = 1024 * 1024
LOOKUP_STATUSES = ("active", "suspended", "pending")
TOKEN_MAX = 512
# The portal's OAuth metadata says whether it takes new accounts ("signup"); remembered this long (a failed
# answer: a minute), asked with a short timeout since the sign-in page waits for it.
METADATA_PATH = "/.well-known/oauth-authorization-server"
SIGNUP_STATE_SECONDS = 5 * 60
SIGNUP_STATE_RETRY_SECONDS = 60
SIGNUP_STATE_TIMEOUT = 3


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


def authorize_url(purpose: str, *, next_url: str = "", user_id: str = "", invite: str = "",
                  session_hash: str = "", create: bool = False) -> str:
    """Remember a new state in the session and build the portal's authorize URL.

    A ``confirm`` flow is bound to the account and the login session (*session_hash*) that started it, and
    asks the portal for a fresh sign-in. *create* (a ``login`` flow) asks the portal for its sign-up form
    first (``prompt=create``), and the state remembers it.
    """
    if purpose not in PURPOSES:
        raise ValueError("Unknown purpose.")
    states = _live_states()
    while len(states) >= MAX_STATES:
        states.pop(min(states, key=lambda key: float(states[key]["at"])))
    state = secrets.token_urlsafe(24)
    states[state] = {"purpose": purpose, "next": next_url or "", "uid": user_id or "", "invite": invite or "",
                     "sid": session_hash or "", "at": time.time(),
                     **({"create": True} if create and purpose == "login" else {})}
    session[STATE_KEY] = states
    query = {"response_type": "code", "client_id": _config().hosting_oauth_client_id,
             "redirect_uri": callback_url(), "state": state, "scope": SCOPE,
             "code_challenge": challenge(_verifier(state)), "code_challenge_method": "S256"}
    if purpose == "confirm":
        query.update(prompt="login", max_age="0")
    elif create and purpose == "login":
        query.update(prompt="create")
    return f"{endpoint('authorize')}?{urlencode(query)}"


def consume_state(state: str) -> dict:
    """The flow *state* belongs to, forgotten now (a state works once); OAuthError when unknown or expired."""
    states = _live_states()
    entry = states.pop(state, None) if isinstance(state, str) and 0 < len(state) <= 128 else None
    session[STATE_KEY] = states
    if not isinstance(entry, dict) or entry.get("purpose") not in PURPOSES:
        raise OAuthError("hosting.error_expired", "unknown, used or expired state")
    return entry


# ----- talking to the portal ----------------------------------------------------

def _request(method: str, path: str, *, headers: dict, body: bytes | None = None,
             max_bytes: int = MAX_RESPONSE_BYTES, timeout: float = TIMEOUT_SECONDS) -> tuple[int, object]:
    """One server-to-server call to the portal: ``(HTTP status, parsed JSON or None)``.

    Tests replace this function. Only the portal's origin is ever called, without
    proxies or redirects, and the answer is read up to *max_bytes*.
    """
    try:
        with upstream.open_request(method, portal_url(), path, body=body, headers=headers,
                                   connect_timeout=timeout, first_byte_timeout=timeout,
                                   read_timeout=timeout, total_timeout=timeout * 2,
                                   max_bytes=max_bytes) as response:
            status, raw = response.status, response.read(max_bytes + 1)
    except upstream.UpstreamError as error:
        if error.status is None:
            raise OAuthError("hosting.error_portal", f"{path}: {error.kind or 'failed'}") from None
        # The portal's error (``invalid_grant``…), for the log only.
        return error.status, {"error": (error.code or str(error))[:60]}
    if len(raw) > max_bytes:
        raise OAuthError("hosting.error_portal", f"{path}: the answer is too large")
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return status, None


def portal_signup_open() -> bool:
    """Whether the portal takes new accounts: its metadata's ``signup`` is ``open`` or ``invite``.

    Remembered per process for :data:`SIGNUP_STATE_SECONDS`; a portal that does not answer, or does not
    say, counts as closed for :data:`SIGNUP_STATE_RETRY_SECONDS`.
    """
    if not configured():
        return False
    cache = current_app.extensions.setdefault("bananachat.portal_signup", {})
    now = time.monotonic()
    if cache.get("until", 0) > now:
        return bool(cache.get("open"))
    answer = None
    try:
        status, data = _request("GET", METADATA_PATH, headers={"Accept": "application/json"},
                                timeout=SIGNUP_STATE_TIMEOUT)
        if status == 200 and isinstance(data, dict):
            answer = data.get("signup")
    except OAuthError as error:
        log.info("The sign-up state of BananaWiki Hosting could not be read: %s", error)
    known = answer in ("open", "invite", "closed")
    cache.update(open=answer in ("open", "invite"),
                 until=now + (SIGNUP_STATE_SECONDS if known else SIGNUP_STATE_RETRY_SECONDS))
    return bool(cache["open"])


def _client_auth() -> str:
    config = _config()
    raw = f"{config.hosting_oauth_client_id}:{config.hosting_oauth_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def exchange_code(code: str, state: str) -> tuple[str, dict]:
    """Trade an authorization code for an access token (kept in memory only), with what else the token
    response says about the sign-in (``auth_time``, ``act``, ``act_expires_at``)."""
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
    return token, {key: data[key] for key in ("auth_time", "act", "act_expires_at") if key in data}


@dataclass(frozen=True)
class Profile:
    """Who the portal vouched for.

    ``auth_time`` is when that person last signed in at the portal interactively (Unix seconds, None when the
    portal did not say). ``act`` (``{"sub", "name"}``) is set when a hosting administrator is acting as the
    account, until ``act_until`` (an aware datetime).
    """

    sub: str
    username: str
    auth_time: int | None = None
    act: dict | None = None
    act_until: datetime | None = None


def _valid_sub(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= SUB_MAX and value.isprintable()
            and value == value.strip())


def _clean_name(value) -> str:
    return value.strip()[:PORTAL_NAME_MAX] if isinstance(value, str) and value.isprintable() else ""


def _unix_seconds(value) -> int | None:
    """A whole number of seconds since 1970 that a portal could mean, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    if not 0 <= value <= 32503680000:  # before the year 3000
        return None
    return int(value)


def _acting(data: dict, sub: str) -> tuple[dict | None, datetime | None]:
    """``(act, until)`` from the portal's ``act`` and ``act_expires_at``; refuses anything unclear."""
    act = data.get("act")
    if act is None:
        return None, None
    if not isinstance(act, dict) or not _valid_sub(act.get("sub")) or act.get("sub") == sub:
        raise OAuthError("hosting.error_portal", "an act claim without a valid administrator")
    expires = _unix_seconds(data.get("act_expires_at"))
    now = time.time()
    if expires is None or expires <= now:
        raise OAuthError("hosting.error_acting_expired", "acting without a future act_expires_at")
    until = datetime.fromtimestamp(min(expires, now + ACT_MAX_SECONDS), timezone.utc)
    name = _clean_name(act.get("preferred_username") or act.get("username"))
    return {"sub": act["sub"], "name": name}, until


def fetch_profile(token: str, token_data: dict | None = None) -> Profile:
    """The profile for an access token; the token is only sent in the header.

    ``auth_time`` and ``act`` are read from the profile, or else from the token response (*token_data*):
    an ``act`` in either one counts, so an acting administrator is never taken for the account itself.
    """
    status, data = _request("GET", "/oauth/userinfo",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status == 403:
        raise OAuthError("hosting.error_portal_account", "userinfo answered 403")
    if status != 200 or not isinstance(data, dict):
        raise OAuthError("hosting.error_portal", f"userinfo answered {status}")
    sub = data.get("sub")
    if not _valid_sub(sub):
        raise OAuthError("hosting.error_portal", "userinfo without a valid sub")
    name = _clean_name(data.get("preferred_username") or data.get("username") or "")
    extra = token_data if isinstance(token_data, dict) else {}
    source = data if data.get("act") is not None or extra.get("act") is None else extra
    act, until = _acting(source, sub)
    auth_time = _unix_seconds(data["auth_time"]) if "auth_time" in data else _unix_seconds(extra.get("auth_time"))
    return Profile(sub=sub, username=name, auth_time=auth_time, act=act, act_until=until)


def complete(code: str, state: str) -> Profile:
    """Exchange the code and read the profile; the access token does not outlive this call."""
    token, token_data = exchange_code(code, state)
    try:
        return fetch_profile(token, token_data)
    finally:
        del token


def acting_allowed(settings: dict | None = None) -> bool:
    """Whether hosting administrators may sign in here while acting as an account (default yes)."""
    if settings is None:
        settings = getattr(g, "settings", None) or site_settings.get()
    value = settings.get(SETTING_ALLOW_ACTING)
    return True if value is None else bool(value)


def confirmation_problem(profile: Profile, linked_sub: str, started_at: float) -> str | None:
    """Why *profile* does not confirm the person linked to *linked_sub* (a translation key), or None.

    Accepted only for that very portal account, not acting, with a portal sign-in (``auth_time``) made
    after the confirmation started (less :data:`AUTH_TIME_SKEW`). Without ``auth_time`` nothing is confirmed.
    """
    if not linked_sub or not hmac.compare_digest(profile.sub, linked_sub):
        return "hosting.confirm_wrong_account"
    if profile.act is not None:
        return "hosting.acting_refused"
    if profile.auth_time is None:
        return "hosting.confirm_no_auth_time"
    if profile.auth_time < started_at - AUTH_TIME_SKEW:
        return "hosting.confirm_stale"
    return None


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


# ----- the linking tool's account lookup --------------------------------------------

@lru_cache(maxsize=4)
def _token_cipher(secret_key: str) -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"BananaChat hosting lookup token",
                   info=b"hosting-lookup-token-v1").derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def _secret_key() -> str:
    secret = _config().secret_key
    if not secret:
        raise ValueError("The server secret key is not loaded.")
    return secret


def token_problem(token: str) -> str | None:
    """Why *token* cannot be a portal API token (for the administrator), or None."""
    if not 8 <= len(token) <= TOKEN_MAX or any(not 33 <= ord(char) <= 126 for char in token):
        return (f"The portal API token must be 8 to {TOKEN_MAX} characters without spaces or control "
                "characters.")
    return None


def encrypt_lookup_token(token: str) -> str:
    """The value to store in ``site_settings.hosting_lookup_token``."""
    return _token_cipher(_secret_key()).encrypt(token.encode("ascii")).decode("ascii")


def lookup_token(settings: dict | None = None) -> str:
    """The stored portal API token, or ``""`` (none, or it no longer decrypts: another secret key)."""
    if settings is None:
        settings = getattr(g, "settings", None) or site_settings.get()
    stored = settings.get("hosting_lookup_token")
    if not stored:
        return ""
    try:
        return _token_cipher(_secret_key()).decrypt(str(stored).encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def lookup_state(settings: dict | None = None) -> str:
    """``none``, ``ready`` or ``unreadable`` (stored, but it does not decrypt with this server's secret key)."""
    if settings is None:
        settings = getattr(g, "settings", None) or site_settings.get()
    if not settings.get("hosting_lookup_token"):
        return "none"
    return "ready" if lookup_token(settings) else "unreadable"


class LookupFailed(Exception):
    """The portal's account lookup failed; the message is for the administrator."""


@dataclass(frozen=True)
class PortalAccount:
    sub: str
    username: str
    status: str


def lookup(usernames: list[str]) -> dict[str, list[PortalAccount]]:
    """Portal accounts by user name (case-insensitive): ``{lowercased name: [accounts]}``.

    Asks the portal in batches of :data:`LOOKUP_BATCH` with the stored token. Names the portal does not know
    are missing from the answer. Raises :class:`LookupFailed` when there is no token or the portal refuses.
    """
    token = lookup_token()
    if not token:
        raise LookupFailed("Store a portal API token (Settings → Sign-in) to look accounts up on BananaWiki "
                           "Hosting, or enter portal account ids.")
    wanted = sorted({name for name in usernames if name})
    found: dict[str, list[PortalAccount]] = {}
    for start in range(0, len(wanted), LOOKUP_BATCH):
        batch = wanted[start:start + LOOKUP_BATCH]
        body = json.dumps({"usernames": batch, "emails": []}).encode("utf-8")
        try:
            status, data = _request("POST", LOOKUP_PATH, body=body, max_bytes=LOOKUP_MAX_RESPONSE_BYTES,
                                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                             "Content-Type": "application/json"})
        except OAuthError as error:
            log.warning("The BananaWiki Hosting account lookup failed: %s", error)
            raise LookupFailed("BananaWiki Hosting could not be reached for the account lookup. Try again "
                               "later.") from None
        if status in (401, 403):
            raise LookupFailed("BananaWiki Hosting refused the portal API token. It must belong to a portal "
                               "administrator and have the accounts:lookup scope.")
        if status == 429:
            raise LookupFailed("BananaWiki Hosting is limiting account lookups. Wait a minute and try again.")
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise LookupFailed(f"BananaWiki Hosting answered the account lookup with HTTP {status}.")
        for entry in data["accounts"]:
            if not isinstance(entry, dict) or not _valid_sub(entry.get("sub")):
                continue
            name = _clean_name(entry.get("username"))
            status_text = entry.get("status") if entry.get("status") in LOOKUP_STATUSES else "pending"
            if name:
                found.setdefault(name.lower(), []).append(PortalAccount(entry["sub"], name, status_text))
    return found


# ----- confirmations -------------------------------------------------------------------

def confirmed_recently() -> bool:
    """Whether this browser session confirmed with BananaWiki Hosting in the last ten minutes."""
    from bananachat import security

    return users.session_hosting_confirmed_within(security.current_session_hash(), CONFIRM_WINDOW_SECONDS)


def can_confirm(user) -> bool:
    """Whether *user* can confirm an action with BananaWiki Hosting: the sign-in is on, the account is linked
    and this is not an acting administrator's session."""
    from bananachat import security

    return (user is not None and effective_mode() != "local" and security.acting() is None
            and hosting_links.link_of(user["id"]) is not None)


def allow_portal_form_action() -> None:
    """Let this page's forms continue to the portal (the Content-Security-Policy's form-action)."""
    origins = tuple(getattr(g, "form_action_origins", ()))
    if portal_origin() not in origins:
        g.form_action_origins = (*origins, portal_origin())
