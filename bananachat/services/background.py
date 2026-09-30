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
"""

from __future__ import annotations

import logging
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


def job(name: str, *, every: float, long_running: bool = False, initial_delay: float = 5.0):
    """Register a periodic job; ``every`` may be a callable taking the app."""
    def decorate(function):
        _jobs[name] = Job(name, every, function, long_running, initial_delay)
        return function
    return decorate


def jobs() -> dict[str, Job]:
    return dict(_jobs)


def is_leader() -> bool:
    return _state["leader"]


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
    value = item.every(app) if callable(item.every) else item.every
    return max(1.0, float(value))


def _call(app, item: Job) -> None:
    with app.app_context():
        try:
            item.function(app)
        except Exception:  # noqa: BLE001 - one failing job must not stop the others
            log.warning("Background job %s failed", item.name, exc_info=True)
        finally:
            db.release_thread_connection()


def _run_periodic(app) -> None:
    due = {name: time.monotonic() + item.initial_delay for name, item in _jobs.items() if not item.long_running}
    while not _stop.is_set():
        now = time.monotonic()
        for name, when in list(due.items()):
            item = _jobs.get(name)
            if item is None or now < when:
                continue
            _call(app, item)
            due[name] = time.monotonic() + _interval(app, item)
        _stop.wait(1.0)
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
