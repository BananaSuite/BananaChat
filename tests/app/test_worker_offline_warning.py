"""Hiding a compute warning changes presentation, never availability or authorization."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bananachat import db
from bananachat.db import access, catalog, claude_pool as accounts, settings
from bananachat.services import claude_pool, health, status
from tests.app.fixtures import Browser
from tests.app.test_api import assert_error, chat, ledger, token_for
from tests.app.test_chat import new_chat, parse_sse, send, setup_models

CLAUDE_MODEL = "warning-test-sonnet"
LOCAL_MODEL = "llama3.2:3b"


@pytest.fixture(autouse=True)
def isolated_transport():
    claude_pool.reset_transport()
    yield
    claude_pool.reset_transport()


def workers_down(app, monkeypatch, *, mode="shutdown"):
    app.config["BC"] = replace(app.config["BC"], inference_local=False, inference_outage_mode=mode,
                               inference_fallback_url="https://backup.example.test" if mode == "fallback" else "")
    monkeypatch.setattr(health, "inference_down", lambda *args: True)
    monkeypatch.setattr(health, "status", lambda: {"since": 1_700_000_000.0})


def warning_enabled(app, enabled):
    with app.app_context():
        settings.update(worker_offline_warning_enabled=int(enabled))


def register_claude(app, *, published=True):
    calls = []

    def answer(account, model, messages, options, *, cancel):
        cancel.check()
        calls.append((account["id"], model))
        yield {"text": "Claude answered without local workers.", "tokens_in": 3, "tokens_out": 4, "done": True}

    claude_pool.register_site_chat(answer)
    claude_pool.register_site_discovery(lambda: [{"name": CLAUDE_MODEL, "family": "sonnet", "reasoning": [],
                                                 "capabilities": ["completion"]}])
    with app.app_context():
        claude_pool.sync_catalog(selected=[CLAUDE_MODEL], source="admin")
        model = catalog.get_by_name(CLAUDE_MODEL)
        catalog.set_rollout(model["id"], published)
        account_id = accounts.add_account("synthetic-warning-account", window_limit=1_000_000)
    return model, account_id, calls


def signed_in(app, make_user, username="warning-user"):
    user = make_user(username)
    browser = Browser(app)
    browser.login(username)
    return user, browser


def test_warning_is_shown_by_default_and_does_not_take_the_site_down(app, make_user, monkeypatch):
    workers_down(app, monkeypatch)
    _, browser = signed_in(app, make_user)
    with app.app_context():
        assert settings.get()["worker_offline_warning_enabled"] == 1
    report = browser.fetch("/status?banner=1")
    assert report.status_code == 200
    assert report.headers["Cache-Control"] == "no-store"
    assert report.json["notices"][0]["visible"] is True
    assert 'data-kind="outage"' in report.json["banner_html"]
    assert browser.get("/account").status_code == 200
    assert browser.get("/health").json == {"status": "ok", "database": "ok"}


@pytest.mark.parametrize("kind,overall,can_send", [
    ("outage", "outage", False),
    ("local_outage", "degraded", True),
    ("fallback", "degraded", True),
])
@pytest.mark.parametrize("enabled", [False, True])
def test_global_warning_setting_changes_only_rendered_worker_notice(
        app, make_user, monkeypatch, kind, overall, can_send, enabled):
    workers_down(app, monkeypatch, mode="fallback" if kind == "fallback" else "shutdown")
    if kind == "local_outage":
        register_claude(app)
    warning_enabled(app, enabled)
    user, browser = signed_in(app, make_user)
    report = browser.fetch("/status?banner=1").json
    assert report["status"] == overall
    assert report["accepting_requests"] is can_send
    assert report["can_send"] is can_send
    assert len(report["notices"]) == 1
    notice = report["notices"][0]
    assert notice["kind"] == kind and notice["visible"] is enabled
    assert notice["blocks_inference"] is (not can_send)
    assert (f'data-kind="{kind}"' in report["banner_html"]) is enabled
    assert (f'data-kind="{kind}"' in browser.get("/account").get_data(as_text=True)) is enabled
    anonymous = app.test_client().get("/status").json
    assert anonymous["status"] == overall and anonymous["accepting_requests"] is can_send
    assert "notices" not in anonymous and "can_send" not in anonymous
    with app.test_request_context():
        assert (status.guard(user) is None) is can_send


@pytest.mark.parametrize("role", ["user", "admin"])
def test_hidden_warning_does_not_allow_a_local_chat_or_api_generation(app, make_user, monkeypatch, fake_ollama, role):
    setup_models(app)
    workers_down(app, monkeypatch)
    warning_enabled(app, False)
    if role == "admin":
        from bananachat.db import users
        with app.app_context():
            user = users.get_by_username("admin")
        browser = Browser(app)
        browser.login("admin")
    else:
        user, browser = signed_in(app, make_user)
    session_id = new_chat(browser)
    refused = send(browser, session_id, model=LOCAL_MODEL)
    assert refused.status_code == 503 and refused.json["error"]["code"] == "outage"
    assert refused.headers["Retry-After"] == "60"
    assert refused.headers["Cache-Control"] == "no-store"
    api_refused = chat(app, token_for(app, user), model=LOCAL_MODEL)
    assert_error(api_refused, 503, "server_error", "outage")
    assert fake_ollama.chat_bodies() == []
    assert ledger(app, user) == []
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM chat_messages") == 0
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("surface", ["chat", "api"])
def test_published_claude_answers_with_workers_off_regardless_of_warning_visibility(
        app, make_user, monkeypatch, fake_ollama, enabled, surface):
    workers_down(app, monkeypatch)
    model, account_id, calls = register_claude(app)
    warning_enabled(app, enabled)
    user, browser = signed_in(app, make_user)
    report = browser.fetch("/status?banner=1").json
    assert report["status"] == "degraded" and report["can_send"] is True
    assert report["notices"][0]["kind"] == "local_outage"
    assert report["notices"][0]["visible"] is enabled
    if surface == "chat":
        response = send(browser, new_chat(browser), model=CLAUDE_MODEL)
        assert response.status_code == 200
        items = parse_sse(response.get_data(as_text=True))
        assert "".join(item["text"] for item in items if item["type"] == "delta") == \
            "Claude answered without local workers."
        assert items[-1]["type"] == "done" and items[-1]["state"] == "completed"
    else:
        response = chat(app, token_for(app, user), model=CLAUDE_MODEL)
        assert response.status_code == 200
        assert response.json["choices"][0]["message"]["content"] == "Claude answered without local workers."
        assert response.json["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    response.close()
    assert calls == [(account_id, CLAUDE_MODEL)] and fake_ollama.chat_bodies() == []
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]["model_id"] == model["id"]
    with app.app_context():
        assert accounts.get(account_id)["window_used"] == 7
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


@pytest.mark.parametrize("reason", ["unpublished", "no_transport", "access_denied"])
def test_hidden_warning_does_not_make_an_ineligible_claude_model_available(
        app, make_user, monkeypatch, reason):
    workers_down(app, monkeypatch)
    model, _, calls = register_claude(app, published=reason != "unpublished")
    warning_enabled(app, False)
    user, browser = signed_in(app, make_user)
    if reason == "no_transport":
        claude_pool.register_site_chat(None)
    elif reason == "access_denied":
        with app.app_context():
            access.set_policy("model", model["id"], "deny_except_allowlist", False, None)
    report = browser.fetch("/status?banner=1").json
    # Monitors describe the whole site; this user's permissions still block
    # sending even when another account can use the published Claude model.
    assert report["status"] == ("degraded" if reason == "access_denied" else "outage")
    assert report["can_send"] is False and report["notices"][0]["kind"] == "outage"
    assert report["notices"][0]["visible"] is False
    assert 'data-kind="outage"' not in report["banner_html"]
    refused = send(browser, new_chat(browser), model=CLAUDE_MODEL)
    assert refused.status_code == 503 and refused.json["error"]["code"] == "outage"
    assert calls == [] and ledger(app, user) == []


def test_hiding_worker_warnings_does_not_hide_maintenance_or_announcements(app, make_user, monkeypatch):
    workers_down(app, monkeypatch)
    register_claude(app)
    warning_enabled(app, False)
    user, browser = signed_in(app, make_user)
    with app.app_context():
        settings.update(maintenance_mode=1, maintenance_message="Planned upgrade",
                        warning_banner_enabled=1, warning_banner_message="Service notice",
                        warning_banner_dismissible=0)
    report = browser.fetch("/status?banner=1").json
    assert report["status"] == "maintenance" and report["can_send"] is False
    notices = {item["kind"]: item for item in report["notices"]}
    assert notices["maintenance"]["visible"] is True and notices["maintenance"]["dismissible"] is False
    assert notices["announcement"]["visible"] is True
    assert notices["local_outage"]["visible"] is False
    assert 'data-kind="maintenance"' in report["banner_html"]
    assert 'data-kind="announcement"' in report["banner_html"]
    assert 'data-kind="local_outage"' not in report["banner_html"]
    assert "Planned upgrade" in report["banner_html"] and "Service notice" in report["banner_html"]
    with app.test_request_context():
        assert status.guard(user).json["error"]["code"] == "maintenance"
    admin = Browser(app)
    admin.login("admin")
    admin_report = admin.fetch("/status?banner=1").json
    assert admin_report["can_send"] is True and admin_report["accepting_requests"] is False
    admin_notice = next(item for item in admin_report["notices"] if item["kind"] == "maintenance")
    assert admin_notice["visible"] is True and admin_notice["admin_bypass"] is True


def test_recovered_workers_clear_the_notice_without_changing_the_global_setting(app, make_user, monkeypatch):
    workers_down(app, monkeypatch)
    warning_enabled(app, False)
    _, browser = signed_in(app, make_user)
    assert browser.fetch("/status").json["status"] == "outage"
    monkeypatch.setattr(health, "inference_down", lambda *args: False)
    report = browser.fetch("/status?banner=1").json
    assert report["status"] == "ok" and report["can_send"] is True and report["accepting_requests"] is True
    assert report["notices"] == [] and 'data-kind="outage"' not in report["banner_html"]
    with app.app_context():
        assert settings.get()["worker_offline_warning_enabled"] == 0
