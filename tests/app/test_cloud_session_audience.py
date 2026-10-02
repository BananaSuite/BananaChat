"""Cloud sessions audience defaults, upgrade behavior and administrator changes."""

from __future__ import annotations

import pytest

from bananachat.db import access, catalog, users
from bananachat.services.agents import service
from bananachat.services.agents import settings as agent_settings
from tests.app.conftest import Browser
from tests.app.test_agents import (  # noqa: F401 - shared sandbox fixtures.
    add_user, agents_app, fast_loop, runner, set_settings, start, start_ok,
    token_file, wait_status,
)


def test_fresh_sessions_are_off_and_administrators_only(app):
    with app.app_context():
        settings = agent_settings.current(fresh=True)
        assert not settings.enabled
        assert settings.access_mode == "admins"
        assert settings.to_dict()["access_mode"] == "admins"
        assert not agent_settings.user_allowed(users.get_by_username("admin"), settings)


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_explicit_settings_preserve_capability_policy(enabled):
    settings = agent_settings.normalise({"enabled": enabled, "max_steps": 12})
    assert settings.access_mode == "custom"
    assert settings.enabled is enabled
    assert agent_settings.normalise(settings.to_dict()) == settings


@pytest.mark.parametrize("mode", ["all", "admin", "", None, False, 1, [], {}])
def test_corrupt_audience_fails_closed(mode):
    settings = agent_settings.normalise({"enabled": True, "access_mode": mode})
    assert not settings.enabled
    assert settings.access_mode == "admins"


@pytest.mark.parametrize("mode,allowed_member,allowed_plain", [
    ("admins", False, False), ("custom", True, False), ("everyone", True, True),
])
def test_audience_uses_same_gates_for_navigation_and_start(agents_app, mode, allowed_member, allowed_plain):
    app = agents_app({"access_mode": mode})
    member = add_user(app, "member", allowed=True)
    plain = add_user(app, "plain")
    for user, allowed in [(member, allowed_member), (plain, allowed_plain)]:
        browser = Browser(app)
        browser.login(user["username"])
        assert ('href="/agents"' in browser.get("/account").get_data(as_text=True)) is allowed
        with app.app_context():
            assert agent_settings.user_allowed(user) is allowed
            assert agent_settings.access_state(user) == ("ok" if allowed else "denied")
            if not allowed:
                with pytest.raises(service.AgentError, match="agents.error_no_access"):
                    service.check_access(user)
        response = start(browser)
        assert response.status_code == (201 if allowed else 403)
        if allowed:
            wait_status(app, response.get_json()["id"], "finished")
    admin = Browser(app)
    admin.login("admin", "admin-password")
    wait_status(app, start_ok(admin), "finished")


def test_everyone_still_checks_model_access_and_suspension(agents_app):
    app = agents_app({"access_mode": "everyone"})
    user = add_user(app, "plain")
    browser = Browser(app)
    browser.login("plain")
    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        access.set_policy("model", model["id"], "deny_except_allowlist", False, None)
    response = start(browser, model=model["ollama_name"])
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "model_unavailable"
    with app.app_context():
        users.suspend(user["id"])
        suspended = users.get(user["id"])
        assert not agent_settings.user_allowed(suspended)
        assert agent_settings.access_state(suspended) == "denied"
        with pytest.raises(service.AgentError, match="agents.error_no_access"):
            service.check_access(suspended)


def _form():
    return {**{name: str(default) for name, (default, _, _) in agent_settings.INTEGER_FIELDS.items()},
            "enabled": "1"}


@pytest.mark.parametrize("mode", agent_settings.ACCESS_MODES)
def test_admin_saves_audience_and_old_forms_preserve_it(app, mode):
    admin = Browser(app)
    admin.login("admin", "admin-password")
    form = {**_form(), "access_mode_present": "1", "access_mode": mode}
    assert admin.post("/admin/agents/settings", form).status_code == 302
    with app.app_context():
        saved = agent_settings.current(fresh=True)
        assert saved.enabled and saved.access_mode == mode
    # Cached old forms have no audience controls; unrelated changes cannot
    # broaden or narrow a site's deliberately chosen audience.
    assert admin.post("/admin/agents/settings", _form()).status_code == 302
    with app.app_context():
        assert agent_settings.current(fresh=True).access_mode == mode


def test_admin_rejects_missing_or_unknown_audience(app):
    admin = Browser(app)
    admin.login("admin", "admin-password")
    for submitted in [{"access_mode_present": "1"},
                      {"access_mode_present": "1", "access_mode": "all"}]:
        assert admin.post("/admin/agents/settings", {**_form(), **submitted}).status_code == 302
        with app.app_context():
            settings = agent_settings.current(fresh=True)
            assert not settings.enabled
            assert settings.access_mode == "admins"
