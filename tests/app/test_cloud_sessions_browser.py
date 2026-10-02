"""Cloud-session audiences and owned workspaces in an actual browser, using fake services."""

from __future__ import annotations

import tarfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.fixtures import Browser
from tests.app.test_agents import (  # noqa: F401 - shared runner and cleanup fixtures
    add_user, agents_app, call, fast_loop, runner, set_settings, token_file, wait_status,
)
from tests.app.test_agents_git import DEMO, git_runner, net, tls_files  # noqa: F401 - TLS fixtures
from tests.app.test_review2_chat import _chromium


@pytest.fixture(scope="module")
def chromium():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=_chromium())
        except Exception as error:  # noqa: BLE001 - browser is an optional test dependency
            pytest.skip(f"Chromium is unavailable: {error}")
        yield browser
        browser.close()


@pytest.fixture
def cloud_browser(agents_app, chromium):
    from werkzeug.serving import make_server

    app = agents_app({"enabled": False, "access_mode": "admins"})
    alice = add_user(app, "alice")
    add_user(app, "bob")
    add_user(app, "allowed", allowed=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    contexts, errors = [], []

    def open_page(username="alice", *, language="en", width=1280, path="/agents"):
        from bananachat.db import users

        with app.app_context():
            user = users.get_by_username(username)
            users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": language})
        signed = Browser(app)
        signed.login(username)
        cookie = signed.client.get_cookie(app.config["SESSION_COOKIE_NAME"])
        context = chromium.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
        contexts.append(context)
        context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": base}])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + path, wait_until="networkidle")
        return page

    yield SimpleNamespace(app=app, alice=alice, base=base, open_page=open_page)
    for context in contexts:
        context.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert errors == []


def _layout(page):
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    duplicate_ids = page.evaluate("[...document.querySelectorAll('[id]')].map(e=>e.id).filter((id,i,ids)=>ids.indexOf(id)!==i)")
    assert duplicate_ids == []


@pytest.mark.parametrize("mode,username,allowed", [
    ("admins", "alice", False), ("admins", "admin", True),
    ("everyone", "alice", True), ("custom", "alice", False), ("custom", "allowed", True),
])
def test_cloud_audience_controls_navigation_and_start_form(cloud_browser, mode, username, allowed):
    set_settings(cloud_browser.app, enabled=True, access_mode=mode)
    page = cloud_browser.open_page(username)
    assert page.locator("#new-task-form").count() == int(allowed)
    assert page.locator("#main-nav a[href='/agents']").count() == int(allowed)
    if mode == "admins" and not allowed:
        assert "Administrators only" in page.inner_text("main")
        assert page.locator("main a[href='/account#access']").count() == 0
    if mode == "custom" and not allowed:
        assert page.locator("main a[href='/account#access']").count() == 1
    _layout(page)


@pytest.mark.parametrize("language,width", [("en", 1280), ("it", 1280), ("en", 390), ("it", 390)])
def test_cloud_start_upload_preview_download_follow_up_and_revocation(cloud_browser, fake_ollama, language, width):
    set_settings(cloud_browser.app, enabled=True, access_mode="everyone")
    fake_ollama.tool_script = [
        call("write_file", path="NOTES.md", content="# Release\n\n<script>window.unexpected=1</script>\n"),
        call("finish", summary="The release notes are ready."),
    ]
    page = cloud_browser.open_page(language=language, width=width)
    assert page.locator("h1").inner_text() == ("Cloud sessions" if language == "en" else "Sessioni cloud")
    page.locator("#task-files").set_input_files({"name": "outline.txt", "mimeType": "text/plain", "buffer": b"Release outline\n"})
    assert "1" in page.locator("#task-files-summary").inner_text()
    page.locator("#task-prompt").fill("Prepare clear release notes from the outline.")
    # The documented keyboard shortcut takes the same CSRF-protected start path as the button.
    page.locator("#task-prompt").press("Control+Enter")
    page.wait_for_url("**/agents/*")
    task_id = page.url.rsplit("/", 1)[-1]
    wait_status(cloud_browser.app, task_id, "finished")
    page.reload(wait_until="networkidle")
    assert page.locator("#task-status").get_attribute("data-status") == "finished"
    page.locator("#workspace-list button").filter(has_text="NOTES.md").click()
    page.wait_for_selector("#workspace-preview:not([hidden])")
    assert "<script>window.unexpected=1</script>" in page.locator("#workspace-file").inner_text()
    assert page.evaluate("window.unexpected") is None
    with page.expect_download() as download:
        page.locator("#workspace-file-download").click()
    assert download.value.failure() is None
    assert "Release" in Path(download.value.path()).read_text()
    with page.expect_download() as archive_download:
        page.locator("#archive-link").click()
    archive = archive_download.value
    assert archive.failure() is None
    with tarfile.open(archive.path(), "r:gz") as contents:
        names = {member.name.lstrip("./"): member for member in contents.getmembers()}
        assert contents.extractfile(names["outline.txt"]).read() == b"Release outline\n"
        assert b"<script>" in contents.extractfile(names["NOTES.md"]).read()
    fake_ollama.tool_script = [call("finish", summary="Added the requested check.")]
    page.locator("#follow-up-input").fill("Add a final review check.")
    with page.expect_response(lambda response: response.url.endswith(f"/agents/{task_id}/messages")) as resumed:
        page.locator("#follow-up-send").click()
    assert resumed.value.status == 202
    wait_status(cloud_browser.app, task_id, "finished")
    page.reload(wait_until="networkidle")
    assert "Added the requested check." in page.locator("#timeline").inner_text()
    set_settings(cloud_browser.app, access_mode="admins")
    page.reload(wait_until="networkidle")
    assert page.locator("#follow-up").is_hidden()
    assert page.locator("#archive-link").is_visible()  # Existing owned work remains readable.
    _layout(page)


def test_cloud_defaults_and_admin_audience_save_use_real_controls(cloud_browser):
    admin = cloud_browser.open_page("admin", path="/admin/agents")
    assert not admin.locator("input[name='enabled']").is_checked()
    assert admin.locator("#agents-access-mode").input_value() == "admins"
    assert cloud_browser.open_page().locator("#new-task-form").count() == 0
    admin.locator("input[name='enabled']").focus()
    admin.keyboard.press("Space")
    admin.locator("#agents-access-mode").select_option("everyone")
    form = admin.locator("#agents-access-mode").locator("xpath=ancestor::form")
    form.locator("button[type='submit']").click()
    admin.wait_for_load_state("networkidle")
    from bananachat.services.agents import settings
    with cloud_browser.app.app_context():
        saved = settings.current(fresh=True)
        assert saved.enabled and saved.access_mode == "everyone"
    assert cloud_browser.open_page().locator("#new-task-form").is_visible()
    _layout(admin)


def test_cloud_missing_runner_has_a_clear_non_actionable_form(cloud_browser):
    set_settings(cloud_browser.app, enabled=True, access_mode="everyone")
    cloud_browser.app.config["BC"] = replace(cloud_browser.app.config["BC"], agents_runner_url="")
    page = cloud_browser.open_page()
    assert page.locator("#new-task-form").count() == 0
    assert "administrator to finish the setup" in page.inner_text("main").lower()
    _layout(page)


def test_cloud_sessions_do_not_expose_another_users_workspace(cloud_browser, fake_ollama):
    set_settings(cloud_browser.app, enabled=True, access_mode="everyone")
    fake_ollama.tool_script = [call("finish", summary="Private session.")]
    alice = Browser(cloud_browser.app)
    alice.login("alice")
    response = alice.fetch("/agents", method="POST", data={"prompt": "Keep this session private."})
    assert response.status_code == 201
    task_id = response.get_json()["id"]
    wait_status(cloud_browser.app, task_id, "finished")
    bob = cloud_browser.open_page("bob")
    targets = [f"/agents/{task_id}" + suffix for suffix in ("", "/events", "/workspace", "/workspace/archive", "/workspace/patch")]
    results = bob.evaluate("async paths=>Promise.all(paths.map(async path=>(await fetch(path)).status))", targets)
    assert results == [404] * len(targets)
    assert bob.locator(f"a[href='/agents/{task_id}']").count() == 0
    _layout(bob)


def test_cloud_repository_patch_download_uses_the_owned_session(cloud_browser, fake_ollama, runner, net):
    set_settings(cloud_browser.app, enabled=True, access_mode="everyone", git_enabled=True)
    net.server.archive("/octo/demo/tar.gz/main", DEMO)
    patch = b"diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # Demo\n+Checked\n"
    git_runner(runner, patch=patch)
    fake_ollama.tool_script = [call("finish", summary="Reviewed the repository.")]
    page = cloud_browser.open_page()
    page.locator("#task-repo summary").click()
    page.locator("#task-repo-url").fill("https://github.com/octo/demo")
    page.locator("#task-repo-ref").fill("main")
    page.locator("#task-prompt").fill("Review the release notes.")
    page.locator("#new-task-submit").click()
    page.wait_for_url("**/agents/*")
    wait_status(cloud_browser.app, page.url.rsplit("/", 1)[-1], "finished")
    page.reload(wait_until="networkidle")
    assert page.locator("#patch-link").is_visible()
    with page.expect_download() as download:
        page.locator("#patch-link").click()
    assert download.value.suggested_filename == "demo-changes.patch"
    assert Path(download.value.path()).read_bytes() == patch
    _layout(page)
