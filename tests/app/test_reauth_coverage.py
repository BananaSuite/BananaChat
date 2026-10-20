"""Which administrator actions ask for a recent confirmation (web/admin/_reauth.py).

Every administrator POST action is classified; a new one fails the first test until someone decides.
The confirmed ones are checked against a session that signed in more than ten minutes ago: without the
password nothing changes, with it the action goes through.
"""

from __future__ import annotations

import inspect

import pytest

from tests.app.fixtures import Browser
from tests.app.test_two_step import ADMIN_PASSWORD, _stale

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _admin_actions(app) -> set[str]:
    return {rule.endpoint for rule in app.url_map.iter_rules()
            if rule.endpoint.startswith("admin.") and (rule.methods or set()) & WRITE_METHODS}


def _classified():
    from bananachat.web.admin import _reauth

    exempt = [endpoint for group in _reauth.EXEMPT.values() for endpoint in group]
    return [*_reauth.ALWAYS, *_reauth.WHEN], exempt


def test_every_administrator_action_is_classified(app):
    confirmed, exempt = _classified()
    everything = [*confirmed, *exempt]
    assert len(everything) == len(set(everything)), "an action is classified twice"
    unclassified = _admin_actions(app) - set(everything)
    assert not unclassified, ("Decide whether these administrator actions need a recent confirmation and add them "
                              f"to bananachat/web/admin/_reauth.py: {sorted(unclassified)}")
    assert not set(everything) - _admin_actions(app), "remove actions that no longer exist from the classification"


def test_confirmed_actions_ask_for_it_and_the_others_do_not(app):
    confirmed, exempt = _classified()
    for endpoint in [*confirmed, *exempt]:
        source = inspect.getsource(inspect.unwrap(app.view_functions[endpoint]))
        asks = "_reauth.confirmed(" in source or "_reauth.refusal(" in source
        assert asks == (endpoint in confirmed), endpoint


# ----- the actions --------------------------------------------------------------------------------------

def _user(app, username):
    from bananachat.db import users

    with app.app_context():
        return users.get_by_username(username)


def test_reinstating_suspending_or_deleting_an_administrator_needs_a_confirmation(app, admin, make_user):
    from bananachat.db import users

    mallory = make_user("mallory", role="admin")
    other = make_user("other", role="admin")
    pupil = make_user("pupil")
    with app.app_context():
        users.suspend(mallory["id"])
    _stale(app)
    page = admin.get(f"/admin/users/{mallory['id']}").get_data(as_text=True)
    assert page.count('name="reauth_secret"') == 4  # role, password, lifting the suspension, deletion
    admin.post(f"/admin/users/{mallory['id']}/unsuspend", {})
    admin.post(f"/admin/users/{other['id']}/suspend", {})
    admin.post(f"/admin/users/{other['id']}/delete", {"confirm_username": "other"})
    assert users.is_suspended(_user(app, "mallory"))
    assert not users.is_suspended(_user(app, "other"))
    assert Browser(app).post("/login", {"username": "mallory", "password": "mallory-password"}).status_code != 302
    # Regular accounts are moderated as before.
    admin.post(f"/admin/users/{pupil['id']}/suspend", {})
    assert users.is_suspended(_user(app, "pupil"))
    admin.post(f"/admin/users/{pupil['id']}/unsuspend", {})
    assert not users.is_suspended(_user(app, "pupil"))
    admin.post(f"/admin/users/{pupil['id']}/delete", {"confirm_username": "pupil"})
    assert _user(app, "pupil") is None

    admin.post(f"/admin/users/{mallory['id']}/unsuspend", {"reauth_secret": ADMIN_PASSWORD})
    assert not users.is_suspended(_user(app, "mallory"))
    admin.post(f"/admin/users/{other['id']}/suspend", {})
    assert users.is_suspended(_user(app, "other"))
    admin.post(f"/admin/users/{other['id']}/delete", {"confirm_username": "other"})
    assert _user(app, "other") is None


