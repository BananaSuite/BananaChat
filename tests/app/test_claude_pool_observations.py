"""Per-account subscription observations and enrollment, without provider credentials."""

import json
import time

import pytest

from bananachat import db
from bananachat.db import catalog, claude_pool as accounts, limits as limits_db, settings
from bananachat.services import claude_pool as pool, limits

MODEL = "observed-sonnet"


@pytest.fixture(autouse=True)
def adapter(app):
    pool.register_site_discovery(lambda: [{"name": MODEL, "family": "sonnet", "reasoning": ["low", "medium"]}])
    pool.register_site_chat(lambda *args: iter([{"text": "Answer", "tokens_in": 8, "tokens_out": 2, "done": True}]))
    yield
    pool.reset_transport()


def add(label, **values):
    return accounts.add_account(label, window_limit=100_000, **values)


def observation(**values):
    return {"source": "claude_code", "available": True, "observed_at": time.time(),
            "window_left": 0.8, "window_resets_at": time.time() + 10_000,
            "weekly_left": 0.6, "weekly_resets_at": time.time() + 100_000,
            "subscription_type": "max", "model_limits": {}, **values}


def reports(values):
    pool.register_site_reporter(lambda: {"account_reports": values})
    return pool.refresh_quota()


def test_failed_report_only_withdraws_its_account_and_never_rewrites_budgets(app):
    failed, healthy = add("failed", priority=90), add("healthy")
    quota = reports({str(failed): {"source": "claude_code", "available": False},
                     str(healthy): observation(window_left=0.9)})
    assert quota["reporter_error"] is False and quota["usable"] == 1
    assert quota["window_left"] == 0.45
    chunks = list(pool.chat_stream(MODEL, [{"role": "user", "content": "hi"}]))
    assert chunks[-1]["done"] and accounts.get(failed)["window_used"] == 0
    assert accounts.get(healthy)["window_used"] == 10
    assert accounts.get(healthy)["window_limit"] == 100_000
    assert accounts.get(healthy)["quota_source"] == "local"


@pytest.mark.parametrize("values", [
    {"observed_at": lambda: time.time() - 901}, {"observed_at": lambda: time.time() + 120},
    {"window_resets_at": lambda: time.time() - 1}, {"weekly_resets_at": lambda: time.time() - 1},
    {"window_left": None}, {"window_left": 0}, {"weekly_left": 0},
    {"window_left": -1}, {"window_left": True}, {"window_left": float("nan")},
])
def test_invalid_stale_or_exhausted_observation_skips_only_that_account(app, values):
    paused, ready = add("paused", priority=99), add("ready")
    # Collection can precede this test by several minutes in the full suite.
    # Resolve timestamps when reporting so the future case remains future.
    values = {key: value() if callable(value) else value for key, value in values.items()}
    quota = reports({str(paused): observation(**values), str(ready): observation()})
    assert not quota["reporter_error"]
    assert [row["id"] for row in pool.accounts_in_order()] == [ready]
    assert pool.usable(accounts.get(paused)) is False


def test_provider_weekly_cap_applies_when_local_weekly_budget_is_disabled(app):
    account = add("weekly")
    reports({str(account): observation(weekly_left=0)})
    assert accounts.get(account)["weekly_limit"] is None
    with pytest.raises(pool.CapacityUnavailable, match="no available quota"):
        list(pool.chat_stream(MODEL, []))


def test_family_scope_only_withdraws_matching_model_and_reduces_its_capacity(app):
    account = add("family")
    pool.register_site_discovery(lambda: [
        {"name": MODEL, "family": "sonnet", "reasoning": []},
        {"name": "observed-opus", "family": "opus", "reasoning": []}])
    pool.sync_catalog(selected=[MODEL, "observed-opus"])
    reports({str(account): observation(model_limits={"opus": {"left": 0, "resets_at": time.time() + 100_000}})})
    assert pool.usable(accounts.get(account))
    assert not pool.usable(accounts.get(account), family="opus")
    assert list(pool.chat_stream(MODEL, []))[-1]["done"]
    with pytest.raises(pool.CapacityUnavailable, match="currently at capacity"):
        list(pool.chat_stream("observed-opus", []))
    assert pool.capacity_report(catalog.get_by_name("observed-opus")).window_left == 0
    assert pool.capacity_report(catalog.get_by_name(MODEL)).window_left > 0


