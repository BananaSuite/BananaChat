"""Version 24: two-step verification and recent sign-in confirmation.

``user_two_step`` holds one row per account that turned two-step
verification on (the authenticator secret encrypted, recovery codes as
digests). ``auth_sessions.verified_at`` records when a browser session last
proved its password or code, for the administrator actions that ask for a
recent confirmation. ``site_settings.admin_two_step_required`` is off, so
nothing changes for existing accounts until they choose it.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS user_two_step (
            user_id          TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            secret_encrypted TEXT NOT NULL,
            recovery_hashes  TEXT NOT NULL DEFAULT '[]',
            last_counter     INTEGER NOT NULL DEFAULT -1,
            enabled_at       TEXT NOT NULL
        );
    """)
    add_columns(conn, "auth_sessions", {"verified_at": "TEXT"})
    add_columns(conn, "site_settings", {
        "admin_two_step_required": "INTEGER NOT NULL DEFAULT 0 CHECK(admin_two_step_required IN (0,1))",
    })
