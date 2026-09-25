"""Shared SQLite lifecycle policy for application and hosting databases.

Only an explicit, serialized schema initializer may create a database. Ordinary
connections never replace a missing or damaged database, including on restart.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
from pathlib import Path
import sqlite3
import stat

from filelock import FileLock, Timeout

_SCHEMA_PATH = ContextVar("banana_schema_path", default=None)
_UNAVAILABLE_CODES = {
    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CORRUPT,
    sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY,
}


class DatabaseUnavailable(sqlite3.OperationalError):
    """Storage requires operator intervention or a later retry."""


def is_unavailable(error):
    if isinstance(error, DatabaseUnavailable):
        return True
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 255 in _UNAVAILABLE_CODES
    return isinstance(error, sqlite3.DatabaseError) and any(text in str(error).lower() for text in (
        "database is locked", "database is busy", "database table is locked",
        "database disk image is malformed", "file is not a database",
        "disk i/o error", "database or disk is full", "unable to open database",
        "attempt to write a readonly database",
    ))


def _path(value):
    return Path(value).absolute()


def _check_path(path, *, initializing=False):
    marker = Path(str(path) + ".initialized")
    if path.is_symlink() or marker.is_symlink():
        raise DatabaseUnavailable("Database and initialization marker must be regular files.")
    if path.exists() and not path.is_file():
        raise DatabaseUnavailable("The configured database is not a regular file.")
    if marker.exists() and not marker.is_file():
        raise DatabaseUnavailable("The database initialization marker is invalid.")
    missing = not path.exists()
    empty = not missing and path.stat().st_size == 0
    if missing or empty:
        sidecars = any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        if not initializing or marker.exists() or sidecars:
            raise DatabaseUnavailable("The database is missing or empty. Preserve its files and restore a verified backup.")


def _mark_initialized(path):
    marker = Path(str(path) + ".initialized")
    if marker.exists():
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(b"BananaSuite SQLite initialized\n")
        output.flush()
        os.fsync(output.fileno())
    if os.name == "posix":
        directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@contextmanager
def boot_lock(path, *, what="Schema initialization"):
    """Hold the setup lock for *path* so only one worker prepares storage.

    Workers that start together otherwise run the same first-boot writes at
    the same time. The schema initializer takes this lock; anything else that
    seeds rows during startup should take it too.
    """
    path = _path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".schema.lock")
    if lock_path.is_symlink():
        raise DatabaseUnavailable("The schema lock cannot be a symbolic link.")
    lock = FileLock(lock_path, timeout=120, mode=0o600)
    try:
        lock.acquire()
    except Timeout as error:
        raise DatabaseUnavailable(f"{what} is busy. Check the active migration before retrying.") from error
    try:
        yield path
    finally:
        lock.release()


def serialized_schema(path_provider):
    """Coordinate schema changes across workers and remember successful setup."""
    def decorate(function):
        @wraps(function)
        def initialize(*args, **kwargs):
            with boot_lock(path_provider()) as path:
                _check_path(path, initializing=True)
                token = _SCHEMA_PATH.set(path)
                try:
                    result = function(*args, **kwargs)
                finally:
                    _SCHEMA_PATH.reset(token)
                _check_path(path)
                _mark_initialized(path)
                return result
        return initialize
    return decorate


def _integer(prefix, suffix, default, minimum, maximum):
    key = prefix + "_DB_" + suffix
    try:
        value = int(os.environ.get(key, default))
    except ValueError:
        raise ValueError(f"{key} must be an integer.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def connect(path, *, prefix, factory=sqlite3.Connection, row_factory=sqlite3.Row):
    """Open storage with bounded per-connection resources and durable commits."""
    path = _path(path)
    initializing = _SCHEMA_PATH.get() == path
    _check_path(path, initializing=initializing)
    busy_ms = _integer(prefix, "BUSY_TIMEOUT_MS", 5000, 100, 30000)
    cache_kib = _integer(prefix, "CACHE_KIB", 4096, 256, 65536)
    synchronous = os.environ.get(prefix + "_DB_SYNCHRONOUS", "FULL").upper()
    if synchronous not in {"FULL", "NORMAL"}:
        raise ValueError(prefix + "_DB_SYNCHRONOUS must be FULL or NORMAL.")
    if initializing:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatabaseUnavailable("The database must be a regular file without hard links.")
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                           timeout=busy_ms / 1000, factory=factory)
    conn.row_factory = row_factory
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        conn.execute("PRAGMA synchronous=" + synchronous)
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute(f"PRAGMA cache_size={-cache_kib}")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn
    except BaseException:
        conn.close()
        raise


def integrity_check(path):
    """Inspect existing storage without creating files or attempting salvage."""
    try:
        path = _path(path)
        _check_path(path)
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "empty result from quick_check"
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        return str(error)
