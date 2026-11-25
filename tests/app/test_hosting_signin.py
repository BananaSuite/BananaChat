"""Sign in with BananaWiki Hosting (web/hosting.py, services/hosting_signin.py).

The portal is imitated by :class:`FakePortal`, which replaces the one function that talks to it
(``hosting_signin._request``) and checks what a real portal checks: the client's HTTP Basic
credentials, the redirect URI, the PKCE verifier and single-use codes and tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.app.fixtures import Browser

PORTAL = "https://hosting.example.org"
CLIENT_ID = "bwa_" + "a1" * 14
CLIENT_SECRET = "portal-client-secret-0123456789"  # noqa: S105 - a test value
HOSTING_ENV = {"BC_HOSTING_OAUTH_URL": PORTAL + "/", "BC_HOSTING_OAUTH_CLIENT_ID": CLIENT_ID,
               "BC_HOSTING_OAUTH_CLIENT_SECRET": CLIENT_SECRET}
CALLBACK = "http://localhost/login/hosting/callback"


class FakePortal:
    """The portal's authorize, token and userinfo endpoints."""

    def __init__(self):
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_answer = None  # (status, body) to answer instead
        self.userinfo_answer = None
        self.failure = None  # an OAuthError raised for every call (portal unreachable)
        self.signup = "open"  # what the OAuth metadata says about sign-up (None: no metadata)

    def authorize(self, location: str, sub: str, username: str = "", *, deny: bool = False) -> str:
        """The browser at the portal: the account *sub* consents; returns the callback URL."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == PORTAL + "/oauth/authorize"
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == CALLBACK and query["scope"] == "profile"
        assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
        if deny:
            return f"/login/hosting/callback?error=access_denied&state={query['state']}"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"sub": sub, "username": username, "challenge": query["code_challenge"],
                            "redirect_uri": query["redirect_uri"]}
        return f"/login/hosting/callback?code={code}&state={query['state']}"

    def request(self, method, path, *, headers, body=None, max_bytes=None, timeout=None):
        self.calls.append((method, path))
        if self.failure is not None:
            raise self.failure
        if path == "/.well-known/oauth-authorization-server":
            assert method == "GET" and body is None
            return (200, {"issuer": PORTAL, "signup": self.signup}) if self.signup else (404, None)
        if path == "/oauth/token":
            assert method == "POST" and headers["Content-Type"] == "application/x-www-form-urlencoded"
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if headers.get("Authorization") != expected:
                return 401, {"error": "invalid_client"}
            if self.token_answer is not None:
                return self.token_answer
            form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            grant = self.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if (grant is None or form.get("grant_type") != "authorization_code"
                    or form.get("redirect_uri") != grant["redirect_uri"] or challenge != grant["challenge"]):
                return 400, {"error": "invalid_grant"}
            token = secrets.token_urlsafe(24)
            self.tokens[token] = grant
            return 200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600, "scope": "profile"}
        if path == "/oauth/userinfo":
            assert method == "GET" and body is None
            if self.userinfo_answer is not None:
                return self.userinfo_answer
            grant = self.tokens.get((headers.get("Authorization") or "").removeprefix("Bearer "))
            if grant is None:
                return 401, {"error": "invalid_token"}
            return 200, {"sub": grant["sub"], "preferred_username": grant["username"], "username": grant["username"],
                         "name": grant["username"], "created_at": "2026-01-02 03:04:05"}
        return 404, None


@pytest.fixture
def portal(monkeypatch):
    from bananachat.services import hosting_signin

    fake = FakePortal()
    monkeypatch.setattr(hosting_signin, "_request", fake.request)
    return fake


@pytest.fixture
def happ(make_app):
    """An application configured for the portal, in mode ``both`` and with open sign-up."""
    app = make_app(**HOSTING_ENV)
    _settings(app, signin_mode="both", signup_mode="open")
    return app


def _settings(app, **values):
    from bananachat.db import settings as site_settings

    with app.app_context():
        site_settings.update(**values)


def _user(app, username):
    from bananachat.db import users

    with app.app_context():
        return users.get_by_username(username)


def _make_user(app, username, role="user"):
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        return users.get(users.create(username, security.hash_password(f"{username}-password"), role=role))


def _link(app, username, sub, portal_username=None):
    from bananachat.db import hosting_links

    with app.app_context():
        hosting_links.create(_user(app, username)["id"], sub, portal_username or username)


def _link_of(app, username):
    from bananachat.db import hosting_links

    with app.app_context():
        return hosting_links.link_of(_user(app, username)["id"])


def _audit(app, prefix=""):
    from bananachat import db

    with app.app_context():
        return [row.to_dict() for row in db.query("SELECT actor_name, action, target, details FROM audit_log "
                                                  "WHERE action LIKE ? ORDER BY id", (prefix + "%",))]


def _english(browser):
    browser.get("/language/en")
    return browser


def _whoami(browser):
    response = browser.get("/account")
    if response.status_code != 200:
        return None
    return re.search(r"<dd><strong>([^<]+)</strong></dd>", response.get_data(as_text=True)).group(1)


def _start(browser, path="/login/hosting/start"):
    response = browser.get(path)
    assert response.status_code == 302, response.status_code
    return response.headers["Location"]


def _hosting_sign_in(browser, portal, sub, username="", **kwargs):
    """Start, consent at the portal and come back: the callback's response."""
    return browser.get(portal.authorize(_start(browser), sub, username, **kwargs))


