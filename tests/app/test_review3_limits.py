"""Third review of the limit system: upgrades from the previous release, request buckets, grants and quota requests."""

from __future__ import annotations

import sqlite3

from tests.app.test_foundation import legacy_instance


def _legacy_user(connection, user_id: str, username: str) -> None:
    """Another account of the previous release, copied from the fixture's user."""
    connection.execute("CREATE TEMP TABLE copy AS SELECT * FROM users WHERE id='4lttj861'")
    connection.execute("UPDATE copy SET id=?, username=?", (user_id, username))
    connection.execute("INSERT INTO users SELECT * FROM copy")
    connection.execute("DROP TABLE copy")


# ----- upgrade from the previous release ----------------------------------------------------------------

def test_quotas_raised_by_automatic_approval_in_1x_survive_the_upgrade(tmp_path, make_app):
    """The previous release approved requests automatically with ``updated_by=NULL``; those raises must not be lost."""
    from bananachat.db import credits, limits

    instance = legacy_instance(tmp_path)
    connection = sqlite3.connect(instance / "bananachat.db")
    _legacy_user(connection, "bob00001", "bob")
    _legacy_user(connection, "carol001", "carol")
    connection.execute("INSERT INTO user_quota (user_id, daily_credits, daily_slow_credits, updated_at, updated_by) "
                       "VALUES ('bob00001', 60, 20, '2026-09-28T18:30:00+00:00', NULL)")
    connection.execute("INSERT INTO quota_requests (user_id, new_credits, new_slow_credits, reason, status, "
                       "resolution_source, created_at, resolved_at) VALUES ('bob00001', 60, 20, 'More please', "
                       "'approved', 'automatic', '2026-09-28T18:30:00+00:00', '2026-09-28T18:30:00+00:00')")
    # A row the previous release created on first use (the site default at the time), never raised: not a custom quota.
    connection.execute("INSERT INTO user_quota (user_id, daily_credits, daily_slow_credits, updated_by) "
                       "VALUES ('carol001', 30, 15, NULL)")
    connection.commit()
    connection.close()

    app = make_app(setup=False, INSTANCE_DIR=str(instance))
    with app.app_context():
        override = limits.get_override("bob00001", "api")
        assert override is not None and (override.window_tokens, override.window_slow_tokens) == (60_000, 20_000)
        assert credits.get_quota("bob00001") == (60_000, 20_000)
        assert limits.get_override("carol001", "api") is None
        assert limits.get_override("4lttj861", "api").window_tokens == 40_000


# ----- account page layout --------------------------------------------------------------

def test_the_account_page_column_can_shrink_below_its_widest_table_on_phones():
    """A bare ``1fr`` grid column grows to the quota history table, so the page scrolled sideways at 390 px."""
    import re
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "bananachat/static/css/account.css").read_text()
    columns = re.findall(r"\.account-layout\s*\{[^}]*grid-template-columns:\s*([^;]+);", css)
    assert columns and all(re.match(r"^(200px )?minmax\(0, 1fr\)$", value.strip()) for value in columns), columns


# ----- quota requests ------------------------------------------------------------------------

def test_automatic_approval_ignores_slow_tokens_while_they_are_switched_off(app, make_user):
    """With slow tokens off the page promises "requests up to N are approved immediately";
    the account's unused slow allowance (15k by default) must not keep every request waiting."""
    from bananachat.db import credits
    from bananachat.db import settings as site_settings

    user = make_user("sloane")
    with app.app_context():
        site_settings.update(slow_credits_enabled=0, quota_auto_approve_enabled=1,
                             quota_auto_approve_max_tokens=100_000, quota_auto_approve_max_slow_tokens=0)
        outcome = credits.submit_request(user["id"], 50_000, None, "More for my course", kind="window", pool="api")
        assert outcome["status"] == "approved"
        assert credits.get_quota(user["id"])[0] == 50_000


