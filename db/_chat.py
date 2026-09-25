"""Chat session and message CRUD."""

import secrets
from contextlib import nullcontext
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


def _gen_session_id():
    return secrets.token_urlsafe(16)


def create_session(user_id, title="New Chat", is_incognito=False):
    sid = _gen_session_id()
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, user_id, title, is_incognito, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, user_id, title, 1 if is_incognito else 0, now, now),
        )
        conn.commit()
    return sid


@retry_on_busy
def get_session(session_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()


@retry_on_busy
def get_session_by_share_token(token):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE shared_token=? AND is_incognito=0 AND deleted_at IS NULL",
            (token,),
        ).fetchone()


def update_session_title(session_id, title):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
            (title, now, session_id),
        )
        conn.commit()


def touch_session(session_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
        conn.commit()


def set_session_personality(session_id, user_id, personality_id):
    with get_db_context() as conn:
        if personality_id is not None:
            owned = conn.execute(
                "SELECT 1 FROM personalities WHERE id=? AND user_id=?",
                (personality_id, user_id),
            ).fetchone()
            if not owned:
                raise ValueError("Personality not found.")
        cur = conn.execute(
            "UPDATE chat_sessions SET personality_id=?, updated_at=? "
            "WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (personality_id, datetime.now(timezone.utc).isoformat(), session_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def create_share_token(session_id, user_id):
    """Create a public share token for a non-incognito session."""
    with get_db_context() as conn:
        sess = conn.execute(
            "SELECT is_incognito, user_id FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not sess or sess["user_id"] != user_id or sess["is_incognito"]:
            raise ValueError("Cannot share this session")
        token = secrets.token_urlsafe(24)
        conn.execute("UPDATE chat_sessions SET shared_token=? WHERE id=?", (token, session_id))
        conn.commit()
        return token


def revoke_share_token(session_id, user_id):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE chat_sessions SET shared_token=NULL WHERE id=? AND user_id=?",
            (session_id, user_id),
        )
        conn.commit()


def delete_session(session_id, user_id):
    """Delete no-history sessions immediately; soft-delete normal chats."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        sess = conn.execute(
            "SELECT is_incognito FROM chat_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
        if sess and sess["is_incognito"]:
            conn.execute(
                "DELETE FROM chat_sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE chat_sessions SET deleted_at=?, shared_token=NULL WHERE id=? AND user_id=?",
                (now, session_id, user_id),
            )
        conn.commit()


def close_incognito_session(session_id, user_id):
    """Soft-close an incognito session so the user can no longer re-access it."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE chat_sessions SET deleted_at=? "
            "WHERE id=? AND user_id=? AND is_incognito=1 AND deleted_at IS NULL",
            (now, session_id, user_id),
        )
        conn.commit()


def delete_all_user_sessions(user_id):
    """Soft-delete all non-incognito sessions for a user."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE chat_sessions SET deleted_at=?, shared_token=NULL "
            "WHERE user_id=? AND is_incognito=0 AND deleted_at IS NULL",
            (now, user_id),
        )
        conn.commit()


@retry_on_busy
def list_user_sessions_for_export(user_id, include_deleted=False):
    """Get all non-incognito sessions for a user for data export."""
    with get_db_context() as conn:
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id=? AND is_incognito=0" + deleted_filter +
            " ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()


@retry_on_busy
def list_sessions(user_id, limit=50):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id=? AND is_incognito=0 AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


@retry_on_busy
def get_latest_empty_session(user_id, is_incognito=False):
    """Return the most recent session of the given type that has no messages, or None."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT cs.* FROM chat_sessions cs "
            "WHERE cs.user_id=? AND cs.is_incognito=? AND cs.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM chat_messages cm WHERE cm.session_id=cs.id) "
            "ORDER BY cs.updated_at DESC LIMIT 1",
            (user_id, 1 if is_incognito else 0),
        ).fetchone()


@retry_on_busy
def list_messages(session_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT cm.*, am.display_name AS model_name, am.ollama_name "
            "FROM chat_messages cm "
            "LEFT JOIN ai_models am ON cm.model_id=am.id "
            "WHERE cm.session_id=? ORDER BY cm.id ASC",
            (session_id,),
        ).fetchall()


