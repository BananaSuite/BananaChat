"""Files a person adds to a task, kept until the task's sandbox exists.

Uploads are stored as opaque bytes under ``<instance>/agent-uploads/<task>/``
(mode 0600) and copied into the sandbox by the agent loop; archives (zip,
tar.gz) are extracted by the runner *inside the container*, never on the web
server. Names are reduced to a safe character set. The whole upload is
bounded by ``BC_AGENTS_MAX_UPLOAD_MB`` and :data:`MAX_FILES`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from flask import current_app

MAX_FILES = 20
ARCHIVE_SUFFIXES = {".zip": "zip", ".tar.gz": "tar", ".tgz": "tar"}
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TASK_RE = re.compile(r"^[0-9a-f]{32}$")


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
    if not _TASK_RE.fullmatch(task_id or ""):
        raise ValueError("Invalid task id.")
    return _root(config) / task_id


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
    if not items:
        return 0
    folder = _folder(task_id, config)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def discard(task_id: str, config=None) -> None:
    try:
        folder = _folder(task_id, config)
    except ValueError:
        return
    shutil.rmtree(folder, ignore_errors=True)
