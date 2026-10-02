"""Authenticated streaming gateway in front of a loopback Ollama (the ``compute`` server role).

Run from the repository root with ``python -m compute.inference_proxy``; it uses
only the standard library (managed compute installations have no extra
packages). Environment:

* ``BC_COMPUTE_HOST`` / ``BC_COMPUTE_PORT`` - listen address (127.0.0.1:11435);
* ``BC_COMPUTE_UPSTREAM`` - the Ollama to protect; loopback ``http://`` only
  (http://127.0.0.1:11434);
* ``BC_COMPUTE_TOKEN_FILE`` - the bearer token, a private (0600) regular file
  holding at least 32 characters;
* ``BANANA_MAINTENANCE_FILE`` - while this file exists, model requests get 503;
* ``BC_SOURCE_URL`` - the corresponding source (AGPL), offered at ``/source``;
* ``BC_COMPUTE_MAX_CONNECTIONS`` (32), ``BC_COMPUTE_UPSTREAM_TIMEOUT`` (900 s).

Endpoints:

* ``GET /health`` and ``/healthz`` (no authentication): 200 only when Ollama
  answers ``/api/version``; the lifecycle manager checks ``/healthz``;
* ``GET /source`` (no authentication): the source URL (JSON, or a redirect for browsers);
* the Ollama and OpenAI-compatible paths in ``ALLOWED`` with
  ``Authorization: Bearer <token>``; responses are streamed through.

Bodies are limited to 32 MB and need ``Content-Length`` (chunked uploads are
refused). Every connection has socket timeouts, at most
``BC_COMPUTE_MAX_CONNECTIONS`` requests are served at once and the next ones
get a JSON 503 instead of a dropped connection. Every response closes its
connection. Importing this module has no side effects.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import signal
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .http_limits import ConnectionDeadlines

DEFAULT_SOURCE_URL = "https://github.com/BananaSuite/BananaChat"
MAX_BODY = 32 * 1024 * 1024
MIN_TOKEN_LENGTH = 32
CLIENT_TIMEOUT = 30.0
HEADER_DEADLINE = 30.0
BODY_DEADLINE = 300.0
HEALTH_TIMEOUT = 3.0
HEALTH_CACHE_SECONDS = 1.0
# How long a refused request's body is read and dropped, so the client gets the
# answer instead of a connection reset (closing with unread data resets it).
DRAIN_SECONDS = 10.0
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Method -> paths forwarded to Ollama. The web server uses tags, ps, version,
# show, chat, generate, pull and delete; the rest serve API clients.
ALLOWED = {
    "GET": frozenset({"/api/tags", "/api/ps", "/api/version", "/v1/models"}),
    "HEAD": frozenset({"/api/version"}),
    "POST": frozenset({"/api/chat", "/api/generate", "/api/show", "/api/pull", "/api/create", "/api/copy",
                       "/api/embed", "/api/embeddings", "/v1/chat/completions", "/v1/completions", "/v1/embeddings"}),
    "DELETE": frozenset({"/api/delete"}),
}
ALL_PATHS = frozenset().union(*ALLOWED.values())


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def check_upstream(upstream: str) -> tuple:
    parsed = urlsplit(upstream)
    if (parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("BC_COMPUTE_UPSTREAM must be a loopback http:// Ollama address such as http://127.0.0.1:11434.")
    return parsed.hostname, parsed.port or 11434


def check_token(token: str) -> str:
    token = (token or "").strip()
    if len(token) < MIN_TOKEN_LENGTH or any(char.isspace() for char in token):
        raise ValueError(f"The compute API token must be at least {MIN_TOKEN_LENGTH} characters without spaces.")
    return token


def read_token_file(path) -> str:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"The compute token file {path} cannot be read: {error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("The compute token file must be a regular file (not a symlink).")
    if os.name != "nt" and info.st_mode & 0o077:
        raise ValueError(f"The compute token file is readable by others; run: chmod 600 {path}")
    if info.st_size > 4096:
        raise ValueError("The compute token file is too large.")
    return check_token(path.read_text(encoding="utf-8"))


def _body_length(value) -> int:
    """The Content-Length of a body that may be read and dropped, else 0."""
    try:
        length = int(value)
    except (TypeError, ValueError):
        return 0
    return length if 0 < length <= MAX_BODY else 0


def _drain_after_headers(sock, received: bytes, deadline: float) -> None:
    """Read and drop the body of a request refused before it was read (see ``Handler.discard_body``)."""
    head, separator, rest = received.partition(b"\r\n\r\n")
    if not separator:
        return
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"transfer-encoding":
            return
        if name.strip().lower() == b"content-length":
            length = _body_length(value.strip().decode("latin-1"))
    remaining = length - len(rest)
    try:
        while remaining > 0 and time.monotonic() < deadline:
            data = sock.recv(min(remaining, 65536))
            if not data:
                break
            remaining -= len(data)
    except OSError:
        pass


class ComputeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, address, *, upstream, token, maintenance="", source=DEFAULT_SOURCE_URL, max_connections=32,
                 upstream_timeout=900.0, client_timeout=CLIENT_TIMEOUT,
                 header_deadline=HEADER_DEADLINE, body_deadline=BODY_DEADLINE):
        self.upstream = check_upstream(upstream)
        self.token = check_token(token).encode("utf-8")
        self.maintenance = maintenance or ""
        self.source = source or DEFAULT_SOURCE_URL
        self.upstream_timeout = float(upstream_timeout)
        self.client_timeout = float(client_timeout)
        self.header_deadline = float(header_deadline)
        self.body_deadline = float(body_deadline)
        self.slots = threading.BoundedSemaphore(max(1, int(max_connections)))
        # Refusals are answered by short-lived threads of their own, bounded too.
        self.refusals = threading.BoundedSemaphore(64)
        self._health = {"at": 0.0, "ok": False}
        self._health_lock = threading.Lock()
        if ":" in str(address[0]):
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self.deadlines = ConnectionDeadlines()

    def server_close(self):
        if hasattr(self, "deadlines"):
            self.deadlines.close()
        super().server_close()

    # ----- concurrency -------------------------------------------------------------------------
    def process_request(self, request, client_address):
        if self.slots.acquire(blocking=False):
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()
                raise
            return
        if self.refusals.acquire(blocking=False):
            thread = threading.Thread(target=self._refuse, args=(request,), daemon=True)
            thread.start()
            return
        self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def _refuse(self, request) -> None:
        try:
            self.deadlines.set(request, self.header_deadline)
            request.settimeout(2.0)
            received = b""
            try:
                while b"\r\n\r\n" not in received and len(received) < 65536:
                    data = request.recv(8192)
                    if not data:
                        break
                    received += data
            except OSError:
                pass
            self.deadlines.set(request, DRAIN_SECONDS)
            _drain_after_headers(request, received, time.monotonic() + DRAIN_SECONDS)
            body = _json_bytes({"error": "The compute server is busy. Retry shortly."})
            head = ("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nRetry-After: 5\r\nCache-Control: no-store\r\n"
                    "Connection: close\r\n\r\n").encode("ascii")
            try:
                request.sendall(head + body)
            except OSError:
                pass
        finally:
            self.deadlines.clear(request)
            self.shutdown_request(request)
            self.refusals.release()

    # ----- helpers ----------------------------------------------------------------------------------
    def in_maintenance(self) -> bool:
        return bool(self.maintenance) and os.path.exists(self.maintenance)

    def upstream_healthy(self) -> bool:
        with self._health_lock:
            if time.monotonic() - self._health["at"] < HEALTH_CACHE_SECONDS:
                return self._health["ok"]
        connection = http.client.HTTPConnection(*self.upstream, timeout=HEALTH_TIMEOUT)
        try:
            connection.request("GET", "/api/version", headers={"Connection": "close"})
            response = connection.getresponse()
            response.read(65536)
            ok = response.status == 200
        except (OSError, http.client.HTTPException):
            ok = False
        finally:
            connection.close()
        with self._health_lock:
            self._health.update(at=time.monotonic(), ok=ok)
        return ok

    def authorized(self, header) -> bool:
        scheme, _, credential = (header or "").strip().partition(" ")
        if scheme.lower() != "bearer":
            return False
        try:
            # http.server decodes headers as ISO-8859-1, so this never fails for
            # what arrived on the wire; compare bytes in constant time.
            supplied = credential.strip().encode("latin-1")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(supplied, self.token)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BananaChat-compute"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.client_timeout)
        self.server.deadlines.set(self.connection, self.server.header_deadline)

    def finish(self):
        self.server.deadlines.clear(self.connection)
        try:
            super().finish()
        except OSError:
            pass

    # ----- responses ---------------------------------------------------------------------------------
    def reply(self, status: int, payload, headers=None) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            self.close_connection = True

    # ----- routing --------------------------------------------------------------------------------------
    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_PATCH(self):
        self.route()

    def do_OPTIONS(self):
        self.route()

    def route(self) -> None:
        self.close_connection = True
        path = self.path
        if self.command in ("GET", "HEAD") and path in ("/health", "/healthz"):
            if self.server.upstream_healthy():
                return self.reply(200, {"status": "ok"})
            return self.reply(503, {"status": "unavailable"}, {"Retry-After": "10"})
        if self.command in ("GET", "HEAD") and path == "/source":
            accept = self.headers.get("Accept", "")
            if "text/html" in accept:
                self.send_response(302)
                self.send_header("Location", self.server.source)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return None
            return self.reply(200, {"source": self.server.source, "license": "AGPL-3.0-only"})
        if not self.server.authorized(self.headers.get("Authorization")):
            self.discard_body()
            return self.reply(401, {"error": "A valid compute API token is required."},
                              {"WWW-Authenticate": 'Bearer realm="bananachat-compute"'})
        if path not in ALL_PATHS:
            self.discard_body()
            return self.reply(404, {"error": "Unsupported compute request."})
        if path not in ALLOWED.get(self.command, ()):
            allowed = ", ".join(sorted(method for method, paths in ALLOWED.items() if path in paths))
            self.discard_body()
            return self.reply(405, {"error": "Method not allowed."}, {"Allow": allowed})
        if self.server.in_maintenance():
            self.discard_body()
            return self.reply(503, {"error": "Maintenance is in progress. Retry shortly."}, {"Retry-After": "60"})
        return self.forward()

    def discard_body(self) -> None:
        """Read and drop the body of a request answered without it.

        Closing a connection whose request body was not read makes the kernel
        reset it, and the client (a web server posting a chat with images) then
        sees a broken pipe instead of this answer.
        """
        self.server.deadlines.set(self.connection, DRAIN_SECONDS)
        if self.headers.get("Transfer-Encoding"):
            return
        remaining = _body_length(self.headers.get("Content-Length"))
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            while remaining > 0 and time.monotonic() < deadline:
                data = self.rfile.read1(min(remaining, 65536))
                if not data:
                    break
                remaining -= len(data)
        except (OSError, ValueError):
            pass

    def _read_body(self):
        self.server.deadlines.set(self.connection, self.server.body_deadline)
        if self.headers.get("Transfer-Encoding"):
            self.reply(411, {"error": "Send the request body with a Content-Length."})
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError:
            self.reply(400, {"error": "Invalid Content-Length."})
            return None
        if length < 0 or length > MAX_BODY:
            self.reply(413, {"error": "The compute request is too large."})
            return None
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self.reply(400, {"error": "Incomplete request body."})
            return None
        return body

    def forward(self) -> None:
        try:
            body = self._read_body()
        except (OSError, socket.timeout):
            return None
        finally:
            # Model downloads and generation may legitimately outlast an upload.
            self.server.deadlines.clear(self.connection)
        if body is None:
            return None
        upstream = http.client.HTTPConnection(*self.server.upstream, timeout=self.server.upstream_timeout)
        started = False
        try:
            headers = {"Content-Type": self.headers.get("Content-Type") or "application/json",
                       "Accept": self.headers.get("Accept") or "*/*", "Connection": "close"}
            upstream.request(self.command, self.path, body=body if body or self.command != "GET" else None,
                             headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            length = response.getheader("Content-Length")
            if length is not None and length.isdigit():
                self.send_header("Content-Length", length)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            if self.command == "HEAD":
                return None
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not started:
                try:
                    self.reply(502, {"error": "The compute backend is unavailable."})
                except OSError:
                    pass
        finally:
            self.close_connection = True
            upstream.close()
        return None


def _integer(environ, name, default, minimum, maximum):
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def create_server(environ=None) -> ComputeServer:
    environ = os.environ if environ is None else environ
    token_file = (environ.get("BC_COMPUTE_TOKEN_FILE") or "").strip()
    if not token_file:
        raise ValueError("BC_COMPUTE_TOKEN_FILE is required.")
    host = (environ.get("BC_COMPUTE_HOST") or "127.0.0.1").strip()
    port = _integer(environ, "BC_COMPUTE_PORT", 11435, 1, 65535)
    return ComputeServer(
        (host, port),
        upstream=(environ.get("BC_COMPUTE_UPSTREAM") or "http://127.0.0.1:11434").strip(),
        token=read_token_file(token_file),
        maintenance=(environ.get("BANANA_MAINTENANCE_FILE") or "").strip(),
        source=(environ.get("BC_SOURCE_URL") or DEFAULT_SOURCE_URL).strip(),
        max_connections=_integer(environ, "BC_COMPUTE_MAX_CONNECTIONS", 32, 1, 1024),
        upstream_timeout=_integer(environ, "BC_COMPUTE_UPSTREAM_TIMEOUT", 900, 10, 86400),
    )


def main() -> int:
    try:
        server = create_server()
    except (OSError, ValueError) as error:
        print(f"compute gateway: {error}", flush=True)
        return 2

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
