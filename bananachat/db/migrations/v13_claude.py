"""Version 13: Claude subscription backend and account pool.

Additive only, safe to run again.

* ``ai_models.backend`` also allows ``'claude'`` (subscription models served
  through the Claude site pool, see ``services.claude_pool``). The table is
  rebuilt with the same columns, rows and indexes, only the CHECK widened.
* New ``claude_accounts`` table: the pooled subscription accounts (label,
  status, reported 5-hour and weekly quota) whose remaining capacity shrinks
  the limits of ``provider='claude'`` models via the capacity hook.
"""

from sqlite_migrations import execute_script


_CLAUDE_ACCOUNTS = """
CREATE TABLE IF NOT EXISTS claude_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled', 'error')),
    priority        INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    window_limit    INTEGER,
    window_used     INTEGER NOT NULL DEFAULT 0,
    window_resets_at TEXT,
    weekly_limit    INTEGER,
    weekly_used     INTEGER NOT NULL DEFAULT 0,
    weekly_resets_at TEXT,
    last_checked_at TEXT,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claude_accounts_status ON claude_accounts(status, priority, id);
"""


def _widen_backend(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_models'").fetchone()
    if row is None or row[0] is None:
        return
    sql = row[0]
    if "'claude'" in sql:
        return
    # Widen the backend CHECK to include 'claude'. Handle the two known spellings.
    widened = sql.replace("'ollama', 'comfyui'", "'ollama', 'comfyui', 'claude'")
    widened = widened.replace('"ollama", "comfyui"', '"ollama", "comfyui", "claude"')
    if widened == sql:
        # Fallback: append claude to any backend IN (...) check mentioning ollama.
        import re
        widened = re.sub(r"(CHECK\s*\(\s*backend\s+IN\s*\([^)]*)\)", r"\1, 'claude')", sql,
                         flags=re.IGNORECASE)
        if widened == sql:
            return
    # Save dependent indexes/triggers, rebuild, restore.
    dependents = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND "
        "(tbl_name='ai_models' AND type IN ('index','trigger'))").fetchall()]
    new_sql = widened.replace("CREATE TABLE ai_models", "CREATE TABLE ai_models_v13", 1)
    if "CREATE TABLE ai_models_v13" not in new_sql:
        new_sql = widened.replace("CREATE TABLE \"ai_models\"", "CREATE TABLE ai_models_v13", 1)
    conn.execute(new_sql)
    cols = [r[1] for r in conn.execute('PRAGMA table_info("ai_models")').fetchall()]
    quoted = ", ".join(f'"{c}"' for c in cols)
    conn.execute(f'INSERT INTO ai_models_v13 ({quoted}) SELECT {quoted} FROM ai_models')
    conn.execute("DROP TABLE ai_models")
    conn.execute("ALTER TABLE ai_models_v13 RENAME TO ai_models")
    for statement in dependents:
        if statement:
            try:
                conn.execute(statement)
            except Exception:
                # Indexes on dropped columns would fail; none exist for backend.
                pass


def upgrade(conn):
    _widen_backend(conn)
    execute_script(conn, _CLAUDE_ACCOUNTS)
