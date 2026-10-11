"""One account's requests cannot pause the volunteer workers everyone shares, and a worker is only blamed for its
own delays."""

from __future__ import annotations

import json
import threading
import time

import pytest

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


def _failing_daemon(app, worker_id, token, error, stop):
    """A volunteer daemon that answers its first job with *error* (as its Ollama would), until *stop* is set."""
    from bananachat.services import remote

    def work():
        while not stop.is_set():
            with app.app_context():
                job = remote.claim(_worker_row(app, worker_id), [MODEL])
            if job is not None:
                Worker(app, token).fail(job["job_id"], {"error": error})
                return
            stop.wait(0.05)
    return work


@pytest.mark.parametrize("error", [
    "Ollama answered HTTP 500: CUDA error: out of memory",
    "Ollama answered HTTP 500: model requires more system memory (12.0 GiB) than is available (7.5 GiB)",
    "Ollama answered HTTP 400: invalid options: num_ctx",
])
def test_a_request_the_workers_hardware_cannot_serve_pauses_no_worker(pool_app, make_user, fake_ollama, error):  # noqa: F811
    workers = [register(pool_app, name) for name in ("PC-1", "PC-2")]
    for _worker_id, token in workers:
        Worker(pool_app, token).heartbeat()
    mallory, bob = make_user("mallory"), make_user("bob")
    stop = threading.Event()
    threads = [run_in_thread(_failing_daemon(pool_app, worker_id, token, error, stop)) for worker_id, token in workers]
    try:
        for _round in range(2):
            events = generate(pool_app, mallory)
            assert text_of(events) == "Hello from the fake model."  # answered by this server instead
    finally:
        stop.set()
        for thread, errors in threads:
            thread.join(10)
            assert not errors, errors
    for worker_id, token in workers:
        row = _worker_row(pool_app, worker_id)
        assert row["strikes"] == 0 and row["cooldown_until"] is None
        Worker(pool_app, token).heartbeat()
    # Each worker got one of the account's jobs: it is not sent to a worker its request failed on for a while, so
    # with both tried it is answered locally.
    rows = job_rows(pool_app)
    assert [row["fault"] for row in rows] == ["request", "request"]
    assert {row["worker_id"] for row in rows} == {worker_id for worker_id, _token in workers}
    assert not _routes(pool_app, mallory)
    assert _routes(pool_app, bob)  # everyone else keeps the workers


_NO_MEMORY = "Ollama answered HTTP 500: model requires more system memory (12.0 GiB) than is available (7.5 GiB)"


def _fail_on(app, worker_id, token, user, error=_NO_MEMORY):
    """One of *user*'s jobs reaches the worker, which reports *error*."""
    import uuid

    from bananachat.db import workers as store

    job_id = uuid.uuid4().hex
    with app.app_context():
        store.insert_job(job_id, MODEL, "[]", None, 2, time.time(), requested_by=user["id"])
        assert store.set_claimed(job_id, worker_id, time.time())
    assert Worker(app, token).fail(job_id, {"error": error}).get_json()["ok"]
    return job_id


def test_a_worker_that_fails_everyone_for_the_requests_sake_is_paused_and_benches_nobody(pool_app, make_user):  # noqa: F811
    """CC-2 (final verification): the failure text comes from the worker. A PC that cannot load the model it
    advertises, or one that lies, fails every job with "more system memory": after the jobs of a few different
    accounts failed on it (and none succeeded) it is paused like any failing worker, and the accounts it failed
    keep the other workers."""
    from bananachat.db import workers as store
    from bananachat.services import remote

    (broken_id, broken_token), (healthy_id, healthy_token) = register(pool_app, "Broken"), register(pool_app, "Healthy")
    users = [make_user(f"user{index}") for index in range(4)]
    for index, user in enumerate(users):
        Worker(pool_app, broken_token).heartbeat()
        Worker(pool_app, healthy_token).heartbeat()
        _fail_on(pool_app, broken_id, broken_token, user)
        row = _worker_row(pool_app, broken_id)
        # Two accounts' requests may be at fault, three are not; each further failure doubles the pause.
        assert row["strikes"] == (0 if index < 2 else index - 1), index
    assert float(_worker_row(pool_app, broken_id)["cooldown_until"]) > time.time()
    assert [row["fault"] for row in job_rows(pool_app)] == ["request"] * 4  # no account used up its one blame
    Worker(pool_app, healthy_token).heartbeat()
    assert all(_routes(pool_app, user) for user in users)  # the healthy worker still serves all of them
    # Once its pause is over, the broken worker is not offered the jobs of the accounts it failed.
    with pool_app.app_context():
        store.pause(broken_id, 0, 0)
        store.insert_job("a" * 32, MODEL, "[]", None, 2, time.time(), requested_by=users[0]["id"])
        assert remote.claim(_worker_row(pool_app, broken_id), [MODEL]) is None
        assert remote.claim(_worker_row(pool_app, healthy_id), [MODEL])["job_id"] == "a" * 32


