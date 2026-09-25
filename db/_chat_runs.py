"""Atomic chat admission, owned checkpoints, completion and crash recovery."""

from datetime import datetime, timezone
import time

import config
from ._chat import add_message
from ._connection import get_db_context
from ._credits import check_chat_credits_available, deduct_credits
from ._runtime import STREAM_LEASE_SECONDS, StreamBusyError, _claim_stream

_TERMINAL_STATES = {"completed", "stopped", "failed", "interrupted"}


class ChatLimitError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _finish(conn, row, content, state, error, model_id, tokens_in, tokens_out,
            duration_ms=0, queue_wait_ms=0, title=None):
    """Commit a reply, usage and outcome together before releasing ownership."""
    session_id = row["session_id"]
    usage_estimated = bool(content and state != "completed" and tokens_out == 0)
    if usage_estimated:
        tokens_out = max(1, (len(content) + 3) // 4)
    if model_id is not None and not conn.execute("SELECT 1 FROM ai_models WHERE id=?", (model_id,)).fetchone():
        model_id = None
    sess = conn.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
    message_id = None
    if sess and not sess["deleted_at"]:
        if content:
            message_id = add_message(
                session_id, "assistant", content, model_id, tokens_in, tokens_out,
                bool(sess["is_incognito"]), sess["user_id"], connection=conn,
                generation_state=state, usage_estimated=usage_estimated,
            )
            deduct_credits(
                sess["user_id"], tokens_in, tokens_out, model_id=model_id,
                request_type="chat_incognito" if sess["is_incognito"] else "chat", connection=conn, usage_estimated=usage_estimated,
            )
            if title and conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id=? AND role='assistant'", (session_id,),
            ).fetchone()[0] == 1:
                conn.execute("UPDATE chat_sessions SET title=? WHERE id=?", (title, session_id))
        conn.execute(
            "UPDATE chat_sessions SET generation_state=?, generation_error=?, generation_message_id=?, updated_at=? "
            "WHERE id=?", (state, error[:500], message_id, _now(), session_id),
        )
        conn.execute(
            "INSERT INTO request_metrics(request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, queue_wait_ms, status, usage_estimated) "
            "VALUES ('chat',?,?,?,?,?,?,?,?)",
            (model_id, sess["user_id"], tokens_in, tokens_out, duration_ms, queue_wait_ms,
             "ok" if state == "completed" else state, int(usage_estimated)),
        )
    conn.execute("DELETE FROM active_streams WHERE session_id=? AND owner_token=?", (session_id, row["owner_token"]))
    return message_id


def _recover_stale(conn, now, session_id=None):
    query = "SELECT * FROM active_streams WHERE heartbeat_at<?"
    parameters = [now - STREAM_LEASE_SECONDS]
    if session_id is not None:
        query += " AND session_id=?"
        parameters.append(session_id)
    identities = [row[0] for row in conn.execute(query.replace("SELECT *", "SELECT session_id") + " LIMIT 1000", parameters)]
    for identity in identities:
        row = conn.execute("SELECT * FROM active_streams WHERE session_id=?", (identity,)).fetchone()
        _finish(
            conn, row, row["partial_content"] or "", "interrupted",
            "The server stopped before this response finished. Its last checkpoint is preserved; send a message to retry.",
            row["model_id"], row["tokens_in"], row["tokens_out"],
        )


def recover_stale_chat_runs(session_id=None):
    # Most polls never take a write lock. The transaction repeats the stale test
    # so a heartbeat racing this read cannot lose a healthy task.
    now = time.time()
    with get_db_context() as conn:
        if not conn.execute("SELECT 1 FROM active_streams WHERE heartbeat_at<? LIMIT 1", (now - STREAM_LEASE_SECONDS,)).fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale(conn, time.time(), session_id)
        conn.commit()


def begin_chat_run(session_id, user, content, attachments=()):
    """Reserve ownership and store the input together; rejections store nothing."""
    now = time.time()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale(conn, now)
        sess = conn.execute(
            "SELECT * FROM chat_sessions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (session_id, user["id"]),
        ).fetchone()
        if sess is None:
            raise ValueError("Chat is unavailable.")
        if user["role"] != "admin" and conn.execute(
            "SELECT 1 FROM active_streams AS a JOIN chat_sessions AS s ON s.id=a.session_id "
            "WHERE s.user_id=? LIMIT 1", (user["id"],),
        ).fetchone():
            raise StreamBusyError("You already have a response in progress. Stop it or wait before sending another.")
        if not check_chat_credits_available(user["id"], role=user["role"], connection=conn)[0]:
            raise ChatLimitError("Daily chat limit reached. Try again tomorrow.")
        token = _claim_stream(conn, session_id, now)
        if attachments:
            if sess["is_incognito"]:
                raise ValueError("Attachments are unavailable in no-history chats.")
            used = conn.execute(
                "SELECT COALESCE(SUM(a.size_bytes),0) FROM chat_attachments AS a "
                "JOIN chat_messages AS m ON m.id=a.message_id WHERE m.session_id=?", (session_id,),
            ).fetchone()[0]
            if used + sum(item["size_bytes"] for item in attachments) > config.CHAT_MAX_SESSION_ATTACHMENT_BYTES:
                raise ValueError("This chat has reached its attachment storage limit.")
        message_id = add_message(session_id, "user", content, is_incognito=bool(sess["is_incognito"]),
                                 user_id=user["id"], connection=conn)
        for item in attachments:
            conn.execute(
                "INSERT INTO chat_attachments(id, message_id, kind, filename, media_type, size_bytes, sha256, extracted_text, image_data, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["id"], message_id, item["kind"], item["filename"], item["media_type"], item["size_bytes"],
                 item["sha256"], item.get("extracted_text"), item.get("image_data"), _now()),
            )
        conn.execute("UPDATE active_streams SET user_message_id=? WHERE session_id=?", (message_id, session_id))
        conn.execute(
            "UPDATE chat_sessions SET generation_state='queued', generation_error='', generation_message_id=NULL, updated_at=? WHERE id=?",
            (_now(), session_id),
        )
        conn.commit()
    return token


