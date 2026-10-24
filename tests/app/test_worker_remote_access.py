"""Maintenance access to worker PCs: the WebSocket layer, the relay, sessions, access keys and the worker's rules.

The end-to-end tests run the application on a real (threaded) HTTP server with
the tunnel relay, a worker speaking through ``worker.remote_access`` and an
administrator using the connect helper, so bytes cross every layer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ----- helpers --------------------------------------------------------------------------------------------------

def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "bananachat_connect_under_test", ROOT / "bananachat" / "static" / "downloads" / "bananachat_connect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Echo:
    """A TCP service on the 'worker PC' that answers every chunk upper-cased."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(connection,), daemon=True).start()

    @staticmethod
    def _echo(connection):
        with connection:
            while True:
                data = connection.recv(65536)
                if not data:
                    return
                connection.sendall(data.upper())

    def close(self):
        self.listener.close()


def _heartbeat(app, token, remote=None, system=None):
    body = {"status": "online", "gpu_name": "RTX", "ollama_version": "0.9.0", "capabilities": {"models": []},
            "activity_state": "idle", "gpu_util": 0.0}
    if remote is not None:
        body["remote"] = remote
    if system is not None:
        body["system"] = system
    response = app.test_client().post("/worker/v1/heartbeat", data=json.dumps(body),
                                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _register(app, name="Lab PC"):
    from bananachat import db
    from bananachat.services import remote

    with app.app_context():
        with db.transaction():
            return remote.register_worker(name)


def _admin_user(app):
    from bananachat.db import users

    with app.app_context():
        return users.get_by_username("admin")


def _remote_settings(**overrides):
    values = {"remote_access": True, "remote_shell": False, "remote_services": (("ssh", 22),),
              "remote_subnets": (), "remote_subnet_ports": (22, 3389, 5900), "server_url": "", "token": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def remote_app(make_app):
    return make_app(workers_enabled=1, worker_remote_access=1)


@pytest.fixture
def live(remote_app):
    """The app on a threaded HTTP server, with the tunnel relay running and announced."""
    from werkzeug.serving import make_server

    from bananachat import db
    from bananachat.db import settings as site_settings
    from bananachat.services import tunnel_broker

    server = make_server("127.0.0.1", 0, remote_app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    broker = tunnel_broker.Broker(tunnel_broker._key(remote_app.secret_key))
    stop = threading.Event()
    threading.Thread(target=broker.serve, args=(stop.is_set, lambda port: tunnel_broker._announce(remote_app, port)),
                     daemon=True).start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with remote_app.app_context():
            state = site_settings.state_get(tunnel_broker.STATE_KEY) or {}
            db.release_thread_connection()
        if state.get("port") == broker.port:
            break
        time.sleep(0.05)
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", broker=broker, app=remote_app)
    stop.set()
    broker.close()
    server.shutdown()


# ----- the WebSocket layer --------------------------------------------------------------------------------------

def test_websocket_frames_cross_between_the_server_and_the_worker_client():
    from bananachat.services import websocket as server_ws
    from worker import websocket_client as client_ws

    left, right = socket.socketpair()
    server, client = server_ws.WebSocket(left), client_ws.WebSocket(right)
    try:
        client.send_text("héllo")
        assert server.receive() == (server_ws.TEXT, "héllo".encode())
        payload = os.urandom(200_000)  # a byte stream: senders may cut it into several messages
        client.send_binary(payload)
        received = bytearray()
        while len(received) < len(payload):
            kind, part = server.receive()
            assert kind == server_ws.BINARY
            received += part
        assert received == payload
        server.send_binary(payload)  # one message: the 8-byte length form
        assert client.receive() == (client_ws.BINARY, payload)
        server.ping(b"x")  # the client answers pings without surfacing them
        server.send_text("after ping")
        assert client.receive() == (client_ws.TEXT, b"after ping")
        client.close()
        with pytest.raises(server_ws.Closed):
            server.receive()
    finally:
        left.close()
        right.close()


def test_the_server_refuses_unmasked_and_oversized_frames():
    from bananachat.services import websocket as server_ws

    left, right = socket.socketpair()
    try:
        right.sendall(bytes([0x82, 3]) + b"abc")  # unmasked binary frame
        with pytest.raises(server_ws.WebSocketError, match="masked"):
            server_ws.WebSocket(left).receive()
    finally:
        left.close()
        right.close()
    left, right = socket.socketpair()
    try:
        right.sendall(bytes([0x82, 0xFF]) + (server_ws.MAX_FRAME + 1).to_bytes(8, "big"))
        with pytest.raises(server_ws.WebSocketError, match="too large"):
            server_ws.WebSocket(left).receive()
    finally:
        left.close()
        right.close()


def test_fragmented_messages_are_joined():
    from bananachat.services import websocket as server_ws

    def masked(first, payload):
        mask = b"\x01\x02\x03\x04"
        return bytes([first, 0x80 | len(payload)]) + mask + server_ws._xor(payload, mask)

    left, right = socket.socketpair()
    try:
        right.sendall(masked(0x01, b"hel") + masked(0x80, b"lo"))  # TEXT without FIN, then the final continuation
        assert server_ws.WebSocket(left).receive() == (server_ws.TEXT, b"hello")
    finally:
        left.close()
        right.close()


def test_the_handshake_needs_a_valid_key():
    from bananachat.services import websocket as server_ws

    environ = {"REQUEST_METHOD": "GET", "HTTP_UPGRADE": "websocket", "HTTP_CONNECTION": "keep-alive, Upgrade",
               "HTTP_SEC_WEBSOCKET_VERSION": "13", "HTTP_SEC_WEBSOCKET_KEY": "short"}
    assert server_ws.requested(environ)
    with pytest.raises(ValueError):
        server_ws.accept(environ)
    assert server_ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455's example


# ----- the relay --------------------------------------------------------------------------------------------------

def _join(port, key, role, session_id, signature=None, *, replay=None):
    """Open a session at the relay like a server process does (BCT2 challenge); *signature* forges the last step."""
    from bananachat.services import tunnel_broker

    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    mine = "1" * 32
    sock.sendall(f"BCT2 {mine}\n".encode())
    theirs, proof = tunnel_broker._line(sock).decode().split()[1:]
    assert proof == tunnel_broker._mac(key, "broker", mine, theirs)  # the relay proved it knows the key
    signed = signature or tunnel_broker._mac(key, role, session_id, theirs, mine)
    line = replay or f"{role} {session_id} {signed}\n"
    sock.sendall(line.encode())
    _openings[sock.fileno()] = f"{role} {session_id} {signed}\n"
    return sock


_openings: dict = {}


def test_the_relay_pairs_the_two_sides_of_a_session_and_refuses_strangers(monkeypatch):
    from bananachat.services import tunnel_broker

    monkeypatch.setattr(tunnel_broker, "PAIR_SECONDS", 1)
    key = tunnel_broker._key("secret")
    broker = tunnel_broker.Broker(key)
    stop = threading.Event()
    threading.Thread(target=broker.serve, args=(stop.is_set,), daemon=True).start()
    try:
        session = "a" * 32
        worker = _join(broker.port, key, "worker", session)
        client = _join(broker.port, key, "client", session)
        assert tunnel_broker.wait_paired(worker, 5) and tunnel_broker.wait_paired(client, 5)
        worker.sendall(b"ping")
        assert client.recv(4) == b"ping"
        client.sendall(b"pong")
        assert worker.recv(4) == b"pong"
        worker.close()
        assert client.recv(10) == b""  # one side leaving ends the other

        forged = _join(broker.port, key, "client", "b" * 32, signature="0" * 64)
        assert forged.recv(10) == b""  # a wrong signature is dropped at once
        alone = _join(broker.port, key, "worker", "c" * 32)
        # A captured opening is useless on a new connection: the relay's challenge changed.
        replayed = _join(broker.port, key, "client", "c" * 32, replay=_openings[alone.fileno()])
        assert replayed.recv(10) == b""
        assert not tunnel_broker.wait_paired(alone, 5)  # nobody came: TIMEOUT
        for sock in (client, forged, alone, replayed):
            sock.close()
    finally:
        stop.set()
        broker.close()


def test_joining_fails_clearly_when_no_process_runs_the_relay(remote_app):
    from bananachat.services import tunnel_broker

    with remote_app.app_context():
        with pytest.raises(tunnel_broker.BrokerUnavailable):
            tunnel_broker.connect(remote_app, "client", "d" * 32)


def test_a_program_posing_as_the_relay_gets_nothing(remote_app):
    """Another local user listening on the relay's announced port cannot prove the key: nothing is sent to it."""
    from bananachat.services import tunnel_broker

    impostor = socket.socket()
    impostor.bind(("127.0.0.1", 0))
    impostor.listen(1)
    received = []

    def pose():
        connection, _ = impostor.accept()
        with connection:
            received.append(tunnel_broker._line(connection))
            connection.sendall(b"BCT2 " + b"2" * 32 + b" " + b"0" * 64 + b"\n")
            connection.settimeout(2)
            try:
                received.append(connection.recv(200))
            except OSError:
                received.append(b"")

    thread = threading.Thread(target=pose, daemon=True)
    thread.start()
    with remote_app.app_context():
        tunnel_broker._announce(remote_app, impostor.getsockname()[1])
        with pytest.raises(tunnel_broker.BrokerUnavailable):
            tunnel_broker.connect(remote_app, "client", "d" * 32)
    thread.join(5)
    impostor.close()
    assert received[0].startswith(b"BCT2 ") and received[1] == b""  # no session ID, no signature


def test_tunnel_slots_are_limited_per_process():
    from bananachat.services import tunnel_broker

    taken = [tunnel_broker.take_slot(2) for _ in range(3)]
    try:
        assert taken == [True, True, False]
    finally:
        for ok in taken:
            if ok:
                tunnel_broker.release_slot()


# ----- what the worker advertises and the server checks ----------------------------------------------------------

def test_the_heartbeat_records_what_the_owner_allows_and_arms_the_worker(remote_app):
    from bananachat.services import worker_remote

    worker_id, token = _register(remote_app)
    reply = _heartbeat(remote_app, token, {"enabled": True, "shell": True, "platform": "Linux",
                                           "services": [{"kind": "ssh", "port": 22, "listening": True}],
                                           "subnets": ["192.168.1.0/24"], "subnet_ports": [22, 5900]},
                       {"ram_total": 32 * 1024 ** 3, "ram_free": 8 * 1024 ** 3, "features": ["model_commands"],
                        "version": "1.7.3"})
    assert reply["remote"] == {"armed": False, "poll_seconds": worker_remote.LONG_POLL_SECONDS}
    with remote_app.app_context():
        worker_remote.arm(worker_id)
        assert worker_remote.heartbeat_reply(worker_id)["armed"] is True
        item = next(item for item in worker_remote.overview() if item["id"] == worker_id)
        assert item["online"] and item["remote"]["shell"] and item["address"] == "10.77.0.2"
        assert item["system"]["ram_total"] == 32 * 1024 ** 3


@pytest.mark.parametrize("remote", [
    {"enabled": True, "subnets": ["8.8.8.0/24"]},  # public networks are never shared
    {"enabled": True, "subnets": ["10.0.0.0/8"]},  # nor huge ones
    {"enabled": True, "services": [{"kind": "ftp", "port": 21}]},
    {"enabled": True, "services": [{"kind": "ssh", "port": 70000}]},
    "yes",
])
def test_invalid_remote_settings_are_rejected(remote):
    from bananachat.services import worker_remote

    with pytest.raises(ValueError):
        worker_remote.parse_remote(remote)


def test_a_worker_that_did_not_opt_in_cannot_be_reached(remote_app):
    from bananachat.services import worker_remote

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": False, "shell": True, "services": [{"kind": "ssh", "port": 22}]})
    with remote_app.app_context():
        assert worker_remote.heartbeat_reply(worker_id) is None  # it is never even asked to listen
        with pytest.raises(worker_remote.RemoteError, match="owner has not allowed"):
            worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22)


def test_sessions_only_reach_what_the_owner_listed(remote_app):
    from bananachat.services import worker_remote

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": True, "shell": False,
                                   "services": [{"kind": "ssh", "port": 22}],
                                   "subnets": ["192.168.1.0/24"], "subnet_ports": [3389]})
    with remote_app.app_context():
        assert worker_remote.create_session(worker_id, "ssh", "localhost", 22)["host"] == "127.0.0.1"
        assert worker_remote.create_session(worker_id, "rdp", "192.168.1.20", 3389)["port"] == 3389
        for kind, host, port, message in [("shell", "", 0, "built-in terminal"),
                                          ("tcp", "127.0.0.1", 8080, "does not offer port 8080"),
                                          ("ssh", "192.168.2.1", 22, "not in a network"),
                                          ("ssh", "192.168.1.20", 22, "did not allow port 22"),
                                          ("ssh", "example.com", 22, "IP address")]:
            with pytest.raises(worker_remote.RemoteError, match=message):
                worker_remote.create_session(worker_id, kind, host, port)


