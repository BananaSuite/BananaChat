"""Registering a worker in the browser when the ten-minute confirmation runs out while the page is open."""

from __future__ import annotations

import threading

import pytest

from tests.app.fixtures import Browser
from tests.app.test_review2_chat import _chromium
from tests.app.test_two_step import ADMIN_PASSWORD, _stale


@pytest.fixture
def admin_page(app, client):
    sync_api = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    errors: list[str] = []
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=_chromium())
            except Exception as error:  # noqa: BLE001 - optional browser dependency
                pytest.skip(f"Chromium is not available: {error}")
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            signed_in = Browser(app)
            signed_in.login("admin", ADMIN_PASSWORD)
            cookie = signed_in.client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
            context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base}])
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/admin/workers")
            yield page
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert errors == []


def test_the_confirmation_field_appears_when_the_window_runs_out_while_the_page_is_open(app, admin_page):
    """Final verification of chat-auth-2fa-1: the page was loaded within the ten minutes, so it has no field; the
    refusal must offer one instead of asking to reload."""
    from bananachat import db

    page = admin_page
    assert page.locator("#register-form input[name=reauth_secret]").count() == 0
    _stale(app)
    page.fill("#worker-name", "Den PC")
    page.click("#register-form button[type=submit]")
    field = page.locator("#register-form input[name=reauth_secret]")
    field.wait_for(state="visible")
    assert page.evaluate("document.activeElement && document.activeElement.name") == "reauth_secret"
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM worker_nodes", default=0) == 0
    field.fill(ADMIN_PASSWORD)
    page.click("#register-form button[type=submit]")
    page.get_by_text("Token for “Den PC”").wait_for()
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM worker_nodes", default=0) == 1
    # Not asked again for ten minutes: the field goes away once used.
    assert page.locator("#register-form input[name=reauth_secret]").count() == 0
