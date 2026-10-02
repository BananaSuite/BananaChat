"""Worker notices can be hidden without changing provider admission or other notices."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.app.fixtures import Browser
from tests.app.test_chat import setup_models
from tests.app.test_review2_chat import _chromium

MODEL = "warning-test-sonnet"
DAY_MS = 24 * 60 * 60 * 1000
WORKER = "#status-region [data-worker-warning='1']"
HIDE = "#status-region [data-hide-worker-status]"


@pytest.fixture(scope="module")
def chromium():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as error:  # noqa: BLE001 - optional browser dependency
            pytest.skip(f"Chromium is not available: {error}")
        yield browser
        browser.close()


@pytest.fixture
def worker_browser(app, make_user, monkeypatch, chromium):
    """An actual HTTP server and browser with synthetic Claude and offline compute."""
    from werkzeug.serving import make_server

    from bananachat.db import catalog, claude_pool as accounts
    from bananachat.services import claude_pool, health

    setup_models(app)
    make_user("alice")
    make_user("bob")
    app.config["BC"] = replace(app.config["BC"], inference_local=False, inference_outage_mode="shutdown")
    state = {"down": True}
    monkeypatch.setattr(health, "inference_down", lambda *args: state["down"])
    monkeypatch.setattr(health, "status", lambda: {"since": None})
    monkeypatch.setattr(claude_pool, "_site_discovery", lambda: [
        {"name": MODEL, "display": "Claude Sonnet", "family": "sonnet", "reasoning": ["low", "medium"]}])
    monkeypatch.setattr(claude_pool, "_site_chat", lambda *args: iter([
        {"text": "Hosted Claude still answers.", "tokens_in": 8, "tokens_out": 5, "done": True}]))
    with app.app_context():
        accounts.add_account("Synthetic account", window_limit=100_000)
        claude_pool.sync_catalog(selected=[MODEL], source="admin")
        catalog.set_rollout(catalog.get_by_name(MODEL)["id"], True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    contexts, errors = [], []

    def authenticate(context, username):
        signed_in = Browser(app)
        signed_in.login(username)
        cookie = signed_in.client.get_cookie(app.config.get("SESSION_COOKIE_NAME", "session"))
        context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base}])

    def open_page(username="alice", *, width=1280, init_script=None):
        context = chromium.new_context(viewport={"width": width, "height": 900})
        contexts.append(context)
        if username is not None:
            authenticate(context, username)
        context.add_init_script(f"try {{ localStorage.setItem('bc-chat-model', {json.dumps(MODEL)}); }} catch {{}}")
        if init_script:
            context.add_init_script(init_script)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + ("/chat" if username is not None else "/login"))
        page.wait_for_selector("#chat-input" if username is not None else "#username")
        return page

    yield SimpleNamespace(app=app, state=state, base=base, open_page=open_page, authenticate=authenticate)
    for context in contexts:
        context.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert errors == []


def _key(page):
    return page.evaluate("'bc-worker-warning:v1:' + encodeURIComponent(document.querySelector('#status-region').dataset.statusUser)")


def _status(page):
    return page.evaluate("async () => (await fetch('/status?banner=1')).json()")


def _poll(page):
    """Exercise the live page poll rather than calling the endpoint alone."""
    with page.expect_response(lambda response: "/status?banner=1" in response.url and response.request.method == "GET"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def _visible(page, selector=WORKER):
    return page.locator(selector).is_visible()


def test_worker_hide_lasts_exactly_a_day_across_reload_and_unchanged_polls(worker_browser):
    page = worker_browser.open_page()
    page.clock.install(time=datetime.now(timezone.utc))
    page.clock.pause_at(datetime.now(timezone.utc))
    assert _visible(page)
    page.locator(HIDE).click()
    key = _key(page)
    hidden_at = int(page.evaluate("key => localStorage.getItem(key)", key))
    assert not _visible(page)
    page.reload()
    page.wait_for_selector("#chat-input")
    assert not _visible(page)
    _poll(page)
    assert int(page.evaluate("key => localStorage.getItem(key)", key)) == hidden_at
    page.clock.fast_forward(DAY_MS - 1000)
    _poll(page)
    assert not _visible(page)
    assert int(page.evaluate("key => localStorage.getItem(key)", key)) == hidden_at
    page.route("**/status?banner=1", lambda route: route.abort())
    page.clock.fast_forward(1001)
    page.wait_for_function("document.querySelector('[data-worker-warning]').hidden === false")
    assert _visible(page)
    assert page.evaluate("key => localStorage.getItem(key)", key) is None
    page.unroute("**/status?banner=1")
    assert _status(page)["can_send"] is True


def test_admin_control_changes_live_banner_without_changing_service_status(worker_browser):
    from bananachat.db import settings

    page = worker_browser.open_page()
    admin = worker_browser.open_page("admin")
    original = _status(page)
    assert original["can_send"] and original["notices"][0]["kind"] == "local_outage"
    admin.goto(worker_browser.base + "/admin/settings#notices")
    toggle = admin.locator("input[name=worker_offline_warning_enabled]")
    assert toggle.is_checked()
    toggle.focus()
    admin.keyboard.press("Space")
    assert not toggle.is_checked()
    admin.get_by_role("button", name="Save settings", exact=True).click()
    admin.wait_for_load_state("networkidle")
    with worker_browser.app.app_context():
        assert settings.get()["worker_offline_warning_enabled"] == 0
    _poll(page)
    page.wait_for_function("!document.querySelector('[data-worker-warning]')")
    hidden = _status(page)
    assert hidden["can_send"] == original["can_send"]
    assert [notice["kind"] for notice in hidden["notices"]] == [notice["kind"] for notice in original["notices"]]
    assert hidden["notices"][0]["visible"] is False
    toggle = admin.locator("input[name=worker_offline_warning_enabled]")
    toggle.focus()
    admin.keyboard.press("Space")
    assert toggle.is_checked()
    admin.get_by_role("button", name="Save settings", exact=True).click()
    admin.wait_for_load_state("networkidle")
    _poll(page)
    page.wait_for_selector(WORKER)
    assert _visible(page)


def test_hiding_blocking_outage_does_not_enable_sending_or_bypass_http_guard(worker_browser):
    from bananachat.db import catalog, settings

    with worker_browser.app.app_context():
        catalog.set_rollout(catalog.get_by_name(MODEL)["id"], False)
    page = worker_browser.open_page()
    assert _status(page)["can_send"] is False
    assert page.locator("#send-button").is_disabled()
    assert page.locator("#chat-input").is_enabled()  # drafts remain editable
    page.locator(HIDE).click()
    assert not _visible(page)
    with worker_browser.app.app_context():
        settings.update(worker_offline_warning_enabled=0)
    _poll(page)
    assert page.locator("#send-button").is_disabled()
    result = page.evaluate("""async () => {
        const boot = JSON.parse(document.querySelector('#bc-boot').textContent);
        const data = JSON.parse(document.querySelector('#page-data').textContent);
        const body = new FormData(); body.set('content', 'Do not bypass the outage');
        const response = await fetch(`/chat/${data.session.id}/send`, {
          method: 'POST', body, headers: {'X-CSRF-Token': boot.csrf, 'X-Requested-With': 'fetch'}
        });
        return {status: response.status, body: await response.json()};
    }""")
    assert result["status"] == 503 and result["body"]["error"]["code"] == "outage"


def test_hosted_claude_can_answer_while_worker_notice_is_hidden(worker_browser):
    page = worker_browser.open_page()
    assert _status(page)["can_send"] is True
    assert page.locator("#chat-input").is_enabled()
    page.locator(HIDE).click()
    page.fill("#chat-input", "Can you answer with all workers offline?")
    page.click("#send-button")
    page.wait_for_selector(".message-assistant:not(.is-pending)", timeout=15000)
    assert "Hosted Claude still answers." in page.inner_text("#chat-messages")
    assert not _visible(page)


def test_worker_hide_is_scoped_to_the_signed_in_account_and_browser(worker_browser):
    page = worker_browser.open_page()
    page.locator(HIDE).click()
    alice_key = _key(page)
    hidden_at = page.evaluate("key => localStorage.getItem(key)", alice_key)
    # Log out through the real protected route, then enter another account on the same origin.
    page.evaluate("""async () => {
        const boot = JSON.parse(document.querySelector('#bc-boot').textContent);
        await fetch('/logout', {method: 'POST', headers: {'X-CSRF-Token': boot.csrf}});
    }""")
    worker_browser.authenticate(page.context, "bob")
    page.goto(worker_browser.base + "/chat")
    page.wait_for_selector("#chat-input")
    assert _key(page) != alice_key and _visible(page)
    worker_browser.authenticate(page.context, "alice")
    page.goto(worker_browser.base + "/chat")
    page.wait_for_selector("#chat-input")
    assert not _visible(page)
    assert page.evaluate("key => localStorage.getItem(key)", alice_key) == hidden_at
    assert _visible(worker_browser.open_page())  # another browser has no dismissal


def test_worker_hide_does_not_dismiss_announcement_or_maintenance(worker_browser):
    from bananachat.db import settings

    with worker_browser.app.app_context():
        settings.update(warning_banner_enabled=1, warning_banner_dismissible=1,
                        warning_banner_message="Community meeting on Friday.")
    page = worker_browser.open_page()
    page.locator(HIDE).click()
    assert _visible(page, "[data-kind=announcement]")
    page.locator("[data-kind=announcement] [data-dismiss-status]").click()
    page.reload()
    page.wait_for_selector("#chat-input")
    assert not _visible(page) and page.locator("[data-kind=announcement]").count() == 0
    with worker_browser.app.app_context():
        settings.update(maintenance_mode=1, maintenance_message="A short maintenance window.",
                        warning_banner_message="Community meeting moved to Saturday.")
    _poll(page)
    page.wait_for_selector("[data-kind=maintenance]")
    assert _visible(page, "[data-kind=maintenance]") and _visible(page, "[data-kind=announcement]")
    assert page.locator("[data-kind=maintenance] [data-hide-worker-status]").count() == 0
    assert page.locator("#send-button").is_disabled() and not _visible(page)


@pytest.mark.parametrize("stored", ["nonsense", "NaN", "-1", "0", "9999999999999999"])
def test_invalid_or_future_worker_dismissals_cannot_suppress_notice(worker_browser, stored):
    page = worker_browser.open_page()
    key = _key(page)
    page.evaluate("([key, value]) => localStorage.setItem(key, value)", [key, stored])
    page.reload()
    page.wait_for_selector("#chat-input")
    assert _visible(page)
    assert page.evaluate("key => localStorage.getItem(key)", key) is None
    assert _status(page)["can_send"] is True


def test_unavailable_storage_can_hide_within_page_without_breaking_polls(worker_browser):
    page = worker_browser.open_page(init_script="""Object.defineProperty(window, 'localStorage', {
      configurable: true, get() { throw new DOMException('Storage disabled', 'SecurityError'); }
    });""")
    assert _visible(page)
    page.locator(HIDE).click()
    assert not _visible(page)
    _poll(page)
    assert not _visible(page)
    page.reload()
    page.wait_for_selector("#chat-input")
    assert _visible(page)  # no promise of persistence when browser storage is disabled


def test_public_login_hides_empty_warning_wrapper_and_restores_it_at_expiry_without_polling(worker_browser):
    from bananachat.db import settings

    page = worker_browser.open_page(None)
    polls = []
    page.on("request", lambda request: polls.append(request.url) if "/status?banner=1" in request.url else None)
    assert page.locator(".auth-status").is_visible()
    assert _visible(page)
    page.clock.install(time=datetime.now(timezone.utc))
    page.clock.pause_at(datetime.now(timezone.utc))
    page.locator(HIDE).click()
    assert page.locator(".auth-status").is_hidden()
    assert _key(page).endswith(":public")
    page.reload()
    page.wait_for_selector("#username")
    assert page.locator(".auth-status").is_hidden()
    page.clock.fast_forward(DAY_MS + 1)
    page.wait_for_function("document.querySelector('.auth-status').hidden === false")
    assert page.locator(".auth-status").is_visible() and _visible(page)
    assert polls == []  # public pages have no authenticated status poll
    with worker_browser.app.app_context():
        settings.update(worker_offline_warning_enabled=0)
    page.reload()
    page.wait_for_selector("#username")
    assert page.locator(".auth-status").count() == 0
    assert page.locator(WORKER).count() == 0


@pytest.mark.parametrize("language, width", [("en", 1280), ("en", 390), ("it", 1280), ("it", 390)])
def test_worker_hide_button_is_translated_keyboard_accessible_and_fits(worker_browser, language, width):
    from bananachat.db import users
    from bananachat.i18n import translate

    with worker_browser.app.app_context():
        user = users.get_by_username("alice")
        users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": language})
    page = worker_browser.open_page(width=width)
    button = page.locator(HIDE)
    assert button.inner_text() == translate(language, "status.hide_worker_24h")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    bounds = button.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1
    button.focus()
    page.keyboard.press("Enter")
    assert not _visible(page)
    assert page.locator("#chat-input").is_enabled()