def checkpoint_chat_run(session_id, token, content, model_id, tokens_in=0, tokens_out=0):
    if len(content.encode("utf-8")) > config.CHAT_MAX_RESPONSE_BYTES:
        raise ValueError("Response checkpoint exceeds the configured size limit.")
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE active_streams SET partial_content=?, model_id=?, tokens_in=?, tokens_out=? "
            "WHERE session_id=? AND owner_token=? AND heartbeat_at>=?",
            (content, model_id, tokens_in, tokens_out, session_id, token, time.time() - STREAM_LEASE_SECONDS),
        )
        if cur.rowcount:
            conn.execute("UPDATE chat_sessions SET generation_state='running' WHERE id=?", (session_id,))
        conn.commit()
        return cur.rowcount > 0


def finish_chat_run(session_id, token, content, state, error="", model_id=None,
                    tokens_in=0, tokens_out=0, duration_ms=0, queue_wait_ms=0, title=None, *, include_title=False):
    if state not in _TERMINAL_STATES:
        raise ValueError("Unknown generation outcome.")
    if len(content.encode("utf-8")) > config.CHAT_MAX_RESPONSE_BYTES:
        raise ValueError("Response exceeds the configured size limit.")
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM active_streams WHERE session_id=? AND owner_token=?", (session_id, token)).fetchone()
        if row is None or row["heartbeat_at"] < time.time() - STREAM_LEASE_SECONDS:
            raise StreamBusyError("Generation ownership expired. The last checkpoint will be recovered.")
        message_id = _finish(conn, row, content, state, error, model_id, tokens_in, tokens_out,
                             duration_ms, queue_wait_ms, title)
        saved = conn.execute("SELECT title FROM chat_sessions WHERE id=?", (session_id,)).fetchone() if include_title else None
        conn.commit()
        return (message_id, saved["title"] if saved else None) if include_title else message_id


def get_chat_run_status(session_id):
    recover_stale_chat_runs(session_id)
    with get_db_context() as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT s.generation_state AS state, s.generation_error AS error, a.partial_content, "
            "a.stop_requested, a.owner_token FROM chat_sessions AS s "
            "LEFT JOIN active_streams AS a ON a.session_id=s.id WHERE s.id=?", (session_id,),
        ).fetchone()
        result = dict(row) if row else {}
        result["message_count"] = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id=?", (session_id,)).fetchone()[0]
        last = conn.execute(
            "SELECT m.*, model.display_name AS model_name FROM chat_messages AS m "
            "LEFT JOIN ai_models AS model ON model.id=m.model_id WHERE m.session_id=? ORDER BY m.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        result["last_message"] = dict(last) if last else None
        return result


def load_chat_context(session_id):
    """Read a bounded recent context without loading the entire conversation."""
    with get_db_context() as conn:
        messages = [dict(row) for row in conn.execute(
            "WITH recent AS (SELECT id, role, substr(content,1,?) AS content FROM chat_messages "
            "WHERE session_id=? ORDER BY id DESC LIMIT ?), "
            "sized AS (SELECT *, SUM(length(content)) OVER (ORDER BY id DESC) AS chars, "
            "ROW_NUMBER() OVER (ORDER BY id DESC) AS position FROM recent) "
            "SELECT id, role, content FROM sized WHERE chars<=? OR position=1 ORDER BY id",
            (config.CHAT_MAX_CONTEXT_CHARS, session_id, config.CHAT_MAX_HISTORY_MESSAGES, config.CHAT_MAX_CONTEXT_CHARS),
        )]
        if not messages:
            return [], []
        ids = [row["id"] for row in messages]
        placeholders = ",".join("?" for _ in ids)
        metadata = conn.execute(
            f"SELECT id, kind FROM chat_attachments WHERE message_id IN ({placeholders}) ORDER BY created_at DESC, id DESC", ids,
        ).fetchall()
        remaining_chars, remaining_images = config.CHAT_MAX_CONTEXT_CHARS, config.CHAT_MAX_CONTEXT_IMAGES
        attachments = []
        for meta in metadata:
            if meta["kind"] == "image":
                if remaining_images <= 0:
                    continue
                row = conn.execute("SELECT * FROM chat_attachments WHERE id=?", (meta["id"],)).fetchone()
                remaining_images -= 1
            else:
                if remaining_chars <= 0:
                    continue
                row = conn.execute(
                    "SELECT id, message_id, kind, filename, substr(extracted_text,1,?) AS extracted_text "
                    "FROM chat_attachments WHERE id=?", (remaining_chars, meta["id"]),
                ).fetchone()
                remaining_chars -= len(row["extracted_text"] or "")
            attachments.append(dict(row))
        return messages, list(reversed(attachments))
