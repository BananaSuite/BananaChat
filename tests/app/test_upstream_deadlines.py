"""Total budgets, cancellation and read gaps must cover real response sockets."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bananachat.services import supervisor
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError, open_request


@pytest.fixture
def slow_backend(monkeypatch):
    monkeypatch.setattr(supervisor, "TICK", 0.02)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            payload = b'{"value":"' + b"x" * 100 + b'"}'
            try:
                if self.path == "/headers":
                    for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n":
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        time.sleep(0.05)
                    return
                self.send_response(500 if self.path == "/error" else 200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.path == "/fast":
                    self.wfile.write(payload)
                    self.wfile.flush()
                    return
                if self.path == "/gap":
                    time.sleep(0.4)
                    self.wfile.write(payload)
                    self.wfile.flush()
                    return
                for byte in payload:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("operation", ["headers", "read", "line", "json", "error"])
def test_trickling_backends_cannot_extend_total_deadlines(slow_backend, operation):
    started = time.monotonic()
    with supervisor._lock:
        before = set(supervisor._deadlines)
    with pytest.raises(UpstreamError):
        with open_request("GET", slow_backend, "/" + operation, first_byte_timeout=2,
                          read_timeout=2, total_timeout=0.15) as response:
            if operation == "line":
                response.readline()
            elif operation == "json":
                response.json()
            else:
                response.read()
    assert time.monotonic() - started < 1.5  # includes the supervisor's maximum one-second tick
    with supervisor._lock:
        assert set(supervisor._deadlines) <= before


def test_cancellation_interrupts_a_detached_close_response_socket(slow_backend):
    cancel = CancelToken()
    with open_request("GET", slow_backend, "/read", cancel=cancel,
                      first_byte_timeout=2, read_timeout=2, total_timeout=5) as response:
        assert response._connection.sock is None  # the response still owns the live socket
        done = threading.Event()

        def stop():
            done.wait(0.05)
            cancel.cancel("stopped")

        thread = threading.Thread(target=stop)
        thread.start()
        started = time.monotonic()
        try:
            with pytest.raises(Cancelled, match="stopped"):
                response.read()
            assert time.monotonic() - started < 1
        finally:
            done.set()
            thread.join(2)
    assert cancel._callbacks == []


def test_read_gap_changes_apply_after_connection_close_headers(slow_backend):
    with open_request("GET", slow_backend, "/gap", first_byte_timeout=2,
                      read_timeout=2, total_timeout=5) as response:
        assert response._connection.sock is None
        response.set_read_timeout(0.05)
        started = time.monotonic()
        with pytest.raises(UpstreamError) as caught:
            response.read()
        assert caught.value.kind == "timeout" and time.monotonic() - started < 0.3


def test_a_consumed_close_response_retains_normal_eof_and_idempotent_cleanup(slow_backend):
    with supervisor._lock:
        before = set(supervisor._deadlines)
    with open_request("GET", slow_backend, "/fast", total_timeout=5) as response:
        assert response.json()["value"] == "x" * 100
        assert response.read() == b"" and response.readline() == b""
    response.close()
    with supervisor._lock:
        assert set(supervisor._deadlines) <= before


def test_nested_deadlines_have_independent_ownership():
    cancel = CancelToken()
    deadline = time.monotonic() + 5
    first = supervisor.cancel_at(deadline, cancel)
    second = supervisor.cancel_at(deadline, cancel)
    try:
        assert first != second
        supervisor.clear_deadline(second)
        with supervisor._lock:
            assert supervisor._deadlines[first] == (deadline, cancel)
            assert second not in supervisor._deadlines
    finally:
        supervisor.clear_deadline(first)
        supervisor.clear_deadline(second)
