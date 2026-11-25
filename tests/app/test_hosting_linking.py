"""The administrators' BananaWiki Hosting linking tool (web/admin/hosting.py; B and A3 of SIGNIN-SPEC.md):
the portal API token stored encrypted, the account lookup, previews with each kind of row, Apply only for an
unchanged preview, unlinking, and hosting-only sign-in refused while an administrator is not linked.

The portal's account lookup is imitated by :class:`LookupPortal`, which checks the Bearer token as the portal
does and answers from its own directory.
"""

from __future__ import annotations

import json

import pytest

from tests.app.fixtures import Browser
from tests.app.test_hosting_signin import _audit, _english, _link, _link_of, _make_user, _settings, _user
from tests.app.test_hosting_signin_methods import Portal

TOKEN = "bwt_lookup-token-0123456789abcdef"  # noqa: S105 - a test value
LOOKUP = "/api/v1/admin/accounts/lookup"


class LookupPortal(Portal):
    def __init__(self):
        super().__init__()
        self.directory: list[dict] = []
        self.lookups: list[dict] = []
        self.lookup_status = None

    def request(self, method, path, *, headers, body=None, max_bytes=None, timeout=None):
        if path != LOOKUP:
            return super().request(method, path, headers=headers, body=body)
        self.calls.append((method, path))
        if self.failure is not None:
            raise self.failure
        if self.lookup_status is not None:
            return self.lookup_status, {"error": "refused"}
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": "invalid_token"}
        query = json.loads(body)
        assert method == "POST" and len(query["usernames"]) <= 500 and max_bytes and max_bytes >= 64 * 1024
        self.lookups.append(query)
        names = {name.lower() for name in query["usernames"]}
        accounts = [dict(account) for account in self.directory if account["username"].lower() in names]
        found = {account["username"].lower() for account in accounts}
        return 200, {"accounts": accounts, "not_found": [name for name in query["usernames"]
                                                         if name.lower() not in found]}


@pytest.fixture
def portal(monkeypatch):
    from bananachat.services import hosting_signin

    fake = LookupPortal()
    monkeypatch.setattr(hosting_signin, "_request", fake.request)
    return fake


@pytest.fixture
def happ(make_app):
    from tests.app.test_hosting_signin import HOSTING_ENV

    app = make_app(**HOSTING_ENV)
    _settings(app, signin_mode="both", signup_mode="open")
    return app


@pytest.fixture
def admin(happ):
    browser = _english(Browser(happ))
    browser.login("admin", "admin-password")
    return browser


def _settings_form(**values):
    return {"site_name": "Test Chat", "signup_mode": "open", "default_theme_mode": "dark", "signin_mode_present": "1",
            "signin_mode": "both", "hosting_acting_present": "1", "hosting_allow_acting": "1",
            **{f"{prefix}{key}_color": "#123456" for prefix in ("", "light_")
               for key in ("primary", "secondary", "accent", "text", "sidebar", "bg")}, **values}


def _store_token(happ, admin):
    admin.post("/admin/settings", _settings_form(hosting_lookup_token=TOKEN))
    from bananachat.services import hosting_signin

    with happ.test_request_context():
        assert hosting_signin.lookup_state() == "ready"


def _account(sub, username, status="active"):
    return {"sub": sub, "username": username, "email_verified": False, "status": status}


def _rows(response) -> dict[str, str]:
    """``{account here: result}`` from a preview page."""
    import re

    html = response.get_data(as_text=True)
    start = html.index('id="preview"')
    table = html[start:html.index("</table>", start)]
    return {local: status for local, status in re.findall(
        r'<td class="cell-full"><strong>([^<]+)</strong>.*?data-label="Result:"><span class="badge[^"]*">([^<]+)</span>',
        table, re.S)}


def _digest(response) -> str:
    import re

    return re.search(r'name="digest" value="([0-9a-f]{64})"', response.get_data(as_text=True)).group(1)


# ----- the portal API token (A3) -------------------------------------------------------------------------

