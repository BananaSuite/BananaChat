"""Chat search under load (full re-audit pass 7, MC2-1): a search has no index to use, so each one is stopped
after a few seconds and each process runs only a few at a time, an account one at a time."""
from __future__ import annotations

import threading
import time

import pytest

from tests.app.conftest import Browser
from tests.app.test_chat import setup_models, user_browser

NEAR_MISS = "a" * 199 + "b"  # compared again at every position of a run of "a"


@pytest.fixture
def alice(app, make_user):
    setup_models(app)
    return user_browser(app, make_user)


def _seed(app, username: str, megabytes: int) -> tuple[str, str]:
    from bananachat.db import chats, users

    with app.app_context():
        user_id = users.get_by_username(username)["id"]
        session_id = chats.create(user_id)
        chats.add_message(session_id, "user", "Notes about bananas", user_id=user_id, incognito=False)
        for _ in range(megabytes * 10):
            chats.add_message(session_id, "user", "a" * 100_000, user_id=user_id, incognito=False)
        return user_id, session_id


def test_mc2_1_a_crafted_search_is_stopped_at_the_deadline(app, alice, monkeypatch):
    from bananachat.db import chats

    _seed(app, "alice", 8)  # about 1 s for the crafted phrase without a deadline
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 0.1)
    started = time.monotonic()
    response = alice.fetch(f"/chat/search?q={NEAR_MISS}")
    elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.json["error"]["code"] == "search_too_slow"
    assert elapsed < 0.6
    # The deadline ends with the search: the next ones on the same connection run as usual.
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 5)
    results = alice.fetch("/chat/search?q=bananas").json["results"]
    assert [item["snippet"] for item in results] == ["Notes about bananas"]
    assert alice.fetch("/chat/search?q=zz").json["results"] == []
    assert not chats._searching  # its place was given back


def test_mc2_1_searches_take_one_of_a_few_places(app, alice, monkeypatch):
    from bananachat.db import chats

    monkeypatch.setattr(chats, "SEARCHES", 2)
    alice_id, _ = _seed(app, "alice", 0)
    # The account's previous search is told to stop; when it does not stop in time, the page is told to ask
    # again (its own code and message, not the generic rate limit).
    monkeypatch.setattr(chats, "REPLACE_SECONDS", 0.1)
    with chats._search_place(alice_id) as stop:
        response = alice.fetch("/chat/search?q=bananas")
        assert stop.is_set()
        assert response.status_code == 429
        assert response.json["error"]["code"] == "search_running"
        assert response.json["error"]["retry_after"] == 2
        assert response.headers["Retry-After"] == "2"
    # Every place of the process is taken by other accounts: busy, at once.
    with chats._search_place("other-1"), chats._search_place("other-2"):
        started = time.monotonic()
        response = alice.fetch("/chat/search?q=bananas")
        assert response.status_code == 503
        assert response.json["error"]["code"] == "server_busy"
        assert time.monotonic() - started < 0.5
    assert len(alice.fetch("/chat/search?q=bananas").json["results"]) == 1
    assert not chats._searching


def test_mc2_1_a_newer_search_of_the_account_replaces_the_running_one(app, alice, monkeypatch):
    """The sidebar searches as you type: the newest query must get its results, not a refusal."""
    from bananachat.db import chats

    alice_id, _ = _seed(app, "alice", 8)  # about 1 s for the crafted phrase
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 10)
    typing = Browser(app)
    typing.login("alice")
    first = {}
    slow = threading.Thread(target=lambda: first.setdefault("response", alice.fetch(f"/chat/search?q={NEAR_MISS}")))
    slow.start()
    try:
        for _ in range(200):
            if alice_id in chats._searching:
                break
            time.sleep(0.005)
        assert alice_id in chats._searching
        started = time.monotonic()
        response = typing.fetch("/chat/search?q=bananas")
        elapsed = time.monotonic() - started
    finally:
        slow.join()
    assert response.status_code == 200
    assert [item["snippet"] for item in response.json["results"]] == ["Notes about bananas"]
    assert elapsed < 0.5
    assert first["response"].status_code == 409
    assert first["response"].json["error"]["code"] == "search_replaced"
    assert not chats._searching and not chats._newest


