"""Subscription models are offered only while their provider's adapter is connected.

Catalogue rows survive a disconnection, so chat history keeps each answer's
model, and an exhausted pool is reported as provider capacity rather than as
the account's own quota.
"""

import pytest

from bananachat import db
from bananachat.db import catalog, chats, subscription_pool as pool_db
from bananachat.services import model_lifecycle, subscription_pool
from tests.app.test_api import assert_error, call, chat, roll_out, token_for

NAME = "claude-sonnet-4-5"


@pytest.fixture
def published(app, make_user):
    """A published subscription model whose provider has no adapter configured."""
    roll_out(app)
    user = make_user("history-owner")
    with app.app_context():
        cursor = db.execute("INSERT INTO ai_models (ollama_name, backend, backend_model_name, display_name, "
                            "is_rolled_out, backend_available, provider, family) VALUES (?, 'claude', ?, "
                            "'Claude Sonnet', 1, 1, 'claude', 'sonnet')", (NAME, NAME))
        model = catalog.get(cursor.lastrowid)
        session_id = chats.create(user["id"])
        chats.add_message(session_id, "user", "Earlier question", user_id=user["id"], incognito=False)
        chats.add_message(session_id, "assistant", "Earlier answer from the pool", user_id=user["id"],
                          incognito=False, model_id=model["id"])
    yield {"model": model, "user": user, "session_id": session_id}
    subscription_pool.reset_transport()


def test_a_disconnected_provider_is_refused_as_provider_capacity(app, published, fake_ollama):
    token = token_for(app, published["user"])
    response = chat(app, token, model=NAME)
    # Not the account's own quota (429, retry in an hour): the shared pool has nothing left.
    assert_error(response, 503, "server_error", "provider_capacity_unavailable")
    assert response.headers.get("Retry-After") == "30"
    assert all(body.get("model") != NAME for body in fake_ollama.chat_bodies())


def test_the_model_sync_withdraws_a_disconnected_providers_models(app, published, fake_ollama):
    token = token_for(app, published["user"])
    with app.app_context():
        model_lifecycle.sync(source="test")
        row = catalog.get(published["model"]["id"])
        # Withdrawn, not marked missing: a temporary disconnection never retires the row.
        assert not row["backend_available"] and not row["missing_at"]
    listed = [item["id"] for item in call(app, "GET", "/v1/models", token).json["data"]]
    assert NAME not in listed and listed


def test_reconnecting_offers_the_discovered_models_again(app, published, fake_ollama):
    with app.app_context():
        pool_db.add_account("claude-1", provider="claude", window_limit=100_000)
        model_lifecycle.sync(source="test")
        subscription_pool.register_site_discovery("claude", lambda: [{"name": NAME, "reasoning": ["low", "medium"]}])
        subscription_pool.register_site_chat("claude", lambda account, model, messages, options: iter(()))
        model_lifecycle.sync(source="test")
        assert catalog.get(published["model"]["id"])["backend_available"]


def test_chat_history_with_a_subscription_model_stays_readable(app, published, client):
    client.login("history-owner", "history-owner-password")
    page = client.get(f"/chat/{published['session_id']}")
    assert page.status_code == 200
    assert "Earlier answer from the pool" in page.get_data(as_text=True)


def test_subscription_models_publish_but_are_never_deleted_from_a_model_server(app, published):
    with app.app_context():
        catalog.set_rollout(published["model"]["id"], False)
        model_lifecycle.enable(catalog.get(published["model"]["id"]))
        assert catalog.get(published["model"]["id"])["is_rolled_out"]
        with pytest.raises(ValueError, match="catalogue"):
            model_lifecycle.delete_from_server(catalog.get(published["model"]["id"]))
