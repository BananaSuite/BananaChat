"""Version 34: the announcement shows whenever it has a text; a switched-off text is emptied."""

import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v34_announcement_text import upgrade
from sqlite_migrations import apply_migrations


def _banner(conn):
    return conn.execute("SELECT warning_banner_enabled, warning_banner_message FROM site_settings").fetchone()


def test_a_switched_off_announcement_stays_hidden_and_a_shown_one_stays(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v33.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:33])
        conn.execute("UPDATE site_settings SET warning_banner_enabled=0, warning_banner_message='Kept for later'")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert MIGRATIONS[33] is upgrade
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 34
        assert _banner(conn) == (0, "")
        conn.execute("UPDATE site_settings SET warning_banner_enabled=1, warning_banner_message='Slow today'")
        upgrade(conn)  # safe to run again
        assert _banner(conn) == (1, "Slow today")
        conn.execute("UPDATE site_settings SET warning_banner_enabled=1, warning_banner_message='  '")
        upgrade(conn)
        assert _banner(conn)[0] == 0
