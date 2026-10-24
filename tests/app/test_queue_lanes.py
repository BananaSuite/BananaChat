"""Hosted models (external providers, Claude, Codex) never wait behind local requests stuck on a model server."""

from __future__ import annotations

import pytest

from bananachat.services import queue


def test_a_full_local_lane_does_not_hold_up_hosted_requests(make_app):
    app = make_app(MAX_CONCURRENT="1")
    with app.app_context():
        stuck = queue.Slot(queue.PRIORITY_CHAT, owner_key="user:a", model="qwen3:4b")
        waiting = queue.Slot(queue.PRIORITY_CHAT, owner_key="user:b", model="qwen3:4b")
        hosted = queue.Slot(queue.PRIORITY_CHAT, owner_key="user:a", model="external:1:gpt", lane="hosted")
        with stuck:
            stuck.wait(timeout=1)
            with waiting:
                with pytest.raises(queue.QueueTimeout):
                    waiting.wait(timeout=0.3)  # the local lane is full (an answer hanging on a server that went away)
                with hosted:
                    hosted.wait(timeout=1)  # the same account's hosted request starts at once
                    assert hosted.running and hosted.position() == 0


def test_hosted_requests_have_their_own_limit(make_app):
    app = make_app(MAX_CONCURRENT_HOSTED="1")
    with app.app_context():
        first = queue.Slot(queue.PRIORITY_API, owner_key="user:a", lane="hosted")
        second = queue.Slot(queue.PRIORITY_API, owner_key="user:b", lane="hosted")
        local = queue.Slot(queue.PRIORITY_API, owner_key="user:b")
        with first:
            first.wait(timeout=1)
            with second:
                with pytest.raises(queue.QueueTimeout):
                    second.wait(timeout=0.3)
                with local:
                    local.wait(timeout=1)


def test_the_lane_follows_the_model():
    assert queue.lane_for({"backend": "ollama"}) == "local"
    assert queue.lane_for({"backend": "external"}) == "hosted"
    assert queue.lane_for({"backend": "claude"}) == "hosted"
    assert queue.lane_for(None) == "local"
    with pytest.raises(ValueError):
        queue.Slot(queue.PRIORITY_CHAT, lane="elsewhere")


def test_an_unreachable_model_server_does_not_pause_hosted_models(make_app, fake_ollama):
    """With the compute server down, a published hosted model keeps chat and the API answering."""
    from bananachat import db
    from bananachat.services import health, status

    app = make_app(INFERENCE_LOCAL="0")
    with app.test_request_context():
        health.record_probe(False, 1, "unreachable")
        try:
            assert status.inference_block(None) is not None  # nothing else to answer with: paused
            with db.transaction():
                db.execute("INSERT INTO external_providers (name, base_url, protocol) VALUES ('p', "
                           "'https://api.example.org/v1', 'openai')")
                provider_id = db.scalar("SELECT id FROM external_providers WHERE name='p'")
                db.execute("INSERT INTO ai_models (ollama_name, backend, provider, external_provider_id, "
                           "backend_model_name, backend_available, display_name, is_rolled_out, enrollment) "
                           "VALUES ('external:1:gpt', 'external', ?, ?, 'gpt', 1, 'GPT', 1, 'enabled')",
                           (f"external:{provider_id}", provider_id))
            from flask import g
            g.pop("_status_notices", None)
            assert status.inference_block(None) is None
            assert any(notice.kind == "local_outage" for notice in status.notices(None))
        finally:
            health.reset()
