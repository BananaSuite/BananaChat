"""API keys and the actions that secure an account (chat-auth-2fa-3).

Someone who briefly held a session may have created an API key, and ending sessions alone leaves it
working. Like changing the password and signing out other devices, turning on two-step verification,
an administrator's "sign out everywhere" and two-step reset offer to revoke the account's keys (ticked by
default); the operator's reset-two-step command revokes them.
"""

from __future__ import annotations

import json

from tests.app.fixtures import Browser
from tests.app.test_api import call, roll_out
from tests.app.test_two_step import _english, _enable, _secret, clock  # noqa: F401 (clock: fixture)

CHECKBOX = 'name="revoke_api_tokens" value="1" checked'


def _with_key(app, username):
    roll_out(app)
    browser = Browser(app)
    browser.login(username)
    return browser, browser.post_json("/developer/tokens", {"name": "script"}).json["token"]


def _works(app, key) -> bool:
    return call(app, "GET", "/v1/models", key).status_code == 200


def test_turning_on_two_step_offers_to_revoke_the_api_keys(app, make_user, clock):
    make_user("devon")
    browser, key = _with_key(app, "devon")
    page = _english(browser).get("/account/two-step").get_data(as_text=True)
    assert CHECKBOX in page and "Also revoke my API key" in page
    response = browser.post("/account/two-step", {"action": "enable", "code": clock.code(_secret(page)),
                                                  "password": "devon-password", "revoke_api_tokens": "1"})
    assert response.status_code == 200 and "Your API key has been revoked." in response.get_data(as_text=True)
    assert not _works(app, key)


def test_keys_stay_when_the_box_is_unticked(app, make_user, clock):
    make_user("devon")
    browser, key = _with_key(app, "devon")
    _enable(browser, "devon", clock)
    assert _works(app, key)


def test_the_two_step_page_asks_in_italian_too(app, make_user):
    make_user("devon")
    browser, _key = _with_key(app, "devon")
    browser.get("/language/it")
    assert "Revoca anche la mia chiave API" in browser.get("/account/two-step").get_data(as_text=True)


def test_an_administrator_signing_someone_out_everywhere_can_revoke_their_keys(app, admin, make_user):
    alice = make_user("alice")
    _browser, kept = _with_key(app, "alice")
    page = admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)
    assert CHECKBOX in page and "Also revoke their API key" in page
    admin.post(f"/admin/users/{alice['id']}/sessions/revoke-all", {})
    assert _works(app, kept)
    _browser, revoked = _with_key(app, "alice")
    admin.post(f"/admin/users/{alice['id']}/sessions/revoke-all", {"revoke_api_tokens": "1"})
    assert not _works(app, kept) and not _works(app, revoked)
    from tests.app.test_two_step import _audit_actions

    assert "admin.user_api_tokens_revoke_all" in _audit_actions(app)


def test_an_administrator_two_step_reset_can_revoke_the_keys(app, admin, make_user, clock):
    alice = make_user("alice")
    browser, key = _with_key(app, "alice")
    _enable(browser, "alice", clock)
    page = admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)
    assert page.count(CHECKBOX) == 2  # sign out everywhere, and the reset
    admin.post(f"/admin/users/{alice['id']}/two-step/reset", {"revoke_api_tokens": "1"})
    assert not _works(app, key)


def test_the_operator_reset_revokes_the_keys(app, make_user, clock, capsys):
    from bananachat import manage
    from tests.app.test_manage_command import _environ

    make_user("alice")
    browser, key = _with_key(app, "alice")
    _enable(browser, "alice", clock)
    assert manage.main(["reset-two-step", "alice"], environ=_environ(app)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"outcome": "reset", "username": "alice", "signed_out": True, "api_keys_revoked": 1}
    assert not _works(app, key)
