"""The removed Claude subscription connector leaves no way to serve its models.

Old installations may still set its variables and hold catalog rows it
created; they start normally, never offer those rows and keep chat history.
"""

import pytest

from bananachat import db
from bananachat.config import load_config
from bananachat.db import catalog, chats
from bananachat.services import model_lifecycle
from bananachat.services.access import AccessContext
from tests.app.test_api import assert_error, call, chat, roll_out, token_for

LEFTOVER = "claude-sonnet-leftover"


def test_old_connector_settings_start_normally_with_one_warning(tmp_path, make_app):
    environ = {"BC_INSTANCE_DIR": str(tmp_path), "BC_SECRET_KEY": "x" * 40,
               "BC_CLAUDE_EXTENSION": "bananachat.services.claude_code",
               "BC_CLAUDE_CODE_CONFIG": "/etc/bananachat/claude.json"}
    warnings = load_config(environ).warnings
    assert len(warnings) == 1 and "removed" in warnings[0] and "BC_CLAUDE_EXTENSION" in warnings[0]
    assert load_config({**environ, "BC_CLAUDE_EXTENSION": "", "BC_CLAUDE_CODE_CONFIG": ""}).warnings == ()
    app = make_app(CLAUDE_EXTENSION="bananachat.services.claude_code")
    assert app.test_client().get("/health").status_code == 200


@pytest.fixture
def leftover(app, make_user):
    """A connector model an administrator restored after the upgrade withdrew it."""
    roll_out(app)
    user = make_user("history-owner")
    with app.app_context():
        cursor = db.execute("INSERT INTO ai_models (ollama_name, backend, backend_model_name, display_name, "
                            "is_rolled_out, backend_available, provider) VALUES (?, 'claude', ?, 'Old Claude', "
                            "1, 1, 'claude')", (LEFTOVER, LEFTOVER))
        model = catalog.get(cursor.lastrowid)
        session_id = chats.create(user["id"])
        chats.add_message(session_id, "user", "Earlier question", user_id=user["id"], incognito=False)
        chats.add_message(session_id, "assistant", "Earlier answer from the connector", user_id=user["id"],
                          incognito=False, model_id=model["id"])
    return {"model": model, "user": user, "session_id": session_id}


def test_leftover_models_are_never_listed_or_served(app, leftover, fake_ollama):
    from bananachat.services import chat as chat_service

    token = token_for(app, leftover["user"])
    listed = [item["id"] for item in call(app, "GET", "/v1/models", token).json["data"]]
    assert LEFTOVER not in listed and listed
    with app.test_request_context():
        context = AccessContext.load(leftover["user"])
        assert LEFTOVER not in {item["name"] for item in chat_service.model_choices(context, "en")}
    assert_error(chat(app, token, model=LEFTOVER), 503, "server_error")
    assert all(body.get("model") != LEFTOVER for body in fake_ollama.chat_bodies())


def test_leftover_models_cannot_be_published_again(app, leftover):
    with app.app_context():
        catalog.set_rollout(leftover["model"]["id"], False)
        with pytest.raises(ValueError, match="removed"):
            model_lifecycle.enable(catalog.get(leftover["model"]["id"]))
        assert not catalog.get(leftover["model"]["id"])["is_rolled_out"]


def test_chat_history_with_a_connector_model_stays_readable(app, leftover, client):
    client.login("history-owner", "history-owner-password")
    page = client.get(f"/chat/{leftover['session_id']}")
    assert page.status_code == 200
    assert "Earlier answer from the connector" in page.get_data(as_text=True)


def test_connector_administration_is_gone(admin):
    assert admin.get("/admin/models/claude").status_code == 404
    page = admin.get("/admin/models").get_data(as_text=True)
    assert "Claude" not in page
