"""User accounts, preferences, login sessions, throttling and the audit log."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from bananachat import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
ROLES = ("user", "admin")

# ----- preferences ----------------------------------------------------------

FONT_SCALES = (0.85, 0.9, 1.0, 1.1, 1.2, 1.35)
PREFERENCE_DEFAULTS = {
    "theme_mode": "default", "interface_language": "default", "font_scale": 1.0, "contrast": 0,
    "line_height": 0, "letter_spacing": 0, "reduce_motion": False, "sidebar_width": 250,
    "custom_bg": "", "custom_text": "", "custom_primary": "", "custom_secondary": "",
    "custom_accent": "", "custom_sidebar": "",
    "semantic_bold": 0, "semantic_italic": 0, "semantic_code": 0, "semantic_link": 0, "semantic_heading": 0,
    "background_image": "",
}
COLOR_KEYS = ("custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar")
SEMANTIC_KEYS = ("semantic_bold", "semantic_italic", "semantic_code", "semantic_link", "semantic_heading")
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_BACKGROUND = re.compile(r"^[0-9a-f]{32}\.jpe?g$")


def _int_in(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if low <= number <= high else default


def clean_preferences(raw) -> dict:
    """Validate preferences on every read and write; unknown keys are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    prefs = dict(PREFERENCE_DEFAULTS)
    if raw.get("theme_mode") in ("default", "dark", "light"):
        prefs["theme_mode"] = raw["theme_mode"]
    if raw.get("interface_language") in ("default", "it", "en"):
        prefs["interface_language"] = raw["interface_language"]
    try:
        scale = float(raw.get("font_scale", 1.0))
        if math.isfinite(scale):
            prefs["font_scale"] = min(FONT_SCALES, key=lambda option: abs(option - scale))
    except (TypeError, ValueError, OverflowError):
        pass
    prefs["contrast"] = _int_in(raw.get("contrast"), 0, 5, 0)
    prefs["line_height"] = _int_in(raw.get("line_height"), 0, 2, 0)
    prefs["letter_spacing"] = _int_in(raw.get("letter_spacing"), 0, 2, 0)
    prefs["reduce_motion"] = raw.get("reduce_motion") is True or raw.get("reduce_motion") in (1, "1", "true")
    prefs["sidebar_width"] = _int_in(raw.get("sidebar_width"), 200, 420, PREFERENCE_DEFAULTS["sidebar_width"])
    for key in COLOR_KEYS:
        value = raw.get(key)
        prefs[key] = value.lower() if isinstance(value, str) and _HEX_COLOR.match(value) else ""
    for key in SEMANTIC_KEYS:
        prefs[key] = _int_in(raw.get(key), 0, 2, 0)
    background = raw.get("background_image")
    prefs["background_image"] = background if isinstance(background, str) and _BACKGROUND.match(background) else ""
    return prefs


def get_preferences(user_id: str) -> dict:
    value = db.scalar("SELECT accessibility FROM users WHERE id=?", (user_id,))
    try:
        return clean_preferences(json.loads(value) if value else {})
    except ValueError:
        return dict(PREFERENCE_DEFAULTS)


def save_preferences(user_id: str, prefs: dict) -> dict:
    cleaned = clean_preferences(prefs)
    db.execute("UPDATE users SET accessibility=? WHERE id=?", (json.dumps(cleaned), user_id))
    return cleaned


# ----- accounts -------------------------------------------------------------

def get(user_id: str):
    return db.one("SELECT * FROM users WHERE id=?", (user_id,)) if user_id else None


def get_by_username(username: str):
    return db.one("SELECT * FROM users WHERE username=?", (username,)) if username else None


def _new_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def create(username: str, password_hash: str, *, role: str = "user", invite_code: str | None = None) -> str:
    """Insert a user; must run inside ``db.transaction()`` when combined with other writes."""
    if role not in ROLES:
        raise ValueError("Unknown role.")
    for _ in range(5):
        user_id = _new_id()
        if not db.one("SELECT 1 FROM users WHERE id=?", (user_id,)):
            db.execute("INSERT INTO users (id, username, password, role, invite_code, created_at) VALUES (?,?,?,?,?,?)",
                       (user_id, username, password_hash, role, invite_code, db.now()))
            return user_id
    raise RuntimeError("Could not allocate a user id.")


def list_page(limit: int = 50, offset: int = 0, search: str = ""):
    params: list = []
    where = ""
    if search:
        where = "WHERE u.username LIKE ? ESCAPE '\\'"
        params.append("%" + _escape_like(search) + "%")
    return db.query(
        "SELECT u.*, q.window_tokens AS quota_tokens, q.window_slow_tokens AS quota_slow_tokens, "
        "l.speed AS limit_speed FROM users u LEFT JOIN user_limit_overrides q ON q.user_id=u.id AND q.pool='api' "
        f"LEFT JOIN user_limits l ON l.user_id=u.id {where} "
        "ORDER BY u.created_at DESC, u.username LIMIT ? OFFSET ?", (*params, limit, offset))


