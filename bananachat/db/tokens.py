"""Personal API tokens (``bc-...``). Only a SHA-256 hash is stored."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from bananachat import db

PREFIX = "bc-"
MAX_PER_USER = 10


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_raw() -> str:
    return PREFIX + secrets.token_urlsafe(40)


def list_for(user_id: str):
    return db.query("SELECT id, name, token_prefix, created_at, last_used_at FROM api_tokens "
                    "WHERE user_id=? AND revoked=0 ORDER BY id DESC", (user_id,))


def count_active(user_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=0", (user_id,), 0)


def _clean_name(name: str) -> str:
    return " ".join((name or "").split())[:64]


def create(user_id: str, name: str = "") -> tuple[int, str]:
    raw = _new_raw()
    with db.transaction():
        if count_active(user_id) >= MAX_PER_USER:
            raise ValueError(f"You can have at most {MAX_PER_USER} active tokens.")
        cursor = db.execute("INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, created_at) "
                            "VALUES (?,?,?,?,?)", (user_id, _clean_name(name), hash_token(raw), raw[:12], db.now()))
    return cursor.lastrowid, raw


def rotate(token_id: int, user_id: str) -> tuple[int, str]:
    raw = _new_raw()
    with db.transaction():
        row = db.one("SELECT name FROM api_tokens WHERE id=? AND user_id=? AND revoked=0", (token_id, user_id))
        if row is None:
            raise LookupError("Token not found.")
        db.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))
        cursor = db.execute("INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, created_at) "
                            "VALUES (?,?,?,?,?)", (user_id, row["name"], hash_token(raw), raw[:12], db.now()))
    return cursor.lastrowid, raw


def rename(token_id: int, user_id: str, name: str) -> bool:
    return db.execute("UPDATE api_tokens SET name=? WHERE id=? AND user_id=? AND revoked=0",
                      (_clean_name(name), token_id, user_id)).rowcount == 1


def revoke(token_id: int, user_id: str) -> bool:
    return db.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=? AND revoked=0",
                      (token_id, user_id)).rowcount == 1


def revoke_all(user_id: str) -> None:
    db.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,))


def authenticate(raw: str):
    """Return ``(token_row, user_row)`` for a valid token, else ``(None, None)``."""
    if not raw or not raw.startswith(PREFIX) or len(raw) > 200:
        return None, None
    token = db.one("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (hash_token(raw),))
    if token is None:
        return None, None
    user = db.one("SELECT * FROM users WHERE id=?", (token["user_id"],))
    return (token, user) if user else (None, None)


def touch(token_id: int) -> None:
    """Record use at most once a minute to keep API calls cheap."""
    db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=? AND "
               "(last_used_at IS NULL OR last_used_at<?)", (db.now(), token_id, db.now(-timedelta(minutes=1))))

