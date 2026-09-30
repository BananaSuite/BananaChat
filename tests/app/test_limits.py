"""The limit system: policies, request-rate rules, 5-hour and weekly windows, tiers, dynamic limits, grants,
speed, quota requests, administrator actions, API headers and the account page. Everything is in tokens.

Model limits, weights, presets, reasoning effort and the migration are covered in ``test_limits_tokens.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def _signed_in(app, username, password=None, *, language="en"):
    from bananachat.db import users

    with app.app_context():
        account = users.get_by_username(username)
        prefs = users.get_preferences(account["id"])
        prefs["interface_language"] = language
        users.save_preferences(account["id"], prefs)
    browser = Browser(app)
    browser.login(username, password)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


def _charge(user_id, tokens, *, request_type="api", at=None, slow=False):
    """A ledger row of *tokens* at *at* (a datetime, default now), as if charged then: the pool's windows open."""
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store

    at = at or datetime.now(timezone.utc)
    db.execute("INSERT INTO credit_ledger (user_id, credits_used, is_slow, tokens_in, tokens_out, request_type, "
               "created_at) VALUES (?,?,?,?,0,?,?)",
               (user_id, tokens / 1000, int(slow), int(tokens), request_type, db.timestamp(at)))
    store.open_windows(user_id, [store.pool_scope(credits.pool_of(request_type))], at)


def _policy(pool, **sections):
    """Update parts of a pool policy: ``_policy("api", window={"tokens": 5000})``."""
    from bananachat.db import limits

    config = limits.get_policy(pool)
    for scope, values in sections.items():
        config[scope].update(values)
    limits.set_policy(pool, config, None)


def _rules(*rules):
    return [{"requests": requests, "per": per, **({"burst": burst} if burst is not None else {})}
            for requests, per, burst in rules]


# ----- the agent pool ---------------------------------------------------------------

def test_the_agent_pool_charges_and_is_unlimited_by_default(app, make_user):
    from bananachat.db import credits

    user = make_user("agnes")
    with app.app_context():
        used, slow = credits.charge(user["id"], 2000, 1000, request_type="agent")
        assert used == 3000 and slow is False
        budget = credits.budget(user, "agent")
        assert budget.unlimited and budget.available
        assert credits.usage_today(user["id"], "agent") == (3000, 0.0)
        assert credits.usage_today(user["id"], "api") == (0.0, 0.0)
        _policy("agent", window={"enabled": True, "tokens": 5000, "slow_tokens": 0})
        budget = credits.budget(user, "agent")
        assert not budget.unlimited and budget.regular_used == 3000 and budget.regular_left == 2000
        credits.charge(user["id"], 2000, 0, request_type="agent")
        assert not credits.budget(user, "agent").available
        assert credits.budget(user, "api").available  # pools are separate
        assert [row["request_type"] for row in credits.history(user["id"], pool="agent")] == ["agent", "agent"]


def test_policies_are_validated_on_write(app):
    from bananachat.db import limits

    with app.app_context():
        good = limits.get_policy("api")
        for scope, key, value in (("rate", "rules", _rules((0, "second", None))),
                                  ("rate", "rules", _rules((1, "second", 0))),
                                  ("rate", "rules", _rules((1.5, "second", None))),
                                  ("window", "tokens", -1), ("weekly", "tokens", 2_000_000_000),
                                  ("rate", "rules", "fast")):
            bad = {section: dict(values) for section, values in good.items() if isinstance(values, dict)}
            bad[scope][key] = value
            with pytest.raises(ValueError):
                limits.set_policy("api", bad, None)
        with pytest.raises(ValueError):
            limits.set_policy("images", good, None)
        assert limits.get_policy("api") == good


# ----- request rate (token buckets) -------------------------------------------------------

def test_a_token_bucket_refills_at_its_rate_up_to_the_burst(app):
    from bananachat.db import limits

    with app.app_context():
        assert limits.take_token("t", 2.0, 2, 1000.0)[:2] == (True, 1.0)
        assert limits.take_token("t", 2.0, 2, 1000.0)[:2] == (True, 0.0)
        allowed, tokens, retry = limits.take_token("t", 2.0, 2, 1000.0)
        assert not allowed and retry == pytest.approx(0.5)
        assert limits.take_token("t", 2.0, 2, 1000.5)[0] is True
        # A long pause fills the bucket up to the burst, not beyond.
        assert limits.take_token("t", 2.0, 2, 5000.0)[1] == pytest.approx(1.0)
        assert limits.purge_buckets(60, 5100.0) == 1


def test_the_request_rate_is_shared_by_app_processes_on_one_database(make_app, make_user):
    from bananachat.db import tokens, users

    first = make_app()
    second = make_app(setup=False, INSTANCE_DIR=str(first.config["BC"].instance_dir))
    with first.app_context():
        from bananachat import security
        user_id = users.create("rita", security.hash_password("rita-password"))
        _policy("api", rate={"rules": _rules((1, "day", 3))})
        raw = tokens.create(user_id, "cli")[1]
    headers = {"Authorization": f"Bearer {raw}"}
    seen = []
    for index, current in enumerate((first, second, first, second)):
        response = current.test_client().get("/v1/models", headers=headers)
        seen.append(response.status_code)
        if index < 3:
            assert response.headers["x-ratelimit-limit-requests"] == "3"
            assert response.headers["x-ratelimit-remaining-requests"] == str(2 - index)
            assert response.headers["x-ratelimit-reset-requests"].endswith("s")
    assert seen == [200, 200, 200, 429]
    assert response.json["error"]["code"] == "rate_limit_exceeded"
    assert response.json["error"]["type"] == "rate_limit_error"
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["x-ratelimit-remaining-requests"] == "0"


