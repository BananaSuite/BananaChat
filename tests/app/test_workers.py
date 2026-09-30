"""Remote workers: the worker API, routing, the relay and the administrator page."""

from __future__ import annotations

import json
import threading
import time

import pytest

MODEL = "llama3.2:3b"


# ----- helpers --------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_routing_state():
    from bananachat.services import remote

    remote._cooldown.clear()
    yield
    remote._cooldown.clear()


@pytest.fixture
def pool_app(app, make_app):
    # ``app`` is created first so fixtures bound to it (make_user, admin)
    # share the database of this app, which is configured last.
    return make_app(workers_enabled=1, worker_claim_timeout=2)


def register(app, name="Gaming PC"):
    from bananachat import db
    from bananachat.services import remote

    with app.app_context():
        with db.transaction():
            return remote.register_worker(name)


class Worker:
    """Speaks the wire protocol exactly like the deployed (first-release) daemon."""

    def __init__(self, app, token):
        self.client = app.test_client()
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                        "Accept": "application/json"}

    def heartbeat(self, models=(MODEL,), **extra):
        body = {"status": "online", "gpu_name": "RTX 3080", "ollama_version": "0.9.0",
                "capabilities": {"models": list(models)}, "activity_state": "idle", "gpu_util": 3.0}
        body.update(extra)
        return self.client.post("/worker/v1/heartbeat", data=json.dumps(body), headers=self.headers)

    def poll(self, models=(MODEL,)):
        query = {"models": ",".join(models)} if models else {}
        return self.client.get("/worker/v1/jobs/poll", query_string=query, headers=self.headers)

    def chunk(self, job_id, seq, content, done=False):
        return self.client.post(f"/worker/v1/jobs/{job_id}/chunk", headers=self.headers,
                                data=json.dumps({"seq": seq, "content": content, "done": done}))

    def complete(self, job_id, tokens_in=7, tokens_out=2, finish_reason="stop"):
        return self.client.post(f"/worker/v1/jobs/{job_id}/complete", headers=self.headers,
                                data=json.dumps({"tokens_in": tokens_in, "tokens_out": tokens_out,
                                                 "finish_reason": finish_reason}))

    def fail(self, job_id, body):
        return self.client.post(f"/worker/v1/jobs/{job_id}/fail", headers=self.headers, data=json.dumps(body))


def run_in_thread(target):
    errors = []

    def wrapper():
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - reported by the test
            errors.append(error)

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()
    return thread, errors


def generate(app, user, *, cancel=None, on_delta=None, fallbacks=()):
    from bananachat.db import catalog
    from bananachat.services import inference, ollama
    from bananachat.services.upstream import CancelToken

    cancel = cancel or CancelToken()
    with app.app_context():
        ollama.sync_catalog()
        model = catalog.get_by_name(MODEL)
        request = inference.TextRequest(user=user, model=model, messages=[{"role": "user", "content": "Hi there"}],
                                        options={"num_predict": 64},
                                        fallbacks=[catalog.get_by_name(name) for name in fallbacks])
        events = []
        for event in inference.generate(request, cancel):
            events.append(event)
            if on_delta and isinstance(event, inference.Delta):
                on_delta(event)
        return events


def text_of(events):
    from bananachat.services.inference import Delta
    return "".join(event.text for event in events if isinstance(event, Delta))


def job_rows(app):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query("SELECT * FROM worker_jobs ORDER BY created_at")]


def chunk_count(app):
    from bananachat import db

    with app.app_context():
        return db.scalar("SELECT COUNT(*) FROM worker_job_chunks", default=0)


# ----- end to end ------------------------------------------------------------------------------

