"""Administrator sessions and account exports (full re-audit pass 12, RC1): a session that has not confirmed
recently cannot leave lasting user-level access behind (RC1-1); an export already streaming stops once the
session that started it ends or its account is suspended (RC1-2)."""
from __future__ import annotations

import pytest

from tests.app.fixtures import Browser
from tests.app.test_two_step import ADMIN_PASSWORD, _stale


def _one(app, sql, params=()):
    from bananachat import db
    with app.app_context():
        return db.one(sql, params)


# ----- RC1-1: lasting user-level access ----------------------------------------------------------------------

def _invite(admin, uses, expires, code, **extra):
    return admin.post("/admin/invites/create", {"max_uses": str(uses), "role": "user", "code": code,
                                                "expires": expires, **extra})


def test_rc1_1_open_ended_user_invitations_need_a_confirmation(app, admin):
    _stale(app)
    for uses, expires, code in ((0, "7d", "ZXCV-BNMA-SDFG-HJKA"), (1, "", "ZXCV-BNMA-SDFG-HJKB"),
                                (11, "7d", "ZXCV-BNMA-SDFG-HJKC")):
        assert _invite(admin, uses, expires, code).status_code == 302
        assert _one(app, "SELECT 1 FROM invite_codes WHERE code=?", (code,)) is None, (uses, expires)
    _invite(admin, 0, "", "ZXCV-BNMA-SDFG-HJKE", reauth_secret=ADMIN_PASSWORD)
    row = _one(app, "SELECT max_uses, expires_at FROM invite_codes WHERE code='ZXCV-BNMA-SDFG-HJKE'")
    assert row is not None and row["max_uses"] == 0 and row["expires_at"] is None


def test_rc1_1_a_one_use_one_day_invitation_needs_a_confirmation_too(app, admin):
    # Even the smallest invitation lets whoever holds the code make an account with a chosen password that
    # outlives the session, and bounded invitations made one after another add up to open sign-up.
    _stale(app)
    assert _invite(admin, 1, "1d", "QWER-TYUP-ASDF-GHJK").status_code == 302
    for letter in "ABCDE":
        _invite(admin, 10, "30d", f"ZZXC-VBNM-QWER-T{letter}YP")
    assert _one(app, "SELECT COUNT(*) AS n FROM invite_codes")["n"] == 0
    form = {"username": "mallory", "password": "mallory-password", "confirm_password": "mallory-password",
            "invite_code": "QWER-TYUP-ASDF-GHJK"}
    Browser(app).post("/signup", form)
    assert _one(app, "SELECT 1 FROM users WHERE username='mallory'") is None
    _invite(admin, 1, "1d", "QWER-TYUP-ASDF-GHJK", reauth_secret=ADMIN_PASSWORD)
    assert _one(app, "SELECT 1 FROM invite_codes WHERE code='QWER-TYUP-ASDF-GHJK'") is not None


def test_rc1_1_creating_a_user_account_needs_a_confirmation(app, admin):
    _stale(app)
    form = {"username": "mallory", "password": "mallory-password", "confirm_password": "mallory-password",
            "role": "user"}
    assert admin.post("/admin/users/create", form).status_code == 302
    assert _one(app, "SELECT 1 FROM users WHERE username='mallory'") is None
    admin.post("/admin/users/create", {**form, "reauth_secret": ADMIN_PASSWORD})
    assert _one(app, "SELECT 1 FROM users WHERE username='mallory'") is not None


# ----- RC1-2: exports after suspension or sign-out -----------------------------------------------------------

CHATS = 12


@pytest.fixture
def check_every_piece(monkeypatch):
    from bananachat.web import account
    monkeypatch.setattr(account, "EXPORT_CHECK_SECONDS", 0)


def _seed(app, make_user, name):
    from bananachat.db import chats, users
    make_user(name)
    with app.app_context():
        user_id = users.get_by_username(name)["id"]
        for number in range(CHATS):
            session_id = chats.create(user_id)
            chats.add_message(session_id, "user", f"MARK{number:02d}-" + "x" * 60_000, user_id=user_id,
                              incognito=False)
    return user_id


def _marks(data: bytes) -> int:
    return sum(f"MARK{number:02d}-".encode() in data for number in range(CHATS))


def _start(browser, path):
    response = browser.post(path, buffered=False)
    assert response.status_code == 200
    pieces = iter(response.response)
    return response, pieces, next(pieces)


def _rest(response, pieces):
    try:
        return b"".join(pieces)
    finally:
        response.close()


def test_rc1_2_suspension_stops_a_running_export(app, make_user, admin, check_every_piece):
    user_id = _seed(app, make_user, "bob")
    bob = Browser(app)
    bob.login("bob")
    response, pieces, first = _start(bob, "/account/export/chats")
    assert admin.post(f"/admin/users/{user_id}/suspend", {"until": ""}).status_code == 302
    assert _marks(first + _rest(response, pieces)) <= 2


def test_rc1_2_signing_out_everywhere_stops_a_stolen_sessions_export(app, make_user, check_every_piece):
    _seed(app, make_user, "carol")
    thief, owner = Browser(app), Browser(app)
    thief.login("carol")
    owner.login("carol")
    response, pieces, first = _start(thief, "/account/export/chats")
    assert owner.post("/account/sessions/revoke-others").status_code == 302
    assert _marks(first + _rest(response, pieces)) <= 2


def test_rc1_2_an_export_left_alone_is_complete(app, make_user, admin, check_every_piece):
    user_id = _seed(app, make_user, "dave")
    dave = Browser(app)
    dave.login("dave")
    response, pieces, first = _start(dave, "/account/export/chats")
    assert _marks(first + _rest(response, pieces)) == CHATS
    # An administrator's export of the account goes on while the administrator stays signed in.
    response, pieces, first = _start(admin, f"/admin/users/{user_id}/export/chats")
    assert _marks(first + _rest(response, pieces)) == CHATS