def test_rate_headers_describe_the_bucket_and_administrators_have_none(app, make_user):
    from bananachat.db import tokens, users

    user = make_user("hugo")
    with app.app_context():
        _policy("api", rate={"rules": _rules((30, "minute", 4))})
        raw = tokens.create(user["id"], "cli")[1]
        admin_raw = tokens.create(users.get_by_username("admin")["id"], "cli")[1]
    response = app.test_client().get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit-requests"] == "4"
    assert response.headers["x-ratelimit-remaining-requests"] == "3"
    assert response.headers["x-ratelimit-reset-requests"] == "2s"
    response = app.test_client().get("/v1/models", headers={"Authorization": f"Bearer {admin_raw}"})
    assert response.status_code == 200 and "x-ratelimit-limit-requests" not in response.headers
    assert "x-ratelimit-limit-tokens" not in response.headers


def test_rate_limits_can_be_switched_off_per_pool(app, make_user):
    from bananachat.services import limits

    user = make_user("ivy")
    with app.app_context():
        _policy("chat", rate={"enabled": False})
        assert all(limits.check_rate(user, "chat").allowed for _ in range(20))
        assert not limits.check_rate(user, "chat").limited
        # A custom rate still applies (an administrator slowed the account down).
        from bananachat.db import limits as store
        store.set_override(user["id"], "chat", None, rate_rules=_rules((36, "hour", 1)))
        assert limits.check_rate(user, "chat").allowed
        refused = limits.check_rate(user, "chat")
        assert not refused.allowed and refused.retry_after == 100


def test_durations_use_the_openai_header_format():
    from bananachat.services.limits import duration

    assert duration(0) == "0s"
    assert duration(0.25) == "0.25s"
    assert duration(2) == "2s"
    assert duration(90) == "1m30s"
    assert duration(3600) == "1h0m0s"


# ----- 5-hour and weekly windows ------------------------------------------------------------

def test_usage_counts_inside_the_open_windows(app, make_user):
    from bananachat.services import limits

    user = make_user("walt")
    now = datetime.now(timezone.utc)
    with app.app_context():
        _policy("api", weekly={"enabled": True, "tokens": 50_000})
        _charge(user["id"], 7000, at=now - timedelta(days=10))                   # an earlier week
        _charge(user["id"], 4000, at=now - timedelta(days=2))                    # opens this week
        _charge(user["id"], 3000, at=now - timedelta(hours=1))                   # opens this 5-hour window
        _charge(user["id"], 2000, at=now - timedelta(minutes=30), slow=True)
        _charge(user["id"], 5000, at=now - timedelta(minutes=30), request_type="chat")
        current = limits.effective(user, "api")
        assert (current.window.used, current.window.slow_used, current.weekly.used) == (3000, 2000, 9000)
        assert current.window.resets_at - (now - timedelta(hours=1)) < timedelta(hours=5, seconds=2)
        assert current.weekly.resets_at - current.weekly.starts_at == timedelta(days=7)
        budget = current.budget()
        assert budget.regular_left == 27_000 and budget.weekly_left == 41_000


def test_weekly_limits_are_off_by_default_and_block_without_a_slow_lane(app, make_user):
    from bananachat.db import credits

    user = make_user("wes")
    with app.app_context():
        assert credits.budget(user, "api").weekly_limit is None
        _policy("api", weekly={"enabled": True, "tokens": 12_000})
        _charge(user["id"], 10_000)
        budget = credits.budget(user, "api")
        assert budget.regular_left == 2000 and budget.slow_left == 2000  # weekly tokens cap both
        _charge(user["id"], 2000)
        budget = credits.budget(user, "api")
        assert budget.weekly_exhausted and not budget.available
        assert budget.slow_limit == 15_000 and budget.slow_used == 0  # slow tokens are left, but blocked
        assert budget.blocked_until == budget.weekly_resets_at
        assert budget.seconds_until_reset() >= 7 * 86_400 - 120


