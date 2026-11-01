"""Runner archive slots: one person holds one at a time, a task start included, through long waits
(AUDIT.md T2-1, U2-3)."""
from __future__ import annotations

import io
import threading
import time
import zipfile

from tests.app.test_agents import (agents_app, call, env, fast_loop, runner, start_ok, token_file,  # noqa: F401
                                   wait_for, wait_status)


def _zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("src/main.py", "print(1)\n")
    return buffer.getvalue()


def test_t2_1_a_task_start_and_a_download_by_one_person_do_not_run_at_once(env, runner, fake_ollama, monkeypatch):
    from bananachat.services.agents import runner as runner_mod

    # 1. a finished task that kept its workspace
    fake_ollama.tool_script = [call("list_files"), call("finish", summary="done")]
    first = start_ok(env.browser, "First")
    wait_status(env.app, first, "finished")

    # 2. count archive transfers in flight (what the runner's LARGE_TRANSFERS=2 semaphore counts)
    state = {"now": 0, "max": 0}
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Event()
    original_put, original_open = runner_mod.Runner.put_archive, runner_mod.Runner.open_archive

    def enter():
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])

    def leave():
        with lock:
            state["now"] -= 1

    def put_archive(self, *args, **kwargs):
        enter()
        entered.set()
        try:
            release.wait(20)  # a slow unpack (the runner allows ARCHIVE_TIMEOUT = 300 s)
            return original_put(self, *args, **kwargs)
        finally:
            leave()

    def open_archive(self, *args, **kwargs):
        enter()
        try:
            return original_open(self, *args, **kwargs)
        finally:
            leave()

    monkeypatch.setattr(runner_mod.Runner, "put_archive", put_archive)
    monkeypatch.setattr(runner_mod.Runner, "open_archive", open_archive)

    # 3. a second task whose start pushes an uploaded archive into its sandbox (slot 1)
    fake_ollama.tool_script = [call("finish", summary="done")]
    response = env.browser.fetch("/agents", method="POST", data={
        "prompt": "Second", "files": [(io.BytesIO(_zip()), "project.zip")]}, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_data(as_text=True)
    second = response.get_json()["id"]
    assert entered.wait(10)

    # 4. meanwhile the same person downloads the first workspace (slot 2): the transfer lease is free
    download = env.browser.get(f"/agents/{first}/workspace/archive")
    download.close()
    release.set()
    wait_status(env.app, second, "finished")
    assert download.status_code == 429  # the person's own task start holds their runner transfer
    assert state["max"] == 1


def test_u2_3_a_task_does_not_copy_once_its_transfer_was_lost(app, make_user, monkeypatch):
    """A long wait let the lease lapse and the person's download take it: the copy waits, not runs beside it."""
    from types import SimpleNamespace

    import pytest

    from bananachat.db import agents as agents_db
    from bananachat.services.agents import loop, runner as runner_mod

    user = make_user("waiter")
    clock = [1000.0]
    monkeypatch.setattr(agents_db.time, "time", lambda: clock[0])
    task = SimpleNamespace(user=user, _sleep=lambda seconds: None)
    with app.test_request_context(), loop.TaskRun._transfer(task) as keep:
        keep()  # renewed while held
        clock[0] += agents_db.TRANSFER_LEASE_SECONDS + 1  # a capacity wait outlived the lease
        download = agents_db.claim_download(user["id"])
        assert download
        with pytest.raises(runner_mod.RunnerError) as refused:
            keep()
        assert refused.value.code == "busy"
        agents_db.release_download(user["id"], download)
        keep()  # taken again once free
