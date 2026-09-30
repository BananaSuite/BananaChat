"""An in-process imitation of the sandbox runner HTTP API (``compute/sandbox_runner.py``) for tests.

It keeps an in-memory filesystem per sandbox and answers commands from a
script instead of running anything:

* ``exec_handler(sandbox, command, timeout)`` returns the exec answer (a dict);
  the default echoes the command. ``hang_exec`` makes commands block until the
  sandbox is interrupted or deleted (or 30 s pass).
* ``max_sandboxes``: creating more answers 429 ``capacity``.
* ``network``/``rootless``/``engine``: reported by ``/healthz``.
* ``requests``: every request as ``(method, path, body)``; ``deleted`` and
  ``interrupted`` list sandbox ids.
"""

from __future__ import annotations

import base64
import io
import json
import posixpath
import tarfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "fake-runner-token-0123456789abcdef0123456789"
READ_LIMIT = 1024 * 1024


class Sandbox:
    def __init__(self, session: str):
        self.id = uuid.uuid4().hex
        self.session = session
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/workspace"}
        self.created_at = time.time()
        self.release = threading.Event()
        self.busy = threading.Lock()

    def public(self) -> dict:
        stamp = datetime.fromtimestamp(self.created_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"id": self.id, "session": self.session, "image": "test-image", "created_at": stamp,
                "last_used_at": stamp, "expires_at": stamp, "busy": self.busy.locked(), "limits": {}}

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)
        parent = posixpath.dirname(path)
        while parent.startswith("/workspace"):
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)


