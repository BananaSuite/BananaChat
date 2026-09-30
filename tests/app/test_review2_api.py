"""Regression tests for defects found in the second review of the API, images, credits, workers and compute."""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tests.app.test_api import assert_error, call, chat, roll_out, token_for
from tests.app.test_images import MODEL_ID as IMAGE_MODEL
from tests.app.test_images import artist, artist_token, comfy, image_app  # noqa: F401 - fixtures
from tests.app.test_workers import MODEL, Worker, generate, job_rows, register, run_in_thread, text_of


@pytest.fixture(autouse=True)
def _fresh_routing_state():
    from bananachat.services import remote

    remote._cooldown.clear()
    yield
    remote._cooldown.clear()


@pytest.fixture
def pool_app(app, make_app):
    return make_app(workers_enabled=1, worker_claim_timeout=2)


# ----- API -----------------------------------------------------------------------------------------

def test_supported_method_on_a_trailing_slash_path_is_not_a_405(app, make_user):
    roll_out(app)
    token = token_for(app, make_user("dev"))
    # "405 - use GET" in answer to a GET is contradictory: the path simply does not exist.
    assert_error(call(app, "GET", "/v1/models/", token), 404, "invalid_request_error", "unknown_endpoint")
    assert_error(call(app, "POST", "/v1/chat/completions/", token, {}), 404, "invalid_request_error")
    response = call(app, "DELETE", "/v1/models", token)
    assert_error(response, 405, "invalid_request_error", "method_not_allowed")
    assert response.headers["Allow"] == "GET"


def test_image_model_named_for_a_chat_completion_is_unsuitable_not_unavailable(image_app, artist_token):  # noqa: F811
    # /v1/models lists image models; choosing one for chat is a client error.
    # A 503 with Retry-After makes SDKs (openai-python) retry a request that can never work.
    response = chat(image_app, artist_token, model=IMAGE_MODEL)
    assert_error(response, 400, "invalid_request_error", "model_not_suitable")
    assert response.json["error"]["param"] == "model" and "Retry-After" not in response.headers


# ----- worker PCs ----------------------------------------------------------------------------------

def test_worker_that_reported_no_models_is_given_no_jobs(pool_app, monkeypatch):
    from bananachat.db import workers as store
    from bananachat.services import remote

    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 0.3)

    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    # A daemon whose Ollama has no models reports an empty inventory and polls
    # without ?models= (as the first-release daemon does).
    assert worker.heartbeat(models=()).status_code == 200
    with pool_app.app_context():
        store.insert_job("d" * 32, MODEL, json.dumps([{"role": "user", "content": "private"}]), None, 2, time.time())
    assert worker.poll(models=()).status_code == 204
    assert job_rows(pool_app)[0]["status"] == "pending" and job_rows(pool_app)[0]["worker_id"] is None
    # Once it advertises the model it gets the job.
    worker.heartbeat(models=(MODEL,))
    assert worker.poll(models=()).get_json()["job_id"] == "d" * 32


@pytest.mark.parametrize("ending", ["fail", "vanish"])
def test_answer_is_complete_once_the_worker_sent_its_final_chunk(pool_app, make_user, monkeypatch, ending):
    from bananachat.services import remote

    monkeypatch.setattr(remote, "lease_seconds", lambda config=None: 1.0)
    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()

    def work():
        job = worker.poll().get_json()
        worker.chunk(job["job_id"], 0, "The whole answer")
        worker.chunk(job["job_id"], 1, "", done=True)
        # The completion report is lost: the daemon reports that it lost contact
        # with the server, or the PC disappears before reporting anything.
        if ending == "fail":
            worker.fail(job["job_id"], {"error": "The worker lost contact with the server."})

    thread, errors = run_in_thread(work)
    events = generate(pool_app, make_user("fay"))
    thread.join(10)
    assert not errors, errors
    finished = events[-1]
    assert text_of(events) == "The whole answer"
    assert finished.state == "completed" and finished.via_worker and finished.usage_estimated


class _SlowHeaders(BaseHTTPRequestHandler):
    """An Ollama that is still loading a model: no response headers for a while."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.server.delay)
        try:
            payload = b'{"message":{"content":"late"},"done":true}\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass


def test_worker_hands_a_job_back_while_ollama_is_still_loading_the_model():
    from worker import config, daemon

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHeaders)
    server.daemon_threads = True
    server.delay = 6.0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = config.from_environ({"BC_SERVER_URL": "https://chat.example.org", "BC_WORKER_TOKEN": "bcw_x",
                                        "BC_OLLAMA_HOST": f"http://127.0.0.1:{server.server_port}"})
        failed = []
        client = SimpleNamespace(fail=lambda job_id, error, requeue=False: failed.append((error, requeue)),
                                 chunk=lambda *args: True, complete=lambda *args: True,
                                 heartbeat=lambda payload: {"ok": True, "job_stop": False})
        ollama = daemon.LocalOllama(settings)
        ollama.ensure_running = lambda: True
        worker = daemon.Worker(settings, client=client, ollama=ollama,
                               monitor=SimpleNamespace(state="idle", gpu=None, own_job=False),
                               priority=SimpleNamespace(apply=lambda *args: None))
        job = {"job_id": "e" * 32, "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
               "options": None, "first_token_timeout": 60}
        threading.Timer(0.5, worker._activity_changed, args=("gaming",)).start()
        started = time.monotonic()
        assert worker.run_job(job) == "deferred"
        # The owner started gaming: the job goes back at once, not when the cold load ends.
        assert time.monotonic() - started < 3
        assert failed == [("", True)]
    finally:
        server.shutdown()
        server.server_close()


# ----- compute proxy -------------------------------------------------------------------------------

@pytest.fixture
def compute_proxy(tmp_path):
    from compute.inference_proxy import ComputeServer

    maintenance = tmp_path / "maintenance"
    proxy = ComputeServer(("127.0.0.1", 0), upstream="http://127.0.0.1:9", token="x" * 64,
                          maintenance=str(maintenance))
    threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    proxy.maintenance_file = maintenance
    yield proxy
    proxy.shutdown()
    proxy.server_close()


def _post_large(proxy, token, path="/api/chat", size=4 * 1024 * 1024):
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=10)
    try:
        connection.request("POST", path, body=b" " * size,
                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_compute_proxy_answers_large_requests_it_refuses_instead_of_resetting_them(compute_proxy):
    # A chat with images is several MB; the web server must see the proxy's
    # answer (maintenance, wrong token...) rather than a broken connection.
    compute_proxy.maintenance_file.write_text("updating")
    status, body = _post_large(compute_proxy, "x" * 64)
    assert status == 503 and "Maintenance" in body["error"]
    status, _body = _post_large(compute_proxy, "y" * 64)
    assert status == 401
    status, _body = _post_large(compute_proxy, "x" * 64, path="/api/unknown")
    assert status == 404


def test_compute_proxy_busy_answer_survives_a_large_body(tmp_path):
    from compute.inference_proxy import ComputeServer

    proxy = ComputeServer(("127.0.0.1", 0), upstream="http://127.0.0.1:9", token="x" * 64, max_connections=1)
    threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert proxy.slots.acquire(blocking=False)  # every slot is in use
        status, body = _post_large(proxy, "x" * 64)
        assert status == 503 and "busy" in body["error"]
    finally:
        proxy.slots.release()
        proxy.shutdown()
        proxy.server_close()
