"""Regression tests for audit findings C3-1, C3-3, C3-5, C3-7, C3-9 and C3-10 (workers, agents, ops)."""

from __future__ import annotations

import pytest

from tests.app.test_agents import (agents_app, call, env, fast_loop, runner, start_ok,  # noqa: F401 - fixtures
                                   steps, token_file, wait_status)
from tests.app.test_agents_git import fetch_error, net, tls_files  # noqa: F401 - fixtures
from tests.test_managed_lifecycle import checkout, installed, revision  # noqa: F401 - fixtures


# ----- C3-9: the model registry allow-list --------------------------------------------------------------------

@pytest.mark.parametrize("name", ["intranet-host/ns/model", "localhost/ns/model:latest", "metadata/computeMetadata/v1",
                                  "2130706433/ns/model", "0x7f000001/ns/model", "hf.co/a/b/c", "a/b/c/d:tag"])
def test_c3_9_dotless_registry_hosts_are_refused(name):
    from bananachat.services import pulls
    from worker.model_commands import valid_name

    with pytest.raises(ValueError, match="Ollama library"):
        pulls.validate_ollama_name(name)
    assert valid_name(name) is False


@pytest.mark.parametrize("name", ["llama3.2:3b", "library/llama3.2", "owner/model:7b", "hf.co/owner/repo:Q4_K_M",
                                  "registry.ollama.ai/library/qwen3", "huggingface.co/owner/repo"])
def test_c3_9_library_and_hugging_face_names_still_pass(name):
    from bananachat.services import pulls
    from worker.model_commands import valid_name

    assert pulls.validate_ollama_name(name)
    assert valid_name(name) is True


# ----- C3-7: the Git import's total deadline --------------------------------------------------------------------


def _dripping(stop, prefix: bytes, *, headers: bool):
    import time

    def route(handler):
        if headers:
            handler.wfile.write(prefix)
        else:
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", "1000000")
            handler.end_headers()
            handler.wfile.write(prefix)
        handler.wfile.flush()
        while not stop.is_set():
            try:
                handler.wfile.write(b"z")  # never idle long enough for READ_TIMEOUT
                handler.wfile.flush()
            except OSError:
                return
            time.sleep(0.1)
    return route


@pytest.mark.parametrize("headers", [False, True], ids=["body", "headers"])
def test_c3_7_a_dripping_server_cannot_outlast_the_total_deadline(net, tmp_path, monkeypatch, headers):
    import threading
    import time

    from bananachat.services.agents import gitfetch

    stop = threading.Event()
    prefix = b"HTTP/1.1 200 OK\r\nX-Slow: " if headers else b"\x1f\x8b"
    net.server.routes["/octo/demo/tar.gz/main"] = _dripping(stop, prefix, headers=headers)
    monkeypatch.setattr(gitfetch, "TOTAL_TIMEOUT", 0.8)
    started = time.monotonic()
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("key", fetch_error(tmp_path)), daemon=True)
    worker.start()
    worker.join(4)
    stop.set()
    assert not worker.is_alive(), "the download kept running past its deadline"
    assert outcome["key"] == "agents.import_timeout"
    assert time.monotonic() - started < 3


# ----- C3-10: hard links in data/ and the packages made of it -----------------------------------------------------


def test_c3_10_hard_linked_files_are_packaged_as_restorable_copies(tmp_path):
    import os

    from banana_ops.files import read_package, regular_files, write_package

    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    (data / "uploads" / "a.bin").write_bytes(b"x" * 100)
    os.link(data / "uploads" / "a.bin", data / "uploads" / "b.bin")
    inputs = [(path, "data/" + name) for path, name in regular_files(data)]
    package = write_package(tmp_path / "out" / "package.tar.gz", {"product": "BananaChat"}, inputs)
    (tmp_path / "staging").mkdir()
    with read_package(package, "BananaChat", tmp_path / "staging") as (root, manifest):
        assert set(manifest["files"]) == {"data/uploads/a.bin", "data/uploads/b.bin"}
        assert (root / "data/uploads/b.bin").read_bytes() == b"x" * 100
        assert (root / "data/uploads/a.bin").stat().st_ino != (root / "data/uploads/b.bin").stat().st_ino


