"""Files the application serves (attachments, exports) are inert when opened on their own."""

from __future__ import annotations

import hashlib
import uuid


def test_c4_1_attachment_downloads_carry_a_sandbox_policy(app, client, make_user):
    from bananachat import db
    from bananachat.db import chats

    user = make_user("filer")
    with app.test_request_context(), db.transaction():
        session_id = chats.create(user["id"])
        message_id = chats.add_message(session_id, "user", "files", user_id=user["id"], incognito=False)
        body = b"<script>alert(1)</script>"
        attachment_id = uuid.uuid4().hex
        chats.add_attachments(message_id, [{
            "id": attachment_id, "kind": "text", "filename": "<svg onload=alert(1)>.html",
            "media_type": "text/plain", "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "extracted_text": body.decode()}])
    client.login("filer")

    response = client.get(f"/chat/{session_id}/attachments/{attachment_id}")

    assert response.status_code == 200 and response.mimetype == "text/plain"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "<" not in response.headers["Content-Disposition"]
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"


def test_c4_1_pages_keep_their_own_policy(client):
    assert "script-src 'self' 'nonce-" in client.get("/login").headers["Content-Security-Policy"]
