"""Regression tests for defects found in the fourth review (chat, API, playground, exports, interface)."""

from __future__ import annotations

import pytest

from tests.app.test_chat import alice, new_chat, send, setup_models, user_browser, wait_idle  # noqa: F401
from tests.app.test_agents import agents_app, fast_loop, runner, token_file  # noqa: F401
from tests.app.test_images import comfy, image_app  # noqa: F401
from tests.app.test_review2_chat import NODE, chat_page, render_markdown  # noqa: F401


def _italian(app, username="alice"):
    from bananachat.db import users

    with app.app_context():
        user = users.get_by_username(username)
        users.save_preferences(user["id"], {**users.get_preferences(user["id"]), "interface_language": "it"})


# ----- chat exports and the shared view ----------------------------------------------------------

def test_the_markdown_export_keeps_a_user_message_that_starts_like_reasoning(app, alice):
    """Only answers carry a reasoning block: a user's "<think>..." text must be exported as typed."""
    session_id = new_chat(alice)
    send(alice, session_id, "<think>my own notes</think> and the question")
    wait_idle(app, session_id)
    text = alice.get(f"/chat/{session_id}/download").get_data(as_text=True)
    body = text.split("\n---\n", 1)[1]  # the title repeats the first message
    assert "<think>my own notes</think> and the question" in body


def test_the_shared_view_counts_tokens_in_the_readers_language(app, alice, fake_ollama):
    from bananachat.i18n import translate

    fake_ollama.reply = "one two three"
    session_id = new_chat(alice)
    send(alice, session_id, "Hello")
    wait_idle(app, session_id)
    url = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    page = app.test_client().get(url, headers={"Accept-Language": "it"}).get_data(as_text=True)
    assert translate("it", "js.chat_tokens", count=3) in page
    assert "3 tokens" not in page


# ----- playground ------------------------------------------------------------------------------------

def test_a_playground_send_refused_for_maintenance_shows_the_translated_notice(app, chat_page):
    """The 503 text is the API's English wording; the playground says it in the reader's language."""
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