def test_usage_resets_start_counting_afresh_without_deleting_the_ledger(app, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store

    user, other = make_user("rene"), make_user("rosa")
    with app.app_context():
        _policy("api", weekly={"enabled": True, "tokens": 100_000})
        _charge(user["id"], 20_000, at=datetime.now(timezone.utc) - timedelta(seconds=5))
        _charge(other["id"], 20_000, at=datetime.now(timezone.utc) - timedelta(seconds=5))
        store.reset_usage(user["id"], weekly=False, updated_by=None)
        budget = credits.budget(user, "api")
        assert budget.regular_used == 0 and budget.weekly_used == 20_000  # the 5-hour reset keeps the week
        store.reset_usage(user["id"], weekly=True, updated_by=None)
        assert credits.budget(user, "api").weekly_used == 0
        assert credits.budget(other, "api").regular_used == 20_000
        store.reset_usage(None, weekly=True, updated_by=None)  # everyone
        budget = credits.budget(other, "api")
        assert budget.regular_used == 0 and budget.weekly_used == 0
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger", default=0) == 2
        credits.charge(other["id"], 1000, 0, request_type="api")
        assert credits.budget(other, "api").regular_used == 1000


def test_custom_limits_override_the_policy_and_admins_are_unlimited(app, make_user):
    from bananachat.db import credits, users
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("otto")
    with app.app_context():
        store.set_override(user["id"], "api", None, window_tokens=80_000)
        current = limits.effective(user, "api")
        assert current.window.tokens == 80_000 and current.window.slow_tokens == 15_000  # slow follows the policy
        assert current.window.custom and current.window.reasons[0].code == "custom"
        assert credits.get_quota(user["id"]) == (80_000, 15_000)
        with pytest.raises(ValueError):
            store.set_override(user["id"], "api", None, rate_rules=_rules((1, "fortnight", None)))
        admin = users.get_by_username("admin")
        for pool in ("api", "chat", "agent"):
            current = limits.effective(admin, pool)
            assert current.admin and current.unlimited and not current.rate.limited
            assert credits.budget(admin, pool).unlimited


# ----- tiers -------------------------------------------------------------------------------

def _age(user_id, days):
    from bananachat import db

    db.execute("UPDATE users SET created_at=? WHERE id=?", (db.now(-timedelta(days=days)), user_id))


def _active(user_id, days, tokens_each):
    now = datetime.now(timezone.utc)
    for day in range(days):
        _charge(user_id, tokens_each, at=now - timedelta(days=day, hours=1))


def test_tiers_promote_automatically_upwards_only(app, make_user):
    from bananachat import db
    from bananachat.db import credits, users
    from bananachat.db import limits as store
    from bananachat.services import limits

    fresh, regular, locked, suspended = (make_user(name) for name in ("fred", "gail", "lars", "sven"))
    with app.app_context():
        for user in (regular, locked, suspended):
            _age(user["id"], 20)
            _active(user["id"], 6, 4000)  # 6 active days, 24k tokens
        store.update_user_settings(locked["id"], None, tier_locked=True)
        users.suspend(suspended["id"], datetime.now(timezone.utc) + timedelta(hours=1))
        assert limits.promote() == []  # auto_tiers is off everywhere
        _policy("api", window={"auto_tiers": True})
        promoted = limits.promote()
        assert promoted == [("gail", "Starter", "Regular")]
        assert credits.budget(regular, "api").regular_limit == 60_000  # 30k x2
        assert credits.budget(fresh, "api").regular_limit == 30_000
        assert credits.budget(regular, "chat").unlimited  # the chat policy has no 5-hour limit
        # Lifting the suspension is not enough: 30 clean days are needed.
        users.unsuspend(suspended["id"])
        assert limits.promote() == []
        db.execute("UPDATE users SET last_suspension_at=? WHERE id=?", (db.now(-timedelta(days=31)),
                                                                       suspended["id"]))
        assert limits.promote() == [("sven", "Starter", "Regular")]
        # Never down: an administrator placed gail in Trusted; she does not qualify, and stays.
        trusted = store.list_tiers()[2]
        store.update_user_settings(regular["id"], None, tier_id=trusted["id"])
        assert limits.promote() == []
        assert credits.budget(regular, "api").regular_limit == 120_000
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='limits.tier_promote'") == 2


def test_tier_progress_lists_the_next_requirements(app, make_user):
    from bananachat.services import limits

    user = make_user("tess")
    with app.app_context():
        _age(user["id"], 3)
        _active(user["id"], 2, 5000)
        progress = limits.tier_progress(user)
        assert progress["tier"]["name"] == "Starter" and progress["next"]["name"] == "Regular"
        needs = {item["key"]: (item["have"], item["need"], item["met"]) for item in progress["requirements"]}
        assert needs["account_days"] == (3, 14, False)
        assert needs["active_days"] == (2, 5, False)
        assert needs["tokens"] == (10_000, 20_000.0, False)
        assert needs["clean_days"][2] is True


def test_tier_multipliers_apply_only_where_auto_tiers_is_on(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("tina")
    with app.app_context():
        store.update_user_settings(user["id"], None, tier_id=store.list_tiers()[1]["id"])
        _policy("api", weekly={"enabled": True, "tokens": 100_000, "auto_tiers": True})
        current = limits.effective(user, "api")
        assert current.window.tokens == 30_000 and current.weekly.tokens == 200_000
        assert [reason.code for reason in current.weekly.reasons] == ["default", "tier"]
        store.set_override(user["id"], "api", None, weekly_tokens=150_000)  # custom beats the tier
        assert limits.effective(user, "api").weekly.tokens == 150_000


def test_tiers_are_validated_and_the_last_one_is_kept(app):
    from bananachat.db import limits as store

    with app.app_context():
        for bad in ({"name": "", "multiplier": 1}, {"name": "X", "multiplier": -1},
                    {"name": "X", "multiplier": 1, "min_active_days": 31}):
            with pytest.raises(ValueError):
                store.create_tier(bad)
        new = store.create_tier({"name": "Legend", "multiplier": 8, "min_account_days": 365})
        assert [tier["name"] for tier in store.list_tiers()][-1] == "Legend"
        store.move_tier(new, -1)
        assert [tier["name"] for tier in store.list_tiers()] == ["Starter", "Regular", "Legend", "Trusted"]
        for tier in store.list_tiers()[1:]:
            store.delete_tier(tier["id"])
        with pytest.raises(ValueError):
            store.delete_tier(store.list_tiers()[0]["id"])


# ----- dynamic adjustment ----------------------------------------------------------------------

def test_dynamic_factors_are_deterministic():
    from bananachat.services import limits

    assert limits.demand_factor(None) == 1.0
    assert limits.demand_factor(0.0) == 1.25 and limits.demand_factor(0.2) == 1.25
    assert limits.demand_factor(0.35) == pytest.approx(1.125)
    assert limits.demand_factor(0.7) == 1.0
    assert limits.demand_factor(1.45) == pytest.approx(0.85)
    assert limits.demand_factor(5) == 0.7
    assert limits.personal_factor(0.0, 20, 100_000) == pytest.approx(1.25)   # off-peak and regular
    assert limits.personal_factor(1.0, 0, 100_000) == pytest.approx(0.85)    # only at peak hours, rarely
    assert limits.personal_factor(0.25, 10, 100_000) == pytest.approx(1.05)  # average hours, half the days
    assert limits.personal_factor(None, 20, 100_000) == pytest.approx(1.10)  # no site histogram yet
    assert limits.personal_factor(0.0, 30, 2000) == 1.0                      # too little history
    assert limits.quantise(1.23) == 1.25 and limits.quantise(1.22) == 1.2
    assert limits.quantise(9) == 2.0 and limits.quantise(0.1) == 0.5


def test_demand_is_an_average_of_queue_samples(app):
    from bananachat.services import limits

    with app.app_context():
        assert limits.current_demand() is None
        assert limits.record_demand(0, 0, 4, at=1000.0) == 0.0
        after = limits.record_demand(4, 4, 4, at=1060.0)  # a busy minute moves the average a little
        assert after == pytest.approx(2 * (1 - 2.718281828 ** (-60 / 900)), rel=1e-3)
        assert limits.record_demand(4, 4, 4, at=5000.0) == 2.0  # after a long gap it restarts


def test_peak_hours_come_from_the_site_histogram(app, make_user):
    from bananachat.services import limits

    user = make_user("pam")
    with app.app_context():
        assert limits.refresh_peak_hours() == []  # too little usage to say
        day = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
        for hour, amount in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 30, 20, 10, 9]):
            _charge(user["id"], amount * 1000, at=day.replace(hour=hour))
        assert limits.refresh_peak_hours() == [6, 7, 8, 9, 10, 11]
        assert limits.peak_hours() == [6, 7, 8, 9, 10, 11]


