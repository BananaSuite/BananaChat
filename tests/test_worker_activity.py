"""Worker daemon units: activity sensing, settings, HTTPS enforcement, transport and the job loop.

Idle and GPU readings are parsed from captured tool output rather than a live
desktop session, so these cover parsing, classification and fallbacks, not
the platform APIs themselves.
"""

import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker import activity, config, daemon, transport
from worker.client import ServerError

IOREG_SAMPLE = """
  +-o IOHIDSystem  <class IOHIDSystem, id 0x100000282, registered, matched>
      {
        "HIDIdleTime" = 4500000000
        "HIDPointerAcceleration" = 49152
      }
"""


def settings(**changes):
    values = {"BC_SERVER_URL": "https://chat.example.org", "BC_WORKER_TOKEN": "bcw_token"}
    values.update(changes)
    return config.from_environ(values)


# ----- readings ---------------------------------------------------------------------------------

def test_macos_idle_time_is_read_in_nanoseconds():
    assert activity.parse_ioreg_idle(IOREG_SAMPLE) == 4.5
    assert activity.parse_ioreg_idle("nothing useful here") is None
    assert activity.parse_ioreg_idle(None) is None


def test_dbus_idle_replies_are_parsed():
    reply = "method return time=1.2 sender=:1.5 -> destination=:1.9 serial=5 reply_serial=2\n   uint64 12345\n"
    assert activity.parse_dbus_integer(reply) == 12345
    assert activity.parse_dbus_integer("uint32 7") == 7
    assert activity.parse_dbus_integer("Error org.freedesktop.DBus") is None


def test_nvidia_smi_utilisation_is_weighted_by_memory():
    assert activity.parse_smi_utilisation("8192, 100\n24576, 0\n") == 25.0
    assert activity.parse_smi_utilisation("garbage") is None
    assert activity.parse_smi_utilisation("") is None


def test_readings_degrade_instead_of_raising(monkeypatch):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(activity.subprocess, "run", missing)
    monkeypatch.setattr(activity, "_nvml", {"ready": False})
    monkeypatch.setattr(activity, "_OS", "Linux")
    assert activity.get_user_idle_seconds() is None
    assert activity.get_gpu_utilisation() is None
    assert activity.get_gpu_name() is None


def test_macos_uses_the_mac_idle_source_and_has_no_gpu_reading(monkeypatch):
    monkeypatch.setattr(activity, "_OS", "Darwin")
    monkeypatch.setattr(activity, "_idle_seconds_linux", lambda: pytest.fail("Linux probes on macOS"))
    monkeypatch.setattr(activity, "_idle_seconds_macos", lambda: 12.0)
    assert activity.get_user_idle_seconds() == 12.0
    assert activity.get_gpu_utilisation() is None


def test_apple_silicon_reports_its_chip_name(monkeypatch):
    monkeypatch.setattr(activity, "_OS", "Darwin")
    monkeypatch.setattr(activity.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(activity._gpu_name_macos, "_cached", activity._UNSET, raising=False)
    monkeypatch.setattr(activity.subprocess, "run",
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="Apple M2 Pro\n"))
    assert activity.get_gpu_name() == "Apple M2 Pro"


# ----- classification -----------------------------------------------------------------------------

@pytest.mark.parametrize("gpu, idle, expected", [
    (85.0, 10_000.0, "gaming"),       # the GPU gate wins
    (70.0, None, "gaming"),           # the threshold itself counts
    (None, 400.0, "idle"),
    (10.0, 400.0, "idle"),
    (40.0, 400.0, "light"),           # away, but the GPU is not calm
    (None, 60.0, "light"),
    (None, 2.0, "active"),            # someone at the keyboard (e.g. a Mac)
    (None, None, "idle"),             # nothing measurable
    (20.0, None, "idle"),
    (60.0, None, "active"),
])
def test_activity_states_use_the_documented_thresholds(gpu, idle, expected):
    assert activity.classify(gpu, idle, settings()) == expected


def test_own_inference_is_not_mistaken_for_gaming():
    current = settings()
    assert activity.classify(95.0, 600.0, current, own_job=True) == "idle"
    assert activity.classify(95.0, 2.0, current, own_job=True) == "gaming"
    assert activity.classify(95.0, None, current, own_job=True) == "idle"


def test_thresholds_are_configurable():
    custom = settings(BC_GPU_GAMING_THRESHOLD="90", BC_IDLE_THRESHOLD="60", BC_LIGHT_THRESHOLD="10")
    assert activity.classify(85.0, None, custom) == "active"
    assert activity.classify(None, 61.0, custom) == "idle"
    assert activity.classify(None, 11.0, custom) == "light"


