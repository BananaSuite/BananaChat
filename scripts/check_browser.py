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
        self.page, self.base, self.problems = page, base, []
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.problems.append(f"page error: {error}"))
        page.on("requestfailed", self._failed)
        page.on("response", self._response)

    def _console(self, message):
        if message.type == "error" and "favicon" not in message.text:
            self.problems.append(f"console error on {self.page.url}: {message.text}")

    def _failed(self, request):
        if request.url.startswith(self.base) and "/chat/" not in request.url:
            self.problems.append(f"request failed: {request.url} {request.failure}")

    def _response(self, response):
        if response.url.startswith(self.base) and response.status >= 500:
            self.problems.append(f"HTTP {response.status} for {response.url}")

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
            for label, viewport in (("desktop", {"width": 1280, "height": 860}),
                                    ("mobile", {"width": 390, "height": 844})):
                context = browser.new_context(viewport=viewport, locale="en-US")
                page = context.new_page()
                check = Checker(page, base)
                page.goto(base + "/login")
                check.shot(f"{label}-login")
                check.sign_in("alice", "alice-password")

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

                # Personality picker: switch mid-chat; the header shows it, names stay escaped.
                page.click("#persona-button")
                page.wait_for_selector("#persona-popover:not([hidden])")
                page.click(".persona-option >> text=Tutor")
                page.wait_for_selector("#persona-chip:not([hidden])")
                if "Tutor" not in page.inner_text("#persona-chip") or page.evaluate("window.__xss === 1"):
                    failures.append(f"{label}: the personality picker did not switch safely")

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
                    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    if overflow > 1:
                        failures.append(f"{label}: {path} scrolls sideways by {overflow}px")
                    check.shot(f"{label}{path.replace('/', '-')}")
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
