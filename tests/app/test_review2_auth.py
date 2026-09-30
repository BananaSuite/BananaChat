"""Second review: authentication, sessions, CSRF, authorization and the admin panel."""

from __future__ import annotations

from tests.app.conftest import Browser


def _legacy_cookie(app, user):
    """A browser holding a cookie issued by the previous release (before server-side sessions)."""
    browser = Browser(app)
    with browser.client.session_transaction() as session:
        session["user_id"] = user["id"]
        session["session_version"] = user["session_version"]
    return browser


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
