"""Model lifecycle: detection, enrollment, ignore list, retirement of missing, broken and deprecated models,
deleting safely and sturdier (bulk) downloads. Everything runs against the imitation Ollama server."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from tests.app.conftest import TEST_CSRF, Browser
from tests.app.test_chat import new_chat, parse_sse, send, user_browser

GPT_OSS = {"family": "gptoss", "parameter_size": "20.9B", "quantization_level": "MXFP4", "context_length": 131072}


def pin_csrf(browser):
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF


@pytest.fixture
def admin(client):
    client.login("admin", "admin-password")
    pin_csrf(client)
    return client


@pytest.fixture
def lenient(monkeypatch):
    """Count every sync as a separate one (the real code merges syncs closer than 20 seconds)."""
    from bananachat.db import catalog

    monkeypatch.setattr(catalog, "ABSENT_COUNT_SPACING", 0)


def ctx(app):
    return app.test_request_context()


def sync(app, **kwargs):
    from bananachat.services import model_lifecycle

    with ctx(app):
        return model_lifecycle.sync(**kwargs)


def model(app, name):
    from bananachat.db import catalog

    with app.app_context():
        return catalog.get_by_name(name)


def one(app, sql, params=()):
    from bananachat import db

    with app.app_context():
        return db.one(sql, params)


def execute(app, sql, params=()):
    from bananachat import db

    with app.app_context():
        return db.execute(sql, params)


def events(app, name):
    with app.app_context():
        from bananachat import db
        return [row["event"] for row in db.query("SELECT event FROM model_lifecycle_events WHERE model_name=? "
                                                 "ORDER BY id", (name,))]


def set_policy(app, **changes):
    from bananachat.services import model_lifecycle

    with app.app_context():
        return model_lifecycle.save_policy(changes)


def publish(app, *names):
    from bananachat.db import catalog

    with app.app_context():
        for name in names:
            catalog.set_rollout(catalog.get_by_name(name)["id"], True)


def run_next(app, **kwargs):
    from bananachat.services import pulls

    with app.app_context():
        return pulls.process_next(app.config["BC"], **kwargs)


def job(app, name):
    return one(app, "SELECT * FROM model_pull_jobs WHERE ollama_name=? ORDER BY id DESC LIMIT 1", (name,))


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ----- detection -------------------------------------------------------------------------------------------

def test_sync_reads_capabilities_details_and_reasoning_levels(app, admin, fake_ollama):
    fake_ollama.models = ["llama3.2:3b", "qwen3:4b", "gpt-oss:20b", "nomic-embed-text:latest", "llava:7b"]
    fake_ollama.capabilities = {"qwen3:4b": ["completion", "tools", "thinking"],
                                "gpt-oss:20b": ["completion", "tools", "thinking"],
                                "nomic-embed-text:latest": ["embedding"], "llava:7b": ["completion", "vision"]}
    fake_ollama.details = {"gpt-oss:20b": GPT_OSS,
                           "nomic-embed-text:latest": {"family": "nomic-bert", "parameter_size": "137M"}}
    state = sync(app)
    assert state["ok"] and state["count"] == 5 and sorted(state["new"]) == sorted(fake_ollama.models)
    gpt = model(app, "gpt-oss:20b")
    assert json.loads(gpt["reasoning_levels"]) == ["off", "low", "medium", "high"]
    assert json.loads(gpt["capabilities"]) == ["completion", "tools", "thinking"]
    assert (gpt["family"], gpt["parameter_size"], gpt["quantization"], gpt["context_length"]) == \
        ("gptoss", "20.9B", "MXFP4", 131072)
    assert gpt["provider"] == "ollama" and gpt["backend_digest"] == fake_ollama.digest("gpt-oss:20b")
    assert gpt["details_digest"] == gpt["backend_digest"] and gpt["details_error"] is None
    assert json.loads(model(app, "qwen3:4b")["reasoning_levels"]) == ["off", "on"]
    assert model(app, "qwen3:4b")["is_reasoning"] == 1
    assert json.loads(model(app, "llama3.2:3b")["reasoning_levels"]) == []
    assert model(app, "llava:7b")["supports_vision"] == 1
    embed = model(app, "nomic-embed-text:latest")
    assert embed["embedding_only"] == 1
    # New models wait for review (manual is the default) and are hidden.
    assert all(model(app, name)["enrollment"] == "new" and model(app, name)["is_rolled_out"] == 0
               for name in fake_ollama.models)
    assert events(app, "gpt-oss:20b") == ["detected"]
    # An embedding model is never offered for chat, even when published.
    publish(app, "nomic-embed-text:latest", "llama3.2:3b")
    from bananachat.services.access import is_text_model
    assert not is_text_model(model(app, "nomic-embed-text:latest")) and is_text_model(model(app, "llama3.2:3b"))
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "Last sync" in page and "5 models on the model server" in page and "Embeddings only" in page
    # Details are read once per digest: a second sync asks nothing.
    fake_ollama.requests.clear()
    sync(app)
    assert not [path for path, _body in fake_ollama.requests if path == "/api/show"]


def test_a_new_version_is_inspected_again(app, fake_ollama):
    sync(app)
    fake_ollama.capabilities["qwen3:4b"] = ["completion", "thinking"]
    fake_ollama.versions["qwen3:4b"] = 2
    sync(app)
    row = model(app, "qwen3:4b")
    assert row["backend_digest"] == row["details_digest"] == fake_ollama.digest("qwen3:4b")
    assert json.loads(row["capabilities"]) == ["completion", "thinking"]
    assert "updated" in events(app, "qwen3:4b")


def test_one_broken_model_does_not_break_the_sync(app, admin, fake_ollama):
    fake_ollama.show_errors = {"qwen3:4b": 500}
    state = sync(app)
    assert state["ok"] and [item["model"] for item in state["errors"]] == ["qwen3:4b"]
    assert model(app, "qwen3:4b")["details_error"] and model(app, "llama3.2:3b")["details_error"] is None
    assert model(app, "qwen3:4b")["backend_available"] == 1
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "1 model could not be inspected" in page and "cannot show" in page
    response = admin.post("/admin/models/sync", follow_redirects=True).get_data(as_text=True)
    assert "Ollama reports 2 models" in response
    # Retried after an hour, not on every sync.
    fake_ollama.show_errors = {}
    sync(app)
    assert model(app, "qwen3:4b")["details_error"]
    execute(app, "UPDATE ai_models SET details_at='2000-01-01 00:00:00' WHERE ollama_name='qwen3:4b'")
    sync(app)
    assert model(app, "qwen3:4b")["details_error"] is None


def test_a_failed_listing_changes_nothing(app, admin, fake_ollama, lenient):
    from bananachat.services.upstream import UpstreamError

    sync(app)
    fake_ollama.tags_status = 500
    for _ in range(5):
        with pytest.raises(UpstreamError):
            sync(app)
    row = model(app, "llama3.2:3b")
    assert row["backend_available"] == 1 and row["absent_syncs"] == 0 and row["missing_at"] is None
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "could not be listed" in page and "nothing was marked missing" in page


def test_an_empty_listing_is_believed_only_when_it_repeats(app, fake_ollama, lenient):
    sync(app)
    fake_ollama.models = []
    first = sync(app)
    assert "listed no models" in first["note"] and model(app, "llama3.2:3b")["backend_available"] == 1
    sync(app)
    sync(app)  # the third empty listing in a row (missing_syncs=3)
    assert model(app, "llama3.2:3b")["backend_available"] == 0


def test_concurrent_syncs_add_each_model_once(app, fake_ollama):
    fake_ollama.models = [f"model-{index}:1b" for index in range(12)]
    errors = []

    def worker():
        from bananachat import db
        try:
            sync(app)
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)
        finally:
            db.close_thread_connection()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert one(app, "SELECT COUNT(*) AS n FROM ai_models")["n"] == 12
    assert one(app, "SELECT COUNT(*) AS n FROM model_lifecycle_events WHERE event='detected'")["n"] == 12


# ----- enrollment -----------------------------------------------------------------------------------------

def test_manual_enrollment_waits_for_review(app, admin, fake_ollama):
    from bananachat.db import access as access_db

    sync(app)
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "2 new models" in page and "wait for your review" in page
    qwen = model(app, "qwen3:4b")
    form = admin.get(f"/admin/models/{qwen['id']}/enable").get_data(as_text=True)
    assert "Enable and publish" in form and "(suggested)" in form
    admin.post("/admin/models/categories/create", {"name": "Coding", "scope": "chat"})
    category = one(app, "SELECT id FROM model_categories WHERE name='Coding'")["id"]
    bad = admin.post(f"/admin/models/{qwen['id']}/enable", {"display_name": "", "access_mode": "allow_all",
                                                            "limit_preset": "light"})
    assert bad.status_code == 400 and model(app, "qwen3:4b")["is_rolled_out"] == 0
    response = admin.post(f"/admin/models/{qwen['id']}/enable", {
        "display_name": "Qwen", "description": "Small and quick", "access_mode": "deny_except_allowlist",
        "limit_preset": "heavy", "categories": str(category), "supports_vision": "1"})
    assert response.status_code == 302
    row = model(app, "qwen3:4b")
    assert (row["is_rolled_out"], row["enrollment"], row["limit_preset"], row["display_name"],
            row["supports_vision"]) == (1, "reviewed", "heavy", "Qwen", 1)
    with app.app_context():
        assert access_db.get_policy("model", row["id"])["mode"] == "deny_except_allowlist"
    assert one(app, "SELECT 1 FROM model_category_assignments WHERE model_id=?", (row["id"],))
    assert events(app, "qwen3:4b")[-1] == "enabled"
    assert one(app, "SELECT 1 FROM audit_log WHERE action='admin.model_enable' AND target='qwen3:4b'")
    # Publishing from the catalog reviews a new model too.
    llama = model(app, "llama3.2:3b")
    admin.post(f"/admin/models/{llama['id']}/rollout", {"enabled": "1"})
    assert model(app, "llama3.2:3b")["enrollment"] == "reviewed"
    assert "new model" not in admin.get("/admin/models").get_data(as_text=True)


def test_automatic_enrollment_uses_size_presets_and_the_limits_hook(app, admin, fake_ollama, monkeypatch):
    from bananachat.services import limits

    calls = []
    monkeypatch.setattr(limits, "apply_model_preset", lambda model_id, preset: calls.append((model_id, preset)),
                        raising=False)
    fake_ollama.models = ["llama3.2:3b", "gpt-oss:20b", "llama3.3:70b", "nomic-embed-text:latest", "broken:1b"]
    fake_ollama.details = {"gpt-oss:20b": GPT_OSS, "llama3.3:70b": {"parameter_size": "70.6B"},
                           "nomic-embed-text:latest": {"family": "nomic-bert", "parameter_size": "137M"}}
    fake_ollama.capabilities = {"nomic-embed-text:latest": ["embedding"]}
    fake_ollama.show_errors = {"broken:1b": 500}
    admin.post("/admin/models/settings", {"enrollment": "automatic", "missing_syncs": "3", "missing_minutes": "30",
                                          "retention_days": "30", "failure_threshold": "5",
                                          "failure_window_minutes": "30", "recheck_minutes": "10",
                                          "download_concurrency": "1", "download_retries": "3", "stall_minutes": "10"})
    state = sync(app)
    assert sorted(state["enabled"]) == ["gpt-oss:20b", "llama3.2:3b", "llama3.3:70b"]
    presets = {name: model(app, name)["limit_preset"] for name in ("llama3.2:3b", "gpt-oss:20b", "llama3.3:70b")}
    assert presets == {"llama3.2:3b": "light", "gpt-oss:20b": "standard", "llama3.3:70b": "heavy"}
    assert sorted(preset for _id, preset in calls) == ["heavy", "light", "standard"]
    assert all(model(app, name)["enrollment"] == "auto" and model(app, name)["is_rolled_out"] == 1
               for name in presets)
    # Never automatically: embedding models and models whose details could not be read.
    assert model(app, "nomic-embed-text:latest")["enrollment"] == "new"
    assert model(app, "broken:1b")["enrollment"] == "new" and model(app, "broken:1b")["is_rolled_out"] == 0
    assert one(app, "SELECT 1 FROM audit_log WHERE action='models.auto_enabled' AND target='llama3.2:3b'")
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "Enabled automatically — review" in page and "Needs attention" in page
    llama = model(app, "llama3.2:3b")
    admin.post(f"/admin/models/{llama['id']}/reviewed")
    assert model(app, "llama3.2:3b")["enrollment"] == "reviewed"
    # Hidden by hand later: never enabled automatically again.
    admin.post(f"/admin/models/{llama['id']}/rollout", {"enabled": "0"})
    sync(app)
    assert model(app, "llama3.2:3b")["is_rolled_out"] == 0


def test_the_preset_is_stored_when_the_limits_cannot_apply_it(app, monkeypatch, fake_ollama):
    from bananachat import db as database
    from bananachat.services import limits, model_lifecycle

    monkeypatch.delattr(limits, "apply_model_preset", raising=False)
    from bananachat.db import limits as limits_db
    monkeypatch.delattr(limits_db, "apply_model_preset", raising=False)
    sync(app)
    row = model(app, "qwen3:4b")
    with app.app_context():
        assert model_lifecycle.apply_limit_preset(row["id"], "heavy") is False
        assert database.scalar("SELECT limit_preset FROM ai_models WHERE id=?", (row["id"],)) == "heavy"

        def broken(model_id, preset):
            raise RuntimeError("limits are being rebuilt")
        monkeypatch.setattr(limits, "apply_model_preset", broken, raising=False)
        assert model_lifecycle.apply_limit_preset(row["id"], "light") is False
        assert database.scalar("SELECT limit_preset FROM ai_models WHERE id=?", (row["id"],)) == "light"
        with pytest.raises(ValueError):
            model_lifecycle.apply_limit_preset(row["id"], "extreme")


def test_parameter_sizes_choose_presets():
    from bananachat.services import model_lifecycle

    assert model_lifecycle.parameters_billions("567.72M") == pytest.approx(0.56772)
    assert model_lifecycle.parameters_billions("1.5T") == 1500
    assert model_lifecycle.parameters_billions("") is None
    assert [model_lifecycle.preset_for({"parameter_size": size}) for size in ("3.2B", "7B", "29.9B", "30B", "")] == \
        ["light", "standard", "standard", "heavy", "standard"]


# ----- ignore list ------------------------------------------------------------------------------------------

def test_the_ignore_list(app, admin, fake_ollama, make_user):
    from bananachat.services import inference
    from bananachat.services.access import AccessContext

    fake_ollama.models = ["llama3.2:3b", "qwen3:4b", "nomic-embed-text:latest"]
    sync(app)
    for bad in ("*", "?*", "a b", "x" * 201):
        response = admin.post("/admin/models/ignore-rules", {"pattern": bad}, follow_redirects=True)
        assert "Enter a model name" in response.get_data(as_text=True) or "at least one letter" in \
            response.get_data(as_text=True) or "at most" in response.get_data(as_text=True)
    assert one(app, "SELECT COUNT(*) AS n FROM model_ignore_rules")["n"] == 0
    admin.post("/admin/models/ignore-rules", {"pattern": "*-EMBED*", "note": "No embeddings here"})
    assert model(app, "nomic-embed-text:latest")["enrollment"] == "ignored"
    # New models matching a rule are recorded as ignored; they never wait for review.
    fake_ollama.models.append("mxbai-embed-large:latest")
    sync(app)
    assert model(app, "mxbai-embed-large:latest")["enrollment"] == "ignored"
    # Ignoring a published model hides it from everyone, even by name.
    publish(app, "llama3.2:3b", "qwen3:4b")
    qwen = model(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/ignore")
    assert (model(app, "qwen3:4b")["enrollment"], model(app, "qwen3:4b")["is_rolled_out"]) == ("ignored", 0)
    user = make_user("ignored-user")
    with app.app_context():
        selection = inference.select_model(AccessContext.load(user), "qwen3:4b", surface="chat")
        assert selection.model["ollama_name"] == "llama3.2:3b"
        with pytest.raises(inference.ModelUnavailable) as refused:
            inference.select_model(AccessContext.load(user), "qwen3:4b", surface="api", strict=True)
        assert refused.value.status == 404
    settings = admin.get("/admin/models/settings").get_data(as_text=True)
    assert "*-EMBED*" in settings and "No embeddings here" in settings and "qwen3:4b" in settings
    assert "qwen3:4b" not in admin.get("/admin/models").get_data(as_text=True).split("Loaded in memory")[0] \
        .split('id="catalog-title"')[1]
    # Restoring puts it back for review (never enabled automatically) and forgets its exact-name rule.
    set_policy(app, enrollment="automatic")
    admin.post(f"/admin/models/{qwen['id']}/restore")
    sync(app)
    row = model(app, "qwen3:4b")
    assert (row["enrollment"], row["is_rolled_out"]) == ("new", 0)
    assert one(app, "SELECT 1 FROM model_ignore_rules WHERE pattern='qwen3:4b'") is None
    rule = one(app, "SELECT id FROM model_ignore_rules WHERE pattern='*-EMBED*'")["id"]
    admin.post(f"/admin/models/ignore-rules/{rule}/delete")
    assert one(app, "SELECT COUNT(*) AS n FROM model_ignore_rules")["n"] == 0
    assert model(app, "nomic-embed-text:latest")["enrollment"] == "ignored"  # stays ignored until restored
    assert admin.post("/admin/models/ignore-rules/999/delete").status_code == 404


def test_ignored_and_retired_models_are_not_suggested_for_download(app):
    from bananachat.services import model_lifecycle, pulls

    with app.app_context():
        model_lifecycle.add_ignore_rule("gpt-oss:*")
        names = [item["name"] for item in pulls.suggestions()]
    assert "gpt-oss:20b" not in names and "qwen3:8b" in names


# ----- missing models ---------------------------------------------------------------------------------------

def test_a_model_is_missing_after_consecutive_syncs_and_returns_as_it_was(app, admin, fake_ollama, lenient):
    sync(app)
    publish(app, "qwen3:4b")
    qwen = model(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/edit", {"display_name": "Company Qwen"})
    fake_ollama.models.remove("qwen3:4b")
    sync(app)
    sync(app)
    row = model(app, "qwen3:4b")
    assert row["backend_available"] == 0 and row["absent_syncs"] == 2 and row["missing_at"] is None
    sync(app)
    row = model(app, "qwen3:4b")
    assert row["missing_at"] and "3 consecutive syncs" in row["missing_reason"]
    assert events(app, "qwen3:4b")[-1] == "missing"
    assert one(app, "SELECT 1 FROM audit_log WHERE action='models.missing' AND target='qwen3:4b'")
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "Missing" in page and "Download again" in page
    fake_ollama.models.append("qwen3:4b")
    sync(app)
    row = model(app, "qwen3:4b")
    assert (row["missing_at"], row["backend_available"], row["is_rolled_out"], row["display_name"], row["absent_syncs"]) \
        == (None, 1, 1, "Company Qwen", 0)
    assert events(app, "qwen3:4b")[-1] == "returned"


def test_syncs_close_together_count_once(app, fake_ollama):
    sync(app)
    fake_ollama.models.remove("qwen3:4b")
    for _ in range(4):
        sync(app)
    row = model(app, "qwen3:4b")
    assert row["absent_syncs"] == 1 and row["missing_at"] is None and row["backend_available"] == 0


def test_a_model_is_missing_after_the_configured_minutes(app, fake_ollama):
    sync(app)
    fake_ollama.models.remove("qwen3:4b")
    sync(app)
    execute(app, "UPDATE ai_models SET absent_since='2000-01-01 00:00:00' WHERE ollama_name='qwen3:4b'")
    sync(app)
    assert model(app, "qwen3:4b")["missing_at"]


def test_long_missing_models_leave_the_catalog_and_chats_keep_their_name(app, make_user, fake_ollama):
    from bananachat.db import chats
    from bananachat.services import model_lifecycle

    sync(app)
    publish(app, "qwen3:4b")
    alice = user_browser(app, make_user)
    session_id = new_chat(alice)
    parse_sse(send(alice, session_id, model="qwen3:4b").get_data(as_text=True))
    execute(app, "UPDATE ai_models SET missing_at='2000-01-01 00:00:00', backend_available=0, display_name='Qwen Old' "
                 "WHERE ollama_name='qwen3:4b'")
    with app.app_context():
        assert model_lifecycle.purge_missing() == 1
        messages, _more = chats.message_page(session_id)
    assert model(app, "qwen3:4b") is None
    assert [row["model_name"] for row in messages if row["role"] == "assistant"] == ["Qwen Old"]
    assert events(app, "qwen3:4b")[-1] == "removed"
    assert alice.get(f"/chat/{session_id}").status_code == 200


# ----- failing models ---------------------------------------------------------------------------------------

def _generate(app, name, user):
    from bananachat.db import catalog
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    with app.app_context():
        request = inference.TextRequest(user=user, model=catalog.get_by_name(name),
                                        messages=[{"role": "user", "content": "Hi"}], options={"num_predict": 8})
        return list(inference.generate(request, CancelToken()))


def test_repeated_failures_mark_a_model_failing_until_it_answers_again(app, admin, fake_ollama, make_user):
    from bananachat.services import inference, model_lifecycle
    from bananachat.services.access import AccessContext

    sync(app)
    publish(app, "llama3.2:3b", "qwen3:4b")
    user = make_user("failing-user")
    fake_ollama.fail_models = {"qwen3:4b"}
    for _attempt in range(4):
        assert _generate(app, "qwen3:4b", user)[-1].state == "failed"
    assert model(app, "qwen3:4b")["failing_at"] is None and model(app, "qwen3:4b")["recent_failures"] == 4
    _generate(app, "qwen3:4b", user)
    row = model(app, "qwen3:4b")
    assert row["failing_at"] and "5 failures within 30 minutes" in row["state_reason"]
    assert events(app, "qwen3:4b")[-1] == "failing"
    with app.app_context():
        context = AccessContext.load(user)
        assert [item["ollama_name"] for item in inference.candidates(context, "chat")] == ["llama3.2:3b"]
        selection = inference.select_model(context, "qwen3:4b", surface="chat")
        assert selection.model["ollama_name"] == "llama3.2:3b" and "not available" in selection.notice
    assert "Failing" in admin.get("/admin/models").get_data(as_text=True)

    # The background re-check: still broken, then answering again.
    fake_ollama.generate_fail = {"qwen3:4b"}
    execute(app, "UPDATE ai_models SET recheck_at='2000-01-01 00:00:00' WHERE ollama_name='qwen3:4b'")
    with ctx(app):
        assert model_lifecycle.recheck_failing() == [("qwen3:4b", False)]
    row = model(app, "qwen3:4b")
    assert row["failing_at"] and row["recheck_attempts"] == 1 and row["recheck_at"] > "2001"
    with ctx(app):
        assert model_lifecycle.recheck_failing() == []  # not due yet
    fake_ollama.generate_fail = set()
    fake_ollama.fail_models = set()
    execute(app, "UPDATE ai_models SET recheck_at='2000-01-01 00:00:00' WHERE ollama_name='qwen3:4b'")
    with ctx(app):
        assert model_lifecycle.recheck_failing() == [("qwen3:4b", True)]
    row = model(app, "qwen3:4b")
    assert row["failing_at"] is None and row["recent_failures"] == 0
    assert events(app, "qwen3:4b")[-2:] == ["recheck_failed", "recovered"]
    probe = [body for path, body in fake_ollama.requests if path == "/api/generate" and body.get("prompt")]
    assert probe and probe[-1]["options"] == {"num_predict": 8}


def test_a_success_resets_the_failure_count_and_admins_can_force_a_model_back(app, admin, fake_ollama, make_user):
    sync(app)
    publish(app, "qwen3:4b")
    user = make_user("reset-user")
    fake_ollama.fail_models = {"qwen3:4b"}
    for _ in range(4):
        _generate(app, "qwen3:4b", user)
    fake_ollama.fail_models = set()
    assert _generate(app, "qwen3:4b", user)[-1].state == "completed"
    assert model(app, "qwen3:4b")["recent_failures"] == 0
    fake_ollama.fail_models = {"qwen3:4b"}
    for _ in range(5):
        _generate(app, "qwen3:4b", user)
    qwen = model(app, "qwen3:4b")
    assert qwen["failing_at"]
    admin.post(f"/admin/models/{qwen['id']}/lifecycle", {"action": "retry_check"})
    assert model(app, "qwen3:4b")["recheck_at"] <= model(app, "qwen3:4b")["updated_at"]
    admin.post(f"/admin/models/{qwen['id']}/lifecycle", {"action": "force_enable", "return_to": "catalog"})
    row = model(app, "qwen3:4b")
    assert row["failing_at"] is None and row["recent_failures"] == 0
    assert events(app, "qwen3:4b")[-2:] == ["recheck_requested", "force_enabled"]
    assert one(app, "SELECT 1 FROM audit_log WHERE action='admin.model_force_enable'")


def test_outages_busy_servers_and_cancellations_are_not_the_models_fault(make_app, fake_ollama, make_user):
    from bananachat.services import health, model_lifecycle
    from bananachat.services.upstream import UpstreamError

    assert model_lifecycle.counts_as_failure(UpstreamError("model failed to load", 500))
    assert model_lifecycle.counts_as_failure(UpstreamError("llama runner process has terminated"))
    assert not model_lifecycle.counts_as_failure(UpstreamError("busy", 503))
    assert not model_lifecycle.counts_as_failure(UpstreamError("busy", 429))
    assert not model_lifecycle.counts_as_failure(UpstreamError("refused", kind="connect"))
    assert not model_lifecycle.counts_as_failure(OSError("reset"))

    remote = make_app(INFERENCE_LOCAL="0")
    sync(remote)
    with remote.app_context():
        row = model(remote, "qwen3:4b")
        health.record_probe(False, 1, "down")
        assert model_lifecycle.record_failure(row, UpstreamError("failed", 500)) is False
        health.record_probe(True, 1)
        health.reset()
    assert model(remote, "qwen3:4b")["recent_failures"] == 0


def test_requests_record_the_model_they_use(app, fake_ollama, make_user):
    from bananachat import db

    sync(app)
    publish(app, "qwen3:4b")
    user = make_user("queue-user")
    fake_ollama.chunk_delay = 0.2
    fake_ollama.reply = "one two three four five"
    seen = []
    worker = threading.Thread(target=lambda: _generate(app, "qwen3:4b", user))
    worker.start()

    def running():
        with app.app_context():
            names = [row["model_name"] for row in db.query("SELECT model_name FROM inference_queue")]
        seen.extend(names)
        return "qwen3:4b" in names
    assert wait_for(running)
    worker.join(10)
    assert one(app, "SELECT COUNT(*) AS n FROM inference_queue")["n"] == 0


# ----- deprecation and retirement ------------------------------------------------------------------------------

def test_deprecated_models_move_chats_to_the_replacement_and_retire_on_their_date(app, admin, make_user):
    from bananachat.db import personalities, tokens
    from bananachat.services import model_lifecycle

    sync(app)
    publish(app, "llama3.2:3b", "qwen3:4b")
    alice = user_browser(app, make_user)
    alice_row = one(app, "SELECT * FROM users WHERE username='alice'")
    with app.app_context():
        personality = personalities.create(alice_row["id"], "Tutor", "Explain.", created_by=alice_row["id"],
                                           preferred_model="qwen3:4b")
        api_token = tokens.create(alice_row["id"], "test")[1]
    qwen, llama = model(app, "qwen3:4b"), model(app, "llama3.2:3b")
    loop = admin.post(f"/admin/models/{llama['id']}/lifecycle", {"action": "deprecate", "replacement_id": str(llama["id"])},
                      follow_redirects=True)
    assert "cannot replace itself" in loop.get_data(as_text=True)
    admin.post(f"/admin/models/{qwen['id']}/lifecycle", {"action": "deprecate", "replacement_id": str(llama["id"]),
                                                         "retire_at": "2999-01-01T00:00", "note": "Superseded"})
    row = model(app, "qwen3:4b")
    assert row["deprecated_at"] and row["replacement_id"] == llama["id"] and row["retire_at"] == "2999-01-01 00:00:00"
    # A loop through replacements is refused.
    looped = admin.post(f"/admin/models/{llama['id']}/lifecycle", {"action": "deprecate",
                                                                   "replacement_id": str(qwen["id"])},
                        follow_redirects=True)
    assert "in a loop" in looped.get_data(as_text=True) and model(app, "llama3.2:3b")["deprecated_at"] is None

    # The chat picker hides it; choosing it moves to the replacement with a notice (en and it).
    page = alice.get(f"/chat/{new_chat(alice)}").get_data(as_text=True)
    data = json.loads(page.split('id="page-data">')[1].split("</script>")[0])
    assert [item["name"] for item in data["models"]] == ["llama3.2:3b"]
    session_id = new_chat(alice)
    events_en = parse_sse(send(alice, session_id, model="qwen3:4b").get_data(as_text=True))
    assert events_en[0]["model"] == "llama3.2:3b"
    assert events_en[0]["notice"] == "Qwen3 is being phased out, so Llama3.2 is answering instead."
    italian = alice.post(f"/chat/{new_chat(alice)}/send", {"content": "Ciao", "model": "qwen3:4b"},
                         headers={"X-Requested-With": "fetch", "Accept-Language": "it"})
    notice = parse_sse(italian.get_data(as_text=True))[0]["notice"]
    assert notice in ("Qwen3 sta per essere dismesso, quindi risponde Llama3.2.",
                      "Qwen3 is being phased out, so Llama3.2 is answering instead.")
    # The API keeps serving it until it is retired.
    client = app.test_client()
    body = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "Hi"}]}
    served = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {api_token}"})
    assert served.status_code == 200 and served.get_json()["model"] == "qwen3:4b"

    execute(app, "UPDATE ai_models SET retire_at='2000-01-01 00:00:00' WHERE ollama_name='qwen3:4b'")
    with app.app_context():
        assert model_lifecycle.retire_due() == 1
        assert model_lifecycle.retire_due() == 0
        assert personalities.get(personality)["preferred_model"] == "llama3.2:3b"
    assert model(app, "qwen3:4b")["retired_at"] and events(app, "qwen3:4b")[-1] == "retired"
    refused = client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {api_token}"})
    assert refused.status_code == 404 and refused.get_json()["error"]["code"] == "model_not_found"
    assert "retired; use 'llama3.2:3b' instead" in refused.get_json()["error"]["message"]
    retired = parse_sse(send(alice, new_chat(alice), model="qwen3:4b").get_data(as_text=True))
    assert retired[0]["notice"] == "Qwen3 was retired, so Llama3.2 is answering instead."
    assert "Retired" in admin.get(f"/admin/models/{qwen['id']}/edit").get_data(as_text=True)
    # Restoring brings it back.
    admin.post(f"/admin/models/{qwen['id']}/lifecycle", {"action": "undeprecate"})
    assert model(app, "qwen3:4b")["retired_at"] is None and model(app, "qwen3:4b")["deprecated_at"] is None


def test_a_deprecated_model_without_replacement_keeps_answering(app, make_user):
    from bananachat.services import inference
    from bananachat.services.access import AccessContext

    sync(app)
    publish(app, "llama3.2:3b", "qwen3:4b")
    user = make_user("deprecated-user")
    with app.app_context():
        from bananachat.services import model_lifecycle
        model_lifecycle.deprecate(model(app, "qwen3:4b"))
        selection = inference.select_model(AccessContext.load(user), "qwen3:4b", surface="chat")
        assert selection.model["ollama_name"] == "qwen3:4b" and not selection.reason
        # Automatic choices prefer models that are not deprecated.
        assert inference.select_model(AccessContext.load(user), "auto", surface="chat").model["ollama_name"] == \
            "llama3.2:3b"
        model_lifecycle.retire(model(app, "qwen3:4b"))
        selection = inference.select_model(AccessContext.load(user), "qwen3:4b", surface="chat")
        assert selection.model["ollama_name"] == "llama3.2:3b" and selection.reason == "retired"


# ----- deleting ------------------------------------------------------------------------------------------------

def _hold_request(app, name):
    import uuid

    execute(app, "INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, started_at, "
                 "heartbeat_at, owner_key, model_name) VALUES (?, 3, 'running', 1, ?, ?, ?, 'user:x', ?)",
            (uuid.uuid4().hex, time.time(), time.time(), time.time(), name))


def test_deleting_waits_for_running_requests(app, admin, fake_ollama):
    from bananachat.db import personalities
    from bananachat.services import model_lifecycle
    from bananachat.services.access import is_text_model

    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    admin_row = one(app, "SELECT * FROM users WHERE username='admin'")
    with app.app_context():
        personality = personalities.create(admin_row["id"], "Pirate", "Arr.", created_by=admin_row["id"],
                                           preferred_model="qwen3:4b")
    qwen = model(app, "qwen3:4b")
    _hold_request(app, "qwen3:4b")
    refused = admin.post(f"/admin/models/{qwen['id']}/delete-server", follow_redirects=True).get_data(as_text=True)
    assert "1 request is using this model right now" in refused
    row = model(app, "qwen3:4b")
    assert row["delete_requested_at"] is None and is_text_model(row) and fake_ollama.deleted == []
    edit = admin.get(f"/admin/models/{qwen['id']}/edit").get_data(as_text=True)
    assert "1 request is using it right now" in edit and "Delete after current requests" in edit

    admin.post(f"/admin/models/{qwen['id']}/delete-server", {"when_idle": "1"})
    row = model(app, "qwen3:4b")
    assert row["delete_requested_at"] and not is_text_model(row) and fake_ollama.deleted == []
    with ctx(app):
        assert model_lifecycle.process_pending_deletes() == 0
    execute(app, "DELETE FROM inference_queue")
    with ctx(app):
        assert model_lifecycle.process_pending_deletes() == 1
    row = model(app, "qwen3:4b")
    assert fake_ollama.deleted == ["qwen3:4b"] and row["backend_available"] == 0 and row["missing_at"]
    assert row["delete_requested_at"] is None
    with app.app_context():
        assert personalities.get(personality)["preferred_model"] == ""
    assert events(app, "qwen3:4b")[-2:] == ["delete_scheduled", "deleted"]


def test_deleting_now_and_cancelling_a_waiting_deletion(app, admin, fake_ollama):
    sync(app)
    llama = model(app, "llama3.2:3b")
    _hold_request(app, "llama3.2:3b")
    admin.post(f"/admin/models/{llama['id']}/delete-server", {"when_idle": "1"})
    admin.post(f"/admin/models/{llama['id']}/lifecycle", {"action": "cancel_delete"})
    assert model(app, "llama3.2:3b")["delete_requested_at"] is None
    execute(app, "DELETE FROM inference_queue")
    admin.post("/admin/models/downloads", {"name": "qwen3:4b"})
    busy = admin.post(f"/admin/models/{model(app, 'qwen3:4b')['id']}/delete-server", follow_redirects=True)
    assert "cancel it first" in busy.get_data(as_text=True) and fake_ollama.deleted == []
    done = admin.post(f"/admin/models/{llama['id']}/delete-server", follow_redirects=True).get_data(as_text=True)
    assert "was deleted from the Ollama server" in done and fake_ollama.deleted == ["llama3.2:3b"]


# ----- downloads ----------------------------------------------------------------------------------------------

@pytest.fixture
def fast_retries(monkeypatch):
    from bananachat.services import pulls

    monkeypatch.setattr(pulls, "RETRY_BASE_SECONDS", 0)


def test_transient_download_errors_are_retried_with_back_off(app, admin, fake_ollama, monkeypatch):
    from bananachat.services import pulls

    fake_ollama.pull_errors = {"tiny:1b": ["pull model manifest: dial tcp: i/o timeout", None]}
    admin.post("/admin/models/downloads", {"name": "tiny:1b"})
    run_next(app)
    row = job(app, "tiny:1b")
    assert (row["status"], row["attempts"]) == ("queued", 1) and row["next_attempt_at"] > row["created_at"]
    assert "Retrying in 30 s (attempt 2 of 4)" in row["progress_detail"] and "i/o timeout" in row["last_error"]
    assert run_next(app) is False  # waiting for its back-off
    monkeypatch.setattr(pulls, "RETRY_BASE_SECONDS", 0)
    execute(app, "UPDATE model_pull_jobs SET next_attempt_at='2000-01-01 00:00:00'")
    assert run_next(app) is True
    row = job(app, "tiny:1b")
    assert row["status"] == "done" and row["digest"] == fake_ollama.digest("tiny:1b")
    assert model(app, "tiny:1b")["backend_available"] == 1
    assert "verified" in admin.get("/admin/models/downloads").get_data(as_text=True)


def test_errors_that_name_the_model_or_a_full_disk_fail_at_once(app, admin, fake_ollama, fast_retries):
    fake_ollama.pull_errors = {"ghost:1b": ["pull model manifest: file does not exist"],
                               "huge:70b": ["write /models/blobs/sha256-partial: no space left on device"]}
    fake_ollama.pull_http_errors = {"private:1b": 401}
    for name in ("ghost:1b", "huge:70b", "private:1b"):
        admin.post("/admin/models/downloads", {"name": name})
        run_next(app)
        assert (job(app, name)["status"], job(app, name)["attempts"]) == ("failed", 0)
    assert "ran out of disk space" in job(app, "huge:70b")["error_message"]
    assert fake_ollama.pull_attempts == {"ghost:1b": 1, "huge:70b": 1, "private:1b": 1}


def test_retries_are_bounded(app, admin, fake_ollama, fast_retries):
    set_policy(app, download_retries=2)
    fake_ollama.pull_errors = {"flaky:1b": ["connection reset by peer"] * 5}
    admin.post("/admin/models/downloads", {"name": "flaky:1b"})
    for _ in range(3):
        execute(app, "UPDATE model_pull_jobs SET next_attempt_at=NULL")
        assert run_next(app) is True
    row = job(app, "flaky:1b")
    assert row["status"] == "failed" and "gave up after 3 attempts" in row["error_message"]
    assert fake_ollama.pull_attempts["flaky:1b"] == 3


def test_a_stalled_download_is_retried_then_fails(app, admin, fake_ollama, fast_retries, monkeypatch):
    from bananachat.services import pulls

    monkeypatch.setattr(pulls, "_stall_seconds", lambda current: 0.6)
    set_policy(app, download_retries=1)
    fake_ollama.pull_stall = {"slow:1b"}
    fake_ollama.pull_stall_seconds = 10
    admin.post("/admin/models/downloads", {"name": "slow:1b"})
    started = time.monotonic()
    run_next(app)
    assert time.monotonic() - started < 8
    row = job(app, "slow:1b")
    assert row["status"] == "queued" and "No progress for 10 minutes" in row["last_error"]
    execute(app, "UPDATE model_pull_jobs SET next_attempt_at=NULL")
    run_next(app)
    assert job(app, "slow:1b")["status"] == "failed"
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)


def test_a_download_counts_only_once_the_server_lists_it(app, admin, fake_ollama, fast_retries):
    set_policy(app, download_retries=0)
    fake_ollama.pull_unlisted = {"phantom:1b"}
    admin.post("/admin/models/downloads", {"name": "phantom:1b"})
    run_next(app)
    row = job(app, "phantom:1b")
    assert row["status"] == "failed" and "does not list phantom:1b" in row["error_message"]


def test_one_active_download_per_model_even_across_processes(app):
    from bananachat.db import pulls as pulls_db

    with app.app_context():
        first = pulls_db.enqueue("tiny:1b", None)
        with pytest.raises(pulls_db.AlreadyActive) as again:
            pulls_db.enqueue("tiny:1b", None)
        assert again.value.job_id == first
    with pytest.raises(sqlite3.IntegrityError):
        # What a second process writing directly would hit: the partial unique index.
        execute(app, "INSERT INTO model_pull_jobs (ollama_name, backend, status, idempotency_key, created_at) "
                     "VALUES ('tiny:1b', 'ollama', 'queued', 'other-key', '2026-01-01 00:00:00')")


def test_bulk_downloads_and_the_queue(app, admin, fake_ollama):
    from bananachat.db import pulls as pulls_db

    sync(app)
    page = admin.get("/admin/models/downloads").get_data(as_text=True)
    assert "Several models" in page and "qwen3:8b" in page and "Queue" in page
    response = admin.post("/admin/models/downloads/bulk", {
        "names": "alpha:1b\nbad name!\nllama3.2:3b, beta:1b\nalpha:1b\n# a comment", "models": ["gamma:1b"]},
        follow_redirects=True).get_data(as_text=True)
    assert "3 downloads queued" in response and "llama3.2:3b (already installed)" in response
    assert "bad name!" in response
    again = admin.post("/admin/models/downloads/bulk", {"names": "alpha:1b"}, follow_redirects=True)
    assert "alpha:1b (already queued)" in again.get_data(as_text=True)
    order = lambda: [row["ollama_name"] for row in pulls_db_active(app)]  # noqa: E731
    assert order() == ["alpha:1b", "beta:1b", "gamma:1b"]
    gamma = job(app, "gamma:1b")
    admin.post(f"/admin/models/downloads/{gamma['id']}/move", {"direction": "top"})
    assert order() == ["gamma:1b", "alpha:1b", "beta:1b"]
    admin.post(f"/admin/models/downloads/{gamma['id']}/move", {"direction": "down"})
    assert order() == ["alpha:1b", "gamma:1b", "beta:1b"]
    assert admin.post(f"/admin/models/downloads/{gamma['id']}/move", {"direction": "sideways"}).status_code == 400

    alpha = job(app, "alpha:1b")
    admin.post(f"/admin/models/downloads/{alpha['id']}/pause")
    run_next(app)
    assert job(app, "gamma:1b")["status"] == "done" and job(app, "alpha:1b")["status"] == "queued"
    admin.post(f"/admin/models/downloads/{alpha['id']}/pause", {"resume": "1"})
    admin.post("/admin/models/downloads/queue", {"action": "pause_all"})
    assert run_next(app) is False
    assert "The queue is paused" in admin.get("/admin/models/downloads").get_data(as_text=True)
    admin.post("/admin/models/downloads/queue", {"action": "resume_all"})
    status = admin.fetch("/admin/api/models/downloads").get_json()
    assert status["queue"]["active"] == 2 and status["queue"]["queue_paused"] is False
    admin.post("/admin/models/downloads/queue", {"action": "cancel_all"})
    assert not pulls_db_active(app)
    with app.app_context():
        assert pulls_db.summary()["active"] == 0
    assert admin.post("/admin/models/downloads/queue", {"action": "explode"}).status_code == 400
    too_many = admin.post("/admin/models/downloads/bulk", {"names": "\n".join(f"m{i}:1b" for i in range(51))},
                          follow_redirects=True)
    assert "at most 50" in too_many.get_data(as_text=True)


def pulls_db_active(app):
    from bananachat.db import pulls as pulls_db

    with app.app_context():
        return [row for row in pulls_db.active_jobs() if row["status"] == "queued"]


def test_concurrency_setting_runs_several_downloads_at_once(app, admin, fake_ollama):
    from bananachat import db
    from bananachat.services import background, pulls

    set_policy(app, download_concurrency=2)
    fake_ollama.pull_steps = 30
    fake_ollama.chunk_delay = 0.05
    for name in ("one:1b", "two:1b", "three:1b"):
        admin.post("/admin/models/downloads", {"name": name})
    peak = []

    def watch():
        while runner.is_alive():
            with app.app_context():
                peak.append(db.scalar("SELECT COUNT(*) FROM model_pull_jobs WHERE status='pulling'", default=0))
            time.sleep(0.05)
        db.close_thread_connection()

    pulls._state["recovered"] = True
    runner = threading.Thread(target=lambda: background.jobs()["model-pulls"].function(app))
    runner.start()
    watcher = threading.Thread(target=watch)
    watcher.start()
    runner.join(30)
    watcher.join(5)
    assert max(peak) == 2
    assert [job(app, name)["status"] for name in ("one:1b", "two:1b", "three:1b")] == ["done"] * 3


def test_pausing_a_running_download_keeps_it_for_later(app, admin, fake_ollama):
    fake_ollama.pull_steps = 40
    fake_ollama.chunk_delay = 0.1
    admin.post("/admin/models/downloads", {"name": "paused:1b"})
    row = job(app, "paused:1b")
    worker = threading.Thread(target=run_next, args=(app,), daemon=True)
    worker.start()
    assert wait_for(lambda: (job(app, "paused:1b")["progress_pct"] or 0) > 0)
    admin.post(f"/admin/models/downloads/{row['id']}/pause")
    worker.join(10)
    row = job(app, "paused:1b")
    assert (row["status"], row["paused"]) == ("queued", 1) and "Paused" in row["progress_detail"]
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)
    admin.post(f"/admin/models/downloads/{row['id']}/pause", {"resume": "1"})
    fake_ollama.chunk_delay = 0
    run_next(app)
    assert job(app, "paused:1b")["status"] == "done"


def test_cancel_can_keep_the_partial_download(app, admin, fake_ollama):
    fake_ollama.pull_steps = 40
    fake_ollama.chunk_delay = 0.1
    admin.post("/admin/models/downloads", {"name": "keep:1b"})
    row = job(app, "keep:1b")
    worker = threading.Thread(target=run_next, args=(app,), daemon=True)
    worker.start()
    assert wait_for(lambda: (job(app, "keep:1b")["progress_pct"] or 0) > 0)
    response = admin.post(f"/admin/models/downloads/{row['id']}/cancel", {"keep_partial": "1"}, follow_redirects=True)
    worker.join(10)
    assert "partial download is kept" in response.get_data(as_text=True)
    assert job(app, "keep:1b")["status"] == "cancelled"
    assert not any(path == "/api/delete" for path, _body in fake_ollama.requests)


# ----- pages, permissions and the migration ----------------------------------------------------------------------

def test_pages_render_every_state_and_need_an_administrator(app, admin, fake_ollama, make_user):
    fake_ollama.models = ["llama3.2:3b", "qwen3:4b", "gpt-oss:20b", "gone:1b"]
    fake_ollama.details = {"gpt-oss:20b": GPT_OSS}
    sync(app)
    publish(app, "llama3.2:3b", "qwen3:4b", "gone:1b")
    execute(app, "UPDATE ai_models SET failing_at=?, state_reason='5 failures', recent_failures=5, "
                 "first_failure_at=?, last_failure='boom' WHERE ollama_name='qwen3:4b'", ("2026-01-01 00:00:00",) * 2)
    execute(app, "UPDATE ai_models SET deprecated_at='2026-01-01 00:00:00', retire_at='2999-01-01 00:00:00', "
                 "replacement_id=(SELECT id FROM ai_models WHERE ollama_name='llama3.2:3b') "
                 "WHERE ollama_name='gone:1b'")
    execute(app, "UPDATE ai_models SET missing_at='2026-01-01 00:00:00', missing_reason='Gone for a while', "
                 "backend_available=0 WHERE ollama_name='gone:1b'")
    admin.post("/admin/models/downloads", {"name": "tiny:1b"})
    admin.post("/admin/models/ignore-rules", {"pattern": "old-*"})
    pages = {"/admin/models": ["1 new model", "Needs attention", "Failing", "Missing", "Deprecated", "gpt-oss:20b"],
             "/admin/models/downloads": ["tiny:1b", "Overall progress", "Catalog models no longer on the server"],
             "/admin/models/settings": ["Manual review", "old-*", "Recent changes"]}
    for model_name in ("llama3.2:3b", "qwen3:4b", "gpt-oss:20b", "gone:1b"):
        row = model(app, model_name)
        pages[f"/admin/models/{row['id']}/edit"] = ["Lifecycle", "History"]
        pages[f"/admin/models/{row['id']}/enable"] = ["Enable and publish"]
    for path, expected in pages.items():
        response = admin.get(path)
        text = response.get_data(as_text=True)
        assert response.status_code == 200, path
        for snippet in expected:
            assert snippet in text, (path, snippet)
        assert "style=" not in text and "<script>" not in text
    user = Browser(app)
    make_user("plain")
    user.login("plain")
    pin_csrf(user)
    for path in ("/admin/models/settings", "/admin/models/downloads"):
        assert user.get(path).status_code in (302, 403, 404)
    assert user.post("/admin/models/settings", {"enrollment": "automatic"}).status_code in (302, 403, 404)
    assert user.post("/admin/models/ignore-rules", {"pattern": "x*"}).status_code in (302, 403, 404)
    no_token = admin.client.post("/admin/models/ignore-rules", data={"pattern": "y*"})
    assert no_token.status_code in (400, 403)
    assert one(app, "SELECT COUNT(*) AS n FROM model_ignore_rules")["n"] == 1


def test_settings_are_validated(app, admin):
    base = {"enrollment": "manual", "missing_syncs": "3", "missing_minutes": "30", "retention_days": "30",
            "failure_threshold": "5", "failure_window_minutes": "30", "recheck_minutes": "10",
            "download_concurrency": "1", "download_retries": "3", "stall_minutes": "10"}
    for field, value in (("enrollment", "sometimes"), ("download_concurrency", "9"), ("missing_syncs", "0"),
                         ("failure_threshold", "x")):
        response = admin.post("/admin/models/settings", {**base, field: value}, follow_redirects=True)
        assert "alert-error" in response.get_data(as_text=True) or "must be" in response.get_data(as_text=True) \
            or "valid" in response.get_data(as_text=True)
    from bananachat.services import model_lifecycle
    with app.app_context():
        assert model_lifecycle.policy()["download_concurrency"] == 1
    admin.post("/admin/models/settings", {**base, "download_concurrency": "2", "retention_days": "7"})
    with app.app_context():
        current = model_lifecycle.policy()
        assert (current["download_concurrency"], current["retention_days"]) == (2, 7)
        # Garbage in the stored document falls back to the defaults.
        from bananachat import db
        db.execute("UPDATE model_lifecycle_policy SET config='{\"missing_syncs\": \"many\", \"enrollment\": 5}'")
        assert model_lifecycle.policy()["missing_syncs"] == 3 and model_lifecycle.policy()["enrollment"] == "manual"
    assert one(app, "SELECT 1 FROM audit_log WHERE action='admin.model_settings'")


def test_the_lifecycle_job_runs_every_step(app, fake_ollama):
    from bananachat.services import background

    sync(app)
    publish(app, "llama3.2:3b", "qwen3:4b")
    execute(app, "UPDATE ai_models SET deprecated_at='2026-01-01 00:00:00', retire_at='2000-01-01 00:00:00' "
                 "WHERE ollama_name='qwen3:4b'")
    execute(app, "UPDATE ai_models SET failing_at='2026-01-01 00:00:00', recheck_at='2000-01-01 00:00:00' "
                 "WHERE ollama_name='llama3.2:3b'")
    with ctx(app):
        background.jobs()["model-lifecycle"].function(app)
    assert model(app, "qwen3:4b")["retired_at"] and model(app, "llama3.2:3b")["failing_at"] is None


def test_the_migration_can_run_again_and_removes_duplicate_downloads(tmp_path):
    import importlib

    from bananachat import db

    v10_model_lifecycle = importlib.import_module("bananachat.db.migrations.v10_model_lifecycle")

    path = tmp_path / "application.db"
    db.configure(path)
    db.init_db()
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX idx_pull_jobs_one_active")
    for key in ("a", "b"):
        connection.execute("INSERT INTO model_pull_jobs (ollama_name, backend, status, idempotency_key, created_at) "
                           "VALUES ('dup:1b', 'ollama', 'queued', ?, '2026-01-01 00:00:00')", (key,))
    v10_model_lifecycle.upgrade(connection)
    v10_model_lifecycle.upgrade(connection)
    connection.commit()
    statuses = [row[0] for row in connection.execute("SELECT status FROM model_pull_jobs ORDER BY id")]
    assert statuses == ["queued", "cancelled"]
    assert connection.execute("SELECT config FROM model_lifecycle_policy").fetchone()[0] == "{}"
    connection.close()
    db.close_thread_connection()