def test_dynamic_limits_follow_demand_explain_themselves_and_respect_custom_limits(app, make_user):
    import time

    from bananachat.db import limits as store
    from bananachat.services import limits

    quiet, custom, exempt = make_user("quin"), make_user("cora"), make_user("xena")
    with app.app_context():
        _policy("api", window={"dynamic": True}, rate={"dynamic": True})
        store.set_override(custom["id"], "api", None, window_tokens=40_000, window_slow_tokens=10_000)
        store.update_user_settings(exempt["id"], None, dynamic_exempt=True)
        limits.record_demand(0, 0, 4, at=time.time())  # a quiet site: +25 %
        current = limits.effective(quiet, "api")
        assert current.dynamic.multiplier == 1.25
        assert current.window.tokens == 37_500 and current.window.slow_tokens == 18_750
        assert [reason.code for reason in current.window.reasons] == ["default", "dynamic_up"]
        assert [reason.code for reason in current.dynamic.reasons] == ["offpeak"]
        assert current.dynamic.reasons[0].text("en") == "Off-peak bonus +25 %"
        assert current.dynamic.reasons[0].text("it") == "Bonus fuori dalle ore di punta +25 %"
        assert current.rate.rules[0].per_second == 1.0  # the request rate is never raised automatically
        assert limits.effective(custom, "api").window.tokens == 50_000  # 40k x1.25
        assert limits.effective(exempt, "api").window.tokens == 30_000

        limits.record_demand(12, 12, 4, at=time.time() + 1000)  # overloaded: -30 %
        current = limits.effective(quiet, "api")
        assert current.dynamic.multiplier == 0.7 and current.window.tokens == 21_000
        assert current.dynamic.reasons[0].text("en") == "High demand −30 %"
        assert current.rate.rules[0].per_second == 0.7 and current.rate.rules[0].burst == 7
        floor = limits.effective(custom, "api").window
        assert floor.tokens == 40_000 and [reason.code for reason in floor.reasons] == ["custom", "custom_floor"]


