"""The browser terminal and VNC viewer of a worker PC, in Chromium, under the site's strict content policy.

A real worker end (``worker.remote_access`` with the built-in terminal) serves
the session, so this covers the page, xterm.js, the WebSocket address, the
relay and the terminal host together.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.browser_helpers import chromium_executable
from tests.app.fixtures import Browser

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def remote_app(make_app):
    return make_app(workers_enabled=1, worker_remote_access=1)


@pytest.fixture
def live(remote_app):
    from werkzeug.serving import make_server

    from bananachat.services import tunnel_broker

    server = make_server("127.0.0.1", 0, remote_app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    broker = tunnel_broker.Broker(tunnel_broker._key(remote_app.secret_key))
    stop = threading.Event()
    threading.Thread(target=broker.serve, args=(stop.is_set, lambda port: tunnel_broker._announce(remote_app, port)),
                     daemon=True).start()
    time.sleep(0.3)
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", app=remote_app, stop=stop)
    stop.set()
    broker.close()
    server.shutdown()


def _worker(live, *, shell: bool, services=()):
    """Register a worker that allows remote access and serve its sessions until the test ends."""
    from bananachat import db
    from bananachat.services import remote
    from worker.client import Client
    from worker.remote_access import RemoteAccess

    with live.app.app_context():
        with db.transaction():
            worker_id, token = remote.register_worker("Lab PC")
    settings = SimpleNamespace(remote_access=True, remote_shell=shell, remote_services=tuple(services),
                               remote_subnets=(), remote_subnet_ports=(), server_url=live.url, token=token)
    access = RemoteAccess(settings, Client(settings))
    body = {"status": "online", "gpu_name": "RTX", "ollama_version": "0.9.0", "capabilities": {"models": []},
            "activity_state": "idle", "gpu_util": 0.0, "remote": access.advertise()}
    response = live.app.test_client().post("/worker/v1/heartbeat", data=json.dumps(body),
                                           headers={"Authorization": f"Bearer {token}",
                                                    "Content-Type": "application/json"})
    assert response.status_code == 200

    def serve():
        while not live.stop.is_set():
            try:
                for session in access.client.remote_poll():
                    access.start(session)
            except Exception:  # noqa: BLE001 - the server is shutting down
                time.sleep(0.2)

    threading.Thread(target=serve, daemon=True).start()
    return worker_id


@pytest.fixture
def page_for(live):
    sync_api = pytest.importorskip("playwright.sync_api")
    problems: list[str] = []
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=chromium_executable())
        except Exception as error:  # noqa: BLE001 - optional browser dependency
            pytest.skip(f"Chromium is not available: {error}")
        context = browser.new_context(viewport={"width": 1100, "height": 800})
        signed_in = Browser(live.app)
        signed_in.login("admin", "admin-password")
        cookie = signed_in.client.get_cookie(live.app.config.get("SESSION_COOKIE_NAME", "session"))
        context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": live.url}])

        def open_page(path):
            page = context.new_page()
            page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
            # Policy violations only: noVNC and Chromium log failed connections as errors on purpose.
            page.on("console", lambda message: problems.append(f"console: {message.text}")
                    if "Content Security Policy" in message.text or "Refused to" in message.text else None)
            page.goto(live.url + path)
            return page

        yield open_page
        context.close()
        browser.close()
    assert problems == []


@pytest.mark.skipif(os.name != "posix", reason="the worker's terminal host runs a POSIX shell here")
def test_the_browser_terminal_runs_commands_on_the_worker(live, page_for, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    worker_id = _worker(live, shell=True)
    page = page_for(f"/admin/workers/{worker_id}/terminal")
    page.wait_for_selector("#remote-status.success-text", timeout=20000)
    page.locator("#terminal").click()
    page.keyboard.type("echo $((6*7))banana\n")
    page.wait_for_function("() => document.querySelector('#terminal').innerText.includes('42banana')",
                           timeout=15000)
    page.locator("#remote-end").click()
    page.wait_for_function("() => document.querySelector('#remote-status').textContent.includes('ended')",
                           timeout=15000)


class FakeVnc:
    """Just enough of a VNC server (RFB 3.8, no authentication, a 64 x 48 screen) for noVNC to connect."""

    def __init__(self):
        import socket

        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import struct

        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.sendall(b"RFB 003.008\n")
                    connection.recv(12)                       # the viewer's version
                    connection.sendall(bytes([1, 1]))         # one security type: None
                    connection.recv(1)
                    connection.sendall(struct.pack("!I", 0))  # security result: OK
                    connection.recv(1)                        # ClientInit
                    pixel_format = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
                    name = b"Lab PC"
                    connection.sendall(struct.pack("!HH", 64, 48) + pixel_format + struct.pack("!I", len(name)) + name)
                    while connection.recv(65536):
                        pass
                except OSError:
                    pass

    def close(self):
        self.listener.close()


def test_the_vnc_viewer_shows_the_worker_screen(live, page_for):
    server = FakeVnc()
    worker_id = _worker(live, shell=False, services=(("vnc", server.port),))
    page = page_for(f"/admin/workers/{worker_id}/vnc?port={server.port}")
    page.wait_for_selector("#remote-status.success-text", timeout=20000)
    assert page.locator("#screen canvas").count() == 1
    server.close()


def test_the_vnc_viewer_explains_why_a_session_failed(live, page_for):
    worker_id = _worker(live, shell=False, services=(("vnc", 5999),))  # nothing listens there
    page = page_for(f"/admin/workers/{worker_id}/vnc?port=5999")
    page.wait_for_function("() => document.querySelector('#remote-status').textContent.includes('Nothing answers')",
                           timeout=20000)
