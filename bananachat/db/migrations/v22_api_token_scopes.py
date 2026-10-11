"""Version 22: API token scopes and optional expiry.

Existing tokens keep full access and never expire, as before.
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "api_tokens", {
        "scope": "TEXT NOT NULL DEFAULT 'full' CHECK(scope IN ('full','read'))",
        "expires_at": "TEXT",
    })
