#!/usr/bin/env python3
"""Drive the interface in Chromium against a temporary instance.

Starts BananaChat with an imitation Ollama server, signs in as a user and an
administrator, and checks the pages that matter: sign-in with CSRF, chatting
with a streamed answer, Markdown escaping, the model picker, the mobile
layout, maintenance mode (site stays up, sending paused, banner shown) and
the reasoning-effort selector (locked levels lead to a request), the main
account, developer and administration pages, and an agent task
(live timeline, tool calls, workspace browser, Stop; with a fake sandbox runner). Any JavaScript error,
failed request to our own server or Content-Security-Policy violation fails
the check. Screenshots go to ``.browser-artifacts/``.

Usage: ``python scripts/check_browser.py`` (needs ``playwright`` and Chromium;
set ``BC_CHROMIUM`` to use a specific browser binary).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACTS = ROOT / ".browser-artifacts"
HOSTILE = '<img src=x onerror="window.__xss=1">'


def build_app(instance: Path, ollama_url: str, agents_env: dict | None = None):
    from bananachat import create_app, db, security
    from bananachat.config import load_config
    from bananachat.db import catalog, settings, users
    from bananachat.services import ollama

    config = load_config({"BC_INSTANCE_DIR": str(instance), "BC_OLLAMA_URL": ollama_url, "BC_ENV": "testing",
                          "BC_SECRET_KEY": "browser-check-secret-key-0123456789abcdef", "BC_LOGGING_LEVEL": "minimal",
                          "BC_MIN_FREE_MEMORY_MB": "0", "BC_DEFAULT_LANGUAGE": "en", **(agents_env or {})})
    app = create_app(config)
    with app.test_request_context():
        with db.transaction():
            users.create("admin", security.hash_password("admin-password"), role="admin")
            users.create("alice", security.hash_password("alice-password"))
            settings.update(setup_done=1, site_name="Browser Check")
        ollama.sync_catalog()
        for model in catalog.list_models():
            catalog.set_rollout(model["id"], True)
        from bananachat.db import personalities
        alice = users.get_by_username("alice")
        personalities.create(alice["id"], f"Tutor {HOSTILE}", "Explain step by step.", created_by=alice["id"],
                             avatar="🦉", color="green", description=HOSTILE, greeting=f"Hello {HOSTILE}",
                             starters=["Teach me fractions"])
        from bananachat.db import tokens
        tokens.create(alice["id"], "Laptop script")  # the developer page lists it in a scrolled table
        from bananachat.db import credits
        # Exercise the account's wide history table at phone widths.
        history = credits.submit_request(alice["id"], 100_000, reason="Additional room for a course project.",
                                         kind="window", pool="api")
        credits.resolve_request(history["id"], users.get_by_username("admin")["id"], False)
    return app


def serve(app):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def chromium_path() -> str | None:
    if os.environ.get("BC_CHROMIUM"):
        return os.environ["BC_CHROMIUM"]
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(candidate)
    return None


class Checker:
    def __init__(self, page, base):
        self.page, self.base, self._problems = page, base, []
        self._failed_requests = []
        self._completed_streams = set()
        page.on("console", self._console)
        page.on("pageerror", lambda error: self._problems.append(f"page error: {error}"))
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    @property
    def problems(self):
        # Chromium can label a fully consumed no-store fetch stream ERR_ABORTED.
        # Only streams whose completion was verified in the UI qualify.
        return self._problems + [f"request failed: {request.url} {request.failure}"
                                 for request in self._failed_requests
                                 if request not in self._completed_streams or request.failure != "net::ERR_ABORTED"]

    def complete_stream(self, request):
        self._completed_streams.add(request)

    def _console(self, message):
        if message.type == "error" and "favicon" not in message.text:
            self._problems.append(f"console error on {self.page.url}: {message.text}")

    def _failed(self, request):
        if request.url.startswith(self.base) and "/chat/" not in request.url:
            self._failed_requests.append(request)

    def _response(self, response):
        if response.url.startswith(self.base) and response.status >= 500:
            self._problems.append(f"HTTP {response.status} for {response.url}")

    def shot(self, name):
        self.page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)

    def sign_in(self, username, password):
        self.page.goto(self.base + "/login")
        self.page.fill("#username", username)
        self.page.fill("#password", password)
        self.page.wait_for_timeout(500)  # the form's minimum fill time
        self.page.click("button[type=submit]")
        self.page.wait_for_load_state("networkidle")
        assert "/login" not in self.page.url, f"sign-in failed for {username}"


def check_models(browser, base, app, fake) -> list[str]:
    """Models: a new model waits for review and is enabled; bulk downloads queue and run; the pages fit a phone."""
    from bananachat.db import catalog
    from bananachat.services import ollama

    failures = []
    fake.models.append("fresh-model:7b")
    fake.details["fresh-model:7b"] = {"parameter_size": "7.6B", "family": "qwen2"}
    fake.capabilities["fresh-model:7b"] = ["completion", "thinking"]
    with app.app_context():
        ollama.sync_catalog()
        fresh = catalog.get_by_name("fresh-model:7b")
    for width, height, label in ((390, 844, "mobile"), (1280, 860, "desktop")):
        context = browser.new_context(viewport={"width": width, "height": height}, locale="en-US")
        page = context.new_page()
        check = Checker(page, base)
        check.sign_in("admin", "admin-password")
        page.goto(base + "/admin/models")
        if page.locator("#new-models").count() == 0:
            failures.append(f"models ({label}): no card for the new model")
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 1:
            failures.append(f"models ({label}): the page scrolls sideways by {overflow}px")
        check.shot(f"models-catalog-{label}")
        page.goto(base + f"/admin/models/{fresh['id']}/enable")
        check.shot(f"models-enable-{label}")
        page.goto(base + "/admin/models/downloads")
        check.shot(f"models-downloads-{label}")
        page.goto(base + "/admin/models/settings")
        check.shot(f"models-settings-{label}")
        page.goto(base + f"/admin/models/{fresh['id']}/edit")
        check.shot(f"models-edit-{label}")
        if label == "desktop":
            page.goto(base + f"/admin/models/{fresh['id']}/enable")
            page.fill("#display-name", "Fresh Model")
            page.click("button:has-text('Enable and publish')")
            page.wait_for_load_state("networkidle")
            with app.app_context():
                row = catalog.get_by_name("fresh-model:7b")
            if not row["is_rolled_out"] or row["enrollment"] != "reviewed" or row["limit_preset"] != "standard":
                failures.append("models: enabling the new model did not publish it with the standard preset")
            page.goto(base + "/admin/models/downloads")
            page.fill("#bulk-names", "bulk-one:1b\nbulk-two:1b")
            page.click("button:has-text('Queue downloads')")
            page.wait_for_load_state("networkidle")
            if page.locator("tr[data-job]").count() < 2:
                failures.append("models: the bulk download did not queue both models")
            page.wait_for_timeout(1500)  # the progress poll runs once
        failures.extend(f"models ({label}): {problem}" for problem in check.problems)
        context.close()
    return failures


def check_agents(browser, base, app, fake, runner) -> list[str]:
    """Agents: start a task, watch the live timeline, expand a tool call, browse the workspace, stop a task."""
    from bananachat.db import access, users
    from bananachat.services.agents import service
    from bananachat.services.agents import settings as agent_settings

    with app.app_context():
        service.refresh_model_capabilities(force=True)
        agent_settings.save({"enabled": True, "max_tasks_per_user": 2, "starts_per_hour": 100}, None)
        access.add_membership("agents", 0, users.get_by_username("alice")["id"], "allowlist", added_by=None)

    def responder(body):
        done = len([message for message in body["messages"] if message["role"] == "tool"])
        script = [
            {"content": f"Starting {HOSTILE}", "tool_calls": [{"name": "bash", "arguments": {"command": f"echo '{HOSTILE}'"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "app.py", "content": "print('hi')\n"}}]},
            {"tool_calls": [{"name": "finish", "arguments": {"summary": f"Made **app.py** {HOSTILE}"}}]},
        ]
        return script[min(done, len(script) - 1)]

    fake.tool_responder = responder
    failures = []
    for label, viewport in (("desktop", {"width": 1280, "height": 860}), ("mobile", {"width": 390, "height": 844})):
        context = browser.new_context(viewport=viewport, locale="en-US")
        page = context.new_page()
        check = Checker(page, base)
        check.sign_in("alice", "alice-password")
        page.goto(base + "/agents")
        if page.locator("nav#main-nav a[href='/agents']").count() == 0:
            failures.append(f"agents {label}: no navigation entry")
        page.fill("#task-prompt", "Write app.py")
        page.click("#new-task-submit")
        page.wait_for_url("**/agents/*")
        page.wait_for_selector(".step-summary", timeout=20000)
        page.click(".tool-call summary >> nth=0")
        page.wait_for_selector(".tool-body")
        page.wait_for_selector(".ws-entry:has-text('app.py')", timeout=10000)
        if page.evaluate("window.__xss === 1"):
            failures.append(f"agents {label}: task output executed injected HTML")
        if page.locator("#task-status[data-status=finished]").count() == 0:
            failures.append(f"agents {label}: the finished state is not shown")
        check.shot(f"agents-{label}-task")
        fake.tool_delay = 30
        page.goto(base + "/agents")
        page.fill("#task-prompt", "Take your time")
        page.click("#new-task-submit")
        page.wait_for_url("**/agents/*")
        page.wait_for_selector("#stop-button:not([hidden])")
        page.click("#stop-button")
        page.wait_for_selector("#task-status[data-status=stopped]", timeout=10000)
        fake.tool_delay = 0
        check.shot(f"agents-{label}-stopped")
        page.goto(base + "/agents")
        check.shot(f"agents-{label}-list")
        failures.extend(f"agents {label}: {problem}" for problem in check.problems)
        context.close()
    return failures


def prepare_effort(app) -> None:
    """A reasoning model (on/off) whose "on" level alice has not unlocked."""
    from bananachat.db import catalog, users
    from bananachat.db import limits as limits_db

    with app.app_context():
        model = catalog.get_by_name("qwen3:4b")
        catalog.update(model["id"], is_reasoning=1)
        limits_db.set_effort_level(users.get_by_username("alice")["id"], None, "low")


def check_effort(page, base) -> list[str]:
    failures = []
    page.click("#model-button")
    page.wait_for_selector("#model-popover:not([hidden])")
    page.click(".model-option >> text=Qwen3")
    page.wait_for_selector("#effort-picker:not([hidden])")

    # A stale or manually edited browser preference cannot break an otherwise
    # allowed selection. Each choice must replace it with a usable per-model map.
    for invalid in ("[]", '"old preference"', "5", "null", "{broken"):
        page.evaluate("value => localStorage.setItem('bc-chat-effort', value)", invalid)
        page.reload()
        page.wait_for_selector("#effort-picker:not([hidden])")
        page.click("#effort-button")
        page.click("#effort-list .effort-option:not(.is-locked)")
        saved = page.evaluate("JSON.parse(localStorage.getItem('bc-chat-effort'))")
        if not isinstance(saved, dict) or saved.get("qwen3:4b") != "off":
            failures.append(f"effort: malformed preference {invalid!r} prevented an allowed choice")

    # Open the selectors using their buttons' keyboard controls. Moving to a
    # different selector closes the previous one without stealing focus.
    for button, panel, focused in (("model", "model", "model-search"),
                                   ("effort", "effort", "effort-list"),
                                   ("persona", "persona", "persona-list")):
        page.locator(f"#{button}-button").focus()
        page.keyboard.press("ArrowDown")
        visible = page.locator(".composer .model-popover:visible")
        if visible.count() != 1 or visible.first.get_attribute("id") != f"{panel}-popover":
            failures.append(f"composer: keyboard opening {button} leaves overlapping selectors")
        if page.evaluate("document.activeElement.id") != focused:
            failures.append(f"composer: keyboard opening {button} lost focus")

    # The personality menu's management link remains reachable before Tab
    # continues to the next composer control.
    page.keyboard.press("Tab")
    if page.evaluate("document.activeElement.id") != "persona-manage" or page.locator("#persona-popover").is_hidden():
        failures.append("composer: Tab cannot reach the personality management link")
    page.keyboard.press("Tab")
    if page.evaluate("document.activeElement.id") != "attach-button" or not page.locator("#persona-popover").is_hidden():
        failures.append("composer: leaving a selector resets the keyboard order")

    page.locator("#model-button").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Tab")
    if page.evaluate("document.activeElement.id") != "effort-button" or not page.locator("#model-popover").is_hidden():
        failures.append("composer: Tab from model search skips the reasoning control")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    if page.evaluate("document.activeElement.id") != "effort-button" or not page.locator("#effort-popover").is_hidden():
        failures.append("composer: Escape does not restore the reasoning button")

    viewport = page.viewport_size
    page.set_viewport_size({"width": viewport["width"], "height": 240})
    page.locator("#effort-button").focus()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("End")
    geometry = page.locator("#effort-list").evaluate("""list => {
        const active = document.getElementById(list.getAttribute('aria-activedescendant'));
        const item = active.getBoundingClientRect(), bounds = list.getBoundingClientRect();
        return {top: item.top, bottom: item.bottom, listTop: bounds.top, listBottom: bounds.bottom,
                scrollHeight: list.scrollHeight, clientHeight: list.clientHeight, scrollTop: list.scrollTop};
    }""")
    if geometry["scrollHeight"] <= geometry["clientHeight"] or geometry["scrollTop"] <= 0:
        failures.append(f"effort: keyboard selection did not scroll the short list ({geometry})")
    if geometry["top"] < geometry["listTop"] - 1 or geometry["bottom"] > geometry["listBottom"] + 1:
        failures.append("effort: the active keyboard option is outside the visible list")
    page.keyboard.press("Escape")
    page.set_viewport_size(viewport)

    page.click("#effort-button")
    page.wait_for_selector("#effort-popover:not([hidden])")
    options = page.locator("#effort-list .effort-option")
    if options.count() != 2:
        failures.append(f"effort: expected 2 levels, found {options.count()}")
    locked = page.locator("#effort-list .effort-option.is-locked")
    if locked.count() != 1 or locked.first.locator(".effort-lock svg").count() != 1:
        failures.append("effort: the locked level shows no lock")
    page.click("#effort-list .effort-option:not(.is-locked)")
    if not page.locator("#effort-popover").is_hidden() or page.inner_text("#effort-button-label").strip() != "Off":
        failures.append("effort: choosing an allowed level did not update the button")
    page.click("#effort-button")
    locked.first.click()
    page.wait_for_url("**/account?**")
    if page.locator("#quota_kind").input_value() != "effort":
        failures.append("effort: the request form did not open on a reasoning-effort request")
    page.goto(base + "/chat")
    page.wait_for_selector("#chat-input")
    page.click("#model-button")
    page.wait_for_selector("#model-popover:not([hidden])")
    page.click(".model-option >> text=Automatic")
    if not page.locator("#effort-picker").is_hidden():
        failures.append("effort: the selector stays visible for the automatic model")
    return failures


def check_navigation(check, viewport) -> list[str]:
    """All links stay visible when they fit; the small-screen menu is operable."""
    page = check.page
    failures = []
    page.goto(check.base + "/chat")
    page.wait_for_selector(".topbar.nav-ready")
    if page.locator("#main-nav details").count():
        failures.append("navigation: destinations are still nested in an extra menu")
    for width in (1280, 800, 390):
        page.set_viewport_size({"width": width, "height": 860})
        page.wait_for_function("collapsed => document.querySelector('.topbar').classList.contains('nav-collapsed') === collapsed",
                               arg=width == 390)
        if width >= 800 and not page.locator('#main-nav > a[href="/developer"]').is_visible():
            failures.append(f"navigation {width}: a direct destination is hidden despite available space")
        if page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") > 1:
            failures.append(f"navigation {width}: the header overflows")
    toggle = page.locator("[data-nav-toggle]")
    toggle.click()
    first = page.locator("#main-nav > a").first
    first.focus()
    page.keyboard.press("Escape")
    if toggle.get_attribute("aria-expanded") != "false" or not toggle.evaluate("element => element === document.activeElement"):
        failures.append("navigation: Escape did not close the menu and return focus")
    toggle.click()
    page.locator("#chat-input").click()
    if toggle.get_attribute("aria-expanded") != "false":
        failures.append("navigation: an outside click did not close the menu")
    if page.locator("#composer-box").evaluate("element => getComputedStyle(element).boxShadow") != "none":
        failures.append("composer: focusing the input adds a shadow")
    account_menu = page.locator(".topbar-account")
    account_menu.locator("summary").focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    if not account_menu.evaluate("element => element.open && element.contains(document.activeElement)"):
        failures.append("navigation: moving focus into the account menu closed it")
    account_menu.locator("button").last.focus()
    page.keyboard.press("Tab")
    if account_menu.evaluate("element => element.open"):
        failures.append("navigation: the account menu stayed open after tabbing away")
    page.goto(check.base + "/personalities")
    import_menu = page.locator("#import")
    import_menu.locator("summary").click()
    page.locator("#import-file").focus()
    page.locator("#import-hint").click()
    if not import_menu.evaluate("element => element.open"):
        failures.append("navigation: clicking import help text closed its menu")
    page.locator(".page-header h1").click()
    if import_menu.evaluate("element => element.open"):
        failures.append("navigation: an outside click left the import menu open")
    page.goto(check.base + "/chat")
    page.set_viewport_size(viewport)
    return failures


def check_playground(check) -> list[str]:
    """Collapsed parameters remain usable, including invalid-field recovery."""
    page, base = check.page, check.base
    failures = []
    page.goto(base + "/developer/playground")
    parameters = page.locator(".dev-parameters")
    if parameters.evaluate("element => element.open"):
        failures.append("playground: optional parameters are initially expanded")
    parameters.locator("summary").click()
    page.select_option("#pg-model", "qwen3:4b")
    page.wait_for_selector("#pg-reasoning:not([disabled])")
    page.select_option("#pg-reasoning", "none")
    page.fill("#pg-temperature", "3")
    page.fill("#pg-input", "Check the playground controls")
    parameters.locator("summary").click()
    page.click("#pg-send")
    page.wait_for_selector(".dev-parameters[open] #pg-temperature")
    if page.evaluate("document.activeElement.id") != "pg-temperature":
        failures.append("playground: an invalid hidden parameter did not receive focus")
    if page.locator(".dev-msg").count():
        failures.append("playground: invalid parameters started a conversation")
    page.fill("#pg-temperature", "0.7")
    page.fill("#pg-top-p", "0.9")
    page.fill("#pg-max-tokens", "64")
    with page.expect_request(base + "/developer/playground/send") as sent:
        page.click("#pg-send")
    body = sent.value.post_data_json
    if any(body.get(key) != value for key, value in
           (("temperature", 0.7), ("top_p", 0.9), ("max_tokens", 64), ("reasoning_effort", "none"))):
        failures.append(f"playground: selected parameters were not sent ({body})")
    page.wait_for_selector("#pg-log .dev-msg-body >> text=escaped.", timeout=20000)
    page.wait_for_selector("#pg-log .dev-msg-meta", timeout=20000)
    page.wait_for_selector("#pg-send:not([hidden])", timeout=20000)
    page.wait_for_selector('#pg-log[aria-busy="false"]', timeout=20000)
    response = sent.value.response()
    if response is None or response.status != 200 or page.locator("#pg-log .dev-msg-error").count():
        failures.append("playground: the streamed request did not finish cleanly")
    else:
        check.complete_stream(sent.value)
    if page.evaluate("window.__xss === 1"):
        failures.append("playground: streamed Markdown executed injected HTML")
    return failures


def check_settings_deep_links(browser, base) -> list[str]:
    """Initial touch fragments reveal their fields clear of the sticky header."""
    failures = []
    for width in (320, 390):
        context = browser.new_context(viewport={"width": width, "height": 844},
                                      is_mobile=True, has_touch=True, locale="en-US")
        page = context.new_page()
        check = Checker(page, base)
        try:
            check.sign_in("admin", "admin-password")
            for path, target in (("/customize#custom_bg", "custom_bg"),
                                 ("/admin/settings#primary_color", "primary_color")):
                page.goto(base + path)
                page.wait_for_timeout(500)  # include Chromium's delayed fragment adjustment
                geometry = page.locator("#" + target).evaluate("""element => {
                    const rect = element.getBoundingClientRect();
                    return {top: rect.top, bottom: rect.bottom, height: innerHeight,
                            header: document.querySelector('.topbar').getBoundingClientRect().bottom};
                }""")
                if geometry["top"] < geometry["header"] or geometry["bottom"] > geometry["height"]:
                    failures.append(f"settings: {path} at {width}px hides its field ({geometry})")
                page.mouse.wheel(0, -10000)
                page.wait_for_timeout(100)
                if page.evaluate("window.scrollY") > 1:
                    failures.append(f"settings: {path} at {width}px overrides the user's scroll")
            failures.extend(f"settings at {width}px: {problem}" for problem in check.problems)
        finally:
            context.close()
    return failures


def check_customization(browser, base, app) -> list[str]:
    """Delayed writes cannot undo newer preferences or a confirmed reset."""
    from bananachat.db import users

    context = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    Checker(page, base).sign_in("alice", "alice-password")
    with app.app_context():
        user_id = users.get_by_username("alice")["id"]
        original = users.get_preferences(user_id)
    held = []

    def pause_save(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()

    def wait_for_writes(count):
        deadline = time.monotonic() + 5
        while len(held) < count and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert len(held) == count, f"preferences: expected {count} writes, got {len(held)}"

    def persisted():
        return page.evaluate("""async () => (await (await fetch('/api/preferences')).json()).preferences""")

    def wait_saved():
        page.wait_for_function("() => document.querySelector('#save-status').classList.contains('is-saved')")

    def reset():
        page.locator("#reset-all").click()
        page.locator("dialog[open] .btn-danger").click()

    try:
        page.goto(base + "/customize")
        page.route(base + "/api/preferences", pause_save)
        page.locator('label:has(input[name="theme_mode"][value="light"])').click()
        wait_for_writes(1)
        page.locator('label:has(input[name="theme_mode"][value="dark"])').click()
        page.wait_for_timeout(600)
        assert len(held) == 1, "preferences: a newer save overtook a delayed earlier save"
        held[0].continue_()
        wait_for_writes(2)
        held[1].continue_()
        wait_saved()
        assert persisted()["theme_mode"] == "dark"
        assert page.locator('input[name="theme_mode"][value="dark"]').is_checked()

        # An unsuccessful write must release the queue for a later change.
        held.clear()
        page.locator('label:has(input[name="contrast"][value="1"])').click()
        wait_for_writes(1)
        page.locator('label:has(input[name="contrast"][value="2"])').click()
        page.wait_for_timeout(600)
        assert len(held) == 1
        held[0].fulfill(status=500, content_type="application/json", body='{"error":{"message":"Fixture failure"}}')
        wait_for_writes(2)
        held[1].continue_()
        wait_saved()
        assert persisted()["contrast"] == 2, "preferences: a failed save stranded later changes"

        # Reset follows an active save and cancels changes still in the debounce.
        held.clear()
        page.locator("#font_scale").select_option("1.2")
        wait_for_writes(1)
        page.locator('label:has(input[name="contrast"][value="3"])').click()
        reset()
        page.wait_for_function("() => document.querySelector('#reset-all').disabled")
        page.wait_for_timeout(600)
        assert len(held) == 1, "preferences: reset left a pending appearance save"
        held[0].continue_()
        wait_saved()
        page.wait_for_timeout(600)
        prefs = persisted()
        assert len(held) == 1
        assert (prefs["theme_mode"], prefs["font_scale"], prefs["contrast"]) == ("default", 1, 0)
        page.unroute(base + "/api/preferences", pause_save)

        # The language reload survives a newer queued appearance change.
        held.clear()
        page.route(base + "/api/preferences", pause_save)
        page.locator("#interface_language").select_option("it")
        wait_for_writes(1)
        page.locator("#font_scale").select_option("1.2")
        page.wait_for_timeout(600)
        assert len(held) == 1
        held[0].continue_()
        wait_for_writes(2)
        held[1].continue_()
        page.wait_for_function("() => document.documentElement.lang === 'it'")
        prefs = persisted()
        assert prefs["interface_language"] == "it" and prefs["font_scale"] == 1.2
        page.unroute(base + "/api/preferences", pause_save)

        # Primary labels stay readable for bright and dark custom colors, in
        # live preview and after reloading the server-rendered theme.
        page.locator("#colours > summary").click()
        for theme, primary, expected in (("light", "#e6be32", "rgb(0, 0, 0)"),
                                         ("dark", "#112244", "rgb(255, 255, 255)")):
            page.locator(f'label:has(input[name="theme_mode"][value="{theme}"])').click()
            page.locator("#custom_primary").evaluate("""(element, color) => {
                element.value = color;
                element.dispatchEvent(new Event('input', {bubbles: true}));
            }""", primary)
            wait_saved()
            for reload in (False, True):
                if reload:
                    page.reload()
                colors = page.locator(".customize-preview .btn-primary").evaluate("""element => {
                    const style = getComputedStyle(element);
                    return {text: style.color, background: style.backgroundColor};
                }""")
                assert colors["text"] == expected, f"primary text: {theme} {primary} ({colors})"
            # Reload closes optional color controls.
            page.locator("#colours > summary").click()

        # Real native form validation must reveal closed ancestors before focus.
        page.evaluate("""() => {
          const form = document.createElement('form');
          form.id = 'validation-fixture';
          form.addEventListener('submit', event => event.preventDefault());
          const outer = document.createElement('details');
          const inner = document.createElement('details');
          for (const [details, text] of [[outer, 'Outer'], [inner, 'Inner']]) {
            const summary = document.createElement('summary');
            summary.textContent = text;
            details.append(summary);
          }
          const input = document.createElement('input');
          input.id = 'validation-fixture-input';
          input.required = true;
          input.setAttribute('aria-label', 'Required fixture value');
          inner.append(input);
          outer.append(inner);
          form.append(outer);
          document.querySelector('main').append(form);
          form.requestSubmit();
        }""")
        page.wait_for_function("() => document.activeElement.id === 'validation-fixture-input'")
        assert page.locator("#validation-fixture details[open]").count() == 2
        page.locator("#validation-fixture").evaluate("form => form.remove()")
        assert not errors, f"customization: browser errors: {errors}"
    finally:
        page.unroute(base + "/api/preferences", pause_save)
        with app.app_context():
            users.save_preferences(user_id, original)
        context.close()
    return []


def check_touch_composer(browser, base, app) -> list[str]:
    """Compact pickers stay tappable while a personality-change toast is visible."""
    from bananachat.db import catalog

    failures = []
    with app.app_context():
        model = catalog.get_by_name("qwen3:4b")
        original_name = model["display_name"]
        long_name = "Qwen3 Thinking — Extended Context Research Model"
        catalog.update(model["id"], display_name=long_name)
    try:
        for width in (320, 390):
            context = browser.new_context(viewport={"width": width, "height": 844}, locale="en-US",
                                          is_mobile=True, has_touch=True)
            try:
                page = context.new_page()
                check = Checker(page, base)
                check.sign_in("alice", "alice-password")
                page.goto(base + "/chat")
                page.locator("#model-button").tap()
                page.locator("#model-list .model-option").filter(has_text=long_name).tap()
                page.wait_for_selector("#effort-picker:not([hidden])")
                page.locator("#persona-button").tap()
                page.locator("#persona-list [role=option]").nth(1).tap()
                page.wait_for_selector("#toasts .toast-success")
                targets = page.evaluate("""selectors => selectors.map(selector => {
                  const button = document.querySelector(selector);
                  const rect = button.getBoundingClientRect();
                  const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                  return { selector, width: rect.width, height: rect.height,
                    inside: rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight,
                    tappable: hit === button || button.contains(hit) };
                })""", ["#model-button", "#effort-button", "#persona-button", "#params-toggle", "#send-button"])
                for target in targets:
                    if target["width"] < 44 or target["height"] < 44:
                        failures.append(f"touch {width}: {target['selector']} is smaller than 44 × 44 ({target})")
                    if not target["inside"] or not target["tappable"]:
                        failures.append(f"touch {width}: {target['selector']} is clipped or covered ({target})")
                check.shot(f"touch-composer-{width}")
                page.locator("#params-toggle").tap()
                page.wait_for_selector("#params-panel:not([hidden])")
                page.keyboard.press("Escape")
                page.locator("#persona-button").tap()
                page.locator("#persona-list [role=option]").first.tap()
                page.wait_for_selector("#persona-chip", state="hidden")
                failures.extend(f"touch {width}: {problem}" for problem in check.problems)
            finally:
                context.close()
    finally:
        with app.app_context():
            catalog.update(model["id"], display_name=original_name)
    return failures


def run() -> int:
    from playwright.sync_api import sync_playwright

    from tests.app.fake_ollama import FakeOllama

    shutil.rmtree(ARTIFACTS, ignore_errors=True)
    ARTIFACTS.mkdir()
    fake = FakeOllama().start()
    from tests.app.fake_runner import TOKEN, FakeRunner
    runner = FakeRunner().start()
    fake.reply = f"Here is **bold** text, `code` and {HOSTILE} escaped.\n\n```python\nprint('hi')\n```"
    fake.chunk_delay = 0.01
    instance = Path(tempfile.mkdtemp(prefix="bananachat-browser-"))
    token = instance / "runner.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    app = build_app(instance, fake.url, {"BC_AGENTS_RUNNER_URL": runner.url, "BC_AGENTS_RUNNER_TOKEN_FILE": str(token)})
    prepare_effort(app)
    server, base = serve(app)
    failures: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium_path())
            failures.extend(check_customization(browser, base, app))
            failures.extend(check_touch_composer(browser, base, app))
            for label, viewport in (("desktop", {"width": 1280, "height": 860}),
                                    ("mobile", {"width": 390, "height": 844})):
                context = browser.new_context(viewport=viewport, locale="en-US")
                page = context.new_page()
                check = Checker(page, base)
                page.goto(base + "/login")
                check.shot(f"{label}-login")
                check.sign_in("alice", "alice-password")
                failures.extend(f"{label}: {problem}" for problem in check_navigation(check, viewport))

                page.goto(base + "/chat")
                page.wait_for_selector("#chat-input")
                if label == "mobile":
                    page.click("#sidebar-toggle")
                    page.wait_for_timeout(300)
                    check.shot("mobile-sidebar")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                page.fill("#chat-input", "Hello there")
                page.click("#send-button")
                page.wait_for_selector("text=escaped.", timeout=20000)
                page.wait_for_selector("#send-button:not([hidden])", timeout=20000)
                if page.evaluate("window.__xss === 1"):
                    failures.append(f"{label}: markdown executed injected HTML")
                if page.locator(".prose strong", has_text="bold").count() == 0:
                    failures.append(f"{label}: markdown bold was not rendered")
                page.reload()
                page.wait_for_selector("text=escaped.")
                check.shot(f"{label}-chat")

                page.click("#model-button")
                page.wait_for_selector("#model-popover:not([hidden])")
                box = page.locator("#model-popover").bounding_box()
                if box["x"] < 0 or box["x"] + box["width"] > viewport["width"]:
                    failures.append(f"{label}: the model picker does not fit the screen ({box})")
                page.keyboard.press("Escape")

                # Reasoning effort: the reasoning model offers its levels; the locked one shows a lock and leads
                # to the account page's request form, pre-filled.
                failures.extend(f"{label}: {problem}" for problem in check_effort(page, base))

                # Personality picker: switch mid-chat; names stay escaped. The
                # compact header leaves room for the title, with the chosen
                # personality still available in the composer.
                page.click("#persona-button")
                page.wait_for_selector("#persona-popover:not([hidden])")
                page.click(".persona-option >> text=Tutor")
                page.wait_for_selector("#persona-chip:not([hidden])", state="attached")
                if "Tutor" not in page.locator("#persona-button").get_attribute("aria-label") or page.evaluate("window.__xss === 1"):
                    failures.append(f"{label}: the personality picker did not switch safely")
                if page.locator("#persona-chip").is_visible() != (label == "desktop"):
                    failures.append(f"{label}: the personality header does not follow the compact layout")

                # The editor's live preview follows the form.
                page.goto(base + "/personalities/new?template=code_reviewer")
                page.fill("#name", "Preview check")
                if page.inner_text("[data-preview-name]") != "Preview check":
                    failures.append(f"{label}: the personality preview did not update")

                # The limit-request form shows only the fields of the chosen kind.
                page.goto(base + "/account#quota")
                page.select_option("#quota_kind", "rate")
                if not page.locator("#quota_rate").is_visible() or page.locator("#quota_credits").is_visible():
                    failures.append(f"{label}: the limit-request form did not switch to the rate fields")

                for path in ("/account", "/customize", "/personalities", "/developer", "/developer/playground"):
                    response = page.goto(base + path)
                    if response is None or response.status != 200:
                        failures.append(f"{label}: {path} answered {response and response.status}")
                    if path == "/account" and not page.locator("#quota .table-wrap table").is_visible():
                        failures.append(f"{label}: the populated quota-history table is missing")
                    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    if overflow > 1:
                        failures.append(f"{label}: {path} scrolls sideways by {overflow}px")
                    check.shot(f"{label}{path.replace('/', '-')}")
                failures.extend(f"{label}: {problem}" for problem in check_playground(check))
                failures.extend(f"{label}: {problem}" for problem in check.problems)
                context.close()

            # Administrator pages and maintenance mode.
            context = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US")
            page = context.new_page()
            check = Checker(page, base)
            check.sign_in("admin", "admin-password")
            with app.app_context():
                from bananachat.db import users
                alice_limits = f"/admin/users/{users.get_by_username('alice')['id']}/limits"
            for path in ("/admin/", "/admin/users", "/admin/models", "/admin/models/downloads", "/admin/models/settings",
                         "/admin/access", "/admin/quotas",
                         "/admin/quotas/requests", "/admin/quotas/tiers", "/admin/quotas/grants", alice_limits,
                         "/admin/settings", "/admin/metrics", "/admin/audit", "/admin/chats", "/admin/workers",
                         "/admin/music", "/admin/invites", "/admin/migration", "/admin/personalities"):
                response = page.goto(base + path)
                if response is None or response.status != 200:
                    failures.append(f"admin: {path} answered {response and response.status}")
                check.shot("admin" + path.rstrip("/").replace("/", "-"))
            page.goto(base + "/admin/agents")
            check.shot("admin-agents")
            failures.extend(check_agents(browser, base, app, fake, runner))
            failures.extend(check_models(browser, base, app, fake))
            failures.extend(check_settings_deep_links(browser, base))
            with app.app_context():
                from bananachat.db import settings
                settings.update(maintenance_mode=1, maintenance_message="Back at 14:00")
            failures.extend(f"admin: {problem}" for problem in check.problems)
            context.close()

            context = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US")
            page = context.new_page()
            check = Checker(page, base)
            page.goto(base + "/login")
            if page.locator("[data-kind=maintenance]").count() == 0:
                failures.append("maintenance: no notice on the sign-in page")
            page.wait_for_timeout(600)
            check.shot("maintenance-login")
            check.sign_in("alice", "alice-password")
            page.goto(base + "/chat")
            page.wait_for_selector("[data-kind=maintenance]")
            if not page.locator("#send-button").is_disabled():
                failures.append("maintenance: the send button is not disabled")
            page.wait_for_timeout(600)  # let the banner finish appearing
            check.shot("maintenance-chat")
            with app.app_context():
                from bananachat.db import settings
                settings.update(maintenance_mode=0)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_selector("[data-kind=maintenance]", state="detached", timeout=10000)
            if page.locator("#send-button").is_disabled():
                failures.append("maintenance: sending did not resume after maintenance ended")
            failures.extend(f"maintenance: {problem}" for problem in check.problems)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        fake.stop()
        runner.stop()
        shutil.rmtree(instance, ignore_errors=True)

    (ARTIFACTS / "results.txt").write_text("\n".join(failures) or "All checks passed.\n")
    if failures:
        print("Browser check failed:\n  " + "\n  ".join(failures))
        return 1
    print(f"Browser check passed. Screenshots: {ARTIFACTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
