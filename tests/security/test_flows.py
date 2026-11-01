"""Account and access flows end to end through HTTP, one test per flow, asserting what matters for security.

Sign-up (open, invitations, closed), sign-in and sign-out, sessions and signing out everywhere, password changes,
two-step verification, suspension, data export, deleting all chats, account deletion, share links and API key
scopes. BananaChat has no sign-up approval queue: sign-up is open, by invitation or closed.

Work that is still running when an account is suspended (chat and API answers, images, agent tasks) is covered by
``test_cauth.py`` (C1-4), the agent sandboxes and uploads of a deleted account by ``test_cauth.py`` (C1-5).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.app.fixtures import Browser
from tests.app.test_api import call, chat, roll_out
from tests.app.test_chat import new_chat, send, setup_models
from tests.security.test_cauth import FAST_HASH

PASSWORD = {"password": "long-password", "confirm_password": "long-password"}


@pytest.fixture
def app(make_app):
    return make_app(**FAST_HASH)


def _signed_in(app, username, password=None):
    browser = Browser(app)
    browser.login(username, password)
    return browser


def _signed_in_now(browser) -> bool:
    return browser.fetch("/account").status_code == 200


def _session_cookie(app, browser) -> str:
    return browser.client.get_cookie(app.session_interface.get_cookie_name(app)).value


def _with_cookie(app, value) -> Browser:
    browser = Browser(app)
    browser.client.set_cookie(app.session_interface.get_cookie_name(app), value)
    return browser


def _new_key(browser, scope="full", **fields):
    response = browser.post_json("/developer/tokens", {"name": "key", "scope": scope, **fields})
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()


def _models(app, key) -> int:
    return call(app, "GET", "/v1/models", key).status_code


def _scalar(app, sql, *params):
    from bananachat import db

    with app.app_context():
        return db.scalar(sql, params, 0)


# ----- sign-up -------------------------------------------------------------------------------------------

def test_signup_follows_the_mode_and_invitations_are_single_use_and_carry_their_role(app, admin):
    from bananachat.db import invites, settings, users

    def signup(username, **fields):
        return Browser(app).post("/signup", {"username": username, **PASSWORD, **fields})

    def exists(username):
        with app.app_context():
            return users.get_by_username(username)

    # Invitation only (the default): no code, a wrong code or a used-up code creates nothing; nor does a name
    # outside the username rules or a password that is too short.
    assert signup("nocode").status_code == 400 and not exists("nocode")
    assert signup("../admin").status_code == 400 and not exists("../admin")
    assert signup("weak", password="short", confirm_password="short").status_code == 400 and not exists("weak")
    assert signup("guess", invite_code="ABCD-EFGH").status_code == 400 and not exists("guess")
    assert admin.post("/admin/invites/create", {"max_uses": "1", "role": "user", "expires": ""}).status_code == 302
    with app.app_context():
        code = invites.list_all()[0][0]["code"]
    response = signup("invited", invite_code=code.lower())
    assert response.status_code == 302 and exists("invited")["role"] == "user"
    assert exists("invited")["invite_code"] == code
    assert signup("second", invite_code=code).status_code == 400 and not exists("second")
    # An administrator invitation makes an administrator, once; an expired one makes nobody.
    with app.app_context():
        owner = users.get_by_username("admin")["id"]
        staff = invites.create(owner, max_uses=1, assigned_role="admin",
                               expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        expired = invites.create(owner, max_uses=1, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert signup("staff", invite_code=staff).status_code == 302 and exists("staff")["role"] == "admin"
    assert signup("late", invite_code=expired).status_code == 400 and not exists("late")
    # Open: anyone gets an ordinary account, whatever invitation field is sent. Closed: nobody.
    with app.app_context():
        settings.update(signup_mode="open")
    assert signup("walkin").status_code == 302 and exists("walkin")["role"] == "user"
    with app.app_context():
        settings.update(signup_mode="disabled")
    assert signup("closed").status_code == 302 and not exists("closed")


# ----- sign-in and sign-out ------------------------------------------------------------------------------

def test_sign_in_needs_the_password_and_sign_out_ends_the_session_on_the_server(app, make_user):
    make_user("lena")
    browser = Browser(app)
    assert browser.post("/login", {"username": "lena", "password": "wrong"}).status_code == 401
    assert browser.post("/login", {"username": "nobody", "password": "wrong"}).status_code == 401
    assert not _signed_in_now(browser)
    browser.login("lena")
    assert _signed_in_now(browser)
    stolen = _session_cookie(app, browser)
    assert browser.post("/logout").status_code == 302
    assert not _signed_in_now(browser)
    # A copy of the cookie taken before signing out is worthless afterwards.
    assert not _signed_in_now(_with_cookie(app, stolen))


# ----- sessions and signing out everywhere ---------------------------------------------------------------

def test_signing_out_everywhere_ends_the_other_sessions_and_api_keys(app, admin, make_user):
    user = make_user("carl")
    laptop, phone, tablet = (_signed_in(app, "carl") for _ in range(3))
    key = _new_key(phone)["token"]
    assert _models(app, key) == 200
    # The account's own "sign out everywhere else", with the default "also revoke my API keys".
    assert laptop.post("/account/sessions/revoke-others", {"revoke_api_tokens": "1"}).status_code == 302
    assert _signed_in_now(laptop)
    assert not _signed_in_now(phone) and not _signed_in_now(tablet)
    assert _models(app, key) == 401
    # An administrator signing the account out everywhere ends the last session too, and its new key.
    key = _new_key(laptop)["token"]
    assert admin.post(f"/admin/users/{user['id']}/sessions/revoke-all", {"revoke_api_tokens": "1"}).status_code == 302
    assert not _signed_in_now(laptop)
    assert _models(app, key) == 401
    assert _scalar(app, "SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", user["id"]) == 0


# ----- passwords -----------------------------------------------------------------------------------------

def test_a_password_change_ends_the_other_sessions_and_an_admin_reset_ends_them_all(app, admin, make_user):
    user = make_user("bob")
    laptop, phone = _signed_in(app, "bob"), _signed_in(app, "bob")
    key = _new_key(laptop)["token"]
    response = laptop.post("/account/password", {"current_password": "wrong", "new_password": "new-secret-99",
                                                  "confirm_password": "new-secret-99", "revoke_api_tokens": "1"})
    assert response.status_code == 302 and _signed_in_now(phone) and _models(app, key) == 200
    response = laptop.post("/account/password", {"current_password": "bob-password", "new_password": "new-secret-99",
                                                  "confirm_password": "new-secret-99", "revoke_api_tokens": "1"})
    assert response.status_code == 302
    assert _signed_in_now(laptop) and not _signed_in_now(phone)
    assert _models(app, key) == 401
    assert Browser(app).post("/login", {"username": "bob", "password": "bob-password"}).status_code == 401
    phone = _signed_in(app, "bob", "new-secret-99")
    # An administrator's reset ends every session and always revokes the keys.
    key = _new_key(phone)["token"]
    response = admin.post(f"/admin/users/{user['id']}/password", {
        "password": "admin-chosen-1", "confirm_password": "admin-chosen-1", "reauth_secret": "admin-password"})
    assert response.status_code == 302
    assert not _signed_in_now(laptop) and not _signed_in_now(phone)
    assert _models(app, key) == 401
    assert Browser(app).post("/login", {"username": "bob", "password": "new-secret-99"}).status_code == 401
    _signed_in(app, "bob", "admin-chosen-1")


# ----- two-step verification -----------------------------------------------------------------------------

@pytest.fixture
def authenticator(monkeypatch):
    """The authenticator's clock: each ``next_code`` is the code of a later 30-second step."""
    from bananachat import totp

    class Clock:
        now = 1_800_000_000.0

        def time(self):
            return self.now

        def next_code(self, secret):
            self.now += totp.STEP_SECONDS
            return totp.totp_code(secret, self.now)

    clock = Clock()
    monkeypatch.setattr(totp, "time", clock)
    return clock


