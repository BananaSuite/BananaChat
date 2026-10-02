"""Hard deadlines for request headers and bodies on the compute HTTP servers."""

from __future__ import annotations

import socket
import threading
import time


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