def test_approving_a_request_never_switches_on_a_limit_the_service_no_longer_has(app, admin, make_user):
    """A 5-hour request sent while chat had a 5-hour limit must not, once the administrator switched
    the limit off for everyone, give this one account a limit again when it is approved."""
    import pytest

    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("stella")
    admin_id = _admin_id(app)
    with app.app_context():
        policy = store.get_policy("chat")
        policy["window"]["enabled"] = True
        store.set_policy("chat", policy, None)
        request_id = credits.submit_request(user["id"], 150_000, 0, "Long study sessions", kind="window",
                                            pool="chat")["id"]
        policy["window"]["enabled"] = False
        store.set_policy("chat", policy, None)
        with pytest.raises(ValueError):
            credits.resolve_request(request_id, admin_id, True)
    assert "now 100k tokens + 0 tokens slow (not limited)" in admin.get("/admin/quotas/requests").get_data(as_text=True)
    with app.app_context():
        assert store.get_override(user["id"], "chat") is None
        assert not limits.effective(user, "chat").window.limited
        credits.resolve_request(request_id, admin_id, False, "No limit any more.")


def _admin_id(app):
    from bananachat.db import users

    with app.app_context():
        return users.get_by_username("admin")["id"]


# ----- administrator forms -------------------------------------------------------------------

def test_a_grant_starting_at_the_end_of_the_calendar_is_refused_not_a_server_error(app, admin, make_user):
    make_user("gina")
    response = admin.post("/admin/quotas/grants", {
        "target": "user", "username": "gina", "pool": "api", "scope": "window", "kind": "extra", "amount": "5k",
        "starts_at": "9999-12-31T23:00", "duration": "7d", "reason": "Late"})
    # Refused on the form itself, which keeps what was typed.
    assert response.status_code == 400
    assert "out of range" in response.get_data(as_text=True)


# ----- request buckets -------------------------------------------------------------------------

def test_forgetting_idle_buckets_never_refills_a_slow_bucket_early(app, make_user):
    """A bucket missing from the table counts as full, so one may only be forgotten once it has refilled."""
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("tortoise")
    with app.app_context():
        # 86 a day with bursts of 100: more than a day to refill.
        store.set_override(user["id"], "api", None, rate_rules=[{"requests": 86, "per": "day", "burst": 100}])
        start = 1_000_000.0
        for _ in range(100):
            assert limits.check_rate(user, "api", now=start).allowed
        assert not limits.check_rate(user, "api", now=start).allowed
        limits.forget_idle_buckets(now=start + 86_401)
        decision = limits.check_rate(user, "api", now=start + 86_401)
        assert decision.remaining == 85  # 86 refilled, one taken now; not a full bucket of 100
        # Buckets that have certainly refilled are still forgotten.
        assert limits.forget_idle_buckets(now=start + 86_401 + 200_000) == 1


def test_a_higher_request_rate_cannot_be_asked_for_a_service_without_a_rate_limit(app, make_user):
    """Approving such a request would switch a (lower) rate limit on for the account, and the form offered it."""
    import pytest

    from bananachat.db import credits
    from bananachat.db import limits as store
    from tests.app.test_limits import _signed_in

    user = make_user("rita")
    with app.app_context():
        policy = store.get_policy("api")
        policy["rate"]["enabled"] = False
        store.set_policy("api", policy, None)
        with pytest.raises(credits.RequestError) as refused:
            credits.submit_request(user["id"], reason="Faster batch jobs", kind="rate", pool="api", per="second",
                                   requests=5)
        assert refused.value.key == "quota_not_limited"
    page = _signed_in(app, "rita").get("/account").get_data(as_text=True)
    assert '"rate": ["chat"]' in page.replace("&#34;", '"')


# ----- speed ---------------------------------------------------------------------------------

def test_changing_speed_again_starts_from_the_normal_rate_not_the_last_adjustment(app, make_user):
    """Slowing down twice used to quarter the rate, and speeding up then slowing down left the normal rate."""
    from bananachat.services import limits

    user = make_user("yoyo")
    with app.app_context():
        def rate():
            rule = limits.rate_limit(user, "api").rules[0]
            return rule.requests, rule.per, rule.burst

        from bananachat.db import limits as store
        policy = store.get_policy("api")
        policy["rate"]["rules"] = [{"requests": 60, "per": "minute", "burst": 10}]
        store.set_policy("api", policy, None)
        limits.set_speed(user["id"], "slow", adjust_rate=True, updated_by=None)
        limits.set_speed(user["id"], "slow", adjust_rate=True, updated_by=None)
        assert rate() == (30, "minute", 5)
        limits.set_speed(user["id"], "fast", adjust_rate=True, updated_by=None)
        assert rate() == (120, "minute", 20)
        limits.set_speed(user["id"], "slow", adjust_rate=True, updated_by=None)
        assert rate() == (30, "minute", 5)


