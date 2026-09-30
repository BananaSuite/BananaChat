"""Schema upgrades are atomic and safe when several workers start at once."""

from __future__ import annotations

import multiprocessing
import sqlite3
from contextlib import closing

import pytest


def _connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_a_failing_upgrade_leaves_the_database_as_it_was(tmp_path, monkeypatch):
    from bananachat import db

    path = tmp_path / "application.db"
    db.configure(path)
    db.init_db()
    with closing(_connect(path)) as conn:
        conn.execute("INSERT INTO users(id, username, password) VALUES ('kept', 'customer', 'hash')")
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION - 1}")
        conn.commit()
        before = list(conn.iterdump())

    def failing(conn):
        conn.execute("CREATE TABLE unfinished (value TEXT)")
        conn.execute("UPDATE users SET username='changed'")
        raise OSError("simulated migration interruption")

    monkeypatch.setattr(db, "MIGRATIONS", (*db.MIGRATIONS[:-1], failing))
    with pytest.raises(OSError, match="simulated migration interruption"):
        db.init_db()
    with closing(_connect(path)) as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    monkeypatch.undo()
    db.configure(path)
    db.init_db()
    assert db.integrity_check() == "ok"
    db.close_thread_connection()


def _initialize(path, number, barrier, result):
    from bananachat import db

    barrier.wait(timeout=20)
    try:
        db.configure(path)
        db.init_db()
        with db.transaction():
            db.execute("INSERT INTO users(id, username, password) VALUES (?, ?, 'hash')",
                       (str(number), f"worker-{number}"))
        result.put(None)
    except Exception as error:  # noqa: BLE001 - reported to the parent process
        result.put(f"{type(error).__name__}: {error}")


def test_workers_starting_together_initialise_one_database(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "workers.db"
    barrier, result = context.Barrier(5), context.Queue()
    workers = [context.Process(target=_initialize, args=(str(path), n, barrier, result)) for n in range(4)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=20)
        assert [result.get(timeout=60) for _ in workers] == [None] * 4
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        with closing(_connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
