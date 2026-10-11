"""Shared chat links go offline with their owner's account, and administrators can take one down."""

from __future__ import annotations

from tests.app.conftest import Browser
from tests.app.test_chat import new_chat, send, setup_models, user_browser


def _shared_chat(app, make_user, fake_ollama, username="alice"):
    setup_models(app)
    owner = user_browser(app, make_user, username)
    session_id = new_chat(owner)
    assert send(owner, session_id, "Something to share").status_code == 200
    url = owner.post_json(f"/chat/{session_id}/share", {"action": "create"}).json["url"]
    return owner, session_id, url.replace("http://localhost", "")


def test_a_suspended_owners_shared_chats_go_offline_until_the_suspension_ends(app, make_user, fake_ollama):
    from bananachat.db import users

    _, _, path = _shared_chat(app, make_user, fake_ollama)
    visitor = Browser(app)
    assert visitor.get(path).status_code == 200
    with app.app_context():
        owner = users.get_by_username("alice")
        users.suspend(owner["id"])
    assert visitor.get(path).status_code == 404
    assert visitor.get(path + "/download").status_code == 404
    with app.app_context():
        users.unsuspend(owner["id"])
    assert visitor.get(path).status_code == 200


def test_a_deleted_owners_shared_chats_are_gone(app, make_user, fake_ollama):
    from bananachat.db import users

    _, _, path = _shared_chat(app, make_user, fake_ollama)
    with app.app_context():
        users.delete(users.get_by_username("alice")["id"])
    assert Browser(app).get(path).status_code == 404


def test_administrators_can_stop_sharing_a_chat_without_deleting_it(app, admin, make_user, fake_ollama):
    from bananachat import db

    _, session_id, path = _shared_chat(app, make_user, fake_ollama)
    page = admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)
    assert f"/admin/chats/{session_id}/unshare" in page
    response = admin.post(f"/admin/chats/{session_id}/unshare", {})
    assert response.status_code == 302
    assert Browser(app).get(path).status_code == 404
    with app.app_context():
        session = db.one("SELECT shared_token, deleted_at FROM chat_sessions WHERE id=?", (session_id,))
        assert session["shared_token"] is None and session["deleted_at"] is None
        assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE session_id=?", (session_id,)) == 2
        audit = db.one("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1")
        assert (audit["action"], audit["target"]) == ("chat.admin_unshare", session_id)
    assert f"/admin/chats/{session_id}/unshare" not in admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)
    # The list offers the action for shared chats too.
    _, other, _ = _shared_chat(app, make_user, fake_ollama, "bob")
    assert f"/admin/chats/{other}/unshare" in admin.get("/admin/chats").get_data(as_text=True)


def test_only_administrators_can_unshare(app, make_user, fake_ollama):
    owner, session_id, path = _shared_chat(app, make_user, fake_ollama)
    stranger = user_browser(app, make_user, "mallory")
    assert stranger.post(f"/admin/chats/{session_id}/unshare", {}).status_code in (403, 404)
    assert Browser(app).get(path).status_code == 200


def test_an_owner_cannot_share_again_a_chat_an_administrator_stopped_sharing(app, admin, make_user, fake_ollama):
    from bananachat import db

    owner, session_id, path = _shared_chat(app, make_user, fake_ollama)
    assert admin.post(f"/admin/chats/{session_id}/unshare", {}).status_code == 302
    refused = owner.post_json(f"/chat/{session_id}/share", {"action": "create"}, headers={"Accept-Language": "en"})
    assert refused.status_code == 403 and "administrator" in refused.json["error"]["message"]
    italian = owner.post_json(f"/chat/{session_id}/share", {"action": "create"}, headers={"Accept-Language": "it"})
    assert italian.status_code == 403 and "amministratore" in italian.json["error"]["message"]
    with app.app_context():
        assert db.scalar("SELECT shared_token FROM chat_sessions WHERE id=?", (session_id,)) is None
    assert Browser(app).get(path).status_code == 404
    # The administrator sees why, and can allow sharing again.
    page = admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)
    assert "Sharing blocked" in page and f"/admin/chats/{session_id}/allow-share" in page
    assert admin.post(f"/admin/chats/{session_id}/allow-share", {}).status_code == 302
    with app.app_context():
        audit = db.one("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1")
        assert (audit["action"], audit["target"]) == ("chat.admin_allow_share", session_id)
    shared = owner.post_json(f"/chat/{session_id}/share", {"action": "create"})
    assert shared.status_code == 200 and shared.json["shared"]
    assert "Sharing blocked" not in admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)


def test_the_owner_turning_sharing_off_does_not_block_it(app, make_user, fake_ollama):
    owner, session_id, _ = _shared_chat(app, make_user, fake_ollama)
    assert owner.post_json(f"/chat/{session_id}/share", {"action": "revoke"}).status_code == 200
    assert owner.post_json(f"/chat/{session_id}/share", {"action": "create"}).status_code == 200


def test_only_administrators_can_allow_sharing_again(app, admin, make_user, fake_ollama):
    from bananachat import db

    owner, session_id, _ = _shared_chat(app, make_user, fake_ollama)
    admin.post(f"/admin/chats/{session_id}/unshare", {})
    assert owner.post(f"/admin/chats/{session_id}/allow-share", {}).status_code in (403, 404)
    with app.app_context():
        assert db.scalar("SELECT share_blocked_at FROM chat_sessions WHERE id=?", (session_id,)) is not None


def test_stopping_sharing_still_blocks_a_chat_its_owner_unshared_first(app, admin, make_user, fake_ollama):
    """Re-audit verification of CC-6: the owner turning sharing off just before the administrator's click must not
    make the moderation a no-op that lets them share again at once."""
    from bananachat import db

    owner, session_id, _ = _shared_chat(app, make_user, fake_ollama)
    assert owner.post_json(f"/chat/{session_id}/share", {"action": "revoke"}).status_code == 200
    assert admin.post(f"/admin/chats/{session_id}/unshare", {}).status_code == 302
    with app.app_context():
        assert db.scalar("SELECT share_blocked_at FROM chat_sessions WHERE id=?", (session_id,)) is not None
        audit = db.one("SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1")
        assert (audit["action"], audit["target"]) == ("chat.admin_unshare", session_id)
    refused = owner.post_json(f"/chat/{session_id}/share", {"action": "create"})
    assert refused.status_code == 403 and refused.json["error"]["code"] == "share_blocked"


def test_administrators_can_block_sharing_of_a_chat_that_is_not_shared(app, admin, make_user, fake_ollama):
    from bananachat import db

    owner, session_id, _ = _shared_chat(app, make_user, fake_ollama)
    assert owner.post_json(f"/chat/{session_id}/share", {"action": "revoke"}).status_code == 200
    page = admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)
    assert f"/admin/chats/{session_id}/unshare" in page and "Block sharing" in page
    assert admin.post(f"/admin/chats/{session_id}/unshare", {}).status_code == 302
    assert owner.post_json(f"/chat/{session_id}/share", {"action": "create"}).status_code == 403
    page = admin.get(f"/admin/chats/{session_id}").get_data(as_text=True)
    assert "Sharing blocked" in page and f"/admin/chats/{session_id}/allow-share" in page
    # A second click changes nothing and records nothing more.
    with app.app_context():
        before = db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='chat.admin_unshare'")
    admin.post(f"/admin/chats/{session_id}/unshare", {})
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='chat.admin_unshare'") == before
