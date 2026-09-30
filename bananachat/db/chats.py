"""Chat conversations: sessions, messages, attachments, sharing and retention.

A session (``chat_sessions``) belongs to one user. Normal chats are listed in
the user's history; *no-history* chats (``is_incognito=1``) never are, cannot
be shared and are erased after a period of inactivity, while a copy of each
of their messages is kept in ``incognito_audit`` for administrators.

Deleting a chat sets ``deleted_at`` (it disappears from every user-facing
view); :func:`purge_deleted` erases it for good after the retention period.
Generation runs (``active_streams``) live in :mod:`bananachat.db.runs`.
"""

from __future__ import annotations

import secrets

from bananachat import db

DEFAULT_TITLE = "New Chat"  # stored by every release for untitled chats
MAX_TITLE = 100
SESSION_ID_MAX = 64


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def is_untitled(title) -> bool:
    return not (title or "").strip() or title == DEFAULT_TITLE


def valid_id(session_id) -> bool:
    return isinstance(session_id, str) and 0 < len(session_id) <= SESSION_ID_MAX and \
        all(char.isascii() and (char.isalnum() or char in "-_") for char in session_id)


# ----- sessions -----------------------------------------------------------

def create(user_id: str, *, incognito: bool = False) -> str:
    session_id = secrets.token_urlsafe(16)
    now = db.now()
    db.execute("INSERT INTO chat_sessions (id, user_id, title, is_incognito, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?)", (session_id, user_id, DEFAULT_TITLE, int(incognito), now, now))
    return session_id


def get(session_id):
    """Any session (deleted ones included) or None."""
    if not valid_id(session_id):
        return None
    return db.one("SELECT * FROM chat_sessions WHERE id=?", (session_id,))


def get_owned(session_id, user_id: str):
    """The user's session unless it is deleted, else None."""
    if not valid_id(session_id):
        return None
    return db.one("SELECT * FROM chat_sessions WHERE id=? AND user_id=? AND deleted_at IS NULL",
                  (session_id, user_id))


def latest_empty(user_id: str, *, incognito: bool = False):
    """The newest session of this kind without messages (reused instead of creating another)."""
    return db.one(
        "SELECT * FROM chat_sessions s WHERE s.user_id=? AND s.is_incognito=? AND s.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id=s.id) "
        "ORDER BY s.updated_at DESC, s.id DESC LIMIT 1", (user_id, int(incognito)))


def touch(session_id: str) -> None:
    """Mark a session as in use now (an empty chat that is reused must not be purged as abandoned)."""
    db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (db.now(), session_id))


_LISTED = ("s.user_id=? AND s.is_incognito=0 AND s.deleted_at IS NULL "
           "AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id=s.id)")


def list_for_user(user_id: str, *, limit: int = 30, before: tuple[str, str] | None = None):
    """History entries (normal chats with messages), newest first, keyset-paged by ``(updated_at, id)``."""
    params: list = [user_id]
    where = _LISTED
    if before is not None:
        where += " AND (s.updated_at<? OR (s.updated_at=? AND s.id<?))"
        params += [before[0], before[0], before[1]]
    return db.query(
        "SELECT s.id, s.title, s.updated_at, s.created_at, s.shared_token IS NOT NULL AS shared "
        f"FROM chat_sessions s WHERE {where} ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (*params, limit))


def search(user_id: str, text: str, *, limit: int = 30):
    """Search every normal chat of the user by title and message text."""
    pattern = _like(text)
    return db.query(
        "SELECT s.id, s.title, s.updated_at, s.shared_token IS NOT NULL AS shared, "
        "(SELECT substr(m.content, max(1, instr(lower(m.content), lower(?)) - 60), 180) FROM chat_messages m "
        " WHERE m.session_id=s.id AND m.content LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT 1) AS snippet "
        f"FROM chat_sessions s WHERE {_LISTED} AND (s.title LIKE ? ESCAPE '\\' OR EXISTS "
        "(SELECT 1 FROM chat_messages m WHERE m.session_id=s.id AND m.content LIKE ? ESCAPE '\\')) "
        "ORDER BY s.updated_at DESC, s.id DESC LIMIT ?",
        (text, pattern, user_id, pattern, pattern, limit))


def count_for_user(user_id: str) -> int:
    """Chats in the user's history (normal, not deleted, with at least one message)."""
    return db.scalar(f"SELECT COUNT(*) FROM chat_sessions s WHERE {_LISTED}", (user_id,), 0)


def rename(session_id: str, title: str) -> str:
    title = " ".join((title or "").split())[:MAX_TITLE]
    if not title:
        raise ValueError("A title is required.")
    db.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title, session_id))
    return title