def test_an_account_whose_request_failed_on_a_worker_is_not_sent_there_again(pool_app, make_user):  # noqa: F811
    """A failure held against the request takes the account off that worker, not off the pool; with no other idle
    worker it is answered locally."""
    (only_id, only_token) = register(pool_app, "Only")
    alice, bob = make_user("alice"), make_user("bob")
    Worker(pool_app, only_token).heartbeat()
    _fail_on(pool_app, only_id, only_token, alice, "Ollama answered HTTP 400: invalid options: num_ctx")
    Worker(pool_app, only_token).heartbeat()
    assert not _routes(pool_app, alice)
    assert _routes(pool_app, bob)
    assert _worker_row(pool_app, only_id)["strikes"] == 0


def test_request_failures_between_answered_jobs_do_not_pause_a_worker(pool_app, make_user):  # noqa: F811
    from bananachat.db import workers as store

    worker_id, token = register(pool_app, "Busy")
    users = [make_user(f"user{index}") for index in range(4)]
    for index, user in enumerate(users):
        _fail_on(pool_app, worker_id, token, user, "Ollama answered HTTP 400: invalid options: num_ctx")
        job_id = f"{index}" * 32
        with pool_app.app_context():  # someone else's job between the failures is answered
            store.insert_job(job_id, MODEL, "[]", None, 2, time.time())
            assert store.set_claimed(job_id, worker_id, time.time())
        worker = Worker(pool_app, token)
        worker.chunk(job_id, 0, "Hi", done=True)
        assert worker.complete(job_id).get_json()["ok"]
    assert _worker_row(pool_app, worker_id)["strikes"] == 0


def test_one_account_holds_at_most_one_worker_responsible_at_a_time(pool_app, make_user):  # noqa: F811
    from bananachat.db import workers as store
    from bananachat.services import remote

    workers = [register(pool_app, name) for name in ("PC-1", "PC-2", "PC-3")]
    mallory, bob = make_user("mallory"), make_user("bob")
    now = time.time()
    with pool_app.app_context():
        # Three of mallory's requests reached three workers at once (API streams); each worker then failed.
        for index, (worker_id, _token) in enumerate(workers):
            store.insert_job(str(index) * 32, MODEL, "[]", None, 2, now, requested_by=mallory["id"])
            assert store.set_claimed(str(index) * 32, worker_id, now)
        store.insert_job("b" * 32, MODEL, "[]", None, 2, now, requested_by=bob["id"])
    for index, (_worker_id, token) in enumerate(workers):
        assert Worker(pool_app, token).fail(str(index) * 32, {"error": "llama runner process has terminated"}
                                            ).get_json()["ok"]
    paused = [worker_id for worker_id, _ in workers if _worker_row(pool_app, worker_id)["strikes"]]
    assert len(paused) == 1  # the first failure counts against its worker; the others against the requests
    faults = {row["id"][:1]: row["fault"] for row in job_rows(pool_app) if row["requested_by"] == mallory["id"]}
    assert sorted(faults.values()) == ["request", "request", "worker"]
    # Another account's failure still counts against a (still unpaused) worker.
    free_id, free_token = next((worker_id, token) for worker_id, token in workers if worker_id not in paused)
    with pool_app.app_context():
        assert store.set_claimed("b" * 32, free_id, time.time())
    Worker(pool_app, free_token).fail("b" * 32, {"error": "llama runner process has terminated"})
    assert _worker_row(pool_app, free_id)["strikes"] == 1
    with pool_app.app_context():
        assert remote.USER_FAULT_SECONDS < remote.PURGE_AFTER_SECONDS  # the record outlives the window


def test_a_stalling_job_of_an_account_that_already_failed_a_worker_pauses_no_other(pool_app, make_user):  # noqa: F811
    from bananachat import db
    from bananachat.db import workers as store
    from bananachat.services import remote

    (first_id, first_token), (second_id, _second_token) = register(pool_app, "A"), register(pool_app, "B")
    mallory = make_user("mallory")
    now = time.time()
    with pool_app.app_context():
        for job_id, worker_id in (("1" * 32, first_id), ("2" * 32, second_id)):
            store.insert_job(job_id, MODEL, "[]", None, 2, now, requested_by=mallory["id"])
            store.set_claimed(job_id, worker_id, now)
    Worker(pool_app, first_token).fail("1" * 32, {"error": "llama runner process has terminated"})
    with pool_app.app_context():
        # B keeps sending heartbeats but never produced a first chunk: the housekeeping times its job out.
        db.execute("UPDATE worker_jobs SET claimed_at=?, heartbeat_at=? WHERE id=?", (now - 10_000, time.time(),
                                                                                    "2" * 32))
        assert remote.maintain()["timed_out"] == 1
    assert _worker_row(pool_app, first_id)["strikes"] == 1
    assert _worker_row(pool_app, second_id)["strikes"] == 0