def test_expired_family_reset_does_not_pause_other_model_families(app):
    account = add("family-reset")
    reports({str(account): observation(model_limits={"opus": {"left": 0.8, "resets_at": time.time() - 1}})})
    assert pool.usable(accounts.get(account), family="sonnet")
    assert not pool.usable(accounts.get(account), family="opus")


def test_priority_then_actual_remaining_capacity_selects_account(app):
    first, second = add("first"), add("second")
    reports({str(first): observation(window_left=0.2), str(second): observation(window_left=0.9)})
    assert pool.pick_account()["id"] == second
    accounts.update_account(first, priority=50)
    assert pool.pick_account()["id"] == first


def test_explicit_local_mode_and_weekly_only_manual_report_keep_local_bound(app):
    local, file = add("local"), add("file")
    quota = reports({str(local): {"source": "local", "available": True},
                     str(file): observation(source="file", window_left=None, weekly_left=0.3)})
    assert quota["usable"] == 2
    assert pool.usable(accounts.get(local)) and pool.usable(accounts.get(file))


def test_missing_account_observation_cannot_borrow_another_account_capacity(app):
    missing, healthy = add("missing"), add("healthy")
    reports({str(healthy): observation()})
    assert not pool.usable(accounts.get(missing)) and pool.usable(accounts.get(healthy))


def test_shared_observations_drop_arbitrary_adapter_metadata_and_errors(app):
    account = add("private")
    reports({str(account): observation(email="private@example.com", credentials="private-secret",
                                      plan_label="private-secret", last_error="private-secret")})
    state = settings.state_get(pool.ACCOUNT_REPORTS_KEY)
    assert "private-secret" not in json.dumps(state) and "private@example.com" not in json.dumps(state)
    assert state["reports"][str(account)]["plan_label"] == "Claude Max"
    pool._pool_cache.update(at=0, value=None)
    assert pool.accounts_in_order()[0]["id"] == account  # shared storage, not request-local metadata


def test_verified_max_multiplier_is_preserved_without_inventing_a_tier(app):
    account = add("tier")
    reports({str(account): observation(plan_label="Claude Max 20x")})
    assert pool.account_reports()[str(account)]["plan_label"] == "Claude Max 20x"
    reports({str(account): observation(plan_label="Unsupported invented tier")})
    assert pool.account_reports()[str(account)]["plan_label"] == "Claude Max"


@pytest.mark.parametrize("exempt", [False, True])
def test_stale_native_usage_preserves_verified_readout_without_unlocking_capacity(app, make_user, exempt):
    account = add("stale-readout")
    pool.record_usage(account, 13, 7)
    before = dict(accounts.get(account))
    checked = time.time() - 3600
    report = observation(available=False, status="stale", observed_at=checked, window_left=0.23,
                         weekly_left=0.47, plan_label="Claude Max 20x",
                         model_limits={"sonnet": {"left": 0.31, "resets_at": time.time() + 100_000}},
                         last_error="private-provider-secret", email="private@example.com")
    pool.sync_catalog(selected=[MODEL], source="admin")
    model = catalog.get_by_name(MODEL)
    user = make_user("stale-exempt" if exempt else "stale-user")
    if exempt:
        limits_db.update_user_settings(user["id"], None, token_exempt=True, rate_exempt=True)
    quota = reports({str(account): report})
    saved = pool.account_reports()[str(account)]
    assert saved["status"] == "stale" and saved["available"] is False
    assert saved["observed_at"] == checked and saved["plan_label"] == "Claude Max 20x"
    assert (saved["window_left"], saved["weekly_left"]) == (0.23, 0.47)
    assert saved["model_limits"] == report["model_limits"]
    assert (saved["window_resets_at"], saved["weekly_resets_at"]) == (
        report["window_resets_at"], report["weekly_resets_at"])
    assert "private-provider-secret" not in json.dumps(saved) and "private@example.com" not in json.dumps(saved)
    assert dict(accounts.get(account)) == before
    assert quota["usable"] == 0 and quota["stale"] == 1 and quota["window_left"] == 0
    assert not pool.subscription_fresh(accounts.get(account))
    assert not pool.usable(accounts.get(account)) and pool.pick_account() is None
    assert not limits.admit(user, "api", model).allowed
    assert pool.capacity_report(model).window_left == 0
    with pytest.raises(pool.CapacityUnavailable, match="usage is unavailable"):
        list(pool.chat_stream(MODEL, []))
    assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert dict(accounts.get(account)) == before