def test_two_step_on_ends_password_only_sessions_and_off_drops_the_code_again(app, make_user, authenticator):
    from bananachat.services import two_step
    from bananachat.web.account import ENROLMENT_KEY

    make_user("tess")
    browser, other = _signed_in(app, "tess"), _signed_in(app, "tess")
    key = _new_key(browser)["token"]
    assert browser.get("/account/two-step").status_code == 200
    with browser.client.session_transaction() as session, app.app_context():
        secret = two_step.decrypt_secret(session[ENROLMENT_KEY]["secret"])
    response = browser.post("/account/two-step", {"action": "enable", "password": "wrong",
                                                  "code": authenticator.next_code(secret)})
    assert response.status_code == 302 and _signed_in_now(other)
    response = browser.post("/account/two-step", {"action": "enable", "password": "tess-password",
                                                  "code": authenticator.next_code(secret), "revoke_api_tokens": "1"})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"  # the recovery codes
    assert _signed_in_now(browser) and not _signed_in_now(other)
    assert _models(app, key) == 401
    # The password alone no longer signs in: the session starts only with a valid code.
    other = Browser(app)
    response = other.post("/login", {"username": "tess", "password": "tess-password"})
    assert response.status_code == 302 and "/login/two-step" in response.headers["Location"]
    assert not _signed_in_now(other)
    assert other.post("/login/two-step", {"code": "000000"}).status_code == 401
    assert not _signed_in_now(other)
    assert other.post("/login/two-step", {"code": authenticator.next_code(secret)}).status_code == 302
    assert _signed_in_now(other)
    # Turning it off takes the password and a code, ends the other sessions, and sign-in needs no code again.
    response = browser.post("/account/two-step", {"action": "disable", "password": "tess-password", "code": "000000"})
    assert response.status_code == 302 and _scalar(app, "SELECT COUNT(*) FROM user_two_step") == 1
    response = browser.post("/account/two-step", {"action": "disable", "password": "tess-password",
                                                  "code": authenticator.next_code(secret)})
    assert response.status_code == 302 and _scalar(app, "SELECT COUNT(*) FROM user_two_step") == 0
    assert _signed_in_now(browser) and not _signed_in_now(other)
    response = Browser(app).post("/login", {"username": "tess", "password": "tess-password"})
    assert response.status_code == 302 and "/login/two-step" not in response.headers["Location"]


