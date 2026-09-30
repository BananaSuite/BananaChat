"""Small HTTP client: no environment proxies, no redirects, bounded bodies, per-read timeouts.

The worker token must only ever reach the configured server, so redirects are
reported as errors instead of being followed, and proxy variables are ignored
(``http.client`` never reads them).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from typing import Optional
from urllib.parse import urlsplit

USER_AGENT = "BananaChat-worker/2"


class TransportError(Exception):
    """The request could not be completed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def connect(url: str, timeout: float):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TransportError(f"Unsupported address: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    return connection, parts.path.rstrip("/")


def _abort(connection) -> None:
    try:
        if connection.sock is not None:
            connection.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Pending:
    """A request still waiting for its response headers; :meth:`abort` unblocks it from another thread."""

    def __init__(self, connection):
        self.connection = connection

    def abort(self) -> None:
        _abort(self.connection)


class Response:
    """An open response; read it with :meth:`read_json` or :meth:`lines`, then :meth:`close`."""

    def __init__(self, connection, response, max_bytes: int):
        self.connection = connection
        self.response = response
        self.status = response.status
        self.remaining = max_bytes

    def set_timeout(self, seconds: float) -> None:
        if self.connection.sock is not None:
            self.connection.sock.settimeout(seconds)

    def abort(self) -> None:
        """Unblock a read from another thread."""
        _abort(self.connection)

    def _account(self, data: bytes) -> bytes:
        self.remaining -= len(data)
        if self.remaining < 0:
            raise TransportError("The response is larger than allowed.")
        return data

    def read(self, limit: Optional[int] = None) -> bytes:
        size = self.remaining + 1 if limit is None else min(limit, self.remaining + 1)
        try:
            return self._account(self.response.read(size))
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(f"Reading the response failed: {error}") from None

    def read_json(self):
        data = self.read()
        try:
            return json.loads(data or b"null")
        except ValueError:
            raise TransportError("The response is not valid JSON.") from None

    def lines(self, max_line: int = 1024 * 1024):
        while True:
            try:
                line = self.response.readline(max_line + 1)
            except (OSError, http.client.HTTPException) as error:
                raise TransportError(f"Reading the response failed: {error}") from None
            if not line:
                return
            if len(line) > max_line:
                raise TransportError("The response contains an oversized line.")
            yield self._account(line)

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_request(method: str, url: str, path: str, *, body=None, headers=None, connect_timeout: float = 15,
                 read_timeout: float = 30, max_bytes: int = 1024 * 1024, on_connect=None) -> Response:
    """Send a request; returns the open response for any status except redirects (an error).

    *on_connect* receives a :class:`Pending` once connected, so another thread
    can abort a request whose answer is slow to start (a cold model load).
    """
    connection, base = connect(url, connect_timeout)
    payload = None
    send = {"User-Agent": USER_AGENT, "Accept": "application/json", "Connection": "close"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        send["Content-Type"] = "application/json"
    send.update(headers or {})
    try:
        connection.connect()
        connection.sock.settimeout(read_timeout)
        if on_connect is not None:
            on_connect(Pending(connection))
        connection.request(method, base + path, body=payload, headers=send)
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as error:
        connection.close()
        kind = "timed out" if isinstance(error, socket.timeout) else f"failed: {error}"
        raise TransportError(f"The connection to {urlsplit(url).hostname} {kind}") from None
    result = Response(connection, response, max_bytes)
    if 300 <= response.status < 400:
        result.close()
        raise TransportError("The server answered with a redirect; set its final https:// address "
                             "(credentials are never sent to a redirect).", response.status)
    return result


def request_json(method: str, url: str, path: str, *, body=None, headers=None, connect_timeout: float = 15,
                 read_timeout: float = 30, max_bytes: int = 1024 * 1024):
    """``(status, decoded JSON or None)``; error bodies are read (bounded) for the message."""
    with open_request(method, url, path, body=body, headers=headers, connect_timeout=connect_timeout,
                      read_timeout=read_timeout, max_bytes=max_bytes) as response:
        if response.status == 204:
            return 204, None
        if not 200 <= response.status < 300:
            text = response.read(4096).decode("utf-8", "replace")
            message = text.strip()[:300]
            try:
                parsed = json.loads(text)
                error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(error, dict):
                    error = error.get("message")
                if isinstance(error, str):
                    message = error[:300]
            except ValueError:
                pass
            raise TransportError(f"HTTP {response.status}: {message}", response.status, message)
        return response.status, response.read_json()
