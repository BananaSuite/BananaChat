"""Subscription pool controls exercised with declared adapter capabilities, no credentials.

Unknown, zero, stale or failed capacity never means unlimited; a lease is
exclusive and only its owner may rest, quarantine or release the account; and
a request is retried on another account only before anything was emitted.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib
import threading
import time

import pytest

from bananachat import db
from bananachat.db import catalog, limits as limits_db, subscription_pool as pool_db
from bananachat.db import settings as site_settings
from bananachat.services import limits, model_lifecycle, subscription_pool as pool
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError

MODEL = "provider-model-id"
MESSAGES = [{"role": "user", "content": "Explain this."}]
STANDARD = {"claude": "sonnet", "codex": "sol"}


def descriptor(provider, **changes):
    return {"name": MODEL, "family": STANDARD[provider], "display": "Discovered model",
            "reasoning": ["low", "medium", "high", "xhigh", "max"],
            "capabilities": ["completion", "vision"], **changes}


def answer(account, model, messages, options, *, cancel):
    cancel.check()
    yield {"text": "Answer", "tokens_in": 8, "tokens_out": 2, "done": True}


@pytest.fixture(params=pool.PROVIDERS)
def provider(request, app):
    yield request.param
    pool.reset_transport()


@pytest.fixture
def adapter(app, provider):
    pool.register_site_chat(provider, answer)
    pool.register_site_discovery(provider, lambda: [descriptor(provider)])
    yield
    pool.reset_transport()


def add(provider, label="account", **changes):
    return pool_db.add_account(label, provider=provider, window_limit=changes.pop("window_limit", 100_000), **changes)


def stamp(**changes):
    return db.timestamp(pool._now() + timedelta(**changes))


def test_no_adapter_discovery_cannot_enroll_reference_models(app, provider):
    reference = pool.CURATED[provider][0]["name"]
    with app.app_context(), pytest.raises(ValueError, match="discovery"):
        pool.sync_catalog(provider, selected=[reference])
    assert pool.discovered_models(provider)["models"] == []
    assert db.scalar("SELECT COUNT(*) FROM ai_models WHERE backend=?", (provider,)) == 0


def test_actual_family_strictness_and_declared_efforts(app, provider, adapter, make_user):
    user = make_user("subscription-user")
    with app.app_context():
        state = pool.sync_catalog(provider, selected=[MODEL], source="admin")
        model = catalog.get_by_name(MODEL)
        assert state["count"] == 1 and model["enrollment"] == "new"
        assert model["family"] == STANDARD[provider] and model["supports_vision"] == 1
        policy = limits_db.get_model_policy(model)
        assert policy["window_tokens"] == 150_000 and policy["weekly_tokens"] is None
        assert policy["counts_toward_pool"] is False and policy["effort_default"] == "medium"
        assert limits.supported_efforts(model) == ("low", "medium", "high", "extra", "max")
        assert limits.allowed_efforts(user, model) == ("low", "medium")
        ordered = [pool.strict_policy(provider, "opaque", family=pool.PRESET_FAMILIES[provider][preset])
                   for preset in ("heavy", "standard", "light")]
        assert ordered[0]["window_tokens"] < ordered[1]["window_tokens"] < ordered[2]["window_tokens"]
        assert ordered[0]["weight"] > ordered[1]["weight"] > ordered[2]["weight"]
        assert all(item["dynamic"] and item["weekly_tokens"] is None for item in ordered)


@pytest.mark.parametrize("changes", [
    {"name": "<script>"}, {"family": "unknown"}, {"reasoning": ["ultracode"]},
    {"reasoning": ["minimal"]}, {"reasoning": ["none"]},
    {"reasoning": "medium"}, {"capabilities": ["tools"]}, {"account_ids": [True]},
    {"account_ids": [0]}, {"account_ids": "all"},
])
def test_invalid_discovery_fails_closed_without_exception_details(app, provider, adapter, changes):
    pool.register_site_discovery(provider, lambda: [descriptor(provider, **changes)])
    state = pool.discovered_models(provider, force=True)
    assert state["ok"] is False and state["models"] == []
    with pytest.raises(ValueError, match="discovery failed"):
        pool.sync_catalog(provider, selected=[MODEL])
    assert catalog.get_by_name(MODEL) is None


def test_another_providers_family_is_not_a_declared_family(app, provider, adapter):
    foreign = "astra" if provider == "claude" else "opus"
    pool.register_site_discovery(provider, lambda: [descriptor(provider, family=foreign)])
    assert pool.discovered_models(provider, force=True)["ok"] is False


@pytest.mark.parametrize("count", [2, 201])
def test_duplicate_or_unbounded_discovery_fails_closed(app, provider, adapter, count):
    pool.register_site_discovery(provider, lambda: [descriptor(provider)] * count)
    assert pool.discovered_models(provider, force=True)["ok"] is False


def test_automatic_publication_rolls_back_when_strict_policy_cannot_be_stored(app, provider, adapter, monkeypatch):
    with app.app_context():
        model_lifecycle.save_policy({"enrollment": "automatic"})
        def fail(*args, **kwargs):
            raise RuntimeError("storage unavailable")
        monkeypatch.setattr(limits_db, "set_model_policy", fail)
        with pytest.raises(RuntimeError, match="storage"):
            pool.sync_catalog(provider, selected=[MODEL], source="admin")
        assert catalog.get_by_name(MODEL) is None
        assert site_settings.state_get(f"{provider}.selected_models") is None
        assert db.scalar("SELECT COUNT(*) FROM model_lifecycle_events WHERE model_name=?", (MODEL,)) == 0


def test_selection_and_ignore_rules_survive_background_discovery(app, provider, adapter):
    with app.app_context():
        model_lifecycle.save_policy({"enrollment": "automatic"})
        state = pool.sync_catalog(provider, selected=[MODEL], source="admin")
        assert state["enabled"] == [MODEL]
        assert catalog.get_by_name(MODEL)["is_rolled_out"] == 1
        pool.sync_catalog(provider, selected=[], source="admin")
        pool.sync_catalog(provider, source="background")
        assert catalog.get_by_name(MODEL)["backend_available"] == 0
        assert site_settings.state_get(f"{provider}.selected_models") == []
        model_lifecycle.add_ignore_rule(MODEL)
        pool.sync_catalog(provider, selected=[MODEL], source="admin")
        row = catalog.get_by_name(MODEL)
        assert row["enrollment"] == "ignored" and row["is_rolled_out"] == 0


def test_failed_discovery_preserves_catalog_but_blocks_capacity(app, provider, adapter, warnings_log):
    with app.app_context():
        pool.sync_catalog(provider, selected=[MODEL])
        before = dict(catalog.get_by_name(MODEL))
        add(provider)
        def fail():
            raise RuntimeError("provider-secret")
        pool.register_site_discovery(provider, fail)
        with pytest.raises(ValueError) as error:
            pool.sync_catalog(provider, selected=[MODEL])
        assert "provider-secret" not in str(error.value)
        assert "model discovery failed (RuntimeError)" in warnings_log.text
        assert "provider-secret" not in warnings_log.text
        assert dict(catalog.get_by_name(MODEL)) == before
        assert pool.capacity_report(catalog.get_by_name(MODEL)).window_left == 0
        with pytest.raises(RuntimeError, match="reported this model"):
            list(pool.chat_stream(provider, MODEL, MESSAGES))


@pytest.mark.parametrize("window,weekly,usable", [(None, None, False), (0, None, False),
                                                  (100, None, True), (100, 0, False), (100, 200, True)])
def test_unknown_or_zero_budgets_never_mean_unlimited(app, provider, adapter, window, weekly, usable):
    account = add(provider, window_limit=window, weekly_limit=weekly)
    assert pool.usable(pool_db.get(account)) is usable
    assert (pool.pick_account(provider) is not None) is usable


def test_provider_snapshots_expire_and_cannot_assume_a_new_allowance(app, provider, adapter):
    account = add(provider)
    pool_db.report_quota(account, window_used=90_000, window_resets_at=stamp(hours=1))
    assert pool.usable(pool_db.get(account))
    db.execute("UPDATE subscription_accounts SET quota_window_updated_at=? WHERE id=?",
               (stamp(minutes=-16), account))
    assert not pool.usable(pool_db.get(account))
    pool_db.report_quota(account, window_used=90_000, window_resets_at=stamp(minutes=-1))
    assert not pool.usable(pool_db.get(account))
    assert pool._current(pool_db.get(account))[0] == 90_000
    pool_db.report_quota(account, window_used=0, window_resets_at=stamp(hours=5))
    assert pool.usable(pool_db.get(account))


def test_failed_snapshot_never_refreshes_usage_or_reactivates_disabled_account(app, provider, adapter):
    account = add(provider)
    pool_db.report_quota(account, window_used=90_000)
    before = pool_db.get(account)
    pool_db.update_account(account, status="disabled")
    pool_db.report_quota(account, window_used=0, error="Report unavailable")
    row = pool_db.get(account)
    assert row["status"] == "disabled" and row["window_used"] == 90_000
    assert row["quota_updated_at"] == before["quota_updated_at"]
    pool_db.report_quota(account, window_used=0)
    assert pool_db.get(account)["status"] == "disabled"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -0.1, 1.1, "0.5"])
def test_invalid_aggregate_quota_blocks_actual_transport(app, provider, adapter, value):
    add(provider)
    pool.register_site_reporter(provider, lambda: {"window_left": value})
    assert pool.refresh_quota(provider)[provider]["reporter_error"]
    with pytest.raises(pool.CapacityUnavailable, match="usage is unavailable"):
        list(pool.chat_stream(provider, MODEL, MESSAGES))
    assert db.scalar("SELECT COUNT(*) FROM subscription_usage") == 0


def test_aggregate_failure_blocks_but_report_cannot_raise_local_capacity(app, provider, adapter, warnings_log):
    account = add(provider)
    pool_db.report_quota(account, window_used=80_000)
    pool.register_site_reporter(provider, lambda: {"window_left": 1.0})
    assert pool.refresh_quota(provider)[provider]["window_left"] == 0.2
    def fail():
        raise RuntimeError("provider-secret")
    pool.register_site_reporter(provider, fail)
    with pytest.raises(pool.CapacityUnavailable, match="usage is unavailable"):
        list(pool.chat_stream(provider, MODEL, MESSAGES))
    assert "pool quota failed (RuntimeError)" in warnings_log.text
    assert "provider-secret" not in warnings_log.text


def test_model_account_restriction_controls_capacity_and_routing(app, provider, adapter):
    unavailable = add(provider, "restricted", window_limit=0)
    add(provider, "other")
    pool.register_site_discovery(provider, lambda: [descriptor(provider, account_ids=[unavailable])])
    pool.sync_catalog(provider, selected=[MODEL])
    model = catalog.get_by_name(MODEL)
    assert pool.capacity_report(model).window_left == 0
    with pytest.raises(pool.CapacityUnavailable, match="currently at capacity"):
        list(pool.chat_stream(provider, MODEL, MESSAGES))
    pool_db.update_account(unavailable, window_limit=100_000)
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        yield {"text": "Restricted answer", "done": True}
    pool.register_site_chat(provider, handler)
    list(pool.chat_stream(provider, MODEL, MESSAGES))
    assert seen == [unavailable]


def test_account_claim_is_exclusive_and_expired_owner_cannot_release_replacement(app, provider, adapter):
    account = add(provider)
    now = time.time()
    assert pool_db.claim(account, "old", provider=provider, now=now)
    assert not pool_db.claim(account, "other", provider=provider, now=now)
    assert pool_db.renew(account, "old", now=now + 10)
    assert not pool_db.renew(account, "old", now=now + 200)
    assert pool_db.claim(account, "new", provider=provider, now=now + 200)
    pool_db.release(account, "old")
    assert pool_db.owns(account, "new", now=now + 201)
    pool_db.update_account(account, status="disabled")
    assert not pool_db.renew(account, "new", now=now + 202)


def test_parallel_requests_use_distinct_accounts_and_keep_leases_through_transport_close(app, provider, adapter,
                                                                                         monkeypatch):
    # A request that finds every account busy waits for one, but only briefly in this test.
    monkeypatch.setattr(pool, "BUSY_WAIT_SECONDS", 0.2)
    accounts = {add(provider, "first"), add(provider, "second")}
    entered = threading.Barrier(3)
    release = threading.Event()
    closed = []
    lock = threading.Lock()
    def handler(account, model, messages, options, *, cancel):
        try:
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
            cancel.check()
            yield {"text": "Answer", "done": True}
        finally:
            with lock:
                closed.append((account["id"], pool_db.leased(account["id"])))
    pool.register_site_chat(provider, handler)
    def worker():
        try:
            with app.app_context():
                return list(pool.chat_stream(provider, MODEL, MESSAGES))
        finally:
            db.close_thread_connection()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        try:
            entered.wait(timeout=5)
            assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 2
            with pytest.raises(pool.CapacityUnavailable, match="currently at capacity"):
                list(pool.chat_stream(provider, MODEL, MESSAGES))
        finally:
            release.set()
        assert all(future.result(timeout=5)[-1]["done"] for future in futures)
    assert {account for account, _ in closed} == accounts
    assert all(leased for _, leased in closed)
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


@pytest.mark.parametrize("partial", [{"text": "Visible"}, {"thinking": "Reasoning"}])
def test_partial_output_prevents_account_retry_and_secret_leak(app, provider, adapter, warnings_log, partial):
    first = add(provider, "first", priority=10)
    add(provider, "second")
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        yield partial
        raise RuntimeError("provider-secret")
    pool.register_site_chat(provider, handler)
    stream = pool.chat_stream(provider, MODEL, MESSAGES)
    assert next(stream)[next(iter(partial))] == next(iter(partial.values()))
    with pytest.raises(RuntimeError) as error:
        next(stream)
    assert "provider-secret" not in str(error.value)
    assert f"account {first} failed (RuntimeError)" in warnings_log.text
    assert "provider-secret" not in warnings_log.text
    assert seen == [first]
    assert pool_db.get(first)["window_used"] > 0
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


def test_pre_output_quota_failure_can_retry_with_required_cancel_keyword(app, provider, adapter):
    first = add(provider, "first", priority=10)
    second = add(provider, "second")
    cancel = CancelToken()
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append((account["id"], cancel, options.get("effort")))
        if account["id"] == first:
            raise pool.QuotaExhausted(resets_at=pool._now() + timedelta(hours=2))
        yield {"text": "Final", "tokens_in": 7}
        yield {"tokens_out": 3, "done": True}
    pool.register_site_chat(provider, handler)
    chunks = list(pool.chat_stream(provider, MODEL, MESSAGES, options={"effort": "xhigh"}, cancel=cancel))
    assert chunks[-1]["done"] and seen == [(first, cancel, "extra"), (second, cancel, "extra")]
    assert pool_db.get(second)["window_used"] == 10 and pool._resting(pool_db.get(first))


@pytest.mark.parametrize("record", [[], {"text": 4}, {"thinking": None}, {"tokens_in": True},
                                     {"tokens_out": -1}, {"done": "true"}, {"finish_reason": "invalid"},
                                     {"error": "provider-secret"}])
def test_invalid_stream_records_never_complete(app, provider, adapter, warnings_log, record):
    add(provider)
    pool.register_site_chat(provider, lambda *args, **kwargs: iter([record]))
    with pytest.raises(UpstreamError) as error:
        list(pool.stream_chunks(provider, MODEL, MESSAGES))
    assert "provider-secret" not in str(error.value)
    assert "failed (_StreamError)" in warnings_log.text and "provider-secret" not in warnings_log.text
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


def test_missing_terminal_is_failure_and_terminal_content_and_zero_usage_are_preserved(app, provider, adapter):
    first = add(provider)
    pool.register_site_chat(provider, lambda *args, **kwargs: iter([{"text": "Partial"}]))
    stream = pool.stream_chunks(provider, MODEL, MESSAGES)
    assert next(stream).content == "Partial"
    with pytest.raises(UpstreamError, match="before finishing"):
        next(stream)
    pool_db.update_account(first, status="disabled")
    add(provider, "second")
    pool.register_site_chat(provider, lambda *args, **kwargs: iter([{"text": "Final", "thinking": "Thought",
                                                                     "tokens_in": 0, "tokens_out": 0,
                                                                     "done": True}]))
    result = list(pool.stream_chunks(provider, MODEL, MESSAGES))
    assert len(result) == 1 and result[0].content == "Final" and result[0].thinking == "Thought"
    assert result[0].done and result[0].prompt_tokens == result[0].completion_tokens == 0


def test_cancel_and_generator_close_settle_once_and_release_after_provider_close(app, provider, adapter):
    account = add(provider)
    cancel = CancelToken()
    cleanup = []
    def handler(account, model, messages, options, *, cancel):
        try:
            yield {"text": "Partial", "tokens_in": 5, "tokens_out": 2}
            cancel.check()
            yield {"done": True}
        finally:
            cleanup.append(pool_db.leased(account["id"]))
    pool.register_site_chat(provider, handler)
    stream = pool.chat_stream(provider, MODEL, MESSAGES, cancel=cancel)
    next(stream)
    cancel.cancel("stopped")
    with pytest.raises(Cancelled):
        next(stream)
    stream.close()
    assert cleanup == [True] and pool_db.get(account)["window_used"] == 7
    assert not pool._resting(pool_db.get(account))
    assert db.scalar("SELECT COUNT(*) FROM subscription_usage") == 1
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


def test_usage_is_idempotent_and_reported_counter_without_reset_is_never_discarded(app, provider, adapter):
    account = add(provider)
    pool_db.report_quota(account, window_used=90, weekly_used=100)
    pool.record_usage(account, 8, 4, provider=provider, lease_id="settlement")
    pool.record_usage(account, 8, 4, provider=provider, lease_id="settlement")
    row = pool_db.get(account)
    assert row["window_used"] == 102 and row["weekly_used"] == 112
    assert row["window_resets_at"] is None and row["weekly_resets_at"] is None
    assert db.scalar("SELECT COUNT(*) FROM subscription_usage") == 1


def test_accounting_failure_never_publishes_terminal_or_retries_another_account(app, provider, adapter, monkeypatch):
    first = add(provider, "first", priority=10)
    add(provider, "second")
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        yield {"text": "Answer", "tokens_in": 3, "tokens_out": 2, "done": True}
    def fail(*args, **kwargs):
        raise RuntimeError("storage-secret")
    pool.register_site_chat(provider, handler)
    monkeypatch.setattr(pool, "record_usage", fail)
    with pytest.raises(RuntimeError, match="could not be recorded") as error:
        list(pool.chat_stream(provider, MODEL, MESSAGES))
    assert "storage-secret" not in str(error.value) and seen == [first]
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


def test_output_and_token_totals_are_bounded(app, provider, adapter, monkeypatch):
    add(provider)
    monkeypatch.setattr(pool, "MAX_STREAM_BYTES", 10)
    pool.register_site_chat(provider, lambda *args, **kwargs: iter([{"text": "First"}, {"text": "Too much"},
                                                                     {"done": True}]))
    stream = pool.chat_stream(provider, MODEL, MESSAGES)
    assert next(stream)["text"] == "First"
    with pytest.raises(RuntimeError, match="output limit"):
        next(stream)
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0


def test_migration_is_additive_and_rerunnable_without_altering_current_local_budget(app, provider, adapter):
    account = add(provider)
    pool.record_usage(account, 4, 2, provider=provider)
    before = dict(pool_db.get(account))
    migration = importlib.import_module("bananachat.db.migrations.v26_subscription_pool")
    migration.upgrade(db.conn())
    migration.upgrade(db.conn())
    assert dict(pool_db.get(account)) == before


@pytest.mark.parametrize("report", [{"weekly_used": 0}, {"window_resets_at": "2099-01-01 00:00:00"},
                                     {"window_limit": 200_000}])
def test_partial_report_cannot_refresh_another_or_missing_usage_snapshot(app, provider, adapter, report):
    account = add(provider)
    pool_db.report_quota(account, window_used=20_000, window_resets_at=stamp(hours=1))
    old = stamp(minutes=-20)
    db.execute("UPDATE subscription_accounts SET quota_window_updated_at=? WHERE id=?", (old, account))
    pool_db.report_quota(account, **report)
    assert pool_db.get(account)["quota_window_updated_at"] == old
    assert not pool.quota_fresh(pool_db.get(account))
    with pytest.raises(pool.CapacityUnavailable, match="usage is unavailable"):
        list(pool.chat_stream(provider, MODEL, MESSAGES))


def test_weekly_scope_requires_its_own_fresh_snapshot_when_enabled(app, provider, adapter):
    account = add(provider, weekly_limit=500_000)
    pool_db.report_quota(account, window_used=0)
    assert not pool.quota_fresh(pool_db.get(account))
    pool_db.report_quota(account, weekly_used=0)
    assert pool.quota_fresh(pool_db.get(account))
    db.execute("UPDATE subscription_accounts SET quota_weekly_updated_at=? WHERE id=?",
               (stamp(minutes=-20), account))
    pool_db.report_quota(account, window_used=1)
    assert not pool.quota_fresh(pool_db.get(account))


def test_stream_chunk_wrapper_preserves_user_cancellation(app, provider, adapter):
    account = add(provider)
    cancel = CancelToken()
    def handler(account, model, messages, options, *, cancel):
        yield {"text": "Partial"}
        cancel.check()
        yield {"done": True}
    pool.register_site_chat(provider, handler)
    stream = pool.stream_chunks(provider, MODEL, MESSAGES, cancel=cancel)
    assert next(stream).content == "Partial"
    cancel.cancel("stopped")
    with pytest.raises(Cancelled):
        next(stream)
    assert not pool._resting(pool_db.get(account))
    assert not pool_db.leased(account)


@pytest.mark.parametrize("reasoning,expected", [(["low", "medium", "high"], "medium"),
                                                 (["low"], "low"), (["off", "on"], "on")])
def test_boolean_thinking_maps_only_to_declared_model_capabilities(app, provider, adapter, reasoning, expected):
    add(provider)
    pool.register_site_discovery(provider, lambda: [descriptor(provider, reasoning=reasoning)])
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(options["effort"])
        yield {"text": "Final", "done": True}
    pool.register_site_chat(provider, handler)
    assert list(pool.stream_chunks(provider, MODEL, MESSAGES, think=True))[-1].done
    assert seen == [expected]


def test_account_with_optional_weekly_budget_can_serve_when_another_weekly_budget_is_exhausted(
        app, provider, adapter):
    exhausted = add(provider, "weekly-exhausted", weekly_limit=100)
    db.execute("UPDATE subscription_accounts SET weekly_used=100 WHERE id=?", (exhausted,))
    ready = add(provider, "weekly-optional")
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        yield {"text": "Answer", "done": True}
    pool.register_site_chat(provider, handler)
    assert list(pool.chat_stream(provider, MODEL, MESSAGES))[-1]["done"]
    assert seen == [ready]


@pytest.mark.parametrize("failure", [RuntimeError("old request failed"), pool.QuotaExhausted()])
def test_stale_worker_exception_does_not_rest_the_replacement_owner(app, provider, adapter, failure):
    account = add(provider)
    def handler(account, model, messages, options, *, cancel):
        db.execute("UPDATE subscription_account_leases SET expires_at=0 WHERE account_id=?", (account["id"],))
        assert pool_db.claim(account["id"], "replacement", provider=provider)
        raise failure
        yield  # make the callback a generator
    pool.register_site_chat(provider, handler)
    with pytest.raises(Cancelled, match="lease lost"):
        list(pool.chat_stream(provider, MODEL, MESSAGES))
    assert pool_db.owns(account, "replacement")
    assert not pool._resting(pool_db.get(account))
    pool_db.release(account, "replacement")


def test_cancelled_transport_runtime_error_cannot_trigger_rest_or_retry(app, provider, adapter):
    account = add(provider, "first", priority=10)
    add(provider, "second")
    cancel = CancelToken()
    seen = []
    def handler(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        cancel.cancel("stopped")
        raise RuntimeError("socket closed after stop")
        yield
    pool.register_site_chat(provider, handler)
    with pytest.raises(Cancelled):
        list(pool.chat_stream(provider, MODEL, MESSAGES, cancel=cancel))
    assert seen == [account] and not pool._resting(pool_db.get(account))


def test_stale_transport_close_failure_does_not_disable_replacement_owner(app, provider, adapter):
    account = add(provider)
    class Transport:
        def __iter__(self):
            return self
        def __next__(self):
            return {"text": "Partial"}
        def close(self):
            raise RuntimeError("old socket could not close")
    pool.register_site_chat(provider, lambda *args, **kwargs: Transport())
    stream = pool.chat_stream(provider, MODEL, MESSAGES)
    next(stream)
    db.execute("UPDATE subscription_account_leases SET expires_at=0 WHERE account_id=?", (account,))
    assert pool_db.claim(account, "replacement", provider=provider)
    stream.close()
    assert pool_db.get(account)["status"] == "active"
    assert pool_db.owns(account, "replacement")
    assert not pool._resting(pool_db.get(account))
    assert pool_db.get(account)["window_used"] > 0
    pool_db.release(account, "replacement")


def test_lease_guarded_cooldown_is_an_atomic_noop_for_an_old_owner(app, provider, adapter):
    account = add(provider)
    assert pool_db.claim(account, "replacement", provider=provider)
    assert not pool.rest(account, pool._now() + timedelta(minutes=5), "Old failure", lease_id="old")
    assert not pool._resting(pool_db.get(account))
    assert not pool_db.quarantine(account, "old")
    assert pool_db.get(account)["status"] == "active"
    pool_db.release(account, "replacement")
