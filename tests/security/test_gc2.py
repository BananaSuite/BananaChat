"""Cloud sessions under volume (full re-audit pass 2, GC2): files stored with task starts are bounded per account
and in time (GC2-1); workspace-browser reads cannot take the sandbox runner's engine slots from other people's
commands (GC2-2)."""
from __future__ import annotations

import io
import os
import time
import types

import pytest

from tests.app.conftest import Browser
from tests.app.test_agents import (add_user, agents_app, call, fast_loop, runner, start_ok,  # noqa: F401
                                   token_file, wait_status)

MB = 1024 * 1024


# ----- GC2-1: stored uploads ---------------------------------------------------------------------------------

@pytest.fixture
def small(agents_app, monkeypatch):
    """1 MB per start, 2 MB stored per account; every sandbox start takes a while, so Stop lands before the
    files are copied (as on a real engine)."""
    from bananachat.services.agents import runner as runner_mod

    original = runner_mod.Runner.create

    def slow_create(self, *args, **kwargs):
        time.sleep(0.3)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(runner_mod.Runner, "create", slow_create)
    app = agents_app(AGENTS_MAX_UPLOAD_MB=1, AGENTS_MAX_STORED_UPLOAD_MB=2)
    alice = add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    return types.SimpleNamespace(app=app, user=alice, browser=browser,
                                 folder=app.config["BC"].instance_dir / "agent-uploads")


def _start_with_file(browser, size, name="blob.bin"):
    return browser.fetch("/agents", method="POST", data={
        "prompt": "Look at the file", "files": [(io.BytesIO(os.urandom(size)), name)]},
        content_type="multipart/form-data")


def _start_and_stop(env, size):
    response = _start_with_file(env.browser, size)
    assert response.status_code == 201, response.get_data(as_text=True)
    task_id = response.get_json()["id"]
    assert env.browser.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    wait_status(env.app, task_id, "stopped", "failed", "finished")
    return task_id


def test_gc2_1_stored_uploads_are_limited_per_account(small, runner, fake_ollama):
    first = _start_and_stop(small, 900 * 1024)
    second = _start_and_stop(small, 900 * 1024)
    assert (small.folder / first / "00.bin").exists() and (small.folder / second / "00.bin").exists()

    response = _start_with_file(small.browser, 900 * 1024)
    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "storage_limit"
    assert "2 MB" in response.get_json()["error"]["message"]
    # Nothing of the refused start stays on disk.
    assert sorted(entry.name for entry in small.folder.iterdir()) == sorted([first, second])

    # A start without files is not refused; deleting a task frees its files' room.
    fake_ollama.tool_script = [call("finish", summary="done")]
    wait_status(small.app, start_ok(small.browser, "No files"), "finished")
    assert small.browser.fetch(f"/agents/{first}/delete", method="POST").status_code == 200
    fake_ollama.tool_script = [call("finish", summary="done")]
    response = _start_with_file(small.browser, 900 * 1024)
    assert response.status_code == 201, response.get_data(as_text=True)


def test_gc2_1_files_of_starts_being_decided_count(small, runner, fake_ollama):
    """Two starts at once: the files the other one staged (no task row yet) count too."""
    from bananachat.services.agents import uploads

    with small.app.app_context():
        uploads.stage("c" * 32, [{"name": "other.bin", "kind": "file", "data": b"x" * (1200 * 1024)}],
                      small.user["id"])
    response = _start_with_file(small.browser, 900 * 1024)
    assert response.status_code == 413
    # Another account's files do not count.
    with small.app.app_context():
        uploads.discard("c" * 32)
        uploads.stage("c" * 32, [{"name": "other.bin", "kind": "file", "data": b"x" * (1200 * 1024)}], "someone")
    fake_ollama.tool_script = [call("finish", summary="done")]
    response = _start_with_file(small.browser, 900 * 1024)
    assert response.status_code == 201, response.get_data(as_text=True)


def test_gc2_1_uncopied_uploads_are_kept_for_a_day_only(small, runner, fake_ollama):
    from datetime import timedelta

    from bananachat import db
    from bananachat.services.agents import service, uploads

    kept = _start_and_stop(small, 500 * 1024)
    assert (small.folder / kept / "00.bin").exists()
    with small.app.app_context():
        service.purge_old_tasks()
        service.reap_sandboxes()
    assert (small.folder / kept / "00.bin").exists()  # a follow-up today still gets them

    with small.app.app_context():
        old = db.now(-(uploads.KEEP_UNCOPIED + timedelta(minutes=1)))
        db.execute("UPDATE agent_tasks SET finished_at=?, updated_at=? WHERE id=?", (old, old, kept))
        service.purge_old_tasks()
    assert not (small.folder / kept).exists()
    with small.app.app_context():
        assert db.one("SELECT 1 FROM agent_tasks WHERE id=?", (kept,)) is not None  # the task itself stays

    # Files left staged by a start that crashed go after a few minutes.
    with small.app.app_context():
        uploads.stage("d" * 32, [{"name": "x.bin", "kind": "file", "data": b"x"}], small.user["id"])
        service.purge_old_tasks()
        assert uploads.staged_tasks() == ["d" * 32]
        past = time.time() - uploads.STAGING_GRACE.total_seconds() - 60
        os.utime(uploads.staging_folder("d" * 32), (past, past))
        service.purge_old_tasks()
        assert uploads.staged_tasks() == []