def set_title_if_untitled(session_id: str, title: str) -> bool:
    return db.execute("UPDATE chat_sessions SET title=? WHERE id=? AND (title=? OR trim(title)='')",
                      (title[:MAX_TITLE], session_id, DEFAULT_TITLE)).rowcount == 1


def set_personality(session_id: str, personality_id) -> None:
    db.execute("UPDATE chat_sessions SET personality_id=? WHERE id=?", (personality_id, session_id))


# ----- sharing -------------------------------------------------------------

def share(session_id: str) -> str:
    """The session's share token, created on first use and stable until revoked."""
    with db.transaction():
        row = db.one("SELECT shared_token, is_incognito FROM chat_sessions WHERE id=? AND deleted_at IS NULL",
                     (session_id,))
        if row is None or row["is_incognito"]:
            raise ValueError("This chat cannot be shared.")
        if row["shared_token"]:
            return row["shared_token"]
        token = secrets.token_urlsafe(24)
        db.execute("UPDATE chat_sessions SET shared_token=? WHERE id=?", (token, session_id))
        return token


def revoke_share(session_id: str) -> None:
    db.execute("UPDATE chat_sessions SET shared_token=NULL WHERE id=?", (session_id,))


def by_share_token(token):
    if not isinstance(token, str) or not 16 <= len(token) <= 64 or not valid_id(token):
        return None
    return db.one("SELECT * FROM chat_sessions WHERE shared_token=? AND is_incognito=0 AND deleted_at IS NULL",
                  (token,))


# ----- deletion ------------------------------------------------------------

def soft_delete(session_id: str) -> None:
    """Remove from every user-facing view; :func:`purge_deleted` erases it later."""
    with db.transaction():
        db.execute("UPDATE active_streams SET stop_requested=1 WHERE session_id=?", (session_id,))
        db.execute("UPDATE chat_sessions SET deleted_at=?, shared_token=NULL WHERE id=? AND deleted_at IS NULL",
                   (db.now(), session_id))


