"""Which machine runs the models: locality, the outage fallback and the tokens each server receives."""

from __future__ import annotations

import pytest

from bananachat.config import load_config
from tests.app.fake_ollama import FakeOllama


def config(**env):
    return load_config({"BC_SECRET_KEY": "x" * 40, **env}, load_secret=False)


@pytest.mark.parametrize("env,local", [
    ({}, True),
    ({"BC_OLLAMA_URL": "http://localhost:11434"}, True),
    # The documented SSH tunnel: a loopback port with the compute token.
    ({"BC_OLLAMA_URL": "http://127.0.0.1:11435", "BC_OLLAMA_API_KEY": "t" * 64}, False),
    ({"BC_OLLAMA_URL": "https://compute.example.org"}, False),
    ({"BC_OLLAMA_URL": "http://10.0.0.5:11434"}, False),
    ({"BC_OLLAMA_URL": "http://127.0.0.1:11435", "BC_OLLAMA_API_KEY": "t" * 64, "BC_INFERENCE_LOCAL": "1"}, True),
    ({"BC_OLLAMA_URL": "http://127.0.0.1:11434", "BC_INFERENCE_LOCAL": "0"}, False),
    ({"BC_OLLAMA_URL": "https://compute.example.org", "BC_INFERENCE_LOCAL": "yes"}, True),
])
def test_locality_is_decided_by_the_token_the_host_and_the_override(env, local):
    assert config(**env).ollama_is_local is local


def test_an_invalid_locality_override_is_reported_and_ignored():
    result = config(BC_OLLAMA_API_KEY="t" * 64, BC_INFERENCE_LOCAL="maybe")
    assert result.ollama_is_local is False
    assert any("BC_INFERENCE_LOCAL" in warning for warning in result.warnings)


def test_fallback_needs_an_explicit_url():
    result = config(BC_OLLAMA_URL="https://compute.example.org", BC_INFERENCE_OUTAGE_MODE="fallback")
    assert result.inference_outage_mode == "shutdown" and not result.fallback_enabled
    assert result.inference_fallback_url == ""
    assert any("BC_INFERENCE_FALLBACK_URL" in warning for warning in result.warnings)


@pytest.mark.parametrize("url", ["ftp://backup.example.org", "https://user:secret@backup.example.org",
                                 "https://backup.example.org/?token=1", "https://compute.example.org"])
def test_an_unusable_fallback_url_disables_fallback(url):
    result = config(BC_OLLAMA_URL="https://compute.example.org", BC_INFERENCE_OUTAGE_MODE="fallback",
                    BC_INFERENCE_FALLBACK_URL=url, BC_INFERENCE_FALLBACK_API_KEY="k" * 40)
    assert result.inference_outage_mode == "shutdown" and not result.fallback_enabled
    assert result.inference_fallback_api_key == ""
    assert result.warnings


def test_an_explicit_fallback_is_kept_with_its_own_key():
    result = config(BC_OLLAMA_URL="https://compute.example.org", BC_OLLAMA_API_KEY="p" * 64,
                    BC_INFERENCE_OUTAGE_MODE="fallback", BC_INFERENCE_FALLBACK_URL="https://backup.example.org/",
                    BC_INFERENCE_FALLBACK_API_KEY="f" * 40)
    assert result.fallback_enabled and result.inference_fallback_url == "https://backup.example.org"
    assert result.inference_fallback_api_key == "f" * 40 and not result.warnings


def test_the_model_folder_follows_the_managed_ollama_setting(tmp_path):
    assert config(OLLAMA_MODELS=str(tmp_path)).ollama_model_dir == str(tmp_path)
    assert config(OLLAMA_MODELS="/elsewhere", BC_OLLAMA_MODEL_DIR=str(tmp_path)).ollama_model_dir == str(tmp_path)


@pytest.fixture
def backup_server():
    server = FakeOllama().start()
    server.api_key = "f" * 40
    server.models = ["backup-model:1b"]
    yield server
    server.stop()


def outage(app):
    from bananachat.services import health
    with app.app_context():
        for _ in range(app.config["BC"].inference_health_failures):
            health.record_probe(False, app.config["BC"].inference_health_failures, "down for the test")


