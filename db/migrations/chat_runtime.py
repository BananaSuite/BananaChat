"""Version 2: durable generation outcomes and recoverable response checkpoints."""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "chat_sessions", {
        "generation_state": "TEXT NOT NULL DEFAULT 'idle'",
        "generation_error": "TEXT NOT NULL DEFAULT ''",
        "generation_message_id": "INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL",
    })
    add_columns(conn, "chat_messages", {
        "generation_state": "TEXT NOT NULL DEFAULT 'completed'",
    })
    add_columns(conn, "active_streams", {
        "model_id": "INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        "tokens_in": "INTEGER NOT NULL DEFAULT 0",
        "tokens_out": "INTEGER NOT NULL DEFAULT 0",
        "started_at": "REAL NOT NULL DEFAULT 0",
        "user_message_id": "INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL",
    })
    # Application updates stop the old workers. Their leases cannot be resumed
    # by the new process; its first status/start request recovers checkpoints.
    conn.execute("UPDATE active_streams SET heartbeat_at=0")
    conn.execute("DELETE FROM inference_queue")
