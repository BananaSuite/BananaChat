"""The chat, playground and API key pages in Chromium, against a live server."""

from __future__ import annotations

import threading

import pytest

from tests.app.browser_helpers import chromium_executable
from tests.app.test_chat import setup_models


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
                browser = playwright.chromium.launch(executable_path=chromium_executable())
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


def shown_messages(page) -> list[str]:
    return page.evaluate("""[...document.querySelectorAll('#chat-messages .message')].map((node) =>
        `${node.dataset.id || '-'}:${node.classList.contains('message-user') ? 'user' : 'assistant'}`)""")


def send_in_page(page, text, *, file=None):
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


def _italian(app, username="alice"):
    from bananachat.db import users

    with app.app_context():
        user = users.get_by_username(username)
        users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": "it"})


# ----- sending ------------------------------------------------------------------------------------

def test_sending_files_keeps_earlier_messages_and_their_order(chat_page):
    send_in_page(chat_page, "First")
    send_in_page(chat_page, "Second", file="notes.txt")
    send_in_page(chat_page, "Third")
    send_in_page(chat_page, "Fourth", file="more.txt")
    assert shown_messages(chat_page) == [f"{index}:{'user' if index % 2 else 'assistant'}" for index in range(1, 9)]
    assert chat_page.locator(".message-user .file-chip-link").count() == 2


def test_a_broken_stream_keeps_earlier_messages_and_their_order(chat_page):
    send_in_page(chat_page, "First")

    def cut_stream(route):
        response = route.fetch()
        body = response.text()
        route.fulfill(response=response, body=body[: body.index('"type":"done"')].rsplit("\n\n", 1)[0] + "\n\n")

    chat_page.route("**/send", cut_stream)
    send_in_page(chat_page, "Second")
    assert shown_messages(chat_page) == ["1:user", "2:assistant", "3:user", "4:assistant"]


def test_a_send_refused_for_maintenance_shows_the_translated_notice(app, chat_page):
    """The 503 message is in English (API wording); the page shows it in the reader's language."""
    from bananachat.db import settings
    from bananachat.i18n import translate

    _italian(app)
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    with app.app_context():
        settings.update(maintenance_mode=1)
    chat_page.fill("#chat-input", "Ciao?")
    chat_page.click("#send-button")
    toast = chat_page.wait_for_selector(".toast", timeout=10000)
    assert toast.text_content().startswith(translate("it", "js.status_paused"))
    assert chat_page.input_value("#chat-input") == "Ciao?"


