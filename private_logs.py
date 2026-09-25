"""Bounded private log files shared safely by multiple server processes."""

import logging
import os
from pathlib import Path
import stat

from filelock import FileLock

MAX_RECORD_BYTES = 64 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
BACKUP_FILES = 5


def _regular(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise OSError("Log paths must be regular files, without links")


def append(path, text, *, max_bytes=MAX_FILE_BYTES, backups=BACKUP_FILES):
    """Append one bounded record; rotate under a process lock without open FDs."""
    path = Path(path).absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    _regular(lock_path)
    encoded = str(text).encode("utf-8", errors="replace")
    if len(encoded) > MAX_RECORD_BYTES:
        encoded = encoded[:MAX_RECORD_BYTES - 32] + b"\n[log record truncated]\n"
    if not encoded.endswith(b"\n"):
        encoded += b"\n"
    with FileLock(lock_path, timeout=0.25, mode=0o600):
        _regular(path)
        if path.exists() and path.stat().st_size + len(encoded) > max_bytes:
            for index in range(backups, 0, -1):
                destination = path.with_name(path.name + "." + str(index))
                previous = path if index == 1 else path.with_name(path.name + "." + str(index - 1))
                _regular(destination)
                _regular(previous)
                if previous.exists():
                    os.replace(previous, destination)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
        with os.fdopen(descriptor, "ab") as output:
            details = os.fstat(output.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise OSError("Invalid log file")
            if os.name != "nt":
                os.fchmod(output.fileno(), 0o600)
            output.write(encoded)


def tail(path, limit=20000):
    """Read a bounded tail without following links or blocking on special files."""
    path = Path(path)
    _regular(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise OSError("Invalid log file")
        start = max(0, details.st_size - limit)
        source.seek(start)
        return ("...\n" if start else "") + source.read(limit).decode("utf-8", errors="replace")


class PrivateFileHandler(logging.Handler):
    """File logging failure never turns a successful application action into 500."""
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)

    def emit(self, record):
        try:
            append(self.path, self.format(record))
        except Exception:
            self.handleError(record)
