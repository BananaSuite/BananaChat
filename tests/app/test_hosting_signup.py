""""Create an account" through BananaWiki Hosting (web/hosting.py, services/hosting_signin.py).

In ``hosting`` mode ``/login/hosting/start?create=1`` asks the portal for its sign-up form
(``prompt=create``); the account here is then created at once, named like the portal account, under this
site's sign-up policy, and never while a hosting administrator acts. No page links to it: with the
BananaWiki Hosting button on the sign-in page there is no "Create an account".
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.app.fixtures import Browser
from tests.app.test_hosting_signin import _english, _link_of, _make_user, _settings, _start, _user, _whoami
from tests.app.test_hosting_signin_methods import Portal

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


def test_the_sign_in_page_has_no_create_link_next_to_the_hosting_button(happ, portal):
    browser = _english(Browser(happ))
    page = browser.get("/login?next=/account").get_data(as_text=True)
    assert "No account yet?" not in page and "create=1" not in page and 'href="/signup"' not in page
    assert METADATA not in portal.calls, "the portal is not asked about sign-up for the page"
    _settings(happ, signin_mode="both")
    page = browser.get("/login").get_data(as_text=True)
    assert "No account yet?" not in page and 'href="/signup"' not in page
    location = _start(browser, "/login/hosting/start?create=1")
    assert "prompt" not in parse_qs(urlsplit(location).query), "create=1 is for hosting-only sign-in"


def test_a_site_with_its_own_accounts_only_keeps_its_create_link(happ, portal):
    _settings(happ, signin_mode="local")
    page = _english(Browser(happ)).get("/login").get_data(as_text=True)
    assert "No account yet?" in page and 'href="/signup"' in page


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
