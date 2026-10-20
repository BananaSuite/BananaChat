"""Menu panels and long code stay on screen at phone, tablet and desktop widths, in English and Italian and at
larger text sizes."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tests.app.fixtures import Browser
from tests.app.browser_helpers import chromium_executable
from tests.app.test_two_step import ADMIN_PASSWORD

WIDTHS = (390, 660, 744, 768, 1440)


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=chromium_executable())
        except Exception as error:  # noqa: BLE001 - optional browser dependency
            pytest.skip(f"Chromium is not available: {error}")
        yield browser
        browser.close()


@pytest.fixture
def screen(app, make_user, chromium):
    from werkzeug.serving import make_server

    from bananachat.db import personalities

    rosa = make_user("rosa")
    with app.app_context():
        personalities.create(rosa["id"], "Mine", "Mine.", created_by=rosa["id"])
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    contexts, errors = [], []

    def open_page(path, *, width, language="en", font_scale=1.0):
        from bananachat.db import users

        with app.app_context():
            user = users.get_by_username("admin")
            users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": language,
                                                "font_scale": font_scale})
        signed_in = Browser(app)
        signed_in.login("admin", ADMIN_PASSWORD)
        cookie = signed_in.client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
        context = chromium.new_context(viewport={"width": width, "height": 900})
        contexts.append(context)
        context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base}])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + path, wait_until="networkidle")
        return page

    yield SimpleNamespace(open_page=open_page)
    for context in contexts:
        context.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert errors == []


def _no_sideways_scroll(page):
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def _reachable(page, selector):
    """The element lies inside the viewport and nothing (an edge, a clipping table) hides its middle."""
    return page.evaluate("""(selector) => {
        const node = document.querySelector(selector);
        const box = node.getBoundingClientRect();
        if (box.left < 0 || box.right > document.documentElement.clientWidth) return false;
        node.scrollIntoView({ block: "nearest" });
        const moved = node.getBoundingClientRect();
        const hit = document.elementFromPoint(moved.left + moved.width / 2, moved.top + moved.height / 2);
        return Boolean(hit && node.contains(hit));
    }""", selector)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("language,font_scale", [("en", 1.0), ("it", 1.0), ("it", 1.35)])
def test_the_personality_import_panel_opens_on_screen(screen, width, language, font_scale):
    page = screen.open_page("/personalities", width=width, language=language, font_scale=font_scale)
    page.click("#import summary")
    assert _reachable(page, ".persona-import-panel")
    assert _reachable(page, "#import-file")
    assert _reachable(page, ".persona-import-panel button[type=submit]")
    _no_sideways_scroll(page)


@pytest.mark.parametrize("width", WIDTHS)
def test_the_admin_disable_form_opens_on_screen(screen, width):
    page = screen.open_page("/admin/personalities", width=width)
    page.click("details.menu summary:has-text('Disable…')")
    assert _reachable(page, ".menu-form")
    assert _reachable(page, ".menu-form input[name=reason]")
    assert _reachable(page, ".menu-form button[type=submit]")
    _no_sideways_scroll(page)


def test_a_menu_moved_back_on_screen_follows_a_resize(screen):
    page = screen.open_page("/personalities", width=1440)
    page.click("#import summary")
    page.set_viewport_size({"width": 660, "height": 900})
    page.wait_for_function("document.querySelector('.persona-import-panel').getBoundingClientRect().left >= 0")
    assert _reachable(page, ".persona-import-panel button[type=submit]")


@pytest.mark.parametrize("width", (320, 390))
def test_long_paths_in_the_worker_setup_steps_wrap_at_the_largest_text_size(screen, width):
    page = screen.open_page("/admin/workers", width=width, font_scale=1.35)
    _no_sideways_scroll(page)
