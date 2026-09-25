"""One-time import of pre-versioned AI schemas, policies and model catalogues."""

import logging
from pathlib import Path
import secrets
import sqlite3

from sqlite_migrations import execute_script

_logger = logging.getLogger("bananachat.schema")


def _migrate_column(conn, sql: str) -> None:
    """Execute an ALTER TABLE … ADD COLUMN migration.

    Silently ignores 'duplicate column name' errors (the column already
    exists from a previous run).  Any other error is logged at ERROR level
    and re-raised so the application does not silently start with an
    incomplete schema.
    """
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return   # expected on re-run. Not a real error
        _logger.error("Schema migration failed: %s: SQL: %s", exc, sql)
        raise


def _upgrade_legacy(conn):
    """Import supported pre-versioned schemas without intermediate commits."""
    execute_script(conn, Path(__file__).with_name("legacy_schema.sql").read_text(encoding="utf-8"))

    # Migration: custom personalities add a global capability scope. SQLite
    # cannot alter CHECK constraints, so preserve rows while rebuilding.
    access_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='model_access_policies'"
    ).fetchone()
    if access_row and "custom_personality" not in access_row["sql"]:
        for trigger in (
            "cleanup_model_access_after_model_delete",
            "cleanup_model_access_after_category_delete",
        ):
            conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        execute_script(conn, _ACCESS_POLICY_MIGRATION_SQL)
        _create_access_cleanup_triggers(conn)
    else:
        _create_access_cleanup_triggers(conn)

    _migrate_column(conn, "ALTER TABLE model_access_memberships ADD COLUMN expires_at TEXT")

    # Migration: extend the existing Ollama pull queue for checkpoint jobs.
    # Defaults intentionally classify every pre-existing row as Ollama.
    for _col_sql in [
        "ALTER TABLE model_pull_jobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'ollama' "
        "CHECK(backend IN ('ollama', 'comfyui'))",
        "ALTER TABLE model_pull_jobs ADD COLUMN repo_id TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN source_filename TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN revision TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN target_name TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN expected_sha256 TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN expected_size INTEGER",
        "ALTER TABLE model_pull_jobs ADD COLUMN remote_job_id TEXT",
        "ALTER TABLE model_pull_jobs ADD COLUMN idempotency_key TEXT",
    ]:
        _migrate_column(conn, _col_sql)

    _migrate_column(
        conn,
        "ALTER TABLE quota_requests ADD COLUMN resolution_source "
        "TEXT NOT NULL DEFAULT 'manual' "
        "CHECK(resolution_source IN ('manual', 'automatic'))",
    )
    for _row in conn.execute(
        "SELECT id FROM model_pull_jobs "
        "WHERE idempotency_key IS NULL OR idempotency_key=''"
    ).fetchall():
        conn.execute(
            "UPDATE model_pull_jobs SET idempotency_key=? WHERE id=?",
            (secrets.token_hex(16), _row["id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_jobs_idempotency "
        "ON model_pull_jobs(idempotency_key)"
    )
    conn.execute(
        "UPDATE model_pull_jobs SET backend='ollama' "
        "WHERE backend IS NULL OR backend NOT IN ('ollama', 'comfyui')"
    )

    # Capability policies always exist. Category/model policies are
    # created only when customized and otherwise evaluate as allow_all.
    conn.execute(
        "INSERT OR IGNORE INTO model_access_policies "
        "(scope, resource_id, mode, requests_enabled) VALUES ('uncensored', 0, ?, 1)",
        ("deny_except_allowlist",),
    )
    conn.execute(
        "INSERT OR IGNORE INTO model_access_policies "
        "(scope, resource_id, mode, requests_enabled) VALUES ('image_generation', 0, ?, 1)",
        ("allow_all",),
    )
    conn.execute(
        "INSERT OR IGNORE INTO model_access_policies "
        "(scope, resource_id, mode, requests_enabled) VALUES ('custom_personality', 0, ?, 1)",
        ("allow_except_denylist",),
    )

    # Seed default site_settings row if missing
    row = conn.execute("SELECT id FROM site_settings WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO site_settings (id) VALUES (1)")

    for _col_sql in [
        "ALTER TABLE site_settings ADD COLUMN default_daily_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN default_slow_credits INTEGER",
    ]:
        _migrate_column(conn, _col_sql)

    for _col_sql in [
        "ALTER TABLE site_settings ADD COLUMN warning_banner_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN warning_banner_dismissible INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN warning_banner_message TEXT NOT NULL DEFAULT ''",
    ]:
        _migrate_column(conn, _col_sql)

    # Migration: synced per-user customization and site-wide theme palettes.
    _migrate_column(conn, "ALTER TABLE users ADD COLUMN accessibility TEXT")
    _migrate_column(conn, "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
    for _col_sql in [
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
    ]:
        _migrate_column(conn, _col_sql)

    _migrate_column(conn, "ALTER TABLE chat_sessions ADD COLUMN deleted_at TEXT")

    _migrate_column(conn, "ALTER TABLE active_streams ADD COLUMN partial_content TEXT")

    for _col_sql in [
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
        "ALTER TABLE ai_models ADD COLUMN backend TEXT NOT NULL DEFAULT 'ollama' "
        "CHECK(backend IN ('ollama', 'comfyui'))",
        "ALTER TABLE ai_models ADD COLUMN backend_model_name TEXT",
        "ALTER TABLE ai_models ADD COLUMN backend_available INTEGER NOT NULL DEFAULT 1 "
        "CHECK(backend_available IN (0, 1))",
        "ALTER TABLE ai_models ADD COLUMN backend_last_seen_at TEXT",
    ]:
        _migrate_column(conn, _col_sql)
    # All rows predating multi-backend support are Ollama models. Keep the
    # public ollama_name identifier while recording the backend's own name.
    conn.execute(
        "UPDATE ai_models SET backend='ollama' "
        "WHERE backend IS NULL OR backend NOT IN ('ollama', 'comfyui')"
    )
    conn.execute(
        "UPDATE ai_models SET backend_model_name=ollama_name "
        "WHERE backend='ollama' AND "
        "(backend_model_name IS NULL OR backend_model_name='')"
    )
    conn.execute(
        "UPDATE ai_models SET backend_available=1 WHERE backend_available IS NULL"
    )
    # Image generation is a ComfyUI-only capability. Clear stale flags from
    # catalogs created before backend isolation was enforced.
    conn.execute(
        "UPDATE ai_models SET is_image_generation=0 WHERE backend='ollama'"
    )
    conn.execute(
        "UPDATE ai_models SET is_image_generation=1 WHERE backend='comfyui'"
    )
    conn.execute(
        "UPDATE ai_models SET backend_last_seen_at=COALESCE(updated_at, datetime('now')) "
        "WHERE backend='ollama' AND backend_last_seen_at IS NULL"
    )

    _migrate_column(
        conn, "ALTER TABLE image_credit_reservations ADD COLUMN updated_at TEXT"
    )
    _migrate_column(
        conn, "ALTER TABLE chat_sessions ADD COLUMN personality_id INTEGER "
        "REFERENCES personalities(id) ON DELETE SET NULL"
    )
    conn.execute(
        "UPDATE image_credit_reservations SET updated_at=created_at "
        "WHERE updated_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_credit_reservations_updated "
        "ON image_credit_reservations(updated_at)"
    )

    # Migration: split system RAM out of the legacy VRAM fields and add
    # source metadata. Older collectors wrote psutil.virtual_memory() into
    # gpu_memory_* whenever no GPU was detected, which made the admin UI
    # report host RAM as VRAM.
    _metrics_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(compute_snapshots)")
    }
    for _name, _decl in [
        ("gpu_utilization_percent", "REAL"),
        ("system_memory_used_mb", "REAL"),
        ("system_memory_total_mb", "REAL"),
        ("metrics_source", "TEXT"),
    ]:
        if _name not in _metrics_columns:
            conn.execute(f"ALTER TABLE compute_snapshots ADD COLUMN {_name} {_decl}")
            _metrics_columns.add(_name)
    conn.execute(
        "UPDATE compute_snapshots SET "
        "system_memory_used_mb=gpu_memory_used_mb, "
        "system_memory_total_mb=gpu_memory_total_mb, "
        "gpu_memory_used_mb=NULL, gpu_memory_total_mb=NULL, "
        "metrics_source='legacy-system-ram' "
        "WHERE metrics_source IS NULL AND gpu_name IS NULL "
        "AND gpu_memory_total_mb IS NOT NULL "
        "AND (active_models IS NULL OR active_models='[]')"
    )

    for _col_sql in [
        "ALTER TABLE site_settings ADD COLUMN music_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN music_visible INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_opt_in_allowed INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_opt_out_allowed INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE site_settings ADD COLUMN music_credit_multiplier REAL NOT NULL DEFAULT 2.0",
        "ALTER TABLE site_settings ADD COLUMN music_playback_mode TEXT NOT NULL DEFAULT 'sequential'",
    ]:
        _migrate_column(conn, _col_sql)

    for _col_sql in [
        "ALTER TABLE users ADD COLUMN music_opted_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN music_forced INTEGER NOT NULL DEFAULT 0",
    ]:
        _migrate_column(conn, _col_sql)

    # Music tracks table (admin-uploaded audio files)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS music_tracks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        filename        TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL,
        sort_order      INTEGER NOT NULL DEFAULT 0,
        uploaded_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """)

    for _col_sql in [
        # Slow credits global toggle (the extra 15/day tier)
        "ALTER TABLE site_settings ADD COLUMN slow_credits_enabled INTEGER NOT NULL DEFAULT 1",
        # API RPM (NULL = 60 default from config)
        "ALTER TABLE site_settings ADD COLUMN api_rpm INTEGER",
        # Chat RPM (NULL = unlimited)
        "ALTER TABLE site_settings ADD COLUMN chat_rpm INTEGER",
        # Chat daily credit limits (optional hard cap)
        "ALTER TABLE site_settings ADD COLUMN chat_daily_limit_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN chat_daily_credits INTEGER",
        "ALTER TABLE site_settings ADD COLUMN chat_daily_slow_credits INTEGER",
        # Music bonus mode: 'multiplier' or 'fixed'
        "ALTER TABLE site_settings ADD COLUMN music_bonus_mode TEXT NOT NULL DEFAULT 'multiplier'",
        "ALTER TABLE site_settings ADD COLUMN music_bonus_fixed_credits INTEGER NOT NULL DEFAULT 30",
        "ALTER TABLE site_settings ADD COLUMN music_bonus_fixed_slow INTEGER NOT NULL DEFAULT 15",
        # Permanent quota requests at or below both limits may be approved
        # without an administrator. The policy is opt-in.
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_max_credits INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE site_settings ADD COLUMN quota_auto_approve_max_slow_credits INTEGER NOT NULL DEFAULT 0",
    ]:
        _migrate_column(conn, _col_sql)

    # Preserve the newest pending request if an older deployment admitted
    # duplicates before enforcing the database-level invariant.
    conn.execute(
        "UPDATE quota_requests AS qr SET status='denied', "
        "admin_message=COALESCE(admin_message, "
        "'Closed automatically because a newer quota request was already pending.'), "
        "resolved_at=COALESCE(resolved_at, datetime('now')) "
        "WHERE status='pending' AND EXISTS ("
        "SELECT 1 FROM quota_requests newer WHERE newer.user_id=qr.user_id "
        "AND newer.status='pending' AND newer.id>qr.id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_quota_requests_one_pending "
        "ON quota_requests(user_id) WHERE status='pending'"
    )

    # Migration: expand credit_ledger.request_type CHECK to include chat types
    # SQLite cannot ALTER a CHECK constraint: rebuild the table if needed.
    cl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='credit_ledger'"
    ).fetchone()
    if cl_row and "'chat'" not in cl_row["sql"]:
        execute_script(conn, """
        CREATE TABLE IF NOT EXISTS credit_ledger_new (
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
        INSERT INTO credit_ledger_new SELECT * FROM credit_ledger;
        DROP TABLE credit_ledger;
        ALTER TABLE credit_ledger_new RENAME TO credit_ledger;
        """)


_ACCESS_POLICY_MIGRATION_SQL = """
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
            INSERT INTO model_access_policies_new
                SELECT * FROM model_access_policies;
            DROP TABLE model_access_policies;
            ALTER TABLE model_access_policies_new RENAME TO model_access_policies;
            """


def _create_access_cleanup_triggers(conn):
    for scope, table, name in (
        ("model", "ai_models", "cleanup_model_access_after_model_delete"),
        ("category", "model_categories", "cleanup_model_access_after_category_delete"),
    ):
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {name}
            AFTER DELETE ON {table}
            BEGIN
                DELETE FROM model_access_policies
                WHERE scope='{scope}' AND resource_id=OLD.id;
            END
        """)