def test_playground_limit_refusals_are_in_the_readers_language(app, alice):
    """The playground is part of the interface: a limit refusal is worded in the reader's language."""
    from bananachat.db import catalog, users
    from bananachat.db import limits as limits_db
    from bananachat.i18n import translate

    _italian(app)
    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        limits_db.set_model_override(users.get_by_username("alice")["id"], model["id"], None, locked=True)
        name = model["display_name"]
    response = alice.post_json("/developer/playground/send",
                               {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Ciao"}]})
    assert response.status_code == 403
    assert response.json["error"]["message"] == translate("it", "account.refusal_model_locked", model=name)


# ----- chat sidebar ------------------------------------------------------------------------------------

def test_renaming_and_sharing_from_search_results_updates_the_result(chat_page):
    """The search results are entries of their own: after a rename or a share they show the new state."""
    from tests.app.test_review2_chat import _send

    _send(chat_page, "Bananas are berries")
    chat_page.click("form[action$='/chat/new'] button[type=submit]")
    chat_page.wait_for_selector("#chat-input")
    chat_page.fill("#chat-search", "Bananas")
    result = chat_page.wait_for_selector("#search-results .session-item")
    result.query_selector(".session-menu-button").click()
    chat_page.click("#popup-menu [role=menuitem]:has-text('Rename')")
    chat_page.fill("dialog[open] input", "Fruit facts")
    chat_page.keyboard.press("Enter")
    chat_page.wait_for_function("() => document.querySelector('#search-results .session-item')?.dataset.title === 'Fruit facts'",
                                timeout=5000)
    assert "Fruit facts" in result.inner_text()
    result.query_selector(".session-menu-button").click()
    chat_page.click("#popup-menu [role=menuitem]:has-text('Share')")
    chat_page.wait_for_selector("dialog[open]")
    chat_page.keyboard.press("Escape")
    assert result.get_attribute("data-shared") == "1"


# ----- personal data export -------------------------------------------------------------------------

def test_the_data_export_contains_the_accounts_agent_tasks(app, make_user):
    """Agent tasks hold the user's prompts, follow-ups and results: personal data the export must include."""
    from bananachat.db import agents
    from bananachat.services import exports

    user = make_user("fay")
    other = make_user("gus")
    with app.app_context():
        for owner, task_id, prompt in ((user, "task-fay", "Write my CV"), (other, "task-gus", "Not fay's")):
            agents.create(task_id, user_id=owner["id"], title=prompt, prompt=prompt, model_id=None,
                          model_name="llama3.2:3b", swarm=False, owner_token="t", max_user=None, max_site=10)
        agents.add_step("task-fay", None, kind="assistant", content="Here is a draft")
        agents.add_message("task-fay", user["id"], "Make it shorter")
        data = exports.gdpr_export(user["id"])
    assert [task["prompt"] for task in data["agent_tasks"]] == ["Write my CV"]
    task = data["agent_tasks"][0]
    assert "owner_token" not in task and "sandbox_id" not in task
    assert [step["content"] for step in task["steps"]] == ["Write my CV", "Here is a draft"]
    assert [message["content"] for message in task["messages"]] == ["Make it shorter"]



# ----- music program ---------------------------------------------------------------------------------

@pytest.mark.parametrize("lang, words", [("en", ("daily",)), ("it", ("giornalier",))])
def test_the_music_page_does_not_promise_a_daily_quota(app, make_user, lang, words):
    """Limits are per 5-hour window (the page's own footnote says so): no "daily quota"/"daily allowance"."""
    from bananachat.db import settings
    from tests.app.test_music import _signed_in

    make_user("amy")
    with app.app_context():
        settings.update(music_enabled=1, music_visible=1, music_opt_in_allowed=1, music_opt_out_allowed=1)
    html = _signed_in(app, "amy").get("/free-quota", headers={"Accept-Language": lang}).get_data(as_text=True)
    assert f'lang="{lang}"' in html
    assert not any(word in html.lower() for word in words)


# ----- attachments -----------------------------------------------------------------------------------

def test_attachment_kinds_are_translated(app, chat_page):
    """The kind badge of a file ("TEXT", "IMAGE"...) is interface text: Italian readers see Italian."""
    from bananachat.i18n import translate
    from tests.app.test_review2_chat import _send

    _italian(app)
    chat_page.reload()
    chat_page.wait_for_selector("#chat-input")
    chat_page.set_input_files("#file-input", files=[{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hi"}])
    expected = translate("it", "js.chat_kind_text").upper()
    assert chat_page.inner_text("#file-chips .file-chip-kind") == expected
    chat_page.click("#file-chips .icon-btn")
    _send(chat_page, "Leggi", file="notes.txt")
    assert chat_page.inner_text(".message-user .file-chip-kind") == expected
    session_id = chat_page.url.rstrip("/").rsplit("/", 1)[1]
    with app.app_context():
        from bananachat.db import chats
        token = chats.share(session_id)
    html = app.test_client().get(f"/share/{token}", headers={"Accept-Language": "it"}).get_data(as_text=True)
    assert f'<span class="file-chip-kind">{expected}</span>' in html


# ----- markdown --------------------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_loose_lists_stay_one_list():
    """Models separate list items with blank lines: that is one (loose) list, not one list per item."""
    ordered, bullets, split = render_markdown("1. **First**\n\n   Details.\n\n2. Second\n\n3. Third",
                                              "- a\n\n- b", "1. a\n\nA paragraph.\n\n2. b")
    assert ordered.count("<ol") == 1 and ordered.count("<li>") == 3
    assert bullets == "<ul><li>a</li><li>b</li></ul>"
    assert split.count("<ol") == 2  # a paragraph in between still ends the list


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_link_destinations_may_contain_parentheses():
    (html,) = render_markdown("See [Python](https://en.wikipedia.org/wiki/Python_(programming_language)).")
    assert html == ('<p>See <a href="https://en.wikipedia.org/wiki/Python_(programming_language)" target="_blank" '
                    'rel="noopener noreferrer nofollow">Python</a>.</p>')


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_backslash_escapes_and_triple_emphasis():
    escaped, triple = render_markdown(r"5 \* 3 \* 2, snake\_case, \# not a heading and `a\*b`", "***both*** and *one*")
    assert escaped == r"<p>5 * 3 * 2, snake_case, # not a heading and <code>a\*b</code></p>"
    assert triple == "<p><strong><em>both</em></strong> and <em>one</em></p>"


# ----- Images page -------------------------------------------------------------------------------

def test_images_page_errors_are_in_the_readers_language(image_app, comfy):
    """The Images page is interface: its refusals are worded in the reader's language (the API keeps English)."""
    from bananachat.db import credits
    from bananachat.i18n import translate
    from tests.app.conftest import Browser
    from tests.app.test_images import create_user, generate, token_for

    artist = create_user(image_app, "artist")
    _italian(image_app, "artist")
    browser = Browser(image_app)
    browser.login("artist")
    browser._ensure_csrf()
    response = browser.post_json("/images/generate", {"prompt": "", "size": "512x512"})
    assert response.json["error"]["message"] == translate("it", "images.error_prompt_required")
    with image_app.app_context():
        credits.set_quota(artist["id"], 4000, 0, None)
    response = browser.post_json("/images/generate", {"prompt": "A cat", "size": "512x512"})
    assert response.status_code == 429
    message = response.json["error"]["message"]
    assert message.startswith("Un'immagine costa 5k token ") and "tokens" not in message, message
    # The API keeps its English wording.
    api = generate(image_app, token_for(image_app, artist))
    assert api.json["error"]["message"].startswith("An image costs")


# ----- developer page ----------------------------------------------------------------------------

def test_renaming_an_api_token_updates_its_buttons_and_confirmations(app, chat_page):
    """After a rename, Rotate/Revoke (their labels and confirmation questions) name the token by its new name."""
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


# ----- composer on phones ------------------------------------------------------------------------

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
    """Touch keyboards have no Shift+Enter: Enter adds a line there, the send button sends."""
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


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_setext_headings_and_images_as_links():
    """"Title" underlined with === or --- is a heading; an image is shown as a link (remote images are never loaded)."""
    headings, rule, image, bare = render_markdown("Big title\n=========\n\nSmall **title**\n---\ntext",
                                                  "Paragraph\n\n---\n\nNext",
                                                  "See ![a *cat*](https://example.com/cat.png) here",
                                                  "![](https://example.com/x.png)")
    assert headings == "<h1>Big title</h1><h2>Small <strong>title</strong></h2><p>text</p>"
    assert rule == "<p>Paragraph</p><hr><p>Next</p>"
    assert image == ('<p>See <a href="https://example.com/cat.png" target="_blank" rel="noopener noreferrer nofollow">'
                     'a <em>cat</em></a> here</p>')
    assert "<img" not in image and "!" not in image
    assert bare == ('<p><a href="https://example.com/x.png" target="_blank" rel="noopener noreferrer nofollow">'
                    'https://example.com/x.png</a></p>')


# ----- API errors ----------------------------------------------------------------------------------

def test_maintenance_refusals_use_the_full_openai_error_envelope(app, make_user):
    """Every /v1 error carries message, type, param and code - also the 503 of maintenance mode."""
    from bananachat.db import settings, tokens

    user = make_user("bob")
    with app.app_context():
        raw = tokens.create(user["id"], "cli")[1]
        settings.update(maintenance_mode=1)
    response = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {raw}"},
                                      json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 503
    assert response.json == {"error": {"message": response.json["error"]["message"], "type": "server_error",
                                       "param": None, "code": "maintenance"}}


# ----- agents ------------------------------------------------------------------------------------------

def test_the_agents_settings_speak_of_tokens_and_limits_not_credits(app, admin):
    """Credits and Quotas were renamed: the agents settings say tokens and point to Limits."""
    page = admin.get("/admin/agents").get_data(as_text=True)
    assert "credit" not in page.lower() and "(Quotas)" not in page
    assert "token limits" in page and "Limits" in page


def _agents_env(agents_app):
    import types

    from tests.app.conftest import Browser
    from tests.app.test_agents import add_user

    app = agents_app()
    alice = add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    return types.SimpleNamespace(app=app, user=alice, browser=browser)


def _model_rate(app, user, rule):
    from bananachat.db import catalog
    from bananachat.db import limits as limits_db

    with app.app_context():
        for model in catalog.list_models():
            limits_db.set_model_override(user["id"], model["id"], None, rate_rules=[rule])


def test_an_agent_start_refused_by_the_models_rate_is_in_the_readers_language(agents_app, fake_ollama):
    from bananachat.services import limits
    from tests.app.test_agents import start, start_ok

    env = _agents_env(agents_app)
    _italian(env.app)
    rule = {"requests": 1, "per": "minute", "burst": 1}
    _model_rate(env.app, env.user, rule)
    fake_ollama.tool_delay = 5
    start_ok(env.browser)
    response = start(env.browser)
    assert response.status_code == 429
    message = response.get_json()["error"]["message"]
    assert limits.rule_text("it", rule) in message and limits.rule_text("en", rule) not in message, message


def test_every_agent_step_takes_a_request_from_the_models_rate_and_waits_for_it(agents_app, fake_ollama):
    """The model's request rate holds for agents too: each step takes a request; when none is left the task waits
    (it does not fail) and goes on."""
    import itertools
    import time

    from tests.app.test_agents import call, start_ok, steps, wait_status

    env = _agents_env(agents_app)
    _model_rate(env.app, env.user, {"requests": 1, "per": "second", "burst": 1})
    moments = []
    script = [call("write_file", path="a.txt", content="a"), call("write_file", path="b.txt", content="b"),
              call("finish", summary="Done.")]

    def responder(body):
        moments.append(time.monotonic())
        return script[min(len(moments), len(script)) - 1]

    fake_ollama.tool_responder = responder
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "finished", "failed", "out_of_budget", timeout=20)
    assert row["status"] == "finished", row["error"]
    assert len(moments) == 3
    # The start took the first request; each later step waited for the next one (one per second).
    assert all(later - earlier >= 0.8 for earlier, later in itertools.pairwise(moments)), moments
    assert any("notice_model_rate" in step["content"] for step in steps(env.app, task_id) if step["kind"] == "notice")


def test_stopping_an_agent_that_waits_for_the_models_rate_is_prompt(agents_app, fake_ollama):
    import time

    from tests.app.test_agents import call, start_ok, steps, wait_for, wait_status

    env = _agents_env(agents_app)
    _model_rate(env.app, env.user, {"requests": 1, "per": "hour", "burst": 1})
    fake_ollama.tool_script = [call("write_file", path="a.txt", content="a"), call("finish", summary="Done.")]
    task_id = start_ok(env.browser)
    wait_for(lambda: any("notice_model_rate" in step["content"] for step in steps(env.app, task_id)), timeout=10,
             message="the rate notice")
    began = time.monotonic()
    assert env.browser.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    row = wait_status(env.app, task_id, "stopped", timeout=10)
    assert time.monotonic() - began < 5 and row["status"] == "stopped"


# ----- chat runs and models that disappear ----------------------------------------------------------------

def test_a_chat_answer_keeps_saving_after_its_model_row_is_removed(app, alice, fake_ollama):
    """Checkpoints store the model like the final save does: a model deleted meanwhile is recorded as NULL."""
    from bananachat import db
    from tests.app.test_chat import messages, read_events

    fake_ollama.reply = " ".join(["token"] * 150)
    fake_ollama.chunk_delay = 0.03
    session_id = new_chat(alice)
    response = send(alice, session_id, buffered=False, model="llama3.2:3b")
    read_events(response, until=lambda event: event["type"] == "delta")
    with app.app_context():
        with db.transaction():
            db.execute("DELETE FROM ai_models WHERE ollama_name='llama3.2:3b'")
    events = read_events(response)
    assert events[-1]["type"] == "done" and events[-1]["state"] == "completed", events[-1]
    saved = messages(app, session_id)[-1]
    assert saved["role"] == "assistant" and saved["content"] == fake_ollama.reply and saved["model_id"] is None


@pytest.mark.parametrize("change", [
    "UPDATE ai_models SET failing_at=datetime('now') WHERE ollama_name='qwen3:4b'",
    "UPDATE ai_models SET is_rolled_out=0 WHERE ollama_name='qwen3:4b'",
    "UPDATE ai_models SET backend_available=0, missing_at=datetime('now') WHERE ollama_name='qwen3:4b'",
    "DELETE FROM ai_models WHERE ollama_name='qwen3:4b'",
])
def test_a_fallback_that_became_unusable_while_queued_is_skipped(app, alice, fake_ollama, change):
    """Fallbacks are chosen when the request is made; one that is failing, disabled, missing or gone by the time
    the first model fails is not tried."""
    from bananachat import db
    from bananachat.db import catalog, users
    from bananachat.services import inference
    from bananachat.services.upstream import CancelToken

    fake_ollama.fail_models = {"llama3.2:3b"}
    with app.app_context():
        request = inference.TextRequest(user=users.get_by_username("alice"), model=catalog.get_by_name("llama3.2:3b"),
                                        fallbacks=[catalog.get_by_name("qwen3:4b")],
                                        messages=[{"role": "user", "content": "Hi"}])
        with db.transaction():
            db.execute(change)  # meanwhile, in the catalog
        events = list(inference.generate(request, CancelToken()))
    started = [event.model["ollama_name"] for event in events if isinstance(event, inference.Started)]
    assert started == ["llama3.2:3b"]
    assert isinstance(events[-1], inference.Finished) and events[-1].state == "failed"
    assert [body["model"] for body in fake_ollama.chat_bodies()] == ["llama3.2:3b"]


def test_a_step_that_waited_for_the_models_rate_checks_the_account_again(agents_app, fake_ollama):
    """While a step waits for the model's rate the account is suspended: the step ends without calling the model."""
    from bananachat.db import users
    from tests.app.test_agents import call, start_ok, steps, wait_for, wait_status

    env = _agents_env(agents_app)
    _model_rate(env.app, env.user, {"requests": 1, "per": "hour", "burst": 1})
    fake_ollama.tool_script = [call("write_file", path="a.txt", content="a"), call("finish", summary="Done.")]
    task_id = start_ok(env.browser)
    wait_for(lambda: any("notice_model_rate" in step["content"] for step in steps(env.app, task_id)), timeout=10,
             message="the rate notice")
    with env.app.app_context():
        users.suspend(env.user["id"], None)
    row = wait_status(env.app, task_id, "stopped", "failed", "out_of_budget", timeout=10)
    assert row["status"] == "stopped" and "agents.stop_account" in row["error"], row["error"]
    assert len([body for body in fake_ollama.chat_bodies() if body.get("tools")]) == 1
