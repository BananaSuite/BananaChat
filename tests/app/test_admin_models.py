"""Administrator model management: catalog, categories, downloads (incl. shutdown safety) and recovery."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def pin_csrf(browser):
    """Signing in starts a new session without a CSRF token and the first page would mint a random one;
    keep the test token so later form posts match."""
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF


def signed_in(app, username, password=None):
    browser = Browser(app)
    browser.login(username, password)
    pin_csrf(browser)
    return browser


@pytest.fixture
def admin(client):
    client.login("admin", "admin-password")
    pin_csrf(client)
    return client


def _one(app, sql, params=()):
    from bananachat import db

    with app.app_context():
        return db.one(sql, params)


def _sync(app):
    from bananachat.services import ollama

    with app.test_request_context():
        ollama.sync_catalog()


def _model(app, name):
    return _one(app, "SELECT * FROM ai_models WHERE ollama_name=?", (name,))


def _run_next(app, **kwargs):
    from bananachat.services import pulls

    with app.app_context():
        return pulls.process_next(app.config["BC"], **kwargs)


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ----- catalog ------------------------------------------------------------------------

def test_sync_now_and_edits_survive_later_syncs(app, admin, fake_ollama):
    response = admin.post("/admin/models/sync", follow_redirects=True)
    assert "Ollama reports 2 models" in response.get_data(as_text=True)
    model = _model(app, "llama3.2:3b")
    assert model["is_rolled_out"] == 0
    admin.post("/admin/models/categories/create", {"name": "Coding", "scope": "chat"})
    category = _one(app, "SELECT id FROM model_categories WHERE name='Coding'")["id"]
    response = admin.post(f"/admin/models/{model['id']}/edit", {
        "display_name": "Company Assistant", "description": "Helpful", "system_prompt": "Be brief.",
        "temperature": "0.7", "top_p": "0.9", "top_k": "40", "num_ctx": "4096", "repeat_penalty": "1.1",
        "supports_vision": "1", "categories": str(category)})
    assert response.status_code == 302
    _sync(app)
    fake_ollama.models.append("new:1b")
    admin.post("/admin/models/sync")
    model = _model(app, "llama3.2:3b")
    assert (model["display_name"], model["description"], model["system_prompt"]) == \
        ("Company Assistant", "Helpful", "Be brief.")
    assert (model["temperature"], model["top_k"], model["num_ctx"], model["supports_vision"]) == (0.7, 40, 4096, 1)
    assert _one(app, "SELECT 1 FROM model_category_assignments WHERE model_id=? AND category_id=?",
                (model["id"], category))
    assert _model(app, "new:1b")["backend_available"] == 1


@pytest.mark.parametrize("field,value,message", [
    ("temperature", "5", "Temperature must be between 0 and 2"),
    ("top_k", "1.5", "Top K must be a whole number"),
    ("num_ctx", "10", "Context length must be between"),
    ("system_prompt", "x" * 4001, "at most 4,000 characters"),
    ("display_name", "", "Display name is required"),
    ("top_p", "nan", "Top P must be between"),
])
def test_model_edit_validates_ranges(app, admin, field, value, message):
    _sync(app)
    model = _model(app, "qwen3:4b")
    response = admin.post(f"/admin/models/{model['id']}/edit", {"display_name": "Qwen", field: value})
    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert _model(app, "qwen3:4b")["display_name"] == model["display_name"]


def test_rollout_order_and_removal(app, admin):
    _sync(app)
    first, second = _model(app, "llama3.2:3b"), _model(app, "qwen3:4b")
    admin.post(f"/admin/models/{first['id']}/rollout", {"enabled": "1"})
    assert _model(app, "llama3.2:3b")["is_rolled_out"] == 1
    admin.post(f"/admin/models/{first['id']}/rollout", {"enabled": "0"})
    assert _model(app, "llama3.2:3b")["is_rolled_out"] == 0

    order = lambda: [row["ollama_name"] for row in _all(app)]  # noqa: E731
    assert order() == ["llama3.2:3b", "qwen3:4b"]
    admin.post(f"/admin/models/{second['id']}/move", {"direction": "up"})
    assert order() == ["qwen3:4b", "llama3.2:3b"]
    admin.post(f"/admin/models/{second['id']}/move", {"direction": "down"})
    assert order() == ["llama3.2:3b", "qwen3:4b"]
    assert admin.post(f"/admin/models/{second['id']}/move", {"direction": "sideways"}).status_code == 400

    admin.post(f"/admin/models/{second['id']}/remove")
    assert _model(app, "qwen3:4b") is None
    assert admin.get("/admin/models/999/edit").status_code == 404


def _all(app):
    from bananachat.db import catalog

    with app.app_context():
        return catalog.list_models()


def test_delete_from_the_ollama_server(app, admin, fake_ollama):
    _sync(app)
    model = _model(app, "qwen3:4b")
    admin.post(f"/admin/models/{model['id']}/delete-server")
    assert fake_ollama.deleted == ["qwen3:4b"]
    assert _model(app, "qwen3:4b")["backend_available"] == 0


def test_running_models_and_unload(app, admin, fake_ollama):
    fake_ollama.running = ["llama3.2:3b"]
    data = admin.fetch("/admin/api/models/running").get_json()
    assert [item["name"] for item in data["models"]] == ["llama3.2:3b"]
    response = admin.post_json("/admin/api/models/unload", {"name": "llama3.2:3b"})
    assert response.get_json()["ok"] is True
    assert ("/api/generate", {"model": "llama3.2:3b", "keep_alive": 0, "stream": False}) in fake_ollama.requests
    assert admin.post_json("/admin/api/models/unload", {"name": "bad name!"}).status_code == 400
    assert admin.post_json("/admin/api/models/unload-all", {}).get_json()["ok"] is True
    status = admin.fetch("/admin/api/inference").get_json()
    assert status["reachable"] is True and status["version"] == "0.9.0"


def test_inference_status_fails_gracefully(make_app):
    app = make_app(OLLAMA_URL="http://127.0.0.1:9")
    browser = signed_in(app, "admin", "admin-password")
    status = browser.fetch("/admin/api/inference").get_json()
    assert status["reachable"] is False and status["error"]
    assert browser.fetch("/admin/api/models/running").status_code == 502
    assert browser.get("/admin/").status_code == 200
    assert browser.get("/admin/models").status_code == 200


# ----- categories ------------------------------------------------------------------------------

def test_category_scope_is_validated(app, admin):
    response = admin.post("/admin/models/categories/create", {"name": "Bad", "scope": "everywhere"},
                          follow_redirects=True)
    assert response.status_code == 200 and "valid where it applies" in response.get_data(as_text=True)
    assert _one(app, "SELECT 1 FROM model_categories WHERE name='Bad'") is None
    admin.post("/admin/models/categories/create", {"name": "Writing", "scope": "both"})
    admin.post("/admin/models/categories/create", {"name": "Staff", "scope": "api"})
    writing = _one(app, "SELECT * FROM model_categories WHERE name='Writing'")
    response = admin.post(f"/admin/models/categories/{writing['id']}/edit", {"name": "Writing", "scope": "nope"})
    assert response.status_code == 400
    admin.post(f"/admin/models/categories/{writing['id']}/edit", {"name": "Prose", "scope": "chat",
                                                                 "description": "Stories"})
    row = _one(app, "SELECT * FROM model_categories WHERE id=?", (writing["id"],))
    assert (row["name"], row["scope"], row["description"]) == ("Prose", "chat", "Stories")
    staff = _one(app, "SELECT id FROM model_categories WHERE name='Staff'")["id"]
    admin.post(f"/admin/models/categories/{staff}/move", {"direction": "top"})
    assert _one(app, "SELECT name FROM model_categories ORDER BY sort_order LIMIT 1")["name"] == "Staff"
    admin.post(f"/admin/models/categories/{staff}/delete")
    assert _one(app, "SELECT 1 FROM model_categories WHERE id=?", (staff,)) is None


# ----- downloads -------------------------------------------------------------------------------

def test_download_lifecycle(app, admin, fake_ollama):
    response = admin.post("/admin/models/downloads", {"name": "evil.example.com/model:1"}, follow_redirects=True)
    assert "Only the Ollama library and Hugging Face" in response.get_data(as_text=True)
    for bad in ("../etc", "a b", "hf.co/onlyowner", ""):
        admin.post("/admin/models/downloads", {"name": bad})
    assert _one(app, "SELECT COUNT(*) AS n FROM model_pull_jobs")["n"] == 0

    admin.post("/admin/models/downloads", {"name": "tiny:1b"})
    response = admin.post("/admin/models/downloads", {"name": "tiny:1b"}, follow_redirects=True)
    assert "already queued" in response.get_data(as_text=True)
    job = _one(app, "SELECT * FROM model_pull_jobs WHERE ollama_name='tiny:1b'")
    assert job["status"] == "queued" and job["idempotency_key"]
    listing = admin.fetch("/admin/api/models/downloads").get_json()
    assert listing["jobs"][0]["status"] == "queued" and "disk" in listing

    assert _run_next(app) is True
    job = _one(app, "SELECT * FROM model_pull_jobs WHERE id=?", (job["id"],))
    assert job["status"] == "done" and job["progress_pct"] == 100 and job["finished_at"]
    assert "tiny:1b" in fake_ollama.models and _model(app, "tiny:1b")["backend_available"] == 1
    assert _run_next(app) is False

    admin.post("/admin/models/downloads", {"name": "https://huggingface.co/owner/repo:Q4_K_M"})
    assert _one(app, "SELECT 1 FROM model_pull_jobs WHERE ollama_name='hf.co/owner/repo:Q4_K_M'")
    admin.post("/admin/models/downloads/clear")
    assert _one(app, "SELECT COUNT(*) AS n FROM model_pull_jobs")["n"] == 1  # the queued job stays


def test_failed_download_can_be_retried(app, admin, fake_ollama, monkeypatch):
    from bananachat.services import ollama
    from bananachat.services.upstream import UpstreamError

    def broken(*_args, **_kwargs):
        raise UpstreamError("pull model manifest: file does not exist")
        yield  # pragma: no cover

    admin.post("/admin/models/downloads", {"name": "ghost:1b"})
    monkeypatch.setattr(ollama, "pull", broken)
    _run_next(app)
    job = _one(app, "SELECT * FROM model_pull_jobs WHERE ollama_name='ghost:1b'")
    assert job["status"] == "failed" and "does not exist" in job["error_message"]
    assert "does not exist" in admin.get("/admin/models/downloads").get_data(as_text=True)
    admin.post(f"/admin/models/downloads/{job['id']}/retry")
    assert _one(app, "SELECT COUNT(*) AS n FROM model_pull_jobs WHERE status='queued'")["n"] == 1
    admin.post(f"/admin/models/downloads/{job['id']}/delete")
    assert _one(app, "SELECT 1 FROM model_pull_jobs WHERE id=?", (job["id"],)) is None


def _start_slow_download(app, admin, fake_ollama, name, **kwargs):
    fake_ollama.pull_steps = 40
    fake_ollama.chunk_delay = 0.1
    admin.post("/admin/models/downloads", {"name": name})
    job_id = _one(app, "SELECT id FROM model_pull_jobs WHERE ollama_name=?", (name,))["id"]
    worker = threading.Thread(target=_run_next, args=(app,), kwargs=kwargs, daemon=True)
    worker.start()
    assert _wait_for(lambda: (_one(app, "SELECT progress_pct FROM model_pull_jobs WHERE id=?", (job_id,))
                              ["progress_pct"] or 0) > 0)
    return job_id, worker


def test_shutdown_requeues_a_download_and_never_deletes_the_model(app, admin, fake_ollama):
    from bananachat.db import pulls as pulls_db
    from bananachat.services import pulls

    _sync(app)
    job_id, worker = _start_slow_download(app, admin, fake_ollama, "llama3.2:3b")
    assert pulls.interrupt_current() is True  # what a process shutdown does
    worker.join(10)
    assert not worker.is_alive()
    job = _one(app, "SELECT * FROM model_pull_jobs WHERE id=?", (job_id,))
    assert job["status"] == "queued" and job["error_message"] is None and "resume" in job["progress_detail"]
    assert fake_ollama.deleted == [] and "llama3.2:3b" in fake_ollama.models
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)
    assert _model(app, "llama3.2:3b")["backend_available"] == 1

    # A process that died mid-download leaves the job "pulling"; the next leader resumes it.
    with app.app_context():
        from bananachat import db
        db.execute("UPDATE model_pull_jobs SET status='pulling' WHERE id=?", (job_id,))
        assert pulls_db.reset_stuck() == 1
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "queued"
    fake_ollama.chunk_delay = 0
    _run_next(app)
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "done"


def test_background_stop_is_treated_as_a_shutdown(app, admin, fake_ollama):
    stopping = threading.Event()
    job_id, worker = _start_slow_download(app, admin, fake_ollama, "brand-new:1b", stopping=stopping.is_set)
    stopping.set()
    worker.join(10)
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "queued"
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)


def test_cancel_cleans_up_only_models_that_were_not_installed(app, admin, fake_ollama):
    job_id, worker = _start_slow_download(app, admin, fake_ollama, "brand-new:1b")
    admin.post(f"/admin/models/downloads/{job_id}/cancel")
    worker.join(10)
    job = _one(app, "SELECT * FROM model_pull_jobs WHERE id=?", (job_id,))
    assert job["status"] == "cancelled"
    assert ("/api/delete", {"model": "brand-new:1b", "name": "brand-new:1b"}) in fake_ollama.requests

    fake_ollama.requests.clear()
    job_id, worker = _start_slow_download(app, admin, fake_ollama, "qwen3:4b")
    admin.post(f"/admin/models/downloads/{job_id}/cancel")
    worker.join(10)
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "cancelled"
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)
    assert "qwen3:4b" in fake_ollama.models


def test_cancel_from_another_process_is_noticed(app, admin, fake_ollama):
    from bananachat.db import pulls as pulls_db

    job_id, worker = _start_slow_download(app, admin, fake_ollama, "other:1b")
    with app.app_context():
        pulls_db.cancel(job_id)  # as if another worker process handled the request
    worker.join(10)
    assert not worker.is_alive()
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "cancelled"


def test_disk_space_guard(make_app, tmp_path):
    from bananachat.services import pulls

    app = make_app(OLLAMA_URL="http://127.0.0.1:11434", OLLAMA_MODEL_DIR=str(tmp_path), MIN_FREE_DISK_GB="100000")
    with app.app_context():
        space = pulls.disk_space()
        assert space["checked"] and not space["ok"]
        with pytest.raises(ValueError, match="free"):
            pulls.enqueue_ollama("tiny:1b", None)
    remote = make_app(OLLAMA_URL="http://10.0.0.5:11434", MIN_FREE_DISK_GB="100000")
    with remote.app_context():
        assert pulls.disk_space()["checked"] is False
    missing = make_app(OLLAMA_URL="http://127.0.0.1:11434", OLLAMA_MODEL_DIR=str(tmp_path / "missing"))
    with missing.app_context():
        assert pulls.disk_space()["checked"] is False


def test_checkpoint_downloads_need_the_agent(app, admin):
    response = admin.post("/admin/models/downloads/checkpoint", {
        "repo_id": "owner/repo", "source_filename": "model.safetensors", "expected_sha256": "a" * 64},
        follow_redirects=True)
    assert "Image generation is disabled" in response.get_data(as_text=True)
    assert _one(app, "SELECT COUNT(*) AS n FROM model_pull_jobs")["n"] == 0


def test_checkpoint_recipe_validation():
    from bananachat.services.pulls import validate_checkpoint

    recipe = validate_checkpoint("owner/repo", "unet/model.safetensors", "", "", "A" * 64, "123")
    assert recipe == {"repo_id": "owner/repo", "source_filename": "unet/model.safetensors", "revision": "main",
                      "target_name": "model.safetensors", "expected_sha256": "a" * 64, "expected_size": 123}
    for args in (("owner", "m.safetensors", "main", "", "a" * 64, None),
                 ("owner/repo", "../m.safetensors", "main", "", "a" * 64, None),
                 ("owner/repo", "m.bin", "main", "", "a" * 64, None),
                 ("owner/repo", "m.safetensors", "main", "", "xyz", None),
                 ("owner/repo", "m.safetensors", "../x", "", "a" * 64, None),
                 ("owner/repo", "m.safetensors", "main", "", "a" * 64, "0")):
        with pytest.raises(ValueError):
            validate_checkpoint(*args)


def test_checkpoint_agent_configuration(tmp_path):
    from bananachat.config import Config
    from bananachat.services import checkpoint_agent

    assert checkpoint_agent.configuration_error(Config()) == "The checkpoint download agent is not configured."
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    token.chmod(0o644)
    config = Config(checkpoint_agent_url="https://gpu.example.org", checkpoint_agent_token_file=str(token))
    assert "0600" in checkpoint_agent.configuration_error(config)
    token.chmod(0o600)
    assert checkpoint_agent.configuration_error(config) is None
    assert checkpoint_agent.client(config).base_url == "https://gpu.example.org"
    insecure = config.replace(checkpoint_agent_url="http://gpu.example.org")
    assert "HTTPS" in checkpoint_agent.configuration_error(insecure)
    assert checkpoint_agent.configuration_error(config.replace(checkpoint_agent_url="http://127.0.0.1:8190")) is None
    tailscale = config.replace(checkpoint_agent_url="http://100.100.1.2:8190",
                               checkpoint_agent_allow_insecure_tailscale=True)
    assert checkpoint_agent.configuration_error(tailscale) is None
    assert checkpoint_agent.sanitize("fail https://x.y/?t=1 Bearer abc\n") == "fail [URL] Bearer [redacted]"


# ----- recovery after a restore ------------------------------------------------------------------

RECIPE = {"repo_id": "owner/repo", "source_filename": "sd.safetensors", "revision": "main",
          "target_name": "sd.safetensors", "expected_sha256": "b" * 64, "expected_size": 10}


def _write_recovery(app, **extra):
    record = {"schema": 1, "state": "pending", "inventory": {
        "ollama": ["llama3.2:3b", "missing:7b"], "huggingface": [RECIPE],
        "manual": [{"backend": "comfyui", "name": "custom.safetensors", "reason": "No recipe."}],
        "excluded_paths": ["models"]}, **extra}
    path = app.config["BC"].model_recovery_file
    path.write_text(json.dumps(record))
    return path


def test_recovery_offer_download_and_defer(app, admin, fake_ollama):
    path = _write_recovery(app)
    html = admin.get("/admin/models").get_data(as_text=True)
    assert "Download selected models" in html and "Not now" in html and "custom.safetensors" in html
    assert "3 models from the backup are missing" in admin.get("/admin/").get_data(as_text=True)

    response = admin.post("/admin/models/recovery", {"action": "download", "items": ["ollama:9"]},
                          follow_redirects=True)
    assert "Select between 1 and" in response.get_data(as_text=True)
    response = admin.post("/admin/models/recovery", {"action": "download", "items": ["ollama:0", "ollama:1"]},
                          follow_redirects=True)
    assert "Already installed: llama3.2:3b" in response.get_data(as_text=True)
    record = json.loads(path.read_text())
    assert record["state"] == "requested" and record["inventory"]["excluded_paths"] == ["models"]
    assert [job["model"] for job in record["jobs"]] == ["missing:7b"]
    job_id = record["jobs"][0]["id"]
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "queued"

    response = admin.post("/admin/models/recovery", {"action": "download", "items": ["hf:0"]}, follow_redirects=True)
    assert "checkpoint download agent" in response.get_data(as_text=True)

    admin.post("/admin/models/recovery", {"action": "defer"})
    record = json.loads(path.read_text())
    assert record["state"] == "deferred"
    assert _one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "cancelled"
    assert path.stat().st_mode & 0o777 == 0o600


def test_recovery_completes_when_downloads_finish(app, admin, fake_ollama):
    path = _write_recovery(app)
    record = json.loads(path.read_text())
    record["inventory"]["manual"] = record["inventory"]["huggingface"] = []
    path.write_text(json.dumps(record))
    admin.post("/admin/models/recovery", {"action": "download", "items": ["ollama:1"]})
    _run_next(app)
    admin.get("/admin/models")
    assert json.loads(path.read_text())["state"] == "complete"
    assert "Models to restore" not in admin.get("/admin/models").get_data(as_text=True)


def test_invalid_recovery_file_is_reported(app, admin):
    app.config["BC"].model_recovery_file.write_text("{not json")
    html = admin.get("/admin/models").get_data(as_text=True)
    assert "not valid JSON" in html
    assert admin.get("/admin/").status_code == 200
