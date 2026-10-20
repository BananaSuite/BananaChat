"""Chat runs keep their accounting when their chat is erased, and leave nothing behind when a worker exits."""

from __future__ import annotations

import time

import pytest

from tests.app.test_chat import new_chat, read_events, send, setup_models, user_browser, wait_idle


def _ledger(app, username):
    from bananachat import db
    from bananachat.db import users

    with app.app_context():
        user = users.get_by_username(username)
        return [row.to_dict() for row in db.query(
            "SELECT tokens_in, tokens_out, request_type, usage_estimated FROM credit_ledger WHERE user_id=?",
            (user["id"],))]


def _wait_ledger(app, username, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _ledger(app, username)
        if rows:
            return rows
        time.sleep(0.05)
    return []


def _wait_no_lease(app, timeout=10.0):
    from bananachat import db

    deadline = time.monotonic() + timeout
    with app.app_context():
        while time.monotonic() < deadline:
            if not db.scalar("SELECT COUNT(*) FROM active_streams", default=0):
                return
            time.sleep(0.05)
    raise AssertionError("the run kept its lease")


def _streaming(app, make_user, fake_ollama, username, *, incognito):
    """A chat whose answer is streaming; returns the browser, the chat and the open response."""
    setup_models(app)
    browser = user_browser(app, make_user, username)
    fake_ollama.reply = " ".join(["word"] * 400)
    fake_ollama.chunk_delay = 0.01
    session_id = new_chat(browser, incognito=incognito)
    response = send(browser, session_id, "Write a long text", buffered=False)
    received = []
    read_events(response, until=lambda event: event["type"] == "delta" and (
        received.append(event["text"]) or sum(len(text) for text in received) > 600))
    return browser, session_id, response


@pytest.mark.parametrize("incognito", [True, False])
def test_erasing_a_chat_while_it_answers_still_charges_what_was_generated(make_app, make_user, fake_ollama,
                                                                          incognito):
    app = make_app(BC_DELETED_CHAT_RETENTION_DAYS=0)
    browser, session_id, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=incognito)
    assert browser.post(f"/chat/{session_id}/delete", {}, headers={"X-Requested-With": "fetch"}).status_code == 200
    read_events(response)
    _wait_no_lease(app)
    rows = _wait_ledger(app, "mallory")
    assert len(rows) == 1, rows
    assert rows[0]["tokens_out"] >= 600 // 4 and rows[0]["tokens_in"] > 0
    assert rows[0]["request_type"] == ("chat_incognito" if incognito else "chat")
    from bananachat import db

    with app.app_context():
        # The chat itself is gone at once, and nothing of the answer was stored for it.
        assert db.scalar("SELECT COUNT(*) FROM chat_sessions WHERE id=?", (session_id,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE session_id=?", (session_id,)) == 0
        assert db.scalar("SELECT status FROM request_metrics WHERE request_type=?",
                         ("chat_incognito" if incognito else "chat",)) == "stopped"


def test_erasing_all_chats_while_one_answers_still_charges_it(make_app, make_user, fake_ollama):
    from bananachat import db
    from bananachat.db import chats, users

    app = make_app(BC_DELETED_CHAT_RETENTION_DAYS=0)
    _, session_id, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=True)
    with app.app_context():
        # "Delete all my chats" (account page) reaches the database layer directly.
        assert chats.delete_all_for_user(users.get_by_username("mallory")["id"], hard=True) == 1
        assert db.scalar("SELECT COUNT(*) FROM chat_sessions WHERE id=?", (session_id,)) == 0
    read_events(response)
    _wait_no_lease(app)
    rows = _wait_ledger(app, "mallory")
    assert len(rows) == 1 and rows[0]["tokens_out"] >= 600 // 4 and rows[0]["request_type"] == "chat_incognito"


def test_a_chat_erased_by_another_process_stops_its_run_and_charges_it(app, make_user, fake_ollama):
    """The delete request may reach a worker process other than the one generating."""
    from bananachat.db import chats

    _, session_id, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=True)
    with app.app_context():
        chats.hard_delete(session_id)  # no local cancel: only the shared database says so
    events = read_events(response)
    assert events[-1]["type"] == "done" and events[-1]["state"] == "stopped", events[-1]
    _wait_no_lease(app)
    rows = _wait_ledger(app, "mallory")
    assert len(rows) == 1 and rows[0]["tokens_out"] >= 600 // 4


