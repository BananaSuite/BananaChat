"""Version 8: sandboxed agents and agent swarms.

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed.

* ``agent_tasks`` - one row per task: owner, prompt, state, model, swarm flag,
  the sandbox on the runner, the lease (``owner_token``/``heartbeat_at``,
  epoch seconds) and stop flag used across processes, usage counters.
* ``agent_steps`` - every model message, tool call (with validated arguments
  and a truncated result) and notice, per agent (0 = main, n = sub-agent).
* ``agent_lanes`` - the sub-agents of a swarm task.
* ``agent_messages`` - follow-up messages from the owner.
* ``agent_model_caps`` - whether a model supports tool calling (from Ollama's
  ``/api/show``), used to offer only suitable models.
* ``agent_settings`` - the administrator's settings (one JSON row; the feature
  is off until an administrator enables it).
* The ``agents`` access capability: ``model_access_policies`` only accepts the
  scopes listed in its CHECK constraint, so the table is rebuilt with the same
  columns and a CHECK that also accepts ``agents`` (foreign keys are disabled
  during migrations, so the allow/deny lists that reference it are kept).
"""

import re

from sqlite_migrations import execute_script

_TABLES = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT '',
    prompt          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'paused', 'finished', 'failed', 'stopped',
                                         'out_of_budget', 'interrupted')),
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    model_name      TEXT NOT NULL DEFAULT '',
    swarm           INTEGER NOT NULL DEFAULT 0 CHECK(swarm IN (0, 1)),
    sandbox_id      TEXT,
    sandbox_expires_at TEXT,
    owner_token     TEXT,
    heartbeat_at    REAL,
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    runs            INTEGER NOT NULL DEFAULT 0,
    steps_used      INTEGER NOT NULL DEFAULT 0,
    tool_calls      INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    summary         TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    notice          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_user ON agent_tasks(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_sandbox ON agent_tasks(sandbox_expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_model ON agent_tasks(model_id);

CREATE TABLE IF NOT EXISTS agent_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    agent           INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL
                        CHECK(kind IN ('user', 'assistant', 'tool', 'notice', 'error', 'summary')),
    content         TEXT NOT NULL DEFAULT '',
    thinking        TEXT NOT NULL DEFAULT '',
    tool_name       TEXT,
    tool_args       TEXT,
    tool_calls      TEXT,
    tool_result     TEXT,
    tool_status     TEXT,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_steps_task ON agent_steps(task_id, id);

CREATE TABLE IF NOT EXISTS agent_lanes (
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    agent           INTEGER NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    instructions    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    steps_used      INTEGER NOT NULL DEFAULT 0,
    summary         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    PRIMARY KEY(task_id, agent)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    user_id         TEXT REFERENCES users(id) ON DELETE SET NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    consumed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(task_id, consumed_at);

CREATE TABLE IF NOT EXISTS agent_model_caps (
    model_id        INTEGER PRIMARY KEY REFERENCES ai_models(id) ON DELETE CASCADE,
    supports_tools  INTEGER NOT NULL DEFAULT 0,
    capabilities    TEXT NOT NULL DEFAULT '',
    checked_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_settings (
    id              INTEGER PRIMARY KEY CHECK(id = 1),
    config          TEXT NOT NULL DEFAULT '{}',
    updated_by      TEXT REFERENCES users(id) ON DELETE SET NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SCOPE_LIST = re.compile(r"scope\s+IN\s*\(\s*'uncensored',\s*'image_generation',\s*'custom_personality'")


def _allow_agents_scope(conn):
    """Rebuild ``model_access_policies`` so its CHECK constraint accepts the ``agents`` capability."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='model_access_policies'").fetchone()
    if row is None or "'agents'" in row[0]:
        return
    sql = row[0]
    rebuilt, count = _SCOPE_LIST.subn(lambda match: match.group(0) + ", 'agents'", sql)
    if count < 2:
        raise RuntimeError("model_access_policies has an unexpected definition; cannot add the agents capability.")
    rebuilt = re.sub(r"^CREATE TABLE\s+(\"?model_access_policies\"?)", "CREATE TABLE model_access_policies_v8",
                     rebuilt, count=1)
    columns = [info[1] for info in conn.execute('PRAGMA table_info("model_access_policies")')]
    names = ", ".join(f'"{name}"' for name in columns)
    triggers = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE "
                            "'%model_access_policies%'").fetchall()
    for name, _ in triggers:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute(rebuilt)
    conn.execute(f"INSERT INTO model_access_policies_v8 ({names}) SELECT {names} FROM model_access_policies")
    conn.execute("DROP TABLE model_access_policies")
    conn.execute("ALTER TABLE model_access_policies_v8 RENAME TO model_access_policies")
    for _, trigger_sql in triggers:
        if trigger_sql:
            conn.execute(trigger_sql)


def upgrade(conn):
    execute_script(conn, _TABLES)
    _allow_agents_scope(conn)
    # Denied by default: users need an administrator's approval (allowlist).
    conn.execute("INSERT OR IGNORE INTO model_access_policies (scope, resource_id, mode, requests_enabled) "
                 "VALUES ('agents', 0, 'deny_except_allowlist', 1)")
    conn.execute("INSERT OR IGNORE INTO agent_settings (id, config) VALUES (1, '{}')")
