"""LC1-1: a request body that arrives too slowly ends the request instead of holding a server thread."""
from __future__ import annotations

import io
import socket
import threading
import time

import pytest
from werkzeug.exceptions import RequestTimeout

from bananachat import read_timeout


class _Stream:
    def __init__(self, sock, pause=0.0, data=b"x" * 100):
        self.sock, self.pause, self.data = sock, pause, io.BytesIO(data)

    def read(self, size=-1):
        if self.pause:
            self.sock.recv(1)  # blocks on the socket like Gunicorn's body reader
        return self.data.read(size)


def _app(environ, start_response):
    environ["wsgi.input"].read(10)
    return [b"ok"]


def _environ(sock, stream, length="100"):
    return {"gunicorn.socket": sock, "wsgi.input": stream, "CONTENT_LENGTH": length}


def test_lc1_1_a_stalled_body_is_refused_and_the_socket_timeout_restored():
    a, b = socket.socketpair()
    try:
        app = read_timeout.guard(_app, 0.3, 32)
        started = time.monotonic()
        with pytest.raises(RequestTimeout):
            app(_environ(a, _Stream(a, pause=1)), lambda *args: None)
        assert time.monotonic() - started < 2
        assert a.gettimeout() is None
    finally:
        a.close()
        b.close()


def test_lc1_1_a_fast_body_and_a_request_without_a_body_are_untouched():
    a, b = socket.socketpair()
    try:
        app = read_timeout.guard(_app, 0.3, 32)
        assert app(_environ(a, _Stream(a)), lambda *args: None) == [b"ok"]
        environ = _environ(a, _Stream(a), "0")
        assert app(environ, lambda *args: None) == [b"ok"]
        assert not isinstance(environ["wsgi.input"], read_timeout._TimedInput)
        assert read_timeout.guard(_app, 0, 32) is _app
    finally:
        a.close()
        b.close()


class _Body:
    """Like Gunicorn's body reader: a read waits until it has every byte it asked for."""

    def __init__(self, sock):
        self.sock = sock

    def read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data


def test_lc1_1_a_body_that_drips_past_its_total_time_is_refused(monkeypatch):
    a, b = socket.socketpair()
    stop = threading.Event()

    def drip():
        while not stop.is_set():
            try:
                b.sendall(b"x")
            except OSError:
                return
            time.sleep(0.05)

    thread = threading.Thread(target=drip, daemon=True)
    try:
        monkeypatch.setattr(read_timeout, "GRACE", 0.0)
        # 100 bytes at 1 KB/s: 0.1 s in total, and a byte arrives every 0.05 s, so no read ever stalls
        app = read_timeout.guard(lambda environ, start_response: environ["wsgi.input"].read(100), 5, 1)
        thread.start()
        started = time.monotonic()
        with pytest.raises(RequestTimeout):
            app(_environ(a, _Body(a)), lambda *args: None)
        assert time.monotonic() - started < 2
    finally:
        stop.set()
        a.close()
        b.close()


def test_fc1_1_a_websocket_upgrade_or_a_request_without_a_body_is_never_timed(monkeypatch):
    a, b = socket.socketpair()
    try:
        monkeypatch.setattr(read_timeout, "UNSIZED", 0.1)
        seen = []
        app = read_timeout.guard(lambda environ, start_response: seen.append(environ["wsgi.input"]) or [], 5, 1)
        stream = _Stream(a)
        for extra in ({"CONTENT_LENGTH": ""}, {"HTTP_UPGRADE": "websocket"}):
            environ = {"gunicorn.socket": a, "wsgi.input": stream, **extra}
            app(environ, lambda *args: None)
            assert environ["wsgi.input"] is stream
        # An Upgrade header is the client's choice: it does not excuse a request that has a body.
        sneaky = {"gunicorn.socket": a, "wsgi.input": stream, "HTTP_UPGRADE": "h2c", "CONTENT_LENGTH": "5"}
        app(sneaky, lambda *args: None)
        assert isinstance(sneaky["wsgi.input"], read_timeout._TimedInput)
        chunked = {"gunicorn.socket": a, "wsgi.input": stream, "HTTP_TRANSFER_ENCODING": "chunked"}
        app(chunked, lambda *args: None)
        assert isinstance(chunked["wsgi.input"], read_timeout._TimedInput)
        before = threading.active_count()
        for _ in range(20):
            app({"gunicorn.socket": a, "wsgi.input": stream}, lambda *args: None)
        assert threading.active_count() <= before
    finally:
        a.close()
        b.close()


def test_hc1_1_the_total_time_counts_only_the_time_spent_waiting_for_the_body(monkeypatch):
    a, b = socket.socketpair()
    try:
        monkeypatch.setattr(read_timeout, "GRACE", 0.2)

        def slow_handler(environ, start_response):
            time.sleep(0.5)  # busy before it reads: the body has long arrived whole
            return [environ["wsgi.input"].read(100)]

        b.sendall(b"x" * 100)
        app = read_timeout.guard(slow_handler, 5, 1000)
        assert app({"gunicorn.socket": a, "wsgi.input": _Body(a), "CONTENT_LENGTH": "100"},
                   lambda *args: None) == [b"x" * 100]

        def partial_handler(environ, start_response):
            first = environ["wsgi.input"].read(10)
            time.sleep(0.5)  # works for longer than the whole allowance, then reads the rest
            return [first + environ["wsgi.input"].read(90)]

        b.sendall(b"y" * 100)
        app = read_timeout.guard(partial_handler, 5, 1000)
        assert app({"gunicorn.socket": a, "wsgi.input": _Body(a), "CONTENT_LENGTH": "100"},
                   lambda *args: None) == [b"y" * 100]
    finally:
        a.close()
        b.close()