class FakeRunner:
    def __init__(self):
        self.token = TOKEN
        self.sandboxes: dict[str, Sandbox] = {}
        self.max_sandboxes = 4
        self.network = "none"
        self.rootless = True
        self.engine = "podman"
        self.hang_exec = False
        self.exec_delay = 0.0
        self.exec_handler = None
        self.fail_create = None  # (status, code) to answer every create with
        self.requests: list[tuple[str, str, object]] = []
        self.deleted: list[str] = []
        self.interrupted: list[str] = []
        self.execs: list[tuple[str, str]] = []
        self.active_execs = 0
        self.max_active_execs = 0
        self._lock = threading.Lock()
        self._server = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def only(self) -> Sandbox:
        assert len(self.sandboxes) == 1, list(self.sandboxes)
        return next(iter(self.sandboxes.values()))

    # ----- behaviour ---------------------------------------------------------------------
    def _exec(self, box: Sandbox, command: str, timeout) -> dict:
        with self._lock:
            self.active_execs += 1
            self.max_active_execs = max(self.max_active_execs, self.active_execs)
            self.execs.append((box.id, command))
        try:
            if self.hang_exec:
                box.release.wait(30)
                return {"exit_code": 137, "stdout": "", "stderr": "[interrupted]", "truncated": False,
                        "timed_out": False, "duration_ms": 1, "interrupted": True, "sandbox_removed": False}
            if self.exec_delay:
                time.sleep(self.exec_delay)
            if self.exec_handler is not None:
                return {"exit_code": 0, "stdout": "", "stderr": "", "truncated": False, "timed_out": False,
                        "duration_ms": 5, **self.exec_handler(box, command, timeout)}
            return {"exit_code": 0, "stdout": f"ran: {command}\n", "stderr": "", "truncated": False,
                    "timed_out": False, "duration_ms": 5}
        finally:
            with self._lock:
                self.active_execs -= 1

    @staticmethod
    def _extract(box: Sandbox, data: bytes, root: str = "/workspace") -> int:
        count = 0
        box.dirs.add(root)
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if not info.is_dir():
                        box.write(posixpath.normpath(root + "/" + info.filename), archive.read(info))
                        count += 1
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        box.write(posixpath.normpath(root + "/" + member.name), archive.extractfile(member).read())
                        count += 1
        return count

    def archive(self, box: Sandbox) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, data in sorted(box.files.items()):
                info = tarfile.TarInfo("./" + posixpath.relpath(path, "/workspace"))
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    # ----- server ------------------------------------------------------------------------
    def start(self) -> "FakeRunner":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status, payload=None, *, raw=None, content_type="application/json"):
                data = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
                self.send_response(status)
                if status != 204:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _error(self, status, code, message):
                self._reply(status, {"error": {"code": code, "message": message}})

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _handle(self):
                parts = urlsplit(self.path)
                body = self._body() if self.command in ("POST", "PUT") else b""
                try:
                    recorded = json.loads(body) if body and self.headers.get("Content-Type") == "application/json" \
                        else len(body)
                except ValueError:
                    recorded = len(body)
                fake.requests.append((self.command, self.path, recorded))
                if parts.path == "/healthz":
                    return self._reply(200, {"ok": True, "engine": fake.engine, "rootless": fake.rootless,
                                             "runtime": "default", "sandboxes": len(fake.sandboxes),
                                             "max": fake.max_sandboxes, "images": ["test-image"],
                                             "network": fake.network})
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._error(401, "unauthorized", "A valid sandbox runner token is required.")
                segments = [segment for segment in parts.path.split("/") if segment]
                query = {key: values[0] for key, values in parse_qs(parts.query).items()}
                if segments[:2] != ["v1", "sandboxes"]:
                    return self._error(404, "not_found", "Unknown path.")
                if len(segments) == 2:
                    if self.command == "GET":
                        return self._reply(200, {"sandboxes": [box.public() for box in fake.sandboxes.values()]})
                    if fake.fail_create:
                        status, code = fake.fail_create
                        return self._error(status, code, "Refused by the test.")
                    if len(fake.sandboxes) >= fake.max_sandboxes:
                        return self._error(429, "capacity", "All sandboxes are in use. Retry later.")
                    request = json.loads(body or b"{}")
                    box = Sandbox(request["session"])
                    fake.sandboxes[box.id] = box
                    return self._reply(201, {key: box.public()[key] for key in ("id", "session", "image",
                                                                                   "limits", "created_at",
                                                                                   "expires_at")})
                box = fake.sandboxes.get(segments[2])
                action = segments[3] if len(segments) > 3 else None
                if action is None and self.command == "DELETE":
                    if box is not None:
                        del fake.sandboxes[box.id]
                        fake.deleted.append(box.id)
                        box.release.set()
                    return self._reply(204)
                if box is None:
                    return self._error(404, "not_found", "No such sandbox.")
                if action is None:
                    return self._reply(200, box.public())
                if action == "interrupt":
                    fake.interrupted.append(box.id)
                    box.release.set()
                    return self._reply(200, {"ok": True, "sandbox_removed": False})
                if action == "exec":
                    request = json.loads(body or b"{}")
                    if not box.busy.acquire(blocking=False):
                        return self._error(409, "busy", "Another command is running in this sandbox.")
                    try:
                        box.release.clear()
                        return self._reply(200, fake._exec(box, request["command"], request.get("timeout")))
                    finally:
                        box.busy.release()
                if action == "files":
                    path = posixpath.normpath(query.get("path", "/workspace"))
                    if not (path == "/workspace" or path.startswith("/workspace/")):
                        return self._error(403, "outside_workspace", "The path resolves outside /workspace.")
                    if self.command == "PUT":
                        if path in box.dirs:
                            return self._error(409, "is_a_directory", "The path is a directory.")
                        box.write(path, body)
                        return self._reply(200, {"path": path, "size": len(body)})
                    if path in box.files:
                        data = box.files[path]
                        content = data[:READ_LIMIT]
                        payload = {"path": path, "type": "file", "size": len(data),
                                   "truncated": len(data) > READ_LIMIT}
                        try:
                            payload.update(content=content.decode("utf-8"), encoding="utf-8")
                        except UnicodeDecodeError:
                            payload.update(content=base64.b64encode(content).decode(), encoding="base64")
                        return self._reply(200, payload)
                    if path in box.dirs:
                        entries = []
                        for item in sorted(box.dirs):
                            if posixpath.dirname(item) == path and item != path:
                                entries.append({"name": posixpath.basename(item), "type": "dir", "size": 0})
                        for item, data in sorted(box.files.items()):
                            if posixpath.dirname(item) == path:
                                entries.append({"name": posixpath.basename(item), "type": "file",
                                                "size": len(data)})
                        return self._reply(200, {"path": path, "type": "dir", "entries": entries,
                                                 "truncated": False})
                    return self._error(404, "not_found", "No such file or directory.")
                if action == "archive":
                    if self.command == "GET":
                        return self._reply(200, raw=fake.archive(box), content_type="application/gzip")
                    root = posixpath.normpath(query.get("path", "/workspace"))
                    if not (root == "/workspace" or root.startswith("/workspace/")):
                        return self._error(403, "outside_workspace", "The path resolves outside /workspace.")
                    count = fake._extract(box, body, root)
                    return self._reply(200, {"path": root, "files": count, "directories": 0,
                                             "skipped": 0, "size": len(body)})
                return self._error(404, "not_found", "Unknown path.")

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        for box in list(self.sandboxes.values()):
            box.release.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