def test_the_fallback_never_receives_the_compute_token(make_app, fake_ollama, backup_server):
    from bananachat.services import ollama
    fake_ollama.api_key = "p" * 64
    app = make_app(OLLAMA_API_KEY="p" * 64, INFERENCE_OUTAGE_MODE="fallback",
                   INFERENCE_FALLBACK_URL=backup_server.url, INFERENCE_FALLBACK_API_KEY="f" * 40)
    outage(app)
    with app.app_context():
        assert ollama.installed_names() == {"backup-model:1b"}
        assert ollama.version()
    assert backup_server.authorizations and all(header == "Bearer " + "f" * 40
                                                for _, header in backup_server.authorizations)
    assert not [path for path, _ in fake_ollama.authorizations]


def test_model_management_always_targets_the_primary_server(make_app, fake_ollama, backup_server):
    from bananachat.services import ollama
    fake_ollama.api_key = "p" * 64
    app = make_app(OLLAMA_API_KEY="p" * 64, INFERENCE_OUTAGE_MODE="fallback",
                   INFERENCE_FALLBACK_URL=backup_server.url)
    outage(app)
    with app.app_context():
        assert [record["status"] for record in ollama.pull("new-model:1b")][-1] == "success"
        assert ollama.delete("new-model:1b") is True
        assert ollama.installed_names(primary=True) == set(fake_ollama.models)
    assert "new-model:1b" in fake_ollama.deleted and backup_server.authorizations == []
    assert all(header == "Bearer " + "p" * 64 for _, header in fake_ollama.authorizations)


def test_a_url_and_its_token_are_chosen_together(make_app, monkeypatch):
    from bananachat.services import health, ollama
    app = make_app(OLLAMA_URL="https://compute.example.org", OLLAMA_API_KEY="p" * 64,
                   INFERENCE_OUTAGE_MODE="fallback", INFERENCE_FALLBACK_URL="https://backup.example.org")
    states = iter([True, False])
    monkeypatch.setattr(health, "inference_down", lambda *args: next(states))
    with app.app_context():
        # The health state flips between the two calls; the pair stays consistent.
        assert ollama.base_url() == "https://backup.example.org"
        assert ollama._headers() == {}
        assert ollama.base_url() == "https://compute.example.org"
        assert ollama._headers() == {"Authorization": "Bearer " + "p" * 64}


def test_an_ssh_tunnel_is_treated_as_a_remote_compute_server(make_app, fake_ollama):
    from bananachat.services import health, housekeeping, pulls, queue, status
    fake_ollama.api_key = "t" * 64
    app = make_app(OLLAMA_API_KEY="t" * 64, MIN_FREE_MEMORY_MB="999999999")
    config = app.config["BC"]
    assert not config.ollama_is_local
    with app.app_context():
        assert queue._memory_ok()  # the web server's own memory does not gate remote inference
        space = pulls.disk_space(config)
        assert not space["checked"] and "compute server" in space["message"]
        housekeeping.probe_inference(app)
        assert health.status()["failures"] == 0
    fake_ollama.api_key = "wrong"
    with app.app_context():
        for _ in range(config.inference_health_failures):
            housekeeping.probe_inference(app)
        assert health.inference_down()
    with app.test_request_context():
        assert [notice.kind for notice in status.notices(None)] == ["outage"]


def test_the_dashboard_names_the_compute_server(make_app, fake_ollama):
    from tests.app.conftest import Browser
    fake_ollama.api_key = "t" * 64
    app = make_app(OLLAMA_API_KEY="t" * 64)
    browser = Browser(app)
    browser.login("admin")
    page = browser.get("/admin/").get_data(as_text=True)
    assert "Compute server" in page and fake_ollama.url in page
    assert "this machine" not in page.lower()
    models = browser.get("/admin/models/downloads").get_data(as_text=True)
    assert "Models are stored on the compute server" in models


def test_a_remote_compute_server_is_not_measured_with_this_machines_gpu_tools(make_app, fake_ollama, monkeypatch):
    from bananachat.services import metrics

    def refuse():
        raise AssertionError("nvidia-smi must not run on a web server")

    monkeypatch.setattr(metrics, "nvidia_metrics", refuse)
    fake_ollama.api_key = "t" * 64
    app = make_app(OLLAMA_API_KEY="t" * 64)
    with app.app_context():
        assert metrics.collect_snapshot(app.config["BC"])["metrics_source"] in {"unavailable", "ollama-allocation"}
