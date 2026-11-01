"""Security regressions: sign-in budgets, confirmations, suspension, account deletion, admin forms, invitations.

Audit findings C1-1 to C1-7 (with C2-3 and C3-6).
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from tests.app.fixtures import Browser
from tests.app.test_agents import (  # noqa: F401  (fixtures)
    agents_app, call, env, fast_loop, runner, set_settings, start_ok, task_row, token_file, wait_status)

FAST_HASH = {"BC_PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000"}


def _device_cookie(app, browser):
    cookie = browser.client.get_cookie(app.session_interface.get_cookie_name(app) + "_device")
    return cookie.value if cookie else None


def _sign_in_again(app, browser, username, password):
    browser.client.delete_cookie(app.session_interface.get_cookie_name(app))
    return browser.post("/login", {"username": username, "password": password})


def _count(app, sql, *params):
    from bananachat import db

    with app.app_context():
        return db.scalar(sql, params, 0)


def _turn_two_step_on(app, user_id):
    from bananachat import totp
    from bananachat.db import users
    from bananachat.services import two_step

    secret = totp.generate_secret()
    with app.test_request_context():
        counter = totp.matching_counter(secret, totp.totp_code(secret))
        assert two_step.enable(user_id, secret, counter)
        users.revoke_sessions(user_id)
    return secret


# ----- C1-1: device cookies collected before two-step verification -------------------------------------

def test_c1_1_device_cookies_collected_before_two_step_do_not_multiply_code_guesses(make_app):
    app = make_app(BC_LOGIN_ATTEMPTS_PER_MINUTE=100000, BC_LOGIN_FAILURES_PER_ADDRESS=100000, **FAST_HASH)
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        user_id = users.create("victim", security.hash_password("victim-password"))
    stock = []
    for _ in range(3):  # the password is known while two-step verification is off
        browser = Browser(app)
        browser.login("victim", "victim-password")
        stock.append(_device_cookie(app, browser))
    _turn_two_step_on(app, user_id)

    def wrong_codes_checked(device):
        browser = Browser(app)
        if device:
            browser.client.set_cookie(app.session_interface.get_cookie_name(app) + "_device", device)
        checked = 0
        for _ in range(11):
            response = _sign_in_again(app, browser, "victim", "victim-password")
            assert "/login/two-step" in response.headers["Location"]
            response = browser.post("/login/two-step", {"code": "000000"})
            if response.status_code == 429:
                break
            checked += response.status_code == 401
        return checked

    assert wrong_codes_checked(None) == 10
    # Before the fix every stockpiled cookie was a fresh budget of 10.
    assert sum(wrong_codes_checked(device) for device in stock) == 0


def test_c1_1_known_browsers_share_a_daily_ceiling_of_wrong_codes(app, make_user, monkeypatch):
    from bananachat.services import two_step

    monkeypatch.setattr(two_step, "FAILURES_PER_ACCOUNT_DAY", 3)
    user = make_user("victim")
    with app.test_request_context():
        granted = [two_step.reserve_code_attempt(user, f"device-{n}") for n in range(5)]
    assert [attempt is not None for attempt in granted] == [True, True, True, False, False]


def test_c1_1_signing_in_again_keeps_the_device_nonce_until_signing_out_everywhere(make_app):
    app = make_app(**FAST_HASH)
    from bananachat.db import users

    browser = Browser(app)
    browser.login("admin", "admin-password")
    first = _device_cookie(app, browser)
    assert _sign_in_again(app, browser, "admin", "admin-password").status_code == 302
    assert _device_cookie(app, browser).split(".")[1] == first.split(".")[1]
    # Signing out everywhere else needs no password, so it keeps device cookies (ZC1-1); a password change
    # retires them.
    browser.post("/account/sessions/revoke-others")
    cookie = f"{app.session_interface.get_cookie_name(app)}_device={first}"
    with app.test_request_context(headers={"Cookie": cookie}):
        from bananachat import security
        assert security.device_cookie_nonce(users.get_by_username("admin")) is not None
    with app.app_context():
        users.revoke_sessions(users.get_by_username("admin")["id"])
    with app.test_request_context(headers={"Cookie": cookie}):
        from bananachat import security
        assert security.device_cookie_nonce(users.get_by_username("admin")) is None


# ----- C1-2: device cookies and per-address sign-in limits, sessions per account ------------------------

def test_c1_2_cycling_device_cookies_stays_within_the_address_limit(make_app):
    app = make_app(BC_LOGIN_ATTEMPTS_PER_MINUTE=2, **FAST_HASH)
    from bananachat import db

    cookies = []
    for _ in range(12):
        browser = Browser(app)
        browser.login("admin", "admin-password")
        cookies.append(_device_cookie(app, browser))
        with app.app_context():  # a new minute for the address, as far as plain sign-ins go
            db.execute("DELETE FROM rate_limit_hits WHERE key LIKE 'login:%'")
    signed_in = refused = 0
    for _ in range(2):
        for cookie in cookies:
            browser = Browser(app)
            browser.client.set_cookie(app.session_interface.get_cookie_name(app) + "_device", cookie)
            status = browser.post("/login", {"username": "admin", "password": "admin-password"}).status_code
            signed_in += status == 302
            refused += status == 429
    # 12 known browsers x 2 per minute each would be 24; the address allows ten times its own limit.
    assert signed_in == 2 * 10 and refused == 4


def test_c1_2_an_account_keeps_a_bounded_number_of_sessions(app, make_user, monkeypatch):
    from bananachat.db import users

    monkeypatch.setattr(users, "MAX_SESSIONS_PER_ACCOUNT", 3)
    user = make_user("mallory")
    with app.app_context():
        tokens = [users.create_session(user["id"], 7) for _ in range(5)]
        live = [token for token in tokens if users.load_session(token)[0] is not None]
    assert live == tokens[-3:]
    assert _count(app, "SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", user["id"]) == 3


# ----- C1-3: password and code confirmations ------------------------------------------------------------

def test_c1_3_password_confirmations_share_one_budget_across_forms(make_app):
    app = make_app(**FAST_HASH)
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        users.create("alice", security.hash_password("alice-password"))
    browser = Browser(app)
    browser.login("alice")
    for _ in range(5):
        browser.post("/account/password", {"current_password": "wrong", "new_password": "a-new-password-1",
                                           "confirm_password": "a-new-password-1"})
    for _ in range(5):
        browser.post("/account/chats/delete-all", {"password": "wrong"})
    # Each form has its own request limit (10 per 15 minutes); the wrong passwords count together.
    browser.post("/account/password", {"current_password": "alice-password", "new_password": "a-new-password-1",
                                       "confirm_password": "a-new-password-1"})
    with app.app_context():
        assert security.verify_password(users.get_by_username("alice")["password"], "alice-password")


def test_c1_3_confirmation_codes_count_in_the_sign_in_daily_budget(make_app):
    app = make_app(**FAST_HASH)
    from bananachat import db, security, totp
    from bananachat.db import users
    from bananachat.services import two_step

    with app.test_request_context():
        target = users.get(users.create("bob", security.hash_password("bob-password")))
    browser = Browser(app)
    browser.login("admin", "admin-password")
    secret = totp.generate_secret()
    with app.app_context():
        admin_id = users.get_by_username("admin")["id"]
        assert two_step.enable(admin_id, secret, totp.matching_counter(secret, totp.totp_code(secret)))
        db.execute("UPDATE auth_sessions SET verified_at=NULL")  # not confirmed recently
    form = {"password": "brand-new-password", "confirm_password": "brand-new-password"}
    assert browser.post(f"/admin/users/{target['id']}/password", {**form, "reauth_secret": "000000"}).status_code == 302
    browser.post(f"/admin/users/{target['id']}/password", {**form, "reauth_secret": "not-the-password"})
    # A wrong code counts like one at sign-in; a wrong password like one on the account page.
    assert _count(app, "SELECT COUNT(*) FROM rate_limit_hits WHERE key=?", f"two-step-fail-day:{admin_id}") == 1
    assert _count(app, "SELECT COUNT(*) FROM rate_limit_hits WHERE key=?", f"confirm-fail-day:{admin_id}") == 2


# ----- C1-4 / C2-3: suspension stops work in progress -----------------------------------------------------

@pytest.fixture
def quick_supervisor(monkeypatch):
    from bananachat.services import supervisor

    monkeypatch.setattr(supervisor, "TICK", 0.1)
    monkeypatch.setattr(supervisor, "LEASE_INTERVAL", 0.2)


def test_c1_4_suspending_an_account_stops_its_chat_answer(app, make_user, fake_ollama, quick_supervisor):
    from bananachat.db import users
    from tests.app.test_chat import read_events
    from tests.app.test_run_lifecycle import _streaming

    _, _, response = _streaming(app, make_user, fake_ollama, "mallory", incognito=False)
    with app.app_context():
        users.suspend(users.get_by_username("mallory")["id"])
    events = read_events(response)
    assert events[-1]["type"] == "done" and events[-1]["state"] == "stopped", events[-1]


def test_c1_4_suspending_an_account_stops_its_api_answer(app, make_user, fake_ollama, quick_supervisor):
    from bananachat.db import users
    from tests.app.test_completion_leases import Client, _user

    user = _user(app, make_user, fake_ollama, delay=0.01)
    client = Client(app, user)
    client.start()
    client.wait_for(100)
    with app.app_context():
        users.suspend(user["id"])
    client.join(5)
    assert not client.is_alive()
    assert [error.code for error in client.errors] == ["account_suspended"]


def test_c1_4_suspending_an_account_stops_its_agent_tasks_and_images(app, make_user):
    from bananachat.db import agents as agents_db
    from bananachat.db import users
    from bananachat.services.images import ImageJob

    user = make_user("mallory")
    job = ImageJob.__new__(ImageJob)
    job.user, job.token_id = user, None
    with app.app_context():
        agents_db.create("a" * 32, user_id=user["id"], title="Task", prompt="Do it", model_id=None,
                         model_name="m", swarm=False, owner_token="lease", max_user=None, max_site=10)
        assert job._account_check(True) is None
        users.suspend(user["id"])
        assert agents_db.lease_check("a" * 32, "lease", renew=True) == "stopped by admin"
        assert job._account_check(True) == "account suspended"


# ----- C1-5 / C3-6: account deletion leaves no agent workspace behind -----------------------------------

def test_c1_5_deleting_the_account_deletes_its_sandboxes(env, runner, fake_ollama):
    fake_ollama.tool_script = [call("bash", command="nohup sleep 9999 &"), call("finish", summary="done")]
    task_id = start_ok(env.browser)
    sandbox_id = wait_status(env.app, task_id, "finished")["sandbox_id"]
    assert sandbox_id in runner.sandboxes
    response = env.browser.post("/account/delete", {"confirm_username": "alice", "password": "alice-password"})
    assert response.status_code == 302
    assert task_row(env.app, task_id) is None
    assert sandbox_id not in runner.sandboxes and sandbox_id in runner.deleted


def test_c1_5_deleting_the_account_deletes_its_pending_uploads(env, runner, monkeypatch):
    from bananachat.services.agents import gitfetch

    set_settings(env.app, git_enabled=True)

    def failing_download(*args, **kwargs):
        raise gitfetch.FetchError("agents.import_not_found")
    monkeypatch.setattr(gitfetch, "download", failing_download)
    response = env.browser.fetch("/agents", method="POST", data={
        "prompt": "Look", "repo_url": "https://github.com/octo/demo",
        "files": [(io.BytesIO(b"SECRET customer data"), "customers.csv")]}, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_data(as_text=True)
    task_id = response.get_json()["id"]
    wait_status(env.app, task_id, "failed")
    folder = env.app.config["BC"].instance_dir / "agent-uploads" / task_id
    assert (folder / "00.bin").exists()
    response = env.browser.post("/account/delete", {"confirm_username": "alice", "password": "alice-password"})
    assert response.status_code == 302
    assert not folder.exists()


def test_c1_5_cleanup_sweeps_leftovers_of_deleted_tasks(env, runner, monkeypatch):
    from bananachat.services.agents import runner as runner_mod
    from bananachat.services.agents import service

    orphan = "b" * 32
    folder = env.app.config["BC"].instance_dir / "agent-uploads" / orphan
    folder.mkdir(parents=True)
    (folder / "00.bin").write_bytes(b"left behind")
    monkeypatch.setattr(service, "UNKNOWN_SANDBOX_GRACE", timedelta(seconds=-60))
    with env.app.app_context():
        sandbox_id = runner_mod.client().create(orphan)["id"]
        service.purge_old_tasks()
        assert service.reap_sandboxes() == 1
    assert not folder.exists()
    assert sandbox_id in runner.deleted


def test_c1_5_an_administrator_deleting_an_account_removes_its_background(app, admin, make_user):
    from bananachat.db import users

    user = make_user("bob")
    name = "c" * 32 + ".jpg"
    path = app.config["BC"].background_upload_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpeg")
    with app.app_context():
        users.save_preferences(user["id"], {"background_image": name})
    response = admin.post(f"/admin/users/{user['id']}/delete", {"confirm_username": "bob"})
    assert response.status_code == 302
    assert not path.exists()


# ----- C1-6: dates at the edge of the calendar -----------------------------------------------------------

def test_c1_6_edge_of_calendar_dates_are_refused_without_an_error(admin, make_user):
    user = make_user("bob")
    response = admin.post(f"/admin/users/{user['id']}/suspend", {"until": "9999-12-31T23:59-23:00"},
                          follow_redirects=True)
    assert response.status_code == 200 and "out of range" in response.get_data(as_text=True)
    response = admin.post("/admin/invites/create", {"max_uses": "1", "role": "user", "expires": "custom",
                                                    "expires_at": "0001-01-01T00:00+01:00"}, follow_redirects=True)
    assert response.status_code == 200 and "out of range" in response.get_data(as_text=True)


# ----- C1-7: invitations carry their creator's authority -------------------------------------------------

def test_c1_7_user_invitations_stop_with_their_creators_administrator_role(app, make_user):
    from bananachat import db
    from bananachat.db import invites, users

    creator = make_user("rogue", role="admin")
    with app.app_context():
        code = invites.create(creator["id"], max_uses=0, assigned_role="user")
        assert invites.find_usable(code) is not None
        users.set_role(creator["id"], "user")
        assert invites.find_usable(code) is None
        users.set_role(creator["id"], "admin")
        # A timed suspension that has ended (not lifted yet) no longer holds the invitation back.
        lapsed = db.timestamp(datetime.now(timezone.utc) - timedelta(hours=1))
        db.execute("UPDATE users SET suspended=1, suspended_until=? WHERE id=?", (lapsed, creator["id"]))
        assert invites.find_usable(code) is not None
        users.delete(creator["id"])
        assert invites.find_usable(code) is None
