"""Subscription pool routing, publication and provider isolation, without provider credentials.

The best usable account answers first and an exhausted one rests while the next
serves; the chat, API and publication paths treat Claude and Codex models
alike; and each provider's adapter, observations, cooldowns and catalogue rows
never reach the other provider's accounts or models.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import pytest

from bananachat import db
from bananachat.db import catalog, limits as limits_db, settings as site_settings, subscription_pool as pool_db
from bananachat.services import limits, model_lifecycle, queue, subscription_pool

STANDARD = {"claude": "claude-sonnet-4-5", "codex": "gpt-6.1-sol"}
NATIVE = {"claude": "claude_code", "codex": "codex"}
OTHER = {"claude": "codex", "codex": "claude"}
FAMILIES = {"claude": ("haiku", "sonnet", "opus", "fable"), "codex": ("luna", "terra", "sol", "astra", "gpt")}


@pytest.fixture(autouse=True)
def declared_test_adapters(app):
    """Fake adapters declare availability and capabilities for both providers."""
    for provider in subscription_pool.PROVIDERS:
        subscription_pool.register_site_discovery(provider, (lambda p: lambda: [
            {**item, "reasoning": ["low", "medium", "high", "xhigh", "max"], "capabilities": ["completion"]}
            for item in subscription_pool.CURATED[p]])(provider))
        subscription_pool.register_site_chat(provider, lambda account, model, messages, options: iter([{"done": True}]))
    yield
    subscription_pool.reset_transport()


@pytest.fixture(params=subscription_pool.PROVIDERS)
def provider(request):
    return request.param


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


def _observe(provider, reports):
    """Publish one provider's fresh observations (fractions left of each allowance) for its accounts."""
    observed = {str(account_id): {"available": True, "source": NATIVE[provider], "observed_at": time.time(),
                                  "window_left": 1.0, "weekly_left": 1.0, **fields}
                for account_id, fields in reports.items()}
    subscription_pool.register_site_reporter(provider, lambda: {"account_reports": observed})
    return subscription_pool.refresh_quota(provider)[provider]


def test_effort_unlock_cascade_and_auto(app, provider):
    """Approving max allows high and below; the request flow already encodes this."""
    user = _user(app, "effort-user")
    with app.test_request_context():
        subscription_pool.sync_catalog(provider, selected=[STANDARD[provider]], source="test")
        model = catalog.get_by_name(STANDARD[provider])
        limits_db.set_effort_level(user["id"], model["id"], "max", source="admin", updated_by=None)
        allowed = limits.allowed_efforts(user, catalog.get(model["id"]))
        assert "high" in allowed and "medium" in allowed and "low" in allowed


def test_pooled_quota_sums_accounts_and_expired_reports_require_refresh(app, provider):
    with app.test_request_context():
        first = pool_db.add_account(f"{provider}-a", provider=provider, window_limit=100_000)
        second = pool_db.add_account(f"{provider}-b", provider=provider, window_limit=100_000)
        pool_db.report_quota(first, window_used=100_000, window_resets_at=_stamp(hours=2))
        quota = subscription_pool.refresh_quota(provider)[provider]
        # One exhausted subscription removes only its share.
        assert quota["window_left"] == 0.5 and quota["usable"] == 1
        assert subscription_pool.pick_account(provider)["id"] == second
        # An expired provider snapshot cannot prove a new allowance.
        pool_db.report_quota(first, window_resets_at=_stamp(hours=-1))
        assert subscription_pool.refresh_quota(provider)[provider]["window_left"] == 0.5
        pool_db.report_quota(first, window_used=0, window_resets_at=_stamp(hours=5))
        assert subscription_pool.refresh_quota(provider)[provider]["window_left"] == 1.0
        # Resting accounts count as empty; all resting means nothing is left.
        subscription_pool.rest(first, subscription_pool._now() + subscription_pool.COOLDOWN_ERROR)
        subscription_pool.rest(second, subscription_pool._now() + subscription_pool.COOLDOWN_ERROR)
        assert subscription_pool.refresh_quota(provider)[provider]["window_left"] == 0.0
        assert subscription_pool.pick_account(provider) is None