def test_chat_request_is_answered_by_a_worker(pool_app, make_user, fake_ollama):
    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    assert worker.heartbeat().get_json() == {"ok": True, "job_stop": False}
    user = make_user("alice")
    seen = {}

    def work():
        response = worker.poll()
        assert response.status_code == 200
        job = response.get_json()
        seen["job"] = job
        assert set(job) >= {"job_id", "model", "messages", "options", "priority"}
        assert worker.chunk(job["job_id"], 0, "Hello ").get_json() == {"ok": True, "stop": False}
        assert worker.chunk(job["job_id"], 1, "from the worker").get_json() == {"ok": True, "stop": False}
        assert worker.chunk(job["job_id"], 2, "", done=True).get_json() == {"ok": True, "stop": False}
        assert worker.complete(job["job_id"]).get_json() == {"ok": True, "stop": False}
        # A retried completion is harmless.
        assert worker.complete(job["job_id"]).get_json()["ok"] is True

    thread, errors = run_in_thread(work)
    events = generate(pool_app, user)
    thread.join(10)
    assert not errors, errors

    from bananachat.services.inference import Finished, Started
    assert events[0].__class__ is Started and events[0].via_worker
    assert text_of(events) == "Hello from the worker"
    finished = events[-1]
    assert isinstance(finished, Finished) and finished.state == "completed" and finished.via_worker
    assert (finished.prompt_tokens, finished.completion_tokens, finished.usage_estimated) == (7, 2, False)
    assert seen["job"]["model"] == MODEL
    assert seen["job"]["messages"] == [{"role": "user", "content": "Hi there"}]
    assert seen["job"]["options"] == {"num_predict": 64}
    assert fake_ollama.chat_bodies() == []
    # Privacy: nothing of the prompt or the answer is left behind.
    [row] = job_rows(pool_app)
    assert row["status"] == "done" and row["messages"] == "[]" and row["options"] is None
    assert chunk_count(pool_app) == 0


def test_unclaimed_job_is_withdrawn_and_answered_locally(pool_app, make_user, fake_ollama):
    _worker_id, token = register(pool_app)
    Worker(pool_app, token).heartbeat()  # advertises the model but never polls
    user = make_user("bob")
    started = time.monotonic()
    events = generate(pool_app, user)
    elapsed = time.monotonic() - started
    assert 2 <= elapsed < 8
    assert text_of(events) == "Hello from the fake model."
    assert events[-1].state == "completed"
    [row] = job_rows(pool_app)
    assert row["status"] == "failed" and row["messages"] == "[]"
    assert len(fake_ollama.chat_bodies()) == 1
    # The model cools down: the next request does not wait for the pool again.
    from bananachat.db import catalog
    from bananachat.services import remote
    with pool_app.app_context():
        assert not remote.should_route(user, catalog.get_by_name(MODEL))


def test_deferred_job_goes_to_another_worker(pool_app, make_user):
    _a, token_a = register(pool_app, "A")
    _b, token_b = register(pool_app, "B")
    first, second = Worker(pool_app, token_a), Worker(pool_app, token_b)
    first.heartbeat()
    second.heartbeat()
    user = make_user("carol")

    def work():
        job = first.poll().get_json()
        # The owner started a game: the old daemon's "deferred" error re-queues.
        assert first.fail(job["job_id"], {"error": "deferred"}).get_json() == {"ok": True, "stop": True}
        again = second.poll().get_json()
        assert again["job_id"] == job["job_id"]
        second.chunk(again["job_id"], 0, "Second worker")
        second.chunk(again["job_id"], 1, "", done=True)
        second.complete(again["job_id"])

    thread, errors = run_in_thread(work)
    events = generate(pool_app, user)
    thread.join(10)
    assert not errors, errors
    assert text_of(events) == "Second worker" and events[-1].state == "completed"


def test_gaming_heartbeat_returns_an_unstarted_job_to_the_pool(pool_app):
    from bananachat.services import remote

    worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    with pool_app.app_context():
        from bananachat.db import workers as store
        store.insert_job("a" * 32, MODEL, "[]", None, 2, time.time())
    job = worker.poll().get_json()
    assert job["job_id"] == "a" * 32
    worker.heartbeat(activity_state="gaming", status="busy")
    [row] = job_rows(pool_app)
    assert row["status"] == "pending" and row["worker_id"] is None
    assert remote.MAX_LONG_POLLS >= 1


