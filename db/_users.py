"""User CRUD and auth helpers."""

import secrets
import string
import base64
import json
import re
from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy


_A11Y_DEFAULTS = {
    "theme_mode": "default",
    "interface_language": "default",
    "font_scale": 1.0,
    "contrast": 0,
    "sidebar_width": 260,
    "custom_bg": "",
    "custom_text": "",
    "custom_primary": "",
    "custom_secondary": "",
    "custom_accent": "",
    "custom_sidebar": "",
    "background_image": "",
    "line_height": 0,
    "letter_spacing": 0,
    "reduce_motion": 0,
    "semantic_bold": 0,
    "semantic_italic": 0,
    "semantic_code": 0,
    "semantic_link": 0,
    "semantic_heading": 0,
}
_A11Y_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_A11Y_RGB_COLOR_RE = re.compile(
    r"^rgb\(\s*(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*,\s*"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\s*\)$"
)
_A11Y_BACKGROUND_IMAGE_RE = re.compile(r"^[0-9a-f]{32}\.jpe?g$")


def _clean_a11y_pref(key, value):
    """Sanitize values that can be interpolated into CSS or asset URLs."""
    if key == "background_image":
        value = value.strip() if isinstance(value, str) else ""
        return value if _A11Y_BACKGROUND_IMAGE_RE.fullmatch(value) else ""
    if key.startswith("custom_"):
        value = value.strip() if isinstance(value, str) else ""
        if _A11Y_HEX_COLOR_RE.fullmatch(value) or _A11Y_RGB_COLOR_RE.fullmatch(value):
            return value
        return ""
    return value


@retry_on_busy
def get_user_accessibility(user_id):
    """Return a user's synchronized UI preferences merged with defaults."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT accessibility FROM users WHERE id=?", (user_id,)
        ).fetchone()
    result = dict(_A11Y_DEFAULTS)
    if row and row["accessibility"]:
        try:
            saved = json.loads(row["accessibility"])
            if isinstance(saved, dict):
                result.update({
                    key: _clean_a11y_pref(key, value)
                    for key, value in saved.items()
                    if key in _A11Y_DEFAULTS
                })
        except (json.JSONDecodeError, TypeError):
            pass
    return result


@retry_on_busy
def save_user_accessibility(user_id, prefs):
    """Persist only recognized, sanitized UI preferences for a user."""
    cleaned = {
        key: _clean_a11y_pref(key, prefs[key])
        for key in _A11Y_DEFAULTS
        if key in prefs
    }
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET accessibility=? WHERE id=?",
            (json.dumps(cleaned, separators=(",", ":")), user_id),
        )
        conn.commit()


def _gen_user_id():
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(8))


def is_suspension_active(user):
    if not user or not user["suspended"]:
        return False
    until = user["suspended_until"]
    if not until:
        return True
    try:
        expiry = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < expiry
    except (ValueError, TypeError):
        return True


def check_suspension_expired(user_id):
    """Auto-unsuspend if timed suspension elapsed. Returns True if unsuspended."""
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT suspended, suspended_until FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row or not row["suspended"]:
            return False
        until = row["suspended_until"]
        if not until:
            return False
        try:
            expiry = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < expiry:
                return False
        except (ValueError, TypeError):
            return False
        conn.execute("UPDATE users SET suspended=0, suspended_until=NULL WHERE id=?", (user_id,))
        conn.commit()
        return True


@retry_on_busy
def get_user_by_id(user_id):
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


@retry_on_busy
def get_user_by_username(username):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)
        ).fetchone()


def _insert_user(conn, username, password_hash, role="user", invite_code=None):
    """Insert a user in the caller's transaction without committing it."""
    for _ in range(5):
        uid = _gen_user_id()
        if conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            continue
        conn.execute(
            "INSERT INTO users (id, username, password, role, invite_code) VALUES (?,?,?,?,?)",
            (uid, username, password_hash, role, invite_code),
        )
        return uid
    raise RuntimeError("Failed to generate unique user ID")


def create_user(username, password_hash, role="user", invite_code=None):
    """Insert an operator-created user and return their ID."""
    with get_db_context() as conn:
        uid = _insert_user(conn, username, password_hash, role, invite_code)
        conn.commit()
        return uid