def test_highest_priority_account_answers_first(app, provider):
    with app.test_request_context():
        low = pool_db.add_account("low", provider=provider, priority=1, window_limit=100_000)
        high = pool_db.add_account("high", provider=provider, priority=10, window_limit=100_000)
        assert subscription_pool.pick_account(provider)["id"] == high
        pool_db.update_account(high, status="disabled")
        assert subscription_pool.pick_account(provider)["id"] == low


def test_an_exhausted_account_rests_and_the_next_one_answers(app, provider):
    calls = []

    def handler(account, model, messages, options):
        calls.append((account["label"], options.get("effort")))
        if account["label"] == "first":
            raise subscription_pool.QuotaExhausted("usage limit reached")
        yield {"text": "Hello ", "tokens_in": 7}
        yield {"text": "world", "tokens_out": 3, "done": True}

    subscription_pool.register_site_chat(provider, handler)
    with app.test_request_context():
        first = pool_db.add_account("first", provider=provider, priority=5, window_limit=100_000)
        second = pool_db.add_account("second", provider=provider, window_limit=100_000)
        chunks = list(subscription_pool.stream_chunks(provider, STANDARD[provider],
                                                      [{"role": "user", "content": "hi"}], effort="high"))
        assert "".join(chunk.content for chunk in chunks) == "Hello world"
        assert chunks[-1].done and (chunks[-1].prompt_tokens, chunks[-1].completion_tokens) == (7, 3)
        assert calls == [("first", "high"), ("second", "high")]
        assert subscription_pool._resting(pool_db.get(first)) and "quota" in pool_db.get(first)["last_error"]
        assert pool_db.get(second)["window_used"] == 10
        # The next request skips the resting account.
        calls.clear()
        list(subscription_pool.stream_chunks(provider, STANDARD[provider], [{"role": "user", "content": "hi"}]))
        assert [label for label, _ in calls] == ["second"]


def test_no_usable_account_is_an_upstream_error(app, provider):
    from bananachat.services.upstream import UpstreamError

    subscription_pool.register_site_chat(provider, lambda *args: iter(()))
    with app.test_request_context():
        try:
            list(subscription_pool.stream_chunks(provider, STANDARD[provider], []))
        except UpstreamError as error:
            assert "quota" in str(error)
        else:
            raise AssertionError("expected an upstream error")


