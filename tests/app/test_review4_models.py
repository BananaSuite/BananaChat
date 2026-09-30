"""Review of the model lifecycle: races between processes and background jobs, restart safety, state
transitions, outages and downloads. Everything runs against the imitation Ollama server."""

from __future__ import annotations

import threading
import time

from tests.app.test_chat import new_chat, parse_sse, read_events, send, user_browser
from tests.app.test_model_lifecycle import (  # noqa: F401  (fixtures)
    _hold_request,
    admin,
    events,
    execute,
    job,
    lenient,
    model,
    one,
    publish,
    run_next,
    sync,
    wait_for,
)


def _user_id(app, username):
    return one(app, "SELECT id FROM users WHERE username=?", (username,))["id"]


# ----- a model removed or deleted while a request uses it ------------------------------------------------------

def test_removing_a_model_from_the_catalog_while_a_chat_streams_from_it_is_refused(app, admin, fake_ollama,
                                                                                    make_user):
    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    alice = user_browser(app, make_user)
    fake_ollama.reply = "one two " + "word " * 40  # longer than a checkpoint of the running answer
    fake_ollama.chunk_delay = 0.1
    session_id = new_chat(alice)
    response = send(alice, session_id, model="qwen3:4b", buffered=False)
    first = read_events(response, until=lambda event: event.get("type") == "delta")
    assert first[-1]["type"] == "delta"
    qwen = model(app, "qwen3:4b")
    page = admin.post(f"/admin/models/{qwen['id']}/remove", follow_redirects=True).get_data(as_text=True)
    assert "request is using this model right now" in page
    rest = read_events(response)
    assert rest[-1]["type"] == "done" and rest[-1]["state"] == "completed"
    stored = one(app, "SELECT content, model_id FROM chat_messages WHERE session_id=? AND role='assistant'",
                 (session_id,))
    assert stored["content"].startswith("one two") and stored["model_id"] == qwen["id"]
    # Once idle it can be removed.
    admin.post(f"/admin/models/{qwen['id']}/remove")
    assert model(app, "qwen3:4b") is None


