"""A stall timeout for the responses Gunicorn writes (``BC_WRITE_TIMEOUT``).

Gunicorn has no write timeout: a gthread worker writes a response with
blocking ``sendall`` (and ``sendfile``) calls, so a client that stops reading
a stream or a download holds a server thread, and the long-request place that
comes with it (``security.long_request``), for as long as it keeps the
connection open. Answers themselves no longer wait for the client
(``services.event_pump``), but the thread would.

:func:`guard` sets a timeout on the client connection once the application has
returned its response, so request bodies are read as before (Gunicorn makes
the socket blocking again for the next request on the connection). A write
that has to wait that long for room in the connection then fails, Gunicorn
closes the connection, the response is closed (which ends its work and
charges what it produced) and the thread is free again.

``sendall`` applies the timeout to the whole call, so a generated body is
handed to Gunicorn at most :data:`MAX_WRITE` bytes at a time. ``sendfile``
(``wsgi.file_wrapper``, left untouched so files stay zero-copy) fills the whole
kernel send buffer, and the kernel reports a TCP socket writable again only
once a third of it has drained; on TCP connections the bytes queued but not
yet sent are therefore kept near :data:`UNSENT` (``TCP_NOTSENT_LOWAT``), which
does not limit the data in flight. Server-sent events are not corked or
buffered: each piece still leaves at once.

HTTP/2 streams share their connection, so they are left alone (the shipped
configuration serves HTTP/1.1 to the reverse proxy). WebSocket views take the
connection over before they return and set their own send timeout
(``services.websocket``).
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable, Iterator
from typing import Any

MAX_WRITE = 64 * 1024
UNSENT = 2 * MAX_WRITE
# Bytes a connection may hold in the kernel (Linux doubles it, within net.core.wmem_max).
SEND_BUFFER = 256 * 1024
# Only HTTP/2 streams have it; no request header maps to it.
_HTTP2 = "gunicorn.http2.send_trailers"

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def _set(sock: Any, name: str, value: int) -> bool:
    """A TCP option on a TCP connection, where the platform has it."""
    option = getattr(socket, name, None)
    if option is None or getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError:
        return False
    return True


def _send_buffer(sock: Any) -> None:
    """Room in the kernel for any small response (under security.LARGE_BODY), whatever the client reads.

    The send buffer starts at about 16 KiB and only grows with the client's window, so a client that
    advertises a tiny window and reads slowly would hold the thread even for a small page.
    """
    if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER)
    except OSError:
        pass


class _Pieces:
    """The body in writes of at most :data:`MAX_WRITE` bytes."""

    def __init__(self, body: Iterable[bytes]) -> None:
        self._body = body

    def __iter__(self) -> Iterator[bytes]:
        for item in self._body:
            if len(item) <= MAX_WRITE:
                yield item
            else:
                for start in range(0, len(item), MAX_WRITE):
                    yield item[start:start + MAX_WRITE]

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if close is not None:
            close()


def guard(app: WSGIApp, timeout: float) -> WSGIApp:
    """*app*, giving up on a client that takes in nothing for *timeout* seconds (0: wait forever)."""
    if timeout <= 0:
        return app

    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        body = app(environ, start_response)
        sock = environ.get("gunicorn.socket")
        if sock is None or _HTTP2 in environ:
            return body
        try:
            sock.settimeout(timeout)
        except OSError:
            return body
        _set(sock, "TCP_NOTSENT_LOWAT", UNSENT)
        _send_buffer(sock)
        wrapper = environ.get("wsgi.file_wrapper")
        if isinstance(wrapper, type) and isinstance(body, wrapper):
            return body  # Gunicorn sends it with sendfile only while it is the file wrapper itself
        return _Pieces(body)

    return guarded
