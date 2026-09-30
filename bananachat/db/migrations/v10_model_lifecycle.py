"""Version 10: model enrollment, retirement of missing or broken models and sturdier downloads.

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed. Safe to run again on a
database that already has it (every step checks what exists).

* ``ai_models`` gains what the catalog sync detects (``provider``,
  ``capabilities``, ``reasoning_levels``, context length, family, parameter
  size, quantisation, digest), the enrollment state (``enrollment``: ``new``,
  ``auto``, ``reviewed`` or ``ignored``; existing models are ``reviewed``), the
  strictness preset for its limits (``limit_preset``), and the retirement
  bookkeeping: absence counters and ``missing_at``, failure counters and
  ``failing_at``, deprecation with a replacement and a retirement date, and a
  deletion that waits for running requests.
* ``model_lifecycle_policy`` - the administrator's settings (one JSON row).
* ``model_ignore_rules`` - names or simple patterns never offered or enabled.
* ``model_lifecycle_events`` - every state change with its reason.
* ``model_pull_jobs`` gains queue order, pause, retry and progress columns;
  duplicate active downloads of one model are cancelled and a partial unique
  index keeps it that way across processes.
* ``inference_queue.model_name`` - the model each waiting or running request
  uses, so a model is not deleted under running requests.
* ``chat_messages.model_label`` - filled with the model's display name when a
  model row is deleted (trigger), so history keeps the name.
"""

from sqlite_migrations import add_columns, execute_script

MODEL_COLUMNS = {
    "provider": "TEXT NOT NULL DEFAULT 'ollama'",
    "capabilities": "TEXT NOT NULL DEFAULT '[]'",
    "reasoning_levels": "TEXT NOT NULL DEFAULT '[]'",
    "reasoning_levels_locked": "INTEGER NOT NULL DEFAULT 0",
    "embedding_only": "INTEGER NOT NULL DEFAULT 0",
    "context_length": "INTEGER",
    "family": "TEXT NOT NULL DEFAULT ''",
    "parameter_size": "TEXT NOT NULL DEFAULT ''",
    "quantization": "TEXT NOT NULL DEFAULT ''",
    "size_bytes": "INTEGER",
    "backend_digest": "TEXT",
    "details_digest": "TEXT",
    "details_at": "TEXT",
    "details_error": "TEXT",
    "limit_preset": "TEXT",
    "enrollment": "TEXT NOT NULL DEFAULT 'reviewed'",
    "enrolled_at": "TEXT",
    "absent_syncs": "INTEGER NOT NULL DEFAULT 0",
    "absent_since": "TEXT",
    "absent_counted_at": "TEXT",
    "missing_at": "TEXT",
    "missing_reason": "TEXT",
    "recent_failures": "INTEGER NOT NULL DEFAULT 0",
    "first_failure_at": "TEXT",
    "last_failure": "TEXT",
    "failing_at": "TEXT",
    "recheck_at": "TEXT",
    "recheck_attempts": "INTEGER NOT NULL DEFAULT 0",
    "deprecated_at": "TEXT",
    "deprecation_note": "TEXT",
    "replacement_id": "INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
    "retire_at": "TEXT",
    "retired_at": "TEXT",
    "delete_requested_at": "TEXT",
    "state_reason": "TEXT",
    "state_changed_at": "TEXT",
}

PULL_COLUMNS = {
    "queue_position": "INTEGER",
    "paused": "INTEGER NOT NULL DEFAULT 0",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at": "TEXT",
    "last_error": "TEXT",
    "bytes_completed": "INTEGER",
    "bytes_total": "INTEGER",
    "progress_at": "TEXT",
    "digest": "TEXT",
    "cleanup_partial": "INTEGER NOT NULL DEFAULT 1",
    "source": "TEXT NOT NULL DEFAULT ''",
}

_SCRIPT = """
CREATE TABLE IF NOT EXISTS model_lifecycle_policy (
    id              INTEGER PRIMARY KEY CHECK(id = 1),
    config          TEXT NOT NULL DEFAULT '{}',
    updated_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_ignore_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern         TEXT NOT NULL UNIQUE,
    note            TEXT NOT NULL DEFAULT '',
    created_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_lifecycle_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    model_name      TEXT NOT NULL,
    event           TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    actor_id        TEXT REFERENCES users(id) ON DELETE SET NULL,
    actor_name      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_model_events_model ON model_lifecycle_events(model_id, id);
CREATE INDEX IF NOT EXISTS idx_model_events_created ON model_lifecycle_events(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_models_replacement ON ai_models(replacement_id);
CREATE INDEX IF NOT EXISTS idx_inference_queue_model ON inference_queue(model_name);
CREATE INDEX IF NOT EXISTS idx_pull_jobs_queue ON model_pull_jobs(status, paused, queue_position, id);

CREATE TRIGGER IF NOT EXISTS keep_model_label_on_model_delete BEFORE DELETE ON ai_models
BEGIN
    UPDATE chat_messages SET model_label=OLD.display_name WHERE model_id=OLD.id AND model_label IS NULL;
END;
"""


def upgrade(conn):
    add_columns(conn, "ai_models", MODEL_COLUMNS)
    add_columns(conn, "model_pull_jobs", PULL_COLUMNS)
    add_columns(conn, "inference_queue", {"model_name": "TEXT"})
    add_columns(conn, "chat_messages", {"model_label": "TEXT"})
    execute_script(conn, _SCRIPT)
    conn.execute("UPDATE ai_models SET provider='comfyui' WHERE backend='comfyui' AND provider='ollama'")
    # A first guess until the next catalog sync reads the model's details.
    conn.execute("UPDATE ai_models SET reasoning_levels='[\"off\",\"on\"]' "
                 "WHERE is_reasoning=1 AND reasoning_levels='[]' AND backend='ollama'")
    conn.execute("UPDATE model_pull_jobs SET queue_position=id WHERE queue_position IS NULL")
    # One active download per model: older releases enforced it only in code.
    conn.execute("UPDATE model_pull_jobs SET status='cancelled', finished_at=datetime('now'), "
                 "error_message='A duplicate download was removed by the upgrade.' "
                 "WHERE status IN ('queued', 'pulling') AND id NOT IN (SELECT MIN(id) FROM model_pull_jobs "
                 "WHERE status IN ('queued', 'pulling') GROUP BY backend, ollama_name)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pull_jobs_one_active ON model_pull_jobs(backend, ollama_name) "
                 "WHERE status IN ('queued', 'pulling')")
    conn.execute("INSERT OR IGNORE INTO model_lifecycle_policy (id, config) VALUES (1, '{}')")
