"""Personal API tokens (``bc-...``). Only a SHA-256 hash is stored.

A token has a scope: ``full`` (every endpoint, the default and what tokens
created before scopes keep) or ``read`` (only reading endpoints such as the
model list). It may expire; an expired token works like a revoked one.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from bananachat import db

PREFIX = "bc-"
MAX_PER_USER = 10
SCOPES = ("full", "read")
EXPIRY_DAYS = (0, 7, 30, 90, 365)  # 0: never
_LIVE = "revoked=0 AND (expires_at IS NULL OR expires_at>?)"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_raw() -> str:
    return PREFIX + secrets.token_urlsafe(40)


def list_for(user_id: str):
    """The account's usable tokens (neither revoked nor expired), newest first."""
    return db.query("SELECT id, name, token_prefix, created_at, last_used_at, scope, expires_at FROM api_tokens "
                    f"WHERE user_id=? AND {_LIVE} ORDER BY id DESC", (user_id, db.now()))


def is_live(token_id: int, user_id: str) -> bool:
    """Whether the account's token is still usable (neither revoked nor expired)."""
    return db.scalar(f"SELECT COUNT(*) FROM api_tokens WHERE id=? AND user_id=? AND {_LIVE}",
                     (token_id, user_id, db.now()), 0) == 1


def count_active(user_id: str) -> int:
    return db.scalar(f"SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND {_LIVE}", (user_id, db.now()), 0)


def _clean_name(name: str) -> str:
    return " ".join((name or "").split())[:64]


def _insert(user_id: str, name: str, scope: str, lifetime: timedelta | None) -> tuple[int, str]:
    if scope not in SCOPES:
        raise ValueError("Unknown token scope.")
    raw = _new_raw()
    cursor = db.execute("INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, created_at, scope, "
                        "expires_at) VALUES (?,?,?,?,?,?,?)",
                        (user_id, name, hash_token(raw), raw[:12], db.now(), scope,
                         db.now(lifetime) if lifetime else None))
    return cursor.lastrowid, raw


def create(user_id: str, name: str = "", *, scope: str = "full", expires_days: int = 0) -> tuple[int, str]:
    if expires_days not in EXPIRY_DAYS:
        raise ValueError("Unsupported token lifetime.")
    with db.transaction():
        if count_active(user_id) >= MAX_PER_USER:
            raise OverflowError(f"You can have at most {MAX_PER_USER} active tokens.")
        return _insert(user_id, _clean_name(name), scope, timedelta(days=expires_days) if expires_days else None)


def rotate(token_id: int, user_id: str) -> tuple[int, str]:
    """Replace a token with a new secret; the name, scope and lifetime carry over."""
    with db.transaction():
        row = db.one(f"SELECT name, scope, created_at, expires_at FROM api_tokens WHERE id=? AND user_id=? AND {_LIVE}",
                     (token_id, user_id, db.now()))
        if row is None:
            raise LookupError("Token not found.")
        lifetime = None
        if row["expires_at"]:
            created, expires = db.parse_timestamp(row["created_at"]), db.parse_timestamp(row["expires_at"])
            lifetime = (expires - created) if created and expires and expires > created else timedelta(days=1)
        db.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))
        return _insert(user_id, row["name"], row["scope"] or "full", lifetime)


def rename(token_id: int, user_id: str, name: str) -> bool:
    return db.execute(f"UPDATE api_tokens SET name=? WHERE id=? AND user_id=? AND {_LIVE}",
                      (_clean_name(name), token_id, user_id, db.now())).rowcount == 1


def revoke(token_id: int, user_id: str) -> bool:
    return db.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=? AND revoked=0",
                      (token_id, user_id)).rowcount == 1


def revoke_all(user_id: str) -> int:
    """Revoke every token of the account; returns how many were usable."""
    live = count_active(user_id)
    db.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=? AND revoked=0", (user_id,))
    return live


def authenticate(raw: str):
    """Return ``(token_row, user_row)`` for a valid token, else ``(None, None)``."""
    if not raw or not raw.startswith(PREFIX) or len(raw) > 200:
        return None, None
    token = db.one(f"SELECT * FROM api_tokens WHERE token_hash=? AND {_LIVE}", (hash_token(raw), db.now()))
    if token is None:
        return None, None
    user = db.one("SELECT * FROM users WHERE id=?", (token["user_id"],))
    return (token, user) if user else (None, None)


def touch(token_id: int) -> None:
    """Record use at most once a minute to keep API calls cheap."""
    db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=? AND "
               "(last_used_at IS NULL OR last_used_at<?)", (db.now(), token_id, db.now(-timedelta(minutes=1))))

