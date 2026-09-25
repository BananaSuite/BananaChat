"""Bounded admission and priority scheduling shared by all web processes.

Waiting requests mostly read. Only admission, promotion, periodic heartbeats and
release take the SQLite write lock. Every acquired slot must be closed, including
when a request is rejected before entering its context manager.
"""

import logging
import os
import random
import sqlite3
import threading
import time
import uuid

import config
import db

PRIORITY_ADMIN = 0
PRIORITY_API = 1
PRIORITY_CHAT = 2
PRIORITY_SLOW = 3

MAX_CONCURRENT = config.MAX_CONCURRENT
MAX_QUEUE_DEPTH = config.MAX_QUEUE_DEPTH
LEASE_SECONDS = 90
POLL_SECONDS = 0.2
HEARTBEAT_SECONDS = 5
# Process headroom only; the inference server controls model and GPU memory.
MIN_FREE_MEMORY_MB = max(128, int(os.environ.get("BC_MIN_FREE_MEMORY_MB", "256")))
_logger = logging.getLogger("bananachat.queue")


class QueueFullError(Exception):
    pass


class QueueCancelledError(RuntimeError):
    pass


class _Entry:
    def __init__(self, req_id, priority, seq, enqueued_at):
        self.req_id, self.priority = req_id, priority
        self.seq, self.enqueued_at = seq, enqueued_at


def _reap_stale(conn, now):
    conn.execute("DELETE FROM inference_queue WHERE heartbeat_at < ?", (now - LEASE_SECONDS,))


def _has_enough_memory():
    try:
        import psutil
        return psutil.virtual_memory().available >= MIN_FREE_MEMORY_MB * 1024 ** 2
    except ImportError:
        return True


def _eligible(conn, req_id, now):
    cutoff = now - LEASE_SECONDS
    running = conn.execute(
        "SELECT COUNT(*) FROM inference_queue WHERE status='running' AND heartbeat_at>=?",
        (cutoff,),
    ).fetchone()[0]
    if running >= MAX_CONCURRENT:
        return False
    first = conn.execute(
        "SELECT w.req_id FROM inference_queue AS w WHERE w.status='waiting' AND w.heartbeat_at>=? "
        "AND (w.owner_key IS NULL OR NOT EXISTS (SELECT 1 FROM inference_queue AS r "
        "WHERE r.status='running' AND r.owner_key=w.owner_key AND r.heartbeat_at>=?)) "
        "ORDER BY w.priority, w.seq LIMIT 1", (cutoff, cutoff),
    ).fetchone()
    return first is not None and first["req_id"] == req_id


def _enqueue(priority, owner_key=None):
    req_id, now = uuid.uuid4().hex, time.time()
    with db.get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _reap_stale(conn, now)
        if conn.execute("SELECT COUNT(*) FROM inference_queue").fetchone()[0] >= MAX_QUEUE_DEPTH:
            raise QueueFullError("Service is overloaded. Please try again shortly.")
        if owner_key is not None and conn.execute(
            "SELECT COUNT(*) FROM inference_queue WHERE owner_key=?", (owner_key,),
        ).fetchone()[0] >= 3:
            raise QueueFullError("You already have three inference requests in progress. Please wait before submitting more.")
        cur = conn.execute(
            "INSERT INTO inference_queue(req_id, priority, status, owner_pid, enqueued_at, heartbeat_at, owner_key) "
            "VALUES(?,?,'waiting',?,?,?,?)", (req_id, priority, os.getpid(), now, now, owner_key),
        )
        conn.commit()
        return _Entry(req_id, priority, cur.lastrowid, now)


def get_queue_position(entry):
    with db.get_db_context() as conn:
        row = conn.execute(
            "SELECT status, priority, seq FROM inference_queue WHERE req_id=?", (entry.req_id,),
        ).fetchone()
        if row is None or row["status"] == "running":
            return 0
        return conn.execute(
            "SELECT COUNT(*) FROM inference_queue WHERE status='waiting' AND heartbeat_at>=? "
            "AND (priority < ? OR (priority=? AND seq <= ?))",
            (time.time() - LEASE_SECONDS, row["priority"], row["priority"], row["seq"]),
        ).fetchone()[0]


