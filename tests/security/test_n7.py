"""Regression tests for the message PDF findings of full re-audit pass 8 (AUDIT.md, NC2-1)."""

from __future__ import annotations

from types import SimpleNamespace

from tests.app.test_chat import new_chat, user_browser


def _answer(app, browser, username, content="A short answer."):
    from bananachat.db import chats, users

    session_id = new_chat(browser)
    with app.app_context():
        user_id = users.get_by_username(username)["id"]
        message_id = chats.add_message(session_id, "assistant", content, user_id=user_id, incognito=False)
    return user_id, f"/chat/{session_id}/messages/{message_id}/pdf"


def _counting_renders(monkeypatch, seconds=0.0):
    """Count render_pdf calls; each one moves the view's clock on by *seconds*."""
    from bananachat.services import attachments
    from bananachat.web import chat as chat_view

    clock = [1000.0]
    calls = []
    real = attachments.render_pdf

    def render(*args, **kwargs):
        calls.append(1)
        clock[0] += seconds
        return real(*args, **kwargs)

    monkeypatch.setattr(attachments, "render_pdf", render)
    monkeypatch.setattr(chat_view, "time", SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    return calls


def test_nc2_1_pdf_renders_spend_a_time_budget_per_account(app, make_user, monkeypatch):
    alice = user_browser(app, make_user, "alice")
    _, url = _answer(app, alice, "alice")
    calls = _counting_renders(monkeypatch, seconds=3.0)  # an answer of many short lines near the limit

    statuses = []
    for _ in range(6):
        response = alice.get(url)
        statuses.append(response.status_code)
        response.close()
    # 10 seconds a minute: the fourth render starts with 9 seconds spent, the fifth is refused before rendering.
    assert statuses == [200, 200, 200, 200, 429, 429]
    assert len(calls) == 4
    refused = alice.get(url)
    assert refused.headers["Retry-After"] == "10"
    refused.close()

    # Another account keeps its own budget.
    bob = user_browser(app, make_user, "bob")
    _, bob_url = _answer(app, bob, "bob")
    response = bob.get(bob_url)
    assert response.status_code == 200 and response.data.startswith(b"%PDF-")
    response.close()


def test_nc2_1_ordinary_pdfs_stay_well_within_the_budget(app, make_user, monkeypatch):
    alice = user_browser(app, make_user, "alice")
    _, url = _answer(app, alice, "alice")
    _counting_renders(monkeypatch, seconds=0.01)  # a usual answer renders in milliseconds
    for _ in range(30):
        response = alice.get(url)
        assert response.status_code == 200
        response.close()


def test_nc2_1_pdf_takes_a_download_place_before_rendering(app, make_user, monkeypatch):
    from bananachat import security

    alice = user_browser(app, make_user, "alice")
    user_id, url = _answer(app, alice, "alice")
    calls = _counting_renders(monkeypatch)
    with app.app_context():
        places = security._downloads()
        held = [places.take("user:" + user_id)[0] for _ in range(places.per_client)]
    try:
        response = alice.get(url)
        assert response.status_code == 429
        assert calls == []  # refused without rendering
        response.close()
    finally:
        for release in held:
            release()

    response = alice.get(url)
    assert response.status_code == 200
    response.close()
    assert places.active == 0  # a small PDF gives its place back
