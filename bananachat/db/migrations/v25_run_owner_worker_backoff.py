"""Version 25: usage is recorded while answers run; workers, storage and sharing are bounded per account.

* ``active_streams.user_id`` and ``is_incognito``: a chat erased while it was
  answering (a no-history chat, ``BC_DELETED_CHAT_RETENTION_DAYS=0``, "delete
  all my chats") keeps its lease until the run ends, so what was generated is
  still charged, also by recovery when the generating process died.
* ``completion_leases``: API and playground answers record their usage while
  they stream, so an answer whose server process was replaced or stopped is
  still charged (and counted in the metrics) from its last record.
* ``worker_nodes.strikes`` and ``cooldown_until``: a worker that times out or
  fails a job gets no new jobs for a while (doubling up to an hour, cleared by
  a completed job).
* ``worker_jobs.requeues`` and ``deferred_by``: a job handed back by its worker
  is not claimed again by that worker, and goes back to the pool a few times
  at most before the server answers it. ``requested_by`` and ``fault`` name the
  account a job served and whether its failure was held against the worker
  (``worker``) or the request (``request``), so one account cannot pause every
  worker.
* ``chat_attachments.stored_bytes``: what an attachment takes in the database
  (the text extracted from a document, the stored image), which the attachment
  storage limits count; ``size_bytes`` stays the uploaded file's size.
* ``chat_sessions.share_blocked_at``: an administrator stopped sharing the
  chat, and its owner cannot share it again until an administrator allows it.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    add_columns(conn, "active_streams", {
        "user_id": "TEXT",
        "is_incognito": "INTEGER NOT NULL DEFAULT 0 CHECK(is_incognito IN (0,1))",
    })
    conn.execute("UPDATE active_streams SET user_id=(SELECT s.user_id FROM chat_sessions s "
                 "WHERE s.id=active_streams.session_id), is_incognito=COALESCE((SELECT s.is_incognito "
                 "FROM chat_sessions s WHERE s.id=active_streams.session_id), 0) WHERE user_id IS NULL")
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS completion_leases (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_id      INTEGER,
            request_type  TEXT NOT NULL,
            model_id      INTEGER,
            tokens_in     INTEGER NOT NULL DEFAULT 0,
            tokens_out    INTEGER NOT NULL DEFAULT 0,
            queue_wait_ms INTEGER NOT NULL DEFAULT 0,
            started_at    REAL NOT NULL,
            heartbeat_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_completion_leases_heartbeat ON completion_leases(heartbeat_at);
    """)
    add_columns(conn, "worker_nodes", {
        "strikes": "INTEGER NOT NULL DEFAULT 0",
        "cooldown_until": "REAL",
    })
    add_columns(conn, "worker_jobs", {
        "requeues": "INTEGER NOT NULL DEFAULT 0",
        "deferred_by": "TEXT",
        "requested_by": "TEXT",
        "fault": "TEXT CHECK(fault IS NULL OR fault IN ('worker','request'))",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_jobs_requested_by ON worker_jobs(requested_by, done_at)")
    add_columns(conn, "chat_attachments", {"stored_bytes": "INTEGER"})
    conn.execute("UPDATE chat_attachments SET stored_bytes=COALESCE(length(image_data), 0) + "
                 "COALESCE(length(CAST(extracted_text AS BLOB)), 0) WHERE stored_bytes IS NULL")
    add_columns(conn, "chat_sessions", {"share_blocked_at": "TEXT"})
