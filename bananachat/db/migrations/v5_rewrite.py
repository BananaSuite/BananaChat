"""Version 5: the data model of the rewrite.

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed.

* Instances installed while the product was called BananaAI still carry that
  name as their site name, which every page title and the navigation show.
  Rename exactly those legacy values; custom names are left alone.
* Timestamps written by earlier code mixed ``YYYY-MM-DD HH:MM:SS`` (SQLite)
  with ISO-8601 ``T``/``+00:00`` strings, which broke time-window queries and
  retention. Normalise every stored timestamp to the SQLite form (UTC).
* Recreate the credit-ledger indexes lost by the version-1 table rebuild and
  add indexes for foreign keys and retention scans.
* Server-side login sessions (revocable, listed per user), a small key/value
  table for state shared between worker processes, and an audit log.
* Remote-worker jobs no longer keep prompts after they finish.
"""

LEGACY_SITE_NAMES = ("bananaai", "banana ai", "banana-ai", "banana_ai", "banana.ai")

_TIMESTAMPS = {
    "users": ("created_at", "last_login_at", "suspended_until"),
    "invite_codes": ("created_at", "expires_at", "deleted_at"),
    "invite_code_usage": ("used_at",),
    "login_attempts": ("attempted_at",),
    "rate_limit_hits": ("hit_at",),
    "ai_models": ("created_at", "updated_at", "backend_last_seen_at"),
    "model_categories": ("created_at",),
    "model_access_policies": ("created_at", "updated_at"),
    "model_access_memberships": ("expires_at", "created_at", "updated_at"),
    "model_access_requests": ("created_at", "resolved_at"),
    "api_tokens": ("created_at", "last_used_at"),
    "user_quota": ("updated_at",),
    "credit_ledger": ("created_at",),
    "image_credit_reservations": ("created_at", "updated_at"),
    "quota_requests": ("created_at", "resolved_at"),
    "personalities": ("disabled_until", "created_at", "updated_at"),
    "chat_sessions": ("created_at", "updated_at", "deleted_at"),
    "chat_messages": ("created_at",),
    "chat_attachments": ("created_at",),
    "incognito_audit": ("created_at",),
    "compute_snapshots": ("recorded_at",),
    "request_metrics": ("created_at",),
    "model_pull_jobs": ("created_at", "started_at", "finished_at"),
    "music_tracks": ("created_at",),
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_date ON credit_ledger(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_credit_ledger_token ON credit_ledger(token_id)",
    "CREATE INDEX IF NOT EXISTS idx_credit_ledger_model ON credit_ledger(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_incognito_audit_session ON incognito_audit(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_generation_message ON chat_sessions(generation_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_personality ON chat_sessions(personality_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_retention ON chat_sessions(is_incognito, deleted_at, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_model ON chat_messages(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_active_streams_user_message ON active_streams(user_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_worker_jobs_worker ON worker_jobs(worker_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_worker_jobs_done ON worker_jobs(status, done_at)",
    "CREATE INDEX IF NOT EXISTS idx_rate_limit_hit_at ON rate_limit_hits(hit_at)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(attempted_at)",
    "CREATE INDEX IF NOT EXISTS idx_invite_usage_code ON invite_code_usage(invite_code_id)",
    "CREATE INDEX IF NOT EXISTS idx_invite_usage_user ON invite_code_usage(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_category_assignments_category ON model_category_assignments(category_id)",
    "CREATE INDEX IF NOT EXISTS idx_request_metrics_user ON request_metrics(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_request_metrics_model ON request_metrics(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_image_reservations_token ON image_credit_reservations(token_id)",
    "CREATE INDEX IF NOT EXISTS idx_access_requests_user ON model_access_requests(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_quota_requests_user ON quota_requests(user_id, created_at)",
)

_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    id_hash       TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT NOT NULL,
    user_agent    TEXT NOT NULL DEFAULT '',
    ip_address    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS runtime_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    TEXT REFERENCES users(id) ON DELETE SET NULL,
    actor_name  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '',
    ip_address  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_log_date ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_id, created_at);
"""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def upgrade(conn):
    from sqlite_migrations import execute_script

    placeholders = ",".join("?" for _ in LEGACY_SITE_NAMES)
    conn.execute(
        f"UPDATE site_settings SET site_name='BananaChat' WHERE lower(trim(site_name)) IN ({placeholders})",
        LEGACY_SITE_NAMES,
    )

    for table, columns in _TIMESTAMPS.items():
        existing = _columns(conn, table)
        for column in columns:
            if column not in existing:
                continue
            conn.execute(
                f'UPDATE "{table}" SET "{column}"=datetime("{column}") '
                f'WHERE "{column}" LIKE \'____-__-__T%\' AND datetime("{column}") IS NOT NULL'
            )

    for statement in _INDEXES:
        conn.execute(statement)
    execute_script(conn, _NEW_TABLES)

    conn.execute("UPDATE worker_jobs SET messages='[]', options=NULL WHERE status IN ('done', 'failed', 'timeout')")
    conn.execute("DELETE FROM worker_job_chunks WHERE job_id IN "
                 "(SELECT id FROM worker_jobs WHERE status IN ('done', 'failed', 'timeout'))")