def _flashes(browser, url="/login"):
    return browser.get(url).get_data(as_text=True)


# ----- configuration ------------------------------------------------------------------

def test_configuration_needs_all_three_settings_and_a_secure_portal():
    from bananachat.config import load_config

    config = load_config(HOSTING_ENV, load_secret=False)
    assert config.hosting_oauth_configured and config.hosting_oauth_url == PORTAL
    assert config.hosting_oauth_client_id == CLIENT_ID and config.hosting_oauth_client_secret == CLIENT_SECRET
    assert CLIENT_SECRET not in repr(config) and not config.warnings
    assert not load_config({}, load_secret=False).hosting_oauth_configured
    assert not load_config({}, load_secret=False).warnings

    def off(**changes):
        config = load_config({**HOSTING_ENV, **changes}, load_secret=False)
        assert not config.hosting_oauth_configured
        assert not (config.hosting_oauth_url or config.hosting_oauth_client_id or config.hosting_oauth_client_secret)
        assert config.warnings and not any(CLIENT_SECRET in warning for warning in config.warnings)
        return config.warnings

    assert "needs" in off(BC_HOSTING_OAUTH_CLIENT_SECRET="")[0]
    assert "needs" in off(BC_HOSTING_OAUTH_URL="")[0]
    assert "https" in off(BC_HOSTING_OAUTH_URL="http://hosting.example.org")[0]
    off(BC_HOSTING_OAUTH_URL="https://hosting.example.org/?next=1")
    off(BC_HOSTING_OAUTH_URL="https://user:pass@hosting.example.org")
    off(BC_HOSTING_OAUTH_URL="ftp://hosting.example.org")
    off(BC_HOSTING_OAUTH_CLIENT_ID="bwa abc")
    off(BC_HOSTING_OAUTH_CLIENT_SECRET="has space")
    # Plain HTTP only to this machine (a development portal).
    for local in ("http://127.0.0.1:5000", "http://localhost:5000/portal"):
        assert load_config({**HOSTING_ENV, "BC_HOSTING_OAUTH_URL": local}, load_secret=False).hosting_oauth_configured


def test_without_configuration_the_saved_mode_does_not_apply(make_app):
    app = make_app()
    _settings(app, signin_mode="hosting")
    browser = Browser(app)
    page = browser.get("/login").get_data(as_text=True)
    assert 'name="password"' in page and "/login/hosting/start" not in page
    assert browser.get("/login/hosting/start").status_code == 404
    assert browser.get("/login/hosting/callback?state=x&code=y").status_code == 404
    assert browser.get("/login/recovery").status_code == 404
    browser.login("admin", "admin-password")
    settings = browser.get("/admin/settings").get_data(as_text=True)
    assert "BC_HOSTING_OAUTH_CLIENT_SECRET" in settings and 'id="signin-mode" disabled' in settings


def test_a_new_database_starts_in_local_mode_with_an_empty_link_table(make_app):
    from bananachat import db
    from bananachat.db import settings as site_settings

    app = make_app(**HOSTING_ENV)
    with app.app_context():
        assert site_settings.get()["signin_mode"] == "local"
        assert db.scalar("SELECT COUNT(*) FROM hosting_links") == 0
        columns = {row["name"]: row for row in db.query("PRAGMA table_info(hosting_links)")}
        assert set(columns) == {"user_id", "account_id", "portal_username", "linked_at"}


# ----- the modes ------------------------------------------------------------------------

def test_local_mode_keeps_the_password_page_and_hides_the_hosting_routes(make_app, portal):
    app = make_app(**HOSTING_ENV)
    browser = Browser(app)
    page = browser.get("/login").get_data(as_text=True)
    assert 'name="password"' in page and "/login/hosting/start" not in page
    for url in ("/login/hosting/start", "/login/hosting/connect", "/login/recovery",
                "/login/hosting/callback?state=a&code=b"):
        assert browser.get(url).status_code == 404, url
    assert browser.post("/login/hosting/create", {"username": "x"}).status_code == 404
    browser.login("admin", "admin-password")
    assert 'action="/account/hosting/link"' not in browser.get("/account").get_data(as_text=True)
    assert browser.post("/account/hosting/link", {"password": "admin-password"}).status_code == 404
    assert portal.calls == []


def test_both_mode_offers_the_password_form_and_the_button(happ):
    browser = Browser(happ)
    page = browser.get("/login?next=/account").get_data(as_text=True)
    assert 'name="password"' in page and 'href="/login/hosting/start?next=/account"' in page
    browser.login("admin", "admin-password")
    assert 'action="/account/hosting/link"' in browser.get("/account").get_data(as_text=True)


