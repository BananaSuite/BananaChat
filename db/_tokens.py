"""API token management."""

import hashlib
import secrets
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy

MAX_TOKENS_PER_USER = 10
TOKEN_PREFIX_LEN = 8


def _make_token():
    """Generate a new API token. Returns (full_token, token_hash, prefix)."""
    raw = "bc-" + secrets.token_urlsafe(40)
    h = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:TOKEN_PREFIX_LEN + 4]  # "bc-XXXX"
    return raw, h, prefix


@retry_on_busy
def list_tokens(user_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM api_tokens WHERE user_id=? AND revoked=0 ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def count_active_tokens(user_id):
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=0", (user_id,)
        ).fetchone()
        return row[0] if row else 0


def create_token(user_id, name=""):
    """Create a new API token. Returns (token_id, full_token) or raises if limit reached."""
    if count_active_tokens(user_id) >= MAX_TOKENS_PER_USER:
        raise ValueError(f"Maximum of {MAX_TOKENS_PER_USER} active tokens reached")
    raw, h, prefix = _make_token()
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, token_prefix) VALUES (?,?,?,?)",
            (user_id, name, h, prefix),
        )
        conn.commit()
        return cur.lastrowid, raw


def rotate_token(token_id, user_id):
    """Revoke old token and create a new one with the same name. Returns new full_token."""
    with get_db_context() as conn:
        old = conn.execute(
            "SELECT name FROM api_tokens WHERE id=? AND user_id=? AND revoked=0",
            (token_id, user_id),
        ).fetchone()
        if not old:
            raise ValueError("Token not found")
        name = old["name"]
        conn.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))
        raw, h, prefix = _make_token()
        cur = conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, token_prefix) VALUES (?,?,?,?)",
            (user_id, name, h, prefix),
        )
        conn.commit()
        return cur.lastrowid, raw


def rename_token(token_id, user_id, name):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE api_tokens SET name=? WHERE id=? AND user_id=?", (name, token_id, user_id)
        )
        conn.commit()


def revoke_token(token_id, user_id):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=?", (token_id, user_id)
        )
        conn.commit()


def get_token_by_hash(token_hash):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT t.*, u.role, u.suspended FROM api_tokens t "
            "JOIN users u ON t.user_id=u.id "
            "WHERE t.token_hash=? AND t.revoked=0",
            (token_hash,),
        ).fetchone()


def touch_token(token_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, token_id))
        conn.commit()


def hash_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()
