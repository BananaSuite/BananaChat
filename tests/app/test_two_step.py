"""Two-step verification (authenticator codes and recovery codes), the requirement for administrators,
the administrator reset, and the recent confirmation that high-impact administrator actions ask for."""

from __future__ import annotations

import io
import re
import sqlite3
import tarfile
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.app.conftest import Browser

ADMIN_PASSWORD = "admin-password"


# ----- helpers ------------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    """Authenticator time that tests move on by one 30-second step per code (a code works once)."""
    from bananachat import totp

    now = [1_900_000_000.0]
    monkeypatch.setattr(totp, "time", SimpleNamespace(time=lambda: now[0]))

    def code(secret):
        now[0] += totp.STEP_SECONDS
        return totp.totp_code(secret, now[0])
    return SimpleNamespace(code=code)


def _secret(page: str) -> str:
    return re.search(r'id="two-step-key">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")


def _enable(browser, username, clock, password=None):
    """Turn it on for the signed-in *browser*; returns ``(secret, recovery codes)``."""
    secret = _secret(browser.get("/account/two-step").get_data(as_text=True))
    response = browser.post("/account/two-step", {"action": "enable", "code": clock.code(secret),
                                                  "password": password or f"{username}-password"})
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    codes = re.findall(r"<li>([0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4})</li>", response.get_data(as_text=True))
    assert len(codes) == 10
    return secret, codes


def _english(browser):
    browser.get("/language/en")
    return browser


def _signed_in(browser) -> bool:
    return browser.get("/account").status_code == 200


def _sign_in(app, username, code, password=None, browser=None):
    browser = browser or Browser(app)
    response = browser.post("/login", {"username": username, "password": password or f"{username}-password"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login/two-step")
    return browser, browser.post("/login/two-step", {"code": code})


def _audit_actions(app) -> list[str]:
    from bananachat import db

    with app.app_context():
        return [row["action"] for row in db.query("SELECT action FROM audit_log ORDER BY id")]


def _stale(app, username="admin"):
    """Pretend the account signed in or confirmed more than ten minutes ago."""
    from bananachat import db
    from bananachat.db import users

    with app.app_context():
        db.execute("UPDATE auth_sessions SET verified_at=? WHERE user_id=?",
                   (db.now(-timedelta(minutes=11)), users.get_by_username(username)["id"]))


# ----- codes ----------------------------------------------------------------------------------

def test_authenticator_codes_follow_rfc_6238():
    from bananachat import totp

    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890", the RFC's SHA-1 key
    assert [totp.totp_code(secret, when) for when in (59, 1111111109, 1234567890, 2000000000)] == \
        ["287082", "081804", "005924", "279037"]
    assert totp.matching_counter(secret, "081 804", when=1111111109 + 30) == 1111111109 // 30
    assert totp.matching_counter(secret, "081804", when=1111111109 + 90) is None
    assert totp.matching_counter(secret, "08180x", when=1111111109) is None


def test_recovery_codes_are_long_random_and_forgiving_to_type():
    from bananachat import totp

    codes = totp.generate_recovery_codes()
    assert len(set(codes)) == 10
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}", code) for code in codes)
    code = codes[0]
    typed = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    assert totp.recovery_hash("u1", typed) == totp.recovery_hash("u1", code)
    assert totp.recovery_hash("u2", code) != totp.recovery_hash("u1", code)


# ----- turning it on ---------------------------------------------------------------------------

def test_turning_it_on_takes_the_password_and_a_code_and_shows_recovery_codes_once(app, make_user, clock):
    from bananachat import db

    alice = make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    page = browser.get("/account/two-step").get_data(as_text=True)
    assert 'src="data:image/png;base64,' in page
    secret = _secret(page)
    assert _secret(browser.get("/account/two-step").get_data(as_text=True)) == secret  # stable while scanning

    wrong_password = browser.post("/account/two-step", {"action": "enable", "code": clock.code(secret),
                                                        "password": "nope-nope"})
    assert wrong_password.status_code == 302
    wrong_code = browser.post("/account/two-step", {"action": "enable", "code": "000000",
                                                    "password": "alice-password"})
    assert wrong_code.status_code == 302
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 0

    response = browser.post("/account/two-step", {"action": "enable", "code": clock.code(secret),
                                                  "password": "alice-password"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    codes = re.findall(r"<li>([0-9A-Z-]{19})</li>", response.get_data(as_text=True))
    assert len(codes) == 10
    # Never shown again, and stored only encrypted or as digests.
    later = browser.get("/account/two-step").get_data(as_text=True)
    assert not any(code in later for code in codes) and secret not in later and "two-step-key" not in later
    with app.app_context():
        row = db.one("SELECT * FROM user_two_step WHERE user_id=?", (alice["id"],))
    stored = " ".join(str(value) for value in tuple(row))
    assert secret not in stored and not any(code in stored or code.replace("-", "") in stored for code in codes)
    assert "account.two_step_enable" in _audit_actions(app)
    assert browser.post("/account/two-step", {"action": "enable", "code": clock.code(secret),
                                              "password": "alice-password"}).status_code == 302


def test_turning_it_on_signs_out_the_other_sessions(app, make_user, clock):
    make_user("alice")
    other = Browser(app)
    other.login("alice")
    browser = Browser(app)
    browser.login("alice")
    _enable(browser, "alice", clock)
    assert _signed_in(browser)
    assert not _signed_in(other)


def test_the_set_up_expires(app, make_user, clock, monkeypatch):
    from bananachat.web import account

    make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret = _secret(browser.get("/account/two-step").get_data(as_text=True))
    with browser.client.session_transaction() as session:
        session[account.ENROLMENT_KEY] = {**session[account.ENROLMENT_KEY],
                                          "at": session[account.ENROLMENT_KEY]["at"] - account.ENROLMENT_SECONDS - 1}
    response = _english(browser).post("/account/two-step", {"action": "enable", "code": clock.code(secret),
                                                            "password": "alice-password"}, follow_redirects=True)
    assert "The set-up took too long" in response.get_data(as_text=True)
    assert _secret(response.get_data(as_text=True)) != secret  # a new secret to scan


# ----- signing in -----------------------------------------------------------------------------

def test_sign_in_asks_for_a_code_after_the_password(app, make_user, clock):
    make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret, _ = _enable(browser, "alice", clock)
    browser.logout()

    response = browser.post("/login", {"username": "alice", "password": "alice-password", "next": "/account"})
    assert response.headers["Location"].endswith("/login/two-step")
    assert not _signed_in(browser)
    assert "one-time-code" in browser.get("/login/two-step").get_data(as_text=True)
    assert browser.post("/login/two-step", {"code": "123456"}).status_code == 401
    assert not _signed_in(browser)
    code = clock.code(secret)
    response = browser.post("/login/two-step", {"code": code})
    assert response.status_code == 302 and response.headers["Location"].endswith("/account")
    assert _signed_in(browser)
    # The same code cannot sign in a second time.
    _, replay = _sign_in(app, "alice", code)
    assert replay.status_code == 401


def test_the_code_step_needs_the_password_step_first(app, make_user):
    make_user("alice")
    browser = Browser(app)
    response = browser.post("/login/two-step", {"code": "123456"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")


def test_a_pending_sign_in_lapses_and_follows_password_changes(app, make_user, clock):
    from bananachat import security
    from bananachat.db import users
    from bananachat.web import auth

    alice = make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret, _ = _enable(browser, "alice", clock)
    browser.logout()

    browser.post("/login", {"username": "alice", "password": "alice-password"})
    with browser.client.session_transaction() as session:
        pending = session[auth.TWO_STEP_PENDING_KEY]
        session[auth.TWO_STEP_PENDING_KEY] = {**pending, "at": pending["at"] - auth.TWO_STEP_PENDING_SECONDS - 1}
    response = browser.post("/login/two-step", {"code": clock.code(secret)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")

    browser.post("/login", {"username": "alice", "password": "alice-password"})
    with app.test_request_context():
        users.set_password(alice["id"], security.hash_password("another-password"))
    response = browser.post("/login/two-step", {"code": clock.code(secret)})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert not _signed_in(browser)


def test_a_recovery_code_signs_in_once(app, make_user, clock):
    make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    _, codes = _enable(browser, "alice", clock)
    browser.logout()

    other, response = _sign_in(app, "alice", codes[3].lower(), browser=_english(Browser(app)))
    assert response.status_code == 302
    assert "9 recovery codes are left" in other.get("/account").get_data(as_text=True)
    assert "auth.two_step_recovery_code_used" in _audit_actions(app)
    _, again = _sign_in(app, "alice", codes[3])
    assert again.status_code == 401


def test_wrong_codes_are_limited_but_cannot_lock_out_a_known_browser(app, make_user, clock):
    from bananachat.services import two_step

    make_user("alice")
    owner = Browser(app)
    owner.login("alice")  # this browser now has a device cookie for alice
    secret, _ = _enable(owner, "alice", clock)
    owner.logout()

    stranger = Browser(app)  # knows the password, not the codes
    stranger.post("/login", {"username": "alice", "password": "alice-password"})
    statuses = [stranger.post("/login/two-step", {"code": f"{number:06d}"}).status_code
                for number in range(two_step.FAILURES + 1)]
    assert statuses == [401] * two_step.FAILURES + [429]
    # Even the right code waits now in that browser...
    assert stranger.post("/login/two-step", {"code": clock.code(secret)}).status_code == 429
    # ...but the owner's browser has attempts of its own.
    _, response = _sign_in(app, "alice", clock.code(secret), browser=owner)
    assert response.status_code == 302 and _signed_in(owner)


def test_suspended_accounts_cannot_finish_the_code_step(app, make_user, clock):
    from bananachat.db import users

    alice = make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret, _ = _enable(browser, "alice", clock)
    browser.logout()
    browser.post("/login", {"username": "alice", "password": "alice-password"})
    with app.app_context():
        users.suspend(alice["id"])
    browser.post("/login/two-step", {"code": clock.code(secret)})
    assert not _signed_in(browser)


def test_api_tokens_keep_working(app, make_user, clock):
    from tests.app.test_api import call, roll_out

    roll_out(app)
    make_user("devon")
    browser = Browser(app)
    browser.login("devon")
    raw = browser.post_json("/developer/tokens", {"name": "script"}).json["token"]
    _enable(browser, "devon", clock)
    assert call(app, "GET", "/v1/models", raw).status_code == 200


# ----- managing it --------------------------------------------------------------------------------

def test_new_recovery_codes_replace_the_old_ones(app, make_user, clock):
    make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret, old = _enable(browser, "alice", clock)
    assert browser.post("/account/two-step", {"action": "renew", "code": clock.code(secret),
                                              "password": "wrong-password"}).status_code == 302
    response = browser.post("/account/two-step", {"action": "renew", "code": clock.code(secret),
                                                  "password": "alice-password"})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    new = re.findall(r"<li>([0-9A-Z-]{19})</li>", response.get_data(as_text=True))
    assert len(new) == 10 and not set(new) & set(old)
    browser.logout()
    assert _sign_in(app, "alice", old[0])[1].status_code == 401
    assert _sign_in(app, "alice", new[0])[1].status_code == 302


def test_turning_it_off_takes_the_password_and_a_code(app, make_user, clock):
    from bananachat import db

    make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    secret, codes = _enable(browser, "alice", clock)
    assert browser.post("/account/two-step", {"action": "disable", "code": clock.code(secret),
                                              "password": "wrong-password"}).status_code == 302
    assert browser.post("/account/two-step", {"action": "disable", "code": "000000",
                                              "password": "alice-password"}).status_code == 302
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 1
    assert browser.post("/account/two-step", {"action": "disable", "code": codes[0],
                                              "password": "alice-password"}).status_code == 302
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 0
    assert "account.two_step_disable" in _audit_actions(app)
    browser.logout()
    Browser(app).login("alice")  # the password alone again
    assert browser.login("alice").headers["Location"].endswith("/")


def test_a_new_secret_key_leaves_recovery_codes_working(make_app, clock):
    from bananachat import security
    from bananachat.db import users

    app = make_app()
    with app.test_request_context():
        users.create("alice", security.hash_password("alice-password"))
    browser = Browser(app)
    browser.login("alice")
    secret, codes = _enable(browser, "alice", clock)
    # The same database on a server with another secret key (a moved database export).
    app.config["BC"] = app.config["BC"].replace(secret_key="another-secret-key-that-is-long-enough-9876543210")
    _, response = _sign_in(app, "alice", clock.code(secret))
    assert response.status_code == 401
    other, response = _sign_in(app, "alice", codes[0], browser=_english(Browser(app)))
    assert response.status_code == 302
    assert "can no longer read your authenticator" in other.get("/account/two-step").get_data(as_text=True)
    assert other.post("/account/two-step", {"action": "disable", "code": codes[1],
                                            "password": "alice-password"}).status_code == 302
    assert "two-step-key" in other.get("/account/two-step").get_data(as_text=True)  # set it up again


def test_the_account_page_shows_the_state(app, make_user, clock):
    make_user("alice")
    browser = _english(Browser(app))
    browser.login("alice")
    page = browser.get("/account").get_data(as_text=True)
    assert 'id="two-step"' in page and "Set up two-step verification" in page
    _enable(browser, "alice", clock)
    page = browser.get("/account").get_data(as_text=True)
    assert "Manage two-step verification" in page and "10 recovery codes left" in page
    browser.get("/language/it")
    page = browser.get("/account").get_data(as_text=True)
    assert "Gestisci la verifica in due passaggi" in page and "Rimangono 10 codici di recupero" in page


def test_every_two_step_text_exists_in_both_languages():
    from pathlib import Path

    from bananachat.i18n import catalogs

    root = Path(__file__).resolve().parents[2] / "bananachat"
    sources = [*root.glob("templates/account/*.html"), *root.glob("templates/auth/*.html"),
               root / "web/auth.py", root / "web/account.py", root / "web/admin/two_step.py",
               root / "services/two_step.py"]
    used = set()
    for source in sources:
        used |= set(re.findall(r"""["'](two_step\.[a-z0-9_]+)["']""", source.read_text()))
    tables = catalogs()
    assert {"two_step.required_notice", "two_step.invalid_code", "two_step.too_many"} <= used
    assert not sorted(key for key in used if key not in tables["en"] or key not in tables["it"])


# ----- administrators -----------------------------------------------------------------------------

def test_requiring_it_of_administrators(app, admin, make_user, clock):
    make_user("bob", role="admin")
    make_user("carol")
    settings_form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
                     "admin_two_step_present": "1", "admin_two_step_required": "1",
                     **{f"{prefix}{key}_color": "#123456" for prefix in ("", "light_")
                        for key in ("primary", "secondary", "accent", "text", "sidebar", "bg")}}
    # Only an administrator who uses it may require it.
    page = _english(admin).post("/admin/settings", settings_form, follow_redirects=True).get_data(as_text=True)
    assert "your own account first" in page
    assert not _changed_settings(app)["admin_two_step_required"]
    _enable(admin, "admin", clock, password=ADMIN_PASSWORD)
    admin.post("/admin/settings", settings_form)
    assert _changed_settings(app)["admin_two_step_required"] == 1
    assert "1 active administrator has not set it up yet" in admin.get("/admin/settings").get_data(as_text=True)

    bob = _english(Browser(app))
    response = bob.login("bob")
    assert response.headers["Location"].endswith("/account/two-step")
    assert "requires administrators to use two-step verification" in \
        bob.get("/account/two-step").get_data(as_text=True)
    response = bob.get("/admin/")
    assert response.status_code == 302 and response.headers["Location"].endswith("/account/two-step")
    assert bob.fetch("/admin/users").status_code == 403
    _enable(bob, "bob", clock)
    assert bob.get("/admin/").status_code == 200
    # Everyone else is not affected.
    carol = Browser(app)
    assert carol.login("carol").headers["Location"].endswith("/")


def _changed_settings(app):
    from bananachat.db import settings

    with app.app_context():
        return settings.get()


def test_an_administrator_can_reset_it_for_someone_who_lost_everything(app, admin, make_user, clock):
    from bananachat import db

    alice = make_user("alice")
    browser = Browser(app)
    browser.login("alice")
    _enable(browser, "alice", clock)
    assert "Recovery codes left: 10" in admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)

    _stale(app)
    admin.post(f"/admin/users/{alice['id']}/two-step/reset", {})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 1
    admin.post(f"/admin/users/{alice['id']}/two-step/reset", {"reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 0
    assert not _signed_in(browser)  # every session of the account ended
    assert "admin.user_two_step_reset" in _audit_actions(app)
    assert Browser(app).login("alice").headers["Location"].endswith("/")


def test_administrators_cannot_reset_their_own(app, admin, clock):
    from bananachat import db
    from bananachat.db import users

    _enable(admin, "admin", clock, password=ADMIN_PASSWORD)
    with app.app_context():
        own = users.get_by_username("admin")["id"]
    page = admin.get(f"/admin/users/{own}").get_data(as_text=True)
    assert "Recovery codes left: 10" in page and "Reset two-step verification" not in page
    admin.post(f"/admin/users/{own}/two-step/reset", {"reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM user_two_step", default=0) == 1


# ----- recent confirmation for high-impact administrator actions ---------------------------------

def test_signing_in_counts_as_a_recent_confirmation(app, admin, make_user):
    alice = make_user("alice")
    page = admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)
    assert 'name="reauth_secret"' not in page
    assert admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin"}).status_code == 302
    with app.app_context():
        from bananachat.db import users

        assert users.get(alice["id"])["role"] == "admin"


def test_a_stale_session_must_confirm_before_high_impact_actions(app, admin, make_user):
    from bananachat import db, security
    from bananachat.db import invites, users

    alice = make_user("alice")
    _stale(app)
    page = admin.get(f"/admin/users/{alice['id']}").get_data(as_text=True)
    assert page.count('name="reauth_secret"') == 2  # role and password forms

    export = admin.post("/admin/migration/export", {})
    assert export.status_code == 302 and export.mimetype != "application/gzip"
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin"})
    admin.post(f"/admin/users/{alice['id']}/password", {"password": "taken-over-1", "confirm_password": "taken-over-1"})
    admin.post("/admin/users/create", {"username": "mallory", "password": "mallory-password",
                                       "confirm_password": "mallory-password", "role": "admin"})
    admin.post("/admin/invites/create", {"max_uses": "1", "expires": "1d", "role": "admin"})
    with app.test_request_context():
        assert users.get(alice["id"])["role"] == "user"
        assert security.verify_password(users.get(alice["id"])["password"], "alice-password")
        assert users.get_by_username("mallory") is None
        assert not db.scalar("SELECT COUNT(*) FROM invite_codes WHERE assigned_role='admin'", default=0)
    # Ordinary actions are not affected.
    admin.post("/admin/users/create", {"username": "pupil", "password": "pupil-password",
                                       "confirm_password": "pupil-password", "role": "user"})
    admin.post("/admin/invites/create", {"max_uses": "1", "expires": "1d", "role": "user"})
    with app.app_context():
        assert users.get_by_username("pupil") is not None
        assert len(invites.list_all()[0]) == 1

    # A wrong password is refused and recorded; the right one lets the action through.
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": "guess-guess"})
    assert "admin.reauth_failed" in _audit_actions(app)
    with app.app_context():
        assert users.get(alice["id"])["role"] == "user"
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert users.get(alice["id"])["role"] == "admin"
    assert "admin.reauth" in _audit_actions(app)
    # The confirmation lasts: the export no longer asks.
    export = admin.post("/admin/migration/export", {})
    assert export.status_code == 200 and export.mimetype == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(export.get_data()), mode="r:gz") as archive:
        assert sorted(archive.getnames()) == ["bananachat.db", "export_meta.json"]


def test_an_authenticator_code_also_confirms(app, admin, make_user, clock):
    from bananachat.db import users

    alice = make_user("alice")
    secret, _ = _enable(admin, "admin", clock, password=ADMIN_PASSWORD)
    _stale(app)
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": "000000"})
    with app.app_context():
        assert users.get(alice["id"])["role"] == "user"
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": clock.code(secret)})
    with app.app_context():
        assert users.get(alice["id"])["role"] == "admin"


def test_provider_keys_need_a_recent_confirmation(app, admin):
    from bananachat.db import external_providers

    _stale(app)
    page = admin.get("/admin/models/providers").get_data(as_text=True)
    assert 'name="reauth_secret"' in page and "Needed when you enter an API key" not in page
    assert 'name="reauth_secret"' in admin.get("/admin/migration").get_data(as_text=True)
    form = {"name": "Gateway", "preset": "custom", "base_url": "https://llm.example.com/v1", "protocol": "openai",
            "auth_style": "bearer", "enabled": "1", "api_key": "sk-test-key-123"}
    admin.post("/admin/models/providers", form)
    with app.app_context():
        assert external_providers.list_providers() == []
    admin.post("/admin/models/providers", {**form, "reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        rows = external_providers.list_providers()
    assert len(rows) == 1 and rows[0]["secret_ref"]
    # Changing where a stored key is sent is a credential change too.
    _stale(app)
    admin.post(f"/admin/models/providers/{rows[0]['id']}", {**form, "api_key": "",
                                                            "base_url": "https://collector.example.net/v1"})
    with app.app_context():
        assert external_providers.get(rows[0]["id"])["base_url"] == "https://llm.example.com/v1"


def test_changing_the_requirement_needs_a_recent_confirmation(app, admin, clock):
    _enable(admin, "admin", clock, password=ADMIN_PASSWORD)
    _stale(app)
    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            "admin_two_step_present": "1", "admin_two_step_required": "1",
            **{f"{prefix}{key}_color": "#123456" for prefix in ("", "light_")
               for key in ("primary", "secondary", "accent", "text", "sidebar", "bg")}}
    admin.post("/admin/settings", form)
    assert not _changed_settings(app)["admin_two_step_required"]
    admin.post("/admin/settings", {**form, "reauth_secret": ADMIN_PASSWORD})
    assert _changed_settings(app)["admin_two_step_required"] == 1


def test_wrong_confirmations_are_limited(app, admin, make_user):
    from bananachat.db import users
    from bananachat.web.admin import _reauth

    alice = make_user("alice")
    _stale(app)
    for _ in range(_reauth.FAILURES):
        admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": "guess-guess"})
    admin.post(f"/admin/users/{alice['id']}/role", {"role": "admin", "reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert users.get(alice["id"])["role"] == "user"


# ----- upgrade ------------------------------------------------------------------------------------

def test_the_upgrade_keeps_existing_sign_ins_and_leaves_two_step_off(tmp_path):
    from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
    from sqlite_migrations import apply_migrations

    with closing(sqlite3.connect(tmp_path / "v23.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:23])
        conn.execute("INSERT INTO users (id, username, password, role) VALUES ('u1', 'alice', 'hash', 'admin')")
        conn.execute("INSERT INTO auth_sessions (id_hash, user_id, expires_at) VALUES ('s1', 'u1', '2999-01-01')")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT user_id, verified_at FROM auth_sessions").fetchall() == [("u1", None)]
        assert conn.execute("SELECT admin_two_step_required FROM site_settings").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM user_two_step").fetchone() == (0,)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO user_two_step (user_id, secret_encrypted, enabled_at) VALUES ('u1', 'x', 'now')")
        conn.execute("DELETE FROM users WHERE id='u1'")
        assert conn.execute("SELECT COUNT(*) FROM user_two_step").fetchone() == (0,)
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
