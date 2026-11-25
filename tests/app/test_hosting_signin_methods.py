"""Sign-in methods with BananaWiki Hosting, round 3 of the BananaSuite sign-in contract (SIGNIN-SPEC.md):
confirming with BananaWiki Hosting instead of the password (C1), no second two-step code (C2), the account
page's sign-in methods (C3), the first sign-in's choice page (C4) and hosting administrators acting as an
account (A2).

The portal is :class:`Portal`, the fake of test_hosting_signin with what round 3 adds: ``prompt`` and
``max_age`` are recorded, and the profile carries ``auth_time`` and, while an administrator acts, ``act``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.app.fixtures import Browser
from tests.app.test_hosting_signin import (
    PORTAL, FakePortal, _audit, _english, _link, _link_of, _make_user, _settings, _start, _user, _whoami,
)


class Portal(FakePortal):
    """The portal of round 3: ``auth_time`` ("now", a number, or None for an older portal) and ``act``."""

    def __init__(self):
        super().__init__()
        self.auth_time = "now"
        self.act = None
        self.act_expires_at = None
        self.queries: list[dict] = []

    def authorize(self, location, sub, username="", **kwargs):
        self.queries.append({key: values[0] for key, values in parse_qs(urlsplit(location).query).items()})
        return super().authorize(location, sub, username, **kwargs)

    def request(self, method, path, *, headers, body=None, max_bytes=None, timeout=None):
        status, data = super().request(method, path, headers=headers, body=body)
        if path == "/oauth/userinfo" and status == 200:
            data = dict(data)
            if self.auth_time == "now":
                data["auth_time"] = int(time.time())
            elif self.auth_time is not None:
                data["auth_time"] = self.auth_time
            if self.act is not None:
                data["act"] = self.act
                data["act_expires_at"] = self.act_expires_at
        return status, data


@pytest.fixture
def portal(monkeypatch):
    from bananachat.services import hosting_signin

    fake = Portal()
    monkeypatch.setattr(hosting_signin, "_request", fake.request)
    return fake


@pytest.fixture
def happ(make_app):
    from tests.app.test_hosting_signin import HOSTING_ENV

    app = make_app(**HOSTING_ENV)
    _settings(app, signin_mode="both", signup_mode="open")
    return app


def _flashes(browser, url="/account"):
    return browser.get(url).get_data(as_text=True)


def _signed_in(happ, username, sub):
    """A browser signed in to *username* (linked to *sub*) with its password."""
    _make_user(happ, username)
    _link(happ, username, sub)
    browser = _english(Browser(happ))
    browser.login(username)
    return browser


def _confirm(browser, portal, sub, next_url="/account#delete"):
    """Confirm with BananaWiki Hosting as the portal account *sub*: the callback's response."""
    response = browser.post("/account/hosting/confirm", {"next": next_url})
    assert response.status_code == 302 and response.headers["Location"].startswith(PORTAL + "/oauth/authorize?")
    return browser.get(portal.authorize(response.headers["Location"], sub, "someone"))


def _created_through_hosting(happ, portal, sub="sub-new", name="newbie"):
    """A browser signed in to a new account made by a first hosting sign-in (it has no password)."""
    browser = _english(Browser(happ))
    assert browser.get(portal.authorize(_start(browser), sub, name)).headers["Location"] == "/login/hosting/connect"
    assert browser.post("/login/hosting/create", {"username": name}).status_code == 302
    assert _whoami(browser) == name
    return browser


# ----- C1: confirming with BananaWiki Hosting -------------------------------------------------------------

def test_confirming_asks_for_a_fresh_sign_in_and_stands_for_the_password_for_ten_minutes(happ, portal):
    from bananachat import db

    browser = _signed_in(happ, "bob", "sub-bob")
    page = browser.get("/account").get_data(as_text=True)
    assert 'action="/account/hosting/confirm" id="hosting-confirm"' in page
    assert 'form="hosting-confirm" name="next" value="/account#delete"' in page
    response = _confirm(browser, portal, "sub-bob")
    assert portal.queries[-1]["prompt"] == "login" and portal.queries[-1]["max_age"] == "0"
    assert response.headers["Location"] == "/account#delete"
    page = _flashes(browser)
    assert "Confirmed. For the next 10 minutes" in page and 'name="current_password"' not in page
    # The password is not asked for the next sensitive change.
    assert browser.post("/account/chats/delete-all", {}).headers["Location"] == "/account#data"
    assert [entry["action"] for entry in _audit(happ, "account.")][-2:] == ["account.hosting_confirm",
                                                                             "account.chats_delete_all"]
    # It counts for the administrators' confirmations too, and lapses after ten minutes.
    with happ.app_context():
        row = db.one("SELECT verified_at, hosting_confirmed_at FROM auth_sessions WHERE user_id=?",
                     (_user(happ, "bob")["id"],))
        assert row["verified_at"] == row["hosting_confirmed_at"] is not None
        db.execute("UPDATE auth_sessions SET hosting_confirmed_at=? WHERE user_id=?",
                   (db.now(-timedelta(minutes=11)), _user(happ, "bob")["id"]))
    browser.post("/account/chats/delete-all", {})
    assert [entry["action"] for entry in _audit(happ, "account.chats")] == ["account.chats_delete_all"]
    assert 'name="password"' in _flashes(browser)


