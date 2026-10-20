"""Per-account subscription observations and enrollment, for each provider and without credentials.

A provider's observation can only withdraw or narrow its own account: a failed,
stale or invalid report never borrows capacity, rewrites a local budget or
leaks adapter metadata into shared storage.
"""

import json
import time

import pytest

from bananachat import db
from bananachat.db import catalog, limits as limits_db, settings, subscription_pool as accounts
from bananachat.services import limits, subscription_pool as pool

MODEL = "observed-standard"
HEAVY = "observed-heavy"
# What each provider's own transport reports: its source, a plan with one verified finer tier, and families.
NATIVE = {
    "claude": {"source": "claude_code", "plan": "max", "plan_label": "Claude Max", "tier": "Claude Max 20x",
               "family": "sonnet", "heavy": "opus"},
    "codex": {"source": "codex", "plan": "pro", "plan_label": "ChatGPT Pro", "tier": "ChatGPT Pro (More)",
              "family": "sol", "heavy": "astra"},
}


@pytest.fixture(autouse=True, params=pool.PROVIDERS)
def provider(request, app):
    name = request.param
    pool.register_site_discovery(name, lambda: [{"name": MODEL, "family": NATIVE[name]["family"],
                                                 "reasoning": ["low", "medium"]}])
    pool.register_site_chat(name, lambda *args: iter([{"text": "Answer", "tokens_in": 8, "tokens_out": 2,
                                                       "done": True}]))
    yield name
    pool.reset_transport()


def add(provider, label, **values):
    return accounts.add_account(label, provider=provider, window_limit=100_000, **values)


def observation(provider, **values):
    return {"source": NATIVE[provider]["source"], "available": True, "observed_at": time.time(),
            "window_left": 0.8, "window_resets_at": time.time() + 10_000,
            "weekly_left": 0.6, "weekly_resets_at": time.time() + 100_000,
            "subscription_type": NATIVE[provider]["plan"], "model_limits": {}, **values}


def reports(provider, values):
    pool.register_site_reporter(provider, lambda: {"account_reports": values})
    return pool.refresh_quota(provider)[provider]


def test_failed_report_only_withdraws_its_account_and_never_rewrites_budgets(app, provider):
    failed, healthy = add(provider, "failed", priority=90), add(provider, "healthy")
    quota = reports(provider, {str(failed): {"source": NATIVE[provider]["source"], "available": False},
                               str(healthy): observation(provider, window_left=0.9)})
    assert quota["reporter_error"] is False and quota["usable"] == 1
    assert quota["window_left"] == 0.45
    chunks = list(pool.chat_stream(provider, MODEL, [{"role": "user", "content": "hi"}]))
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
def test_invalid_stale_or_exhausted_observation_skips_only_that_account(app, provider, values):
    paused, ready = add(provider, "paused", priority=99), add(provider, "ready")
    # Collection can precede this test by several minutes in the full suite.
    # Resolve timestamps when reporting so the future case remains future.
    values = {key: value() if callable(value) else value for key, value in values.items()}
    quota = reports(provider, {str(paused): observation(provider, **values), str(ready): observation(provider)})
    assert not quota["reporter_error"]
    assert [row["id"] for row in pool.accounts_in_order(provider)] == [ready]
    assert pool.usable(accounts.get(paused)) is False


def test_provider_weekly_cap_applies_when_local_weekly_budget_is_disabled(app, provider):
    account = add(provider, "weekly")
    reports(provider, {str(account): observation(provider, weekly_left=0)})
    assert accounts.get(account)["weekly_limit"] is None
    with pytest.raises(pool.CapacityUnavailable, match="no available quota"):
        list(pool.chat_stream(provider, MODEL, []))


def test_family_scope_only_withdraws_matching_model_and_reduces_its_capacity(app, provider):
    family, heavy = NATIVE[provider]["family"], NATIVE[provider]["heavy"]
    account = add(provider, "family")
    pool.register_site_discovery(provider, lambda: [
        {"name": MODEL, "family": family, "reasoning": []},
        {"name": HEAVY, "family": heavy, "reasoning": []}])
    pool.sync_catalog(provider, selected=[MODEL, HEAVY])
    reports(provider, {str(account): observation(
        provider, model_limits={heavy: {"left": 0, "resets_at": time.time() + 100_000}})})
    assert pool.usable(accounts.get(account))
    assert not pool.usable(accounts.get(account), family=heavy)
    assert list(pool.chat_stream(provider, MODEL, []))[-1]["done"]
    with pytest.raises(pool.CapacityUnavailable, match="currently at capacity"):
        list(pool.chat_stream(provider, HEAVY, []))
    assert pool.capacity_report(catalog.get_by_name(HEAVY)).window_left == 0
    assert pool.capacity_report(catalog.get_by_name(MODEL)).window_left > 0


