"""Administrators can hide the notice without cached forms changing its value."""

import json

import pytest

from bananachat import db
from bananachat.db import settings as site_settings
from tests.app.fixtures import Browser


def _form(**overrides):
    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            "maintenance_message": "", "warning_banner_message": ""}
    for key, value in site_settings.DARK_PALETTE.items():
        form[f"{key}_color"] = value
    for key, value in site_settings.LIGHT_PALETTE.items():
        form[f"light_{key}_color"] = value
    form.update(overrides)
    return form


def _enabled(app):
    with app.app_context():
        return site_settings.get()["worker_offline_warning_enabled"]


def test_admin_can_disable_and_enable_notice_with_audited_changes(app, admin):
    html = admin.get("/admin/settings").get_data(as_text=True)
    assert 'name="worker_offline_warning_enabled" value="1" checked' in html
    assert 'name="worker_offline_warning_present" value="1"' in html
    assert "Users can hide it for 24 hours" in html
    response = admin.post("/admin/settings", _form(worker_offline_warning_present="1"))
    assert response.status_code == 302
    assert _enabled(app) == 0
    with app.app_context():
        entry = db.one("SELECT details FROM audit_log WHERE action='admin.settings_save' ORDER BY id DESC LIMIT 1")
        assert json.loads(entry["details"])["worker_offline_warning_enabled"] == 0
        assert site_settings.get()["maintenance_mode"] == 0
    html = admin.get("/admin/settings").get_data(as_text=True)
    assert 'name="worker_offline_warning_enabled" value="1">' in html
    admin.post("/admin/settings", _form(worker_offline_warning_present="1", worker_offline_warning_enabled="1"))
    assert _enabled(app) == 1


@pytest.mark.parametrize("enabled", [0, 1])
def test_cached_form_preserves_current_notice_setting(app, admin, enabled):
    with app.app_context():
        site_settings.update(worker_offline_warning_enabled=enabled)
    admin.post("/admin/settings", _form(site_name="Renamed"))
    assert _enabled(app) == enabled
    with app.app_context():
        assert site_settings.get()["site_name"] == "Renamed"


def test_invalid_settings_form_does_not_change_notice(app, admin):
    response = admin.post("/admin/settings", _form(worker_offline_warning_present="1", primary_color="invalid"),
                          follow_redirects=True)
    assert "must be a colour" in response.get_data(as_text=True)
    assert _enabled(app) == 1


def test_only_admin_can_change_notice(app, make_user):
    make_user("reader")
    reader = Browser(app)
    reader.login("reader")
    assert reader.get("/admin/settings").status_code == 403
    assert reader.post("/admin/settings", _form(worker_offline_warning_present="1")).status_code == 403
    assert _enabled(app) == 1


def test_notice_change_requires_csrf(app, admin):
    response = admin.client.post("/admin/settings", data=_form(worker_offline_warning_present="1"))
    assert response.status_code == 400
    assert _enabled(app) == 1
