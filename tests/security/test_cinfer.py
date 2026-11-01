"""Regression tests for audit findings C2-1, C2-2, C2-4, C3-2, C3-4 and C3-8 (inference, limits, workers, pools)."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import timedelta

from tests.app.test_api import chat, ledger, roll_out, token_for
from tests.app.test_images import artist, comfy, image_app  # noqa: F401 - fixtures


def _wait(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _queue_rows(app):
    from bananachat import db

    with app.app_context():
        return db.scalar("SELECT COUNT(*) FROM inference_queue")


# ----- C2-1: a client that stops reading a stream ------------------------------------------------------------

def _sse_text(response) -> str:
    text = []
    for block in response.get_data(as_text=True).split("\n\n"):
        if block.startswith("data: {"):
            payload = json.loads(block[6:])
            for choice in payload.get("choices", []):
                text.append(choice["delta"].get("content") or "")
    return "".join(text)


def test_c2_1_streamed_answer_does_not_wait_for_the_reader(app, make_user, fake_ollama):
    """A streamed API answer finishes, is charged and frees its queue place although nobody reads the stream."""
    roll_out(app)
    user = make_user("staller")
    token = token_for(app, user)
    fake_ollama.reply = " ".join(["word"] * 300)
    response = app.test_client().post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, buffered=False,
        json={"model": "auto", "stream": True, "messages": [{"role": "user", "content": "Count"}]})
    try:
        assert response.status_code == 200
        # Nothing reads the body: before the fix the answer stopped at its first piece and kept its place.
        assert _wait(lambda: _queue_rows(app) == 0 and len(ledger(app, user)) == 1)
        rows = ledger(app, user)
        assert rows[0]["usage_estimated"] == 0 and rows[0]["tokens_out"] > 0
        # A reader that comes back late still gets the whole answer.
        assert _sse_text(response) == fake_ollama.reply
    finally:
        response.close()
    assert len(ledger(app, user)) == 1


def test_c2_1_playground_answer_does_not_wait_for_the_reader(app, make_user, fake_ollama):
    roll_out(app)
    make_user("player", password="player-password")
    from tests.app.fixtures import Browser

    browser = Browser(app)
    browser.login("player", "player-password")
    fake_ollama.reply = " ".join(["word"] * 300)
    response = browser.post_json("/developer/playground/send", {"model": "auto", "messages": [
        {"role": "user", "content": "Count"}]}, buffered=False)
    try:
        assert response.status_code == 200
        assert _wait(lambda: _queue_rows(app) == 0)
    finally:
        response.close()


def test_c2_1_streamed_image_does_not_wait_for_the_reader(image_app, artist, comfy):
    """The Images page's stream: the image is made, charged and its queue place freed without a reader."""
    from tests.app.fixtures import Browser

    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()
    response = browser.client.post("/images/generate", json={"prompt": "A dog"}, buffered=False,
                                   headers={"X-CSRF-Token": "test-csrf-token", "X-Requested-With": "fetch",
                                            "Accept": "text/event-stream"})
    try:
        assert response.status_code == 200
        assert _wait(lambda: _queue_rows(image_app) == 0 and len(ledger(image_app, artist)) == 1)
        assert '"type":"done"' in response.get_data(as_text=True)
    finally:
        response.close()


def test_c2_1_a_write_to_a_client_that_stopped_reading_times_out():
    """The server thread of a stream nobody reads is freed after BC_WRITE_TIMEOUT."""
    from bananachat.write_timeout import guard

    def endless(_environ, start_response):
        start_response("200 OK", [("Content-Type", "text/event-stream")])
        return (b"data: " + b"x" * 4096 + b"\n\n" for _ in range(100_000))

    server, client = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
    outcome = []

    def write():
        body = guard(endless, 0.3)({"gunicorn.socket": server}, lambda *_args: None)
        try:
            for piece in body:
                server.sendall(piece)
            outcome.append("finished")
        except OSError as error:
            outcome.append(error)
        finally:
            body.close()

    thread = threading.Thread(target=write, daemon=True)
    try:
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "still blocked writing to a client that does not read"
        assert isinstance(outcome[0], TimeoutError)
    finally:
        server.close()
        client.close()