def test_a_run_whose_process_died_after_its_chat_was_erased_is_charged_by_recovery(app, make_user, fake_ollama):
    from bananachat import db
    from bananachat.db import chats, runs, users

    setup_models(app)
    make_user("mallory")
    with app.app_context():
        user = users.get_by_username("mallory")
        session_id = chats.create(user["id"], incognito=True)
        session = chats.get(session_id)
        begun = runs.begin(session, user, content="x" * 400, attachments=[], title=None, one_per_user=True)
        model = db.one("SELECT id FROM ai_models LIMIT 1")
        assert runs.mark_running(session_id, begun.token, model["id"])
        assert runs.checkpoint(session_id, begun.token, "y" * 800, model["id"], tokens_in=150, tokens_out=40)
        chats.hard_delete(session_id)
        # The process generating died: its lease is no longer renewed.
        db.execute("UPDATE active_streams SET heartbeat_at=0 WHERE session_id=?", (session_id,))
        assert runs.recover_stale() == 1
        assert db.scalar("SELECT COUNT(*) FROM active_streams", default=0) == 0
    rows = _ledger(app, "mallory")
    assert len(rows) == 1, rows
    # The checkpoint's prompt count; at least the estimate of the saved partial answer.
    assert rows[0]["tokens_out"] == 200 and rows[0]["tokens_in"] == 150
    assert rows[0]["request_type"] == "chat_incognito" and rows[0]["usage_estimated"] == 1


def test_a_run_whose_chat_was_erased_still_counts_as_the_accounts_one_run(app, make_user):
    """Until the stopping run (or recovery) has charged it, the account cannot start a second run."""
    from bananachat.db import chats, runs

    user = make_user("mallory")
    with app.app_context():
        erased = chats.create(user["id"], incognito=True)
        other = chats.create(user["id"])
        runs.begin(chats.get(erased), user, content="Hi", attachments=[], title=None, one_per_user=True)
        chats.hard_delete(erased)  # the lease stays until the run has charged what it generated
        assert runs.user_busy(user["id"])
        with pytest.raises(runs.Busy) as refused:
            runs.begin(chats.get(other), user, content="Hi", attachments=[], title=None, one_per_user=True)
        assert refused.value.scope == "user"
        assert not runs.user_busy(make_user("bob")["id"])


def test_soft_deleting_a_chat_while_it_answers_still_charges_it(app, make_user, fake_ollama):
    browser, session_id, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=False)
    assert browser.post(f"/chat/{session_id}/delete", {}, headers={"X-Requested-With": "fetch"}).status_code == 200
    read_events(response)
    wait_idle(app, session_id)
    rows = _wait_ledger(app, "mallory")
    assert len(rows) == 1 and rows[0]["tokens_out"] >= 600 // 4 and rows[0]["request_type"] == "chat"


def test_deleting_the_account_while_it_answers_stops_the_run(app, make_user, fake_ollama):
    """The deletion may reach a worker process other than the one generating: the database tells it."""
    from bananachat import db
    from bananachat.db import users

    _, session_id, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=False)
    with app.app_context():
        user_id = users.get_by_username("mallory")["id"]
        users.delete(user_id)
    events = read_events(response)
    assert events[-1]["type"] == "done" and events[-1]["state"] == "stopped", events[-1]
    _wait_no_lease(app)
    with app.app_context():
        # Nothing is stored or charged for an account that no longer exists.
        assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE session_id=?", (session_id,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user_id,)) == 0


def test_deleting_the_account_ends_the_leases_of_its_agent_tasks(app, make_user):
    """An agent run checks its lease every few seconds; the account's tasks go with it, so the run stops."""
    from bananachat.db import agents as agents_db
    from bananachat.db import users

    user = make_user("mallory")
    with app.app_context():
        agents_db.create("a" * 32, user_id=user["id"], title="Task", prompt="Do it", model_id=None,
                         model_name="m", swarm=False, owner_token="lease", max_user=None, max_site=10)
        assert agents_db.lease_check("a" * 32, "lease", renew=True) is None
        users.delete(user["id"])
        assert agents_db.lease_check("a" * 32, "lease", renew=True) == "lease lost"


# ----- a worker process exits (gunicorn max_requests recycling, reload, shutdown) --------------------------------