def test_hosting_mode_refuses_passwords_and_sends_sign_up_to_the_portal(happ, portal):
    _settings(happ, signin_mode="hosting")
    _make_user(happ, "bob")
    browser = _english(Browser(happ))
    page = browser.get("/login").get_data(as_text=True)
    assert 'name="password"' not in page and 'href="/login/hosting/start"' in page
    assert "create=1" not in page and "No account yet?" not in page
    response = browser.post("/login", {"username": "bob", "password": "bob-password"})
    assert response.status_code == 403 and "Signing in with a password is turned off" in response.get_data(as_text=True)
    assert _whoami(browser) is None
    signup = browser.get("/signup?invite=ABCD")
    assert signup.status_code == 302 and signup.headers["Location"] == "/login/hosting/start?invite=ABCD"
    assert browser.post("/signup", {"username": "eve", "password": "eve-password-1",
                                    "confirm_password": "eve-password-1"}).status_code == 302
    assert _user(happ, "eve") is None
    _settings(happ, signup_mode="disabled")
    assert browser.get("/signup").headers["Location"] == "/login"


# ----- state and PKCE -------------------------------------------------------------------

def test_the_flow_checks_the_state_and_uses_it_once(happ, portal):
    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    browser = _english(Browser(happ))
    location = _start(browser)
    callback = portal.authorize(location, "sub-bob", "bob")
    forged = re.sub(r"state=[^&]+", "state=forged", callback)
    response = browser.get(forged)
    assert response.status_code == 302 and response.headers["Location"] == "/login"
    assert "took too long or was already used" in _flashes(browser)
    assert _whoami(browser) is None
    # The real state still works once (the code was not used).
    assert browser.get(callback).status_code == 302
    assert _whoami(browser) == "bob"
    # Replaying the callback in another browser, or after signing out, finds no state.
    other = Browser(happ)
    assert other.get(callback).headers["Location"] == "/login" and _whoami(other) is None
    browser.logout()
    assert browser.get(callback).headers["Location"] == "/login" and _whoami(browser) is None
    # Only the first exchange reached the portal's token endpoint.
    assert portal.calls.count(("POST", "/oauth/token")) == 1


def test_a_state_expires_and_only_a_few_are_kept(happ, portal):
    from bananachat.services import hosting_signin

    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    browser = Browser(happ)
    callback = portal.authorize(_start(browser), "sub-bob", "bob")
    with browser.client.session_transaction() as session:
        session[hosting_signin.STATE_KEY] = {
            state: {**entry, "at": entry["at"] - hosting_signin.STATE_TTL_SECONDS - 5}
            for state, entry in session[hosting_signin.STATE_KEY].items()}
    assert browser.get(callback).headers["Location"] == "/login" and _whoami(browser) is None
    callbacks = [portal.authorize(_start(browser), "sub-bob", "bob") for _ in range(hosting_signin.MAX_STATES + 1)]
    assert browser.get(callbacks[0]).headers["Location"] == "/login"  # the oldest was dropped
    assert browser.get(callbacks[-1]).status_code == 302 and _whoami(browser) == "bob"


def test_the_verifier_matches_the_challenge_and_a_denial_signs_nobody_in(happ, portal):
    from bananachat.services import hosting_signin

    browser = _english(Browser(happ))
    response = _hosting_sign_in(browser, portal, "sub-x", "x", deny=True)
    assert response.headers["Location"] == "/login" and "was cancelled" in _flashes(browser)
    assert portal.calls == []
    with happ.test_request_context():
        verifier = hosting_signin._verifier("some-state")
        assert len(verifier) == 43 and re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
        assert verifier != hosting_signin._verifier("other-state")


# ----- portal failures -----------------------------------------------------------------

@pytest.mark.parametrize("token,userinfo,message", [
    ((400, {"error": "invalid_grant"}), None, "could not complete the sign-in"),
    ((401, {"error": "invalid_client"}), None, "could not complete the sign-in"),
    ((200, {"token_type": "Bearer"}), None, "could not complete the sign-in"),
    ((200, {"access_token": "tok-never-logged", "token_type": "mac"}), None, "could not complete the sign-in"),
    (None, (401, {"error": "invalid_token"}), "could not complete the sign-in"),
    (None, (200, {"preferred_username": "nosub"}), "could not complete the sign-in"),
    (None, (200, {"sub": "x" * 65, "username": "long"}), "could not complete the sign-in"),
    (None, (403, {"error": "account_suspended"}), "sign in right now"),
])
def test_token_and_profile_failures_sign_nobody_in(happ, portal, warnings_log, token, userinfo, message):
    portal.token_answer, portal.userinfo_answer = token, userinfo
    browser = _english(Browser(happ))
    response = _hosting_sign_in(browser, portal, "sub-x", "x")
    assert response.headers["Location"] == "/login" and message in _flashes(browser)
    assert _whoami(browser) is None
    with happ.app_context():
        from bananachat import db
        assert db.scalar("SELECT COUNT(*) FROM hosting_links") == 0
    assert "Sign-in with BananaWiki Hosting failed" in warnings_log.text
    assert CLIENT_SECRET not in warnings_log.text and "tok-never-logged" not in warnings_log.text


def test_an_unreachable_portal_fails_the_sign_in(happ, portal, warnings_log):
    from bananachat.services.hosting_signin import OAuthError

    portal.failure = OAuthError("hosting.error_portal", "/oauth/token: connect")
    browser = _english(Browser(happ))
    assert _hosting_sign_in(browser, portal, "sub-x", "x").headers["Location"] == "/login"
    assert "could not complete the sign-in" in _flashes(browser)
    assert "/oauth/token: connect" in warnings_log.text and CLIENT_SECRET not in warnings_log.text


