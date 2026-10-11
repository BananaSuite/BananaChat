"""Sign-in throttling behind shared addresses, API token revocation, scopes and expiry,
``__Host-`` session cookies and invitation-code strength."""

from __future__ import annotations

import threading

import pytest

from tests.app.conftest import Browser


def _open_signup(app):
    from bananachat.db import settings

    with app.app_context():
        settings.update(signup_mode="open")


# ----- shared addresses (school NAT, CGNAT, VPN exits) ------------------------------

def test_failures_from_strangers_on_the_same_address_do_not_lock_out_a_known_browser(app, client, make_user):
    make_user("alice")
    client.login("alice")
    client.logout()
    stranger = Browser(app)  # the test client always uses the same address
    for number in range(20):
        assert stranger.post("/login", {"username": f"nobody{number}", "password": "nope"}).status_code == 401
    # Everyone else on the address is now throttled, the browser that signed in before is not.
    assert stranger.post("/login", {"username": "alice", "password": "alice-password"}).status_code == 429
    assert client.post("/login", {"username": "alice", "password": "alice-password"}).status_code == 302


def test_a_flood_of_requests_from_the_address_does_not_block_a_known_browser(make_app, make_user):
    app = make_app(BC_LOGIN_ATTEMPTS_PER_MINUTE=5)
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        alice = users.create("alice", security.hash_password("alice-password"))
    known = Browser(app)
    known.login("alice")  # without a device cookie yet: one of the address's five
    known.logout()
    stranger = Browser(app)
    statuses = [stranger.post("/login", {"username": "x", "password": "y"}).status_code for _ in range(5)]
    assert statuses == [401] * 4 + [429]
    assert known.post("/login", {"username": "alice", "password": "alice-password"}).status_code == 302
    assert alice


def test_successful_sign_ins_never_count_as_failures(make_app):
    app = make_app(BC_LOGIN_FAILURES_PER_ADDRESS=3)
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        for number in range(5):
            users.create(f"pupil{number}", security.hash_password(f"pupil{number}-password"))
    for number in range(5):
        Browser(app).login(f"pupil{number}")


def test_sign_in_limits_are_configurable(make_app):
    app = make_app(BC_LOGIN_FAILURES_PER_ADDRESS=2, BC_LOGIN_FAILURES_PER_ACCOUNT=50)
    browser = Browser(app)
    assert [browser.post("/login", {"username": f"u{n}", "password": "x"}).status_code for n in range(3)] == \
        [401, 401, 429]
    config = app.config["BC"]
    assert (config.login_failures_per_address, config.login_failures_per_account) == (2, 50)


def test_sign_ups_from_one_address_count_created_accounts_and_failures_apart(make_app):
    app = make_app(BC_SIGNUPS_PER_HOUR=3, BC_SIGNUP_FAILURES_PER_HOUR=2)
    _open_signup(app)
    browser = Browser(app)
    # Mistakes (here a taken name) do not use up the accounts a class may create...
    for _ in range(2):
        assert browser.post("/signup", {"username": "admin", "password": "long-password",
                                        "confirm_password": "long-password"}).status_code == 409
    # ...but they have a limit of their own.
    assert browser.post("/signup", {"username": "admin", "password": "long-password",
                                    "confirm_password": "long-password"}).status_code == 429
    app2 = make_app(BC_SIGNUPS_PER_HOUR=3)
    _open_signup(app2)
    statuses = []
    for number in range(4):
        response = Browser(app2).post("/signup", {"username": f"pupil{number}", "password": "long-password",
                                                  "confirm_password": "long-password"})
        statuses.append(response.status_code)
    assert statuses == [302, 302, 302, 429]


def test_default_sign_up_limit_admits_a_class(make_app):
    app = make_app()
    _open_signup(app)
    for number in range(12):
        response = Browser(app).post("/signup", {"username": f"pupil{number}", "password": "long-password",
                                                 "confirm_password": "long-password"})
        assert response.status_code == 302, number


def test_ipv6_clients_are_grouped_by_their_64_network(app):
    from bananachat import security

    with app.test_request_context(environ_base={"REMOTE_ADDR": "2001:db8:1:2:aaaa::1"}):
        first = security.client_key()
    with app.test_request_context(environ_base={"REMOTE_ADDR": "2001:db8:1:2:bbbb::9"}):
        second = security.client_key()
    assert first == second == "2001:db8:1:2::/64"


# ----- the account lockout cannot be raced ------------------------------------------

