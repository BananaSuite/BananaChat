"""The real worker daemon against the real server over HTTP, with an imitation Ollama on the worker side."""

from __future__ import annotations

import threading

import pytest
from werkzeug.serving import make_server

from tests.app.test_workers import MODEL, generate, register, text_of
from tests.app.fake_ollama import FakeOllama


@pytest.fixture(autouse=True)
def _fresh_routing_state():
    from bananachat.services import remote

    remote._cooldown.clear()
    yield
    remote._cooldown.clear()


@pytest.fixture
def served(app, make_app):
    pool = make_app(workers_enabled=1, worker_claim_timeout=5)
    server = make_server("127.0.0.1", 0, pool, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield pool, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def worker_ollama():
    ollama = FakeOllama()
    ollama.reply = "Answer computed on the volunteer PC."
    ollama.start()
    yield ollama
    ollama.stop()


def test_daemon_serves_a_chat_request(served, worker_ollama, fake_ollama, make_user):
    from types import SimpleNamespace

    from worker import config, daemon

    app, url = served
    _worker_id, token = register(app)
    settings = config.from_environ({"BC_SERVER_URL": url, "BC_WORKER_TOKEN": token, "BC_WORKER_NAME": "test pc",
                                    "BC_OLLAMA_HOST": worker_ollama.url})
    assert settings.problems() == []  # http:// is accepted for loopback
    monitor = SimpleNamespace(state="idle", gpu=12.0, own_job=False)
    worker = daemon.Worker(settings, monitor=monitor, priority=SimpleNamespace(apply=lambda *args: None))
    worker.refresh_models()
    assert MODEL in worker.models
    worker.send_heartbeat()
    outcome = {}

    def serve_one():
        job = worker.client.poll(worker.models)
        outcome["result"] = worker.run_job(job)

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()
    events = generate(app, make_user("ivy"))
    thread.join(20)
    assert outcome == {"result": "completed"}
    assert text_of(events) == "Answer computed on the volunteer PC."
    finished = events[-1]
    assert finished.state == "completed" and finished.via_worker and not finished.usage_estimated
    assert fake_ollama.chat_bodies() == []  # the server's own Ollama was not used
    [body] = worker_ollama.chat_bodies()
    assert body["messages"] == [{"role": "user", "content": "Hi there"}]


def test_daemon_is_told_when_workers_are_switched_off(make_app, worker_ollama):
    from werkzeug.serving import make_server as serve

    from worker import config
    from worker.client import Client, ServerError

    app = make_app(workers_enabled=0)
    server = serve("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _worker_id, token = register(app)
        client = Client(config.from_environ({"BC_SERVER_URL": f"http://127.0.0.1:{server.server_port}",
                                             "BC_WORKER_TOKEN": token}))
        with pytest.raises(ServerError) as refused:
            client.heartbeat({"status": "online"})
        assert refused.value.status == 503 and "disabled" in str(refused.value)
    finally:
        server.shutdown()
