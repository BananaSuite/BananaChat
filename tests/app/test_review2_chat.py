"""Regression tests for defects found in the second review of chat and the inference core."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.test_chat import messages, new_chat, parse_sse, send, setup_models, user_browser

MARKDOWN = Path(__file__).resolve().parents[2] / "bananachat" / "static" / "js" / "markdown.js"
NODE = shutil.which("node")


@pytest.fixture
def alice(app, make_user):
    setup_models(app)
    return user_browser(app, make_user)


def render_markdown(*texts: str) -> list[str]:
    script = (f"import {{ renderMessage }} from {json.dumps(MARKDOWN.as_uri())};\n"
              f"const inputs = JSON.parse({json.dumps(json.dumps(list(texts)))});\n"
              "console.log(JSON.stringify(inputs.map((text) => renderMessage(text))));\n")
    result = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
                            check=True)
    return json.loads(result.stdout)


# ----- markdown -------------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_code_spans_inside_link_labels_are_rendered():
    (html,) = render_markdown("See [the `config` file](https://example.com/config) and `run`.")
    assert '<a href="https://example.com/config" target="_blank" rel="noopener noreferrer nofollow">' \
           "the <code>config</code> file</a>" in html
    assert "<code>run</code>" in html and "\ufffd" not in html


# ----- runs and the supervisor ------------------------------------------------------------------

def test_a_finished_run_does_not_unregister_the_next_run_of_the_same_chat(app):
    """The old run's thread releases after its answer is saved; a new run may already be registered."""
    from bananachat.db import runs
    from bananachat.services import chat as chat_service
    from bananachat.services import supervisor
    from bananachat.services.upstream import CancelToken

    prepared = SimpleNamespace(session={"id": "race-session"}, selection=SimpleNamespace(model={"id": 1}))
    old = chat_service.Run(app, prepared, runs.Begun("old-token", 1, None, ""))
    new = chat_service.Run(app, prepared, runs.Begun("new-token", 2, None, ""))
    old_cancel = CancelToken()
    supervisor.register_run("race-session", "old-token", old_cancel)
    with chat_service._local_lock:
        chat_service._local_runs["race-session"] = new
    supervisor.register_run("race-session", "new-token", new.cancel)
    try:
        old._release()
        with supervisor._lock:
            registered = supervisor._runs.get("race-session")
        assert registered is not None and registered.owner_token == "new-token"
        assert chat_service._local_runs.get("race-session") is new
    finally:
        new._release()
    with supervisor._lock:
        assert "race-session" not in supervisor._runs


# ----- chat management views --------------------------------------------------------------------

@pytest.mark.parametrize("action", ["title", "share", "personality"])
def test_json_bodies_that_are_not_objects_are_refused_not_crashing(app, alice, action):
    session_id = new_chat(alice)
    send(alice, session_id)
    response = alice.post_json(f"/chat/{session_id}/{action}", ["not", "an", "object"])
    assert response.status_code == 400, response.get_data(as_text=True)[:300]


def test_terminal_events_carry_the_saved_user_message(app, alice):
    session_id = new_chat(alice)
    done = parse_sse(send(alice, session_id, "Remember me").get_data(as_text=True))[-1]
    saved = messages(app, session_id)
    assert done["type"] == "done" and done["user_message_id"] == saved[0]["id"] and saved[0]["role"] == "user"


# ----- the chat page in a browser ---------------------------------------------------------------

def _chromium() -> str | None:
    if os.environ.get("BC_CHROMIUM"):
        return os.environ["BC_CHROMIUM"]
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(candidate)
    return None


@pytest.fixture
def chat_page(app, make_user, fake_ollama):
    """A signed-in Chromium page on a live server, or skip when Playwright/Chromium are missing."""
    sync_api = pytest.importorskip("playwright.sync_api")
    from werkzeug.serving import make_server

    setup_models(app)
    make_user("alice")
    fake_ollama.reply = "Answer text."
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=_chromium())
            except Exception as error:  # noqa: BLE001 - no browser installed here
                pytest.skip(f"Chromium is not available: {error}")
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/login")
            page.fill("#username", "alice")
            page.fill("#password", "alice-password")
            page.wait_for_timeout(600)  # the form's minimum fill time
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            page.goto(base + "/chat")
            page.wait_for_selector("#chat-input")
            yield page
            browser.close()
            assert errors == []
    finally:
        server.shutdown()


def _shown(page) -> list[str]:
    return page.evaluate("""[...document.querySelectorAll('#chat-messages .message')].map((node) =>
        `${node.dataset.id || '-'}:${node.classList.contains('message-user') ? 'user' : 'assistant'}`)""")


def _send(page, text, *, file=None):
    if file:
        page.set_input_files("#file-input", files=[{"name": file, "mimeType": "text/plain", "buffer": b"notes"}])
    before = page.locator("#chat-messages .message").count()
    page.fill("#chat-input", text)
    page.click("#send-button")
    page.wait_for_function("""(count) => document.querySelectorAll('#chat-messages .message').length >= count
        && !document.querySelector('.message.is-pending') && !document.getElementById('send-button').hidden""",
                           arg=before + 2, timeout=20000)
    try:  # the saved copies (ids, attachment links) arrive right after the answer
        page.wait_for_function("() => !document.querySelector('#chat-messages .message:not([data-id])')", timeout=3000)
    except Exception:  # noqa: BLE001 - the assertions below report what is shown
        pass


