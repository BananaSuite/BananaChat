"""Administrator interface: access control, pages, users, invitations, settings, quotas, access rules,
personalities, metrics, audit log and the database export."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tarfile
from datetime import timedelta

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def pin_csrf(browser):
    """Signing in starts a new session without a CSRF token and the first page would mint a random one;
    keep the test token so later form posts match."""
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF


def signed_in(app, username, password=None):
    browser = Browser(app)
    browser.login(username, password)
    pin_csrf(browser)
    return browser


@pytest.fixture
def admin(client):
    client.login("admin", "admin-password")
    pin_csrf(client)
    return client


def _db(app, sql, params=()):
    from bananachat import db

    with app.app_context():
        return db.query(sql, params)


def _one(app, sql, params=()):
    rows = _db(app, sql, params)
    return rows[0] if rows else None


def _admin_urls(app, method):
    """Every admin URL for *method*, with placeholder values for its arguments."""
    from werkzeug.routing import IntegerConverter

    adapter = app.url_map.bind("localhost")
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith("admin.") or method not in rule.methods:
            continue
        values = {name: 1 if isinstance(converter, IntegerConverter) else "x"
                  for name, converter in rule._converters.items()}
        yield rule.endpoint, adapter.build(rule.endpoint, values, method=method)


# ----- access control ---------------------------------------------------------------

def test_non_admins_get_403_on_every_admin_url(app, make_user):
    make_user("bob")
    browser = signed_in(app, "bob")
    checked = 0
    for method in ("GET", "POST"):
        for endpoint, url in _admin_urls(app, method):
            response = browser.get(url) if method == "GET" else browser.post(url, {"name": "x", "action": "defer"})
            assert response.status_code == 403, (method, endpoint, url, response.status_code)
            checked += 1
            json_response = browser.fetch(url, method=method)
            assert json_response.status_code == 403, (method, endpoint)
            assert json_response.get_json()["error"]["code"] == "forbidden"
    assert checked > 40


def test_anonymous_visitors_are_sent_to_sign_in(app):
    browser = Browser(app)
    for endpoint, url in _admin_urls(app, "GET"):
        response = browser.get(url)
        assert response.status_code == 302 and "/login" in response.headers["Location"], endpoint


def test_every_admin_page_renders(app, admin, make_user, fake_ollama):
    from bananachat.db import access as access_db
    from bananachat.db import catalog, credits, invites, metrics, personalities, pulls, users
    from bananachat.services import ollama

    bob = make_user("bob")
    with app.test_request_context():
        ollama.sync_catalog()
        category = catalog.create_category("Coding", "", "both")
        model = catalog.get_by_name("llama3.2:3b")
        invites.create(users.get_by_username("admin")["id"], max_uses=2)
        admin_id = users.get_by_username("admin")["id"]
        # Created first so /admin/personalities/1/edit (the URL placeholder) is a featured one.
        personalities.create(admin_id, "Guide", "Be helpful.", created_by=admin_id, kind="featured")
        personalities.create(bob["id"], "Pirate", "Talk like a pirate.", created_by=bob["id"])
        access_db.create_request("uncensored", 0, bob["id"], "I research model safety.", confirmed_safe=True,
                                 confirmed_logging=True)
        credits.get_quota(bob["id"])
        credits.submit_request(bob["id"], 100_000, 50_000, "More research please")
        metrics.record_request("chat", model_id=model["id"], user_id=bob["id"], tokens_in=10, tokens_out=20,
                               duration_ms=1200, queue_wait_ms=30, status="ok", usage_estimated=False)
        metrics.record_snapshot(gpu_memory_used_mb=1000, gpu_memory_total_mb=8000, metrics_source="nvidia-smi",
                                active_models=["llama3.2:3b"])
        pulls.enqueue("qwen3:4b", None)
    pages = [url for endpoint, url in _admin_urls(app, "GET") if "<" not in url and "/x" not in url
             and not url.endswith("/1")]
    pages += [f"/admin/users/{bob['id']}", f"/admin/models/{model['id']}/edit",
              f"/admin/models/categories/{category}/edit", "/admin/access/uncensored/0",
              f"/admin/access/model/{model['id']}", f"/admin/access/category/{category}", "/admin/metrics?range=7d",
              "/admin/metrics?range=30d", "/admin/quotas/requests?status=all", "/admin/users?q=bo",
              "/admin/audit?action=x", f"/admin/users/{bob['id']}/limits"]
    for url in pages:
        response = admin.get(url)
        assert response.status_code == 200, (url, response.get_data(as_text=True)[:2000])
    html = admin.get("/admin/").get_data(as_text=True)
    assert "1 quota request" in html and "1 access request" in html
    assert 'style="' not in html and "onclick" not in html


def test_admin_pages_use_no_inline_scripts_or_styles(admin):
    for url in ("/admin/", "/admin/users", "/admin/models", "/admin/settings", "/admin/metrics", "/admin/access",
                "/admin/quotas", "/admin/invites", "/admin/personalities", "/admin/audit", "/admin/migration"):
        html = admin.get(url).get_data(as_text=True)
        assert ' style="' not in html and " onclick=" not in html and " onsubmit=" not in html, url
        for chunk in html.split("<script")[1:]:
            assert 'type="application/json"' in chunk.split(">", 1)[0] or "src=" in chunk.split(">", 1)[0], url


# ----- users --------------------------------------------------------------------------

def test_create_user_and_manage_it(app, admin):
    response = admin.post("/admin/users/create", {"username": "carol", "password": "short", "confirm_password": "short"})
    assert response.status_code == 302
    assert _one(app, "SELECT 1 FROM users WHERE username='carol'") is None
    admin.post("/admin/users/create", {"username": "carol", "password": "carol-password",
                                       "confirm_password": "carol-password", "role": "user"})
    carol = _one(app, "SELECT * FROM users WHERE username='carol'")
    assert carol["role"] == "user"
    # Duplicate usernames are refused without an error page.
    response = admin.post("/admin/users/create", {"username": "CAROL", "password": "carol-password",
                                                  "confirm_password": "carol-password"}, follow_redirects=True)
    assert "already taken" in response.get_data(as_text=True)

    admin.post(f"/admin/users/{carol['id']}/role", {"role": "admin"})
    assert _one(app, "SELECT role FROM users WHERE id=?", (carol["id"],))["role"] == "admin"
    admin.post(f"/admin/users/{carol['id']}/role", {"role": "user"})

    # The datetime-local value is read as UTC.
    admin.post(f"/admin/users/{carol['id']}/suspend", {"until": "2099-01-02T10:30"})
    row = _one(app, "SELECT suspended, suspended_until FROM users WHERE id=?", (carol["id"],))
    assert row["suspended"] == 1 and row["suspended_until"] == "2099-01-02 10:30:00"
    response = admin.post(f"/admin/users/{carol['id']}/suspend", {"until": "2001-01-01T00:00"}, follow_redirects=True)
    assert "must be in the future" in response.get_data(as_text=True)
    admin.post(f"/admin/users/{carol['id']}/unsuspend")
    assert _one(app, "SELECT suspended FROM users WHERE id=?", (carol["id"],))["suspended"] == 0


def test_suspend_without_end_and_password_reset_sign_the_user_out(app, admin, make_user):
    dave = make_user("dave")
    browser = signed_in(app, "dave")
    assert browser.get("/admin/").status_code == 403
    response = admin.post(f"/admin/users/{dave['id']}/password", {"password": "abc", "confirm_password": "abc"},
                          follow_redirects=True)
    assert "characters" in response.get_data(as_text=True)
    admin.post(f"/admin/users/{dave['id']}/password", {"password": "new-password-1",
                                                       "confirm_password": "new-password-1"})
    assert _one(app, "SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id=?", (dave["id"],))["n"] == 0
    assert browser.get("/admin/").status_code == 302  # signed out
    browser.login("dave", "new-password-1")
    pin_csrf(browser)
    admin.post(f"/admin/users/{dave['id']}/suspend", {"until": ""})
    row = _one(app, "SELECT suspended, suspended_until FROM users WHERE id=?", (dave["id"],))
    assert row["suspended"] == 1 and row["suspended_until"] is None
    assert browser.get("/admin/").status_code == 302


def test_administrators_cannot_lock_themselves_out(app, admin):
    me = _one(app, "SELECT * FROM users WHERE username='admin'")
    for url, data in ((f"/admin/users/{me['id']}/role", {"role": "user"}),
                      (f"/admin/users/{me['id']}/suspend", {"until": ""}),
                      (f"/admin/users/{me['id']}/delete", {"confirm_username": "admin"}),
                      (f"/admin/users/{me['id']}/password", {"password": "x" * 12, "confirm_password": "x" * 12})):
        response = admin.post(url, data, follow_redirects=True)
        assert "your own account" in response.get_data(as_text=True), url
    row = _one(app, "SELECT role, suspended FROM users WHERE id=?", (me["id"],))
    assert row["role"] == "admin" and row["suspended"] == 0


def test_last_administrator_errors_are_shown(app, admin, make_user, monkeypatch):
    from bananachat.db import users

    ops = make_user("ops", role="admin")
    monkeypatch.setattr(users, "count_active_admins", lambda: 1)
    response = admin.post(f"/admin/users/{ops['id']}/role", {"role": "user"}, follow_redirects=True)
    assert "The last administrator cannot be demoted." in response.get_data(as_text=True)
    response = admin.post(f"/admin/users/{ops['id']}/delete", {"confirm_username": "ops"}, follow_redirects=True)
    assert "The last administrator cannot be deleted." in response.get_data(as_text=True)
    assert _one(app, "SELECT role FROM users WHERE id=?", (ops["id"],))["role"] == "admin"


def test_delete_requires_the_typed_username(app, admin, make_user):
    erin = make_user("erin")
    admin.post(f"/admin/users/{erin['id']}/delete", {"confirm_username": "eri"})
    assert _one(app, "SELECT 1 FROM users WHERE id=?", (erin["id"],)) is not None
    response = admin.post(f"/admin/users/{erin['id']}/delete", {"confirm_username": "erin"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/admin/users")
    assert _one(app, "SELECT 1 FROM users WHERE id=?", (erin["id"],)) is None


_POLICY_FORM = {"rate_enabled": "1", "rule_requests_0": "1", "rule_per_0": "second", "rule_burst_0": "10",
                "window_enabled": "1", "window_tokens": "30k", "window_slow_tokens": "15k", "weekly_tokens": "150k"}


def test_quota_uses_the_site_defaults(app, admin, make_user):
    frank = make_user("frank")
    admin.post("/admin/quotas/policy/api", {**_POLICY_FORM, "window_tokens": "40k", "window_slow_tokens": "20k"})
    html = admin.get("/admin/users").get_data(as_text=True)
    assert "Default (40k)" in html
    html = admin.get(f"/admin/users/{frank['id']}").get_data(as_text=True)
    assert "40k tokens per 5 hours" in html
    limits_page = f"/admin/users/{frank['id']}/limits"
    response = admin.post(limits_page + "/custom", {"pool": "api", "window_tokens": "-5", "window_slow_tokens": "1"},
                          follow_redirects=True)
    assert "write a number of tokens" in response.get_data(as_text=True)
    admin.post(limits_page + "/custom", {"pool": "api", "window_tokens": "90k", "window_slow_tokens": "9000"})
    row = _one(app, "SELECT window_tokens, window_slow_tokens, updated_by FROM user_limit_overrides "
                    "WHERE user_id=? AND pool='api'", (frank["id"],))
    assert (row["window_tokens"], row["window_slow_tokens"]) == (90_000, 0) and row["updated_by"]
    assert "90k" in admin.get("/admin/users").get_data(as_text=True)
    admin.post(limits_page + "/restore")
    assert _one(app, "SELECT 1 FROM user_limit_overrides WHERE user_id=?", (frank["id"],)) is None
    assert "40k tokens per 5 hours" in admin.get(f"/admin/users/{frank['id']}").get_data(as_text=True)


def test_sessions_are_listed_and_revoked(app, admin, make_user):
    gina = make_user("gina")
    browser = signed_in(app, "gina")
    html = admin.get(f"/admin/users/{gina['id']}").get_data(as_text=True)
    session_hash = _one(app, "SELECT id_hash FROM auth_sessions WHERE user_id=?", (gina["id"],))["id_hash"]
    assert session_hash in html
    admin.post(f"/admin/users/{gina['id']}/sessions/revoke", {"session": session_hash})
    assert browser.get("/admin/").status_code == 302
    browser.login("gina")
    pin_csrf(browser)
    admin.post(f"/admin/users/{gina['id']}/sessions/revoke-all")
    assert _one(app, "SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id=?", (gina["id"],))["n"] == 0
    # Revoking all of your own sessions keeps the current one.
    me = _one(app, "SELECT id FROM users WHERE username='admin'")
    admin.post(f"/admin/users/{me['id']}/sessions/revoke-all")
    assert admin.get("/admin/").status_code == 200


def test_user_exports_call_the_export_service(app, admin, make_user, monkeypatch):
    import sys
    import types

    hank = make_user("hank")
    fake = types.ModuleType("bananachat.services.exports")
    fake.gdpr_export = lambda user_id: {"user": user_id, "kind": "gdpr"}
    fake.chats_export = lambda user_id: {"user": user_id, "chats": []}
    monkeypatch.setitem(sys.modules, "bananachat.services.exports", fake)
    import bananachat.services as services_package
    monkeypatch.setattr(services_package, "exports", fake, raising=False)
    response = admin.get(f"/admin/users/{hank['id']}/export/gdpr")
    assert response.status_code == 200 and response.get_json() == {"user": hank["id"], "kind": "gdpr"}
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert admin.get(f"/admin/users/{hank['id']}/export/chats").get_json()["chats"] == []


# ----- invitations ----------------------------------------------------------------------

def test_invitation_is_created_used_and_deleted(app, admin):
    admin.post("/admin/invites/create", {"max_uses": "2", "expires": "7d", "role": "user", "code": "team-2026"})
    row = _one(app, "SELECT * FROM invite_codes WHERE code='TEAM-2026'")
    assert row and row["max_uses"] == 2 and row["expires_at"] > "2000"
    html = admin.get("/admin/invites").get_data(as_text=True)
    assert "/signup?invite=TEAM-2026" in html and "data-copy=" in html

    visitor = Browser(app)
    response = visitor.post("/signup", {"username": "ivan", "password": "ivan-password",
                                        "confirm_password": "ivan-password", "invite_code": "team-2026"})
    assert response.status_code == 302
    assert _one(app, "SELECT use_count FROM invite_codes WHERE id=?", (row["id"],))["use_count"] == 1
    assert "ivan" in admin.get("/admin/invites").get_data(as_text=True)

    response = admin.post("/admin/invites/create", {"max_uses": "1", "expires": "custom", "expires_at": ""},
                          follow_redirects=True)
    assert "Choose the expiry" in response.get_data(as_text=True)
    admin.post("/admin/invites/create", {"max_uses": "1", "expires": "custom", "expires_at": "2099-05-01T08:00"})
    assert _one(app, "SELECT expires_at FROM invite_codes ORDER BY id DESC LIMIT 1")["expires_at"] == \
        "2099-05-01 08:00:00"
    admin.post(f"/admin/invites/{row['id']}/delete")
    assert _one(app, "SELECT deleted FROM invite_codes WHERE id=?", (row["id"],))["deleted"] == 1


# ----- settings -------------------------------------------------------------------------

def _settings_form(**overrides):
    from bananachat.db import settings as site_settings

    form = {"site_name": "Banana HQ", "signup_mode": "open", "default_theme_mode": "light",
            "maintenance_message": "", "warning_banner_message": ""}
    for key, value in site_settings.DARK_PALETTE.items():
        form[f"{key}_color"] = value
    for key, value in site_settings.LIGHT_PALETTE.items():
        form[f"light_{key}_color"] = value
    form.update(overrides)
    return form


def test_settings_validate_colours_and_reset_palettes(app, admin):
    response = admin.post("/admin/settings", _settings_form(primary_color="red"), follow_redirects=True)
    assert "must be a colour like" in response.get_data(as_text=True)
    assert _one(app, "SELECT site_name FROM site_settings")["site_name"] == "Test Chat"
    response = admin.post("/admin/settings", _settings_form(light_bg_color="#ABCDEF;x"), follow_redirects=True)
    assert "must be a colour" in response.get_data(as_text=True)

    admin.post("/admin/settings", _settings_form(primary_color="#AA0011", warning_banner_enabled="1",
                                                 warning_banner_message="Heads up"))
    row = _one(app, "SELECT * FROM site_settings")
    assert (row["site_name"], row["signup_mode"], row["default_theme_mode"]) == ("Banana HQ", "open", "light")
    assert row["primary_color"] == "#aa0011" and row["warning_banner_enabled"] == 1
    assert "Heads up" in admin.get("/admin/").get_data(as_text=True)
    assert 'fill="#aa0011"' in admin.get("/admin/settings").get_data(as_text=True)

    response = admin.post("/admin/settings", _settings_form(warning_banner_enabled="1"), follow_redirects=True)
    assert "Write the banner message" in response.get_data(as_text=True)
    admin.post("/admin/settings/palette/reset", {"mode": "dark"})
    assert _one(app, "SELECT primary_color FROM site_settings")["primary_color"] == "#e6be32"
    response = admin.post("/admin/settings", _settings_form(signup_mode="everyone"), follow_redirects=True)
    assert "valid sign-up" in response.get_data(as_text=True)


# ----- quotas -----------------------------------------------------------------------------

def test_quota_settings_are_validated(app, admin):
    base = {"quota_auto_approve_max_tokens": "0", "quota_auto_approve_max_slow_tokens": "0"}
    for field, value in (("quota_auto_approve_max_tokens", "2000000001"), ("quota_auto_approve_max_weekly_tokens", "-1")):
        response = admin.post("/admin/quotas", {**base, field: value}, follow_redirects=True)
        text = response.get_data(as_text=True)
        assert "must be" in text or "write a number of tokens" in text, field
    # A cached form's retired setting is ignored, even if it is malformed.
    assert admin.post("/admin/quotas", {**base, "quota_auto_approve_max_slow_tokens": "abc"}).status_code == 302
    for field, value in (("window_tokens", "2000000001"), ("rule_requests_0", "-1"), ("rule_burst_0", "abc"),
                         ("rule_burst_0", "0")):
        response = admin.post("/admin/quotas/policy/api", {**_POLICY_FORM, field: value}, follow_redirects=True)
        assert "must be" in response.get_data(as_text=True), field
    admin.post("/admin/quotas", {**base, "quota_auto_approve_enabled": "1", "quota_auto_approve_max_tokens": "60k"})
    row = _one(app, "SELECT * FROM site_settings")
    assert row["slow_credits_enabled"] == 0 and row["quota_auto_approve_max_tokens"] == 60_000
    assert row["quota_auto_approve_enabled"] == 1


def test_quota_requests_are_resolved_with_a_message(app, admin, make_user):
    from bananachat.db import credits

    jane = make_user("jane")
    kurt = make_user("kurt")
    with app.app_context():
        first = credits.submit_request(jane["id"], 100_000, 40_000, "Thesis research")["id"]
        second = credits.submit_request(kurt["id"], 500_000, 40_000, "Batch jobs")["id"]
    assert "Thesis research" in admin.get("/admin/quotas/requests").get_data(as_text=True)
    admin.post(f"/admin/quotas/requests/{first}", {"decision": "approve", "message": "Enjoy"})
    row = _one(app, "SELECT status, admin_message FROM quota_requests WHERE id=?", (first,))
    assert (row["status"], row["admin_message"]) == ("approved", "Enjoy")
    assert _one(app, "SELECT window_tokens FROM user_limit_overrides WHERE user_id=?", (jane["id"],))[0] == 100_000
    admin.post(f"/admin/quotas/requests/{second}", {"decision": "deny", "message": "Too much"})
    assert _one(app, "SELECT status FROM quota_requests WHERE id=?", (second,))["status"] == "denied"
    assert _one(app, "SELECT 1 FROM user_limit_overrides WHERE user_id=?", (kurt["id"],)) is None
    response = admin.post(f"/admin/quotas/requests/{second}", {"decision": "approve"}, follow_redirects=True)
    assert "already resolved" in response.get_data(as_text=True)


# ----- access -----------------------------------------------------------------------------

def test_allowlist_with_expiry_and_request_approval(app, admin, make_user):
    from bananachat.db import access as access_db
    from bananachat.db import users
    from bananachat.services.access import AccessContext

    lena = make_user("lena")
    mike = make_user("mike")
    response = admin.post("/admin/access/uncensored/0/members", {"username": "nobody", "list_type": "allowlist"},
                          follow_redirects=True)
    assert "There is no user called nobody" in response.get_data(as_text=True)
    admin.post("/admin/access/uncensored/0/members", {"username": "lena", "list_type": "allowlist",
                                                      "reason": "Researcher", "expires_at": "2099-01-01T00:00"})
    row = _one(app, "SELECT * FROM model_access_memberships WHERE user_id=?", (lena["id"],))
    assert row["expires_at"] == "2099-01-01 00:00:00" and row["reason"] == "Researcher"
    with app.app_context():
        assert AccessContext.load(users.get(lena["id"])).allows("uncensored")
        assert not AccessContext.load(users.get(mike["id"])).allows("uncensored")
        from bananachat import db
        db.execute("UPDATE model_access_memberships SET expires_at=? WHERE user_id=?",
                   (db.now(-timedelta(minutes=1)), lena["id"]))
        assert not AccessContext.load(users.get(lena["id"])).allows("uncensored")
    assert "Expired" in admin.get("/admin/access/uncensored/0").get_data(as_text=True)

    with app.app_context():
        request_id = access_db.create_request("uncensored", 0, mike["id"], "Studying jailbreak resistance.",
                                              confirmed_safe=True, confirmed_logging=True)
    assert "Studying jailbreak resistance." in admin.get("/admin/access").get_data(as_text=True)
    admin.post(f"/admin/access/requests/{request_id}", {"decision": "approve", "message": "Granted"})
    with app.app_context():
        assert AccessContext.load(users.get(mike["id"])).allows("uncensored")
    assert _one(app, "SELECT admin_message FROM model_access_requests WHERE id=?", (request_id,))["admin_message"] \
        == "Granted"

    admin.post("/admin/access/uncensored/0/members/remove", {"user_id": mike["id"], "list_type": "allowlist"})
    with app.app_context():
        assert not AccessContext.load(users.get(mike["id"])).allows("uncensored")


def test_policies_for_models_and_categories(app, admin, make_user, fake_ollama):
    from bananachat.db import catalog, users
    from bananachat.services import ollama
    from bananachat.services.access import AccessContext

    nina = make_user("nina")
    with app.test_request_context():
        ollama.sync_catalog()
        model = catalog.get_by_name("qwen3:4b")
        catalog.set_rollout(model["id"], True)
    response = admin.post(f"/admin/access/model/{model['id']}/policy", {"mode": "sometimes"}, follow_redirects=True)
    assert "Choose a valid" in response.get_data(as_text=True)
    admin.post(f"/admin/access/model/{model['id']}/policy", {"mode": "allow_except_denylist"})
    admin.post(f"/admin/access/model/{model['id']}/members", {"username": "nina", "list_type": "denylist"})
    with app.app_context():
        model = catalog.get(model["id"])
        assert not AccessContext.load(users.get(nina["id"])).can_use(model, "chat")
    assert admin.get("/admin/access/model/999").status_code == 404
    assert admin.get("/admin/access/galaxy/0").status_code == 404


# ----- personalities ------------------------------------------------------------------------

def test_personality_moderation(app, admin, make_user):
    from bananachat.db import personalities

    olga = make_user("olga")
    response = admin.post("/admin/personalities/create", {"username": "olga", "name": "", "instructions": "x"},
                          follow_redirects=True)
    assert "Name is required" in response.get_data(as_text=True)
    admin.post("/admin/personalities/create", {"username": "olga", "name": "Poet", "instructions": "Rhyme."})
    row = _one(app, "SELECT * FROM personalities WHERE user_id=?", (olga["id"],))
    admin.post(f"/admin/personalities/{row['id']}/disable", {"reason": "Spam", "until": "2099-02-03T04:05"})
    with app.app_context():
        current = personalities.get(row["id"])
        assert current["admin_disabled"] == 1 and current["disabled_until"] == "2099-02-03 04:05:00"
        assert not personalities.is_active(current)
    assert "Spam" in admin.get("/admin/personalities?q=olga").get_data(as_text=True)
    admin.post(f"/admin/personalities/{row['id']}/enable")
    with app.app_context():
        assert personalities.is_active(personalities.get(row["id"]))
    admin.post(f"/admin/personalities/{row['id']}/delete")
    assert _one(app, "SELECT 1 FROM personalities WHERE id=?", (row["id"],)) is None


# ----- metrics ---------------------------------------------------------------------------------

def test_metrics_page_csv_and_retention(app, admin, make_user):
    from bananachat import db
    from bananachat.db import metrics
    from bananachat.services import metrics as metrics_service

    paul = make_user("paul")
    with app.app_context():
        metrics.record_request("api", model_id=None, user_id=paul["id"], tokens_in=5, tokens_out=7, duration_ms=900,
                               queue_wait_ms=12, status="ok", usage_estimated=True)
        metrics.record_request("=cmd|calc", model_id=None, user_id=None, tokens_in=-3, tokens_out=float("nan"),
                               duration_ms=10, queue_wait_ms=0, status="error", usage_estimated=False)
        db.execute("INSERT INTO request_metrics (request_type, status, created_at) VALUES ('chat', 'ok', ?)",
                   (db.now(-timedelta(days=60)),))
    html = admin.get("/admin/metrics").get_data(as_text=True)
    assert "Requests per hour" in html and "bar-chart" in html
    response = admin.get("/admin/metrics/export.csv?range=30d")
    assert response.mimetype == "text/csv" and "attachment" in response.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert len(rows) == 2
    assert rows[0]["username"] == "paul" and rows[0]["tokens_out"] == "7" and rows[0]["usage_estimated"] == "1"
    assert rows[1]["request_type"].startswith("'=")  # spreadsheet formulas are neutralised
    assert rows[1]["tokens_in"] == "0" and rows[1]["tokens_out"] == "0"

    with app.app_context():
        requests_removed, _ = metrics_service.purge(app.config["BC"])
        assert requests_removed == 1
        metrics_service.record_snapshot(app.config["BC"])
        latest = metrics.latest_snapshot()
        assert latest is not None and latest["metrics_source"] in ("nvidia-smi", "ollama-allocation", "unavailable")


def test_compute_snapshot_survives_an_unreachable_server(make_app):
    from bananachat.db import metrics
    from bananachat.services import metrics as metrics_service

    app = make_app(OLLAMA_URL="http://127.0.0.1:9")
    with app.app_context():
        metrics_service.record_snapshot(app.config["BC"])
        assert metrics.latest_snapshot()["active_models"] == []


# ----- audit and error log ----------------------------------------------------------------------

def test_actions_are_audited_and_the_log_is_filterable(app, admin, make_user):
    rita = make_user("rita")
    admin.post(f"/admin/users/{rita['id']}/role", {"role": "admin"})
    admin.post("/admin/invites/create", {"max_uses": "1", "expires": ""})
    admin.post("/admin/database-check")
    actions = {row["action"] for row in _db(app, "SELECT action FROM audit_log")}
    assert {"admin.user_role", "admin.invite_create", "admin.database_check"} <= actions
    entry = _one(app, "SELECT * FROM audit_log WHERE action='admin.user_role'")
    assert entry["target"] == "rita" and json.loads(entry["details"]) == {"from": "user", "to": "admin"}
    html = admin.get("/admin/audit?action=admin.user_role").get_data(as_text=True)
    assert "admin.user_role" in html and "admin.invite_create</code>" not in html


def test_database_check_runs_on_request(app, admin):
    response = admin.post("/admin/database-check", follow_redirects=True)
    assert "Database check passed" in response.get_data(as_text=True)


def test_error_log_is_escaped_and_not_cached(app, admin):
    path = app.config["BC"].error_log
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Traceback: <script>alert(1)</script>\n")
    response = admin.get("/admin/errors")
    html = response.get_data(as_text=True)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html
    assert response.headers["Cache-Control"] == "private, no-store"


# ----- backup & migration ------------------------------------------------------------------------

def test_database_export_is_a_restorable_archive(app, admin, tmp_path):
    from bananachat import db

    response = admin.post("/admin/migration/export")
    assert response.status_code == 200 and response.mimetype == "application/gzip"
    assert response.headers["Cache-Control"] == "private, no-store"
    archive = tmp_path / "export.tar.gz"
    archive.write_bytes(response.get_data())
    response.close()
    with tarfile.open(archive, "r:gz") as bundle:
        assert sorted(bundle.getnames()) == ["bananachat.db", "export_meta.json"]
        meta = json.load(bundle.extractfile("export_meta.json"))
        bundle.extract("bananachat.db", tmp_path, filter="data")
    assert meta["format"] == "bananachat-migration-v1" and meta["schema_version"] == db.SCHEMA_VERSION
    assert set(meta) == {"format", "exported_at", "schema_version"}
    connection = sqlite3.connect(tmp_path / "bananachat.db")
    assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert connection.execute("SELECT username FROM users").fetchall() == [("admin",)]
    connection.close()
    assert not list(app.config["BC"].instance_dir.glob(".export-*"))
    assert _one(app, "SELECT 1 FROM audit_log WHERE action='admin.migration_export'")


def test_export_is_accepted_by_the_legacy_import(app, admin, tmp_path):
    from banana_ops.legacy_import import stage_database

    archive = tmp_path / "export.tar.gz"
    archive.write_bytes(admin.post("/admin/migration/export").get_data())
    staging = tmp_path / "staging"
    staging.mkdir()
    with stage_database(archive, staging) as database:
        assert database.name == "bananachat.db" and database.stat().st_size > 0


@pytest.mark.parametrize("size", [0, 1, 511, 512, 513, 3 * 1024 * 1024 + 7])
def test_tar_stream_matches_tarfile(tmp_path, size):
    from bananachat.web.admin.migration import tar_gz_stream

    source = tmp_path / "data.bin"
    source.write_bytes(bytes(range(256)) * (size // 256) + bytes(size % 256))
    target = tmp_path / "out.tar.gz"
    target.write_bytes(b"".join(tar_gz_stream([("data.bin", source), ("meta.json", b"{}")])))
    with tarfile.open(target, "r:gz") as bundle:
        assert bundle.extractfile("data.bin").read() == source.read_bytes()
        assert bundle.extractfile("meta.json").read() == b"{}"


def test_overview_shows_the_service_status(app, admin):
    html = admin.get("/admin/").get_data(as_text=True)
    assert "Operational" in html and "No notices are shown to users." in html
    admin.post("/admin/settings", _settings_form(maintenance_mode="1", maintenance_message="Back at 18:00 UTC"))
    assert _one(app, "SELECT maintenance_mode FROM site_settings")["maintenance_mode"] == 1
    html = admin.get("/admin/").get_data(as_text=True)
    assert "Maintenance</span>" in html and "Back at 18:00 UTC" in html and "Turn off maintenance" in html
    settings_html = admin.get("/admin/settings").get_data(as_text=True)
    assert "the site stays reachable" in settings_html and 'id="maintenance-preview"' in settings_html