def test_expired_family_reset_does_not_pause_other_model_families(app, provider):
    family, heavy = NATIVE[provider]["family"], NATIVE[provider]["heavy"]
    account = add(provider, "family-reset")
    reports(provider, {str(account): observation(
        provider, model_limits={heavy: {"left": 0.8, "resets_at": time.time() - 1}})})
    assert pool.usable(accounts.get(account), family=family)
    assert not pool.usable(accounts.get(account), family=heavy)


def test_priority_then_actual_remaining_capacity_selects_account(app, provider):
    first, second = add(provider, "first"), add(provider, "second")
    reports(provider, {str(first): observation(provider, window_left=0.2),
                       str(second): observation(provider, window_left=0.9)})
    assert pool.pick_account(provider)["id"] == second
    accounts.update_account(first, priority=50)
    assert pool.pick_account(provider)["id"] == first


def test_explicit_local_mode_and_weekly_only_manual_report_keep_local_bound(app, provider):
    local, file = add(provider, "local"), add(provider, "file")
    quota = reports(provider, {str(local): {"source": "local", "available": True},
                               str(file): observation(provider, source="file", window_left=None, weekly_left=0.3)})
    assert quota["usable"] == 2
    assert pool.usable(accounts.get(local)) and pool.usable(accounts.get(file))


def test_missing_account_observation_cannot_borrow_another_account_capacity(app, provider):
    missing, healthy = add(provider, "missing"), add(provider, "healthy")
    reports(provider, {str(healthy): observation(provider)})
    assert not pool.usable(accounts.get(missing)) and pool.usable(accounts.get(healthy))


def test_shared_observations_drop_arbitrary_adapter_metadata_and_errors(app, provider):
    account = add(provider, "private")
    reports(provider, {str(account): observation(provider, email="private@example.com", credentials="private-secret",
                                                 plan_label="private-secret", last_error="private-secret")})
    state = settings.state_get(f"{provider}.account_reports")
    assert "private-secret" not in json.dumps(state) and "private@example.com" not in json.dumps(state)
    assert state["reports"][str(account)]["plan_label"] == NATIVE[provider]["plan_label"]
    pool._pool_cache.pop(provider, None)
    assert pool.accounts_in_order(provider)[0]["id"] == account  # shared storage, not request-local metadata


def test_verified_plan_tier_is_preserved_without_inventing_a_tier(app, provider):
    account = add(provider, "tier")
    reports(provider, {str(account): observation(provider, plan_label=NATIVE[provider]["tier"])})
    assert pool.account_reports(provider)[str(account)]["plan_label"] == NATIVE[provider]["tier"]
    reports(provider, {str(account): observation(provider, plan_label="Unsupported invented tier")})
    assert pool.account_reports(provider)[str(account)]["plan_label"] == NATIVE[provider]["plan_label"]


@pytest.mark.parametrize("exempt", [False, True])
def test_stale_native_usage_preserves_verified_readout_without_unlocking_capacity(app, make_user, provider, exempt):
    native = NATIVE[provider]
    account = add(provider, "stale-readout")
    pool.record_usage(account, 13, 7, provider=provider)
    before = dict(accounts.get(account))
    checked = time.time() - 3600
    report = observation(provider, available=False, status="stale", observed_at=checked, window_left=0.23,
                         weekly_left=0.47, plan_label=native["tier"],
                         model_limits={native["family"]: {"left": 0.31, "resets_at": time.time() + 100_000}},
                         last_error="private-provider-secret", email="private@example.com")
    pool.sync_catalog(provider, selected=[MODEL], source="admin")
    model = catalog.get_by_name(MODEL)
    user = make_user("stale-exempt" if exempt else "stale-user")
    if exempt:
        limits_db.update_user_settings(user["id"], None, token_exempt=True, rate_exempt=True)
    quota = reports(provider, {str(account): report})
    saved = pool.account_reports(provider)[str(account)]
    assert saved["status"] == "stale" and saved["available"] is False
    assert saved["observed_at"] == checked and saved["plan_label"] == native["tier"]
    assert (saved["window_left"], saved["weekly_left"]) == (0.23, 0.47)
    assert saved["model_limits"] == report["model_limits"]
    assert (saved["window_resets_at"], saved["weekly_resets_at"]) == (
        report["window_resets_at"], report["weekly_resets_at"])
    assert "private-provider-secret" not in json.dumps(saved) and "private@example.com" not in json.dumps(saved)
    assert dict(accounts.get(account)) == before
    assert quota["usable"] == 0 and quota["stale"] == 1 and quota["window_left"] == 0
    assert not pool.subscription_fresh(accounts.get(account))
    assert not pool.usable(accounts.get(account)) and pool.pick_account(provider) is None
    assert not limits.admit(user, "api", model).allowed
    assert pool.capacity_report(model).window_left == 0
    with pytest.raises(pool.CapacityUnavailable, match="usage is unavailable"):
        list(pool.chat_stream(provider, MODEL, []))
    assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0
    assert dict(accounts.get(account)) == before


