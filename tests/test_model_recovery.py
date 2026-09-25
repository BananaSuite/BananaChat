"""Recovery needs a deliberate admin decision and keeps chat permissions intact."""

import json
import os
from pathlib import Path
import tempfile

import pytest

_environment = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _environment.name)
os.environ.setdefault("BC_DATABASE_PATH", str(Path(_environment.name) / "tests.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")

import config
import db
from app import app
from services import chat_routing, model_recovery, ollama, dispatcher
from banana_ops.models import inventory


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "bananachat.db"))
    monkeypatch.setattr(config, "IMAGE_BACKEND", "comfyui")
    db.init_db()
    db.update_site_settings(setup_done=1)
    user = db.create_user("recovery-user", "unused", role="user")
    admin = db.create_user("recovery-admin", "unused", role="admin")
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [])
    monkeypatch.setattr(ollama, "list_running_models", lambda: [])
    monkeypatch.setattr(dispatcher, "should_use_worker", lambda *_: False)
    return app, db.get_user_by_id(user), db.get_user_by_id(admin)


def client(application, who):
    result = application[0].test_client()
    with result.session_transaction() as session:
        session["user_id"] = who["id"]
        session["language"] = "en"
    return result


def model(name, *, available=True, rollout=True, **fields):
    db.upsert_model(name, name)
    result = db.get_model_by_ollama_name(name)
    db.update_model(result["id"], is_rolled_out=rollout, **fields)
    with db.get_db_context() as connection:
        connection.execute("UPDATE ai_models SET backend_available=? WHERE id=?", (int(available), result["id"]))
        connection.commit()
    return db.get_model_by_ollama_name(name)


def offer(ollama_models=None, huggingface=None):
    value = {"schema": 1, "state": "pending", "inventory": {
        "schema": 1, "ollama": ollama_models or [], "huggingface": huggingface or [], "manual": [], "excluded_paths": ["models"]}}
    model_recovery.write(value)
    return value


def events(response):
    return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]


def test_admin_sees_offer_and_declining_downloads_keeps_chats(application, monkeypatch):
    _, user, admin = application
    session = db.create_session(user["id"], title="Keep this conversation")
    db.add_message(session, "user", "Saved before migration")
    offer(["tiny:latest"])
    monkeypatch.setattr(db, "enqueue_pull_job", lambda *_args, **_kwargs: pytest.fail("Declining must not queue downloads"))
    operator = client(application, admin)
    response = operator.get("/admin/models")
    assert response.status_code == 200
    assert b"Download selected models" in response.data and b"Not now" in response.data
    assert client(application, user).post("/admin/models/recovery", json={"action": "download", "models": ["ollama:0"]}).status_code == 403
    response = operator.post("/admin/models/recovery", data={"action": "defer"})
    assert response.status_code == 302
    assert model_recovery.read()["state"] == "deferred"
    assert db.list_messages(session)[0]["content"] == "Saved before migration"
    assert db.list_pull_jobs() == []


def test_approval_only_queues_selected_missing_models(application, monkeypatch):
    _, _, admin = application
    offer(["already:latest", "missing:latest", "not-selected:latest"])
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": "already:latest"}])
    result = model_recovery.decide("download", ["ollama:0", "ollama:1"], admin["id"])
    assert result["skipped"] == ["already:latest"]
    assert [job["ollama_name"] for job in db.list_pull_jobs()] == ["missing:latest"]
    assert model_recovery.read()["state"] == "requested"
    model_recovery.decide("defer", [], admin["id"])
    assert db.list_pull_jobs()[0]["status"] == "cancelled"


