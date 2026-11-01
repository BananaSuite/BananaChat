"""Regression tests for the LC2 findings of the full re-audit pass 5 (download places and token APIs)."""

from __future__ import annotations

import json
import time

from tests.app.fixtures import Browser
from tests.app.test_agents import (add_user, agents_app, call, fast_loop, runner, start_ok,  # noqa: F401
                                   token_file, wait_status)
from tests.app.test_chat import new_chat, send
from tests.app.test_workers import MODEL, Worker, register  # noqa: F401
from tests.security.test_reaudit import _held, _image_chat

def _job(app, job_id, size=100_000):
    from bananachat.db import workers as store
    with app.app_context():
        store.insert_job(job_id, MODEL, json.dumps([{"role": "user", "content": "q" * size}]), None, 2,
                         time.time())


def _row(app, job_id):
    from bananachat import db
    with app.app_context():
        return dict(db.one("SELECT status, worker_id, requeues, deferred_by FROM worker_jobs WHERE id=?", (job_id,)))


def test_lc2_1_full_download_places_never_lose_a_claimed_job(make_app, make_user):
    from bananachat.db import chats
    from tests.app.test_chat import user_browser

    app = make_app(workers_enabled=1, worker_claim_timeout=2, http_threads=8, download_threads=8)
    alice = user_browser(app, make_user, "alice")
    session_id, _attachment = _image_chat(app, alice, "alice")
    with app.app_context():
        token = chats.share(session_id)
    # Four anonymous visitors (four addresses) read a share link slowly: the JC1-2 residual.
    held = []
    for index in range(4):
        visitor = app.test_client()
        visitor.environ_base["REMOTE_ADDR"] = f"203.0.113.{index + 1}"
        held += [_held(visitor, f"/share/{token}/download"), _held(visitor, f"/share/{token}")]
    assert app.extensions["bananachat.downloads"].active == 8

    worker_id, wtoken = register(app)
    worker = Worker(app, wtoken)
    worker.client.environ_base["REMOTE_ADDR"] = "198.51.100.7"
    assert worker.heartbeat().status_code == 200
    job_id = "c" * 32
    _job(app, job_id)
    reply = worker.poll()
    print("\npoll reply:", reply.status_code, reply.get_json())
    row = _row(app, job_id)
    print("job after the refused poll:", row)
    # The job a worker claimed is delivered, whatever other people's downloads hold.
    assert reply.status_code == 200 and job_id in reply.get_data(as_text=True)
    assert row["status"] == "claimed" and row["worker_id"] == worker_id
    reply.close()
    for response in reversed(held):  # streams opened last are closed first (test client contexts)
        response.close()


def test_lc2_1_workers_sharing_an_address_get_their_claimed_jobs(make_app):
    app = make_app(workers_enabled=1, worker_claim_timeout=2, download_threads=8)  # 2 places per address
    workers = []
    for index in range(3):
        _worker_id, wtoken = register(app, f"Lab PC {index}")
        worker = Worker(app, wtoken)
        worker.client.environ_base["REMOTE_ADDR"] = "198.51.100.7"  # one school network behind NAT
        assert worker.heartbeat().status_code == 200
        workers.append(worker)
    for index in range(3):
        _job(app, str(index) * 32)
    # Two lab PCs are still receiving their (large) jobs on a slow uplink when the third claims one.
    open_replies = []
    for worker in workers[:2]:
        reply = worker.client.get("/worker/v1/jobs/poll", query_string={"models": MODEL}, headers=worker.headers,
                                  buffered=False)
        assert reply.status_code == 200
        next(iter(reply.response))
        open_replies.append(reply)
    third = workers[2].poll()
    print("\nthird lab PC:", third.status_code, third.get_json())
    assert third.status_code == 200 and _row(app, "2" * 32)["status"] == "claimed"
    third.close()
    for reply in reversed(open_replies):
        reply.close()


def test_lc2_2_slow_archive_readers_leave_the_answers_places_free(agents_app, runner, fake_ollama):
    app = agents_app({"max_tasks_total": 8}, http_threads=8, download_threads=8)  # 4 long-request places
    held = []
    for name in ("p1", "p2", "p3", "p4"):
        add_user(app, name, allowed=True)
        browser = Browser(app)
        browser.login(name)
        fake_ollama.tool_script = [call("write_file", path="a.txt", content="hi"), call("finish", summary="ok")]
        task_id = start_ok(browser)
        wait_status(app, task_id, "finished")
        response = browser.get(f"/agents/{task_id}/workspace/archive", buffered=False)
        assert response.status_code == 200
        next(iter(response.response))  # a slow reader
        held.append(response)
    longs = app.extensions["bananachat.long_requests"]
    downloads = app.extensions.get("bananachat.downloads")
    print("\nlong-request places held:", longs.active, "download places:", downloads.active if downloads else 0)
    assert longs.active == 0  # fetching held a long-request place; sending (a small archive here) holds none
    add_user(app, "victim")
    victim = Browser(app)
    victim.login("victim")
    reply = send(victim, new_chat(victim))
    print("another user's chat send:", reply.status_code, reply.get_data(as_text=True)[:120])
    assert reply.status_code == 200
    for response in reversed(held):
        response.close()
    assert downloads.active == 0


def test_lc2_refused_exports_are_neither_built_nor_audited(make_app, make_user):
    from bananachat import db
    from tests.app.test_chat import user_browser

    app = make_app(download_threads=8)  # 2 places per client
    alice = user_browser(app, make_user)
    session_id, attachment = _image_chat(app, alice, "alice")
    held = [_held(alice, attachment), _held(alice, f"/chat/{session_id}/download")]
    refused = alice.post("/account/export/gdpr")
    assert refused.status_code == 429
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='account.export'", (), 0) == 0
        assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE key LIKE 'account-export%'", (), 0) == 0
    for response in reversed(held):
        response.close()
    assert app.extensions["bananachat.downloads"].active == 0
