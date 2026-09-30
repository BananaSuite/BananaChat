"""Claude subscription pool: strict token limits, shared capacity, enrollment."""

import importlib

from bananachat import db
from bananachat.db import catalog, claude_pool as pool_db
from bananachat.db import limits as limits_db
from bananachat.services import claude_pool, limits


def _user(app, username="user1"):
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        with db.transaction():
            try:
                users.create(username, security.hash_password(f"{username}-password"), role="user")
            except ValueError:
                pass
        return users.get_by_username(username)


def test_strict_defaults_are_token_based_and_not_counted(app):
    opus = claude_pool.strict_policy("claude-opus-4-1")
    sonnet = claude_pool.strict_policy("claude-sonnet-4-5")
    haiku = claude_pool.strict_policy("claude-haiku-4-5")
    # Opus strictest, Haiku most generous; 5-hour windows, weekly off by default.
    assert opus["window_tokens"] < sonnet["window_tokens"] < haiku["window_tokens"]
    assert opus["weekly_tokens"] is None and sonnet["weekly_tokens"] is None
    assert opus["counts_toward_pool"] is False
    assert opus["enabled"] is True
    # Effort defaults: powerful models start low/medium only.
    assert opus["effort_default"] == "low"
    assert sonnet["effort_default"] == "medium"


def test_sync_enrolls_selected_models_with_strict_limits(app):
    with app.test_request_context():
        state = claude_pool.sync_catalog(selected=["claude-sonnet-4-5"], source="test")
        assert state["count"] == 1
        row = catalog.get_by_name("claude-sonnet-4-5")
        assert row is not None and row["backend"] == "claude" and row["provider"] == "claude"
        assert row["enrollment"] == "new"  # manual policy waits for review
        assert catalog.get_by_name("claude-opus-4-1") is None  # not selected


def test_pooled_capacity_shrinks_claude_model_limits(app):
    user = _user(app)
    with app.test_request_context():
        claude_pool.sync_catalog(selected=["claude-opus-4-1"], source="test")
        model = catalog.get_by_name("claude-opus-4-1")
        limits_db.set_model_policy(model["id"], claude_pool.strict_policy(model["ollama_name"]), None)
        # With no subscription account nothing can be served: the model counts as used up.
        claude_pool.ensure_registered()
        assert limits.model_limits(user, model).window.tokens == 0

        account_id = pool_db.add_account("claude-1", window_limit=100_000)
        before = limits.model_limits(user, model)
        assert before.window.limited and before.window.tokens > 0
        pool_db.report_quota(account_id, window_used=90_000, window_limit=100_000)
        # Less than half left (10 %): model limits shrink via capacity hook.
        after = limits.model_limits(user, catalog.get(model["id"]))
        assert after.capacity < 1.0
        assert after.window.tokens < before.window.tokens

        pool_db.remove_account(account_id)


def test_effort_unlock_cascade_and_auto(app):
    """Approving max allows high and below; the request flow already encodes this."""
    user = _user(app, "effort-user")
    with app.test_request_context():
        claude_pool.sync_catalog(selected=["claude-sonnet-4-5"], source="test")
        model = catalog.get_by_name("claude-sonnet-4-5")
        # Ceiling max covers every level at or below it.
        limits_db.set_effort_level(user["id"], model["id"], "max", source="admin", updated_by=None)
        allowed = limits.allowed_efforts(user, catalog.get(model["id"]))
        assert "high" in allowed and "medium" in allowed and "low" in allowed


def test_migration_v13_is_additive_and_rerunnable(app):
    v13 = importlib.import_module("bananachat.db.migrations.v13_claude")
    with app.test_request_context():
        connection = db.conn()
        v13.upgrade(connection)  # safe to run again
        row = catalog.get_by_name("claude-sonnet-4-5")  # data preserved
        assert row is None or row["backend"] == "claude"


# ----- pool accounting, failover and routing ------------------------------------------------------

