"""Version 15: optional chat token consumption, recorded on each usage row.

Local chats are unmetered by default; cloud chats keep their model limits.
Existing usage remains counted, including when administrators change settings.
The migration is additive and safe to run again.
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "site_settings", {
        "chat_local_token_consumption": "INTEGER NOT NULL DEFAULT 0",
        "chat_cloud_token_consumption": "INTEGER NOT NULL DEFAULT 1",
    })
    add_columns(conn, "credit_ledger", {
        "consumes_limits": "INTEGER NOT NULL DEFAULT 1 CHECK(consumes_limits IN (0, 1))",
    })
