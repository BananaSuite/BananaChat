"""Production regressions for concurrent revocation and malformed auth input."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from tests.app.conftest import Browser, TEST_CSRF


@pytest.mark.parametrize("new_mode", ["disabled", "invite"])
def test_signup_rechecks_registration_policy_after_password_hashing(app, monkeypatch, new_mode):
    from bananachat import security
    from bananachat.db import settings, users

    with app.app_context():
        settings.update(signup_mode="open")
    hash_password = security.hash_password

    def hash_and_change_policy(password):
        hashed = hash_password(password)
        settings.update(signup_mode=new_mode)
        return hashed

    monkeypatch.setattr(security, "hash_password", hash_and_change_policy)
    browser = Browser(app)
    response = browser.post("/signup", {"username": "policy-race", "password": "new-password",
                                        "confirm_password": "new-password"})
    if new_mode == "disabled":
        assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    else:
        assert response.status_code == 400
    with app.app_context():
        assert users.get_by_username("policy-race") is None
    assert browser.fetch("/api/preferences").status_code == 401


@pytest.mark.parametrize("change", ["password", "revoke", "suspend"])
def test_login_cannot_restore_access_revoked_during_password_check(app, make_user, monkeypatch, change):
    from bananachat import security
    from bananachat.db import users

    user = make_user("river")
    browser = Browser(app)
    verify = security.verify_password
    changed = False

    def verify_and_revoke(stored, password):
        nonlocal changed
        valid = verify(stored, password)
        if not changed:
            changed = True
            if change == "password":
                users.set_password(user["id"], security.hash_password("replacement-password"))
            elif change == "revoke":
                users.revoke_sessions(user["id"])
            else:
                # Even a suspension lifted before the expensive check finishes
                # must retire authentication using the earlier account state.
                users.suspend(user["id"])
                users.unsuspend(user["id"])
        return valid

    monkeypatch.setattr(security, "verify_password", verify_and_revoke)
    response = browser.post("/login", {"username": "river", "password": "river-password"})
    assert response.status_code == 401
    assert browser.fetch("/api/preferences").status_code == 401
    with app.app_context():
        assert users.list_sessions(user["id"]) == []
        if change == "password":
            assert verify(users.get(user["id"])["password"], "replacement-password")


def test_login_rehash_cannot_overwrite_a_concurrent_password_reset(app, make_user, monkeypatch):
    from werkzeug.security import generate_password_hash

    from bananachat import db, security
    from bananachat.db import users

    user = make_user("willow")
    with app.app_context():
        old_hash = generate_password_hash("willow-password", method="pbkdf2:sha256:1000")
        db.execute("UPDATE users SET password=? WHERE id=?", (old_hash, user["id"]))
    hash_password = security.hash_password

    def rehash_and_reset(password):
        rehashed = hash_password(password)
        users.set_password(user["id"], hash_password("replacement-password"))
        return rehashed

    monkeypatch.setattr(security, "hash_password", rehash_and_reset)
    browser = Browser(app)
    response = browser.post("/login", {"username": "willow", "password": "willow-password"})
    assert response.status_code == 401
    with app.app_context():
        assert security.verify_password(users.get(user["id"])["password"], "replacement-password")
        assert users.list_sessions(user["id"]) == []


@pytest.mark.parametrize("change", ["password", "session"])
def test_account_password_change_cannot_undo_concurrent_revocation(app, make_user, monkeypatch, change):
    from bananachat import security
    from bananachat.db import users

    user = make_user("brook")
    browser = Browser(app)
    browser.login("brook")
    verify = security.verify_password
    changed = False

    def verify_and_revoke(stored, password):
        nonlocal changed
        valid = verify(stored, password)
        if not changed:
            changed = True
            if change == "password":
                users.set_password(user["id"], security.hash_password("replacement-password"))
            else:
                # Revoking this individual session need not change the password.
                users.revoke_session(user["id"], security.current_session_hash())
        return valid

    monkeypatch.setattr(security, "verify_password", verify_and_revoke)
    response = browser.post("/account/password", {"current_password": "brook-password",
                           "new_password": "attacker-password", "confirm_password": "attacker-password"})
    assert response.status_code == 302
    assert browser.fetch("/api/preferences").status_code == 401
    with app.app_context():
        assert not verify(users.get(user["id"])["password"], "attacker-password")
        if change == "password":
            assert verify(users.get(user["id"])["password"], "replacement-password")


@pytest.mark.parametrize("action", ["account", "chats"])
def test_destructive_account_actions_cannot_use_credentials_revoked_during_verification(
        app, make_user, monkeypatch, action):
    from bananachat import security
    from bananachat.db import chats, users

    user = make_user("aspen")
    browser = Browser(app)
    browser.login("aspen")
    with app.app_context():
        chat_id = chats.create(user["id"])
    verify = security.verify_password
    changed = False

    def verify_and_reset(stored, password):
        nonlocal changed
        valid = verify(stored, password)
        if not changed:
            changed = True
            users.set_password(user["id"], security.hash_password("replacement-password"))
        return valid

    monkeypatch.setattr(security, "verify_password", verify_and_reset)
    path = "/account/delete" if action == "account" else "/account/chats/delete-all"
    response = browser.post(path, {"password": "aspen-password", "confirm_username": "aspen"})
    assert response.status_code == 302
    with app.app_context():
        assert users.get(user["id"]) is not None
        assert chats.get_owned(chat_id, user["id"]) is not None


@pytest.mark.parametrize("token", ["é", "☃", "\ud800"])
def test_non_ascii_csrf_input_is_rejected_without_a_server_error(app, token):
    browser = Browser(app)
    response = browser.post_json("/api/preferences", {}, headers={"X-CSRF-Token": token})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize("issued", ["123", "²", "9" * 5000])
def test_malformed_bot_token_is_rejected_without_a_server_error(app, issued):
    app.config["TESTING"] = False
    browser = Browser(app)
    signature = ("é" if issued == "123" else hmac.new(app.config["SECRET_KEY"].encode(),
                 b"form:" + issued.encode(), hashlib.sha256).hexdigest()[:24])
    response = browser.post("/login", {"username": "admin", "password": "admin-password",
                                       "_form_time": f"{issued}.{signature}", "csrf_token": TEST_CSRF})
    assert response.status_code == 400


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10**400])
def test_non_finite_stored_preferences_fall_back_to_valid_defaults(value):
    from bananachat.db import users

    cleaned = users.clean_preferences({"contrast": value, "sidebar_width": value, "font_scale": value})
    assert cleaned["contrast"] == 0
    assert cleaned["sidebar_width"] == 250
    assert cleaned["font_scale"] == 1.0


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN"])
def test_non_finite_preference_strings_do_not_break_the_account(app, make_user, value):
    make_user("cedar")
    browser = Browser(app)
    browser.login("cedar")
    response = browser.post_json("/api/preferences", {"contrast": value, "sidebar_width": value,
                                                     "font_scale": value})
    assert response.status_code == 200
    assert response.get_json()["preferences"]["contrast"] == 0
    assert response.get_json()["preferences"]["sidebar_width"] == 250
    assert response.get_json()["preferences"]["font_scale"] == 1.0
    assert browser.get("/account").status_code == 200
