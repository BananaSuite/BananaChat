"""Regression tests for OC2-1 and OC2-2 of the full re-audit pass 9 (queued images checked again, Markdown
downloads of chats limited per address and per account)."""

from __future__ import annotations

import threading
import time

import pytest

from tests.app.test_api import ledger, token_for
from tests.app.test_images import MODEL_ID, comfy, create_user, generate  # noqa: F401


def _image_app(make_app, comfy):
    from bananachat.db import catalog
    from bananachat.services import comfyui
    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05", MAX_CONCURRENT="1")
    with app.app_context():
        assert comfyui.sync_models() == 1
        model_id = catalog.get_by_name(MODEL_ID)["id"]
        catalog.set_rollout(model_id, True)
    return app, model_id


def _hold_the_only_place(app, release, holding):
    from bananachat.services import queue
    with app.app_context():
        with queue.Slot(0, owner_key="holder") as slot:
            for _ in slot.positions(timeout=30):
                pass
            holding.set()
            release.wait(30)


@pytest.mark.parametrize("change", ["lock", "unpublish", "no_access", "none"])
def test_oc2_1_queued_image_is_checked_again_when_it_leaves_the_queue(make_app, comfy, change):
    from bananachat import db
    from bananachat.db import access, catalog
    app, model_id = _image_app(make_app, comfy)
    artist = create_user(app, "artist")
    token = token_for(app, artist)
    release, holding = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_the_only_place, args=(app, release, holding))
    holder.start()
    try:
        assert holding.wait(10)
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("r", generate(app, token, model=MODEL_ID)))
        worker.start()
        for _ in range(200):
            with app.app_context():
                waiting = db.one("SELECT 1 FROM inference_queue WHERE owner_key=? AND status='waiting'",
                                 (f"user:{artist['id']}",))
            if waiting:
                break
            time.sleep(0.02)
        assert waiting
        with app.app_context():
            if change == "lock":
                db.execute("INSERT INTO user_model_limits (user_id, model_id, locked) VALUES (?,?,1)",
                           (artist["id"], model_id))
            elif change == "unpublish":
                catalog.set_rollout(model_id, False)
            elif change == "no_access":
                access.set_policy("image_generation", 0, "deny_except_allowlist", True, None)
    finally:
        release.set()
        holder.join(10)
    worker.join(30)
    response = result["r"]
    if change == "none":
        assert response.status_code == 200
        assert len(comfy.prompts) == 1 and ledger(app, artist)
        return
    assert response.status_code == 403
    assert response.json["error"]["code"] == ("model_locked" if change == "lock" else "model_not_allowed")
    assert comfy.prompts == []
    assert ledger(app, artist) == []
    with app.app_context():
        assert db.one("SELECT COUNT(*) AS n FROM image_credit_reservations")["n"] == 0


def test_oc2_2_markdown_downloads_are_limited_per_address_and_per_account(make_app, make_user, monkeypatch):
    from bananachat.db import chats, users
    from bananachat.web import chat as chat_views
    from tests.app.test_chat import new_chat, user_browser

    monkeypatch.setattr(chat_views, "SHARED_DOWNLOADS_PER_HOUR", 3)
    monkeypatch.setattr(chat_views, "CHAT_DOWNLOADS_PER_HOUR", 4)
    app = make_app()
    alice = user_browser(app, make_user)
    session_id = new_chat(alice)
    with app.app_context():
        chats.add_message(session_id, "user", "x" * 10_000, user_id=users.get_by_username("alice")["id"],
                          incognito=False)
        token = chats.share(session_id)
    visitor = app.test_client()
    for _ in range(3):
        response = visitor.get(f"/share/{token}/download")
        assert response.status_code == 200 and len(response.data) > 10_000
        response.close()
    limited = visitor.get(f"/share/{token}/download")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "600"
    assert limited.headers["X-Robots-Tag"] == "noindex, nofollow" and b"x" * 100 not in limited.data
    # The share page keeps its own limit.
    page = visitor.get(f"/share/{token}")
    assert page.status_code == 200
    page.close()

    for _ in range(4):
        response = alice.get(f"/chat/{session_id}/download")
        assert response.status_code == 200 and len(response.data) > 10_000
        response.close()
    limited = alice.get(f"/chat/{session_id}/download")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "600"