def test_parallel_guesses_cannot_exceed_the_account_limit(make_app, monkeypatch):
    # Fourteen sign-ins write at once. On a slow disk their commits queue for longer than the default 5 s lock
    # wait, which answers 503 (by design, not what this test is about), so the lock wait is the maximum here.
    monkeypatch.setenv("BC_DB_BUSY_TIMEOUT_MS", "30000")
    app = make_app(BC_LOGIN_FAILURES_PER_ADDRESS=1000, BC_LOGIN_ATTEMPTS_PER_MINUTE=1000)
    from bananachat import security

    # Hold every verification until all requests have passed the limit check.
    barrier = threading.Barrier(14, timeout=3)
    original = security.verify_password

    def slow_verify(stored, password):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return original(stored, password)

    monkeypatch.setattr(security, "verify_password", slow_verify)
    statuses = []

    def guess():
        statuses.append(Browser(app).post("/login", {"username": "admin", "password": "wrong"}).status_code)

    threads = [threading.Thread(target=guess) for _ in range(14)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(statuses) == [401] * 10 + [429] * 4


# ----- API tokens: revocation with the owner's sign-out, scopes and expiry ---------------

@pytest.fixture
def dev(app, make_user):
    from tests.app.test_api import roll_out

    roll_out(app)
    user = make_user("devon")
    browser = Browser(app)
    browser.login("devon")
    browser.user = user
    return browser


def _models(app, token):
    from tests.app.test_api import call

    return call(app, "GET", "/v1/models", token).status_code


def test_changing_the_password_revokes_api_tokens_by_default(app, dev):
    raw = dev.post_json("/developer/tokens", {"name": "planted"}).json["token"]
    response = dev.post("/account/password", {"current_password": "devon-password", "new_password": "fresh-pass-1",
                                              "confirm_password": "fresh-pass-1", "revoke_api_tokens": "1"})
    assert response.status_code == 302
    assert _models(app, raw) == 401


def test_the_owner_can_keep_api_tokens_when_changing_the_password(app, dev):
    raw = dev.post_json("/developer/tokens", {"name": "ci"}).json["token"]
    dev.post("/account/password", {"current_password": "devon-password", "new_password": "fresh-pass-1",
                                   "confirm_password": "fresh-pass-1"})
    assert _models(app, raw) == 200


def test_signing_out_other_devices_revokes_api_tokens_when_asked(app, dev):
    from bananachat.db import users

    raw = dev.post_json("/developer/tokens", {"name": "planted"}).json["token"]
    other = Browser(app)
    other.login("devon")
    page = dev.get("/account").get_data(as_text=True)
    assert 'name="revoke_api_tokens"' in page
    assert dev.post("/account/sessions/revoke-others", {"revoke_api_tokens": "1"}).status_code == 302
    assert _models(app, raw) == 401
    assert other.fetch("/account").status_code == 401
    with app.app_context():
        assert users.count_audit("account.api_tokens_revoke_all") == 1


def test_password_form_offers_revoking_api_tokens_only_when_there_are_some(app, dev):
    assert 'name="revoke_api_tokens"' not in dev.get("/account").get_data(as_text=True)
    dev.post_json("/developer/tokens", {"name": "x"})
    page = dev.get("/account").get_data(as_text=True)
    assert page.count('name="revoke_api_tokens"') == 1  # one other session is missing: no sign-out-others form
    assert 'name="revoke_api_tokens" value="1" checked' in page


def test_existing_tokens_keep_full_access_and_never_expire(app, dev):
    from bananachat import db
    from bananachat.db import tokens

    with app.app_context():
        token_id, raw = tokens.create(dev.user["id"], "old")
        row = db.one("SELECT scope, expires_at FROM api_tokens WHERE id=?", (token_id,))
    assert (row["scope"], row["expires_at"]) == ("full", None)
    assert _models(app, raw) == 200


def test_read_only_tokens_can_list_models_but_not_generate(app, dev):
    from tests.app.test_api import assert_error, chat

    response = dev.post_json("/developer/tokens", {"name": "monitor", "scope": "read"})
    assert response.status_code == 201 and response.json["scope"] == "read"
    raw = response.json["token"]
    assert _models(app, raw) == 200
    assert_error(chat(app, raw), 403, "permission_error", "insufficient_scope")
    page = dev.get("/developer").get_data(as_text=True)
    assert "Read only" in page or "Sola lettura" in page


def test_tokens_can_expire(app, dev):
    from datetime import timedelta

    from bananachat import db

    response = dev.post_json("/developer/tokens", {"name": "week", "expires_days": "7"})
    assert response.status_code == 201 and response.json["expires_at"]
    raw = response.json["token"]
    assert _models(app, raw) == 200
    with app.app_context():
        db.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", (db.now(-timedelta(seconds=1)),
                                                                     response.json["id"]))
    assert _models(app, raw) == 401
    # An expired token no longer counts toward the account's token limit or appears in the list.
    assert "week" not in dev.get("/developer").get_data(as_text=True)