def get_queue_depth():
    return get_stats()["queued"]


def _try_acquire(req_id):
    now = time.time()
    with db.get_db_context() as conn:
        row = conn.execute(
            "SELECT status, heartbeat_at FROM inference_queue WHERE req_id=?", (req_id,),
        ).fetchone()
        if row is None or row["heartbeat_at"] < now - LEASE_SECONDS:
            raise QueueCancelledError("The request lost its queue lease. Please retry.")
        if row["status"] == "running":
            return True
        if not _eligible(conn, req_id, now) or not _has_enough_memory():
            return False
        conn.execute("BEGIN IMMEDIATE")
        _reap_stale(conn, now)
        if not _eligible(conn, req_id, now):
            return False
        conn.execute(
            "UPDATE inference_queue SET status='running', started_at=?, heartbeat_at=? "
            "WHERE req_id=? AND status='waiting'", (now, now, req_id),
        )
        conn.commit()
        return True


def _touch(req_id):
    with db.get_db_context() as conn:
        cur = conn.execute(
            "UPDATE inference_queue SET heartbeat_at=? WHERE req_id=? AND heartbeat_at>=?",
            (time.time(), req_id, time.time() - LEASE_SECONDS),
        )
        conn.commit()
        return cur.rowcount > 0


def _delete(req_id):
    with db.get_db_context() as conn:
        conn.execute("DELETE FROM inference_queue WHERE req_id=?", (req_id,))
        conn.commit()


class QueueSlot:
    def __init__(self, priority, timeout=120, stop_ev=None, owner_key=None):
        self._timeout, self._stop_ev = timeout, stop_ev
        self._wait_ms = 0
        self._entry = _enqueue(priority, owner_key)
        self._heartbeat_stop = threading.Event()
        self._lost = threading.Event()
        self._heartbeat_thread = None
        self._closed = False
        try:
            self.queue_position = get_queue_position(self._entry)
        except BaseException:
            self.close()
            raise

    def check(self):
        if self._closed or self._lost.is_set():
            raise QueueCancelledError("The request lost its queue lease. Please retry.")
        if self._stop_ev is not None and self._stop_ev.is_set():
            raise QueueCancelledError("Generation stopped.")

    def __enter__(self):
        started = time.monotonic()
        last_touch = started
        try:
            while True:
                self.check()
                if _try_acquire(self._entry.req_id):
                    break
                now = time.monotonic()
                if now - started >= self._timeout:
                    raise TimeoutError("Request timed out waiting in queue. Please try again.")
                if now - last_touch >= HEARTBEAT_SECONDS:
                    if not _touch(self._entry.req_id):
                        raise QueueCancelledError("The request lost its queue lease. Please retry.")
                    last_touch = now
                # Jitter keeps process-local waiters from polling in lockstep.
                self._heartbeat_stop.wait(POLL_SECONDS * random.uniform(0.8, 1.2))
            self._wait_ms = int((time.monotonic() - started) * 1000)
            self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._heartbeat_thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _heartbeat(self):
        while not self._heartbeat_stop.wait(HEARTBEAT_SECONDS):
            try:
                if _touch(self._entry.req_id):
                    continue
            except (sqlite3.DatabaseError, OSError):
                _logger.exception("Inference queue heartbeat failed")
            self._lost.set()
            return

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=1)
        try:
            _delete(self._entry.req_id)
        except (sqlite3.DatabaseError, OSError):
            # The lease expires independently if storage is temporarily unavailable.
            _logger.exception("Could not release inference queue lease")

    def __exit__(self, *_):
        self.close()

    @property
    def wait_ms(self):
        return self._wait_ms


def acquire(priority=PRIORITY_CHAT, timeout=120, stop_ev=None, owner_key=None):
    return QueueSlot(priority, timeout=timeout, stop_ev=stop_ev, owner_key=owner_key)


def get_stats():
    with db.get_db_context() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM inference_queue "
            "WHERE heartbeat_at>=? GROUP BY status", (time.time() - LEASE_SECONDS,),
        ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    running, queued = counts.get("running", 0), counts.get("waiting", 0)
    return {"running": running, "queued": queued, "depth": queued,
            "total": running + queued, "rejected": 0}
