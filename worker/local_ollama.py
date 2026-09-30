"""The PC's own Ollama: health, models, starting and stopping ``ollama serve``, streaming chat.

If Ollama already runs (a desktop app, a system service) the worker uses it
and never stops it. Otherwise it starts ``ollama serve`` listening on the
address of ``BC_OLLAMA_HOST`` and stops it again after
``BC_OLLAMA_IDLE_TIMEOUT`` seconds without a job, to give the memory back.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import List, Optional

from .transport import TransportError, open_request, request_json

log = logging.getLogger("bananachat.worker.ollama")


class OllamaError(Exception):
    pass


class Cancelled(Exception):
    pass


class LocalOllama:
    def __init__(self, settings):
        self.settings = settings
        self.url = settings.ollama_host
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.last_used = time.monotonic()

    # ----- metadata -------------------------------------------------------------------------
    def is_alive(self) -> bool:
        try:
            status, _data = request_json("GET", self.url, "/api/version", connect_timeout=3, read_timeout=5)
            return status == 200
        except TransportError:
            return False

    def version(self) -> Optional[str]:
        try:
            _status, data = request_json("GET", self.url, "/api/version", connect_timeout=3, read_timeout=5)
        except TransportError:
            return None
        value = data.get("version") if isinstance(data, dict) else None
        return value if isinstance(value, str) else None

    def models(self) -> List[str]:
        try:
            _status, data = request_json("GET", self.url, "/api/tags", connect_timeout=3, read_timeout=15,
                                         max_bytes=8 * 1024 * 1024)
        except TransportError as error:
            log.debug("Could not list Ollama models: %s", error)
            return []
        entries = data.get("models") if isinstance(data, dict) else None
        return [entry["name"] for entry in entries or [] if isinstance(entry, dict) and isinstance(entry.get("name"), str)]

    # ----- process ----------------------------------------------------------------------------
    @property
    def managed_pid(self) -> Optional[int]:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def ensure_running(self, wait: float = 30.0) -> bool:
        if self.is_alive():
            return True
        with self._lock:
            if not self.settings.ollama_is_local:
                log.error("Ollama at %s is not reachable, and the worker only starts Ollama on this computer.",
                          self.url)
                return False
            if self._process is None or self._process.poll() is not None:
                environment = dict(os.environ)
                environment["OLLAMA_HOST"] = self.settings.ollama_listen_address
                options = {"env": environment, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                           "stdin": subprocess.DEVNULL}
                if os.name == "nt":
                    options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    options["start_new_session"] = True
                try:
                    self._process = subprocess.Popen([self.settings.ollama_binary, "serve"], **options)
                except OSError as error:
                    log.error("Could not start '%s serve': %s. Install Ollama or set BC_OLLAMA_BINARY.",
                              self.settings.ollama_binary, error)
                    return False
                log.info("Started Ollama (pid %s) on %s", self._process.pid, self.settings.ollama_listen_address)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.is_alive():
                return True
            if self._process is not None and self._process.poll() is not None:
                log.error("Ollama exited right after starting (code %s)", self._process.returncode)
                return False
            time.sleep(0.5)
        log.error("Ollama did not answer within %.0f seconds of starting", wait)
        return False

    def stop_managed(self) -> None:
        """Stop the Ollama the worker started (never one the owner runs)."""
        with self._lock:
            process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        log.info("Stopping the Ollama the worker started (pid %s)", process.pid)
        try:
            if os.name == "nt":
                process.terminate()
            else:
                process.send_signal(signal.SIGTERM)
            process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def stop_if_idle(self) -> bool:
        timeout = self.settings.ollama_idle_timeout
        if timeout <= 0 or self.managed_pid is None or time.monotonic() - self.last_used < timeout:
            return False
        log.info("No job for %d seconds; stopping Ollama to free memory", timeout)
        self.stop_managed()
        return True

    # ----- inference --------------------------------------------------------------------------
    def chat(self, model: str, messages: list, options: Optional[dict], *, first_token_timeout: float,
             read_timeout: float, total_timeout: float, cancel: threading.Event, on_open=None):
        """Yield ``(content, done, usage)``; ``usage`` is filled on the last record."""
        self.last_used = time.monotonic()
        body = {"model": model, "messages": messages, "stream": True, "keep_alive": self.settings.ollama_keep_alive}
        if options:
            body["options"] = options
        deadline = time.monotonic() + total_timeout
        try:
            # The first record may take a cold model load.
            response = open_request("POST", self.url, "/api/chat", body=body, connect_timeout=10,
                                    read_timeout=first_token_timeout, max_bytes=64 * 1024 * 1024,
                                    on_connect=on_open)
        except TransportError as error:
            if cancel.is_set():
                raise Cancelled() from None
            raise OllamaError(f"Ollama could not be reached: {error}") from None
        if on_open is not None:
            on_open(response)
        try:
            if response.status != 200:
                text = response.read(2048).decode("utf-8", "replace")
                raise OllamaError(f"Ollama answered HTTP {response.status}: {text.strip()[:300]}")
            first = True
            for raw in response.lines():
                if cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise OllamaError("The answer took too long.")
                if first:
                    response.set_timeout(read_timeout)
                    first = False
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    raise OllamaError("Ollama sent an invalid record.") from None
                if not isinstance(record, dict):
                    continue
                if record.get("error"):
                    raise OllamaError(str(record["error"])[:500])
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                content = message.get("content") if isinstance(message.get("content"), str) else ""
                if record.get("done"):
                    usage = {"prompt_tokens": _count(record.get("prompt_eval_count")),
                             "completion_tokens": _count(record.get("eval_count")),
                             "finish_reason": "length" if record.get("done_reason") == "length" else "stop"}
                    yield content, True, usage
                    return
                if content:
                    yield content, False, {}
            if cancel.is_set():
                raise Cancelled()
            raise OllamaError("Ollama stopped before finishing the answer.")
        except TransportError as error:
            if cancel.is_set():
                raise Cancelled() from None
            raise OllamaError(str(error)) from None
        finally:
            response.close()
            self.last_used = time.monotonic()


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**31 else 0
