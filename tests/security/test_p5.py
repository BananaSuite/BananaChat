"""Regression test for PC2-1 of the full re-audit pass 10 (one running inference place per account and lane,
whatever the account uses: chat, API, playground or images)."""

from __future__ import annotations

import threading
import time

from tests.app.fixtures import Browser
from tests.app.test_api import chat as api_chat, token_for
from tests.app.test_chat import new_chat, send, setup_models
from tests.app.test_images import MODEL_ID, comfy, create_user, generate  # noqa: F401


def _rows(app, user):
    from bananachat import db
    with app.app_context():
        return [dict(row) for row in db.query("SELECT owner_key, status, lane FROM inference_queue "
                                              "WHERE owner_key LIKE ? ORDER BY seq", (f"user:{user['id']}%",))]


def _hold(app, owner_key, release, holding):
    from bananachat.services import queue
    with app.app_context():
        with queue.Slot(queue.PRIORITY_ADMIN, owner_key=owner_key) as slot:
            for _ in slot.positions(timeout=30):
                pass
            holding.set()
            release.wait(30)


def test_pc2_1_chat_api_and_image_answers_share_the_accounts_place(make_app, comfy, fake_ollama):
    from bananachat.db import catalog
    from bananachat.services import comfyui, queue
    app = make_app(IMAGE_BACKEND="comfyui", COMFYUI_URL=comfy.url, COMFYUI_POLL_INTERVAL="0.05",
                   MAX_CONCURRENT="4", QUEUE_TIMEOUT="30", COMFYUI_QUEUE_TIMEOUT="30")
    with app.app_context():
        assert comfyui.sync_models() == 1
        catalog.set_rollout(catalog.get_by_name(MODEL_ID)["id"], True)
    setup_models(app)
    mallory, victim = create_user(app, "mallory"), create_user(app, "victim")
    token, victim_token = token_for(app, mallory), token_for(app, victim)
    browser = Browser(app)
    browser.login("mallory")
    session_id = new_chat(browser)

    # Mallory already has an answer running (as a chat answer would).
    release, holding = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold, args=(app, queue.account_key(mallory["id"]), release, holding))
    holder.start()
    results = {}
    workers = [threading.Thread(target=lambda: results.setdefault("api", api_chat(app, token))),
               threading.Thread(target=lambda: results.setdefault("image", generate(app, token, model=MODEL_ID)))]
    try:
        assert holding.wait(10)
        for worker in workers:
            worker.start()
        for _ in range(200):
            rows = _rows(app, mallory)
            if len(rows) == 3:
                break
            time.sleep(0.02)
        time.sleep(0.3)  # three local places are free: an API answer or an image would start by now
        rows = _rows(app, mallory)
        assert [row["status"] for row in rows] == ["running", "waiting", "waiting"], rows
        assert {row["owner_key"] for row in rows} == {queue.account_key(mallory["id"])}
        # Other accounts still get the free places.
        assert api_chat(app, victim_token).status_code == 200
        # Three admitted requests per account, not per surface: a chat message now is refused.
        assert send(browser, session_id, "hi").status_code == 429
    finally:
        release.set()
        holder.join(10)
        for worker in workers:
            if worker.ident:
                worker.join(30)
    assert results["api"].status_code == 200 and results["image"].status_code == 200
