"""Regression tests for audit findings QC2-1 (fallback lanes) and QC4-1 (snapshot index reads)."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from tests.app.fixtures import Browser, _hashed
from tests.app.test_chat import new_chat, parse_sse, send, setup_models
from tests.app.test_external_providers import FakeAPI, enrolled, provider


# ----- QC2-1 -------------------------------------------------------------------

def _hold_local(app, owner_key, release, holding):
    from bananachat.services import queue

    with app.app_context():
        with queue.Slot(queue.PRIORITY_ADMIN, owner_key=owner_key, lane="local") as slot:
            for _ in slot.positions(timeout=10):
                pass
            holding.set()
            release.wait(10)


def _user(app, name):
    from bananachat.db import users

    with app.test_request_context():
        users.create(name, _hashed(f"{name}-password"), role="user")
        return users.get_by_username(name)["id"]


def _local_chats(fake_ollama):
    return len([path for path, _ in fake_ollama.requests if path == "/api/chat"])


@pytest.mark.parametrize("own_place", [False, True])
def test_qc2_1_local_fallback_of_hosted_model_needs_a_local_place(make_app, fake_ollama, own_place):
    from bananachat.services import queue

    # Another account holds the only local place, or this account holds its own local place (an API answer).
    app = make_app(MAX_CONCURRENT="1" if not own_place else "2")
    app.config["BC"] = replace(app.config["BC"], queue_timeout=1)  # below the 5 s floor of BC_QUEUE_TIMEOUT
    setup_models(app)
    fake = FakeAPI()
    try:
        external = enrolled(app, provider(app, fake))
        fake.error = 400  # the provider refuses the request: no output, so the local fallback is tried
        uid = _user(app, "mallory")
        browser = Browser(app)
        browser.login("mallory")
        sid = new_chat(browser)
        release, holding = threading.Event(), threading.Event()
        owner = queue.account_key(uid) if own_place else "user:someone-else"
        holder = threading.Thread(target=_hold_local, args=(app, owner, release, holding))
        holder.start()
        assert holding.wait(5)
        try:
            before = _local_chats(fake_ollama)
            response = send(browser, sid, "hello", model=external["ollama_name"])
            events = parse_sse(response.get_data(as_text=True))
            assert _local_chats(fake_ollama) == before
            assert not any(e.get("type") == "start" and e.get("model") == "llama3.2:3b" for e in events)
            assert events[-1].get("type") == "error"
        finally:
            release.set()
            holder.join(10)
    finally:
        fake.close()


def test_qc2_1_local_fallbacks_stay_within_max_concurrent(make_app, fake_ollama, monkeypatch):
    from bananachat import db
    from bananachat.services import inference

    app = make_app(MAX_CONCURRENT="1", QUEUE_TIMEOUT="20")
    setup_models(app)
    fake_ollama.chunk_delay = 0.05
    lock = threading.Lock()
    stats = {"active": 0, "peak": 0, "local_rows": []}
    real_open = inference._open_stream

    def counting_open(request, model, via_worker, cancel, config):
        stream = real_open(request, model, via_worker, cancel, config)
        if model["backend"] != "ollama":
            return stream

        def wrapped():
            with lock:
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
                stats["local_rows"].append(db.scalar(
                    "SELECT COUNT(*) FROM inference_queue WHERE status='running' AND lane='local'", (), 0))
            try:
                yield from stream
            finally:
                with lock:
                    stats["active"] -= 1
        return wrapped()

    monkeypatch.setattr(inference, "_open_stream", counting_open)
    fake = FakeAPI()
    try:
        external = enrolled(app, provider(app, fake))
        fake.error = 400  # the provider refuses the request (a user can cause this)
        browsers = []
        for index in range(3):
            _user(app, f"u{index}")
            browser = Browser(app)
            browser.login(f"u{index}")
            browsers.append((browser, new_chat(browser)))
        results = {}

        def go(index, browser, sid):
            response = send(browser, sid, "hello", model=external["ollama_name"])
            results[index] = [e.get("model") for e in parse_sse(response.get_data(as_text=True))
                              if e.get("type") == "done"]

        threads = [threading.Thread(target=go, args=(i, b, s)) for i, (b, s) in enumerate(browsers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert results == {0: ["llama3.2:3b"], 1: ["llama3.2:3b"], 2: ["llama3.2:3b"]}
        assert stats["peak"] == 1
        assert stats["local_rows"] == [1, 1, 1]  # each local answer held the one local place
        with app.app_context():
            assert db.scalar("SELECT COUNT(*) FROM inference_queue", (), 0) == 0
    finally:
        fake.close()


# ----- QC4-1 -------------------------------------------------------------------

def _snapshot_remote(tmp_path, index: bytes, *, allow_filter: bool):
    """A bare remote with one snapshot branch holding only *index* as index.json; its ref and head."""
    import os
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    if allow_filter:
        subprocess.run(["git", "-C", str(remote), "config", "uploadpack.allowFilter", "true"], check=True)
    env = {**os.environ, "GIT_DIR": str(remote), "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b", "GIT_CONFIG_GLOBAL": "/dev/null"}

    def git(*args, data=None):
        return subprocess.run(["git", *args], input=data, env=env, capture_output=True, check=True).stdout.strip()

    blob = git("hash-object", "-w", "--stdin", data=index).decode()
    tree = git("mktree", data=f"100644 blob {blob}\tindex.json\n".encode()).decode()
    commit = git("commit-tree", tree, "-m", "x").decode()
    ref = "refs/heads/banana-backups/bananachat/chat-web/20260101T000000.000000Z-deadbeef"
    git("update-ref", ref, commit)
    return remote, ref, commit, blob


def _client(tmp_path, remote):
    from banana_backup.files import atomic_write
    from banana_backup.git import Git

    token = tmp_path / "token"
    atomic_write(token, "fixture-backup-token")
    work = tmp_path / "client"
    work.mkdir(mode=0o700)
    return Git(work, {"url": "file://" + str(remote), "forge": "github", "max_mib": 1}, token, allow_local=True)


@pytest.mark.parametrize("allow_filter", [True, False])
def test_qc4_1_oversized_snapshot_index_is_refused_unread(tmp_path, monkeypatch, allow_filter):
    import subprocess

    remote, ref, commit, blob = _snapshot_remote(tmp_path, b"[" + b"0," * 100_000 + b"0]",
                                                 allow_filter=allow_filter)
    git = _client(tmp_path, remote)
    with pytest.raises(ValueError, match="size limit"):
        git.index(ref, commit)
    if allow_filter:
        # The partial fetch left the index on the server, and refusing it did not fetch it later.
        missing = subprocess.run(["git", "--git-dir", str(git.repository), "-c", "core.hooksPath=/dev/null",
                                  "cat-file", "-e", blob], env={**git.environment, "GIT_NO_LAZY_FETCH": "1"},
                                 capture_output=True)
        assert missing.returncode != 0


def test_qc4_1_small_snapshot_index_is_still_read(tmp_path):
    index = b'{"schema": 1, "parts": []}'
    remote, ref, commit, blob = _snapshot_remote(tmp_path, index, allow_filter=True)
    git = _client(tmp_path, remote)
    assert git.index(ref, commit) == blob
    assert git.run("cat-file", "blob", blob, bounded=True) == index