@pytest.mark.parametrize("sub,auth_time,message", [
    ("sub-mallory", "now", "a different BananaWiki Hosting account"),
    ("sub-bob", None, "didn&#39;t report when you signed in"),
    ("sub-bob", int(time.time()) - 3600, "didn&#39;t ask you to sign in again"),
])
def test_a_confirmation_is_refused_for_another_account_or_without_a_fresh_auth_time(happ, portal, sub, auth_time,
                                                                                     message):
    browser = _signed_in(happ, "bob", "sub-bob")
    portal.auth_time = auth_time
    assert _confirm(browser, portal, sub).headers["Location"] == "/account#delete"
    page = _flashes(browser)
    assert message in page and "Confirmed. For the next" not in page
    assert 'name="password"' in page  # still asked
    browser.post("/account/chats/delete-all", {})
    assert "account.chats_delete_all" not in [entry["action"] for entry in _audit(happ, "account.")]
    assert "account.hosting_confirm" not in [entry["action"] for entry in _audit(happ, "account.")]


def test_a_confirmation_finishes_only_in_the_session_that_started_it(happ, portal):
    from bananachat.services import hosting_signin

    def confirmations():
        return [entry["action"] for entry in _audit(happ, "account.hosting_confirm")]

    browser = _signed_in(happ, "bob", "sub-bob")
    location = browser.post("/account/hosting/confirm", {"next": "/account"}).headers["Location"]
    callback = portal.authorize(location, "sub-bob", "bob")
    # A forged state, and the same account signed in in another browser, confirm nothing.
    browser.get(re.sub(r"state=[^&]+", "state=forged", callback))
    other = _english(Browser(happ))
    other.login("bob")
    other.get(callback)
    assert confirmations() == []
    # A state bound to another login session of the account confirms nothing either.
    with browser.client.session_transaction() as session:
        states = session[hosting_signin.STATE_KEY]
        session[hosting_signin.STATE_KEY] = {key: {**entry, "sid": "0" * 64} for key, entry in states.items()}
    browser.get(callback)
    assert confirmations() == [] and "took too long or was already used" in _flashes(browser)
    # A fresh one works, once.
    location = browser.post("/account/hosting/confirm", {"next": "/account"}).headers["Location"]
    callback = portal.authorize(location, "sub-bob", "bob")
    assert browser.get(callback).headers["Location"] == "/account"
    browser.get(callback)
    assert confirmations() == ["account.hosting_confirm"]


def test_the_confirm_route_needs_a_linked_signed_in_account_and_the_feature(happ, portal):
    _make_user(happ, "carol")
    browser = _english(Browser(happ))
    assert browser.post("/account/hosting/confirm", {}).headers["Location"].startswith("/login")
    browser.login("carol")
    assert browser.post("/account/hosting/confirm", {}).headers["Location"] == "/account#security"
    assert 'id="hosting-confirm"' not in browser.get("/account").get_data(as_text=True)
    _settings(happ, signin_mode="local")
    assert browser.post("/account/hosting/confirm", {}).status_code == 404
    # A next address outside the site is never followed.
    _settings(happ, signin_mode="both")
    _link(happ, "carol", "sub-carol")
    location = browser.post("/account/hosting/confirm", {"next": "https://evil.example/x"}).headers["Location"]
    assert browser.get(portal.authorize(location, "sub-carol")).headers["Location"] == "/account#security"


