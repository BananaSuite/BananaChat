"""Regressions for bugs found in the pre-release correctness review."""

from __future__ import annotations

from datetime import timedelta

from tests.app.conftest import Browser


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    return browser


# ----- quotas -----------------------------------------------------------------------------------

def test_an_automatically_approved_quota_is_a_custom_quota(app, make_user, admin):
    """A raise the site granted automatically is a custom limit: new policy defaults do not undo it."""
    from bananachat.db import credits, limits, settings

    user = make_user("quinn")
    with app.app_context():
        settings.update(quota_auto_approve_enabled=1, quota_auto_approve_max_tokens=100_000,
                        quota_auto_approve_max_slow_tokens=50_000)
    _signed_in(app, "quinn").post("/account/quota-request",
                                  {"tokens": "80k", "slow_tokens": "20k", "reason": "More experiments"})
    with app.app_context():
        assert credits.get_quota(user["id"]) == (80_000, 20_000)
    with app.app_context():
        policy = limits.get_policy("api")
        policy["window"].update(tokens=10_000, slow_tokens=5000)
        limits.set_policy("api", policy, None)
        assert credits.get_quota(user["id"]) == (80_000, 20_000)
    html = admin.get(f"/admin/users/{user['id']}").get_data(as_text=True)
    assert '80k tokens + 20k tokens slow per 5 hours <span class="badge">custom</span>' in html


# ----- image reservations -------------------------------------------------------------------------

def test_abandoned_image_reservations_stop_counting_against_the_budget(app, make_user):
    """A reservation left by a process that died (no refund ran) must expire even if no image follows."""
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import background, images  # noqa: F401  (registers the job)

    user = make_user("rory")
    with app.app_context():
        credits.reserve_image(user, 10_000, ttl_seconds=app.config["BC"].image_credit_reservation_ttl)
        assert credits.usage_today(user["id"], "api")[0] == 10_000
        stale = db.now(-timedelta(seconds=app.config["BC"].image_credit_reservation_ttl + 60))
        db.execute("UPDATE image_credit_reservations SET created_at=?, updated_at=?", (stale, stale))
        background.jobs()["image-reservations"].function(app)
        assert credits.usage_today(user["id"], "api") == (0.0, 0.0)
        assert db.scalar("SELECT COUNT(*) FROM image_credit_reservations") == 0


# ----- language preference ----------------------------------------------------------------------------

def test_choosing_the_default_language_again_follows_the_browser(app, make_user):
    make_user("sofia")
    browser = _signed_in(app, "sofia")
    browser.post_json("/api/preferences", {"interface_language": "it"})
    assert '<html lang="it"' in browser.get("/customize", headers={"Accept-Language": "en"}).get_data(as_text=True)
    response = browser.post_json("/api/preferences", {"interface_language": "default"})
    assert response.get_json()["reload"] is True
    assert '<html lang="en"' in browser.get("/customize", headers={"Accept-Language": "en"}).get_data(as_text=True)

    browser.post_json("/api/preferences", {"interface_language": "it"})
    browser.post_json("/api/preferences/reset", {})
    assert '<html lang="en"' in browser.get("/customize", headers={"Accept-Language": "en"}).get_data(as_text=True)


# ----- personalities ----------------------------------------------------------------------------

def test_a_personality_created_switched_off_stays_off(app, make_user):
    """The create form's "enabled" checkbox sends nothing when unticked."""
    from bananachat.db import personalities

    user = make_user("tess")
    browser = _signed_in(app, "tess")
    browser.post("/personalities", {"name": "Draft", "instructions": "Not ready yet."})
    browser.post("/personalities", {"name": "Live", "instructions": "Ready.", "enabled": "1"})
    with app.app_context():
        state = {row["name"]: bool(row["is_enabled"]) for row in personalities.list_for(user["id"])}
    assert state == {"Draft": False, "Live": True}


# ----- metrics ----------------------------------------------------------------------------------

def test_api_metrics_durations_include_the_queue_wait(make_app, make_user):
    """request_metrics.duration_ms is the total time, queue wait included (as for chat and images)."""
    import threading
    import time

    from bananachat import db
    from tests.app.test_api import chat, roll_out, token_for

    app = make_app(MAX_CONCURRENT=1)
    roll_out(app)
    with app.test_request_context():
        from bananachat import security
        from bananachat.db import users
        user = users.get(users.create("wren", security.hash_password("wren-password")))
    token = token_for(app, user)
    with app.app_context():
        # Another request holds the only slot for a moment.
        db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, started_at, "
                   "heartbeat_at, owner_key) VALUES ('busy', 0, 'running', 1, ?, ?, ?, 'other')",
                   (time.time(), time.time(), time.time()))

    def release():
        time.sleep(0.8)
        with app.app_context():
            db.execute("DELETE FROM inference_queue WHERE req_id='busy'")

    threading.Thread(target=release).start()
    assert chat(app, token).status_code == 200
    with app.app_context():
        row = db.one("SELECT duration_ms, queue_wait_ms FROM request_metrics WHERE request_type='api'")
    assert row["queue_wait_ms"] >= 500
    assert row["duration_ms"] >= row["queue_wait_ms"]