def test_the_http_helper_reads_only_the_portal_with_bounded_answers(happ, monkeypatch):
    """The real ``_request``: the configured origin and paths, the status and the JSON of an error answer."""
    from bananachat.services import hosting_signin, upstream

    seen = []

    class Answer:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, size):
            return self.body[:size]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_open(method, base_url, path, **kwargs):
        seen.append((method, base_url, path, kwargs["max_bytes"], kwargs["headers"]))
        if path == "/oauth/token":
            raise upstream.UpstreamError("invalid_grant", 400)
        return Answer(b"x" * (hosting_signin.MAX_RESPONSE_BYTES + 10) if path == "/big" else b'{"sub": "s"}')

    monkeypatch.setattr(upstream, "open_request", fake_open)
    with happ.test_request_context():
        assert hosting_signin._request("GET", "/oauth/userinfo", headers={"A": "b"}) == (200, {"sub": "s"})
        assert hosting_signin._request("POST", "/oauth/token", headers={}, body=b"x") == (400, {"error": "invalid_grant"})
        with pytest.raises(hosting_signin.OAuthError):
            hosting_signin._request("GET", "/big", headers={})
    assert {base for _, base, *_ in seen} == {PORTAL}
    assert {limit for *_, limit, _ in seen} == {hosting_signin.MAX_RESPONSE_BYTES}


# ----- linked accounts -----------------------------------------------------------------

def test_a_linked_account_signs_in_and_is_audited(happ, portal):
    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob", "bobby")
    browser = Browser(happ)
    response = browser.get(portal.authorize(_start(browser, "/login/hosting/start?next=/account"), "sub-bob", "bobby"))
    assert response.headers["Location"] == "/account"
    assert _whoami(browser) == "bob"
    assert [entry["action"] for entry in _audit(happ, "auth.hosting")] == ["auth.hosting_login"]
    # No access token is kept anywhere: not in the cookie session, not in the database.
    with browser.client.session_transaction() as session:
        assert not any(token in json.dumps(dict(session)) for token in portal.tokens)


def test_an_administrator_sign_in_is_audited_as_one(happ, portal):
    _link(happ, "admin", "sub-admin")
    browser = Browser(happ)
    _hosting_sign_in(browser, portal, "sub-admin", "admin")
    assert _whoami(browser) == "admin"
    entries = _audit(happ, "auth.admin_login")
    assert json.loads(entries[-1]["details"]) == {"method": "hosting"}


def test_two_step_codes_are_asked_after_a_password_sign_in_only(happ, portal):
    """BananaWiki Hosting asks for its own second step: a hosting sign-in never asks for this site's code."""
    from bananachat import totp
    from bananachat.services import two_step

    bob = _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    secret = totp.generate_secret()
    with happ.app_context():
        two_step.enable(bob["id"], secret, 0)
    browser = _english(Browser(happ))
    response = _hosting_sign_in(browser, portal, "sub-bob", "bob")
    assert response.headers["Location"] == "/"
    assert _whoami(browser) == "bob"
    browser.logout()
    # The password still asks for the code.
    response = browser.post("/login", {"username": "bob", "password": "bob-password"})
    assert response.headers["Location"] == "/login/two-step" and _whoami(browser) is None
    assert browser.post("/login/two-step", {"code": totp.totp_code(secret)}).status_code == 302
    assert _whoami(browser) == "bob"


def test_a_suspended_linked_account_is_refused(happ, portal):
    from bananachat.db import users

    bob = _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    with happ.app_context():
        users.suspend(bob["id"])
    browser = _english(Browser(happ))
    assert _hosting_sign_in(browser, portal, "sub-bob", "bob").headers["Location"] == "/login"
    assert "suspended" in _flashes(browser) and _whoami(browser) is None


def test_the_portal_name_never_picks_an_account(happ, portal):
    """A portal account named like a local one gets the connect page, not that account."""
    _make_user(happ, "bob")
    browser = _english(Browser(happ))
    response = _hosting_sign_in(browser, portal, "sub-mallory", "bob")
    assert response.headers["Location"] == "/login/hosting/connect"
    assert _whoami(browser) is None and _link_of(happ, "bob") is None
    page = browser.get("/login/hosting/connect").get_data(as_text=True)
    assert 'id="existing_username" name="username" required maxlength="64" autocomplete="username" ' \
           'autocapitalize="none" spellcheck="false" value=""' in page
    assert 'value="bob2"' in page and "The name bob can&#39;t be used here" in page


# ----- the first sign-in: a new account ----------------------------------------------------

def _connect(happ, portal, sub="sub-new", name="newbie"):
    browser = _english(Browser(happ))
    assert _hosting_sign_in(browser, portal, sub, name).headers["Location"] == "/login/hosting/connect"
    return browser