def _stamp(**delta):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def test_pooled_quota_is_the_sum_of_accounts_and_expired_windows_are_empty(app):
    with app.test_request_context():
        first = pool_db.add_account("claude-a", window_limit=100_000)
        second = pool_db.add_account("claude-b", window_limit=100_000)
        pool_db.report_quota(first, window_used=100_000, window_resets_at=_stamp(hours=2))
        quota = claude_pool.refresh_quota()
        # One exhausted subscription removes only its share.
        assert quota["window_left"] == 0.5 and quota["usable"] == 1
        assert claude_pool.pick_account()["id"] == second
        # Its window ended: the pool counts it as empty again.
        pool_db.report_quota(first, window_resets_at=_stamp(hours=-1))
        assert claude_pool.refresh_quota()["window_left"] == 1.0
        # Resting accounts count as empty; all resting means nothing is left.
        claude_pool.rest(first, claude_pool._now() + claude_pool.COOLDOWN_ERROR)
        claude_pool.rest(second, claude_pool._now() + claude_pool.COOLDOWN_ERROR)
        assert claude_pool.refresh_quota()["window_left"] == 0.0
        assert claude_pool.pick_account() is None


def test_highest_priority_account_answers_first(app):
    with app.test_request_context():
        low = pool_db.add_account("low", priority=1)
        high = pool_db.add_account("high", priority=10)
        assert claude_pool.pick_account()["id"] == high
        pool_db.update_account(high, status="disabled")
        assert claude_pool.pick_account()["id"] == low


def test_an_exhausted_account_rests_and_the_next_one_answers(app, monkeypatch):
    calls = []

    def handler(account, model, messages, options):
        calls.append((account["label"], options.get("effort")))
        if account["label"] == "first":
            raise claude_pool.QuotaExhausted("usage limit reached")
        yield {"text": "Hello ", "tokens_in": 7}
        yield {"text": "world", "tokens_out": 3, "done": True}

    monkeypatch.setattr(claude_pool, "_site_chat", handler)
    with app.test_request_context():
        first = pool_db.add_account("first", priority=5)
        second = pool_db.add_account("second")
        chunks = list(claude_pool.stream_chunks("claude-sonnet-4-5", [{"role": "user", "content": "hi"}],
                                                effort="high"))
        assert "".join(chunk.content for chunk in chunks) == "Hello world"
        assert chunks[-1].done and (chunks[-1].prompt_tokens, chunks[-1].completion_tokens) == (7, 3)
        assert calls == [("first", "high"), ("second", "high")]
        assert claude_pool._resting(pool_db.get(first)) and "usage limit" in pool_db.get(first)["last_error"]
        assert pool_db.get(second)["window_used"] == 10
        # The next request skips the resting account.
        calls.clear()
        list(claude_pool.stream_chunks("claude-sonnet-4-5", [{"role": "user", "content": "hi"}]))
        assert [label for label, _ in calls] == ["second"]


def test_no_usable_account_is_an_upstream_error(app, monkeypatch):
    from bananachat.services.upstream import UpstreamError

    monkeypatch.setattr(claude_pool, "_site_chat", lambda *args: iter(()))
    with app.test_request_context():
        try:
            list(claude_pool.stream_chunks("claude-sonnet-4-5", []))
        except UpstreamError as error:
            assert "quota" in str(error)
        else:
            raise AssertionError("expected an upstream error")


