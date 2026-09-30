"""Schema versions 1-4, carried over unchanged in effect from earlier releases.

Every installed database has already run these. They stay so that very old
databases (and exports imported with ``bananachat restore --legacy-database``)
upgrade along exactly the same path. Do not edit their behaviour; add a new
version instead.
"""

import secrets
import sqlite3
from pathlib import Path

from sqlite_migrations import add_columns, execute_script


def _add_column(conn, sql):
    """Run ``ALTER TABLE ... ADD COLUMN`` unless the column already exists."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as error:
        if "duplicate column name" not in str(error).lower():
            raise


_ACCESS_POLICY_REBUILD = """
CREATE TABLE model_access_policies_new (
    scope           TEXT NOT NULL
                        CHECK(scope IN ('uncensored', 'image_generation', 'custom_personality', 'category', 'model')),
    resource_id     INTEGER NOT NULL DEFAULT 0,
    mode            TEXT NOT NULL DEFAULT 'allow_all'
                        CHECK(mode IN ('allow_all', 'deny_except_allowlist', 'allow_except_denylist')),
    requests_enabled INTEGER NOT NULL DEFAULT 0 CHECK(requests_enabled IN (0, 1)),
    updated_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(scope, resource_id),
    CHECK(
        (scope IN ('uncensored', 'image_generation', 'custom_personality') AND resource_id=0)
        OR (scope IN ('category', 'model') AND resource_id>0)
    )
);
INSERT INTO model_access_policies_new (scope, resource_id, mode, requests_enabled, updated_by, created_at, updated_at)
    SELECT scope, resource_id, mode, requests_enabled, updated_by, created_at, updated_at FROM model_access_policies;