def test_stale_native_snapshot_with_fresh_timestamp_still_cannot_admit(app, provider):
    account = add(provider, "stale-positive")
    reports(provider, {str(account): observation(provider, available=False, status="stale")})
    saved = pool.account_reports(provider)[str(account)]
    assert saved["observed_at"] > time.time() - 10 and saved["window_left"] > 0
    assert saved["status"] == "stale" and not pool.usable(accounts.get(account))
    with pytest.raises(pool.CapacityUnavailable):
        list(pool.chat_stream(provider, MODEL, []))
    assert db.scalar("SELECT COUNT(*) FROM subscription_usage") == 0


def test_stale_native_readout_does_not_pause_other_fresh_account(app, provider):
    stale, ready = add(provider, "stale", priority=99), add(provider, "ready")
    quota = reports(provider, {
        str(stale): observation(provider, available=False, status="stale", observed_at=time.time() - 901),
        str(ready): observation(provider, window_left=0.9)})
    assert not quota["reporter_error"] and quota["usable"] == 1 and quota["stale"] == 1
    assert pool.accounts_in_order(provider)[0]["id"] == ready
    assert list(pool.chat_stream(provider, MODEL, []))[-1]["done"]
    assert accounts.get(stale)["window_used"] == 0 and accounts.get(ready)["window_used"] == 10
    assert pool.account_reports(provider)[str(stale)]["status"] == "stale"


def test_fresh_native_observation_replaces_stale_status_and_restores_admission(app, provider):
    account = add(provider, "refreshed")
    reports(provider, {str(account): observation(provider, available=False, status="stale",
                                                 observed_at=time.time() - 901)})
    assert not pool.usable(accounts.get(account))
    updated = observation(provider, window_left=0.6, weekly_left=0.5)
    quota = reports(provider, {str(account): updated})
    saved = pool.account_reports(provider)[str(account)]
    assert saved["status"] == "available" and saved["available"] is True
    assert saved["observed_at"] == updated["observed_at"] and saved["window_left"] == 0.6
    assert quota["usable"] == 1 and quota["stale"] == 0
    assert list(pool.chat_stream(provider, MODEL, []))[-1]["done"]


@pytest.mark.parametrize("source", ["native", "file", "local"])
def test_available_stale_marker_is_inconsistent_and_fails_closed(app, provider, source):
    native = NATIVE[provider]["source"]
    account = add(provider, "inconsistent")
    reports(provider, {str(account): observation(provider, source=native if source == "native" else source,
                                                 available=True, status="stale")})
    saved = pool.account_reports(provider)[str(account)]
    assert saved == {"source": native, "available": False, "status": "unavailable"}
    assert not pool.usable(accounts.get(account))
    with pytest.raises(pool.CapacityUnavailable):
        list(pool.chat_stream(provider, MODEL, []))


@pytest.mark.parametrize("source,status", [
    ("file", "stale"), ("local", "stale"), ("native", "private-provider-secret"),
    ("native", {"diagnostic": "private-provider-secret"}),
])
def test_report_status_only_preserves_allowlisted_native_stale_marker(app, provider, source, status):
    account = add(provider, "sanitized-status")
    source = NATIVE[provider]["source"] if source == "native" else source
    reports(provider, {str(account): observation(provider, source=source, available=False, status=status)})
    saved = pool.account_reports(provider)[str(account)]
    assert saved["status"] == "unavailable" and saved["available"] is False
    assert "private-provider-secret" not in json.dumps(saved)
    assert not pool.usable(accounts.get(account))


def test_automatic_enrollment_accepts_new_models_without_reenabling_unchecked_models(app, provider):
    family = NATIVE[provider]["family"]
    discovered = [{"name": name, "family": family, "reasoning": []} for name in (MODEL, "unchecked-standard")]
    pool.register_site_discovery(provider, lambda: discovered)
    pool.sync_catalog(provider, selected=[MODEL], auto_enroll=True, source="admin")
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 1
    discovered.append({"name": "new-standard", "family": family, "reasoning": []})
    result = pool.sync_catalog(provider, source="background")
    assert result["enabled"] == ["new-standard"]
    assert catalog.get_by_name("unchecked-standard") is None
    policy = limits_db.get_model_policy(catalog.get_by_name("new-standard"))
    assert policy["enabled"] and policy["window_tokens"] == 150_000
    assert policy["weekly_tokens"] is None and policy["counts_toward_pool"] is False


