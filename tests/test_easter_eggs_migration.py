"""Version 31 adds the easter egg list and the finds without touching anything stored."""

import sqlite3
from contextlib import closing

import pytest

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS
from bananachat.db.migrations.v31_easter_eggs import upgrade
from sqlite_migrations import apply_migrations


def test_v30_upgrade_keeps_the_switches_and_starts_with_the_default_egg(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v30.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:30])
        conn.execute("UPDATE site_settings SET site_name='Existing', easter_konami=0, easter_mascot_shades=0 "
                     "WHERE id=1")
        conn.execute("INSERT INTO users(id,username,password) VALUES ('ann','ann','fixture')")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:31])
        assert MIGRATIONS[30] is upgrade
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 31
        assert conn.execute("SELECT site_name, easter_konami, easter_mascot_shades, easter_egg_configs "
                            "FROM site_settings").fetchone() == ("Existing", 0, 0, None)
        assert conn.execute("SELECT COUNT(*) FROM easter_egg_finds").fetchone() == (0,)
        upgrade(conn)  # safe to run again
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_finds_are_one_per_egg_and_go_with_the_account(tmp_path):
    with closing(sqlite3.connect(tmp_path / "finds.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users(id,username,password) VALUES ('ann','ann','fixture'), ('ben','ben','fixture')")
        conn.execute("INSERT INTO easter_egg_finds VALUES ('ann','konami','2026-11-24 10:00:00'), "
                     "('ann','party','2026-11-24 10:01:00'), ('ben','konami','2026-11-24 10:02:00')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO easter_egg_finds VALUES ('ann','konami','2026-11-24 11:00:00')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO easter_egg_finds VALUES ('nobody','konami','2026-11-24 11:00:00')")
        conn.execute("DELETE FROM users WHERE id='ann'")
        assert conn.execute("SELECT user_id, egg_id FROM easter_egg_finds").fetchall() == [("ben", "konami")]