def count(search: str = "") -> int:
    if search:
        return db.scalar("SELECT COUNT(*) FROM users WHERE username LIKE ? ESCAPE '\\'",
                         ("%" + _escape_like(search) + "%",), 0)
    return db.scalar("SELECT COUNT(*) FROM users", default=0)


def count_active_admins() -> int:
    return db.scalar("SELECT COUNT(*) FROM users WHERE role='admin' AND suspended=0", default=0)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AuthenticationChanged(ValueError):
    """The credentials or login session verified by a request have been revoked."""


@contextmanager
def verified_credentials(user_id: str, password_hash: str, id_hash: str | None):
    """Lock an account mutation to the password and session already verified.

    Hash verification can run outside the write transaction; the action itself
    must run inside this context to respect concurrent resets and revocation.
    """
    with db.transaction():
        current = get(user_id)
        live = id_hash and db.one(
            "SELECT 1 FROM auth_sessions WHERE user_id=? AND id_hash=? AND expires_at>?",
            (user_id, id_hash, db.now()))
        if current is None or current["password"] != password_hash or not live or is_suspended(current):
            raise AuthenticationChanged("The verified credentials or session have changed.")
        yield current


def set_password(user_id: str, password_hash: str, *, keep_session: str | None = None,
                 expected_password: str | None = None) -> None:
    """Change a password and end every other login session of the account.

    Account self-service supplies the hash it verified. Check it and the retained
    session under the same lock as the update so revocation cannot be undone by
    an in-flight password change. Administrator resets need no old password.
    """
    guard = (verified_credentials(user_id, expected_password, keep_session)
             if expected_password is not None else db.transaction())
    with guard:
        db.execute("UPDATE users SET password=? WHERE id=?", (password_hash, user_id))
        revoke_sessions(user_id, except_hash=keep_session)


def set_role(user_id: str, role: str) -> None:
    """Change a role; refuses to remove the last active administrator."""
    if role not in ROLES:
        raise ValueError("Unknown role.")
    with db.transaction():
        current = get(user_id)
        if current is None:
            raise LookupError("User not found.")
        if current["role"] == "admin" and role != "admin" and not current["suspended"] and count_active_admins() <= 1:
            raise ValueError("The last administrator cannot be demoted.")
        db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def suspend(user_id: str, until: datetime | None = None) -> None:
    with db.transaction():
        current = get(user_id)
        if current is None:
            raise LookupError("User not found.")
        if current["role"] == "admin" and not current["suspended"] and count_active_admins() <= 1:
            raise ValueError("The last administrator cannot be suspended.")
        # last_suspension_at: tier promotion asks for some time without a suspension.
        db.execute("UPDATE users SET suspended=1, suspended_until=?, last_suspension_at=? WHERE id=?",
                   (db.timestamp(until) if until else None, db.now(), user_id))
        revoke_sessions(user_id)


def unsuspend(user_id: str) -> None:
    db.execute("UPDATE users SET suspended=0, suspended_until=NULL, last_suspension_at=? WHERE id=? AND suspended=1",
               (db.now(), user_id))


def is_suspended(user) -> bool:
    """True while a suspension is in force (timed suspensions lapse by themselves)."""
    if not user or not user["suspended"]:
        return False
    until = db.parse_timestamp(user["suspended_until"])
    if user["suspended_until"] and until is None:
        return True
    return until is None or until > datetime.now(timezone.utc)


def lift_expired_suspension(user) -> bool:
    """Clear a lapsed timed suspension atomically. Returns True when lifted."""
    if not user or not user["suspended"] or is_suspended(user):
        return False
    cursor = db.execute("UPDATE users SET suspended=0, suspended_until=NULL, last_suspension_at=suspended_until "
                        "WHERE id=? AND suspended=1 AND suspended_until=?", (user["id"], user["suspended_until"]))
    return cursor.rowcount == 1


def delete(user_id: str) -> None:
    """Delete an account and everything it owns; refuses the last administrator."""
    with db.transaction():
        current = get(user_id)
        if current is None:
            return
        if current["role"] == "admin" and not current["suspended"] and count_active_admins() <= 1:
            raise ValueError("The last administrator cannot be deleted.")
        db.execute("DELETE FROM users WHERE id=?", (user_id,))


def touch_login(user_id: str) -> None:
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), user_id))


# ----- login sessions -------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: str, days: int, *, user_agent: str = "", ip_address: str = "") -> str:
    """Start a login session; returns the raw token for the cookie."""
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO auth_sessions (id_hash, user_id, created_at, last_seen_at, expires_at, user_agent, ip_address) "
        "VALUES (?,?,?,?,?,?,?)",
        (_hash(token), user_id, db.now(), db.now(), db.now(timedelta(days=days)), user_agent[:300], ip_address[:64]),
    )
    return token