@pytest.mark.parametrize("provider,current,strictest,family,next_strictest", [
    ("claude", {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"},
     "claude-fable-5-1", "fable", "claude-opus-5-5"),
    ("codex", {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-terra"}, "gpt-6-astra", "astra", "gpt-6.1-sol"),
])
def test_current_models_are_curated_with_family_strictness(app, provider, current, strictest, family, next_strictest):
    names = {item["name"] for item in subscription_pool.CURATED[provider]}
    assert current <= names
    assert subscription_pool.family_of(provider, strictest) == family
    first = subscription_pool.strict_policy(provider, strictest)
    second = subscription_pool.strict_policy(provider, next_strictest)
    assert first["window_tokens"] < second["window_tokens"] and first["weight"] > second["weight"]
    # Availability/capabilities come from actual discovery, not reference names.
    assert all("reasoning" not in item for item in subscription_pool.CURATED[provider])
    heavy = next(item for item in subscription_pool.discovered_models(provider)["models"] if item["name"] == strictest)
    assert "off" not in heavy["reasoning"]


def _publish(app, provider, name=None):
    name = name or STANDARD[provider]
    with app.test_request_context():
        subscription_pool.sync_catalog(provider, selected=[name], source="test")
        model = catalog.get_by_name(name)
        catalog.set_rollout(model["id"], True)
        limits_db.set_model_policy(model["id"], subscription_pool.strict_policy(provider, name), None)
        return catalog.get(model["id"])


def test_chat_with_a_subscription_model_goes_through_the_pool(app, make_user, fake_ollama, provider):
    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish(app, provider)
    seen = []

    def handler(account, model, messages, options):
        seen.append((account["label"], model, messages[-1]["content"]))
        yield {"text": "From the pool", "tokens_in": 4, "tokens_out": 2, "done": True}

    subscription_pool.register_site_chat(provider, handler)
    with app.app_context():
        pool_db.add_account(f"{provider}-1", provider=provider, window_limit=1_000_000)
    browser = user_browser(app, make_user)
    session_id = new_chat(browser)
    before = len(fake_ollama.chat_bodies())
    events = parse_sse(send(browser, session_id, "Hi pool", model=STANDARD[provider]).get_data(as_text=True))
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "From the pool"
    assert events[-1]["state"] == "completed"
    assert seen == [(f"{provider}-1", STANDARD[provider], "Hi pool")]
    assert len(fake_ollama.chat_bodies()) == before  # never sent to Ollama
    with app.app_context():
        assert pool_db.get_by_label(f"{provider}-1")["window_used"] == 6


def test_used_up_subscription_model_falls_back_to_a_local_model(app, make_user, fake_ollama, provider):
    from bananachat.db import settings

    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish(app, provider)  # no subscription account: nothing left in the pool
    browser = user_browser(app, make_user)
    session_id = new_chat(browser)
    events = parse_sse(send(browser, session_id, "Hello", model=STANDARD[provider]).get_data(as_text=True))
    start = next(event for event in events if event["type"] == "start")
    assert start["model"] != STANDARD[provider]
    assert events[-1]["state"] == "completed"
    assert "".join(event["text"] for event in events if event["type"] == "delta") == fake_ollama.reply

    # With cloud → local switched off, the request is refused instead.
    with app.app_context():
        settings.update(quota_fallback_to_local=0)
    response = send(browser, session_id, "Again", model=STANDARD[provider])
    assert response.status_code == 429


def test_used_up_local_tokens_fall_back_to_a_subscription_model(app, make_user, fake_ollama, provider):
    from bananachat.db import limits as limits_store
    from bananachat.db import settings

    from tests.app.test_chat import new_chat, parse_sse, send, setup_models, user_browser

    setup_models(app)
    _publish(app, provider)
    subscription_pool.register_site_chat(provider, lambda *args: iter([{"text": "Cloud answer", "tokens_out": 2,
                                                                       "done": True}]))
    with app.app_context():
        pool_db.add_account(f"{provider}-1", provider=provider, window_limit=1_000_000)
        config = limits_store.get_policy("chat")
        config["window"].update(enabled=True, tokens=1000, slow_tokens=0)
        limits_store.set_policy("chat", config, None)
        settings.update(slow_credits_enabled=0, chat_local_token_consumption=1)
        local = db.one("SELECT * FROM ai_models WHERE backend='ollama' ORDER BY sort_order LIMIT 1")
    browser = user_browser(app, make_user)
    with app.app_context():
        from bananachat.db import users
        user = users.get_by_username("alice")
        db.execute("INSERT INTO credit_ledger (user_id, model_id, credits_used, tokens_in, tokens_out, request_type, "
                   "created_at) VALUES (?,?,?,?,0,'chat',?)", (user["id"], local["id"], 5.0, 5000, db.now()))
        limits_store.open_windows(user["id"], [limits_store.pool_scope("chat")])
    session_id = new_chat(browser)
    events = parse_sse(send(browser, session_id, "Hello", model=local["ollama_name"]).get_data(as_text=True))
    start = next(event for event in events if event["type"] == "start")
    assert start["model"] == STANDARD[provider]
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "Cloud answer"


@pytest.mark.parametrize("provider,name", [("claude", "claude-test-sonnet"), ("codex", "gpt-test-sol")])
def test_configured_adapter_routes_api_extra_and_shares_account_rate_between_keys(
        make_app, monkeypatch, provider, name):
    from bananachat import security
    from bananachat.db import users
    from tests.app.test_api import assert_error, chat, token_for

    calls = []

    def provider_chat(account, model, messages, options, *, cancel):
        cancel.check()
        calls.append((account["id"], model, options["effort"]))
        yield {"text": "Hello", "tokens_in": 4, "tokens_out": 6, "done": True}

    module = ModuleType(f"bc_{provider}_api_provider_fixture")
    module.create_adapter = lambda config: SimpleNamespace(
        chat=provider_chat,
        discover=lambda: [{"name": name, "reasoning": ["low", "medium", "high", "xhigh"]}],
        quota=lambda: {"window_left": 1.0, "weekly_left": None},
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = make_app(**{f"{provider.upper()}_EXTENSION": module.__name__})
    assert subscription_pool.connected(provider) and not subscription_pool.connected(OTHER[provider])
    with app.app_context():
        users.create("adapter-api", security.hash_password("adapter-api-password"), role="user")
        user = users.get_by_username("adapter-api")
        account_id = pool_db.add_account("provider-fixture", provider=provider, window_limit=100_000)
        subscription_pool.sync_catalog(provider, selected=[name], source="test")
        model = catalog.get_by_name(name)
        catalog.set_rollout(model["id"], True)
        catalog.set_lifecycle(model["id"], enrollment="reviewed")
        limits_db.set_effort_level(user["id"], model["id"], "extra", source="admin")
        limits_db.set_override(user["id"], "api", None, rate_rules=[{"requests": 1, "per": "hour", "burst": 1}])
    first = token_for(app, user, "first")
    second = token_for(app, user, "second")
    response = chat(app, first, model=model["ollama_name"], reasoning_effort="xhigh")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json["choices"][0]["message"]["content"] == "Hello"
    assert response.json["usage"]["total_tokens"] == 10
    assert calls == [(account_id, model["ollama_name"], "extra")]
    with app.app_context():
        assert pool_db.get(account_id)["window_used"] == 10
        assert db.scalar("SELECT COUNT(*) FROM subscription_account_leases") == 0
        assert limits.effective(user, "api").window.used == 0
        assert limits.model_limits(user, catalog.get(model["id"])).window.used == 10
    assert_error(chat(app, second, model=model["ollama_name"]), 429, "rate_limit_error")
    assert len(calls) == 1


def _discovered(provider, family):
    # Discovery supplies the family; opaque provider ids cannot be inferred from a name.
    name = f"opaque-{provider}-{family}"
    subscription_pool.register_site_discovery(provider, lambda: [{"name": name, "family": family,
                                                                  "reasoning": ["low", "medium", "high"],
                                                                  "capabilities": ["completion"]}])
    subscription_pool.sync_catalog(provider, selected=[name], source="admin")
    return catalog.get_by_name(name)


@pytest.mark.parametrize("provider,family", [(provider, family) for provider in FAMILIES
                                             for family in FAMILIES[provider]])
def test_publication_keeps_family_budget_and_reasoning_defaults(app, provider, family):
    with app.app_context():
        row = _discovered(provider, family)
        before = limits_db.get_model_policy(row)
        model_lifecycle.enable(row)
        published = catalog.get(row["id"])
        assert published["is_rolled_out"]
        assert published["limit_preset"] == subscription_pool.strict_policy(provider, "opaque", family=family)["preset"]
        assert limits_db.get_model_policy(published) == before


@pytest.mark.parametrize("provider,family", [(provider, family) for provider in FAMILIES
                                             for family in FAMILIES[provider]])
def test_explicit_preset_uses_subscription_defaults_and_keeps_switches(app, provider, family):
    with app.app_context():
        row = _discovered(provider, family)
        current = {**limits_db.get_model_policy(row), "auto_tiers": True, "counts_toward_pool": True,
                   "tokens_enabled": False, "rate_enabled": False, "effort_gating_off": True}
        limits_db.set_model_policy(row["id"], current, None)
        preset = model_lifecycle.preset_for(row)
        assert model_lifecycle.apply_limit_preset(row["id"], preset)
        after = limits_db.get_model_policy(catalog.get(row["id"]))
        strict = subscription_pool.strict_policy(provider, "opaque", family=family)
        for key in ("enabled", "window_tokens", "weekly_tokens", "weight", "rate_rules", "dynamic",
                    "sensitivity", "effort_default"):
            assert after[key] == strict[key]
        for key in ("auto_tiers", "counts_toward_pool", "tokens_enabled", "rate_enabled", "effort_gating_off"):
            assert after[key] == current[key]


@pytest.mark.parametrize("explicit_current_preset", [False, True])
def test_review_and_republication_preserve_custom_subscription_model_limits(app, provider, explicit_current_preset):
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        preset = model_lifecycle.preset_for(row)
        custom = {**limits_db.get_model_policy(row), "preset": "custom", "window_tokens": 1234,
                  "weekly_tokens": 9876, "weight": 2, "counts_toward_pool": True,
                  "rate_rules": [{"requests": 2, "per": "hour", "burst": 1}],
                  "effort_default": "low", "effort_gating_off": True}
        custom = limits_db.set_model_policy(row["id"], custom, None)
        model_lifecycle.enable(row, preset=preset if explicit_current_preset else None)
        catalog.set_lifecycle(row["id"], is_rolled_out=0)
        model_lifecycle.enable(catalog.get(row["id"]), preset=preset)
        assert limits_db.get_model_policy(catalog.get(row["id"])) == custom


@pytest.mark.parametrize("provider,window_tokens", [("claude", 50_000), ("codex", 30_000)])
def test_different_publication_preset_applies_strict_new_budget(app, provider, window_tokens):
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        model_lifecycle.enable(row, preset="heavy")
        after = limits_db.get_model_policy(catalog.get(row["id"]))
        assert after["enabled"] and after["window_tokens"] == window_tokens
        assert after["counts_toward_pool"] is False


def test_publication_installs_limits_when_policy_is_missing(app, provider):
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        limits_db.clear_model_policy(row["id"])
        model_lifecycle.enable(row)
        policy = limits_db.get_model_policy(catalog.get(row["id"]))
        assert policy["enabled"] and policy["window_tokens"] == 150_000
        assert policy["counts_toward_pool"] is False


def test_subscription_model_uses_a_queue_slot_without_local_model_memory(app, monkeypatch, provider):
    monkeypatch.setattr(queue, "_memory_ok", lambda: False)
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        assert queue._model_memory_ok(row["ollama_name"])
        assert not queue._model_memory_ok("unregistered-local-model")
        with queue.Slot(queue.PRIORITY_CHAT, model=row["ollama_name"]) as slot:
            assert slot._try_start()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_subscription_request_can_run_while_local_queue_head_waits_for_memory(app, monkeypatch, provider):
    monkeypatch.setattr(queue, "_memory_ok", lambda: False)
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        with queue.Slot(queue.PRIORITY_CHAT, model="local-queue-head") as local:
            with queue.Slot(queue.PRIORITY_CHAT, model=row["ollama_name"]) as hosted:
                assert not local._try_start()
                assert hosted._try_start()
                assert not local._try_start()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_direct_limits_preset_uses_subscription_budget(app, provider):
    with app.app_context():
        row = _discovered(provider, subscription_pool.PRESET_FAMILIES[provider]["standard"])
        policy = limits.apply_model_preset(row["id"], "standard")
        assert policy["enabled"] and policy["window_tokens"] == 150_000
        assert policy["rate_rules"] and policy["dynamic"]


def test_capacity_hooks_follow_each_providers_own_pool(app):
    subscription_pool.ensure_registered()
    with app.app_context():
        claude = _discovered("claude", "sonnet")
        codex = _discovered("codex", "sol")
        pool_db.add_account("claude-only", provider="claude", window_limit=100_000)
        subscription_pool.refresh_quota()
        assert limits.capacity_factor(claude) == 1.0
        # A Claude account is no capacity for a Codex model.
        assert limits.capacity_factor(codex) == 0.0


def test_one_providers_observations_never_reach_another_providers_accounts(app, provider):
    with app.app_context():
        own = pool_db.add_account("own", provider=provider, window_limit=100_000)
        foreign = pool_db.add_account("foreign", provider=OTHER[provider], window_limit=100_000)
        _observe(provider, {own: {"window_left": 0.4}, foreign: {"window_left": 0}})
        assert set(subscription_pool.account_reports(provider)) == {str(own)}
        # The other provider has no reporter: its account keeps local-only routing.
        assert subscription_pool.account_reports(OTHER[provider]) is None
        assert subscription_pool.usable(pool_db.get(foreign))
        assert [row["id"] for row in subscription_pool.accounts_in_order(OTHER[provider])] == [foreign]
        assert [row["id"] for row in subscription_pool.accounts_in_order(provider)] == [own]


@pytest.mark.parametrize("marker", [{"available": True}, {"available": False, "status": "stale"}])
def test_a_report_labelled_with_another_providers_transport_fails_closed(app, provider, marker):
    with app.app_context():
        account = pool_db.add_account("mislabelled", provider=provider, window_limit=100_000)
        subscription_pool.register_site_reporter(provider, lambda: {"account_reports": {str(account): {
            "source": NATIVE[OTHER[provider]], "observed_at": time.time(), "window_left": 0.9,
            "weekly_left": 0.9, **marker}}})
        subscription_pool.refresh_quota(provider)
        saved = subscription_pool.account_reports(provider)[str(account)]
        assert saved == {"source": NATIVE[provider], "available": False, "status": "unavailable"}
        assert not subscription_pool.usable(pool_db.get(account))


@pytest.mark.parametrize("provider,plan,label,saved", [
    ("claude", "plus", None, (None, None)),
    ("claude", "max", "ChatGPT Pro (Max)", ("max", "Claude Max")),
    ("codex", "max", "Claude Max 20x", (None, None)),
    ("codex", "plus", "ChatGPT Pro (More)", ("plus", "ChatGPT Plus")),
    ("codex", "pro", "ChatGPT Pro (Max)", ("pro", "ChatGPT Pro (Max)")),
])
def test_only_the_providers_own_plans_and_verified_tiers_are_kept(app, provider, plan, label, saved):
    with app.app_context():
        account = pool_db.add_account("plan", provider=provider, window_limit=100_000)
        _observe(provider, {account: {"subscription_type": plan, "plan_label": label}})
        report = subscription_pool.account_reports(provider)[str(account)]
        assert (report["subscription_type"], report["plan_label"]) == saved


def test_another_providers_family_limit_is_not_kept_in_an_observation(app, provider):
    foreign = subscription_pool.PRESET_FAMILIES[OTHER[provider]]["standard"]
    with app.app_context():
        account = pool_db.add_account("foreign-family", provider=provider, window_limit=100_000)
        _observe(provider, {account: {"model_limits": {foreign: {"left": 0, "resets_at": None}}}})
        assert subscription_pool.account_reports(provider)[str(account)]["model_limits"] == {}
        family = subscription_pool.PRESET_FAMILIES[provider]["standard"]
        assert subscription_pool.usable(pool_db.get(account), family=family)


def test_a_model_restricted_to_another_providers_account_has_no_capacity(app, provider):
    called = []

    def foreign_chat(account, model, messages, options):
        called.append(account["id"])
        yield {"done": True}

    subscription_pool.register_site_chat(OTHER[provider], foreign_chat)
    with app.app_context():
        foreign = pool_db.add_account("foreign-restricted", provider=OTHER[provider], window_limit=100_000)
        pool_db.add_account("own-unlisted", provider=provider, window_limit=100_000)
        subscription_pool.register_site_discovery(provider, lambda: [{
            "name": STANDARD[provider], "family": subscription_pool.PRESET_FAMILIES[provider]["standard"],
            "account_ids": [foreign]}])
        subscription_pool.sync_catalog(provider, selected=[STANDARD[provider]], source="test")
        assert subscription_pool.capacity_report(catalog.get_by_name(STANDARD[provider])).window_left == 0
        with pytest.raises(subscription_pool.CapacityUnavailable, match="currently at capacity"):
            list(subscription_pool.chat_stream(provider, STANDARD[provider], []))
        assert called == [] and not pool_db.leased(foreign)


def test_a_discovered_id_owned_by_another_provider_is_refused_without_changes(app, provider):
    other = OTHER[provider]
    with app.app_context():
        subscription_pool.sync_catalog(other, selected=[STANDARD[other]], source="test")
        before = dict(catalog.get_by_name(STANDARD[other]))
        subscription_pool.register_site_discovery(provider, lambda: [{
            "name": STANDARD[other], "family": subscription_pool.PRESET_FAMILIES[provider]["standard"]}])
        with pytest.raises(ValueError, match="different model backend"):
            subscription_pool.sync_catalog(provider, selected=[STANDARD[other]], source="admin")
        assert dict(catalog.get_by_name(STANDARD[other])) == before
        assert site_settings.state_get(f"{provider}.selected_models") is None
        assert site_settings.state_get(f"{other}.selected_models") == [STANDARD[other]]


def test_one_providers_catalogue_sync_never_withdraws_the_other_providers_models(app, provider):
    other = OTHER[provider]
    with app.app_context():
        subscription_pool.sync_catalog(other, selected=[STANDARD[other]], source="test")
        excluded = site_settings.state_get(f"{other}.excluded_models")
        subscription_pool.sync_catalog(provider, selected=[STANDARD[provider]], source="test")
        result = subscription_pool.sync_catalog(provider, selected=[], source="admin")
        assert result["missing"] == [STANDARD[provider]]
        row = catalog.get_by_name(STANDARD[other])
        assert row["backend_available"] and not row["missing_at"]
        assert site_settings.state_get(f"{other}.excluded_models") == excluded
        assert site_settings.state_get(f"{other}.selected_models") == [STANDARD[other]]


def test_invalidating_one_provider_keeps_the_other_providers_discovery(app, provider):
    calls = {name: 0 for name in subscription_pool.PROVIDERS}

    def counted(name):
        def discover():
            calls[name] += 1
            return [{"name": STANDARD[name], "family": subscription_pool.PRESET_FAMILIES[name]["standard"]}]
        return discover

    with app.app_context():
        for name in subscription_pool.PROVIDERS:
            subscription_pool.register_site_discovery(name, counted(name))
            subscription_pool.discovered_models(name)
        subscription_pool.invalidate(provider)
        for name in subscription_pool.PROVIDERS:
            assert subscription_pool.discovered_models(name)["ok"]
        assert calls == {provider: 2, OTHER[provider]: 1}


def test_records_after_the_terminal_are_never_read_emitted_or_charged(app, provider):
    late = []

    def handler(account, model, messages, options):
        yield {"text": "Answer", "tokens_in": 4, "tokens_out": 2, "done": True}
        late.append(account["id"])
        yield {"text": "After the end", "tokens_out": 500}
        yield {"done": True}

    subscription_pool.register_site_chat(provider, handler)
    with app.app_context():
        account = pool_db.add_account("one-terminal", provider=provider, window_limit=100_000)
        chunks = list(subscription_pool.stream_chunks(provider, STANDARD[provider], []))
        assert [chunk.content for chunk in chunks] == ["Answer"] and chunks[0].done
        assert chunks[0].completion_tokens == 2 and late == []
        assert pool_db.get(account)["window_used"] == 6 and not pool_db.leased(account)
        assert db.scalar("SELECT COUNT(*) FROM subscription_usage") == 1


@pytest.mark.parametrize("caps,observed", [
    ({"window_cap_pct": 70}, None),
    ({"window_cap_pct": 70}, {"source": "local"}),
    ({"window_cap_pct": 70}, {"source": "file", "window_left": None}),
    ({"weekly_cap_pct": 90}, {"weekly_left": None}),
])
def test_a_percentage_cap_on_an_unreported_allowance_never_serves(app, provider, caps, observed):
    with app.app_context():
        account = pool_db.add_account("blind-cap", provider=provider, window_limit=100_000, **caps)
        if observed is not None:
            _observe(provider, {account: observed})
        assert not subscription_pool.usable(pool_db.get(account))
        assert subscription_pool.pick_account(provider) is None
        # The same cap serves once the provider reports the allowance it limits.
        _observe(provider, {account: {"window_left": 0.9, "weekly_left": 0.95}})
        assert subscription_pool.usable(pool_db.get(account))


def test_a_family_share_cap_without_that_familys_report_denies_only_that_family(app, provider):
    heavy = subscription_pool.PRESET_FAMILIES[provider]["heavy"]
    standard = subscription_pool.PRESET_FAMILIES[provider]["standard"]
    with app.app_context():
        account = pool_db.add_account("family-blind", provider=provider, window_limit=100_000)
        pool_db.set_scoped_cap(account, heavy, cap_pct=50)
        _observe(provider, {account: {}})
        row = pool_db.get(account)
        assert not subscription_pool.usable(row, family=heavy)
        assert subscription_pool.usable(row, family=standard) and subscription_pool.usable(row)


def test_requests_for_one_provider_never_lease_another_providers_account(app, provider):
    called = []

    def foreign_chat(account, model, messages, options):
        called.append(account["id"])
        yield {"done": True}

    subscription_pool.register_site_chat(OTHER[provider], foreign_chat)
    with app.app_context():
        foreign = pool_db.add_account("foreign-only", provider=OTHER[provider], window_limit=100_000)
        with pytest.raises(subscription_pool.CapacityUnavailable):
            list(subscription_pool.chat_stream(provider, STANDARD[provider], []))
        with pytest.raises(subscription_pool.CapacityUnavailable):
            list(subscription_pool.chat_stream(provider, STANDARD[provider], [], account_id=foreign))
        assert called == [] and not pool_db.leased(foreign)
        assert pool_db.get(foreign)["cooldown_until"] is None


def test_exhaustion_on_one_provider_leaves_the_other_serving(app, provider):
    def exhausted(account, model, messages, options):
        raise subscription_pool.QuotaExhausted("usage limit reached")
        yield

    subscription_pool.register_site_chat(provider, exhausted)
    with app.app_context():
        own = pool_db.add_account("exhausted", provider=provider, window_limit=100_000)
        other = pool_db.add_account("serving", provider=OTHER[provider], window_limit=100_000)
        with pytest.raises(subscription_pool.CapacityUnavailable, match="no available quota"):
            list(subscription_pool.chat_stream(provider, STANDARD[provider], []))
        assert subscription_pool._resting(pool_db.get(own)) and not subscription_pool._resting(pool_db.get(other))
        assert subscription_pool.refresh_quota(OTHER[provider])[OTHER[provider]]["window_left"] == 1.0
        assert list(subscription_pool.chat_stream(OTHER[provider], STANDARD[OTHER[provider]], []))[-1]["done"]


def test_disconnecting_one_provider_keeps_the_other_connected(app, provider):
    with app.app_context():
        pool_db.add_account("own", provider=provider, window_limit=100_000)
        pool_db.add_account("other", provider=OTHER[provider], window_limit=100_000)
        subscription_pool.reset_transport(provider)
        assert not subscription_pool.connected(provider) and subscription_pool.connected(OTHER[provider])
        assert subscription_pool.refresh_quota(provider)[provider]["window_left"] == 0.0
        assert subscription_pool.refresh_quota(OTHER[provider])[OTHER[provider]]["window_left"] == 1.0
        with pytest.raises(RuntimeError, match="not connected"):
            list(subscription_pool.chat_stream(provider, STANDARD[provider], []))


def test_withdrawing_one_provider_keeps_the_other_providers_models(app, provider):
    with app.app_context():
        subscription_pool.sync_catalog(provider, selected=[STANDARD[provider]], source="test")
        subscription_pool.sync_catalog(OTHER[provider], selected=[STANDARD[OTHER[provider]]], source="test")
        assert subscription_pool.withdraw(provider) == [STANDARD[provider]]
        own, other = catalog.get_by_name(STANDARD[provider]), catalog.get_by_name(STANDARD[OTHER[provider]])
        assert not own["backend_available"] and not own["missing_at"]
        assert other["backend_available"] and not other["state_reason"].startswith("No adapter")


def test_reprobe_clears_only_that_providers_expired_cooldowns(app, provider):
    with app.app_context():
        expired = pool_db.add_account("expired", provider=provider, window_limit=100_000)
        resting = pool_db.add_account("resting", provider=provider, window_limit=100_000)
        foreign = pool_db.add_account("foreign", provider=OTHER[provider], window_limit=100_000)
        now = subscription_pool._now()
        subscription_pool.rest(expired, now - timedelta(minutes=1))
        subscription_pool.rest(resting, now + timedelta(hours=1))
        subscription_pool.rest(foreign, now - timedelta(minutes=1))
        result = subscription_pool.reprobe(provider)
        assert set(result) == {provider} and result[provider]["cleared"] == 1
        assert pool_db.get(expired)["cooldown_until"] is None
        assert subscription_pool._resting(pool_db.get(resting))
        assert pool_db.get(foreign)["cooldown_until"] is not None


def test_family_caps_apply_to_each_providers_own_families(app, provider):
    strict = subscription_pool.PRESET_FAMILIES[provider]["heavy"]
    standard = subscription_pool.PRESET_FAMILIES[provider]["standard"]
    with app.app_context():
        account = pool_db.add_account("family-cap", provider=provider, window_limit=1_000_000)
        pool_db.set_scoped_cap(account, strict, cap_pct=60)
        _observe(provider, {account: {"model_limits": {strict: {"left": 0.3, "resets_at": None}}}})
        row = pool_db.get(account)
        assert subscription_pool.usable(row, family=strict) is False
        assert subscription_pool.usable(row, family=standard) is True
        pool_db.set_scoped_cap(account, standard, window_cap=100)
        subscription_pool.record_usage(account, 60, 60, provider=provider, family=standard, lease_id="S1")
        assert subscription_pool.usable(pool_db.get(account), family=standard) is False
        assert subscription_pool.usable(pool_db.get(account)) is True