def test_invalid_token_options_are_refused(app, dev):
    assert dev.post_json("/developer/tokens", {"scope": "admin"}).status_code == 400
    assert dev.post_json("/developer/tokens", {"expires_days": "3"}).status_code == 400


def test_rotation_keeps_scope_and_lifetime(app, dev):
    from bananachat import db

    first = dev.post_json("/developer/tokens", {"name": "r", "scope": "read", "expires_days": "30"}).json
    rotated = dev.post_json(f"/developer/tokens/{first['id']}/rotate").json
    with app.app_context():
        row = db.one("SELECT scope, created_at, expires_at FROM api_tokens WHERE id=?", (rotated["id"],))
        lifetime = db.parse_timestamp(row["expires_at"]) - db.parse_timestamp(row["created_at"])
    assert row["scope"] == "read" and round(lifetime.total_seconds() / 86400) == 30


# ----- __Host- session cookies over HTTPS --------------------------------------------

HTTPS = {"base_url": "https://chat.example.org"}


def _set_cookie_headers(response, name):
    return [value for value in response.headers.getlist("Set-Cookie") if value.startswith(name + "=")]


def test_secure_session_cookies_use_the_host_prefix(make_app):
    app = make_app(BC_SECURE_COOKIES=1)
    client = app.test_client()
    with client.session_transaction(**HTTPS) as session:
        session["csrf"] = "test-csrf-token"
    response = client.post("/login", data={"username": "admin", "password": "admin-password",
                                           "csrf_token": "test-csrf-token"}, **HTTPS)
    assert response.status_code == 302
    [session_cookie] = _set_cookie_headers(response, "__Host-bc_session")
    [device_cookie] = _set_cookie_headers(response, "__Host-bc_session_device")
    for cookie in (session_cookie, device_cookie):
        assert "Secure" in cookie and "Path=/" in cookie and "Domain" not in cookie
    assert not _set_cookie_headers(response, "bc_session")


def test_plain_cookie_names_stay_without_secure_cookies(app, client):
    response = client.post("/login", {"username": "admin", "password": "admin-password"})
    assert _set_cookie_headers(response, "bc_session") and not _set_cookie_headers(response, "__Host-bc_session")


def _legacy_cookie(app, data):
    from flask.sessions import SecureCookieSessionInterface

    return SecureCookieSessionInterface().get_signing_serializer(app).dumps(data)


def _pre_switch_session(app, username, user_agent):
    """A session signed in before the instance switched to prefixed cookies (*user_agent*: that browser)."""
    import time

    from bananachat.db import users

    with app.test_request_context(**HTTPS):
        token = users.create_session(users.get_by_username(username)["id"], 7, user_agent=user_agent)
    app.session_interface.legacy_before = time.time() + 60
    return _legacy_cookie(app, {"sid": token, "csrf": "c"})


def _browser_with(app, cookie, user_agent):
    client = app.test_client()
    client.set_cookie("bc_session", cookie, domain="chat.example.org")
    return lambda path="/account": client.get(path, headers={"User-Agent": user_agent}, **HTTPS)


def test_sessions_from_before_the_prefix_are_moved_once(make_app):
    app = make_app(BC_SECURE_COOKIES=1)
    from bananachat import db
    from bananachat.db import users

    cookie = _pre_switch_session(app, "admin", "Browser/1")
    with app.app_context():
        db.execute("UPDATE auth_sessions SET verified_at=?", (db.now(),))
    response = _browser_with(app, cookie, "Browser/1")()
    assert response.status_code == 200
    assert _set_cookie_headers(response, "__Host-bc_session")
    [removal] = _set_cookie_headers(response, "bc_session")
    assert "Max-Age=0" in removal or "expires=Thu, 01 Jan 1970" in removal
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions") == 1
        # A moved session confirms again before high-impact administrator actions.
        assert not users.session_verified_within(db.scalar("SELECT id_hash FROM auth_sessions"), 600)