def _detached_run(app, make_user, fake_ollama, *, words=400, delay=0.01):
    """A chat answering after its reader went away; returns the chat id."""
    setup_models(app)
    browser = user_browser(app, make_user, "alice")
    fake_ollama.reply = " ".join(["word"] * words)
    fake_ollama.chunk_delay = delay
    session_id = new_chat(browser)
    response = send(browser, session_id, "Write a long text", buffered=False)
    read_events(response, until=lambda event: event["type"] == "delta")
    response.close()  # the browser tab closed: the answer keeps going
    return session_id


def _rows(app, sql, *params):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query(sql, params)]


def test_a_worker_exiting_lets_detached_answers_finish_within_its_grace_time(app, make_user, fake_ollama):
    from bananachat.services import chat

    session_id = _detached_run(app, make_user, fake_ollama, words=60, delay=0.01)
    chat.shutdown(grace=20, save_timeout=5)
    saved = _rows(app, "SELECT content, generation_state FROM chat_messages WHERE session_id=? AND role='assistant'",
                  session_id)
    assert [row["generation_state"] for row in saved] == ["completed"]
    assert _rows(app, "SELECT * FROM inference_queue") == [] and _rows(app, "SELECT * FROM active_streams") == []


def test_a_worker_exiting_saves_and_charges_detached_answers_and_frees_their_places(app, make_user, fake_ollama):
    from bananachat.services import chat

    session_id = _detached_run(app, make_user, fake_ollama, words=2000, delay=0.01)
    time.sleep(0.3)
    started = time.monotonic()
    chat.shutdown(grace=0, save_timeout=10)
    assert time.monotonic() - started < 5
    saved = _rows(app, "SELECT content, generation_state FROM chat_messages WHERE session_id=? AND role='assistant'",
                  session_id)
    assert len(saved) == 1 and saved[0]["generation_state"] == "interrupted" and saved[0]["content"]
    state = _rows(app, "SELECT generation_state, generation_error FROM chat_sessions WHERE id=?", session_id)[0]
    assert state["generation_state"] == "interrupted"
    assert state["generation_error"] == "The server stopped while answering; this is the part saved so far."
    ledger = _rows(app, "SELECT tokens_out FROM credit_ledger")
    assert len(ledger) == 1 and ledger[0]["tokens_out"] > 0
    assert _rows(app, "SELECT * FROM inference_queue") == [] and _rows(app, "SELECT * FROM active_streams") == []


def test_answers_that_cannot_be_saved_in_time_are_handed_to_recovery_at_once(app, make_user, fake_ollama, monkeypatch):
    import os

    from bananachat.db import runs
    from bananachat.services import chat

    session_id = _detached_run(app, make_user, fake_ollama, words=2000, delay=0.01)
    time.sleep(2.5)  # past the first checkpoint
    # The run thread cannot save (storage stalls, a provider ignores the cancellation...).
    monkeypatch.setattr(chat.Run, "_end", lambda self, *args, **kwargs: time.sleep(30))
    chat.shutdown(grace=0, save_timeout=0.5)
    assert _rows(app, "SELECT * FROM inference_queue WHERE owner_pid=?", os.getpid()) == []
    lease = _rows(app, "SELECT heartbeat_at FROM active_streams WHERE session_id=?", session_id)
    assert lease and lease[0]["heartbeat_at"] == 0
    time.sleep(1.5)  # the supervisor no longer renews it
    with app.app_context():
        assert runs.recover_stale() == 1
    saved = _rows(app, "SELECT generation_state FROM chat_messages WHERE session_id=? AND role='assistant'",
                  session_id)
    assert [row["generation_state"] for row in saved] == ["interrupted"]
    assert len(_rows(app, "SELECT * FROM credit_ledger")) == 1


def _gunicorn_settings():
    import runpy
    from pathlib import Path

    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "gunicorn.conf.py"))


