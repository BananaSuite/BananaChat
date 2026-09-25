"""Private configuration and bounded portable installation packages."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import tarfile
import tempfile


def absolute_path(value):
    path = Path(value).absolute()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Use an absolute installation path below a dedicated directory, without spaces or '..'.")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Managed installation paths cannot traverse symlinks.")
    return path


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to replace a configuration symlink.")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content.encode() if isinstance(content, str) else content)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Invalid configuration file.")
    return json.loads(path.read_text())


def read_environment(path):
    output = {}
    if not Path(path).exists():
        return output
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Environment files use one NAME=value assignment per line.")
        parsed = shlex.split(value, comments=False)
        output[key] = " ".join(parsed)
    return output


def write_environment(path, values):
    lines = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in str(value) for c in "\r\n\0"):
            raise ValueError("Invalid environment assignment.")
        lines.append(key + "=" + json.dumps(str(value), ensure_ascii=False))
    atomic_write(path, "\n".join(lines) + "\n")


@contextmanager
def maintenance_lock(root):
    import fcntl
    path = Path(root) / "config" / "maintenance.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another maintenance operation is running. Inspect status and retry when it finishes.") from None
        yield
    finally:
        os.close(descriptor)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def regular_files(directory, *, allowed_link=None, excluded=()):
    root = Path(directory)
    if not root.exists():
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        def omitted(name, current=current):
            relative = (Path(current) / name).relative_to(root).as_posix()
            return any(relative == item or relative.startswith(item + "/") for item in excluded)
        dirs[:] = [name for name in dirs if not omitted(name)]
        files = [name for name in files if not omitted(name)]
        for name in (*dirs, *files):
            path = Path(current) / name
            if path.name == ".banana-maintenance":
                continue
            if path.is_symlink() and allowed_link is not None and allowed_link(path):
                continue
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError(f"Portable packages require regular files and directories: {path.name}")
            if path.is_file():
                yield path, path.relative_to(root).as_posix()


def write_package(destination, manifest, inputs):
    destination = Path(destination).absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new backup filename; existing packages are never overwritten.")
    files = list(inputs)
    total = sum(path.stat().st_size for path, _ in files)
    if len(files) > 100000:
        raise ValueError("The installation exceeds the package limit of 100,000 files.")
    if shutil.disk_usage(destination.parent).free < total + 128 * 1024 * 1024:
        raise ValueError("Not enough free space for a complete backup.")
    manifest = {**manifest, "schema": 1, "files": {}}
    fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=3) as archive:
            for path, name in files:
                if name in manifest["files"]:
                    raise ValueError("Duplicate package entry.")
                manifest["files"][name] = {"sha256": digest_file(path), "size": path.stat().st_size}
                archive.add(path, arcname=name, recursive=False)
            raw = json.dumps(manifest, sort_keys=True).encode()
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Package manifest is too large.")
            import io
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(raw), 0o600
            archive.addfile(info, io.BytesIO(raw))
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        destination.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def extract_archive(archive_path, destination, *, max_bytes=1024 ** 4, max_files=100000):
    """Stream extraction rejects links, special files, duplicates, and traversal."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Extract only into an empty staging directory.")
    seen, total = set(), 0
    with tarfile.open(archive_path, "r|*") as archive:
        for member in archive:
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts)
                    or "\\" in member.name or "\0" in member.name or member.name in seen):
                raise ValueError("Unsafe or duplicate archive path.")
            seen.add(member.name)
            if len(seen) > max_files or member.size < 0:
                raise ValueError("Archive exceeds file limits.")
            total += member.size
            if total > max_bytes:
                raise ValueError("Archive exceeds the extracted-size limit.")
            path = destination.joinpath(*relative.parts)
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError("Archive links and special files are not supported.")
            if shutil.disk_usage(destination).free < member.size + 64 * 1024 * 1024:
                raise ValueError("Not enough free space to restore the archive.")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            if path.stat().st_size != member.size:
                raise ValueError("Truncated archive entry.")
            path.chmod(0o700 if member.mode & 0o111 else 0o600)
    return seen


@contextmanager
def read_package(path, product, staging_parent):
    with tempfile.TemporaryDirectory(prefix="restore-", dir=staging_parent) as temporary:
        root = Path(temporary)
        members = extract_archive(path, root)
        manifest = read_json(root / "manifest.json")
        if not manifest or manifest.get("schema") != 1 or manifest.get("product") != product:
            raise ValueError("This package does not match the application or package format.")
        declared = manifest.get("files", {})
        if not isinstance(declared, dict) or set(declared) != {item for item in members if (root / item).is_file()} - {"manifest.json"}:
            raise ValueError("The package does not match its manifest.")
        for name, entry in declared.items():
            file = root / name
            if file.stat().st_size != entry.get("size") or digest_file(file) != entry.get("sha256"):
                raise ValueError(f"Package integrity verification failed for {name!r}.")
        # Even a read-only SQLite connection may rewrite a WAL shared-memory
        # index. Validate copies after checking every hash, keeping the archived
        # payload unchanged for restoration and its manifest still verifiable.
        for name in declared:
            file = root / name
            if file.suffix in {".db", ".sqlite", ".sqlite3"}:
                with tempfile.TemporaryDirectory(prefix="database-check-", dir=staging_parent) as directory:
                    copy = Path(directory) / file.name
                    inputs = [file, *(Path(str(file) + suffix) for suffix in ("-wal", "-shm", "-journal"))]
                    size = sum(item.stat().st_size for item in inputs if item.is_file())
                    if shutil.disk_usage(directory).free < size + 64 * 1024 * 1024:
                        raise ValueError("Not enough free space to validate a packaged database.")
                    for item in inputs:
                        if item.is_file():
                            shutil.copyfile(item, Path(directory) / item.name)
                    try:
                        with sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True) as database:
                            database.execute("PRAGMA trusted_schema=OFF")
                            if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                raise ValueError("A packaged database failed its integrity check.")
                    except sqlite3.Error:
                        raise ValueError(f"A packaged database failed its integrity check: {name!r}.") from None
        yield root, manifest
