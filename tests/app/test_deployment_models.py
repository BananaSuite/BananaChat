"""Model restore in one step: the web server checks what is missing and asks only about that."""

from __future__ import annotations

import json
import re

import pytest


def recovery_file(app, ollama=("llama3.2:3b", "missing:7b"), manual=(), huggingface=(), **extra):
    record = {"schema": 1, "state": "pending", "inventory": {
        "ollama": list(ollama), "huggingface": list(huggingface), "manual": list(manual),
        "excluded_paths": ["models"]}, **extra}
    path = app.config["BC"].model_recovery_file
    path.write_text(json.dumps(record))
    return path


def record(app):
    return json.loads(app.config["BC"].model_recovery_file.read_text())


def check(app):
    from bananachat.services import housekeeping
    with app.app_context():
        housekeeping.check_model_recovery(app)


def publish(app, *names):
    from bananachat.db import catalog
    from bananachat.services import ollama
    with app.app_context():
        ollama.sync_catalog()
        for name in names:
            catalog.set_rollout(catalog.get_by_name(name)["id"], True)


def test_a_restore_whose_models_are_all_installed_completes_silently(app, admin):
    recovery_file(app, ollama=["llama3.2:3b", "qwen3:4b"],
                  manual=[{"backend": "ollama", "name": "qwen3:4b", "reason": "Custom registry."}])
    check(app)
    assert record(app)["state"] == "complete"
    page = admin.get("/admin/").get_data(as_text=True)
    assert "from the backup" not in page and 'id="model-recovery"' not in page
    assert "Models to restore" not in admin.get("/admin/models").get_data(as_text=True)


