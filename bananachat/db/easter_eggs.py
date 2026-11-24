"""The site's easter egg list (``site_settings.easter_egg_configs``) and who found which egg
(``easter_egg_finds``, schema version 31). The format and its rules are in :mod:`bananachat.eggs`."""

from __future__ import annotations

import copy
from functools import lru_cache

from bananachat import db, eggs


@lru_cache(maxsize=8)
def _read(stored: str) -> dict:
    return eggs.clean(stored)


def config(settings: dict) -> dict:
    """The egg list as stored, cleaned (never raises); the default Konami egg when nothing is stored."""
    stored = settings.get("easter_egg_configs")
    if not isinstance(stored, str) or not stored.strip():
        return eggs.default_config()
    return copy.deepcopy(_read(stored))


def stored_text(settings: dict) -> str:
    """The list for the editor's hidden field: what is stored, or the default list."""
    return eggs.dumps(config(settings))


def enabled(settings: dict, egg_id: str) -> dict | None:
    """The egg *egg_id* when the site offers key-sequence eggs and that egg is on, else None."""
    if not settings.get("easter_konami", 1):
        return None
    return next((egg for egg in config(settings)["eggs"] if egg["id"] == egg_id and egg["enabled"]), None)


def record_find(user_id: str, egg_id: str) -> bool:
    """Remember that the account found the egg; True the first time only."""
    cursor = db.execute("INSERT INTO easter_egg_finds (user_id, egg_id, found_at) VALUES (?,?,?) "
                        "ON CONFLICT(user_id, egg_id) DO NOTHING", (user_id, egg_id, db.now()))
    return cursor.rowcount == 1


def finds(user_id: str) -> dict[str, str]:
    """``{egg id: found_at}`` for the account (eggs removed since are included)."""
    return {row["egg_id"]: row["found_at"] for row in db.query(
        "SELECT egg_id, found_at FROM easter_egg_finds WHERE user_id=? ORDER BY found_at, egg_id", (user_id,))}
