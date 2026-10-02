"""Community consent for quota requests: votes, renounced tokens, automatic approval, closing, cancelling,
model-specific requests and the pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def _settings(**values):
    from bananachat.db import settings

    defaults = {"community_min_account_days": 0}
    settings.update(**{**defaults, **values})


def _people(make_user, *names):
    return [make_user(name) for name in names]


def _submit(app, user, **kwargs):
    from bananachat.db import credits

    with app.test_request_context():
        kwargs.setdefault("reason", "Need more for a project")
        kwargs.setdefault("community", True)
        return credits.submit_request(user["id"], **kwargs)


def _vote(app, user, request_id, stance="support", tokens=0):
    from bananachat.services import community

    with app.test_request_context():
        return community.vote(user, request_id, stance, tokens)


def _row(app, request_id):
    from bananachat import db

    with app.app_context():
        return db.one("SELECT * FROM quota_requests WHERE id=?", (request_id,))


def _window_limit(app, user, pool="api"):
    from bananachat.services import limits

    with app.test_request_context():
        with limits.snapshot(fresh=True):
            return limits.effective(user, pool).window


@pytest.fixture
def people(app, make_user):
    with app.app_context():
        _settings()
        # Keep the consent examples' 30k baseline independent of site defaults.
        from bananachat.db import limits
        policy = limits.get_policy("api")
        policy["window"]["tokens"] = 30_000
        limits.set_policy("api", policy, None)
    return _people(make_user, "asker", "ann", "bob", "cid", "dee")


def test_consent_with_renounced_tokens_approves_and_moves_quota(app, people):
    asker, ann, bob, cid, _dee = people
    before = _window_limit(app, ann).tokens
    assert before == 30_000
    outcome = _submit(app, asker, kind="window", pool="api", tokens=60_000)
    assert outcome["status"] == "pending"
    request_id = outcome["id"]
    assert _row(app, request_id)["community"] == 1

    # Two supporters: not enough people yet, and the increase (30k) is not covered.
    assert _vote(app, ann, request_id, tokens=10_000)["status"] == "pending"
    assert _vote(app, bob, request_id, tokens=10_000)["status"] == "pending"
    # Promised tokens are not taken before approval.
    assert _window_limit(app, ann).tokens == before
    result = _vote(app, cid, request_id, tokens=10_000)
    assert result["status"] == "approved"
    assert result["consent"].reached

    row = _row(app, request_id)
    assert row["status"] == "approved" and row["resolution_source"] == "community"
    assert row["grant_id"] and row["boost_ends_at"]
    # The requester gets the increase as a grant; each supporter gives up what they renounced.
    assert _window_limit(app, asker).tokens == 60_000
    window = _window_limit(app, ann)
    assert window.tokens == before - 10_000
    assert any(reason.code == "renounced" for reason in window.reasons)
    with app.test_request_context():
        assert "Renounced" in next(r for r in window.reasons if r.code == "renounced").text("en")


def test_objections_and_uncovered_tokens_keep_a_request_pending(app, people):
    asker, ann, bob, cid, dee = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=60_000)["id"]
    for person in (ann, bob, cid):
        _vote(app, person, request_id, tokens=5_000)  # 15k of 30k covered
    assert _row(app, request_id)["status"] == "pending"
    with app.app_context():
        _settings(community_coverage_percent=0)  # votes alone decide ...
        _settings(community_approval_percent=80)
    # ... but three in favour and one against is 75 %, below 80 %.
    assert _vote(app, dee, request_id, "object")["status"] == "pending"
    with app.app_context():
        _settings(community_approval_percent=75)
        from bananachat.services import community
        community.close_expired()  # the job settles requests whose consent is now enough
    assert _row(app, request_id)["status"] == "approved"


def test_who_can_vote_and_how_much(app, people, make_user):
    from bananachat.services import community

    asker, ann, *_ = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=60_000)["id"]
    with pytest.raises(community.VoteError) as own:
        _vote(app, asker, request_id)
    assert own.value.key == "vote_own"
    with app.app_context():
        from bananachat.db import users
        admin = users.get_by_username("admin")
    with pytest.raises(community.VoteError) as admin_vote:
        _vote(app, admin, request_id)
    assert admin_vote.value.key == "vote_admin"
    # At most half of one's own limit can be renounced (30k × 50 %).
    with pytest.raises(community.VoteError) as too_many:
        _vote(app, ann, request_id, tokens=20_000)
    assert too_many.value.key == "vote_too_many" and too_many.value.params["max"] == 15_000
    # New accounts wait.
    with app.app_context():
        _settings(community_min_account_days=7)
    with pytest.raises(community.VoteError) as new:
        _vote(app, ann, request_id)
    assert new.value.key == "vote_too_new"


def test_closed_voting_waits_for_an_administrator_and_cancelling_releases(app, people):
    from bananachat import db
    from bananachat.db import credits
    from bananachat.services import community

    asker, ann, *_ = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=40_000)["id"]
    _vote(app, ann, request_id, tokens=5_000)
    later = (datetime.now(timezone.utc) + timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
    with app.app_context():
        assert community.close_expired(later) == [request_id]
        row = _row(app, request_id)
        assert row["status"] == "pending" and row["community_closed_at"]
        assert db.scalar("SELECT released_at FROM quota_request_votes WHERE request_id=?", (request_id,))
        assert not community.voting_open(row)
    with pytest.raises(community.VoteError):
        _vote(app, ann, request_id)
    # Still pending: the requester can cancel it at any time.
    with app.app_context():
        assert credits.cancel_request(request_id, asker["id"])
        assert _row(app, request_id)["status"] == "cancelled"
        assert not credits.cancel_request(request_id, asker["id"])


def test_an_administrator_can_decide_while_voting_and_pledges_are_released(app, people):
    from bananachat import db
    from bananachat.db import credits, users

    asker, ann, *_ = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=40_000)["id"]
    _vote(app, ann, request_id, tokens=5_000)
    with app.test_request_context():
        admin = users.get_by_username("admin")
        credits.resolve_request(request_id, admin["id"], True)
        assert _row(app, request_id)["resolution_source"] == "manual"
        assert db.scalar("SELECT released_at FROM quota_request_votes WHERE request_id=?", (request_id,))
    # The supporter keeps their tokens: the site granted it.
    assert _window_limit(app, ann).tokens == 30_000
    assert _window_limit(app, asker).tokens == 40_000


def test_disabled_or_excluded_kinds_only_wait_for_administrators(app, people):
    asker, *_ = people
    with app.app_context():
        _settings(community_kinds="window,weekly")
    request_id = _submit(app, asker, kind="temporary", pool="api", hours=24, unlimited=True)["id"]
    assert _row(app, request_id)["community"] == 0
    with app.app_context():
        from bananachat.db import credits
        credits.cancel_request(request_id, asker["id"])
        _settings(community_quota_enabled=0, community_kinds="window,weekly,rate,temporary,effort")
    request_id = _submit(app, asker, kind="window", pool="api", tokens=40_000)["id"]
    assert _row(app, request_id)["community"] == 0


def _available_claude(monkeypatch, model_name):
    """Community tests use a discovered fake provider, never curated availability."""
    from bananachat.db import claude_pool as pool_db
    from bananachat.services import claude_pool

    monkeypatch.setattr(claude_pool, "_site_discovery", lambda: [
        {"name": model_name, "reasoning": ["low", "medium", "high", "extra", "max"]}])
    monkeypatch.setattr(claude_pool, "_site_chat", lambda *args, **kwargs: iter(()))
    pool_db.add_account("community-fixture", window_limit=100_000)


def test_unlimited_for_a_day_on_one_model_by_votes(app, people, monkeypatch):
    """A temporary unlimited boost for one model's own limits (e.g. a strict cloud model)."""
    from bananachat import db
    from bananachat.db import catalog
    from bananachat.db import limits as limits_db
    from bananachat.services import claude_pool, limits

    asker, ann, bob, cid, _ = people
    with app.test_request_context():
        _available_claude(monkeypatch, "claude-opus-4-1")
        claude_pool.sync_catalog(selected=["claude-opus-4-1"], source="test")
        model = catalog.get_by_name("claude-opus-4-1")
        db.execute("UPDATE ai_models SET is_rolled_out=1 WHERE id=?", (model["id"],))
        limits_db.set_model_policy(model["id"], claude_pool.strict_policy(model["ollama_name"]), None)
        assert limits.model_limits(asker, model).window.limited
    request_id = _submit(app, asker, kind="temporary", pool="chat", hours=24, unlimited=True,
                         model_id=model["id"])["id"]
    row = _row(app, request_id)
    assert row["model_id"] == model["id"] and row["community"] == 1
    for person in (ann, bob):
        assert _vote(app, person, request_id)["status"] == "pending"
    assert _vote(app, cid, request_id)["status"] == "approved"
    with app.test_request_context():
        grant = limits_db.get_grant(_row(app, request_id)["grant_id"])
        assert grant["model_id"] == model["id"] and grant["kind"] == "unlimited" and grant["pool"] is None
        assert not limits.model_limits(asker, catalog.get(model["id"])).window.limited
        # Nobody else's limits changed.
        assert limits.model_limits(ann, catalog.get(model["id"])).window.limited