def test_sending_files_keeps_earlier_messages_and_their_order(chat_page):
    _send(chat_page, "First")
    _send(chat_page, "Second", file="notes.txt")
    _send(chat_page, "Third")
    _send(chat_page, "Fourth", file="more.txt")
    assert _shown(chat_page) == [f"{index}:{'user' if index % 2 else 'assistant'}" for index in range(1, 9)]
    assert chat_page.locator(".message-user .file-chip-link").count() == 2


def test_a_broken_stream_keeps_earlier_messages_and_their_order(chat_page):
    _send(chat_page, "First")

    def cut_stream(route):
        response = route.fetch()
        body = response.text()
        route.fulfill(response=response, body=body[: body.index('"type":"done"')].rsplit("\n\n", 1)[0] + "\n\n")

    chat_page.route("**/send", cut_stream)
    _send(chat_page, "Second")
    assert _shown(chat_page) == ["1:user", "2:assistant", "3:user", "4:assistant"]


# ----- model recovery -----------------------------------------------------------------------------

def test_model_recovery_completes_when_every_selected_model_is_already_installed(app):
    from bananachat.services import model_recovery

    with app.app_context():
        model_recovery.write({"schema": 1, "state": "pending",
                              "inventory": {"ollama": ["llama3.2:3b"], "huggingface": [], "manual": []}})
        result = model_recovery.download(["ollama:0"], None)
        assert result["queued"] == [] and result["skipped"] == ["llama3.2:3b"]
        assert model_recovery.status()["state"] == "complete"


def test_a_send_refused_for_maintenance_shows_the_translated_notice(app, chat_page):
    """The 503 message is English (API wording); the page says it in the reader's language."""
    from bananachat.db import settings, users
    from bananachat.i18n import translate

    with app.app_context():
        user = users.get_by_username("alice")
        users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": "it"})
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    with app.app_context():
        settings.update(maintenance_mode=1)
    chat_page.fill("#chat-input", "Ciao?")
    chat_page.click("#send-button")
    toast = chat_page.wait_for_selector(".toast", timeout=10000)
    assert toast.text_content().startswith(translate("it", "js.status_paused"))
    assert chat_page.input_value("#chat-input") == "Ciao?"


# ----- retention ------------------------------------------------------------------------------------

@pytest.mark.parametrize("incognito", [False, True])
def test_a_reused_empty_chat_is_not_erased_while_the_user_is_on_it(app, alice, incognito):
    """/chat and "New chat" reuse the newest empty chat; an old one must not be purged under the user."""
    from datetime import timedelta

    from bananachat import db
    from bananachat.db import chats, users
    from bananachat.services import chat as chat_service

    with app.app_context():
        old = chats.create(users.get_by_username("alice")["id"], incognito=incognito)
        db.execute("UPDATE chat_sessions SET created_at=?, updated_at=? WHERE id=?",
                   (db.now(-timedelta(days=8)), db.now(-timedelta(days=8)), old))
    assert new_chat(alice, incognito=incognito) == old
    with app.app_context():
        chat_service.purge_no_history(app)
        chat_service.purge_deleted(app)
    assert send(alice, old, "Still here?").status_code == 200


def test_a_no_history_message_refused_by_a_full_queue_leaves_no_audit_copy(make_app, make_user):
    """A refused send is undone completely: the page gives the text back and nothing of it is kept."""
    import time

    from bananachat import db

    app = make_app(MAX_CONCURRENT=1, MAX_QUEUE_DEPTH=1)
    setup_models(app)
    browser = user_browser(app, make_user)
    session_id = new_chat(browser, incognito=True)
    with app.app_context():
        db.execute("INSERT INTO inference_queue (req_id, priority, status, owner_pid, enqueued_at, heartbeat_at) "
                   "VALUES ('other', 0, 'running', 1, ?, ?)", (time.time(), time.time() + 3600))
    response = send(browser, session_id, "Private question")
    assert response.status_code == 429
    assert messages(app, session_id) == []
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM incognito_audit WHERE session_id=?", (session_id,)) == 0


def test_images_in_the_context_share_one_byte_budget_newest_first(app, make_user, fake_ollama, monkeypatch):
    """Model servers refuse bodies over 32 MB: older images are left out of the context, the send still works."""
    import secrets

    from bananachat import db
    from bananachat.db import chats
    from bananachat.services import chat as chat_service
    from bananachat.services import upstream

    assert chat_service.MAX_CONTEXT_IMAGE_BYTES == upstream.MAX_CONTEXT_IMAGE_BYTES
    monkeypatch.setattr(chat_service, "MAX_CONTEXT_IMAGE_BYTES", 250_000)
    setup_models(app, vision=True)
    browser = user_browser(app, make_user)
    session_id = new_chat(browser)
    with app.app_context():
        user_id = chats.get(session_id)["user_id"]
        for index in range(3):
            message_id = chats.add_message(session_id, "user", f"Picture {index}", user_id=user_id, incognito=False)
            chats.add_attachments(message_id, [{
                "id": secrets.token_urlsafe(18), "kind": "image", "filename": f"{index}.png", "media_type": "image/png",
                "size_bytes": 100_000, "sha256": "0" * 64, "image_data": bytes([index]) * 100_000,
                "extracted_text": None}])
            chats.add_message(session_id, "assistant", "Nice.", user_id=user_id, incognito=False)
        assert db.scalar("SELECT COUNT(*) FROM chat_attachments") == 3
    events = parse_sse(send(browser, session_id, "Compare them").get_data(as_text=True))
    assert events[-1]["state"] == "completed"
    sent = {message["content"]: len(message.get("images", [])) for message in fake_ollama.chat_bodies()[-1]["messages"]}
    assert sent["Picture 0"] == 0 and sent["Picture 1"] == 1 and sent["Picture 2"] == 1