def list_message_page(session_id, *, before=None, after=None, limit=20):
    """Read one indexed history window without materializing the conversation."""
    limit = min(100, max(1, int(limit)))
    if before is not None and after is not None:
        raise ValueError("Choose one history cursor.")
    where, values = "cm.session_id=?", [session_id]
    if before is not None:
        where += " AND cm.id<?"
        values.append(before)
    if after is not None:
        where += " AND cm.id>?"
        values.append(after)
    direction = "ASC" if after is not None else "DESC"
    with get_db_context() as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT cm.*, am.display_name AS model_name, am.ollama_name FROM chat_messages cm "
            "LEFT JOIN ai_models am ON cm.model_id=am.id WHERE " + where +
            " ORDER BY cm.id " + direction + " LIMIT ?", (*values, limit),
        ).fetchall()
        if after is None:
            rows.reverse()
        older = bool(rows and conn.execute(
            "SELECT 1 FROM chat_messages WHERE session_id=? AND id<? LIMIT 1", (session_id, rows[0]["id"]),
        ).fetchone())
        newer = bool(rows and conn.execute(
            "SELECT 1 FROM chat_messages WHERE session_id=? AND id>? LIMIT 1", (session_id, rows[-1]["id"]),
        ).fetchone())
    return rows, older, newer


def iter_messages(session_id):
    """Stream a download in bounded batches, fencing messages added afterwards."""
    with get_db_context() as conn:
        last = conn.execute("SELECT MAX(id) FROM chat_messages WHERE session_id=?", (session_id,)).fetchone()[0] or 0
    cursor = 0
    while cursor < last:
        rows, _, _ = list_message_page(session_id, after=cursor)
        if not rows:
            return
        for row in rows:
            if row["id"] > last:
                return
            cursor = row["id"]
            yield dict(row)


def add_message(session_id, role, content, model_id=None, tokens_in=0, tokens_out=0,
                 is_incognito=False, user_id=None, *, connection=None, generation_state="completed", usage_estimated=False):
    now = datetime.now(timezone.utc).isoformat()
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, model_id, tokens_in, tokens_out, created_at, generation_state, usage_estimated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, role, content, model_id, tokens_in, tokens_out, now, generation_state, int(usage_estimated)),
        )
        if is_incognito and user_id:
            conn.execute(
                "INSERT INTO incognito_audit (session_id, user_id, role, content, model_id, tokens_in, tokens_out, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, user_id, role, content, model_id, tokens_in, tokens_out, now),
            )
        if connection is None:
            conn.commit()
        return cur.lastrowid


