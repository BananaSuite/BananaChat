"""Files a person adds to a task, kept until they are copied into the task's sandbox.

Uploads are stored as opaque bytes under ``<instance>/agent-uploads/<task>/``
(mode 0600) and copied into the sandbox by the agent loop; archives (zip,
tar.gz) are extracted by the runner *inside the container*, never on the web
server. Names are reduced to a safe character set. One start's upload is
bounded by ``BC_AGENTS_MAX_UPLOAD_MB`` and :data:`MAX_FILES`; what one account
has stored at once by ``BC_AGENTS_MAX_STORED_UPLOAD_MB`` (the folder's
:data:`OWNER_FILE` names the account, see :func:`owned_bytes`). A start writes
its files to ``incoming-<task>/`` first (:func:`stage`) and moves them into
place once its task row exists (:func:`commit`), so the account's limit counts
them while the start is decided. Files are deleted once copied; files a run
could not copy are kept for the task's next run for :data:`KEEP_UNCOPIED`
(the retention job, ``service.purge_stored_uploads``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app

MAX_FILES = 20
ARCHIVE_SUFFIXES = {".zip": "zip", ".tar.gz": "tar", ".tgz": "tar"}
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
OWNER_FILE = "owner"
STAGING_PREFIX = "incoming-"
KEEP_UNCOPIED = timedelta(days=1)       # after the run that could not copy them ended
STAGING_GRACE = timedelta(minutes=10)   # a start's staged files older than this were left by a crash


class UploadError(ValueError):
    """``key`` is an i18n key, ``params`` its values."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params


def _root(config=None) -> Path:
    config = config or current_app.config["BC"]
    return config.instance_dir / "agent-uploads"


def _folder(task_id: str, config=None) -> Path:
    if not TASK_ID_RE.fullmatch(task_id or ""):
        raise ValueError("Invalid task id.")
    return _root(config) / task_id


def staging_folder(task_id: str, config=None) -> Path:
    if not TASK_ID_RE.fullmatch(task_id or ""):
        raise ValueError("Invalid task id.")
    return _root(config) / f"{STAGING_PREFIX}{task_id}"


def safe_name(name: str) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_RE.sub("_", base).strip("._") or "file"
    return base[:100]


def archive_kind(name: str) -> str | None:
    lowered = name.lower()
    for suffix, kind in ARCHIVE_SUFFIXES.items():
        if lowered.endswith(suffix):
            return kind
    return None


def read_uploads(files, max_bytes: int) -> list[dict]:
    """Read the request's files within the limits. Raises :class:`UploadError`."""
    items, total, names = [], 0, set()
    present = [storage for storage in files if storage and storage.filename]
    if len(present) > MAX_FILES:
        raise UploadError("agents.upload_too_many", max=MAX_FILES)
    for storage in present:
        data = storage.stream.read(max_bytes - total + 1)
        total += len(data)
        if total > max_bytes:
            raise UploadError("agents.upload_too_large", size=max_bytes // (1024 * 1024))
        name = safe_name(storage.filename)
        stem, counter = name, 2
        while name in names:
            name = f"{counter}-{stem}"[:100]
            counter += 1
        names.add(name)
        items.append({"name": name, "kind": archive_kind(name) or "file", "data": data})
    return items


def store(task_id: str, items: list[dict], config=None) -> int:
    return _write(_folder(task_id, config), items)


def stage(task_id: str, items: list[dict], owner: str, config=None) -> int:
    """Write *items* of a task being started, recorded for account *owner*; then :func:`commit` or
    :func:`discard`."""
    return _write(staging_folder(task_id, config), items, owner=owner)


def commit(task_id: str, config=None) -> None:
    """Move the files :func:`stage` wrote into place (their task row exists now)."""
    staging = staging_folder(task_id, config)
    if staging.is_dir():
        staging.rename(_folder(task_id, config))


def _write(folder: Path, items: list[dict], *, owner: str | None = None) -> int:
    if not items:
        return 0
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if owner is not None:
        (folder / OWNER_FILE).write_text(str(owner), encoding="utf-8")
    manifest = []
    for index, item in enumerate(items):
        path = folder / f"{index:02d}.bin"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(item["data"])
        manifest.append({"file": path.name, "name": item["name"], "kind": item["kind"]})
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return len(manifest)


def pending(task_id: str, config=None) -> list[dict]:
    folder = _folder(task_id, config)
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    result = []
    for item in manifest if isinstance(manifest, list) else []:
        if not isinstance(item, dict) or not re.fullmatch(r"\d{2}\.bin", str(item.get("file"))):
            continue
        result.append({"path": folder / item["file"], "name": safe_name(str(item.get("name"))),
                       "kind": item.get("kind") if item.get("kind") in ("zip", "tar", "file") else "file"})
    return result


def retain(task_id: str, items: list[dict], config=None) -> None:
    """Keep only *items* (from :func:`pending`) for the next run; discard everything when there are none."""
    if not items:
        discard(task_id, config)
        return
    folder = _folder(task_id, config)
    keep = {item["path"].name for item in items}
    manifest = [{"file": item["path"].name, "name": item["name"], "kind": item["kind"]} for item in items]
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for path in folder.glob("*.bin"):
        if path.name not in keep:
            path.unlink(missing_ok=True)


def stored_tasks(config=None) -> list[str]:
    """The task ids that have a folder here."""
    try:
        return [entry.name for entry in _root(config).iterdir() if TASK_ID_RE.fullmatch(entry.name)]
    except OSError:
        return []


def _folder_bytes(folder: Path) -> int:
    total = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        total += info.st_size
    return total


def staged_tasks(config=None) -> list[str]:
    """The task ids that have files staged by a start (:func:`stage`)."""
    try:
        names = [entry.name for entry in _root(config).iterdir() if entry.name.startswith(STAGING_PREFIX)]
    except OSError:
        return []
    return [name[len(STAGING_PREFIX):] for name in names if TASK_ID_RE.fullmatch(name[len(STAGING_PREFIX):])]


def owned_bytes(owner: str, config=None) -> int:
    """What the files stored for account *owner* take on disk now, those of starts being decided included."""
    total = 0
    folders = [_folder(task_id, config) for task_id in stored_tasks(config)] + \
        [staging_folder(task_id, config) for task_id in staged_tasks(config)]
    for folder in folders:
        try:
            if (folder / OWNER_FILE).read_text(encoding="utf-8") != str(owner):
                continue
        except OSError:
            continue
        total += _folder_bytes(folder)
    return total


def staged_age(task_id: str, config=None) -> timedelta | None:
    """How long ago a start staged files for *task_id* (None without any)."""
    try:
        changed = staging_folder(task_id, config).stat().st_mtime
    except (OSError, ValueError):
        return None
    return timedelta(seconds=max(0.0, time.time() - changed))


def discard(task_id: str, config=None) -> None:
    try:
        folders = (_folder(task_id, config), staging_folder(task_id, config))
    except ValueError:
        return
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
