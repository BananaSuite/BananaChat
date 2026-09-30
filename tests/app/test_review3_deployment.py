"""Third review of the two-server deployment: the outage fallback and the one-step model restore."""

from __future__ import annotations

import pytest

from tests.app.fake_ollama import FakeOllama
from tests.app.test_deployment_models import check, publish, record, recovery_file


@pytest.fixture
def backup_server():
    server = FakeOllama().start()
    server.models = []
    yield server
    server.stop()


def test_a_download_asks_the_primary_server_whether_the_model_exists(make_app, fake_ollama, backup_server):
    """Cancelling an update of an installed model must not delete it because the fallback lacks it."""
    from bananachat.services import health, pulls
    fake_ollama.api_key = "p" * 64
    app = make_app(OLLAMA_API_KEY="p" * 64, INFERENCE_OUTAGE_MODE="fallback", INFERENCE_FALLBACK_URL=backup_server.url)
    with app.app_context():
        for _ in range(app.config["BC"].inference_health_failures):
            health.record_probe(False, app.config["BC"].inference_health_failures, "down for the test")
        assert pulls._installed("llama3.2:3b", app.config["BC"]) is True
    assert backup_server.authorizations == []


def test_a_list_whose_downloads_failed_offers_them_again(app, admin):
    from bananachat.db import pulls as pulls_db
    recovery_file(app)
    admin.post("/admin/models/recovery", {"action": "download_missing"})
    with app.app_context():
        for job in record(app)["jobs"]:
            pulls_db.finish(job["id"], "failed", "The registry could not be reached.")
    check(app)
    assert record(app)["state"] == "deferred"
    page = admin.get("/admin/").get_data(as_text=True)
    assert "Download them" in page and "0 downloads in progress" not in page


def test_a_running_download_keeps_the_list_requested(app, admin):
    recovery_file(app)
    admin.post("/admin/models/recovery", {"action": "download_missing"})
    check(app)
    assert record(app)["state"] == "requested"


def _delete_from_server(app, admin):
    from bananachat.db import catalog
    with app.app_context():
        model_id = catalog.get_by_name("llama3.2:3b")["id"]
    assert admin.post(f"/admin/models/{model_id}/delete-server").status_code == 302


def test_a_check_between_deleting_and_remembering_the_deletion_offers_nothing(app, admin, fake_ollama, monkeypatch):
    """The background check runs in another process while an administrator deletes a model."""
    from bananachat.services import model_recovery
    publish(app, "llama3.2:3b")
    ignore = model_recovery.ignore

    def check_first(names, config=None):
        check(app)  # the background job fires at this moment
        ignore(names, config)

    monkeypatch.setattr(model_recovery, "ignore", check_first)
    _delete_from_server(app, admin)
    monkeypatch.undo()
    assert "llama3.2:3b" in fake_ollama.deleted
    check(app)
    assert record(app)["state"] not in model_recovery.OPEN_STATES
    assert 'id="model-recovery"' not in admin.get("/admin/").get_data(as_text=True)


def test_a_check_that_listed_the_models_before_a_deletion_does_not_forget_it(app, admin, fake_ollama, monkeypatch):
    from bananachat.services import model_recovery, ollama
    publish(app, "llama3.2:3b")
    installed_names = ollama.installed_names

    def stale(*args, **kwargs):
        snapshot = installed_names(*args, **kwargs)  # still lists the model
        monkeypatch.setattr(ollama, "installed_names", installed_names)
        _delete_from_server(app, admin)  # meanwhile, in another process
        return snapshot

    monkeypatch.setattr(ollama, "installed_names", stale)
    check(app)
    assert "llama3.2:3b" in fake_ollama.deleted
    check(app)
    saved = record(app)
    assert saved["state"] not in model_recovery.OPEN_STATES and saved["ignored"] == ["llama3.2:3b"]


def test_the_overview_opens_while_another_process_holds_the_model_list(app, admin, monkeypatch):
    from filelock import FileLock

    from bananachat.services import model_recovery
    recovery_file(app)
    monkeypatch.setattr(model_recovery, "FileLock", lambda path, timeout, mode: FileLock(path, timeout=0.2, mode=mode))
    held = FileLock(str(app.config["BC"].model_recovery_file) + ".lock")
    with held:
        response = admin.get("/admin/")
    assert response.status_code == 200 and 'id="model-recovery"' in response.get_data(as_text=True)


def test_choosing_downloads_lists_the_models_before_taking_the_lock(app, admin, monkeypatch):
    """A slow compute server must not keep the other process waiting for the list."""
    from bananachat.services import model_recovery, ollama
    recovery_file(app)
    installed_names = ollama.installed_names
    held = []

    def observe(*args, **kwargs):
        lock = model_recovery._lock(app.config["BC"])
        lock.timeout = 0
        try:
            with lock:
                held.append(False)
        except OSError:
            held.append(True)
        return installed_names(*args, **kwargs)

    monkeypatch.setattr(ollama, "installed_names", observe)
    admin.post("/admin/models/recovery", {"action": "download", "items": ["ollama:1"]})
    assert held == [False] and record(app)["state"] == "requested"


@pytest.mark.parametrize("variable", ["BC_OLLAMA_URL", "BC_INFERENCE_FALLBACK_URL"])
def test_a_token_is_never_sent_in_clear_text_to_another_machine(variable):
    from bananachat.config import load_config
    env = {"BC_SECRET_KEY": "x" * 40, "BC_OLLAMA_URL": "https://compute.example.org", "BC_OLLAMA_API_KEY": "p" * 64,
           "BC_INFERENCE_OUTAGE_MODE": "fallback", "BC_INFERENCE_FALLBACK_URL": "https://backup.example.org",
           "BC_INFERENCE_FALLBACK_API_KEY": "f" * 40, variable: "http://10.0.0.5:11435"}
    config = load_config(env, load_secret=False)
    key = config.ollama_api_key if variable == "BC_OLLAMA_URL" else config.inference_fallback_api_key
    other = config.inference_fallback_api_key if variable == "BC_OLLAMA_URL" else config.ollama_api_key
    assert key == "" and other and any("plain HTTP" in warning for warning in config.warnings)
    assert not config.ollama_is_local


def test_a_loopback_tunnel_keeps_its_token():
    from bananachat.config import load_config
    config = load_config({"BC_SECRET_KEY": "x" * 40, "BC_OLLAMA_URL": "http://127.0.0.1:11435",
                          "BC_OLLAMA_API_KEY": "t" * 64}, load_secret=False)
    assert config.ollama_api_key == "t" * 64 and not config.warnings
