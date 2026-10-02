"""Admin forms keep one token allowance and ignore retired slow-token fields."""

from __future__ import annotations

import pytest


POLICY_FORM = {
    "rate_enabled": "1", "rule_requests_0": "40", "rule_per_0": "minute", "rule_burst_0": "5",
    "rate_dynamic": "1", "window_enabled": "1", "window_tokens": "50k", "window_dynamic": "1",
    "window_auto_tiers": "1", "weekly_enabled": "1", "weekly_tokens": "500k", "weekly_dynamic": "1",
    "weekly_auto_tiers": "1",
}
MUSIC_FORM = {
    "music_enabled": "1", "music_visible": "1", "music_opt_in_allowed": "1", "music_opt_out_allowed": "1",
    "music_playback_mode": "shuffle", "music_bonus_mode": "fixed", "music_credit_multiplier": "2.5",
    "music_bonus_fixed_tokens": "35k", "music_bonus_fixed_weekly_tokens": "200k",
}
RETIRED_FIELDS = (
    "window_slow_tokens", "slow_credits_enabled", "quota_auto_approve_max_slow_tokens",
    "music_bonus_fixed_slow_tokens",
)


def assert_no_slow_fields(html):
    for name in RETIRED_FIELDS:
        assert f'name="{name}"' not in html
    assert "slow tokens" not in html.lower()


@pytest.mark.parametrize("legacy", ["900M", "not an amount"])
def test_policy_post_ignores_retired_amount_and_keeps_adjustments(app, admin, legacy):
    from bananachat.db import limits

    response = admin.post("/admin/quotas/policy/chat", {**POLICY_FORM, "window_slow_tokens": legacy})
    assert response.status_code == 302
    with app.app_context():
        policy = limits.get_policy("chat")
        assert policy["rate"] == {
            "enabled": True, "rules": [{"requests": 40, "per": "minute", "burst": 5}], "dynamic": True,
        }
        assert policy["window"] == {"enabled": True, "tokens": 50_000, "dynamic": True, "auto_tiers": True}
        assert policy["weekly"] == {"enabled": True, "tokens": 500_000, "dynamic": True, "auto_tiers": True}
    assert_no_slow_fields(admin.get("/admin/quotas").get_data(as_text=True))


@pytest.mark.parametrize("legacy", ["900M", "not an amount"])
def test_custom_post_ignores_retired_amount_and_keeps_rate_weekly(app, admin, make_user, legacy):
    from bananachat import db
    from bananachat.db import limits

    user = make_user("custom-user")
    response = admin.post(f"/admin/users/{user['id']}/limits/custom", {
        "pool": "api", "rule_requests_0": "12", "rule_per_0": "hour", "rule_burst_0": "3",
        "window_tokens": "42k", "weekly_tokens": "300k", "window_slow_tokens": legacy,
    })
    assert response.status_code == 302
    with app.app_context():
        override = limits.get_override(user["id"], "api")
        assert (override.window_tokens, override.weekly_tokens) == (42_000, 300_000)
        assert override.rate_rules == ({"requests": 12, "per": "hour", "burst": 3},)
        assert db.scalar("SELECT window_slow_tokens FROM user_limit_overrides WHERE user_id=?", (user["id"],)) == 0
    assert_no_slow_fields(admin.get(f"/admin/users/{user['id']}/limits").get_data(as_text=True))


def test_retired_only_custom_post_does_not_create_an_allowance(app, admin, make_user):
    from bananachat.db import limits

    user = make_user("inherited-user")
    response = admin.post(f"/admin/users/{user['id']}/limits/custom", {
        "pool": "api", "window_slow_tokens": "900M",
    })
    assert response.status_code == 302
    with app.app_context():
        assert limits.get_override(user["id"], "api") is None


@pytest.mark.parametrize("legacy", ["900M", "not an amount"])
def test_autoapproval_post_keeps_only_active_thresholds(app, admin, legacy):
    from bananachat.db import settings

    response = admin.post("/admin/quotas", {
        "quota_auto_approve_enabled": "1", "quota_auto_approve_max_tokens": "60k",
        "quota_auto_approve_max_weekly_tokens": "1.5M", "slow_credits_enabled": "1",
        "quota_auto_approve_max_slow_tokens": legacy,
    })
    assert response.status_code == 302
    with app.app_context():
        saved = settings.get()
        assert saved["quota_auto_approve_enabled"] == 1
        assert saved["quota_auto_approve_max_tokens"] == 60_000
        assert saved["quota_auto_approve_max_weekly_tokens"] == 1_500_000
        assert saved["slow_credits_enabled"] == saved["quota_auto_approve_max_slow_tokens"] == 0


@pytest.mark.parametrize("legacy", ["900M", "not an amount"])
def test_music_post_keeps_program_customization_without_second_bonus(app, admin, legacy):
    from bananachat.db import credits, settings

    response = admin.post("/admin/music/settings", {**MUSIC_FORM, "music_bonus_fixed_slow_tokens": legacy})
    assert response.status_code == 302
    with app.app_context():
        saved = settings.get()
        assert all(saved[name] == 1 for name in (
            "music_enabled", "music_visible", "music_opt_in_allowed", "music_opt_out_allowed",
        ))
        assert saved["music_playback_mode"] == "shuffle" and saved["music_bonus_mode"] == "fixed"
        assert saved["music_credit_multiplier"] == 2.5
        assert credits.music_fixed(saved) == (35_000, 0)
        assert credits.music_weekly_fixed(saved) == 200_000
        assert saved["music_bonus_fixed_slow_tokens"] == 0
    html = admin.get("/admin/music").get_data(as_text=True)
    assert_no_slow_fields(html)
    assert 'name="music_bonus_fixed_tokens"' in html and 'aria-describedby="fixed-hint"' in html
    assert 'name="music_bonus_fixed_weekly_tokens"' in html and 'value="200k"' in html