def test_registering_a_worker_needs_a_confirmation(app, admin):
    from bananachat import db

    _stale(app)
    assert 'name="reauth_secret"' in admin.get("/admin/workers").get_data(as_text=True)
    admin.post("/admin/workers", {"name": "Den PC"})
    response = admin.post_json("/admin/workers", {"name": "Den PC"})
    assert response.status_code == 403 and response.json["error"]["code"] == "reauth_required"
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM worker_nodes", default=0) == 0
    wrong = admin.post_json("/admin/workers", {"name": "Den PC", "reauth_secret": "guess-guess"})
    assert wrong.status_code == 403 and "not correct" in wrong.json["error"]["message"]
    response = admin.post_json("/admin/workers", {"name": "Den PC", "reauth_secret": ADMIN_PASSWORD})
    assert response.status_code == 201 and response.json["token"]


def test_every_provider_change_needs_a_confirmation_with_or_without_a_key(app, admin):
    from bananachat.db import external_providers

    form = {"name": "Gateway", "preset": "custom", "base_url": "https://llm.example.com/v1", "protocol": "openai",
            "auth_style": "bearer", "enabled": "1", "api_key": ""}
    _stale(app)
    page = admin.get("/admin/models/providers").get_data(as_text=True)
    assert 'name="reauth_secret"' in page and "Needed when you enter an API key" not in page
    admin.post("/admin/models/providers", form)  # keyless: prompts would still go there
    with app.app_context():
        assert external_providers.list_providers() == []
    admin.post("/admin/models/providers", {**form, "reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        [keyless] = external_providers.list_providers()

    _stale(app)
    admin.post(f"/admin/models/providers/{keyless['id']}", {**form, "base_url": "https://collector.example.net/v1"})
    with app.app_context():
        assert external_providers.get(keyless["id"])["base_url"] == "https://llm.example.com/v1"
    admin.post(f"/admin/models/providers/{keyless['id']}",
               {**form, "base_url": "https://llm2.example.com/v1", "reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert external_providers.get(keyless["id"])["base_url"] == "https://llm2.example.com/v1"


def test_removing_a_provider_and_its_key_needs_a_confirmation(app, admin):
    """Removing a provider deletes its stored key, so it asks for the password again."""
    from bananachat.db import external_providers
    from bananachat.services import provider_secrets

    with app.app_context():
        provider_id = external_providers.save(name="Keyed", base_url="https://llm.example.com/v1", protocol="openai",
                                              secret_ref=provider_secrets.save("sk-test-key-123"))
        reference = external_providers.get(provider_id)["secret_ref"]
    _stale(app)
    page = admin.get("/admin/models/providers").get_data(as_text=True)
    assert f'id="provider-remove-{provider_id}-reauth"' in page  # the field is inside the removal form
    admin.post(f"/admin/models/providers/{provider_id}/remove", {})
    with app.app_context():
        assert external_providers.get(provider_id) is not None
        assert provider_secrets.load(reference) == "sk-test-key-123"
    admin.post(f"/admin/models/providers/{provider_id}/remove", {"reauth_secret": ADMIN_PASSWORD})
    with app.app_context():
        assert external_providers.get(provider_id) is None


@pytest.mark.parametrize("field,value,needs", [("signup_mode", "open", True), ("signup_mode", "disabled", False),
                                               ("site_name", "Renamed", False)])
def test_opening_sign_up_to_everyone_needs_a_confirmation(app, admin, field, value, needs):
    from tests.app.test_two_step import _changed_settings

    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            **{f"{prefix}{key}_color": "#123456" for prefix in ("", "light_")
               for key in ("primary", "secondary", "accent", "text", "sidebar", "bg")}}
    _stale(app)
    assert "or to open sign-up to everyone" in admin.get("/admin/settings").get_data(as_text=True)
    admin.post("/admin/settings", {**form, field: value})
    assert (_changed_settings(app)[field] == value) is not needs
    admin.post("/admin/settings", {**form, field: value, "reauth_secret": ADMIN_PASSWORD})
    assert _changed_settings(app)[field] == value
