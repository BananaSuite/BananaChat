"""The optional notice migrates without changing existing settings or data."""

import sqlite3
from contextlib import closing

import pytest

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v20_worker_offline_warning import upgrade
from sqlite_migrations import apply_migrations


def test_v19_upgrade_defaults_notice_on_and_preserves_disabled_setting(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v19.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:19])
        conn.execute("UPDATE site_settings SET site_name='Existing', warning_banner_enabled=1, "
                     "warning_banner_message='Announcement', maintenance_mode=1 WHERE id=1")
        conn.execute("INSERT INTO users(id,username,password) VALUES ('existing','existing','fixture')")
        previous = conn.execute("SELECT site_name,warning_banner_enabled,warning_banner_message,maintenance_mode "
                                "FROM site_settings WHERE id=1").fetchone()
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT worker_offline_warning_enabled FROM site_settings").fetchone() == (1,)
        assert conn.execute("SELECT site_name,warning_banner_enabled,warning_banner_message,maintenance_mode "
                            "FROM site_settings WHERE id=1").fetchone() == previous
        assert conn.execute("SELECT id FROM users").fetchall() == [("existing",)]
        conn.execute("UPDATE site_settings SET worker_offline_warning_enabled=0 WHERE id=1")
        upgrade(conn)
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("SELECT worker_offline_warning_enabled FROM site_settings").fetchone() == (0,)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("value", [None, -1, 2, "invalid"])
def test_notice_flag_has_a_boolean_constraint(tmp_path, value):
    with closing(sqlite3.connect(tmp_path / "notice.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE site_settings SET worker_offline_warning_enabled=? WHERE id=1", (value,))
        assert conn.execute("SELECT worker_offline_warning_enabled FROM site_settings").fetchone() == (1,)
