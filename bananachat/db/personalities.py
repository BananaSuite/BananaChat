"""Personalities: extra instructions and a presentation applied to a chat.

Two kinds share the table: ``user`` personalities are private to their owner,
``featured`` ones are published by an administrator for every user (stored
under the administrator who created them, see migration v7). Validation of
the fields lives in :mod:`bananachat.services.personalities`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone

from bananachat import db

MAX_PER_USER = 20
MAX_NAME = 80
MAX_INSTRUCTIONS = 8000
MAX_FEATURED = 50

KINDS = ("user", "featured")
# Optional fields with their stored default; ``starters`` is a list stored as JSON.
EXTRA_FIELDS = {"description": "", "avatar": "", "color": "", "greeting": "", "starters": (),
                "preferred_model": "", "response_length": "balanced", "creativity": "balanced"}


def _validate(name: str, instructions: str) -> tuple[str, str]:
    name, instructions = (name or "").strip(), (instructions or "").strip()
    if not 1 <= len(name) <= MAX_NAME:
        raise ValueError(f"Names need 1-{MAX_NAME} characters.")
    if not 1 <= len(instructions) <= MAX_INSTRUCTIONS:
        raise ValueError(f"Instructions need 1-{MAX_INSTRUCTIONS} characters.")
    return name, instructions


def _extras(fields: dict) -> dict:
    unknown = set(fields) - set(EXTRA_FIELDS)
    if unknown:
        raise TypeError(f"Unknown personality fields: {', '.join(sorted(unknown))}")
    values = {name: fields.get(name, default) for name, default in EXTRA_FIELDS.items()}
    values["starters"] = json.dumps([str(item) for item in values["starters"] or ()], ensure_ascii=False)
    return values


def starters(row) -> list[str]:
    """The conversation starters of a row (tolerates malformed stored values)."""
    try:
        value = json.loads(row["starters"] or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


# ----- reading ------------------------------------------------------------------

def get(personality_id):
    return db.one("SELECT p.*, u.username FROM personalities p JOIN users u ON u.id=p.user_id WHERE p.id=?",
                  (personality_id,))


def list_for(user_id: str, *, enabled_only: bool = False):
    """The user's own personalities (featured ones an administrator published are not included)."""
    clause = " AND is_enabled=1" if enabled_only else ""
    return db.query(f"SELECT * FROM personalities WHERE user_id=? AND kind='user'{clause} "
                    "ORDER BY name COLLATE NOCASE", (user_id,))