def test_an_offline_worker_cannot_be_reached_and_remote_access_can_be_off(remote_app, make_app):
    from bananachat.services import worker_remote

    worker_id, _token = _register(remote_app)
    with remote_app.app_context():
        with pytest.raises(worker_remote.RemoteError, match="offline"):
            worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22)
    off = make_app(workers_enabled=1)
    with off.app_context():
        assert not worker_remote.enabled()
        with pytest.raises(worker_remote.RemoteError, match="off on this server"):
            worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22)


def test_sessions_the_worker_never_picks_up_expire(remote_app):
    from bananachat.db import worker_remote as store
    from bananachat.services import worker_remote

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": True, "services": [{"kind": "ssh", "port": 22}]})
    with remote_app.app_context():
        session = worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22)
        from bananachat import db

        db.execute("UPDATE worker_tunnels SET created_at=created_at-? WHERE id=?",
                   (worker_remote.CLAIM_SECONDS + 5, session["id"]))
        assert worker_remote.maintain()["expired"] == 1
        row = store.tunnel(session["id"])
        assert row["status"] == "failed" and "did not pick" in row["error"]


def test_the_worker_long_poll_claims_each_session_once(remote_app):
    from bananachat.services import worker_remote

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": True, "services": [{"kind": "vnc", "port": 5900}]})
    with remote_app.app_context():
        session = worker_remote.create_session(worker_id, "vnc", "127.0.0.1", 5900)
    client = remote_app.test_client()
    headers = {"Authorization": f"Bearer {token}"}
    first = client.get("/worker/v1/remote/poll", headers=headers)
    assert first.status_code == 200
    assert first.get_json()["sessions"] == [{"id": session["id"], "kind": "vnc", "host": "127.0.0.1", "port": 5900}]
    with remote_app.app_context():
        assert worker_remote.poll({"id": worker_id}, wait=0) == []  # already claimed