def add_message_with_attachments(session_id, role, content, attachments, model_id=None):
    """Atomically store one message and its already-normalized attachments."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO chat_messages "
                "(session_id, role, content, model_id, created_at) VALUES (?,?,?,?,?)",
                (session_id, role, content, model_id, now),
            )
            message_id = cur.lastrowid
            for item in attachments:
                conn.execute(
                    "INSERT INTO chat_attachments "
                    "(id, message_id, kind, filename, media_type, size_bytes, sha256, "
                    "extracted_text, image_data, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["id"], message_id, item["kind"], item["filename"],
                        item["media_type"], item["size_bytes"], item["sha256"],
                        item.get("extracted_text"), item.get("image_data"), now,
                    ),
                )
            conn.commit()
            return message_id
        except Exception:
            conn.rollback()
            raise


@retry_on_busy
def get_message(message_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT cm.*, cs.user_id FROM chat_messages cm "
            "JOIN chat_sessions cs ON cs.id=cm.session_id WHERE cm.id=?",
            (message_id,),
        ).fetchone()


@retry_on_busy
def list_session_attachments(session_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ca.* FROM chat_attachments ca "
            "JOIN chat_messages cm ON cm.id=ca.message_id "
            "WHERE cm.session_id=? ORDER BY ca.created_at ASC, ca.id ASC",
            (session_id,),
        ).fetchall()


@retry_on_busy
def list_session_attachment_metadata(session_id, message_ids=None):
    if message_ids is not None and not message_ids:
        return []
    condition = "" if message_ids is None else " AND cm.id IN (" + ",".join("?" for _ in message_ids) + ")"
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ca.id, ca.message_id, ca.kind, ca.filename, ca.media_type, "
            "ca.size_bytes, ca.sha256, ca.created_at FROM chat_attachments ca "
            "JOIN chat_messages cm ON cm.id=ca.message_id "
            "WHERE cm.session_id=?" + condition + " ORDER BY ca.created_at ASC, ca.id ASC",
            (session_id, *(message_ids or [])),
        ).fetchall()


@retry_on_busy
def get_session_attachment_bytes(session_id):
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(ca.size_bytes), 0) AS total FROM chat_attachments ca "
            "JOIN chat_messages cm ON cm.id=ca.message_id WHERE cm.session_id=?",
            (session_id,),
        ).fetchone()
        return int(row["total"] or 0)


def session_has_image_attachments(session_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT 1 FROM chat_messages cm JOIN chat_attachments ca ON ca.message_id=cm.id "
            "WHERE cm.session_id=? AND ca.kind='image' LIMIT 1", (session_id,),
        ).fetchone() is not None


@retry_on_busy
def get_attachment(attachment_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ca.*, cm.session_id, cs.user_id FROM chat_attachments ca "
            "JOIN chat_messages cm ON cm.id=ca.message_id "
            "JOIN chat_sessions cs ON cs.id=cm.session_id WHERE ca.id=?",
            (attachment_id,),
        ).fetchone()


# Everything below is reachable only from the admin screens.

@retry_on_busy
def list_incognito_audit(limit=200, offset=0):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT ia.*, u.username, am.display_name AS model_name "
            "FROM incognito_audit ia "
            "JOIN users u ON ia.user_id=u.id "
            "LEFT JOIN ai_models am ON ia.model_id=am.id "
            "ORDER BY ia.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def delete_incognito_entry(entry_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM incognito_audit WHERE id=?", (entry_id,))
        conn.commit()


def delete_incognito_session(session_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM incognito_audit WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=? AND is_incognito=1", (session_id,))
        conn.commit()


def purge_expired_incognito_sessions(hours=24):
    """Remove transient chat copies while retaining the disclosed audit log."""
    hours = max(1, int(hours))
    with get_db_context() as conn:
        cursor = conn.execute(
            "DELETE FROM chat_sessions WHERE is_incognito=1 "
            "AND datetime(updated_at) < datetime('now', ?)",
            (f"-{hours} hours",),
        )
        conn.commit()
    return cursor.rowcount


@retry_on_busy
def list_all_sessions_admin(limit=50, offset=0, user_id=None, include_deleted=True):
    """List all chat sessions for admin audit, optionally filtered by user."""
    with get_db_context() as conn:
        deleted_filter = "" if include_deleted else " AND cs.deleted_at IS NULL"
        if user_id:
            return conn.execute(
                "SELECT cs.*, u.username "
                "FROM chat_sessions cs JOIN users u ON cs.user_id = u.id "
                "WHERE cs.user_id = ?" + deleted_filter +
                " ORDER BY cs.updated_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
        return conn.execute(
            "SELECT cs.*, u.username "
            "FROM chat_sessions cs JOIN users u ON cs.user_id = u.id " +
            ("WHERE cs.deleted_at IS NULL " if not include_deleted else "") +
            "ORDER BY cs.updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()


def count_all_sessions_admin(user_id=None, include_deleted=True):
    """Count chat sessions matching the same filters as list_all_sessions_admin."""
    with get_db_context() as conn:
        if user_id:
            deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_sessions WHERE user_id=?" + deleted_filter,
                (user_id,),
            ).fetchone()
        else:
            where = "" if include_deleted else " WHERE deleted_at IS NULL"
            row = conn.execute(
                "SELECT COUNT(*) FROM chat_sessions" + where,
            ).fetchone()
        return row[0] if row else 0


def count_incognito_audit():
    """Return total number of incognito audit entries."""
    with get_db_context() as conn:
        row = conn.execute("SELECT COUNT(*) FROM incognito_audit").fetchone()
        return row[0] if row else 0


def admin_hard_delete_session(session_id):
    """Hard-delete a session and its messages (admin purge)."""
    with get_db_context() as conn:
        conn.execute("DELETE FROM incognito_audit WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        conn.commit()


@retry_on_busy
def search_sessions(user_id, query, limit=20):
    """Search non-incognito sessions by title and message content for a user."""
    with get_db_context() as conn:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            """
            SELECT DISTINCT cs.id, cs.title, cs.updated_at
            FROM chat_sessions cs
            LEFT JOIN chat_messages cm ON cm.session_id = cs.id
            WHERE cs.user_id = ?
              AND cs.deleted_at IS NULL
              AND cs.is_incognito = 0
              AND (cs.title LIKE ? ESCAPE '\\' OR cm.content LIKE ? ESCAPE '\\')
            ORDER BY cs.updated_at DESC
            LIMIT ?
            """,
            (user_id, pattern, pattern, limit),
        ).fetchall()
        return rows
