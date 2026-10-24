"""Easter eggs (the mascot's animations, its sunglasses, the Konami code's banana shower): site and personal switches."""

from __future__ import annotations

import re
import threading

import pytest

from tests.app.browser_helpers import chromium_executable
from tests.app.fixtures import Browser

KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"]


def _mascot(html: str) -> dict:
    tag = re.search(r"<button[^>]*data-mascot[^>]*>", html).group(0)
    return {name: value for name, value in re.findall(r'data-mascot-(motion|shades|konami)="(\d)"', tag)}


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    return browser


def test_every_easter_egg_is_on_by_default(app, make_user):
    make_user("ann")
    assert _mascot(_signed_in(app, "ann").get("/customize").get_data(as_text=True)) == \
        {"motion": "1", "shades": "1", "konami": "1"}


def test_the_site_and_each_person_switch_them_off(app, make_user):
    from bananachat.db import settings as site_settings
    from bananachat.i18n import translate

    make_user("ben")
    browser = _signed_in(app, "ben")
    saved = browser.post_json("/api/preferences", {"egg_mascot_shades": False}).get_json()["preferences"]
    assert saved["egg_mascot_shades"] is False and saved["egg_konami"] is True
    assert _mascot(browser.get("/customize").get_data(as_text=True)) == {"motion": "1", "shades": "0", "konami": "1"}
    with app.app_context():
        site_settings.update(easter_konami=0, easter_mascot_motion=0)
    page = browser.get("/customize").get_data(as_text=True)
    assert _mascot(page) == {"motion": "0", "shades": "0", "konami": "0"}
    # What the site turned off cannot be switched on in Customise, and says why.
    language = re.search(r'<html lang="([a-z]+)"', page).group(1)
    assert re.search(r'<input type="checkbox" name="egg_konami"[^>]*disabled', page)
    assert translate(language, "customize.egg_site_off") in page


def test_administrators_choose_the_easter_eggs_the_site_offers(app):
    from bananachat.db import settings as site_settings

    admin = _signed_in(app, "admin")
    assert 'name="easter_konami"' in admin.get("/admin/settings").get_data(as_text=True)
    form = {"site_name": "Test Chat", "signup_mode": "invite", "default_theme_mode": "dark",
            "easter_eggs_present": "1", "easter_mascot_motion": "on"}
    for prefix, palette in (("", site_settings.DARK_PALETTE), ("light_", site_settings.LIGHT_PALETTE)):
        form.update({f"{prefix}{key}_color": value for key, value in palette.items()})
    assert admin.post("/admin/settings", form).status_code == 302
    with app.app_context():
        values = site_settings.get()
    assert (values["easter_mascot_motion"], values["easter_mascot_shades"], values["easter_konami"]) == (1, 0, 0)
    # A cached form without the easter egg fields changes none of them.
    form.pop("easter_eggs_present")
    form["easter_konami"] = "on"
    admin.post("/admin/settings", form)
    with app.app_context():
        assert site_settings.get()["easter_konami"] == 0


@pytest.fixture
def customise_page(app, make_user):
    sync_api = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    make_user("cleo")
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
            page.fill("#username", "cleo")
            page.fill("#password", "cleo-password")
            page.wait_for_timeout(600)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.goto(base + "/customize")
            page.wait_for_selector("#easter-eggs")
            yield page
            browser.close()
            assert errors == []
    finally:
        server.shutdown()


def _konami(page):
    page.locator("h1").click()  # focus the page, not a form field
    for key in KONAMI:
        page.keyboard.press(key)


def test_easter_eggs_switch_off_live_in_the_browser(customise_page):
    page = customise_page
    _konami(page)
    page.wait_for_selector(".mascot-shower", state="attached", timeout=3000)
    page.wait_for_selector(".mascot-shower", state="detached", timeout=6000)
    for _ in range(11):
        page.click("[data-mascot]")
    assert page.evaluate("() => document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")

    # Off in Customise: the change applies at once, without a reload.
    page.locator('label:has(input[name="egg_konami"])').click()
    page.locator('label:has(input[name="egg_mascot_shades"])').click()
    page.wait_for_function("() => document.querySelector('[data-mascot]').dataset.mascotKonami === '0'"
                           " && !document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")
    _konami(page)
    page.wait_for_timeout(500)
    assert page.locator(".mascot-shower").count() == 0
    for _ in range(11):
        page.click("[data-mascot]")
    assert not page.evaluate(
        "() => document.querySelector('.mascot-sprite').classList.contains('mascot-sprite--shades')")
    page.wait_for_selector("#save-status.is-saved", timeout=5000)
    page.reload()
    assert page.evaluate("() => document.querySelector('[data-mascot]').dataset.mascotKonami") == "0"
