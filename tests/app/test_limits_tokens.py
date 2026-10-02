"""The token limit system: tokens, request-rate rules, 5-hour and rolling weekly windows, per-model limits and weights,
presets, reasoning-effort tiers, dynamic inputs, the migration from credits, and the pages that explain them."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from tests.app.conftest import TEST_CSRF, Browser
from tests.app.test_chat import new_chat, parse_sse, send, setup_models, wait_idle
from tests.app.test_foundation import legacy_instance

# ----- helpers ----------------------------------------------------------------------------------


def _signed_in(app, username, *, language="en"):
    from bananachat.db import users

    with app.app_context():
        account = users.get_by_username(username)
        prefs = users.get_preferences(account["id"])
        prefs["interface_language"] = language
        users.save_preferences(account["id"], prefs)
    browser = Browser(app)
    browser.login(username)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


def _policy(pool, **sections):
    """Update parts of a pool policy: ``_policy("api", window={"tokens": 5000})``."""
    from bananachat.db import limits

    config = limits.get_policy(pool)
    for scope, values in sections.items():
        config[scope].update(values)
    return limits.set_policy(pool, config, None)


def _models(app):
    """The fake catalog, published: ``{name: row}``."""
    from bananachat.db import catalog

    setup_models(app)
    with app.app_context():
        return {model["ollama_name"]: model for model in catalog.list_models()}


def _model_policy(model, **values):
    from bananachat.db import limits

    return limits.set_model_policy(model["id"], {**limits.get_model_policy(model), **values}, None)


def _ensure_column(table, name, definition):
    """Columns the model-lifecycle feature adds; tests that read them add them when absent."""
    from bananachat import db

    if name not in {row[1] for row in db.query(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _charge(user_id, tokens, *, model_id=None, request_type="api", at=None, weight=1.0):
    """A ledger row of *tokens* (counted × *weight*) at *at*, without opening windows."""
    from bananachat import db

    db.execute("INSERT INTO credit_ledger (user_id, model_id, credits_used, tokens_in, tokens_out, request_type, "
               "created_at) VALUES (?,?,?,?,0,?,?)",
               (user_id, model_id, tokens * weight / 1000, tokens, request_type, db.timestamp(at) if at else db.now()))


def _age_windows(user_id, hours):
    """Move every open window of an account *hours* into the past (as if time went by)."""
    from bananachat import db

    for row in db.query("SELECT scope, window_started_at, week_started_at FROM limit_windows WHERE user_id=?",
                        (user_id,)):
        shifted = [db.timestamp(db.parse_timestamp(value) - timedelta(hours=hours)) if value else None
                   for value in (row["window_started_at"], row["week_started_at"])]
        db.execute("UPDATE limit_windows SET window_started_at=?, week_started_at=? WHERE user_id=? AND scope=?",
                   (*shifted, user_id, row["scope"]))
    db.execute("UPDATE credit_ledger SET created_at=datetime(created_at, ?) WHERE user_id=?", (f"-{hours} hours",
                                                                                               user_id))


def _api_key(app, user_id):
    from bananachat.db import tokens

    with app.app_context():
        return tokens.create(user_id, "cli")[1]


def _complete(app, raw, **body):
    payload = {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Hi"}], **body}
    return app.test_client().post("/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {raw}"})


# ----- formatting ----------------------------------------------------------------------------------

def test_token_amounts_are_friendly_in_english_and_italian():
    from bananachat.formatting import compact, parse_amount, token_input, tokens_text

    assert [compact(value, "en") for value in (850, 45_250, 150_000, 1_200_000, 2_000_000_000)] == \
        ["850", "45.2k", "150k", "1.2M", "2B"]
    assert compact(45_250, "it") == "45,2k" and compact(1_500_000, "it") == "1,5M"
    assert tokens_text(1, "en") == "1 token" and tokens_text(30_000, "en") == "30k tokens"
    assert tokens_text(30_000, "it") == "30k token"
    for text, value in (("30000", 30_000), ("30,000", 30_000), ("30 000", 30_000), ("30.000", 30_000),
                        ("50k", 50_000), ("1.5M", 1_500_000), ("1,5M", 1_500_000), ("2,5", 2.5)):
        assert parse_amount(text) == value, text
    assert parse_amount("lots") is None and parse_amount("") is None
    assert [token_input(value) for value in (30_000, 1_500_000, 12_345, None)] == ["30k", "1.5M", "12345", ""]


def test_durations_cover_hours_for_the_token_headers():
    from bananachat.services.limits import duration

    assert duration(0) == "0s" and duration(90) == "1m30s"
    assert duration(5 * 3600 - 30) == "4h59m30s"


# ----- migration ----------------------------------------------------------------------------------

def test_rate_rules_from_per_second_rates():
    from bananachat.db.migrations.v9_limits_tokens import rule

    assert rule(1.0, 10) == {"requests": 1, "per": "second", "burst": 10}
    assert rule(0.3333, 3) == {"requests": 20, "per": "minute", "burst": 3}
    assert rule(0.05, 1) == {"requests": 3, "per": "minute", "burst": 1}
    assert rule(1 / 3600, 2) == {"requests": 1, "per": "hour", "burst": 2}
    assert rule(0.001, 3) == {"requests": 86, "per": "day", "burst": 3}


def test_the_upgrade_from_the_previous_release_converts_credits_to_tokens(tmp_path, make_app):
    from bananachat import db
    from bananachat.db import credits, limits

    app = make_app(setup=False, INSTANCE_DIR=str(legacy_instance(tmp_path)))
    with app.app_context():
        policies = limits.all_policies()
        assert policies["api"]["rate"]["rules"] == [{"requests": 1, "per": "second", "burst": 10}]
        assert policies["chat"]["rate"]["rules"] == [{"requests": 20, "per": "minute", "burst": 3}]
        # The daily amount becomes the same amount per 5 hours, in tokens.
        assert policies["api"]["window"]["tokens"] == 45_000
        assert policies["api"]["window"]["enabled"] and not policies["chat"]["window"]["enabled"]
        assert all(not policy["weekly"]["enabled"] for policy in policies.values())
        override = limits.get_override("4lttj861", "api")
        assert override.window_tokens == 50_000
        assert credits.get_quota("4lttj861") == (50_000, 0)
        assert [tier["min_tokens_30d"] for tier in limits.list_tiers()] == [0, 20_000, 200_000]
        # Credit columns are kept for older releases.
        assert db.scalar("SELECT daily_credits FROM user_limit_overrides WHERE user_id='4lttj861'") == 40
        assert db.scalar("SELECT effort_default_level FROM site_settings") == "medium"
        assert db.integrity_check() == "ok"


def test_version_9_converts_a_database_of_this_release_forward(tmp_path):
    from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS
    from sqlite_migrations import apply_migrations

    path = tmp_path / "v8.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=OFF")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:8])
        conn.execute("INSERT INTO users (id, username, password) VALUES ('u1', 'uma', 'x')")
        conn.execute("INSERT INTO ai_models (ollama_name, display_name) VALUES ('big:70b', 'Big')")
        conn.execute("UPDATE limit_policy SET config=? WHERE pool='api'", (json.dumps(
            {"rate": {"enabled": True, "per_second": 0.5, "burst": 4, "dynamic": True},
             "daily": {"enabled": True, "credits": 12, "slow_credits": 3, "dynamic": False, "auto_tiers": True},
             "weekly": {"enabled": True, "credits": 60, "dynamic": False, "auto_tiers": False}}),))
        conn.execute("INSERT INTO user_limit_overrides (user_id, pool, rate_per_second, rate_burst, daily_credits, "
                     "weekly_credits) VALUES ('u1', 'api', 2.0, 8, 25, 100)")
        conn.execute("INSERT INTO limit_grants (user_id, pool, scope, kind, amount, starts_at) VALUES "
                     "('u1', 'api', 'daily', 'extra', 5, '2026-01-01 00:00:00'), "
                     "('u1', 'api', 'rate', 'extra', 0.5, '2026-01-01 00:00:00'), "
                     "(NULL, NULL, NULL, 'multiplier', 2, '2026-01-01 00:00:00')")
        conn.execute("INSERT INTO quota_requests (user_id, kind, pool, new_credits, new_slow_credits, reason, "
                     "duration_type, status) VALUES ('u1', 'daily', 'api', 40, 5, 'more please', 'permanent', "
                     "'pending')")
        conn.execute("UPDATE site_settings SET quota_auto_approve_max_credits=20, music_bonus_fixed_credits=7")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        conn.row_factory = sqlite3.Row
        policy = json.loads(conn.execute("SELECT config FROM limit_policy WHERE pool='api'").fetchone()[0])
        assert policy["version"] == 3
        assert policy["rate"] == {"enabled": True, "rules": [{"requests": 30, "per": "minute", "burst": 4}],
                                  "dynamic": True}
        assert policy["window"] == {"enabled": True, "tokens": 15_000, "dynamic": False,
                                    "auto_tiers": True}
        assert policy["weekly"]["tokens"] == 60_000 and policy["weekly"]["enabled"]
        override = conn.execute("SELECT * FROM user_limit_overrides").fetchone()
        assert json.loads(override["rate_rules"]) == [{"requests": 2, "per": "second", "burst": 8}]
        assert (override["window_tokens"], override["weekly_tokens"]) == (28_000, 100_000)  # inherited 3k is active
        grants = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM limit_grants")}
        assert (grants[1]["scope"], grants[1]["amount"]) == ("window", 5000)
        assert (grants[2]["kind"], grants[2]["amount"]) == ("multiplier", 2.0)  # (0.5 + 0.5) / 0.5
        assert grants[3]["model_id"] is None and grants[3]["kind"] == "multiplier"
        request = conn.execute("SELECT * FROM quota_requests").fetchone()
        assert (request["kind"], request["new_tokens"], request["new_slow_tokens"]) == ("window", 45_000, 0)
        settings = conn.execute("SELECT * FROM site_settings").fetchone()
        assert settings["quota_auto_approve_max_tokens"] == 20_000 and settings["music_bonus_fixed_tokens"] == 22_000
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ----- policies and request-rate rules -------------------------------------------------------------

def test_policies_validate_rules_and_token_amounts(app):
    from bananachat.db import limits

    with app.app_context():
        good = limits.get_policy("api")
        for scope, key, value in (("rate", "rules", [{"requests": 1, "per": "fortnight"}]),
                                  ("rate", "rules", [{"requests": 0, "per": "second"}]),
                                  ("rate", "rules", [{"requests": 1, "per": "minute"}, {"requests": 5, "per": "minute"}]),
                                  ("rate", "rules", [{"requests": 1, "per": unit} for unit in
                                                     ("second", "minute", "hour", "day", "second")]),
                                  ("rate", "rules", []), ("window", "tokens", -1),
                                  ("weekly", "tokens", limits.TOKENS_MAX + 1), ("window", "tokens", 1.5)):
            bad = json.loads(json.dumps(good))
            bad[scope][key] = value
            with pytest.raises(ValueError):
                limits.set_policy("api", bad, None)
        saved = _policy("api", rate={"rules": [{"requests": 300, "per": "hour"}, {"requests": 2, "per": "second"}]})
        # Sorted by unit, and the burst defaults to the number of requests.
        assert saved["rate"]["rules"] == [{"requests": 2, "per": "second", "burst": 2},
                                          {"requests": 300, "per": "hour", "burst": 300}]
        # A rate switched off may keep no rules.
        assert _policy("api", rate={"enabled": False, "rules": []})["rate"]["enabled"] is False


def test_every_rule_must_pass_and_a_refusal_takes_nothing(app, make_user):
    from bananachat.services import limits

    user = make_user("rula")
    with app.app_context():
        _policy("api", rate={"rules": [{"requests": 2, "per": "second"}, {"requests": 3, "per": "hour"}]})
        decisions = [limits.check_rate(user, "api", now=1000.0 + index) for index in range(4)]
        assert [decision.allowed for decision in decisions] == [True, True, True, False]
        # The hour rule is the tight one: its bucket drives the headers and the wait.
        assert decisions[2].rule.per == "hour" and decisions[2].remaining == 0
        assert decisions[3].rule.per == "hour" and decisions[3].retry_after == 1200 - 3
        # The per-second bucket was not charged for the refused request: it refilled to its burst.
        from bananachat import db
        seconds = db.one("SELECT tokens FROM rate_buckets WHERE key LIKE 'api:%:2/second/2'")["tokens"]
        assert seconds == pytest.approx(2.0)
        assert "3 requests per hour" in limits.rate_text("en", limits.rate_limit(user, "api").rules)
        assert limits.rate_text("it", limits.rate_limit(user, "api").rules) == \
            "2 richieste al secondo e 3 richieste all'ora"


def test_the_account_shares_its_buckets_across_api_keys_and_the_playground(app, make_user):
    from bananachat.db import tokens

    user = make_user("shay")
    setup_models(app)
    with app.app_context():
        _policy("api", rate={"rules": [{"requests": 3, "per": "hour", "burst": 3}]})
        first, second = tokens.create(user["id"], "one")[1], tokens.create(user["id"], "two")[1]
    for raw in (first, second):
        assert app.test_client().get("/v1/models", headers={"Authorization": f"Bearer {raw}"}).status_code == 200
    browser = _signed_in(app, "shay")
    response = browser.post_json("/developer/playground/send", {"model": "llama3.2:3b",
                                                                "messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 200
    refused = app.test_client().get("/v1/models", headers={"Authorization": f"Bearer {first}"})
    assert refused.status_code == 429 and "3 requests per hour" in refused.json["error"]["message"]


def test_api_responses_describe_the_5_hour_token_window(app, make_user):
    user = make_user("hedy")
    setup_models(app)
    raw = _api_key(app, user["id"])
    with app.app_context():
        _policy("api", window={"tokens": 50_000})
    headers = {"Authorization": f"Bearer {raw}"}
    first = app.test_client().get("/v1/models", headers=headers)
    assert first.headers["x-ratelimit-limit-tokens"] == "50000"
    assert first.headers["x-ratelimit-remaining-tokens"] == "50000"
    assert first.headers["x-ratelimit-reset-tokens"] == "0s"  # no window open yet
    assert first.headers["x-ratelimit-limit-requests"] == "10"
    assert _complete(app, raw).status_code == 200
    after = app.test_client().get("/v1/models", headers=headers)
    assert int(after.headers["x-ratelimit-remaining-tokens"]) < 50_000
    assert after.headers["x-ratelimit-reset-tokens"].startswith(("4h59m", "5h0m"))


# ----- 5-hour and weekly windows ------------------------------------------------------------------

def test_the_5_hour_window_opens_at_the_first_request_and_resets_when_it_ends(app, make_user):
    from bananachat.db import credits
    from bananachat.services import limits

    user = make_user("wendy")
    with app.app_context():
        _policy("api", window={"tokens": 10_000, "slow_tokens": 0})
        before = limits.effective(user, "api")
        assert not before.window.open and before.window.resets_at is None and before.window.used == 0
        credits.charge(user["id"], 3000, 1000, request_type="api")
        opened = limits.effective(user, "api").window
        assert opened.open and opened.used == 4000
        assert opened.resets_at - opened.starts_at == timedelta(hours=5)
        credits.charge(user["id"], 6000, 0, request_type="api")
        assert not credits.budget(user, "api").available
        refusal = limits.admit(user, "api", None).refusal
        assert refusal.code == "insufficient_quota" and refusal.key == "window"
        _age_windows(user["id"], 5)
        after = limits.effective(user, "api").window
        assert not after.open and after.used == 0 and credits.budget(user, "api").available
        credits.charge(user["id"], 500, 0, request_type="api")
        again = limits.effective(user, "api").window
        assert again.open and again.used == 500


def test_the_weekly_window_is_rolling_off_by_default_and_blocks_without_a_slow_lane(app, make_user):
    from bananachat.db import credits
    from bananachat.services import limits

    user = make_user("wilma")
    with app.app_context():
        assert not limits.effective(user, "api").weekly.limited
        _policy("api", window={"tokens": 100_000, "slow_tokens": 50_000}, weekly={"enabled": True, "tokens": 8000})
        credits.charge(user["id"], 8000, 0, request_type="api")
        budget = credits.budget(user, "api")
        assert budget.weekly_exhausted and not budget.available and budget.slow_left == 0
        assert budget.blocked_until - datetime.now(timezone.utc) > timedelta(days=6, hours=23)
        _age_windows(user["id"], 6)  # a new 5-hour window, the same week
        assert not credits.budget(user, "api").available
        _age_windows(user["id"], 24 * 7)
        assert credits.budget(user, "api").available


def test_resets_close_the_windows_and_keep_the_ledger(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import limits

    user = make_user("rosa")
    with app.app_context():
        _policy("api", window={"tokens": 5000, "slow_tokens": 0}, weekly={"enabled": True, "tokens": 20_000})
        credits.charge(user["id"], 5000, 0, request_type="api")
        assert not credits.budget(user, "api").available
    assert admin.post(f"/admin/users/{user['id']}/limits/reset", {"period": "window"}).status_code == 302
    with app.app_context():
        current = limits.effective(user, "api")
        assert current.window.used == 0 and not current.window.open and current.weekly.used == 5000
        credits.charge(user["id"], 1000, 0, request_type="api")
    assert admin.post("/admin/quotas/reset-usage", {"period": "week"}).status_code == 302
    with app.app_context():
        current = limits.effective(user, "api")
        assert current.window.used == 0 and current.weekly.used == 0
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 2


# ----- model weights and model limits ------------------------------------------------------------

def test_a_models_weight_spends_the_pool_faster_and_model_limits_count_raw_tokens(app, make_user):
    from bananachat.db import credits, settings
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("wes")
    with app.app_context():
        settings.update(chat_local_token_consumption=1)
        _policy("api", window={"tokens": 20_000, "slow_tokens": 0})
        _model_policy(big, weight=3, enabled=True, window_tokens=10_000)
        counted, _slow = credits.charge(user["id"], 2000, 1000, request_type="api", model_id=big["id"])
        assert counted == 9000
        assert limits.effective(user, "api").window.used == 9000
        own = limits.model_limits(user, big)
        assert own.window.used == 3000 and own.window.tokens == 10_000 and own.window.open
        # The light model counts ×1 and has no limits of its own.
        credits.charge(user["id"], 1000, 0, request_type="chat", model_id=models["llama3.2:3b"]["id"])
        assert limits.effective(user, "chat").window.used == 1000
        # Model limits count every service.
        credits.charge(user["id"], 7000, 0, request_type="chat", model_id=big["id"])
        refusal = limits.admit(user, "chat", big).refusal
        assert refusal.key == "model_window" and refusal.status == 429 and "qwen3" in refusal.message("en").lower()
        assert limits.admit(user, "chat", models["llama3.2:3b"]).allowed


def test_a_model_outside_the_pool_is_admitted_when_the_pool_is_used_up(app, make_user):
    from bananachat.db import credits
    from bananachat.services import limits

    models = _models(app)
    outside = models["qwen3:4b"]
    user = make_user("otto")
    with app.app_context():
        _policy("api", window={"tokens": 1000, "slow_tokens": 0})
        _model_policy(outside, counts_toward_pool=False, enabled=True, window_tokens=5000)
        credits.charge(user["id"], 1000, 0, request_type="api", model_id=models["llama3.2:3b"]["id"])
        assert not limits.admit(user, "api", models["llama3.2:3b"]).allowed
        assert limits.admit(user, "api", outside).allowed
        counted, _slow = credits.charge(user["id"], 2000, 0, request_type="api", model_id=outside["id"])
        assert counted == 0 and limits.effective(user, "api").window.used == 1000
        assert limits.model_limits(user, outside).window.used == 2000
        assert limits.outside_pool("qwen3:4b") and not limits.outside_pool("llama3.2:3b")


def test_other_providers_do_not_count_toward_the_pool_by_default(app):
    from bananachat.services import limits

    local = {"id": 1, "provider": None}
    remote = {"id": 2, "provider": "anthropic"}
    policy = {"counts_toward_pool": None}
    assert limits.counts_toward_pool(local, policy) and not limits.counts_toward_pool(remote, policy)
    assert limits.counts_toward_pool(remote, {"counts_toward_pool": True})


def test_model_rate_rules_locks_and_per_account_overrides(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("mila")
    with app.app_context():
        _model_policy(big, enabled=True, rate_rules=[{"requests": 2, "per": "hour"}])
        assert limits.admit(user, "chat", big).allowed and limits.admit(user, "api", big).allowed
        refused = limits.admit(user, "chat", big)
        assert refused.refusal.code == "rate_limit_exceeded" and refused.refusal.key == "model_rate"
        # A custom limit for the account replaces the model's rules; a lock refuses the model outright.
        store.set_model_override(user["id"], big["id"], None, rate_rules=[{"requests": 100, "per": "hour"}])
        assert limits.admit(user, "chat", big).allowed
        store.set_model_override(user["id"], big["id"], None, locked=True)
        locked = limits.admit(user, "chat", big).refusal
        assert (locked.code, locked.status) == ("model_locked", 403)
        choices = limits.composer_choices(user, [{"name": "qwen3:4b"}, {"name": "llama3.2:3b"}], lang="en",
                                          request_url=lambda *args: "")
        assert [choice["name"] for choice in choices] == ["llama3.2:3b"]
        store.set_model_override(user["id"], big["id"], None, rate_rules=None, locked=False)
        assert store.get_model_override(user["id"], big["id"]) is None


def test_a_locked_model_is_skipped_by_auto_and_refused_by_name(app, make_user):
    from bananachat.db import limits as store

    models = _models(app)
    user = make_user("lola")
    raw = _api_key(app, user["id"])
    with app.app_context():
        for name in models:
            if name != "qwen3:4b":
                store.set_model_override(user["id"], models[name]["id"], None, locked=True)
    refused = _complete(app, raw)
    assert refused.status_code == 403 and refused.json["error"]["code"] == "model_locked"
    auto = _complete(app, raw, model="auto")
    assert auto.status_code == 200 and auto.json["model"] == "qwen3:4b"


def test_model_grants_raise_only_that_model(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("gina")
    with app.app_context():
        _policy("chat", window={"enabled": True, "tokens": 1000})
        _model_policy(big, enabled=True, window_tokens=2000)
        store.create_grant(created_by=None, user_id=user["id"], pool=None, model_id=big["id"], scope="window",
                           kind="extra", amount=3000, starts_at=db.now(), ends_at=None, reason="")
        assert limits.model_limits(user, big).window.tokens == 5000
        assert limits.effective(user, "chat").window.tokens == 1000
        with pytest.raises(ValueError):
            store.create_grant(created_by=None, user_id=None, pool="api", model_id=big["id"], scope=None,
                               kind="unlimited", amount=0, starts_at=db.now(), ends_at=None, reason="")
        with pytest.raises(ValueError):  # the request rate takes multipliers only
            store.create_grant(created_by=None, user_id=None, pool="api", scope="rate", kind="extra", amount=2,
                               starts_at=db.now(), ends_at=None, reason="")


def test_presets_fill_a_model_and_the_catalog_preset_is_the_default(app):
    from bananachat.db import catalog
    from bananachat.db import limits as store
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    with app.app_context():
        _model_policy(big, counts_toward_pool=False)
        heavy = limits.apply_model_preset(big["id"], "heavy")
        assert heavy["preset"] == "heavy" and heavy["weight"] == 3 and heavy["enabled"]
        assert heavy["window_tokens"] == 200_000 and heavy["rate_rules"][0]["per"] == "minute"
        assert heavy["effort_default"] == "low" and heavy["sensitivity"] == 2
        assert heavy["counts_toward_pool"] is False  # kept
        light = limits.apply_model_preset(big["id"], "light")
        assert light["weight"] == 0.5 and not light["enabled"]
        with pytest.raises(ValueError):
            limits.apply_model_preset(big["id"], "extreme")
        # A model nobody configured follows the preset the catalog gives it.
        small = models["llama3.2:3b"]
        _ensure_column("ai_models", "limit_preset", "TEXT")
        from bananachat import db
        db.execute("UPDATE ai_models SET limit_preset='heavy' WHERE id=?", (small["id"],))
        assert store.get_model_policy(catalog.get(small["id"]))["weight"] == 3


# ----- reasoning effort ---------------------------------------------------------------------------

def test_effort_levels_come_from_the_catalog_and_map_to_ollama_think(app):
    from bananachat.services import inference, limits

    named = {"id": 1, "is_reasoning": 1, "reasoning_levels": '["low", "medium", "high"]'}
    switch = {"id": 2, "is_reasoning": 1}
    plain = {"id": 3, "is_reasoning": 0}
    assert limits.supported_efforts(named) == ("low", "medium", "high")
    assert limits.supported_efforts(switch) == ("off", "on")
    assert limits.supported_efforts(plain) == ()
    assert limits.supported_efforts({"id": 4, "reasoning_levels": ["on", "off", "bogus"]}) == ("off", "on")
    assert [inference.think_for(named, level) for level in ("low", "high", "max")] == ["low", "high", "high"]
    assert inference.think_for(switch, "on") is True and inference.think_for(switch, "off") is False
    assert inference.think_for(switch, "high") is True and inference.think_for(switch, None) is True
    assert inference.think_for(plain, None) is None
    assert inference.compatible_fallbacks([named, switch, plain], "high") == [named]
    assert inference.compatible_fallbacks([named, switch, plain], True) == [named, switch]


def test_levels_up_to_medium_are_open_and_unlocking_one_opens_those_below(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("eve")
    named = {"id": 1, "is_reasoning": 1, "display_name": "Named",
             "reasoning_levels": '["off", "low", "medium", "high", "max"]'}
    with app.app_context():
        from bananachat import db
        model_id = db.execute("INSERT INTO ai_models (ollama_name, display_name, is_reasoning) VALUES "
                              "('named:1b', 'Named', 1)").lastrowid
        named["id"] = model_id
        policy = store.DEFAULT_MODEL_POLICY
        assert limits.allowed_efforts(user, named, policy=policy) == ("off", "low", "medium")
        assert limits.resolve_effort(user, named, None, policy=policy) == "medium"
        with pytest.raises(limits.EffortLocked) as locked:
            limits.resolve_effort(user, named, "high", policy=policy)
        assert locked.value.allowed == "medium"
        limits.unlock_effort(user["id"], model_id, "max", source="admin", updated_by=None)
        assert limits.allowed_efforts(user, named, policy=policy)[-1] == "max"
        # Unlocking never lowers; an administrator can lock below the default.
        assert limits.unlock_effort(user["id"], model_id, "low", source="request", updated_by=None) == "max"
        store.set_effort_level(user["id"], None, "low")
        store.set_effort_level(user["id"], model_id, None)
        assert limits.allowed_efforts(user, named, policy=policy) == ("off", "low")
        assert limits.resolve_effort(user, named, None, policy=policy) == "low"
        # Gating off for the account, then for everyone.
        store.update_user_settings(user["id"], None, effort_gating_off=True)
        assert limits.allowed_efforts(user, named, policy=policy)[-1] == "max"
        store.update_user_settings(user["id"], None, effort_gating_off=False)
        from bananachat.db import settings
        settings.update(effort_gating_enabled=0)
        assert limits.allowed_efforts(user, named, policy=policy)[-1] == "max"
        settings.update(effort_gating_enabled=1, effort_default_level="high")
        store.set_effort_level(user["id"], None, None)
        assert limits.allowed_efforts(user, named, policy={**policy, "effort_default": "high"})[-1] == "high"


def test_the_api_refuses_a_locked_effort_and_sends_the_level_to_ollama(app, make_user, fake_ollama):
    from bananachat import db

    models = _models(app)
    user = make_user("ada")
    raw = _api_key(app, user["id"])
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels=? WHERE id=?",
                   ('["low", "medium", "high"]', models["qwen3:4b"]["id"]))
    locked = _complete(app, raw, model="qwen3:4b", reasoning_effort="high")
    assert locked.status_code == 403
    error = locked.json["error"]
    assert error["code"] == "reasoning_effort_locked" and error["param"] == "reasoning_effort"
    assert "'medium'" in error["message"]
    assert _complete(app, raw, model="qwen3:4b", reasoning_effort="low").status_code == 200
    bodies = [body for path, body in fake_ollama.requests if path == "/api/chat"]
    assert bodies[-1]["think"] == "low"
    assert _complete(app, raw, model="qwen3:4b").status_code == 200
    assert [body for path, body in fake_ollama.requests if path == "/api/chat"][-1]["think"] == "medium"
    # The model cannot stop thinking: "none" runs at its lowest level.
    assert _complete(app, raw, model="qwen3:4b", reasoning_effort="none").status_code == 200
    assert [body for path, body in fake_ollama.requests if path == "/api/chat"][-1]["think"] == "low"
    bad = _complete(app, raw, model="qwen3:4b", reasoning_effort="extreme")
    assert bad.status_code == 400 and bad.json["error"]["param"] == "reasoning_effort"


def test_chat_sends_the_chosen_effort_and_refuses_a_locked_one(app, make_user, fake_ollama):
    from bananachat.db import users

    setup_models(app, reasoning=True)
    make_user("cleo")
    browser = _signed_in(app, "cleo")
    session_id = new_chat(browser)
    refused = send(browser, session_id, model="qwen3:4b", effort="high")
    # An on/off model: any level means "on" (medium), which is open by default.
    assert refused.status_code == 200
    wait_idle(app, session_id)
    assert [body for path, body in fake_ollama.requests if path == "/api/chat"][-1]["think"] is True
    assert send(browser, session_id, model="qwen3:4b", effort="off").status_code == 200
    wait_idle(app, session_id)
    assert [body for path, body in fake_ollama.requests if path == "/api/chat"][-1]["think"] is False
    with app.app_context():
        from bananachat.db import limits as store
        store.set_effort_level(users.get_by_username("cleo")["id"], None, "low")
    locked = send(browser, session_id, model="qwen3:4b", effort="on")
    assert locked.status_code == 403 and locked.json["error"]["code"] == "reasoning_effort_locked"
    page = browser.get(f"/chat/{session_id}").get_data(as_text=True)
    data = json.loads(page.split('id="page-data">', 1)[1].split("</script>", 1)[0])
    effort = next(model for model in data["models"] if model["name"] == "qwen3:4b")["effort"]
    assert [level["allowed"] for level in effort["levels"]] == [True, False]
    assert "request=effort" in effort["levels"][1]["request_url"]


def test_effort_requests_unlock_the_level_and_those_below(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import limits

    models = _models(app)
    user = make_user("ria")
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"low\",\"medium\",\"high\",\"max\"]' "
                   "WHERE id=?", (models["qwen3:4b"]["id"],))
    browser = _signed_in(app, "ria")
    page = browser.get("/account?request=effort&model=qwen3:4b&level=max#quota").get_data(as_text=True)
    assert 'value="effort" selected' in page and 'value="max" selected' in page
    response = browser.post("/account/quota-request", {"kind": "effort", "effort_model": "qwen3:4b",
                                                       "effort_level": "max", "reason": "Hard proofs"})
    assert response.status_code == 302
    with app.app_context():
        row = credits.pending_request(user["id"])
        assert (row["kind"], row["effort_level"], row["model_id"]) == ("effort", "max", models["qwen3:4b"]["id"])
    requests_page = admin.get("/admin/quotas/requests").get_data(as_text=True)
    assert "Reasoning effort up to Max" in requests_page and "now up to Medium" in requests_page
    assert admin.post(f"/admin/quotas/requests/{row['id']}", {"decision": "approve"}).status_code == 302
    with app.app_context():
        model = db.one("SELECT * FROM ai_models WHERE id=?", (models["qwen3:4b"]["id"],))
        assert limits.allowed_efforts(user, model) == ("low", "medium", "high", "max")
    again = browser.post("/account/quota-request", {"kind": "effort", "effort_model": "qwen3:4b",
                                                    "effort_level": "high", "reason": "Again please"})
    assert again.status_code == 400 and "already use that level" in again.get_data(as_text=True)


def test_sustained_use_unlocks_the_next_level_up_to_the_ceiling(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.db import settings
    from bananachat.services import limits

    models = _models(app)
    model = models["qwen3:4b"]
    user = make_user("sam")
    idle = make_user("ida")
    now = datetime.now(timezone.utc)
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"low\",\"medium\",\"high\",\"max\"]' "
                   "WHERE id=?", (model["id"],))
        for day in range(31):
            _charge(user["id"], 70_000, model_id=model["id"], at=now - timedelta(days=day, hours=1))
        _charge(idle["id"], 5_000_000, model_id=model["id"], at=now - timedelta(days=1))
        assert limits.auto_unlock_effort(now) == [("sam", "qwen3:4b", "high")]
        # One level at a time: counting starts again after an unlock.
        assert limits.auto_unlock_effort(now) == []
        mine = store.effort_levels(user["id"])[model["id"]]
        assert (mine.level, mine.source) == ("high", "automatic")
        # Never above the ceiling (High by default), never a kept level, never while switched off.
        db.execute("UPDATE user_effort_levels SET updated_at=? WHERE user_id=?",
                   (db.timestamp(now - timedelta(days=61)), user["id"]))
        assert limits.auto_unlock_effort(now) == []
        settings.update(effort_auto_ceiling="max")
        store.set_effort_level(user["id"], model["id"], "high", pinned=True)
        db.execute("UPDATE user_effort_levels SET updated_at=?", (db.timestamp(now - timedelta(days=61)),))
        assert limits.auto_unlock_effort(now) == []
        store.set_effort_level(user["id"], model["id"], "high")
        db.execute("UPDATE user_effort_levels SET updated_at=?", (db.timestamp(now - timedelta(days=61)),))
        settings.update(effort_auto_unlock=0)
        assert limits.auto_unlock_effort(now) == []
        settings.update(effort_auto_unlock=1)
        assert limits.auto_unlock_effort(now) == [("sam", "qwen3:4b", "max")]
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='limits.effort_unlock'") == 2


def test_agents_use_at_most_the_unlocked_effort(app, make_user):
    from bananachat.services import inference, limits

    user = make_user("aga")
    named = {"id": 99, "is_reasoning": 1, "reasoning_levels": '["low", "medium", "high"]'}
    with app.app_context():
        policy = {"effort_default": "low"}
        level = limits.resolve_effort(user, named, None, policy=policy, own={})
        assert inference.think_for(named, level) == "low"


# ----- dynamic limits and provider capacity --------------------------------------------------------

def test_the_number_of_people_online_moves_demand_and_heavy_models_react_more(app, make_user):
    from bananachat.services import limits

    assert limits.people_factor(None, 4) == 1.0
    assert limits.people_factor(2, 4) == pytest.approx(1.1)
    assert limits.people_factor(16, 4) == 1.0
    assert limits.people_factor(60, 4) == pytest.approx(0.8)
    assert limits.scaled(0.8, 2.0) == 0.6 and limits.scaled(1.2, 0.5) == 1.1 and limits.scaled(0.8, 0) == 1.0
    user = make_user("dora")
    with app.app_context():
        limits.record_demand(0, 0, 2, people=40)
        dynamic = limits.dynamic_for(user["id"])
        assert dynamic.people == 40
        assert "many_people" in [reason.code for reason in dynamic.reasons]
        assert dynamic.multiplier == limits.quantise(1.25 * 0.8)


def test_model_limits_follow_demand_scaled_by_sensitivity(app, make_user):
    from bananachat.services import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("hana")
    with app.app_context():
        _model_policy(big, enabled=True, window_tokens=100_000, dynamic=True, sensitivity=2.0)
        limits.record_demand(8, 0, 4)  # overloaded: -30 % for normal limits
        assert limits.dynamic_for(user["id"]).multiplier == 0.7
        assert limits.model_limits(user, big).window.tokens == 50_000  # -60 %, clamped to 0.5


def test_a_provider_reporting_little_capacity_shrinks_its_models_limits(app, make_user):
    from bananachat.services import limits

    models = _models(app)
    big = dict(models["qwen3:4b"])
    user = make_user("cara")
    with app.app_context():
        _model_policy(big, enabled=True, window_tokens=100_000, rate_rules=[{"requests": 10, "per": "minute"}])
        big["provider"] = "example"
        assert limits.capacity_factor(big) == 1.0  # no provider registered: no change
        limits.register_capacity_provider("example", lambda model: limits.Capacity(window_left=0.2))
        try:
            assert limits.capacity_factor(big) == 0.4
            current = limits.model_limits(user, big)
            assert current.window.tokens == 40_000 and current.rate.rules[0].requests == 4
            limits.register_capacity_provider("example", lambda model: limits.Capacity(weekly_left=0.9))
            assert limits.capacity_factor(big) == 1.0
            limits.register_capacity_provider("example", lambda model: 1 / 0)
            assert limits.capacity_factor(big) == 1.0  # a broken report never blocks
        finally:
            limits.unregister_capacity_provider("example")


# ----- requests, tiers, grants in tokens ----------------------------------------------------------

def test_5_hour_and_rate_requests_in_tokens_and_rules(app, admin, make_user):
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.db import settings
    from bananachat.services import limits

    user = make_user("tom")
    with app.app_context():
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=60_000,
                        quota_auto_approve_max_slow_tokens=20_000)
        _policy("api", rate={"rules": [{"requests": 60, "per": "minute", "burst": 10},
                                       {"requests": 1000, "per": "day"}]})
    browser = _signed_in(app, "tom")
    response = browser.post("/account/quota-request", {"kind": "window", "pool": "api", "tokens": "50k",
                                                       "slow_tokens": "15k", "reason": "Batch job"})
    assert response.status_code == 302
    with app.app_context():
        assert credits.get_quota(user["id"]) == (50_000, 0)
    rate = browser.post("/account/quota-request", {"kind": "rate", "pool": "api", "rate_per": "day",
                                                   "rate_requests": "3000", "reason": "Nightly sync"})
    assert rate.status_code == 302
    with app.app_context():
        pending = credits.pending_request(user["id"])
    assert admin.post(f"/admin/quotas/requests/{pending['id']}", {"decision": "approve"}).status_code == 302
    with app.app_context():
        rules = limits.base_limits(user["id"], "api")["rate_rules"]
        assert rules == [{"requests": 60, "per": "minute", "burst": 10},
                         {"requests": 3000, "per": "day", "burst": 3000}]
        assert store.get_override(user["id"], "api").window_tokens == 50_000


def test_tiers_multiply_tokens_and_promote_on_tokens(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("tia")
    with app.app_context():
        _policy("api", window={"auto_tiers": True, "tokens": 10_000})
        tiers = store.list_tiers()
        store.update_tier(tiers[1]["id"], {**tiers[1].to_dict(), "min_account_days": 0, "min_active_days": 0,
                                           "min_tokens_30d": 5000, "clean_days": 0})
        _charge(user["id"], 6000)
        assert limits.promote() == [("tia", "Starter", "Regular")]
        assert limits.effective(user, "api").window.tokens == 20_000
        assert db.scalar("SELECT min_credits_30d FROM limit_tiers WHERE id=?", (tiers[1]["id"],)) == 5


# ----- admission cost ------------------------------------------------------------------------------

def test_admission_is_a_handful_of_queries(app, make_user):
    from bananachat import db
    from bananachat.services import limits

    models = _models(app)
    user = make_user("quinn")
    with app.app_context():
        _policy("api", window={"tokens": 50_000}, weekly={"enabled": True, "tokens": 200_000})
        _model_policy(models["qwen3:4b"], enabled=True, window_tokens=10_000,
                      rate_rules=[{"requests": 5, "per": "minute"}])
        limits.admit(user, "api", models["qwen3:4b"])  # warm the per-process caches
        statements = []
        db.conn().set_trace_callback(statements.append)
        try:
            assert limits.admit(user, "api", models["qwen3:4b"]).allowed
        finally:
            db.conn().set_trace_callback(None)
        selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
        assert len(selects) <= 20, selects


# ----- pages -------------------------------------------------------------------------------------

def test_admin_limit_pages_render_and_explain(app, admin, make_user):
    user = make_user("pat")
    _models(app)
    for path, text in (("/admin/quotas", "How limits work"), ("/admin/quotas/models", "Quick start"),
                       ("/admin/quotas/effort", "How it works"), ("/admin/quotas/requests", "Quota requests"),
                       ("/admin/quotas/tiers", "Token multiplier"), ("/admin/quotas/grants", "One model"),
                       (f"/admin/users/{user['id']}/limits", "Reasoning effort")):
        response = admin.get(path)
        assert response.status_code == 200, path
        assert text in response.get_data(as_text=True), path


def test_admins_edit_service_policies_with_rules_and_friendly_amounts(app, admin):
    from bananachat import db
    from bananachat.db import limits

    form = {"rate_enabled": "1", "rule_requests_0": "2", "rule_per_0": "second", "rule_burst_0": "",
            "rule_requests_1": "300", "rule_per_1": "hour", "rule_burst_1": "50",
            "window_enabled": "1", "window_tokens": "45k", "window_slow_tokens": "1.5M",
            "weekly_tokens": "500,000"}
    assert admin.post("/admin/quotas/policy/api", form).status_code == 302
    with app.app_context():
        policy = limits.get_policy("api")
        assert policy["rate"]["rules"] == [{"requests": 2, "per": "second", "burst": 2},
                                           {"requests": 300, "per": "hour", "burst": 50}]
        assert policy["window"]["tokens"] == 45_000 and "slow_tokens" not in policy["window"]
        assert not policy["weekly"]["enabled"] and policy["weekly"]["tokens"] == 500_000
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='admin.limits_policy'") == 1
    bad = admin.post("/admin/quotas/policy/api", {**form, "window_tokens": "plenty"}, follow_redirects=True)
    assert "write a number of tokens" in bad.get_data(as_text=True)
    duplicate = admin.post("/admin/quotas/policy/api", {**form, "rule_per_1": "second"}, follow_redirects=True)
    assert "one request-rate rule per time unit" in duplicate.get_data(as_text=True)


def test_admins_apply_presets_and_edit_models(app, admin):
    from bananachat.db import catalog
    from bananachat.db import limits
    from bananachat.db import settings

    models = _models(app)
    big = models["qwen3:4b"]
    assert admin.post(f"/admin/quotas/models/{big['id']}", {"action": "heavy"}).status_code == 302
    with app.app_context():
        assert limits.get_model_policy(catalog.get(big["id"]))["preset"] == "heavy"
    form = {"action": "save", "weight": "2", "counts_toward_pool": "no", "enabled": "1", "rule_requests_0": "4",
            "rule_per_0": "minute", "window_tokens": "100k", "weekly_tokens": "", "sensitivity": "1.5",
            "effort_default": "high"}
    assert admin.post(f"/admin/quotas/models/{big['id']}", form).status_code == 302
    with app.app_context():
        policy = limits.get_model_policy(catalog.get(big["id"]))
        assert (policy["weight"], policy["counts_toward_pool"], policy["window_tokens"]) == (2, False, 100_000)
        assert policy["weekly_tokens"] is None and policy["preset"] == "custom" and policy["effort_default"] == "high"
    page = admin.get("/admin/quotas/models").get_data(as_text=True)
    assert "Service tokens excluded" in page and "1 rate rule" in page
    assert 'name="rule_requests_0"' in page and 'value="4"' in page
    with app.app_context():
        from bananachat import db
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels=? WHERE id=?",
                   ('["low", "medium", "high"]', big["id"]))
        settings.update(effort_gating_enabled=0)
    page = admin.get("/admin/quotas/models").get_data(as_text=True)
    assert "All reasoning levels" in page and "Reasoning up to High" not in page
    assert admin.post(f"/admin/quotas/models/{big['id']}", {"action": "reset"}).status_code == 302
    with app.app_context():
        assert not limits.has_model_policy(big["id"])


def test_admins_set_model_limits_locks_and_effort_for_one_account(app, admin, make_user):
    from bananachat.db import limits

    models = _models(app)
    big = models["qwen3:4b"]
    user = make_user("una")
    url = f"/admin/users/{user['id']}/limits"
    assert admin.post(f"{url}/model", {"model_id": big["id"], "window_tokens": "20k", "locked": "1"}).status_code == 302
    with app.app_context():
        override = limits.get_model_override(user["id"], big["id"])
        assert override.window_tokens == 20_000 and override.locked
    page = admin.get(url).get_data(as_text=True)
    assert "locked" in page and "20k tokens" in page
    assert admin.post(f"{url}/model", {"model_id": big["id"], "action": "clear"}).status_code == 302
    assert admin.post(f"{url}/effort", {"model_id": "all", "level": "high", "pinned": "1"}).status_code == 302
    assert admin.post(f"{url}/effort", {"action": "gating", "effort_gating_off": "1"}).status_code == 302
    with app.app_context():
        assert limits.get_model_override(user["id"], big["id"]) is None
        mine = limits.effort_levels(user["id"])[None]
        assert (mine.level, mine.pinned, mine.source) == ("high", True, "admin")
        assert limits.user_settings(user["id"]).effort_gating_off
    custom = admin.post(f"{url}/custom", {"pool": "chat", "rule_requests_0": "5", "rule_per_0": "minute",
                                          "window_tokens": "1M"})
    assert custom.status_code == 302
    with app.app_context():
        override = limits.get_override(user["id"], "chat")
        assert override.window_tokens == 1_000_000 and override.rate_rules[0]["requests"] == 5


def test_admins_grant_extra_tokens_to_one_model(app, admin, make_user):
    from bananachat.db import limits

    models = _models(app)
    big = models["qwen3:4b"]
    make_user("gus")
    with app.app_context():
        _model_policy(big, enabled=True, window_tokens=1000)
    response = admin.post("/admin/quotas/grants", {"target": "user", "username": "gus", "pool": f"model:{big['id']}",
                                                   "scope": "window", "kind": "extra", "amount": "5k",
                                                   "duration": "24h", "reason": "Deadline"})
    assert response.status_code == 302
    with app.app_context():
        grant = limits.list_grants("active")[0]
        assert (grant["model_id"], grant["amount"], grant["scope"]) == (big["id"], 5000, "window")
    assert "model Qwen" in admin.get("/admin/quotas/grants").get_data(as_text=True) or \
        big["display_name"] in admin.get("/admin/quotas/grants").get_data(as_text=True)


def test_admins_change_effort_settings(app, admin):
    from bananachat.services import limits

    form = {"effort_gating_enabled": "1", "effort_default_level": "low", "effort_auto_unlock": "1",
            "effort_auto_active_days": "10", "effort_auto_tokens": "500k", "effort_auto_period_days": "30",
            "effort_auto_clean_days": "30", "effort_auto_ceiling": "max"}
    assert admin.post("/admin/quotas/effort", form).status_code == 302
    with app.app_context():
        settings = limits.effort_settings(None)
        assert (settings["default"], settings["tokens"], settings["ceiling"]) == ("low", 500_000, "max")
    wrong = admin.post("/admin/quotas/effort", {**form, "effort_auto_active_days": "40"}, follow_redirects=True)
    assert "cannot be more than the days of the period" in wrong.get_data(as_text=True)


@pytest.mark.parametrize(("language", "texts"), [
    ("en", ["Tokens in this 5-hour window", "Your next request starts a new 5-hour window.", "counts ×3",
            "Reasoning effort you can use", "Ask for High"]),
    ("it", ["Token in questa finestra di 5 ore", "La tua prossima richiesta apre una nuova finestra di 5 ore.",
            "conta ×3", "Livelli di ragionamento che puoi usare", "Chiedi Alto"]),
])
def test_the_account_page_explains_tokens_windows_models_and_effort(app, make_user, language, texts):
    from bananachat import db

    models = _models(app)
    make_user("lia")
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"low\",\"medium\",\"high\"]' WHERE id=?",
                   (models["qwen3:4b"]["id"],))
        _model_policy(models["qwen3:4b"], weight=3)
    page = _signed_in(app, "lia", language=language).get("/account").get_data(as_text=True)
    for text in texts:
        assert text in page, text
    assert "credit" not in page.lower().replace("credits_", "")


def test_the_account_page_shows_when_an_open_window_resets(app, make_user):
    from bananachat.db import credits, users

    make_user("rhea")
    with app.app_context():
        credits.charge(users.get_by_username("rhea")["id"], 1000, 500, request_type="api")
    page = _signed_in(app, "rhea").get("/account").get_data(as_text=True)
    assert "1.5k tokens of 30k tokens" in page or "1.5k tokens" in page
    assert "Full again at" in page and "UTC" in page


def test_the_developer_pages_show_tokens(app, make_user):
    from bananachat.db import credits, users

    make_user("dev")
    with app.app_context():
        credits.charge(users.get_by_username("dev")["id"], 2000, 1000, request_type="api")
    browser = _signed_in(app, "dev")
    overview = browser.get("/developer").get_data(as_text=True)
    assert "Tokens in this 5-hour window" in overview and "3k tokens" in overview
    usage = browser.get("/developer/usage").get_data(as_text=True)
    assert "Counted" in usage and "3,000" in usage


def test_effort_requests_cost_the_same_queries_for_one_or_many(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits

    models = _models(app)
    with app.app_context():
        _ensure_column("ai_models", "reasoning_levels", "TEXT")
        db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels='[\"low\",\"medium\",\"high\"]' WHERE id=?",
                   (models["qwen3:4b"]["id"],))

    def queries() -> int:
        statements = []
        db.conn().set_trace_callback(statements.append)
        try:
            assert admin.get("/admin/quotas/requests").status_code == 200
        finally:
            db.conn().set_trace_callback(None)
        return len(statements)

    with app.app_context():
        first = make_user("thinker0")
        credits.submit_request(first["id"], kind="effort", model_id=models["qwen3:4b"]["id"], level="high",
                               reason="Harder problems")
    queries()
    one = queries()
    with app.app_context():
        for number in range(1, 5):
            user = make_user(f"thinker{number}")
            credits.submit_request(user["id"], kind="effort", model_id=None if number % 2 else models["qwen3:4b"]["id"],
                                   level="high", reason="Harder problems")
    assert queries() == one