def test_c3_10_packages_with_link_members_from_older_releases_still_restore(tmp_path):
    import io
    import json
    import os
    import tarfile

    from banana_ops.files import digest_file, read_package

    data = tmp_path / "data"
    data.mkdir()
    (data / "a.bin").write_bytes(b"payload")
    os.link(data / "a.bin", data / "b.bin")
    package = tmp_path / "old.tar.gz"
    files = {}
    with tarfile.open(package, "w:gz") as archive:  # what write_package did before: tarfile.add
        for name in ("a.bin", "b.bin"):
            files["data/" + name] = {"sha256": digest_file(data / name), "size": 7}
            archive.add(data / name, arcname="data/" + name, recursive=False)
        assert archive.getmember("data/b.bin").islnk()
        raw = json.dumps({"product": "BananaChat", "schema": 1, "files": files}).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    (tmp_path / "staging").mkdir()
    with read_package(package, "BananaChat", tmp_path / "staging") as (root, _manifest):
        assert (root / "data/b.bin").read_bytes() == b"payload"


def test_c3_10_a_failed_update_rolls_back_despite_a_hard_link_in_data(tmp_path, checkout):
    import os

    manager, services = installed(tmp_path, checkout)
    data = manager.root / "data"
    (data / "upload.bin").write_bytes(b"user file")
    os.link(data / "upload.bin", data / "upload-copy.bin")
    old = manager.settings()["revision"]
    services.fail_revision = revision(checkout)
    with pytest.raises(RuntimeError, match="readiness"):
        manager.update()
    assert manager.settings()["revision"] == old
    assert not (manager.config_dir / "transaction.json").exists()
    assert not (data / ".banana-maintenance").exists()
    assert (data / "upload-copy.bin").read_bytes() == b"user file"
    package = manager.backup()
    manager.restore(package)
    assert (data / "upload.bin").read_bytes() == b"user file"


# ----- C3-1: workers sharing overlapping networks -------------------------------------------------------------------

@pytest.fixture
def remote_app(make_app):
    from bananachat.services import remote

    remote._cooldown.clear()
    yield make_app(workers_enabled=1, worker_remote_access=1)
    remote._cooldown.clear()


