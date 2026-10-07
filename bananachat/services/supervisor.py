"""One housekeeping thread per process for long-running work.

Instead of extra threads per request, active queue slots and chat runs
register here. Every second the supervisor:

* renews the leases of registered queue slots and chat runs (every 5 s),
  so other processes can tell live work from work abandoned by a crash;
* cancels chat runs whose user pressed Stop (``active_streams.stop_requested``)
  or whose lease was taken over;
* cancels anything past its deadline;
* runs registered *watches* (agent tasks): a callback that renews its own
  lease and returns a reason to cancel (``"stopped"``, ``"lease lost"``).
"""

from __future__ import annotations

import logging
import itertools
import threading
import time
from dataclasses import dataclass

from bananachat import db
from bananachat.services.upstream import CancelToken

log = logging.getLogger("bananachat.supervisor")
LEASE_INTERVAL = 5.0
TICK = 1.0


@dataclass
class _Run:
    session_id: str
    owner_token: str
    cancel: CancelToken
    deadline: float | None


@dataclass
class _Slot:
    request_id: str
    cancel: CancelToken | None
    deadline: float | None


_lock = threading.Lock()
_runs: dict[str, _Run] = {}
_slots: dict[str, _Slot] = {}
_deadlines: dict[int, tuple[float, CancelToken]] = {}
_deadline_ids = itertools.count(1)
_watches: dict[str, "_Watch"] = {}
_thread: threading.Thread | None = None


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="bananachat-supervisor", daemon=True)
    _thread.start()


def register_run(session_id: str, owner_token: str, cancel: CancelToken, deadline: float | None = None) -> None:
    """Keep a chat run's ``active_streams`` lease alive and honour Stop requests."""
    with _lock:
        _runs[session_id] = _Run(session_id, owner_token, cancel, deadline)
        _ensure_thread()


def unregister_run(session_id: str, owner_token: str | None = None) -> None:
    """Forget a chat run; with *owner_token*, only if a newer run of the chat has not replaced it."""
    with _lock:
        current = _runs.get(session_id)
        if current is not None and (owner_token is None or current.owner_token == owner_token):
            del _runs[session_id]


@dataclass
class _Watch:
    key: str
    cancel: CancelToken
    check: object  # check(renew: bool) -> str | None, a reason to cancel


def register_watch(key: str, cancel: CancelToken, check) -> None:
    """Call ``check(renew)`` every tick (``renew`` every LEASE_INTERVAL); cancel *cancel* with its answer."""
    with _lock:
        _watches[key] = _Watch(key, cancel, check)
        _ensure_thread()


def unregister_watch(key: str, cancel: CancelToken | None = None) -> None:
    with _lock:
        current = _watches.get(key)
        if current is not None and (cancel is None or current.cancel is cancel):
            del _watches[key]


def register_slot(request_id: str, cancel: CancelToken | None = None, deadline: float | None = None) -> None:
    """Keep an ``inference_queue`` row's heartbeat fresh."""
    with _lock:
        _slots[request_id] = _Slot(request_id, cancel, deadline)
        _ensure_thread()


def unregister_slot(request_id: str) -> None:
    with _lock:
        _slots.pop(request_id, None)


def cancel_at(deadline: float, cancel: CancelToken) -> int:
    """Cancel *cancel* at monotonic time *deadline*. Returns a handle for :func:`clear_deadline`."""
    with _lock:
        # Nested requests may register the same token and millisecond. Each
        # owner must be able to clear its deadline without clearing another's.
        handle = next(_deadline_ids)
        _deadlines[handle] = (deadline, cancel)
        _ensure_thread()
    return handle


def clear_deadline(handle: int) -> None:
    with _lock:
        _deadlines.pop(handle, None)


def _loop() -> None:
    last_lease = 0.0
    while True:
        time.sleep(TICK)
        now = time.monotonic()
        with _lock:
            runs = list(_runs.values())
            slots = list(_slots.values())
            deadlines = list(_deadlines.items())
            watches = list(_watches.values())
        for handle, (deadline, cancel) in deadlines:
            if now >= deadline:
                cancel.cancel("deadline")
                clear_deadline(handle)
        for item in (*runs, *slots):
            if item.deadline is not None and now >= item.deadline and item.cancel is not None:
                item.cancel.cancel("deadline")
        renew = now - last_lease >= LEASE_INTERVAL
        if renew:
            last_lease = now
        try:
            _check_runs(runs, renew)
            if renew and slots:
                stamp = time.time()
                db.executemany("UPDATE inference_queue SET heartbeat_at=? WHERE req_id=?",
                               [(stamp, slot.request_id) for slot in slots])
        except Exception:  # noqa: BLE001 - the supervisor must survive storage hiccups
            log.warning("Supervisor tick failed", exc_info=True)
            db.close_thread_connection()
        for watch in watches:
            try:
                reason = watch.check(renew)
            except Exception:  # noqa: BLE001 - one failing watch must not stop the others
                log.warning("Supervisor watch %s failed", watch.key, exc_info=True)
                db.close_thread_connection()
                continue
            if reason:
                watch.cancel.cancel(reason)


def _check_runs(runs: list[_Run], renew: bool) -> None:
    if not runs:
        return
    placeholders = ",".join("?" for _ in runs)
    rows = {row["session_id"]: row for row in db.query(
        f"SELECT session_id, owner_token, stop_requested FROM active_streams WHERE session_id IN ({placeholders})",
        [run.session_id for run in runs])}
    stamp = time.time()
    for run in runs:
        row = rows.get(run.session_id)
        if row is None or row["owner_token"] != run.owner_token:
            run.cancel.cancel("lease lost")
            continue
        if row["stop_requested"]:
            run.cancel.cancel("stopped")
            continue
        if renew:
            db.execute("UPDATE active_streams SET heartbeat_at=? WHERE session_id=? AND owner_token=?",
                       (stamp, run.session_id, run.owner_token))