def test_c2_1_write_timeout_is_configurable():
    from bananachat.config import load_config

    assert load_config({"BC_SECRET_KEY": "x" * 40}, load_secret=False).write_timeout == 120
    assert load_config({"BC_SECRET_KEY": "x" * 40, "BC_WRITE_TIMEOUT": "0"}, load_secret=False).write_timeout == 0


# ----- C2-2: concurrent API answers and the token allowance ----------------------------------------------------

def _api_window(app, tokens):
    from bananachat.db import limits as limits_db

    with app.app_context():
        policy = limits_db.get_policy("api")
        policy["window"]["tokens"] = tokens
        policy["window"]["enabled"] = True
        limits_db.set_policy("api", policy, None)


def test_c2_2_a_running_answer_holds_the_allowance_it_may_spend(app, make_user, fake_ollama):
    """A second answer admitted while the first runs must not spend the same remaining tokens."""
    roll_out(app)
    user = make_user("racer")
    token = token_for(app, user)
    _api_window(app, 300)
    fake_ollama.reply = " ".join(["w"] * 200)
    fake_ollama.chunk_delay = 0.003
    first = app.test_client().post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, buffered=False,
        json={"model": "auto", "stream": True, "messages": [{"role": "user", "content": "Count"}]})
    try:
        assert first.status_code == 200
        second = chat(app, token)  # the first answer is still running and holds what is left
        assert second.status_code == 429 and second.json["error"]["code"] == "insufficient_quota"
        first.get_data()
    finally:
        first.close()
    rows = ledger(app, user)
    assert len(rows) == 1  # only the first answer ran
    from bananachat import db

    with app.app_context():  # the hold was replaced by the charge
        assert db.scalar("SELECT COUNT(*) FROM image_credit_reservations") == 0
    assert chat(app, token).status_code == 200  # what the first answer did not spend is available again


def test_c2_2_the_hold_is_given_back_when_the_answer_ends(app, make_user):
    roll_out(app)
    user = make_user("holder")
    token = token_for(app, user)
    _api_window(app, 1_000_000)
    for _ in range(3):
        assert chat(app, token).status_code == 200
    from bananachat import db

    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM image_credit_reservations") == 0
    assert len(ledger(app, user)) == 3


# ----- C2-4: community grants of unlimited use -------------------------------------------------------------------

def test_c2_4_three_friends_cannot_grant_unlimited_use(app, make_user):
    """A temporary unlimited grant would lift every token limit while supporters renounce nothing."""
    from bananachat import db
    from bananachat.db import credits, settings
    from bananachat.services import community, limits

    with app.app_context():
        settings.update(community_min_account_days=0)
    asker, ann, bob, cid = (make_user(name) for name in ("asker", "ann", "bob", "cid"))
    with app.test_request_context():
        outcome = credits.submit_request(asker["id"], None, 0, "need it for my project", kind="temporary",
                                         pool="api", hours=720, unlimited=True, community=True)
        assert outcome["status"] == "pending"
        row = db.one("SELECT * FROM quota_requests WHERE id=?", (outcome["id"],))
        assert row["community"] == 0  # it waits for an administrator only
        # A request put to the vote by an earlier release cannot be approved by votes either.
        db.execute("UPDATE quota_requests SET community=1, community_until=? WHERE id=?",
                   (db.now(timedelta(hours=72)), outcome["id"]))
        assert community.count_open() == 0
        for person in (ann, bob, cid):
            try:
                community.vote(person, outcome["id"], "support", 0)
            except community.VoteError:
                pass
        row = db.one("SELECT * FROM quota_requests WHERE id=?", (outcome["id"],))
        assert row["status"] == "pending" and row["grant_id"] is None
        with limits.snapshot(fresh=True):
            assert not limits.effective(asker, "api").unlimited


# ----- C3-8: one bad request must not rest every subscription account ---------------------------------------------

CLAUDE_MODEL = {"name": "claude-test-sonnet", "family": "sonnet", "reasoning": ["low", "medium", "high"]}


