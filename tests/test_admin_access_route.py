"""The maintenance-time administrator login path.

This route stays reachable while the main site is in maintenance or the
inference backend is down, so it is the one door left open when everything
else is closed. It has to reject bad credentials without revealing whether
the username exists, and without erroring out.
"""

import re

import pytest


def _comparable(response):
    """Blank out the values that are meant to differ between two requests.

    The CSP nonce is generated per request and the CSRF token per session, so
    both differ no matter what was submitted. Everything else in the page
    should be identical, or the page itself reveals whether the account exists.
    """
    body = re.sub(rb'nonce="[0-9a-f]+"', b'nonce=""', response.data)
    return re.sub(rb'(csrf[-_]token"[^>]*?(?:content|value)=")[^"]+', rb'\1', body)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Hand out clients that share a database but never a browser session.

    A flash message left over in one session would otherwise show up in the
    next response and make two pages differ for reasons unrelated to the
    credentials that were submitted.
    """
    import config
    import db
    from app import app
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "admin-access.db"))
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)
    db.init_db()
    return app.test_client


@pytest.fixture
def client(make_client):
    return make_client()


def _make_admin(username, password):
    import db
    from helpers._passwords import generate_password_hash
    db.complete_initial_setup(username, generate_password_hash(password))


def test_unknown_and_known_usernames_fail_identically(make_client):
    """A wrong password and a missing account must be indistinguishable."""
    _make_admin("site-admin", "Correct-horse-battery-1")
    missing = make_client().post(
        "/admin-access", data={"username": "no-such-account", "password": "whatever"}
    )
    wrong = make_client().post(
        "/admin-access", data={"username": "site-admin", "password": "whatever"}
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert b"no-such-account" not in missing.data
    assert _comparable(missing) == _comparable(wrong)


def test_correct_administrator_credentials_are_accepted(client):
    """The route has to actually let the administrator in."""
    _make_admin("site-admin", "Correct-horse-battery-1")
    response = client.post(
        "/admin-access",
        data={"username": "site-admin", "password": "Correct-horse-battery-1"},
    )
    assert response.status_code == 302
    with client.session_transaction() as stored:
        assert stored.get("user_id") is not None


def test_a_failed_attempt_is_recorded_against_the_client_address(client):
    """Otherwise this route is an unmetered channel for guessing the password."""
    import db
    _make_admin("site-admin", "Correct-horse-battery-1")
    for _ in range(11):
        response = client.post("/admin-access", data={"username": "site-admin", "password": "no"})
    assert response.status_code == 429
    assert db.check_login_rate_limit("127.0.0.1", max_attempts=10, window_seconds=60)
