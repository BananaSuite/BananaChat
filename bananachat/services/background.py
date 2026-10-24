"""Periodic jobs, run by exactly one process per installation.

Every worker process calls :func:`start`; the process holding the file lock
``<instance>/.background-services.lock`` runs the jobs, the others keep
retrying so a replacement takes over when the leader exits (for example
after a reload).

Feature modules register jobs at import time::

    @background.job("purge-old-things", every=3600)
    def purge_old_things(app): ...

``long_running`` jobs (such as model downloads) get their own thread and are
called in a loop with the given pause between calls.

Periodic jobs run in short-lived threads, at most :data:`MAX_PARALLEL` at once
and never two runs of the same job together, so one slow or hanging job (a
model server that stops answering) cannot hold up the others. The leader
records a heartbeat in ``runtime_state`` (:func:`leader_status`), so pages can
tell administrators when no process runs the background services and queued
work (downloads, worker commands) cannot start.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass

from filelock import FileLock, Timeout

from bananachat import db

log = logging.getLogger("bananachat.background")


@dataclass
class Job:
    name: str
    every: float
    function: object
    long_running: bool = False
    initial_delay: float = 5.0


_jobs: dict[str, Job] = {}
_stop = threading.Event()
_threads: list[threading.Thread] = []
_state = {"leader": False, "lock": None}

MAX_PARALLEL = 4                  # periodic jobs running at the same time
HEARTBEAT_KEY = "background_leader"
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 90                # a leader silent for longer is gone (or hanging)
SLOW_JOB_SECONDS = 600            # a periodic job running longer is reported in the log


def job(name: str, *, every: float, long_running: bool = False, initial_delay: float = 5.0):
    """Register a periodic job; ``every`` may be a callable taking the app."""
    def decorate(function):
        _jobs[name] = Job(name, every, function, long_running, initial_delay)
        return function
    return decorate


def jobs() -> dict[str, Job]:
    return dict(_jobs)


def start(app) -> None:
    """Begin competing for leadership (idempotent)."""
    if _threads:
        return
    _stop.clear()
    thread = threading.Thread(target=_elect, args=(app,), name="bananachat-background", daemon=True)
    thread.start()
    _threads.append(thread)


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    for thread in list(_threads):
        thread.join(timeout)
    _threads.clear()
    lock = _state.get("lock")
    if lock is not None and lock.is_locked:
        lock.release()
    _state.update(leader=False, lock=None)


def _elect(app) -> None:
    config = app.config["BC"]
    config.instance_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(str(config.instance_dir / ".background-services.lock"), timeout=0, mode=0o600)
    while not _stop.is_set():
        try:
            lock.acquire()
            break
        except Timeout:
            _stop.wait(30)
    else:
        return
    _state.update(leader=True, lock=lock)
    log.info("This process runs the background services")
    for item in _jobs.values():
        if item.long_running:
            thread = threading.Thread(target=_run_long, args=(app, item), name=f"bananachat-{item.name}", daemon=True)
            thread.start()
            _threads.append(thread)
    _run_periodic(app)


def _interval(app, item: Job) -> float:
    try:
        value = item.every(app) if callable(item.every) else item.every
        return max(1.0, float(value))
    except Exception:  # noqa: BLE001 - a broken setting must not stop the scheduler
        log.warning("Background job %s has an invalid interval; using 60 s", item.name, exc_info=True)
        return 60.0


def _call(app, item: Job) -> None:
    with app.app_context():
        try:
            item.function(app)
        except Exception:  # noqa: BLE001 - one failing job must not stop the others
            log.warning("Background job %s failed", item.name, exc_info=True)
        finally:
            db.release_thread_connection()


def _beat(app) -> None:
    """Record that this process leads the background services (read by :func:`leader_status`)."""
    from bananachat.db import settings as site_settings

    with app.app_context():
        try:
            site_settings.state_set(HEARTBEAT_KEY, {"pid": os.getpid(), "host": socket.gethostname()[:120],
                                                    "at": time.time()})
        except Exception:  # noqa: BLE001 - the database may be busy; the next beat tries again
            log.debug("Could not record the background heartbeat", exc_info=True)
        finally:
            db.release_thread_connection()


def leader_status() -> dict:
    """``{"alive": bool, "here": bool, "age": seconds or None}`` of the process running the background services."""
    if _state.get("leader"):
        return {"alive": True, "here": True, "age": 0}
    from bananachat.db import settings as site_settings

    try:
        state = site_settings.state_get(HEARTBEAT_KEY) or {}
    except Exception:  # noqa: BLE001 - unknown is reported as unknown, never as an error page
        return {"alive": None, "here": False, "age": None}
    at = state.get("at") if isinstance(state, dict) else None
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return {"alive": False, "here": False, "age": None}
    age = max(0.0, time.time() - float(at))
    return {"alive": age <= STALE_SECONDS, "here": False, "age": round(age)}


def _run_periodic(app) -> None:
    lock = threading.Lock()
    due = {name: time.monotonic() + item.initial_delay for name, item in _jobs.items() if not item.long_running}
    running: dict[str, tuple[threading.Thread, float]] = {}
    last_beat = 0.0
    warned: set[str] = set()

    def run(name: str, item: Job) -> None:
        try:
            _call(app, item)
        finally:
            with lock:
                due[name] = time.monotonic() + _interval(app, item)
                running.pop(name, None)
                warned.discard(name)

    while not _stop.is_set():
        try:
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                _beat(app)
                last_beat = now
            with lock:
                for name, when in list(due.items()):
                    item = _jobs.get(name)
                    if item is None:
                        due.pop(name, None)
                        continue
                    if name in running:
                        started = running[name][1]
                        if now - started > SLOW_JOB_SECONDS and name not in warned:
                            warned.add(name)
                            log.warning("Background job %s has been running for %d s", name, now - started)
                        continue
                    if when is None or now < when or len(running) >= MAX_PARALLEL:
                        continue
                    due[name] = None
                    thread = threading.Thread(target=run, args=(name, item), name=f"bananachat-{name}", daemon=True)
                    running[name] = (thread, now)
                    thread.start()
        except Exception:  # noqa: BLE001 - the scheduler itself must never stop
            log.warning("The background scheduler failed; continuing", exc_info=True)
        _stop.wait(1.0)
    with lock:
        threads = [thread for thread, _started in running.values()]
    for thread in threads:
        thread.join(5)
    db.close_thread_connection()


def _run_long(app, item: Job) -> None:
    _stop.wait(item.initial_delay)
    while not _stop.is_set():
        _call(app, item)
        _stop.wait(_interval(app, item))
    db.close_thread_connection()


def stopping() -> bool:
    """Long-running jobs should check this and return promptly when True."""
    return _stop.is_set()
