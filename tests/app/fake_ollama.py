"""A small in-process imitation of the Ollama HTTP API for tests."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    """Start with ``FakeOllama().start()``; configure replies through attributes.

    * ``models``: names returned by ``/api/tags``.
    * ``reply``: text streamed by ``/api/chat`` (split into word chunks).
    * ``thinking``: optional reasoning streamed before the reply.
    * ``chunk_delay``: seconds between chunks.
    * ``fail_models``: model names whose chat fails with HTTP 500.
    * ``hang_models``: model names whose chat never sends anything.
    * ``omit_usage``: send no token counts.
    * ``requests``: every JSON body received, as ``(path, body)``.
    * ``api_key``: require this bearer token; ``authorizations`` records the
      ``Authorization`` header of every request as ``(path, header)``.

    Tool calling (agents), for ``/api/chat`` requests that carry ``tools``:

    * ``tool_responder(body)`` returns the reply as a dict ``{"content",
      "thinking", "tool_calls": [{"name", "arguments"}]}``; otherwise replies
      are taken in order from ``tool_script``; when both are empty the model
      calls ``finish``.
    * ``tool_delay``: seconds to wait before answering (for stop and
      concurrency tests); ``active_tool_chats``/``max_tool_chats`` count
      tool requests answered at the same time.
    * ``capabilities``: name -> list returned by ``/api/show`` (default
      ``["completion", "tools"]``).

    Model management (the model lifecycle):

    * ``details``: name -> ``{"family", "parameter_size", "quantization_level",
      "context_length"}`` reported by ``/api/tags`` and ``/api/show``.
    * ``versions``: name -> a number; changing it changes the model's digest.
    * ``show_errors``: name -> HTTP status for ``/api/show``; ``show_delay``.
    * ``tags_status``: when set, ``/api/tags`` answers with that HTTP status.
    * ``pull_errors``: name -> ``[error, ...]``; each pull attempt pops one and
      streams it as an error record after the first progress line (``None``
      means that attempt succeeds). ``pull_http_errors``: name -> status.
    * ``pull_stall``: names whose pull keeps reporting the same progress
      (``pull_stall_seconds`` long) and then ends without success.
    * ``pull_attempts``: name -> number of pull requests received.
    * ``pull_unlisted``: names that "succeed" but never appear in ``/api/tags``.
    * ``generate_fail``: names whose ``/api/generate`` fails with HTTP 500.
    * ``generate_delay``: seconds ``/api/generate`` with a prompt waits before answering.
    """

    def __init__(self):
        self.models = ["llama3.2:3b", "qwen3:4b"]
        self.running: list[str] = []
        self.reply = "Hello from the fake model."
        self.thinking = ""
        self.chunk_delay = 0.0
        self.fail_models: set[str] = set()
        self.hang_models: set[str] = set()
        self.omit_usage = False
        self.requests: list[tuple[str, dict]] = []
        self.pull_steps = 3
        self.deleted: list[str] = []
        self.api_key = None
        self.authorizations: list[tuple[str, str | None]] = []
        self.tool_script: list = []
        self.tool_responder = None
        self.tool_delay = 0.0
        self.active_tool_chats = 0
        self.max_tool_chats = 0
        self.capabilities: dict[str, list] = {}
        self.details: dict[str, dict] = {}
        self.versions: dict[str, int] = {}
        self.show_errors: dict[str, int] = {}
        self.show_delay = 0.0
        self.tags_status: int | None = None
        self.pull_errors: dict[str, list] = {}
        self.pull_http_errors: dict[str, int] = {}
        self.pull_stall: set[str] = set()
        self.pull_stall_seconds = 5.0
        self.pull_attempts: dict[str, int] = {}
        self.pull_unlisted: set[str] = set()
        self.generate_fail: set[str] = set()
        self.generate_delay = 0.0
        self._tool_lock = threading.Lock()
        self._server = None

    def next_tool_reply(self, body: dict) -> dict:
        if self.tool_responder is not None:
            return self.tool_responder(body)
        with self._tool_lock:
            if self.tool_script:
                return self.tool_script.pop(0)
        return {"tool_calls": [{"name": "finish", "arguments": {"summary": "All done."}}]}

    def digest(self, name: str) -> str:
        return hashlib.sha256(f"{name}@{self.versions.get(name, 1)}".encode()).hexdigest()

    def tag(self, name: str) -> dict:
        info = self.details.get(name, {})
        return {"name": name, "model": name, "size": 2 * 1024 ** 3, "digest": self.digest(name),
                "details": {"family": info.get("family", "llama"), "parameter_size": info.get("parameter_size", "3B"),
                            "quantization_level": info.get("quantization_level", "Q4_K_M")}}

    def show_body(self, name: str) -> dict:
        info = self.details.get(name, {})
        family = info.get("family", "llama")
        return {"capabilities": self.capabilities.get(name, ["completion", "tools"]),
                "details": self.tag(name)["details"],
                "model_info": {"general.architecture": family,
                               f"{family}.context_length": info.get("context_length", 131072)}}

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeOllama":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}") if length else {}
                fake.requests.append((self.path, data))
                return data

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                fake.authorizations.append((self.path, self.headers.get("Authorization")))
                if fake.api_key and self.headers.get("Authorization") != f"Bearer {fake.api_key}":
                    self._json(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/api/tags":
                    if fake.tags_status:
                        self._json(fake.tags_status, {"error": "listing failed"})
                        return
                    self._json(200, {"models": [fake.tag(name) for name in list(fake.models)
                                                if name not in fake.pull_unlisted]})
                elif self.path == "/api/ps":
                    self._json(200, {"models": [{"name": name, "size": 1, "size_vram": 1} for name in fake.running]})
                elif self.path == "/api/version":
                    self._json(200, {"version": "0.9.0"})
                else:
                    self._json(404, {"error": "not found"})

            def do_DELETE(self):
                if not self._authorized():
                    return
                body = self._body()
                name = body.get("model") or body.get("name")
                if name in fake.models:
                    fake.models.remove(name)
                    fake.deleted.append(name)
                    self._json(200, {})
                else:
                    self._json(404, {"error": f"model '{name}' not found"})

            def do_POST(self):
                if not self._authorized():
                    return
                body = self._body()
                if self.path == "/api/chat":
                    self._chat(body)
                elif self.path == "/api/pull":
                    self._pull(body)
                elif self.path == "/api/generate":
                    name = body.get("model")
                    if fake.generate_delay and body.get("prompt"):
                        time.sleep(fake.generate_delay)
                    if name in fake.generate_fail or (body.get("prompt") and name not in fake.models):
                        self._json(500, {"error": f"model '{name}' failed to load"})
                    else:
                        self._json(200, {"model": name, "response": "OK" if body.get("prompt") else "",
                                         "done": True})
                elif self.path == "/api/show":
                    name = body.get("model") or body.get("name")
                    if fake.show_delay:
                        time.sleep(fake.show_delay)
                    if name in fake.show_errors:
                        self._json(fake.show_errors[name], {"error": f"cannot show '{name}'"})
                    elif name not in fake.models:
                        self._json(404, {"error": f"model '{name}' not found"})
                    else:
                        self._json(200, fake.show_body(name))
                else:
                    self._json(404, {"error": "not found"})

            def _stream_start(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()

            def _line(self, payload):
                self.wfile.write(json.dumps(payload).encode() + b"\n")
                self.wfile.flush()

            def _chat(self, body):
                model = body.get("model")
                if model in fake.fail_models or model not in fake.models:
                    self._json(500 if model in fake.fail_models else 404,
                               {"error": f"model '{model}' failed" if model in fake.fail_models
                                else f"model '{model}' not found"})  # Ollama's words for a model it lacks
                    return
                if model in fake.hang_models:
                    time.sleep(30)
                    return
                if body.get("tools"):
                    self._tool_chat(model, body)
                    return
                self._stream_start()
                try:
                    for word in fake.thinking.split(" ") if fake.thinking else []:
                        self._line({"model": model, "message": {"role": "assistant", "content": "",
                                                                "thinking": word + " "}, "done": False})
                        time.sleep(fake.chunk_delay)
                    words = fake.reply.split(" ")
                    for index, word in enumerate(words):
                        text = word if index == len(words) - 1 else word + " "
                        self._line({"model": model, "message": {"role": "assistant", "content": text}, "done": False})
                        time.sleep(fake.chunk_delay)
                    final = {"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                             "done_reason": "stop"}
                    if not fake.omit_usage:
                        final.update(prompt_eval_count=11, eval_count=len(words))
                    self._line(final)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _tool_chat(self, model, body):
                with fake._tool_lock:
                    fake.active_tool_chats += 1
                    fake.max_tool_chats = max(fake.max_tool_chats, fake.active_tool_chats)
                try:
                    if fake.tool_delay:
                        time.sleep(fake.tool_delay)
                    reply = fake.next_tool_reply(body)
                    self._stream_start()
                    if reply.get("thinking"):
                        self._line({"model": model, "message": {"role": "assistant", "content": "",
                                                                "thinking": reply["thinking"]}, "done": False})
                    if reply.get("content"):
                        self._line({"model": model, "message": {"role": "assistant", "content": reply["content"]},
                                    "done": False})
                    calls = [{"function": {"name": call["name"], "arguments": call.get("arguments", {})}}
                             for call in reply.get("tool_calls") or []]
                    if calls:
                        self._line({"model": model, "message": {"role": "assistant", "content": "",
                                                                "tool_calls": calls}, "done": False})
                    self._line({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                                "done_reason": "stop", "prompt_eval_count": reply.get("prompt_tokens", 100),
                                "eval_count": reply.get("completion_tokens", 20)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with fake._tool_lock:
                        fake.active_tool_chats -= 1

            def _pull(self, body):
                name = body.get("model") or body.get("name")
                fake.pull_attempts[name] = fake.pull_attempts.get(name, 0) + 1
                if name in fake.pull_http_errors:
                    self._json(fake.pull_http_errors[name], {"error": f"pull of '{name}' refused"})
                    return
                errors = fake.pull_errors.get(name)
                error = errors.pop(0) if errors else None
                self._stream_start()
                try:
                    self._line({"status": "pulling manifest"})
                    if error:
                        self._line({"error": error})
                        return
                    if name in fake.pull_stall:
                        deadline = time.monotonic() + fake.pull_stall_seconds
                        while time.monotonic() < deadline:
                            self._line({"status": "downloading", "completed": 1, "total": 10})
                            time.sleep(0.1)
                        return
                    for step in range(fake.pull_steps):
                        self._line({"status": "downloading", "completed": step + 1, "total": fake.pull_steps})
                        time.sleep(fake.chunk_delay)
                    self._line({"status": "verifying sha256 digest"})
                    if name not in fake.models:
                        fake.models.append(name)
                    self._line({"status": "success"})
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def chat_bodies(self) -> list[dict]:
        return [body for path, body in self.requests if path == "/api/chat"]
