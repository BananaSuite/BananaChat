"""Chat pages, history, no-history chats, deletion, sharing, exports, search and the admin audit."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import unquote

import pytest

from tests.app.conftest import TEST_CSRF, Browser
from tests.app.test_chat import messages, new_chat, parse_sse, send, setup_models, user_browser, wait_idle


@pytest.fixture
def alice(app, make_user):
    setup_models(app)
    return user_browser(app, make_user)


def chat_with_answer(browser, text="Hello there", *, incognito=False):
    session_id = new_chat(browser, incognito=incognito)
    events = parse_sse(send(browser, session_id, text).get_data(as_text=True))
    assert events[-1]["state"] == "completed", events
    return session_id


def form_post(browser, url, data=None):
    """Post a form after pages were loaded (a page load after sign-in issues a fresh CSRF token)."""
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser.post(url, data)


def page_data(html: str) -> dict:
    return json.loads(html.split('id="page-data">', 1)[1].split("</script>", 1)[0])


# ----- pages ----------------------------------------------------------------------------

def test_chat_index_reuses_the_empty_chat_and_the_page_is_csp_clean(app, alice):
    first = alice.get("/chat")
    assert first.status_code == 302
    second = alice.get("/chat")
    assert first.headers["Location"] == second.headers["Location"]
    page = alice.get(first.headers["Location"])
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'style="' not in html and "<script>" not in html
    assert "microphone=(self)" in page.headers["Permissions-Policy"]
    data = page_data(html)
    assert data["session"]["incognito"] is False and [model["name"] for model in data["models"]]


def test_renaming_sets_the_title(app, alice):
    session_id = chat_with_answer(alice)
    response = alice.post_json(f"/chat/{session_id}/title", {"title": "  Trip   plans "})
    assert response.status_code == 200 and response.json["title"] == "Trip plans"
    assert "<title>Trip plans · Test Chat</title>" in alice.get(f"/chat/{session_id}").get_data(as_text=True)
    assert alice.post_json(f"/chat/{session_id}/title", {"title": "   "}).status_code == 400


def test_other_users_chats_are_not_found(app, alice, make_user):
    session_id = chat_with_answer(alice)
    mallory = user_browser(app, make_user, "mallory")
    with app.app_context():
        from bananachat import db
        message_id = db.scalar("SELECT MAX(id) FROM chat_messages WHERE session_id=?", (session_id,))
    for method, url in [("GET", f"/chat/{session_id}"), ("GET", f"/chat/{session_id}/status"),
                        ("GET", f"/chat/{session_id}/download"), ("GET", f"/chat/{session_id}/messages"),
                        ("GET", f"/chat/{session_id}/messages/{message_id}/pdf"),
                        ("POST", f"/chat/{session_id}/send"), ("POST", f"/chat/{session_id}/stop"),
                        ("POST", f"/chat/{session_id}/share"), ("POST", f"/chat/{session_id}/delete"),
                        ("POST", f"/chat/{session_id}/title")]:
        assert mallory.fetch(url, method=method).status_code == 404, url
    assert mallory.fetch("/chat/search?q=Hello").json["results"] == []
    assert len(messages(app, session_id)) == 2


def test_signed_out_requests_get_a_json_401(app):
    response = Browser(app).fetch("/chat/abc/status")
    assert response.status_code == 401 and response.json["error"]["code"] == "auth_required"
    italian = Browser(app).fetch("/chat/abc/status", headers={"Accept-Language": "it"})
    assert italian.status_code == 401
    assert italian.json["error"]["message"] == "La sessione è terminata. Accedi di nuovo."


@pytest.mark.parametrize("action", ["title", "share", "personality"])
def test_json_bodies_that_are_not_objects_are_refused(app, alice, action):
    session_id = new_chat(alice)
    send(alice, session_id)
    response = alice.post_json(f"/chat/{session_id}/{action}", ["not", "an", "object"])
    assert response.status_code == 400, response.get_data(as_text=True)[:300]


# ----- history, search and paging ---------------------------------------------------------

def _many_chats(app, username, count):
    from bananachat import db
    from bananachat.db import chats, users

    with app.app_context():
        user = users.get_by_username(username)
        ids = []
        for index in range(count):
            session_id = chats.create(user["id"])
            chats.add_message(session_id, "user", f"Question number {index} about topic{index}", user_id=user["id"],
                              incognito=False)
            db.execute("UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
                       (f"Chat {index}", db.now(timedelta(minutes=index - count)), session_id))
            ids.append(session_id)
        return ids


def test_conversation_container_is_a_labelled_region(alice):
    html = alice.get(f"/chat/{new_chat(alice)}").get_data(as_text=True)
    container = html.split('id="chat-messages"', 1)[1].split(">", 1)[0]
    assert 'role="region"' in container and "aria-label=" in container


def test_search_is_limited_per_account(app, alice):
    statuses = [alice.fetch("/chat/search?q=topic").status_code for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429


def test_search_covers_every_chat_not_just_the_loaded_ones(app, alice):
    ids = _many_chats(app, "alice", 70)
    page = page_data(alice.get(f"/chat/{ids[-1]}").get_data(as_text=True))
    assert page["sidebar_next"]
    results = alice.fetch("/chat/search?q=topic3").json["results"]  # the oldest chats match too
    assert {item["title"] for item in results} >= {"Chat 3", "Chat 30"}
    assert "topic3" in results[0]["snippet"]
    assert alice.fetch("/chat/search?q=%25").json["results"] == []  # LIKE wildcards are literal
    assert alice.fetch("/chat/search?q=x").json["results"] == []  # too short


def test_the_sidebar_pages_through_all_chats(app, alice):
    ids = _many_chats(app, "alice", 65)
    seen, cursor = [], page_data(alice.get(f"/chat/{ids[0]}").get_data(as_text=True))["sidebar_next"]
    assert cursor
    while cursor:
        result = alice.fetch(f"/chat/sessions?before={cursor}").json
        seen += [item["id"] for item in result["sessions"]]
        cursor = result["next"]
    assert len(seen) == 35 and len(set(seen)) == 35
    assert alice.fetch("/chat/sessions?before=garbage").status_code == 400


def test_messages_are_paged(app, alice):
    from bananachat.db import chats, users

    session_id = new_chat(alice)
    with app.app_context():
        user = users.get_by_username("alice")
        for index in range(120):
            chats.add_message(session_id, "user", f"m{index}", user_id=user["id"], incognito=False)
    data = page_data(alice.get(f"/chat/{session_id}").get_data(as_text=True))
    assert data["has_more"] and data["messages"][-1]["content"] == "m119" and len(data["messages"]) == 50
    older = alice.fetch(f"/chat/{session_id}/messages?before={data['messages'][0]['id']}").json
    assert older["messages"][-1]["content"] == "m69" and older["has_more"]


# ----- no-history chats -----------------------------------------------------------------

def test_no_history_chats_stay_out_of_history_and_are_audited(app, alice):
    from bananachat import db
    from bananachat.db import chats, users

    session_id = chat_with_answer(alice, "A private question", incognito=True)
    assert alice.get(f"/chat/{session_id}").status_code == 200  # reloading keeps working
    assert alice.get(f"/chat/{session_id}").status_code == 200
    normal = chat_with_answer(alice, "A normal question")
    page = alice.get(f"/chat/{normal}").get_data(as_text=True)
    assert f'data-session-id="{session_id}"' not in page
    assert alice.fetch("/chat/search?q=private").json["results"] == []
    with app.app_context():
        user = users.get_by_username("alice")
        assert [item["id"] for item in chats.export_for_user(user["id"])] == [normal]
        audit = db.query("SELECT role, content FROM incognito_audit WHERE session_id=? ORDER BY id", (session_id,))
        assert [(row["role"], row["content"]) for row in audit][0] == ("user", "A private question")
        assert audit[1]["role"] == "assistant"
        assert db.scalar("SELECT request_type FROM credit_ledger ORDER BY id LIMIT 1") == "chat_incognito"
    assert alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).status_code == 400
    refused = send(alice, session_id, "file", files=[("a.txt", b"hello")])
    assert refused.status_code == 400 and "files" in refused.json["error"]["message"]
    page = alice.get(f"/chat/{session_id}").get_data(as_text=True)
    assert "Administrators can review" in page or "amministratori" in page.lower()


def test_no_history_chats_are_erased_after_inactivity_but_the_audit_stays(app, alice):
    from bananachat import db
    from bananachat.services import chat as chat_service

    session_id = chat_with_answer(alice, incognito=True)
    with app.app_context():
        chat_service.purge_no_history(app)
        assert db.one("SELECT 1 FROM chat_sessions WHERE id=?", (session_id,))
        db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (db.now(-timedelta(hours=25)), session_id))
        chat_service.purge_no_history(app)
        assert db.one("SELECT 1 FROM chat_sessions WHERE id=?", (session_id,)) is None
        assert db.scalar("SELECT COUNT(*) FROM incognito_audit WHERE session_id=?", (session_id,)) == 2
    assert alice.get(f"/chat/{session_id}").status_code == 404


def test_ending_a_no_history_chat_erases_it(app, alice):
    session_id = chat_with_answer(alice, incognito=True)
    response = alice.post(f"/chat/{session_id}/delete")
    assert response.status_code == 302
    assert messages(app, session_id) == []
    assert alice.get(f"/chat/{session_id}").status_code == 404


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


@pytest.mark.parametrize("incognito", [False, True])
def test_a_reused_empty_chat_is_not_erased_while_the_user_is_on_it(app, alice, incognito):
    """/chat and "New chat" reuse the newest empty chat, so an old one must not be purged under the user."""
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


# ----- deletion -------------------------------------------------------------------------

def test_deleted_chats_disappear_and_are_erased_after_the_retention_period(app, alice):
    from bananachat import db
    from bananachat.services import chat as chat_service

    session_id = chat_with_answer(alice)
    share = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    response = alice.post_json(f"/chat/{session_id}/delete")
    assert response.status_code == 200 and response.json["redirect"] == "/chat"
    assert alice.get(f"/chat/{session_id}").status_code == 404
    assert Browser(app).get(share.replace("http://localhost", "")).status_code == 404
    with app.app_context():
        assert db.scalar("SELECT deleted_at FROM chat_sessions WHERE id=?", (session_id,))
        chat_service.purge_deleted(app)
        assert len(messages(app, session_id)) == 2  # still within the retention period
        db.execute("UPDATE chat_sessions SET deleted_at=? WHERE id=?", (db.now(-timedelta(days=31)), session_id))
        chat_service.purge_deleted(app)
    assert messages(app, session_id) == []


def test_without_retention_deletion_is_immediate(make_app, make_user):
    app = make_app(DELETED_CHAT_RETENTION_DAYS=0)
    setup_models(app)
    browser = user_browser(app, make_user)
    session_id = chat_with_answer(browser)
    browser.post(f"/chat/{session_id}/delete")
    assert messages(app, session_id) == []


def test_delete_all_count_and_export_for_the_account_area(app, alice):
    from bananachat.db import chats, users

    first = chat_with_answer(alice, "One")
    chat_with_answer(alice, "Two")
    chat_with_answer(alice, "Secret", incognito=True)
    with app.app_context():
        user = users.get_by_username("alice")
        assert chats.count_for_user(user["id"]) == 2
        export = chats.export_for_user(user["id"])
        assert [item["messages"][0]["content"] for item in export] == ["One", "Two"]
        assert export[0]["id"] == first and export[0]["messages"][1]["role"] == "assistant"
        from bananachat.services import chat as chat_service
        assert chat_service.delete_all_chats(user["id"]) == 3
        assert chats.count_for_user(user["id"]) == 0
        assert chats.export_for_user(user["id"]) == []
        assert len(chats.export_for_user(user["id"], include_deleted=True)) == 2


# ----- sharing and exports ------------------------------------------------------------------

def test_sharing_is_stable_public_escaped_and_revocable(app, alice):
    session_id = new_chat(alice)
    assert alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).status_code == 400  # empty chat
    send(alice, session_id, "<script>alert(1)</script> and <img src=x onerror=alert(2)>").get_data()
    wait_idle(app, session_id)
    first = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    second = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    assert first == second
    path = first.replace("http://localhost", "")

    visitor = Browser(app)
    page = visitor.get(path)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert page.headers["X-Robots-Tag"].startswith("noindex") and page.headers["Referrer-Policy"] == "no-referrer"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html
    assert "<img src=x" not in html
    assert f"/chat/{session_id}/attachments" not in html
    download = visitor.get(f"{path}/download")
    assert download.status_code == 200 and b"<script>alert(1)</script>" in download.get_data()

    assert alice.post_json(f"/chat/{session_id}/share", {"action": "revoke"}).status_code == 200
    assert visitor.get(path).status_code == 404
    assert visitor.get("/share/short").status_code == 404
    third = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    assert third != first


def test_downloads_handle_non_latin_titles(app, alice):
    session_id = chat_with_answer(alice)
    alice.post_json(f"/chat/{session_id}/title", {"title": "Привет 你好 / plans"})
    response = alice.get(f"/chat/{session_id}/download")
    assert response.status_code == 200 and response.mimetype == "text/markdown"
    disposition = response.headers["Content-Disposition"]
    disposition.encode("latin-1")  # header values must be Latin-1
    ascii_name = re.search(r'filename="([^"]+)"', disposition).group(1)
    assert ascii_name.endswith(".md") and ascii_name.isascii()
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "Привет 你好 plans.md"
    body = response.get_data(as_text=True)
    assert body.startswith("# Привет 你好 / plans") and "Hello there" in body


def test_download_reads_attachments_once_per_batch_of_messages(app, alice):
    from bananachat import db

    session_id = chat_with_answer(alice)
    with app.app_context(), db.transaction():
        for number in range(450):
            message = db.execute("INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                                 (session_id, "user", f"note {number}", db.now())).lastrowid
            if number % 100 == 0:
                db.execute("INSERT INTO chat_attachments (id, message_id, kind, filename, media_type, size_bytes, "
                           "sha256, extracted_text, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f"file{number:03d}", message, "text", f"notes-{number}.txt", "text/plain", 4,
                            "cd" * 32, "text", db.now()))
    statements = []
    db.conn().set_trace_callback(statements.append)
    try:
        body = alice.get(f"/chat/{session_id}/download").get_data(as_text=True)
    finally:
        db.conn().set_trace_callback(None)
    assert 1 <= sum("FROM chat_attachments" in statement for statement in statements) <= 3, len(statements)
    for number in range(0, 450, 100):
        assert f"note {number}\n\n_" in body and f"notes-{number}.txt" in body
    assert body.index("notes-100.txt") < body.index("note 101\n")


def test_answers_export_as_pdf(app, alice):
    session_id = chat_with_answer(alice)
    answer = [row for row in messages(app, session_id) if row["role"] == "assistant"][0]
    response = alice.get(f"/chat/{session_id}/messages/{answer['id']}/pdf")
    assert response.status_code == 200 and response.mimetype == "application/pdf"
    assert response.get_data().startswith(b"%PDF-1.4") and b"Hello from the fake model." in response.get_data()
    question = [row for row in messages(app, session_id) if row["role"] == "user"][0]
    assert alice.get(f"/chat/{session_id}/messages/{question['id']}/pdf").status_code == 404


def test_the_markdown_export_keeps_a_user_message_that_starts_like_reasoning(app, alice):
    """Only answers carry a reasoning block, so a user's "<think>..." text is exported as typed."""
    session_id = new_chat(alice)
    send(alice, session_id, "<think>my own notes</think> and the question")
    wait_idle(app, session_id)
    text = alice.get(f"/chat/{session_id}/download").get_data(as_text=True)
    body = text.split("\n---\n", 1)[1]  # the title repeats the first message
    assert "<think>my own notes</think> and the question" in body


