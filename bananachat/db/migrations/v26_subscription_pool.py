"""Version 26: Claude and Codex subscription account pool.

Additive only, safe to run again. The version 13 connector was removed in
version 21; this reintroduces the pool for two providers and never stores a
credential. Every amount is in tokens (prompt + completion).

* ``ai_models.backend`` also allows ``'codex'`` (ChatGPT subscription models),
  alongside ``'claude'`` which the version 13 CHECK already permits. The table
  is rebuilt with the same columns, rows and indexes, only the CHECK widened.
* New ``subscription_accounts`` table: the pooled Claude and Codex accounts
  (label, provider, status, administrator caps and the reported 5-hour and
  weekly usage) whose remaining capacity shrinks the limits of
  ``provider IN ('claude', 'codex')`` models through the capacity hook. The
  ``*_cap_pct`` columns stop routing to an account once that share of the
  provider's own 5-hour or weekly allowance is used.
* New ``subscription_account_caps`` table: an optional per-account, per-family
  ceiling (e.g. Fable), as a share of the provider's allowance for that family
  or as local token budgets, enforced on top of the account's own windows.
* New ``subscription_account_leases`` table: one exclusive, renewable lease per
  account so a subscription answers a single request at a time.
* New ``subscription_usage`` table: the durable per-request token ledger the
  rolling 5-hour and weekly windows are summed from.
"""

import re

from sqlite_migrations import execute_script


_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_accounts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    provider         TEXT NOT NULL DEFAULT 'claude' CHECK(provider IN ('claude', 'codex')),
    label            TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled', 'error')),
    priority         INTEGER NOT NULL DEFAULT 0,
    note             TEXT NOT NULL DEFAULT '',
    window_limit     INTEGER,
    window_used      INTEGER NOT NULL DEFAULT 0,
    window_resets_at TEXT,
    weekly_limit     INTEGER,
    weekly_used      INTEGER NOT NULL DEFAULT 0,
    weekly_resets_at TEXT,
    window_cap_pct   INTEGER CHECK(window_cap_pct BETWEEN 1 AND 100),
    weekly_cap_pct   INTEGER CHECK(weekly_cap_pct BETWEEN 1 AND 100),
    quota_source     TEXT NOT NULL DEFAULT 'local' CHECK(quota_source IN ('local', 'reported')),
    quota_updated_at TEXT,
    quota_window_updated_at TEXT,
    quota_weekly_updated_at TEXT,
    cooldown_until   TEXT,
    last_checked_at  TEXT,
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subscription_accounts_route
    ON subscription_accounts(provider, status, priority, id);

CREATE TABLE IF NOT EXISTS subscription_account_caps (
    account_id INTEGER NOT NULL REFERENCES subscription_accounts(id) ON DELETE CASCADE,
    family     TEXT NOT NULL,
    cap_pct    INTEGER CHECK(cap_pct BETWEEN 1 AND 100),
    window_cap INTEGER,
    weekly_cap INTEGER,
    PRIMARY KEY (account_id, family)
);

CREATE TABLE IF NOT EXISTS subscription_account_leases (
    account_id   INTEGER PRIMARY KEY REFERENCES subscription_accounts(id) ON DELETE CASCADE,
    lease_id     TEXT NOT NULL UNIQUE,
    provider     TEXT NOT NULL DEFAULT 'claude',
    heartbeat_at REAL NOT NULL,
    expires_at   REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subscription_leases_expiry
    ON subscription_account_leases(expires_at);

CREATE TABLE IF NOT EXISTS subscription_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id      TEXT NOT NULL UNIQUE,
    account_id    INTEGER NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'claude' CHECK(provider IN ('claude', 'codex')),
    family        TEXT NOT NULL DEFAULT '',
    tokens_in     INTEGER NOT NULL,
    tokens_out    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    created_epoch REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscription_usage_window
    ON subscription_usage(account_id, created_epoch);
CREATE INDEX IF NOT EXISTS idx_subscription_usage_scope
    ON subscription_usage(account_id, family, created_epoch);
"""


def _widen_backend(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_models'").fetchone()
    if row is None or row[0] is None:
        return
    sql = row[0]
    if "'codex'" in sql:
        return
    widened = re.sub(r"(CHECK\s*\(\s*backend\s+IN\s*\([^)]*)\)", r"\1, 'codex')", sql, flags=re.I)
    if widened == sql:
        raise RuntimeError("The model backend constraint could not be upgraded.")
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='ai_models'").fetchone()
    dependents = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='ai_models' AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL")]
    temporary = re.sub(r'CREATE TABLE\s+["`\[]?ai_models["`\]]?', "CREATE TABLE ai_models_v26", widened,
                       count=1, flags=re.I)
    if "ai_models_v26" not in temporary:
        raise RuntimeError("The model backend constraint could not be upgraded.")
    conn.execute(temporary)
    fields = ", ".join('"' + r[1] + '"' for r in conn.execute('PRAGMA table_info(ai_models)'))
    conn.execute(f"INSERT INTO ai_models_v26 ({fields}) SELECT {fields} FROM ai_models")
    conn.execute("DROP TABLE ai_models")
    conn.execute("ALTER TABLE ai_models_v26 RENAME TO ai_models")
    for statement in dependents:
        conn.execute(statement)
    if sequence is not None:
        changed = conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='ai_models'",
                               (sequence[0],)).rowcount
        if not changed:
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('ai_models', ?)", (sequence[0],))


def upgrade(conn):
    _widen_backend(conn)
    execute_script(conn, _SCHEMA)
