"""Routing under pressure: caps stop exactly at their share, bad requests spare the pool, busy accounts are awaited.

An account is rested only for its own faults, an interrupted request still costs allowance, workers do not erase
each other's observations, and a provider with nothing to serve its models stops offering them.
"""

import threading
import time

import pytest

from bananachat.db import catalog, subscription_pool as pool_db
from bananachat.services import model_lifecycle, subscription_pool
from bananachat.services.upstream import Cancelled

NATIVE = {"claude": "claude_code", "codex": "codex"}
MODEL = {"claude": "claude-sonnet-4-5", "codex": "gpt-6.1-sol"}
MESSAGES = [{"role": "user", "content": "Summarise the quarterly figures for the whole team, please."}]


@pytest.fixture(params=subscription_pool.PROVIDERS)
def provider(request, app):
    name = request.param
    subscription_pool.register_site_discovery(name, lambda: [
        {**item, "reasoning": ["low", "medium"], "capabilities": ["completion"]}
        for item in subscription_pool.CURATED[name]])
    subscription_pool.register_site_chat(name, _answers)
    yield name
    subscription_pool.reset_transport()


def _answers(account, model, messages, options):
    yield {"text": "Done."}
    yield {"tokens_in": 10, "tokens_out": 2, "done": True}


def _chat(provider, **kwargs):
    return list(subscription_pool.chat_stream(provider, MODEL[provider], MESSAGES, options={}, **kwargs))


def _observe(provider, reports):
    observed = {str(account_id): {"available": True, "source": NATIVE[provider], "observed_at": time.time(),
                                  "window_left": 1.0, "weekly_left": 1.0, **fields}
                for account_id, fields in reports.items()}
    subscription_pool.register_site_reporter(provider, lambda: {"account_reports": observed})
    return subscription_pool.refresh_quota(provider)[provider]


def test_every_whole_percentage_cap_stops_exactly_at_its_share():
    for percent in range(1, 101):
        left = (100 - percent) / 100  # the figure the usage readers derive from the provider's used percentage
        assert subscription_pool._capped(left, percent) == 0.0, percent
        if percent > 1:
            assert subscription_pool._capped((101 - percent) / 100, percent) > 0.0, percent


def test_a_request_the_adapter_rejects_rests_no_account(app, provider):
    def rejects(account, model, messages, options):
        raise subscription_pool.RequestRejected("The conversation exceeds the connector's input limit.")

    subscription_pool.register_site_chat(provider, rejects)
    with app.test_request_context():
        ids = [pool_db.add_account(f"{provider}-{number}", provider=provider, window_limit=100_000)
               for number in range(2)]
        with pytest.raises(RuntimeError, match="input limit"):
            _chat(provider)
        assert [pool_db.get(account_id)["cooldown_until"] for account_id in ids] == [None, None]
        subscription_pool.register_site_chat(provider, _answers)
        assert _chat(provider)[-1]["done"]


def test_an_account_that_fails_is_rested_and_the_next_one_answers(app, provider):
    def fails_once(account, model, messages, options):
        if account["label"].endswith("-0"):
            raise RuntimeError("provider fault")
        return _answers(account, model, messages, options)

    subscription_pool.register_site_chat(provider, fails_once)
    with app.test_request_context():
        first = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000, priority=5)
        pool_db.add_account(f"{provider}-1", provider=provider, window_limit=100_000)
        assert _chat(provider)[-1]["done"]
        assert pool_db.get(first)["cooldown_until"] is not None


def test_a_request_cancelled_before_any_output_still_uses_allowance(app, provider):
    def cancelled(account, model, messages, options):
        raise Cancelled("The client left.")
        yield  # makes this a generator, as a real transport is

    subscription_pool.register_site_chat(provider, cancelled)
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        with pytest.raises(Cancelled):
            _chat(provider)
        assert pool_db.get(account_id)["window_used"] > 0


def test_a_failed_discovery_is_tried_again_within_seconds(app, provider, monkeypatch):
    calls = []

    def discover():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("metadata busy")
        return [{"name": MODEL[provider], "family": subscription_pool.family_of(provider, MODEL[provider]),
                 "reasoning": [], "capabilities": ["completion"]}]

    subscription_pool.register_site_discovery(provider, discover)
    with app.test_request_context():
        assert not subscription_pool.discovered_models(provider)["ok"]
        cached = subscription_pool._discovery_cache[provider]["at"]
        retry_in = subscription_pool.DISCOVERY_RETRY_SECONDS
        assert time.monotonic() - cached == pytest.approx(subscription_pool.DISCOVERY_CACHE_SECONDS - retry_in, abs=5)
        monkeypatch.setattr(subscription_pool, "DISCOVERY_RETRY_SECONDS", 0)
        subscription_pool._discovery_cache[provider]["at"] = time.monotonic() - subscription_pool.DISCOVERY_CACHE_SECONDS
        assert subscription_pool.discovered_models(provider)["ok"]
        assert len(calls) == 2