def test_creating_an_account_links_it_and_signs_in(happ, portal):
    from bananachat import security

    browser = _connect(happ, portal)
    page = browser.get("/login/hosting/connect").get_data(as_text=True)
    assert "Signed in to BananaWiki Hosting as newbie" in page and 'value="newbie"' in page
    response = browser.post("/login/hosting/create", {"username": "newbie"})
    assert response.status_code == 302 and _whoami(browser) == "newbie"
    link = _link_of(happ, "newbie")
    assert link["account_id"] == "sub-new" and link["portal_username"] == "newbie"
    with happ.test_request_context():
        assert not security.verify_password(_user(happ, "newbie")["password"], "")
    assert [entry["action"] for entry in _audit(happ, "auth.hosting")] == ["auth.hosting_link", "auth.hosting_login"]
    # The next sign-in finds the account by its link.
    browser.logout()
    _hosting_sign_in(browser, portal, "sub-new", "renamed-on-the-portal")
    assert _whoami(browser) == "newbie"


def test_a_taken_name_gets_a_suggestion(happ, portal):
    _make_user(happ, "newbie")
    browser = _connect(happ, portal)
    response = browser.post("/login/hosting/create", {"username": "newbie"})
    assert response.status_code == 409
    page = response.get_data(as_text=True)
    assert "Username is already taken. Try newbie2." in page
    assert browser.post("/login/hosting/create", {"username": "no spaces"}).status_code == 400
    assert browser.post("/login/hosting/create", {"username": "newbie2"}).status_code == 302
    assert _whoami(browser) == "newbie2"


def test_creating_follows_the_sign_up_policy(happ, portal):
    from bananachat.db import invites

    _settings(happ, signup_mode="disabled")
    browser = _connect(happ, portal)
    page = browser.get("/login/hosting/connect").get_data(as_text=True)
    assert "New accounts can&#39;t be created on this site" in page and 'action="/login/hosting/create"' not in page
    assert browser.post("/login/hosting/create", {"username": "newbie"}).status_code == 403
    assert _user(happ, "newbie") is None

    _settings(happ, signup_mode="invite")
    assert browser.post("/login/hosting/create", {"username": "newbie"}).status_code == 400
    assert browser.post("/login/hosting/create", {"username": "newbie", "invite_code": "WRONG"}).status_code == 400
    assert _user(happ, "newbie") is None
    with happ.app_context():
        code = invites.create(_user(happ, "admin")["id"], max_uses=1)
    assert browser.post("/login/hosting/create", {"username": "newbie", "invite_code": code}).status_code == 302
    assert _whoami(browser) == "newbie" and _user(happ, "newbie")["invite_code"] == code.upper()
    with happ.app_context():
        assert invites.find_usable(code) is None  # used up


def test_an_invitation_link_carries_its_code_to_the_connect_page(happ, portal):
    _settings(happ, signin_mode="hosting", signup_mode="invite")
    browser = _english(Browser(happ))
    location = _start(browser, _start(browser, "/signup?invite=CODE123"))
    callback = portal.authorize(location, "sub-new", "newbie")
    assert browser.get(callback).headers["Location"] == "/login/hosting/connect"
    assert 'name="invite_code" required maxlength="32" autocomplete="off" autocapitalize="characters" ' \
           'spellcheck="false" value="CODE123"' in browser.get("/login/hosting/connect").get_data(as_text=True)


def test_the_connect_page_needs_a_fresh_pending_sign_in(happ, portal):
    from bananachat.web import hosting

    browser = _english(Browser(happ))
    assert browser.get("/login/hosting/connect").headers["Location"] == "/login"
    assert browser.post("/login/hosting/create", {"username": "x"}).headers["Location"] == "/login"
    _hosting_sign_in(browser, portal, "sub-new", "newbie")
    with browser.client.session_transaction() as session:
        session[hosting.PENDING_KEY] = {**session[hosting.PENDING_KEY],
                                        "at": session[hosting.PENDING_KEY]["at"] - hosting.PENDING_SECONDS - 5}
    assert browser.post("/login/hosting/create", {"username": "newbie"}).headers["Location"] == "/login"
    assert _user(happ, "newbie") is None


# ----- the first sign-in: an existing account ----------------------------------------------

def test_connecting_an_existing_account_takes_its_password(happ, portal):
    _make_user(happ, "carol")
    browser = _connect(happ, portal, "sub-carol", "caz")
    response = browser.post("/login/hosting/connect-existing", {"username": "carol", "password": "wrong-password"})
    assert response.status_code == 401 and "Incorrect username or password" in response.get_data(as_text=True)
    assert _link_of(happ, "carol") is None and _whoami(browser) is None
    response = browser.post("/login/hosting/connect-existing", {"username": "nobody", "password": "x"})
    assert response.status_code == 401
    response = browser.post("/login/hosting/connect-existing", {"username": "carol", "password": "carol-password"})
    assert response.status_code == 302 and _whoami(browser) == "carol"
    assert _link_of(happ, "carol")["account_id"] == "sub-carol"
    details = json.loads(_audit(happ, "auth.hosting_link")[0]["details"])
    assert details == {"portal_username": "caz", "via": "existing account"}


def test_an_account_linked_elsewhere_cannot_be_connected_again(happ, portal):
    _make_user(happ, "carol")
    _link(happ, "carol", "sub-other")
    browser = _connect(happ, portal, "sub-carol", "carol")
    response = browser.post("/login/hosting/connect-existing", {"username": "carol", "password": "carol-password"})
    assert response.status_code == 409 and "already connected to another BananaWiki Hosting account" in \
        response.get_data(as_text=True)
    assert _link_of(happ, "carol")["account_id"] == "sub-other" and _whoami(browser) is None


