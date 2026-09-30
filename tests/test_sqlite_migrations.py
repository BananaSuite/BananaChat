"""Failure and process-concurrency checks for durable schema upgrades."""

from contextlib import closing
import sqlite3

import pytest

from sqlite_migrations import apply_migrations, execute_script
from sqlite_runtime import DatabaseUnavailable


def test_failed_table_rebuild_preserves_data_schema_and_version(tmp_path):
    with closing(sqlite3.connect(tmp_path / "data.db")) as conn:
        conn.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE child (id INTEGER REFERENCES parent(id) ON DELETE CASCADE);
            INSERT INTO parent VALUES (1, 'customer content');
            INSERT INTO child VALUES (1);
        """)
        before = list(conn.iterdump())

        def interrupted(connection):
            execute_script(connection, """
                CREATE TABLE replacement (id INTEGER PRIMARY KEY, value TEXT, extra TEXT);
                INSERT INTO replacement (id, value) SELECT id, value FROM parent;
                DROP TABLE parent;
                ALTER TABLE replacement RENAME TO parent;
            """)
            raise OSError("simulated failure before migration completion")

        with pytest.raises(OSError, match="simulated failure"):
            apply_migrations(conn, 101, (interrupted,))
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_scripts_keep_trigger_bodies_and_foreign_keys_in_the_transaction(tmp_path):
    with closing(sqlite3.connect(tmp_path / "triggers.db")) as conn:
        def create(connection):
            execute_script(connection, """
                -- Semicolons in strings and trigger bodies are not boundaries.
                CREATE TABLE parent (id INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE audit (value TEXT);
                CREATE TABLE child (id INTEGER REFERENCES parent(id));
                CREATE TRIGGER record_insert AFTER INSERT ON parent BEGIN
                    INSERT INTO audit VALUES ('first; second');
                    INSERT INTO audit VALUES (new.value);
                END;
                INSERT INTO parent VALUES (1, 'kept');
                INSERT INTO child VALUES (1);
            """)

        apply_migrations(conn, 102, (create,))
        assert conn.execute("SELECT value FROM audit").fetchall() == [("first; second",), ("kept",)]
        apply_migrations(conn, 102, (create,))  # Must not rerun the CREATE statements.
        before = list(conn.iterdump())

        def orphan(connection):
            connection.execute("DELETE FROM parent")

        with pytest.raises(DatabaseUnavailable, match="foreign-key"):
            apply_migrations(conn, 102, (create, orphan))
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        with pytest.raises(DatabaseUnavailable, match="different"):
            apply_migrations(conn, 103, (create,))
        with pytest.raises(DatabaseUnavailable, match="newer"):
            apply_migrations(conn, 102, ())
