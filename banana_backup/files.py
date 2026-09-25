"""Private operator files, exclusive outputs, and per-destination locking."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


def directory(path):
    path = Path(path).absolute()
    if ".." in path.parts or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Backup paths cannot traverse symbolic links or '..'.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise ValueError("The backup working directory must belong to the operator and have mode 0700.")
    return path


def private_bytes(path, limit=16384):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Credential paths cannot traverse symbolic links.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("Credential files must belong to the operator, have mode 0600, and fit the size limit.")
        return handle.read(limit + 1)


def atomic_write(path, content):
    path = Path(path)
    directory(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing to replace a backup configuration symlink.")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode() if isinstance(content, str) else content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    if not Path(path).exists():
        return default
    return json.loads(private_bytes(path, 128 * 1024))


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def publish(temporary, destination):
    """Publish only a completed file; never replace an existing output."""
    destination = Path(destination).absolute()
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("Output paths cannot traverse symbolic links.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The caller stages on this same filesystem. link() provides no-clobber
    # publication, including when another process creates the name first.
    os.link(temporary, destination, follow_symlinks=False)
    sync_dir(destination.parent)
    return destination


@contextmanager
def lock(root):
    directory(root)
    fd = os.open(Path(root) / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another backup operation is running. Retry after it finishes.") from None
        yield
    finally:
        os.close(fd)
