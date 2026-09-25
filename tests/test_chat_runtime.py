"""Exercise request races, failure atomicity, disconnections and worker loss."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import tracemalloc

import pytest

_environment = tempfile.TemporaryDirectory()
os.environ.setdefault("BC_ENV", "testing")
os.environ.setdefault("BC_INSTANCE_DIR", _environment.name)
os.environ.setdefault("BC_DATABASE_PATH", str(Path(_environment.name) / "runtime.db"))
os.environ.setdefault("BC_LOGGING_LEVEL", "off")

from app import app
import config
import db
from db import _chat_runs as runs
from db._runtime import StreamBusyError
from services import chat_execution, dispatcher, ollama, queue as q


@pytest.fixture
def runtime_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setattr(config, "INSTANCE_DIR", str(tmp_path))
    db.init_db()
    db.update_site_settings(setup_done=1)
    user_id = db.create_user("runtime-user", "unused", role="user")
    user = db.get_user_by_id(user_id)
    db.upsert_model("fixture:latest", "Fixture")
    model = db.get_model_by_ollama_name("fixture:latest")
    db.update_model(model["id"], is_rolled_out=1)
    monkeypatch.setattr(dispatcher, "should_use_worker", lambda *_: False)
    monkeypatch.setattr(ollama, "list_available_models", lambda: [{"name": "fixture:latest"}])
    monkeypatch.setattr(ollama, "list_running_models", lambda: [])
    monkeypatch.setattr(q, "_has_enough_memory", lambda: True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    session = db.create_session(user_id)
    tasks = []
    original = chat_execution.ChatExecution.start
    def start(task):
        tasks.append(task)
        original(task)
    monkeypatch.setattr(chat_execution.ChatExecution, "start", start)
    yield app, user, session, model, tasks
    for task in tasks:
        task.stop_event.set()
        task.thread.join(timeout=6)
        assert not task.thread.is_alive(), "Test left a generation running against its temporary database"


def _client(application, user):
    client = application.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user["id"]
        session["language"] = "en"
    return client


def _send(client, session):
    return client.post(f"/chat/{session}/send", json={"content": "A real request", "model": "fixture:latest"}, buffered=False)


def _events(response):
    return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]


def test_concurrent_sends_do_not_replace_ownership_or_duplicate_input(runtime_app, monkeypatch):
    application, user, session, _, tasks = runtime_app
    release = threading.Event()
    def backend(*_args, **_kwargs):
        assert release.wait(5)
        yield "One response", True, {"prompt_tokens": 3, "completion_tokens": 2}
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: _send(_client(application, user), session), range(8)))
        assert sorted(response.status_code for response in responses) == [200] + [409] * 7
        assert len(db.list_messages(session)) == 1
        assert len(tasks) == 1
        release.set()
        accepted = next(response for response in responses if response.status_code == 200)
        assert _events(accepted)[-1]["state"] == "completed"
        assert [message["role"] for message in db.list_messages(session)] == ["user", "assistant"]
        with db.get_db_context() as conn:
            assert conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)).fetchone()[0] == 1
        assert q.get_stats()["total"] == 0
    finally:
        release.set()


def test_overload_is_rejected_before_history_or_task_creation(runtime_app, monkeypatch):
    application, user, session, _, tasks = runtime_app
    monkeypatch.setattr(q, "MAX_QUEUE_DEPTH", 1)
    with q.acquire():
        response = _send(_client(application, user), session)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "5"
        assert db.list_messages(session) == []
        assert not tasks and not db.is_active_stream(session)


def test_queued_request_can_be_stopped_before_inference(runtime_app, monkeypatch):
    application, user, session, _, _ = runtime_app
    monkeypatch.setattr(q, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(ollama, "generate_chat_stream", lambda *_a, **_k: pytest.fail("Stopped work must not reach inference"))
    client = _client(application, user)
    with q.acquire():
        response = _send(client, session)
        assert response.status_code == 200
        assert client.post(f"/chat/{session}/stop").status_code == 200
        outcome = _events(response)[-1]
        assert outcome["state"] == "stopped" and outcome["message_id"] is None
        assert runs.get_chat_run_status(session)["state"] == "stopped"
        assert len(db.list_messages(session)) == 1


def test_disconnected_browser_does_not_retain_events_or_lose_completed_reply(runtime_app, monkeypatch):
    application, user, session, _, tasks = runtime_app
    entered, release = threading.Event(), threading.Event()
    def backend(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        for _ in range(1000):
            yield "x" * 100, False, {}
        yield "", True, {"prompt_tokens": 5, "completion_tokens": 25000}
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    try:
        response = _send(_client(application, user), session)
        assert entered.wait(5)
        response.close()
        release.set()
        tasks[0].thread.join(timeout=5)
        assert not tasks[0].thread.is_alive()
        assert tasks[0].channel._bytes == 0
        status = runs.get_chat_run_status(session)
        assert status["state"] == "completed"
        assert status["last_message"]["content"] == "x" * 100000
    finally:
        release.set()


def test_slow_transport_stays_bounded_and_tells_reader_to_reconnect():
    channel = chat_execution.EventChannel()
    tracemalloc.start()
    try:
        for _ in range(10000):
            channel.send({"type": "delta", "content": "x" * 4096})
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 4 * 1024 * 1024
    finally:
        tracemalloc.stop()
    assert "Reload this chat" in "".join(channel.stream())


def test_partial_backend_failure_is_preserved_and_never_marked_complete(runtime_app, monkeypatch):
    application, user, session, _, _ = runtime_app
    def backend(*_args, **_kwargs):
        yield "Unfinished but useful", False, {}
        raise OSError("private backend diagnostic")
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    response = _send(_client(application, user), session)
    outcome = _events(response)[-1]
    assert outcome["type"] == "error" and outcome["state"] == "failed"
    assert "private backend diagnostic" not in json.dumps(outcome)
    message = db.list_messages(session)[-1]
    assert message["content"] == "Unfinished but useful" and message["generation_state"] == "failed"


def test_response_size_limit_saves_a_stopped_reply(runtime_app, monkeypatch):
    application, user, session, _, _ = runtime_app
    monkeypatch.setattr(config, "CHAT_MAX_RESPONSE_BYTES", 1000)
    def backend(*_args, **_kwargs):
        for _ in range(100):
            yield "é" * 400, False, {}
        pytest.fail("The generator should have been closed when its byte budget ended")
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    outcome = _events(_send(_client(application, user), session))[-1]
    message = db.list_messages(session)[-1]
    assert outcome["state"] == "stopped"
    assert len(message["content"].encode("utf-8")) == 1000


def _checkpoint_then_wait(path, session, user, model_id, ready):
    config.DATABASE_PATH = path
    token = runs.begin_chat_run(session, user, "Before process loss")
    runs.checkpoint_chat_run(session, token, "Saved before process loss", model_id)
    ready.put(token)
    threading.Event().wait(30)


def test_worker_death_recovers_once_and_fences_old_completion(runtime_app):
    _, user, session, model, _ = runtime_app
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    process = ctx.Process(target=_checkpoint_then_wait, args=(config.DATABASE_PATH, session, dict(user), model["id"], ready))
    try:
        process.start()
        token = ready.get(timeout=5)
        process.kill()
        process.join(timeout=5)
        assert process.exitcode != 0
        with db.get_db_context() as conn:
            conn.execute("UPDATE active_streams SET heartbeat_at=0 WHERE session_id=?", (session,))
            conn.commit()
        for _ in range(3):
            status = runs.get_chat_run_status(session)
            assert status["state"] == "interrupted"
        assert status["last_message"]["content"] == "Saved before process loss"
        assert len(db.list_messages(session)) == 2
        with pytest.raises(StreamBusyError):
            runs.finish_chat_run(session, token, "Late result", "completed")
        assert len(db.list_messages(session)) == 2
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        ready.close()
        ready.join_thread()


def test_message_usage_and_outcome_roll_back_together(runtime_app, monkeypatch):
    _, user, session, model, _ = runtime_app
    token = runs.begin_chat_run(session, user, "Keep input")
    runs.checkpoint_chat_run(session, token, "Keep checkpoint", model["id"])
    def fail_usage(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated disk full")
    monkeypatch.setattr(runs, "deduct_credits", fail_usage)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        runs.finish_chat_run(session, token, "New reply", "completed", model_id=model["id"])
    assert len(db.list_messages(session)) == 1
    assert db.is_active_stream(session)
    assert db.get_stream_partial_content(session) == "Keep checkpoint"
    assert runs.get_chat_run_status(session)["state"] == "running"


def test_waiting_poll_can_read_while_an_unrelated_writer_holds_sqlite(runtime_app, monkeypatch):
    monkeypatch.setattr(q, "MAX_CONCURRENT", 1)
    monkeypatch.setenv("BC_DB_BUSY_TIMEOUT_MS", "100")
    with q.acquire():
        waiting = q.acquire()
        try:
            with closing(db.get_db()) as writer:
                writer.execute("BEGIN IMMEDIATE")
                assert q._try_acquire(waiting._entry.req_id) is False
                writer.rollback()
        finally:
            waiting.close()


def test_context_keeps_recent_history_with_a_fixed_text_budget(runtime_app, monkeypatch):
    _, _, session, _, _ = runtime_app
    for number in range(30):
        db.add_message(session, "user" if number % 2 == 0 else "assistant", f"{number:02}" + "x" * 98)
    monkeypatch.setattr(config, "CHAT_MAX_CONTEXT_CHARS", 500)
    messages, attachments = runs.load_chat_context(session)
    assert sum(len(row["content"]) for row in messages) <= 500
    assert messages[-1]["content"].startswith("29")
    assert len(db.list_messages(session)) == 30 and not attachments
