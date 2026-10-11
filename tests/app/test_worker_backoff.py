"""Volunteer workers that stall or keep handing jobs back are paused, and a job never loops between them."""

from __future__ import annotations

import time

from tests.app.test_workers import (MODEL, Worker, _fresh_routing_state, generate, job_rows,  # noqa: F401
                                    pool_app, register, run_in_thread, text_of)


def _worker_row(app, worker_id):
    from bananachat.db import workers as store

    with app.app_context():
        return store.get(worker_id)


def _routes(app, user):
    from bananachat.db import catalog
    from bananachat.services import ollama, remote

    with app.app_context():
        ollama.sync_catalog()
        return remote.should_route(user, catalog.get_by_name(MODEL))


def _claim(app, worker_id):
    from bananachat.services import remote

    with app.app_context():
        return remote.claim(_worker_row(app, worker_id), [MODEL])


def test_a_worker_that_never_starts_answering_is_paused(pool_app, make_user, fake_ollama):  # noqa: F811
    from bananachat import db
    from bananachat.db import workers as store

    worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    user = make_user("alice")
    assert _routes(pool_app, user)

    def stall():
        job = worker.poll().get_json()
        # Its Ollama hangs loading the model: no chunk ever comes. Fast-forward past the first-token timeout.
        with pool_app.app_context():
            db.execute("UPDATE worker_jobs SET claimed_at=? WHERE id=?", (time.time() - 10_000, job["job_id"]))

    thread, errors = run_in_thread(stall)
    events = generate(pool_app, user)
    thread.join(10)
    assert not errors, errors
    assert text_of(events) == "Hello from the fake model." and events[-1].state == "completed"  # answered locally
    row = _worker_row(pool_app, worker_id)
    assert row["strikes"] == 1 and row["cooldown_until"] > time.time() + 30
    # While it is paused, requests stay on this server and it is offered no job.
    worker.heartbeat()
    assert not _routes(pool_app, user)
    with pool_app.app_context():
        store.insert_job("f" * 32, MODEL, "[]", None, 2, time.time())
    assert _claim(pool_app, worker_id) is None


def test_repeated_failures_pause_a_worker_longer_and_an_answer_clears_them(pool_app, make_user):  # noqa: F811
    from bananachat import db
    from bananachat.db import workers as store
    from bananachat.services import remote

    worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    pauses = []
    for index in range(3):
        with pool_app.app_context():
            db.execute("UPDATE worker_nodes SET cooldown_until=NULL WHERE id=?", (worker_id,))
            store.insert_job(str(index) * 32, MODEL, "[]", None, 2, time.time())
        job = _claim(pool_app, worker_id)
        # Its own Ollama is down (memory a request needs is that request's problem, see test_worker_fairness).
        assert worker.fail(job["job_id"], {"error": "Ollama could not be reached: connection refused"}
                           ).get_json()["ok"]
        pauses.append(_worker_row(pool_app, worker_id)["cooldown_until"] - time.time())
    assert pauses[0] < pauses[1] < pauses[2] <= remote.PAUSE_MAX_SECONDS
    with pool_app.app_context():
        db.execute("UPDATE worker_nodes SET cooldown_until=NULL WHERE id=?", (worker_id,))
        store.insert_job("9" * 32, MODEL, "[]", None, 2, time.time())
    job = _claim(pool_app, worker_id)
    worker.chunk(job["job_id"], 0, "Fine", done=True)
    assert worker.complete(job["job_id"]).get_json()["ok"]
    row = _worker_row(pool_app, worker_id)
    assert row["strikes"] == 0 and row["cooldown_until"] is None


def test_a_worker_that_hands_a_job_back_never_gets_the_same_job_again(pool_app, make_user, fake_ollama):  # noqa: F811
    worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    user = make_user("bob")
    claims = []

    def defer_forever():
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = _claim(pool_app, worker_id)
            if job is None:
                time.sleep(0.1)
                continue
            claims.append(job["job_id"])
            worker.fail(job["job_id"], {"requeue": True})

    thread, errors = run_in_thread(defer_forever)
    started = time.monotonic()
    events = generate(pool_app, user)
    elapsed = time.monotonic() - started
    thread.join(10)
    assert not errors, errors
    assert claims and len(claims) == 1  # deferring is fine, but not to grab the job again
    # Answered locally at the claim timeout (2 s here), not after the first-token timeout (180 s); the margin is
    # for a slow, loaded machine.
    assert text_of(events) == "Hello from the fake model." and elapsed < 60
    assert _worker_row(pool_app, worker_id)["strikes"] == 0  # handing a job back is not a failure


def test_a_job_goes_back_to_the_pool_only_a_few_times(pool_app, make_user, fake_ollama):  # noqa: F811
    from bananachat.services import remote

    workers = [register(pool_app, name) for name in ("A", "B", "C", "D", "E")]
    for _worker_id, token in workers:
        Worker(pool_app, token).heartbeat()
    user = make_user("carol")
    claims = []

    def pass_it_around():
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            for worker_id, token in workers:
                job = _claim(pool_app, worker_id)
                if job is not None:
                    claims.append(worker_id)
                    Worker(pool_app, token).fail(job["job_id"], {"error": "deferred"})
            time.sleep(0.05)

    thread, errors = run_in_thread(pass_it_around)
    events = generate(pool_app, user)
    thread.join(10)
    assert not errors, errors
    assert len(claims) == remote.MAX_REQUEUES + 1
    assert text_of(events) == "Hello from the fake model." and events[-1].state == "completed"
    [row] = job_rows(pool_app)
    assert row["status"] == "failed" and row["requeues"] == remote.MAX_REQUEUES


def test_administrators_see_a_paused_worker(pool_app, admin):  # noqa: F811
    from bananachat import db

    worker_id, token = register(pool_app, "Flaky PC")
    Worker(pool_app, token).heartbeat()
    assert "Paused after failures" not in admin.get("/admin/workers").get_data(as_text=True)
    with pool_app.app_context():
        db.execute("UPDATE worker_nodes SET strikes=2, cooldown_until=? WHERE id=?", (time.time() + 120, worker_id))
    assert "Paused after failures" in admin.get("/admin/workers").get_data(as_text=True)


def test_enabling_a_worker_again_clears_its_pause(pool_app):  # noqa: F811
    from bananachat import db
    from bananachat.services import remote

    worker_id, _token = register(pool_app)
    with pool_app.app_context():
        db.execute("UPDATE worker_nodes SET strikes=3, cooldown_until=? WHERE id=?", (time.time() + 600, worker_id))
        assert remote.set_enabled(worker_id, False) and remote.set_enabled(worker_id, True)
    row = _worker_row(pool_app, worker_id)
    assert row["strikes"] == 0 and row["cooldown_until"] is None


def test_a_failure_caused_by_the_users_input_does_not_pause_the_worker(pool_app):  # noqa: F811
    import json

    from bananachat.db import workers as store

    worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    with pool_app.app_context():
        store.insert_job("7" * 32, MODEL, json.dumps([{"role": "user", "content": "x", "images": ["AAAA"]}]), None, 2,
                         time.time())
    job = _claim(pool_app, worker_id)
    assert worker.fail(job["job_id"], {"error": "failed to process inputs: image: unknown format"}).get_json()["ok"]
    row = _worker_row(pool_app, worker_id)
    assert row["strikes"] == 0 and row["cooldown_until"] is None
