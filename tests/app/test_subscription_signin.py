"""Signing Claude and Codex subscription accounts in from the administration pages.

Fake CLIs stand in for the official ones: the Claude one prints its sign-in link on a terminal and waits for
the code, like ``claude auth login``; the Codex one prints a link and a one-time code and waits for approval,
like ``codex login --device-auth``. Nothing contacts a provider.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananachat.db import settings as site_settings
from bananachat.db import subscription_pool as accounts
from bananachat.services import claude_sandbox, subscription_login, subscription_pool
from bananachat.services.subscription_cli import ConnectorError
from bananachat.services.subscription_profiles import NEW_PROFILE

FAKE_CODEX = Path(__file__).with_name("fake_codex.py")
LINK = ("https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code&redirect_uri="
        "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=abc")
GOOD_CODE = "good-code-0123456789#state-abc"
FAKE_CLAUDE = r'''#!{python} -I
import json, os, sys, time
from pathlib import Path
config = Path(os.environ["CLAUDE_CONFIG_DIR"])
state = config / "fake-login.json"
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    logged = json.loads(state.read_text()) if state.exists() else None
    status = {{"loggedIn": bool(logged), "authMethod": "claude.ai" if logged else "none", "apiProvider": "firstParty"}}
    if logged:
        status.update(subscriptionType=logged["plan"], email=logged["email"])
    print(json.dumps(status))
    sys.exit(0 if logged else 1)
if args[:2] == ["auth", "logout"]:
    state.unlink(missing_ok=True)
    print("Successfully logged out from your Anthropic account.")
    sys.exit(0)
if args[:2] == ["auth", "login"]:
    assert os.isatty(0), "the login needs a terminal"
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: \x1b]8;;{link}\x07\x1b[94m{link}\x1b[39m\x1b]8;;\x07")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "{code}":
        state.write_text(json.dumps({{"plan": "max", "email": "owner@example.org"}}))
        print("Login successful.")
        sys.exit(0)
    print("OAuth error: Invalid code sk-ant-secret-that-must-not-leak-0123456789abcdef")
    sys.exit(1)
sys.exit(2)
'''


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@pytest.fixture
def claude_manifest(tmp_path):
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text(FAKE_CLAUDE.format(python=sys.executable, link=LINK, code=GOOD_CODE))
    binary.chmod(0o700)
    root = _private(tmp_path / "signed-in" / "claude")
    manifest = tmp_path / "claude.json"
    manifest.write_text(json.dumps({"binary": str(binary), "managed_root": str(root), "profiles": {}}))
    manifest.chmod(0o600)
    return manifest, root


@pytest.fixture
def codex_manifest(tmp_path):
    binary = tmp_path / "bin-codex" / "codex"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable} -I\nimport runpy\nrunpy.run_path({str(FAKE_CODEX)!r}, run_name='__main__')\n")
    binary.chmod(0o700)
    root = _private(tmp_path / "signed-in" / "codex")
    manifest = tmp_path / "codex.json"
    manifest.write_text(json.dumps({"binary": str(binary), "managed_root": str(root)}))
    manifest.chmod(0o600)
    return manifest, root


@pytest.fixture
def claude_app(make_app, claude_manifest):
    manifest, root = claude_manifest
    app = make_app(CLAUDE_EXTENSION="bananachat.services.claude_code", CLAUDE_CODE_CONFIG=str(manifest))
    yield app, root
    subscription_pool.reset_transport()


@pytest.fixture
def codex_app(make_app, codex_manifest):
    manifest, root = codex_manifest
    app = make_app(CODEX_EXTENSION="bananachat.services.codex_cli", CODEX_CLI_CONFIG=str(manifest))
    yield app, root
    subscription_pool.reset_transport()


def _admin(app):
    from tests.app.fixtures import Browser

    browser = Browser(app)
    browser.login("admin", "admin-password")
    return browser


# ----- managed profiles -----------------------------------------------------------------------------------

def test_managed_profiles_are_created_bound_seen_by_other_processes_and_removed(claude_manifest):
    from bananachat.services.claude_code import Adapter

    manifest, root = claude_manifest
    config = SimpleNamespace(environment="testing", claude_code_config=str(manifest))
    first, second = Adapter(config), Adapter(config)
    first.bindings = second.bindings = None  # replaced below, without a database
    assert first.profiles == {} and first.managed_root == str(root)
    name = first.create_managed_profile(7)
    assert name == "managed-7" and (root / name).stat().st_mode & 0o777 == 0o700
    assert (root / name / ".claude").is_dir()
    second._refresh_managed(force=True)  # another gunicorn process sees it
    assert second.profiles[name]["home"] == str(root / name)
    assert second._managed_bindings({}) == {"7": name}
    assert second._managed_bindings({"7": "operator-profile"}) == {"7": "operator-profile"}  # explicit wins
    (root / name / ".claude" / "credentials").write_text("secret")
    assert first.remove_managed_profile(name) is True
    assert not (root / name).exists() and name not in first.profiles
    assert first.remove_managed_profile(name) is False


def test_a_shared_or_linked_profile_directory_is_never_used(claude_manifest, tmp_path):
    from bananachat.services.claude_code import Adapter

    manifest, root = claude_manifest
    adapter = Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))
    readable = root / "managed-3"
    readable.mkdir(mode=0o755)
    readable.chmod(0o755)
    elsewhere = _private(tmp_path / "elsewhere")
    (root / "managed-4").symlink_to(elsewhere)
    adapter._refresh_managed(force=True)
    assert "managed-3" not in adapter.profiles and "managed-4" not in adapter.profiles


def test_manifest_names_reserved_for_managed_profiles_are_refused(claude_manifest, tmp_path):
    from bananachat.services.claude_code import Adapter

    manifest, root = claude_manifest
    home = _private(tmp_path / "static-home")
    _private(home / "config")
    data = json.loads(manifest.read_text())
    data["profiles"] = {"managed-1": {"home": str(home), "config_dir": str(home / "config")}}
    manifest.write_text(json.dumps(data))
    with pytest.raises(ConnectorError, match="reserved"):
        Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))
    data["profiles"] = {"inside": {"home": str(root), "config_dir": str(root)}}
    manifest.write_text(json.dumps(data))
    with pytest.raises(ConnectorError, match="outside managed_root"):
        Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))


def test_the_managed_root_needs_root_owned_parents_in_production(tmp_path):
    root = _private(tmp_path / "group-writable" / "claude")
    (tmp_path / "group-writable").chmod(0o777)
    with pytest.raises(claude_sandbox.SandboxError):
        claude_sandbox.managed_root(str(root), uid=os.getuid(), sandboxed=True)
    assert claude_sandbox.managed_root(str(root), uid=os.getuid(), sandboxed=False) == root


def test_managed_paths_give_the_service_the_managed_root(claude_manifest, monkeypatch):
    manifest, root = claude_manifest
    monkeypatch.setattr(claude_sandbox, "trusted_executable", lambda value: str(value))
    monkeypatch.setattr(claude_sandbox, "managed_root", lambda value, **kwargs: Path(value))
    assert claude_sandbox.managed_paths(str(manifest), uid=os.getuid(), root="/opt/bananachat") == [root]


# ----- the Claude sign-in -----------------------------------------------------------------------------------

def _account(app, label="claude-one", provider="claude"):
    with app.app_context():
        return accounts.add_account(label, provider=provider, priority=0, window_limit=100_000)


def test_claude_sign_in_relays_the_link_and_the_code(claude_app):
    app, root = claude_app
    account_id = _account(app)
    with app.app_context():
        adapter = app.extensions["claude_adapter"]
        adapter.create_managed_profile(account_id)
        row = accounts.get(account_id)
        session = subscription_login.start(app, adapter, "claude", row, by="admin")
        assert session["state"] == "starting" and session["kind"] == "code"
        waiting = wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") == "waiting"
                           and subscription_login.status("claude", account_id))
        assert waiting["url"] == LINK and waiting["needs_code"]
        with pytest.raises(subscription_login.LoginError):
            subscription_login.submit_code("claude", account_id, "short")
        subscription_login.submit_code("claude", account_id, f"  {GOOD_CODE}\n")
        done = wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") in
                        subscription_login.FINAL and subscription_login.status("claude", account_id))
        assert done["state"] == "done", done["message"]
        assert done["plan"] == "max"
        assert subscription_login.signin_status("claude", account_id)["signed_in"] is True
        assert site_settings.state_get(f"subscription_login:claude:{account_id}:code") is None


def test_a_wrong_claude_code_fails_without_showing_secrets(claude_app):
    app, _root = claude_app
    account_id = _account(app)
    with app.app_context():
        adapter = app.extensions["claude_adapter"]
        adapter.create_managed_profile(account_id)
        subscription_login.start(app, adapter, "claude", accounts.get(account_id))
        assert wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") == "waiting")
        subscription_login.submit_code("claude", account_id, "wrong-code-0123456789")
        failed = wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") in
                          subscription_login.FINAL and subscription_login.status("claude", account_id))
        assert failed["state"] == "failed"
        assert "could not complete the sign-in" in failed["message"] and "Invalid code" in failed["message"]
        assert "sk-ant-secret" not in failed["message"]


def test_a_sign_in_can_be_cancelled_and_only_one_runs_at_a_time(claude_app):
    app, _root = claude_app
    account_id = _account(app)
    with app.app_context():
        adapter = app.extensions["claude_adapter"]
        adapter.create_managed_profile(account_id)
        subscription_login.start(app, adapter, "claude", accounts.get(account_id))
        with pytest.raises(subscription_login.LoginError, match="already running"):
            subscription_login.start(app, adapter, "claude", accounts.get(account_id))
        assert wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") == "waiting")
        subscription_login.cancel("claude", account_id)
        assert wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") == "cancelled")


def test_an_abandoned_sign_in_is_reported_as_interrupted(claude_app):
    app, _root = claude_app
    with app.app_context():
        site_settings.state_set("subscription_login:claude:9", {
            "id": "x", "state": "waiting", "pid": 2 ** 22 + 12345, "host": subscription_login.socket.gethostname(),
            "expires_at": time.time() + 600})
        assert subscription_login.status("claude", 9)["state"] == "failed"
        assert "interrupted" in subscription_login.status("claude", 9)["message"]


def test_links_and_messages_are_filtered():
    assert subscription_login.find_link("claude", "visit https://evil.example/https://claude.ai x") is None
    assert subscription_login.find_link("claude", "go https://claude.ai.evil.example/x") is None
    assert subscription_login.find_link("codex", f"x {LINK}") is None
    assert subscription_login.find_link("codex", "1. https://auth.openai.com/codex/device\n") == \
        "https://auth.openai.com/codex/device"
    assert subscription_login.find_device_code("Enter this one-time code (expires)\n   ABCD-12345\n") == "ABCD-12345"
    text = "Error: token sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA at https://claude.ai/x"
    shown = subscription_login.summary(text)
    assert "sk-ant" not in shown and "https://" not in shown and "[link]" in shown


# ----- the Codex sign-in ------------------------------------------------------------------------------------

def test_codex_device_sign_in_shows_the_code_and_finishes_on_approval(codex_app):
    app, root = codex_app
    account_id = _account(app, "codex-one", "codex")
    with app.app_context():
        adapter = app.extensions["codex_adapter"]
        name = adapter.create_managed_profile(account_id)
        (root / name / "scenario.json").write_text(json.dumps({"account": None}))
        session = subscription_login.start(app, adapter, "codex", accounts.get(account_id))
        assert session["kind"] == "device"
        waiting = wait_for(lambda: (subscription_login.status("codex", account_id) or {}).get("state") == "waiting"
                           and subscription_login.status("codex", account_id))
        assert waiting["url"] == "https://auth.openai.com/codex/device" and waiting["user_code"] == "WXYZ-12345"
        (root / name / "approved").write_text("yes")
        done = wait_for(lambda: (subscription_login.status("codex", account_id) or {}).get("state") in
                        subscription_login.FINAL and subscription_login.status("codex", account_id), 15)
        assert done["state"] == "done", done["message"]
        assert done["plan"] == "plus"


def test_a_denied_codex_sign_in_fails(codex_app):
    app, root = codex_app
    account_id = _account(app, "codex-two", "codex")
    with app.app_context():
        adapter = app.extensions["codex_adapter"]
        name = adapter.create_managed_profile(account_id)
        subscription_login.start(app, adapter, "codex", accounts.get(account_id))
        assert wait_for(lambda: (subscription_login.status("codex", account_id) or {}).get("state") == "waiting")
        (root / name / "denied").write_text("no")
        failed = wait_for(lambda: (subscription_login.status("codex", account_id) or {}).get("state") in
                          subscription_login.FINAL and subscription_login.status("codex", account_id), 15)
        assert failed["state"] == "failed" and "denied" in failed["message"]


# ----- the administration page ------------------------------------------------------------------------------

def test_the_page_shows_the_setup_checklist_and_signs_a_new_account_in(claude_app):
    app, root = claude_app
    admin = _admin(app)
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Setup" in html and "Sign in from this page" in html and str(root) in html
    assert f'value="{NEW_PROFILE}" selected' in html
    response = admin.post("/admin/models/claude/accounts", {"label": "team-one", "profile": NEW_PROFILE,
                                                             "window_limit": "100k"})
    assert response.status_code == 302
    with app.app_context():
        row = accounts.get_by_label("team-one")
    profile = root / f"managed-{row['id']}"
    assert profile.is_dir()
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "data-signin-start" in html and "js/admin-subscriptions.js" in html
    started = admin.post_json(f"/admin/models/claude/accounts/{row['id']}/login", {})
    assert started.status_code == 202, started.get_data(as_text=True)
    status = wait_for(lambda: admin.get(f"/admin/models/claude/accounts/{row['id']}/login").get_json()["login"]
                      ["state"] == "waiting")
    assert status
    submitted = admin.post_json(f"/admin/models/claude/accounts/{row['id']}/login/code", {"code": GOOD_CODE})
    assert submitted.status_code == 200
    assert wait_for(lambda: admin.get(f"/admin/models/claude/accounts/{row['id']}/login").get_json()["login"]
                    ["state"] == "done")
    html = admin.get("/admin/models/claude").get_data(as_text=True)
    assert "Signed in" in html and "Max" in html
    # Signing out uses the CLI's own logout; removing the account deletes its private profile.
    response = admin.post(f"/admin/models/claude/accounts/{row['id']}/logout")
    assert response.status_code == 302
    with app.app_context():
        assert subscription_login.signin_status("claude", row["id"])["signed_in"] is False
    response = admin.post(f"/admin/models/claude/accounts/{row['id']}", {"action": "delete"})
    assert response.status_code == 302
    assert not profile.exists()


def test_starting_a_sign_in_needs_a_recent_confirmation(claude_app):
    from tests.app.test_two_step import _stale

    app, _root = claude_app
    admin = _admin(app)
    account_id = _account(app)
    with app.app_context():
        app.extensions["claude_adapter"].create_managed_profile(account_id)
    _stale(app)
    refused = admin.post_json(f"/admin/models/claude/accounts/{account_id}/login", {})
    assert refused.status_code == 403 and refused.get_json()["error"]["code"] == "reauth_required"
    accepted = admin.post_json(f"/admin/models/claude/accounts/{account_id}/login",
                               {"reauth_secret": "admin-password"})
    assert accepted.status_code == 202
    with app.app_context():
        subscription_login.cancel("claude", account_id)
        wait_for(lambda: (subscription_login.status("claude", account_id) or {}).get("state") in
                 subscription_login.FINAL)


def test_the_checklist_explains_a_disconnected_pool(app):
    admin = _admin(app)
    html = admin.get("/admin/models/codex").get_data(as_text=True)
    assert "Needs attention" in html and "BC_CODEX_EXTENSION is not set" in html
    assert "setup_subscriptions.py" in html