def test_a_pre_switch_cookie_planted_in_another_browser_is_refused_and_ends(make_app):
    """chat-auth-2fa-2: a copy of someone's pre-switch cookie, planted from a sibling subdomain, signs nobody in."""
    app = make_app(BC_SECURE_COOKIES=1)
    from bananachat import db
    from bananachat.db import users

    with app.app_context():
        users.create("mallory", "x")
    cookie = _pre_switch_session(app, "mallory", "Mallory's browser/1")
    response = _browser_with(app, cookie, "Victim's browser/2")()
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions") == 0  # the copied session never works again
    assert _browser_with(app, cookie, "Mallory's browser/1")().status_code == 302


def test_a_pre_switch_cookie_moves_into_one_browser_only(make_app):
    """chat-auth-2fa-2: the move re-issues the session under a new token, so a saved copy is useless."""
    app = make_app(BC_SECURE_COOKIES=1)
    cookie = _pre_switch_session(app, "admin", "Browser/1")
    first = _browser_with(app, cookie, "Browser/1")
    assert first().status_code == 200
    assert _browser_with(app, cookie, "Browser/1")().status_code == 302
    assert first().status_code == 200  # the browser it moved into stays signed in


def test_pre_switch_cookies_are_ignored_a_day_after_the_switch(make_app, monkeypatch):
    """chat-auth-2fa-2: the move is a short transition, not the session lifetime (BC_SESSION_DAYS)."""
    import time
    from datetime import datetime, timezone

    from itsdangerous import TimestampSigner

    from bananachat import db, security

    app = make_app(BC_SECURE_COOKIES=1, BC_SESSION_DAYS=30)
    switch = time.time() - security.HOST_COOKIE_MIGRATION_SECONDS - 60
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: int(switch) - 60)
    cookie = _pre_switch_session(app, "admin", "Browser/1")
    monkeypatch.undo()
    with app.app_context():  # signed in, and signed, before the switch: only the day since then refuses it
        db.execute("UPDATE auth_sessions SET created_at=?",
                   (db.timestamp(datetime.fromtimestamp(switch - 120, timezone.utc)),))
    app.session_interface.legacy_before = switch
    response = _browser_with(app, cookie, "Browser/1")()
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert _set_cookie_headers(response, "bc_session")  # the old cookie is deleted all the same
    # The same session a minute before the day was over still moves.
    app.session_interface.legacy_before = time.time() - security.HOST_COOKIE_MIGRATION_SECONDS + 60
    assert _browser_with(app, cookie, "Browser/1")().status_code == 200


def test_saved_pre_switch_cookies_of_one_account_sign_in_one_look_alike_browser_only(make_app, monkeypatch):
    """chat-auth-2fa-2 (final verification): someone who signed in several times before the update with the
    most common User-Agent, and kept each cookie, cannot plant them into as many look-alike browsers. One
    session per account and User-Agent moves; the account's other sessions from that browser type end."""
    import time
    from datetime import datetime, timezone

    from itsdangerous import TimestampSigner

    app = make_app(BC_SECURE_COOKIES=1)
    from bananachat import db
    from bananachat.db import users

    chrome = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")
    switch = time.time() - 60
    with app.app_context():
        users.create("mallory", "x")
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: int(switch) - 60)
    saved = [_pre_switch_session(app, "mallory", chrome) for _ in range(3)]
    phone = _pre_switch_session(app, "mallory", "Phone/1")
    monkeypatch.undo()
    with app.app_context():
        db.execute("UPDATE auth_sessions SET created_at=?",
                   (db.timestamp(datetime.fromtimestamp(switch - 60, timezone.utc)),))
    app.session_interface.legacy_before = switch
    with app.test_request_context(**HTTPS):  # signed in after the switch: not a migration, left alone
        later = users.create_session(users.get_by_username("mallory")["id"], 7, user_agent=chrome)
    signed_in = [_browser_with(app, cookie, chrome)().status_code for cookie in saved]
    assert signed_in == [200, 302, 302]
    with app.app_context():
        user_agents = sorted(row["user_agent"] for row in db.query("SELECT user_agent FROM auth_sessions"))
        assert user_agents == sorted([chrome, chrome, "Phone/1"])  # the moved one, the later one, the phone
        assert users.load_session(later)[0] is not None
    assert _browser_with(app, phone, "Phone/1")().status_code == 200  # another browser of the account moves too


def test_a_plain_cookie_planted_after_the_switch_is_ignored(make_app):
    app = make_app(BC_SECURE_COOKIES=1)
    from bananachat.db import users

    with app.test_request_context(**HTTPS):
        attacker = users.create("mallory", "x")
        token = users.create_session(attacker, 7)
    # A sibling subdomain sets its own (validly signed) session for the parent domain.
    client = app.test_client()
    client.set_cookie("bc_session", _legacy_cookie(app, {"sid": token, "csrf": "c"}), domain="chat.example.org")
    response = client.get("/account", **HTTPS)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_a_cookie_name_with_a_prefix_is_normalised(tmp_path):
    from bananachat.config import load_config

    config = load_config({"BC_INSTANCE_DIR": str(tmp_path), "BC_SECRET_KEY": "x" * 40,
                          "BC_SESSION_COOKIE_NAME": "__Host-chat"})
    assert config.session_cookie_name == "chat" and config.warnings


