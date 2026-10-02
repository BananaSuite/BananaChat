"""Runtime regressions for retiring the second token allowance.

Historical slow ledger/reservation rows still consume tokens. Account queue
speed, weekly limits and the other administrator controls remain independent.
"""

from datetime import datetime, timedelta, timezone

import pytest


def _policy(pool, **sections):
    from bananachat.db import limits as store

    policy = store.get_policy(pool)
    for scope, values in sections.items():
        policy[scope].update(values)
    store.set_policy(pool, policy, None)


def _historical_charge(user_id, tokens, *, pool="api", slow=False, at=None):
    from bananachat import db
    from bananachat.db import limits as store

    at = at or datetime.now(timezone.utc) - timedelta(seconds=1)
    db.execute("INSERT INTO credit_ledger (user_id, credits_used, is_slow, tokens_in, tokens_out, request_type, "
               "created_at) VALUES (?,?,?,?,0,?,?)",
               (user_id, tokens / 1000, int(slow), tokens, pool, db.timestamp(at)))
    store.open_windows(user_id, [store.pool_scope(pool)], at)


def test_legacy_result_fields_cannot_offer_an_extra_allowance():
    from bananachat.db.credits import Budget
    from bananachat.services.limits import Admission, Window

    budget = Budget("api", False, 1000, 99_000, 1000, 0)
    window = Window("window", True, 1000, 99_000, 1000, 0)
    assert budget.regular_left == budget.slow_left == window.slow_left == 0
    assert not budget.available and window.exhausted
    assert not budget.next_is_slow and not Admission(None, budget).slow


@pytest.mark.parametrize("pool", ["api", "chat", "agent"])
def test_historical_slow_usage_counts_in_the_single_budget(app, make_user, pool):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("historic")
    with app.app_context():
        _policy(pool, window={"enabled": True, "tokens": 3000}, weekly={"enabled": True, "tokens": 5000})
        _historical_charge(user["id"], 1000, pool=pool)
        _historical_charge(user["id"], 2000, pool=pool, slow=True)
        current = limits.effective(user, pool)
        assert current.window.used == current.weekly.used == 3000
        assert current.window.slow_tokens == current.window.slow_used == 0
        assert current.window.exhausted and not current.budget().available
        assert credits.usage_today(user["id"], pool) == (3000, 0)
        assert not limits.admit(user, pool, None).allowed
        assert store.usage(user["id"], pool, "2000-01-01", "2000-01-01") == (3000, 0, 3000)
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=? AND is_slow=1", (user["id"],)) == 1


def test_historical_image_reservations_share_the_budget_and_finalize_once(app, make_user):
    from bananachat import db
    from bananachat.db import credits

    user = make_user("reserved")
    with app.app_context():
        _policy("api", window={"tokens": 1000}, weekly={"enabled": True, "tokens": 3000})
        _historical_charge(user["id"], 500, slow=True)
        stamp = db.now()
        reservation = db.execute("INSERT INTO image_credit_reservations "
                                 "(user_id, credits_reserved, is_slow, created_at, updated_at) VALUES (?,?,1,?,?)",
                                 (user["id"], .3, stamp, stamp)).lastrowid
        assert credits.budget(user, "api").regular_left == 200
        assert credits.usage_today(user["id"], "api") == (800, 0)
        with pytest.raises(credits.InsufficientCredits) as error:
            credits.reserve_image(user, 201, ttl_seconds=3600)
        assert not error.value.weekly
        next_reservation = credits.reserve_image(user, 200, ttl_seconds=3600)
        assert db.scalar("SELECT is_slow FROM image_credit_reservations WHERE id=?", (next_reservation,)) == 0
        assert not credits.budget(user, "api").available
        credits.finalize_reservation(reservation, user_id=user["id"], tokens=300)
        assert credits.budget(user, "api").regular_used == 1000
        assert db.scalar("SELECT COUNT(*) FROM image_credit_reservations WHERE id=?", (reservation,)) == 0
        assert db.one("SELECT credits_used, is_slow FROM credit_ledger ORDER BY id DESC")[0] == .3
        credits.refund_reservation(next_reservation)
        assert credits.budget(user, "api").regular_left == 200


