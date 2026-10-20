"""Hard deadlines for request headers and bodies on the compute HTTP servers."""

from __future__ import annotations

import socket
import threading
import time
from collections import deque


class ConnectionDeadlines:
    """Disconnect slow uploads even when each individual read makes progress.

    Socket timeouts only bound an idle read. A small watchdog bounds the whole
    header or body instead; clear the deadline before a model starts streaming
    or an authenticated operation starts processing its complete request.
    """

    def __init__(self):
        self._ends = {}
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="compute-http-deadlines", daemon=True)
        self._thread.start()

    def set(self, connection, seconds: float) -> None:
        with self._lock:
            self._ends[connection] = time.monotonic() + seconds

    def clear(self, connection) -> None:
        with self._lock:
            self._ends.pop(connection, None)

    @staticmethod
    def _disconnect(connections) -> None:
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _watch(self) -> None:
        while not self._closing.wait(0.1):
            now = time.monotonic()
            with self._lock:
                expired = [connection for connection, end in self._ends.items() if end <= now]
                for connection in expired:
                    self._ends.pop(connection, None)
            self._disconnect(expired)

    def close(self) -> None:
        self._closing.set()
        self._thread.join(timeout=1)
        with self._lock:
            connections = list(self._ends)
            self._ends.clear()
        self._disconnect(connections)


class FailureLimiter:
    """Counts failed authentications per peer address within a sliding window.

    After *limit* failures in *window* seconds, ``blocked`` is true for that
    peer: callers answer 429 to further bad tokens without checking them.
    Valid tokens are still accepted, so a noisy neighbour behind the same
    proxy cannot lock the web server out. At most 1024 peers are tracked.
    """

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque] = {}

    def blocked(self, peer: str) -> bool:
        with self._lock:
            return self._blocked(peer)

    def failed(self, peer: str) -> None:
        with self._lock:
            self._count(peer)

    def record(self, peer: str) -> bool:
        """Count a failure unless *peer* is blocked already; False when it is (nothing is counted)."""
        with self._lock:
            if self._blocked(peer):
                return False
            self._count(peer)
            return True

    def _blocked(self, peer: str) -> bool:
        events = self._failures.get(peer)
        if not events:
            return False
        now = self.clock()
        while events and now - events[0] > self.window:
            events.popleft()
        return len(events) >= self.limit

    def _count(self, peer: str) -> None:
        if peer not in self._failures and len(self._failures) >= 1024:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(peer, deque(maxlen=self.limit * 2)).append(self.clock())
