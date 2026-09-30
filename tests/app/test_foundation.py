"""Configuration, storage, upgrades, authentication and the inference core."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from tests.app.conftest import TEST_CSRF, Browser, csrf_from

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_v4.sql"
LEGACY_TOKEN = "bc-emci_AwPxusYFfYl15DHrI1GlcsgJeSOMkBJJYYYfmZLXDv4tuqhqg"


def legacy_instance(tmp_path):
    """An instance directory holding a database written by the previous release."""
    instance = tmp_path / "legacy"
    instance.mkdir()
    database = instance / "bananachat.db"
    connection = sqlite3.connect(database)
    connection.executescript(FIXTURE.read_text())
    connection.close()
    Path(str(database) + ".initialized").write_text("BananaSuite SQLite initialized\n")
    return instance


# ----- configuration ------------------------------------------------------------

def test_invalid_values_fall_back_with_warnings(tmp_path):
    from bananachat.config import load_config

    config = load_config({"BC_INSTANCE_DIR": str(tmp_path), "BC_SECRET_KEY": "x" * 40, "BC_PORT": "eighty",
                          "BC_PROXY_MODE": "off", "BC_HTTP_THREADS": "500", "BC_IMAGE_BACKEND": "magic"})
    assert config.port == 8000
    assert config.proxy_mode is False  # "off" used to switch proxy mode on
    assert config.http_threads == 64
    assert config.image_backend == "disabled"
    assert len(config.warnings) == 3


def test_secret_key_file_is_created_private_and_reused(tmp_path):
    from bananachat.config import load_config

    first = load_config({"BC_INSTANCE_DIR": str(tmp_path)})
    second = load_config({"BC_INSTANCE_DIR": str(tmp_path)})
    key_file = tmp_path / ".secret_key"
    assert first.secret_key == second.secret_key == key_file.read_text()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert first.setup_token == second.setup_token and len(first.setup_token) == 64


# ----- storage and upgrades -------------------------------------------------------

def test_fresh_database_is_at_the_latest_version(app):
    from bananachat import db

    with app.app_context():
        assert db.schema_version() == db.SCHEMA_VERSION
        assert db.scalar("PRAGMA application_id") == db.APPLICATION_ID
        assert db.scalar("PRAGMA foreign_keys") == 1


def test_previous_release_database_upgrades_in_place(tmp_path, make_app):
    from bananachat import db

    app = make_app(setup=False, INSTANCE_DIR=str(legacy_instance(tmp_path)))
    with app.app_context():
        assert db.schema_version() == db.SCHEMA_VERSION
        settings = db.one("SELECT * FROM site_settings")
        assert settings["site_name"] == "BananaChat"  # was "BananaAI"
        assert settings["chat_rpm"] == 20 and settings["warning_banner_message"] == "Heads up"
        assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE created_at LIKE '%T%'") == 0
        indexes = {row["name"] for row in db.query("PRAGMA index_list(credit_ledger)")}
        assert "idx_credit_ledger_user_date" in indexes
        model = db.one("SELECT * FROM ai_models WHERE ollama_name='llama3.2:3b'")
        assert model["display_name"] == "Company Assistant" and model["is_rolled_out"] == 1

    browser = Browser(app)
    browser.login("admin", "correct horse battery")
    page = browser.get("/")
    assert page.status_code == 302
    alice = Browser(app)
    alice.login("alice", "alice-password-1")


def test_custom_site_names_are_not_renamed(tmp_path, make_app):
    instance = legacy_instance(tmp_path)
    connection = sqlite3.connect(instance / "bananachat.db")
    connection.execute("UPDATE site_settings SET site_name='Banana AI Lab'")
    connection.commit()
    connection.close()
    from bananachat import db

    app = make_app(setup=False, INSTANCE_DIR=str(instance))
    with app.app_context():
        assert db.scalar("SELECT site_name FROM site_settings") == "Banana AI Lab"


@pytest.mark.parametrize("legacy_name", ["BananaAI", "banana ai", " Banana-AI "])
def test_every_spelling_of_the_former_name_is_renamed(tmp_path, make_app, legacy_name):
    instance = legacy_instance(tmp_path)
    connection = sqlite3.connect(instance / "bananachat.db")
    connection.execute("UPDATE site_settings SET site_name=?", (legacy_name,))
    connection.commit()
    connection.close()
    from bananachat import db

    app = make_app(setup=False, INSTANCE_DIR=str(instance))
    with app.app_context():
        assert db.scalar("SELECT site_name FROM site_settings") == "BananaChat"


def test_newer_database_is_refused(tmp_path, make_app):
    from bananachat import db

    instance = legacy_instance(tmp_path)
    connection = sqlite3.connect(instance / "bananachat.db")
    connection.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(sqlite3.OperationalError):
        make_app(setup=False, INSTANCE_DIR=str(instance))


def test_missing_database_is_never_silently_recreated(tmp_path, make_app):
    from bananachat import db

    app = make_app()
    config = app.config["BC"]
    Path(config.database_path).unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(config.database_path) + suffix).unlink(missing_ok=True)
    db.close_thread_connection()
    response = app.test_client().get("/health")
    assert response.status_code == 503


# ----- pages and security ---------------------------------------------------------

def test_health_is_cheap_and_open(app):
    response = app.test_client().get("/health")
    assert response.status_code == 200 and response.json["status"] == "ok"
    assert app.test_client().get("/healthz").status_code == 200


def test_setup_requires_the_installation_token(make_app):
    app = make_app(setup=False)
    browser = Browser(app)
    assert browser.get("/").headers["Location"].endswith("/setup")
    form = {"username": "owner", "password": "password123", "confirm_password": "password123"}
    assert browser.post("/setup", {**form, "setup_token": "wrong"}).status_code == 403
    response = browser.post("/setup", {**form, "setup_token": app.config["BC"].setup_token})
    assert response.status_code == 302
    assert browser.post("/setup", {**form, "setup_token": app.config["BC"].setup_token}).status_code == 302
    from bananachat import db
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM users") == 1


def test_security_headers_and_csp(client):
    response = client.get("/login")
    policy = response.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in policy and "script-src 'self' 'nonce-" in policy
    assert response.headers["X-Frame-Options"] == "DENY"
    assert 'style="' not in response.get_data(as_text=True)


def test_forms_without_csrf_token_are_rejected(app):
    client = app.test_client()
    response = client.post("/login", data={"username": "admin", "password": "admin-password"})
    assert response.status_code == 400
    page = client.get("/login").get_data(as_text=True)
    response = client.post("/login", data={"username": "admin", "password": "admin-password",
                                           "csrf_token": csrf_from(page)})
    assert response.status_code == 302


def test_error_messages_follow_the_page_language_but_not_for_api_clients(app):
    client = app.test_client()
    client.get("/language/it")
    page = client.post("/login", data={"username": "admin"})
    assert page.status_code == 400 and "Il modulo è scaduto" in page.get_data(as_text=True)
    fetched = client.post("/login", headers={"Accept": "application/json", "X-Requested-With": "fetch"})
    assert "Il modulo è scaduto" in fetched.json["error"]["message"]
    missing = client.get("/nope", headers={"Accept": "application/json", "X-Requested-With": "fetch"})
    assert missing.status_code == 404 and missing.json["error"]["message"] == \
        "Questa pagina non esiste o non è più disponibile."
    api = client.get("/v1/nope", headers={"Accept-Language": "it"})
    assert api.status_code == 404 and "Unknown endpoint" in api.json["error"]["message"]
    cross_site = client.post("/login", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403 and "un altro sito" in cross_site.get_data(as_text=True)


def test_login_failures_are_throttled_per_account(client):
    for _ in range(10):
        assert client.post("/login", {"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/login", {"username": "admin", "password": "admin-password"}).status_code == 429


def test_open_redirects_are_refused(client):
    response = client.post("/login", {"username": "admin", "password": "admin-password",
                                      "next": "//evil.example/"})
    assert response.headers["Location"] == "/"


def test_logout_ends_the_server_side_session(app, client):
    client.login("admin", "admin-password")
    with client.client.session_transaction() as session:
        stolen = dict(session)
    client.logout()
    thief = Browser(app)
    with thief.client.session_transaction() as session:
        session.update(stolen)
    response = thief.fetch("/account")
    assert response.status_code == 401


def test_cookies_from_the_previous_release_keep_working_once(app):
    from bananachat.db import users

    with app.app_context():
        admin = users.get_by_username("admin")
    browser = Browser(app)
    with browser.client.session_transaction() as session:
        session["user_id"] = admin["id"]
        session["session_version"] = admin["session_version"]
    assert browser.get("/account").status_code == 200
    with browser.client.session_transaction() as session:
        assert "sid" in session and "user_id" not in session


def test_previous_release_cookies_without_a_session_version_count_as_version_zero(app):
    """The old setup page signed the first administrator in without storing a version."""
    from bananachat import db
    from bananachat.db import users

    with app.app_context():
        admin = users.get_by_username("admin")
    browser = Browser(app)
    with browser.client.session_transaction() as session:
        session["user_id"] = admin["id"]
    assert browser.get("/account").status_code == 200

    with app.app_context():
        db.execute("UPDATE users SET session_version=1 WHERE id=?", (admin["id"],))
    revoked = Browser(app)
    with revoked.client.session_transaction() as session:
        session["user_id"] = admin["id"]
    assert revoked.fetch("/account").status_code == 401


def test_password_change_revokes_other_sessions(app, make_user):
    from bananachat import security
    from bananachat.db import users

    user = make_user("bob")
    laptop, phone = Browser(app), Browser(app)
    laptop.login("bob")
    phone.login("bob")
    with app.test_request_context():
        users.set_password(user["id"], security.hash_password("new-password-1"))
    assert phone.fetch("/account").status_code == 401


def test_suspended_accounts_are_signed_out(app, make_user):
    from bananachat.db import users

    user = make_user("carol")
    browser = Browser(app)
    browser.login("carol")
    with app.app_context():
        users.suspend(user["id"])
    assert browser.fetch("/account").status_code == 401
    assert browser.post("/login", {"username": "carol", "password": "carol-password"}).status_code == 403


def test_last_admin_cannot_be_removed(app):
    from bananachat.db import users

    with app.app_context():
        admin = users.get_by_username("admin")
        with pytest.raises(ValueError):
            users.delete(admin["id"])
        with pytest.raises(ValueError):
            users.set_role(admin["id"], "user")


def test_maintenance_mode_keeps_the_site_up_and_pauses_sending(app, make_user):
    from bananachat.db import settings
    from bananachat.services import status

    make_user("dave")
    with app.app_context():
        settings.update(maintenance_mode=1, maintenance_message="Back at 14:00")
    anonymous = app.test_client()
    assert anonymous.get("/").status_code == 302
    login_page = anonymous.get("/login", headers={"Accept-Language": "en"})
    assert login_page.status_code == 200
    assert "Maintenance in progress" in login_page.get_data(as_text=True)
    user = Browser(app)
    user.login("dave")
    page = user.get("/account", headers={"Accept-Language": "en"})
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "Back at 14:00" in text and 'data-kind="maintenance"' in text
    with app.test_request_context():
        from bananachat.db import users
        dave = users.get_by_username("dave")
        admin = users.get_by_username("admin")
        refused = status.guard(dave)
        assert refused is not None and refused.status_code == 503 and refused.headers["Retry-After"]
        assert status.guard(admin) is None
    report = anonymous.get("/status")
    assert report.status_code == 200
    assert report.json["status"] == "maintenance" and report.json["accepting_requests"] is False
    assert app.test_client().get("/health").status_code == 200


def test_an_unreachable_ai_server_shows_a_banner_instead_of_an_error(make_app, make_user):
    from bananachat.services import health, status

    app = make_app(OLLAMA_URL="http://10.255.255.1:11434")
    make_user("erin2")
    with app.app_context():
        health.record_probe(False, 1, "timed out")
    user = Browser(app)
    user.login("erin2")
    page = user.get("/account", headers={"Accept-Language": "en"})
    assert page.status_code == 200 and "The AI server is unreachable" in page.get_data(as_text=True)
    report = user.fetch("/status?banner=1")
    assert report.status_code == 200 and report.json["status"] == "outage"
    assert report.json["can_send"] is False and 'data-kind="outage"' in report.json["banner_html"]
    with app.test_request_context():
        refused = status.guard(None, openai=True)
        assert refused.status_code == 503 and refused.json["error"]["code"] == "outage"
        health.record_probe(True, 1)
    health.reset()
    assert user.fetch("/status").json["status"] == "ok"


def test_the_announcement_banner_can_be_dismissible(app):
    from bananachat.db import settings

    with app.app_context():
        settings.update(warning_banner_enabled=1, warning_banner_message="Slow today", warning_banner_dismissible=1)
    text = app.test_client().get("/login").get_data(as_text=True)
    assert "Slow today" in text and "data-dismiss-status" in text


def test_update_page_while_the_lifecycle_tool_works(tmp_path):
    from bananachat.update_gate import UpdateGate

    marker = tmp_path / "maintenance"
    calls = []

    def inner(environ, start_response):
        calls.append(environ["PATH_INFO"])
        start_response("200 OK", [])
        return [b"app"]

    gate = UpdateGate(inner, str(marker))

    def call(path, **headers):
        result = {}
        body = b"".join(gate({"PATH_INFO": path, **headers}, lambda status, headers: result.update(status=status)))
        return result["status"], body

    assert call("/login") == ("200 OK", b"app")
    marker.write_text("updating")
    status, body = call("/login")
    assert status.startswith("503") and b"right back" in body and b"Torniamo subito" in body
    assert call("/status")[0] == "200 OK"
    assert call("/health") == ("200 OK", b"app")
    assert b'"updating"' in call("/v1/models")[1]


def test_language_negotiation_and_switch(app):
    client = app.test_client()
    assert 'lang="en"' in client.get("/login", headers={"Accept-Language": "en-GB,en;q=0.9"}).get_data(as_text=True)
    assert 'lang="it"' in client.get("/login", headers={"Accept-Language": "de"}).get_data(as_text=True)
    client.get("/language/en")
    assert 'lang="en"' in client.get("/login").get_data(as_text=True)


def test_translation_catalogs_are_complete():
    from bananachat.i18n import catalogs

    tables = catalogs()
    assert set(tables["en"]) == set(tables["it"])
    for language in tables:
        for key, text in tables[language].items():
            assert text, f"{language}:{key} is empty"


# ----- inference core -------------------------------------------------------------

def _generate(app, fake_ollama, **overrides):
    from bananachat.db import catalog, users
    from bananachat.services import inference, ollama
    from bananachat.services.access import AccessContext
    from bananachat.services.upstream import CancelToken

    cancel = overrides.pop("cancel", None) or CancelToken()
    with app.app_context():
        ollama.sync_catalog()
        for model in catalog.list_models():  # reviewed: models waiting for review are never chosen automatically
            catalog.set_rollout(model["id"], True)
        admin = users.get_by_username("admin")
        selection = inference.select_model(AccessContext.load(admin), overrides.pop("requested", "auto"),
                                           surface="chat")
        request = inference.TextRequest(user=admin, model=selection.model, fallbacks=selection.fallbacks,
                                        messages=[{"role": "user", "content": "Hi"}],
                                        options=inference.build_options(selection.model), **overrides)
        return list(inference.generate(request, cancel)), catalog


def test_generation_streams_and_reports_usage(app, fake_ollama):
    events, _ = _generate(app, fake_ollama)
    from bananachat.services.inference import Delta, Finished, Started

    assert isinstance(events[0], Started)
    text = "".join(event.text for event in events if isinstance(event, Delta))
    assert text == "Hello from the fake model."
    finished = events[-1]
    assert isinstance(finished, Finished) and finished.state == "completed"
    assert (finished.prompt_tokens, finished.completion_tokens, finished.usage_estimated) == (11, 5, False)
    body = fake_ollama.chat_bodies()[-1]
    assert body["options"]["num_predict"] == app.config["BC"].max_output_tokens


def test_generation_falls_back_before_any_output(app, fake_ollama):
    from bananachat.services.inference import Finished, Started

    fake_ollama.fail_models = {"llama3.2:3b"}
    events, _ = _generate(app, fake_ollama)
    started = [event for event in events if isinstance(event, Started)]
    assert [event.model["ollama_name"] for event in started] == ["llama3.2:3b", "qwen3:4b"]
    assert started[1].notice
    assert isinstance(events[-1], Finished) and events[-1].state == "completed"


def test_usage_is_estimated_when_the_backend_reports_none(app, fake_ollama):
    fake_ollama.omit_usage = True
    events, _ = _generate(app, fake_ollama)
    assert events[-1].usage_estimated and events[-1].completion_tokens > 0


def test_responses_are_cut_at_the_size_limit(app, fake_ollama):
    fake_ollama.reply = "word " * 400
    events, _ = _generate(app, fake_ollama, max_response_bytes=100)
    finished = events[-1]
    assert finished.state == "stopped" and finished.truncated and finished.finish_reason == "length"


def test_stop_cancels_a_running_generation(app, fake_ollama):
    from bananachat.services.inference import Delta
    from bananachat.services.upstream import CancelToken

    fake_ollama.reply = " ".join(["token"] * 200)
    fake_ollama.chunk_delay = 0.02
    cancel = CancelToken()
    threading.Timer(0.3, cancel.cancel, args=("stopped",)).start()
    events, _ = _generate(app, fake_ollama, cancel=cancel)
    assert events[-1].state == "stopped"
    assert 0 < sum(isinstance(event, Delta) for event in events) < 200


def test_user_options_are_clamped(app):
    from bananachat.db import catalog
    from bananachat.services import inference

    with app.app_context():
        catalog.sync_ollama([{"name": "m1"}])
        model = catalog.get_by_name("m1")
        options = inference.build_options(model, {"num_ctx": 1_000_000, "temperature": 9, "top_k": "40"},
                                          max_tokens=50)
    assert options["num_ctx"] == app.config["BC"].max_num_ctx
    assert "temperature" not in options and options["top_k"] == 40 and options["num_predict"] == 50


def test_queue_admits_by_priority_and_limits_owners(app):
    from bananachat.services import queue

    with app.test_request_context():
        first = queue.Slot(queue.PRIORITY_CHAT, owner_key="u1").__enter__()
        first.wait(timeout=2)
        for _ in range(2):
            queue.Slot(queue.PRIORITY_CHAT, owner_key="u1").__enter__()
        with pytest.raises(queue.QueueFull):
            queue.Slot(queue.PRIORITY_CHAT, owner_key="u1").__enter__()
        second = queue.Slot(queue.PRIORITY_CHAT, owner_key="u1").__class__(queue.PRIORITY_API, owner_key="u2")
        with second:
            second.wait(timeout=2)
            assert second.running
        first.release()


def test_sync_keeps_administrator_edits(app):
    from bananachat.db import catalog

    with app.app_context():
        catalog.sync_ollama([{"name": "m1", "description": "3B"}])
        model = catalog.get_by_name("m1")
        catalog.update(model["id"], display_name="Company Assistant")
        catalog.set_rollout(model["id"], True)
        catalog.sync_ollama([{"name": "m1", "description": "3B"}])
        model = catalog.get_by_name("m1")
        assert model["display_name"] == "Company Assistant" and model["is_rolled_out"] == 1
        catalog.sync_ollama([])
        assert catalog.get_by_name("m1")["backend_available"] == 0


def test_approving_access_replaces_an_expired_allowlist_entry(app, make_user):
    from bananachat.db import access

    user = make_user("erin")
    with app.app_context():
        access.add_membership("uncensored", 0, user["id"], "allowlist", added_by=None, expires_at="2000-01-01 00:00:00")
        request_id = access.create_request("uncensored", 0, user["id"], "Research on moderation.",
                                           confirmed_safe=True, confirmed_logging=True)
        access.resolve_request(request_id, None, True)
        assert "allowlist" in access.user_memberships(user["id"])[("uncensored", 0)]


def test_music_bonus_accepts_zero(app, make_user):
    from bananachat.db import credits, settings

    user = make_user("fay")
    with app.app_context():
        settings.update(music_enabled=1, music_bonus_mode="fixed", music_bonus_fixed_tokens=0,
                        music_bonus_fixed_slow_tokens=0)
        from bananachat import db
        db.execute("UPDATE users SET music_opted_in=1 WHERE id=?", (user["id"],))
        assert credits.music_bonus(user["id"]) == ("fixed", 0.0, 0.0)


def test_usage_counts_the_open_window_only(app, make_user):
    from bananachat import db
    from bananachat.db import credits

    user = make_user("gus")
    with app.app_context():
        credits.charge(user["id"], 1000, 1000, request_type="api")
        db.execute("INSERT INTO credit_ledger (user_id, credits_used, request_type, created_at) "
                   "VALUES (?, 50, 'api', '2000-01-01 10:00:00')", (user["id"],))
        budget = credits.budget(users_row(user), "api")
        assert budget.regular_used == 2000


def users_row(user):
    from bananachat.db import users
    return users.get(user["id"])


def test_csrf_header_is_accepted_for_fetch(client):
    client.login("admin", "admin-password")
    client._ensure_csrf()
    response = client.client.post("/logout", headers={"X-CSRF-Token": TEST_CSRF})
    assert response.status_code == 302
