"""Second review: authentication, sessions, CSRF, authorization and the admin panel."""

from __future__ import annotations

import sqlite3

import pytest

from tests.app.conftest import Browser


def _legacy_cookie(app, user):
    """A browser holding a cookie issued by the previous release (before server-side sessions)."""
    browser = Browser(app)
    with browser.client.session_transaction() as session:
        session["user_id"] = user["id"]
        session["session_version"] = user["session_version"]
    return browser


@pytest.mark.parametrize("upgrade_legacy", [False, True])
def test_logout_retires_previous_release_cookies_but_keeps_other_current_sessions(app, make_user, upgrade_legacy):
    from bananachat.db import users

    user = make_user("lena")
    with app.app_context():
        stolen = users.get(user["id"])
    other = Browser(app)
    other.login("lena")
    if upgrade_legacy:
        leaving = _legacy_cookie(app, stolen)
    else:
        leaving = Browser(app)
        leaving.login("lena")
    assert leaving.fetch("/api/preferences").status_code == 200
    assert leaving.post("/logout").status_code == 302
    assert leaving.fetch("/api/preferences").status_code == 401
    replay = _legacy_cookie(app, stolen)
    assert replay.fetch("/api/preferences").status_code == 401
    with replay.client.session_transaction() as session:
        assert "sid" not in session
    assert other.fetch("/api/preferences").status_code == 200


@pytest.mark.parametrize("administrator", [False, True])
def test_individual_revocation_retires_legacy_replay_but_keeps_other_current_sessions(
        app, make_user, admin, administrator):
    from bananachat.db import users

    user = make_user("mira")
    with app.app_context():
        stolen = users.get(user["id"])
    owner, other = Browser(app), Browser(app)
    owner.login("mira")
    other.login("mira")
    target = _legacy_cookie(app, stolen)
    assert target.fetch("/api/preferences").status_code == 200
    with target.client.session_transaction() as session:
        id_hash = users.session_hash(session["sid"])
    if administrator:
        response = admin.post(f"/admin/users/{user['id']}/sessions/revoke", {"session": id_hash})
    else:
        response = owner.post(f"/account/sessions/{id_hash}/revoke")
    assert response.status_code == 302
    assert target.fetch("/api/preferences").status_code == 401
    assert _legacy_cookie(app, stolen).fetch("/api/preferences").status_code == 401
    assert owner.fetch("/api/preferences").status_code == 200
    assert other.fetch("/api/preferences").status_code == 200


def test_foreign_or_missing_session_revocation_does_not_retire_legacy_cookies(app, make_user):
    from bananachat.db import users

    owner, victim = make_user("owner"), make_user("victim")
    with app.app_context():
        owner_cookie, victim_cookie = users.get(owner["id"]), users.get(victim["id"])
    browser, target = Browser(app), Browser(app)
    browser.login("owner")
    target.login("victim")
    with target.client.session_transaction() as session:
        id_hash = users.session_hash(session["sid"])
    assert browser.post(f"/account/sessions/{id_hash}/revoke").status_code == 302
    assert browser.post(f"/account/sessions/{'0' * 64}/revoke").status_code == 302
    assert _legacy_cookie(app, owner_cookie).fetch("/api/preferences").status_code == 200
    assert _legacy_cookie(app, victim_cookie).fetch("/api/preferences").status_code == 200
    assert target.fetch("/api/preferences").status_code == 200


@pytest.mark.parametrize("action", ["logout", "revoke"])
def test_session_revocation_and_legacy_retirement_are_atomic(app, make_user, monkeypatch, action):
    from bananachat import db
    from bananachat.db import users

    user = make_user("nora")
    with app.app_context():
        token = users.create_session(user["id"], 7)
        before = users.get(user["id"])["session_version"]
        execute = db.execute

        def fail_legacy_retirement(sql, params=()):
            if sql.startswith("UPDATE users SET session_version="):
                raise sqlite3.OperationalError("simulated storage failure")
            return execute(sql, params)

        with monkeypatch.context() as patch:
            patch.setattr(db, "execute", fail_legacy_retirement)
            with pytest.raises(sqlite3.OperationalError, match="simulated storage failure"):
                if action == "logout":
                    users.end_session(token)
                else:
                    users.revoke_session(user["id"], users.session_hash(token))
        assert users.load_session(token)[0] is not None
        assert users.get(user["id"])["session_version"] == before


def test_signing_out_everywhere_also_ends_cookies_from_the_previous_release(app, make_user):
    from bananachat.db import users

    user = make_user("lena")
    with app.app_context():
        stolen = users.get(user["id"])
    browser = Browser(app)
    browser.login("lena")
    assert browser.post("/account/sessions/revoke-others").status_code == 302
    assert _legacy_cookie(app, stolen).fetch("/account").status_code == 401


def test_suspension_ends_cookies_from_the_previous_release_even_after_it_is_lifted(app, make_user, admin):
    from bananachat.db import users

    user = make_user("mira")
    with app.app_context():
        stolen = users.get(user["id"])
    assert admin.post(f"/admin/users/{user['id']}/suspend").status_code == 302
    assert admin.post(f"/admin/users/{user['id']}/unsuspend").status_code == 302
    assert _legacy_cookie(app, stolen).fetch("/account").status_code == 401


def test_admin_sign_out_everywhere_ends_cookies_from_the_previous_release(app, make_user, admin):
    from bananachat.db import users

    user = make_user("nils")
    with app.app_context():
        stolen = users.get(user["id"])
    assert admin.post(f"/admin/users/{user['id']}/sessions/revoke-all").status_code == 302
    assert _legacy_cookie(app, stolen).fetch("/account").status_code == 401


def test_invite_only_sign_up_does_not_reveal_usernames_without_a_valid_invitation(app, make_user):
    make_user("taken")
    response = Browser(app).post("/signup", {"username": "taken", "password": "long-password",
                                             "confirm_password": "long-password", "invite_code": "NOPE-NOPE"})
    assert response.status_code == 400
    with app.test_request_context():
        from bananachat.i18n import translate
        assert translate("en", "auth.username_taken") not in response.get_data(as_text=True)


def test_open_sign_up_does_not_record_an_invitation_that_was_never_checked(app):
    from bananachat.db import settings, users

    with app.app_context():
        settings.update(signup_mode="open")
    response = Browser(app).post("/signup", {"username": "olga", "password": "long-password",
                                             "confirm_password": "long-password", "invite_code": "X" * 5000})
    assert response.status_code == 302
    with app.app_context():
        assert users.get_by_username("olga")["invite_code"] is None


def test_unsafe_requests_to_unknown_addresses_get_404_or_405_rather_than_a_csrf_error(app, make_user):
    make_user("pat")
    browser = Browser(app)
    headers = {"Accept": "application/json"}
    response = browser.client.post("/worker/v1/no-such-endpoint", headers={"Authorization": "Bearer x", **headers})
    assert response.status_code == 404
    assert browser.client.post("/no-such-page", headers=headers).status_code == 404
    browser.login("pat")
    response = browser.client.post("/account", headers=headers)
    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == "method_not_allowed"
    # A real view still needs the token.
    assert browser.client.post("/account/sessions/revoke-others", headers=headers).status_code == 400
