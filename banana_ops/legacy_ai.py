"""Import older BananaChat database exports through quiesced server maintenance."""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from sqlite_snapshot import snapshot
from .files import maintenance_lock, read_environment, write_json
from .models import inventory, prepare_recovery


@contextmanager
def stage_database(source, staging):
    """Validate an old raw database or bounded v1 archive without touching live data."""
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Choose a regular database/export file")
    maximum = min(64 * 1024 ** 3, max(0, (shutil.disk_usage(staging).free - 128 * 1024 ** 2) // 3))
    if not 0 < source.stat().st_size <= maximum:
        raise ValueError("The export exceeds the available staging space")
    with tempfile.TemporaryDirectory(prefix="legacy-ai-", dir=staging) as directory:
        database = Path(directory) / "bananachat.db"
        with source.open("rb") as stream:
            raw_database = stream.read(16) == b"SQLite format 3\x00"
        if raw_database:
            snapshot(source, database)
        else:
            with tarfile.open(source, "r|*") as archive:
                found = set()
                for member in archive:
                    if member.name not in {"bananachat.db", "export_meta.json"} or member.name in found or not member.isfile():
                        raise ValueError("Use an unmodified BananaChat v1 export or a raw database")
                    found.add(member.name)
                    limit = maximum if member.name == "bananachat.db" else 1024 * 1024
                    if member.size < 0 or member.size > limit:
                        raise ValueError("The expanded export exceeds its size limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("The export is incomplete")
                    if member.name == "bananachat.db":
                        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(descriptor, "wb") as destination, stream:
                            shutil.copyfileobj(stream, destination, length=1024 * 1024)
                            destination.flush()
                            os.fsync(destination.fileno())
                if "bananachat.db" not in found:
                    raise ValueError("The export has no database")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("The imported database failed integrity checks")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"users", "chat_sessions", "chat_messages", "ai_models"} <= tables or conn.execute("PRAGMA application_id").fetchone()[0] not in {0, 0x42414941}:
                raise ValueError("This is not a BananaChat database")
        yield database


def restore_database(manager, source):
    """Back up existing code/data, stop all processes, import, then health-check."""
    manager.layout()
    with maintenance_lock(manager.root):
        manager.recover()
        settings = manager.settings()
        if manager.product != "BananaChat" or settings["mode"] not in {"single", "web"}:
            raise ValueError("Legacy database imports require an installed BananaChat single or web server")
        environment = read_environment(manager.config_dir / "app.env")
        data = manager.root / "data"
        destination = Path(environment.get("BC_DATABASE_PATH", data / "bananachat.db"))
        if not destination.is_relative_to(data) or destination.is_symlink():
            raise ValueError("Move the database inside the managed data directory before importing")
        with stage_database(source, manager.root / "staging") as staged:
            model_inventory = inventory(staged.parent, {"BC_DATABASE_PATH": str(staged)})
            prepare_recovery(staged.parent, model_inventory, database=staged)
            journal = manager.snapshot_state(settings)
            try:
                manager.quiesce(journal)
                before = manager.package(settings, manager.backup_name("before-legacy-import"))
                journal.update(backup=str(before), phase="backed_up")
                write_json(manager.config_dir / "transaction.json", journal)
                # No old process or sidecar can observe the new schema mid-import.
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(destination) + suffix).unlink(missing_ok=True)
                os.replace(staged, destination)
                prepare_recovery(data, model_inventory, database=destination)
                manager.system.data_permissions(settings)
                manager.finish(journal, settings)
            except BaseException:
                manager.recover()
                raise
        return manager.event("legacy_import", "complete", safety_backup=str(before), model_downloads="require administrator approval")
