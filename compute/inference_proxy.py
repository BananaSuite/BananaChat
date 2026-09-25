"""Authenticated streaming access to a loopback Ollama compute service."""

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit


ALLOWED = {"/api/chat", "/api/generate", "/api/tags", "/api/show", "/api/pull", "/api/delete", "/api/create", "/api/copy",
           "/api/version", "/api/ps", "/api/embed", "/api/embeddings", "/v1/chat/completions", "/v1/completions", "/v1/models", "/v1/embeddings"}
MAX_BODY = 32 * 1024 * 1024


class ComputeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, *, upstream, token, maintenance="", source="https://github.com/BananaSuite/BananaChat"):
        parsed = urlsplit(upstream)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.username or parsed.password:
            raise ValueError("The compute upstream must be a loopback Ollama listener.")
        if len(token) < 32 or any(char.isspace() for char in token):
            raise ValueError("Use a compute API token of at least 32 characters.")
        self.upstream = (parsed.hostname, parsed.port or 11434)
        self.token, self.maintenance, self.source = token, maintenance, source
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in {"/health", "/healthz"}:
            connection = http.client.HTTPConnection(*self.server.upstream, timeout=3)
            try:
                connection.request("GET", "/api/version")
                response = connection.getresponse()
                self.reply(200 if response.status == 200 else 503, {"status": "ok" if response.status == 200 else "unavailable"})
            except (OSError, http.client.HTTPException):
                self.reply(503, {"status": "unavailable"})
            finally:
                connection.close()
        elif self.path == "/source":
            self.reply(200, {"source": self.server.source})
        else:
            self.forward()

    def do_POST(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def forward(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            return self.reply(401, {"error": "A valid compute API token is required."})
        if self.server.maintenance and Path(self.server.maintenance).exists():
            return self.reply(503, {"error": "Maintenance is in progress. Retry shortly."})
        if self.path not in ALLOWED or self.headers.get("Transfer-Encoding"):
            return self.reply(400, {"error": "Unsupported compute request."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                return self.reply(413, {"error": "The compute request is too large."})
            body = self.rfile.read(length)
            if len(body) != length:
                return self.reply(400, {"error": "Incomplete request."})
        except (ValueError, OSError):
            return self.reply(400, {"error": "Invalid request body."})
        upstream = http.client.HTTPConnection(*self.server.upstream, timeout=900)
        started = False
        try:
            upstream.request(self.command, self.path, body=body, headers={"Content-Type": "application/json"})
            response = upstream.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not started:
                self.reply(502, {"error": "The compute backend is unavailable."})
        finally:
            self.close_connection = True
            upstream.close()


def main():
    token_file = Path(os.environ["BC_COMPUTE_TOKEN_FILE"])
    if token_file.is_symlink() or not token_file.is_file() or token_file.stat().st_mode & 0o077:
        raise ValueError("The compute token must be a regular private file with mode 0600.")
    server = ComputeServer((os.environ.get("BC_COMPUTE_HOST", "127.0.0.1"), int(os.environ.get("BC_COMPUTE_PORT", "11435"))),
                           upstream=os.environ.get("BC_COMPUTE_UPSTREAM", "http://127.0.0.1:11434"), token=token_file.read_text().strip(),
                           maintenance=os.environ.get("BANANA_MAINTENANCE_FILE", ""),
                           source=os.environ.get("BC_SOURCE_URL", "https://github.com/BananaSuite/BananaChat"))
    server.serve_forever()


if __name__ == "__main__":
    main()
