"""With the real Gunicorn and this repository's configuration: an API answer streaming while its worker process is
replaced or stopped is still charged and metered, and the client is told what happened."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn is a POSIX server")


@pytest.fixture
def server(tmp_path):
    """Start Gunicorn on a fresh instance; yields a starter ``start(args, *, young_workers_recycle)``."""
    pytest.importorskip("gunicorn")
    from tests.app.fake_ollama import FakeOllama

    fake = FakeOllama().start()
    fake.reply = " ".join(["word"] * 3000)
    fake.chunk_delay = 0.02  # about a minute of streaming
    instance = tmp_path / "instance"
    environment = {"BC_INSTANCE_DIR": str(instance), "BC_OLLAMA_URL": fake.url, "BC_ENV": "testing",
                   "BC_SECRET_KEY": "s" * 48, "BC_MIN_FREE_MEMORY_MB": "0", "BC_LOGGING_LEVEL": "minimal"}
    from bananachat import create_app, db, security
    from bananachat.config import load_config
    from bananachat.db import catalog, settings as site_settings, tokens, users
    from bananachat.services import ollama

    app = create_app(load_config(environment), testing=True)
    with app.test_request_context():
        with db.transaction():
            users.create("admin", security.hash_password("admin-password"), role="admin")
            site_settings.update(setup_done=1, site_name="Test")
            user_id = users.create("mallory", security.hash_password("mallory-password"))
        ollama.sync_catalog()
        for model in catalog.list_models():
            catalog.set_rollout(model["id"], True)
        _token_id, raw_token = tokens.create(user_id, "key")
    db.close_thread_connection()
    state = {"process": None, "log": None}

    def start(arguments: str, *, young_workers_recycle: bool = False):
        configuration = ROOT / "gunicorn.conf.py"
        if young_workers_recycle:
            # This test cannot wait an hour for a replacement: let the worker be replaced at once.
            wrapper = tmp_path / "gunicorn.test.conf.py"
            wrapper.write_text(
                "import runpy\n"
                f"_settings = runpy.run_path({str(configuration)!r})\n"
                "globals().update({key: value for key, value in _settings.items() if not key.startswith('__')})\n"
                "pre_request.__globals__['_MIN_WORKER_SECONDS'] = 0\n")
            configuration = wrapper
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = (tmp_path / "gunicorn.log").open("w")
        process = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "-c", str(configuration), "wsgi:app"], cwd=ROOT,
            env={**os.environ, **environment, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT),
                 "GUNICORN_CMD_ARGS": f"--bind 127.0.0.1:{port} --workers 1 {arguments}"},
            stdout=log, stderr=subprocess.STDOUT)
        state.update(process=process, log=log)
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 30
        while True:
            try:
                opener.open(base + "/healthz", timeout=2).read()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("Gunicorn did not start: " + (tmp_path / "gunicorn.log").read_text()[-2000:])
                time.sleep(0.1)
        return base, opener, raw_token, process

    def database():
        return sqlite3.connect(instance / "bananachat.db")

    start.database = database
    start.log = lambda: (tmp_path / "gunicorn.log").read_text()
    yield start
    process = state["process"]
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
    if state["log"] is not None:
        state["log"].close()
    fake.stop()


class Stream(threading.Thread):
    def __init__(self, base, opener, token):
        super().__init__(daemon=True)
        self.request = urllib.request.Request(
            base + "/v1/chat/completions", method="POST",
            data=json.dumps({"model": "llama3.2:3b", "stream": True,
                             "messages": [{"role": "user", "content": "Write"}]}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        self.opener = opener
        self.text: list[str] = []
        self.errors: list[dict] = []
        self.done = False

    def run(self):
        try:
            with self.opener.open(self.request, timeout=300) as response:
                for line in response:
                    if line.startswith(b"data: [DONE]"):
                        self.done = True
                    elif line.startswith(b"data: {"):
                        payload = json.loads(line[6:])
                        if "error" in payload:
                            self.errors.append(payload["error"])
                        for choice in payload.get("choices", []):
                            self.text.append((choice.get("delta") or {}).get("content") or "")
        except OSError:
            pass

    def wait_for_text(self, timeout=30):
        deadline = time.monotonic() + timeout
        while len(self.text) < 10:
            assert time.monotonic() < deadline, "the answer did not stream"
            time.sleep(0.05)


def _charged(start):
    with start.database() as conn:
        return (conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens_out), 0) FROM credit_ledger "
                             "WHERE request_type='api'").fetchone(),
                conn.execute("SELECT status FROM request_metrics WHERE request_type='api'").fetchall(),
                conn.execute("SELECT COUNT(*) FROM completion_leases").fetchone()[0])


def test_an_api_answer_cut_by_a_worker_replacement_is_charged(server):
    base, opener, token, process = server("--max-requests 40 --max-requests-jitter 0 --timeout 30 "
                                          "--graceful-timeout 5", young_workers_recycle=True)
    stream = Stream(base, opener, token)
    stream.start()
    stream.wait_for_text()
    for _ in range(60):  # cheap requests drive the worker to max_requests: it is replaced
        try:
            opener.open(base + "/healthz", timeout=5).read()
        except OSError:
            pass
    stream.join(60)
    assert not stream.is_alive(), server.log()[-3000:]
    assert "Autorestarting worker" in server.log()
    # It was stopped before the master's deadline, told why, and charged for what it produced.
    assert [error["code"] for error in stream.errors] == ["server_restarting"] and stream.done, server.log()[-3000:]
    (rows, tokens_out), statuses, leases = _charged(server)
    assert rows == 1 and tokens_out >= len("".join(stream.text)) // 4 // 2 > 0
    assert statuses == [("stopped",)] and leases == 0
    assert "WORKER TIMEOUT" not in server.log()


def test_young_workers_are_not_replaced_however_many_requests_arrive(server):
    base, opener, token, process = server("--max-requests 40 --max-requests-jitter 0 --timeout 30 "
                                          "--graceful-timeout 5")
    for _ in range(120):
        opener.open(base + "/healthz", timeout=5).read()
    assert "Autorestarting worker" not in server.log()


def test_an_api_answer_cut_by_a_stop_is_charged_before_the_master_kills_the_worker(server):
    base, opener, token, process = server("--timeout 60 --graceful-timeout 20")
    stream = Stream(base, opener, token)
    stream.start()
    stream.wait_for_text()
    process.send_signal(signal.SIGTERM)
    stream.join(40)
    process.wait(40)
    assert not stream.is_alive()
    assert [error["code"] for error in stream.errors] == ["server_restarting"], server.log()[-3000:]
    (rows, tokens_out), statuses, leases = _charged(server)
    assert rows == 1 and tokens_out > 0 and statuses == [("stopped",)] and leases == 0
