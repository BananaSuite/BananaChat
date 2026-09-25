"""A real writer can keep committing while a verified snapshot is created."""

from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from sqlite_snapshot import snapshot


def test_snapshot_is_consistent_during_writes_and_supports_normal_filenames(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE totals (count INTEGER)")
        conn.execute("INSERT INTO totals VALUES (0)")
    stopped, ready = threading.Event(), threading.Event()
    def writer():
        with closing(sqlite3.connect(source)) as conn:
            number = 0
            while not stopped.is_set():
                with conn:
                    conn.execute("INSERT INTO ledger VALUES (?, ?)", (number, "committed together"))
                    conn.execute("UPDATE totals SET count=count+1")
                number += 1
                ready.set()
    thread = threading.Thread(target=writer)
    thread.start()
    destination = tmp_path / "Luca's wiki backup: verified.db"
    try:
        assert ready.wait(3)
        snapshot(source, destination)
        with closing(sqlite3.connect(destination)) as copied:
            assert copied.execute("SELECT count FROM totals").fetchone()[0] == copied.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            assert copied.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        stopped.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not list(tmp_path.glob(".sqlite-snapshot-*"))


def test_missing_corrupt_and_existing_targets_are_preserved(tmp_path):
    missing, target = tmp_path / "missing.db", tmp_path / "backup.db"
    with pytest.raises(ValueError):
        snapshot(missing, target)
    assert not missing.exists() and not target.exists()
    source = tmp_path / "damaged.db"
    source.write_bytes(b"damaged customer database")
    with pytest.raises(sqlite3.DatabaseError):
        snapshot(source, target)
    assert source.read_bytes() == b"damaged customer database" and not target.exists()
    target.write_bytes(b"previous verified backup")
    with pytest.raises(FileExistsError):
        snapshot(source, target)
    assert target.read_bytes() == b"previous verified backup"
    assert not list(tmp_path.glob(".sqlite-snapshot-*"))


def test_interrupted_snapshot_never_publishes_partial_copy(tmp_path):
    source, destination = tmp_path / "live.db", tmp_path / "interrupted.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE records(value TEXT)")
        conn.execute("INSERT INTO records VALUES ('keep this data')")
    before = source.read_bytes()
    with pytest.raises(TimeoutError):
        snapshot(source, destination, timeout=0)
    assert not destination.exists() and source.read_bytes() == before
    assert not list(tmp_path.glob(".sqlite-snapshot-*"))