def _claude_pool(tmp_path, make_app, monkeypatch, scenario=None):
    """Three pooled Claude accounts behind the real adapter and the repository's fake CLI."""
    import sys
    from types import ModuleType

    from bananachat import security
    from bananachat.db import catalog, users
    from bananachat.db import subscription_pool as pool_db
    from bananachat.services import claude_code, subscription_pool
    from tests.app.test_claude_code import FAKE, _delta, _terminal

    profiles = {}
    for name in ("acct-a", "acct-b", "acct-c"):
        home = tmp_path / name
        (home / "config").mkdir(parents=True)
        home.chmod(0o700)
        (home / "config").chmod(0o700)
        (home / "scenario.json").write_text(json.dumps(scenario or {"records": [_delta("Hello"), _terminal()]}))
        profiles[name] = {"home": str(home), "config_dir": str(home / "config"), "usage_source": "local",
                          "models": [CLAUDE_MODEL]}
    binary = tmp_path / "claude"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    manifest = tmp_path / "connector.json"
    manifest.write_text(json.dumps({"binary": str(binary), "profiles": profiles}))
    manifest.chmod(0o600)
    module = ModuleType("bc_cinfer_claude")

    def create_adapter(config):
        adapter = claude_code.Adapter(config)
        adapter.quota = lambda: {"window_left": 1.0, "weekly_left": None}
        adapter.check_profile = lambda name, cancel=None: {"subscription_type": "max"}
        adapter._require_unique_identity = lambda name, cancel=None: None
        return adapter

    module.create_adapter = create_adapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = make_app(CLAUDE_EXTENSION=module.__name__, CLAUDE_CODE_CONFIG=str(manifest))
    assert subscription_pool.connected("claude")
    with app.app_context():
        user = users.get(users.create("pooler", security.hash_password("pooler-password")))
        ids = [pool_db.add_account(name, provider="claude", window_limit=1_000_000) for name in profiles]
        subscription_pool.sync_catalog("claude", selected=[CLAUDE_MODEL["name"]], source="test")
        model = catalog.get_by_name(CLAUDE_MODEL["name"])
        catalog.set_rollout(model["id"], True)
        catalog.set_lifecycle(model["id"], enrollment="reviewed")
    return app, user, ids


def _rested(app, ids):
    from bananachat.db import subscription_pool as pool_db

    with app.app_context():
        return [row["label"] for row in (pool_db.get(account) for account in ids) if row["cooldown_until"]]


def _claude_chat(app, token, content):
    body = ('{"model": "%s", "messages": [{"role": "user", "content": "%s"}]}'
            % (CLAUDE_MODEL["name"], content)).encode()
    from tests.app.test_api import call

    return call(app, "POST", "/v1/chat/completions", token, data=body, headers={"Content-Type": "application/json"})


def test_c3_8_lone_surrogate_is_refused_and_rests_no_account(tmp_path, make_app, monkeypatch):
    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch)
    token = token_for(app, user)
    bad = _claude_chat(app, token, "hi \\ud800 there")  # the JSON escape of a lone high surrogate
    assert bad.status_code == 400 and bad.json["error"]["code"] == "invalid_text"
    assert _rested(app, ids) == []
    assert _claude_chat(app, token, "hello").status_code == 200


def test_c3_8_adapters_reject_text_they_cannot_encode(tmp_path, make_app, monkeypatch):
    """Defence in depth: the pool is given the bad text anyway (another caller): no account is rested."""
    import pytest

    from bananachat.services import subscription_pool

    app, _user, ids = _claude_pool(tmp_path, make_app, monkeypatch)
    with app.app_context(), pytest.raises(RuntimeError, match="not valid Unicode"):
        list(subscription_pool.chat_stream("claude", CLAUDE_MODEL["name"],
                                           [{"role": "user", "content": "hi \ud800"}]))
    assert _rested(app, ids) == []


def test_c3_8_failures_before_the_cli_starts_rest_no_account(tmp_path, make_app, monkeypatch):
    from bananachat.services import claude_code

    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch)

    def broken(*_args, **_kwargs):
        raise OSError("no space left for a temporary directory")

    monkeypatch.setattr(claude_code.tempfile, "TemporaryDirectory", broken)
    assert _claude_chat(app, token_for(app, user), "hello").status_code >= 500
    assert _rested(app, ids) == []


