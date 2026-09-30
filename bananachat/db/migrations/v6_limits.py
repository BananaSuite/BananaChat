"""Version 6: the limit system (request rates, daily and weekly limits, tiers, dynamic limits, grants).

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed.

* ``limit_policy`` holds one JSON policy per credit pool (``api``, ``chat``,
  ``agent``). It is created from the previous release's settings, which stay in
  ``site_settings`` but are no longer read: ``api_rpm``/``chat_rpm`` become a
  token-bucket rate (``rpm/60`` per second, burst ``rpm/6``), the default
  daily credits the API pool's daily limit and the chat limits the chat
  pool's.
* Per-user settings (``user_limits``: tier, speed, usage resets) and custom
  limits per pool (``user_limit_overrides``). Custom API quotas of the previous release
  (``user_quota`` rows set by an administrator or an approved request) are
  copied; ``user_quota`` itself is kept for older releases.
* ``limit_tiers`` (three seeded levels), ``limit_grants`` (temporary or
  permanent increases), ``rate_buckets`` (request-rate state shared by the
  worker processes) and additive columns on ``quota_requests`` for the new
  request kinds.
* The ledger accepts the ``agent`` request type. SQLite cannot change a
  CHECK constraint in place, so ``credit_ledger`` is rebuilt with the same
  columns, rows and indexes (plus a date index for site-wide statistics).
"""

import json

from sqlite_migrations import add_columns, execute_script

_TABLES = """
CREATE TABLE IF NOT EXISTS limit_policy (
    pool        TEXT PRIMARY KEY,
    config      TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  TEXT REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS limit_tiers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    position          INTEGER NOT NULL DEFAULT 0,
    name              TEXT NOT NULL,
    multiplier        REAL NOT NULL DEFAULT 1.0,
    min_account_days  INTEGER NOT NULL DEFAULT 0,
    min_active_days   INTEGER NOT NULL DEFAULT 0,
    min_credits_30d   REAL NOT NULL DEFAULT 0,
    clean_days        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_limits (
    user_id          TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    tier_id          INTEGER REFERENCES limit_tiers(id) ON DELETE SET NULL,
    tier_locked      INTEGER NOT NULL DEFAULT 0,
    tier_changed_at  TEXT,
    dynamic_exempt   INTEGER NOT NULL DEFAULT 0,
    speed            TEXT NOT NULL DEFAULT 'normal' CHECK(speed IN ('slow', 'normal', 'fast')),
    usage_reset_at   TEXT,
    weekly_reset_at  TEXT,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by       TEXT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_user_limits_tier ON user_limits(tier_id);
CREATE INDEX IF NOT EXISTS idx_user_limits_updated_by ON user_limits(updated_by);

CREATE TABLE IF NOT EXISTS user_limit_overrides (
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pool                TEXT NOT NULL,
    rate_per_second     REAL,
    rate_burst          INTEGER,
    daily_credits       INTEGER,
    daily_slow_credits  INTEGER,
    weekly_credits      INTEGER,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by          TEXT REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, pool)
);
CREATE INDEX IF NOT EXISTS idx_user_limit_overrides_updated_by ON user_limit_overrides(updated_by);

CREATE TABLE IF NOT EXISTS limit_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
    pool        TEXT,
    scope       TEXT CHECK(scope IS NULL OR scope IN ('rate', 'daily', 'weekly')),
    kind        TEXT NOT NULL CHECK(kind IN ('unlimited', 'multiplier', 'extra')),
    amount      REAL NOT NULL DEFAULT 0,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at  TEXT,
    revoked_by  TEXT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_limit_grants_user ON limit_grants(user_id, ends_at);
CREATE INDEX IF NOT EXISTS idx_limit_grants_created_by ON limit_grants(created_by);
CREATE INDEX IF NOT EXISTS idx_limit_grants_revoked_by ON limit_grants(revoked_by);

CREATE TABLE IF NOT EXISTS rate_buckets (
    key         TEXT PRIMARY KEY,
    tokens      REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_buckets_updated ON rate_buckets(updated_at);
"""

_LEDGER_REBUILD = """
CREATE TABLE credit_ledger_v6 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id        INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    model_id        INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
    credits_used    REAL NOT NULL DEFAULT 0,
    is_slow         INTEGER NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    request_type    TEXT NOT NULL DEFAULT 'api'
                        CHECK(request_type IN ('api', 'playground', 'chat', 'chat_incognito', 'agent')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    usage_estimated INTEGER NOT NULL DEFAULT 0
);
INSERT INTO credit_ledger_v6 (id, user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out,
                              request_type, created_at, usage_estimated)
    SELECT id, user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out, request_type, created_at,
           usage_estimated
    FROM credit_ledger;
DROP TABLE credit_ledger;
ALTER TABLE credit_ledger_v6 RENAME TO credit_ledger;
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_date ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_token ON credit_ledger(token_id);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_model ON credit_ledger(model_id);
"""

# Seeded tiers: (position, name, multiplier, account days, active days in 30, credits in 30 days, clean days).
_TIERS = (
    (0, "Starter", 1.0, 0, 0, 0, 0),
    (1, "Regular", 2.0, 14, 5, 20, 30),
    (2, "Trusted", 4.0, 60, 15, 200, 90),
)

