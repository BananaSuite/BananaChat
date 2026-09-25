"""Create private, verified SQLite snapshots without modifying the source."""

import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time


def snapshot(source, destination, *, timeout=300):
    """Publish a consistent copy only after integrity checks and durable writes.

    The destination must be new. Interrupted/failed copies leave the source and
    any previous backup untouched; unpublished temporary files are removed.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Snapshot source must be an existing, nonempty regular database.")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Choose a new backup filename; existing backups are never replaced.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < source.stat().st_size + 64 * 1024 * 1024:
        raise OSError("Not enough free space for a verified database snapshot.")
    descriptor, temporary = tempfile.mkstemp(prefix=".sqlite-snapshot-", dir=destination.parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Snapshot destination must be a regular file.")
        os.close(descriptor)
        descriptor = -1
        deadline = time.monotonic() + timeout

        def progress(*_):
            if time.monotonic() >= deadline:
                raise TimeoutError("Database snapshot exceeded its time limit.")

        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            src.execute("PRAGMA trusted_schema=OFF")
            dst = sqlite3.connect(temporary, timeout=5)
            try:
                src.backup(dst, pages=256, progress=progress, sleep=0.01)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.execute("PRAGMA trusted_schema=OFF")
                dst.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("The database snapshot failed its integrity check.")
            finally:
                dst.close()
        finally:
            src.close()
        with open(temporary, "rb") as completed:
            os.fsync(completed.fileno())
        if os.name == "nt":
            os.rename(temporary, destination)  # Windows rename fails if the target exists.
        else:
            os.link(temporary, destination)  # Publish without clobbering a concurrent backup.
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(temporary + suffix).unlink(missing_ok=True)