def test_stale_native_snapshot_with_fresh_timestamp_still_cannot_admit(app):
    account = add("stale-positive")
    reports({str(account): observation(available=False, status="stale")})
    saved = pool.account_reports()[str(account)]
    assert saved["observed_at"] > time.time() - 10 and saved["window_left"] > 0
    assert saved["status"] == "stale" and not pool.usable(accounts.get(account))
    with pytest.raises(pool.CapacityUnavailable):
        list(pool.chat_stream(MODEL, []))
    assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0


def test_stale_native_readout_does_not_pause_other_fresh_account(app):
    stale, ready = add("stale", priority=99), add("ready")
    quota = reports({str(stale): observation(available=False, status="stale", observed_at=time.time() - 901),
                     str(ready): observation(window_left=0.9)})
    assert not quota["reporter_error"] and quota["usable"] == 1 and quota["stale"] == 1
    assert pool.accounts_in_order()[0]["id"] == ready
    assert list(pool.chat_stream(MODEL, []))[-1]["done"]
    assert accounts.get(stale)["window_used"] == 0 and accounts.get(ready)["window_used"] == 10
    assert pool.account_reports()[str(stale)]["status"] == "stale"


def test_fresh_native_observation_replaces_stale_status_and_restores_admission(app):
    account = add("refreshed")
    reports({str(account): observation(available=False, status="stale", observed_at=time.time() - 901)})
    assert not pool.usable(accounts.get(account))
    updated = observation(window_left=0.6, weekly_left=0.5)
    quota = reports({str(account): updated})
    saved = pool.account_reports()[str(account)]
    assert saved["status"] == "available" and saved["available"] is True
    assert saved["observed_at"] == updated["observed_at"] and saved["window_left"] == 0.6
    assert quota["usable"] == 1 and quota["stale"] == 0
    assert list(pool.chat_stream(MODEL, []))[-1]["done"]


@pytest.mark.parametrize("source", ["claude_code", "file", "local"])
def test_available_stale_marker_is_inconsistent_and_fails_closed(app, source):
    account = add("inconsistent")
    reports({str(account): observation(source=source, available=True, status="stale")})
    saved = pool.account_reports()[str(account)]
    assert saved == {"source": "claude_code", "available": False, "status": "unavailable"}
    assert not pool.usable(accounts.get(account))
    with pytest.raises(pool.CapacityUnavailable):
        list(pool.chat_stream(MODEL, []))


@pytest.mark.parametrize("source,status", [
    ("file", "stale"), ("local", "stale"), ("claude_code", "private-provider-secret"),
    ("claude_code", {"diagnostic": "private-provider-secret"}),
])
def test_report_status_only_preserves_allowlisted_native_stale_marker(app, source, status):
    account = add("sanitized-status")
    reports({str(account): observation(source=source, available=False, status=status)})
    saved = pool.account_reports()[str(account)]
    assert saved["status"] == "unavailable" and saved["available"] is False
    assert "private-provider-secret" not in json.dumps(saved)
    assert not pool.usable(accounts.get(account))


def test_admin_readout_separates_real_subscription_percentages_from_token_ledger(app, admin):
    account = add("readout")
    pool.record_usage(account, 13, 7)
    reports({str(account): observation(window_left=0.23, weekly_left=0.47)})
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Local token budget: 20 / 100000" in html
    assert "Subscription: Claude Max" in html
    assert "5-hour remaining: 23.0%" in html and "Weekly remaining: 47.0%" in html
    assert "Subscription percentages are separate from local token usage" in html


def test_automatic_enrollment_accepts_new_models_without_reenabling_unchecked_models(app, admin):
    discovered = [{"name": name, "family": "sonnet", "reasoning": []} for name in (MODEL, "unchecked-sonnet")]
    pool.register_site_discovery(lambda: discovered)
    pool.sync_catalog(selected=[MODEL], auto_enroll=True, source="admin")
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 1
    discovered.append({"name": "new-sonnet", "family": "sonnet", "reasoning": []})
    result = pool.sync_catalog(source="background")
    assert result["enabled"] == ["new-sonnet"]
    assert catalog.get_by_name("unchecked-sonnet") is None
    policy = limits_db.get_model_policy(catalog.get_by_name("new-sonnet"))
    assert policy["enabled"] and policy["window_tokens"] == 150_000
    assert policy["weekly_tokens"] is None and policy["counts_toward_pool"] is False
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert 'value="new-sonnet" checked' in html
    assert 'value="unchecked-sonnet" checked' not in html