def load_session(token: str):
    """Return ``(session_row, user_row)`` for a live session token, else ``(None, None)``."""
    if not token or len(token) > 128:
        return None, None
    row = db.one("SELECT s.id_hash, s.user_id, s.last_seen_at, s.expires_at, s.created_at, s.user_agent "
                 "FROM auth_sessions s WHERE s.id_hash=? AND s.expires_at>?", (_hash(token), db.now()))
    if row is None:
        return None, None
    user = get(row["user_id"])
    return (row, user) if user else (None, None)


def refresh_session(session_row, days: int) -> None:
    """Slide the expiry forward, at most every five minutes."""
    seen = db.parse_timestamp(session_row["last_seen_at"])
    if seen and datetime.now(timezone.utc) - seen < timedelta(minutes=5):
        return
    db.execute("UPDATE auth_sessions SET last_seen_at=?, expires_at=? WHERE id_hash=?",
               (db.now(), db.now(timedelta(days=days)), session_row["id_hash"]))


def end_session(token: str) -> None:
    """End one login session and retire the account's previous-release cookies."""
    if not token:
        return
    id_hash = _hash(token)
    with db.transaction():
        row = db.one("SELECT user_id FROM auth_sessions WHERE id_hash=?", (id_hash,))
        if row:
            revoke_session(row["user_id"], id_hash)


def session_hash(token: str) -> str:
    return _hash(token)


def list_sessions(user_id: str):
    return db.query("SELECT id_hash, created_at, last_seen_at, expires_at, user_agent, ip_address "
                    "FROM auth_sessions WHERE user_id=? AND expires_at>? ORDER BY last_seen_at DESC",
                    (user_id, db.now()))


def revoke_session(user_id: str, id_hash: str) -> bool:
    """End one session without letting an old cookie recreate it.

    Old cookies have an account-wide version, so all unconverted cookies for
    this account must be retired. Other server-side sessions stay valid.
    """
    with db.transaction():
        removed = db.execute("DELETE FROM auth_sessions WHERE user_id=? AND id_hash=?",
                             (user_id, id_hash)).rowcount == 1
        if removed:
            db.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (user_id,))
        return removed


def revoke_sessions(user_id: str, *, except_hash: str | None = None) -> None:
    """End the account's login sessions (all, or all but one), including previous-release cookies."""
    with db.transaction():
        # Those cookies carry no session token; a new session version retires them.
        db.execute("UPDATE users SET session_version=session_version+1 WHERE id=?", (user_id,))
        if except_hash:
            db.execute("DELETE FROM auth_sessions WHERE user_id=? AND id_hash<>?", (user_id, except_hash))
        else:
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions() -> int:
    return db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (db.now(),)).rowcount


# ----- sliding-window rate limits -------------------------------------------

def hit(key: str, limit: int, window_seconds: int, *, record: bool = True) -> bool:
    """Record one event for *key*; return False when *limit* is already reached."""
    if limit <= 0:
        return False
    since = db.now(-timedelta(seconds=window_seconds))
    with db.transaction():
        used = db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE key=? AND hit_at>?", (key, since), 0)
        if used >= limit:
            return False
        if record:
            db.execute("INSERT INTO rate_limit_hits (key, hit_at) VALUES (?, ?)", (key, db.now()))
    return True


def count_hits(key: str, window_seconds: int) -> int:
    since = db.now(-timedelta(seconds=window_seconds))
    return db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE key=? AND hit_at>?", (key, since), 0)


def clear_hits(key: str) -> None:
    db.execute("DELETE FROM rate_limit_hits WHERE key=?", (key,))


def purge_rate_limits(max_window_seconds: int = 86400) -> None:
    cutoff = db.now(-timedelta(seconds=max_window_seconds))
    db.execute("DELETE FROM rate_limit_hits WHERE hit_at<?", (cutoff,))
    db.execute("DELETE FROM login_attempts WHERE attempted_at<?", (cutoff,))


# ----- audit log ------------------------------------------------------------

def audit(actor, action: str, target: str = "", details: dict | str | None = None, ip_address: str = "") -> None:
    """Record an administrative or security-relevant action."""
    if isinstance(details, dict):
        details = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)
    db.execute(
        "INSERT INTO audit_log (actor_id, actor_name, action, target, details, ip_address, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (actor["id"] if actor else None, actor["username"] if actor else "", action[:80], str(target)[:200],
         (details or "")[:4000], ip_address[:64], db.now()),
    )


def list_audit(limit: int = 100, offset: int = 0, action: str = ""):
    if action:
        return db.query("SELECT * FROM audit_log WHERE action=? ORDER BY id DESC LIMIT ? OFFSET ?",
                        (action, limit, offset))
    return db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))


def count_audit(action: str = "") -> int:
    if action:
        return db.scalar("SELECT COUNT(*) FROM audit_log WHERE action=?", (action,), 0)
    return db.scalar("SELECT COUNT(*) FROM audit_log", default=0)