def create_signup_user(username, password_hash, invite_code=None):
    """Create an account and consume its invite as one authorized transaction."""
    from ._invites import _get_valid_invite, _consume_invite

    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = conn.execute("SELECT * FROM site_settings WHERE id=1").fetchone()
        if not settings or not settings["setup_done"] or settings["signup_mode"] == "disabled" or settings["maintenance_mode"]:
            raise ValueError("Signups are currently unavailable.")
        invite = None
        role = "user"
        if settings["signup_mode"] == "invite":
            invite = _get_valid_invite(conn, invite_code)
            if invite is None:
                raise ValueError("Invalid or expired invite code.")
            role = invite["assigned_role"] if invite["assigned_role"] in {"user", "admin"} else "user"
        uid = _insert_user(conn, username, password_hash, role, invite["code"] if invite else None)
        if invite:
            _consume_invite(conn, invite, uid)
        conn.commit()
        return uid


def update_last_login(user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))
        conn.commit()


@retry_on_busy
def change_password(user_id, new_hash):
    with get_db_context() as conn:
        conn.execute("UPDATE users SET password=?, session_version=session_version+1 WHERE id=?", (new_hash, user_id))
        conn.commit()


@retry_on_busy
def delete_user(user_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()


@retry_on_busy
def list_users(limit=200, offset=0):
    with get_db_context() as conn:
        return conn.execute(
            """SELECT u.*, uq.daily_credits, uq.daily_slow_credits
               FROM users u
               LEFT JOIN user_quota uq ON u.id = uq.user_id
               ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()


@retry_on_busy
def suspend_user(user_id, until=None):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET suspended=1, suspended_until=? WHERE id=?",
            (until, user_id),
        )
        conn.commit()


@retry_on_busy
def set_user_role(user_id, role):
    with get_db_context() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        conn.commit()


@retry_on_busy
def unsuspend_user(user_id):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE users SET suspended=0, suspended_until=NULL WHERE id=?", (user_id,)
        )
        conn.commit()


def count_admins():
    with get_db_context() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()
        return row[0] if row else 0


def count_users():
    with get_db_context() as conn:
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] if row else 0


@retry_on_busy
def record_login_attempt(ip):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO login_attempts (ip_address, attempted_at) VALUES (?,?)", (ip, now)
        )
        conn.commit()


def check_login_rate_limit(ip, max_attempts=10, window_seconds=60):
    """Return True if IP is over the login attempt limit."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_address=? AND attempted_at>=?",
            (ip, cutoff),
        ).fetchone()
        return (row[0] if row else 0) >= max_attempts


@retry_on_busy
def clear_login_attempts(ip):
    with get_db_context() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip_address=?", (ip,))
        conn.commit()


def collect_user_gdpr_data(user_id):
    """Collect all GDPR-relevant data for a user.

    Returns account, access-policy, usage, and chat data owned by the user.
    Callers add export metadata (exported_at, exported_by_admin, etc.) on top.
    """
    with get_db_context() as conn:
        quota_row = conn.execute(
            "SELECT daily_credits, daily_slow_credits, updated_at "
            "FROM user_quota WHERE user_id=?",
            (user_id,),
        ).fetchone()

        tokens_rows = conn.execute(
            "SELECT name, token_prefix, created_at, last_used_at, revoked "
            "FROM api_tokens WHERE user_id=? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

        ledger_rows = conn.execute(
            "SELECT cl.credits_used, cl.is_slow, cl.tokens_in, cl.tokens_out, "
            "cl.request_type, cl.created_at, am.display_name AS model_name "
            "FROM credit_ledger cl LEFT JOIN ai_models am ON cl.model_id=am.id "
            "WHERE cl.user_id=? ORDER BY cl.created_at ASC",
            (user_id,),
        ).fetchall()

        access_memberships = conn.execute(
            "SELECT scope, resource_id, list_type, reason, expires_at, created_at, updated_at "
            "FROM model_access_memberships WHERE user_id=? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        access_requests = conn.execute(
            "SELECT scope, resource_id, use_case, confirmed_safe, confirmed_logging, "
            "status, admin_message, created_at, resolved_at "
            "FROM model_access_requests WHERE user_id=? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        quota_requests = conn.execute(
            "SELECT new_credits, new_slow_credits, reason, duration_type, status, "
            "admin_message, resolution_source, created_at, resolved_at FROM quota_requests "
            "WHERE user_id=? ORDER BY created_at ASC, id ASC",
            (user_id,),
        ).fetchall()
        image_reservations = conn.execute(
            "SELECT credits_reserved, is_slow, created_at "
            "FROM image_credit_reservations WHERE user_id=? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        personality_rows = conn.execute(
            "SELECT id, name, instructions, is_enabled, admin_disabled, disabled_until, "
            "disabled_reason, created_at, updated_at FROM personalities "
            "WHERE user_id=? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

        session_rows = conn.execute(
            "SELECT id, title, created_at, updated_at, deleted_at "
            "FROM chat_sessions WHERE user_id=? AND is_incognito=0 "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

        chat_sessions = []
        for sess in session_rows:
            msg_rows = conn.execute(
                "SELECT id, role, content, created_at FROM chat_messages "
                "WHERE session_id=? ORDER BY id ASC",
                (sess["id"],),
            ).fetchall()
            messages = []
            for message in msg_rows:
                attachment_rows = conn.execute(
                    "SELECT id, kind, filename, media_type, size_bytes, sha256, "
                    "extracted_text, image_data, created_at FROM chat_attachments "
                    "WHERE message_id=? ORDER BY created_at ASC",
                    (message["id"],),
                ).fetchall()
                attachments = []
                for attachment in attachment_rows:
                    item = dict(attachment)
                    image_data = item.pop("image_data", None)
                    if image_data is not None:
                        item["image_base64"] = base64.b64encode(image_data).decode("ascii")
                    attachments.append(item)
                item = dict(message)
                item["attachments"] = attachments
                messages.append(item)
            chat_sessions.append({
                "id": sess["id"],
                "title": sess["title"],
                "created_at": sess["created_at"],
                "updated_at": sess["updated_at"],
                "deleted_at": sess["deleted_at"],
                "messages": messages,
            })

    return {
        "ui_customization": get_user_accessibility(user_id),
        "quota": dict(quota_row) if quota_row else {},
        "api_tokens": [
            {
                "name": t["name"],
                "prefix": t["token_prefix"],
                "created_at": t["created_at"],
                "last_used_at": t["last_used_at"],
                "revoked": bool(t["revoked"]),
            }
            for t in tokens_rows
        ],
        "credit_history": [dict(r) for r in ledger_rows],
        "model_access_memberships": [dict(r) for r in access_memberships],
        "model_access_requests": [dict(r) for r in access_requests],
        "quota_requests": [dict(r) for r in quota_requests],
        "pending_image_credit_reservations": [dict(r) for r in image_reservations],
        "custom_personalities": [dict(r) for r in personality_rows],
        "chat_sessions": chat_sessions,
    }


def check_rate_limit(key, max_requests, window_seconds):
    """Return True if the request is allowed, False if rate-limited.

    Uses rate_limit_hits for cross-worker coordination.  The write is
    serialised with BEGIN IMMEDIATE so the count + insert is atomic.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff_iso = (now - timedelta(seconds=window_seconds)).isoformat()
    cleanup_iso = (now - timedelta(hours=2)).isoformat()

    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM rate_limit_hits WHERE hit_at < ?", (cleanup_iso,))
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_hits WHERE key=? AND hit_at>=?",
            (key, cutoff_iso),
        ).fetchone()[0]
        if count >= max_requests:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO rate_limit_hits (key, hit_at) VALUES (?,?)",
            (key, now_iso),
        )
        conn.commit()
    return True


def complete_initial_setup(username, password_hash, site_name="BananaChat"):
    """Create the first administrator and close setup in the same transaction."""
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        settings = conn.execute("SELECT setup_done FROM site_settings WHERE id=1").fetchone()
        if not settings or settings["setup_done"] or conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("Initial setup has already been completed.")
        uid = _gen_user_id()
        conn.execute(
            "INSERT INTO users (id, username, password, role) VALUES (?, ?, ?, 'admin')",
            (uid, username, password_hash),
        )
        conn.execute("UPDATE site_settings SET setup_done=1, site_name=? WHERE id=1", (site_name,))
        conn.commit()
    return uid