def test_connecting_counts_wrong_passwords_like_sign_in(happ, portal):
    happ.config["BC"] = happ.config["BC"].replace(login_failures_per_account=2)
    _make_user(happ, "carol")
    browser = _connect(happ, portal, "sub-carol", "carol")
    for _ in range(2):
        assert browser.post("/login/hosting/connect-existing",
                            {"username": "carol", "password": "wrong"}).status_code == 401
    assert browser.post("/login/hosting/connect-existing",
                        {"username": "carol", "password": "carol-password"}).status_code == 429
    assert _link_of(happ, "carol") is None


def test_connecting_an_account_with_two_step_verification_needs_its_code(happ, portal):
    from bananachat import totp
    from bananachat.services import two_step

    carol = _make_user(happ, "carol")
    secret = totp.generate_secret()
    with happ.app_context():
        two_step.enable(carol["id"], secret, 0)
    browser = _connect(happ, portal, "sub-carol", "carol")
    response = browser.post("/login/hosting/connect-existing", {"username": "carol", "password": "carol-password"})
    assert response.headers["Location"] == "/login/hosting/connect/code"
    assert _link_of(happ, "carol") is None
    assert "to connect carol" in browser.get("/login/hosting/connect/code").get_data(as_text=True)
    assert browser.post("/login/hosting/connect/code", {"code": "123456"}).status_code == 401
    assert _link_of(happ, "carol") is None and _whoami(browser) is None
    response = browser.post("/login/hosting/connect/code", {"code": totp.totp_code(secret)})
    assert response.status_code == 302 and response.headers["Location"] != "/login/two-step"
    assert _whoami(browser) == "carol" and _link_of(happ, "carol")["account_id"] == "sub-carol"


def test_the_code_step_needs_the_password_step_of_this_flow(happ, portal):
    browser = _connect(happ, portal, "sub-carol", "carol")
    response = browser.get("/login/hosting/connect/code")
    assert response.headers["Location"] == "/login/hosting/connect"


# ----- the lock-out guard and the recovery page --------------------------------------------

def _settings_form(**values):
    return {"site_name": "Test Chat", "signup_mode": "open", "default_theme_mode": "dark",
            "signin_mode_present": "1",
            **{f"{prefix}{key}_color": "#123456" for prefix in ("", "light_")
               for key in ("primary", "secondary", "accent", "text", "sidebar", "bg")}, **values}


def _stored_mode(app):
    from bananachat.db import settings as site_settings

    with app.app_context():
        return site_settings.get()["signin_mode"]


def test_an_administrator_switches_to_hosting_only_once_linked(happ):
    admin = _english(Browser(happ))
    admin.login("admin", "admin-password")
    page = admin.get("/admin/settings").get_data(as_text=True)
    assert f"<code>{CALLBACK}</code>" in page and CLIENT_ID in page and CLIENT_SECRET not in page
    assert "/login/recovery" in page and "signin-mode both" in page
    admin.post("/admin/settings", _settings_form(signin_mode="hosting"))
    assert _stored_mode(happ) == "both"
    assert "Connect your own account to BananaWiki Hosting first" in admin.get("/admin/settings").get_data(as_text=True)
    _link(happ, "admin", "sub-admin")
    admin.post("/admin/settings", _settings_form(signin_mode="hosting"))
    assert _stored_mode(happ) == "hosting"
    admin.post("/admin/settings", _settings_form(signin_mode="local"))
    assert _stored_mode(happ) == "local"
    changes = [json.loads(entry["details"]) for entry in _audit(happ, "admin.settings_save")]
    assert [change.get("signin_mode") for change in changes] == ["hosting", "local"]


def test_changing_the_mode_needs_a_recent_confirmation(happ):
    from tests.app.test_two_step import _stale

    admin = Browser(happ)
    admin.login("admin", "admin-password")
    _stale(happ)
    admin.post("/admin/settings", _settings_form(signin_mode="local"))
    assert _stored_mode(happ) == "both"
    admin.post("/admin/settings", _settings_form(signin_mode="local", reauth_secret="admin-password"))
    assert _stored_mode(happ) == "local"


def test_the_recovery_page_signs_administrators_in_and_audits_every_attempt(happ):
    _settings(happ, signin_mode="hosting")
    _make_user(happ, "bob")
    browser = _english(Browser(happ))
    assert browser.get("/login/recovery").status_code == 200
    assert "/login/recovery" not in browser.get("/login").get_data(as_text=True)
    wrong = browser.post("/login/recovery", {"username": "admin", "password": "wrong-password"})
    not_admin = browser.post("/login/recovery", {"username": "bob", "password": "bob-password"})
    assert wrong.status_code == not_admin.status_code == 401
    assert "Incorrect username or password" in not_admin.get_data(as_text=True)
    assert _whoami(browser) is None
    assert browser.post("/login/recovery", {"username": "admin", "password": "admin-password"}).status_code == 302
    assert _whoami(browser) == "admin"
    entries = _audit(happ, "auth.recovery")
    assert [(entry["action"], entry["target"]) for entry in entries] == [
        ("auth.recovery_login_failed", "admin"), ("auth.recovery_login_failed", "bob"),
        ("auth.recovery_login", _user(happ, "admin")["id"])]
    assert json.loads(_audit(happ, "auth.admin_login")[-1]["details"]) == {"method": "recovery"}


