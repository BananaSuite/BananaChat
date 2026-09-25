"""Exercise real multi-process startup, data preservation and resource policy."""

from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import time

import pytest

import sqlite_runtime


def _migrate_in_process(path, number, barrier, results):
    @sqlite_runtime.serialized_schema(lambda: path)
    def initialize():
        with closing(sqlite_runtime.connect(path, prefix="FIXTURE")) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
            if "value" not in columns:
                time.sleep(0.03)
                conn.execute("ALTER TABLE records ADD COLUMN value TEXT")
            conn.execute("INSERT INTO records VALUES (?, ?)", (number, "customer content"))
            conn.commit()
    barrier.wait(timeout=15)
    try:
        initialize()
        results.put(None)
    except Exception as error:
        results.put(type(error).__name__ + ": " + str(error))


def test_concurrent_startup_serializes_migrations_without_lost_records(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "shared.db"
    barrier, results = ctx.Barrier(5), ctx.Queue()
    workers = [ctx.Process(target=_migrate_in_process, args=(str(path), number, barrier, results)) for number in range(4)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=15)
        assert [results.get(timeout=20) for _ in workers] == [None] * 4
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT id, value FROM records ORDER BY id").fetchall() == [(number, "customer content") for number in range(4)]
    assert sqlite_runtime.integrity_check(path) == "ok"
    assert Path(str(path) + ".initialized").is_file()


def _seed_in_process(path, barrier, results):
    barrier.wait(timeout=15)
    try:
        with sqlite_runtime.boot_lock(path, what="Fixture seeding"):
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS seeds (id TEXT PRIMARY KEY)")
                if conn.execute("SELECT 1 FROM seeds WHERE id = ?", ("only",)).fetchone() is None:
                    time.sleep(0.03)
                    conn.execute("INSERT INTO seeds VALUES (?)", ("only",))
                conn.commit()
        results.put(None)
    except Exception as error:
        results.put(type(error).__name__ + ": " + str(error))


def test_boot_lock_serializes_first_boot_seeding(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = str(tmp_path / "seeded.db")
    barrier, results = ctx.Barrier(5), ctx.Queue()
    workers = [ctx.Process(target=_seed_in_process, args=(path, barrier, results)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait(timeout=15)
        assert [results.get(timeout=20) for _ in workers] == [None] * 4
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT id FROM seeds").fetchall() == [("only",)]


def test_creation_requires_schema_scope_and_never_discards_orphaned_wal(tmp_path):
    path = tmp_path / "lost.db"
    with pytest.raises(sqlite_runtime.DatabaseUnavailable):
        sqlite_runtime.connect(path, prefix="FIXTURE")
    assert not path.exists()
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"unrecovered customer writes")
    @sqlite_runtime.serialized_schema(lambda: path)
    def initialize():
        sqlite_runtime.connect(path, prefix="FIXTURE").close()
    with pytest.raises(sqlite_runtime.DatabaseUnavailable):
        initialize()
    assert sidecar.read_bytes() == b"unrecovered customer writes"
    assert not path.exists()


def test_resource_policy_is_bounded_and_durable_by_default(tmp_path, monkeypatch):
    path = tmp_path / "policy.db"
    @sqlite_runtime.serialized_schema(lambda: path)
    def initialize():
        with closing(sqlite_runtime.connect(path, prefix="FIXTURE")) as conn:
            conn.execute("CREATE TABLE data (value TEXT)")
            conn.commit()
    initialize()
    with closing(sqlite_runtime.connect(path, prefix="FIXTURE")) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -4096
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    monkeypatch.setenv("FIXTURE_DB_BUSY_TIMEOUT_MS", "30001")
    with pytest.raises(ValueError, match="between"):
        sqlite_runtime.connect(path, prefix="FIXTURE")


def test_schema_failure_is_not_reported_as_initialized(tmp_path):
    path = tmp_path / "interrupted.db"
    @sqlite_runtime.serialized_schema(lambda: path)
    def initialize():
        with closing(sqlite_runtime.connect(path, prefix="FIXTURE")) as conn:
            conn.execute("CREATE TABLE retained (value TEXT)")
            conn.execute("INSERT INTO retained VALUES ('committed customer data')")
            conn.commit()
        raise RuntimeError("migration failed")
    with pytest.raises(RuntimeError, match="migration failed"):
        initialize()
    assert not Path(str(path) + ".initialized").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM retained").fetchone()[0] == "committed customer data"
