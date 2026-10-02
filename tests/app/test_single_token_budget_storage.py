"""Deprecated allowance writes are inert; historical usage still spends the one budget."""

import json

import pytest


def test_policy_ignores_deprecated_field_preserves_supported_customizations(app):
    from bananachat import db
    from bananachat.db import limits

    with app.app_context():
        policy = limits.get_policy("api")
        policy["window"].update(tokens=12_345, slow_tokens=999_999_999, dynamic=True, auto_tiers=True)
        policy["weekly"].update(enabled=True, tokens=456_789)
        clean = limits.set_policy("api", policy, None)
        assert clean["version"] == 3
        assert clean["window"] == {"enabled": True, "tokens": 12_345, "dynamic": True, "auto_tiers": True}
        assert clean["weekly"]["tokens"] == 456_789
        assert "slow_tokens" not in json.loads(db.scalar("SELECT config FROM limit_policy WHERE pool='api'"))["window"]


def test_ignored_slow_only_override_creates_no_row_and_does_not_change_existing(app, make_user):
    from bananachat import db
    from bananachat.db import limits

    user = make_user("uma")
    with app.app_context():
        assert limits.set_override(user["id"], "api", None, window_slow_tokens=9000) is None
        assert db.scalar("SELECT COUNT(*) FROM user_limit_overrides") == 0
        current = limits.set_override(user["id"], "api", None, window_tokens=15_000, weekly_tokens=123_456,
                                      automatic=True)
        after = limits.set_override(user["id"], "api", None, window_slow_tokens=999_999_999)
        assert after == current
        assert not hasattr(after, "window_slow_tokens")
        row = db.one("SELECT * FROM user_limit_overrides WHERE user_id=?", (user["id"],))
        assert row["window_slow_tokens"] == row["daily_slow_credits"] == 0
        assert after.automatic == frozenset({"window_tokens", "weekly_tokens"})
        assert limits.set_override(user["id"], "api", None, window_tokens=None, weekly_tokens=None) is None


def test_deprecated_settings_are_zero_and_weekly_music_remains_writable(app):
    from bananachat import db
    from bananachat.db import settings

    with app.app_context():
        settings.update(**{name: 123 for name in settings.DEPRECATED_SLOW_COLUMNS},
                        music_bonus_fixed_tokens=70_000, music_bonus_fixed_weekly_tokens=90_000,
                        chat_local_token_consumption=1)
        row = settings.get()
        assert all(row[name] == 0 for name in settings.DEPRECATED_SLOW_COLUMNS)
        assert all(db.scalar(f"SELECT {name} FROM site_settings") == 0 for name in settings.DEPRECATED_SLOW_COLUMNS)
        assert (row["music_bonus_fixed_tokens"], row["music_bonus_fixed_weekly_tokens"]) == (70_000, 90_000)
        assert row["chat_local_token_consumption"] == 1


def test_usage_counts_regular_and_historical_slow_ledger_and_image_reservations(app, make_user):
    from bananachat import db
    from bananachat.db import limits

    user = make_user("uma")
    with app.app_context():
        for slow, used, request_type in ((0, 3, "api"), (1, 7, "playground"), (1, 11, "chat")):
            db.execute("INSERT INTO credit_ledger (user_id,credits_used,is_slow,request_type,created_at) "
                       "VALUES (?,?,?,?,?)", (user["id"], used, slow, request_type, db.now()))
        for slow, used in ((0, 2), (1, 5)):
            db.execute("INSERT INTO image_credit_reservations (user_id,credits_reserved,is_slow) VALUES (?,?,?)",
                       (user["id"], used, slow))
        start = "2000-01-01 00:00:00"
        assert limits.usage(user["id"], "api", start, start) == (17_000, 0, 17_000)
        assert limits.usage(user["id"], "chat", start, start) == (11_000, 0, 11_000)
        assert limits.usage(user["id"], "chat", None, None) == (0, 0, 0)
        assert db.scalar("SELECT SUM(is_slow) FROM credit_ledger") == 2
        assert db.scalar("SELECT SUM(is_slow) FROM image_credit_reservations") == 1


def test_migrated_tier_derived_override_remains_editable_with_bounded_storage(app, make_user):
    from bananachat.db import limits

    user = make_user("uma")
    with app.app_context():
        own = limits.set_override(user["id"], "api", None, window_tokens=4_020_000_000)
        assert own.window_tokens == 4_020_000_000
        own = limits.set_override(user["id"], "api", None, window_tokens=limits.OVERRIDE_WINDOW_MAX)
        assert own.window_tokens == 200_000_000_000
        with pytest.raises(ValueError):
            limits.set_override(user["id"], "api", None, window_tokens=limits.OVERRIDE_WINDOW_MAX + 1)
        with pytest.raises(ValueError):
            limits.set_override(user["id"], "api", None, weekly_tokens=limits.TOKENS_MAX + 1)