def test_one_workers_failed_read_keeps_the_observation_another_worker_just_made(app, provider):
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        _observe(provider, {account_id: {"window_left": 0.8}})
        subscription_pool.register_site_reporter(provider, lambda: {"account_reports": {
            str(account_id): {"available": False, "source": NATIVE[provider]}}})
        subscription_pool.refresh_quota(provider)
        report = subscription_pool.account_reports(provider)[str(account_id)]
        assert report["available"] and report["window_left"] == 0.8
        assert subscription_pool.usable(pool_db.get(account_id))


def test_a_stale_observation_is_not_kept_by_a_later_failed_read(app, provider):
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        _observe(provider, {account_id: {"window_left": 0.8,
                                         "observed_at": time.time() - 2 * subscription_pool.QUOTA_MAX_AGE.total_seconds()}})
        subscription_pool.register_site_reporter(provider, lambda: {"account_reports": {
            str(account_id): {"available": False, "source": NATIVE[provider]}}})
        subscription_pool.refresh_quota(provider)
        assert not subscription_pool.account_reports(provider)[str(account_id)]["available"]
        assert not subscription_pool.usable(pool_db.get(account_id))


def test_a_request_waits_for_the_only_account_to_finish_another_answer(app, provider, monkeypatch):
    monkeypatch.setattr(subscription_pool, "BUSY_POLL_SECONDS", 0.05)
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        assert pool_db.claim(account_id, "busy-with-another-answer", provider=provider)

        def finish():
            with app.app_context():
                pool_db.release(account_id, "busy-with-another-answer")

        threading.Timer(0.3, finish).start()
        assert _chat(provider)[-1]["done"]


def test_a_request_does_not_wait_when_no_account_could_ever_serve_it(app, provider, monkeypatch):
    monkeypatch.setattr(subscription_pool, "BUSY_WAIT_SECONDS", 5)
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=10)
        subscription_pool.record_usage(account_id, 10, 10, provider=provider, lease_id="used-up")
        started = time.monotonic()
        with pytest.raises(subscription_pool.CapacityUnavailable):
            _chat(provider)
        assert time.monotonic() - started < 2


def test_a_wait_gives_up_after_its_limit_when_the_account_stays_busy(app, provider, monkeypatch):
    monkeypatch.setattr(subscription_pool, "BUSY_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(subscription_pool, "BUSY_POLL_SECONDS", 0.05)
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        assert pool_db.claim(account_id, "never-finishes", provider=provider)
        with pytest.raises(subscription_pool.CapacityUnavailable, match="at capacity"):
            _chat(provider)


def test_pooled_weekly_share_is_the_average_not_the_best_account(app, provider):
    with app.test_request_context():
        ids = [pool_db.add_account(f"{provider}-{number}", provider=provider, window_limit=100_000)
               for number in range(2)]
        quota = _observe(provider, {ids[0]: {"weekly_left": 0.9}, ids[1]: {"weekly_left": 0.1}})
        assert quota["weekly_left"] == pytest.approx(0.5)


def test_an_exhausted_account_rests_until_the_providers_own_reset(app, provider):
    def exhausted(account, model, messages, options):
        raise subscription_pool.QuotaExhausted()

    subscription_pool.register_site_chat(provider, exhausted)
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        reset = time.time() + 2 * 3600
        _observe(provider, {account_id: {"window_left": 0.4, "window_resets_at": reset}})
        with pytest.raises(subscription_pool.CapacityUnavailable):
            _chat(provider)
        rested = pool_db.get(account_id)["cooldown_until"]
        assert abs(subscription_pool.db.parse_timestamp(rested).timestamp() - reset) < 2


def test_a_provider_with_no_active_account_stops_offering_its_models_without_retiring_them(app, provider):
    with app.test_request_context():
        account_id = pool_db.add_account(f"{provider}-0", provider=provider, window_limit=100_000)
        subscription_pool.sync_catalog(provider, selected=[MODEL[provider]], source="test")
        assert catalog.get_by_name(MODEL[provider])["backend_available"]
        pool_db.update_account(account_id, status="disabled")
        model_lifecycle._sync_subscriptions({"new": [], "enabled": [], "missing": []}, "test")
        row = catalog.get_by_name(MODEL[provider])
        assert not row["backend_available"] and not row["missing_at"]
        pool_db.update_account(account_id, status="active")
        model_lifecycle._sync_subscriptions({"new": [], "enabled": [], "missing": []}, "test")
        assert catalog.get_by_name(MODEL[provider])["backend_available"]