def test_manual_enrollment_override_does_not_publish_new_discovered_models(app, provider):
    family = NATIVE[provider]["family"]
    pool.sync_catalog(provider, selected=[MODEL], auto_enroll=False, source="admin")
    pool.register_site_discovery(provider, lambda: [{"name": MODEL, "family": family},
                                                    {"name": "new-standard", "family": family}])
    assert pool.sync_catalog(provider, source="background")["new"] == []
    assert catalog.get_by_name("new-standard") is None
    assert catalog.get_by_name(MODEL)["is_rolled_out"] == 0


@pytest.mark.parametrize("fallback", [False, True])
def test_changed_model_admission_does_not_cool_down_unrelated_families(app, provider, fallback):
    family, heavy = NATIVE[provider]["family"], NATIVE[provider]["heavy"]
    first = add(provider, "changed", priority=20)
    second = add(provider, "fallback") if fallback else None
    observed = {str(first): observation(provider)}
    if second:
        observed[str(second)] = observation(provider)
    pool.register_site_discovery(provider, lambda: [{"name": MODEL, "family": family},
                                                    {"name": HEAVY, "family": heavy}])
    reports(provider, observed)
    calls = []

    def handler(account, name, messages, options):
        calls.append((account["id"], name))
        if account["id"] == first and name == MODEL:
            observed[str(first)] = observation(
                provider, model_limits={family: {"left": 0, "resets_at": time.time() + 100_000}})
            pool.refresh_quota(provider)
            raise pool.AdmissionChanged("private-provider-material")
        yield {"text": "Ready", "tokens_in": 8, "tokens_out": 2, "done": True}

    pool.register_site_chat(provider, handler)
    if fallback:
        assert list(pool.chat_stream(provider, MODEL, []))[-1]["done"]
        assert calls == [(first, MODEL), (second, MODEL)]
    else:
        with pytest.raises(RuntimeError, match="availability changed") as error:
            list(pool.chat_stream(provider, MODEL, []))
        assert "private-provider-material" not in str(error.value)
    row = accounts.get(first)
    assert row["status"] == "active" and row["cooldown_until"] is None and not accounts.leased(first)
    assert not pool.usable(row, family=family) and pool.usable(row, family=heavy)
    assert list(pool.chat_stream(provider, HEAVY, []))[-1]["done"]
    assert calls[-1] == (first, HEAVY)


def test_changed_admission_after_output_never_retries_or_globally_rests_account(app, provider):
    first, second = add(provider, "partial", priority=20), add(provider, "fallback")
    reports(provider, {str(first): observation(provider), str(second): observation(provider)})
    called = []

    def handler(account, name, messages, options):
        called.append(account["id"])
        yield {"text": "Partial", "tokens_in": 8}
        raise pool.AdmissionChanged("private-provider-material")

    pool.register_site_chat(provider, handler)
    stream = pool.chat_stream(provider, MODEL, [])
    assert next(stream)["text"] == "Partial"
    with pytest.raises(RuntimeError, match="availability changed") as error:
        next(stream)
    assert "private-provider-material" not in str(error.value)
    assert called == [first]
    row = accounts.get(first)
    assert row["status"] == "active" and row["cooldown_until"] is None and row["window_used"] > 0
    assert not accounts.leased(first)


def test_uncertain_shutdown_quarantines_account_before_releasing_lease(app, provider, warnings_log):
    first, second = add(provider, "uncertain", priority=20), add(provider, "healthy")
    called = []

    class ShutdownFailed(RuntimeError):
        requires_quarantine = True

    def handler(account, name, messages, options):
        called.append(account["id"])
        if account["id"] == first:
            raise ShutdownFailed("private-provider-material")
        yield {"text": "Ready", "done": True}

    pool.register_site_chat(provider, handler)
    assert list(pool.chat_stream(provider, MODEL, []))[-1]["done"]
    row = accounts.get(first)
    assert row["status"] == "disabled" and not accounts.leased(first)
    assert "Transport shutdown failed" in row["last_error"]
    assert called == [first, second]
    assert f"account {first} failed (ShutdownFailed)" in warnings_log.text
    assert "private-provider-material" not in row["last_error"] + warnings_log.text
