"""Public limits use one token allowance, including cached forms and old usage rows."""

from __future__ import annotations

import pytest

from tests.app.conftest import Browser


def _configure(app, user, *, language="en", music_mode="fixed"):
    from bananachat import db
    from bananachat.db import catalog, credits, limits, settings, users
    from tests.app.test_api import roll_out

    roll_out(app)
    with app.app_context():
        policy = limits.get_policy("api")
        policy["window"].update(enabled=True, tokens=1000)
        policy["weekly"].update(enabled=True, tokens=3000)
        limits.set_policy("api", policy, None)
        catalog.update(catalog.get_by_name("qwen3:4b")["id"], is_reasoning=1)
        limits.set_effort_level(user["id"], None, "low")
        settings.update(music_enabled=1, music_visible=1, music_bonus_mode=music_mode,
                        music_bonus_fixed_tokens=200, music_bonus_fixed_weekly_tokens=300,
                        music_credit_multiplier=2)
        db.execute("UPDATE users SET music_opted_in=1 WHERE id=?", (user["id"],))
        prefs = users.get_preferences(user["id"])
        users.save_preferences(user["id"], {**prefs, "interface_language": language})
        credits.charge(user["id"], 50, 50, request_type="api")
        # A row written by a released version remains charged after the lane is retired.
        db.execute("UPDATE credit_ledger SET is_slow=1 WHERE user_id=?", (user["id"],))
    browser = Browser(app)
    browser.login(user["username"])
    return browser


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize("music_mode,window_limit,weekly_limit", [("fixed", 1200, 3300),
                                                                ("multiplier", 2000, 6000)])
def test_public_limits_keep_usage_and_bonus_without_a_second_allowance(app, make_user, language,
                                                                     music_mode, window_limit, weekly_limit):
    from bananachat.formatting import tokens_text

    user = make_user("nora")
    browser = _configure(app, user, language=language, music_mode=music_mode)
    account = browser.get("/account").get_data(as_text=True)
    assert f'value="100.0" max="{window_limit}.0"' in account
    assert f'value="100.0" max="{weekly_limit}.0"' in account
    assert 'id="quota_tokens"' in account and 'id="quota_weekly"' in account
    assert 'id="quota_rate"' in account and 'id="quota_effort_level"' in account
    assert 'name="slow_tokens"' not in account and '"slow":' not in account
    for path in ("/account", "/developer", "/developer/usage", "/developer/playground", "/free-quota"):
        response = browser.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "slow tokens" not in html.casefold() and "token lenti" not in html.casefold()
        assert "{slow}" not in html
    history = browser.get("/developer/usage").get_data(as_text=True)
    assert "100" in history
    music = browser.get("/free-quota").get_data(as_text=True)
    assert tokens_text(weekly_limit, language) in music
    assert ("Per week" if language == "en" else "A settimana") in music
    if music_mode == "fixed":
        for path in ("/account", "/developer", "/free-quota"):
            assert tokens_text(300, language) in browser.get(path).get_data(as_text=True)


def test_cached_quota_form_cannot_restore_a_retired_allowance(app, make_user):
    from bananachat.db import credits, settings

    user = make_user("oliver")
    browser = _configure(app, user)
    with app.app_context():
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=2500)
    response = browser.post("/account/quota-request", {
        "tokens": "2k", "slow_tokens": "999999999", "reason": "More room for my project",
    })
    assert response.status_code == 302
    with app.app_context():
        row = credits.user_requests(user["id"])[0]
        assert row["status"] == "approved" and row["new_tokens"] == 2000
        assert not row["new_slow_tokens"]
        assert credits.get_quota(user["id"]) == (2000, 0)
    html = browser.get("/account").get_data(as_text=True)
    assert "2k tokens per 5 hours" in html and "999999999" not in html


def test_fixed_weekly_bonus_is_not_promised_without_a_weekly_allowance(app, make_user):
    from bananachat.db import limits

    browser = _configure(app, make_user("pat"))
    with app.app_context():
        policy = limits.get_policy("api")
        policy["weekly"]["enabled"] = False
        limits.set_policy("api", policy, None)
    for path in ("/account", "/developer", "/free-quota"):
        html = browser.get(path).get_data(as_text=True)
        assert "300 tokens per week" not in html and "300 tokens more per week" not in html
    assert "Per week" not in browser.get("/free-quota").get_data(as_text=True)
