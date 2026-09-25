"""Invite code management."""

import string
import secrets
import sqlite3
from datetime import datetime, timezone


from ._connection import get_db_context, retry_on_busy


def _generate_random_invite_code():
    """Generate a human-readable random invite code."""
    chars = string.ascii_uppercase + string.digits
    return (
        "".join(secrets.choice(chars) for _ in range(4))
        + "-"
        + "".join(secrets.choice(chars) for _ in range(4))
    )


def _normalize_custom_invite_code(custom_code):
    """Normalize and validate an administrator-supplied invite code."""
    code = (custom_code or "").strip().upper()
    if not code or len(code) > 32 or not code.isalnum():
        raise ValueError("custom invite code must be 1-32 alphanumeric characters")
    return code


def generate_invite_code(created_by, max_uses=1, expires_at=None,
                         assigned_role=None, custom_code=None):
    """Generate and persist a new invite code for *created_by*.

    - *max_uses*: Number of times the code can be used (0 for unlimited).
    - *expires_at*: ISO datetime string for expiration (None for never).
    - *assigned_role*: Role to assign on signup ('user', 'admin'). None defaults to 'user'.
    - *custom_code*: Optional admin-specified alphanumeric code (1-32 chars).
    Returns the code string.
    """
    _VALID_ROLES = ("user", "admin")
    if assigned_role and assigned_role not in _VALID_ROLES:
        assigned_role = None

    code = _normalize_custom_invite_code(custom_code) if custom_code else None
    now = datetime.now(timezone.utc)
    with get_db_context() as conn:
        attempts = 1 if code else 5
        for _ in range(attempts):
            candidate = code or _generate_random_invite_code()
            existing = conn.execute(
                "SELECT 1 FROM invite_codes WHERE code=? COLLATE NOCASE",
                (candidate,),
            ).fetchone()
            if existing:
                if code:
                    raise sqlite3.IntegrityError("invite code already exists")
                continue
            try:
                conn.execute(
                    "INSERT INTO invite_codes (code, created_by, created_at, expires_at, max_uses, assigned_role) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (candidate, created_by, now.isoformat(), expires_at, max_uses, assigned_role),
                )
                conn.commit()
                return candidate
            except sqlite3.IntegrityError:
                if code:
                    raise
                continue
        raise RuntimeError("Failed to generate a unique invite code after 5 attempts")


def _get_valid_invite(conn, code):
    """Read current invite permissions using the caller's transaction."""
    row = conn.execute(
        "SELECT ic.*, u.suspended AS creator_suspended "
        "FROM invite_codes ic "
        "LEFT JOIN users u ON ic.created_by = u.id "
        "WHERE ic.code=? COLLATE NOCASE AND ic.deleted=0",
        (code,),
    ).fetchone()
    if not row or row["creator_suspended"]:
        return None
    if row["max_uses"] > 0 and row["use_count"] >= row["max_uses"]:
        return None
    if row["expires_at"]:
        try:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires:
                return None
        except (ValueError, TypeError):
            return None
    return row


@retry_on_busy
def validate_invite_code(code):
    """Return a currently valid invite, comparing codes without case sensitivity."""
    with get_db_context() as conn:
        return _get_valid_invite(conn, code)


def _consume_invite(conn, invite, user_id):
    """Record redemption in the transaction that validated the invite."""
    changed = conn.execute(
        "UPDATE invite_codes SET use_count=use_count+1 "
        "WHERE id=? AND deleted=0 AND (max_uses=0 OR use_count<max_uses)",
        (invite["id"],),
    )
    if changed.rowcount != 1:
        raise ValueError("Invalid or expired invite code.")
    conn.execute(
        "INSERT INTO invite_code_usage (invite_code_id, user_id, used_at) VALUES (?, ?, ?)",
        (invite["id"], user_id, datetime.now(timezone.utc).isoformat()),
    )


def use_invite_code(code, user_id):
    """Redeem an invite for an existing user, rechecking all validity conditions."""
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite = _get_valid_invite(conn, code)
        if invite is None:
            return False
        _consume_invite(conn, invite, user_id)
        conn.commit()
        return True


def delete_invite_code(code_id):
    """Soft-delete an invite code by setting its ``deleted`` flag."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("UPDATE invite_codes SET deleted=1, deleted_at=? WHERE id=?", (now, code_id))
        conn.commit()


def hard_delete_invite_code(code_id):
    """Permanently remove an expired/used/deleted invite code record."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM invite_codes WHERE id=?", (code_id,))
        conn.commit()


@retry_on_busy
def list_invite_codes(active_only=True):
    """Return invite codes, optionally limited to active (not fully used, non-expired, non-deleted) ones."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        if active_only:
            rows = conn.execute(
                "SELECT ic.*, COALESCE(u.username, 'deleted user') AS creator_name FROM invite_codes ic "
                "LEFT JOIN users u ON ic.created_by=u.id "
                "WHERE ic.deleted=0 AND (ic.max_uses=0 OR ic.use_count < ic.max_uses) "
                "AND (ic.expires_at IS NULL OR julianday(ic.expires_at) > julianday(?)) "
                "ORDER BY ic.created_at DESC",
                (now,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ic.*, COALESCE(u.username, 'deleted user') AS creator_name FROM invite_codes ic "
                "LEFT JOIN users u ON ic.created_by=u.id "
                "ORDER BY ic.created_at DESC"
            ).fetchall()
        return rows


@retry_on_busy
def list_expired_codes():
    """Return all invite codes that have been fully used, soft-deleted, or have expired."""
    with get_db_context() as conn:
        now = datetime.now(timezone.utc).isoformat()
        rows = conn.execute(
            "SELECT ic.*, COALESCE(u.username, 'deleted user') AS creator_name FROM invite_codes ic "
            "LEFT JOIN users u ON ic.created_by=u.id "
            "WHERE (ic.max_uses > 0 AND ic.use_count >= ic.max_uses) OR ic.deleted=1 OR (ic.expires_at IS NOT NULL AND julianday(ic.expires_at) <= julianday(?)) "
            "ORDER BY ic.created_at DESC",
            (now,),
        ).fetchall()
        return rows


@retry_on_busy
def get_invite_code_usage(invite_code_id):
    """Return detailed usage records for an invite code."""
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT icu.*, u.username "
            "FROM invite_code_usage icu "
            "LEFT JOIN users u ON icu.user_id = u.id "
            "WHERE icu.invite_code_id=? "
            "ORDER BY icu.used_at DESC",
            (invite_code_id,)
        ).fetchall()
        return rows