def test_the_lookup_token_is_stored_encrypted_never_shown_nor_audited(happ, admin, portal):
    from bananachat import db
    from bananachat.services import hosting_signin

    page = admin.get("/admin/settings").get_data(as_text=True)
    assert 'name="hosting_lookup_token"' in page and "None stored." in page
    _store_token(happ, admin)
    with happ.app_context():
        stored = db.scalar("SELECT hosting_lookup_token FROM site_settings")
    assert stored and TOKEN not in stored
    page = admin.get("/admin/settings").get_data(as_text=True)
    assert TOKEN not in page and stored not in page and "A token is stored" in page
    details = [entry["details"] for entry in _audit(happ, "admin.settings_save")]
    assert json.loads(details[-1])["hosting_lookup_token"] == "replaced"
    assert not any(TOKEN in text or stored in text for text in details)
    # Another secret key cannot read it: the page asks for it again.
    happ.config["BC"] = happ.config["BC"].replace(secret_key="another-secret-key-that-is-long-enough-987")
    with happ.test_request_context():
        assert hosting_signin.lookup_state() == "unreadable" and hosting_signin.lookup_token() == ""
    happ.config["BC"] = happ.config["BC"].replace(secret_key="test-secret-key-that-is-long-enough-0123456789")
    admin.post("/admin/settings", _settings_form(hosting_lookup_token_remove="1"))
    with happ.app_context():
        assert db.scalar("SELECT hosting_lookup_token FROM site_settings") is None
    assert json.loads(_audit(happ, "admin.settings_save")[-1]["details"])["hosting_lookup_token"] == "removed"


def test_changing_the_token_or_allowing_acting_again_needs_a_confirmation(happ, admin, portal):
    from bananachat import db
    from tests.app.test_two_step import _stale

    admin.post("/admin/settings", _settings_form(hosting_allow_acting=""))
    with happ.app_context():
        assert db.scalar("SELECT hosting_allow_acting FROM site_settings") == 0
    _stale(happ)
    admin.post("/admin/settings", _settings_form())
    admin.post("/admin/settings", _settings_form(hosting_allow_acting="", hosting_lookup_token=TOKEN))
    with happ.app_context():
        assert db.scalar("SELECT hosting_allow_acting FROM site_settings") == 0
        assert db.scalar("SELECT hosting_lookup_token FROM site_settings") is None
    admin.post("/admin/settings", _settings_form(reauth_secret="admin-password"))
    with happ.app_context():
        assert db.scalar("SELECT hosting_allow_acting FROM site_settings") == 1


def test_a_refused_token_or_unreachable_portal_says_so(happ, admin, portal):
    from bananachat.services.hosting_signin import OAuthError

    _make_user(happ, "alice")
    _store_token(happ, admin)
    portal.lookup_status = 403
    response = admin.post("/admin/users/hosting/preview", {"rule": "username"})
    assert response.status_code == 400 and "refused the portal API token" in response.get_data(as_text=True)
    portal.lookup_status = None
    portal.failure = OAuthError("hosting.error_portal", "connect")
    response = admin.post("/admin/users/hosting/preview", {"rule": "list", "column": "username",
                                                           "lines": "alice,alice"})
    assert "could not be reached" in response.get_data(as_text=True)
    assert "alice,alice" in response.get_data(as_text=True)  # what was typed is kept


def test_without_a_token_only_portal_account_ids_are_taken(happ, admin, portal):
    _make_user(happ, "alice")
    page = admin.get("/admin/users/hosting").get_data(as_text=True)
    assert 'value="username" disabled' in page and 'name="column" value="sub" checked' in page
    response = admin.post("/admin/users/hosting/preview", {"rule": "username"})
    assert response.status_code == 400 and "need the portal API token" in response.get_data(as_text=True)
    response = admin.post("/admin/users/hosting/preview", {"rule": "one", "column": "sub",
                                                           "local_username": "alice", "hosting_account": "sub-alice"})
    assert _rows(response) == {"alice": "Will link"}
    assert portal.lookups == []


# ----- previews and Apply (B) ----------------------------------------------------------------------------

def _population(happ, portal):
    """Accounts here and on the portal that give every kind of preview row with the "same user name" rule."""
    from bananachat.db import users

    for name in ("alice", "bob", "carol", "frank", "george", "gina", "hank", "ivan", "eve"):
        _make_user(happ, name)
    _make_user(happ, "dave", role="admin")
    _link(happ, "bob", "sub-bob")
    _link(happ, "carol", "sub-carol-old")
    _link(happ, "george", "sub-frank")
    with happ.app_context():
        users.suspend(_user(happ, "eve")["id"])
    portal.directory = [
        _account("sub-alice", "Alice"),            # same name, other capitals: will link
        _account("sub-bob", "bob"),                # already linked to it
        _account("sub-carol", "carol"),            # carol is linked to another hosting account
        _account("sub-frank", "frank"),            # linked to george here
        _account("sub-gina", "gina", "suspended"),
        _account("sub-hank-1", "hank"), _account("sub-hank-2", "HANK"),  # several candidates
        _account("sub-dave", "dave"), _account("sub-eve", "eve"), _account("sub-admin", "admin"),
    ]


