"""Admission control for inference, shared by every worker process.

``inference_queue`` holds one row per waiting or running request. A request
runs when fewer than ``BC_MAX_CONCURRENT`` are running and it is the first
waiting row in (priority, arrival) order whose owner has nothing running
(at most three requests per owner, one of them running). Rows carry a heartbeat
renewed by the supervisor; rows of crashed processes expire after
``LEASE_SECONDS`` and are removed.

Usage::

    with queue.Slot(queue.PRIORITY_CHAT, owner_key=f"user:{uid}") as slot:
        slot.wait(timeout=120, cancel=token, on_position=report)
        ... run inference ...
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid

from flask import current_app

from bananachat import db
from bananachat.services import supervisor
from bananachat.services.upstream import Cancelled, CancelToken

# Lower runs first. Rows only live while a request waits or runs, so these
# numbers can change between releases.
PRIORITY_ADMIN = 0
PRIORITY_FAST = 1   # accounts an administrator sped up
PRIORITY_API = 2
PRIORITY_CHAT = 3
PRIORITY_SLOW = 4   # slow credits, and accounts an administrator slowed down
LEASE_SECONDS = 90
MAX_PER_OWNER = 3
# Longest pause between admission checks; requests in this process are also
# woken as soon as a slot here is released.
MAX_POLL_SECONDS = 0.3
_released = threading.Condition()


class QueueFull(RuntimeError):
    """The queue (or this owner's share of it) is full."""


class QueueTimeout(RuntimeError):
    """The request waited too long for a free slot."""


def _limits():
    config = current_app.config["BC"]
    return config.max_concurrent, config.max_queue_depth


def _memory_ok() -> bool:
    config = current_app.config["BC"]
    if not config.min_free_memory_mb or not config.ollama_is_local:
        return True
    try:
        import psutil
    except ImportError:
        return True
    return psutil.virtual_memory().available >= config.min_free_memory_mb * 1024 * 1024


def _wake_waiters() -> None:
    with _released:
        _released.notify_all()


class Slot:
    """A place in the inference queue. Always use as a context manager."""

    def __init__(self, priority: int, *, owner_key: str | None = None, model: str | None = None):
        self.priority = priority
        self.owner_key = owner_key
        # The model this request uses, so a model is not deleted under it (see set_model).
        self.model = model
        self.request_id = uuid.uuid4().hex
        self.running = False
        self.enqueued_at = time.time()
        self.started_at: float | None = None
        self._entered = False

    # context management -------------------------------------------------
    def __enter__(self):
        max_concurrent, max_depth = _limits()
        now = time.time()
        with db.transaction():
            db.execute("DELETE FROM inference_queue WHERE heartbeat_at<?", (now - LEASE_SECONDS,))
            if db.scalar("SELECT COUNT(*) FROM inference_queue", default=0) >= max_depth:
                raise QueueFull("The service is busy. Please try again shortly.")
            if self.owner_key and db.scalar("SELECT COUNT(*) FROM inference_queue WHERE owner_key=?",
                                            (self.owner_key,), 0) >= MAX_PER_OWNER:
                raise QueueFull("You already have several requests in progress. Wait for them to finish.")
            db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, heartbeat_at, "
                       "owner_key, model_name) VALUES (?,?,'waiting',?,?,?,?,?)",
                       (self.request_id, self.priority, os.getpid(), now, now, self.owner_key, self.model))
        supervisor.register_slot(self.request_id)
        self._entered = True
        return self

    def __exit__(self, *_):
        self.release()

    def set_model(self, model: str | None) -> None:
        """Record the model a fallback attempt switched to."""
        if model == self.model:
            return
        self.model = model
        if self._entered:
            try:
                db.execute("UPDATE inference_queue SET model_name=? WHERE req_id=?", (model, self.request_id))
            except Exception:  # noqa: BLE001 - informational; the lease expires on its own
                db.close_thread_connection()

    def release(self) -> None:
        if not self._entered:
            return
        self._entered = False
        supervisor.unregister_slot(self.request_id)
        try:
            db.execute("DELETE FROM inference_queue WHERE req_id=?", (self.request_id,))
        except Exception:  # noqa: BLE001 - the lease expires on its own
            db.close_thread_connection()
        _wake_waiters()

    # waiting ------------------------------------------------------------
    def position(self) -> int:
        """1-based place among waiting requests (0 once running)."""
        row = db.one("SELECT status, priority, seq FROM inference_queue WHERE req_id=?", (self.request_id,))
        if row is None or row["status"] == "running":
            return 0
        return db.scalar(
            "SELECT COUNT(*) FROM inference_queue WHERE status='waiting' AND heartbeat_at>=? AND "
            "(priority<? OR (priority=? AND seq<=?))",
            (time.time() - LEASE_SECONDS, row["priority"], row["priority"], row["seq"]), 1)

    def _try_start(self) -> bool:
        max_concurrent, _ = _limits()
        cutoff = time.time() - LEASE_SECONDS
        running = db.scalar("SELECT COUNT(*) FROM inference_queue WHERE status='running' AND heartbeat_at>=?",
                            (cutoff,), 0)
        if running >= max_concurrent:
            return False
        if not self._is_next(cutoff) or not _memory_ok():
            return False
        with db.transaction():
            running = db.scalar("SELECT COUNT(*) FROM inference_queue WHERE status='running' AND heartbeat_at>=?",
                                (cutoff,), 0)
            if running >= max_concurrent or not self._is_next(cutoff):
                return False
            updated = db.execute("UPDATE inference_queue SET status='running', started_at=?, heartbeat_at=? "
                                 "WHERE req_id=? AND status='waiting'", (time.time(), time.time(), self.request_id))
            return updated.rowcount == 1

    def _is_next(self, cutoff: float) -> bool:
        first = db.one(
            "SELECT w.req_id FROM inference_queue w WHERE w.status='waiting' AND w.heartbeat_at>=? AND "
            "(w.owner_key IS NULL OR NOT EXISTS (SELECT 1 FROM inference_queue r WHERE r.status='running' "
            "AND r.owner_key=w.owner_key AND r.heartbeat_at>=?)) ORDER BY w.priority, w.seq LIMIT 1",
            (cutoff, cutoff))
        return first is not None and first["req_id"] == self.request_id

    def wait(self, *, timeout: float, cancel: CancelToken | None = None, on_position=None) -> None:
        """Block until this request may run. Raises QueueTimeout or Cancelled."""
        for position in self.positions(timeout=timeout, cancel=cancel):
            if on_position is not None:
                on_position(position)

    def positions(self, *, timeout: float, cancel: CancelToken | None = None):
        """Wait for admission, yielding the queue position whenever it changes.

        Nothing is yielded when the request starts immediately. Raises
        QueueTimeout or Cancelled.
        """
        deadline = time.monotonic() + timeout
        delay = 0.05
        reported = None
        if cancel is not None:
            cancel.on_cancel(_wake_waiters)
        try:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise Cancelled(cancel.reason)
                if self._try_start():
                    self.running = True
                    self.started_at = time.time()
                    if reported not in (None, 0):
                        yield 0
                    return
                if db.one("SELECT 1 FROM inference_queue WHERE req_id=?", (self.request_id,)) is None:
                    raise QueueTimeout("The request lost its place in the queue.")
                position = self.position()
                if position != reported:
                    reported = position
                    yield position
                if time.monotonic() >= deadline:
                    raise QueueTimeout("The request waited too long for a free slot. Please try again.")
                pause = min(delay * random.uniform(0.8, 1.2), max(0.05, deadline - time.monotonic()))
                with _released:
                    if cancel is None or not cancel.cancelled:
                        _released.wait(pause)
                delay = min(MAX_POLL_SECONDS, delay * 1.5)
        finally:
            if cancel is not None:
                cancel.remove(_wake_waiters)

    @property
    def wait_ms(self) -> int:
        end = self.started_at or time.time()
        return max(0, int((end - self.enqueued_at) * 1000))


def stats() -> dict:
    cutoff = time.time() - LEASE_SECONDS
    rows = db.query("SELECT status, COUNT(*) AS n FROM inference_queue WHERE heartbeat_at>=? GROUP BY status",
                    (cutoff,))
    counts = {row["status"]: row["n"] for row in rows}
    max_concurrent, max_depth = _limits()
    return {"running": counts.get("running", 0), "waiting": counts.get("waiting", 0),
            "max_concurrent": max_concurrent, "max_depth": max_depth}


def priority_for(user, *, slow: bool, api: bool = False) -> int:
    """Queue priority: administrators first, then fast accounts, API, chat, and the slow lane.

    Slow credits and accounts set to ``slow`` always wait in the slow lane;
    accounts set to ``fast`` get the priority lane otherwise.
    """
    if user is not None and user["role"] == "admin":
        return PRIORITY_ADMIN
    speed = "normal"
    if user is not None:
        from bananachat.db import limits

        speed = limits.speed_of(user["id"])
    if slow or speed == "slow":
        return PRIORITY_SLOW
    if speed == "fast":
        return PRIORITY_FAST
    return PRIORITY_API if api else PRIORITY_CHAT