def test_the_recovery_page_asks_for_the_two_step_code_and_is_rate_limited(happ):
    from bananachat import totp
    from bananachat.services import two_step
    from bananachat.web import hosting

    _settings(happ, signin_mode="hosting")
    secret = totp.generate_secret()
    with happ.app_context():
        two_step.enable(_user(happ, "admin")["id"], secret, 0)
    browser = Browser(happ)
    response = browser.post("/login/recovery", {"username": "admin", "password": "admin-password"})
    assert response.headers["Location"] == "/login/two-step" and _whoami(browser) is None
    assert browser.post("/login/two-step", {"code": totp.totp_code(secret)}).status_code == 302
    assert _whoami(browser) == "admin"
    assert json.loads(_audit(happ, "auth.admin_login")[-1]["details"]) == {"method": "recovery+totp"}

    # Every request from this address counts, the successful one above included.
    other = Browser(happ)
    statuses = [other.post("/login/recovery", {"username": "admin", "password": "wrong"}).status_code
                for _ in range(hosting.RECOVERY_ATTEMPTS - 1)]
    assert statuses == [401] * (hosting.RECOVERY_ATTEMPTS - 1)
    assert other.post("/login/recovery", {"username": "admin", "password": "admin-password"}).status_code == 429


def test_the_recovery_page_exists_only_in_hosting_mode(happ):
    browser = Browser(happ)
    assert browser.get("/login/recovery").status_code == 404
    assert browser.post("/login/recovery", {"username": "admin", "password": "admin-password"}).status_code == 404


# ----- the account page ------------------------------------------------------------------

def test_linking_from_the_account_page_takes_the_password_first(happ, portal):
    _make_user(happ, "bob")
    browser = _english(Browser(happ))
    browser.login("bob")
    page = browser.get("/account")
    assert f"form-action 'self' {PORTAL};" in page.headers["Content-Security-Policy"]
    assert 'action="/account/hosting/link"' in page.get_data(as_text=True)
    response = browser.post("/account/hosting/link", {"password": "wrong"})
    assert response.headers["Location"] == "/account#hosting"
    response = browser.post("/account/hosting/link", {"password": "bob-password"})
    assert response.headers["Location"].startswith(PORTAL + "/oauth/authorize?")
    callback = portal.authorize(response.headers["Location"], "sub-bob", "bob-on-hosting")
    assert browser.get(callback).headers["Location"] == "/account#hosting"
    assert _link_of(happ, "bob")["portal_username"] == "bob-on-hosting"
    page = browser.get("/account")
    assert "Connected to bob-on-hosting since" in page.get_data(as_text=True)
    # Linked: the page offers a confirmation with BananaWiki Hosting instead of the password, which goes there.
    assert 'action="/account/hosting/confirm"' in page.get_data(as_text=True)
    assert f"form-action 'self' {PORTAL};" in page.headers["Content-Security-Policy"]
    _settings(happ, signin_mode="local")
    assert "form-action 'self';" in browser.get("/account").headers["Content-Security-Policy"]
    assert json.loads(_audit(happ, "auth.hosting_link")[0]["details"])["via"] == "account page"


def test_a_link_flow_only_finishes_in_the_session_that_started_it(happ, portal):
    _make_user(happ, "bob")
    _make_user(happ, "carol")
    _link(happ, "carol", "sub-taken")
    bob = Browser(happ)
    bob.login("bob")
    location = bob.post("/account/hosting/link", {"password": "bob-password"}).headers["Location"]
    # A portal account linked to someone else is refused.
    assert bob.get(portal.authorize(location, "sub-taken", "carol")).headers["Location"] == "/account#hosting"
    assert _link_of(happ, "bob") is None
    # A link callback signs nobody in, and does not link the account that finishes it elsewhere.
    location = bob.post("/account/hosting/link", {"password": "bob-password"}).headers["Location"]
    stranger = Browser(happ)
    assert stranger.get(portal.authorize(location, "sub-bob", "bob")).headers["Location"] == "/login"
    assert _whoami(stranger) is None
    stranger.login("carol")
    assert stranger.get(portal.authorize(location, "sub-bob", "bob")).headers["Location"] == "/account"
    assert _link_of(happ, "bob") is None and _link_of(happ, "carol")["account_id"] == "sub-taken"


def test_unlinking_takes_the_password_and_is_refused_in_hosting_mode(happ):
    _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    browser = _english(Browser(happ))
    browser.login("bob")
    assert browser.post("/account/hosting/unlink", {"password": "wrong"}).headers["Location"] == "/account#hosting"
    assert _link_of(happ, "bob") is not None
    _settings(happ, signin_mode="hosting")
    page = browser.get("/account").get_data(as_text=True)
    assert 'action="/account/hosting/unlink"' not in page and "can&#39;t be removed" in page
    browser.post("/account/hosting/unlink", {"password": "bob-password"})
    assert _link_of(happ, "bob") is not None
    _settings(happ, signin_mode="both")
    browser.post("/account/hosting/unlink", {"password": "bob-password"})
    assert _link_of(happ, "bob") is None
    assert [entry["action"] for entry in _audit(happ, "account.hosting")] == ["account.hosting_unlink"]