def test_the_same_user_name_preview_shows_every_kind_of_row_and_apply_links_only_will_link(happ, admin, portal):
    _store_token(happ, admin)
    _population(happ, portal)
    response = admin.post("/admin/users/hosting/preview", {"rule": "username"})
    assert response.status_code == 200
    assert _rows(response) == {
        "alice": "Will link", "bob": "Already linked", "carol": "Conflict", "frank": "Conflict",
        "gina": "Conflict", "hank": "Conflict", "george": "Not found", "ivan": "Not found",
        "admin": "Skipped", "dave": "Skipped", "eve": "Skipped",
    }
    page = response.get_data(as_text=True)
    for reason in ("linked to another BananaWiki Hosting account", "is linked to george",
                   "The BananaWiki Hosting account is suspended", "Several BananaWiki Hosting accounts match",
                   "tick Include administrators", "The account here is suspended"):
        assert reason in page, reason
    response = admin.post("/admin/users/hosting/apply", {"rule": "username", "digest": _digest(response)})
    assert response.headers["Location"] == "/admin/users/hosting"
    assert _link_of(happ, "alice")["account_id"] == "sub-alice" and _link_of(happ, "alice")["portal_username"] \
        == "Alice"
    assert _link_of(happ, "carol")["account_id"] == "sub-carol-old" and _link_of(happ, "dave") is None
    entries = _audit(happ, "admin.hosting_link")
    assert [(entry["target"], json.loads(entry["details"])) for entry in entries] == [
        ("alice", {"portal_username": "Alice", "rule": "same user name", "sub": "sub-alice"})]
    # Administrators are linked only when included.
    response = admin.post("/admin/users/hosting/preview", {"rule": "username", "include_admins": "1"})
    assert _rows(response)["dave"] == "Will link" and _rows(response)["admin"] == "Will link"


def test_a_changed_or_altered_preview_links_nothing(happ, admin, portal):
    _store_token(happ, admin)
    _make_user(happ, "alice")
    _make_user(happ, "bob")
    portal.directory = [_account("sub-alice", "alice"), _account("sub-bob", "bob")]
    response = admin.post("/admin/users/hosting/preview", {"rule": "username"})
    digest = _digest(response)
    # Meanwhile bob connects another hosting account himself.
    _link(happ, "bob", "sub-bob-elsewhere")
    response = admin.post("/admin/users/hosting/apply", {"rule": "username", "digest": digest})
    assert response.status_code == 409 and "Something changed since the preview" in response.get_data(as_text=True)
    assert _link_of(happ, "alice") is None
    assert _rows(response) == {"alice": "Will link", "bob": "Conflict", "admin": "Skipped"}
    # A digest made up or taken from another rule is refused as well.
    assert admin.post("/admin/users/hosting/apply", {"rule": "username", "digest": "0" * 64}).status_code == 409
    assert admin.post("/admin/users/hosting/apply", {"rule": "username", "include_admins": "1",
                                                     "digest": _digest(response)}).status_code == 409
    assert _link_of(happ, "alice") is None
    # The new preview applies.
    admin.post("/admin/users/hosting/apply", {"rule": "username", "digest": _digest(response)})
    assert _link_of(happ, "alice")["account_id"] == "sub-alice"


def test_a_pasted_list_of_portal_ids_finds_duplicates_and_unknown_accounts(happ, admin, portal):
    _make_user(happ, "alice")
    _make_user(happ, "bob")
    _make_user(happ, "carol")
    lines = "local_username,hosting_sub\nalice,sub-a\nalice,sub-b\nnobody,sub-x\nbob," + "x" * 70 + "\ncarol,sub-c\n" \
            "# a comment\n"
    # As a browser sends a textarea: Windows line ends (and here an empty first line, which a hidden copy loses).
    form = {"rule": "list", "column": "sub", "lines": "\r\n" + lines.replace("\n", "\r\n")}
    response = admin.post("/admin/users/hosting/preview", form)
    assert _rows(response) == {"carol": "Will link", "alice": "Conflict", "bob": "Not found", "nobody": "Not found"}
    assert "listed more than once" in response.get_data(as_text=True)
    admin.post("/admin/users/hosting/apply", {**form, "digest": _digest(response)})
    assert _link_of(happ, "carol")["account_id"] == "sub-c" and _link_of(happ, "alice") is None
    assert json.loads(_audit(happ, "admin.hosting_link")[0]["details"])["rule"] == "pasted list"
    # Two accounts here for one hosting account are a conflict too.
    _make_user(happ, "dan")
    response = admin.post("/admin/users/hosting/preview", {"rule": "list", "column": "sub",
                                                           "lines": "alice,sub-z\ndan,sub-z\nbob,sub-c"})
    assert _rows(response) == {"alice": "Conflict", "bob": "Conflict", "dan": "Conflict"}
    assert "is linked to carol" in response.get_data(as_text=True)
    assert admin.post("/admin/users/hosting/preview", {"rule": "list", "column": "sub",
                                                       "lines": "just-one-column"}).status_code == 400