def test_the_personal_factor_rewards_off_peak_regular_use(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import limits

    night_owl, peak_user = make_user("nora"), make_user("pete")
    with app.app_context():
        _policy("api", window={"dynamic": True})
        now = datetime.now(timezone.utc)
        base = now.replace(minute=0, second=0, microsecond=0)
        for day in range(1, 21):
            _charge(peak_user["id"], 20_000, at=(base - timedelta(days=day)).replace(hour=12))
        limits.refresh_peak_hours()
        peaks = limits.peak_hours()
        assert 12 in peaks
        off = next(hour for hour in range(24) if hour not in peaks)
        for day in range(1, 21):
            _charge(night_owl["id"], 1000, at=(base - timedelta(days=day)).replace(hour=off))
        limits.forget_personal()
        owl = limits.effective(night_owl, "api")
        assert owl.dynamic.personal == pytest.approx(1.25) and owl.window.tokens == 37_500
        assert "pattern_bonus" in [reason.code for reason in owl.dynamic.reasons]
        busy = limits.effective(peak_user, "api")
        assert busy.dynamic.personal == pytest.approx(0.95)  # -15 % at peak, +10 % regular
        assert busy.window.tokens == 28_500
        store.update_user_settings(night_owl["id"], None, dynamic_exempt=True)
        assert limits.effective(night_owl, "api").dynamic is None


# ----- grants ---------------------------------------------------------------------------------

def _grant(**values):
    from bananachat import db
    from bananachat.db import limits as store

    values.setdefault("user_id", None)
    values.setdefault("pool", None)
    values.setdefault("scope", None)
    values.setdefault("amount", 0)
    values.setdefault("starts_at", db.now(-timedelta(minutes=1)))
    values.setdefault("ends_at", None)
    values.setdefault("reason", "test")
    return store.create_grant(created_by=None, **values)


def test_grants_stack_unlimited_then_multipliers_then_extras(app, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    user, other = make_user("gina"), make_user("hank")
    with app.app_context():
        _grant(user_id=user["id"], pool="api", scope="window", kind="multiplier", amount=2,
               ends_at=db.now(timedelta(hours=24)))
        _grant(kind="extra", scope="window", amount=10_000)                  # everyone, forever
        _grant(user_id=user["id"], kind="multiplier", amount=3, ends_at=db.now(-timedelta(seconds=1)))  # over
        _grant(user_id=user["id"], kind="unlimited", starts_at=db.now(timedelta(hours=1)))            # upcoming
        current = limits.effective(user, "api")
        assert current.window.tokens == 70_000 and current.window.slow_tokens == 30_000  # 30k x2 + 10k; slow x2
        codes = [reason.code for reason in current.window.reasons]
        assert codes == ["default", "grant_multiplier", "grant_extra_forever"]
        assert current.window.reasons[2].text("en") == "+10k tokens"
        assert limits.effective(other, "api").window.tokens == 40_000
        assert limits.effective(other, "chat").window.limited is False  # extras never switch a limit on
        assert len(store.user_grants(user["id"])) == 3  # two active, one upcoming
        unlimited = _grant(user_id=other["id"], pool="api", kind="unlimited", ends_at=db.now(timedelta(hours=1)))
        other_now = limits.effective(other, "api")
        assert credits.budget(other, "api").unlimited and not other_now.rate.limited
        assert other_now.window.reasons[-1].code == "grant_unlimited"
        assert limits.check_rate(other, "api").limited is False
        assert limits.effective(other, "chat").rate.limited  # only the api pool
        assert store.revoke_grant(unlimited, None) and not store.revoke_grant(unlimited, None)
        assert not credits.budget(other, "api").unlimited
        assert [row["id"] for row in store.list_grants("ended")][0] == unlimited


def test_rate_grants_multiply_every_rule(app, make_user):
    from bananachat.services import limits

    user = make_user("rory")
    with app.app_context():
        _policy("api", rate={"rules": _rules((1, "second", 10), (100, "hour", None))})
        _grant(user_id=user["id"], pool="api", scope="rate", kind="multiplier", amount=3)
        rules = limits.effective(user, "api").rate.rules
        assert [(rule.requests, rule.burst) for rule in rules] == [(3.0, 30), (300.0, 300)]


def test_grants_are_validated(app):
    from bananachat import db
    from bananachat.db import limits as store

    with app.app_context():
        now = db.now()
        for values in ({"kind": "bonus"}, {"kind": "multiplier", "amount": 1}, {"kind": "extra", "amount": 5},
                       {"kind": "extra", "scope": "window", "amount": 0}, {"kind": "extra", "scope": "rate",
                                                                            "amount": 1},
                       {"kind": "unlimited", "scope": "yearly"}, {"kind": "unlimited", "pool": "images"},
                       {"kind": "unlimited", "ends_at": now}):
            payload = {"user_id": None, "pool": None, "scope": None, "amount": 0, "starts_at": now, "ends_at": None,
                       "reason": "", **values}
            with pytest.raises(ValueError):
                store.create_grant(created_by=None, **payload)


# ----- speed ---------------------------------------------------------------------------------

def test_speed_sets_the_queue_lane_and_can_adjust_the_rate(app, make_user):
    from bananachat.db import limits as store
    from bananachat.db import users
    from bananachat.services import limits, queue

    user = make_user("sid")
    with app.app_context():
        assert queue.priority_for(user, slow=False, api=True) == queue.PRIORITY_API
        assert queue.priority_for(user, slow=False) == queue.PRIORITY_CHAT
        limits.set_speed(user["id"], "fast", adjust_rate=False, updated_by=None)
        assert queue.priority_for(user, slow=False) == queue.PRIORITY_FAST
        assert queue.priority_for(user, slow=True) == queue.PRIORITY_SLOW  # slow tokens keep the slow lane
        assert queue.PRIORITY_ADMIN < queue.PRIORITY_FAST < queue.PRIORITY_API < queue.PRIORITY_CHAT
        assert queue.priority_for(users.get_by_username("admin"), slow=True) == queue.PRIORITY_ADMIN
        _policy("api", rate={"rules": _rules((60, "minute", 10))})
        limits.set_speed(user["id"], "slow", adjust_rate=True, updated_by=None)
        assert queue.priority_for(user, slow=False, api=True) == queue.PRIORITY_SLOW
        rule = limits.rate_limit(user, "api").rules[0]
        assert (rule.requests, rule.per, rule.burst) == (30, "minute", 5)
        assert limits.rate_limit(user, "chat").rules[0].burst == 2
        limits.set_speed(user["id"], "normal", adjust_rate=True, updated_by=None)
        assert store.overrides_for(user["id"]) == {}
        assert store.user_settings(user["id"]).speed == "normal"


# ----- quota requests --------------------------------------------------------------------------

def test_5_hour_requests_are_approved_automatically_within_the_thresholds(app, make_user):
    from bananachat.db import credits, settings
    from bananachat.db import limits as store

    user = make_user("dora")
    with app.app_context():
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=100_000,
                        quota_auto_approve_max_slow_tokens=50_000)
        outcome = credits.submit_request(user["id"], 80_000, 20_000, "Testing things", kind="window", pool="api")
        assert outcome["status"] == "approved"
        override = store.get_override(user["id"], "api")
        assert (override.window_tokens, override.window_slow_tokens, override.updated_by) == \
            (80_000, 20_000, user["id"])
        # The chat pool has no 5-hour limit by default: nothing to raise.
        with pytest.raises(credits.RequestError) as refused:
            credits.submit_request(user["id"], 80_000, 0, "More chat", kind="window", pool="chat")
        assert refused.value.key == "quota_not_limited"


def test_weekly_requests_can_be_approved_automatically_when_configured(app, make_user):
    from bananachat.db import credits, settings
    from bananachat.db import limits as store

    user, other = make_user("walt"), make_user("wanda")
    with app.app_context():
        with pytest.raises(credits.RequestError):
            credits.submit_request(user["id"], reason="More per week", kind="weekly", weekly=300_000)  # weekly is off
        _policy("api", weekly={"enabled": True, "tokens": 150_000})
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=100_000)
        assert credits.submit_request(user["id"], reason="More per week", kind="weekly",
                                      weekly=300_000)["status"] == "pending"  # no weekly threshold yet
        settings.update(quota_auto_approve_max_weekly_tokens=400_000)
        assert credits.submit_request(other["id"], reason="More per week", kind="weekly",
                                      weekly=300_000)["status"] == "approved"
        assert store.get_override(other["id"], "api").weekly_tokens == 300_000
        with pytest.raises(credits.RequestError) as refused:
            credits.submit_request(other["id"], reason="Less per week", kind="weekly", weekly=200_000)
        assert refused.value.key == "quota_must_raise"


