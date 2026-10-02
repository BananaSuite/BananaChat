"""The controls migration preserves existing effort caps and database objects."""

import sqlite3

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS
from bananachat.db.migrations.v18_limit_controls import upgrade
from sqlite_migrations import apply_migrations


def test_v18_preserves_caps_flags_indexes_triggers_and_sequence(tmp_path):
    with sqlite3.connect(tmp_path / "v17.db") as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:17])
        conn.execute("INSERT INTO users(id,username,password) VALUES ('u','uma','fixture')")
        conn.execute("INSERT INTO users(id,username,password) VALUES ('v','victor','fixture')")
        conn.execute("INSERT INTO user_limits(user_id,speed,effort_gating_off) VALUES ('u','slow',1)")
        conn.execute("INSERT INTO user_effort_levels(id,user_id,level,pinned,source,updated_at) "
                     "VALUES (7,'u','high',1,'admin','2026-01-01 00:00:00')")
        conn.execute("INSERT INTO user_effort_levels(id,user_id,level) VALUES (99,'v','low')")
        conn.execute("DELETE FROM user_effort_levels WHERE id=99")
        conn.execute("CREATE TABLE effort_probe(level TEXT)")
        conn.execute("CREATE TRIGGER effort_probe_insert AFTER INSERT ON user_effort_levels "
                     "BEGIN INSERT INTO effort_probe VALUES (new.level); END")
        before = conn.execute("SELECT * FROM user_effort_levels").fetchall()
        objects = conn.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name='user_effort_levels' "
                               "AND type IN ('index','trigger') ORDER BY name").fetchall()
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:18])
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert conn.execute("SELECT * FROM user_effort_levels").fetchall() == before
        assert conn.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name='user_effort_levels' "
                            "AND type IN ('index','trigger') ORDER BY name").fetchall() == objects
        assert conn.execute("SELECT speed,effort_gating_off,effort_auto_unlock_off,token_exempt,rate_exempt "
                            "FROM user_limits").fetchone() == ("slow", 1, 0, 0, 0)
        conn.execute("DELETE FROM user_effort_levels")
        conn.execute("INSERT INTO user_effort_levels(user_id,level) VALUES ('u','extra')")
        assert conn.execute("SELECT id,level FROM user_effort_levels").fetchone() == (100, "extra")
        assert conn.execute("SELECT level FROM effort_probe").fetchall() == [("extra",)]
        upgrade(conn)
        assert conn.execute("SELECT id,level FROM user_effort_levels").fetchone() == (100, "extra")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
