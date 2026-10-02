"""Unavailable native telemetry keeps a useful admin readout without granting capacity."""

import time

import pytest

from bananachat.db import claude_pool as accounts
from bananachat.services import claude_pool as pool


@pytest.fixture
def telemetry(app):
    pool.register_site_chat(lambda *args: iter([{"text": "answer", "done": True}]))
    pool.register_site_discovery(lambda: [{"name": "display-sonnet", "family": "sonnet", "reasoning": []}])
    aid = accounts.add_account("Display account", window_limit=100_000)
    report = {"source": "claude_code", "available": False, "status": "stale",
              "observed_at": time.time() - 1_200, "subscription_type": "max",
              "window_left": 0.0, "window_resets_at": time.time() + 1_800,
              "weekly_left": 0.84, "weekly_resets_at": time.time() + 100_000, "model_limits": {}}
    pool.register_site_reporter(lambda: {"account_reports": {str(aid): dict(report)}})
    yield aid, report
    pool.reset_transport()


def test_admin_retains_plan_and_original_stale_percentages_without_routing(app, admin, telemetry):
    aid, report = telemetry
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Subscription: Claude Max" in html
    assert "Last known usage · stale." in html
    assert "Requests are paused until a fresh report is available" in html
    assert "5-hour remaining: 0.0%" in html
    assert "Weekly remaining: 84.0%" in html
    assert "Local token budget: 0 / 100000" in html
    assert pool.account_reports()[str(aid)]["observed_at"] == report["observed_at"]
    assert not pool.usable(accounts.get(aid))


def test_fresh_observation_replaces_stale_label_and_can_resume_routing(app, admin, telemetry):
    aid, report = telemetry
    admin.get("/admin/models/claude")
    report.update(status="available", available=True, observed_at=time.time(), window_left=0.9)
    pool.refresh_quota()
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Last known usage · stale." not in html
    assert "5-hour remaining: 90.0%" in html
    assert pool.usable(accounts.get(aid))


def test_refresh_explains_staleness_without_claiming_success(admin, telemetry):
    response = admin.post("/admin/models/claude/refresh", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert "waiting for a fresh subscription report" in html
    assert "Last-known figures are marked stale" in html
    assert "Claude pool quota re-read" not in html


def test_future_observation_is_not_presented_as_last_known(admin, telemetry):
    _, report = telemetry
    report["observed_at"] = time.time() + 100
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Last known usage · stale." not in html
    assert "Weekly remaining: 84.0%" not in html
    assert "Subscription usage is unavailable" in html


def test_missing_quota_keeps_verified_plan_without_making_up_percentages(admin, telemetry):
    _, report = telemetry
    report.update(window_left=None, weekly_left=None)
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Subscription: Claude Max" in html
    assert "Subscription usage is unavailable" in html
    assert "Last known usage · stale." not in html
    assert "Weekly remaining: 84.0%" not in html


def test_subscription_readout_requires_admin_authentication(app, telemetry):
    response = app.test_client().get("/admin/models/claude")
    assert response.status_code in (302, 401, 403)
    assert "Subscription: Claude Max" not in response.get_data(as_text=True)