# The previous release's defaults, repeated here so this released migration never changes behaviour.
_API_RPM_DEFAULT = 60
_DAILY_DEFAULT, _SLOW_DEFAULT = 30, 15


def _rate(rpm, *, fallback):
    """``(enabled, per_second, burst)`` from a requests-per-minute value of the previous release."""
    if rpm is None:
        rpm = fallback
    rpm = int(rpm)
    if rpm <= 0:
        return None
    return {"enabled": True, "per_second": round(rpm / 60, 4), "burst": max(1, round(rpm / 6)), "dynamic": False}


def _policies(settings):
    get = settings.get
    api_rate = _rate(get("api_rpm"), fallback=_API_RPM_DEFAULT) or \
        {"enabled": False, "per_second": 1.0, "burst": 10, "dynamic": False}
    chat_rate = _rate(get("chat_rpm") or None, fallback=0) or \
        {"enabled": True, "per_second": 1.0, "burst": 5, "dynamic": False}
    api_daily = _DAILY_DEFAULT if get("default_daily_credits") is None else int(get("default_daily_credits"))
    api_slow = _SLOW_DEFAULT if get("default_slow_credits") is None else int(get("default_slow_credits"))
    chat_credits = get("chat_daily_credits")
    chat_enabled = bool(get("chat_daily_limit_enabled")) and chat_credits is not None
    return {
        "api": {"rate": api_rate,
                "daily": {"enabled": True, "credits": api_daily, "slow_credits": api_slow, "dynamic": False,
                          "auto_tiers": False},
                "weekly": {"enabled": False, "credits": api_daily * 5, "dynamic": False, "auto_tiers": False}},
        "chat": {"rate": chat_rate,
                 "daily": {"enabled": chat_enabled, "credits": 100 if chat_credits is None else int(chat_credits),
                           "slow_credits": int(get("chat_daily_slow_credits") or 0), "dynamic": False,
                           "auto_tiers": False},
                 "weekly": {"enabled": False, "credits": 500, "dynamic": False, "auto_tiers": False}},
        "agent": {"rate": {"enabled": True, "per_second": 1.0, "burst": 10, "dynamic": False},
                  "daily": {"enabled": False, "credits": 100, "slow_credits": 0, "dynamic": False,
                            "auto_tiers": False},
                  "weekly": {"enabled": False, "credits": 500, "dynamic": False, "auto_tiers": False}},
    }


def upgrade(conn):
    execute_script(conn, _TABLES)

    cursor = conn.execute("SELECT * FROM site_settings WHERE id=1")
    row = cursor.fetchone()
    settings = dict(zip([column[0] for column in cursor.description], row, strict=True)) if row else {}
    for pool, config in _policies(settings).items():
        conn.execute("INSERT OR IGNORE INTO limit_policy (pool, config) VALUES (?, ?)",
                     (pool, json.dumps(config, sort_keys=True)))

    if not conn.execute("SELECT 1 FROM limit_tiers").fetchone():
        conn.executemany("INSERT INTO limit_tiers (position, name, multiplier, min_account_days, min_active_days, "
                         "min_credits_30d, clean_days) VALUES (?,?,?,?,?,?,?)", _TIERS)

    # Custom API quotas of the previous release (set by an administrator or an approved request) become custom limits.
    # The previous release left updated_by empty for automatic approvals (and a deleted administrator empties it too),
    # so an approved request also marks the row as custom.
    conn.execute(
        "INSERT OR IGNORE INTO user_limit_overrides (user_id, pool, daily_credits, daily_slow_credits, updated_at, "
        "updated_by) SELECT q.user_id, 'api', q.daily_credits, q.daily_slow_credits, q.updated_at, q.updated_by "
        "FROM user_quota q JOIN users u ON u.id=q.user_id WHERE q.updated_by IS NOT NULL OR EXISTS "
        "(SELECT 1 FROM quota_requests r WHERE r.user_id=q.user_id AND r.status='approved')")

    add_columns(conn, "quota_requests", {
        "kind": "TEXT NOT NULL DEFAULT 'daily'",
        "pool": "TEXT NOT NULL DEFAULT 'api'",
        "new_weekly_credits": "INTEGER",
        "new_rate_per_second": "REAL",
        "new_rate_burst": "INTEGER",
        "grant_hours": "INTEGER",
        "grant_unlimited": "INTEGER NOT NULL DEFAULT 0",
        "grant_id": "INTEGER REFERENCES limit_grants(id) ON DELETE SET NULL",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quota_requests_grant ON quota_requests(grant_id)")
    add_columns(conn, "site_settings", {
        "quota_auto_approve_max_weekly_credits": "INTEGER NOT NULL DEFAULT 0",
        "limits_usage_reset_at": "TEXT",
        "limits_weekly_reset_at": "TEXT",
    })
    # When the account was last under suspension (tier promotion asks for a clean record).
    add_columns(conn, "users", {"last_suspension_at": "TEXT"})
    conn.execute("UPDATE users SET last_suspension_at=datetime('now') WHERE suspended=1")

    ledger = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='credit_ledger'").fetchone()
    if ledger and "'agent'" not in ledger[0]:
        execute_script(conn, _LEDGER_REBUILD)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_credit_ledger_date ON credit_ledger(created_at)")
