"""`bananachat restore --legacy-database`: importing an old database export."""

from contextlib import closing
import io
from pathlib import Path
import sqlite3
import tarfile

import pytest

from test_managed_lifecycle import checkout, installed  # noqa: F401
from banana_ops.files import read_json
from banana_ops.legacy_import import restore_database, stage_database

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_v4.sql"


def _database(path, *, username="alice", busy=False):
    """A database written by the previous release, optionally mid-generation."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(FIXTURE.read_text())
        conn.execute("UPDATE users SET username=? WHERE username='alice'", (username,))
        if busy:
            session = conn.execute("SELECT id FROM chat_sessions WHERE deleted_at IS NULL AND is_incognito=0").fetchone()[0]
            conn.execute("INSERT INTO active_streams (session_id, owner_token, heartbeat_at, partial_content, started_at) "
                         "VALUES (?, 'owner', strftime('%s','now'), 'Retained partial', strftime('%s','now'))", (session,))
            conn.execute("INSERT INTO model_pull_jobs (ollama_name, status, idempotency_key) "
                         "VALUES ('restored:latest', 'queued', 'k1')")
        conn.commit()
    return path


def _installed_chat(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout, product="BananaChat", mode="web")
    destination = _database(manager.root / "data/bananachat.db", username="current-user")
    return manager, services, destination


def _username(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT username FROM users WHERE username IN ('current-user','alice')").fetchone()[0]


def test_legacy_restore_quiesces_and_requires_new_model_download_approval(checkout, tmp_path, monkeypatch):
    manager, services, destination = _installed_chat(tmp_path, checkout)
    source = _database(tmp_path / "legacy.db", busy=True)
    observed = []
    original_package = manager.package

    def package(*args, **kwargs):
        assert not services.running
        assert (manager.root / "data/.banana-maintenance").exists()
        observed.append("quiesced")
        return original_package(*args, **kwargs)

    monkeypatch.setattr(manager, "package", package)
    result = restore_database(manager, source)
    assert observed == ["quiesced"] and result["outcome"] == "complete"
    assert Path(result["safety_backup"]).is_file()
    assert _username(destination) == "alice"
    assert not (manager.config_dir / "transaction.json").exists()
    assert read_json(manager.root / "data/.model-recovery.json")["state"] == "pending"
    with closing(sqlite3.connect(destination)) as conn:
        assert conn.execute("SELECT status FROM model_pull_jobs WHERE ollama_name='restored:latest'").fetchone()[0] \
            == "cancelled"
        heartbeat, partial = conn.execute("SELECT heartbeat_at, partial_content FROM active_streams").fetchone()
        assert heartbeat == 0 and partial == "Retained partial"


def test_failed_legacy_startup_restores_previous_database_and_files(checkout, tmp_path):
    manager, services, destination = _installed_chat(tmp_path, checkout)
    key = manager.root / "data/private.key"
    key.write_text("Keep operator credentials")
    checks = []

    def check(_settings):
        checks.append(_username(destination))
        if len(checks) == 1:
            raise RuntimeError("Simulated restored startup failure")

    services.on_health = check
    with pytest.raises(RuntimeError, match="Simulated"):
        restore_database(manager, _database(tmp_path / "legacy.db"))
    assert checks == ["alice", "current-user"]
    assert _username(destination) == "current-user" and key.read_text() == "Keep operator credentials"
    assert not (manager.config_dir / "transaction.json").exists()


def test_invalid_legacy_archive_leaves_running_data_untouched(checkout, tmp_path):
    manager, services, destination = _installed_chat(tmp_path, checkout)
    archive = tmp_path / "invalid.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../../bananachat.db")
        member.size = 3
        output.addfile(member, io.BytesIO(b"bad"))
    running = services.running.copy()
    with pytest.raises(ValueError, match="unmodified"):
        restore_database(manager, archive)
    assert services.running == running and _username(destination) == "current-user"
    assert not list((manager.root / "backups").glob("*.tar.gz"))


def test_a_raw_database_and_an_export_archive_are_both_accepted(tmp_path):
    raw = _database(tmp_path / "raw.db")
    with stage_database(raw, tmp_path) as staged:
        assert _username(staged) == "alice"
    archive = tmp_path / "export.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(raw, arcname="bananachat.db")
        meta = b'{"format": "bananachat-migration-v1"}'
        info = tarfile.TarInfo("export_meta.json")
        info.size = len(meta)
        output.addfile(info, io.BytesIO(meta))
    with stage_database(archive, tmp_path) as staged:
        assert _username(staged) == "alice"
