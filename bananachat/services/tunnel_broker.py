"""The relay between the two ends of a worker tunnel, across Gunicorn's worker processes.

A remote session has two WebSocket connections that may reach different
server processes: the worker's (``/worker/v1/tunnel/<id>``) and the
administrator's (the browser terminal or VNC viewer, or the connect helper).
Each process that receives one connects it to the **broker**, a small TCP
listener on ``127.0.0.1`` run by the process that leads the background
services. The broker pairs the two connections of a session and copies bytes
between them; it never looks inside.

Between a process and the broker every WebSocket message travels as a frame
(``type``, 4-byte length, payload), so binary data, text control messages and
the close stay distinct end to end.

A connection to the broker starts with a mutual challenge, both sides proving
they know a key derived from the installation's secret key, so another local
user can neither join a session, nor replay a captured opening, nor pose as
the broker on its port after it stopped:

1. the process sends ``BCT2 <client nonce>``;
2. the broker answers ``BCT2 <broker nonce> <HMAC("broker", client nonce, broker nonce)>``;
3. the process checks it and sends ``<role> <session> <HMAC(role, session, broker nonce, client nonce)>``;
4. the broker answers ``OK`` once both sides of the session are there.

At most ``MAX_GREETINGS`` connections may be in steps 1-3 at once (others are
closed at once), and each has ``GREETING_SECONDS`` for all of them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import select
import socket
import struct
import threading
import time

from bananachat import db
from bananachat.services import background
from bananachat.services.websocket import TEXT, Closed, WebSocketError

log = logging.getLogger("bananachat.tunnels")

STATE_KEY = "tunnel_broker"
PAIR_SECONDS = 60            # how long one side waits for the other
ANNOUNCE_SECONDS = 15
STALE_SECONDS = 60
MAX_PAIRS = 64
MAX_GREETINGS = 2 * MAX_PAIRS  # connections still proving the key; any more are closed at once
GREETING_SECONDS = 10        # for the whole challenge, however slowly its bytes arrive
MAX_FRAME = 1024 * 1024
KEEPALIVE_SECONDS = 25       # below Cloudflare's 100 s idle limit for WebSockets
CHECK_SECONDS = 2            # how often an idle relay checks whether it should stop
IDLE_SECONDS = 2 * 3600      # a session without traffic for this long ends
LONGEST_SECONDS = 12 * 3600  # and no session lasts longer than this
DATA, CONTROL, CLOSE = 1, 2, 3
_HEADER = struct.Struct("!BI")
_GREETING = re.compile(rb"^BCT2 ([0-9a-f]{32})\n$")
_CHALLENGE = re.compile(rb"^BCT2 ([0-9a-f]{32}) ([0-9a-f]{64})\n$")
_JOIN = re.compile(rb"^(worker|client) ([0-9a-f]{32}) ([0-9a-f]{64})\n$")
ROLES = ("worker", "client")


class BrokerUnavailable(RuntimeError):
    """No process runs the relay (the background services are not running)."""


def _key(secret_key) -> bytes:
    secret = secret_key.encode("utf-8") if isinstance(secret_key, str) else bytes(secret_key)
    return hmac.new(secret, b"bananachat-tunnel-broker-v1", hashlib.sha256).digest()


def _mac(key: bytes, *parts: str) -> str:
    return hmac.new(key, ":".join(parts).encode("ascii"), hashlib.sha256).hexdigest()


def _line(sock, limit: int = 200, deadline: float | None = None) -> bytes:
    """One line of at most *limit* bytes; with *deadline* (``time.monotonic()``), all of it must arrive by then."""
    line = bytearray()
    while not line.endswith(b"\n") and len(line) < limit:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("the greeting took too long")
            sock.settimeout(remaining)
        chunk = sock.recv(1)
        if not chunk:
            break
        line += chunk
    return bytes(line)


# ----- the broker ---------------------------------------------------------------------------------------------

class Broker:
    def __init__(self, key: bytes, host: str = "127.0.0.1"):
        self.key = key
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, 0))
        self.listener.listen(64)
        self.listener.settimeout(1.0)
        self.port = self.listener.getsockname()[1]
        self.lock = threading.Lock()
        self.waiting: dict[str, dict[str, tuple[socket.socket, threading.Event]]] = {}
        self.pairs = 0
        # Only pairs count against MAX_PAIRS, so connections that have not proven the key yet are bounded here.
        self.greetings = threading.BoundedSemaphore(MAX_GREETINGS)

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass
        with self.lock:
            waiting, self.waiting = self.waiting, {}
        for slot in waiting.values():
            for sock, ready in slot.values():
                _close(sock)
                ready.set()

    def serve(self, stopping, announce=None) -> None:
        announced = 0.0
        while not stopping():
            if announce is not None and time.monotonic() - announced >= ANNOUNCE_SECONDS:
                announce(self.port)
                announced = time.monotonic()
            try:
                connection, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if stopping():
                    break
                raise
            if not self.greetings.acquire(blocking=False):
                _close(connection)
                continue
            try:
                threading.Thread(target=self._handle, args=(connection,), name="bananachat-tunnel-join",
                                 daemon=True).start()
            except RuntimeError:  # no thread to be had: drop this one, keep serving
                self.greetings.release()
                _close(connection)

    def _hello(self, connection) -> tuple[str, str] | None:
        deadline = time.monotonic() + GREETING_SECONDS
        greeting = _GREETING.fullmatch(_line(connection, deadline=deadline))
        if greeting is None:
            return None
        theirs, mine = greeting.group(1).decode("ascii"), os.urandom(16).hex()
        connection.sendall(f"BCT2 {mine} {_mac(self.key, 'broker', theirs, mine)}\n".encode("ascii"))
        join = _JOIN.fullmatch(_line(connection, deadline=deadline))
        if join is None:
            return None
        role, session_id, sent = (part.decode("ascii") for part in join.groups())
        if not hmac.compare_digest(sent, _mac(self.key, role, session_id, mine, theirs)):
            return None
        return role, session_id

    def _handle(self, connection) -> None:
        try:
            hello = self._hello(connection)
        except OSError:
            hello = None
        finally:
            self.greetings.release()
        if hello is None:
            _close(connection)
            return
        role, session_id = hello
        ready = threading.Event()
        peer = None
        with self.lock:
            slot = self.waiting.setdefault(session_id, {})
            if role in slot or self.pairs >= MAX_PAIRS:
                _close(connection)
                if not slot:
                    self.waiting.pop(session_id, None)
                return
            other = "client" if role == "worker" else "worker"
            if other in slot:
                peer, peer_ready = slot.pop(other)
                self.waiting.pop(session_id, None)
                self.pairs += 1
            else:
                slot[role] = (connection, ready)
        if peer is None:
            if not ready.wait(PAIR_SECONDS):
                with self.lock:
                    slot = self.waiting.get(session_id)
                    mine = slot.get(role) if slot else None
                    if mine is not None and mine[0] is connection:
                        slot.pop(role)
                        if not slot:
                            self.waiting.pop(session_id, None)
                        timed_out = True
                    else:
                        timed_out = False  # paired at the last moment: the other thread runs the session
                if timed_out:
                    try:
                        connection.sendall(b"TIMEOUT\n")
                    except OSError:
                        pass
                    _close(connection)
            return
        peer_ready.set()  # the waiting side's thread ends; this one runs the session
        try:
            for sock in (connection, peer):
                sock.settimeout(None)
                sock.sendall(b"OK\n")
            _splice(connection, peer)
        except OSError:
            pass
        finally:
            _close(connection)
            _close(peer)
            with self.lock:
                self.pairs -= 1


def _close(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _splice(first, second) -> None:
    def pump(source, target):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    other = threading.Thread(target=pump, args=(second, first), name="bananachat-tunnel-pump", daemon=True)
    other.start()
    pump(first, second)
    other.join(5)


# ----- running it -------------------------------------------------------------------------------------------

def _announce(app, port: int) -> None:
    from bananachat.db import settings as site_settings

    with app.app_context():
        try:
            site_settings.state_set(STATE_KEY, {"port": port, "pid": os.getpid(), "at": time.time()})
        finally:
            db.release_thread_connection()


@background.job("tunnel-broker", every=5, long_running=True, initial_delay=1)
def run_broker(app) -> None:
    """Leader only, while remote access is on: relay worker tunnels until the services stop."""
    if not app.config["BC"].worker_remote_access:
        return
    broker = Broker(_key(app.secret_key))
    log.info("Worker tunnel relay listening on 127.0.0.1:%d", broker.port)
    try:
        broker.serve(background.stopping, lambda port: _announce(app, port))
    finally:
        broker.close()


def connect(app, role: str, session_id: str) -> socket.socket:
    """Join a session at the broker; returns the socket once the other side is there (``OK``)."""
    from bananachat.db import settings as site_settings

    state = site_settings.state_get(STATE_KEY) or {}
    port, at = state.get("port"), state.get("at")
    if not isinstance(port, int) or not isinstance(at, (int, float)) or time.time() - at > STALE_SECONDS:
        raise BrokerUnavailable("The tunnel relay is not running (no process runs the background services).")
    key = _key(app.secret_key)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        raise BrokerUnavailable("The tunnel relay cannot be reached.") from None
    try:
        mine = os.urandom(16).hex()
        sock.sendall(f"BCT2 {mine}\n".encode("ascii"))
        challenge = _CHALLENGE.fullmatch(_line(sock))
        if challenge is None:
            raise OSError("no challenge")
        theirs, proof = (part.decode("ascii") for part in challenge.groups())
        if not hmac.compare_digest(proof, _mac(key, "broker", mine, theirs)):
            log.warning("Something that is not the tunnel relay listens on 127.0.0.1:%d; session refused", port)
            raise OSError("not the relay")
        sock.sendall(f"{role} {session_id} {_mac(key, role, session_id, theirs, mine)}\n".encode("ascii"))
    except OSError:
        _close(sock)
        raise BrokerUnavailable("The tunnel relay cannot be reached.") from None
    from bananachat.services.websocket import limit_sends

    limit_sends(sock)
    return sock


def wait_paired(sock, timeout: float = PAIR_SECONDS + 5, stopping=None) -> bool:
    """Wait for the broker's ``OK``: True once the other side joined, False when it did not come."""
    deadline = time.monotonic() + timeout
    line = bytearray()
    while time.monotonic() < deadline:
        if stopping is not None and stopping():
            return False
        sock.settimeout(min(1.0, max(0.05, deadline - time.monotonic())))
        try:
            chunk = sock.recv(1)
        except socket.timeout:
            continue
        except OSError:
            return False
        if not chunk:
            return False
        line += chunk
        if line.endswith(b"\n"):
            return bytes(line) == b"OK\n"
        if len(line) > 16:
            return False
    return False


