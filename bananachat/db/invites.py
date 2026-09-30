"""Invitation codes for invite-only sign-up."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from bananachat import db

CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,32}$")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I look-alikes


def _random_code() -> str:
    part = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(4))  # noqa: E731
    return f"{part()}-{part()}"


def create(created_by: str, *, max_uses: int = 1, expires_at: datetime | None = None,
           assigned_role: str | None = None, code: str | None = None) -> str:
    if not 0 <= max_uses <= 100000:
        raise ValueError("Uses must be between 0 (unlimited) and 100000.")
    if assigned_role not in (None, "user", "admin"):
        raise ValueError("Unknown role.")
    if code:
        if not CUSTOM_CODE_RE.match(code):
            raise ValueError("Custom codes use 4-32 letters, digits or hyphens.")
        code = code.upper()
    with db.transaction():
        for _ in range(10):
            candidate = code or _random_code()
            if not db.one("SELECT 1 FROM invite_codes WHERE upper(code)=?", (candidate.upper(),)):
                break
            if code:
                raise ValueError("That code already exists.")
        else:
            raise RuntimeError("Could not generate a unique code.")
        db.execute("INSERT INTO invite_codes (code, created_by, created_at, expires_at, max_uses, assigned_role) "
                   "VALUES (?,?,?,?,?,?)",
                   (candidate, created_by, db.now(), db.timestamp(expires_at) if expires_at else None,
                    max_uses, assigned_role))
    return candidate


def _usable(row) -> bool:
    if row is None or row["deleted"]:
        return False
    if row["max_uses"] and row["use_count"] >= row["max_uses"]:
        return False
    expires = db.parse_timestamp(row["expires_at"])
    if expires and expires <= datetime.now(timezone.utc):
        return False
    creator = db.one("SELECT role, suspended FROM users WHERE id=?", (row["created_by"],)) \
        if row["created_by"] else None
    if creator and creator["suspended"]:
        return False
    # An administrator invitation carries its creator's authority: it stops working
    # when the creator is no longer an administrator (demoted or deleted).
    if row["assigned_role"] == "admin" and (creator is None or creator["role"] != "admin"):
        return False
    return True


def find_usable(code: str):
    row = db.one("SELECT * FROM invite_codes WHERE upper(code)=?", ((code or "").strip().upper(),))
    return row if _usable(row) else None


def consume(code: str, user_id: str):
    """Use one invitation for *user_id*; call inside the sign-up transaction."""
    row = find_usable(code)
    if row is None:
        raise ValueError("This invitation code is invalid, used up or expired.")
    updated = db.execute("UPDATE invite_codes SET use_count=use_count+1 WHERE id=? AND "
                         "(max_uses=0 OR use_count<max_uses)", (row["id"],))
    if updated.rowcount != 1:
        raise ValueError("This invitation code is invalid, used up or expired.")
    db.execute("INSERT INTO invite_code_usage (invite_code_id, user_id, used_at) VALUES (?,?,?)",
               (row["id"], user_id, db.now()))
    return row


def delete(invite_id: int) -> None:
    db.execute("UPDATE invite_codes SET deleted=1, deleted_at=? WHERE id=? AND deleted=0", (db.now(), invite_id))


def list_all():
    rows = db.query("SELECT i.*, u.username AS created_by_name FROM invite_codes i "
                    "LEFT JOIN users u ON u.id=i.created_by WHERE i.deleted=0 ORDER BY i.id DESC LIMIT 500")
    active, inactive = [], []
    for row in rows:
        (active if _usable(row) else inactive).append(row)
    return active, inactive


def usage(invite_id: int):
    return db.query("SELECT u.used_at, us.username FROM invite_code_usage u JOIN users us ON us.id=u.user_id "
                    "WHERE u.invite_code_id=? ORDER BY u.used_at DESC", (invite_id,))