# ----- invitation codes ------------------------------------------------------------

def test_short_or_repetitive_custom_invitation_codes_are_refused(app, make_user):
    from bananachat.db import invites, users

    with app.app_context():
        admin = users.get_by_username("admin")
        for weak in ("STAFF", "team-2026", "aaaa-aaaa-aaaa", "ab12ab12ab12ab12"):
            with pytest.raises(ValueError):
                invites.create(admin["id"], code=weak)
        assert invites.create(admin["id"], code="spring-class-2026") == "SPRING-CLASS-2026"


def test_administrator_invitations_must_expire_and_have_limited_uses(app):
    from datetime import datetime, timedelta, timezone

    from bananachat.db import invites, users

    soon = datetime.now(timezone.utc) + timedelta(days=7)
    with app.app_context():
        admin = users.get_by_username("admin")
        with pytest.raises(ValueError):
            invites.create(admin["id"], assigned_role="admin")  # never expires
        with pytest.raises(ValueError):
            invites.create(admin["id"], assigned_role="admin", expires_at=soon, max_uses=0)  # unlimited
        with pytest.raises(ValueError):
            invites.create(admin["id"], assigned_role="admin", expires_at=soon + timedelta(days=60))
        code = invites.create(admin["id"], assigned_role="admin", expires_at=soon)
        assert invites.find_usable(code)["max_uses"] == 1


def test_the_invitation_form_explains_the_administrator_rules(app, admin):
    from bananachat import db

    response = admin.post("/admin/invites/create", {"max_uses": "0", "expires": "", "role": "admin"},
                          follow_redirects=True)
    assert "Administrator invitations" in response.get_data(as_text=True)
    response = admin.post("/admin/invites/create", {"max_uses": "1", "expires": "7d", "role": "user",
                                                    "code": "STAFF"}, follow_redirects=True)
    assert "at least 12" in response.get_data(as_text=True)
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 0
    admin.post("/admin/invites/create", {"max_uses": "1", "expires": "7d", "role": "admin"})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM invite_codes WHERE assigned_role='admin'") == 1


def test_invitations_from_before_the_stricter_rules_keep_working_but_are_flagged(app, admin):
    from bananachat import db

    with app.app_context():
        creator = db.scalar("SELECT id FROM users WHERE username='admin'")
        for code, uses, role in (("STAFF", 0, "admin"), ("TEAM-2026", 5, "user"), ("ABCD-EFGH", 1, "user")):
            db.execute("INSERT INTO invite_codes (code, created_by, created_at, max_uses, assigned_role) "
                       "VALUES (?,?,?,?,?)", (code, creator, db.now(), uses, role))
        assert db.scalar("SELECT COUNT(*) FROM invite_codes") == 3
    page = admin.get("/admin/invites").get_data(as_text=True)
    assert "/signup?invite=STAFF" in page and "/signup?invite=TEAM-2026" in page  # still usable
    assert page.count('class="badge badge-danger invite-weak"') == 2  # STAFF and TEAM-2026, not the random-looking one
    assert "never expires and has unlimited uses" in page and "easy to guess" in page


# ----- the compute management credential ------------------------------------------------

def test_model_downloads_and_deletions_use_the_management_token(make_app, fake_ollama):
    app = make_app(OLLAMA_API_KEY="p" * 64, OLLAMA_MANAGEMENT_API_KEY="m" * 64)
    from bananachat.services import ollama

    with app.app_context():
        list(ollama.pull("llama3.2:3b"))
        ollama.delete("llama3.2:3b")
        ollama.list_tags()
    seen = dict(fake_ollama.authorizations)
    assert seen["/api/pull"] == seen["/api/delete"] == "Bearer " + "m" * 64
    assert seen["/api/tags"] == "Bearer " + "p" * 64


def test_without_a_management_token_the_compute_token_is_used(make_app, fake_ollama):
    app = make_app(OLLAMA_API_KEY="p" * 64)
    from bananachat.services import ollama

    with app.app_context():
        ollama.delete("llama3.2:3b")
    assert dict(fake_ollama.authorizations)["/api/delete"] == "Bearer " + "p" * 64