def test_rate_and_temporary_requests_always_wait_and_approval_applies_them(app, make_user):
    from bananachat import db
    from bananachat.db import credits, settings, users
    from bananachat.db import limits as store
    from bananachat.services import limits

    rae, tom, uma = make_user("rae"), make_user("tom"), make_user("uma")
    with app.app_context():
        admin = users.get_by_username("admin")
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=100_000_000,
                        quota_auto_approve_max_slow_tokens=100_000_000)
        for bad in ({"per": "second", "requests": 1}, {"per": "hour", "requests": 5}, {"per": "second", "requests": 0}):
            with pytest.raises(credits.RequestError):
                credits.submit_request(rae["id"], reason="Batch jobs", kind="rate", **bad)
        rate = credits.submit_request(rae["id"], reason="Batch jobs", kind="rate", per="second", requests=2)
        assert rate["status"] == "pending"
        temporary = credits.submit_request(tom["id"], reason="Hackathon tonight", kind="temporary", hours=24,
                                           unlimited=True)
        extra = credits.submit_request(uma["id"], 50_000, reason="Exam week", kind="temporary", hours=48)
        assert temporary["status"] == extra["status"] == "pending"
        with pytest.raises(credits.RequestError) as refused:
            credits.submit_request(tom["id"], reason="Hackathon tonight", kind="temporary", hours=2000)
        assert refused.value.key in ("quota_hours_range", "quota_already_pending")

        credits.resolve_request(rate["id"], admin["id"], True, "Go ahead")
        rule = limits.rate_limit(rae, "api").rules[0]
        assert (rule.requests, rule.per, rule.burst) == (2, "second", 20)  # the burst grows with the rate
        credits.resolve_request(temporary["id"], admin["id"], True)
        row = db.one("SELECT * FROM quota_requests WHERE id=?", (temporary["id"],))
        grant = store.get_grant(row["grant_id"])
        assert grant["kind"] == "unlimited" and grant["user_id"] == tom["id"] and grant["pool"] == "api"
        ends = db.parse_timestamp(grant["ends_at"]) - db.parse_timestamp(grant["starts_at"])
        assert ends == timedelta(hours=24) and "Hackathon tonight" in grant["reason"]
        assert credits.budget(tom, "api").unlimited
        credits.resolve_request(extra["id"], admin["id"], True)
        assert limits.effective(uma, "api").window.tokens == 80_000  # 30k + 50k extra
        with pytest.raises(ValueError):
            credits.resolve_request(extra["id"], admin["id"], False)


def test_the_account_page_offers_every_kind_of_request(app, make_user):
    from bananachat import db

    user = make_user("kim")
    browser = _signed_in(app, "kim")
    html = browser.get("/account").get_data(as_text=True)
    for kind in ("window", "rate", "temporary"):
        assert f'<option value="{kind}"' in html
    assert '<option value="weekly"' not in html  # no weekly limit is on
    response = browser.post("/account/quota-request", {"kind": "rate", "pool": "chat", "rate_per": "second",
                                                       "rate_requests": "3", "reason": "Group chat bot"})
    assert response.status_code == 302
    with app.app_context():
        row = db.one("SELECT * FROM quota_requests WHERE user_id=?", (user["id"],))
        assert (row["kind"], row["pool"], row["new_rate_rules"]) == ("rate", "chat",
                                                                     '[{"requests": 3, "per": "second"}]')
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='quota.request'") == 1
    html = browser.get("/account").get_data(as_text=True)
    assert "3 requests per second · Chat" in html
    response = browser.post("/account/quota-request", {"kind": "temporary", "pool": "api", "hours": "9",
                                                       "extra_tokens": "5k", "reason": "One more"})
    assert response.status_code == 400
    assert "You already have a request waiting for review." in response.get_data(as_text=True)
    make_user("lou")
    response = _signed_in(app, "lou").post("/account/quota-request", {
        "kind": "temporary", "pool": "api", "hours": "9999", "extra_tokens": "5k", "reason": "Too long"})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "Ask for 1 to 720 hours." in html and "Too long</textarea>" in html
    assert '<option value="temporary" selected>' in html and 'name="hours" min="1" max="720" step="1" value="9999"' in html


# ----- administrator pages and actions ---------------------------------------------------------------

def test_admin_limit_pages_render(app, admin, make_user):
    user = make_user("page")
    for url in ("/admin/quotas", "/admin/quotas/requests", "/admin/quotas/requests?status=all",
                "/admin/quotas/tiers", "/admin/quotas/grants", "/admin/quotas/grants?user=page",
                "/admin/quotas/models", "/admin/quotas/effort",
                f"/admin/users/{user['id']}/limits", f"/admin/users/{user['id']}"):
        response = admin.get(url)
        assert response.status_code == 200, url
    assert 'value="page"' in admin.get("/admin/quotas/grants?user=page").get_data(as_text=True)
    assert _signed_in(app, "page").get("/admin/quotas/tiers").status_code in (302, 403, 404)


def test_admins_edit_policies_with_validation_and_audit(app, admin):
    from bananachat import db
    from bananachat.db import limits

    form = {"rate_enabled": "1", "rule_requests_0": "30", "rule_per_0": "minute", "rule_burst_0": "4",
            "window_enabled": "1", "window_tokens": "25000", "window_slow_tokens": "5k", "window_dynamic": "1",
            "weekly_enabled": "1", "weekly_tokens": "100k", "weekly_auto_tiers": "1"}
    response = admin.post("/admin/quotas/policy/chat", {**form, "rule_requests_0": "0"}, follow_redirects=True)
    assert "must be between" in response.get_data(as_text=True)
    response = admin.post("/admin/quotas/policy/chat", form, follow_redirects=True)
    assert "Chat: limits saved" in response.get_data(as_text=True)
    assert admin.post("/admin/quotas/policy/images", form).status_code == 404
    with app.app_context():
        policy = limits.get_policy("chat")
        assert policy["rate"] == {"enabled": True, "rules": [{"requests": 30, "per": "minute", "burst": 4}],
                                  "dynamic": False}
        assert policy["window"] == {"enabled": True, "tokens": 25_000, "slow_tokens": 5000, "dynamic": True,
                                    "auto_tiers": False}
        assert policy["weekly"] == {"enabled": True, "tokens": 100_000, "dynamic": False, "auto_tiers": True}
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='admin.limits_policy' AND target='chat'") == 1
    admin.post("/admin/quotas", {"slow_credits_enabled": "1", "quota_auto_approve_enabled": "1",
                                 "quota_auto_approve_max_tokens": "60k",
                                 "quota_auto_approve_max_weekly_tokens": "500000"})
    with app.app_context():
        row = db.one("SELECT * FROM site_settings")
        assert row["quota_auto_approve_max_tokens"] == 60_000
        assert row["quota_auto_approve_max_weekly_tokens"] == 500_000


