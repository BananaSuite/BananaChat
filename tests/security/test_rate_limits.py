"""Rate limits that matter for abuse: one test per documented limit.

Limits already guarded in this directory are not repeated here:
* two-step codes per browser and per account: test_cauth.py (C1-1),
* sign-ins from known browsers per address: test_cauth.py (C1-2),
* the password/code confirmation budget: test_cauth.py (C1-3),
* concurrent API answers against the token allowance: test_cinfer.py (C2-2),
* worker job polls against the long-request cap: test_cinfer.py (C3-4),
* agent workspace uploads per hour: test_cworker.py (C3-5).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.app.fixtures import Browser
from tests.app.test_api import assert_error, call, chat, ledger, roll_out, token_for
from tests.app.test_chat import messages, new_chat, read_events, send, setup_models, user_browser, wait_idle
from tests.app.test_images import MODEL_ID, comfy, create_user, generate  # noqa: F401  (fixture)

FAST_HASH = {"BC_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000"}


def _from(address):
    return {"environ_base": {"REMOTE_ADDR": address}}


def _freeze_limiter(monkeypatch):
    """Stop the token-bucket clock, so a slow machine cannot refill a bucket between two calls."""
    from bananachat.services import limits

    frozen = time.time()
    monkeypatch.setattr(limits, "time", SimpleNamespace(time=lambda: frozen, monotonic=time.monotonic))


def _policy(pool, **sections):
    from bananachat.db import limits

    policy = limits.get_policy(pool)
    for scope, values in sections.items():
        policy[scope].update(values)
    limits.set_policy(pool, policy, None)


# ----- signing in and signing up -------------------------------------------------------------------------

def test_sign_in_attempts_are_limited_per_address(make_app):
    app = make_app(BC_LOGIN_ATTEMPTS_PER_MINUTE=3, **FAST_HASH)
    attacker = Browser(app)
    statuses = [attacker.post("/login", {"username": "admin", "password": "guess"}, **_from("203.0.113.7")).status_code
                for _ in range(4)]
    assert statuses == [401, 401, 401, 429]
    # The right password does not get through the limit either; another address is not affected.
    assert attacker.post("/login", {"username": "admin", "password": "admin-password"},
                         **_from("203.0.113.7")).status_code == 429
    assert Browser(app).post("/login", {"username": "admin", "password": "admin-password"},
                             **_from("198.51.100.1")).status_code == 302


def test_sign_in_failures_are_limited_per_address(make_app):
    app = make_app(BC_LOGIN_FAILURES_PER_ADDRESS=2, BC_LOGIN_FAILURES_PER_ACCOUNT=50, **FAST_HASH)
    browser = Browser(app)
    # Guessing across many accounts from one address.
    assert [browser.post("/login", {"username": f"u{n}", "password": "x"}).status_code for n in range(3)] == \
        [401, 401, 429]


def test_sign_in_failures_lock_the_account_from_every_address(make_app):
    app = make_app(BC_LOGIN_FAILURES_PER_ACCOUNT=3, BC_LOGIN_FAILURES_PER_ADDRESS=1000, **FAST_HASH)
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        users.create("bystander", security.hash_password("bystander-password"))
    for number in range(3):  # a botnet: every guess from a different address
        assert Browser(app).post("/login", {"username": "admin", "password": "guess"},
                                 **_from(f"203.0.113.{number}")).status_code == 401
    assert Browser(app).post("/login", {"username": "admin", "password": "admin-password"},
                             **_from("198.51.100.1")).status_code == 429
    assert Browser(app).post("/login", {"username": "bystander", "password": "bystander-password"},
                             **_from("198.51.100.1")).status_code == 302


def test_sign_ups_from_one_address_count_created_accounts_and_failures_apart(make_app):
    from bananachat.db import settings

    app = make_app(BC_SIGNUPS_PER_HOUR=3, BC_SIGNUP_FAILURES_PER_HOUR=2, **FAST_HASH)
    with app.app_context():
        settings.update(signup_mode="open")
    form = {"password": "long-password", "confirm_password": "long-password"}
    browser = Browser(app)
    # Mistakes (here a taken name) do not use up the accounts a class may create...
    for _ in range(2):
        assert browser.post("/signup", {"username": "admin", **form}).status_code == 409
    # ...but they have a limit of their own,
    assert browser.post("/signup", {"username": "admin", **form}).status_code == 429
    # and so do the accounts created (from another address: the one above is now refused altogether).
    statuses = [Browser(app).post("/signup", {"username": f"pupil{number}", **form},
                                  **_from("198.51.100.1")).status_code for number in range(4)]
    assert statuses == [302, 302, 302, 429]


# ----- the API ----------------------------------------------------------------------------------------------

@pytest.fixture
def api_user(app, make_user):
    roll_out(app)
    return make_user("dev")


def test_invalid_api_tokens_are_throttled_per_address(app, api_user):
    token = token_for(app, api_user)
    for _ in range(30):
        assert call(app, "GET", "/v1/models", "bc-wrong").status_code == 401
    response = call(app, "GET", "/v1/models", "bc-wrong")
    assert_error(response, 429, "rate_limit_error")
    assert response.headers["Retry-After"] == "60"
    assert call(app, "GET", "/v1/models", token).status_code == 200  # valid tokens are unaffected


def test_api_requests_are_limited_per_account_across_its_tokens(app, api_user, monkeypatch):
    _freeze_limiter(monkeypatch)
    with app.app_context():
        _policy("api", rate={"rules": [{"requests": 1, "per": "minute", "burst": 2}]})
    first, second = token_for(app, api_user, "first"), token_for(app, api_user, "second")
    assert call(app, "GET", "/v1/models", first).status_code == 200
    assert call(app, "GET", "/v1/models", second).status_code == 200
    response = call(app, "GET", "/v1/models", first)
    assert_error(response, 429, "rate_limit_error", "rate_limit_exceeded")
    assert response.headers["Retry-After"] == "60"
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


def test_an_api_account_without_tokens_left_is_refused_before_generation(app, api_user, fake_ollama):
    from bananachat.db import credits

    token = token_for(app, api_user)
    with app.app_context():
        credits.set_quota(api_user["id"], 0, 0, None)
    asked = len(fake_ollama.chat_bodies())
    response = chat(app, token)
    assert_error(response, 429, "rate_limit_error", "insufficient_quota")
    assert int(response.headers["Retry-After"]) > 0
    assert ledger(app, api_user) == [] and len(fake_ollama.chat_bodies()) == asked


def test_an_api_account_cannot_fill_the_queue(app, api_user):
    from bananachat import db

    token = token_for(app, api_user)
    with app.app_context():
        for index in range(3):
            db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, "
                       "heartbeat_at, owner_key) VALUES (?, 1, 'waiting', 1, strftime('%s','now'), "
                       "strftime('%s','now') + 1000, ?)", (f"r{index}", f"user:{api_user['id']}"))
    response = chat(app, token)
    assert_error(response, 429, "rate_limit_error", "too_many_requests")
    assert response.headers["Retry-After"]


# ----- chat sends and streaming ----------------------------------------------------------------------------

@pytest.fixture
def alice(app, make_user):
    setup_models(app)
    return user_browser(app, make_user)


def test_chat_sends_follow_the_default_burst_then_one_a_second(app, alice, monkeypatch):
    _freeze_limiter(monkeypatch)
    session_id = new_chat(alice)
    for _ in range(5):
        assert send(alice, session_id).status_code == 200
        wait_idle(app, session_id)
    stored = len(messages(app, session_id))
    limited = send(alice, session_id)
    assert limited.status_code == 429 and limited.json["error"]["code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "1"
    assert len(messages(app, session_id)) == stored  # a refused message is not saved


def test_chat_tokens_are_checked_before_uploads_are_parsed(app, alice, monkeypatch):
    from bananachat.db import settings
    from bananachat.services import attachments

    with app.app_context():
        settings.update(chat_local_token_consumption=1)
        _policy("chat", window={"enabled": True, "tokens": 0, "slow_tokens": 0})
    monkeypatch.setattr(attachments, "process", lambda *args, **kwargs: pytest.fail("the upload was parsed"))
    session_id = new_chat(alice)
    refused = send(alice, session_id, files=[("broken.pdf", b"not a pdf")])
    assert refused.status_code == 429 and refused.json["error"]["code"] == "quota_exhausted"
    assert messages(app, session_id) == []


def test_one_chat_answer_at_a_time_per_account(app, alice, fake_ollama):
    from bananachat.db import chats, users

    fake_ollama.reply = " ".join(["slow"] * 200)
    fake_ollama.chunk_delay = 0.02
    first = new_chat(alice)
    response = send(alice, first, buffered=False)
    read_events(response, until=lambda event: event["type"] == "delta")
    with app.app_context():
        other = chats.create(users.get_by_username("alice")["id"])
    for target in (first, other):
        refused = send(alice, target, "Again")
        assert refused.status_code == 409
        assert refused.json["error"]["code"] == "busy" and refused.headers["Retry-After"]
    alice.fetch(f"/chat/{first}/stop", method="POST")
    read_events(response)
    assert messages(app, other) == []


# ----- image generation, exports, uploads ------------------------------------------------------------------

def test_image_generation_is_limited_per_account_and_minute(make_app, comfy):  # noqa: F811
    from bananachat.db import catalog
    from bananachat.services import comfyui

    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05",
                   IMAGE_GENERATION_RPM="1")
    with app.app_context():
        comfyui.sync_models()
        catalog.set_rollout(catalog.get_by_name(MODEL_ID)["id"], True)
    token = token_for(app, create_user(app, "quick"))
    assert generate(app, token).status_code == 200
    response = generate(app, token)
    assert_error(response, 429, "rate_limit_error", "rate_limit_exceeded")
    assert response.headers["Retry-After"] == "60"
    assert len(comfy.prompts) == 1


def test_data_exports_are_limited_per_account(app, make_user, monkeypatch):
    from bananachat.web import account

    monkeypatch.setattr(account, "EXPORTS_PER_HOUR", 2)
    alice = user_browser(app, make_user)
    bob = user_browser(app, make_user, "bob")
    # Both kinds of export share one budget.
    assert alice.post("/account/export/gdpr").status_code == 200
    assert alice.post("/account/export/chats").status_code == 200
    limited = alice.post("/account/export/gdpr")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "600"
    assert bob.post("/account/export/gdpr").status_code == 200


def test_background_uploads_are_limited_per_account(app, make_user):
    alice = user_browser(app, make_user)
    for _ in range(12):  # counted before the upload is read, so even empty ones count
        assert alice.fetch("/api/preferences/background", method="POST").status_code == 400
    limited = alice.fetch("/api/preferences/background", method="POST")
    assert limited.status_code == 429 and limited.get_json()["error"]["code"] == "rate_limited"
    assert user_browser(app, make_user, "bob").fetch("/api/preferences/background", method="POST").status_code == 400
