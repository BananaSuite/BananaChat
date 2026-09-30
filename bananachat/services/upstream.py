"""Minimal HTTP client for trusted backends (Ollama, ComfyUI, the checkpoint agent).

* No environment proxies and no redirects (credentials never leave the host).
* Separate timeouts for connecting, the first byte and gaps between reads,
  plus a total deadline.
* Requests can be cancelled from another thread (the socket is shut down).
* Error responses are read (bounded) so callers can show the backend's message.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

# Everything sent to a model server must fit the compute proxy's 32 MB body
# limit (1.0 compute servers enforce the same), so the images of one request
# share this budget of raw bytes (about 27 MB once base64-encoded).
MAX_CONTEXT_IMAGE_BYTES = 20 * 1024 * 1024


class UpstreamError(RuntimeError):
    """The backend failed, timed out or returned an error status (``code``: its error code, if it sent one).

    ``kind`` is ``connect`` when the backend could not be reached at all and
    ``timeout`` when it did not answer in time (None otherwise).
    """

    def __init__(self, message: str, status: int | None = None, code: str | None = None, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.kind = kind


class Cancelled(RuntimeError):
    """The operation was cancelled by the caller."""


class CancelToken:
    """A thread-safe cancellation flag with callbacks (e.g. closing a socket)."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list = []
        self.reason = ""

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - best effort
                pass

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)

    def on_cancel(self, callback) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def remove(self, callback) -> None:
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled(self.reason or "cancelled")


def _connection(url: str, timeout: float):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UpstreamError("The backend URL must be http:// or https://.")
    if parts.username or parts.password:
        raise UpstreamError("Do not put credentials in the backend URL; use the API key setting.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                           context=ssl.create_default_context()), parts
    return http.client.HTTPConnection(parts.hostname, port, timeout=timeout), parts


def _target(parts, path: str) -> str:
    base = parts.path.rstrip("/")
    return f"{base}{path}"


class Response:
    """An open streaming response. Always close it (use ``with``)."""

    def __init__(self, connection, response, *, read_timeout: float, deadline: float | None,
                 max_bytes: int, cancel: CancelToken | None):
        self._connection = connection
        self._response = response
        self._read_timeout = read_timeout
        self._deadline = deadline
        self._remaining = max_bytes
        self._cancel = cancel
        self.status = response.status
        self.headers = response.headers
        if cancel is not None:
            cancel.on_cancel(self._abort)

    def _abort(self):
        try:
            sock = self._connection.sock
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _prepare_read(self):
        if self._cancel is not None:
            self._cancel.check()
        timeout = self._read_timeout
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise UpstreamError("The backend took too long to respond.")
            timeout = min(timeout, left)
        sock = self._connection.sock
        if sock is not None:
            sock.settimeout(timeout)

    def set_read_timeout(self, seconds: float) -> None:
        self._read_timeout = seconds

    def _account(self, data: bytes) -> bytes:
        self._remaining -= len(data)
        if self._remaining < 0:
            raise UpstreamError("The backend response exceeded its size limit.")
        return data

    def _call(self, function, *args):
        self._prepare_read()
        try:
            return function(*args)
        except (socket.timeout, TimeoutError):
            if self._cancel is not None and self._cancel.cancelled:
                raise Cancelled(self._cancel.reason) from None
            raise UpstreamError("The backend stopped responding.", kind="timeout") from None
        except (OSError, http.client.HTTPException) as error:
            if self._cancel is not None and self._cancel.cancelled:
                raise Cancelled(self._cancel.reason) from None
            raise UpstreamError(f"The connection to the backend failed: {error}", kind="connect") from None

    def read(self, size: int = -1) -> bytes:
        return self._account(self._call(self._response.read, size))

    def readline(self, limit: int = 1024 * 1024) -> bytes:
        line = self._call(self._response.readline, limit + 1)
        if len(line) > limit:
            raise UpstreamError("The backend sent an oversized record.")
        return self._account(line)

    def iter_json_lines(self):
        """Yield decoded objects from an NDJSON body; malformed lines are skipped."""
        while True:
            line = self.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue

    def json(self, limit: int = 16 * 1024 * 1024):
        data = self.read(limit + 1)
        if len(data) > limit:
            raise UpstreamError("The backend response is too large.")
        try:
            return json.loads(data or b"null")
        except ValueError:
            raise UpstreamError("The backend returned invalid JSON.") from None

    def close(self):
        if self._cancel is not None:
            self._cancel.remove(self._abort)
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def open_request(method: str, base_url: str, path: str, *, body=None, headers=None, connect_timeout: float = 10,
                 first_byte_timeout: float = 30, read_timeout: float = 30, total_timeout: float | None = None,
                 max_bytes: int = 64 * 1024 * 1024, cancel: CancelToken | None = None) -> Response:
    """Send a request and return the open response (raises :class:`UpstreamError` for non-2xx)."""
    if cancel is not None:
        cancel.check()
    deadline = time.monotonic() + total_timeout if total_timeout else None
    connection, parts = _connection(base_url, connect_timeout)
    payload = None
    send_headers = {"Accept": "application/json", "Connection": "close", "User-Agent": "BananaChat"}
    if body is not None:
        payload = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        send_headers["Content-Type"] = "application/json"
    send_headers.update(headers or {})

    aborter = None
    if cancel is not None:
        def aborter():
            try:
                if connection.sock is not None:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        cancel.on_cancel(aborter)
    try:
        try:
            connection.request(method, _target(parts, path), body=payload, headers=send_headers)
            if connection.sock is not None:
                wait = first_byte_timeout
                if deadline is not None:
                    wait = max(0.1, min(wait, deadline - time.monotonic()))
                connection.sock.settimeout(wait)
            raw = connection.getresponse()
        except (socket.timeout, TimeoutError):
            if cancel is not None and cancel.cancelled:
                raise Cancelled(cancel.reason) from None
            raise UpstreamError("The backend did not answer in time.", kind="timeout") from None
        except (OSError, http.client.HTTPException) as error:
            if cancel is not None and cancel.cancelled:
                raise Cancelled(cancel.reason) from None
            raise UpstreamError(f"The backend could not be reached: {error}", kind="connect") from None
    except BaseException:
        connection.close()
        raise
    finally:
        if aborter is not None:
            cancel.remove(aborter)

    response = Response(connection, raw, read_timeout=read_timeout, deadline=deadline, max_bytes=max_bytes,
                        cancel=cancel)
    if not 200 <= raw.status < 300:
        try:
            message, code = _error_details(response)
        finally:
            response.close()
        raise UpstreamError(message or f"The backend returned HTTP {raw.status}.", raw.status, code)
    return response


def _error_details(response: Response) -> tuple[str, str | None]:
    try:
        data = response._response.read(16384)
    except (OSError, http.client.HTTPException):
        return "", None
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            code = None
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), str) else None
                error = error.get("message")
            if isinstance(error, str):
                return error[:500], (code[:64] if code else None)
    except ValueError:
        pass
    return data.decode("utf-8", errors="replace").strip()[:300], None


def request_json(method: str, base_url: str, path: str, *, body=None, headers=None, timeout: float = 15,
                 max_bytes: int = 16 * 1024 * 1024, cancel: CancelToken | None = None):
    """Send a request and decode the JSON response."""
    with open_request(method, base_url, path, body=body, headers=headers, connect_timeout=min(timeout, 10),
                      first_byte_timeout=timeout, read_timeout=timeout, total_timeout=timeout * 2,
                      max_bytes=max_bytes, cancel=cancel) as response:
        return response.json(max_bytes)