def test_missing_models_get_one_card_with_their_size_and_download_them_queues_only_those(app, admin, fake_ollama):
    from bananachat.db import settings as site_settings
    with app.app_context():
        site_settings.state_set("ollama_model_sizes", {"missing:7b": 4 * 1024 ** 3})
    recovery_file(app)
    page = admin.get("/admin/").get_data(as_text=True)
    # The administrator's page uses the interface language's number format.
    assert re.search(r"1 model from the backup is missing in Ollama on this machine \(about 4[.,]0 GB\)", page)
    assert "Download them" in page and "Choose…" in page and "Dismiss" in page
    response = admin.post("/admin/models/recovery", {"action": "download_missing", "return_to": "dashboard"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/")
    saved = record(app)
    assert saved["state"] == "requested" and [job["model"] for job in saved["jobs"]] == ["missing:7b"]
    page = admin.get("/admin/").get_data(as_text=True)
    assert "1 download in progress" in page and "Download them" not in page


def test_the_list_completes_once_the_downloads_finished(app, admin, fake_ollama):
    from bananachat.services import pulls
    recovery_file(app)
    admin.post("/admin/models/recovery", {"action": "download_missing"})
    with app.app_context():
        pulls.process_next(app.config["BC"])
    check(app)
    assert record(app)["state"] == "complete"


def test_the_model_page_lists_only_what_is_missing(app, admin):
    recovery_file(app)
    page = admin.get("/admin/models").get_data(as_text=True)
    assert 'value="ollama:1"' in page and 'value="ollama:0"' not in page
    assert "1 already installed" in page


def test_a_dismissed_list_is_closed_and_not_offered_again(app, admin, fake_ollama):
    recovery_file(app)
    check(app)
    response = admin.post("/admin/models/recovery", {"action": "dismiss", "return_to": "dashboard"})
    assert response.headers["Location"].endswith("/admin/")
    assert record(app)["state"] == "dismissed" and record(app)["ignored"] == ["missing:7b"]
    check(app)
    assert record(app)["state"] == "dismissed"
    assert 'id="model-recovery"' not in admin.get("/admin/").get_data(as_text=True)


def test_manual_items_and_postponed_lists_can_be_dismissed(app, admin):
    recovery_file(app, ollama=[], manual=[{"backend": "comfyui", "name": "custom.safetensors", "reason": "No recipe."}])
    admin.post("/admin/models/recovery", {"action": "defer"})
    page = admin.get("/admin/").get_data(as_text=True)
    assert "1 model from the backup is missing" in page and "Postponed." in page and "manual action" in page
    assert "Download them" not in page
    admin.post("/admin/models/recovery", {"action": "dismiss"})
    assert record(app)["state"] == "dismissed"


def test_published_models_missing_on_a_rebuilt_compute_server_are_offered(app, admin, fake_ollama):
    publish(app, "llama3.2:3b", "qwen3:4b")
    check(app)
    assert not app.config["BC"].model_recovery_file.exists()
    fake_ollama.models = ["qwen3:4b"]  # the compute server was replaced
    check(app)
    saved = record(app)
    assert saved["origin"] == "catalog" and saved["inventory"]["ollama"] == ["llama3.2:3b"]
    page = admin.get("/admin/").get_data(as_text=True)
    assert "1 published model is not installed" in page
    # Dismissed models stay quiet until they are installed and lost again.
    admin.post("/admin/models/recovery", {"action": "dismiss"})
    check(app)
    assert record(app)["state"] == "dismissed"
    fake_ollama.models = ["qwen3:4b", "llama3.2:3b"]
    check(app)
    assert record(app)["ignored"] == []
    fake_ollama.models = ["qwen3:4b"]
    check(app)
    assert record(app)["state"] == "pending"


def test_an_unreachable_model_server_is_reported_and_retried(make_app, fake_ollama):
    from tests.app.conftest import Browser
    fake_ollama.api_key = "t" * 64
    app = make_app(OLLAMA_API_KEY="wrong-token-for-the-test-0123456789")
    recovery_file(app)
    check(app)
    saved = record(app)
    assert saved["state"] == "pending" and "unauthorized" in saved["check_error"]
    browser = Browser(app)
    browser.login("admin")
    page = browser.get("/admin/").get_data(as_text=True)
    assert "2 models from the backup may be missing on the compute server" in page
    assert "could not be checked yet" in page


def test_checks_ignore_the_outage_fallback(make_app, fake_ollama):
    from tests.app.fake_ollama import FakeOllama
    backup = FakeOllama().start()
    try:
        backup.models = []
        fake_ollama.api_key = "p" * 64
        app = make_app(OLLAMA_API_KEY="p" * 64, INFERENCE_OUTAGE_MODE="fallback", INFERENCE_FALLBACK_URL=backup.url)
        from bananachat.services import health, model_recovery
        with app.app_context():
            for _ in range(3):
                health.record_probe(False, 3, "down")
        recovery_file(app, ollama=["llama3.2:3b"])
        with app.app_context():
            model_recovery.check(app.config["BC"])
        assert record(app)["state"] == "complete" and backup.authorizations == []
    finally:
        backup.stop()


@pytest.mark.parametrize("state", ["complete", "dismissed"])
def test_closed_lists_are_not_shown(app, admin, state):
    recovery_file(app, state=state)
    assert 'id="model-recovery"' not in admin.get("/admin/").get_data(as_text=True)
    assert 'id="recovery"' not in admin.get("/admin/models").get_data(as_text=True)


def test_a_model_deleted_from_the_server_on_purpose_is_not_offered_again(app, admin, fake_ollama):
    from bananachat.db import catalog
    publish(app, "llama3.2:3b")
    with app.app_context():
        model_id = catalog.get_by_name("llama3.2:3b")["id"]
    admin.post(f"/admin/models/{model_id}/delete-server")
    assert "llama3.2:3b" in fake_ollama.deleted
    check(app)
    assert record(app)["state"] == "dismissed" and record(app)["ignored"] == ["llama3.2:3b"]
    assert 'id="model-recovery"' not in admin.get("/admin/").get_data(as_text=True)
