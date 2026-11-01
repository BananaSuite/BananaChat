"""Regression tests for the QC3 findings of the full re-audit pass 11 (worker job replies and download places)."""

from __future__ import annotations

import pytest

from tests.app.test_workers import MODEL, Worker, register  # noqa: F401
from tests.security.test_lc2 import _job, _row


def _open_poll(worker):
    """A poll whose reply the worker reads slowly: the response stays open."""
    reply = worker.client.get("/worker/v1/jobs/poll", query_string={"models": MODEL}, headers=worker.headers,
                              buffered=False)
    if reply.status_code == 200:
        next(iter(reply.response))
    return reply


def _places(app):
    downloads = app.extensions.get("bananachat.downloads")
    longs = app.extensions.get("bananachat.long_requests")
    return (downloads.active if downloads else 0), (longs.active if longs else 0)


@pytest.fixture
def short_polls(monkeypatch):
    from bananachat.services import remote

    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 0.3)


def test_qc3_1_one_worker_reading_slowly_holds_one_place_for_its_large_jobs(make_app, short_polls):
    app = make_app(workers_enabled=1, http_threads=8, download_threads=8)
    worker_id, token = register(app)
    worker = Worker(app, token)
    assert worker.heartbeat().status_code == 200
    for index in range(6):
        _job(app, f"{index:x}" * 32)

    # The first poll's large reply is written after the view returns: it holds a place until it is closed.
    first = _open_poll(worker)
    assert first.status_code == 200
    assert sum(_places(app)) == 1
    # More polls of the same token claim nothing while that reply is open, and hold no place once answered.
    others = [_open_poll(worker) for _ in range(5)]
    statuses = [reply.status_code for reply in others]
    rows = [_row(app, f"{index:x}" * 32)["status"] for index in range(6)]
    print("\nfurther polls:", statuses, "jobs:", rows, "places (downloads, long requests):", _places(app))
    assert statuses == [204] * 5
    assert sum(_places(app)) == 1
    assert rows.count("claimed") <= 1  # asking again gave the first job back to the pool, and claimed no other
    for reply in others:
        reply.close()
    first.close()
    assert _places(app) == (0, 0)
    # Once read, the next job reaches the worker as before.
    second = worker.poll()
    assert second.status_code == 200 and second.get_json()["job_id"] in {f"{index:x}" * 32 for index in range(6)}
    second.close()
    assert _places(app) == (0, 0)


def test_qc3_1_small_jobs_need_no_place(make_app, short_polls):
    app = make_app(workers_enabled=1, http_threads=8, download_threads=8)
    _worker_id, token = register(app)
    worker = Worker(app, token)
    assert worker.heartbeat().status_code == 200
    _job(app, "a" * 32)
    _job(app, "b" * 32, size=100)
    first = _open_poll(worker)
    assert first.status_code == 200 and sum(_places(app)) == 1
    small = worker.poll()
    assert small.status_code == 200 and small.get_json()["job_id"] == "b" * 32
    assert sum(_places(app)) == 1
    small.close()
    first.close()
    assert _places(app) == (0, 0)


def test_qc3_1_a_job_reply_uses_a_long_request_place_when_downloads_are_full(make_app, short_polls):
    app = make_app(workers_enabled=1, http_threads=8, download_threads=1)
    _worker_id, token = register(app)
    worker = Worker(app, token)
    assert worker.heartbeat().status_code == 200
    from bananachat import security

    with app.test_request_context():
        visitor = security._downloads().take("address:203.0.113.1")[0]  # someone's slow download
    _job(app, "a" * 32)
    reply = _open_poll(worker)
    assert reply.status_code == 200
    assert _places(app) == (1, 1)
    reply.close()
    assert _places(app) == (1, 0)
    visitor()


def test_qc3_1_a_large_api_answer_keeps_its_long_request_place_until_it_is_read(make_app, make_user,
                                                                                  fake_ollama):
    from bananachat import security
    from tests.app.test_api import roll_out, token_for

    app = make_app(http_threads=8, download_threads=1)
    roll_out(app)
    token = token_for(app, make_user("dev"))
    with app.test_request_context():
        visitor = security._downloads().take("address:203.0.113.1")[0]  # someone's slow download
    fake_ollama.reply = "x" * 200_000
    response = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}"},
                                      json={"model": "auto", "messages": [{"role": "user", "content": "Hi"}]},
                                      buffered=False)
    assert response.status_code == 200
    next(iter(response.response))  # a slow reader
    print("\nplaces while the answer is read (downloads, long requests):", _places(app))
    assert _places(app) == (1, 1)
    response.close()
    assert _places(app) == (1, 0)
    visitor()
