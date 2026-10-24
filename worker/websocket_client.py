"""A small WebSocket client (RFC 6455) on the standard library, for remote-access tunnels.

Like :mod:`.transport` it ignores proxy variables and never follows a
redirect, so the worker token reaches only ``BC_SERVER_URL``. Frames are
masked as the protocol requires; pings are answered; messages are limited in
size.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 * 1024 * 1024
USER_AGENT = "BananaChat-worker/2"


def _xor(payload: bytes, mask: bytes) -> bytes:
    """Apply a WebSocket mask quickly (one big-integer XOR instead of a Python loop per byte)."""
    size = len(payload)
    if not size:
        return b""
    repeated = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(repeated, "little")).to_bytes(size, "little")


class WebSocketError(Exception):
    """The connection failed or broke the protocol. ``status`` is the HTTP status of a refused handshake."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Closed(WebSocketError):
    """The other side closed the connection."""


class WebSocket:
    def __init__(self, sock, buffered: bytes = b""):
        self.sock = sock
        self._buffer = bytearray(buffered)
        self._send_lock = threading.Lock()
        self.closed = False

    # ----- reading ---------------------------------------------------------------------------------
    def _exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise
            except OSError as error:
                raise Closed(f"the connection broke: {error}") from None
            if not chunk:
                raise Closed("the connection closed")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _frame(self) -> Tuple[bool, int, bytes]:
        first, second = self._exact(2)
        if second & 0x80:
            raise WebSocketError("the server masked a frame")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if length > MAX_MESSAGE:
            raise WebSocketError("frame too large")
        return bool(first & 0x80), first & 0x0F, self._exact(length)

    def receive(self) -> Tuple[int, bytes]:
        """The next message as ``(TEXT or BINARY, payload)``; raises :class:`Closed` at the end."""
        message, kind = bytearray(), None
        while True:
            fin, opcode, payload = self._frame()
            if opcode == PING:
                self._send(PONG, payload)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self.close()
                raise Closed("closed by the server")
            if opcode in (TEXT, BINARY):
                kind = opcode
            elif opcode != CONTINUATION or kind is None:
                raise WebSocketError("unexpected frame")
            message.extend(payload)
            if len(message) > MAX_MESSAGE:
                raise WebSocketError("message too large")
            if fin:
                return kind, bytes(message)

    # ----- writing ---------------------------------------------------------------------------------
    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        masked = _xor(payload, mask)
        with self._send_lock:
            if self.closed and opcode != CLOSE:
                raise Closed("already closed")
            try:
                self.sock.sendall(bytes(header) + mask + masked)
            except OSError as error:
                raise Closed(f"the connection broke: {error}") from None

    def send_binary(self, payload: bytes) -> None:
        for start in range(0, max(1, len(payload)), 65536):
            self._send(BINARY, payload[start:start + 65536])

    def send_text(self, text: str) -> None:
        self._send(TEXT, text.encode("utf-8"))

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._send(CLOSE, struct.pack("!H", 1000))
        except Closed:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def connect(base_url: str, path: str, headers: Optional[Dict[str, str]] = None, *, timeout: float = 20,
            context: Optional[ssl.SSLContext] = None) -> WebSocket:
    """Open a WebSocket to ``base_url + path`` (an http(s) base address)."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebSocketError(f"Unsupported address: {base_url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
    except OSError as error:
        raise WebSocketError(f"The connection to {parts.hostname} failed: {error}") from None
    sock = raw
    try:
        if parts.scheme == "https":
            sock = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        lines = [f"GET {parts.path.rstrip('/')}{path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                 f"User-Agent: {USER_AGENT}"]
        for name, value in (headers or {}).items():
            if any(char in f"{name}{value}" for char in "\r\n"):
                raise WebSocketError("Invalid header.")
            lines.append(f"{name}: {value}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("The server closed the connection during the handshake.")
            response.extend(chunk)
            if len(response) > 65536:
                raise WebSocketError("The handshake answer is too large.")
        head, _, rest = bytes(response).partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(status_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            raise WebSocketError("The server sent an invalid handshake answer.") from None
        received = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            received[name.strip().lower()] = value.strip()
        if status != 101:
            body = rest[:600].decode("utf-8", "replace").strip()
            raise WebSocketError(f"HTTP {status}{': ' + body if body else ''}", status)
        expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")  # noqa: S324
        if received.get("sec-websocket-accept") != expected or received.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("The server did not accept the WebSocket.")
        sock.settimeout(None)
        return WebSocket(sock, rest)
    except (OSError, ssl.SSLError) as error:
        try:
            sock.close()
        except OSError:
            pass
        raise WebSocketError(f"The connection failed: {error}") from None
    except WebSocketError:
        try:
            sock.close()
        except OSError:
            pass
        raise