# ----- suspension ----------------------------------------------------------------------------------------

def test_suspension_cuts_the_session_api_key_sign_in_and_share_links_at_once(app, admin, make_user):
    user = make_user("mallory")
    setup_models(app)
    browser = _signed_in(app, "mallory")
    key = _new_key(browser)["token"]
    session_id = new_chat(browser)
    assert send(browser, session_id).status_code == 200
    link = browser.post_json(f"/chat/{session_id}/share").get_json()["url"]
    path = "/share/" + link.rsplit("/", 1)[1]
    assert Browser(app).get(path).status_code == 200
    assert admin.post(f"/admin/users/{user['id']}/suspend", {"until": ""}).status_code == 302
    assert not _signed_in_now(browser)
    response = call(app, "GET", "/v1/models", key)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "account_suspended"
    assert Browser(app).post("/login", {"username": "mallory", "password": "mallory-password"}).status_code == 403
    assert Browser(app).get(path).status_code == 404
    # Lifting the suspension restores the key and the link, not the ended session.
    assert admin.post(f"/admin/users/{user['id']}/unsuspend").status_code == 302
    assert _models(app, key) == 200 and Browser(app).get(path).status_code == 200
    assert not _signed_in_now(browser)


# ----- data export ---------------------------------------------------------------------------------------

def test_the_data_export_holds_the_accounts_own_data_and_no_secrets(app, make_user):
    from bananachat import db, totp
    from bananachat.db import tokens, users
    from bananachat.services import two_step

    setup_models(app)
    fay, bob = make_user("fay"), make_user("bob")
    browser, other = _signed_in(app, "fay"), _signed_in(app, "bob")
    assert send(browser, new_chat(browser), "fay writes about apricots").status_code == 200
    assert send(other, new_chat(other), "bob writes about blueberries").status_code == 200
    key = _new_key(browser)["token"]
    _new_key(other)
    secret = totp.generate_secret()
    with app.app_context():
        codes = two_step.enable(fay["id"], secret, 1)
        stored = db.one("SELECT secret_encrypted, recovery_hashes FROM user_two_step WHERE user_id=?", (fay["id"],))
        fay_row, bob_row = users.get(fay["id"]), users.get(bob["id"])
        digests = [row["token_hash"] for row in db.query("SELECT token_hash FROM api_tokens")]
        sessions = [row["id_hash"] for row in db.query("SELECT id_hash FROM auth_sessions")]
    for url in ("/account/export/gdpr", "/account/export/chats"):
        response = browser.post(url)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
        text = response.get_data(as_text=True)
        assert "apricots" in text
        assert "blueberries" not in text and bob["id"] not in text and "bob" not in text
        secrets = [fay_row["password"], bob_row["password"], key, secret, stored["secret_encrypted"],
                   *json.loads(stored["recovery_hashes"]), *codes, *digests, *sessions]
        assert not [value for value in secrets if value in text]
    data = json.loads(browser.post("/account/export/gdpr").get_data())
    assert data["account"]["username"] == "fay" and data["two_step"]["enabled"] is True
    assert [token["name"] for token in data["api_tokens"]] == ["key"]
    with app.app_context():
        assert tokens.authenticate(key)[0] is not None  # exporting changes nothing