def test_current_models_are_curated_with_family_strictness(app):
    names = {item["name"] for item in claude_pool.CURATED}
    assert {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"} <= names
    assert claude_pool.family_of("claude-fable-5-1") == "fable"
    fable, opus = claude_pool.strict_policy("claude-fable-5-1"), claude_pool.strict_policy("claude-opus-5-5")
    assert fable["window_tokens"] < opus["window_tokens"] and fable["weight"] > opus["weight"]
    # Thinking cannot be switched off on Fable 5.1 and Opus 5.5.
    opus_item = next(item for item in claude_pool.CURATED if item["name"] == "claude-opus-5-5")
    assert "off" not in opus_item["reasoning"]


def _publish_claude(app, name="claude-sonnet-4-5"):
    with app.test_request_context():
        claude_pool.sync_catalog(selected=[name], source="test")
        model = catalog.get_by_name(name)
        catalog.set_rollout(model["id"], True)
        limits_db.set_model_policy(model["id"], claude_pool.strict_policy(name), None)
        return catalog.get(model["id"])


def test_chat_with_a_claude_model_goes_through_the_pool(app, make_user, fake_ollama, monkeypatch):
    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish_claude(app)
    seen = []

    def handler(account, model, messages, options):
        seen.append((account["label"], model, messages[-1]["content"]))
        yield {"text": "From Claude", "tokens_in": 4, "tokens_out": 2, "done": True}

    monkeypatch.setattr(claude_pool, "_site_chat", handler)
    with app.app_context():
        pool_db.add_account("claude-1", window_limit=1_000_000)
    browser = user_browser(app, make_user)
    session_id = new_chat(browser)
    before = len(fake_ollama.chat_bodies())
    events = parse_sse(send(browser, session_id, "Hi Claude", model="claude-sonnet-4-5").get_data(as_text=True))
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "From Claude"
    assert events[-1]["state"] == "completed"
    assert seen == [("claude-1", "claude-sonnet-4-5", "Hi Claude")]
    assert len(fake_ollama.chat_bodies()) == before  # never sent to Ollama
    with app.app_context():
        assert pool_db.get_by_label("claude-1")["window_used"] == 6


def test_used_up_claude_model_falls_back_to_a_local_model(app, make_user, fake_ollama):
    from bananachat.db import settings

    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish_claude(app)  # no subscription account: nothing left in the pool
    browser = user_browser(app, make_user)
    session_id = new_chat(browser)
    events = parse_sse(send(browser, session_id, "Hello", model="claude-sonnet-4-5").get_data(as_text=True))
    start = next(event for event in events if event["type"] == "start")
    assert start["model"] != "claude-sonnet-4-5"
    assert events[-1]["state"] == "completed"
    assert "".join(event["text"] for event in events if event["type"] == "delta") == fake_ollama.reply

    # With cloud → local switched off, the request is refused instead.
    with app.app_context():
        settings.update(quota_fallback_to_local=0)
    response = send(browser, session_id, "Again", model="claude-sonnet-4-5")
    assert response.status_code == 429


def test_used_up_local_tokens_fall_back_to_a_cloud_model(app, make_user, fake_ollama, monkeypatch):
    from bananachat import db
    from bananachat.db import limits as limits_store
    from bananachat.db import settings

    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish_claude(app)
    monkeypatch.setattr(claude_pool, "_site_chat",
                        lambda *args: iter([{"text": "Cloud answer", "tokens_out": 2, "done": True}]))
    with app.app_context():
        pool_db.add_account("claude-1", window_limit=1_000_000)
        config = limits_store.get_policy("chat")
        config["window"].update(enabled=True, tokens=1000, slow_tokens=0)
        limits_store.set_policy("chat", config, None)
        settings.update(slow_credits_enabled=0)
        local = db.one("SELECT * FROM ai_models WHERE backend='ollama' ORDER BY sort_order LIMIT 1")
    browser = user_browser(app, make_user)
    with app.app_context():
        from bananachat.db import users
        user = users.get_by_username("alice")
        db.execute("INSERT INTO credit_ledger (user_id, model_id, credits_used, tokens_in, tokens_out, request_type, "
                   "created_at) VALUES (?,?,?,?,0,'chat',?)", (user["id"], local["id"], 5.0, 5000, db.now()))
        limits_store.open_windows(user["id"], [limits_store.pool_scope("chat")])
    session_id = new_chat(browser)
    events = parse_sse(send(browser, session_id, "Hello", model=local["ollama_name"]).get_data(as_text=True))
    start = next(event for event in events if event["type"] == "start")
    assert start["model"] == "claude-sonnet-4-5"
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "Cloud answer"
