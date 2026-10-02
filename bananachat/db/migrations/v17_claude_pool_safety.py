"""Version 17: exclusive, renewable Claude account leases and quota freshness.

No credentials are stored here. Existing quota counters and administrator
limits remain intact; previous reports are conservatively marked as reported
snapshots so an old observation cannot silently become unlimited capacity.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(claude_accounts)")}
    add_columns(conn, "claude_accounts", {
        "quota_source": "TEXT NOT NULL DEFAULT 'local' CHECK(quota_source IN ('local', 'reported'))",
        "quota_updated_at": "TEXT",
        "quota_window_updated_at": "TEXT",
        "quota_weekly_updated_at": "TEXT",
    })
    if "quota_source" not in columns:
        conn.execute("UPDATE claude_accounts SET quota_source='reported', quota_updated_at=last_checked_at "
                     "WHERE last_checked_at IS NOT NULL")
    for scope in ("window", "weekly"):
        field = f"quota_{scope}_updated_at"
        if field not in columns:
            conn.execute(f"UPDATE claude_accounts SET {field}=quota_updated_at WHERE quota_source='reported'")
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS claude_account_leases (
            account_id INTEGER PRIMARY KEY REFERENCES claude_accounts(id) ON DELETE CASCADE,
            lease_id TEXT NOT NULL UNIQUE,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_claude_account_leases_expiry ON claude_account_leases(expires_at);
        CREATE TABLE IF NOT EXISTS claude_account_usage (
            lease_id TEXT PRIMARY KEY,
            account_id INTEGER REFERENCES claude_accounts(id) ON DELETE SET NULL,
            tokens_in INTEGER NOT NULL,
            tokens_out INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