# ----- deleting all chats --------------------------------------------------------------------------------

def test_deleting_all_chats_also_erases_no_history_chats_and_takes_links_offline(app, make_user):
    from bananachat import db
    from bananachat.db import chats

    setup_models(app)
    user = make_user("nico")
    make_user("olga")
    browser, other = _signed_in(app, "nico"), _signed_in(app, "olga")
    kept = new_chat(browser)
    assert send(browser, kept, "remember the plan").status_code == 200
    hidden = new_chat(browser, incognito=True)
    assert send(browser, hidden, "off the record").status_code == 200
    path = "/share/" + browser.post_json(f"/chat/{kept}/share").get_json()["url"].rsplit("/", 1)[1]
    theirs = new_chat(other)
    assert send(other, theirs).status_code == 200
    assert browser.post("/account/chats/delete-all", {"password": "wrong"}).status_code == 302
    assert browser.get(f"/chat/{kept}").status_code == 200
    assert browser.post("/account/chats/delete-all", {"password": "nico-password"}).status_code == 302
    assert browser.get(f"/chat/{kept}").status_code == 404 and browser.get(f"/chat/{hidden}").status_code == 404
    assert Browser(app).get(path).status_code == 404
    assert browser.fetch("/chat/sessions").get_json()["sessions"] == []
    # The no-history chat is gone at once; the others wait out the retention period, then go too.
    assert _scalar(app, "SELECT COUNT(*) FROM chat_sessions WHERE id=?", hidden) == 0
    assert _scalar(app, "SELECT COUNT(*) FROM chat_messages WHERE session_id=?", kept) == 2
    with app.app_context():
        chats.purge_deleted(db.now(timedelta(seconds=1)))
    assert _scalar(app, "SELECT COUNT(*) FROM chat_sessions WHERE user_id=?", user["id"]) == 0
    assert _scalar(app, "SELECT COUNT(*) FROM chat_messages WHERE session_id IN (?, ?)", kept, hidden) == 0
    assert other.get(f"/chat/{theirs}").status_code == 200  # nobody else's chats


# ----- account deletion ----------------------------------------------------------------------------------

def test_deleting_the_account_removes_its_chats_files_keys_sessions_and_background(app, make_user):
    from bananachat.db import users

    setup_models(app)
    user = make_user("hana")
    browser, phone = _signed_in(app, "hana"), _signed_in(app, "hana")
    session_id = new_chat(browser)
    assert send(browser, session_id, "Read", files=[("notes.txt", b"private notes")]).status_code == 200
    path = "/share/" + browser.post_json(f"/chat/{session_id}/share").get_json()["url"].rsplit("/", 1)[1]
    key = _new_key(browser)["token"]
    background = app.config["BC"].background_upload_dir / ("d" * 32 + ".jpg")
    background.parent.mkdir(parents=True, exist_ok=True)
    background.write_bytes(b"jpeg")
    with app.app_context():
        users.save_preferences(user["id"], {"background_image": background.name})
    assert _scalar(app, "SELECT COUNT(*) FROM chat_attachments") == 1
    response = browser.post("/account/delete", {"confirm_username": "hana", "password": "wrong"})
    assert response.status_code == 302 and _models(app, key) == 200
    response = browser.post("/account/delete", {"confirm_username": "hana", "password": "hana-password"})
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert not _signed_in_now(browser) and not _signed_in_now(phone)
    assert _models(app, key) == 401
    assert Browser(app).get(path).status_code == 404
    assert not background.exists()
    assert _scalar(app, "SELECT COUNT(*) FROM users WHERE id=?", user["id"]) == 0  # preferences live in the row
    for table in ("chat_sessions", "api_tokens", "auth_sessions"):
        assert _scalar(app, f"SELECT COUNT(*) FROM {table} WHERE user_id=?", user["id"]) == 0, table
    assert _scalar(app, "SELECT COUNT(*) FROM chat_messages WHERE session_id=?", session_id) == 0
    assert _scalar(app, "SELECT COUNT(*) FROM chat_attachments") == 0
    assert Browser(app).post("/login", {"username": "hana", "password": "hana-password"}).status_code == 401