def test_virtual_addresses_are_stable_and_follow_the_configured_network(remote_app, make_app):
    from bananachat.services import worker_remote

    first, _ = _register(remote_app, "One")
    second, _ = _register(remote_app, "Two")
    with remote_app.app_context():
        addresses = worker_remote.virtual_ips()
        assert set(addresses) == {first, second} and sorted(addresses.values()) == ["10.77.0.2", "10.77.0.3"]
        assert worker_remote.virtual_ips() == addresses
        assert worker_remote.resolve(addresses[second], 22)[0] == second
        assert worker_remote.resolve("two.bc", 5901)[:2] == (second, "vnc")
        with pytest.raises(worker_remote.RemoteError):
            worker_remote.resolve("nobody.bc", 22)
    moved = make_app(setup=False, workers_enabled=1, worker_remote_access=1, worker_subnet="172.20.5.0/24",
                     instance_dir=remote_app.config["BC"].instance_dir)
    with moved.app_context():
        assert sorted(worker_remote.virtual_ips().values()) == ["172.20.5.2", "172.20.5.3"]


# ----- access keys ------------------------------------------------------------------------------------------------

def test_access_keys_authenticate_the_connect_api_until_revoked(remote_app):
    from bananachat.services import worker_remote

    admin = _admin_user(remote_app)
    with remote_app.app_context():
        key_id, token = worker_remote.create_key(admin, "laptop", 1)
    client = remote_app.test_client()
    assert client.get("/remote/v1/workers").status_code == 401
    assert client.get("/remote/v1/workers", headers={"Authorization": "Bearer bca_wrong"}).status_code == 401
    listed = client.get("/remote/v1/workers", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200 and listed.get_json()["subnet"] == "10.77.0.0/16"
    assert listed.headers["Cache-Control"] == "no-store"
    with remote_app.app_context():
        assert worker_remote.revoke_key(key_id)
    assert client.get("/remote/v1/workers", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_access_keys_expire_and_die_with_their_administrator_role(remote_app, make_user):
    from bananachat import db
    from bananachat.services import worker_remote

    admin = _admin_user(remote_app)
    with remote_app.app_context():
        _key_id, token = worker_remote.create_key(admin, "", 1)
        assert worker_remote.authenticate_key(f"Bearer {token}") is not None
        db.execute("UPDATE users SET role='user' WHERE id=?", (admin["id"],))
        assert worker_remote.authenticate_key(f"Bearer {token}") is None
        db.execute("UPDATE users SET role='admin' WHERE id=?", (admin["id"],))
        db.execute("UPDATE worker_access_keys SET expires_at=?", (time.time() - 1,))
        assert worker_remote.authenticate_key(f"Bearer {token}") is None
        with pytest.raises(worker_remote.RemoteError):
            worker_remote.create_key(admin, "", 0)


def test_the_connect_api_is_hidden_when_remote_access_is_off(make_app):
    off = make_app(workers_enabled=1)
    assert off.test_client().get("/remote/v1/workers").status_code == 404


# ----- the administrator's pages ----------------------------------------------------------------------------------

def test_browser_sessions_need_a_confirmation_and_the_websocket_our_origin(remote_app):
    from tests.app.fixtures import Browser

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": True, "shell": True, "services": [{"kind": "vnc", "port": 5900}]})
    admin = Browser(remote_app)
    admin.login("admin", "admin-password")
    from tests.app.test_two_step import _stale

    _stale(remote_app)
    url = f"/admin/workers/{worker_id}/remote/sessions"
    refused = admin.post_json(url, {"kind": "shell"})
    assert refused.status_code == 403 and refused.get_json()["error"]["code"] == "reauth_required"
    created = admin.post_json(url, {"kind": "shell", "reauth_secret": "admin-password"})
    assert created.status_code == 201, created.get_data(as_text=True)
    session = created.get_json()["session"]
    assert session["kind"] == "shell" and session["ws"].endswith(f"/tunnels/{session['id']}/ws")
    upgrade = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
               "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
    cross = admin.get(session["ws"], headers={**upgrade, "Origin": "https://evil.example"})
    assert cross.status_code == 403 and cross.get_json()["error"]["code"] == "cross_origin"
    missing = admin.get(session["ws"], headers=upgrade)
    assert missing.status_code == 403

    for page in (f"/admin/workers/{worker_id}", f"/admin/workers/{worker_id}/terminal",
                 f"/admin/workers/{worker_id}/vnc?port=5900"):
        response = admin.get(page)
        assert response.status_code == 200, page
    viewer = admin.get(f"/admin/workers/{worker_id}/terminal")
    policy = viewer.headers["Content-Security-Policy"]
    assert "connect-src 'self' wss://localhost ws://localhost" in policy and "unsafe-inline" not in policy

    closed = admin.fetch(f"/admin/workers/tunnels/{session['id']}/close", method="POST")
    assert closed.status_code == 200


# ----- end to end -------------------------------------------------------------------------------------------------

def test_a_native_client_reaches_a_service_of_the_worker_through_the_tunnel(live):
    from bananachat.db import worker_remote as store
    from bananachat.services import worker_remote
    from worker.client import Client
    from worker.remote_access import RemoteAccess

    echo = Echo()
    helper = _load_helper()
    app = live.app
    worker_id, token = _register(app)
    settings = _remote_settings(remote_services=(("ssh", echo.port),), server_url=live.url, token=token)
    access = RemoteAccess(settings, Client(settings))
    _heartbeat(app, token, access.advertise())
    with app.app_context():
        _key_id, key = worker_remote.create_key(_admin_user(app), "test", 1)

    result = {}

    def administrator():
        try:
            ws = helper.open_session(live.url, key, "lab-pc.bc", echo.port, quiet=True)
            ws.send_binary(b"hello worker")
            kind, payload = ws.receive()
            result["reply"] = (kind, payload)
            ws.close()
        except BaseException as error:  # noqa: BLE001 - reported below
            result["error"] = error

    thread = threading.Thread(target=administrator, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    sessions = []
    while not sessions and time.monotonic() < deadline:
        sessions = access.client.remote_poll()
    assert sessions and sessions[0]["port"] == echo.port
    assert access.start(sessions[0])
    thread.join(15)
    echo.close()
    assert "error" not in result, result.get("error")
    assert result["reply"] == (helper.BINARY, b"HELLO WORKER")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with app.app_context():
            row = store.tunnel(sessions[0]["id"])
        if row["status"] == "closed":
            break
        time.sleep(0.1)
    assert row["status"] == "closed" and row["opened_at"] is not None
    assert row["bytes_up"] == len(b"hello worker") and row["bytes_down"] == len(b"HELLO WORKER")


def test_the_worker_refuses_a_session_its_owner_did_not_allow_even_if_the_server_asks(live):
    from worker.client import Client
    from worker.remote_access import RemoteAccess

    helper = _load_helper()
    app = live.app
    _worker_id, token = _register(app)
    settings = _remote_settings(remote_services=(("ssh", 2222),), server_url=live.url, token=token)
    access = RemoteAccess(settings, Client(settings))
    _heartbeat(app, token, access.advertise())
    from bananachat.services import worker_remote

    with app.app_context():
        _key_id, key = worker_remote.create_key(_admin_user(app), "test", 1)
    # The owner changes their mind after the server last heard from the worker.
    settings.remote_services = (("ssh", 22),)
    result = {}

    def administrator():
        try:
            ws = helper.open_session(live.url, key, "10.77.0.2", 2222, quiet=True)
            kind, payload = ws.receive()  # the worker joined, then said why it refuses
            assert kind == helper.TEXT
            result["problem"] = json.loads(payload)["message"]
            ws.receive()
        except helper.Problem as problem:
            result["problem"] = str(problem)
        except EOFError:
            result["closed"] = True

    thread = threading.Thread(target=administrator, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    sessions = []
    while not sessions and time.monotonic() < deadline:
        sessions = access.client.remote_poll()
    access.start(sessions[0])
    thread.join(15)
    assert "BC_WORKER_REMOTE_SERVICES" in result.get("problem", "") and result.get("closed")


def test_the_socks_proxy_of_the_connect_helper_reaches_a_worker_by_its_virtual_address(live, monkeypatch):
    import struct

    from bananachat.services import worker_remote
    from worker.client import Client
    from worker.remote_access import RemoteAccess

    echo = Echo()
    helper = _load_helper()
    app = live.app
    _worker_id, token = _register(app)
    settings = _remote_settings(remote_services=(("vnc", echo.port),), server_url=live.url, token=token)
    access = RemoteAccess(settings, Client(settings))
    _heartbeat(app, token, access.advertise())
    with app.app_context():
        _key_id, key = worker_remote.create_key(_admin_user(app), "test", 1)
    monkeypatch.setenv("BANANACHAT_CONNECT_SERVER", live.url)
    monkeypatch.setenv("BANANACHAT_CONNECT_KEY", key)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    threading.Thread(target=helper.cmd_socks, args=(SimpleNamespace(listen=f"127.0.0.1:{port}"),),
                     daemon=True).start()
    stop = threading.Event()

    def worker_loop():
        from worker.client import ServerError

        while not stop.is_set():
            try:
                for session in access.client.remote_poll():
                    access.start(session)
            except ServerError:
                return  # the test ended and its server went away

    threading.Thread(target=worker_loop, daemon=True).start()
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=15)
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        client.sendall(b"\x05\x01\x00")
        assert client.recv(2) == b"\x05\x00"
        client.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("10.77.0.2") + struct.pack("!H", echo.port))
        assert client.recv(10)[:2] == b"\x05\x00"
        client.sendall(b"through socks")
        assert client.recv(100) == b"THROUGH SOCKS"
        client.close()
    finally:
        stop.set()
        echo.close()


def test_a_session_ends_when_its_administrator_loses_the_role(remote_app, make_user):
    from bananachat import db
    from bananachat.db import worker_remote as store
    from bananachat.services import worker_remote
    from bananachat.web import _tunnels

    worker_id, token = _register(remote_app)
    _heartbeat(remote_app, token, {"enabled": True, "services": [{"kind": "ssh", "port": 22}]})
    other = make_user("dora", role="admin")
    with remote_app.app_context():
        browser = worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22, created_by=other["id"])
        key_id, _token = worker_remote.create_key(_admin_user(remote_app), "", 1)
        helper = worker_remote.create_session(worker_id, "ssh", "127.0.0.1", 22, access_key_id=key_id)
    with remote_app.test_request_context():
        browser_check = _tunnels._closed_by_administrator(browser["id"])
        helper_check = _tunnels._closed_by_administrator(helper["id"])
        assert not browser_check() and not helper_check()
    with remote_app.app_context():
        db.execute("UPDATE users SET role='user' WHERE id=?", (other["id"],))
        worker_remote.revoke_key(key_id)
    with remote_app.test_request_context():
        assert _tunnels._closed_by_administrator(browser["id"])()
        assert _tunnels._closed_by_administrator(helper["id"])()
    with remote_app.app_context():
        row = store.tunnel(browser["id"])
        assert row["status"] == "failed" and "withdrawn" in row["error"]


def test_heartbeats_with_malformed_remote_fields_are_refused_not_crashing(remote_app):
    _worker_id, token = _register(remote_app)
    body = {"status": "online", "capabilities": {"models": []}, "remote": {"enabled": True, "services": {"a": 1}},
            "system": {"features": 5}}
    response = remote_app.test_client().post("/worker/v1/heartbeat", data=json.dumps(body), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert response.status_code == 400


# ----- the worker's own rules -------------------------------------------------------------------------------------

def test_the_worker_checks_every_session_against_its_owner_settings():
    from worker.remote_access import RemoteAccess

    access = RemoteAccess(_remote_settings(remote_subnets=("192.168.1.0/24",), remote_subnet_ports=(22,)), None)
    assert access.refusal({"kind": "ssh", "host": "127.0.0.1", "port": 22}) is None
    assert access.refusal({"kind": "ssh", "host": "192.168.1.9", "port": 22}) is None
    assert "BC_WORKER_REMOTE_SHELL" in access.refusal({"kind": "shell"})
    assert "BC_WORKER_REMOTE_SERVICES" in access.refusal({"kind": "tcp", "host": "127.0.0.1", "port": 8080})
    assert "BC_WORKER_REMOTE_SUBNETS" in access.refusal({"kind": "ssh", "host": "10.0.0.1", "port": 22})
    assert "SUBNET_PORTS" in access.refusal({"kind": "rdp", "host": "192.168.1.9", "port": 3389})
    assert "IP addresses" in access.refusal({"kind": "ssh", "host": "nas.local", "port": 22})
    assert access.refusal({"kind": "ssh", "host": "127.0.0.1", "port": True}) == "Invalid port."
    off = RemoteAccess(_remote_settings(remote_access=False), None)
    assert "BC_WORKER_REMOTE_ACCESS" in off.refusal({"kind": "ssh", "host": "127.0.0.1", "port": 22})
    assert off.advertise() == {"enabled": False}


def test_the_worker_listens_only_while_armed_and_backs_off():
    from worker.client import ServerError
    from worker.remote_access import RemoteAccess

    calls = []

    class Client:
        def remote_poll(self):
            calls.append(time.monotonic())
            raise ServerError("off", 404)

    access = RemoteAccess(_remote_settings(), Client())
    stop = threading.Event()
    thread = threading.Thread(target=access.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.3)
    assert calls == []  # not armed: no polling at all
    access.heartbeat_reply({"remote": {"armed": True, "poll_seconds": 20}})
    time.sleep(1.5)
    stop.set()
    thread.join(5)
    assert len(calls) == 1 and access.armed is False  # a 404 disarms it for a minute


@pytest.mark.skipif(os.name != "posix", reason="the POSIX terminal host")
def test_the_terminal_host_runs_a_shell_on_a_pty(tmp_path):
    import subprocess
    import sys

    from worker import shell_host

    environment = {**os.environ, "SHELL": "/bin/sh", "PYTHONPATH": str(ROOT)}
    process = subprocess.Popen([sys.executable, str(ROOT / "worker" / "bananachat_worker.py"), "shell-host"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment, cwd=tmp_path)
    try:
        writer = shell_host.Writer(process.stdin)
        writer.frame(2, json.dumps({"type": "resize", "cols": 100, "rows": 30}).encode())
        writer.frame(1, b"echo $((6*7)); stty size; exit\n")
        output = bytearray()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            kind, payload = shell_host.read_frame(process.stdout)
            if kind == 3:
                break
            output += payload
        text = output.decode("utf-8", "replace")
        assert "42" in text and "30 100" in text
    finally:
        process.kill()
        process.wait(5)