def test_the_personal_data_export_names_the_link(happ):
    from bananachat.services import exports

    bob = _make_user(happ, "bob")
    with happ.test_request_context():
        assert exports.gdpr_export(bob["id"])["hosting_account"] is None
    _link(happ, "bob", "sub-bob", "bobby")
    with happ.test_request_context():
        exported = exports.gdpr_export(bob["id"])["hosting_account"]
    assert exported["account_id"] == "sub-bob" and exported["portal_username"] == "bobby"


# ----- administration ---------------------------------------------------------------------

def test_an_administrator_sees_and_removes_a_link(happ):
    from bananachat import db

    bob = _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob", "bobby")
    with happ.app_context():
        from bananachat.db import users
        users.create_session(bob["id"], 7)
    admin = Browser(happ)
    admin.login("admin", "admin-password")
    page = admin.get(f"/admin/users/{bob['id']}").get_data(as_text=True)
    assert "bobby" in page and "sub-bob" in page and "Remove link" in page
    assert admin.post(f"/admin/users/{bob['id']}/hosting/unlink").status_code == 302
    assert _link_of(happ, "bob") is None
    with happ.app_context():
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (bob["id"],)) == 0
    entry = _audit(happ, "admin.user_hosting_unlink")[0]
    assert entry["target"] == "bob" and json.loads(entry["details"]) == {"portal_username": "bobby", "sub": "sub-bob"}


def test_an_administrator_cannot_remove_their_own_link_in_hosting_mode(happ):
    _link(happ, "admin", "sub-admin")
    _settings(happ, signin_mode="hosting")
    admin = Browser(happ)
    admin.post("/login/recovery", {"username": "admin", "password": "admin-password"})
    admin.post(f"/admin/users/{_user(happ, 'admin')['id']}/hosting/unlink")
    assert _link_of(happ, "admin") is not None


# ----- the operator command -------------------------------------------------------------------

def test_the_operator_command_shows_and_sets_the_mode(happ, capsys):
    from bananachat import manage

    config = happ.config["BC"]
    environ = {"BC_INSTANCE_DIR": str(config.instance_dir), "BC_DATABASE_PATH": str(config.database_path)}
    _settings(happ, signin_mode="hosting")
    assert manage.main(["signin-mode"], environ={**environ, **HOSTING_ENV}) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "unchanged", "mode": "hosting", "previous": "hosting",
                                                   "configured": True, "effective": "hosting"}
    assert manage.main(["signin-mode", "both"], environ=environ) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "changed", "mode": "both", "previous": "hosting",
                                                   "configured": False, "effective": "local"}
    assert _stored_mode(happ) == "both"
    entry = _audit(happ, "operator.signin_mode")[0]
    assert entry["actor_name"] == "server operator"
    assert json.loads(entry["details"]) == {"from": "hosting", "to": "both", "via": "signin-mode command"}
    with pytest.raises(SystemExit):
        manage.main(["signin-mode", "platform"], environ=environ)
    assert _stored_mode(happ) == "both"


def test_callbacks_are_rate_limited_per_address(happ, portal):
    happ.config["BC"] = happ.config["BC"].replace(login_attempts_per_minute=2)
    browser = Browser(happ)
    statuses = [browser.get("/login/hosting/callback?state=x&code=y").status_code for _ in range(3)]
    assert statuses == [302, 302, 429]
    assert portal.calls == []


def test_deleting_an_account_removes_its_link(happ):
    from bananachat.db import hosting_links, users

    bob = _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob")
    with happ.app_context():
        users.delete(bob["id"])
        assert hosting_links.linked_user("sub-bob") is None and hosting_links.link_of(bob["id"]) is None


def test_a_code_owed_since_a_password_or_recovery_step_lapses_when_that_step_no_longer_applies(happ):
    from bananachat import totp
    from bananachat.db import users
    from bananachat.services import two_step

    bob = _make_user(happ, "bob")
    secret = totp.generate_secret()
    with happ.app_context():
        two_step.enable(bob["id"], secret, 0)
        two_step.enable(_user(happ, "admin")["id"], secret, 0)
    # A password sign-in started before hosting-only sign-in was switched on.
    browser = Browser(happ)
    assert browser.post("/login", {"username": "bob", "password": "bob-password"}).headers["Location"] \
        == "/login/two-step"
    _settings(happ, signin_mode="hosting")
    assert browser.post("/login/two-step", {"code": totp.totp_code(secret)}).headers["Location"] == "/login"
    assert _whoami(browser) is None
    # A recovery sign-in of an administrator who is no longer one.
    _make_user(happ, "other", role="admin")
    admin = Browser(happ)
    assert admin.post("/login/recovery", {"username": "admin", "password": "admin-password"}).headers["Location"] \
        == "/login/two-step"
    with happ.app_context():
        users.set_role(_user(happ, "admin")["id"], "user")
    assert admin.post("/login/two-step", {"code": totp.totp_code(secret)}).headers["Location"] \
        == "/login"
    assert _whoami(admin) is None