def test_manual_enrollment_override_does_not_publish_new_discovered_models(app):
    pool.sync_catalog(selected=[MODEL], auto_enroll=False, source="admin")
    pool.register_site_discovery(lambda: [{"name": MODEL, "family": "sonnet"},
                                        {"name": "new-sonnet", "family": "sonnet"}])
    assert pool.sync_catalog(source="background")["new"] == []
    assert catalog.get_by_name("new-sonnet") is None
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 0


def test_admin_can_choose_automatic_enrollment_without_changing_global_catalog_policy(app, admin):
    response = admin.post("/admin/models/claude/sync", {"models": MODEL, "enrollment": "automatic"})
    assert response.status_code == 302 and pool.automatic_enrollment()
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 1
    assert settings.state_get(pool.AUTO_ENROLL_KEY) is True


@pytest.mark.parametrize("fallback", [False, True])
def test_changed_model_admission_does_not_cool_down_unrelated_families(app, fallback):
    first = add("changed", priority=20)
    second = add("fallback") if fallback else None
    observed = {str(first): observation()}
    if second:
        observed[str(second)] = observation()
    pool.register_site_discovery(lambda: [{"name": MODEL, "family": "sonnet"},
                                        {"name": "observed-opus", "family": "opus"}])
    reports(observed)
    calls = []

    def handler(account, name, messages, options):
        calls.append((account["id"], name))
        if account["id"] == first and name == MODEL:
            observed[str(first)] = observation(model_limits={"sonnet": {"left": 0, "resets_at": time.time() + 100_000}})
            pool.refresh_quota()
            raise pool.AdmissionChanged("private-provider-material")
        yield {"text": "Ready", "tokens_in": 8, "tokens_out": 2, "done": True}

    pool.register_site_chat(handler)
    if fallback:
        assert list(pool.chat_stream(MODEL, []))[-1]["done"]
        assert calls == [(first, MODEL), (second, MODEL)]
    else:
        with pytest.raises(RuntimeError, match="availability changed") as error:
            list(pool.chat_stream(MODEL, []))
        assert "private-provider-material" not in str(error.value)
    row = accounts.get(first)
    assert row["status"] == "active" and row["cooldown_until"] is None and not accounts.leased(first)
    assert not pool.usable(row, family="sonnet") and pool.usable(row, family="opus")
    assert list(pool.chat_stream("observed-opus", []))[-1]["done"]
    assert calls[-1] == (first, "observed-opus")


def test_changed_admission_after_output_never_retries_or_globally_rests_account(app):
    first, second = add("partial", priority=20), add("fallback")
    reports({str(first): observation(), str(second): observation()})
    called = []

    def handler(account, name, messages, options):
        called.append(account["id"])
        yield {"text": "Partial", "tokens_in": 8}
        raise pool.AdmissionChanged("private-provider-material")

    pool.register_site_chat(handler)
    stream = pool.chat_stream(MODEL, [])
    assert next(stream)["text"] == "Partial"
    with pytest.raises(RuntimeError, match="availability changed") as error:
        next(stream)
    assert "private-provider-material" not in str(error.value)
    assert called == [first]
    row = accounts.get(first)
    assert row["status"] == "active" and row["cooldown_until"] is None and row["window_used"] > 0
    assert not accounts.leased(first)


def test_uncertain_shutdown_quarantines_account_before_releasing_lease(app, caplog):
    first, second = add("uncertain", priority=20), add("healthy")
    called = []

    class ShutdownFailed(RuntimeError):
        requires_quarantine = True

    def handler(account, name, messages, options):
        called.append(account["id"])
        if account["id"] == first:
            raise ShutdownFailed("private-provider-material")
        yield {"text": "Ready", "done": True}

    pool.register_site_chat(handler)
    assert list(pool.chat_stream(MODEL, []))[-1]["done"]
    row = accounts.get(first)
    assert row["status"] == "disabled" and not accounts.leased(first)
    assert "Transport shutdown failed" in row["last_error"]
    assert called == [first, second]
    assert "private-provider-material" not in row["last_error"] + caplog.text
