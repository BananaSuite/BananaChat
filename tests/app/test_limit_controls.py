"""Internal exemptions and reasoning controls retain locks, capacity and usage."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from tests.app.test_chat import setup_models


def _model(app):
    from bananachat.db import catalog

    setup_models(app)
    return catalog.get_by_name("qwen3:4b")


def _policies(model):
    from bananachat.db import limits as store

    policy = store.get_policy("api")
    policy["rate"].update(enabled=True, rules=[{"requests": 1, "per": "day", "burst": 1}])
    policy["window"].update(enabled=True, tokens=10)
    policy["weekly"].update(enabled=True, tokens=20)
    store.set_policy("api", policy, None)
    store.set_model_policy(model["id"], {"enabled": True, "counts_toward_pool": True,
        "window_tokens": 8, "weekly_tokens": 16,
        "rate_rules": [{"requests": 1, "per": "day", "burst": 1}]}, None)


@pytest.mark.parametrize("token_exempt,rate_exempt", [(True, False), (False, True), (True, True), (False, False)])
def test_account_exemptions_are_independent_and_still_record_usage(app, make_user, token_exempt, rate_exempt):
    from bananachat.db import credits, limits as store
    from bananachat.services import limits

    user = make_user("controls")
    with app.app_context():
        model = _model(app)
        _policies(model)
        store.update_user_settings(user["id"], None, token_exempt=token_exempt, rate_exempt=rate_exempt)
        credits.charge(user["id"], 30, 0, request_type="api", model_id=model["id"])
        pool = limits.effective(user, "api")
        own = limits.model_limits(user, model)
        assert pool.window.used == pool.weekly.used == own.window.used == own.weekly.used == 30
        assert pool.window.limited == pool.weekly.limited == own.window.limited == own.weekly.limited == (not token_exempt)
        assert pool.rate.limited == own.rate.limited == (not rate_exempt)
        base = limits.base_limits(user["id"], "api")
        many = limits.base_limits_many([(user["id"], "api")])[(user["id"], "api")]
        assert base == many
        model_base = limits.model_base(user["id"], model)
        assert base["window_enabled"] == base["weekly_enabled"] == model_base["window_enabled"] == \
            model_base["weekly_enabled"] == (not token_exempt)
        assert base["rate_enabled"] == model_base["rate_enabled"] == (not rate_exempt)
        assert limits.limit_on(user["id"], "api", "window") == (not token_exempt)
        assert limits.limit_on(user["id"], None, "weekly", model) == (not token_exempt)
        assert limits.limit_on(user["id"], None, "rate", model) == (not rate_exempt)
        assert limits.check_rate(user, "api", now=1000).allowed
        assert limits.check_rate(user, "api", now=1000).allowed == rate_exempt
        assert limits.admit(user, "api", model, take_rate=False).allowed == token_exempt


def test_own_model_exemptions_leave_service_limits_in_force(app, make_user):
    from bananachat.db import credits, limits as store
    from bananachat.services import limits

    user = make_user("model-controls")
    with app.app_context():
        model = _model(app)
        _policies(model)
        own = store.set_model_override(user["id"], model["id"], None, token_exempt=True, rate_exempt=True)
        assert not own.empty
        credits.charge(user["id"], 30, 0, request_type="api", model_id=model["id"])
        effective = limits.model_limits(user, model)
        assert not effective.window.limited and not effective.weekly.limited and not effective.rate.limited
        base = limits.model_base(user["id"], model)
        assert not any(base[f"{scope}_enabled"] for scope in ("window", "weekly", "rate"))
        assert limits.admit(user, "api", model).refusal.key == "weekly"
        assert limits.check_rate(user, "api", now=1000).allowed
        assert not limits.check_rate(user, "api", now=1000).allowed
        store.set_model_override(user["id"], model["id"], None, token_exempt=False, rate_exempt=False)
        assert store.get_model_override(user["id"], model["id"]) is None


@pytest.mark.parametrize("source", ["account", "model", "policy"])
def test_token_rate_exemptions_never_bypass_model_lock_or_provider_capacity(app, make_user, monkeypatch, source):
    from bananachat import db
    from bananachat.db import catalog, limits as store
    from bananachat.services import limits

    user = make_user("safe-controls")
    with app.app_context():
        model = _model(app)
        _policies(model)
        if source == "account":
            store.update_user_settings(user["id"], None, token_exempt=True, rate_exempt=True)
        elif source == "model":
            store.set_model_override(user["id"], model["id"], None, token_exempt=True, rate_exempt=True)
        else:
            policy = store.get_model_policy(model)
            store.set_model_policy(model["id"], {**policy, "tokens_enabled": False, "rate_enabled": False}, None)
        store.set_model_override(user["id"], model["id"], None, locked=True)
        assert limits.admit(user, "api", model).refusal.code == "model_locked"
        store.set_model_override(user["id"], model["id"], None, locked=False)
        db.execute("UPDATE ai_models SET provider='controls-provider' WHERE id=?", (model["id"],))
        model = catalog.get(model["id"])
        monkeypatch.setitem(limits._capacity_providers, "controls-provider", lambda _: limits.Capacity(window_left=0))
        assert limits.admit(user, "api", model).refusal.key == "model_window_none"
        assert limits.model_blocked(user, model, "api")


def test_model_policy_switches_are_independent_and_override_custom_rules(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("policy-controls")
    with app.app_context():
        model = _model(app)
        _policies(model)
        store.set_model_override(user["id"], model["id"], None, window_tokens=1, weekly_tokens=1,
                                 rate_rules=[{"requests": 1, "per": "day"}])
        policy = store.get_model_policy(model)
        store.set_model_policy(model["id"], {**policy, "tokens_enabled": False}, None)
        own = limits.model_limits(user, model)
        assert own.rate.limited and not own.window.limited and not own.weekly.limited
        assert not limits.limit_on(None, None, "window", model)
        assert not limits.model_base(user["id"], model)["window_enabled"]
        store.set_model_policy(model["id"], {**policy, "rate_enabled": False}, None)
        own = limits.model_limits(user, model)
        assert not own.rate.limited and own.window.limited and own.weekly.limited
        assert not limits.model_base(user["id"], model)["rate_enabled"]


def test_restore_defaults_removes_exemptions_but_preserves_deliberate_effort_controls(app, make_user):
    from bananachat.db import limits as store

    user = make_user("restore-controls")
    with app.app_context():
        store.update_user_settings(user["id"], None, token_exempt=True, rate_exempt=True, dynamic_exempt=True,
                                   effort_auto_unlock_off=True)
        assert store.count_custom() == 1
        assert store.restore_defaults(user["id"], None) == 1
        prefs = store.user_settings(user["id"])
        assert not prefs.token_exempt and not prefs.rate_exempt
        assert prefs.dynamic_exempt and prefs.effort_auto_unlock_off
        assert store.count_custom() == 0


def _reasoning(model):
    from bananachat import db
    from bananachat.db import catalog

    db.execute("UPDATE ai_models SET is_reasoning=1,reasoning_levels=? WHERE id=?",
               ('["low","medium","high","xhigh","max"]', model["id"]))
    return catalog.get(model["id"])


def test_extra_is_a_declared_tier_between_high_and_max_and_xhigh_aliases_it(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("extra-controls")
    with app.app_context():
        model = _reasoning(_model(app))
        assert limits.supported_efforts(model) == ("low", "medium", "high", "extra", "max")
        assert limits.allowed_efforts(user, model) == ("low", "medium")
        assert limits.resolve_effort(user, model, None) == "medium"
        with pytest.raises(limits.EffortLocked):
            limits.resolve_effort(user, model, "extra")
        limits.unlock_effort(user["id"], model["id"], "extra", source="request", updated_by=None)
        assert store.effort_levels(user["id"])[model["id"]].level == "extra"
        assert limits.resolve_effort(user, model, "xhigh") == "extra"
        assert limits.allowed_efforts(user, model) == ("low", "medium", "high", "extra")
        assert limits._next_level(limits.supported_efforts(model), "high") == "extra"
        assert limits._next_level(limits.supported_efforts(model), "extra") == "max"
        assert "extra" not in limits.supported_efforts({"is_reasoning": 1, "reasoning_levels": '["low","high","max"]'})


def test_default_effort_cannot_bypass_a_cap_when_model_has_no_allowed_level(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("blocked-effort")
    with app.app_context():
        model = _reasoning(_model(app))
        store.set_effort_level(user["id"], model["id"], "off", pinned=True)
        assert limits.allowed_efforts(user, model) == ()
        with pytest.raises(limits.EffortLocked):
            limits.resolve_effort(user, model, None)
        summary = limits.effort_summary(user, model)
        assert summary["default"] is None and all(not item["allowed"] for item in summary["levels"])


@pytest.mark.parametrize("source", ["account", "model", "site"])
def test_reasoning_gating_off_allows_only_actual_model_tiers(app, make_user, source):
    from bananachat.db import limits as store, settings
    from bananachat.services import limits

    user = make_user("effort-controls")
    with app.app_context():
        model = _reasoning(_model(app))
        store.set_effort_level(user["id"], model["id"], "low", pinned=True)
        if source == "account":
            store.update_user_settings(user["id"], None, effort_gating_off=True)
        elif source == "model":
            policy = store.get_model_policy(model)
            store.set_model_policy(model["id"], {**policy, "effort_gating_off": True}, None)
        else:
            settings.update(effort_gating_enabled=0)
        assert limits.resolve_effort(user, model, "max") == "max"
        assert limits.allowed_efforts(user, model) == limits.supported_efforts(model)


def _activity(user, model, now):
    from bananachat import db
    from bananachat.db import settings

    settings.update(effort_auto_active_days=2, effort_auto_tokens=10, effort_auto_ceiling="max")
    for days in (1, 2):
        db.execute("INSERT INTO credit_ledger(user_id,model_id,tokens_in,tokens_out,credits_used,request_type,created_at) "
                   "VALUES (?,?,10,0,0.01,'api',?)", (user["id"], model["id"], db.timestamp(now - timedelta(days=days))))


@pytest.mark.parametrize("source", ["account", "model", "site", "all-models-pin"])
def test_automatic_effort_unlocks_honor_explicit_opt_outs_and_account_pins(app, make_user, source):
    from bananachat.db import limits as store, settings
    from bananachat.services import limits

    user = make_user("auto-controls")
    with app.app_context():
        model = _reasoning(_model(app))
        now = datetime.now(timezone.utc)
        _activity(user, model, now)
        if source == "account":
            store.update_user_settings(user["id"], None, effort_auto_unlock_off=True)
        elif source == "model":
            policy = store.get_model_policy(model)
            store.set_model_policy(model["id"], {**policy, "effort_auto_unlock_off": True}, None)
        elif source == "site":
            settings.update(effort_auto_unlock=0)
        else:
            store.set_effort_level(user["id"], None, "medium", pinned=True)
        assert limits.auto_unlock_effort(now) == []
        assert model["id"] not in store.effort_levels(user["id"])


def test_automatic_unlock_does_not_overwrite_admin_pin_added_after_candidate_scan(app, make_user, monkeypatch):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("pin-race")
    with app.app_context():
        model = _reasoning(_model(app))
        now = datetime.now(timezone.utc)
        _activity(user, model, now)
        original = db.transaction
        inserted = False

        @contextmanager
        def race(*args, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                with original():
                    store.set_effort_level(user["id"], model["id"], "low", pinned=True)
            with original(*args, **kwargs) as connection:
                yield connection

        monkeypatch.setattr(db, "transaction", race)
        assert limits.auto_unlock_effort(now) == []
        mine = store.effort_levels(user["id"])[model["id"]]
        assert (mine.level, mine.pinned, mine.source) == ("low", True, "admin")


def test_sustained_use_advances_high_then_extra_then_max_only_after_new_activity(app, make_user, monkeypatch):
    from bananachat import db
    from bananachat.services import limits

    user = make_user("auto-extra-chain")
    with app.app_context():
        model = _reasoning(_model(app))
        clock = [datetime.now(timezone.utc)]
        monkeypatch.setattr(db, "now", lambda delta=timedelta(): db.timestamp(clock[0] + delta))
        for level in ("high", "extra", "max"):
            _activity(user, model, clock[0])
            assert limits.auto_unlock_effort(clock[0]) == [(user["username"], model["ollama_name"], level)]
            assert limits.auto_unlock_effort(clock[0]) == []
            clock[0] += timedelta(days=4)
