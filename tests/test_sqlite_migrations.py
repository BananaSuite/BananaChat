"""Failure and process-concurrency checks for durable schema upgrades."""

from contextlib import closing
import multiprocessing
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


def test_application_upgrade_rolls_back_when_late_step_fails(tmp_path, monkeypatch):
    import config
    import db
    import db._schema as schema

    path = str(tmp_path / "application.db")
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    db.init_db()
    with closing(db.get_db()) as conn:
        conn.execute("INSERT INTO users(id, username, password) VALUES ('kept', 'customer', 'hash')")
        conn.execute("PRAGMA user_version=0")
        conn.commit()
        before = list(conn.iterdump())

    original = schema._upgrade_legacy

    def failing_upgrade(conn):
        original(conn)
        execute_script(conn, "CREATE TABLE unfinished (value TEXT); UPDATE users SET username='changed';")
        raise OSError("simulated migration interruption")

    monkeypatch.setattr(schema, "_upgrade_legacy", failing_upgrade)
    with pytest.raises(OSError, match="simulated migration interruption"):
        db.init_db()
    with closing(db.get_db()) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    monkeypatch.setattr(schema, "_upgrade_legacy", original)
    db.init_db()
    assert db.integrity_check() == "ok"


def _initialize_application(path, number, barrier, result):
    import config
    config.DATABASE_PATH = path
    import db

    barrier.wait(timeout=20)
    try:
        db.init_db()
        with closing(db.get_db()) as conn:
            conn.execute("INSERT INTO users(id, username, password) VALUES (?, ?, 'hash')",
                         (str(number), "worker-" + str(number)))
            conn.commit()
        result.put(None)
    except Exception as error:
        result.put(type(error).__name__ + ": " + str(error))


def test_real_application_initialization_across_workers(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "workers.db"
    barrier, result = ctx.Barrier(5), ctx.Queue()
    workers = [ctx.Process(target=_initialize_application, args=(str(path), n, barrier, result)) for n in range(4)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        assert [result.get(timeout=30) for _ in workers] == [None] * 4
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        result.close()
        result.join_thread()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT username FROM users ORDER BY username").fetchall() == [("worker-" + str(n),) for n in range(4)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