def test_a_worker_is_not_blamed_for_a_lease_that_ran_out_while_this_server_was_down(pool_app, monkeypatch):  # noqa: F811
    from bananachat import db
    from bananachat.db import workers as store
    from bananachat.services import remote

    (silent_id, _), (stalled_id, _) = register(pool_app, "Silent"), register(pool_app, "Stalled")
    long_ago = time.time() - 10_000
    with pool_app.app_context():
        for job_id, worker_id in (("1" * 32, silent_id), ("2" * 32, stalled_id)):
            store.insert_job(job_id, MODEL, "[]", None, 2, long_ago)
            store.set_claimed(job_id, worker_id, long_ago)
        # The stalled worker went silent while this server ran; the other one last spoke before an update.
        db.execute("UPDATE worker_jobs SET heartbeat_at=? WHERE id=?", (time.time() - 1000, "2" * 32))
    monkeypatch.setattr(remote, "_SERVER_STARTED", time.time() - 2000)
    with pool_app.app_context():
        db.execute("UPDATE worker_jobs SET heartbeat_at=? WHERE id=?", (time.time() - 5000, "1" * 32))
        assert remote.maintain()["timed_out"] == 2
    assert _worker_row(pool_app, silent_id)["strikes"] == 0  # it could not reach the server
    assert _worker_row(pool_app, stalled_id)["strikes"] == 1


def test_jobs_name_their_account(pool_app, make_user, fake_ollama):  # noqa: F811
    _worker_id, token = register(pool_app)
    worker = Worker(pool_app, token)
    worker.heartbeat()
    user = make_user("alice")

    def work():
        job = worker.poll().get_json()
        worker.chunk(job["job_id"], 0, "Hi", done=True)
        worker.complete(job["job_id"])

    thread, errors = run_in_thread(work)
    generate(pool_app, user)
    thread.join(10)
    assert not errors, errors
    [row] = job_rows(pool_app)
    assert row["requested_by"] == user["id"] and row["fault"] is None and row["status"] == "done"
    assert _routes(pool_app, user)  # a completed job keeps the account on the pool


# ----- the pool's deadline ------------------------------------------------------------------------------------

@pytest.fixture
def slow_pool_app(app, make_app):
    return make_app(workers_enabled=1, worker_claim_timeout=2, first_token_timeout=10)


def test_a_worker_that_took_over_a_job_late_is_not_blamed_for_the_pools_deadline(slow_pool_app, make_user,
                                                                                 fake_ollama):
    (first_id, first_token), (second_id, second_token) = register(slow_pool_app, "A"), register(slow_pool_app, "B")
    first, second = Worker(slow_pool_app, first_token), Worker(slow_pool_app, second_token)
    first.heartbeat()
    second.heartbeat()
    user = make_user("carol")
    claims = []

    def work():
        job = first.poll().get_json()
        claims.append(("A", job["job_id"]))
        time.sleep(7)  # A loads the model for a while, then its owner starts a game: the job goes back
        first.heartbeat(activity_state="gaming")
        again = second.poll().get_json()
        claims.append(("B", again["job_id"]))
        # B's cold model load outlasts what is left of the pool's time; it never sends a chunk.

    thread, errors = run_in_thread(work)
    started = time.monotonic()
    events = generate(slow_pool_app, user)
    thread.join(15)
    assert not errors, errors
    assert [name for name, _ in claims] == ["A", "B"]
    assert text_of(events) == "Hello from the fake model."  # answered locally once the pool's time was up
    assert time.monotonic() - started < 20
    assert _worker_row(slow_pool_app, first_id)["strikes"] == 0
    assert _worker_row(slow_pool_app, second_id)["strikes"] == 0  # it held the job only a few seconds
    [row] = job_rows(slow_pool_app)
    assert row["status"] == "timeout" and row["fault"] is None


def test_a_worker_that_held_a_job_past_the_first_token_timeout_is_still_blamed(slow_pool_app, make_user,
                                                                              fake_ollama):
    worker_id, token = register(slow_pool_app, "A")
    worker = Worker(slow_pool_app, token)
    worker.heartbeat()
    user = make_user("dave")

    def work():
        worker.poll()  # claims, then stalls

    thread, errors = run_in_thread(work)
    events = generate(slow_pool_app, user)
    thread.join(10)
    assert not errors, errors
    assert text_of(events) == "Hello from the fake model."
    assert _worker_row(slow_pool_app, worker_id)["strikes"] == 1
    assert json.loads(json.dumps(job_rows(slow_pool_app)[0]))["fault"] == "worker"
