"""Long histories stay browsable, private and complete when downloaded."""

import re

from test_chat_runtime import runtime_app, _client  # noqa: F401
import db


def test_paged_history_has_no_gaps_and_download_remains_complete(runtime_app, monkeypatch):
    application, user, sid, _, _ = runtime_app
    ids = [db.add_message(sid, "assistant", f"history-{number:03d}") for number in range(63)]
    # Neither rendering nor downloading may fall back to the old full-table read.
    monkeypatch.setattr(db, "list_messages", lambda *_: (_ for _ in ()).throw(AssertionError("unbounded history read")))
    client = _client(application, user)
    first = client.get("/chat/" + sid)
    assert first.status_code == 200
    assert b"history-062" in first.data and b"history-000" not in first.data
    assert first.data.count(b'data-message-id="') == 20
    seen, before = [], None
    while True:
        rows, older, _ = db.list_message_page(sid, before=before)
        seen = [row["id"] for row in rows] + seen
        if not older:
            break
        before = rows[0]["id"]
    assert seen == ids
    older_page = client.get("/chat/" + sid + "?before=" + str(ids[-20]))
    assert older_page.status_code == 200 and b'id="chat-input"' not in older_page.data
    assert b"history-042" in older_page.data and b"history-062" not in older_page.data
    download = client.get("/chat/" + sid + "/download")
    assert download.status_code == 200
    assert re.findall(r"history-\d{3}", download.get_data(as_text=True)) == [f"history-{n:03d}" for n in range(63)]


def test_history_cursors_never_cross_session_or_share_permissions(runtime_app):
    application, user, sid, _, _ = runtime_app
    for number in range(25):
        db.add_message(sid, "user", "visible-" + str(number))
    other_user = db.create_user("other-history-user", "unused")
    other = db.create_session(other_user)
    other_id = db.add_message(other, "user", "PRIVATE_OTHER_SESSION")
    client = _client(application, user)
    assert client.get("/chat/" + other + "?before=" + str(other_id + 1)).status_code == 404
    assert b"PRIVATE_OTHER_SESSION" not in client.get("/chat/" + sid + "?before=" + str(other_id + 1)).data
    for query in ("before=-1", "before=nope", "before=99999999999999999999", "before=2&after=3"):
        assert client.get("/chat/" + sid + "?" + query).status_code == 400
    token = db.create_share_token(sid, user["id"])
    anonymous = application.test_client()
    shared = anonymous.get("/share/" + token)
    assert shared.status_code == 200 and shared.data.count(b'class="message user"') == 20
    assert b"PRIVATE_OTHER_SESSION" not in anonymous.get("/share/" + token + "/download").data
    db.revoke_share_token(sid, user["id"])
    assert anonymous.get("/share/" + token + "?before=2").status_code == 404


def test_download_fences_concurrent_appends_and_paging_uses_index(runtime_app):
    _, _, sid, _, _ = runtime_app
    for number in range(30):
        db.add_message(sid, "user", "original-" + str(number))
    iterator = db.iter_messages(sid)
    first = next(iterator)
    db.add_message(sid, "user", "appended later")
    result = [first, *iterator]
    assert len(result) == 30 and all(m["content"] != "appended later" for m in result)
    with db.get_db_context() as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM chat_messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 20", (sid, 1000)).fetchall()
    assert any("idx_chat_messages_session_id" in row["detail"] for row in plan)
    assert all("TEMP B-TREE" not in row["detail"] for row in plan)
