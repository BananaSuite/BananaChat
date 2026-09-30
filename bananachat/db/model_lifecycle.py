"""Model lifecycle storage: the administrator's policy, the ignore list and state-change events.

Decisions (what the policy means, which models match a rule) live in
``services.model_lifecycle``; the model columns are written by ``db.catalog``.
"""

from __future__ import annotations

import json

from bananachat import db


def load_policy() -> dict:
    raw = db.scalar("SELECT config FROM model_lifecycle_policy WHERE id=1", default="{}")
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def save_policy(config: dict, updated_by: str | None) -> None:
    db.execute("INSERT INTO model_lifecycle_policy (id, config, updated_by, updated_at) VALUES (1, ?, ?, ?) "
               "ON CONFLICT(id) DO UPDATE SET config=excluded.config, updated_by=excluded.updated_by, "
               "updated_at=excluded.updated_at",
               (json.dumps(config, sort_keys=True, separators=(",", ":")), updated_by, db.now()))


# ----- ignore list --------------------------------------------------------------------

def ignore_rules():
    return db.query("SELECT r.*, u.username AS created_by_name FROM model_ignore_rules r "
                    "LEFT JOIN users u ON u.id=r.created_by ORDER BY r.pattern COLLATE NOCASE")


def get_rule(rule_id: int):
    return db.one("SELECT * FROM model_ignore_rules WHERE id=?", (rule_id,))


def add_rule(pattern: str, note: str, created_by: str | None) -> tuple[int, bool]:
    """Store a rule; returns ``(id, created)`` (an identical rule is reused)."""
    with db.transaction():
        existing = db.one("SELECT id FROM model_ignore_rules WHERE pattern=?", (pattern,))
        if existing is not None:
            return existing["id"], False
        cursor = db.execute("INSERT INTO model_ignore_rules (pattern, note, created_by, created_at) VALUES (?,?,?,?)",
                            (pattern, note[:200], created_by, db.now()))
    return cursor.lastrowid, True


def delete_rule(rule_id: int) -> bool:
    return db.execute("DELETE FROM model_ignore_rules WHERE id=?", (rule_id,)).rowcount == 1


def delete_rule_pattern(pattern: str) -> bool:
    return db.execute("DELETE FROM model_ignore_rules WHERE pattern=?", (pattern,)).rowcount == 1


# ----- events ------------------------------------------------------------------------

def add_event(model_id, model_name: str, event: str, reason: str = "", actor=None) -> None:
    db.execute("INSERT INTO model_lifecycle_events (model_id, model_name, event, reason, actor_id, actor_name, "
               "created_at) VALUES (?,?,?,?,?,?,?)",
               (model_id, model_name[:300], event[:40], (reason or "")[:1000], actor["id"] if actor else None,
                actor["username"] if actor else "", db.now()))


def events_for(model_id: int, limit: int = 20):
    return db.query("SELECT * FROM model_lifecycle_events WHERE model_id=? ORDER BY id DESC LIMIT ?", (model_id, limit))


def recent_events(limit: int = 20):
    return db.query("SELECT * FROM model_lifecycle_events ORDER BY id DESC LIMIT ?", (limit,))


def purge_events(before: str) -> int:
    return db.execute("DELETE FROM model_lifecycle_events WHERE created_at<?", (before,)).rowcount
