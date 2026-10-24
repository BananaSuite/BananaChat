"""A small WebSocket (RFC 6455) server side for the worker tunnels, on the WSGI server's own socket.

Gunicorn's threaded workers (``environ["gunicorn.socket"]``) and Werkzeug's
development server (``environ["werkzeug.socket"]``) hand a view the client's
socket. :func:`accept` answers the opening handshake on it; the view then
exchanges messages and finally returns :func:`finished`, a response that tells
the server the connection was taken over (it writes nothing and closes it).

Only what the tunnels need is implemented: binary and text messages
(fragmented ones included), ping/pong and close. Client frames must be masked;
frames and messages have size limits; a protocol error closes the connection.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading

from werkzeug.wrappers import Response

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0
MAX_FRAME = 1024 * 1024
MAX_MESSAGE = 4 * 1024 * 1024


def _xor(payload: bytes, mask: bytes) -> bytes:
    """Apply a WebSocket mask quickly (one big-integer XOR instead of a Python loop per byte)."""
    size = len(payload)
    if not size:
        return b""
    repeated = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(repeated, "little")).to_bytes(size, "little")


class WebSocketError(Exception):
    """The peer broke the protocol or went away."""


class Closed(WebSocketError):
    """The peer closed the connection (or it dropped)."""


def requested(environ) -> bool:
    upgrade = environ.get("HTTP_UPGRADE", "").lower()
    connection = [part.strip().lower() for part in environ.get("HTTP_CONNECTION", "").split(",")]
    return upgrade == "websocket" and "upgrade" in connection


def raw_socket(environ):
    return environ.get("gunicorn.socket") or environ.get("werkzeug.socket")


SEND_TIMEOUT_SECONDS = 60


def limit_sends(sock, seconds: float = SEND_TIMEOUT_SECONDS) -> None:
    """Make a send fail when the peer has read nothing for *seconds* (``SO_SNDTIMEO``), without a read timeout.

    A peer that stops reading would otherwise block a blocking send, and the thread that sends, for ever. Each
    send call that makes progress starts the time again, so a slow link is fine; a stalled one ends the session.
    """
    whole = int(seconds)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                        struct.pack("ll", whole, int((seconds - whole) * 1_000_000)))
    except (OSError, AttributeError, struct.error):
        pass  # a platform without it: the session's own limits still apply


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")  # noqa: S324


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    # ----- receiving ------------------------------------------------------------------------------------

    def _exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            try:
                chunk = self.sock.recv(min(65536, size - len(data)))
            except socket.timeout:
                raise
            except OSError as error:
                raise Closed(str(error)) from None
            if not chunk:
                raise Closed("the connection closed")
            data.extend(chunk)
        return bytes(data)

    def _frame(self):
        first, second = self._exact(2)
        fin, opcode = bool(first & 0x80), first & 0x0F
        if first & 0x70:
            raise WebSocketError("reserved bits set")
        if not second & 0x80:
            raise WebSocketError("client frames must be masked")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if length > MAX_FRAME:
            raise WebSocketError("frame too large")
        if opcode >= 0x8 and (length > 125 or not fin):
            raise WebSocketError("invalid control frame")
        mask = self._exact(4)
        return fin, opcode, _xor(self._exact(length), mask)

    def receive(self):
        """The next message as ``(opcode, payload)`` (``TEXT`` or ``BINARY``); raises :class:`Closed` at the end."""
        message, kind = bytearray(), None
        while True:
            fin, opcode, payload = self._frame()
            if opcode == PING:
                self._send(PONG, payload)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self.close(1000)
                raise Closed("closed by the peer")
            if opcode in (TEXT, BINARY):
                if kind is not None:
                    raise WebSocketError("a new message started inside a fragmented one")
                kind = opcode
            elif opcode == CONTINUATION:
                if kind is None:
                    raise WebSocketError("continuation without a message")
            else:
                raise WebSocketError("unknown opcode")
            message.extend(payload)
            if len(message) > MAX_MESSAGE:
                raise WebSocketError("message too large")
            if fin:
                return kind, bytes(message)

    # ----- sending --------------------------------------------------------------------------------------

    def _send(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        with self._send_lock:
            if self.closed and opcode != CLOSE:
                raise Closed("already closed")
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError as error:
                raise Closed(str(error)) from None

    def send_binary(self, payload: bytes) -> None:
        for start in range(0, max(1, len(payload)), MAX_FRAME):
            self._send(BINARY, payload[start:start + MAX_FRAME])

    def send_text(self, text: str) -> None:
        self._send(TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self._send(PING, payload[:125])

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self._send(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        except Closed:
            pass
        self.closed = True


def accept(environ, *, protocols=()) -> WebSocket:
    """Answer the opening handshake; raises ValueError when the request is not one."""
    if environ.get("REQUEST_METHOD") != "GET" or not requested(environ):
        raise ValueError("not a WebSocket request")
    if environ.get("HTTP_SEC_WEBSOCKET_VERSION") != "13":
        raise ValueError("unsupported WebSocket version")
    key = environ.get("HTTP_SEC_WEBSOCKET_KEY", "")
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid WebSocket key") from None
    sock = raw_socket(environ)
    if sock is None:
        raise ValueError("this server cannot hand over the connection")
    lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Accept: {accept_key(key)}"]
    offered = [item.strip() for item in environ.get("HTTP_SEC_WEBSOCKET_PROTOCOL", "").split(",") if item.strip()]
    chosen = next((item for item in offered if item in protocols), None)
    if chosen:
        lines.append(f"Sec-WebSocket-Protocol: {chosen}")
    sock.settimeout(None)
    limit_sends(sock)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    return WebSocket(sock)


class _Finished(Response):
    """What a view returns after it took the connection over: the server must not write a response."""

    def __call__(self, environ, start_response):
        if "gunicorn.socket" in environ:
            raise StopIteration()  # gunicorn closes the connection without writing anything
        raise ConnectionError("WebSocket finished")  # Werkzeug: a dropped connection


def finished() -> Response:
    return _Finished()


def random_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")