def test_a_model_waiting_to_be_deleted_is_not_downloaded_at_the_same_time(app, admin, fake_ollama):
    from bananachat.services import model_lifecycle

    sync(app)
    qwen = model(app, "qwen3:4b")
    _hold_request(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/delete-server", {"when_idle": "1"})
    assert model(app, "qwen3:4b")["delete_requested_at"]
    page = admin.post("/admin/models/downloads", {"name": "qwen3:4b"}, follow_redirects=True).get_data(as_text=True)
    assert "being deleted" in page and job(app, "qwen3:4b") is None
    bulk = admin.post("/admin/models/downloads/bulk", {"names": "qwen3:4b\nfresh:1b"},
                      follow_redirects=True).get_data(as_text=True)
    assert "being deleted" in bulk and job(app, "qwen3:4b") is None and job(app, "fresh:1b")["status"] == "queued"

    # A download queued before the deletion was asked for keeps the deletion waiting.
    execute(app, "DELETE FROM inference_queue")
    admin.post(f"/admin/models/{qwen['id']}/lifecycle", {"action": "cancel_delete"})
    admin.post("/admin/models/downloads", {"name": "qwen3:4b"})
    execute(app, "UPDATE ai_models SET delete_requested_at=? WHERE ollama_name='qwen3:4b'", ("2026-01-01 00:00:00",))
    with app.test_request_context():
        assert model_lifecycle.process_pending_deletes() == 0
    assert "qwen3:4b" in fake_ollama.models and fake_ollama.deleted == []


# ----- failing: only failures that are the model's fault -----------------------------------------------------------

def _generate(app, name, user):
    from tests.app.test_model_lifecycle import _generate as generate

    return generate(app, name, user)


def test_a_model_removed_on_the_server_is_missing_not_failing_and_returns_as_it_was(app, fake_ollama, make_user,
                                                                                       lenient):
    from bananachat.services import model_lifecycle
    from bananachat.services.upstream import UpstreamError

    assert not model_lifecycle.counts_as_failure(UpstreamError("model 'qwen3:4b' not found", 404))
    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    user = make_user("gone-user")
    fake_ollama.models.remove("qwen3:4b")  # removed on the compute server by hand (ollama rm)
    for _ in range(6):
        assert _generate(app, "qwen3:4b", user)[-1].state == "failed"
    row = model(app, "qwen3:4b")
    assert row["failing_at"] is None and row["recent_failures"] == 0
    for _ in range(3):
        sync(app)
    assert model(app, "qwen3:4b")["missing_at"]
    fake_ollama.models.append("qwen3:4b")  # downloaded again
    sync(app)
    row = model(app, "qwen3:4b")
    assert row["missing_at"] is None and row["failing_at"] is None and row["is_rolled_out"] == 1
    assert "failing" not in events(app, "qwen3:4b")


# ----- downloads ----------------------------------------------------------------------------------------------

def test_downloads_wait_while_the_compute_server_is_down_instead_of_using_up_their_retries(make_app, fake_ollama):
    from bananachat.db import pulls as pulls_db
    from bananachat.services import health, pulls

    remote = make_app(INFERENCE_LOCAL="0")
    with remote.app_context():
        first = pulls.enqueue_ollama("tiny:1b", None)
        health.record_probe(False, 1, "unreachable")
        try:
            assert pulls.process_next(remote.config["BC"]) is False
            assert pulls_db.get(first)["status"] == "queued" and fake_ollama.pull_attempts == {}
            # A transient error seen while the server is down does not use up an attempt either.
            job_row = pulls_db.claim_next(1)
            pulls._retry_or_fail(job_row, pulls.DownloadProblem("connection refused", transient=True))
            row = pulls_db.get(first)
            assert row["status"] == "queued" and row["attempts"] == 0 and row["next_attempt_at"]
        finally:
            health.reset()
        execute(remote, "UPDATE model_pull_jobs SET next_attempt_at='2000-01-01 00:00:00'")
        assert pulls.process_next(remote.config["BC"]) is True
        assert pulls_db.get(first)["status"] == "done"


class _Agent:
    def __init__(self):
        self.cancelled = []

    def cancel_download(self, remote_id):
        self.cancelled.append(remote_id)


def _waiting_checkpoint(app, remote_id="remote-1"):
    from bananachat.db import pulls as pulls_db

    with app.app_context():
        job_id = pulls_db.enqueue("model.safetensors", None, backend="comfyui", repo_id="owner/repo",
                                  source_filename="model.safetensors", revision="main",
                                  target_name="model.safetensors", expected_sha256="a" * 64)
    # Paused (or interrupted by a restart) after the agent started it: queued again, the agent keeps its job.
    execute(app, "UPDATE model_pull_jobs SET remote_job_id=?, paused=1 WHERE id=?", (remote_id, job_id))
    return job_id


def test_cancelling_a_waiting_checkpoint_download_cancels_it_on_the_agent(app, admin, monkeypatch):
    from bananachat.services import checkpoint_agent

    agent = _Agent()
    monkeypatch.setattr(checkpoint_agent, "client", lambda config=None: agent)
    job_id = _waiting_checkpoint(app)
    admin.post(f"/admin/models/downloads/{job_id}/cancel")
    assert one(app, "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))["status"] == "cancelled"
    assert agent.cancelled == ["remote-1"]
    # Keeping the partial download leaves the agent's job alone, as for a running download.
    kept = _waiting_checkpoint(app, "remote-2")
    admin.post(f"/admin/models/downloads/{kept}/cancel", {"keep_partial": "1"})
    assert agent.cancelled == ["remote-1"]


# ----- background jobs ----------------------------------------------------------------------------------------

def test_a_slow_test_prompt_does_not_hold_up_the_other_background_jobs(app, fake_ollama, monkeypatch):
    """The re-test of a failing model may wait minutes for an answer; the health probe and the sync must not."""
    import dataclasses

    from bananachat.services import background

    sync(app)
    execute(app, "UPDATE ai_models SET failing_at='2026-01-01 00:00:00', recheck_at='2000-01-01 00:00:00' "
                 "WHERE ollama_name='qwen3:4b'")
    fake_ollama.generate_delay = 4.0
    ticks = []
    lifecycle = background.jobs()["model-lifecycle"]
    monkeypatch.setattr(background, "_jobs", {
        "model-lifecycle": dataclasses.replace(lifecycle, initial_delay=0),
        "inference-health": background.Job("inference-health", 1, lambda _app: ticks.append(time.monotonic()),
                                           initial_delay=0)})
    background.start(app)
    try:
        assert wait_for(lambda: any(path == "/api/generate" for path, _body in fake_ollama.requests), 5)
        time.sleep(2.5)
        assert len(ticks) >= 2
    finally:
        background.stop(timeout=10)
        background._stop.clear()  # later tests run long jobs by hand, which check background.stopping()


# ----- admin pages ---------------------------------------------------------------------------------------------

def test_a_deletion_that_waits_for_no_request_says_when_it_happens(app, admin, fake_ollama):
    sync(app)
    qwen = model(app, "qwen3:4b")
    _hold_request(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/delete-server", {"when_idle": "1"})
    page = admin.get(f"/admin/models/{qwen['id']}/edit").get_data(as_text=True)
    assert "waiting for 1 running request to finish" in page
    execute(app, "DELETE FROM inference_queue")
    page = admin.get(f"/admin/models/{qwen['id']}/edit").get_data(as_text=True)
    assert "waiting for 0 running requests" not in page and "deleted within a minute" in page


def test_a_deletion_cancelled_while_the_job_checks_it_is_not_carried_out(app, admin, fake_ollama, monkeypatch):
    from bananachat.services import model_lifecycle

    sync(app)
    qwen = model(app, "qwen3:4b")
    _hold_request(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/delete-server", {"when_idle": "1"})
    execute(app, "DELETE FROM inference_queue")

    def cancelled_meanwhile(row):  # the administrator chooses "Keep it" (another process) during the check
        model_lifecycle.cancel_delete(model_lifecycle.catalog.get(row["id"]))
        return 0

    monkeypatch.setattr(model_lifecycle, "in_use", cancelled_meanwhile)
    with app.test_request_context():
        assert model_lifecycle.process_pending_deletes() == 0
    assert fake_ollama.deleted == [] and model(app, "qwen3:4b")["backend_available"] == 1


# ----- round 2 -------------------------------------------------------------------------------------------------

def _waiting_request(app, name="other:1b"):
    import uuid

    execute(app, "INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, heartbeat_at, "
                 "owner_key, model_name) VALUES (?, 3, 'waiting', 1, ?, ?, 'user:y', ?)",
            (uuid.uuid4().hex, time.time(), time.time(), name))


def test_timeouts_count_only_when_the_system_is_not_overloaded(app, fake_ollama):
    from bananachat.services import model_lifecycle
    from bananachat.services.upstream import UpstreamError

    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    timeout = UpstreamError("The backend stopped responding.", kind="timeout")
    qwen, llama = model(app, "qwen3:4b"), model(app, "llama3.2:3b")
    with app.test_request_context():
        # Requests waiting for a slot: the queue is saturated, a slow answer says nothing about the model.
        _waiting_request(app)
        for _ in range(6):
            model_lifecycle.record_failure(qwen, timeout)
        assert model(app, "qwen3:4b")["recent_failures"] == 0
        execute(app, "DELETE FROM inference_queue")
        # Alone and with room in the queue, a timeout counts.
        model_lifecycle.record_failure(qwen, timeout)
        assert model(app, "qwen3:4b")["recent_failures"] == 1
        # Another model timing out at the same time points at the server, not at the model.
        model_lifecycle.record_failure(llama, timeout)
        model_lifecycle.record_failure(qwen, timeout)
        assert model(app, "llama3.2:3b")["recent_failures"] == 0 and model(app, "qwen3:4b")["recent_failures"] == 1
        # Errors that are not timeouts are unaffected by load.
        _waiting_request(app)
        model_lifecycle.record_failure(llama, UpstreamError("model failed to load", 500))
        assert model(app, "llama3.2:3b")["recent_failures"] == 1


def test_a_stale_download_thread_cannot_change_a_job_another_leader_claimed(app, fake_ollama):
    from bananachat.db import pulls as pulls_db
    from bananachat.services import pulls
    from bananachat.services.upstream import CancelToken

    with app.app_context():
        job_id = pulls.enqueue_ollama("tiny:1b", None)
        old = pulls_db.claim_next(1)
        assert old["id"] == job_id and old["claim_token"]
        # The leader died (or its stop timed out); the next leader re-queues and claims the job again.
        assert pulls_db.reset_stuck() == 1
        new = pulls_db.claim_next(1)
        assert new["id"] == job_id and new["claim_token"] != old["claim_token"]
        # What the old thread does on its way out changes nothing.
        assert pulls_db.requeue(job_id, owner=old["claim_token"]) is False
        assert pulls_db.finish(job_id, "failed", "stale", owner=old["claim_token"]) is False
        assert pulls_db.schedule_retry(job_id, 1, db_now(), "x", "y", owner=old["claim_token"]) is False
        pulls_db.update_progress(job_id, 77, "stale", owner=old["claim_token"])
        row = pulls_db.get(job_id)
        assert (row["status"], row["progress_pct"], row["claim_token"]) == ("pulling", 0, new["claim_token"])
        # The old thread's watcher notices it lost the job and stops its download.
        token, finished = CancelToken(), threading.Event()
        watcher = threading.Thread(target=pulls._watch, args=(old, token, finished, lambda: False,
                                                             {"at": time.monotonic()}))
        watcher.start()
        watcher.join(5)
        finished.set()
        assert token.cancelled and token.reason == pulls.GONE
        assert pulls_db.finish(job_id, "done", owner=new["claim_token"]) is True


def db_now():
    from bananachat import db

    return db.now()


def test_delete_now_updates_only_that_model_without_a_catalog_sync(app, admin, fake_ollama, monkeypatch):
    from bananachat.db import personalities
    from bananachat.services import model_lifecycle

    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    admin_id = _user_id(app, "admin")
    with app.app_context():
        personality = personalities.create(admin_id, "Pirate", "Arr.", created_by=admin_id, preferred_model="qwen3:4b")
    fake_ollama.requests.clear()
    qwen = model(app, "qwen3:4b")
    real_sync, syncs = model_lifecycle.sync, []
    monkeypatch.setattr(model_lifecycle, "sync",
                        lambda *args, **kwargs: syncs.append(kwargs) or real_sync(*args, **kwargs))
    page = admin.post(f"/admin/models/{qwen['id']}/delete-server", follow_redirects=True).get_data(as_text=True)
    assert "was deleted from the Ollama server" in page and syncs == []
    assert [path for path, _body in fake_ollama.requests if path != "/api/ps"] == ["/api/delete"]
    row = model(app, "qwen3:4b")
    assert row["backend_available"] == 0 and row["missing_at"] and row["delete_requested_at"] is None
    with app.app_context():
        assert personalities.get(personality)["preferred_model"] == ""
    assert events(app, "qwen3:4b")[-1] == "deleted"
    monkeypatch.setattr(model_lifecycle, "sync", real_sync)
    sync(app)  # the background sync reconciles later and changes nothing more
    assert model(app, "qwen3:4b")["missing_at"] == row["missing_at"]


def test_an_ignored_model_is_published_only_after_it_is_restored(app, admin, fake_ollama):
    sync(app)
    qwen = model(app, "qwen3:4b")
    admin.post(f"/admin/models/{qwen['id']}/ignore")
    refused = admin.post(f"/admin/models/{qwen['id']}/rollout", {"enabled": "1"}, follow_redirects=True)
    assert "Restore it" in refused.get_data(as_text=True)
    row = model(app, "qwen3:4b")
    assert row["enrollment"] == "ignored" and row["is_rolled_out"] == 0
    form = admin.get(f"/admin/models/{qwen['id']}/enable").get_data(as_text=True)
    assert "is on the ignore list" in form
    enabled = admin.post(f"/admin/models/{qwen['id']}/enable", {"display_name": "Qwen", "access_mode": "everyone",
                                                                 "limit_preset": "light"}, follow_redirects=True)
    assert "Restore it" in enabled.get_data(as_text=True) and model(app, "qwen3:4b")["is_rolled_out"] == 0
    assert one(app, "SELECT 1 FROM model_ignore_rules WHERE pattern='qwen3:4b'")
    admin.post(f"/admin/models/{qwen['id']}/restore")
    admin.post(f"/admin/models/{qwen['id']}/rollout", {"enabled": "1"})
    row = model(app, "qwen3:4b")
    assert row["enrollment"] == "reviewed" and row["is_rolled_out"] == 1


def test_unreviewed_models_are_never_chosen_automatically_even_for_administrators(app, fake_ollama):
    from bananachat.db import users
    from bananachat.services import inference
    from bananachat.services.access import AccessContext

    fake_ollama.running = ["qwen3:4b"]  # loaded in memory, so it would sort first
    sync(app)
    publish(app, "llama3.2:3b")
    with app.test_request_context():
        admin_user = users.get_by_username("admin")
        context = AccessContext.load(admin_user)
        auto = inference.select_model(context, "auto", surface="chat")
        assert auto.model["ollama_name"] == "llama3.2:3b" and all(
            item["enrollment"] != "new" for item in auto.fallbacks)
        explicit = inference.select_model(context, "qwen3:4b", surface="api", strict=True)
        assert explicit.model["ollama_name"] == "qwen3:4b"


def test_an_api_request_for_a_model_that_vanished_gets_model_not_found(app, fake_ollama, make_user):
    from bananachat.db import tokens

    sync(app)
    publish(app, "qwen3:4b", "llama3.2:3b")
    make_user("bob")
    with app.app_context():
        token = tokens.create(_user_id(app, "bob"), "script")[1]
    fake_ollama.models.remove("qwen3:4b")  # deleted between the model check and the request reaching Ollama
    client = app.test_client()
    for stream in (False, True):
        response = client.post("/v1/chat/completions", json={
            "model": "qwen3:4b", "stream": stream, "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {token}"})
        error = response.get_json()["error"]
        assert response.status_code == 404 and error["code"] == "model_not_found", response.get_data(as_text=True)
        assert error["message"] == "The model 'qwen3:4b' is no longer available on the model server."


def test_the_download_claim_migration_can_run_again(tmp_path):
    import importlib
    import sqlite3

    v12 = importlib.import_module("bananachat.db.migrations.v12_pull_claims")
    connection = sqlite3.connect(tmp_path / "claims.db")
    connection.execute("CREATE TABLE model_pull_jobs (id INTEGER PRIMARY KEY, status TEXT)")
    v12.upgrade(connection)
    v12.upgrade(connection)
    assert "claim_token" in [row[1] for row in connection.execute("PRAGMA table_info(model_pull_jobs)")]
    connection.close()


def test_administrators_still_see_unreviewed_models_in_their_pickers_marked_as_new(app, admin, fake_ollama,
                                                                                    make_user):
    import json

    from bananachat.db import tokens

    sync(app)
    publish(app, "llama3.2:3b")  # qwen3:4b waits for review
    page = admin.get(f"/chat/{new_chat(admin)}", headers={"Accept-Language": "en"}).get_data(as_text=True)
    models = {item["name"]: item for item in
              json.loads(page.split('id="page-data">')[1].split("</script>")[0])["models"]}
    assert "qwen3:4b" in models and models["qwen3:4b"]["description"].startswith("New — not reviewed")
    assert not models["llama3.2:3b"]["description"].startswith("New")
    playground = admin.get("/developer/playground", headers={"Accept-Language": "en"}).get_data(as_text=True)
    data = json.loads(playground.split('id="page-data">')[1].split("</script>")[0])
    assert any(item["id"] == "qwen3:4b" and "new — not reviewed" in item["name"] for item in data["models"])
    assert "New — not reviewed" in admin.get("/developer", headers={"Accept-Language": "en"}).get_data(as_text=True)
    with app.app_context():
        admin_token = tokens.create(_user_id(app, "admin"), "admin script")[1]
        make_user("carol")
        user_token = tokens.create(_user_id(app, "carol"), "script")[1]
    client = app.test_client()
    listed = client.get("/v1/models", headers={"Authorization": f"Bearer {admin_token}"}).get_json()["data"]
    assert "qwen3:4b" in [item["id"] for item in listed]
    assert client.get("/v1/models/qwen3:4b", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200
    others = client.get("/v1/models", headers={"Authorization": f"Bearer {user_token}"}).get_json()["data"]
    assert "qwen3:4b" not in [item["id"] for item in others]
    # Listing is not choosing: auto still passes it over.
    auto = client.post("/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "Hi"}]},
                       headers={"Authorization": f"Bearer {admin_token}"}).get_json()
    assert auto["model"] == "llama3.2:3b"
