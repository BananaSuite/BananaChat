"""API and playground answers record their usage while they stream: a replaced, stopped or killed server process
cannot leave them uncharged, and nothing is charged twice."""

from __future__ import annotations

import threading
import time

import pytest

from tests.app.test_chat import setup_models

WRITE = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "Write a long text"}]}


def _user(app, make_user, fake_ollama, *, words=2000, delay=0.01, username="mallory"):
    setup_models(app)
    fake_ollama.reply = " ".join(["word"] * words)
    fake_ollama.chunk_delay = delay
    return make_user(username)


def _rows(app, sql, *params):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query(sql, params)]


def _ledger(app):
    return _rows(app, "SELECT tokens_in, tokens_out, request_type, usage_estimated FROM credit_ledger")


def _metrics(app, request_type="api"):
    return _rows(app, "SELECT tokens_out, status FROM request_metrics WHERE request_type=?", request_type)


class Client(threading.Thread):
    """Reads one streamed completion in its own thread, as a request thread of the server does."""

    def __init__(self, app, user, request_type="api"):
        super().__init__(daemon=True)
        self.app, self.user, self.request_type = app, user, request_type
        self.deltas: list[str] = []
        self.errors: list = []
        self.finished = None
        self.run_object = None
        self.started_event = threading.Event()

    def run(self):
        from bananachat.services import completions, inference

        with self.app.test_request_context():
            try:
                params = completions.parse_chat_request(WRITE)
                self.run_object = completions.CompletionRun(self.user, params, request_type=self.request_type)
                for event in self.run_object.events():
                    if isinstance(event, inference.Delta):
                        self.deltas.append(event.text)
                        self.started_event.set()
                    elif isinstance(event, inference.Finished):
                        self.finished = event
            except completions.CompletionError as error:
                self.errors.append(error)
            finally:
                self.started_event.set()
                if self.run_object is not None:
                    self.run_object.close()

    def wait_for(self, characters: int, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while sum(len(text) for text in self.deltas) < characters:
            assert time.monotonic() < deadline, "the answer did not stream"
            time.sleep(0.02)


def test_an_api_answer_whose_process_died_is_charged_from_its_last_record(app, make_user, fake_ollama):
    from bananachat import db
    from bananachat.db import runs
    from bananachat.services import completions, inference, supervisor

    user = _user(app, make_user, fake_ollama)
    with app.test_request_context():
        run = completions.CompletionRun(user, completions.parse_chat_request(WRITE), request_type="api")
        produced = []
        events = run.events()  # kept: a dropped generator is closed, which would settle the answer
        for event in events:
            if isinstance(event, inference.Delta):
                produced.append(event.text)
                if sum(len(text) for text in produced) > 600:
                    break
        time.sleep(supervisor.LEASE_INTERVAL + 1.5)  # its usage was recorded at least once since
        [lease] = _rows(app, "SELECT * FROM completion_leases")
        assert lease["user_id"] == user["id"] and lease["request_type"] == "api" and lease["tokens_out"] >= 150
        # The process is killed: nothing settles the answer and nothing renews its lease any more.
        supervisor.unregister_watch(f"completion:{lease['id']}")
        db.execute("UPDATE completion_leases SET heartbeat_at=0 WHERE id=?", (lease["id"],))
        assert runs.recover_completions() == 1
        [charged] = _ledger(app)
        assert charged["request_type"] == "api" and charged["usage_estimated"] == 1
        assert charged["tokens_out"] >= 150 and charged["tokens_in"] > 0
        assert _metrics(app) == [{"tokens_out": charged["tokens_out"], "status": "stopped"}]
        assert _rows(app, "SELECT * FROM completion_leases") == []
        # The original process, had it only been paused, does not charge the answer a second time.
        run.close()
    assert len(_ledger(app)) == 1 and len(_metrics(app)) == 1


@pytest.mark.parametrize("request_type", ["api", "playground"])
def test_a_worker_exiting_charges_the_answers_it_was_streaming(app, make_user, fake_ollama, request_type):
    from bananachat.services import completions

    user = _user(app, make_user, fake_ollama)
    client = Client(app, user, request_type)
    client.start()
    client.wait_for(400)
    started = time.monotonic()
    completions.shutdown(grace=0, save_timeout=10)
    assert time.monotonic() - started < 5
    client.join(10)
    assert not client.is_alive()
    assert [error.code for error in client.errors] == ["server_restarting"]
    [charged] = _ledger(app)
    assert charged["request_type"] == request_type and charged["tokens_out"] >= 100
    assert [row["status"] for row in _metrics(app, request_type)] == ["stopped"]
    assert _rows(app, "SELECT * FROM completion_leases") == [] and _rows(app, "SELECT * FROM inference_queue") == []


def test_a_worker_exiting_lets_answers_finish_within_its_grace_time(app, make_user, fake_ollama):
    from bananachat.services import completions

    user = _user(app, make_user, fake_ollama, words=80)
    client = Client(app, user)
    client.start()
    client.wait_for(20)
    completions.shutdown(grace=20, save_timeout=5)
    client.join(10)
    assert not client.errors and client.finished is not None and client.finished.state == "completed"
    assert len(_ledger(app)) == 1 and [row["status"] for row in _metrics(app)] == ["ok"]
    assert _rows(app, "SELECT * FROM completion_leases") == []


def test_answers_finishing_while_a_worker_exits_keep_their_queue_places(app, make_user, fake_ollama):
    """The exiting process frees its inference queue places only once every answer was handed over: an API answer
    still finishing within the grace time keeps counting against the concurrency limit (also with no chat run)."""
    import runpy
    from pathlib import Path

    pytest.importorskip("gunicorn")
    settings = runpy.run_path(str(Path(__file__).resolve().parents[2] / "gunicorn.conf.py"))
    user = _user(app, make_user, fake_ollama, words=400, delay=0.01)
    client = Client(app, user)
    client.start()
    client.wait_for(50)
    hand_over = threading.Thread(target=settings["_hand_over"], args=(30.0,), daemon=True)
    hand_over.start()
    lost = False
    while client.is_alive() and not lost:
        places = _rows(app, "SELECT status FROM inference_queue")
        # The answer records its outcome before it leaves the queue: no place while it has none means it was freed
        # under the running answer.
        lost = not places and client.finished is None
        time.sleep(0.05)
    assert not lost
    hand_over.join(45)
    client.join(10)
    assert not hand_over.is_alive() and client.finished is not None and client.finished.state == "completed"
    assert _rows(app, "SELECT * FROM inference_queue") == [] and _rows(app, "SELECT * FROM completion_leases") == []


def test_an_answer_that_cannot_be_saved_in_time_is_handed_to_recovery_at_once(app, make_user, fake_ollama,
                                                                             monkeypatch):
    from bananachat.db import runs
    from bananachat.services import completions, supervisor

    user = _user(app, make_user, fake_ollama)
    release = threading.Event()
    client = Client(app, user)
    client.start()
    client.wait_for(400)
    time.sleep(supervisor.LEASE_INTERVAL + 1)  # past its first usage record
    # The request thread cannot save (storage stalls, a provider ignores the cancellation...).
    monkeypatch.setattr(completions.CompletionRun, "_settle", lambda self, finished: release.wait(30))
    try:
        completions.shutdown(grace=0, save_timeout=0.5)
        [lease] = _rows(app, "SELECT heartbeat_at FROM completion_leases")
        assert lease["heartbeat_at"] == 0
        time.sleep(1.5)  # nothing renews it any more
        assert _rows(app, "SELECT heartbeat_at FROM completion_leases") == [{"heartbeat_at": 0}]
        with app.app_context():
            assert runs.recover_completions() == 1
        [charged] = _ledger(app)
        assert charged["tokens_out"] > 0
    finally:
        release.set()
        client.join(10)


def test_an_answer_whose_record_was_recovered_elsewhere_stops_without_a_second_charge(app, make_user, fake_ollama):
    from bananachat import db
    from bananachat.db import runs
    from bananachat.services import supervisor

    user = _user(app, make_user, fake_ollama, delay=0.02)
    client = Client(app, user)
    client.start()
    client.wait_for(200)
    time.sleep(supervisor.LEASE_INTERVAL + 1)
    with app.app_context():
        # This process stalled past the lease: another one recovered (and charged) the answer meanwhile.
        db.execute("UPDATE completion_leases SET heartbeat_at=0")
        assert runs.recover_completions() == 1
    client.join(supervisor.LEASE_INTERVAL + 10)
    assert not client.is_alive() and client.errors and client.finished is None
    assert len(_ledger(app)) == 1 and len(_metrics(app)) == 1


def test_recovery_leaves_live_answers_alone(app, make_user, fake_ollama):
    from bananachat.db import runs

    user = _user(app, make_user, fake_ollama, words=400, delay=0.02)
    client = Client(app, user)
    client.start()
    client.wait_for(100)
    with app.app_context():
        assert runs.recover_completions() == 0
    client.join(30)
    assert client.finished is not None and client.finished.state == "completed"
    [charged] = _ledger(app)
    assert charged["usage_estimated"] == 0  # the model's own counts, charged once by the request itself


def test_nothing_is_recorded_for_an_answer_refused_before_it_runs(app, make_user, fake_ollama):
    from bananachat.services import completions

    user = _user(app, make_user, fake_ollama)
    with app.test_request_context():
        with pytest.raises(completions.CompletionError):
            completions.CompletionRun(user, completions.parse_chat_request({**WRITE, "model": "missing"}),
                                      request_type="api")
    assert _rows(app, "SELECT * FROM completion_leases") == []
