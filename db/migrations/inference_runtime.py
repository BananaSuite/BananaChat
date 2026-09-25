"""Version 3: fair queue ownership and bounded worker relay accounting."""

from sqlite_migrations import add_columns


def upgrade(conn):
    for table in ("request_metrics", "credit_ledger", "chat_messages"):
        add_columns(conn, table, {"usage_estimated": "INTEGER NOT NULL DEFAULT 0"})
    add_columns(conn, "inference_queue", {"owner_key": "TEXT"})
    conn.execute("DELETE FROM inference_queue")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inference_owner_running ON inference_queue(owner_key) WHERE status='running' AND owner_key IS NOT NULL")
    add_columns(conn, "worker_jobs", {
        "stream_bytes": "INTEGER NOT NULL DEFAULT 0",
        "next_chunk_seq": "INTEGER NOT NULL DEFAULT 0",
        "finish_reason": "TEXT NOT NULL DEFAULT 'stop'",
    })
    conn.execute("UPDATE worker_jobs SET status='failed', stop_requested=1, done_at=strftime('%s','now'), error_message='Server upgraded during inference' WHERE status IN ('pending','claimed','streaming')")
    conn.execute("DELETE FROM worker_job_chunks")