def _worker(app, name, subnets, *, shell=False):
    import json

    from bananachat import db
    from bananachat.services import remote

    with app.app_context():
        with db.transaction():
            worker_id, token = remote.register_worker(name)
    body = {"status": "online", "gpu_name": "RTX", "ollama_version": "0.9.0", "capabilities": {"models": []},
            "activity_state": "idle", "gpu_util": 0.0,
            "remote": {"enabled": True, "shell": shell, "subnets": subnets, "subnet_ports": [22, 3389, 5900]}}
    response = app.test_client().post("/worker/v1/heartbeat", data=json.dumps(body), headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return worker_id, token


def _helper_key(app):
    from bananachat.db import users
    from bananachat.services import worker_remote

    with app.app_context():
        return worker_remote.create_key(users.get_by_username("admin"), "laptop", 8)[1]


@pytest.mark.parametrize("evil_subnet", ["192.168.0.0/16", "192.168.1.0/25", "192.168.1.0/24"])
def test_c3_1_a_worker_cannot_capture_sessions_to_another_workers_network(remote_app, evil_subnet):
    import json

    from bananachat.db import worker_remote as store

    evil_id, _ = _worker(remote_app, "Evil PC", [evil_subnet])  # registered first
    office_id, _ = _worker(remote_app, "Office PC", ["192.168.1.0/24"])
    headers = {"Authorization": f"Bearer {_helper_key(remote_app)}", "Content-Type": "application/json"}
    client = remote_app.test_client()
    refused = client.post("/remote/v1/sessions", data=json.dumps({"host": "192.168.1.20", "port": 22}),
                          headers=headers)
    assert refused.status_code == 409
    message = refused.get_json()["error"]["message"]
    assert "Evil PC" in message and "Office PC" in message and "--worker" in message
    with remote_app.app_context():
        assert store.live_tunnels(evil_id) == []
    chosen = client.post("/remote/v1/sessions", data=json.dumps(
        {"worker": office_id, "host": "192.168.1.20", "port": 22}), headers=headers)
    assert chosen.status_code == 201 and chosen.get_json()["session"]["worker_id"] == office_id


def test_c3_1_an_address_only_one_worker_shares_still_resolves(remote_app):
    from bananachat.services import worker_remote

    _worker(remote_app, "Home PC", ["192.168.1.0/24"])
    office_id, _ = _worker(remote_app, "Office PC", ["10.20.0.0/16"])
    with remote_app.app_context():
        assert worker_remote.resolve("10.20.3.4", 3389) == (office_id, "rdp", "10.20.3.4", 3389)


def test_c3_1_the_helper_names_the_worker_that_answered_in_proxy_mode(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from tests.app.test_worker_remote_access import _load_helper

    helper = _load_helper()
    sent = {}

    class Socket:
        worker = ""

        def receive(self):
            return helper.TEXT, json.dumps({"type": "status", "state": "connected"}).encode()

    def request(_server, _key, method, path, body=None):
        if path == "/remote/v1/workers":
            return {"workers": [{"id": "f" * 32, "slug": "office-pc", "address": "10.77.0.3", "name": "Office PC"}]}
        sent.update(body)
        return {"session": {"id": "a" * 32, "worker": "Office PC", "host": body["host"], "port": body["port"]}}

    monkeypatch.setattr(helper, "load_settings", lambda: ("https://chat.example.org", "bca_x"))
    monkeypatch.setattr(helper, "request", request)
    monkeypatch.setattr(helper, "open_socket", lambda *_args: Socket())
    monkeypatch.setattr(helper, "relay", lambda *_args: None)
    assert helper.cmd_proxy(SimpleNamespace(host="192.168.1.20", port=22, worker="office-pc")) == 0
    assert sent == {"host": "192.168.1.20", "port": 22, "worker": "f" * 32}
    assert "Connected to Office PC" in capsys.readouterr().err


# ----- C3-3: browser tunnels and the sign-in that opened them -------------------------------------------------------

def test_c3_3_a_browser_terminal_ends_when_its_sign_in_is_revoked(remote_app, monkeypatch):
    from flask import Response

    from bananachat import db
    from bananachat.db import users
    from bananachat.db import worker_remote as store
    from bananachat.web import _tunnels
    from tests.app.fixtures import Browser

    worker_id, _ = _worker(remote_app, "Lab PC", [], shell=True)
    stolen = Browser(remote_app)  # a stolen (or forgotten) admin cookie
    stolen.login("admin", "admin-password")
    created = stolen.post_json(f"/admin/workers/{worker_id}/remote/sessions", {"kind": "shell"})
    assert created.status_code == 201, created.get_data(as_text=True)
    session = created.get_json()["session"]
    bridged = {}

    def bridge(tunnel_id, **kwargs):
        bridged.update(kwargs, tunnel_id=tunnel_id)
        return Response(status=204)

    monkeypatch.setattr(_tunnels, "client_bridge", bridge)
    stolen.get(session["ws"], headers={"Upgrade": "websocket", "Connection": "Upgrade", "Origin": "http://localhost",
                                       "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
    assert bridged["tunnel_id"] == session["id"] and bridged["auth_session"]
    with remote_app.test_request_context():
        store.open_tunnel(session["id"], 0)
        assert not _tunnels._closed_by_administrator(session["id"], bridged["auth_session"])()
        with db.transaction():  # the administrator signs out everywhere (or changes the password)
            users.revoke_sessions(users.get_by_username("admin")["id"])
        assert _tunnels._closed_by_administrator(session["id"], bridged["auth_session"])()
        assert store.tunnel(session["id"])["status"] == "failed"


# ----- C3-5: the runner's two archive slots ------------------------------------------------------------------------


def test_c3_5_workspace_uploads_are_one_at_a_time_and_capped_per_hour(env, runner, fake_ollama, monkeypatch):
    import io

    from bananachat.db import agents as agents_db
    from bananachat.web import agents as agents_web

    fake_ollama.tool_script = [call("bash", command="ls"), call("finish", summary="done")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")  # its sandbox is kept for a while

    def upload():
        return env.browser.fetch(f"/agents/{task_id}/workspace/upload", method="POST",
                                 data={"files": [(io.BytesIO(b"x"), "a.txt")]}, content_type="multipart/form-data")

    with env.app.app_context():
        held = agents_db.claim_upload(env.user["id"])  # another upload of this person is in flight
    busy = upload()
    assert busy.status_code == 429 and busy.get_json()["error"]["code"] == "busy"
    with env.app.app_context():
        agents_db.release_upload(env.user["id"], held)
    monkeypatch.setattr(agents_web, "UPLOADS_PER_HOUR", 2)
    assert [upload().status_code for _ in range(3)] == [200, 200, 429]


def _busy_archives(monkeypatch, times):
    """The runner's archive slots are taken by someone else for the first *times* archive uploads."""
    from bananachat.services.agents import runner as runner_mod

    original = runner_mod.Runner.put_archive
    calls = {"busy": 0, "times": times}

    def put_archive(self, *args, **kwargs):
        if calls["busy"] < calls["times"]:
            calls["busy"] += 1
            raise runner_mod.RunnerError("Too many transfers at once. Retry shortly.", 503, code="busy",
                                         transient=True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(runner_mod.Runner, "put_archive", put_archive)
    return calls


def _zip():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("src/main.py", "print(1)\n")
    return buffer.getvalue()


def test_c3_5_a_task_start_waits_for_a_busy_runner_instead_of_dropping_its_archive(env, runner, fake_ollama,
                                                                                    monkeypatch):
    import io

    from bananachat.services.agents import loop

    monkeypatch.setattr(loop, "RUNNER_BUSY_BACKOFF", 0.01, raising=False)
    calls = _busy_archives(monkeypatch, 8)  # more than the runner retries
    fake_ollama.tool_script = [call("finish", summary="done")]
    response = env.browser.fetch("/agents", method="POST", data={
        "prompt": "Look", "files": [(io.BytesIO(_zip()), "project.zip")]}, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_data(as_text=True)
    wait_status(env.app, response.get_json()["id"], "finished")
    assert calls["busy"] == 8
    assert runner.only().files["/workspace/src/main.py"] == b"print(1)\n"


def test_c3_5_uploads_not_copied_are_kept_for_the_next_run(env, runner, fake_ollama, monkeypatch):
    import io

    from bananachat.services.agents import loop, uploads

    monkeypatch.setattr(loop, "RUNNER_BUSY_BACKOFF", 0.01, raising=False)
    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 0.2)
    busy = _busy_archives(monkeypatch, 10 ** 6)
    fake_ollama.tool_script = [call("finish", summary="first")]
    response = env.browser.fetch("/agents", method="POST", data={
        "prompt": "Look", "files": [(io.BytesIO(_zip()), "project.zip")]}, content_type="multipart/form-data")
    task_id = response.get_json()["id"]
    wait_status(env.app, task_id, "finished")
    assert any("agents.notice_upload_kept" in (step["content"] or "") for step in steps(env.app, task_id))
    with env.app.app_context():
        assert [item["name"] for item in uploads.pending(task_id)] == ["project.zip"]
    busy["times"] = 0  # the runner has room again
    fake_ollama.tool_script = [call("finish", summary="second")]
    assert env.browser.post_json(f"/agents/{task_id}/messages", {"content": "go on"}).status_code == 202
    wait_status(env.app, task_id, "finished")
    assert runner.only().files["/workspace/src/main.py"] == b"print(1)\n"
    with env.app.app_context():
        assert uploads.pending(task_id) == []
