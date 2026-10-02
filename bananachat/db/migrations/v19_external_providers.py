"""Version 19: external API providers and model bindings; credentials stay off-DB."""
import re

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS external_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            protocol TEXT NOT NULL CHECK(protocol IN ('openai', 'anthropic')),
            secret_ref TEXT,
            token_parameter TEXT NOT NULL DEFAULT 'auto' CHECK(token_parameter IN ('auto','max_tokens','max_completion_tokens')),
            stream_usage INTEGER NOT NULL DEFAULT 1 CHECK(stream_usage IN (0,1)),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            allow_private INTEGER NOT NULL DEFAULT 0 CHECK(allow_private IN (0,1)),
            auto_enroll INTEGER NOT NULL DEFAULT 0 CHECK(auto_enroll IN (0,1)),
            revision INTEGER NOT NULL DEFAULT 1,
            discovered_models TEXT NOT NULL DEFAULT '[]',
            last_checked_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    add_columns(conn, "ai_models", {
        "external_provider_id": "INTEGER REFERENCES external_providers(id) ON DELETE SET NULL",
        "external_config": "TEXT NOT NULL DEFAULT '{}'",
    })
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_models'").fetchone()[0]
    if "'external'" not in sql:
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='ai_models'").fetchone()
        widened = re.sub(r"(CHECK\s*\(\s*backend\s+IN\s*\([^)]*)\)", r"\1, 'external')", sql, flags=re.I)
        if widened == sql:
            raise RuntimeError("The model backend constraint could not be upgraded.")
        dependents = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='ai_models' "
                                                   "AND type IN ('index','trigger') AND sql IS NOT NULL")]
        temporary = re.sub(r'CREATE TABLE\s+["`\[]?ai_models["`\]]?', 'CREATE TABLE ai_models_v19', widened,
                           count=1, flags=re.I)
        if temporary == widened:
            raise RuntimeError("The model table could not be upgraded.")
        conn.execute(temporary)
        fields = ', '.join('"' + row[1] + '"' for row in conn.execute('PRAGMA table_info(ai_models)'))
        conn.execute(f'INSERT INTO ai_models_v19 ({fields}) SELECT {fields} FROM ai_models')
        conn.execute('DROP TABLE ai_models')
        conn.execute('ALTER TABLE ai_models_v19 RENAME TO ai_models')
        if sequence is not None:
            # Copying surviving rows cannot retain IDs of previously deleted
            # models. Preserve the high-water mark so old model IDs stay retired.
            changed = conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='ai_models'",
                                   (sequence[0],)).rowcount
            if not changed:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('ai_models',?)", (sequence[0],))
        for statement in dependents:
            conn.execute(statement)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_models_external_provider ON ai_models(external_provider_id)')
