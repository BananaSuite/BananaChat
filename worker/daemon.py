"""The worker loop: heartbeats, activity, polling for jobs and streaming answers back.

Threads:

* heartbeat - every ``BC_HEARTBEAT_INTERVAL`` seconds reports status, activity,
  GPU and models; the server's reply can stop the running job;
* activity - samples idle time and GPU use, adjusts process priority and, when
  the owner starts gaming before the running job produced any text, hands the
  job back to the server so another worker can take it;
* main - long-polls for a job, runs it on the local Ollama, streams the answer
  in pieces (at most every 100 ms or 4,096 characters) and completes it.

SIGTERM/SIGINT stop the worker gracefully: a job that has not produced text
goes back to the pool, a job in the middle of its answer is reported as
failed, and the Ollama the worker started is stopped.
"""

from __future__ import annotations

import logging
import platform
import signal
import threading
import time
from typing import List, Optional

from .activity import Monitor, get_gpu_name
from .client import Client, ServerError
from .local_ollama import Cancelled, LocalOllama, OllamaError
from .resources import Priority

log = logging.getLogger("bananachat.worker")

PIECE_CHARS = 4096
PIECE_SECONDS = 0.1
MODEL_REFRESH_SECONDS = 60
PERMANENT_STATUSES = (401, 403, 503)


class _Stop(Exception):
    """The server no longer wants this job."""


class Job:
    def __init__(self, payload: dict):
        self.payload = payload
        self.id = payload["job_id"]
        self.cancel = threading.Event()
        self.sent_any = False
        self.reason = ""          # "defer", "shutdown" or "server"
        self._response = None
        self._lock = threading.Lock()

    def attach(self, response) -> None:
        with self._lock:
            self._response = response
            if self.cancel.is_set():
                response.abort()

    def stop(self, reason: str) -> None:
        with self._lock:
            if not self.reason:
                self.reason = reason
            self.cancel.set()
            if self._response is not None:
                self._response.abort()


