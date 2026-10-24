"""Model downloads and removals the server's administrators ask this PC to run.

On by default (the PC lends its Ollama to the server, so the server may keep its
models current); the owner switches it off with ``BC_WORKER_MODEL_MANAGEMENT=0``.
A download needs ``BC_WORKER_MIN_FREE_GB`` free on the disk that holds Ollama's
models (10 GB by default) and only accepts names from the Ollama library or
Hugging Face, as the server does. Progress is reported every few seconds; a
command the administrator cancels stops at the next report.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Dict

from . import system_info
from .client import ServerError
from .local_ollama import Cancelled, OllamaError

log = logging.getLogger("bananachat.worker.models")

FEATURE = "model_commands"
REPORT_SECONDS = 3.0
MAX_RUNNING = 2
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/\-]{0,299}$")
REGISTRIES = ("registry.ollama.ai", "hf.co", "huggingface.co")


def valid_name(name) -> bool:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ".." in name or "//" in name:
        return False
    first, _, rest = name.partition("/")
    return not (rest and ("." in first or ":" in first) and first.lower() not in REGISTRIES)


class ModelCommands:
    def __init__(self, settings, client, ollama, *, on_change: Callable[[], None] = lambda: None):
        self.settings = settings
        self.client = client
        self.ollama = ollama
        self.on_change = on_change
        self._running: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.model_management)

    def running_ids(self) -> list:
        """The commands this process is running (the server fails those it handed out and that are not here)."""
        with self._lock:
            return [key for key, thread in self._running.items() if thread.is_alive()][:16]

    def handle(self, commands) -> None:
        """Start the commands of a heartbeat reply (each once)."""
        if not self.enabled or not isinstance(commands, list):
            return
        for command in commands[:MAX_RUNNING]:
            if not isinstance(command, dict) or not isinstance(command.get("id"), str):
                continue
            with self._lock:
                for key, thread in list(self._running.items()):
                    if not thread.is_alive():
                        self._running.pop(key)
                if command["id"] in self._running:
                    continue
                thread = threading.Thread(target=self._run, args=(command,), name=f"model-{command['id'][:8]}",
                                          daemon=True)
                self._running[command["id"]] = thread
            thread.start()

    def _report(self, command_id: str, **body) -> bool:
        """Send a report; True when the server wants the command stopped."""
        for attempt in range(3):
            try:
                reply = self.client.command_report(command_id, body)
                return bool(reply.get("cancel"))
            except ServerError as error:
                if error.status is not None and error.status < 500:
                    return True
                if attempt == 2:
                    log.warning("Could not report command %s: %s", command_id[:8], error)
                    return False
                time.sleep(1 + attempt)
        return False

    def _run(self, command: dict) -> None:
        command_id, kind, model = command["id"], command.get("kind"), command.get("model")
        if kind not in ("pull", "delete") or not valid_name(model):
            self._report(command_id, status="failed", error="The worker refused an invalid command.")
            return
        log.info("Administrator command %s: %s %s", command_id[:8], kind, model)
        try:
            if not self.ollama.ensure_running():
                raise OllamaError("Ollama is not running and could not be started.")
            if kind == "delete":
                removed = self.ollama.delete(model)
                self._report(command_id, status="done", detail="Removed." if removed else "It was not installed.")
            else:
                self._pull(command_id, model)
            self.on_change()
        except Cancelled:
            log.info("Command %s cancelled by the server", command_id[:8])
        except OllamaError as error:
            log.warning("Command %s failed: %s", command_id[:8], error)
            self._report(command_id, status="failed", error=str(error))

    def _pull(self, command_id: str, model: str) -> None:
        free = system_info.free_disk()
        needed = self.settings.min_free_gb * 1024 ** 3
        if free is not None and free < needed:
            raise OllamaError(f"Only {free / 1024 ** 3:.1f} GB free on this PC; downloads need at least "
                              f"{self.settings.min_free_gb:g} GB (BC_WORKER_MIN_FREE_GB).")
        cancel = threading.Event()
        state = {"at": 0.0}

        def progress(status: str, completed, total) -> None:
            now = time.monotonic()
            if now - state["at"] < REPORT_SECONDS:
                return
            state["at"] = now
            percent = int(completed * 100 / total) if completed and total else 0
            detail = f"{status}: {completed / 1024 ** 2:,.0f} / {total / 1024 ** 2:,.0f} MB" if total and completed \
                else status or "Working…"
            if self._report(command_id, status="running", percent=min(99, percent), detail=detail,
                            completed=completed, total=total):
                cancel.set()
            if free is not None and total and system_info.free_disk() is not None \
                    and system_info.free_disk() < self.settings.min_free_gb * 1024 ** 3 / 2:
                raise OllamaError("The disk is almost full; the download stopped.")

        self._report(command_id, status="running", percent=0, detail="Starting the download…")
        self.ollama.pull(model, cancel=cancel, on_progress=progress)
        self._report(command_id, status="done", detail="Downloaded.")
