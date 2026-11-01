"""Run a streamed response's work in a thread of its own, so a client that stops reading cannot hold it up.

A WSGI response that generates its answer while it writes it stops generating
as soon as a write blocks: a client that opens a stream and stops reading would
keep the request's inference queue place (and its generation deadline, which is
only checked between pieces of the answer) for as long as it keeps the
connection open. :class:`EventPump` iterates the work's events in a background
thread instead and keeps them for the response, which only drains them. The
work, its deadline, its queue place and its charging no longer depend on the
client; the response thread itself is freed by the server's write timeout
(``BC_WRITE_TIMEOUT``, see :mod:`bananachat.write_timeout`).

Events that continue the previous one (the next piece of an answer, a new
queue position) are grouped with it (*merge*) and handed to the reader as one
(*join*), so a reader that falls behind costs about the size of the answer,
which inference already bounds. A reader that falls behind by more than
:data:`MAX_EVENTS` groups gets its work cancelled.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator

from bananachat import db
from bananachat.services.upstream import CancelToken

log = logging.getLogger("bananachat.event_pump")

MAX_EVENTS = 10_000
# How long close() waits for the work to notice its cancellation and finish (it charges what was produced).
CLOSE_WAIT = 10.0


class EventPump:
    """Iterate *source* in a thread with *app*'s context; :meth:`drain` yields its events.

    ``merge(last, event)`` tells whether *event* continues *last* (both unread); ``join(events)`` turns such a
    run of events into the one the reader gets. An exception raised by *source* is raised again by :meth:`drain`
    after the events before it. :meth:`close` before the reader got to the end (the client went away, or the
    server gave up writing to it) cancels *cancel* and waits up to :data:`CLOSE_WAIT` seconds for the work to end.
    """

    def __init__(self, app, source: Iterator, *, cancel: CancelToken, merge: Callable | None = None,
                 join: Callable | None = None, name: str = "stream"):
        self._app = app
        self._source = source
        self._cancel = cancel
        self._merge = merge
        self._join = join or (lambda events: events[-1])
        self._items: deque = deque()
        self._condition = threading.Condition()
        self._finished = False
        self._drained = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._main, name=f"pump-{name}", daemon=True)

    def start(self) -> EventPump:
        self._thread.start()
        return self

    # the work ---------------------------------------------------------------------
    def _main(self) -> None:
        with self._app.app_context():
            try:
                for event in self._source:
                    self._push(event)
            except BaseException as error:  # noqa: BLE001 - handed to the reader
                self._error = error
            finally:
                try:
                    close = getattr(self._source, "close", None)
                    if close is not None:
                        close()
                except Exception:  # noqa: BLE001 - the work's own cleanup already ran or logged
                    log.exception("Closing a streamed response's work failed")
                with self._condition:
                    self._finished = True
                    self._condition.notify_all()
                db.close_thread_connection()

    def _push(self, event) -> None:
        with self._condition:
            group = self._items[-1] if self._items else None
            if group is not None and self._merge is not None and self._merge(group[-1], event):
                group.append(event)
            else:
                self._items.append([event])
            overflow = len(self._items) > MAX_EVENTS
            self._condition.notify_all()
        if overflow:
            # The reader is hopelessly behind: end the work (it charges what it produced and frees its place).
            self._cancel.cancel("reader too slow")

    # the response -----------------------------------------------------------------
    def drain(self):
        """The events in order; then the work's exception, if it raised one."""
        while True:
            with self._condition:
                while not self._items and not self._finished:
                    self._condition.wait()
                if self._items:
                    group = self._items.popleft()
                else:
                    self._drained = True
                    error, self._error = self._error, None
                    if error is not None:
                        raise error
                    return
            yield group[0] if len(group) == 1 else self._join(group)

    def close(self) -> None:
        if not self._drained:
            self._cancel.cancel("disconnected")
        self._thread.join(CLOSE_WAIT)
        with self._condition:
            self._items.clear()