def test_an_interrupted_answer_is_tried_again_through_the_normal_send(app, chat_page, fake_ollama):
    """"Try again" sends the question like typing it would, with the same checks and charging; the draft stays."""
    from bananachat import db
    from bananachat.db import chats, settings, users
    from bananachat.i18n import translate
    from tests.app.test_chat import _stale_run

    with app.app_context():
        session_id = chats.create(users.get_by_username("alice")["id"], incognito=False)
    _stale_run(app, session_id, partial="Half an answer")
    chat_page.goto(chat_page.url.split("/chat")[0] + f"/chat/{session_id}")  # the reload saves the partial answer
    chat_page.wait_for_selector(".message-assistant .state-interrupted")
    retry = chat_page.locator(".message-retry button")
    assert retry.count() == 1 and retry.is_visible()
    assert retry.inner_text() == translate(chat_page.evaluate("document.documentElement.lang"), "js.chat_try_again")
    chat_page.fill("#chat-input", "A draft")
    chat_page.set_input_files("#file-input", files=[{"name": "staged.txt", "mimeType": "text/plain", "buffer": b"x"}])
    with app.app_context():
        charged = db.one("SELECT COUNT(*) AS n FROM credit_ledger")["n"]
        settings.update(maintenance_mode=1)

    retry.click()  # refused like any send: nothing is kept and the button comes back
    chat_page.wait_for_selector(".toast", timeout=10000)
    chat_page.wait_for_function("document.querySelectorAll('#chat-messages .message').length === 2")
    assert retry.is_visible()
    with app.app_context():
        settings.update(maintenance_mode=0)
    bodies = len(fake_ollama.chat_bodies())

    retry.click()
    chat_page.wait_for_function("""() => document.querySelectorAll('#chat-messages .message').length === 4
        && !document.querySelector('.message.is-pending') && !document.getElementById('send-button').hidden""",
                                timeout=20000)
    shown = chat_page.locator("#chat-messages .message")
    assert shown.nth(2).inner_text().startswith("Question")
    assert "Answer text." in shown.nth(3).inner_text()
    assert chat_page.locator(".message-retry button:visible").count() == 0  # only the newest answer offers it
    assert chat_page.input_value("#chat-input") == "A draft"
    assert chat_page.locator("#file-chips").inner_text().count("staged.txt") == 1
    sent = fake_ollama.chat_bodies()[bodies:]
    assert len(sent) == 1 and sent[0]["messages"][-1] == {"role": "user", "content": "Question"}
    with app.app_context():
        saved = chats.context_messages(session_id, max_messages=10, max_chars=10_000)
        assert [(row["role"], row["content"]) for row in saved] == [
            ("user", "Question"), ("assistant", "Half an answer"), ("user", "Question"), ("assistant", "Answer text.")]
        assert db.query("SELECT COUNT(*) AS n FROM chat_attachments")[0]["n"] == 0
        assert db.one("SELECT COUNT(*) AS n FROM credit_ledger")["n"] == charged + 1  # charged as a new message


# ----- files ----------------------------------------------------------------------------------------

def test_file_sizes_use_the_readers_decimal_separator(app, chat_page):
    _italian(app)
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    chat_page.set_input_files("#file-input", files=[{"name": "note.txt", "mimeType": "text/plain", "buffer": b"x" * 1536}])
    assert "1,5 KB" in chat_page.locator(".file-chip").first.inner_text()


def test_attachment_kinds_are_translated(app, chat_page):
    """The kind badge of a file ("TEXT", "IMAGE"...) is interface text: Italian readers see Italian."""
    from bananachat.i18n import translate

    _italian(app)
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    chat_page.set_input_files("#file-input", files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hi"}])
    expected = translate("it", "js.chat_kind_text").upper()
    assert chat_page.inner_text("#file-chips .file-chip-kind") == expected
    chat_page.click("#file-chips .icon-btn")
    send_in_page(chat_page, "Leggi", file="notes.txt")
    assert chat_page.inner_text(".message-user .file-chip-kind") == expected
    session_id = chat_page.url.rstrip("/").rsplit("/", 1)[1]
    with app.app_context():
        from bananachat.db import chats
        token = chats.share(session_id)
    html = app.test_client().get(f"/share/{token}", headers={"Accept-Language": "it"}).get_data(as_text=True)
    assert f'<span class="file-chip-kind">{expected}</span>' in html


# ----- history sidebar ------------------------------------------------------------------------------

@pytest.mark.parametrize("language", ["en", "it"])
def test_renaming_and_sharing_from_search_results_updates_the_result(app, chat_page, language):
    from bananachat.db import users
    from bananachat.i18n import translate

    with app.app_context():
        users.save_preferences(users.get_by_username("alice")["id"], {"interface_language": language})
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    assert chat_page.locator("html").get_attribute("lang") == language
    send_in_page(chat_page, "Bananas are berries")
    chat_page.click("form[action$='/chat/new'] button[type=submit]")
    chat_page.wait_for_selector("#chat-input")
    chat_page.fill("#chat-search", "Bananas")
    result = chat_page.wait_for_selector("#search-results .session-item")
    result.query_selector(".session-menu-button").click()
    chat_page.get_by_role("menuitem", name=translate(language, "js.chat_rename"), exact=True).click()
    chat_page.fill("dialog[open] input", "Fruit facts")
    chat_page.keyboard.press("Enter")
    chat_page.wait_for_function("() => document.querySelector('#search-results .session-item')?.dataset.title === 'Fruit facts'",
                                timeout=5000)
    assert "Fruit facts" in result.inner_text()
    result.query_selector(".session-menu-button").click()
    chat_page.get_by_role("menuitem", name=translate(language, "js.chat_share"), exact=True).click()
    chat_page.wait_for_selector("dialog[open]")
    chat_page.keyboard.press("Escape")
    assert result.get_attribute("data-shared") == "1"


# ----- phones ---------------------------------------------------------------------------------------

def _phone_page(chat_page):
    base = chat_page.url.split("/chat")[0]
    context = chat_page.context.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                                                    has_touch=True)
    page = context.new_page()
    page.goto(base + "/login")
    page.fill("#username", "alice")
    page.fill("#password", "alice-password")
    page.wait_for_timeout(600)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto(base + "/chat")
    page.wait_for_selector("#chat-input")
    return page