class Worker:
    def __init__(self, settings, *, client=None, ollama=None, monitor=None, priority=None):
        self.settings = settings
        self.client = client or Client(settings)
        self.ollama = ollama or LocalOllama(settings)
        self.monitor = monitor or Monitor(settings)
        self.priority = priority or Priority()
        self.stop_event = threading.Event()
        self.models: List[str] = []
        self.ollama_version: Optional[str] = None
        self.gpu_name: Optional[str] = None
        self.current: Optional[Job] = None
        self._models_at = 0.0
        self._problem = ""

    # ----- lifecycle ---------------------------------------------------------------------------
    def request_stop(self, *_args) -> None:
        if not self.stop_event.is_set():
            log.info("Stopping the worker")
        self.stop_event.set()
        job = self.current
        if job is not None:
            job.stop("shutdown")

    def install_signal_handlers(self) -> None:
        for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                try:
                    signal.signal(number, self.request_stop)
                except (OSError, ValueError):
                    pass

    def run(self) -> int:
        problems = self.settings.problems()
        if problems:
            for problem in problems:
                log.error("Configuration: %s", problem)
            return 2
        for warning in self.settings.warnings:
            log.warning("Configuration: %s", warning)
        log.info("BananaChat worker %s starting; server %s", self.settings.name, self.settings.server_url)
        self.gpu_name = get_gpu_name()
        try:
            self.monitor.sample()
        except Exception:  # noqa: BLE001
            log.debug("First activity sample failed", exc_info=True)
        self.refresh_models(start=True)
        threads = [
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
            threading.Thread(target=self.monitor.run, args=(self.stop_event, self._activity_changed),
                             name="activity", daemon=True),
        ]
        for thread in threads:
            thread.start()
        self.send_heartbeat()
        try:
            self._loop()
        finally:
            self.stop_event.set()
            self.send_heartbeat(status="offline")
            self.ollama.stop_managed()
            log.info("Worker stopped")
        return 0

    # ----- state reports --------------------------------------------------------------------
    def refresh_models(self, *, start: bool = False) -> None:
        alive = self.ollama.is_alive() or (start and self.ollama.ensure_running())
        if alive:
            models = self.ollama.models()
            if models != self.models:
                log.info("Models offered: %s", ", ".join(models) or "none")
            self.models = models  # kept while Ollama is stopped for being idle
            self.ollama_version = self.ollama.version() or self.ollama_version
        self._models_at = time.monotonic()

    def heartbeat_payload(self, status: Optional[str] = None) -> dict:
        state = self.monitor.state
        gpu = self.monitor.gpu
        payload = {
            "status": status or ("busy" if state == "gaming" else "online"),
            "gpu_name": (self.gpu_name or None) and self.gpu_name[:500],
            "ollama_version": self.ollama_version,
            "capabilities": {"models": self.models[:512]},
            "activity_state": state,
            "gpu_util": None if gpu is None else round(min(100.0, max(0.0, float(gpu))), 1),
            "platform": f"{platform.system()} {platform.machine()}".strip()[:120],
        }
        job = self.current
        if job is not None:
            payload["job_id"] = job.id
        return payload

    def send_heartbeat(self, status: Optional[str] = None) -> None:
        try:
            reply = self.client.heartbeat(self.heartbeat_payload(status))
        except ServerError as error:
            self._report(error, "Heartbeat")
            return
        self._problem = ""
        job = self.current
        if job is not None and reply.get("job_stop"):
            log.info("The server stopped job %s", job.id[:8])
            job.stop("server")

    def _report(self, error: ServerError, what: str) -> None:
        text = {401: "The server rejected the worker token. Check BC_WORKER_TOKEN.",
                403: "An administrator disabled this worker.",
                503: "Remote workers are turned off on the server (or it is in maintenance)."}.get(error.status)
        message = f"{what} failed: {text or error}"
        if message != self._problem:
            log.warning("%s", message)
            self._problem = message

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.settings.heartbeat_interval):
            self.send_heartbeat()

    def _activity_changed(self, state: str) -> None:
        self.priority.apply(state, self.ollama.managed_pid)
        job = self.current
        if state == "gaming" and job is not None and not job.sent_any:
            log.info("The owner started gaming; handing job %s back to the server", job.id[:8])
            job.stop("defer")
        if state == "gaming":
            self.send_heartbeat()

    # ----- jobs ---------------------------------------------------------------------------------
    def _loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            if self.monitor.state == "gaming":
                self.stop_event.wait(5)
                continue
            self.ollama.stop_if_idle()
            if time.monotonic() - self._models_at > MODEL_REFRESH_SECONDS:
                self.refresh_models()
            if not self.models:
                self._report(ServerError("no models are installed in Ollama (or it is not running)"), "Polling")
                self.stop_event.wait(30)
                self.refresh_models(start=True)
                continue
            try:
                payload = self.client.poll(self.models)
            except ServerError as error:
                self._report(error, "Polling")
                wait = 60.0 if error.status in PERMANENT_STATUSES else backoff
                backoff = min(60.0, backoff * 2)
                self.stop_event.wait(wait)
                continue
            backoff = 1.0
            self._problem = ""
            if payload is None:
                self.stop_event.wait(self.settings.poll_gap)
                continue
            if self.stop_event.is_set() or self.monitor.state == "gaming":
                self.client.fail(payload["job_id"], "", requeue=True)
                continue
            self.run_job(payload)
            self.stop_event.wait(self.settings.poll_gap)

    def _send(self, job: Job, seq: int, content: str, done: bool) -> None:
        for attempt in range(3):
            try:
                if not self.client.chunk(job.id, seq, content, done):
                    raise _Stop()
                job.sent_any = True
                return
            except ServerError as error:
                if error.status is not None and error.status < 500 or attempt == 2:
                    raise _Stop() from None
                time.sleep(attempt + 1)  # accepted chunks are acknowledged again, so retrying is safe

    def run_job(self, payload: dict) -> str:
        """Run one job; returns ``completed``, ``deferred``, ``stopped`` or ``failed``."""
        job = Job(payload)
        self.current = job
        self.monitor.own_job = True
        self.priority.apply(self.monitor.state, self.ollama.managed_pid)
        settings = self.settings
        first_token = _positive(payload.get("first_token_timeout"), settings.first_token_timeout)
        total = _positive(payload.get("generation_timeout"), settings.generation_timeout)
        log.info("Job %s: %s, %d messages", job.id[:8], payload["model"], len(payload["messages"]))
        seq, pending, last_send = 0, "", time.monotonic()
        try:
            if not self.ollama.ensure_running():
                raise OllamaError("Ollama is not running and could not be started.")
            for content, done, usage in self.ollama.chat(
                    payload["model"], payload["messages"], payload.get("options") or None,
                    first_token_timeout=first_token, read_timeout=settings.read_timeout, total_timeout=total,
                    cancel=job.cancel, on_open=job.attach):
                pending += content
                while pending and (done or len(pending) >= PIECE_CHARS or time.monotonic() - last_send >= PIECE_SECONDS):
                    piece, pending = pending[:PIECE_CHARS], pending[PIECE_CHARS:]
                    self._send(job, seq, piece, False)
                    seq += 1
                    last_send = time.monotonic()
                if done:
                    self._send(job, seq, "", True)
                    ok = self.client.complete(job.id, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                                              usage.get("finish_reason", "stop"))
                    log.info("Job %s %s", job.id[:8], "completed" if ok else "was not accepted by the server")
                    return "completed" if ok else "stopped"
            raise OllamaError("Ollama ended without finishing the answer.")
        except (Cancelled, _Stop):
            return self._interrupted(job)
        except OllamaError as error:
            if job.cancel.is_set():
                return self._interrupted(job)
            log.warning("Job %s failed: %s", job.id[:8], error)
            self.client.fail(job.id, str(error))
            return "failed"
        except ServerError as error:
            log.warning("Job %s: the server could not be reached: %s", job.id[:8], error)
            self.client.fail(job.id, "The worker lost contact with the server.")
            return "failed"
        finally:
            self.current = None
            self.monitor.own_job = False

    def _interrupted(self, job: Job) -> str:
        if job.reason == "server" or (not job.reason and not job.cancel.is_set()):
            log.info("Job %s stopped by the server", job.id[:8])
            return "stopped"
        if not job.sent_any:
            self.client.fail(job.id, "", requeue=True)
            log.info("Job %s handed back to the server", job.id[:8])
            return "deferred"
        self.client.fail(job.id, "The worker is shutting down." if job.reason == "shutdown"
                         else "The worker's owner needed the computer.")
        return "stopped"


def _positive(value, default) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 24 * 3600:
        return float(value)
    return float(default)


def run(settings) -> int:
    worker = Worker(settings)
    worker.install_signal_handlers()
    return worker.run()