def test_charges_never_switch_to_a_second_budget(app, make_user):
    from bananachat import db
    from bananachat.db import credits

    user = make_user("charged")
    with app.app_context():
        _policy("api", window={"tokens": 1000})
        _historical_charge(user["id"], 950, slow=True)
        assert credits.charge(user["id"], 100, 50, request_type="api") == (150, False)
        current = credits.budget(user, "api")
        assert current.regular_used == 1100 and not current.available
        assert current.slow_limit == current.slow_used == 0
        assert db.scalar("SELECT is_slow FROM credit_ledger ORDER BY id DESC") == 0


def test_weekly_historical_usage_still_blocks_after_the_five_hour_window_ends(app, make_user):
    from bananachat.db import credits
    from bananachat.services import limits

    user = make_user("weekly")
    with app.app_context():
        _policy("api", window={"tokens": 1000}, weekly={"enabled": True, "tokens": 2000})
        _historical_charge(user["id"], 2000, slow=True,
                           at=datetime.now(timezone.utc) - timedelta(hours=6))
        current = credits.budget(user, "api")
        assert current.regular_used == 0 and current.weekly_used == 2000
        assert current.weekly_exhausted and not current.available
        assert limits.admit(user, "api", None).refusal.key == "weekly"
        with pytest.raises(credits.InsufficientCredits) as error:
            credits.reserve_image(user, 1, ttl_seconds=3600)
        assert error.value.weekly


@pytest.mark.parametrize("speed,expected", [("normal", 2), ("fast", 1), ("slow", 4)])
def test_queue_speed_remains_an_independent_account_setting(app, make_user, speed, expected):
    from bananachat.db import limits as store
    from bananachat.services import queue

    user = make_user("speed")
    with app.app_context():
        store.update_user_settings(user["id"], None, speed=speed)
        assert queue.priority_for(user, api=True) == expected
        assert queue.priority_for(user, api=True, slow=True) == expected
        assert queue.priority_for({"id": user["id"], "role": "admin"}, slow=True) == queue.PRIORITY_ADMIN


def test_speed_rate_adjustment_is_preserved(app, make_user):
    from bananachat.services import limits, queue

    user = make_user("rate")
    with app.app_context():
        _policy("api", rate={"rules": [{"requests": 10, "per": "minute", "burst": 10}]})
        limits.set_speed(user["id"], "slow", adjust_rate=True, updated_by=None)
        assert queue.priority_for(user, api=True) == queue.PRIORITY_SLOW
        assert limits.base_limits(user["id"], "api")["rate_rules"] == \
            [{"requests": 5, "per": "minute", "burst": 5}]
        limits.set_speed(user["id"], "normal", adjust_rate=True, updated_by=None)
        assert queue.priority_for(user, api=True) == queue.PRIORITY_API
        assert limits.base_limits(user["id"], "api")["rate_rules"][0]["requests"] == 10


@pytest.mark.parametrize("mode,window_bonus,weekly_bonus", [("fixed", 1500, 7000), ("multiplier", 1000, 4000)])
def test_music_bonus_preserves_independent_five_hour_and_weekly_amounts(app, make_user, mode,
                                                                      window_bonus, weekly_bonus):
    from bananachat import db
    from bananachat.db import credits, settings
    from bananachat.services import limits

    user = make_user("music")
    with app.app_context():
        _policy("api", window={"tokens": 1000}, weekly={"enabled": True, "tokens": 4000})
        settings.update(music_enabled=1, music_bonus_mode=mode, music_credit_multiplier=2,
                        music_bonus_fixed_tokens=1500, music_bonus_fixed_weekly_tokens=7000)
        db.execute("UPDATE users SET music_opted_in=1 WHERE id=?", (user["id"],))
        current = limits.effective(user, "api")
        assert current.window.tokens == 1000 + window_bonus
        assert current.weekly.tokens == 4000 + weekly_bonus
        assert current.window.slow_tokens == current.weekly.slow_tokens == 0
        assert credits.music_bonus(user["id"])[2] == 0
        assert credits.music_weekly_fixed({"music_bonus_fixed_tokens": 1500}) == 10_500