def hard_delete(session_id: str) -> None:
    """Erase a session, its messages and attachments (no-history audit copies are kept)."""
    with db.transaction():
        db.execute("DELETE FROM active_streams WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


def delete_all_for_user(user_id: str, *, hard: bool = False) -> int:
    """Delete every chat of a user ("delete all my chats"). Returns how many.

    Normal chats are soft-deleted (erased after the retention period) unless
    *hard*; no-history chats are always erased at once. Running answers stop.
    """
    with db.transaction():
        db.execute("UPDATE active_streams SET stop_requested=1 WHERE session_id IN "
                   "(SELECT id FROM chat_sessions WHERE user_id=?)", (user_id,))
        count = db.execute("DELETE FROM chat_sessions WHERE user_id=? AND is_incognito=1", (user_id,)).rowcount
        if hard:
            db.execute("DELETE FROM active_streams WHERE session_id IN "
                       "(SELECT id FROM chat_sessions WHERE user_id=?)", (user_id,))
            count += db.execute("DELETE FROM chat_sessions WHERE user_id=?", (user_id,)).rowcount
        else:
            count += db.execute("UPDATE chat_sessions SET deleted_at=?, shared_token=NULL "
                                "WHERE user_id=? AND deleted_at IS NULL", (db.now(), user_id)).rowcount
        db.execute("DELETE FROM active_streams WHERE session_id NOT IN (SELECT id FROM chat_sessions)")
    return count


# ----- messages ------------------------------------------------------------

MESSAGE_COLUMNS = ("m.id, m.session_id, m.role, m.content, m.model_id, m.tokens_in, m.tokens_out, m.created_at, "
                   "m.generation_state, m.usage_estimated, COALESCE(a.display_name, m.model_label) AS model_name, "
                   "a.ollama_name AS model")


def add_message(session_id: str, role: str, content: str, *, user_id: str, incognito: bool, model_id=None,
                tokens_in: int = 0, tokens_out: int = 0, generation_state: str = "completed",
                usage_estimated: bool = False) -> int:
    """Store a message; no-history chats also get an audit copy. Joins the caller's transaction."""
    now = db.now()
    with db.transaction():
        cursor = db.execute(
            "INSERT INTO chat_messages (session_id, role, content, model_id, tokens_in, tokens_out, created_at, "
            "generation_state, usage_estimated) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, role, content, model_id, tokens_in, tokens_out, now, generation_state, int(usage_estimated)))
        if incognito:
            db.execute("INSERT INTO incognito_audit (session_id, user_id, role, content, model_id, tokens_in, "
                       "tokens_out, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (session_id, user_id, role, content, model_id, tokens_in, tokens_out, now))
        db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
    return cursor.lastrowid


def count_messages(session_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM chat_messages WHERE session_id=?", (session_id,), 0)


def delete_message(message_id: int) -> None:
    db.execute("DELETE FROM chat_messages WHERE id=?", (message_id,))


def get_message(session_id: str, message_id: int):
    return db.one(f"SELECT {MESSAGE_COLUMNS} FROM chat_messages m LEFT JOIN ai_models a ON a.id=m.model_id "
                  "WHERE m.id=? AND m.session_id=?", (message_id, session_id))


def message_page(session_id: str, *, before: int | None = None, limit: int = 50):
    """The newest *limit* messages (older than *before*), oldest first, and whether more exist."""
    params: list = [session_id]
    where = "m.session_id=?"
    if before is not None:
        where += " AND m.id<?"
        params.append(before)
    rows = db.query(f"SELECT {MESSAGE_COLUMNS} FROM chat_messages m LEFT JOIN ai_models a ON a.id=m.model_id "
                    f"WHERE {where} ORDER BY m.id DESC LIMIT ?", (*params, limit + 1))
    more = len(rows) > limit
    return list(reversed(rows[:limit])), more


def messages_after(session_id: str, after: int, *, limit: int = 50):
    return db.query(f"SELECT {MESSAGE_COLUMNS} FROM chat_messages m LEFT JOIN ai_models a ON a.id=m.model_id "
                    "WHERE m.session_id=? AND m.id>? ORDER BY m.id LIMIT ?", (session_id, after, limit))


def iter_messages(session_id: str, *, batch: int = 200):
    """All messages in order, read in batches; messages added meanwhile are not included."""
    last = db.scalar("SELECT MAX(id) FROM chat_messages WHERE session_id=?", (session_id,), 0)
    cursor = 0
    while cursor < last:
        rows = db.query(f"SELECT {MESSAGE_COLUMNS} FROM chat_messages m LEFT JOIN ai_models a ON a.id=m.model_id "
                        "WHERE m.session_id=? AND m.id>? AND m.id<=? ORDER BY m.id LIMIT ?",
                        (session_id, cursor, last, batch))
        if not rows:
            return
        yield from rows
        cursor = rows[-1]["id"]


def last_model_name(session_id: str) -> str | None:
    return db.scalar("SELECT a.ollama_name FROM chat_messages m JOIN ai_models a ON a.id=m.model_id "
                     "WHERE m.session_id=? AND m.role='assistant' ORDER BY m.id DESC LIMIT 1", (session_id,))


def context_messages(session_id: str, *, max_messages: int, max_chars: int):
    """The newest messages that fit *max_chars* (the last one always), oldest first."""
    return db.query(
        "WITH recent AS (SELECT id, role, substr(content, 1, ?) AS content FROM chat_messages "
        "WHERE session_id=? AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?), "
        "sized AS (SELECT *, SUM(length(content)) OVER (ORDER BY id DESC) AS chars, "
        "ROW_NUMBER() OVER (ORDER BY id DESC) AS position FROM recent) "
        "SELECT id, role, content FROM sized WHERE chars<=? OR position=1 ORDER BY id",
        (max_chars, session_id, max_messages, max_chars))


# ----- attachments ---------------------------------------------------------

ATTACHMENT_META = "t.id, t.message_id, t.kind, t.filename, t.media_type, t.size_bytes, t.created_at"


def add_attachments(message_id: int, items: list[dict]) -> None:
    now = db.now()
    for item in items:
        db.execute(
            "INSERT INTO chat_attachments (id, message_id, kind, filename, media_type, size_bytes, sha256, "
            "extracted_text, image_data, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item["id"], message_id, item["kind"], item["filename"], item["media_type"], item["size_bytes"],
             item["sha256"], item.get("extracted_text"), item.get("image_data"), now))


def attachments_for(message_ids) -> dict[int, list]:
    """Attachment metadata (no content) grouped by message id."""
    ids = list(message_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result: dict[int, list] = {}
    for row in db.query(f"SELECT {ATTACHMENT_META} FROM chat_attachments t WHERE t.message_id IN ({placeholders}) "
                        "ORDER BY t.created_at, t.id", ids):
        result.setdefault(row["message_id"], []).append(row)
    return result


def get_attachment(session_id: str, attachment_id: str):
    if not valid_id(attachment_id):
        return None
    return db.one("SELECT t.* FROM chat_attachments t JOIN chat_messages m ON m.id=t.message_id "
                  "WHERE t.id=? AND m.session_id=?", (attachment_id, session_id))


def attachment_bytes(session_id: str) -> int:
    return db.scalar("SELECT COALESCE(SUM(t.size_bytes), 0) FROM chat_attachments t "
                     "JOIN chat_messages m ON m.id=t.message_id WHERE m.session_id=?", (session_id,), 0)


def has_images(session_id: str) -> bool:
    return db.one("SELECT 1 FROM chat_attachments t JOIN chat_messages m ON m.id=t.message_id "
                  "WHERE m.session_id=? AND t.kind='image' LIMIT 1", (session_id,)) is not None


def context_attachments(message_ids, *, max_chars: int, max_images: int,
                        max_image_bytes: int | None = None) -> list[dict]:
    """Attachments of *message_ids* for the model: newest first within the budgets, returned oldest first.

    Images count against *max_images* and, when given, *max_image_bytes* (stored bytes); an image
    that does not fit is left out.
    """
    ids = list(message_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    chosen = []
    chars_left, images_left = max_chars, max_images
    image_bytes_left = max_image_bytes
    for meta in db.query("SELECT id, kind, CASE WHEN kind='image' THEN length(image_data) ELSE 0 END AS image_bytes "
                         f"FROM chat_attachments WHERE message_id IN ({placeholders}) "
                         "ORDER BY message_id DESC, created_at DESC, id DESC", ids):
        if meta["kind"] == "image":
            if images_left <= 0 or (image_bytes_left is not None and meta["image_bytes"] > image_bytes_left):
                continue
            row = db.one("SELECT id, message_id, kind, filename, image_data FROM chat_attachments WHERE id=?",
                         (meta["id"],))
            images_left -= 1
            if image_bytes_left is not None:
                image_bytes_left -= meta["image_bytes"]
        else:
            if chars_left <= 0:
                continue
            row = db.one("SELECT id, message_id, kind, filename, substr(extracted_text, 1, ?) AS extracted_text "
                         "FROM chat_attachments WHERE id=?", (chars_left, meta["id"]))
            chars_left -= len(row["extracted_text"] or "")
        chosen.append(row.to_dict())
    chosen.reverse()
    return chosen


# ----- export --------------------------------------------------------------

def export_for_user(user_id: str, include_deleted: bool = False) -> list[dict]:
    """Every normal chat of the user with its messages, as plain dicts (for data exports).

    No-history chats are not included. Attachments are described (name, kind,
    type, size) without their content.
    """
    clause = "" if include_deleted else " AND deleted_at IS NULL"
    result = []
    for session in db.query("SELECT id, title, created_at, updated_at, deleted_at, shared_token IS NOT NULL AS shared "
                            f"FROM chat_sessions WHERE user_id=? AND is_incognito=0{clause} "
                            "ORDER BY created_at, rowid", (user_id,)):
        messages = list(iter_messages(session["id"]))
        attachments = attachments_for(message["id"] for message in messages)
        result.append({
            "id": session["id"],
            "title": "" if is_untitled(session["title"]) else session["title"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "deleted_at": session["deleted_at"],
            "shared": bool(session["shared"]),
            "messages": [{
                "id": message["id"], "role": message["role"], "content": message["content"],
                "model": message["model"], "created_at": message["created_at"],
                "tokens_in": message["tokens_in"], "tokens_out": message["tokens_out"],
                "state": message["generation_state"],
                "attachments": [{"filename": item["filename"], "kind": item["kind"],
                                 "media_type": item["media_type"], "size_bytes": item["size_bytes"]}
                                for item in attachments.get(message["id"], [])],
            } for message in messages],
        })
    return result


# ----- retention -----------------------------------------------------------

def _purge(where: str, params) -> int:
    ids = [row["id"] for row in db.query(f"SELECT id FROM chat_sessions WHERE {where} LIMIT 500", params)]
    for session_id in ids:
        hard_delete(session_id)
    return len(ids)


def purge_no_history(inactive_since: str) -> int:
    """Erase no-history chats untouched since *inactive_since* (audit copies stay)."""
    return _purge("is_incognito=1 AND updated_at<? AND id NOT IN (SELECT session_id FROM active_streams)",
                  (inactive_since,))


def purge_deleted(deleted_before: str) -> int:
    """Erase chats deleted before *deleted_before*."""
    return _purge("deleted_at IS NOT NULL AND deleted_at<?", (deleted_before,))


def purge_empty(untouched_since: str) -> int:
    """Erase abandoned chats that never received a message."""
    return _purge("updated_at<? AND id NOT IN (SELECT session_id FROM active_streams) "
                  "AND NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.session_id=chat_sessions.id)",
                  (untouched_since,))


# ----- administration ------------------------------------------------------

def _admin_where(user_id, include_deleted, incognito):
    clauses, params = [], []
    if user_id:
        clauses.append("s.user_id=?")
        params.append(user_id)
    if not include_deleted:
        clauses.append("s.deleted_at IS NULL")
    if incognito is not None:
        clauses.append("s.is_incognito=?")
        params.append(int(incognito))
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def admin_list(*, user_id: str | None = None, include_deleted: bool = False, incognito: bool | None = None,
               limit: int = 50, offset: int = 0):
    where, params = _admin_where(user_id, include_deleted, incognito)
    return db.query(
        "SELECT s.id, s.user_id, s.title, s.is_incognito, s.shared_token IS NOT NULL AS shared, s.created_at, "
        "s.updated_at, s.deleted_at, s.generation_state, u.username, "
        "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS message_count "
        f"FROM chat_sessions s JOIN users u ON u.id=s.user_id {where} "
        "ORDER BY s.updated_at DESC, s.id DESC LIMIT ? OFFSET ?", (*params, limit, offset))


def admin_count(*, user_id: str | None = None, include_deleted: bool = False, incognito: bool | None = None) -> int:
    where, params = _admin_where(user_id, include_deleted, incognito)
    return db.scalar(f"SELECT COUNT(*) FROM chat_sessions s {where}", params, 0)


def admin_get(session_id: str):
    if not valid_id(session_id):
        return None
    return db.one("SELECT s.*, u.username FROM chat_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
                  (session_id,))


def audit_sessions(*, user_id: str | None = None, limit: int = 50, offset: int = 0):
    """No-history audit copies grouped by chat, newest activity first."""
    where, params = ("WHERE ia.user_id=?", [user_id]) if user_id else ("", [])
    return db.query(
        "SELECT ia.session_id, ia.user_id, u.username, COUNT(*) AS entries, MIN(ia.created_at) AS first_at, "
        "MAX(ia.created_at) AS last_at, "
        "EXISTS (SELECT 1 FROM chat_sessions s WHERE s.id=ia.session_id) AS live "
        f"FROM incognito_audit ia JOIN users u ON u.id=ia.user_id {where} "
        "GROUP BY ia.session_id, ia.user_id ORDER BY last_at DESC LIMIT ? OFFSET ?", (*params, limit, offset))


def audit_session_count(*, user_id: str | None = None) -> int:
    where, params = ("WHERE user_id=?", [user_id]) if user_id else ("", [])
    return db.scalar(f"SELECT COUNT(DISTINCT session_id) FROM incognito_audit {where}", params, 0)


def audit_entries(session_id: str):
    return db.query(
        "SELECT ia.*, u.username, a.display_name AS model_name FROM incognito_audit ia "
        "JOIN users u ON u.id=ia.user_id LEFT JOIN ai_models a ON a.id=ia.model_id "
        "WHERE ia.session_id=? ORDER BY ia.id", (session_id,))


def audit_entry(entry_id: int):
    return db.one("SELECT * FROM incognito_audit WHERE id=?", (entry_id,))


def delete_audit_entry(entry_id: int) -> None:
    db.execute("DELETE FROM incognito_audit WHERE id=?", (entry_id,))


def delete_audit_session(session_id: str) -> int:
    """Delete a no-history chat's audit copies and the chat itself if it still exists."""
    with db.transaction():
        count = db.execute("DELETE FROM incognito_audit WHERE session_id=?", (session_id,)).rowcount
        if db.one("SELECT 1 FROM chat_sessions WHERE id=? AND is_incognito=1", (session_id,)):
            hard_delete(session_id)
    return count
