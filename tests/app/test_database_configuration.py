"""Reinitializing an app must not invalidate another thread's transaction."""

from __future__ import annotations

import sqlite3
import threading

import pytest


def test_same_database_configuration_preserves_a_background_transaction(app):
    from bananachat import db

    db.execute("CREATE TABLE configuration_markers (value INTEGER)")
    target = app.config["BC"].database_path
    started, proceed = threading.Event(), threading.Event()
    errors = []

    def work():
        try:
            with db.transaction():
                db.execute("INSERT INTO configuration_markers VALUES (1)")
                started.set()
                if not proceed.wait(5):
                    raise RuntimeError("database reconfiguration was not signalled")
                db.execute("INSERT INTO configuration_markers VALUES (2)")
        except BaseException as error:
            errors.append(error)
        finally:
            db.close_thread_connection()

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert started.wait(5), "the background transaction did not start"
        # The app factory selects the same database while the task is active.
        db.configure(target)
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
    # Both writes must commit atomically. Closing the original connection used
    # to roll back 1, autocommit 2 separately, then fail at the original COMMIT.
    assert [row["value"] for row in db.query("SELECT value FROM configuration_markers ORDER BY value")] == [1, 2]


def test_same_database_configuration_preserves_nested_savepoints(app):
    from bananachat import db

    db.execute("CREATE TABLE configuration_markers (value INTEGER)")
    target = app.config["BC"].database_path
    with db.transaction() as connection:
        db.execute("INSERT INTO configuration_markers VALUES (1)")
        with db.transaction():
            db.configure(str(target))
            assert db.conn() is connection
            db.execute("INSERT INTO configuration_markers VALUES (2)")
        db.execute("INSERT INTO configuration_markers VALUES (3)")
    assert [row["value"] for row in db.query("SELECT value FROM configuration_markers ORDER BY value")] == [1, 2, 3]


def test_different_database_configuration_still_switches_connections(app, tmp_path):
    from bananachat import db

    original_path = app.config["BC"].database_path
    db.execute("CREATE TABLE configuration_markers (value INTEGER)")
    db.execute("INSERT INTO configuration_markers VALUES (11)")
    original_connection = db.conn()
    target = tmp_path / "second.sqlite"
    with sqlite3.connect(target) as second:
        second.execute("CREATE TABLE configuration_markers (value INTEGER)")
        second.execute("INSERT INTO configuration_markers VALUES (22)")

    try:
        db.configure(target)
        assert db.path() == target.absolute()
        assert db.conn() is not original_connection
        assert db.scalar("SELECT value FROM configuration_markers") == 22
        db.execute("INSERT INTO configuration_markers VALUES (33)")
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            original_connection.execute("SELECT 1")
        with sqlite3.connect(original_path) as original:
            assert original.execute("SELECT value FROM configuration_markers").fetchall() == [(11,)]
        with sqlite3.connect(target) as second:
            assert second.execute("SELECT value FROM configuration_markers ORDER BY value").fetchall() == [(22,), (33,)]
    finally:
        db.close_thread_connection()
        db.configure(original_path)