def test_monitor_reports_changes():
    readings = {"gpu": 5.0, "idle": 1000.0}
    monitor = activity.Monitor(settings(), gpu_reader=lambda: readings["gpu"], idle_reader=lambda: readings["idle"])
    assert monitor.sample() == "idle"
    readings.update(gpu=90.0, idle=1.0)
    assert monitor.sample() == "gaming" and monitor.gpu == 90.0


# ----- settings -------------------------------------------------------------------------------------

def test_environment_file_fills_gaps_without_overriding_exports(tmp_path):
    env_file = tmp_path / "worker.env"
    env_file.write_text("# a comment\n\nBC_SERVER_URL=https://from-file.example.com\n"
                        'BC_WORKER_TOKEN="quoted-token"\n'
                        "export BC_WORKER_NAME='single-quoted'\nthis line is malformed\n")
    environ = {"BC_SERVER_URL": "https://exported.example.com", "BC_WORKER_ENV_FILE": str(env_file)}
    loaded = config.load(environ)
    assert loaded.env_file == str(env_file)
    assert loaded.server_url == "https://exported.example.com"
    assert loaded.token == "quoted-token" and loaded.name == "single-quoted"


def test_a_missing_environment_file_is_not_an_error(tmp_path):
    assert config.load_env_file(str(tmp_path / "absent.env"), {}) is None


def test_invalid_numbers_fall_back_with_warnings():
    loaded = settings(BC_OLLAMA_IDLE_TIMEOUT="ten", BC_HEARTBEAT_INTERVAL="500", BC_WORKER_LOG_LEVEL="loud")
    assert loaded.ollama_idle_timeout == 600
    assert loaded.heartbeat_interval == 30.0
    assert loaded.log_level == "INFO"
    assert len(loaded.warnings) == 3
    assert loaded.first_token_timeout == 180


@pytest.mark.parametrize("url, allow, ok", [
    ("https://chat.example.org", "", True),
    ("https://chat.example.org/base", "", True),
    ("http://chat.example.org", "", False),
    ("http://chat.example.org", "1", True),
    ("http://127.0.0.1:8000", "", True),
    ("http://localhost:8000", "", True),
    ("http://[::1]:8000", "", True),
    ("https://user:secret@chat.example.org", "", False),
    ("https://chat.example.org/?x=1", "", False),
    ("ftp://chat.example.org", "", False),
])
def test_server_address_must_be_https_unless_loopback_or_allowed(url, allow, ok):
    problems = settings(BC_SERVER_URL=url, BC_WORKER_ALLOW_HTTP=allow).problems()
    assert (not problems) is ok, problems


def test_token_is_required():
    assert any("BC_WORKER_TOKEN" in problem for problem in settings(BC_WORKER_TOKEN="").problems())


def test_managed_ollama_listens_on_the_configured_port():
    """The old worker ignored non-default ports when it started ``ollama serve``."""
    assert settings(BC_OLLAMA_HOST="http://127.0.0.1:11500").ollama_listen_address == "127.0.0.1:11500"
    assert settings().ollama_listen_address == "127.0.0.1:11434"
    assert settings(BC_OLLAMA_HOST="http://[::1]:9000").ollama_listen_address == "[::1]:9000"
    assert settings().ollama_is_local and not settings(BC_OLLAMA_HOST="http://10.0.0.5:11434").ollama_is_local


def test_log_file_is_rotated_and_a_bad_path_falls_back_to_the_console(tmp_path):
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "worker" / "bananachat_worker.py"
    spec = importlib.util.spec_from_file_location("worker_entry_for_test", script)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        entry.setup_logging(SimpleNamespace(log_level="INFO", log_file=str(tmp_path / "logs" / "worker.log"),
                                            log_max_bytes=65536, log_backups=2))
        handler = root.handlers[0]
        assert handler.maxBytes == 65536 and handler.backupCount == 2
        logging.getLogger("worker-log-test").info("recorded")
        handler.flush()
        assert "recorded" in (tmp_path / "logs" / "worker.log").read_text()
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        entry.setup_logging(SimpleNamespace(log_level="INFO", log_file=str(blocker / "nested.log"),
                                            log_max_bytes=65536, log_backups=2))
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved


# ----- transport -------------------------------------------------------------------------------------

