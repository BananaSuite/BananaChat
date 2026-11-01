"""Deadlines for the request bodies Gunicorn reads (``BC_BODY_STALL_TIMEOUT``, ``BC_BODY_MIN_KBPS``).

A gthread worker reads a request body from a blocking socket, in the application's thread, with no deadline: a
client that sends a few bytes every few seconds holds that thread for as long as it likes (AUDIT.md LC1-1). The
reverse proxy's ``read_body`` bounds this to minutes, per connection, which many slow connections still add up.

:func:`guard` wraps the body stream (``wsgi.input``) of each request. A read that gets nothing for *stall* seconds
fails, and so does a body that is waited for longer in total than :data:`GRACE` seconds plus its declared length at
*min_kbps* (a body without a length: :data:`UNSIZED` seconds). The request then ends with ``408`` and the thread
is free again (a timer ends the connection's read side when the total time is up, since a read waits for every
byte it asked for). Only the reads are timed: the socket's own timeout is put back after each one, so the response
writes (:mod:`bananachat.write_timeout`) and WebSocket views are unaffected, and requests without a body never
touch the socket.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from werkzeug.exceptions import RequestTimeout

GRACE = 10.0
UNSIZED = 120.0

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


class _TimedInput:
    """The body stream of one request, with its deadlines.

    The total time counts only the time spent waiting inside reads, not what a handler does between them (a
    body that has arrived whole is never refused because the handler was busy before it read it).
    """

    def __init__(self, stream: Any, sock: Any, stall: float, total: float, length: int) -> None:
        self._stream = stream
        self._sock = sock
        self._stall = stall
        self._total = total
        self._length = length
        self._received = 0
        self._ended = False
        self.expired = False
        self._spent = 0.0  # seconds spent inside finished reads
        self._reading_since: float | None = None
        self._stopped = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()

    def _arm(self, seconds: float) -> None:
        """Called with the lock held."""
        if self._stopped:
            return
        self._timer = threading.Timer(max(seconds, 0.001), self._due)
        self._timer.daemon = True
        self._timer.start()

    def _complete(self) -> bool:
        return self._ended or (0 <= self._length <= self._received)

    def _due(self) -> None:
        """The time allowed for the body may be up: if so, end the read the thread is blocked in (a read waits for
        every byte it asked for, however slowly they come, so only the connection can wake it). Time the handler
        spent away from the body does not count; the timer then waits for what is left."""
        with self._lock:
            if self._stopped or self._complete():
                return
            now = time.monotonic()
            spent = self._spent + (now - self._reading_since if self._reading_since is not None else 0.0)
            left = self._total - spent
            if left > 0.01:
                self._arm(left)
                return
            self.expired = True
            reading = self._reading_since is not None
        if reading:
            try:
                self._sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def _timed(self, call: Callable[..., Any], *args: Any) -> Any:
        with self._lock:
            if self.expired:
                raise RequestTimeout("The request body took too long to arrive.")
            self._reading_since = time.monotonic()
            if self._timer is None:
                self._arm(self._total - self._spent)
        try:
            return self._read(call, *args)
        finally:
            with self._lock:
                if self._reading_since is not None:
                    self._spent += time.monotonic() - self._reading_since
                self._reading_since = None

    def _read(self, call: Callable[..., Any], *args: Any) -> Any:
        try:
            before = self._sock.gettimeout()
            self._sock.settimeout(self._stall)
        except OSError:
            return call(*args)
        try:
            result = call(*args)
        except Exception as error:
            if self.expired or isinstance(error, (TimeoutError, socket.timeout)):
                raise RequestTimeout("The request body took too long to arrive.") from error
            raise
        finally:
            try:
                self._sock.settimeout(before)
            except OSError:
                pass
        size = sum(len(piece) for piece in result) if isinstance(result, list) else len(result)
        self._received += size
        if self.expired and not (0 <= self._length <= self._received):
            raise RequestTimeout("The request body took too long to arrive.")
        if not size:
            self._ended = True
        return result

    def read(self, *args: Any) -> bytes:
        return self._timed(self._stream.read, *args)

    def readline(self, *args: Any) -> bytes:
        return self._timed(self._stream.readline, *args)

    def readlines(self, *args: Any) -> list[bytes]:
        return self._timed(self._stream.readlines, *args)

    def __iter__(self) -> "_TimedInput":
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def guard(app: WSGIApp, stall: float, min_kbps: float) -> WSGIApp:
    """*app*, ending a request whose body stalls for *stall* seconds or arrives slower than *min_kbps* (0: off)."""
    if stall <= 0:
        return app

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        sock = environ.get("gunicorn.socket")
        stream = environ.get("wsgi.input")
        # Only a request that has a body is timed. A WebSocket handshake (a terminal tunnel, say) has none and
        # takes the connection over: its reads must never be ended by a body deadline. An Upgrade header changes
        # nothing here, as the client chooses it.
        chunked = "chunked" in (environ.get("HTTP_TRANSFER_ENCODING") or "").lower()
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if sock is not None and stream is not None and (length > 0 or chunked) \
                and "gunicorn.http2.send_trailers" not in environ:
            if chunked:
                length = -1
            total = UNSIZED if length < 0 or min_kbps <= 0 else GRACE + length / (min_kbps * 1024)
            timed = _TimedInput(stream, sock, stall, total, length)
            environ["wsgi.input"] = timed
            try:
                return app(environ, start_response)
            finally:
                timed.stop()
        return app(environ, start_response)

    return guarded