DROP TABLE model_access_policies;
ALTER TABLE model_access_policies_new RENAME TO model_access_policies;
"""

_LEDGER_REBUILD = """
CREATE TABLE credit_ledger_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    credits_used    REAL NOT NULL DEFAULT 0,
    is_slow         INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    request_type    TEXT NOT NULL DEFAULT 'api'
                        CHECK(request_type IN ('api', 'playground', 'chat', 'chat_incognito')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO credit_ledger_new (id, user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out,
                               request_type, created_at)
    SELECT id, user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out, request_type, created_at
    FROM credit_ledger;
DROP TABLE credit_ledger;
ALTER TABLE credit_ledger_new RENAME TO credit_ledger;
"""


def _access_cleanup_triggers(conn):
    for scope, table, name in (
        ("model", "ai_models", "cleanup_model_access_after_model_delete"),
        ("category", "model_categories", "cleanup_model_access_after_category_delete"),
    ):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {name} AFTER DELETE ON {table} BEGIN "
            f"DELETE FROM model_access_policies WHERE scope='{scope}' AND resource_id=OLD.id; END"
        )


def v1_baseline(conn):
    """Create the baseline schema and normalise databases from before versioning."""
    execute_script(conn, Path(__file__).with_name("legacy_schema.sql").read_text(encoding="utf-8"))

    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='model_access_policies'").fetchone()
    if row and "custom_personality" not in row[0]:
        conn.execute("DROP TRIGGER IF EXISTS cleanup_model_access_after_model_delete")
        conn.execute("DROP TRIGGER IF EXISTS cleanup_model_access_after_category_delete")
        execute_script(conn, _ACCESS_POLICY_REBUILD)
    _access_cleanup_triggers(conn)

    for sql in (
        "ALTER TABLE model_access_memberships ADD COLUMN expires_at TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'ollama' CHECK(backend IN ('ollama', 'comfyui'))",
        "ALTER TABLE model_pull_jobs ADD COLUMN repo_id TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN source_filename TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN revision TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN target_name TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN expected_sha256 TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN expected_size INTEGER",
        "ALTER TABLE model_pull_jobs ADD COLUMN remote_job_id TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN idempotency_key TEXT",
        "ALTER TABLE quota_requests ADD COLUMN resolution_source TEXT NOT NULL DEFAULT 'manual' "
        "CHECK(resolution_source IN ('manual', 'automatic'))",
    ):
        _add_column(conn, sql)
    for (job_id,) in conn.execute(
            "SELECT id FROM model_pull_jobs WHERE idempotency_key IS NULL OR idempotency_key=''").fetchall():
        conn.execute("UPDATE model_pull_jobs SET idempotency_key=? WHERE id=?", (secrets.token_hex(16), job_id))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_jobs_idempotency ON model_pull_jobs(idempotency_key)")
    conn.execute("UPDATE model_pull_jobs SET backend='ollama' WHERE backend IS NULL OR backend NOT IN ('ollama', 'comfyui')")

    for scope, mode in (("uncensored", "deny_except_allowlist"), ("image_generation", "allow_all"),
                        ("custom_personality", "allow_except_denylist")):
        conn.execute("INSERT OR IGNORE INTO model_access_policies (scope, resource_id, mode, requests_enabled) "
                     "VALUES (?, 0, ?, 1)", (scope, mode))
    if not conn.execute("SELECT 1 FROM site_settings WHERE id=1").fetchone():
        conn.execute("INSERT INTO site_settings (id) VALUES (1)")

    for sql in (
        "ALTER TABLE site_settings ADD COLUMN default_daily_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN default_slow_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN warning_banner_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN warning_banner_dismissible INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN warning_banner_message TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN accessibility TEXT",
        "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN default_theme_mode TEXT NOT NULL DEFAULT 'dark'",
        "ALTER TABLE site_settings ADD COLUMN primary_color TEXT NOT NULL DEFAULT '#e6be32'",
        "ALTER TABLE site_settings ADD COLUMN secondary_color TEXT NOT NULL DEFAULT '#1d1d1d'",
        "ALTER TABLE site_settings ADD COLUMN accent_color TEXT NOT NULL DEFAULT '#cda624'",
        "ALTER TABLE site_settings ADD COLUMN text_color TEXT NOT NULL DEFAULT '#ededed'",
        "ALTER TABLE site_settings ADD COLUMN sidebar_color TEXT NOT NULL DEFAULT '#181818'",
        "ALTER TABLE site_settings ADD COLUMN bg_color TEXT NOT NULL DEFAULT '#141414'",
        "ALTER TABLE site_settings ADD COLUMN light_primary_color TEXT NOT NULL DEFAULT '#8a6500'",
        "ALTER TABLE site_settings ADD COLUMN light_secondary_color TEXT NOT NULL DEFAULT '#ffffff'",
        "ALTER TABLE site_settings ADD COLUMN light_accent_color TEXT NOT NULL DEFAULT '#6f5000'",
        "ALTER TABLE site_settings ADD COLUMN light_text_color TEXT NOT NULL DEFAULT '#202124'",
        "ALTER TABLE site_settings ADD COLUMN light_sidebar_color TEXT NOT NULL DEFAULT '#f4f1e8'",
        "ALTER TABLE site_settings ADD COLUMN light_bg_color TEXT NOT NULL DEFAULT '#faf9f5'",
        "ALTER TABLE chat_sessions ADD COLUMN deleted_at TEXT",
        "ALTER TABLE active_streams ADD COLUMN partial_content TEXT",
        "ALTER TABLE ai_models ADD COLUMN system_prompt TEXT",
        "ALTER TABLE ai_models ADD COLUMN temperature REAL",
        "ALTER TABLE ai_models ADD COLUMN top_p REAL",
        "ALTER TABLE ai_models ADD COLUMN top_k INTEGER",
        "ALTER TABLE ai_models ADD COLUMN num_ctx INTEGER",
        "ALTER TABLE ai_models ADD COLUMN repeat_penalty REAL",
        "ALTER TABLE ai_models ADD COLUMN is_reasoning INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_models ADD COLUMN is_uncensored INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_models ADD COLUMN is_image_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_models ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ai_models ADD COLUMN backend TEXT NOT NULL DEFAULT 'ollama' CHECK(backend IN ('ollama', 'comfyui'))",
        "ALTER TABLE ai_models ADD COLUMN backend_model_name TEXT",
        "ALTER TABLE ai_models ADD COLUMN backend_available INTEGER NOT NULL DEFAULT 1 CHECK(backend_available IN (0, 1))",
        "ALTER TABLE ai_models ADD COLUMN backend_last_seen_at TEXT",
    ):
        _add_column(conn, sql)
    conn.execute("UPDATE ai_models SET backend='ollama' WHERE backend IS NULL OR backend NOT IN ('ollama', 'comfyui')")
    conn.execute("UPDATE ai_models SET backend_model_name=ollama_name "
                 "WHERE backend='ollama' AND (backend_model_name IS NULL OR backend_model_name='')")
    conn.execute("UPDATE ai_models SET backend_available=1 WHERE backend_available IS NULL")
    conn.execute("UPDATE ai_models SET is_image_generation=0 WHERE backend='ollama'")
    conn.execute("UPDATE ai_models SET is_image_generation=1 WHERE backend='comfyui'")
    conn.execute("UPDATE ai_models SET backend_last_seen_at=COALESCE(updated_at, datetime('now')) "
                 "WHERE backend='ollama' AND backend_last_seen_at IS NULL")

    _add_column(conn, "ALTER TABLE image_credit_reservations ADD COLUMN updated_at TEXT")
    _add_column(conn, "ALTER TABLE chat_sessions ADD COLUMN personality_id INTEGER "
                      "REFERENCES personalities(id) ON DELETE SET NULL")
    conn.execute("UPDATE image_credit_reservations SET updated_at=created_at WHERE updated_at IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_image_credit_reservations_updated "
                 "ON image_credit_reservations(updated_at)")

    add_columns(conn, "compute_snapshots", {
        "gpu_utilization_percent": "REAL", "system_memory_used_mb": "REAL",
        "system_memory_total_mb": "REAL", "metrics_source": "TEXT",
    })
    # Older collectors stored host RAM in the GPU columns when no GPU existed.
    conn.execute(
        "UPDATE compute_snapshots SET system_memory_used_mb=gpu_memory_used_mb, "
        "system_memory_total_mb=gpu_memory_total_mb, gpu_memory_used_mb=NULL, gpu_memory_total_mb=NULL, "
        "metrics_source='legacy-system-ram' WHERE metrics_source IS NULL AND gpu_name IS NULL "
        "AND gpu_memory_total_mb IS NOT NULL AND (active_models IS NULL OR active_models='[]')"
    )

    for sql in (
        "ALTER TABLE site_settings ADD COLUMN music_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN music_visible INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_opt_in_allowed INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_opt_out_allowed INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_credit_multiplier REAL NOT NULL DEFAULT 2.0",
        "ALTER TABLE site_settings ADD COLUMN music_playback_mode TEXT NOT NULL DEFAULT 'sequential'",
        "ALTER TABLE users ADD COLUMN music_opted_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN music_forced INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column(conn, sql)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS music_tracks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,"
        " sort_order INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT REFERENCES users(id) ON DELETE SET NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for sql in (
        "ALTER TABLE site_settings ADD COLUMN slow_credits_enabled INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN api_rpm INTEGER",
        "ALTER TABLE site_settings ADD COLUMN chat_rpm INTEGER",
        "ALTER TABLE site_settings ADD COLUMN chat_daily_limit_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN chat_daily_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN chat_daily_slow_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN music_bonus_mode TEXT NOT NULL DEFAULT 'multiplier'",
        "ALTER TABLE site_settings ADD COLUMN music_bonus_fixed_credits INTEGER NOT NULL DEFAULT 30",
        "ALTER TABLE site_settings ADD COLUMN music_bonus_fixed_slow INTEGER NOT NULL DEFAULT 15",
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_max_credits INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_max_slow_credits INTEGER NOT NULL DEFAULT 0",
    ):
        _add_column(conn, sql)

    # Keep the newest pending quota request per user before enforcing uniqueness.
    conn.execute(
        "UPDATE quota_requests SET status='denied', admin_message=COALESCE(admin_message, "
        "'Closed automatically because a newer quota request was already pending.'), "
        "resolved_at=COALESCE(resolved_at, datetime('now')) "
        "WHERE status='pending' AND EXISTS (SELECT 1 FROM quota_requests newer "
        "WHERE newer.user_id=quota_requests.user_id AND newer.status='pending' AND newer.id>quota_requests.id)"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_quota_requests_one_pending "
                 "ON quota_requests(user_id) WHERE status='pending'")

    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='credit_ledger'").fetchone()
    if row and "'chat'" not in row[0]:
        execute_script(conn, _LEDGER_REBUILD)


def v2_chat_runtime(conn):
    """Durable generation outcomes and recoverable response checkpoints."""
    add_columns(conn, "chat_sessions", {
        "generation_state": "TEXT NOT NULL DEFAULT 'idle'",
        "generation_error": "TEXT NOT NULL DEFAULT ''",
        "generation_message_id": "INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL",
    })
    add_columns(conn, "chat_messages", {"generation_state": "TEXT NOT NULL DEFAULT 'completed'"})
    add_columns(conn, "active_streams", {
        "model_id": "INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        "tokens_in": "INTEGER NOT NULL DEFAULT 0",
        "tokens_out": "INTEGER NOT NULL DEFAULT 0",
        "started_at": "REAL NOT NULL DEFAULT 0",
        "user_message_id": "INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL",
    })
    conn.execute("UPDATE active_streams SET heartbeat_at=0")
    conn.execute("DELETE FROM inference_queue")


def v3_inference_runtime(conn):
    """Fair queue ownership and bounded worker relay accounting."""
    for table in ("request_metrics", "credit_ledger", "chat_messages"):
        add_columns(conn, table, {"usage_estimated": "INTEGER NOT NULL DEFAULT 0"})
    add_columns(conn, "inference_queue", {"owner_key": "TEXT"})
    conn.execute("DELETE FROM inference_queue")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inference_owner_running ON inference_queue(owner_key) "
                 "WHERE status='running' AND owner_key IS NOT NULL")
    add_columns(conn, "worker_jobs", {
        "stream_bytes": "INTEGER NOT NULL DEFAULT 0",
        "next_chunk_seq": "INTEGER NOT NULL DEFAULT 0",
        "finish_reason": "TEXT NOT NULL DEFAULT 'stop'",
    })
    conn.execute("UPDATE worker_jobs SET status='failed', stop_requested=1, done_at=strftime('%s','now'), "
                 "error_message='Server upgraded during inference' WHERE status IN ('pending','claimed','streaming')")
    conn.execute("DELETE FROM worker_job_chunks")


def v4_chat_history(conn):
    """Keyset paging and recent-context loading share a covering index."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id,id)")
