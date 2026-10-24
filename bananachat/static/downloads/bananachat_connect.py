#!/usr/bin/env python3
"""BananaChat connect helper: reach worker PCs with your own SSH, VNC and RDP clients.

Worker PCs connect out to the BananaChat server and open no port; this helper
asks the server for a session and carries your connection through it. It needs
only Python 3.9+ (Linux, macOS or Windows) and an access key from
Administration -> Workers -> Maintenance access.

  python bananachat_connect.py login https://chat.example.org     save the server and an access key
  python bananachat_connect.py list                                workers, their addresses and services
  python bananachat_connect.py proxy HOST PORT                     for ssh's ProxyCommand (stdin/stdout)
  python bananachat_connect.py forward HOST:PORT [--local [ADDR:]PORT]
                                                                   a local port that reaches HOST:PORT
  python bananachat_connect.py socks [--listen ADDR:PORT]          a SOCKS5 proxy for every worker
  python bananachat_connect.py shell WORKER                        the worker's built-in terminal, here

HOST is a worker's address in the server's virtual network (see ``list``),
``<name>.bc``, or an address of a network a worker shares. Examples:

  ssh -o ProxyCommand="python bananachat_connect.py proxy %h %p" user@10.77.0.2
  python bananachat_connect.py forward 10.77.0.2:5900 --local 15900   # then a VNC viewer on localhost:15900
  python bananachat_connect.py forward 10.77.0.3:3389 --local 13389   # then RDP to localhost:13389

The access key is saved in ~/.config/bananachat-connect.json (only you can read
it); BANANACHAT_CONNECT_SERVER and BANANACHAT_CONNECT_KEY override it. Proxy variables are
ignored and redirects are not followed, so the key reaches only that server.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.client
import json
import os
import platform
import socket
import ssl
import struct
import sys
import threading
from typing import Optional, Tuple
from urllib.parse import urlsplit

VERSION = "1.7.3"
CONFIG = os.path.join(os.path.expanduser("~"), ".config", "bananachat-connect.json")
TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 * 1024 * 1024


class Problem(Exception):
    pass


# ----- settings -----------------------------------------------------------------------------------------------

def load_settings() -> Tuple[str, str]:
    server, key = os.environ.get("BANANACHAT_CONNECT_SERVER", ""), os.environ.get("BANANACHAT_CONNECT_KEY", "")
    if not (server and key):
        try:
            with open(CONFIG, encoding="utf-8") as source:
                saved = json.load(source)
            server, key = server or saved.get("server", ""), key or saved.get("key", "")
        except (OSError, ValueError):
            pass
    if not server or not key:
        raise Problem("Run 'python bananachat_connect.py login https://your-server' first.")
    return server.rstrip("/"), key


def save_settings(server: str, key: str) -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    descriptor = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)  # an existing file keeps its mode otherwise
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump({"server": server, "key": key}, target)


# ----- HTTP and WebSocket ------------------------------------------------------------------------------------

def _check_server(server: str) -> None:
    parts = urlsplit(server)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise Problem("Use the server's https:// address.")


def request(server: str, key: str, method: str, path: str, body=None):
    _check_server(server)
    parts = urlsplit(server)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(parts.hostname, port, timeout=30,
                                                 context=ssl.create_default_context())
    else:
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=30)
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": f"BananaChat-connect/{VERSION}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, parts.path.rstrip("/") + path, body=data, headers=headers)
        response = connection.getresponse()
        text = response.read(4 * 1024 * 1024)
    except (OSError, http.client.HTTPException) as error:
        raise Problem(f"The server could not be reached: {error}") from None
    finally:
        connection.close()
    if 300 <= response.status < 400:
        raise Problem("The server answered with a redirect; use its final https:// address.")
    try:
        value = json.loads(text or b"null")
    except ValueError:
        value = None
    if response.status >= 400:
        message = ((value or {}).get("error") or {}).get("message") if isinstance(value, dict) else None
        raise Problem(message or f"HTTP {response.status}")
    return value


def _xor(payload: bytes, mask: bytes) -> bytes:
    size = len(payload)
    if not size:
        return b""
    repeated = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(repeated, "little")).to_bytes(size, "little")


class WebSocket:
    def __init__(self, sock, buffered: bytes = b""):
        self.sock = sock
        self.buffer = bytearray(buffered)
        self.lock = threading.Lock()
        self.closed = False

    def _exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                raise EOFError()
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def receive(self):
        message, kind = bytearray(), None
        while True:
            first, second = self._exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            if length > MAX_MESSAGE:
                raise EOFError()
            payload = self._exact(length)
            opcode = first & 0x0F
            if opcode == PING:
                self.send(PONG, payload)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self.close()
                raise EOFError()
            if opcode in (TEXT, BINARY):
                kind = opcode
            message.extend(payload)
            if first & 0x80:
                return kind, bytes(message)

    def send(self, opcode: int, payload: bytes) -> None:
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
        with self.lock:
            if self.closed and opcode != CLOSE:
                raise EOFError()
            try:
                self.sock.sendall(bytes(header) + mask + _xor(payload, mask))
            except OSError:
                raise EOFError() from None

    def send_binary(self, payload: bytes) -> None:
        for start in range(0, max(1, len(payload)), 65536):
            self.send(BINARY, payload[start:start + 65536])

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.send(CLOSE, struct.pack("!H", 1000))
        except EOFError:
            pass
        self.closed = True
        for action in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                action()
            except OSError:
                pass


def open_socket(server: str, key: str, path: str) -> WebSocket:
    _check_server(server)
    parts = urlsplit(server)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    raw = socket.create_connection((parts.hostname, port), timeout=30)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname) \
        if parts.scheme == "https" else raw
    nonce = base64.b64encode(os.urandom(16)).decode("ascii")
    host = parts.netloc.rsplit("@", 1)[-1]
    lines = [f"GET {parts.path.rstrip('/')}{path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {nonce}", "Sec-WebSocket-Version: 13",
             f"Authorization: Bearer {key}", f"User-Agent: BananaChat-connect/{VERSION}"]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    answer = bytearray()
    while b"\r\n\r\n" not in answer:
        chunk = sock.recv(4096)
        if not chunk or len(answer) > 65536:
            raise Problem("The server closed the connection.")
        answer.extend(chunk)
    head, _, rest = bytes(answer).partition(b"\r\n\r\n")
    status_line, *headers = head.decode("latin-1").split("\r\n")
    status = status_line.split(" ", 2)[1] if " " in status_line else ""
    if status != "101":
        try:
            message = json.loads(rest.decode("utf-8", "replace"))["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {status}"
        raise Problem(message)
    accept = base64.b64encode(hashlib.sha1(nonce.encode("ascii") + GUID).digest()).decode("ascii")  # noqa: S324
    if not any(line.lower().startswith("sec-websocket-accept:") and line.split(":", 1)[1].strip() == accept
               for line in headers):
        raise Problem("The server did not accept the WebSocket.")
    sock.settimeout(None)
    return WebSocket(sock, rest)


def open_session(server: str, key: str, host: str, port: int, *, quiet: bool = False) -> WebSocket:
    """Create a session and return its WebSocket once the worker joined it."""
    session = request(server, key, "POST", "/remote/v1/sessions", {"host": host, "port": port})["session"]
    ws = open_socket(server, key, f"/remote/v1/sessions/{session['id']}/ws")
    while True:
        try:
            kind, payload = ws.receive()
        except EOFError:
            raise Problem("The session closed before the worker joined it.") from None
        if kind != TEXT:
            raise Problem("Unexpected data before the session opened.")
        message = json.loads(payload.decode("utf-8", "replace"))
        if message.get("type") == "status" and message.get("state") == "connected":
            if not quiet:
                print(f"Connected to {session['worker']} ({session['host']}:{session['port']}).", file=sys.stderr)
            return ws
        if message.get("type") == "status" and message.get("state") == "failed":
            raise Problem(message.get("message") or "The worker did not connect.")
        if message.get("type") == "error":
            raise Problem(message.get("message") or "The worker refused the session.")


def relay(ws: WebSocket, read, write, close_input=lambda: None) -> None:
    """Copy *read()* chunks to the session and its binary messages to *write(bytes)* until either ends."""
    def downstream():
        try:
            while True:
                kind, payload = ws.receive()
                if kind == BINARY:
                    write(payload)
                elif kind == TEXT:
                    try:
                        message = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if message.get("type") == "error":
                        print(message.get("message"), file=sys.stderr)
        except (EOFError, OSError):
            pass
        finally:
            close_input()

    thread = threading.Thread(target=downstream, daemon=True)
    thread.start()
    try:
        while True:
            data = read()
            if not data:
                break
            ws.send_binary(data)
    except (EOFError, OSError):
        pass
    finally:
        ws.close()
        thread.join(5)


# ----- commands -----------------------------------------------------------------------------------------------

def cmd_login(args) -> int:
    server = args.server.rstrip("/")
    _check_server(server)
    key = os.environ.get("BANANACHAT_CONNECT_KEY") or getpass.getpass("Access key (Administration -> Workers): ").strip()
    data = request(server, key, "GET", "/remote/v1/workers")
    save_settings(server, key)
    print(f"Saved. {len(data.get('workers', []))} worker(s) on {server}; network {data.get('subnet')}.")
    return 0


def cmd_list(_args) -> int:
    server, key = load_settings()
    data = request(server, key, "GET", "/remote/v1/workers")
    for item in data.get("workers", []):
        remote = item.get("remote") or {}
        if not remote.get("enabled"):
            reach = "remote access off"
        else:
            parts = [f"{service['kind']} {service['port']}" for service in remote.get("services") or []]
            if remote.get("shell"):
                parts.append("terminal")
            if remote.get("subnets"):
                parts.append("network " + ", ".join(remote["subnets"]))
            reach = ", ".join(parts) or "nothing allowed"
        state = "online" if item.get("online") else "offline"
        print(f"{item.get('address') or '-':<15} {item['slug'] + '.bc':<28} {state:<8} {reach}")
    return 0


def cmd_proxy(args) -> int:
    server, key = load_settings()
    ws = open_session(server, key, args.host, args.port, quiet=True)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    def read():
        return stdin.read1(65536) if hasattr(stdin, "read1") else os.read(stdin.fileno(), 65536)

    def write(data):
        stdout.write(data)
        stdout.flush()

    relay(ws, read, write)
    return 0


def _address(text: str, default_host: str) -> Tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not port.isdigit():
        raise Problem(f"Expected HOST:PORT, got {text!r}.")
    return (host.strip("[]") or default_host), int(port)


def _serve(listen: Tuple[str, int], handle, label: str) -> int:
    family = socket.AF_INET6 if ":" in listen[0] else socket.AF_INET
    server_socket = socket.socket(family, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(listen)
    server_socket.listen(16)
    print(f"{label} on {listen[0]}:{server_socket.getsockname()[1]} (Ctrl+C stops it)", file=sys.stderr)
    try:
        while True:
            connection, _peer = server_socket.accept()
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    except KeyboardInterrupt:
        return 0
    finally:
        server_socket.close()


def _pipe_socket(ws: WebSocket, connection: socket.socket) -> None:
    def close_input():
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def read():
        try:
            return connection.recv(65536)
        except OSError:
            return b""

    relay(ws, read, connection.sendall, close_input)
    connection.close()


def cmd_forward(args) -> int:
    server, key = load_settings()
    host, port = _address(args.target, "")
    if not host:
        raise Problem("Give the worker's address, e.g. 10.77.0.2:5900.")
    local = args.local or str(port + 10000 if port + 10000 <= 65535 else 0)
    listen = _address(local if ":" in local else f"127.0.0.1:{local}", "127.0.0.1")

    def handle(connection):
        try:
            ws = open_session(server, key, host, port)
        except Problem as error:
            print(f"Session refused: {error}", file=sys.stderr)
            connection.close()
            return
        _pipe_socket(ws, connection)

    return _serve(listen, handle, f"Forwarding {host}:{port}")


def cmd_socks(args) -> int:
    server, key = load_settings()
    listen = _address(args.listen, "127.0.0.1")

    def handle(connection):
        try:
            greeting = connection.recv(262)
            if len(greeting) < 2 or greeting[0] != 5:
                raise Problem("not SOCKS5")
            connection.sendall(b"\x05\x00")  # no authentication: listen on localhost only
            header = connection.recv(4)
            if len(header) < 4 or header[1] != 1:
                connection.sendall(b"\x05\x07\x00\x01" + bytes(6))
                raise Problem("only CONNECT is supported")
            kind = header[3]
            if kind == 1:
                host = socket.inet_ntoa(connection.recv(4))
            elif kind == 3:
                host = connection.recv(connection.recv(1)[0]).decode("ascii", "replace")
            elif kind == 4:
                host = socket.inet_ntop(socket.AF_INET6, connection.recv(16))
            else:
                raise Problem("unknown address type")
            port = struct.unpack("!H", connection.recv(2))[0]
            try:
                ws = open_session(server, key, host, port, quiet=True)
            except Problem as error:
                print(f"{host}:{port}: {error}", file=sys.stderr)
                connection.sendall(b"\x05\x05\x00\x01" + bytes(6))
                connection.close()
                return
            connection.sendall(b"\x05\x00\x00\x01" + bytes(6))
            print(f"{host}:{port} connected", file=sys.stderr)
            _pipe_socket(ws, connection)
        except (Problem, OSError, IndexError):
            connection.close()

    if listen[0] not in ("127.0.0.1", "::1", "localhost"):
        print("Warning: the SOCKS proxy has no password; anyone who reaches it uses your access key.", file=sys.stderr)
    return _serve(listen, handle, "SOCKS5 proxy")


def cmd_shell(args) -> int:
    server, key = load_settings()
    data = request(server, key, "GET", "/remote/v1/workers")
    name = args.worker.lower().removesuffix(".bc")
    worker = next((item for item in data.get("workers", []) if name in (item["slug"], item.get("address"),
                                                                         item["id"], item["name"].lower())), None)
    if worker is None:
        raise Problem(f"No worker called {args.worker!r}.")
    session = request(server, key, "POST", "/remote/v1/sessions",
                      {"worker": worker["id"], "kind": "shell", "host": "shell", "port": 1})["session"]
    ws = open_socket(server, key, f"/remote/v1/sessions/{session['id']}/ws")
    while True:
        kind, payload = ws.receive()
        message = json.loads(payload.decode("utf-8", "replace")) if kind == TEXT else {}
        if message.get("state") == "connected":
            break
        if message.get("state") == "failed" or message.get("type") == "error":
            raise Problem(message.get("message") or "The terminal could not open.")
    columns, rows = os.get_terminal_size() if sys.stdout.isatty() else (80, 24)
    ws.send(TEXT, json.dumps({"type": "resize", "cols": columns, "rows": rows}).encode())
    stdout = sys.stdout.buffer
    if platform.system() != "Windows" and sys.stdin.isatty():
        import termios
        import tty

        saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        try:
            relay(ws, lambda: os.read(sys.stdin.fileno(), 4096), lambda data: (stdout.write(data), stdout.flush()))
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
    else:
        relay(ws, lambda: (sys.stdin.readline().encode() or b"").replace(b"\n", b"\r"),
              lambda data: (stdout.write(data), stdout.flush()))
    print("\r\nThe terminal closed.", file=sys.stderr)
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="save the server's address and an access key")
    login.add_argument("server")
    commands.add_parser("list", help="list workers, their addresses and what they allow")
    proxy = commands.add_parser("proxy", help="connect stdin/stdout to HOST:PORT (ssh ProxyCommand)")
    proxy.add_argument("host")
    proxy.add_argument("port", type=int)
    forward = commands.add_parser("forward", help="listen locally and forward to HOST:PORT")
    forward.add_argument("target", metavar="HOST:PORT")
    forward.add_argument("--local", help="[ADDRESS:]PORT to listen on (default: the port + 10000 on 127.0.0.1)")
    socks = commands.add_parser("socks", help="a SOCKS5 proxy for every worker and shared network")
    socks.add_argument("--listen", default="127.0.0.1:1080")
    shell = commands.add_parser("shell", help="open a worker's built-in terminal here")
    shell.add_argument("worker")
    args = parser.parse_args(argv)
    handlers = {"login": cmd_login, "list": cmd_list, "proxy": cmd_proxy, "forward": cmd_forward,
                "socks": cmd_socks, "shell": cmd_shell}
    try:
        return handlers[args.command](args)
    except Problem as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