def test_on_phones_enter_starts_a_new_line_and_the_button_sends(chat_page):
    """Touch keyboards have no Shift+Enter, so Enter adds a line there and the send button sends."""
    phone = _phone_page(chat_page)
    assert phone.get_attribute("#chat-input", "enterkeyhint") == "enter"
    assert "Shift" not in phone.text_content("#composer-hint")
    phone.click("#chat-input")
    phone.keyboard.type("First line")
    phone.keyboard.press("Enter")
    phone.keyboard.type("Second line")
    assert phone.input_value("#chat-input") == "First line\nSecond line"
    assert phone.locator("#chat-messages .message").count() == 0
    phone.click("#send-button")
    phone.wait_for_selector(".message-user", timeout=10000)
    assert phone.inner_text(".message-user .message-text") == "First line\nSecond line"
    phone.context.close()
    # A desktop keeps Enter to send.
    assert chat_page.get_attribute("#chat-input", "enterkeyhint") == "send"
    chat_page.fill("#chat-input", "Hello")
    chat_page.press("#chat-input", "Enter")
    chat_page.wait_for_selector(".message-user", timeout=10000)


# ----- playground and API keys ----------------------------------------------------------------------

def test_a_playground_send_refused_for_maintenance_shows_the_translated_notice(app, chat_page):
    from bananachat.db import settings
    from bananachat.i18n import translate

    _italian(app)
    chat_page.goto(chat_page.url.split("/chat")[0] + "/developer/playground")
    chat_page.wait_for_selector("#pg-input")
    with app.app_context():
        settings.update(maintenance_mode=1)
    chat_page.fill("#pg-input", "Ciao?")
    chat_page.click("#pg-send")
    error = chat_page.wait_for_selector(".dev-msg-error", timeout=10000)
    assert error.text_content() == translate("it", "js.status_paused")


def test_renaming_an_api_token_updates_its_buttons_and_confirmations(app, chat_page):
    """After a rename, the Rotate and Revoke labels and confirmation questions use the new name."""
    from bananachat.db import tokens, users

    with app.app_context():
        tokens.create(users.get_by_username("alice")["id"], "Old laptop")
    chat_page.goto(chat_page.url.split("/chat")[0] + "/developer")
    chat_page.click("details.dev-rename summary")
    chat_page.fill("dialog[open] input", "New laptop")
    chat_page.keyboard.press("Enter")
    chat_page.wait_for_function("() => document.querySelector('.dev-token-name')?.textContent.trim() === 'New laptop'",
                                timeout=5000)
    chat_page.wait_for_function(
        "() => document.querySelector('form[action$=\"/revoke\"]')?.dataset.confirm.includes('New laptop')", timeout=5000)
    labels = chat_page.eval_on_selector_all(".dev-tokens td.actions .visually-hidden", "nodes => nodes.map(n => n.textContent.trim())")
    assert labels and all(label == "New laptop" for label in labels)
