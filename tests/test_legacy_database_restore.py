"""Legacy exports remain usable through maintenance with automatic rollback."""

from contextlib import closing
import io
from pathlib import Path
import sqlite3
import tarfile

import pytest

from test_chat_runtime import runtime_app, _client  # noqa: F401
from test_managed_lifecycle import checkout, installed  # noqa: F401
from banana_ops.legacy_ai import restore_database, stage_database
from banana_ops.files import read_json
import config
import db
from db import _chat_runs as runs
from sqlite_snapshot import snapshot


def _installed_ai(tmp_path, checkout, runtime_app):
    manager, services = installed(tmp_path, checkout, product="BananaChat", mode="web")
    destination = manager.root / "data/bananachat.db"
    snapshot(config.DATABASE_PATH, destination)
    with closing(sqlite3.connect(destination)) as conn:
        conn.execute("UPDATE users SET username='current-user' WHERE username='runtime-user'")
        conn.commit()
    return manager, services, destination


def _username(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT username FROM users WHERE username IN ('current-user','runtime-user')").fetchone()[0]


def test_legacy_restore_quiesces_and_requires_new_model_download_approval(runtime_app, checkout, tmp_path, monkeypatch):
    manager, services, destination = _installed_ai(tmp_path, checkout, runtime_app)
    _, user, session, model, _ = runtime_app
    db.enqueue_pull_job("restored:latest", user["id"])
    token = runs.begin_chat_run(session, user, "Before migration")
    runs.checkpoint_chat_run(session, token, "Retained partial", model["id"])
    source = snapshot(config.DATABASE_PATH, tmp_path / "legacy.db")
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
    assert _username(destination) == "runtime-user"
    assert not (manager.config_dir / "transaction.json").exists()
    assert read_json(manager.root / "data/.model-recovery.json")["state"] == "pending"
    monkeypatch.setattr(config, "DATABASE_PATH", str(destination))
    assert db.claim_next_pull_job() is None
    recovered = runs.get_chat_run_status(session)
    assert recovered["state"] == "interrupted" and recovered["last_message"]["content"] == "Retained partial"


def test_failed_legacy_startup_restores_previous_database_and_files(runtime_app, checkout, tmp_path):
    manager, services, destination = _installed_ai(tmp_path, checkout, runtime_app)
    key = manager.root / "data/private.key"
    key.write_text("Keep operator credentials")
    checks = []
    def check(_settings):
        checks.append(_username(destination))
        if len(checks) == 1:
            raise RuntimeError("Simulated restored startup failure")
    services.on_health = check
    with pytest.raises(RuntimeError, match="Simulated"):
        restore_database(manager, config.DATABASE_PATH)
    assert checks == ["runtime-user", "current-user"]
    assert _username(destination) == "current-user" and key.read_text() == "Keep operator credentials"
    assert not (manager.config_dir / "transaction.json").exists()


def test_invalid_legacy_archive_leaves_running_data_untouched(runtime_app, checkout, tmp_path):
    manager, services, destination = _installed_ai(tmp_path, checkout, runtime_app)
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


def test_web_export_roundtrips_and_old_web_import_does_not_replace_live_data(runtime_app, tmp_path):
    application, user, *_ = runtime_app
    db.set_user_role(user["id"], "admin")
    client = _client(application, user)
    response = client.get("/admin/migration/export")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    export = tmp_path / "export.tar.gz"
    export.write_bytes(response.data)
    response.close()
    with stage_database(export, tmp_path) as staged:
        assert _username(staged) == "runtime-user"
    result = client.post("/admin/migration/import", data={"backup_file": (io.BytesIO(b"invalid"), "old.db")}, follow_redirects=True)
    assert result.status_code == 200 and b"restore --legacy-database" in result.data
    assert db.get_user_by_id(user["id"])["username"] == "runtime-user"
