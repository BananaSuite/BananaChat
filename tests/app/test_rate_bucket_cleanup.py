"""Cleanup must not restore request allowance before effective rate buckets refill."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.app.test_chat import setup_models


DAY = 86_400
START = 1_000_000.0
RULE = {"requests": 1, "per": "day", "burst": 1}


def _pool_rate(*, dynamic=False):
    from bananachat.db import limits as store

    policy = store.get_policy("api")
    policy["rate"].update(enabled=True, dynamic=dynamic, rules=[RULE])
    store.set_policy("api", policy, None)


def _model(app, *, dynamic=False, provider=""):
    from bananachat import db
    from bananachat.db import catalog, limits as store

    setup_models(app)
    db.execute("UPDATE ai_models SET provider=? WHERE ollama_name='llama3.2:3b'", (provider,))
    model = catalog.get_by_name("llama3.2:3b")
    store.set_model_policy(model["id"], {"enabled": True, "dynamic": dynamic,
                           "rate_rules": [RULE], "counts_toward_pool": False}, None)
    return model


def test_cleanup_preserves_a_pool_rate_reduced_by_demand(app, make_user, monkeypatch):
    from bananachat.services import limits

    user = make_user("demand-cleanup")
    monkeypatch.setattr(limits, "dynamic_for", lambda *args, **kwargs: limits.Dynamic(0.5, 0.5, 1.0))
    with app.app_context():
        _pool_rate(dynamic=True)
        assert limits.check_rate(user, "api", now=START).allowed
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 0
        assert not limits.check_rate(user, "api", now=START + DAY + 1).allowed
        assert limits.forget_idle_buckets(now=START + 3 * DAY + 2) == 1
        assert limits.check_rate(user, "api", now=START + 3 * DAY + 2).allowed


def test_cleanup_preserves_a_model_rate_reduced_by_demand(app, make_user, monkeypatch):
    from bananachat.services import limits

    user = make_user("model-demand-cleanup")
    monkeypatch.setattr(limits, "dynamic_for", lambda *args, **kwargs: limits.Dynamic(0.5, 0.5, 1.0))
    with app.app_context():
        model = _model(app, dynamic=True)
        assert limits.admit(user, "api", model, now=START).allowed
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 0
        refusal = limits.admit(user, "api", model, now=START + DAY + 1).refusal
        assert refusal is not None and refusal.code == "rate_limit_exceeded"


@pytest.mark.parametrize("custom", [False, True])
def test_cleanup_preserves_provider_reduced_model_rates(app, make_user, monkeypatch, custom):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("capacity-cleanup")
    monkeypatch.setitem(limits._capacity_providers, "cleanup-provider",
                        lambda model: limits.Capacity(window_left=0.025))
    with app.app_context():
        model = _model(app, dynamic=True, provider="cleanup-provider")
        if custom:
            store.set_model_override(user["id"], model["id"], None, rate_rules=[RULE])
        monkeypatch.setattr(limits, "dynamic_for", lambda *args, **kwargs: limits.Dynamic(0.5, 0.5, 1.0))
        assert limits.capacity_factor(model) == 0.05
        assert limits.admit(user, "api", model, now=START).allowed
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 0
        refusal = limits.admit(user, "api", model, now=START + DAY + 1).refusal
        assert refusal is not None and refusal.code == "rate_limit_exceeded"
        # Both ordinary and custom rules remain eligible for eventual cleanup.
        assert limits.forget_idle_buckets(now=START + 42 * DAY) == 1


def test_cleanup_includes_grants_that_round_up_the_burst(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("grant-cleanup")
    with app.app_context():
        _pool_rate()
        store.create_grant(created_by=None, user_id=user["id"], pool="api", scope="rate", kind="multiplier",
                           amount=1.01, starts_at=db.now(-timedelta(seconds=1)), ends_at=None, reason="Test")
        assert limits.check_rate(user, "api", now=START).allowed
        assert limits.check_rate(user, "api", now=START).allowed
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 0
        decision = limits.check_rate(user, "api", now=START + DAY + 1)
        assert decision.allowed and decision.remaining == 0
        assert limits.forget_idle_buckets(now=START + 4 * DAY) == 1


def test_cleanup_keeps_custom_pool_rates_independent_of_dynamic_adjustment(app, make_user, monkeypatch):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("custom-cleanup")
    monkeypatch.setattr(limits, "dynamic_for", lambda *args, **kwargs: limits.Dynamic(0.5, 0.5, 1.0))
    with app.app_context():
        policy = store.get_policy("api")
        policy["rate"]["dynamic"] = True
        store.set_policy("api", policy, None)
        store.set_override(user["id"], "api", None, rate_rules=[RULE])
        assert limits.check_rate(user, "api", now=START).allowed
        # Custom rates are exact, even while their pool policy follows demand.
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 1


def test_cleanup_ignores_unreadable_custom_rules(app, make_user):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("invalid-cleanup")
    with app.app_context():
        store.set_override(user["id"], "api", None, rate_rules=[RULE])
        db.execute("UPDATE user_limit_overrides SET rate_rules='invalid json' WHERE user_id=?", (user["id"],))
        assert limits.check_rate(user, "api", now=START).allowed
        assert limits.forget_idle_buckets(now=START + DAY + 1) == 1