def test_stop_reaches_the_worker(pool_app, make_user):
    from bananachat.services.upstream import CancelToken

    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    user = make_user("dave")
    cancel = CancelToken()
    stopped = threading.Event()
    replies = []

    def work():
        job = worker.poll().get_json()
        replies.append(worker.chunk(job["job_id"], 0, "First ").get_json())
        stopped.wait(10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job_rows(pool_app)[0]["status"] == "streaming":
            time.sleep(0.02)  # the relay records the stop on its next pass
        replies.append(worker.chunk(job["job_id"], 1, "second").get_json())

    def on_delta(_event):
        cancel.cancel("stopped")
        stopped.set()

    thread, errors = run_in_thread(work)
    events = generate(pool_app, user, cancel=cancel, on_delta=on_delta)
    thread.join(10)
    assert not errors, errors
    assert events[-1].state == "stopped" and text_of(events) == "First "
    assert replies == [{"ok": True, "stop": False}, {"ok": False, "stop": True}]
    [row] = job_rows(pool_app)
    assert row["status"] == "failed" and row["stop_requested"] == 1 and row["messages"] == "[]"


def test_worker_failure_before_output_falls_back_to_the_local_server(pool_app, make_user, fake_ollama):
    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()

    def work():
        job = worker.poll().get_json()
        worker.fail(job["job_id"], {"error": "model 'llama3.2:3b' not found"})

    thread, errors = run_in_thread(work)
    events = generate(pool_app, make_user("erin"))
    thread.join(10)
    assert not errors, errors
    assert text_of(events) == "Hello from the fake model." and events[-1].state == "completed"


# ----- protocol details -------------------------------------------------------------------------

def _claimed_job(app, token, job_id="b" * 32):
    from bananachat.db import workers as store

    with app.app_context():
        store.insert_job(job_id, MODEL, json.dumps([{"role": "user", "content": "x"}]), None, 2, time.time())
    worker = Worker(app, token)
    worker.heartbeat()
    assert worker.poll().get_json()["job_id"] == job_id
    return worker, job_id


def test_chunks_are_ordered_and_exactly_once(pool_app):
    _worker_id, token = register(pool_app)
    worker, job_id = _claimed_job(pool_app, token)
    assert worker.chunk(job_id, 0, "a").get_json() == {"ok": True, "stop": False}
    # A retry of an accepted chunk (lost reply) is acknowledged, not stored twice.
    assert worker.chunk(job_id, 0, "a").get_json() == {"ok": True, "stop": False}
    assert chunk_count(pool_app) == 1
    # Completing without the terminal chunk fails the job.
    assert worker.complete(job_id).get_json() == {"ok": False, "stop": True}
    assert job_rows(pool_app)[0]["status"] == "failed"


def test_out_of_order_chunk_fails_the_job(pool_app):
    _worker_id, token = register(pool_app)
    worker, job_id = _claimed_job(pool_app, token)
    assert worker.chunk(job_id, 2, "skipped").get_json() == {"ok": False, "stop": True}
    row = job_rows(pool_app)[0]
    assert row["status"] == "failed" and row["messages"] == "[]"


def test_response_budget_ends_the_answer(pool_app):
    _worker_id, token = register(pool_app)
    worker, job_id = _claimed_job(pool_app, token)
    budget = pool_app.config["BC"].chat_max_response_bytes
    piece = "x" * 16 * 1024
    for seq in range(budget // len(piece)):
        assert worker.chunk(job_id, seq, piece).get_json() == {"ok": True, "stop": False}
    assert worker.chunk(job_id, budget // len(piece), "overflow").get_json() == {"ok": True, "stop": True}
    row = job_rows(pool_app)[0]
    assert row["status"] == "done" and row["finish_reason"] == "length" and row["stream_bytes"] == budget


def test_invalid_payloads_are_rejected(pool_app):
    _worker_id, token = register(pool_app)
    worker, job_id = _claimed_job(pool_app, token)
    assert worker.chunk(job_id, -1, "x").status_code == 400
    assert worker.chunk(job_id, 0, "x" * (17 * 1024)).status_code == 400
    assert worker.heartbeat(gpu_util=500).status_code == 400
    assert worker.heartbeat(activity_state="sleeping").status_code == 400
    assert worker.complete("not-a-job").status_code == 404
    assert worker.complete("c" * 32).status_code == 404


def test_poll_answers_204_and_caps_long_polls(pool_app, monkeypatch):
    from bananachat.services import remote

    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 0.3)
    started = time.monotonic()
    assert worker.poll().status_code == 204
    assert time.monotonic() - started >= 0.25
    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 20)
    monkeypatch.setitem(remote._polls, "active", remote.MAX_LONG_POLLS)
    started = time.monotonic()
    assert worker.poll().status_code == 204
    assert time.monotonic() - started < 2


def test_authentication_and_disabled_workers(pool_app, admin):
    worker_id, token = register(pool_app)
    assert Worker(pool_app, "wrong").heartbeat().status_code == 401
    lower = Worker(pool_app, token)
    lower.headers["Authorization"] = f"bearer {token}"
    assert lower.heartbeat().status_code == 200
    admin.post(f"/admin/workers/{worker_id}/state", {"enabled": "0"})
    response = Worker(pool_app, token).heartbeat()
    assert response.status_code == 403 and "disabled" in response.get_json()["error"]


def test_workers_switched_off_on_the_server(make_app, make_user):
    from bananachat.db import catalog
    from bananachat.services import ollama, remote

    app = make_app(workers_enabled=0)
    _worker_id, token = register(app)
    response = Worker(app, token).heartbeat()
    assert response.status_code == 503
    assert response.get_json() == {"error": "Remote workers are disabled on this server."}
    user = make_user("frank")
    with app.app_context():
        ollama.sync_catalog()
        assert not remote.should_route(user, catalog.get_by_name(MODEL))


def test_routing_policy(pool_app, make_user):
    from bananachat.db import catalog
    from bananachat.db import users
    from bananachat.services import ollama, remote

    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    user = make_user("gina")
    with pool_app.app_context():
        ollama.sync_catalog()
        model, other = catalog.get_by_name(MODEL), catalog.get_by_name("qwen3:4b")
        admin_user = users.get_by_username("admin")
    with pool_app.app_context():
        assert not remote.should_route(user, model)  # no heartbeat yet
    worker.heartbeat(models=["llama3.2:3b"])
    with pool_app.app_context():
        assert remote.should_route(user, model)
        assert not remote.should_route(user, other)  # not advertised
        assert not remote.should_route(admin_user, model)  # administrators stay local
        assert not remote.should_route(user, model, request_type="chat_incognito")
        assert not remote.should_route(user, model, think=True)  # reasoning is shown only from this server
        assert remote.should_route(user, model, think=False)
    worker.heartbeat(activity_state="gaming")
    with pool_app.app_context():
        assert not remote.should_route(user, model)
    worker.heartbeat(models=["qwen3"])  # "qwen3" and "qwen3:latest" are the same model
    with pool_app.app_context():
        assert remote.model_aliases("qwen3") == {"qwen3", "qwen3:latest"}
        assert remote.model_aliases("qwen3:latest") == {"qwen3", "qwen3:latest"}
        assert not remote.should_route(user, other)


def test_first_token_timeout_and_heartbeat_lease():
    from bananachat.services import remote

    now = 10_000.0
    cold = {"claimed_at": now - 100, "heartbeat_at": now - 5, "next_chunk_seq": 0}
    assert not remote.lease_expired(cold, now, 60, 180)  # a slow cold load is fine
    assert remote.lease_expired({**cold, "claimed_at": now - 200}, now, 60, 180)
    assert remote.lease_expired({**cold, "heartbeat_at": now - 61}, now, 60, 180)  # the worker went silent
    streaming = {"claimed_at": now - 1000, "heartbeat_at": now - 30, "next_chunk_seq": 5}
    assert not remote.lease_expired(streaming, now, 60, 180)


def test_heartbeat_renews_the_lease_of_the_running_job(pool_app):
    from bananachat import db

    _worker_id, token = register(pool_app)
    worker, job_id = _claimed_job(pool_app, token)
    with pool_app.app_context():
        db.execute("UPDATE worker_jobs SET heartbeat_at=? WHERE id=?", (time.time() - 50, job_id))
    reply = worker.heartbeat(job_id=job_id).get_json()
    assert reply == {"ok": True, "job_stop": False}
    assert job_rows(pool_app)[0]["heartbeat_at"] > time.time() - 5


def test_maintenance_times_out_and_purges_jobs(pool_app):
    from bananachat import db
    from bananachat.db import workers as store
    from bananachat.services import remote

    now = time.time()
    with pool_app.app_context():
        store.insert_job("d" * 32, MODEL, '[{"content": "secret"}]', '{"a": 1}', 2, now - 1000)
        db.execute("UPDATE worker_jobs SET heartbeat_at=? WHERE id=?", (now - 1000, "d" * 32))
        store.insert_job("e" * 32, MODEL, "[]", None, 2, now - 7200)
        store.finish("e" * 32, "done", now - 7200)
        store.insert_chunk("e" * 32, 0, "old", True, now - 7200)
        result = remote.maintain()
        assert result["timed_out"] == 1 and result["purged"] == 1
        stale = store.get_job("d" * 32)
        assert stale["status"] == "timeout" and stale["messages"] == "[]" and stale["options"] is None
        assert store.get_job("e" * 32) is None
        assert db.scalar("SELECT COUNT(*) FROM worker_job_chunks") == 0


def test_the_purge_is_a_background_job(pool_app):
    from bananachat.services import background

    assert "worker-jobs" in background.jobs()


# ----- administrator page ------------------------------------------------------------------------

def test_admin_page_warns_when_workers_are_off(make_app):
    from tests.app.conftest import Browser

    app = make_app()
    browser = Browser(app)
    browser.login("admin", "admin-password")
    page = browser.get("/admin/workers").get_data(as_text=True)
    assert "Remote workers are turned off" in page and "BC_WORKERS_ENABLED=1" in page
    assert "can read the prompts" in page


def test_admin_registers_a_worker_and_sees_the_token_once(pool_app, admin):
    from bananachat.db import users
    from bananachat.db import workers as store

    response = admin.post_json("/admin/workers", {"name": "Den PC"})
    assert response.status_code == 201
    data = response.get_json()
    token = data["token"]
    assert data["worker"]["name"] == "Den PC" and len(token) > 40
    with admin.client.session_transaction() as session:
        assert token not in json.dumps(dict(session))
    with pool_app.app_context():
        row = store.by_token_hash(store.hash_token(token))
        assert row is not None and row["token_hash"] != token
        assert any(entry["action"] == "admin.workers.register" for entry in users.list_audit())
    listing = admin.fetch("/admin/workers/data").get_json()
    assert [worker["name"] for worker in listing["workers"]] == ["Den PC"]
    assert token not in admin.get("/admin/workers").get_data(as_text=True)
    assert admin.post_json("/admin/workers", {"name": " "}).status_code == 400


def test_admin_page_escapes_worker_data(pool_app, admin):
    hostile = '<img src=x onerror=alert(1)>'
    admin.post_json("/admin/workers", {"name": hostile})
    _worker_id, token = register(pool_app, "second")
    Worker(pool_app, token).heartbeat(models=["<script>alert(2)</script>"], gpu_name="<b>gpu</b>")
    page = admin.get("/admin/workers").get_data(as_text=True)
    assert hostile not in page and "<script>alert(2)" not in page and "<b>gpu</b>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "admin-workers.js" in page


def test_admin_form_fallback_disable_enable_and_delete(pool_app, admin):
    from bananachat.db import users
    from bananachat.db import workers as store

    response = admin.post("/admin/workers", {"name": "No-script PC"})
    assert response.status_code == 201
    with pool_app.app_context():
        worker = store.list_all()[0]
    assert "Token for" in response.get_data(as_text=True)
    worker_id = worker["id"]
    assert admin.post(f"/admin/workers/{worker_id}/state", {"enabled": "0"}).status_code == 302
    with pool_app.app_context():
        assert store.get(worker_id)["status"] == "disabled"
    admin.post(f"/admin/workers/{worker_id}/state", {"enabled": "1"})
    with pool_app.app_context():
        assert store.get(worker_id)["status"] == "offline"
    page = admin.get("/admin/workers").get_data(as_text=True)
    assert "data-confirm=" in page and "onsubmit" not in page
    assert admin.post(f"/admin/workers/{worker_id}/delete").status_code == 302
    with pool_app.app_context():
        assert store.get(worker_id) is None
        actions = {entry["action"] for entry in users.list_audit()}
    assert {"admin.workers.disable", "admin.workers.enable", "admin.workers.delete"} <= actions
    assert admin.post(f"/admin/workers/{worker_id}/delete").status_code == 404


def test_admin_pages_require_an_administrator(make_user, browser_for):
    make_user("henry")
    browser = browser_for()
    browser.login("henry")
    assert browser.post_json("/admin/workers", {"name": "x"}).status_code == 403
    assert browser.get("/admin/workers").status_code == 403


def test_no_history_chats_are_never_sent_to_worker_pcs(app, make_user):
    from bananachat.services import remote

    user = make_user("private-person")
    with app.app_context():
        model = {"ollama_name": "llama3.2:3b", "backend_model_name": "llama3.2:3b"}
        config = app.config["BC"].replace(workers_enabled=True)
        assert remote.should_route(user, model, request_type="chat_incognito", config=config) is False
