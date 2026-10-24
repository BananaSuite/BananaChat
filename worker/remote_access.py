"""Maintenance access for the server's administrators: tunnels this PC opens to the server, on request.

Off unless the owner sets ``BC_WORKER_REMOTE_ACCESS=1``. Then the worker tells
the server, in its heartbeat, what may be reached: the services of this PC
listed in ``BC_WORKER_REMOTE_SERVICES`` (its own SSH, VNC or RDP server, by
default ``ssh:22,vnc:5900,rdp:3389`` on this computer), a built-in terminal
when ``BC_WORKER_REMOTE_SHELL=1`` (a shell as the account the worker runs as,
with no further password: enable it only if you trust the server's
administrators with that account), and machines of the owner's network listed
in ``BC_WORKER_REMOTE_SUBNETS`` on ``BC_WORKER_REMOTE_SUBNET_PORTS``.

While an administrator works with it, the server *arms* the worker (its
heartbeat reply says so) and the worker long-polls for sessions. For each one
it checks the request against the owner's settings again, connects to the
service (or starts the terminal) and opens a WebSocket to the server, which
relays it to the administrator. No port is opened on this PC's network: the
worker only connects out, like for chat requests. Every session is logged.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import websocket_client
from .client import ServerError

log = logging.getLogger("bananachat.worker.remote")

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_SESSIONS = 8
PROBE_SECONDS = 60
MIN_POLL_SECONDS = 3.0
DATA, CONTROL, CLOSE = 1, 2, 3


def _probe(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class RemoteAccess:
    def __init__(self, settings, client, *, entry_point: Optional[str] = None):
        self.settings = settings
        self.client = client
        self.armed = False
        self.poll_seconds = 20
        self.entry_point = entry_point or str(Path(__file__).resolve().with_name("bananachat_worker.py"))
        self._sessions: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._probed = (0.0, [])
        self._disabled_until = 0.0

    # ----- what the heartbeat says ----------------------------------------------------------------
    def services(self) -> List[dict]:
        at, cached = self._probed
        if time.monotonic() - at < PROBE_SECONDS:
            return cached
        found = [{"kind": kind, "port": port, "listening": _probe(port)} for kind, port in self.settings.remote_services]
        self._probed = (time.monotonic(), found)
        return found

    def advertise(self) -> dict:
        if not self.settings.remote_access:
            return {"enabled": False}
        return {"enabled": True, "shell": bool(self.settings.remote_shell), "services": self.services(),
                "subnets": list(self.settings.remote_subnets), "subnet_ports": list(self.settings.remote_subnet_ports),
                "platform": platform.system()}

    def heartbeat_reply(self, reply: dict) -> None:
        remote = reply.get("remote") if isinstance(reply, dict) else None
        if isinstance(remote, dict):
            self.armed = remote.get("armed") is True
            seconds = remote.get("poll_seconds")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 1 <= seconds <= 60:
                self.poll_seconds = float(seconds)
        else:
            self.armed = False

    # ----- the owner's rules ----------------------------------------------------------------------
    def refusal(self, session: dict) -> Optional[str]:
        """Why the owner's settings do not allow this session (None: allowed)."""
        settings = self.settings
        if not settings.remote_access:
            return "Remote access is off on this worker (BC_WORKER_REMOTE_ACCESS)."
        kind, host, port = session.get("kind"), str(session.get("host") or ""), session.get("port")
        if kind == "shell":
            return None if settings.remote_shell else "The built-in terminal is off on this worker (BC_WORKER_REMOTE_SHELL)."
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return "Invalid port."
        if host in LOCAL_HOSTS:
            if any(port == allowed for _kind, allowed in settings.remote_services):
                return None
            return f"Port {port} of this PC is not allowed (BC_WORKER_REMOTE_SERVICES)."
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "Only IP addresses can be reached."
        if not any(address in ipaddress.ip_network(network) for network in settings.remote_subnets):
            return f"{host} is not in a network this worker shares (BC_WORKER_REMOTE_SUBNETS)."
        if port not in settings.remote_subnet_ports:
            return f"Port {port} is not allowed on the shared network (BC_WORKER_REMOTE_SUBNET_PORTS)."
        return None

    # ----- the loop -------------------------------------------------------------------------------
    def run(self, stop_event: threading.Event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            if not self.settings.remote_access or not self.armed or time.monotonic() < self._disabled_until:
                stop_event.wait(1.0)
                continue
            started = time.monotonic()
            try:
                sessions = self.client.remote_poll()
            except ServerError as error:
                if error.status == 404:
                    self.armed = False  # the server has remote access off; the heartbeat arms us again later
                    self._disabled_until = time.monotonic() + 60
                else:
                    log.debug("Remote access poll failed: %s", error)
                stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            backoff = 1.0
            for session in sessions:
                self.start(session)
            if not sessions and time.monotonic() - started < MIN_POLL_SECONDS:
                stop_event.wait(MIN_POLL_SECONDS)  # the server answered at once (busy): do not hammer it

    def start(self, session: dict) -> bool:
        session_id = session.get("id")
        if not isinstance(session_id, str) or len(session_id) != 32:
            return False
        with self._lock:
            for key, thread in list(self._sessions.items()):
                if not thread.is_alive():
                    self._sessions.pop(key)
            if len(self._sessions) >= MAX_SESSIONS or session_id in self._sessions:
                log.warning("Remote session %s refused: too many sessions", session_id[:8])
                return False
            thread = threading.Thread(target=self._run_session, args=(session,), name=f"remote-{session_id[:8]}",
                                      daemon=True)
            self._sessions[session_id] = thread
        thread.start()
        return True

    def _connect(self, session_id: str):
        return websocket_client.connect(self.settings.server_url, f"/worker/v1/tunnel/{session_id}",
                                        {"Authorization": f"Bearer {self.settings.token}"})

    def _run_session(self, session: dict) -> None:
        session_id = session["id"]
        kind = session.get("kind")
        target = "terminal" if kind == "shell" else f"{session.get('host')}:{session.get('port')}"
        problem = self.refusal(session)
        try:
            ws = self._connect(session_id)
        except websocket_client.WebSocketError as error:
            log.warning("Remote session %s (%s): could not reach the server: %s", session_id[:8], target, error)
            return
        if problem:
            log.warning("Remote session %s (%s) refused: %s", session_id[:8], target, problem)
            self._error(ws, problem)
            return
        log.info("Remote session %s: %s opened by an administrator", session_id[:8], target)
        try:
            if kind == "shell":
                self._shell(ws)
            else:
                self._tcp(ws, str(session.get("host")), int(session.get("port")))
        except Exception as error:  # noqa: BLE001 - one session must never stop the worker
            log.warning("Remote session %s ended with an error: %s", session_id[:8], error)
        finally:
            ws.close()
            log.info("Remote session %s closed", session_id[:8])

    @staticmethod
    def _error(ws, message: str) -> None:
        try:
            ws.send_text(json.dumps({"type": "error", "message": message}))
        except websocket_client.WebSocketError:
            pass
        ws.close()

    # ----- a TCP service --------------------------------------------------------------------------
    def _tcp(self, ws, host: str, port: int) -> None:
        try:
            sock = socket.create_connection(("127.0.0.1" if host == "localhost" else host, port), timeout=10)
        except OSError as error:
            self._error(ws, f"Nothing answers on {host}:{port} ({error.strerror or error}).")
            return
        sock.settimeout(None)

        def upstream():
            try:
                while True:
                    kind, payload = ws.receive()
                    if kind == websocket_client.BINARY:
                        sock.sendall(payload)
            except (websocket_client.WebSocketError, OSError):
                pass
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        reader = threading.Thread(target=upstream, daemon=True)
        reader.start()
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                ws.send_binary(data)
        except (websocket_client.WebSocketError, OSError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
            ws.close()
            reader.join(5)

    # ----- the built-in terminal -----------------------------------------------------------------
    def _shell(self, ws) -> None:
        from . import shell_host

        environment = {key: value for key, value in os.environ.items() if not key.startswith("BC_")}
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                   "env": environment, "cwd": os.path.expanduser("~")}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen([sys.executable, self.entry_point, "shell-host"], **options)
        except OSError as error:
            self._error(ws, f"The terminal could not start: {error}")
            return
        writer = shell_host.Writer(process.stdin)

        def upstream():
            try:
                while True:
                    kind, payload = ws.receive()
                    if kind == websocket_client.BINARY:
                        writer.frame(DATA, payload)
                    elif kind == websocket_client.TEXT and len(payload) <= 4096:
                        writer.frame(CONTROL, payload)
            except (websocket_client.WebSocketError, OSError, ValueError):
                pass
            finally:
                try:
                    writer.frame(CLOSE)
                except (OSError, ValueError):
                    pass

        reader = threading.Thread(target=upstream, daemon=True)
        reader.start()
        try:
            while True:
                kind, payload = shell_host.read_frame(process.stdout)
                if kind == DATA:
                    ws.send_binary(payload)
                elif kind == CLOSE:
                    break
        except (EOFError, OSError, ValueError, websocket_client.WebSocketError):
            pass
        finally:
            ws.close()
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            reader.join(5)