def test_admins_manage_one_accounts_limits(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits, queue

    user = make_user("mia")
    base = f"/admin/users/{user['id']}/limits"
    with app.app_context():
        _charge(user["id"], 12_000, at=datetime.now(timezone.utc) - timedelta(seconds=5))
    response = admin.post(base + "/custom", {"pool": "api", "window_tokens": "90k", "window_slow_tokens": "",
                                             "weekly_tokens": ""}, follow_redirects=True)
    assert "custom limits of mia saved" in response.get_data(as_text=True)
    response = admin.post(base + "/custom", {"pool": "api", "rule_requests_0": "2", "rule_per_0": "week"},
                          follow_redirects=True)
    assert "second, minute, hour or day" in response.get_data(as_text=True)
    admin.post(base + "/reset", {"period": "window"})
    admin.post(base + "/speed", {"speed": "fast", "adjust_rate": "1"})
    with app.app_context():
        tiers = store.list_tiers()
        budget = credits.budget(user, "api")
        assert budget.regular_limit == 90_000 and budget.regular_used == 0
        assert queue.priority_for(user, slow=False) == queue.PRIORITY_FAST
        assert limits.rate_limit(user, "api").rules[0].requests == 2
    admin.post(base + "/tier", {"tier_id": str(tiers[2]["id"]), "tier_locked": "1"})
    admin.post(base + "/dynamic", {"dynamic_exempt": "1"})
    with app.app_context():
        settings = store.user_settings(user["id"])
        assert settings.tier_id == tiers[2]["id"] and settings.tier_locked and settings.dynamic_exempt
    html = admin.get(base).get_data(as_text=True)
    assert "Custom limit for your account" in html and "Sped up" in html
    admin.post(base + "/restore")
    with app.app_context():
        assert store.overrides_for(user["id"]) == {}
        assert store.user_settings(user["id"]).speed == "normal"
        assert store.user_settings(user["id"]).tier_id == tiers[2]["id"]  # tiers are kept
        actions = {row["action"] for row in db.query("SELECT action FROM audit_log WHERE target='mia'")}
    assert {"admin.limits_user_custom", "admin.limits_user_reset_usage", "admin.limits_user_speed",
            "admin.limits_user_tier", "admin.limits_user_dynamic", "admin.limits_user_restore"} <= actions


def test_admins_grant_and_revoke_for_one_account_or_everyone(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    lea, max_ = make_user("lea"), make_user("max")
    response = admin.post("/admin/quotas/grants", {"target": "user", "username": "nobody", "kind": "unlimited",
                                                   "duration": "24h"}, follow_redirects=True)
    assert "no account called nobody" in response.get_data(as_text=True)
    admin.post("/admin/quotas/grants", {"target": "user", "username": "lea", "kind": "unlimited", "pool": "api",
                                        "duration": "24h", "reason": "Explained a deadline"})
    admin.post("/admin/quotas/grants", {"target": "everyone", "kind": "multiplier", "amount": "2", "scope": "window",
                                        "duration": "forever", "reason": "Coding jam"})
    response = admin.post("/admin/quotas/grants", {"target": "everyone", "kind": "extra", "amount": "5k",
                                                   "scope": "window", "duration": "custom"}, follow_redirects=True)
    assert "Choose when a custom grant ends" in response.get_data(as_text=True)
    with app.app_context():
        grants = store.list_grants("active")
        assert len(grants) == 2
        lea_grant = next(grant for grant in grants if grant["user_id"] == lea["id"])
        span = db.parse_timestamp(lea_grant["ends_at"]) - db.parse_timestamp(lea_grant["starts_at"])
        assert span == timedelta(hours=24)
        assert credits.budget(lea, "api").unlimited
        assert limits.effective(max_, "api").window.tokens == 60_000
    page = admin.get("/admin/quotas/grants").get_data(as_text=True)
    assert "Coding jam" in page and "Explained a deadline" in page
    account = _signed_in(app, "max").get("/account").get_data(as_text=True)
    assert "Coding jam" in account and "for everyone" in account
    admin.post(f"/admin/quotas/grants/{lea_grant['id']}/revoke")
    with app.app_context():
        assert not credits.budget(lea, "api").unlimited
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action IN ('admin.limits_grant_create', "
                         "'admin.limits_grant_revoke')") == 3


def test_admins_edit_tiers(app, admin, make_user):
    from bananachat import db
    from bananachat.db import limits as store

    admin.post("/admin/quotas/tiers", {"name": "Legend", "multiplier": "8", "min_account_days": "365"})
    with app.app_context():
        tiers = store.list_tiers()
        assert tiers[-1]["name"] == "Legend" and tiers[-1]["multiplier"] == 8
    legend = tiers[-1]["id"]
    admin.post(f"/admin/quotas/tiers/{legend}", {"action": "save", "name": "Legend", "multiplier": "6",
                                                 "min_account_days": "300", "min_active_days": "20",
                                                 "min_tokens_30d": "500k", "clean_days": "180"})
    admin.post(f"/admin/quotas/tiers/{legend}", {"action": "up"})
    response = admin.post(f"/admin/quotas/tiers/{legend}", {"action": "save", "name": "", "multiplier": "1"},
                          follow_redirects=True)
    assert "Name is required" in response.get_data(as_text=True)
    with app.app_context():
        tier = store.get_tier(legend)
        assert (tier["multiplier"], tier["min_active_days"], tier["clean_days"]) == (6, 20, 180)
        assert tier["min_tokens_30d"] == 500_000
        assert [row["name"] for row in store.list_tiers()] == ["Starter", "Regular", "Legend", "Trusted"]
    admin.post(f"/admin/quotas/tiers/{legend}", {"action": "delete"})
    response = admin.post("/admin/quotas/tiers/promote", follow_redirects=True)
    assert "nobody was promoted" in response.get_data(as_text=True)
    with app.app_context():
        assert len(store.list_tiers()) == 3
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'admin.limits_tier%'") == 5


def test_global_resets_and_restoring_defaults(app, admin, make_user):
    from bananachat.db import credits
    from bananachat.db import limits as store

    ann, ben = make_user("ann"), make_user("ben")
    with app.app_context():
        store.set_override(ann["id"], "api", None, window_tokens=99_000)
        store.update_user_settings(ben["id"], None, speed="slow")
        _charge(ann["id"], 10_000, at=datetime.now(timezone.utc) - timedelta(seconds=5))
    admin.post("/admin/quotas/reset-usage", {"period": "window"})
    with app.app_context():
        assert credits.budget(ann, "api").regular_used == 0
    response = admin.post("/admin/quotas/restore-defaults", follow_redirects=True)
    assert "removed from 2 accounts" in response.get_data(as_text=True)
    with app.app_context():
        assert credits.budget(ann, "api").regular_limit == 30_000
        assert store.user_settings(ben["id"]).speed == "normal"


def test_admins_resolve_every_kind_of_request(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import limits

    joe = make_user("joe")
    with app.app_context():
        request_id = credits.submit_request(joe["id"], reason="A bot for my club", kind="rate", pool="chat",
                                            per="second", requests=2)["id"]
    html = admin.get("/admin/quotas/requests").get_data(as_text=True)
    assert "A bot for my club" in html and "2 requests per second · Chat" in html
    assert "now 1 request per second (bursts of up to 5)" in html
    admin.post(f"/admin/quotas/requests/{request_id}", {"decision": "approve", "message": "Enjoy"})
    with app.app_context():
        assert limits.rate_limit(joe, "chat").rules[0].requests == 2
        row = db.one("SELECT status, admin_message, resolved_by FROM quota_requests WHERE id=?", (request_id,))
        assert (row["status"], row["admin_message"]) == ("approved", "Enjoy") and row["resolved_by"]
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='admin.quota_request_approve'") == 1


# ----- the account page --------------------------------------------------------------------------------

@pytest.mark.parametrize(("language", "texts"), [
    ("en", ("Your limits", "Requests: 1 request per second (bursts of up to 10)", "Tokens this week",
            "Level Regular ×2", "Your level", "To reach Trusted:", "Extra allowances", "×2 until",
            "120k tokens")),
    ("it", ("I tuoi limiti", "Richieste: 1 richiesta al secondo (fino a 10 di fila)", "Token di questa settimana",
            "Livello Regular ×2", "Il tuo livello", "Per passare a Trusted:", "Aumenti concessi", "×2 fino al",
            "120k token")),
])
def test_the_account_page_shows_limits_in_both_languages(app, make_user, language, texts):
    from bananachat import db
    from bananachat.db import limits as store

    user = make_user(f"lang{language}")
    with app.app_context():
        _policy("api", window={"auto_tiers": True}, weekly={"enabled": True, "tokens": 100_000})
        store.update_user_settings(user["id"], None, tier_id=store.list_tiers()[1]["id"])
        _grant(user_id=user["id"], pool="api", scope="window", kind="multiplier", amount=2,
               ends_at=db.now(timedelta(hours=5)))
    html = _signed_in(app, f"lang{language}", language=language).get("/account").get_data(as_text=True)
    for text in texts:
        assert text in html, text
    assert 'max="120000.0"' in html  # 30k x2 (tier) x2 (grant)
    assert 'id="quota-data"' in html


def test_the_developer_page_shows_the_api_pool(app, make_user):
    from bananachat import db

    user = make_user("devon")
    with app.app_context():
        _policy("api", weekly={"enabled": True, "tokens": 50_000}, rate={"rules": _rules((2, "second", 8))})
        _charge(user["id"], 50_000)
    html = _signed_in(app, "devon").get("/developer").get_data(as_text=True)
    assert "Request rate: at most 2 requests per second (bursts of up to 8)." in html
    assert "Tokens this week" in html and "This week&#39;s tokens are used up" in html
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 1


# ----- refusals and exports ------------------------------------------------------------------------

def test_the_api_refuses_used_up_weekly_tokens_until_the_week_ends(app, make_user):
    from tests.app.test_api import chat, roll_out, token_for

    user = make_user("wynn")
    roll_out(app)
    started = datetime.now(timezone.utc) - timedelta(days=2)
    with app.app_context():
        _policy("api", weekly={"enabled": True, "tokens": 3000})
        _charge(user["id"], 3000, at=started)
    response = chat(app, token_for(app, user))
    assert response.status_code == 429
    error = response.json["error"]
    assert error["code"] == "insufficient_quota" and error["type"] == "rate_limit_error"
    assert "this week's tokens" in error["message"] and "UTC" in error["message"]
    ends = started + timedelta(days=7)
    assert abs(int(response.headers["Retry-After"]) - (ends - datetime.now(timezone.utc)).total_seconds()) < 60
    assert response.headers["x-ratelimit-limit-requests"] == "10"


def test_chat_explains_weekly_exhaustion(app, make_user):
    from bananachat.db import credits
    from bananachat.services import chat

    user = make_user("cleo")
    with app.app_context():
        _policy("chat", weekly={"enabled": True, "tokens": 1000})
        _charge(user["id"], 1000, request_type="chat")
        budget = credits.budget(user, "chat")
        assert budget.weekly_exhausted
        assert chat.quota_message(budget, "en").startswith("You have used this week's tokens. They come back at")
        assert chat.quota_message(budget, "it").startswith("Hai usato i token di questa settimana")


def test_personal_data_exports_include_limits(app, make_user):
    from bananachat.db import limits as store
    from bananachat.services import exports

    user = make_user("xavi")
    with app.app_context():
        store.set_override(user["id"], "chat", None, weekly_tokens=70_000)
        store.update_user_settings(user["id"], None, speed="slow")
        store.set_effort_level(user["id"], None, "high")
        _grant(user_id=user["id"], kind="unlimited", reason="Deadline")
        _grant(kind="multiplier", amount=2)  # for everyone: not personal data
        data = exports.gdpr_export(user["id"])
    assert data["limits"]["settings"]["speed"] == "slow"
    assert data["limits"]["custom_limits"][0]["weekly_tokens"] == 70_000
    assert data["limits"]["reasoning_effort"][0]["level"] == "high"
    assert [grant["reason"] for grant in data["limits"]["grants"]] == ["Deadline"]