def test_gc2_1_copied_uploads_are_deleted_and_runs_still_get_them(env_files, runner, fake_ollama):
    """The normal path is unchanged: the files reach the workspace and leave the web server."""
    app, browser, folder = env_files
    fake_ollama.tool_script = [call("finish", summary="done")]
    response = _start_with_file(browser, 1000, "data.csv")
    assert response.status_code == 201, response.get_data(as_text=True)
    task_id = response.get_json()["id"]
    wait_status(app, task_id, "finished")
    assert "/workspace/data.csv" in runner.only().files
    assert not (folder / task_id).exists() and not list(folder.glob("incoming-*"))


@pytest.fixture
def env_files(agents_app):
    app = agents_app()
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    return app, browser, app.config["BC"].instance_dir / "agent-uploads"


# ----- GC2-2: the sandbox runner's engine slots ------------------------------------------------------------------

def _is_file_op(argv):
    return argv[:3] == ["timeout", "-s", "KILL"]


def test_gc2_2_one_sandbox_holds_at_most_its_share_of_engine_slots():
    import threading

    from compute import sandbox_runner as sr
    from tests.test_sandbox_runner import FakeEngine, make_config

    engine = FakeEngine()
    release, entered = threading.Event(), []

    def handler(argv, stdin):
        if _is_file_op(argv):
            entered.append(1)
            release.wait(10)
            return sr.RunResult(0, b"D\n")
        return sr.RunResult(0, b"ok\n")

    engine.exec_handler = handler
    manager = sr.SandboxManager(make_config(max_sandboxes=2, max_execs=4), engine)
    manager.preflight()
    assert manager.config.exec_share == 2
    hog, other = manager.create({"session": "hog"}), manager.create({"session": "other"})
    refused = []

    def read():
        try:
            manager.read_path(hog.id, "/workspace")
        except sr.ApiError as error:
            refused.append((error.status, error.code, error.headers.get("Retry-After")))

    readers = [threading.Thread(target=read) for _ in range(4)]
    for thread in readers:
        thread.start()
    deadline = time.monotonic() + 5
    while len(entered) + len(refused) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert len(entered) == 2 and refused == [(503, "busy", "2")] * 2
        # The other sandbox's command still finds a slot.
        assert manager.execute(other.id, {"command": "make"})["exit_code"] == 0
    finally:
        release.set()
        for thread in readers:
            thread.join(5)
    assert hog.slots == 0 and other.slots == 0
    assert manager.read_path(hog.id, "/workspace")["type"] == "dir"  # the slots came back


def test_gc2_2_share_setting():
    from compute import sandbox_runner as sr

    base = {"BC_SANDBOX_TOKEN_FILE": "/unused"}
    assert sr.Config.from_env(base).exec_share == 2  # 8 slots, 4 sandboxes
    assert sr.Config.from_env({**base, "BC_SANDBOX_MAX_EXECS": "32"}).exec_share == 8
    assert sr.Config.from_env({**base, "BC_SANDBOX_MAX": "64"}).exec_share == 2  # never below 2 by default
    assert sr.Config.from_env({**base, "BC_SANDBOX_MAX_EXECS_PER_SANDBOX": "3"}).exec_share == 3
    assert sr.Config.from_env({**base, "BC_SANDBOX_MAX_EXECS": "2",
                               "BC_SANDBOX_MAX_EXECS_PER_SANDBOX": "5"}).exec_share == 2
    with pytest.raises(ValueError):
        sr.Config.from_env({**base, "BC_SANDBOX_MAX_EXECS_PER_SANDBOX": "-1"})


@pytest.fixture
def env_two(agents_app):
    app = agents_app()
    add_user(app, "alice", allowed=True)
    add_user(app, "bob", allowed=True)
    alice, bob = Browser(app), Browser(app)
    alice.login("alice")
    bob.login("bob")
    return app, alice, bob


def _finished_with_workspace(app, browser, fake_ollama):
    fake_ollama.tool_script = [call("list_files"), call("finish", summary="done")]
    task_id = start_ok(browser, "Make files")
    row = wait_status(app, task_id, "finished")
    assert row["sandbox_id"]
    return task_id