def test_apply_needs_a_recent_confirmation(happ, admin, portal):
    from tests.app.test_two_step import _stale

    _make_user(happ, "alice")
    form = {"rule": "one", "column": "sub", "local_username": "alice", "hosting_account": "sub-alice"}
    response = admin.post("/admin/users/hosting/preview", form)
    _stale(happ)
    response = admin.post("/admin/users/hosting/apply", {**form, "digest": _digest(response)})
    assert response.status_code == 403 and _link_of(happ, "alice") is None
    assert 'name="reauth_secret"' in response.get_data(as_text=True)
    admin.post("/admin/users/hosting/apply", {**form, "digest": _digest(response), "reauth_secret": "admin-password"})
    assert _link_of(happ, "alice")["account_id"] == "sub-alice"
    assert json.loads(_audit(happ, "admin.hosting_link")[0]["details"])["rule"] == "one account"


def test_the_page_lists_link_states_and_unlinks(happ, admin, portal):
    from bananachat import db

    _make_user(happ, "alice")
    bob = _make_user(happ, "bob")
    _link(happ, "bob", "sub-bob", "bobby")
    with happ.app_context():
        from bananachat.db import users
        users.create_session(bob["id"], 7)
    page = admin.get("/admin/users/hosting").get_data(as_text=True)
    assert "1 of 3 accounts is connected" in page and "1 active administrator is not" in page
    assert "bobby" in page and "sub-bob" in page and "Unlink bob" in page and "Unlink alice" not in page
    assert "alice" not in admin.get("/admin/users/hosting?state=linked").get_data(as_text=True).split(
        'id="accounts"')[1]
    response = admin.post(f"/admin/users/{bob['id']}/hosting/unlink", {"return_to": "links"})
    assert response.headers["Location"] == "/admin/users/hosting"
    assert _link_of(happ, "bob") is None
    with happ.app_context():
        assert db.scalar("SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (bob["id"],)) == 0
    assert _audit(happ, "admin.user_hosting_unlink")[0]["target"] == "bob"


def test_hosting_only_is_refused_while_an_administrator_is_not_linked(happ, admin, portal):
    from bananachat.db import settings as site_settings

    def mode():
        with happ.app_context():
            return site_settings.get()["signin_mode"]

    _link(happ, "admin", "sub-admin")
    _make_user(happ, "chief", role="admin")
    page = admin.get("/admin/users/hosting").get_data(as_text=True)
    assert "Not connected: chief" in page and 'action="/admin/users/hosting/hosting-only"' not in page
    admin.post("/admin/users/hosting/hosting-only", {})
    admin.post("/admin/settings", _settings_form(signin_mode="hosting"))
    assert mode() == "both"
    assert "Connect every administrator to BananaWiki Hosting first" in admin.get("/admin/settings").get_data(
        as_text=True)
    _link(happ, "chief", "sub-chief")
    assert 'action="/admin/users/hosting/hosting-only"' in admin.get("/admin/users/hosting").get_data(as_text=True)
    admin.post("/admin/users/hosting/hosting-only", {})
    assert mode() == "hosting"
    assert json.loads(_audit(happ, "admin.hosting_only")[0]["details"]) == {"from": "both", "to": "hosting"}


def test_users_cannot_reach_the_linking_tool(happ, portal):
    _make_user(happ, "mallory")
    browser = Browser(happ)
    browser.login("mallory")
    assert browser.get("/admin/users/hosting").status_code == 403
    assert browser.post("/admin/users/hosting/preview", {"rule": "username"}).status_code == 403
    assert browser.post("/admin/users/hosting/apply", {"rule": "username", "digest": "0" * 64}).status_code == 403
    assert browser.post("/admin/users/hosting/hosting-only", {}).status_code == 403
    assert portal.calls == []


def test_the_linking_page_saves_nothing_and_nests_no_forms(happ, admin, portal):
    from tests.app.test_page_save import assert_forms_not_nested, assert_no_save
    from tests.app.test_two_step import _stale

    _link(happ, "admin", "sub-admin")
    _make_user(happ, "alice")
    _stale(happ)  # the confirmation fields and the hidden "Confirm with BananaWiki Hosting" form show
    response = admin.post("/admin/users/hosting/preview", {"rule": "one", "column": "sub",
                                                           "local_username": "alice", "hosting_account": "sub-a"},
                          headers={"Referer": "http://localhost/admin/users/hosting"})
    for html in (admin.get("/admin/users/hosting").get_data(as_text=True), response.get_data(as_text=True)):
        assert 'id="reauth-hosting"' in html
        assert_no_save(html, "/admin/users/hosting")
        assert_forms_not_nested(html, "/admin/users/hosting")
    # Confirming from the preview comes back to the linking page (the preview itself answered a form).
    assert '<input type="hidden" name="next" value="/admin/users/hosting">' in response.get_data(as_text=True)
