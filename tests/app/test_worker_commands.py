"""Model downloads and removals on worker PCs, and the reasoning relay of newer daemons."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests.app.fake_ollama import FakeOllama
from tests.app.fixtures import Browser
from tests.app.test_workers import MODEL, Worker, register

FEATURES = {"features": ["think", "model_commands"], "version": "1.7.3"}


@pytest.fixture(autouse=True)
def _fresh_routing_state():
    from bananachat.services import remote

    remote._cooldown.clear()
    yield
    remote._cooldown.clear()


@pytest.fixture
def pool(make_app):
    return make_app(workers_enabled=1, worker_claim_timeout=5)


def _report(worker, command_id, **body):
    return worker.client.post(f"/worker/v1/commands/{command_id}", data=json.dumps(body), headers=worker.headers)


def _command(app, command_id):
    from bananachat.db import worker_remote as store

    with app.app_context():
        return dict(store.command(command_id))


# ----- the server side ----------------------------------------------------------------------------------------

def test_a_download_is_handed_to_the_worker_once_and_follows_its_reports(pool):
    from bananachat.services import worker_commands

    worker_id, token = register(pool)
    worker = Worker(pool, token)
    worker.heartbeat()  # an older daemon: no features reported
    with pool.app_context():
        with pytest.raises(worker_commands.CommandError, match="update its worker daemon"):
            worker_commands.queue(worker_id, "pull", "qwen3:8b", None)
    assert worker.heartbeat(system=FEATURES).get_json()["commands"] == []
    with pool.app_context():
        command_id = worker_commands.queue(worker_id, "pull", "qwen3:8b", None)
        with pytest.raises(worker_commands.CommandError, match="already has"):
            worker_commands.queue(worker_id, "pull", "qwen3:8b", None)
        with pytest.raises(worker_commands.CommandError, match="does not have"):
            worker_commands.queue(worker_id, "delete", "mistral:7b", None)
        with pytest.raises(worker_commands.CommandError):
            worker_commands.queue(worker_id, "pull", "evil.example/owner/model", None)
    reply = worker.heartbeat(system=FEATURES).get_json()
    assert reply["commands"] == [{"id": command_id, "kind": "pull", "model": "qwen3:8b"}]
    assert worker.heartbeat(system=FEATURES, commands_running=[command_id]).get_json()["commands"] == []

    progress = _report(worker, command_id, status="running", percent=40, detail="downloading\x1b[2J: 400 MB",
                       completed=400, total=1000)
    assert progress.get_json() == {"ok": True, "cancel": False}
    row = _command(pool, command_id)
    assert row["status"] == "running" and row["progress_pct"] == 40 and row["bytes_total"] == 1000
    assert "\x1b" not in row["detail"]
    assert _report(worker, command_id, status="done", detail="Downloaded.").get_json() == {"ok": True,
                                                                                            "cancel": False}
    row = _command(pool, command_id)
    assert row["status"] == "done" and row["progress_pct"] == 100
    assert _report(worker, command_id, status="running", percent=50).get_json()["cancel"] is True


def test_reports_are_validated_and_belong_to_their_worker(pool):
    from bananachat.services import worker_commands

    worker_id, token = register(pool, "A")
    _other_id, other_token = register(pool, "B")
    worker, other = Worker(pool, token), Worker(pool, other_token)
    worker.heartbeat(system=FEATURES)
    with pool.app_context():
        command_id = worker_commands.queue(worker_id, "delete", MODEL, None)
    worker.heartbeat(system=FEATURES)
    assert _report(other, command_id, status="done").status_code == 404
    assert _report(worker, command_id, status="exploded").status_code == 400
    assert _report(worker, command_id, status="running", percent=float("nan")).status_code == 400
    assert _report(worker, command_id, status="running", completed=-1).status_code == 400
    assert _report(worker, "f" * 32, status="done").status_code == 404


def test_an_administrator_cancels_a_running_download(pool):
    from bananachat.services import worker_commands

    worker_id, token = register(pool)
    worker = Worker(pool, token)
    worker.heartbeat(system=FEATURES)
    admin = Browser(pool)
    admin.login("admin", "admin-password")
    queued = admin.post_json(f"/admin/workers/{worker_id}/commands", {"kind": "pull", "model": "qwen3:8b"})
    assert queued.status_code == 201, queued.get_data(as_text=True)
    command_id = queued.get_json()["id"]
    worker.heartbeat(system=FEATURES)
    cancelled = admin.post_json(f"/admin/workers/commands/{command_id}/cancel", {})
    assert cancelled.get_json() == {"ok": True}
    assert _report(worker, command_id, status="running", percent=10).get_json() == {"ok": False, "cancel": True}
    assert _command(pool, command_id)["status"] == "cancelled"
    retried = admin.post_json(f"/admin/workers/commands/{command_id}/retry", {})
    assert retried.status_code == 200 and retried.get_json()["id"] != command_id
    with pool.app_context():
        assert [row["status"] for row in worker_commands.overview(worker_id)][:2] == ["queued", "cancelled"]
    page = admin.get(f"/admin/workers/{worker_id}")
    assert page.status_code == 200 and "qwen3:8b" in page.get_data(as_text=True)


def test_nothing_stays_queued_or_running_for_ever(pool):
    from bananachat import db
    from bananachat.services import worker_commands

    worker_id, token = register(pool)
    worker = Worker(pool, token)
    worker.heartbeat(system=FEATURES)
    with pool.app_context():
        lost = worker_commands.queue(worker_id, "pull", "qwen3:8b", None)
    worker.heartbeat(system=FEATURES)
    # A restarted worker lists no running command: after a short grace the command fails with a reason.
    worker.heartbeat(system=FEATURES, commands_running=[])
    assert _command(pool, lost)["status"] == "running"
    with pool.app_context():
        db.execute("UPDATE worker_commands SET started_at=started_at-? WHERE id=?",
                   (worker_commands.LOST_SECONDS + 5, lost))
    worker.heartbeat(system=FEATURES, commands_running=[])
    row = _command(pool, lost)
    assert row["status"] == "failed" and "restarted" in row["error"]

    with pool.app_context():
        stalled = worker_commands.queue(worker_id, "pull", "qwen3:4b", None)
    worker.heartbeat(system=FEATURES)
    with pool.app_context():
        db.execute("UPDATE worker_commands SET updated_at=? WHERE id=?",
                   (time.time() - worker_commands.STALL_SECONDS - 5, stalled))
        waiting = worker_commands.queue(worker_id, "pull", "gemma3:1b", None)
        db.execute("UPDATE worker_nodes SET last_heartbeat=? WHERE id=?", (time.time() - 3600, worker_id))
        assert worker_commands.maintain()["failed"] == 1
    assert _command(pool, stalled)["status"] == "failed"
    row = _command(pool, waiting)
    assert row["status"] == "queued" and row["detail"] == "Waiting for the worker to come online."
    with pool.app_context():
        db.execute("UPDATE worker_commands SET created_at=? WHERE id=?",
                   (time.time() - worker_commands.QUEUE_SECONDS - 5, waiting))
        worker_commands.maintain()
    assert _command(pool, waiting)["status"] == "failed"


def test_bulk_downloads_go_to_every_online_worker_that_manages_its_models(pool):
    from bananachat.db import worker_remote as store

    first_id, first_token = register(pool, "First")
    _second_id, second_token = register(pool, "Second")
    Worker(pool, first_token).heartbeat(system=FEATURES)
    Worker(pool, second_token).heartbeat()  # an older daemon
    admin = Browser(pool)
    admin.login("admin", "admin-password")
    response = admin.post("/admin/workers/commands", {"model": "qwen3:8b"})
    assert response.status_code == 302
    with pool.app_context():
        rows = store.commands()
    assert [(row["worker_id"], row["model_name"]) for row in rows] == [(first_id, "qwen3:8b")]


# ----- reasoning ----------------------------------------------------------------------------------------------

def test_reasoning_jobs_wait_for_a_daemon_that_relays_the_reasoning(pool):
    from bananachat import db
    from bananachat.db import workers as store
    from bananachat.services import remote

    old_id, old_token = register(pool, "Old")
    new_id, new_token = register(pool, "New")
    Worker(pool, old_token).heartbeat()
    Worker(pool, new_token).heartbeat(system=FEATURES)
    with pool.app_context():
        with db.transaction():
            store.insert_job("a" * 32, MODEL, json.dumps([{"role": "user", "content": "Why?"}]), None, 0,
                             time.time(), think=json.dumps("high"))
        assert remote.claim(store.get(old_id), [MODEL]) is None
        job = remote.claim(store.get(new_id), [MODEL])
        assert job["job_id"] == "a" * 32 and job["think"] == "high"
        assert remote.should_route({"role": "user", "id": "u"}, {"name": MODEL, "backend": "ollama"}, think=True) \
            is False  # the only daemon that relays reasoning is busy now


def test_the_reasoning_a_worker_sends_reaches_the_answer(pool):
    from bananachat.services import remote

    worker_id, token = register(pool)
    worker = Worker(pool, token)
    worker.heartbeat(system=FEATURES)
    events = []

    def answer():
        polled = worker.poll().get_json()
        job_id = polled["job_id"]
        assert polled["think"] is True
        worker.client.post(f"/worker/v1/jobs/{job_id}/chunk", headers=worker.headers,
                           data=json.dumps({"seq": 0, "content": "", "thinking": "Let me think. ", "done": False}))
        worker.client.post(f"/worker/v1/jobs/{job_id}/chunk", headers=worker.headers,
                           data=json.dumps({"seq": 1, "content": "42", "done": True}))
        worker.complete(job_id)

    thread = threading.Thread(target=answer, daemon=True)
    with pool.app_context():
        from bananachat.services.upstream import CancelToken

        stream = remote.stream(MODEL, [{"role": "user", "content": "Why?"}], None, cancel=CancelToken(), priority=0,
                               first_token_timeout=10, total_timeout=30, think=True)
        thread.start()
        for chunk in stream:
            events.append((chunk.thinking, chunk.content))
    thread.join(10)
    assert "".join(thinking for thinking, _ in events) == "Let me think. "
    assert "".join(content for _, content in events) == "42"


# ----- the worker side ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, ok", [
    ("llama3.2:3b", True), ("hf.co/owner/repo:Q4_K_M", True), ("registry.ollama.ai/library/qwen3", True),
    ("evil.example/owner/model", False), ("../etc", False), ("a//b", False), ("", False), (None, False),
    ("x" * 400, False),
])
def test_the_worker_accepts_only_library_and_hugging_face_names(name, ok):
    from worker.model_commands import valid_name

    assert valid_name(name) is ok


class Reports:
    def __init__(self, cancel_after=None):
        self.items = []
        self.cancel_after = cancel_after

    def command_report(self, command_id, body):
        self.items.append(body)
        return {"ok": True, "cancel": self.cancel_after is not None and len(self.items) > self.cancel_after}


def _commands(ollama_url, reports, **settings):
    from worker.local_ollama import LocalOllama
    from worker.model_commands import ModelCommands

    values = {"model_management": True, "min_free_gb": 0, "ollama_host": ollama_url, "ollama_is_local": False}
    values.update(settings)
    config = SimpleNamespace(**values)
    return ModelCommands(config, reports, LocalOllama(config))


def _wait(commands, timeout=10):
    deadline = time.monotonic() + timeout
    while commands.running_ids() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert commands.running_ids() == []


def test_the_worker_downloads_and_removes_models_on_request(monkeypatch):
    from worker import model_commands

    monkeypatch.setattr(model_commands, "REPORT_SECONDS", 0)
    ollama = FakeOllama().start()
    try:
        reports = Reports()
        changed = []
        commands = _commands(ollama.url, reports)
        commands.on_change = lambda: changed.append(True)
        commands.handle([{"id": "1" * 32, "kind": "pull", "model": "gemma3:1b"}])
        commands.handle([{"id": "1" * 32, "kind": "pull", "model": "gemma3:1b"}])  # the same command: ignored
        _wait(commands)
        assert "gemma3:1b" in ollama.models and reports.items[-1]["status"] == "done"
        assert any(item.get("percent", 0) > 0 for item in reports.items[:-1])
        commands.handle([{"id": "2" * 32, "kind": "delete", "model": "gemma3:1b"}])
        _wait(commands)
        assert "gemma3:1b" not in ollama.models and reports.items[-1] == {"status": "done", "detail": "Removed."}
        assert changed == [True, True]
        commands.handle([{"id": "3" * 32, "kind": "pull", "model": "evil.example/x/y"}])
        _wait(commands)
        assert reports.items[-1]["status"] == "failed" and "invalid" in reports.items[-1]["error"]
    finally:
        ollama.stop()


def test_the_worker_stops_a_download_the_administrator_cancelled(monkeypatch):
    from worker import model_commands

    monkeypatch.setattr(model_commands, "REPORT_SECONDS", 0)
    ollama = FakeOllama()
    ollama.pull_stall.add("big:70b")
    ollama.start()
    try:
        reports = Reports(cancel_after=2)
        commands = _commands(ollama.url, reports)
        commands.handle([{"id": "4" * 32, "kind": "pull", "model": "big:70b"}])
        _wait(commands)
        assert "big:70b" not in ollama.models
        assert all(item["status"] == "running" for item in reports.items)  # no "done" or "failed" after a cancel
    finally:
        ollama.stop()


def test_the_worker_refuses_downloads_without_free_disk_space_or_when_switched_off(monkeypatch):
    from worker import model_commands, system_info

    monkeypatch.setattr(system_info, "free_disk", lambda: 2 * 1024 ** 3)
    ollama = FakeOllama().start()
    try:
        reports = Reports()
        commands = _commands(ollama.url, reports, min_free_gb=10)
        commands.handle([{"id": "5" * 32, "kind": "pull", "model": "gemma3:1b"}])
        _wait(commands)
        assert reports.items[-1]["status"] == "failed" and "BC_WORKER_MIN_FREE_GB" in reports.items[-1]["error"]
        off = _commands(ollama.url, reports, model_management=False)
        before = len(reports.items)
        off.handle([{"id": "6" * 32, "kind": "pull", "model": "gemma3:1b"}])
        assert off.running_ids() == [] and len(reports.items) == before
        assert model_commands.FEATURE == "model_commands"
    finally:
        ollama.stop()


def test_system_information_is_plain_numbers():
    from worker import system_info

    info = system_info.collect(["think"], "1.7.3")
    assert info["features"] == ["think"] and info["version"] == "1.7.3"
    for key in ("ram_total", "ram_free", "disk_total", "disk_free", "cpu_count"):
        assert info.get(key) is None or isinstance(info[key], int)
