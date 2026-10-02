"""Chat token consumption defaults, quota isolation, and administrator controls."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import pytest

from tests.app.conftest import Browser
from tests.app.test_api import chat as api_chat
from tests.app.test_api import token_for
from tests.app.test_chat import new_chat, parse_sse, send, setup_models, wait_idle


@pytest.fixture
def chat_account(app, make_user):
    from bananachat.db import catalog

    setup_models(app)
    user = make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    with app.app_context():
        models = {model["ollama_name"]: model for model in catalog.list_models()}
    return user, browser, models


def _policy(pool, *, tokens=16, rate=False):
    from bananachat.db import limits

    policy = limits.get_policy(pool)
    policy["rate"].update(enabled=rate, rules=[{"requests": 1, "per": "hour", "burst": 1}])
    policy["window"].update(enabled=True, tokens=tokens, slow_tokens=0)
    policy["weekly"].update(enabled=True, tokens=tokens)
    limits.set_policy(pool, policy, None)


def _model_policy(model, **values):
    from bananachat.db import limits

    limits.set_model_policy(model["id"], {**limits.get_model_policy(model), **values}, None)


def _only_model(model):
    """Keep refusal tests from silently selecting another allowed model."""
    from bananachat import db

    db.execute("UPDATE ai_models SET is_rolled_out=0 WHERE id<>?", (model["id"],))


def _cloud_model(model):
    """Use the fake transport with a non-local provider identity for quota decisions."""
    from bananachat import db
    from bananachat.db import catalog

    db.execute("UPDATE ai_models SET provider='anthropic' WHERE id=?", (model["id"],))
    return catalog.get(model["id"])


def _ledger(user_id):
    from bananachat import db

    return db.query("SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id", (user_id,))


def _completed(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    events = parse_sse(response.get_data(as_text=True))
    assert events[-1]["type"] == "done" and events[-1]["state"] == "completed", events
    assert (events[-1]["tokens_in"], events[-1]["tokens_out"]) == (11, 5)
    return events


@pytest.mark.parametrize("incognito", [False, True])
def test_local_chats_record_usage_without_consuming_limits_by_default(app, chat_account, incognito):
    """Even exhausted pool/model windows permit local normal and no-history chat."""
    from bananachat import db
    from bananachat.db import settings
    from bananachat.services import limits

    user, browser, models = chat_account
    model = models["llama3.2:3b"]
    with app.app_context():
        assert settings.get()["chat_local_token_consumption"] == 0
        assert settings.get()["chat_cloud_token_consumption"] == 1
        _policy("chat", tokens=0)
        _model_policy(model, enabled=True, window_tokens=0, weekly_tokens=0)
    session_id = new_chat(browser, incognito=incognito)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    with app.app_context():
        row, = _ledger(user["id"])
        assert (row["tokens_in"], row["tokens_out"], row["credits_used"], row["consumes_limits"]) == (11, 5, 0, 0)
        assert row["request_type"] == ("chat_incognito" if incognito else "chat")
        assert db.scalar("SELECT COUNT(*) FROM limit_windows WHERE user_id=?", (user["id"],)) == 0
        assert limits.effective(user, "chat").window.used == 0
        assert limits.model_limits(user, model).window.used == 0
        metric = db.one("SELECT * FROM request_metrics WHERE user_id=?", (user["id"],))
        assert metric["status"] == "ok" and (metric["tokens_in"], metric["tokens_out"]) == (11, 5)


def test_local_consumption_opt_in_enforces_weighted_pool_and_raw_model_limits(app, chat_account):
    from bananachat.db import settings
    from bananachat.services import limits

    user, browser, models = chat_account
    model = models["llama3.2:3b"]
    with app.app_context():
        settings.update(chat_local_token_consumption=1)
        _only_model(model)
        _policy("chat", tokens=48)
        _model_policy(model, enabled=True, weight=3, window_tokens=16, weekly_tokens=16)
    session_id = new_chat(browser)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    refused = send(browser, session_id, model=model["ollama_name"])
    assert refused.status_code == 429 and refused.json["error"]["code"] == "quota_exhausted"
    with app.app_context():
        row, = _ledger(user["id"])
        assert row["consumes_limits"] == 1 and row["credits_used"] == pytest.approx(0.048)
        assert limits.effective(user, "chat").window.used == 48
        assert limits.effective(user, "chat").weekly.used == 48
        assert limits.model_limits(user, model).window.used == 16
        assert limits.model_limits(user, model).weekly.used == 16


def test_enabling_consumption_never_counts_previously_unmetered_chat_tokens(app, chat_account):
    from bananachat import db
    from bananachat.db import settings
    from bananachat.services import limits

    user, browser, models = chat_account
    model = models["llama3.2:3b"]
    with app.app_context():
        _only_model(model)
        _policy("chat")
        _model_policy(model, enabled=True, window_tokens=16, weekly_tokens=16)
    session_id = new_chat(browser)
    for _ in range(3):
        _completed(send(browser, session_id, model=model["ollama_name"]))
        wait_idle(app, session_id)
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM limit_windows WHERE user_id=?", (user["id"],)) == 0
        settings.update(chat_local_token_consumption=1)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    assert send(browser, session_id, model=model["ollama_name"]).status_code == 429
    with app.app_context():
        assert [row["consumes_limits"] for row in _ledger(user["id"])] == [0, 0, 0, 1]
        assert limits.effective(user, "chat").window.used == 16
        assert limits.effective(user, "chat").weekly.used == 16
        assert limits.model_limits(user, model).window.used == 16
        assert limits.model_limits(user, model).weekly.used == 16


def test_unmetered_chats_preserve_api_quota_and_work_after_shared_model_exhaustion(app, chat_account):
    from bananachat.services import limits

    user, browser, models = chat_account
    model = models["llama3.2:3b"]
    raw = token_for(app, user)
    with app.app_context():
        _only_model(model)
        _policy("api")
        _policy("chat", tokens=0)
        _model_policy(model, enabled=True, window_tokens=16, weekly_tokens=16)
    session_id = new_chat(browser)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    first_api = api_chat(app, raw, model=model["ollama_name"])
    assert first_api.status_code == 200 and first_api.json["usage"]["total_tokens"] == 16
    second_api = api_chat(app, raw, model=model["ollama_name"])
    assert second_api.status_code == 429 and second_api.json["error"]["code"] == "insufficient_quota"
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    with app.app_context():
        assert [(row["request_type"], row["consumes_limits"]) for row in _ledger(user["id"])] == [
            ("chat", 0), ("api", 1), ("chat", 0)]
        assert limits.effective(user, "api").window.used == 16
        assert limits.model_limits(user, model).window.used == 16


def test_cloud_chats_consume_own_limits_by_default_without_changing_pool_membership(app, chat_account):
    from bananachat.services import limits

    user, browser, models = chat_account
    with app.app_context():
        model = _cloud_model(models["qwen3:4b"])
        _only_model(model)
        _policy("chat", tokens=0)
        _model_policy(model, enabled=True, window_tokens=16, weekly_tokens=16)
        assert not limits.counts_toward_pool(model)
    session_id = new_chat(browser)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    refused = send(browser, session_id, model=model["ollama_name"])
    assert refused.status_code == 429 and refused.json["error"]["code"] == "quota_exhausted"
    with app.app_context():
        row, = _ledger(user["id"])
        assert row["consumes_limits"] == 1 and row["credits_used"] == 0
        assert limits.model_limits(user, model).window.used == 16
        assert limits.effective(user, "chat").window.used == 0


@pytest.mark.parametrize("incognito", [False, True])
def test_cloud_consumption_can_be_disabled_without_losing_usage_records(app, chat_account, incognito):
    from bananachat import db
    from bananachat.db import settings

    user, browser, models = chat_account
    with app.app_context():
        model = _cloud_model(models["qwen3:4b"])
        _only_model(model)
        _policy("chat", tokens=0)
        _model_policy(model, enabled=True, counts_toward_pool=True, window_tokens=0, weekly_tokens=0)
        settings.update(chat_cloud_token_consumption=0)
    session_id = new_chat(browser, incognito=incognito)
    for _ in range(2):
        _completed(send(browser, session_id, model=model["ollama_name"]))
        wait_idle(app, session_id)
    with app.app_context():
        rows = _ledger(user["id"])
        assert len(rows) == 2
        assert all((row["tokens_in"], row["tokens_out"], row["credits_used"], row["consumes_limits"])
                   == (11, 5, 0, 0) for row in rows)
        assert db.scalar("SELECT COUNT(*) FROM limit_windows WHERE user_id=?", (user["id"],)) == 0


@pytest.mark.parametrize("rate_scope", ["pool", "model"])
def test_unmetered_local_chat_keeps_request_rates_and_model_locks(app, chat_account, rate_scope):
    from bananachat.db import limits

    user, browser, models = chat_account
    model = models["llama3.2:3b"]
    with app.app_context():
        _only_model(model)
        _policy("chat", tokens=0, rate=rate_scope == "pool")
        _model_policy(model, enabled=True, window_tokens=0,
                      rate_rules=[{"requests": 1, "per": "hour"}] if rate_scope == "model" else [])
    session_id = new_chat(browser)
    _completed(send(browser, session_id, model=model["ollama_name"]))
    wait_idle(app, session_id)
    refused = send(browser, session_id, model=model["ollama_name"])
    assert refused.status_code == 429 and refused.json["error"]["code"] == "rate_limited"
    with app.app_context():
        _policy("chat", tokens=0)
        limits.set_model_override(user["id"], model["id"], None, locked=True)
    locked = send(browser, session_id, model=model["ollama_name"])
    assert locked.status_code == 403 and locked.json["error"]["code"] == "model_locked"


def test_disabling_cloud_consumption_keeps_provider_capacity_rates_and_locks(app, chat_account):
    from bananachat.db import credits, settings
    from bananachat.db import limits as store
    from bananachat.services import limits

    user, _browser, models = chat_account
    with app.app_context():
        model = _cloud_model(models["qwen3:4b"])
        _model_policy(model, enabled=True, window_tokens=16, weekly_tokens=16,
                      rate_rules=[{"requests": 1, "per": "hour"}])
        credits.charge(user["id"], 11, 5, request_type="chat", model_id=model["id"])
        assert not limits.admit(user, "chat", model, take_rate=False).allowed
        settings.update(chat_cloud_token_consumption=0)
        limits.register_capacity_provider("anthropic", lambda _model: limits.Capacity(window_left=0))
        try:
            refused = limits.admit(user, "chat", model)
            assert not refused.allowed and refused.refusal.status == 429
            limits.register_capacity_provider("anthropic", lambda _model: limits.Capacity(window_left=1))
            assert limits.admit(user, "chat", model).allowed
            rate_limited = limits.admit(user, "chat", model)
            assert not rate_limited.allowed and rate_limited.refusal.key == "model_rate"
            store.set_model_override(user["id"], model["id"], None, locked=True)
            locked = limits.admit(user, "chat", model)
            assert not locked.allowed and locked.refusal.code == "model_locked"
        finally:
            limits.unregister_capacity_provider("anthropic")


@pytest.mark.parametrize("first_is_cloud", [False, True])
def test_fallback_usage_is_metered_for_the_model_that_actually_answered(app, chat_account, fake_ollama,
                                                                     first_is_cloud):
    from bananachat.db import settings

    user, browser, models = chat_account
    with app.app_context():
        cloud = _cloud_model(models["qwen3:4b"])
        local = models["llama3.2:3b"]
        settings.update(quota_fallback_to_local=1, quota_fallback_to_cloud=1)
        _policy("chat")
    first, fallback = (cloud, local) if first_is_cloud else (local, cloud)
    fake_ollama.fail_models = {first["ollama_name"]}
    session_id = new_chat(browser)
    events = _completed(send(browser, session_id, model=first["ollama_name"]))
    wait_idle(app, session_id)
    assert events[-1]["model"] == fallback["ollama_name"]
    assert [body["model"] for body in fake_ollama.chat_bodies()] == [first["ollama_name"], fallback["ollama_name"]]
    with app.app_context():
        row, = _ledger(user["id"])
        assert row["model_id"] == fallback["id"]
        assert row["consumes_limits"] == int(not first_is_cloud)
        assert (row["tokens_in"], row["tokens_out"]) == (11, 5)


@pytest.mark.parametrize("allow_local", [False, True])
def test_cloud_pool_exhaustion_obeys_cloud_to_local_fallback_direction(app, chat_account, fake_ollama,
                                                                    allow_local):
    """An exhausted cloud pool can switch to free local chat only in the allowed direction."""
    from bananachat.db import settings

    user, browser, models = chat_account
    with app.app_context():
        cloud = _cloud_model(models["qwen3:4b"])
        local = models["llama3.2:3b"]
        _policy("chat", tokens=0)
        _model_policy(cloud, counts_toward_pool=True)
        settings.update(quota_fallback_to_local=int(allow_local), quota_fallback_to_cloud=0)
    session_id = new_chat(browser)
    response = send(browser, session_id, model=cloud["ollama_name"])
    if not allow_local:
        assert response.status_code == 429 and response.json["error"]["code"] == "quota_exhausted"
        assert fake_ollama.chat_bodies() == []
        with app.app_context():
            assert _ledger(user["id"]) == []
        return
    events = _completed(response)
    wait_idle(app, session_id)
    assert events[-1]["model"] == local["ollama_name"]
    assert [body["model"] for body in fake_ollama.chat_bodies()] == [local["ollama_name"]]
    with app.app_context():
        row, = _ledger(user["id"])
        assert row["model_id"] == local["id"] and row["consumes_limits"] == 0


@pytest.mark.parametrize("identity", ["missing", "unknown", "deleted"])
def test_unknown_or_deleted_model_usage_remains_metered_at_normal_weight(app, chat_account, identity):
    from bananachat import db
    from bananachat.db import credits

    user, _browser, models = chat_account
    with app.app_context():
        _policy("chat")
        model_id = None
        if identity == "unknown":
            model_id = 999_999
        elif identity == "deleted":
            model_id = models["llama3.2:3b"]["id"]
            db.execute("DELETE FROM ai_models WHERE id=?", (model_id,))
        counted, slow = credits.charge(user["id"], 11, 5, request_type="chat", model_id=model_id)
        assert counted == 16 and not slow
        row, = _ledger(user["id"])
        assert row["model_id"] is None and row["consumes_limits"] == 1
        assert row["credits_used"] == pytest.approx(0.016)


def test_chat_consumption_controls_are_admin_only_and_require_csrf(app, admin, chat_account):
    from bananachat.db import settings

    _user, browser, _models = chat_account
    url = "/admin/quotas/chat-consumption"
    values = {"chat_local_token_consumption": "1"}
    anonymous = Browser(app).post(url, values)
    assert anonymous.status_code == 302 and "/login" in anonymous.headers["Location"]
    assert browser.post(url, values).status_code == 403
    assert admin.client.post(url, data=values).status_code == 400
    assert admin.client.post(url, data={**values, "csrf_token": "wrong"}).status_code == 400
    with app.app_context():
        stored = settings.get()
        assert (stored["chat_local_token_consumption"], stored["chat_cloud_token_consumption"]) == (0, 1)


def test_admin_chat_consumption_controls_persist_independently_and_are_audited(app, admin):
    from bananachat import db
    from bananachat.db import settings

    def checked(name):
        html = admin.get("/admin/quotas").get_data(as_text=True)
        field = re.search(rf'<input\b(?=[^>]*\bname="{name}")[^>]*>', html)
        assert field is not None
        return re.search(r"\bchecked\b", field.group()) is not None

    assert not checked("chat_local_token_consumption") and checked("chat_cloud_token_consumption")
    for form, expected in [({"chat_local_token_consumption": "1"}, (1, 0)),
                           ({"chat_cloud_token_consumption": "1"}, (0, 1))]:
        response = admin.post("/admin/quotas/chat-consumption", form)
        assert response.status_code == 302 and response.headers["Location"].endswith("#chat-consumption")
        with app.app_context():
            stored = settings.get()
            assert (stored["chat_local_token_consumption"], stored["chat_cloud_token_consumption"]) == expected
        assert (checked("chat_local_token_consumption"), checked("chat_cloud_token_consumption")) == expected
    with app.app_context():
        audits = db.query("SELECT actor_name FROM audit_log WHERE action='admin.chat_token_consumption'")
        assert [row["actor_name"] for row in audits] == ["admin", "admin"]


def test_v15_upgrade_preserves_preexisting_usage_as_metered(tmp_path):
    from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS
    from sqlite_migrations import apply_migrations

    with closing(sqlite3.connect(tmp_path / "v14.db")) as connection:
        apply_migrations(connection, APPLICATION_ID, MIGRATIONS[:14])
        connection.execute("INSERT INTO users (id, username, password) VALUES ('old-user', 'old-user', 'test hash')")
        connection.execute("UPDATE site_settings SET site_name='Existing installation' WHERE id=1")
        for request_type, tokens, credits in [("chat", 100, 0.1), ("chat_incognito", 80, 0.24), ("api", 30, 0.03)]:
            connection.execute("INSERT INTO credit_ledger (user_id, request_type, tokens_in, tokens_out, "
                               "credits_used) VALUES ('old-user', ?, ?, 5, ?)", (request_type, tokens, credits))
        connection.commit()
        before = connection.execute("SELECT id, request_type, tokens_in, tokens_out, credits_used FROM credit_ledger "
                                    "ORDER BY id").fetchall()
        apply_migrations(connection, APPLICATION_ID, MIGRATIONS)
        after = connection.execute("SELECT id, request_type, tokens_in, tokens_out, credits_used FROM credit_ledger "
                                   "ORDER BY id").fetchall()
        assert after == before
        assert connection.execute("SELECT consumes_limits FROM credit_ledger ORDER BY id").fetchall() == [(1,)] * 3
        assert connection.execute("SELECT site_name, chat_local_token_consumption, chat_cloud_token_consumption "
                                  "FROM site_settings WHERE id=1").fetchone() == ("Existing installation", 0, 1)
        apply_migrations(connection, APPLICATION_ID, MIGRATIONS)
        assert connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone() == (3,)