def count_for(user_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM personalities WHERE user_id=? AND kind='user'", (user_id,), 0)


def list_featured(*, published_only: bool = True):
    clause = " AND p.is_enabled=1" if published_only else ""
    return db.query("SELECT p.*, u.username FROM personalities p JOIN users u ON u.id=p.user_id "
                    f"WHERE p.kind='featured'{clause} ORDER BY p.name COLLATE NOCASE LIMIT ?", (MAX_FEATURED,))


def count_featured() -> int:
    return db.scalar("SELECT COUNT(*) FROM personalities WHERE kind='featured'", (), 0)


def name_taken(user_id: str, name: str, *, exclude_id: int | None = None) -> bool:
    """Whether the owner already has a personality (of any kind) with this name, ignoring case."""
    return db.scalar("SELECT 1 FROM personalities WHERE user_id=? AND name=? COLLATE NOCASE AND id<>?",
                     (user_id, name.strip(), exclude_id or 0)) is not None


def featured_name_taken(name: str, *, exclude_id: int | None = None) -> bool:
    return db.scalar("SELECT 1 FROM personalities WHERE kind='featured' AND name=? COLLATE NOCASE AND id<>?",
                     (name.strip(), exclude_id or 0)) is not None


def free_name(user_id: str, name: str) -> str:
    """*name*, or "name (2)", "name (3)"… when the owner already uses it."""
    name = name.strip()[:MAX_NAME]
    if not name_taken(user_id, name):
        return name
    for number in range(2, MAX_PER_USER + MAX_FEATURED + 3):
        suffix = f" ({number})"
        candidate = name[:MAX_NAME - len(suffix)].rstrip() + suffix
        if not name_taken(user_id, candidate):
            return candidate
    return name[:MAX_NAME - 9].rstrip() + " " + secrets.token_hex(4)


def admin_page(limit: int, offset: int, *, search: str = "", kind: str = ""):
    """(total, rows) for the moderation list; *kind* is ``user``, ``featured``, ``shared`` or empty."""
    clauses, params = [], []
    if search:
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(u.username LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    if kind in KINDS:
        clauses.append("p.kind=?")
        params.append(kind)
    elif kind == "shared":
        clauses.append("p.share_token IS NOT NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.scalar(f"SELECT COUNT(*) FROM personalities p JOIN users u ON u.id=p.user_id {where}", params, 0)
    rows = db.query("SELECT p.*, u.username FROM personalities p JOIN users u ON u.id=p.user_id "
                    f"{where} ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    return total, rows


# ----- writing ------------------------------------------------------------------

def create(user_id: str, name: str, instructions: str, *, created_by: str | None, enabled: bool = True,
           kind: str = "user", **fields) -> int:
    name, instructions = _validate(name, instructions)
    if kind not in KINDS:
        raise ValueError("Unknown personality kind.")
    extras = _extras(fields)
    with db.transaction():
        if kind == "user" and count_for(user_id) >= MAX_PER_USER:
            raise ValueError(f"You can have at most {MAX_PER_USER} personalities.")
        if kind == "featured" and count_featured() >= MAX_FEATURED:
            raise ValueError(f"There can be at most {MAX_FEATURED} featured personalities.")
        columns = ["user_id", "name", "instructions", "is_enabled", "kind", "created_by", "updated_by",
                   "created_at", "updated_at", *extras]
        values = [user_id, name, instructions, int(enabled), kind, created_by, created_by, db.now(), db.now(),
                  *extras.values()]
        try:
            cursor = db.execute(f"INSERT INTO personalities ({', '.join(columns)}) "
                                f"VALUES ({', '.join('?' for _ in columns)})", values)
        except sqlite3.IntegrityError:
            raise ValueError("A personality with that name already exists.") from None
    return cursor.lastrowid


def update(personality_id: int, *, name: str, instructions: str, enabled: bool, updated_by: str | None,
           **fields) -> None:
    """Replace the editable fields. Fields not given keep their stored value."""
    name, instructions = _validate(name, instructions)
    extras = _extras(fields)
    extras = {key: value for key, value in extras.items() if key in fields}
    assignments = "".join(f", {column}=?" for column in extras)
    try:
        db.execute(f"UPDATE personalities SET name=?, instructions=?, is_enabled=?, updated_by=?, updated_at=?"
                   f"{assignments} WHERE id=?",
                   (name, instructions, int(enabled), updated_by, db.now(), *extras.values(), personality_id))
    except sqlite3.IntegrityError:
        raise ValueError("A personality with that name already exists.") from None


def set_enabled(personality_id: int, enabled: bool, updated_by: str) -> None:
    db.execute("UPDATE personalities SET is_enabled=?, updated_by=?, updated_at=? WHERE id=?",
               (int(enabled), updated_by, db.now(), personality_id))


def moderate(personality_id: int, *, disabled: bool, updated_by: str | None, until: datetime | None = None,
             reason: str = "") -> None:
    db.execute("UPDATE personalities SET admin_disabled=?, disabled_until=?, disabled_reason=?, updated_by=?, "
               "updated_at=? WHERE id=?",
               (int(disabled), db.timestamp(until) if (disabled and until) else None,
                (reason or "")[:500] if disabled else "", updated_by, db.now(), personality_id))


def delete(personality_id: int) -> None:
    db.execute("DELETE FROM personalities WHERE id=?", (personality_id,))


# ----- share links ----------------------------------------------------------------

def share(personality_id: int) -> str:
    """The personality's share token, created on first use and stable until revoked."""
    with db.transaction():
        token = db.scalar("SELECT share_token FROM personalities WHERE id=?", (personality_id,))
        if token:
            return token
        token = secrets.token_urlsafe(24)
        db.execute("UPDATE personalities SET share_token=?, shared_at=? WHERE id=?",
                   (token, db.now(), personality_id))
    return token


def revoke_share(personality_id: int) -> None:
    db.execute("UPDATE personalities SET share_token=NULL, shared_at=NULL WHERE id=?", (personality_id,))


def by_share_token(token):
    if not isinstance(token, str) or not 16 <= len(token) <= 64 or \
            not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in token):
        return None
    return db.one("SELECT p.*, u.username FROM personalities p JOIN users u ON u.id=p.user_id "
                  "WHERE p.share_token=? AND p.kind='user'", (token,))


# ----- default personality ----------------------------------------------------------

def default_id(user_id: str) -> int | None:
    return db.scalar("SELECT personality_id FROM personality_defaults WHERE user_id=?", (user_id,))


def set_default(user_id: str, personality_id: int | None) -> None:
    if personality_id is None:
        db.execute("DELETE FROM personality_defaults WHERE user_id=?", (user_id,))
        return
    db.execute("INSERT INTO personality_defaults (user_id, personality_id, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(user_id) DO UPDATE SET personality_id=excluded.personality_id, "
               "updated_at=excluded.updated_at", (user_id, personality_id, db.now()))


# ----- state ------------------------------------------------------------------------

def is_blocked(personality) -> bool:
    """Disabled by an administrator and the suspension (if it has an end) has not ended."""
    if personality is None or not personality["admin_disabled"]:
        return False
    until = db.parse_timestamp(personality["disabled_until"])
    return until is None or until > datetime.now(timezone.utc)


def is_active(personality) -> bool:
    """Enabled by its owner (published, for featured ones) and not (or no longer) disabled by an administrator."""
    if personality is None or not personality["is_enabled"]:
        return False
    return not is_blocked(personality)