def test_tiers_dynamic_floors_and_grants_still_scale_one_budget(app, make_user, monkeypatch):
    from bananachat import db
    from bananachat.db import limits as store
    from bananachat.services import limits

    user = make_user("scaled")
    with app.app_context():
        _policy("api", window={"tokens": 1000, "auto_tiers": True, "dynamic": True})
        tier_id = store.create_tier({"name": "Established", "multiplier": 2})
        store.update_user_settings(user["id"], None, tier_id=tier_id)
        monkeypatch.setattr(limits, "dynamic_for", lambda *_args: limits.Dynamic(1.5, 1.5, 1))
        store.create_grant(created_by=None, user_id=user["id"], pool="api", scope="window",
                           kind="multiplier", amount=2, starts_at=db.now(-timedelta(minutes=1)),
                           ends_at=db.now(timedelta(hours=1)), reason="test")
        store.create_grant(created_by=None, user_id=user["id"], pool="api", scope="window",
                           kind="extra", amount=500, starts_at=db.now(-timedelta(minutes=1)),
                           ends_at=db.now(timedelta(hours=1)), reason="test")
        assert limits.effective(user, "api").window.tokens == 6500  # 1000 × tier2 × dynamic1.5 × grant2 +500
        store.set_override(user["id"], "api", None, window_tokens=2500)
        monkeypatch.setattr(limits, "dynamic_for", lambda *_args: limits.Dynamic(.5, .5, 1))
        current = limits.effective(user, "api")
        assert current.window.tokens == 5500  # the custom floor survives high demand
        assert "custom_floor" in [reason.code for reason in current.window.reasons]
        assert current.window.slow_tokens == 0


def test_quota_requests_ignore_legacy_slow_values_and_allow_raises_above_old_ceiling(app, make_user):
    from bananachat import db
    from bananachat.db import credits, settings

    user = make_user("quota")
    with app.app_context():
        credits.set_quota(user["id"], 200_000_000, 999_999_999, None)
        assert credits.get_quota(user["id"]) == (200_000_000, 0)
        with pytest.raises(credits.RequestError) as error:
            credits.submit_request(user["id"], 200_000_000, 999_999_999, "Only slow changes")
        assert error.value.key == "quota_must_raise"
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=300_000_000)
        result = credits.submit_request(user["id"], 250_000_000, 999_999_999, "More tokens for work")
        assert result["status"] == "approved"
        assert credits.get_quota(user["id"]) == (250_000_000, 0)
        row = db.one("SELECT new_tokens, new_slow_tokens, new_slow_credits FROM quota_requests WHERE id=?",
                     (result["id"],))
        assert tuple(row) == (250_000_000, 0, 0)


@pytest.mark.parametrize("exhausted", [False, True])
def test_api_headers_and_admission_count_historical_slow_usage(app, make_user, fake_ollama, exhausted):
    from bananachat.db import credits
    from tests.app.test_api import chat, roll_out, token_for

    roll_out(app)
    user = make_user("api")
    raw = token_for(app, user)
    with app.app_context():
        _policy("api", window={"tokens": 2000})
        _historical_charge(user["id"], 500)
        _historical_charge(user["id"], 1500 if exhausted else 500, slow=True)
    response = chat(app, raw, model="llama3.2:3b")
    assert response.status_code == (429 if exhausted else 200), response.get_data(as_text=True)[:500]
    assert response.headers["x-ratelimit-limit-tokens"] == "2000"
    with app.app_context():
        current = credits.budget(user, "api")
        assert response.headers["x-ratelimit-remaining-tokens"] == str(int(current.regular_left))
    assert not any("slow" in name.lower() or name.lower().endswith("-mode") for name in response.headers.keys())
    if exhausted:
        assert response.json["error"]["code"] == "insufficient_quota"
        assert not fake_ollama.chat_bodies()
    else:
        assert current.regular_used == 1000 + response.json["usage"]["total_tokens"]