def test_mc2_1_only_the_newest_waiting_search_takes_the_place(app, alice, monkeypatch):
    from bananachat.db import chats

    monkeypatch.setattr(chats, "REPLACE_SECONDS", 2)
    outcomes = {}

    def wait(name):
        try:
            with chats._search_place("bob"):
                outcomes[name] = "searched"
        except chats.SearchReplaced:
            outcomes[name] = "replaced"

    with chats._search_place("bob") as stop:
        second = threading.Thread(target=wait, args=("second",))
        second.start()
        for _ in range(200):
            if "bob" in chats._newest:
                break
            time.sleep(0.005)
        third = threading.Thread(target=wait, args=("third",))
        third.start()
        second.join(1)
        assert outcomes == {"second": "replaced"}  # gave up for the newer one, without waiting it out
        assert stop.is_set()
    third.join(1)
    assert outcomes == {"second": "replaced", "third": "searched"}
    assert not chats._searching and not chats._newest


def test_mc2_1_an_account_has_a_few_seconds_of_search_a_minute(app, alice, make_user, monkeypatch):
    """Two accounts sending the crafted phrase back to back must not keep every search place taken."""
    from bananachat.db import chats, users

    alice_id, _ = _seed(app, "alice", 8)  # about 1 s for the crafted phrase
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 0.3)
    monkeypatch.setattr(chats, "SEARCH_BUDGET_SECONDS", 0.5)
    assert alice.fetch(f"/chat/search?q={NEAR_MISS}").json["error"]["code"] == "search_too_slow"
    # The rest of the budget bounds the next search, then the account waits for its minute.
    started = time.monotonic()
    response = alice.fetch(f"/chat/search?q={NEAR_MISS}")
    assert response.status_code == 429
    assert response.json["error"]["code"] == "search_budget"
    assert time.monotonic() - started < 0.4
    started = time.monotonic()
    response = alice.fetch("/chat/search?q=bananas")
    assert response.json["error"]["code"] == "search_budget"
    assert response.headers["Retry-After"]
    assert time.monotonic() - started < 0.1  # refused without searching
    assert not chats._searching
    with app.app_context():
        # The first search ran 0.3-0.4 s or more (4 events or more), the second the rest (1 event or more).
        assert users.count_hits("chat-search-time:user:" + alice_id, 60) >= 5
    # Other accounts search as usual.
    bob = user_browser(app, make_user, "bob")
    assert bob.fetch("/chat/search?q=bananas").json["results"] == []


def test_mc2_1_ordinary_searches_stay_within_the_budget(app, alice):
    """At the view's 60 a minute, quick searches (charged a tenth of a second each) never reach the budget."""
    from bananachat.db import chats, users

    alice_id, _ = _seed(app, "alice", 0)
    for _ in range(60):
        assert len(alice.fetch("/chat/search?q=bananas").json["results"]) == 1
    with app.app_context():
        spent = users.count_hits("chat-search-time:user:" + alice_id, 60) * chats._BUDGET_STEP
    assert spent < chats.SEARCH_BUDGET_SECONDS
    assert alice.fetch("/chat/search?q=bananas").json["error"]["code"] == "rate_limited"  # the view's count


def test_mc2_1_the_last_tenth_of_the_budget_can_be_spent(app, alice):
    """The budget is counted in whole events: 10 - 99 * 0.1 is just under 0.1 in floating point."""
    from bananachat.db import chats, users

    alice_id, _ = _seed(app, "alice", 0)
    key = "chat-search-time:user:" + alice_id
    with app.app_context():
        users.record_hits(key, round(chats.SEARCH_BUDGET_SECONDS / chats._BUDGET_STEP) - 1)
    assert len(alice.fetch("/chat/search?q=bananas").json["results"]) == 1
    assert alice.fetch("/chat/search?q=bananas").json["error"]["code"] == "search_budget"


def test_mc2_1_a_search_stops_within_a_message_or_two_of_long_answers(app, alice, monkeypatch):
    """Answers are stored up to chat_max_response_bytes (8 MiB at most), and each is one LIKE that cannot be
    stopped halfway: the stop must be checked every message or two, not every dozen."""
    from bananachat.db import chats

    alice_id, session_id = _seed(app, "alice", 0)
    with app.app_context():
        for _ in range(20):
            chats.add_message(session_id, "assistant", "a" * 256 * 1024, user_id=alice_id, incognito=False)
    monkeypatch.setattr(chats, "SEARCH_BUDGET_SECONDS", 1000)
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 30)
    started = time.monotonic()
    assert alice.fetch(f"/chat/search?q={NEAR_MISS}").json["results"] == []
    whole = time.monotonic() - started  # all 20 compared
    monkeypatch.setattr(chats, "SEARCH_SECONDS", 0.001)
    started = time.monotonic()
    assert alice.fetch(f"/chat/search?q={NEAR_MISS}").json["error"]["code"] == "search_too_slow"
    assert time.monotonic() - started < whole / 4
