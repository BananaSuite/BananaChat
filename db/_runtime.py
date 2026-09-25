"""Ownership leases shared by every web process; reads never steal a stream."""

import time
import uuid

from ._connection import get_db_context

STREAM_LEASE_SECONDS = 90


class StreamBusyError(RuntimeError):
    pass


def _claim_stream(conn, session_id, now):
    if conn.execute("SELECT 1 FROM active_streams WHERE session_id=?", (session_id,)).fetchone():
        raise StreamBusyError("This chat is already generating. Stop it or wait for its response.")
    token = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO active_streams(session_id, owner_token, stop_requested, heartbeat_at, started_at) "
        "VALUES(?,?,0,?,?)", (session_id, token, now, now),
    )
    return token


def register_active_stream(session_id):
    from ._chat_runs import _recover_stale
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        _recover_stale(conn, now)
        token = _claim_stream(conn, session_id, now)
        conn.commit()
    return token


def touch_active_stream(session_id, owner_token):
    now = time.time()
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE active_streams SET heartbeat_at=? WHERE session_id=? AND owner_token=? AND heartbeat_at>=?",
            (now, session_id, owner_token, now - STREAM_LEASE_SECONDS),
        )
        conn.commit()
        return cur.rowcount > 0


def active_stream_should_stop(session_id, owner_token):
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT stop_requested, heartbeat_at FROM active_streams WHERE session_id=? AND owner_token=?",
            (session_id, owner_token),
        ).fetchone()
    return row is None or bool(row["stop_requested"]) or row["heartbeat_at"] < time.time() - STREAM_LEASE_SECONDS


def request_stop_stream(session_id):
    with get_db_context() as conn:
        cur = conn.execute("UPDATE active_streams SET stop_requested=1 WHERE session_id=?", (session_id,))
        conn.commit()
        return cur.rowcount > 0


def deregister_active_stream(session_id, owner_token):
    with get_db_context() as conn:
        conn.execute("DELETE FROM active_streams WHERE session_id=? AND owner_token=?", (session_id, owner_token))
        conn.commit()


def is_active_stream(session_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT 1 FROM active_streams WHERE session_id=? AND heartbeat_at>=?",
            (session_id, time.time() - STREAM_LEASE_SECONDS),
        ).fetchone() is not None


def update_stream_partial_content(session_id, owner_token, content):
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE active_streams SET partial_content=? WHERE session_id=? AND owner_token=? AND heartbeat_at>=?",
            (content, session_id, owner_token, time.time() - STREAM_LEASE_SECONDS),
        )
        conn.commit()
        return cur.rowcount > 0


def get_stream_partial_content(session_id):
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT partial_content FROM active_streams WHERE session_id=? AND heartbeat_at>=?",
            (session_id, time.time() - STREAM_LEASE_SECONDS),
        ).fetchone()
    return row["partial_content"] if row else None
