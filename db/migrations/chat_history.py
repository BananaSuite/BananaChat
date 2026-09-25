"""Version 4: keyset paging and recent inference context share a covering index."""


def upgrade(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id,id)")