def test_the_shared_view_calls_the_sharer_user(app, alice):
    session_id = chat_with_answer(alice)
    url = alice.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    html = Browser(app).get(url.replace("http://localhost", ""), headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert 'aria-label="User"' in html and 'aria-label="You"' not in html


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


# ----- service status ------------------------------------------------------------------

def test_maintenance_pauses_new_answers_but_not_the_site(app, alice, make_user):
    from bananachat.db import settings

    session_id = chat_with_answer(alice)
    with app.app_context():
        settings.update(maintenance_mode=1)
    page = alice.get(f"/chat/{session_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'id="send-button" disabled' in html and "status-banner" in html
    refused = send(alice, session_id, "Hello?")
    assert refused.status_code == 503 and refused.json["error"]["code"] == "maintenance"
    assert len(messages(app, session_id)) == 2

    admin = Browser(app)
    admin.login("admin", "admin-password")
    admin_chat = new_chat(admin)
    assert parse_sse(send(admin, admin_chat).get_data(as_text=True))[-1]["state"] == "completed"


# ----- administrator audit --------------------------------------------------------------------

def test_admin_chat_audit(app, alice, admin):
    from bananachat import db

    session_id = chat_with_answer(alice, "Audit **me**")
    listing = admin.get("/admin/chats?user=alice")
    assert listing.status_code == 200 and "Audit me" in listing.get_data(as_text=True)
    assert "No user is called" in admin.get("/admin/chats?user=nobody").get_data(as_text=True)
    view = admin.get(f"/admin/chats/{session_id}")
    assert view.status_code == 200 and "data-markdown" in view.get_data(as_text=True)

    alice.post(f"/chat/{session_id}/delete")
    assert session_id not in admin.get("/admin/chats").get_data(as_text=True)
    assert session_id in admin.get("/admin/chats?deleted=1").get_data(as_text=True)
    assert form_post(admin, f"/admin/chats/{session_id}/delete").status_code == 302
    assert messages(app, session_id) == []
    with app.app_context():
        assert db.scalar("SELECT action FROM audit_log WHERE target=?", (session_id,)) == "chat.admin_delete"


def test_admin_no_history_audit(app, alice, admin):
    from bananachat import db

    session_id = chat_with_answer(alice, "Hidden question", incognito=True)
    overview = admin.get("/admin/incognito").get_data(as_text=True)
    assert session_id[:10] in overview and "alice" in overview
    detail = admin.get(f"/admin/incognito/{session_id}")
    assert detail.status_code == 200 and "Hidden question" in detail.get_data(as_text=True)
    with app.app_context():
        entry = db.scalar("SELECT MIN(id) FROM incognito_audit WHERE session_id=?", (session_id,))
    assert form_post(admin, f"/admin/incognito/entries/{entry}/delete").status_code == 302
    assert form_post(admin, f"/admin/incognito/{session_id}/delete").status_code == 302
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM incognito_audit") == 0
        assert db.one("SELECT 1 FROM chat_sessions WHERE id=?", (session_id,)) is None
        actions = {row["action"] for row in db.query("SELECT action FROM audit_log")}
        assert {"chat.no_history_entry_delete", "chat.no_history_delete"} <= actions
    assert admin.get(f"/admin/incognito/{session_id}").status_code == 404


def test_admin_audit_requires_an_administrator(app, alice):
    session_id = chat_with_answer(alice)
    for url in ("/admin/chats", f"/admin/chats/{session_id}", "/admin/incognito"):
        assert alice.get(url).status_code == 403
    assert alice.post(f"/admin/chats/{session_id}/delete").status_code == 403