# ----- what counts for tiers and dynamic limits ----------------------------------------------------

def test_tiers_and_the_personal_factor_count_only_services_with_token_limits(app, make_user):
    """Chat has no token limit by default: using it freely must not earn higher tiers or dynamic bonuses."""
    from datetime import datetime, timedelta, timezone

    from bananachat.services import limits
    from tests.app.test_limits import _age, _charge, _policy

    user = make_user("chatty")
    with app.app_context():
        _policy("api", window={"auto_tiers": True, "dynamic": True})
        _age(user["id"], 40)
        now = datetime.now(timezone.utc)
        for day in range(1, 8):
            _charge(user["id"], 10_000, request_type="chat", at=now - timedelta(days=day, hours=1))
        assert limits.promote() == []
        progress = limits.tier_progress(user)
        assert {item["key"]: item["have"] for item in progress["requirements"]}["tokens"] == 0
        limits.forget_personal()
        assert limits.effective(user, "api").dynamic.personal == 1.0  # no counted history: neutral
        _policy("chat", window={"enabled": True})
        limits.forget_personal()
        assert limits.promote() == [("chatty", "Starter", "Regular")]
        assert limits.effective(user, "api").dynamic.personal > 1.0


# ----- temporary increases ---------------------------------------------------------------------

def test_temporary_extra_tokens_need_a_5_hour_limit(app, admin, make_user):
    import pytest

    from bananachat.db import credits
    from bananachat.db import limits as store
    from tests.app.test_limits import _signed_in

    user = make_user("tempo")
    with app.app_context():
        with pytest.raises(credits.RequestError) as refused:
            credits.submit_request(user["id"], 50_000, reason="Exam week coming", kind="temporary", pool="chat",
                                   hours=24)
        assert refused.value.key == "quota_temporary_no_window"
        # Unlimited use for a while still makes sense there (it lifts the request rate too).
        outcome = credits.submit_request(user["id"], reason="Exam week coming", kind="temporary", pool="chat",
                                         hours=24, unlimited=True)
        credits.resolve_request(outcome["id"], _admin_id(app), False)
    page = _signed_in(app, "tempo").get("/account").get_data(as_text=True).replace("&#34;", '"')
    assert '"extra_pools": ["api"]' in page
    assert "only unlimited use can be requested" in page
    # Administrators cannot create an extra-token grant that would do nothing either.
    response = admin.post("/admin/quotas/grants", {
        "target": "user", "username": "tempo", "pool": "chat", "scope": "window", "kind": "extra", "amount": "5k",
        "duration": "24h", "reason": "Exam week"})
    assert response.status_code == 400
    with app.app_context():
        assert store.list_grants("active", user_id=user["id"]) == []
    assert "has no 5-hour limit" in response.get_data(as_text=True)


# ----- indexes and the request queue -------------------------------------------------------------

def test_new_foreign_keys_of_the_limit_tables_are_indexed(app):
    from bananachat import db

    with app.app_context():
        for table, column in (("user_limits", "updated_by"), ("user_limit_overrides", "updated_by"),
                              ("quota_requests", "grant_id"), ("quota_requests", "model_id"),
                              ("limit_grants", "model_id"), ("user_model_limits", "model_id"),
                              ("user_effort_levels", "model_id"), ("user_effort_levels", "updated_by"),
                              ("model_limit_policy", "updated_by")):
            first_columns = [db.query(f"PRAGMA index_info('{index['name']}')")[0]["name"]
                             for index in db.query(f"PRAGMA index_list('{table}')")]
            assert column in first_columns, (table, column)


def test_the_request_queue_costs_the_same_queries_for_one_or_many_requests(app, admin, make_user):
    from bananachat import db
    from bananachat.db import credits

    def queries() -> int:
        statements = []
        db.conn().set_trace_callback(statements.append)
        try:
            assert admin.get("/admin/quotas/requests").status_code == 200
        finally:
            db.conn().set_trace_callback(None)
        return len(statements)

    with app.app_context():
        first = make_user("queue0")
        credits.submit_request(first["id"], 60_000, None, "Need more tokens", kind="window", pool="api")
    queries()  # the first page of a sign-in also refreshes the session
    one = queries()
    with app.app_context():
        for number in range(1, 6):
            user = make_user(f"queue{number}")
            credits.submit_request(user["id"], reason="Need a faster rate", kind="rate", pool="chat",
                                   per="second", requests=2)
    assert queries() == one
