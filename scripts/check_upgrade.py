#!/usr/bin/env python3
"""Upgrade a real installation of the previous release to this release and check what people would notice.

1. Checks out the previous release (``--from``, default: the
   last commit before the rewrite) in a temporary Git worktree and runs it under Gunicorn with an
   imitation Ollama server.
2. Uses it through its own pages in Chromium: completes setup with the old
   site name "BananaAI", rolls out the models, creates a user who chats,
   shares the chat, creates an API token and a personality and chooses
   Italian and the light theme.
3. Starts this release on the same data directory and checks that sign-in
   cookies, chats, preferences, the share link, the API token and the
   administration pages still work, that "BananaAI" is gone and that the
   database passes SQLite's integrity check.

Usage: ``python scripts/check_upgrade.py`` (needs the full Git history,
``playwright`` and Chromium; set ``BC_CHROMIUM`` to use a specific browser).
The previous release gets its own virtual environment, installed from its
``requirements.txt`` unless ``--old-python`` names an interpreter that
already has them.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PREVIOUS_RELEASE = "4eac06bcc69f24ac51129a9710324d7398cf9ab1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def chromium_path() -> str | None:
    if os.environ.get("BC_CHROMIUM"):
        return os.environ["BC_CHROMIUM"]
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(candidate)
    return None


def start(python: str, code: Path, instance: Path, ollama_url: str, log: Path):
    environment = {**os.environ, "BC_INSTANCE_DIR": str(instance), "BC_OLLAMA_URL": ollama_url,
                   "BC_PORT": str(free_port()), "BC_MIN_FREE_MEMORY_MB": "0", "BC_MIN_FREE_DISK_GB": "0",
                   "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("BC_DATABASE_PATH", "BC_SECRET_KEY", "SECRET_KEY", "BC_SETUP_TOKEN"):
        environment.pop(name, None)
    output = open(log, "w")
    server = subprocess.Popen([python, "-m", "gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"], cwd=code,
                              env=environment, stdout=output, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{environment['BC_PORT']}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"The server stopped during start-up; see {log}")
        try:
            with OPENER.open(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return server, base, output
        except OSError:
            time.sleep(0.3)
    server.terminate()
    raise SystemExit(f"The server did not become healthy; see {log}")


def stop(server, output) -> None:
    server.terminate()
    server.wait(timeout=90)
    output.close()


def old_release(work: Path, revision: str, old_python: str | None) -> tuple[Path, str]:
    code = work / "previous"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(code), revision], check=True,
                   capture_output=True)
    if old_python:
        return code, old_python
    subprocess.run([sys.executable, "-m", "venv", str(work / "venv-previous")], check=True)
    python = str(work / "venv-previous/bin/python")
    subprocess.run([python, "-m", "pip", "install", "-q", "-r", str(code / "requirements.txt")], check=True)
    return code, python


def use_previous_release(playwright, base: str, instance: Path, state: dict, work: Path) -> None:
    """Set up and use the previous release through its pages."""
    def csrf(page):
        return page.get_attribute('meta[name="csrf-token"]', "content")

    def post_form(page, path, data):
        return page.request.post(base + path, form={**data, "csrf_token": csrf(page)})

    def json_post(page, path, data):
        return page.request.post(base + path, data=json.dumps(data),
                                 headers={"X-CSRFToken": csrf(page), "Content-Type": "application/json"})

    key = (instance / ".secret_key").read_text().strip()
    setup_token = hmac.new(key.encode(), b"initial-admin-setup", hashlib.sha256).hexdigest()
    browser = playwright.chromium.launch(executable_path=chromium_path())
    context = browser.new_context(viewport={"width": 1280, "height": 860})
    admin = context.new_page()
    admin.goto(base + "/setup")
    admin.fill("#setup_token", setup_token)
    admin.fill("#site_name", "BananaAI")
    admin.fill("#username", "admin")
    admin.fill("#password", "admin-password-1")
    admin.fill("#confirm_password", "admin-password-1")
    admin.wait_for_timeout(1500)  # the old forms' minimum fill time
    admin.click("button[type=submit]")
    admin.wait_for_load_state("networkidle")
    admin.goto(base + "/admin/models")
    post_form(admin, "/admin/models/sync", {})
    admin.goto(base + "/admin/models")
    for action in sorted(set(re.findall(r'action="(/admin/models/\d+/rollout)"', admin.content()))):
        post_form(admin, action, {"rolled_out": "1"})
    admin.goto(base + "/admin/users/create")
    post_form(admin, "/admin/users/create", {"username": "alice", "password": "alice-password-1", "role": "user"})
    context.storage_state(path=str(work / "admin-cookies.json"))

    context = browser.new_context(viewport={"width": 1280, "height": 860})
    alice = context.new_page()
    alice.goto(base + "/login")
    alice.fill("#username", "alice")
    alice.fill("#password", "alice-password-1")
    alice.wait_for_timeout(1500)
    alice.click("button[type=submit]")
    alice.wait_for_load_state("networkidle")
    alice.goto(base + "/chat")
    alice.fill("#chat-input", "Hi, this chat was written before the upgrade")
    alice.click("#chat-send-btn")
    alice.wait_for_selector("text=Hello from the previous release.", timeout=30000)
    alice.wait_for_timeout(1500)
    state["session_id"] = alice.url.rstrip("/").rsplit("/", 1)[1]
    state["share_path"] = "/share/" + json_post(alice, f"/chat/{state['session_id']}/share", {}).json()["token"]
    alice.goto(base + "/api")
    created = post_form(alice, "/api/tokens/create", {"name": "laptop"})
    state["api_token"] = re.search(r"(bc-[A-Za-z0-9_\-]{20,})", created.text()).group(1)
    alice.goto(base + "/personalities")
    post_form(alice, "/personalities/create", {"name": "Pirate", "instructions": "Talk like a pirate, always.",
                                               "enabled": "1"})
    json_post(alice, "/api/accessibility", {"theme_mode": "light", "interface_language": "it"})
    context.storage_state(path=str(work / "alice-cookies.json"))
    browser.close()


def check_this_release(playwright, base: str, state: dict, work: Path) -> list[str]:
    problems: list[str] = []

    def check(condition, message):
        print(("  ok    " if condition else "  FAIL  ") + message)
        if not condition:
            problems.append(message)

    def watch(page, label):
        page.on("pageerror", lambda error: problems.append(f"{label}: page error {error}"))
        page.on("console", lambda message: message.type == "error" and "favicon" not in message.text
                and problems.append(f"{label}: console error {message.text}"))
        page.on("response", lambda response: response.status >= 500
                and problems.append(f"{label}: HTTP {response.status} {response.url}"))

    browser = playwright.chromium.launch(executable_path=chromium_path())
    context = browser.new_context(storage_state=str(work / "alice-cookies.json"),
                                  viewport={"width": 1280, "height": 860})
    alice = context.new_page()
    watch(alice, "user")
    alice.goto(base + "/chat/" + state["session_id"])
    check("/login" not in alice.url, "a user's sign-in cookie from the previous release still works")
    check("BananaAI" not in alice.content() and "BananaChat" in alice.title(), "the chat page says BananaChat")
    check(alice.locator("text=Hi, this chat was written before the upgrade").count() > 0
          and alice.locator("text=Hello from the previous release.").count() > 0, "the old chat is intact")
    check(alice.get_attribute("html", "lang") == "it", "the Italian interface preference is kept")
    check("light" in str(alice.evaluate("document.documentElement.dataset.theme")), "the light theme is kept")
    alice.fill("#chat-input", "And this one after the upgrade")
    alice.click("#send-button")
    alice.wait_for_selector("text=Hello from this release.", timeout=30000)
    alice.wait_for_selector("#send-button:not([hidden])", timeout=30000)
    alice.reload()
    check(alice.locator("text=Hello from this release.").count() > 0, "a new answer is saved in the old chat")
    alice.goto(base + "/personalities")
    check(alice.locator("text=Pirate").count() > 0, "the personality is kept")
    alice.goto(base + "/developer")
    check(alice.locator("text=laptop").count() > 0, "the API token is listed")
    for path in ("/account", "/customize", "/developer/usage"):
        check(alice.goto(base + path).status == 200, f"{path} opens")
    context.close()

    visitor = browser.new_context().new_page()
    watch(visitor, "visitor")
    response = visitor.goto(base + state["share_path"])
    check(response.status == 200 and visitor.locator("text=Hello from the previous release.").count() > 0,
          "a share link from the previous release still works")
    bearer = {"Authorization": "Bearer " + state["api_token"], "Content-Type": "application/json"}
    check(visitor.request.get(base + "/v1/models", headers=bearer).status == 200, "an API token from the previous release works")
    answer = visitor.request.post(base + "/v1/chat/completions", headers=bearer, data=json.dumps(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}))
    check(answer.status == 200 and "this release" in answer.text(), "an API token from the previous release gets answers")
    status = visitor.request.get(base + "/status")
    check(status.status == 200 and status.json().get("status") == "ok", "GET /status reports ok")
    visitor.goto(base + "/login")
    check("BananaAI" not in visitor.content(), "the sign-in page says BananaChat")

    context = browser.new_context(viewport={"width": 390, "height": 844})
    admin = context.new_page()
    watch(admin, "administrator")
    admin.goto(base + "/login")
    admin.fill("#username", "admin")
    admin.fill("#password", "admin-password-1")
    admin.wait_for_timeout(600)
    admin.click("button[type=submit]")
    admin.wait_for_load_state("networkidle")
    check("/login" not in admin.url, "the administrator signs in with their existing password")
    for path in ("/admin/", "/admin/users", "/admin/models", "/admin/access", "/admin/settings", "/admin/metrics",
                 "/admin/chats", "/admin/audit", "/admin/quotas"):
        check(admin.goto(base + path).status == 200, f"{path} opens")
    context.close()
    context = browser.new_context(storage_state=str(work / "admin-cookies.json"))
    page = context.new_page()
    check(page.goto(base + "/admin/users").status == 200 and "/login" not in page.url,
          "the administrator's setup cookie from the previous release still works")
    browser.close()
    return problems


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="revision", default=PREVIOUS_RELEASE, help="the release to upgrade from")
    parser.add_argument("--old-python", help="an interpreter that already has the previous release's requirements")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory for inspection")
    options = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from tests.app.fake_ollama import FakeOllama

    work = Path(tempfile.mkdtemp(prefix="bananachat-upgrade-"))
    instance = work / "data"
    fake = FakeOllama().start()
    fake.chunk_delay = 0.01
    state: dict = {}
    problems: list[str] = []
    try:
        code, old_python = old_release(work, options.revision, options.old_python)
        print(f"Using {options.revision[:12]} under Gunicorn")
        fake.reply = "Hello from the previous release."
        server, base, output = start(old_python, code, instance, fake.url, work / "previous.log")
        try:
            with sync_playwright() as playwright:
                use_previous_release(playwright, base, instance, state, work)
        finally:
            stop(server, output)

        print("Starting this release on the same data")
        fake.reply = "Hello from this release."
        server, base, output = start(sys.executable, ROOT, instance, fake.url, work / "this-release.log")
        try:
            with sync_playwright() as playwright:
                problems = check_this_release(playwright, base, state, work)
        finally:
            stop(server, output)
        with sqlite3.connect(instance / "bananachat.db") as conn:
            site_name = conn.execute("SELECT site_name FROM site_settings").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if site_name != "BananaChat":
            problems.append(f"the stored site name is still {site_name!r}")
        if integrity != "ok":
            problems.append(f"SQLite integrity check: {integrity}")
    finally:
        fake.stop()
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(work / "previous")],
                       capture_output=True)
        if options.keep:
            print(f"Kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    if problems:
        print("Upgrade check failed:\n  " + "\n  ".join(problems))
        return 1
    print("Upgrade check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
