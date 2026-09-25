"""Database connection helper."""

import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps

import config
import sqlite_runtime

from ops_observability import get_observability_snapshot, record_sql_timing, record_sqlite_retry

SYSTEM_USER_ID = -1

_logger = logging.getLogger("bananachat.db")


# Retry complete read-only/idempotent operations after transient SQLite
# contention. Connection deadlines and memory limits live in sqlite_runtime.
_BUSY_RETRY_ATTEMPTS = 4
_BUSY_RETRY_BASE_SLEEP = 0.05  # seconds; jittered backoff multiplier
_TRANSIENT_BUSY_TOKENS = (
    "database is locked",
    "database is busy",
    "locking protocol",
)
_OBSERVABILITY_ENABLED = os.environ.get("BC_DB_OBSERVABILITY", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

# Global write serialization lock.  Hot-path writers that want to opt
# in to in-process write serialization (rather than relying solely on
# SQLite's ``BEGIN IMMEDIATE`` + ``busy_timeout``) can wrap their
# transaction in ``with write_serialized(): ...``.  This is exposed as
# a knob for callers that have profiled write contention; it is not
# applied globally because SQLite's own busy_timeout already serialises
# writers cooperatively and adding a Python-level lock everywhere would
# regress write throughput in the common, uncontended case.
_WRITE_LOCK = threading.Lock()


def get_database_health():
    """Return a bounded SQLite health and durability report for admins."""
    report = {
        "healthy": False,
        "quick_check": "unavailable",
        "foreign_key_violations": None,
        "journal_mode": None,
        "synchronous": None,
        "busy_timeout_ms": None,
        "database_size_bytes": None,
        "wal_size_bytes": 0,
        "page_count": None,
        "freelist_count": None,
        "database_file_mode": None,
        "secure_permissions": None,
    }
    try:
        with get_db_context() as conn:
            report["quick_check"] = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            # Bound the result so a severely damaged database cannot make the
            # admin page allocate an unbounded list.
            violations = conn.execute("PRAGMA foreign_key_check").fetchmany(101)
            report["foreign_key_violations"] = len(violations)
            report["foreign_key_violations_truncated"] = len(violations) > 100
            report["journal_mode"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            sync_value = int(conn.execute("PRAGMA synchronous").fetchone()[0])
            report["synchronous"] = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(sync_value, str(sync_value))
            report["busy_timeout_ms"] = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            report["page_count"] = int(conn.execute("PRAGMA page_count").fetchone()[0])
            report["freelist_count"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        if os.path.exists(config.DATABASE_PATH):
            report["database_size_bytes"] = os.path.getsize(config.DATABASE_PATH)
            if os.name != "nt":
                mode = os.stat(config.DATABASE_PATH).st_mode & 0o777
                report["database_file_mode"] = f"{mode:03o}"
                report["secure_permissions"] = (mode & 0o077) == 0
        wal_path = config.DATABASE_PATH + "-wal"
        if os.path.exists(wal_path):
            report["wal_size_bytes"] = os.path.getsize(wal_path)
        report["healthy"] = (
            report["quick_check"].lower() == "ok"
            and report["foreign_key_violations"] == 0
            and report["journal_mode"].lower() == "wal"
            and report["secure_permissions"] is not False
        )
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        report["error"] = str(exc)
    return report


def integrity_check():
    """Report existing database integrity without replacing damaged data."""
    return sqlite_runtime.integrity_check(config.DATABASE_PATH)


@contextmanager
def write_serialized():
    """Serialize writers in this Python process.

    Use as ``with db.write_serialized(): ...`` around hot-path write
    transactions where SQLite's ``busy_timeout`` has empirically been
    insufficient.  This does *not* replace ``BEGIN IMMEDIATE``; it is
    an additional in-process queue that prevents N Gunicorn worker
    threads from all stampeding the same write at once.

    Opt-in only.  Wrapping every write in this lock would regress
    write throughput in the common uncontended case because SQLite's
    own busy_timeout already serialises writers cooperatively.
    """
    _WRITE_LOCK.acquire()
    try:
        yield
    finally:
        _WRITE_LOCK.release()


class _ObservedConnection(sqlite3.Connection):
    """SQLite connection subclass that records query timing buckets."""

    def execute(self, sql, parameters=(), /):
        """Execute *sql* and record its elapsed time in the SQL timing bucket."""
        started = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql, elapsed_ms)

    def executemany(self, sql, seq_of_parameters, /):
        """Execute *sql* once per parameter set; record elapsed time as a single sample."""
        started = time.perf_counter()
        try:
            return super().executemany(sql, seq_of_parameters)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql, elapsed_ms)

    def executescript(self, sql_script, /):
        """Execute a multi-statement SQL script and record total elapsed time."""
        started = time.perf_counter()
        try:
            return super().executescript(sql_script)
        finally:
            if _OBSERVABILITY_ENABLED:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                record_sql_timing(sql_script, elapsed_ms)


def _is_transient_busy_error(exc):
    """Return True if *exc* looks like a transient SQLite contention error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    msg = str(exc).lower()
    return any(token in msg for token in _TRANSIENT_BUSY_TOKENS)


def retry_on_busy(fn):
    """Decorator that retries *fn* on transient ``SQLITE_BUSY`` errors.

    The wrapper retries up to :data:`_BUSY_RETRY_ATTEMPTS` times with
    jittered exponential backoff (~50 ms, 100 ms, 200 ms, ...).  After
    the last attempt the original :class:`~sqlite3.OperationalError` is
    re-raised so genuinely-stuck callers still surface the problem.

    Use this on **read-only / idempotent** operations only: re-running a
    half-completed write transaction risks duplicating its effect once
    the lock clears.  Most application reads are safe; writes should
    rely on ``BEGIN IMMEDIATE`` + the connection-level ``busy_timeout``.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc = None
        retries = 0
        started = time.perf_counter()
        for attempt in range(_BUSY_RETRY_ATTEMPTS):
            try:
                result = fn(*args, **kwargs)
                if retries and _OBSERVABILITY_ENABLED:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    record_sqlite_retry(fn.__name__, retries, elapsed_ms, exhausted=False)
                return result
            except sqlite3.OperationalError as exc:
                if not _is_transient_busy_error(exc):
                    raise
                last_exc = exc
                if attempt == _BUSY_RETRY_ATTEMPTS - 1:
                    break
                retries += 1
                # Jittered exponential backoff: 50 ms, 100 ms, 200 ms …
                sleep_s = _BUSY_RETRY_BASE_SLEEP * (2 ** attempt)
                sleep_s *= 0.5 + random.random()
                time.sleep(sleep_s)
        if _OBSERVABILITY_ENABLED:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            record_sqlite_retry(fn.__name__, retries, elapsed_ms, exhausted=True)
        raise last_exc
    return wrapper


class _SafeRow(sqlite3.Row):
    """sqlite3.Row subclass with dict-like .get() method.

    ``sqlite3.Row`` does not support ``.get(key, default)``, so every
    function that receives a Row and wants safe attribute access must
    use bracket notation or catch ``KeyError``/``IndexError``.  This
    subclass adds ``.get()`` so callers that treat Rows as dicts
    (e.g. ``user.get("force_password_change")``) work without crashing.
    """

    def get(self, key, default=None):
        """Return self[key] or *default* without raising."""
        try:
            return self[key]
        except (KeyError, IndexError):
            return default


def get_db():
    """Open an existing database; only init_db may create fresh storage."""
    return sqlite_runtime.connect(config.DATABASE_PATH, prefix="BC",
                                  factory=_ObservedConnection, row_factory=_SafeRow)


def get_db_observability_snapshot():
    """Return current database observability counters and timing buckets."""
    return get_observability_snapshot()


@contextmanager
def get_db_context():
    """Context manager that yields a database connection and guarantees close.

    Usage::

        with get_db_context() as conn:
            conn.execute("SELECT ...")
    """
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def create_consistent_backup(dest_path):
    """Publish a private, consistent snapshot after verifying its integrity."""
    from sqlite_snapshot import snapshot
    return snapshot(config.DATABASE_PATH, dest_path)