def test_administrators_confirm_with_bananawiki_hosting_too(happ, portal):
    from tests.app.test_two_step import _stale

    _link(happ, "admin", "sub-admin")
    admin = _english(Browser(happ))
    admin.login("admin", "admin-password")
    _stale(happ)
    page = admin.get("/admin/users").get_data(as_text=True)
    assert 'form="reauth-hosting"' in page and 'id="reauth-hosting"' in page
    assert '<input type="hidden" name="next" value="/admin/users">' in page
    response = admin.post("/account/hosting/confirm", {"next": "/admin/users"})
    assert admin.get(portal.authorize(response.headers["Location"], "sub-admin")).headers["Location"] \
        == "/admin/users"
    # Confirmed: the field is gone and a confirmed action goes through without a secret.
    assert 'name="reauth_secret"' not in admin.get("/admin/users").get_data(as_text=True)
    admin.post("/admin/users/create", {"username": "newcomer", "password": "newcomer-password",
                                       "confirm_password": "newcomer-password", "role": "user"})
    assert _user(happ, "newcomer") is not None


# ----- C3: sign-in methods ---------------------------------------------------------------------------

def test_the_account_page_has_one_sign_in_methods_section(happ, portal):
    browser = _signed_in(happ, "bob", "sub-bob")
    page = browser.get("/account").get_data(as_text=True)
    assert page.count('id="sign-in-methods"') == 1
    section = page[page.index('id="sign-in-methods"'):page.index('id="sessions"')]
    for anchor in ('id="hosting"', 'id="password"', 'id="two-step"'):
        assert anchor in section
    assert "Connected to bob since" in section and "Codes are asked after signing in with a password" in section
    assert 'action="/account/hosting/unlink"' in section and 'action="/account/password"' in section


