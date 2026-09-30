"""Backup guidance and the database export used to move to another server.

The export is a gzip-compressed tar archive holding exactly two files:
``bananachat.db`` (a consistent, integrity-checked SQLite snapshot) and
``export_meta.json`` (``{"format": "bananachat-migration-v1", "exported_at",
"schema_version"}``). ``bananachat restore --legacy-database FILE`` imports it.
The archive is streamed while it is compressed, so memory use stays small
whatever the size of the database; the snapshot lives in a private temporary
directory under the instance directory and is removed afterwards.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import Response, current_app, flash, render_template

from bananachat import db
from bananachat.security import admin_required

from . import bp
from ._helpers import audit, back

log = logging.getLogger("bananachat.admin")

EXPORT_FORMAT = "bananachat-migration-v1"
TEMP_PREFIX = ".export-"
CHUNK = 1024 * 1024


@bp.get("/migration", endpoint="migration")
@admin_required
def show():
    try:
        size = db.path().stat().st_size
    except OSError:
        size = None
    return render_template("admin/migration.html", section="migration", database_size=size,
                           schema_version=db.schema_version(), database_path=str(db.path()))


class _Sink:
    """A write-only buffer the gzip stream writes into; drained after each step."""

    def __init__(self):
        self._buffer = bytearray()

    def write(self, data) -> int:
        self._buffer += data
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


def tar_gz_stream(members: list[tuple[str, object]]):
    """Yield a ``.tar.gz`` of *members* (``(name, Path | bytes)``) chunk by chunk."""
    sink = _Sink()
    written = 0
    with gzip.GzipFile(fileobj=sink, mode="wb", compresslevel=6, mtime=int(time.time())) as archive:
        for name, source in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.mtime = int(time.time())
            info.size = len(source) if isinstance(source, bytes) else Path(source).stat().st_size
            header = info.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8", errors="strict")
            archive.write(header)
            written += len(header)
            if isinstance(source, bytes):
                archive.write(source)
            else:
                copied = 0
                with open(source, "rb") as handle:
                    while chunk := handle.read(CHUNK):
                        copied += len(chunk)
                        if copied > info.size:
                            raise OSError(f"{name} changed while it was being exported.")
                        archive.write(chunk)
                        yield sink.drain()
                if copied != info.size:
                    raise OSError(f"{name} changed while it was being exported.")
            padding = (-info.size) % tarfile.BLOCKSIZE
            archive.write(b"\0" * padding)
            written += info.size + padding
            yield sink.drain()
        end = tarfile.BLOCKSIZE * 2
        written += end
        end += (-written) % tarfile.RECORDSIZE
        archive.write(b"\0" * end)
    yield sink.drain()


def _remove_stale_exports(instance_dir: Path, max_age: float = 6 * 3600) -> None:
    """Remove temporary export directories left behind by a crash."""
    for candidate in instance_dir.glob(TEMP_PREFIX + "*"):
        try:
            if candidate.is_dir() and not candidate.is_symlink() and time.time() - candidate.stat().st_mtime > max_age:
                shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            continue


@bp.post("/migration/export", endpoint="migration_export")
@admin_required
def export():
    import sqlite_snapshot

    config = current_app.config["BC"]
    config.instance_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _remove_stale_exports(config.instance_dir)
    workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=config.instance_dir))
    lock = threading.Lock()

    def cleanup():
        with lock:
            shutil.rmtree(workdir, ignore_errors=True)

    try:
        snapshot = sqlite_snapshot.snapshot(db.path(), workdir / "bananachat.db")
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as connection:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        cleanup()
        log.warning("Database export failed: %s", error)
        flash(f"The export could not be created: {error}", "error")
        return back("admin.migration")
    exported_at = datetime.now(timezone.utc)
    metadata = json.dumps({"format": EXPORT_FORMAT, "exported_at": exported_at.isoformat(timespec="seconds"),
                           "schema_version": schema_version}, indent=2).encode("utf-8")
    audit("migration_export", "database", {"schema_version": schema_version, "bytes": snapshot.stat().st_size})

    def generate():
        try:
            yield from tar_gz_stream([("bananachat.db", snapshot), ("export_meta.json", metadata)])
        finally:
            cleanup()

    response = Response(generate(), mimetype="application/gzip")
    response.headers["Content-Disposition"] = \
        f'attachment; filename="bananachat_export_{exported_at:%Y%m%d_%H%M%S}.tar.gz"'
    response.headers["Cache-Control"] = "private, no-store"
    response.call_on_close(cleanup)
    return response