# ----- share links ---------------------------------------------------------------------------------------

def test_a_share_link_shows_that_chat_only_to_anyone_until_it_is_revoked(app, make_user):
    setup_models(app)
    make_user("qzowner")  # a name no page text contains
    make_user("tom")
    owner, stranger = _signed_in(app, "qzowner"), _signed_in(app, "tom")
    shared = new_chat(owner)
    assert send(owner, shared, "the shared question", files=[("notes.txt", b"attached secret")]).status_code == 200
    private = new_chat(owner)
    assert send(owner, private, "the private question").status_code == 200
    hidden = new_chat(owner, incognito=True)
    assert send(owner, hidden, "the no-history question").status_code == 200
    assert owner.post_json(f"/chat/{hidden}/share").status_code == 400
    # Only the owner can share or revoke it.
    assert stranger.post_json(f"/chat/{shared}/share").status_code == 404
    url = owner.post_json(f"/chat/{shared}/share").get_json()["url"]
    path = "/share/" + url.rsplit("/", 1)[1]
    assert stranger.post_json(f"/chat/{shared}/share", {"action": "revoke"}).status_code == 404
    anonymous = Browser(app)
    page = anonymous.get(path)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store" and "noindex" in page.headers["X-Robots-Tag"]
    html = page.get_data(as_text=True)
    assert "the shared question" in html
    assert "the private question" not in html and "qzowner" not in html and "attached secret" not in html
    download = anonymous.get(path + "/download").get_data(as_text=True)
    assert "the shared question" in download and "the private question" not in download and "qzowner" not in download
    # The link opens nothing else: not the chat itself, its files or the owner's other chats.
    with app.app_context():
        from bananachat import db
        attachment = db.scalar("SELECT id FROM chat_attachments")
    for other in (f"/chat/{shared}", f"/chat/{shared}/attachments/{attachment}", f"/chat/{private}"):
        assert anonymous.get(other).status_code in (302, 401, 404), other
    assert stranger.get(f"/chat/{shared}/attachments/{attachment}").status_code == 404
    assert owner.post_json(f"/chat/{shared}/share", {"action": "revoke"}).status_code == 200
    assert anonymous.get(path).status_code == 404 and anonymous.get(path + "/download").status_code == 404
    # Sharing again makes a new link; the old one stays dead.
    assert owner.post_json(f"/chat/{shared}/share").get_json()["url"] != url
    assert anonymous.get(path).status_code == 404


# ----- API key scopes ------------------------------------------------------------------------------------

def test_api_keys_are_scoped_owned_and_end_when_revoked_or_expired(app, make_user):
    from bananachat import db

    roll_out(app)
    make_user("dev")
    make_user("eve")
    owner, other = _signed_in(app, "dev"), _signed_in(app, "eve")
    read = _new_key(owner, scope="read")
    full = _new_key(owner, expires_days=7)
    assert owner.post_json("/developer/tokens", {"name": "x", "scope": "admin"}).status_code == 400
    assert _models(app, read["token"]) == 200
    response = chat(app, read["token"])
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "insufficient_scope"
    assert chat(app, full["token"]).status_code == 200
    # Another account can neither revoke nor rotate someone else's key.
    assert other.post_json(f"/developer/tokens/{full['id']}/revoke").status_code == 404
    assert other.post_json(f"/developer/tokens/{full['id']}/rotate").status_code == 404
    assert _models(app, full["token"]) == 200
    # Rotating retires the old value; revoking and expiry end a key at once.
    rotated = owner.post_json(f"/developer/tokens/{full['id']}/rotate").get_json()
    assert _models(app, full["token"]) == 401 and chat(app, rotated["token"]).status_code == 200
    assert rotated["scope"] == "full"
    assert owner.post_json(f"/developer/tokens/{read['id']}/revoke").status_code == 200
    assert _models(app, read["token"]) == 401
    with app.app_context():
        db.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", (db.now(-timedelta(seconds=1)), rotated["id"]))
    assert _models(app, rotated["token"]) == 401
