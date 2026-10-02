"""Subscription telemetry stays distinct from locally configured token budgets."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bananachat.services import claude_usage as usage
from bananachat.services.upstream import Cancelled, CancelToken

NOW = 1_790_940_000


def window(used, seconds=3600):
    return {"utilization": used, "resets_at": datetime.fromtimestamp(NOW + seconds, timezone.utc).isoformat()}


def response(**extra):
    return {"subscription_type": "max", "rate_limits_available": True,
            "rate_limits": {"five_hour": window(83), "seven_day": window(14, 604800)}, **extra}


@pytest.fixture(autouse=True)
def no_shared_cache():
    usage._cache.clear()
    usage._flights.clear()
    usage._attempts.clear()
    yield
    usage._cache.clear()
    usage._flights.clear()
    usage._attempts.clear()


def test_report_omits_identity_raw_bodies_local_history_and_spend():
    value = response(email="private@example.test", organization="secret-identity",
                     session={"tokens": 777}, behaviors={"private": "local-transcripts"})
    value["rate_limits"].update(extra_usage={"monthly_limit": 900}, malicious="raw-error-body",
                                seven_day_opus=window(97, 604800), seven_day_sonnet=window(10, 604800),
                                model_scoped=[{"display_name": "Fable", **window(40, 604800)}])
    report = usage.parse(value, observed_at=NOW, now=NOW)
    assert report["subscription_type"] == "max"
    assert report["plan_label"] == "Max"
    assert report["window_left"] == .17
    assert report["weekly_left"] == .86
    assert report["model_limits"]["fable"]["left"] == .6
    for sensitive in ("private@example.test", "secret-identity", "local-transcripts", "raw-error-body", "monthly_limit"):
        assert sensitive not in json.dumps(report)
    assert usage.for_model(report, "claude-opus-4", now=NOW)["weekly_left"] == .03
    assert usage.for_model(report, "claude-sonnet-4", now=NOW)["weekly_left"] == .86
    assert usage.for_model(report, "claude-fable-5", now=NOW)["weekly_left"] == .6


def test_model_buckets_match_words_and_expire_only_matching_model():
    value = response()
    value["rate_limits"]["model_scoped"] = [{"display_name": "Sonnet", **window(99, 5)}]
    report = usage.parse(value, observed_at=NOW, now=NOW)
    assert usage.for_model(report, "claude-sonnet-4", now=NOW)["weekly_left"] == .01
    assert usage.for_model(report, "claude-sonnetish-4", now=NOW)["weekly_left"] == .86
    assert usage.fresh(report, now=NOW + 5)
    assert usage.for_model(report, "claude-sonnet-4", now=NOW + 5)["weekly_left"] == 0
    assert usage.for_model(report, "claude-opus-4", now=NOW + 5)["weekly_left"] == .86


def test_verified_mythos_scope_uses_existing_fable_family_alias():
    value = response()
    value["rate_limits"]["model_scoped"] = [{"display_name": "Claude Mythos 5.1", **window(98, 604800)}]
    report = usage.parse(value, observed_at=NOW, now=NOW)
    assert report["model_limits"]["fable"]["left"] == .02
    assert usage.for_model(report, "claude-mythos-5-1", now=NOW)["weekly_left"] == .02
    assert usage.for_model(report, "claude-fable-5-1", now=NOW)["weekly_left"] == .02
    assert usage.for_model(report, "claude-sonnet-4", now=NOW)["weekly_left"] == .86


def test_absent_weekly_and_unverified_multiplier_remain_unknown():
    value = response()
    value["rate_limits"]["seven_day"] = None
    report = usage.parse(value, observed_at=NOW, now=NOW, plan_label="Max 20x (unverified)")
    assert report["weekly_left"] is None
    assert report["plan_label"] == "Max"
    assert usage.parse(value, observed_at=NOW, now=NOW, plan_label="Max 20x")["plan_label"] == "Max 20x"


@pytest.mark.parametrize("value", [None, True, 1, {"raw": "provider-message"}])
def test_invalid_response_fails_without_returning_body(value):
    with pytest.raises(usage.UsageError):
        usage.parse(value, observed_at=NOW, now=NOW)


@pytest.mark.parametrize("bad", [True, -1, 101, 10 ** 1000, float("nan"), float("inf"), "40", []])
@pytest.mark.parametrize("allow_stale", [False, True])
def test_invalid_percentages_fail_closed(bad, allow_stale):
    value = response()
    value["rate_limits"]["five_hour"]["utilization"] = bad
    with pytest.raises(usage.UsageError, match="percentage"):
        usage.parse(value, observed_at=NOW, now=NOW, allow_stale=allow_stale)


@pytest.mark.parametrize("stamp", [NOW - 901, NOW + 1, float("nan"), True, "now"])
def test_observation_age_cannot_unlock_capacity(stamp):
    with pytest.raises(usage.UsageError, match="stale"):
        usage.parse(response(), observed_at=stamp, now=NOW)


@pytest.mark.parametrize("reset", ["2026-10-02", "not a date", True, 99, "2026-10-02T13:00:00"])
def test_invalid_reset_times_fail_closed(reset):
    value = response()
    value["rate_limits"]["five_hour"]["resets_at"] = reset
    with pytest.raises(usage.UsageError, match="reset time"):
        usage.parse(value, observed_at=NOW, now=NOW)


def test_provider_reset_and_age_invalidate_general_record():
    value = response()
    value["rate_limits"]["five_hour"] = window(10, 10)
    report = usage.parse(value, observed_at=NOW, now=NOW)
    assert usage.fresh(report, now=NOW + 9)
    assert not usage.fresh(report, now=NOW + 10)
    assert usage.for_model(report, "sonnet", now=NOW + 10) == {"window_left": 0, "weekly_left": 0}
    with pytest.raises(usage.UsageError, match="after its reset"):
        usage.parse(value, observed_at=NOW, now=NOW + 10)
    assert not usage.fresh(usage.parse(response(), observed_at=NOW, now=NOW), now=NOW + 901)


def test_unknown_availability_does_not_claim_capacity():
    for value in (response(rate_limits=None), response(rate_limits_available=False),
                  response(rate_limits={"five_hour": None})):
        report = usage.parse(value, observed_at=NOW, now=NOW)
        assert report["available"] is False
        assert report["window_left"] is None
        assert not usage.fresh(report, now=NOW)


def test_oauth_scope_and_weekly_caps_binding_without_extra_spend():
    value = response()
    value["rate_limits"].update(seven_day_oauth_apps=window(95, 604800),
                                extra_usage={"is_enabled": True, "monthly_limit": 999999999})
    report = usage.parse(value, observed_at=NOW, now=NOW)
    assert report["weekly_left"] == .05
    assert usage.for_model(report, "haiku", now=NOW)["weekly_left"] == .05


def test_malformed_model_lists_or_plan_fail_safely():
    for value in (response(subscription_type=[]), response(rate_limits_available="yes")):
        with pytest.raises(usage.UsageError):
            usage.parse(value, observed_at=NOW, now=NOW)
    for scoped in ([None], [{"display_name": "secret\nerror", **window(10)}], {}, [window(10)] * 201):
        value = response()
        value["rate_limits"]["model_scoped"] = scoped
        with pytest.raises(usage.UsageError, match="model quota"):
            usage.parse(value, observed_at=NOW, now=NOW)


def cache_file(profile, *, stamp=NOW, plan="Max 20x", profile_stamp=NOW):
    path = profile["config_dir"] / ".claude.json"
    path.write_text(json.dumps({"cachedUsageUtilization": {"fetchedAtMs": stamp * 1000},
                               "oauthAccount": {"planDisplayName": plan, "profileFetchedAt": profile_stamp * 1000,
                                                "email": "never-expose@example.test"}}))
    path.chmod(0o600)
    return path


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    profile = {"home": str(tmp_path), "config_dir": config}
    cache_file(profile)
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    calls = []

    def request(adapter, selected, subtype, **kwargs):
        calls.append((subtype, kwargs))
        return response()

    monkeypatch.setattr(usage.claude_control, "request", request)
    return SimpleNamespace(binary="/verified/claude"), profile, calls


def test_native_cache_timestamp_plan_and_private_result_copy(metadata):
    adapter, profile, calls = metadata
    report = usage.read(adapter, profile)
    assert calls[0][0] == "get_usage"
    assert report["observed_at"] == NOW
    assert report["plan_label"] == "Max 20x"
    assert "never-expose" not in json.dumps(report)
    report["window_left"] = 1
    assert usage.read(adapter, profile)["window_left"] == .17
    assert len(calls) == 1


def test_old_native_snapshot_is_not_reaged_when_rpc_succeeds(metadata):
    adapter, profile, _ = metadata
    cache_file(profile, stamp=NOW - 901)
    report = usage.read(adapter, profile, force=True)
    assert report["observed_at"] == NOW - 901
    assert report["available"] is False and report["status"] == "stale"
    assert report["plan_label"] == "Max 20x"
    assert report["window_left"] == .17 and report["weekly_left"] == .86
    assert not usage.fresh(report)
    assert usage.for_model(report, "sonnet") == {"window_left": 0, "weekly_left": 0}


def test_failed_refresh_discards_previous_capacity(metadata, monkeypatch):
    adapter, profile, _ = metadata
    usage.read(adapter, profile)

    def fail(*args, **kwargs):
        raise usage.claude_control.ControlError("Metadata unavailable.")

    monkeypatch.setattr(usage.claude_control, "request", fail)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 60)
    with pytest.raises(usage.claude_control.ControlError):
        usage.read(adapter, profile, force=True)
    assert not usage._cache


def test_missing_cache_never_reads_auth_store(metadata):
    adapter, profile, _ = metadata
    (profile["config_dir"] / ".claude.json").unlink()
    (profile["config_dir"] / ".credentials.json").write_text("must-not-be-opened")
    with pytest.raises(usage.UsageError, match="freshness could not be verified"):
        usage.read(adapter, profile, force=True)


@pytest.mark.parametrize("scenario", ["public", "symlink", "fifo", "oversize", "invalid"])
def test_private_cache_validation(metadata, scenario, tmp_path):
    adapter, profile, _ = metadata
    path = profile["config_dir"] / ".claude.json"
    if scenario == "public":
        path.chmod(0o644)
    elif scenario == "symlink":
        alternate = tmp_path / "target.json"
        path.rename(alternate)
        path.symlink_to(alternate)
    elif scenario == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif scenario == "oversize":
        with path.open("r+b") as dest:
            dest.truncate(usage.MAX_CONFIG_BYTES + 1)
    else:
        path.write_text("invalid json")
    with pytest.raises(usage.UsageError):
        usage.read(adapter, profile, force=True)


def test_old_unknown_plan_does_not_claim_multiplier(metadata):
    adapter, profile, _ = metadata
    cache_file(profile, plan="Max 20x", profile_stamp=NOW - 86401)
    assert usage.read(adapter, profile)["plan_label"] == "Max"
    cache_file(profile, plan={"private": "identity"})
    usage.invalidate(adapter, profile)
    assert usage.read(adapter, profile, force=True)["plan_label"] == "Max"


def test_cancel_before_rpc_or_cached_capacity(metadata):
    adapter, profile, calls = metadata
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        usage.read(adapter, profile, cancel=token)
    assert not calls


def test_refresh_retains_actual_observation_age(metadata, monkeypatch):
    adapter, profile, calls = metadata
    usage.read(adapter, profile)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 61)
    assert usage.read(adapter, profile)["observed_at"] == NOW
    assert len(calls) == 2
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 901)
    report = usage.read(adapter, profile)
    assert report["observed_at"] == NOW
    assert report["available"] is False and report["status"] == "stale"


def test_concurrent_cold_reads_share_one_native_usage_request(metadata, monkeypatch):
    adapter, profile, _ = metadata
    barrier = threading.Barrier(9)
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        time.sleep(.15)
        return response()

    def worker():
        barrier.wait(timeout=2)
        return usage.read(adapter, profile)

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(8)]
        barrier.wait(timeout=2)
        reports = [future.result(timeout=2) for future in futures]
    assert len(calls) == 1
    assert all(report["window_left"] == .17 for report in reports)
    reports[0]["window_left"] = 1
    assert reports[1]["window_left"] == .17
    assert not usage._flights


def test_cancelling_waiter_does_not_cancel_profile_refresh(metadata, monkeypatch):
    adapter, profile, _ = metadata
    entered, release, waiter_started = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return response()

    token = CancelToken()

    def waiter():
        waiter_started.set()
        return usage.read(adapter, profile, cancel=token)

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(usage.read, adapter, profile)
        assert entered.wait(timeout=1)
        follower = executor.submit(waiter)
        assert waiter_started.wait(timeout=1)
        token.cancel()
        try:
            with pytest.raises(Cancelled):
                follower.result(timeout=1)
        finally:
            release.set()
        assert leader.result(timeout=1)["available"]
    assert len(calls) == 1


def test_blocked_profile_does_not_block_other_subscription(metadata, monkeypatch, tmp_path):
    adapter, profile, _ = metadata
    config = tmp_path / "second-config"
    config.mkdir(mode=0o700)
    other = {"home": str(tmp_path / "second-home"), "config_dir": config}
    cache_file(other)
    entered, release = threading.Event(), threading.Event()

    def request(adapter, selected, *args, **kwargs):
        if selected is profile:
            entered.set()
            assert release.wait(timeout=2)
        return response()

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(usage.read, adapter, profile)
        assert entered.wait(timeout=1)
        healthy = executor.submit(usage.read, adapter, other)
        try:
            assert healthy.result(timeout=1)["window_left"] == .17
            assert not leader.done()
        finally:
            release.set()
        assert leader.result(timeout=1)["available"]


def test_shared_refresh_failure_never_reuses_old_capacity(metadata, monkeypatch):
    adapter, profile, _ = metadata
    usage.read(adapter, profile)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 60)
    barrier = threading.Barrier(9)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        time.sleep(.15)
        raise usage.claude_control.ControlError("Metadata unavailable.")

    def worker():
        barrier.wait(timeout=2)
        return usage.read(adapter, profile, force=True)

    monkeypatch.setattr(usage.claude_control, "request", fail)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(8)]
        barrier.wait(timeout=2)
        for future in futures:
            with pytest.raises((usage.UsageError, usage.claude_control.ControlError)):
                future.result(timeout=2)
    assert len(calls) == 1
    assert not usage._cache and not usage._flights
    monkeypatch.setattr(usage.claude_control, "request", lambda *args, **kwargs: response())
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 120)
    assert usage.read(adapter, profile)["available"]


def test_cancelled_leader_allows_uncancelled_waiter_to_retry(metadata, monkeypatch):
    adapter, profile, _ = metadata
    entered, release = threading.Event(), threading.Event()
    token, calls = CancelToken(), []

    def request(*args, cancel=None, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(timeout=2)
            cancel.check()
        return response()

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(usage.read, adapter, profile, cancel=token)
        assert entered.wait(timeout=1)
        follower = executor.submit(usage.read, adapter, profile)
        token.cancel()
        release.set()
        with pytest.raises(Cancelled):
            leader.result(timeout=1)
        assert follower.result(timeout=1)["available"]
    assert len(calls) == 2


def test_waiter_timeout_and_lock_map_are_bounded(metadata, monkeypatch, tmp_path):
    adapter, profile, _ = metadata
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(usage, "_WAIT_SECONDS", .01)
    monkeypatch.setattr(usage, "_FLIGHT_LIMIT", 1)

    def request(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return response()

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(usage.read, adapter, profile)
        assert entered.wait(timeout=1)
        try:
            with pytest.raises(usage.UsageError, match="timeout"):
                usage.read(adapter, profile)
            other = {"home": str(tmp_path / "other"), "config_dir": tmp_path / "other-config"}
            with pytest.raises(usage.UsageError, match="Too many"):
                usage.read(adapter, other)
        finally:
            release.set()
        assert leader.result(timeout=1)["available"]
    assert not usage._flights


def test_invalidation_prevents_old_inflight_result_from_recaching(metadata, monkeypatch):
    adapter, profile, _ = metadata
    entered, release = threading.Event(), threading.Event()

    def request(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return response()

    monkeypatch.setattr(usage.claude_control, "request", request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(usage.read, adapter, profile)
        assert entered.wait(timeout=1)
        usage.invalidate(adapter, profile)
        release.set()
        with pytest.raises(usage.UsageError, match="changing this profile"):
            leader.result(timeout=1)
    assert not usage._cache and not usage._flights


def test_identity_change_floor_also_rejects_cached_previous_account(metadata):
    adapter, profile, _ = metadata
    usage.read(adapter, profile)
    profile["identity_changed_at"] = NOW + 1
    with pytest.raises(usage.UsageError, match="changing this profile"):
        usage.read(adapter, profile)
    assert not usage._cache


def test_explicit_stale_display_retains_original_timestamp_and_zero_capacity():
    report = usage.parse(response(), observed_at=NOW - 901, now=NOW,
                         plan_label="Max 20x", allow_stale=True)
    assert report["status"] == "stale" and report["available"] is False
    assert report["observed_at"] == NOW - 901
    assert report["plan_label"] == "Max 20x"
    assert report["window_left"] == .17 and report["weekly_left"] == .86
    assert not usage.fresh(report, now=NOW)
    assert usage.for_model(report, "sonnet", now=NOW) == {"window_left": 0, "weekly_left": 0}
    with pytest.raises(usage.UsageError, match="stale"):
        usage.parse(response(), observed_at=NOW - 901, now=NOW)


def test_passed_general_reset_stays_visible_but_unavailable():
    value = response()
    value["rate_limits"]["five_hour"] = window(100, -1)
    report = usage.parse(value, observed_at=NOW, now=NOW, allow_stale=True)
    assert report["status"] == "stale" and report["available"] is False
    assert report["window_left"] == 0
    assert report["window_resets_at"] == NOW - 1
    assert report["plan_label"] == "Max"
    with pytest.raises(usage.UsageError, match="after its reset"):
        usage.parse(value, observed_at=NOW, now=NOW)


@pytest.mark.parametrize("stamp", [NOW + 1, -1, True, float("inf"), "unverified"])
def test_display_mode_does_not_accept_future_or_invalid_observation_times(stamp):
    with pytest.raises(usage.UsageError, match="stale"):
        usage.parse(response(), observed_at=stamp, now=NOW, allow_stale=True)


def test_stale_regular_backoff_expires_into_a_genuinely_fresh_report(metadata, monkeypatch):
    adapter, profile, calls = metadata
    cache_file(profile, stamp=NOW - 901)
    assert usage.read(adapter, profile)["status"] == "stale"
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 299)
    assert usage.read(adapter, profile)["observed_at"] == NOW - 901
    assert len(calls) == 1
    cache_file(profile, stamp=NOW + 300)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 300)
    report = usage.read(adapter, profile)
    assert len(calls) == 2
    assert report["observed_at"] == NOW + 300
    assert report["available"] is True and "status" not in report
    assert usage.fresh(report)


def test_stale_manual_refresh_cannot_poll_native_more_often_than_a_minute(metadata, monkeypatch):
    adapter, profile, calls = metadata
    cache_file(profile, stamp=NOW - 901)
    usage.read(adapter, profile)
    for elapsed in (0, 1, 59):
        monkeypatch.setattr(usage.time, "time", lambda elapsed=elapsed: NOW + elapsed)
        assert usage.read(adapter, profile, force=True)["status"] == "stale"
        assert len(calls) == 1
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 60)
    report = usage.read(adapter, profile, force=True)
    assert report["observed_at"] == NOW - 901
    assert len(calls) == 2
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 61)
    assert usage.read(adapter, profile, force=True)["available"] is False
    assert len(calls) == 2


def test_fresh_manual_refresh_observes_the_same_minimum_poll_interval(metadata, monkeypatch):
    adapter, profile, calls = metadata
    usage.read(adapter, profile)
    for elapsed in (0, 59):
        monkeypatch.setattr(usage.time, "time", lambda elapsed=elapsed: NOW + elapsed)
        assert usage.read(adapter, profile, force=True)["available"]
        assert len(calls) == 1
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 60)
    assert usage.read(adapter, profile, force=True)["available"]
    assert len(calls) == 2


def test_failed_refresh_discards_stale_plan_and_stats_and_backs_off(metadata, monkeypatch):
    adapter, profile, calls = metadata
    cache_file(profile, stamp=NOW - 901)
    usage.read(adapter, profile)
    failures = []

    def fail(*args, **kwargs):
        failures.append(1)
        raise usage.claude_control.ControlError("Authentication metadata is unavailable.")

    monkeypatch.setattr(usage.claude_control, "request", fail)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 60)
    with pytest.raises(usage.claude_control.ControlError):
        usage.read(adapter, profile, force=True)
    assert not usage._cache and len(failures) == 1
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 61)
    with pytest.raises(usage.UsageError, match="Retry after one minute"):
        usage.read(adapter, profile, force=True)
    assert not usage._cache and len(failures) == 1
    assert len(calls) == 1


def test_invalid_schema_cannot_be_kept_as_a_stale_display(metadata, monkeypatch):
    adapter, profile, _ = metadata
    cache_file(profile, stamp=NOW - 901)
    value = response()
    value["rate_limits"]["five_hour"]["utilization"] = "private-raw-error"
    monkeypatch.setattr(usage.claude_control, "request", lambda *args, **kwargs: value)
    with pytest.raises(usage.UsageError, match="percentage") as failure:
        usage.read(adapter, profile)
    assert "private-raw-error" not in str(failure.value)
    assert not usage._cache


def test_identity_invalidation_clears_stale_data_and_refresh_cooldown(metadata, monkeypatch):
    adapter, profile, calls = metadata
    cache_file(profile, stamp=NOW - 901)
    assert usage.read(adapter, profile)["plan_label"] == "Max 20x"
    profile["identity_changed_at"] = NOW + 1
    usage.invalidate(adapter, profile)
    cache_file(profile, stamp=NOW + 1)
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 1)

    def new_account(*args, **kwargs):
        calls.append(("get_usage", {}))
        return response(subscription_type="pro")

    monkeypatch.setattr(usage.claude_control, "request", new_account)
    report = usage.read(adapter, profile, force=True)
    assert len(calls) == 2
    assert report["available"] is True and report["observed_at"] == NOW + 1
    assert report["subscription_type"] == "pro" and report["plan_label"] == "Pro"


def test_cached_snapshot_crossing_age_limit_becomes_a_display_only_report(metadata, monkeypatch):
    adapter, profile, calls = metadata
    cache_file(profile, stamp=NOW - 895)
    assert usage.read(adapter, profile)["available"]
    monkeypatch.setattr(usage.time, "time", lambda: NOW + 6)
    report = usage.read(adapter, profile, force=True)
    assert report["available"] is False and report["status"] == "stale"
    assert report["observed_at"] == NOW - 895
    assert len(calls) == 1
    assert usage.for_model(report, "sonnet") == {"window_left": 0, "weekly_left": 0}


def test_fresh_usage_after_login_change_does_not_reuse_previous_accounts_multiplier(metadata):
    adapter, profile, _ = metadata
    profile["identity_changed_at"] = NOW - 10
    cache_file(profile, stamp=NOW - 1, plan="Max 20x", profile_stamp=NOW - 60)
    report = usage.read(adapter, profile)
    assert report["available"] is True and report["observed_at"] == NOW - 1
    assert report["subscription_type"] == "max" and report["plan_label"] == "Max"


def test_multiplier_observed_after_login_change_can_be_shown(metadata):
    adapter, profile, _ = metadata
    profile["identity_changed_at"] = NOW - 10
    cache_file(profile, stamp=NOW - 1, plan="Max 20x", profile_stamp=NOW - 5)
    report = usage.read(adapter, profile)
    assert report["available"] is True and report["observed_at"] == NOW - 1
    assert report["plan_label"] == "Max 20x"