def test_model_window_request_approved_by_an_administrator(app, people, monkeypatch):
    from bananachat.db import catalog, credits, users
    from bananachat.db import limits as limits_db
    from bananachat.services import claude_pool, limits

    asker, *_ = people
    with app.test_request_context():
        _available_claude(monkeypatch, "claude-sonnet-4-5")
        claude_pool.sync_catalog(selected=["claude-sonnet-4-5"], source="test")
        model = catalog.get_by_name("claude-sonnet-4-5")
        limits_db.set_model_policy(model["id"], claude_pool.strict_policy(model["ollama_name"]), None)
        base = limits.model_base(asker["id"], model)
        assert base["window_enabled"] and base["window_tokens"] == 150_000
    request_id = _submit(app, asker, kind="window", pool="chat", tokens=300_000, model_id=model["id"],
                         community=False)["id"]
    with app.test_request_context():
        credits.resolve_request(request_id, users.get_by_username("admin")["id"], True)
        assert limits_db.get_model_override(asker["id"], model["id"]).window_tokens == 300_000


def test_pages(app, people):
    asker, ann, *_ = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=60_000)["id"]

    browser = Browser(app)
    browser.login("ann")
    page = browser.get("/community")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Need more for a project" in html and "asker" in html
    response = browser.post(f"/community/{request_id}/vote", {"stance": "support", "tokens": "5k"})
    assert response.status_code == 302
    with app.app_context():
        from bananachat import db
        assert db.scalar("SELECT tokens FROM quota_request_votes WHERE request_id=?", (request_id,)) == 5_000
    assert browser.post(f"/community/{request_id}/withdraw").status_code == 302

    mine = Browser(app)
    mine.login("asker")
    account = mine.get("/account").get_data(as_text=True)
    assert 'id="quota-consent"' in account and "/account/quota-request/cancel" in account
    assert mine.post("/account/quota-request/cancel").status_code == 302
    assert _row(app, request_id)["status"] == "cancelled"
    # A new request from the form, offered to the community.
    response = mine.post("/account/quota-request", {"kind": "window", "pool": "api", "tokens": "50k",
                                                    "reason": "More please", "community": "1"})
    assert response.status_code == 302
    with app.app_context():
        from bananachat.db import credits
        assert credits.pending_request(asker["id"])["community"] == 1

    admin = Browser(app)
    admin.login("admin", "admin-password")
    with admin.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    assert "Community consent" in admin.get("/admin/quotas").get_data(as_text=True)
    assert "Community" in admin.get("/admin/quotas/requests").get_data(as_text=True)
    response = admin.post("/admin/quotas/community", {
        "community_quota_enabled": "1", "community_kind_window": "1", "community_min_supporters": "2",
        "community_approval_percent": "60", "community_coverage_percent": "50", "community_hours": "24",
        "community_boost_hours": "48", "community_min_account_days": "1", "community_max_pledge_percent": "40"})
    assert response.status_code == 302
    response = admin.post("/admin/quotas/fallback", {"quota_fallback_to_local": "1"})
    assert response.status_code == 302
    with app.app_context():
        from bananachat.db import settings
        from bananachat.services import community
        options = community.settings()
        assert options["kinds"] == ("window",) and options["min_supporters"] == 2 and options["hours"] == 24
        stored = settings.get()
        assert stored["quota_fallback_to_local"] == 1 and stored["quota_fallback_to_cloud"] == 0


def test_migration_14_keeps_requests_and_the_one_pending_rule(app, people):
    import importlib
    import sqlite3

    from bananachat import db

    asker, *_ = people
    request_id = _submit(app, asker, kind="window", pool="api", tokens=40_000, community=False)["id"]
    v14 = importlib.import_module("bananachat.db.migrations.v14_community")
    with app.app_context():
        sql = db.scalar("SELECT sql FROM sqlite_master WHERE name='quota_requests'")
        assert "'cancelled'" in sql and "'community'" in sql
        assert db.scalar("SELECT 1 FROM sqlite_master WHERE name='idx_quota_requests_one_pending'")
        v14.upgrade(db.conn())  # safe to run again
        assert _row(app, request_id)["status"] == "pending"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO quota_requests (user_id, new_credits, new_slow_credits, status) "
                       "VALUES (?, 1, 0, 'pending')", (asker["id"],))