def test_gc2_2_a_person_reads_one_workspace_path_at_a_time(env_two, runner, fake_ollama, monkeypatch):
    import threading

    from bananachat.services.agents import runner as runner_mod

    app, alice, bob = env_two
    mine = _finished_with_workspace(app, alice, fake_ollama)
    theirs = _finished_with_workspace(app, bob, fake_ollama)
    original, entered, release = runner_mod.Runner.read, threading.Event(), threading.Event()

    def slow_read(self, sandbox_id, path, **kwargs):
        if path == "/workspace/slow":
            entered.set()
            release.wait(10)
        return original(self, sandbox_id, "/workspace", **kwargs)

    monkeypatch.setattr(runner_mod.Runner, "read", slow_read)
    tab = Browser(app)
    for cookie in alice.client._cookies.values():
        tab.client.set_cookie(cookie.key, cookie.value)
    first = {}
    worker = threading.Thread(target=lambda: first.update(
        response=tab.get(f"/agents/{mine}/workspace?path=/workspace/slow")))
    worker.start()
    try:
        assert entered.wait(5)
        for url in (f"/agents/{mine}/workspace?path=/workspace", f"/agents/{mine}/workspace/file?path=/workspace/a"):
            response = alice.get(url)
            assert response.status_code == 429 and response.headers["Retry-After"] == "2"
            assert response.get_json()["error"]["code"] == "busy"
        # Somebody else is not held up by it.
        assert bob.get(f"/agents/{theirs}/workspace?path=/workspace").status_code == 200
    finally:
        release.set()
        worker.join(10)
    assert first["response"].status_code == 200
    assert alice.get(f"/agents/{mine}/workspace?path=/workspace").status_code == 200


def test_gc2_2_workspace_reads_are_rate_limited(env_two, runner, fake_ollama, monkeypatch):
    from bananachat.services.agents import service

    app, alice, bob = env_two
    monkeypatch.setattr(service, "WORKSPACE_READS_PER_MINUTE", 3)
    mine = _finished_with_workspace(app, alice, fake_ollama)
    for _ in range(3):
        assert alice.get(f"/agents/{mine}/workspace?path=/workspace").status_code == 200
    response = alice.get(f"/agents/{mine}/workspace?path=/workspace")
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0
    theirs = _finished_with_workspace(app, bob, fake_ollama)
    assert bob.get(f"/agents/{theirs}/workspace?path=/workspace").status_code == 200


def test_gc2_2_workspace_browsing_does_not_fail_other_peoples_tasks(make_app, fake_ollama, tmp_path):
    """The auditor's scenario with the real runner (FakeEngine; every engine call takes 50 ms): Mallory opens
    her kept workspace from 16 tabs over and over while Alice's task runs four commands."""
    import threading

    from bananachat.db import agents as agents_db
    from compute import sandbox_runner as sr
    from tests.app.test_agents import configure
    from tests.test_sandbox_runner import FakeEngine, make_config

    secret = "k" * 64
    engine = FakeEngine()

    def handler(argv, stdin):
        time.sleep(0.05)
        return sr.RunResult(0, b"D\n" if _is_file_op(argv) else b"ok\n")

    engine.exec_handler = handler
    manager = sr.SandboxManager(make_config(max_sandboxes=4, max_execs=8), engine)
    manager.preflight()
    server = sr.RunnerServer(("127.0.0.1", 0), token=secret, manager=manager)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    serving.start()
    stop = threading.Event()
    hammers = []
    try:
        path = tmp_path / "runner.token"
        path.write_text(secret + "\n")
        os.chmod(path, 0o600)
        host, port = server.server_address[:2]
        app = make_app(AGENTS_RUNNER_URL=f"http://{host}:{port}", AGENTS_RUNNER_TOKEN_FILE=str(path))
        configure(app)
        add_user(app, "mallory", allowed=True)
        add_user(app, "alice", allowed=True)
        mallory, alice = Browser(app), Browser(app)
        mallory.login("mallory")
        alice.login("alice")
        own = _finished_with_workspace(app, mallory, fake_ollama)
        cookies = [(cookie.key, cookie.value) for cookie in mallory.client._cookies.values()]
        answers = {}

        def hammer():
            tab = Browser(app)
            for key, value in cookies:
                tab.client.set_cookie(key, value)
            while not stop.is_set():
                status = tab.get(f"/agents/{own}/workspace?path=/workspace").status_code
                answers[status] = answers.get(status, 0) + 1

        hammers = [threading.Thread(target=hammer, daemon=True) for _ in range(16)]
        for thread in hammers:
            thread.start()
        time.sleep(0.3)
        fake_ollama.tool_script = [call("bash", command=f"echo {index}") for index in range(4)] + \
            [call("finish", summary="done")]
        victim = start_ok(alice, "Build")
        row = wait_status(app, victim, "finished", "failed", "stopped", "out_of_budget", timeout=60)
        stop.set()
        with app.app_context():
            results = [step["tool_result"] or "" for step in agents_db.steps(victim) if step["kind"] == "tool"]
        assert row["status"] == "finished", (row["status"], row["error"], results)
        assert not any("busy" in result for result in results), results
        assert set(answers) <= {200, 429}, answers  # Mallory is told to wait, never a runner failure
    finally:
        stop.set()
        for thread in hammers:
            thread.join(10)
        server.shutdown()
        server.server_close()
