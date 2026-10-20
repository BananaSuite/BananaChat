"""Subscription pool: strict limits, shared capacity, enrollment, two providers."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from bananachat import db
from bananachat.db import catalog
from bananachat.db import limits as limits_db
from bananachat.db import subscription_pool as pool_db
from bananachat.services import limits, subscription_pool


@pytest.fixture(autouse=True)
def declared_test_adapters(app):
    """Fake adapters declare availability and capabilities for both providers."""
    for provider in subscription_pool.PROVIDERS:
        subscription_pool.register_site_discovery(provider, (lambda p: lambda: [
            {**item, "reasoning": ["low", "medium", "high"], "capabilities": ["completion"]}
            for item in subscription_pool.CURATED[p]])(provider))
        subscription_pool.register_site_chat(provider, lambda account, model, messages, options: iter([{"done": True}]))
    yield
    subscription_pool.reset_transport()


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


def _stamp(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def test_strict_defaults_are_token_based_and_not_counted(app):
    opus = subscription_pool.strict_policy("claude", "claude-opus-4-1")
    sonnet = subscription_pool.strict_policy("claude", "claude-sonnet-4-5")
    haiku = subscription_pool.strict_policy("claude", "claude-haiku-4-5")
    assert opus["window_tokens"] < sonnet["window_tokens"] < haiku["window_tokens"]
    assert opus["weekly_tokens"] is None and opus["counts_toward_pool"] is False
    assert opus["enabled"] is True and opus["effort_default"] == "medium"
    astra = subscription_pool.strict_policy("codex", "gpt-6-astra")
    luna = subscription_pool.strict_policy("codex", "gpt-6-luna")
    assert astra["window_tokens"] < luna["window_tokens"] and astra["preset"] == "heavy"
    assert subscription_pool.family_of("codex", "gpt-5.5") == "gpt"


def test_sync_enrolls_selected_models_per_provider(app):
    with app.test_request_context():
        state = subscription_pool.sync_catalog("claude", selected=["claude-sonnet-4-5"], source="test")
        assert state["count"] == 1
        row = catalog.get_by_name("claude-sonnet-4-5")
        assert row is not None and row["backend"] == "claude" and row["provider"] == "claude"
        assert row["enrollment"] == "new"  # manual policy waits for review
        assert catalog.get_by_name("claude-opus-4-1") is None  # not selected

        subscription_pool.sync_catalog("codex", selected=["gpt-6.1-sol"], source="test")
        codex_row = catalog.get_by_name("gpt-6.1-sol")
        assert codex_row["backend"] == "codex" and codex_row["provider"] == "codex"


def test_pooled_capacity_shrinks_model_limits(app):
    user = _user(app)
    with app.test_request_context():
        subscription_pool.sync_catalog("claude", selected=["claude-opus-4-1"], source="test")
        model = catalog.get_by_name("claude-opus-4-1")
        limits_db.set_model_policy(model["id"], subscription_pool.strict_policy("claude", model["ollama_name"]), None)
        subscription_pool.ensure_registered()
        # With no account nothing can be served: the model counts as used up.
        assert limits.model_limits(user, model).window.tokens == 0

        account_id = pool_db.add_account("claude-1", provider="claude", window_limit=100_000)
        before = limits.model_limits(user, model)
        assert before.window.limited and before.window.tokens > 0
        pool_db.report_quota(account_id, window_used=90_000, window_limit=100_000)
        after = limits.model_limits(user, catalog.get(model["id"]))
        assert after.capacity < 1.0 and after.window.tokens < before.window.tokens
        pool_db.remove_account(account_id)


def test_pooled_quota_sums_accounts_and_one_exhausted_removes_only_its_share(app):
    with app.test_request_context():
        first = pool_db.add_account("claude-a", provider="claude", window_limit=100_000)
        pool_db.add_account("claude-b", provider="claude", window_limit=100_000)
        pool_db.report_quota(first, window_used=100_000, window_limit=100_000, window_resets_at=_stamp(hours=2))
        quota = subscription_pool.refresh_quota("claude")["claude"]
        assert quota["window_left"] == 0.5 and quota["usable"] == 1


def test_unknown_window_limit_is_never_unlimited(app):
    """Fail closed: an account with no configured 5-hour cap cannot serve."""
    with app.test_request_context():
        account_id = pool_db.add_account("claude-open", provider="claude")
        row = pool_db.get(account_id)
        assert row["window_limit"] is None
        assert subscription_pool.usable(row) is False
        assert subscription_pool.accounts_in_order("claude") == []


def test_accounts_are_partitioned_by_provider(app):
    with app.test_request_context():
        claude_id = pool_db.add_account("claude-main", provider="claude", window_limit=100_000)
        codex_id = pool_db.add_account("codex-main", provider="codex", window_limit=100_000)
        claude_ids = [row["id"] for row in subscription_pool.accounts_in_order("claude")]
        codex_ids = [row["id"] for row in subscription_pool.accounts_in_order("codex")]
        assert claude_ids == [claude_id] and codex_ids == [codex_id]
        # A codex claim can never take a claude account, even by id.
        assert pool_db.claim(claude_id, "x", provider="codex") is False
        assert pool_db.claim(claude_id, "x", provider="claude") is True


def test_one_account_holds_one_lease(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-lease", provider="claude", window_limit=100_000)
        assert pool_db.claim(account_id, "lease-a", provider="claude") is True
        assert pool_db.claim(account_id, "lease-b", provider="claude") is False
        assert pool_db.leased(account_id) is True
        pool_db.release(account_id, "lease-a")
        assert pool_db.leased(account_id) is False


def test_usage_is_idempotent_on_the_lease(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-usage", provider="claude", window_limit=100_000)
        subscription_pool.record_usage(account_id, 30, 70, provider="claude", family="opus", lease_id="L1")
        subscription_pool.record_usage(account_id, 30, 70, provider="claude", family="opus", lease_id="L1")
        assert pool_db.get(account_id)["window_used"] == 100  # the repeat was ignored


def _observe(account_id, **fields):
    """Publish one fresh provider observation for an account (fractions left of each allowance)."""
    report = {"available": True, "source": "claude_code", "observed_at": time.time(), "window_left": 1.0,
              "weekly_left": 1.0, **fields}
    subscription_pool.register_site_reporter("claude", lambda: {"account_reports": {str(account_id): report}})
    subscription_pool.refresh_quota("claude")


def test_capped_share_reaches_zero_exactly_at_the_cap():
    assert subscription_pool._capped(1.0, 70) == 1.0
    assert subscription_pool._capped(0.65, 70) == pytest.approx(0.5)
    assert subscription_pool._capped(0.3, 70) == 0.0
    assert subscription_pool._capped(0.4, None) == 0.4
    assert subscription_pool._capped(None, 70) == 0.0  # a cap on an unreported allowance denies


def test_percentage_caps_stop_routing_at_that_share_of_the_providers_allowance(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-pct", provider="claude", window_limit=100_000,
                                         window_cap_pct=70, weekly_cap_pct=90)
        _observe(account_id, window_left=0.5, weekly_left=0.5)
        assert subscription_pool.usable(pool_db.get(account_id)) is True
        _observe(account_id, window_left=0.25, weekly_left=0.5)  # 75 % of the 5-hour window used
        assert subscription_pool.usable(pool_db.get(account_id)) is False
        _observe(account_id, window_left=0.5, weekly_left=0.05)  # 95 % of the week used
        assert subscription_pool.usable(pool_db.get(account_id)) is False


def test_family_cap_limits_only_that_familys_share(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-fable", provider="claude", window_limit=100_000)
        pool_db.set_scoped_cap(account_id, "fable", cap_pct=60)
        _observe(account_id, model_limits={"fable": {"left": 0.3, "resets_at": None}})  # 70 % of Fable used
        row = pool_db.get(account_id)
        assert subscription_pool.usable(row, family="fable") is False
        assert subscription_pool.usable(row, family="opus") is True


def test_a_percentage_cap_without_provider_observations_never_serves(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-blind", provider="claude", window_limit=100_000, window_cap_pct=70)
        assert subscription_pool.usable(pool_db.get(account_id)) is False


def test_pooled_quota_and_capacity_reflect_the_caps(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-share", provider="claude", window_limit=100_000, window_cap_pct=70)
        _observe(account_id, window_left=0.65)  # 35 % used: half of the capped allowance is left
        assert subscription_pool.pooled_quota("claude")["window_left"] == pytest.approx(0.5)


def test_caps_must_be_whole_percentages(app):
    with app.test_request_context():
        for value in (0, 101, 70.5, True):
            with pytest.raises(ValueError):
                pool_db.add_account(f"claude-bad-{value}", provider="claude", window_limit=1, window_cap_pct=value)


def test_scoped_family_cap_denies_only_its_family(app):
    with app.test_request_context():
        account_id = pool_db.add_account("claude-scoped", provider="claude", window_limit=1_000_000)
        pool_db.set_scoped_cap(account_id, "fable", window_cap=100)
        subscription_pool.record_usage(account_id, 60, 60, provider="claude", family="fable", lease_id="F1")
        row = pool_db.get(account_id)
        assert subscription_pool.usable(row, family="fable") is False  # 120 >= 100 cap
        assert subscription_pool.usable(row, family="opus") is True    # other families unaffected
        assert subscription_pool.usable(row) is True