# ----- bridging a WebSocket to the broker ---------------------------------------------------------------------

def _exact(sock, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise Closed("the relay closed")
        data += chunk
    return bytes(data)


def send_frame(sock, kind: int, payload: bytes) -> None:
    sock.sendall(_HEADER.pack(kind, len(payload)) + payload)


def bridge(ws, sock, *, stopping=None) -> dict:
    """Copy messages between a WebSocket and the broker until either ends. Returns ``{"up", "down", "reason"}``:
    bytes from the WebSocket's peer and to it."""
    counters = {"up": 0, "down": 0, "reason": "", "last": time.monotonic()}
    started = time.monotonic()

    def upstream():
        try:
            while True:
                kind, payload = ws.receive()
                counters["up"] += len(payload)
                counters["last"] = time.monotonic()
                send_frame(sock, CONTROL if kind == TEXT else DATA, payload)
        except (Closed, WebSocketError, OSError) as error:
            counters["reason"] = counters["reason"] or f"client: {error}"
        finally:
            try:
                send_frame(sock, CLOSE, b"")
            except OSError:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    sock.settimeout(None)  # blocking: reads wait in select() below, sends are bounded by limit_sends
    reader = threading.Thread(target=upstream, name="bananachat-tunnel-up", daemon=True)
    reader.start()
    pinged = checked = time.monotonic()
    try:
        while True:
            # Checked every few seconds, never before what the other side already sent was passed on (its end
            # may mark the session closed in the database a moment before its last words arrive here).
            if stopping is not None and time.monotonic() - checked >= CHECK_SECONDS:
                checked = time.monotonic()
                if stopping():
                    counters["reason"] = "ended by an administrator or a restart"
                    break
            now = time.monotonic()
            if now - counters["last"] > IDLE_SECONDS or now - started > LONGEST_SECONDS:
                counters["reason"] = "time limit"
                break
            ready, _, _ = select.select([sock], [], [], CHECK_SECONDS)
            if not ready:
                if time.monotonic() - pinged >= KEEPALIVE_SECONDS:
                    ws.ping(b"bc")
                    pinged = time.monotonic()
                continue
            kind, length = _HEADER.unpack(_exact(sock, _HEADER.size))
            if length > MAX_FRAME or kind not in (DATA, CONTROL, CLOSE):
                counters["reason"] = "invalid relay frame"
                break
            payload = _exact(sock, length) if length else b""
            if kind == CLOSE:
                counters["reason"] = counters["reason"] or "closed by the other side"
                break
            counters["down"] += len(payload)
            counters["last"] = time.monotonic()
            if kind == CONTROL:
                ws.send_text(payload.decode("utf-8", "replace"))
            else:
                ws.send_binary(payload)
    except (Closed, WebSocketError, OSError) as error:
        counters["reason"] = counters["reason"] or str(error)
    finally:
        ws.close(1000)
        try:
            ws.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        _close(sock)
        reader.join(5)
    counters.pop("last", None)
    return counters


# Per-process limit on WebSockets held by tunnels (each holds a server thread).
_slots = {"used": 0}
_slots_lock = threading.Lock()


def take_slot(limit: int, threads: int | None = None) -> bool:
    """Take one of this process's tunnel slots; never more than the threads that long requests may hold."""
    if threads is not None:
        from bananachat.security import RESERVED_THREADS

        limit = min(limit, max(1, threads - RESERVED_THREADS))
    with _slots_lock:
        if _slots["used"] >= limit:
            return False
        _slots["used"] += 1
        return True


def release_slot() -> None:
    with _slots_lock:
        _slots["used"] = max(0, _slots["used"] - 1)
