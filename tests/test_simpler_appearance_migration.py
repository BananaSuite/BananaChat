"""Version 32 converts the six contrast levels and the five highlighting choices without lowering any."""

import json
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v32_simpler_appearance import upgrade
from sqlite_migrations import apply_migrations

BEFORE = {
    "ann": {"theme_mode": "light", "contrast": 0, "font_scale": 1.1, "semantic_bold": 0, "semantic_italic": 0,
            "semantic_code": 0, "semantic_link": 0, "semantic_heading": 0, "letter_spacing": 2},
    "ben": {"contrast": 1, "semantic_code": 1},
    "cat": {"contrast": 4, "semantic_bold": 1, "semantic_heading": 2},
    "dan": {"contrast": 5, "semantic_link": "2"},
    "eve": {"contrast": "x", "semantic_code": 7},
}
AFTER = {
    "ann": {"theme_mode": "light", "contrast": 0, "font_scale": 1.1, "letter_spacing": 2, "highlight": 0},
    "ben": {"contrast": 1, "highlight": 1},
    "cat": {"contrast": 1, "highlight": 2},
    "dan": {"contrast": 2, "highlight": 2},
    "eve": {"contrast": 0, "highlight": 0},
}


def _prefs(conn):
    return {user_id: json.loads(stored) for user_id, stored in
            conn.execute("SELECT id, accessibility FROM users WHERE accessibility LIKE '{%'")}


def test_v31_preferences_keep_at_least_the_chosen_contrast_and_highlighting(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v31.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:31])
        for name, prefs in BEFORE.items():
            conn.execute("INSERT INTO users(id,username,password,accessibility) VALUES (?,?,?,?)",
                         (name, name, "fixture", json.dumps(prefs)))
        conn.execute("INSERT INTO users(id,username,password,accessibility) VALUES ('fay','fay','fixture',NULL), "
                     "('gil','gil','fixture','not json'), ('hal','hal','fixture','[1, 2]')")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert MIGRATIONS[31] is upgrade
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 32
        assert _prefs(conn) == AFTER
        # Values it cannot read stay as they were (they read as the defaults, as before).
        assert conn.execute("SELECT accessibility FROM users WHERE id IN ('fay','gil','hal') ORDER BY id").fetchall() \
            == [(None,), ("not json",), ("[1, 2]",)]
        upgrade(conn)  # safe to run again: converted preferences are left alone
        assert _prefs(conn) == AFTER
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_converted_preferences_read_back_unchanged():
    from bananachat.db import users

    for prefs in AFTER.values():
        cleaned = users.clean_preferences(prefs)
        assert (cleaned["contrast"], cleaned["highlight"]) == (prefs["contrast"], prefs["highlight"])
        assert not any(key.startswith("semantic_") for key in cleaned)