def test_c3_8_a_timeout_rests_at_most_one_account(tmp_path, make_app, monkeypatch):
    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch, scenario={"hang": True})
    app.extensions["claude_adapter"].timeout = 0.2
    assert _claude_chat(app, token_for(app, user), "a very long story").status_code >= 500
    assert len(_rested(app, ids)) == 1


# ----- C3-2: usage a worker reports ---------------------------------------------------------------------------

def _worker_app(make_app):
    from bananachat.services import remote

    remote._cooldown.clear()
    return make_app(workers_enabled=1, worker_claim_timeout=5)


def test_c3_2_worker_cannot_charge_thousands_of_times_the_prompt(make_app, make_user):
    import base64

    from bananachat.db import catalog
    from bananachat.services import inference, ollama, remote
    from bananachat.services.upstream import CancelToken
    from tests.app.test_workers import MODEL, Worker, register, run_in_thread

    app = _worker_app(make_app)
    _worker_id, secret = register(app)
    worker = Worker(app, secret)
    worker.heartbeat()
    with app.app_context():
        from bananachat.db import users

        user = users.get(make_user("alice")["id"])
    image = base64.b64encode(b"\x89PNG" + b"\0" * 300_000).decode()  # a photo: its base64 is not its tokens

    def work():
        job = worker.poll().get_json()
        worker.chunk(job["job_id"], 0, "ok", done=True)
        worker.complete(job["job_id"], tokens_in=2**31 - 1, tokens_out=2**31 - 1)

    thread, errors = run_in_thread(work)
    try:
        with app.app_context():
            ollama.sync_catalog()
            request = inference.TextRequest(user=user, model=catalog.get_by_name(MODEL), options={"num_predict": 64},
                                            messages=[{"role": "user", "content": "What is this?",
                                                       "images": [image]}])
            estimate = inference.estimate_prompt(request.messages)
            events = list(inference.generate(request, CancelToken()))
    finally:
        thread.join(10)
        remote._cooldown.clear()
    assert not errors, errors
    finished = events[-1]
    assert isinstance(finished, inference.Finished) and finished.via_worker, events
    assert finished.prompt_tokens <= remote.prompt_token_cap(json.dumps(request.messages))
    # A small multiple of the server's own estimate (it was about 400,000 tokens for this 300 KB image).
    assert finished.prompt_tokens <= remote.PROMPT_ESTIMATE_FACTOR * estimate + remote.PROMPT_TOKEN_ALLOWANCE


# ----- C3-4: worker job polls and the threads kept for short requests ---------------------------------------------

def test_c3_4_job_polls_count_as_long_requests(make_app, monkeypatch):
    from bananachat.services import remote
    from tests.app.test_workers import register

    app = _worker_app(make_app)
    monkeypatch.setattr(remote, "LONG_POLL_SECONDS", 1.0)
    _worker_id, secret = register(app)
    headers = {"Authorization": f"Bearer {secret}"}
    from bananachat.security import _LongRequests

    state = app.extensions.setdefault("bananachat.long_requests", _LongRequests())
    results = []
    thread = threading.Thread(target=lambda: results.append(
        app.test_client().get("/worker/v1/jobs/poll", headers=headers).status_code), daemon=True)
    thread.start()
    try:
        assert _wait(lambda: remote._polls["active"] == 1, 2)
        assert state.active == 1  # a waiting poll holds one of the long-request places
    finally:
        thread.join(5)
    assert results == [204] and state.active == 0

    # With every long-request place taken, a poll does not wait (it would take a reserved thread).
    limit = max(2, app.config["BC"].http_threads - 4)
    with state.lock:
        state.active = limit
    try:
        started = time.monotonic()
        assert app.test_client().get("/worker/v1/jobs/poll", headers=headers).status_code == 204
        assert time.monotonic() - started < 0.5
    finally:
        with state.lock:
            state.active = 0