def test_model_download_approval_requires_csrf(application, monkeypatch):
    _, _, admin = application
    offer(["missing:latest"])
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    response = client(application, admin).post("/admin/models/recovery", data={"action": "download", "models": "ollama:0"}, headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert "CSRF" in response.get_json()["error"]
    assert db.list_pull_jobs() == []


def test_huggingface_recipes_keep_digest_and_are_explicit(application, monkeypatch):
    _, _, admin = application
    recipe = {"repo_id": "team/checkpoint", "revision": "a" * 40, "source_filename": "weights/model.safetensors",
              "target_name": "model.safetensors", "expected_sha256": "b" * 64, "expected_size": 1234}
    offer(huggingface=[recipe])
    monkeypatch.setattr(model_recovery.checkpoint_agent, "configuration_error", lambda: None)
    monkeypatch.setattr(model_recovery.comfyui, "discover_checkpoints", lambda: [])
    assert db.list_pull_jobs() == []
    model_recovery.decide("download", ["hf:0"], admin["id"])
    job = db.list_pull_jobs()[0]
    assert job["backend"] == "comfyui"
    for key, value in recipe.items():
        assert job[key] == value
    assert job["requested_by"] == admin["id"]


def test_restore_cancels_old_queue_before_exposing_database(application):
    _, _, admin = application
    queued = db.enqueue_pull_job("old-queued:latest", admin["id"])
    with db.get_db_context() as connection:
        model_recovery.cancel_restored_pulls(connection)
    assert db.get_pull_job(queued)["status"] == "cancelled"
    assert db.claim_next_pull_job() is None


def test_inventory_remembers_hf_provenance_and_ollama_tags(application, tmp_path):
    _, _, admin = application
    model("tiny:latest")
    db.sync_comfyui_models(["image.safetensors", "custom.safetensors"])
    job = db.enqueue_pull_job("image.safetensors", admin["id"], backend="comfyui", repo_id="team/images",
                              revision="a" * 40, source_filename="image.safetensors", target_name="image.safetensors",
                              expected_sha256="b" * 64, expected_size=1234)
    db.finish_pull_job(job, True)
    manifest = tmp_path / "models/manifests/registry.ollama.ai/library/new/3b"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    result = inventory(tmp_path, {"BC_DATABASE_PATH": config.DATABASE_PATH})
    assert result["ollama"] == ["new:3b", "tiny:latest"]
    assert result["huggingface"][0]["expected_sha256"] == "b" * 64
    assert result["manual"][0]["name"] == "custom.safetensors"


def test_unavailable_chat_model_uses_an_authorized_replacement(application, monkeypatch):
    _, user, _ = application
    previous = model("old:latest", available=False)
    replacement = model("tiny:latest")
    blocked = model("forbidden:latest", rollout=False)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": replacement["ollama_name"]}, {"name": blocked["ollama_name"]}])
    monkeypatch.setattr(ollama, "list_running_models", lambda: [{"name": blocked["ollama_name"]}])
    session = db.create_session(user["id"], title="Existing chat")
    db.add_message(session, "assistant", "Earlier answer", model_id=previous["id"])
    seen = []
    def stream(name, history, options=None):
        seen.append((name, history))
        yield "Continued", True, {"prompt_tokens": 1, "completion_tokens": 1}
    monkeypatch.setattr(ollama, "generate_chat_stream", stream)
    response = client(application, user).post(f"/chat/{session}/send", json={"model": previous["ollama_name"], "content": "Continue"})
    values = events(response)
    assert response.status_code == 200
    assert next(item for item in values if item["type"] == "start")["notice"]
    assert seen[0][0] == "tiny:latest" and any(item["content"] == "Earlier answer" for item in seen[0][1])
    assert db.list_messages(session)[-1]["model_id"] == replacement["id"]
    assert db.list_messages(session)[0]["model_id"] == previous["id"]


def test_inference_failure_switches_defaults_and_preserves_history(application, monkeypatch):
    _, user, _ = application
    previous = model("broken:latest", system_prompt="Old operator prompt", temperature=1.5)
    replacement = model("tiny:latest", system_prompt="Replacement operator prompt", temperature=0.2)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": "broken:latest"}, {"name": "tiny:latest"}])
    session = db.create_session(user["id"], title="Existing chat")
    db.add_message(session, "user", "Earlier question")
    seen = []
    def stream(name, history, options=None):
        seen.append((name, history, options))
        if name == "broken:latest":
            raise RuntimeError("Model failed to load")
        yield "Recovered", True, {"prompt_tokens": 1, "completion_tokens": 1}
    monkeypatch.setattr(ollama, "generate_chat_stream", stream)
    response = client(application, user).post(f"/chat/{session}/send", json={"model": previous["ollama_name"], "content": "Continue"})
    values = events(response)
    assert values[-1]["type"] == "done"
    assert [entry[0] for entry in seen] == ["broken:latest", "tiny:latest"]
    assert seen[1][1][0]["content"] == "Replacement operator prompt"
    assert seen[1][2]["temperature"] == 0.2
    assert len(db.list_messages(session)) == 3
    assert db.list_messages(session)[-1]["model_id"] == replacement["id"]


def test_no_switch_after_partial_output_and_no_permission_bypass(application, monkeypatch):
    _, user, _ = application
    previous = model("partial:latest")
    forbidden = model("forbidden:latest", rollout=False)
    session = db.create_session(user["id"])
    def stream(*_args, **_kwargs):
        yield "Partial answer", False, {}
        raise RuntimeError("Stream interrupted")
    monkeypatch.setattr(ollama, "generate_chat_stream", stream)
    monkeypatch.setattr(ollama, "select_auto_model", lambda **_kwargs: pytest.fail("Do not replace an answer already in progress"))
    response = client(application, user).post(f"/chat/{session}/send", json={"model": previous["ollama_name"], "content": "Continue"})
    assert events(response)[-1]["type"] == "error"
    response = client(application, user).post(f"/chat/{session}/send", json={"model": forbidden["ollama_name"], "content": "Continue"})
    assert response.status_code == 403


def test_auto_selection_does_not_use_an_unapproved_tag(application, monkeypatch):
    _, user, _ = application
    model("family:allowed")
    model("family:restricted", rollout=False)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": "family:restricted"}])
    result, error = ollama.select_auto_model(user)
    assert result is None and error


def test_auto_selection_can_recover_before_catalog_sync_and_keeps_vision_rules(application, monkeypatch):
    _, user, _ = application
    model("tiny:latest", available=False)
    vision = model("vision:latest", available=False, supports_vision=1)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": "tiny:latest"}, {"name": "vision:latest"}])
    chosen, error = ollama.select_auto_model(user, vision_only=True)
    assert error is None and chosen["id"] == vision["id"]