def test_an_account_without_a_password_confirms_with_hosting_only_and_can_then_set_one(happ, portal):
    browser = _created_through_hosting(happ, portal)
    assert not _user(happ, "newbie")["password_set"]
    page = browser.get("/account").get_data(as_text=True)
    assert 'name="password"' not in page and 'name="current_password"' not in page
    assert "Your account has no password here. Confirm it&#39;s you first" in page
    assert 'action="/account/password"' not in page and "Not set" in page
    # Without a confirmation nothing is set, and no password is accepted instead.
    browser.post("/account/password", {"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert not _user(happ, "newbie")["password_set"]
    assert "Confirm it&#39;s you with BananaWiki Hosting first" in _flashes(browser)
    _confirm(browser, portal, "sub-new", "/account#password")
    page = browser.get("/account").get_data(as_text=True)
    assert 'action="/account/password"' in page and "Set password" in page
    browser.post("/account/password", {"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"})
    assert _user(happ, "newbie")["password_set"]
    assert json.loads(_audit(happ, "account.password_change")[-1]["details"]) == {"first": True}
    browser.logout()
    browser.login("newbie", "brand-new-pass")
    assert _whoami(browser) == "newbie"


def test_disconnecting_is_refused_without_a_password(happ, portal):
    browser = _created_through_hosting(happ, portal)
    _confirm(browser, portal, "sub-new", "/account#hosting")
    page = browser.get("/account").get_data(as_text=True)
    assert 'action="/account/hosting/unlink"' not in page and "Set a password before you disconnect it" in page
    browser.post("/account/hosting/unlink", {})
    assert _link_of(happ, "newbie") is not None
    assert "Set a password first" in _flashes(browser)


def test_two_step_codes_need_a_password_and_the_requirement_spares_accounts_without_one(happ, portal):
    from bananachat.db import users
    from bananachat.services import two_step

    browser = _created_through_hosting(happ, portal)
    page = browser.get("/account/two-step").get_data(as_text=True)
    assert "Your account has no password here, so set one first" in page and 'name="code"' not in page
    with happ.app_context():
        users.set_role(_user(happ, "newbie")["id"], "admin")
    _settings(happ, admin_two_step_required=1)
    with happ.app_context():
        assert not two_step.required(_user(happ, "newbie"))
        assert two_step.required(_user(happ, "admin"))
    assert browser.get("/admin/").status_code == 200


# ----- C4: the first sign-in's choice -------------------------------------------------------------------

def test_the_first_sign_in_creates_first_and_connects_behind_a_button(happ, portal):
    _make_user(happ, "carol")
    browser = _english(Browser(happ))
    browser.get(portal.authorize(_start(browser), "sub-carol", "caz"))
    page = browser.get("/login/hosting/connect").get_data(as_text=True)
    assert page.index('action="/login/hosting/create"') < page.index("I already have an account here")
    assert '<details class="hosting-existing">' in page, "closed until asked for"
    assert '<summary class="btn btn-block">I already have an account here</summary>' in page
    assert "Sign in once with its username and password" in page and "create your account" in page
    response = browser.post("/login/hosting/connect-existing", {"username": "carol", "password": "wrong"})
    body = response.get_data(as_text=True)
    assert 'value="carol"' in body and '<details class="hosting-existing" open>' in body
    response = browser.post("/login/hosting/create", {"username": "carol"})
    assert response.status_code == 409 and 'value="carol"' in response.get_data(as_text=True)


# ----- A2: hosting administrators acting as an account ---------------------------------------------------

def _act(portal, seconds=1800, name="root"):
    portal.act = {"sub": "sub-root", "preferred_username": name}
    portal.act_expires_at = int(time.time()) + seconds


def _acting_browser(happ, portal, username="bob", sub="sub-bob", role="user"):
    _make_user(happ, username, role=role)
    _link(happ, username, sub)
    _act(portal)
    browser = _english(Browser(happ))
    response = browser.get(portal.authorize(_start(browser), sub, username))
    assert response.status_code == 302 and response.headers["Location"] == "/"
    # No two-step code is asked and no device cookie is issued: the browser is the administrator's.
    assert not any("_device=" in cookie for cookie in response.headers.getlist("Set-Cookie"))
    return browser


def test_an_acting_administrator_signs_in_with_a_banner_and_is_audited_as_such(happ, portal):
    from bananachat import db

    browser = _acting_browser(happ, portal)
    assert _whoami(browser) == "bob"
    page = browser.get("/account").get_data(as_text=True)
    assert "root (BananaWiki Hosting administrator) is signed in as bob" in page
    assert 'class="acting-banner"' in page and 'action="/logout"' in page
    assert "root (BananaWiki Hosting administrator) is signed in as bob" in browser.get("/chat", follow_redirects=True).get_data(as_text=True)
    entry = _audit(happ, "auth.hosting_acting_login")[0]
    assert entry["actor_name"] == "root (BananaWiki Hosting) on behalf of bob"
    assert json.loads(entry["details"])["acting_admin_sub"] == "sub-root"
    # Every audited action of the session names the administrator.
    browser.post("/account/sessions/revoke-others", {})
    entry = _audit(happ, "account.sessions_revoke_others")[0]
    assert entry["actor_name"] == "root (BananaWiki Hosting) on behalf of bob"
    with happ.app_context():
        row = db.one("SELECT expires_at, acting_until, acting_sub FROM auth_sessions WHERE user_id=?",
                     (_user(happ, "bob")["id"],))
    assert row["acting_sub"] == "sub-root" and row["expires_at"] == row["acting_until"]


def test_an_acting_session_refuses_the_accounts_sensitive_actions(happ, portal):
    from bananachat import totp
    from bananachat.db import tokens
    from bananachat.services import two_step

    browser = _acting_browser(happ, portal)
    bob = _user(happ, "bob")
    page = browser.get("/account").get_data(as_text=True)
    assert "is signed in as this account. The password, two-step codes and sign-in methods can&#39;t be changed" \
        in page
    assert 'action="/account/password"' not in page and 'action="/account/delete"' not in page
    password = bob["password"]
    browser.post("/account/password", {"current_password": "bob-password", "new_password": "taken-over-1",
                                       "confirm_password": "taken-over-1"})
    browser.post("/account/two-step", {"action": "enable", "password": "bob-password",
                                       "code": totp.totp_code(totp.generate_secret())})
    browser.post("/account/hosting/unlink", {"password": "bob-password"})
    browser.post("/account/hosting/confirm", {})
    browser.post("/account/chats/delete-all", {"password": "bob-password"})
    browser.post("/account/delete", {"confirm_username": "bob", "password": "bob-password"})
    assert browser.post_json("/developer/tokens", {"name": "x"}).status_code == 403
    assert _user(happ, "bob")["password"] == password
    assert _link_of(happ, "bob") is not None
    with happ.app_context():
        assert not two_step.enabled(bob["id"]) and tokens.count_active(bob["id"]) == 0
    assert "Not while a BananaWiki Hosting administrator is signed in as this account" in _flashes(browser)
    actions = [entry["action"] for entry in _audit(happ, "account.")]
    assert not {"account.password_change", "account.delete", "account.hosting_unlink",
                "account.chats_delete_all", "account.hosting_confirm"} & set(actions)


def test_an_acting_administrator_gets_no_administrator_confirmation(happ, portal):
    from tests.app.test_two_step import _stale

    browser = _acting_browser(happ, portal, "chief", "sub-chief", role="admin")
    _stale(happ, "chief")
    page = browser.get("/admin/users").get_data(as_text=True)
    assert 'name="reauth_secret"' not in page and "refused while a BananaWiki Hosting administrator" in page
    browser.post("/admin/users/create", {"username": "planted", "password": "planted-password",
                                         "confirm_password": "planted-password", "role": "admin",
                                         "reauth_secret": "chief-password"})
    assert _user(happ, "planted") is None


def test_an_acting_session_ends_when_acting_ends(happ, portal):
    from bananachat import db
    from bananachat.db import users

    browser = _acting_browser(happ, portal)
    bob = _user(happ, "bob")
    with happ.app_context():
        row = db.one("SELECT * FROM auth_sessions WHERE user_id=?", (bob["id"],))
        # Sliding the expiry never goes past the end of acting.
        db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id_hash=?",
                   (db.now(-timedelta(hours=1)), row["id_hash"]))
        users.refresh_session(db.one("SELECT * FROM auth_sessions WHERE id_hash=?", (row["id_hash"],)), 7)
        assert db.scalar("SELECT expires_at FROM auth_sessions WHERE id_hash=?", (row["id_hash"],)) \
            == row["acting_until"]
        db.execute("UPDATE auth_sessions SET acting_until=?, expires_at=? WHERE id_hash=?",
                   (db.now(-timedelta(seconds=1)), db.now(-timedelta(seconds=1)), row["id_hash"]))
    assert _whoami(browser) is None


def test_acting_never_links_or_creates_an_account(happ, portal):
    from bananachat import db

    _act(portal)
    browser = _english(Browser(happ))
    response = browser.get(portal.authorize(_start(browser), "sub-stranger", "stranger"))
    assert response.headers["Location"] == "/login" and _whoami(browser) is None
    assert "never connects or creates an account" in _flashes(browser, "/login")
    assert browser.get("/login/hosting/connect").headers["Location"] == "/login"
    with happ.app_context():
        assert db.scalar("SELECT COUNT(*) FROM hosting_links") == 0
    assert _audit(happ, "auth.hosting_acting_refused")[0]["target"] == "sub-stranger"
    # Nor does a link flow from the account page finish while the portal says someone is acting.
    _make_user(happ, "carol")
    portal.act = None
    carol = _english(Browser(happ))
    carol.login("carol")
    location = carol.post("/account/hosting/link", {"password": "carol-password"}).headers["Location"]
    _act(portal)
    assert carol.get(portal.authorize(location, "sub-carol")).headers["Location"] == "/account#hosting"
    assert _link_of(happ, "carol") is None


@pytest.mark.parametrize("change,message", [
    ({"setting": 0}, "doesn&#39;t let BananaWiki Hosting administrators sign in"),
    ({"expires": -5}, "has already ended"),
    ({"act": {"preferred_username": "no-sub"}}, "could not complete the sign-in"),
    ({"act": "root"}, "could not complete the sign-in"),
])
def test_acting_is_refused_when_the_site_says_no_or_the_portal_is_unclear(happ, portal, change, message):
    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    _act(portal, change.get("expires", 1800))
    if "act" in change:
        portal.act = change["act"]
    if "setting" in change:
        _settings(happ, hosting_allow_acting=change["setting"])
    browser = _english(Browser(happ))
    assert browser.get(portal.authorize(_start(browser), "sub-bob", "bob")).headers["Location"] == "/login"
    assert _whoami(browser) is None and message in _flashes(browser, "/login")


def test_acting_reads_act_from_the_token_answer_too(happ, portal, monkeypatch):
    """An ``act`` only in the token response still counts: an acting administrator is never taken for the
    account itself."""
    from bananachat.services import hosting_signin

    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    original = hosting_signin.exchange_code

    def exchange(code, state):
        token, data = original(code, state)
        return token, {**data, "act": {"sub": "sub-root", "preferred_username": "root"},
                       "act_expires_at": int(time.time()) + 600}

    monkeypatch.setattr(hosting_signin, "exchange_code", exchange)
    browser = _english(Browser(happ))
    browser.get(portal.authorize(_start(browser), "sub-bob", "bob"))
    assert "root (BananaWiki Hosting administrator) is signed in as bob" in browser.get("/account").get_data(as_text=True)