def test_gunicorn_hands_answers_over_when_a_worker_exits(monkeypatch):
    from types import SimpleNamespace

    from bananachat.services import background, chat, completions

    pytest.importorskip("gunicorn")
    settings = _gunicorn_settings()
    calls = []
    monkeypatch.setattr(chat, "shutdown", lambda **kwargs: calls.append(("chat", kwargs["grace"])))
    monkeypatch.setattr(completions, "shutdown", lambda **kwargs: calls.append(("api", kwargs["grace"])))
    monkeypatch.setattr(chat, "release_queue_places", lambda: calls.append("places"))
    monkeypatch.setattr(background, "stop", lambda: calls.append("background"))
    cfg = SimpleNamespace(timeout=settings["timeout"], graceful_timeout=settings["graceful_timeout"],
                          max_requests=settings["max_requests"])

    def exit_worker(nr, **overrides):
        calls.clear()
        settings["worker_exit"](None, SimpleNamespace(nr=nr, max_requests=5000, cfg=SimpleNamespace(
            **{**vars(cfg), **overrides})))
        # The queue places are freed once chat runs and API answers were both handed over.
        assert calls[-2:] == ["places", "background"]
        graces = dict(call for call in calls if call not in ("places", "background"))
        assert set(graces) == {"chat", "api"} and graces["chat"] == graces["api"]  # API answers too
        return graces["chat"]

    # Replaced after max_requests: answers get time to finish, within the master's patience.
    grace = exit_worker(5000)
    assert 0 < grace and grace + 10 < settings["timeout"] - settings["graceful_timeout"]
    # The effective settings count: a shorter --timeout leaves no time to wait.
    assert exit_worker(5000, timeout=70) == 0
    # Stopped or reloaded: the master waits only graceful_timeout, so answers are saved at once.
    assert exit_worker(10) == 0


def test_cheap_requests_cannot_force_a_young_worker_to_be_replaced():
    from types import SimpleNamespace

    pytest.importorskip("gunicorn")
    settings = _gunicorn_settings()
    log = SimpleNamespace(debug=lambda *args: None)
    request = SimpleNamespace(method="GET", path="/healthz")
    worker = SimpleNamespace(nr=4999, max_requests=5000, bc_started=time.monotonic(), log=log,
                             cfg=SimpleNamespace(max_requests=5000))
    settings["pre_request"](worker, request)
    assert worker.max_requests > worker.nr + 1  # the next request does not end this young worker
    old = SimpleNamespace(nr=4999, max_requests=5000, log=log, cfg=SimpleNamespace(max_requests=5000),
                          bc_started=time.monotonic() - settings["_MIN_WORKER_SECONDS"] - 1)
    settings["pre_request"](old, request)
    assert old.max_requests == 5000  # an older worker is replaced as configured


def test_a_stopping_worker_hands_answers_over_before_the_master_kills_it(monkeypatch):
    import signal
    from types import SimpleNamespace

    pytest.importorskip("gunicorn")
    settings = _gunicorn_settings()
    hand_overs, exits, timers = [], [], []

    class Timer:
        def __init__(self, delay, function, args=()):
            self.delay, self.function, self.args, self.daemon = delay, function, args, False
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(settings["threading"], "Timer", Timer)
    monkeypatch.setitem(settings["_hand_over"].__globals__, "_hand_over", lambda grace: hand_overs.append(grace))
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, frame: exits.append(signum))
    try:
        worker = SimpleNamespace(cfg=SimpleNamespace(graceful_timeout=60))
        settings["_hand_over_before_the_master_kills"](worker)
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)  # a second signal starts no second hand-over
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert exits == [signal.SIGTERM, signal.SIGTERM]  # gunicorn's own handling still runs
    [timer] = timers
    assert timer.daemon and 0 < timer.delay <= 60 - 10  # saved before the master's SIGKILL at graceful_timeout
    timer.function(*timer.args)
    assert hand_overs == [0.0]


def test_the_master_lets_the_queue_places_of_a_worker_that_died_expire_at_once(app):
    from types import SimpleNamespace

    from bananachat import db
    from bananachat.services import queue

    pytest.importorskip("gunicorn")
    settings = _gunicorn_settings()
    with app.app_context():
        now = time.time()
        for request_id, pid in (("dead", 999_999_001), ("alive", 999_999_002)):
            db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, heartbeat_at) "
                       "VALUES (?,?,'running',?,?,?)", (request_id, 3, pid, now, now))
    log = SimpleNamespace(warning=lambda *args, **kwargs: None)
    settings["child_exit"](SimpleNamespace(log=log), SimpleNamespace(pid=999_999_001))
    with app.app_context():
        assert queue.stats()["running"] == 2  # a live process with that number would renew its row in time
    # Seconds later the dead worker's place stops counting, well before queue.LEASE_SECONDS.
    later = now + queue.ORPHAN_GRACE_SECONDS + 1
    counted = _rows(app, "SELECT req_id FROM inference_queue WHERE heartbeat_at>=?", later - queue.LEASE_SECONDS)
    assert [row["req_id"] for row in counted] == ["alive"]
