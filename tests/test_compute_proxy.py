"""The compute gateway (``python -m compute.inference_proxy``) in front of an imitation Ollama."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from compute import inference_proxy
from compute.inference_proxy import ComputeServer

TOKEN = "x" * 64


class Ollama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        if self.path == "/api/chat" and self.server.slow.is_set():
            time.sleep(1.0)
        payload = b'{"message":{"content":"hello"},"done":false}\n{"done":true}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


def start(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
    server.daemon_threads = True
    server.calls = []
    server.slow = threading.Event()
    start(server)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def make_proxy(backend, tmp_path):
    created = []

    def factory(**options):
        options.setdefault("maintenance", str(tmp_path / "maintenance"))
        proxy = ComputeServer(("127.0.0.1", 0), upstream=f"http://127.0.0.1:{backend.server_port}", token=TOKEN,
                              **options)
        start(proxy)
        created.append(proxy)
        return proxy

    yield factory
    for proxy in created:
        proxy.shutdown()
        proxy.server_close()


@pytest.fixture
def proxy(make_proxy):
    return make_proxy()


def call(proxy, path, token=TOKEN, method="GET", body=None, headers=None, scheme="Bearer"):
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"{scheme} {token}"
    connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=sent)
    response = connection.getresponse()
    result = response.status, response.read(), response
    connection.close()
    return result


def test_requests_need_the_token_which_is_never_forwarded(proxy, backend):
    assert call(proxy, "/api/chat", token=None, method="POST", body={})[0] == 401
    assert call(proxy, "/api/chat", token="y" * 64, method="POST", body={})[0] == 401
    assert call(proxy, "/api/chat", token=TOKEN, scheme="Basic", method="POST", body={})[0] == 401
    assert not backend.calls
    status, body, response = call(proxy, "/api/chat", method="POST", body={"stream": True})
    assert status == 200 and len(body.splitlines()) == 2
    assert response.getheader("Connection") == "close"
    assert backend.calls[0][:3] == ("POST", "/api/chat", None)
    assert json.loads(backend.calls[0][3]) == {"stream": True}
    # The scheme is case-insensitive.
    assert call(proxy, "/api/tags", scheme="bearer")[0] == 200


def test_non_ascii_authorization_is_a_401_not_a_crash(proxy):
    with socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5) as sock:
        sock.sendall(b"GET /api/tags HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer \xc3\xa9t\xc3\xa9\r\n\r\n")
        reply = sock.recv(4096)
    assert reply.startswith(b"HTTP/1.1 401")


@pytest.mark.parametrize("path", ["/api/../private", "//external.example/api/chat", "/not-supported",
                                  "/api/chat?redirect=elsewhere", "/api/push"])
def test_only_allowed_paths_are_forwarded(proxy, backend, path):
    assert call(proxy, path, method="POST", body={})[0] == 404
    assert not backend.calls


def test_methods_are_checked_per_path(proxy, backend):
    status, _body, response = call(proxy, "/api/chat")
    assert status == 405 and response.getheader("Allow") == "POST"
    assert call(proxy, "/api/tags", method="DELETE")[0] == 405
    assert not backend.calls
    for method, path in (("GET", "/api/tags"), ("GET", "/api/ps"), ("GET", "/api/version"),
                         ("POST", "/api/generate"), ("POST", "/api/pull"), ("DELETE", "/api/delete"),
                         ("POST", "/v1/chat/completions"), ("GET", "/v1/models")):
        assert call(proxy, path, method=method, body={} if method != "GET" else None)[0] == 200, path


def test_maintenance_blocks_model_requests_but_not_health(proxy, tmp_path):
    marker = tmp_path / "maintenance"
    marker.write_text("updating")
    status, body, response = call(proxy, "/api/chat", method="POST", body={})
    assert status == 503 and b"Maintenance" in body and response.getheader("Retry-After")
    assert call(proxy, "/healthz", token=None)[0] == 200
    assert call(proxy, "/source", token=None)[0] == 200
    marker.unlink()
    assert call(proxy, "/api/tags")[0] == 200


def test_health_reflects_the_upstream(make_proxy, backend):
    proxy = make_proxy()
    assert call(proxy, "/health", token=None)[0] == 200
    status, body, _ = call(proxy, "/healthz", token=None)
    assert status == 200 and json.loads(body) == {"status": "ok"}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    dead = ComputeServer(("127.0.0.1", 0), upstream=f"http://127.0.0.1:{dead_port}", token=TOKEN)
    start(dead)
    try:
        assert call(dead, "/healthz", token=None)[0] == 503
        assert call(dead, "/api/tags")[0] == 502
    finally:
        dead.shutdown()
        dead.server_close()


def test_source_offer(proxy):
    status, body, _ = call(proxy, "/source", token=None)
    assert status == 200 and json.loads(body)["source"] == inference_proxy.DEFAULT_SOURCE_URL
    status, _body, response = call(proxy, "/source", token=None, headers={"Accept": "text/html"})
    assert status == 302 and response.getheader("Location") == inference_proxy.DEFAULT_SOURCE_URL


def test_chunked_and_oversized_bodies_are_refused(proxy, backend):
    with socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5) as sock:
        sock.sendall(f"POST /api/chat HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                     "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n".encode())
        assert sock.recv(4096).startswith(b"HTTP/1.1 411")
    with socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5) as sock:
        sock.sendall(f"POST /api/chat HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                     f"Content-Length: {inference_proxy.MAX_BODY + 1}\r\n\r\n".encode())
        assert sock.recv(4096).startswith(b"HTTP/1.1 413")
    assert not backend.calls


def test_excess_connections_get_a_json_503(make_proxy, backend):
    proxy = make_proxy(max_connections=1)
    backend.slow.set()
    results = []
    first = threading.Thread(target=lambda: results.append(call(proxy, "/api/chat", method="POST", body={})[0]))
    first.start()
    time.sleep(0.3)
    status, body, response = call(proxy, "/api/chat", method="POST", body={})
    first.join(5)
    assert status == 503 and b"busy" in body and response.getheader("Retry-After") == "5"
    assert results == [200]


def test_idle_clients_are_disconnected(make_proxy):
    proxy = make_proxy(client_timeout=0.3)
    with socket.create_connection(("127.0.0.1", proxy.server_port), timeout=5) as sock:
        sock.sendall(b"GET /api/tags HTTP/1.1\r\n")
        started = time.monotonic()
        assert sock.recv(4096) == b""
        assert time.monotonic() - started < 3


@pytest.mark.parametrize("stage", ["headers", "body", "refusal"])
def test_trickling_clients_cannot_hold_a_connection_forever(make_proxy, backend, stage):
    proxy = make_proxy(max_connections=1, client_timeout=0.5, header_deadline=0.2, body_deadline=0.2)
    held = None
    if stage == "refusal":
        held = socket.create_connection(("127.0.0.1", proxy.server_port), timeout=2)
        held.sendall(b"GET /api/tags HTTP/1.1\r\nHost: ")
        time.sleep(0.05)
    request = b"GET /api/tags HTTP/1.1\r\nHost: "
    if stage == "body":
        request = (f"POST /api/chat HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                   "Content-Length: 10000\r\n\r\n{").encode()
    stop = threading.Event()
    try:
        with socket.create_connection(("127.0.0.1", proxy.server_port), timeout=2) as sock:
            sock.sendall(request)

            def drip():
                while not stop.wait(0.05):
                    try:
                        sock.sendall(b" ")
                    except OSError:
                        break

            thread = threading.Thread(target=drip, daemon=True)
            thread.start()
            started = time.monotonic()
            reply = sock.recv(4096)
            assert time.monotonic() - started < 1
            assert reply == b"" or reply.startswith(b"HTTP/1.1 503")
            stop.set()
            thread.join(1)
    finally:
        stop.set()
        if held is not None:
            held.close()
    assert not backend.calls


def test_request_deadline_does_not_interrupt_a_model_loading(make_proxy, backend):
    proxy = make_proxy(header_deadline=0.1, body_deadline=0.1)
    backend.slow.set()
    assert call(proxy, "/api/chat", method="POST", body={})[0] == 200


def test_configuration_is_validated(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        ComputeServer(("127.0.0.1", 0), upstream="http://10.0.0.5:11434", token=TOKEN)
    with pytest.raises(ValueError, match="loopback"):
        ComputeServer(("127.0.0.1", 0), upstream="https://127.0.0.1:11434", token=TOKEN)
    with pytest.raises(ValueError, match="32"):
        ComputeServer(("127.0.0.1", 0), upstream="http://127.0.0.1:11434", token="short")
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        inference_proxy.read_token_file(token_file)
    token_file.chmod(0o600)
    assert inference_proxy.read_token_file(token_file) == TOKEN
    link = tmp_path / "link"
    link.symlink_to(token_file)
    with pytest.raises(ValueError, match="regular file"):
        inference_proxy.read_token_file(link)
    with pytest.raises(ValueError, match="BC_COMPUTE_TOKEN_FILE"):
        inference_proxy.create_server({})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = inference_proxy.create_server({"BC_COMPUTE_TOKEN_FILE": str(token_file), "BC_COMPUTE_PORT": str(port)})
    try:
        assert server.upstream == ("127.0.0.1", 11434) and server.source == inference_proxy.DEFAULT_SOURCE_URL
    finally:
        server.server_close()


def test_module_imports_without_side_effects_or_web_packages():
    root = Path(__file__).resolve().parents[1]
    code = ("import sys, compute.inference_proxy as p; "
            "assert not any(name.split('.')[0] in ('flask', 'werkzeug', 'bananachat') for name in sys.modules); "
            "print('ok')")
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60,
                            env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
