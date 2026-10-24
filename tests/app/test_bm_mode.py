"""BM mode, the easy mode: on for everyone, or per account, switched by the person or by administrators."""

from __future__ import annotations

import re
import threading

import pytest

from tests.app.browser_helpers import chromium_executable
from tests.app.fixtures import Browser


def _body_classes(html: str) -> set[str]:
    return set(re.search(r'<body class="([^"]*)"', html).group(1).split())


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    return browser


def test_bm_mode_is_off_until_chosen_and_hides_the_advanced_controls(app, make_user):
    from tests.app.test_chat import setup_models

    setup_models(app)
    make_user("ada")
    browser = _signed_in(app, "ada")
    html = browser.get("/chat", follow_redirects=True).get_data(as_text=True)
    assert "bm-mode" not in _body_classes(html)
    # The advanced controls are marked so that BM mode hides them.
    for marker in ('id="params-panel"', 'id="effort-picker"', 'id="params-toggle"'):
        tag = html[html.rindex("<", 0, html.index(marker)):html.index(">", html.index(marker))]
        assert "bm-hide" in tag, marker
    assert 'action="/customize/bm-mode"' in html

    response = browser.post("/customize/bm-mode", {"next": "/chat"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/chat")
    html = browser.get("/chat", follow_redirects=True).get_data(as_text=True)
    assert "bm-mode" in _body_classes(html)
    assert 'aria-pressed="true"' in html
    browser.post("/customize/bm-mode", {"next": "https://evil.example/"})
    html = browser.get("/chat", follow_redirects=True).get_data(as_text=True)
    assert "bm-mode" not in _body_classes(html)


def _site(app, **values):
    from bananachat.db import settings as site_settings

    with app.app_context():
        site_settings.update(**values)


def _account(app, user_id, **columns):
    from bananachat import db

    with app.app_context():
        for name, value in columns.items():
            db.execute(f"UPDATE users SET {name}=? WHERE id=?", (value, user_id))


def _state(browser):
    """(BM mode on, may the person switch it) as the pages show it."""
    html = browser.get("/customize").get_data(as_text=True)
    switch = re.search(r'<input type="checkbox" name="bm_mode"[^>]*>', html).group(0)
    return "bm-mode" in _body_classes(html), "disabled" not in switch and 'action="/customize/bm-mode"' in html


def test_people_switch_it_themselves_while_the_site_allows_it(app, make_user):
    make_user("bea")
    browser = _signed_in(app, "bea")
    assert _state(browser) == (False, True)
    saved = browser.post_json("/api/preferences", {"bm_mode": True}).get_json()
    assert saved["preferences"]["bm_mode"] is True and saved["bm_mode_switchable"] is True
    assert _state(browser) == (True, True)
    browser.post("/customize/bm-mode", {"next": "/customize"})
    assert _state(browser) == (False, True)
    # Visitors who are not signed in always get the normal pages.
    assert "bm-mode" not in _body_classes(Browser(app).get("/login").get_data(as_text=True))


def test_when_only_administrators_switch_it_the_person_cannot(app, make_user):
    from bananachat.i18n import translate

    user = make_user("cid")
    _site(app, bm_mode_self_service=0)
    _account(app, user["id"], bm_mode=1)
    browser = _signed_in(app, "cid")
    assert _state(browser) == (True, False)
    page = browser.get("/customize").get_data(as_text=True)
    language = re.search(r'<html lang="([a-z]+)"', page).group(1)
    assert translate(language, "customize.bm_mode_admin_only") in page
    refused = browser.post_json("/api/preferences", {"bm_mode": False}).get_json()
    assert refused["preferences"]["bm_mode"] is True and refused["bm_mode_switchable"] is False
    browser.post("/customize/bm-mode", {"next": "/customize"})
    assert _state(browser) == (True, False)
    # An administrator can leave it to this one person anyway.
    _account(app, user["id"], bm_mode_control="self")
    assert _state(browser) == (True, True)
    browser.post("/customize/bm-mode", {"next": "/customize"})
    assert _state(browser) == (False, True)


def test_an_administrator_can_lock_one_account_while_others_switch_freely(app, make_user):
    locked = make_user("dan")
    make_user("dee")
    _account(app, locked["id"], bm_mode_control="admin")
    assert _state(_signed_in(app, "dan")) == (False, False)
    assert _state(_signed_in(app, "dee")) == (False, True)


def test_on_for_everyone_overrides_every_account(app, make_user):
    from bananachat.i18n import translate

    user = make_user("eli")
    _account(app, user["id"], bm_mode=0, bm_mode_control="self")
    _site(app, bm_mode_global=1)
    browser = _signed_in(app, "eli")
    assert _state(browser) == (True, False)
    page = browser.get("/customize").get_data(as_text=True)
    language = re.search(r'<html lang="([a-z]+)"', page).group(1)
    assert translate(language, "customize.bm_mode_everyone") in page
    assert "bm-mode" in _body_classes(_signed_in(app, "admin").get("/admin/").get_data(as_text=True))
    _site(app, bm_mode_global=0)
    assert _state(browser) == (False, True)  # the account's own state comes back


def test_administrators_switch_accounts_one_by_one_or_all_at_once(app, make_user):
    from bananachat.db import users

    user = make_user("fay")
    other = make_user("gus")
    admin = _signed_in(app, "admin")
    page = admin.get(f"/admin/users/{user['id']}").get_data(as_text=True)
    assert 'id="bm-mode"' in page
    assert admin.post(f"/admin/users/{user['id']}/bm-mode", {"state": "on", "control": "admin"}).status_code == 302
    with app.app_context():
        row = users.get(user["id"])
    assert (row["bm_mode"], row["bm_mode_control"]) == (1, "admin")
    assert "BM mode</span>" in admin.get("/admin/users").get_data(as_text=True)
    assert admin.post(f"/admin/users/{user['id']}/bm-mode", {"state": "on", "control": "nobody"}).status_code == 302
    with app.app_context():
        assert users.get(user["id"])["bm_mode_control"] == "admin"  # refused: unknown choice

    admin.post("/admin/settings/bm-mode/all", {"state": "on"})
    with app.app_context():
        assert users.get(other["id"])["bm_mode"] == 1 and users.get(user["id"])["bm_mode_control"] == "admin"
    admin.post("/admin/settings/bm-mode/all", {"state": "off"})
    with app.app_context():
        assert {users.get(row_id)["bm_mode"] for row_id in (user["id"], other["id"])} == {0}


def test_administrators_choose_the_site_rules(app):
    from bananachat.db import settings as site_settings

    admin = _signed_in(app, "admin")
    page = admin.get("/admin/settings").get_data(as_text=True)
    assert 'name="bm_mode_global"' in page and 'name="bm_mode_self_service"' in page
    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            "bm_mode_present": "1", "bm_mode_global": "1"}
    for prefix, palette in (("", site_settings.DARK_PALETTE), ("light_", site_settings.LIGHT_PALETTE)):
        form.update({f"{prefix}{key}_color": value for key, value in palette.items()})
    assert admin.post("/admin/settings", form).status_code == 302
    with app.app_context():
        values = site_settings.get()
    assert (values["bm_mode_global"], values["bm_mode_self_service"]) == (1, 0)


@pytest.fixture
def easy_chat_page(app, make_user, fake_ollama):
    sync_api = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    from bananachat.db import users
    from tests.app.test_chat import setup_models

    setup_models(app)
    user = make_user("eve")
    with app.app_context():
        users.set_bm_mode(user["id"], True)
    fake_ollama.reply = "Simple answer."
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=chromium_executable())
            except Exception as error:  # noqa: BLE001 - optional browser dependency
                pytest.skip(f"Chromium is not available: {error}")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/login")
            page.fill("#username", "eve")
            page.fill("#password", "eve-password")
            page.wait_for_timeout(600)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            # Saved sampling settings from before BM mode must not apply unseen.
            page.evaluate("() => { for (const key of Object.keys(localStorage)) localStorage.removeItem(key); }")
            page.goto(base + "/chat")
            page.wait_for_selector("#chat-input")
            yield page
            browser.close()
            assert errors == []
    finally:
        server.shutdown()


def test_bm_mode_shows_a_simple_composer_that_still_sends(easy_chat_page, fake_ollama):
    page = easy_chat_page
    assert page.evaluate("() => document.body.classList.contains('bm-mode')")
    for selector in ("#params-toggle", "#effort-picker", "#composer-hint"):
        assert not page.locator(selector).is_visible(), selector
    assert page.locator("#model-button").is_visible() and page.locator("#send-button").is_visible()
    page.evaluate("() => { document.getElementById('param-temperature').value = '1.9'; }")
    page.fill("#chat-input", "Hello")
    page.click("#send-button")
    page.wait_for_function("() => document.querySelector('#chat-messages').innerText.includes('Simple answer.')",
                           timeout=20000)
    [body] = fake_ollama.chat_bodies()[-1:]
    assert "temperature" not in (body.get("options") or {})
