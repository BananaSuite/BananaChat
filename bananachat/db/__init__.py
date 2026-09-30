"""SQLite access: one connection per thread, explicit transactions.

Usage::

    from bananachat import db

    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    with db.transaction():
        db.execute("UPDATE ...")

Connections run in autocommit mode; writes that must be atomic use
``db.transaction()`` (``BEGIN IMMEDIATE``, or a savepoint when nested).
Timestamps are stored as UTC ``YYYY-MM-DD HH:MM:SS`` strings (see ``now``).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlite_runtime
from sqlite_migrations import apply_migrations
from sqlite_runtime import DatabaseUnavailable, is_unavailable  # noqa: F401  (re-exported)

from .migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION  # noqa: F401

__all__ = [
    "Row", "configure", "path", "conn", "close_thread_connection", "transaction", "execute", "executemany",
    "query", "one", "scalar", "init_db", "now", "timestamp", "parse_timestamp", "DatabaseUnavailable",
    "is_unavailable", "SCHEMA_VERSION", "APPLICATION_ID",
]


class Row(sqlite3.Row):
    """``sqlite3.Row`` with ``dict``-style ``get``."""

    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default

    def to_dict(self) -> dict:
        return {key: self[key] for key in self.keys()}


_path: Path | None = None
_local = threading.local()
_generation = 0


def configure(database_path) -> None:
    """Select the database used by :func:`conn` (called by the app factory)."""
    global _path, _generation
    _path = Path(database_path).absolute()
    _generation += 1


def path() -> Path:
    if _path is None:
        raise RuntimeError("The database has not been configured.")
    return _path


def _open(target: Path) -> sqlite3.Connection:
    connection = sqlite_runtime.connect(target, prefix="BC", row_factory=Row)
    connection.isolation_level = None
    return connection


def conn() -> sqlite3.Connection:
    """This thread's connection to the configured database."""
    current = getattr(_local, "connection", None)
    if current is not None and getattr(_local, "generation", None) == _generation:
        return current
    if current is not None:
        _safe_close(current)
    connection = _open(path())
    _local.connection = connection
    _local.generation = _generation
    return connection


def _safe_close(connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    try:
        connection.close()
    except sqlite3.Error:
        pass


def close_thread_connection() -> None:
    """Close this thread's connection (thread exit, fork, or broken state)."""
    connection = getattr(_local, "connection", None)
    _local.connection = None
    if connection is not None:
        _safe_close(connection)


def release_thread_connection() -> None:
    """End-of-request hygiene: never let an open transaction leak to the next request."""
    connection = getattr(_local, "connection", None)
    if connection is not None and connection.in_transaction:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            close_thread_connection()


@contextmanager
def transaction(*, immediate: bool = True):
    """Run a block atomically. Nested calls use savepoints."""
    connection = conn()
    if connection.in_transaction:
        name = f"sp_{id(connection)}_{time.monotonic_ns()}"
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO {name}")
            connection.execute(f"RELEASE {name}")
            raise
        connection.execute(f"RELEASE {name}")
        return
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            close_thread_connection()
        raise
    connection.execute("COMMIT")


def execute(sql: str, params=()) -> sqlite3.Cursor:
    return conn().execute(sql, params)


def executemany(sql: str, rows) -> sqlite3.Cursor:
    return conn().executemany(sql, rows)


def query(sql: str, params=()) -> list[Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params=()) -> Row | None:
    return conn().execute(sql, params).fetchone()


def scalar(sql: str, params=(), default=None):
    row = conn().execute(sql, params).fetchone()
    return default if row is None or row[0] is None else row[0]


# ----- schema ---------------------------------------------------------------

def init_db(database_path=None) -> None:
    """Create or upgrade the database. Safe to call from every worker process."""
    target = Path(database_path or path()).absolute()

    @sqlite_runtime.serialized_schema(lambda: target)
    def _initialize():
        connection = _open(target)
        try:
            apply_migrations(connection, APPLICATION_ID, MIGRATIONS)
        finally:
            connection.close()

    _initialize()


def schema_version() -> int:
    return int(scalar("PRAGMA user_version", default=0))


def ping() -> bool:
    """Cheap readiness probe used by ``/health``."""
    try:
        return scalar("SELECT 1") == 1
    except sqlite3.Error:
        close_thread_connection()
        return False


def integrity_check() -> str:
    return sqlite_runtime.integrity_check(path())


# ----- time -----------------------------------------------------------------

_FORMAT = "%Y-%m-%d %H:%M:%S"


def now(offset: timedelta | None = None) -> str:
    """Current UTC time in the stored format, optionally shifted."""
    moment = datetime.now(timezone.utc)
    if offset:
        moment += offset
    return moment.strftime(_FORMAT)


def timestamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_FORMAT)


def parse_timestamp(value) -> datetime | None:
    """Parse any stored timestamp format (SQLite, ISO-8601, epoch) as aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def day_bounds(moment: datetime | None = None) -> tuple[str, str]:
    """Start (inclusive) and end (exclusive) of the UTC day containing *moment*."""
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime(_FORMAT), (start + timedelta(days=1)).strftime(_FORMAT)