@pytest.fixture
def http_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.seen.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"data": "x" * 5000}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_redirects_are_not_followed_and_responses_are_bounded(http_server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    url = f"http://127.0.0.1:{http_server.server_port}"
    with pytest.raises(transport.TransportError) as redirect:
        transport.request_json("GET", url, "/redirect", headers={"Authorization": "Bearer secret"})
    assert redirect.value.status == 302
    assert http_server.seen == [("/redirect", "Bearer secret")]  # no second request anywhere
    with pytest.raises(transport.TransportError, match="larger than allowed"):
        transport.request_json("GET", url, "/big", max_bytes=1000)
    assert transport.request_json("GET", url, "/big")[0] == 200  # and no environment proxy was used


# ----- job loop ----------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, stop_after=None):
        self.chunks, self.completed, self.failed, self.heartbeats = [], [], [], []
        self.stop_after = stop_after

    def chunk(self, job_id, seq, content, done):
        self.chunks.append((seq, content, done))
        return self.stop_after is None or len(self.chunks) <= self.stop_after

    def complete(self, job_id, tokens_in, tokens_out, finish_reason):
        self.completed.append((tokens_in, tokens_out, finish_reason))
        return True

    def fail(self, job_id, error, requeue=False):
        self.failed.append((error, requeue))

    def heartbeat(self, payload):
        self.heartbeats.append(payload)
        return {"ok": True, "job_stop": False}


class FakeOllama:
    managed_pid = None

    def __init__(self, pieces, *, before_first=None):
        self.pieces = pieces
        self.before_first = before_first
        self.calls = []

    def ensure_running(self):
        return True

    def chat(self, model, messages, options, *, first_token_timeout, read_timeout, total_timeout, cancel, on_open):
        self.calls.append({"model": model, "first_token_timeout": first_token_timeout, "options": options})
        if self.before_first:
            self.before_first()
        for index, piece in enumerate(self.pieces):
            if cancel.is_set():
                from worker.local_ollama import Cancelled
                raise Cancelled()
            done = index == len(self.pieces) - 1
            yield piece, done, ({"prompt_tokens": 5, "completion_tokens": 3, "finish_reason": "stop"} if done else {})


def make_worker(client, ollama, state="idle"):
    monitor = SimpleNamespace(state=state, gpu=None, own_job=False)
    priority = SimpleNamespace(apply=lambda *args: None)
    return daemon.Worker(settings(), client=client, ollama=ollama, monitor=monitor, priority=priority)


JOB = {"job_id": "f" * 32, "model": "llama3", "messages": [{"role": "user", "content": "hi"}], "options": None,
       "priority": 2, "first_token_timeout": 240}


def test_a_job_is_streamed_in_order_and_completed():
    client = FakeClient()
    ollama = FakeOllama(["Hel", "lo", ""])
    worker = make_worker(client, ollama)
    assert worker.run_job(dict(JOB)) == "completed"
    text = "".join(content for _seq, content, _done in client.chunks)
    assert text == "Hello"
    assert [seq for seq, _c, _d in client.chunks] == list(range(len(client.chunks)))
    assert client.chunks[-1] == (len(client.chunks) - 1, "", True)
    assert client.completed == [(5, 3, "stop")] and not client.failed
    assert ollama.calls[0]["first_token_timeout"] == 240  # the server's cold-load allowance


def test_gaming_before_the_first_token_hands_the_job_back():
    client = FakeClient()
    worker = make_worker(client, None)
    worker.ollama = FakeOllama(["never sent"], before_first=lambda: worker._activity_changed("gaming"))
    assert worker.run_job(dict(JOB)) == "deferred"
    assert client.failed == [("", True)] and client.chunks == []


def test_server_stop_ends_the_job_without_a_report():
    client = FakeClient(stop_after=1)
    worker = make_worker(client, FakeOllama(["a" * 5000, "b" * 5000, "c", ""]))
    assert worker.run_job(dict(JOB)) == "stopped"
    assert not client.failed and not client.completed


def test_shutdown_before_any_text_requeues():
    client = FakeClient()
    worker = make_worker(client, None)
    worker.ollama = FakeOllama(["x"], before_first=worker.request_stop)
    assert worker.run_job(dict(JOB)) == "deferred"
    assert client.failed == [("", True)]


def test_ollama_failure_is_reported():
    class Broken(FakeOllama):
        def chat(self, *args, **kwargs):
            from worker.local_ollama import OllamaError
            raise OllamaError("model 'llama3' not found")
            yield  # pragma: no cover

    client = FakeClient()
    assert make_worker(client, Broken([])).run_job(dict(JOB)) == "failed"
    assert client.failed == [("model 'llama3' not found", False)]


def test_heartbeat_payload_matches_the_protocol():
    client = FakeClient()
    worker = make_worker(client, FakeOllama([]), state="gaming")
    worker.models = ["llama3:latest"]
    worker.send_heartbeat()
    payload = client.heartbeats[-1]
    assert payload["status"] == "busy" and payload["activity_state"] == "gaming"
    assert payload["capabilities"] == {"models": ["llama3:latest"]}
    assert set(payload) >= {"gpu_name", "gpu_util", "ollama_version"}


def test_the_client_uses_the_worker_api(http_server):
    from worker.client import Client

    client = Client(settings(BC_SERVER_URL=f"http://127.0.0.1:{http_server.server_port}"))
    with pytest.raises(ServerError):
        client.poll(["m"])  # the stub answers a non-job object
    path, auth = http_server.seen[-1]
    assert path == "/worker/v1/jobs/poll?models=m" and auth == "Bearer bcw_token"