def test_legacy_music_form_preserves_independent_weekly_bonus(app, admin):
    from bananachat.db import credits, settings

    with app.app_context():
        settings.update(music_bonus_fixed_weekly_tokens=123_000)
    legacy_form = {name: value for name, value in MUSIC_FORM.items() if name != "music_bonus_fixed_weekly_tokens"}
    assert admin.post("/admin/music/settings", legacy_form).status_code == 302
    with app.app_context():
        saved = settings.get()
        assert credits.music_fixed(saved) == (35_000, 0)
        assert credits.music_weekly_fixed(saved) == 123_000


@pytest.mark.parametrize("amount", ["above maximum", "invalid amount"])
def test_invalid_music_weekly_bonus_keeps_saved_program(app, admin, amount):
    from bananachat.db import limits, settings

    maximum = 7 * limits.TOKENS_MAX
    amount = str(maximum + 1) if amount == "above maximum" else amount
    with app.app_context():
        before = settings.get()
    response = admin.post("/admin/music/settings", {**MUSIC_FORM, "music_bonus_fixed_weekly_tokens": amount},
                          follow_redirects=True)
    assert response.status_code == 200
    assert f"write a number of tokens up to {maximum:,}" in response.get_data(as_text=True)
    with app.app_context():
        after = settings.get()
        for name in MUSIC_FORM:
            assert after[name] == before[name]


@pytest.mark.parametrize("kind", ["policy", "override", "music"])
def test_combined_large_allowances_remain_editable(app, admin, make_user, kind):
    from bananachat.db import credits, limits, settings

    if kind == "policy":
        response = admin.post("/admin/quotas/policy/chat", {**POLICY_FORM, "window_tokens": "1.2B"})
    elif kind == "override":
        user = make_user("large-allowance")
        response = admin.post(f"/admin/users/{user['id']}/limits/custom", {
            "pool": "api", "window_tokens": "1.2B", "weekly_tokens": "500k",
        })
    else:
        response = admin.post("/admin/music/settings", {
            **MUSIC_FORM, "music_bonus_fixed_tokens": "1.2B", "music_bonus_fixed_weekly_tokens": "8B",
        })
    assert response.status_code == 302
    with app.app_context():
        if kind == "policy":
            assert limits.get_policy("chat")["window"]["tokens"] == 1_200_000_000
        elif kind == "override":
            assert limits.get_override(user["id"], "api").window_tokens == 1_200_000_000
        else:
            saved = settings.get()
            assert credits.music_fixed(saved) == (1_200_000_000, 0)
            assert credits.music_weekly_fixed(saved) == 8_000_000_000


def test_inherited_tier_adjusted_allowance_remains_editable(app, admin, make_user):
    from bananachat.db import limits

    user = make_user("tier-derived-budget")
    response = admin.post(f"/admin/users/{user['id']}/limits/custom", {
        "pool": "api", "window_tokens": "101B", "weekly_tokens": "500k",
    })
    assert response.status_code == 302
    with app.app_context():
        override = limits.get_override(user["id"], "api")
        assert override.window_tokens == 101_000_000_000
        assert override.weekly_tokens == 500_000


def test_refused_active_policy_preserves_fields_without_retired_input(app, admin):
    from bananachat.db import limits

    with app.app_context():
        before = limits.get_policy("chat")
    response = admin.post("/admin/quotas/policy/chat", {
        **POLICY_FORM, "window_tokens": "invalid active amount", "window_slow_tokens": "900M",
    })
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert_no_slow_fields(html)
    assert 'value="invalid active amount"' in html and 'value="500k"' in html
    assert 'name="weekly_auto_tiers" value="1" checked' in html
    with app.app_context():
        assert limits.get_policy("chat") == before


def test_refused_active_override_preserves_fields_without_retired_input(app, admin, make_user):
    from bananachat.db import limits

    user = make_user("refused-user")
    response = admin.post(f"/admin/users/{user['id']}/limits/custom", {
        "pool": "api", "window_tokens": "invalid active amount", "weekly_tokens": "300k",
        "window_slow_tokens": "900M",
    })
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert_no_slow_fields(html)
    assert 'value="invalid active amount"' in html and 'value="300k"' in html
    with app.app_context():
        assert limits.get_override(user["id"], "api") is None


def test_account_speed_remains_separate_and_historical_usage_is_unified(app, admin, make_user):
    from bananachat import db
    from bananachat.db import limits
    from bananachat.services import queue

    user = make_user("slow-priority")
    assert admin.post(f"/admin/users/{user['id']}/limits/speed", {"speed": "slow"}).status_code == 302
    with app.app_context():
        assert limits.user_settings(user["id"]).speed == "slow"
        assert queue.priority_for(user, slow=False) == queue.PRIORITY_SLOW
        db.execute("INSERT INTO credit_ledger (user_id, credits_used, tokens_in, tokens_out, request_type, is_slow) "
                   "VALUES (?,2,1000,1000,'api',1)", (user["id"],))
    detail = admin.get(f"/admin/users/{user['id']}").get_data(as_text=True)
    assert "API 2k tokens" in detail and "Slowed down" in detail
    assert_no_slow_fields(detail)
    people = admin.get("/admin/users").get_data(as_text=True)
    assert "Default (45k)" in people and "slowed down" in people
    assert_no_slow_fields(people)
    custom = admin.get(f"/admin/users/{user['id']}/limits").get_data(as_text=True)
    assert 'name="speed" value="slow"' in custom and "Lowest priority." in custom
    assert_no_slow_fields(custom)
