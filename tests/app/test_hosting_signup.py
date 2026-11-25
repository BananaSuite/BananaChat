""""Create an account" through BananaWiki Hosting (web/hosting.py, services/hosting_signin.py).

In ``hosting`` mode the sign-in page links to the portal's sign-up (``prompt=create``) while the portal's
metadata says it takes new accounts; the account here is then created at once, named like the portal
account, under this site's sign-up policy, and never while a hosting administrator acts.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.app.fixtures import Browser
from tests.app.test_hosting_signin import _english, _link_of, _make_user, _settings, _start, _user, _whoami
from tests.app.test_hosting_signin_methods import Portal

CREATE_LINK = 'href="/login/hosting/start?create=1"'
METADATA = ("GET", "/.well-known/oauth-authorization-server")


@pytest.fixture
def portal(monkeypatch):
    from bananachat.services import hosting_signin

    fake = Portal()
    monkeypatch.setattr(hosting_signin, "_request", fake.request)
    return fake


@pytest.fixture
def happ(make_app):
    """An application that only BananaWiki Hosting signs in to, with open sign-up."""
    from tests.app.test_hosting_signin import HOSTING_ENV

    app = make_app(**HOSTING_ENV)
    _settings(app, signin_mode="hosting", signup_mode="open")
    return app


def _create(browser, portal, sub="sub-new", name="newbie"):
    """Click "Create an account", sign up at the portal and come back: the callback's response."""
    location = _start(browser, "/login/hosting/start?create=1")
    assert parse_qs(urlsplit(location).query)["prompt"] == ["create"]
    return browser.get(portal.authorize(location, sub, name))


def _forget_signup_state(app):
    app.extensions.pop("bananachat.portal_signup", None)


def test_the_link_is_shown_while_the_portal_takes_new_accounts(happ, portal):
    browser = _english(Browser(happ))
    page = browser.get("/login?next=/account").get_data(as_text=True)
    assert 'href="/login/hosting/start?create=1&amp;next=/account">Create an account</a>' in page
    assert 'href="/signup"' not in page
    portal.signup = "invite"
    _forget_signup_state(happ)
    assert CREATE_LINK in browser.get("/login").get_data(as_text=True)


@pytest.mark.parametrize("answer", ["closed", None])
def test_the_link_is_hidden_when_the_portal_is_closed_or_silent(happ, portal, answer):
    portal.signup = answer
    assert "create=1" not in _english(Browser(happ)).get("/login").get_data(as_text=True)


def test_an_unreachable_portal_hides_the_link(happ, portal):
    from bananachat.services.hosting_signin import OAuthError

    portal.failure = OAuthError("hosting.error_portal", "unreachable")
    assert "create=1" not in _english(Browser(happ)).get("/login").get_data(as_text=True)


def test_the_portal_is_asked_once_in_a_while(happ, portal):
    browser = Browser(happ)
    for _ in range(3):
        browser.get("/login")
    assert portal.calls.count(METADATA) == 1


def test_a_closed_site_offers_no_link(happ, portal):
    _settings(happ, signup_mode="disabled")
    assert "create=1" not in _english(Browser(happ)).get("/login").get_data(as_text=True)
    assert METADATA not in portal.calls


def test_both_mode_keeps_its_own_sign_up_and_adds_none(happ, portal):
    _settings(happ, signin_mode="both")
    browser = _english(Browser(happ))
    page = browser.get("/login").get_data(as_text=True)
    assert 'href="/signup"' in page and "create=1" not in page
    location = _start(browser, "/login/hosting/start?create=1")
    assert "prompt" not in parse_qs(urlsplit(location).query), "create=1 is for hosting-only sign-in"


def test_a_new_portal_account_gets_its_account_here_without_the_choice(happ, portal):
    browser = _english(Browser(happ))
    response = _create(browser, portal)
    assert response.status_code == 302 and response.headers["Location"] != "/login/hosting/connect"
    assert _whoami(browser) == "newbie"
    link = _link_of(happ, "newbie")
    assert link["account_id"] == "sub-new" and link["portal_username"] == "newbie"
    assert _user(happ, "newbie")["password_set"] == 0


def test_a_taken_name_falls_back_to_the_create_form(happ, portal):
    _make_user(happ, "newbie")
    browser = _english(Browser(happ))
    response = _create(browser, portal)
    page = response.get_data(as_text=True)
    assert response.status_code == 409 and 'action="/login/hosting/create"' in page
    assert "Username is already taken. Try newbie2." in page and 'value="newbie"' in page
    assert _link_of(happ, "newbie") is None and _whoami(browser) is None
    browser.get("/login/hosting/connect")  # a fresh form token for the next post
    assert browser.post("/login/hosting/create", {"username": "newbie2"}).status_code == 302
    assert _whoami(browser) == "newbie2"


def test_the_sign_up_policy_still_applies(happ, portal):
    from bananachat.db import invites

    _settings(happ, signup_mode="invite")
    browser = _english(Browser(happ))
    response = _create(browser, portal)
    assert response.status_code == 400 and 'name="invite_code"' in response.get_data(as_text=True)
    assert _user(happ, "newbie") is None
    with happ.app_context():
        code = invites.create(_user(happ, "admin")["id"], max_uses=1)
    invited = _english(Browser(happ))
    location = _start(invited, f"/login/hosting/start?create=1&invite={code}")
    assert invited.get(portal.authorize(location, "sub-new", "newbie")).status_code == 302
    assert _whoami(invited) == "newbie" and _user(happ, "newbie")["invite_code"] == code.upper()


def test_no_account_is_created_while_acting(happ, portal):
    portal.act = {"sub": "sub-admin", "preferred_username": "chief"}
    portal.act_expires_at = int(time.time()) + 900
    browser = _english(Browser(happ))
    response = _create(browser, portal)
    assert response.headers["Location"] == "/login"
    assert _user(happ, "newbie") is None and _whoami(browser) is None
