"""Version 9: token-based limits, configurable rate windows, per-model limits, 5-hour windows and reasoning effort tiers.

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed. Amounts move from
credits to tokens (1 credit = 1,000 tokens); the credit columns of the previous release
keep their values and new token columns hold the converted amounts.

* ``limit_policy.config`` (a table added in this release) is rewritten in format 2:
  the request rate becomes a list of rules (``N requests per second, minute,
  hour or day`` with a burst), the daily limit a 5-hour ``window`` with the
  same amount in tokens, and the weekly limit a token amount.
* ``user_limit_overrides`` gains ``rate_rules`` (JSON), ``window_tokens``,
  ``window_slow_tokens`` and ``weekly_tokens``; ``limit_tiers`` gains
  ``min_tokens_30d``; ``quota_requests`` gains token amounts, ``new_rate_rules``,
  ``model_id``, ``effort_level`` and ``effort_all_models`` (request kind ``daily`` becomes ``window``);
  ``site_settings`` gains token thresholds for automatic approval, the music
  bonus in tokens and the reasoning-effort settings; ``user_limits`` gains
  ``effort_gating_off``.
* ``limit_grants`` (a table added in this release) is rebuilt so a grant can target one model
  (``model_id``) and its scope names the 5-hour ``window``; extra credits
  become extra tokens and extra requests per second become a multiplier.
* New tables: ``model_limit_policy`` (per-model limits, weight and effort
  defaults), ``user_model_limits`` (per-account per-model limits and locks),
  ``user_effort_levels`` (unlocked reasoning effort per account and model or
  all models) and ``limit_windows`` (when each account's 5-hour and weekly
  windows opened, per pool and per model).
* The ledger gets an index for per-model usage of one account.
"""

import json

from sqlite_migrations import add_columns, execute_script

TOKENS_PER_CREDIT = 1000

_TABLES = """
CREATE TABLE IF NOT EXISTS model_limit_policy (
    model_id    INTEGER PRIMARY KEY REFERENCES ai_models(id) ON DELETE CASCADE,
    config      TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  TEXT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_model_limit_policy_updated_by ON model_limit_policy(updated_by);

CREATE TABLE IF NOT EXISTS user_model_limits (
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id       INTEGER NOT NULL REFERENCES ai_models(id) ON DELETE CASCADE,
    rate_rules     TEXT,
    window_tokens  INTEGER,
    weekly_tokens  INTEGER,
    locked         INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_user_model_limits_model ON user_model_limits(model_id);
CREATE INDEX IF NOT EXISTS idx_user_model_limits_updated_by ON user_model_limits(updated_by);

CREATE TABLE IF NOT EXISTS user_effort_levels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_id    INTEGER REFERENCES ai_models(id) ON DELETE CASCADE,
    level       TEXT NOT NULL CHECK(level IN ('off', 'low', 'medium', 'high', 'max')),
    pinned      INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
    source      TEXT NOT NULL DEFAULT 'admin' CHECK(source IN ('admin', 'request', 'automatic')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  TEXT REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_effort_levels_key ON user_effort_levels(user_id, IFNULL(model_id, 0));
CREATE INDEX IF NOT EXISTS idx_user_effort_levels_model ON user_effort_levels(model_id);
CREATE INDEX IF NOT EXISTS idx_user_effort_levels_updated_by ON user_effort_levels(updated_by);

CREATE TABLE IF NOT EXISTS limit_windows (
    user_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope              TEXT NOT NULL,
    window_started_at  TEXT,
    week_started_at    TEXT,
    PRIMARY KEY (user_id, scope)
);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_model_date ON credit_ledger(user_id, model_id, created_at);
"""

_GRANTS_REBUILD = """
CREATE TABLE limit_grants_v9 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
    pool        TEXT,
    model_id    INTEGER REFERENCES ai_models(id) ON DELETE CASCADE,
    scope       TEXT CHECK(scope IS NULL OR scope IN ('rate', 'window', 'weekly')),
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
INSERT INTO limit_grants_v9 (id, user_id, pool, model_id, scope, kind, amount, starts_at, ends_at, reason, created_by,
                             created_at, revoked_at, revoked_by)
    SELECT id, user_id, pool, NULL, CASE scope WHEN 'daily' THEN 'window' ELSE scope END, kind, amount, starts_at,
           ends_at, reason, created_by, created_at, revoked_at, revoked_by
    FROM limit_grants;
DROP TABLE limit_grants;
ALTER TABLE limit_grants_v9 RENAME TO limit_grants;
CREATE INDEX IF NOT EXISTS idx_limit_grants_user ON limit_grants(user_id, ends_at);
CREATE INDEX IF NOT EXISTS idx_limit_grants_model ON limit_grants(model_id);
CREATE INDEX IF NOT EXISTS idx_limit_grants_created_by ON limit_grants(created_by);
CREATE INDEX IF NOT EXISTS idx_limit_grants_revoked_by ON limit_grants(revoked_by);
"""

# Frozen copies of what this version converts, so the migration never changes behaviour later.
_UNITS = (("second", 1), ("minute", 60), ("hour", 3600), ("day", 86400))
_DEFAULTS = {
    "api": ({"enabled": True, "per_second": 1.0, "burst": 10, "dynamic": False},
            {"enabled": True, "credits": 30, "slow_credits": 15, "dynamic": False, "auto_tiers": False},
            {"enabled": False, "credits": 150, "dynamic": False, "auto_tiers": False}),
    "chat": ({"enabled": True, "per_second": 1.0, "burst": 5, "dynamic": False},
             {"enabled": False, "credits": 100, "slow_credits": 0, "dynamic": False, "auto_tiers": False},
             {"enabled": False, "credits": 500, "dynamic": False, "auto_tiers": False}),
    "agent": ({"enabled": True, "per_second": 1.0, "burst": 10, "dynamic": False},
              {"enabled": False, "credits": 100, "slow_credits": 0, "dynamic": False, "auto_tiers": False},
              {"enabled": False, "credits": 500, "dynamic": False, "auto_tiers": False}),
}


def rule(per_second, burst) -> dict:
    """A rate rule for a token bucket that refills at *per_second*: the smallest unit with a whole count."""
    per_second, burst = float(per_second), max(1, int(burst))
    for unit, seconds in _UNITS:
        count = per_second * seconds
        if count >= 1 and abs(count - round(count)) <= max(0.01, count * 0.005):
            return {"requests": int(round(count)), "per": unit, "burst": burst}
    return {"requests": max(1, int(round(per_second * 86400))), "per": "day", "burst": burst}


def _number(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number and abs(number) != float("inf") else fallback


def convert_policy(pool: str, stored) -> dict:
    """A policy in format 1 (credits, per-second rate, daily limit) converted to format 2."""
    rate_default, daily_default, weekly_default = _DEFAULTS.get(pool, _DEFAULTS["chat"])
    stored = stored if isinstance(stored, dict) else {}
    rate = {**rate_default, **(stored.get("rate") if isinstance(stored.get("rate"), dict) else {})}
    daily = {**daily_default, **(stored.get("daily") if isinstance(stored.get("daily"), dict) else {})}
    weekly = {**weekly_default, **(stored.get("weekly") if isinstance(stored.get("weekly"), dict) else {})}
    per_second = _number(rate.get("per_second"), rate_default["per_second"])
    burst = _number(rate.get("burst"), rate_default["burst"])
    if per_second <= 0:
        per_second = rate_default["per_second"]
    return {
        "version": 2,
        "rate": {"enabled": bool(rate.get("enabled")), "rules": [rule(per_second, burst)],
                 "dynamic": bool(rate.get("dynamic"))},
        "window": {"enabled": bool(daily.get("enabled")),
                   "tokens": int(round(_number(daily.get("credits"), daily_default["credits"]) * TOKENS_PER_CREDIT)),
                   "slow_tokens": int(round(_number(daily.get("slow_credits"), 0) * TOKENS_PER_CREDIT)),
                   "dynamic": bool(daily.get("dynamic")), "auto_tiers": bool(daily.get("auto_tiers"))},
        "weekly": {"enabled": bool(weekly.get("enabled")),
                   "tokens": int(round(_number(weekly.get("credits"), weekly_default["credits"]) * TOKENS_PER_CREDIT)),
                   "dynamic": bool(weekly.get("dynamic")), "auto_tiers": bool(weekly.get("auto_tiers"))},
    }


def _columns(conn, table) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _policies(conn) -> dict:
    policies = {}
    for pool, raw in conn.execute("SELECT pool, config FROM limit_policy").fetchall():
        try:
            stored = json.loads(raw) if raw else None
        except ValueError:
            stored = None
        if isinstance(stored, dict) and stored.get("version") == 2:
            policies[pool] = stored
            continue
        policies[pool] = convert_policy(pool, stored)
        conn.execute("UPDATE limit_policy SET config=? WHERE pool=?", (json.dumps(policies[pool], sort_keys=True), pool))
    return policies


def _rate_per_second(policy) -> float:
    rules = ((policy or {}).get("rate") or {}).get("rules") or []
    for item in rules:
        seconds = dict(_UNITS).get(item.get("per"))
        if seconds:
            return float(item["requests"]) / seconds
    return 1.0


def upgrade(conn):
    execute_script(conn, _TABLES)
    policies = _policies(conn)

    # Custom limits of one account: a rate rule, and the daily and weekly credits as tokens.
    add_columns(conn, "user_limit_overrides", {
        "rate_rules": "TEXT", "window_tokens": "INTEGER", "window_slow_tokens": "INTEGER", "weekly_tokens": "INTEGER",
    })
    for user_id, pool, per_second, burst in conn.execute(
            "SELECT user_id, pool, rate_per_second, rate_burst FROM user_limit_overrides "
            "WHERE rate_per_second IS NOT NULL AND rate_per_second > 0 AND rate_burst IS NOT NULL").fetchall():
        conn.execute("UPDATE user_limit_overrides SET rate_rules=? WHERE user_id=? AND pool=?",
                     (json.dumps([rule(per_second, burst)]), user_id, pool))
    conn.execute(f"UPDATE user_limit_overrides SET window_tokens=CAST(ROUND(daily_credits * {TOKENS_PER_CREDIT}) AS INTEGER), "
                 f"window_slow_tokens=CAST(ROUND(daily_slow_credits * {TOKENS_PER_CREDIT}) AS INTEGER), "
                 f"weekly_tokens=CAST(ROUND(weekly_credits * {TOKENS_PER_CREDIT}) AS INTEGER)")

    add_columns(conn, "limit_tiers", {"min_tokens_30d": "REAL NOT NULL DEFAULT 0"})
    conn.execute(f"UPDATE limit_tiers SET min_tokens_30d=min_credits_30d * {TOKENS_PER_CREDIT}")

    if "model_id" not in _columns(conn, "limit_grants"):
        execute_script(conn, _GRANTS_REBUILD)
        conn.execute(f"UPDATE limit_grants SET amount=amount * {TOKENS_PER_CREDIT} "
                     "WHERE kind='extra' AND scope IN ('window', 'weekly')")
        # Extra requests per second have no meaning with several rules: they become the equivalent multiplier.
        for grant_id, pool, amount in conn.execute(
                "SELECT id, pool, amount FROM limit_grants WHERE kind='extra' AND scope='rate'").fetchall():
            base = _rate_per_second(policies.get(pool)) if pool else 1.0
            factor = max(1.01, min(100.0, round((base + float(amount)) / base, 2)))
            conn.execute("UPDATE limit_grants SET kind='multiplier', amount=? WHERE id=?", (factor, grant_id))

    add_columns(conn, "quota_requests", {
        "new_tokens": "INTEGER", "new_slow_tokens": "INTEGER", "new_weekly_tokens": "INTEGER",
        "new_rate_rules": "TEXT", "model_id": "INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        "effort_level": "TEXT", "effort_all_models": "INTEGER NOT NULL DEFAULT 0",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quota_requests_model ON quota_requests(model_id)")
    conn.execute(f"UPDATE quota_requests SET new_tokens=CAST(ROUND(new_credits * {TOKENS_PER_CREDIT}) AS INTEGER), "
                 f"new_slow_tokens=CAST(ROUND(new_slow_credits * {TOKENS_PER_CREDIT}) AS INTEGER), "
                 f"new_weekly_tokens=CAST(ROUND(new_weekly_credits * {TOKENS_PER_CREDIT}) AS INTEGER)")
    conn.execute("UPDATE quota_requests SET kind='window' WHERE kind='daily'")
    for request_id, per_second, burst in conn.execute(
            "SELECT id, new_rate_per_second, new_rate_burst FROM quota_requests WHERE kind='rate' "
            "AND new_rate_per_second > 0 AND new_rate_burst IS NOT NULL").fetchall():
        conn.execute("UPDATE quota_requests SET new_rate_rules=? WHERE id=?",
                     (json.dumps([rule(per_second, burst)]), request_id))

    add_columns(conn, "site_settings", {
        "quota_auto_approve_max_tokens": "INTEGER NOT NULL DEFAULT 0",
        "quota_auto_approve_max_slow_tokens": "INTEGER NOT NULL DEFAULT 0",
        "quota_auto_approve_max_weekly_tokens": "INTEGER NOT NULL DEFAULT 0",
        "music_bonus_fixed_tokens": "INTEGER",
        "music_bonus_fixed_slow_tokens": "INTEGER",
        "effort_gating_enabled": "INTEGER NOT NULL DEFAULT 1",
        "effort_default_level": "TEXT NOT NULL DEFAULT 'medium'",
        "effort_auto_unlock": "INTEGER NOT NULL DEFAULT 1",
        "effort_auto_active_days": "INTEGER NOT NULL DEFAULT 30",
        "effort_auto_tokens": "INTEGER NOT NULL DEFAULT 2000000",
        "effort_auto_period_days": "INTEGER NOT NULL DEFAULT 60",
        "effort_auto_clean_days": "INTEGER NOT NULL DEFAULT 90",
        "effort_auto_ceiling": "TEXT NOT NULL DEFAULT 'high'",
    })
    settings = _columns(conn, "site_settings")
    conversions = [("quota_auto_approve_max_tokens", "quota_auto_approve_max_credits"),
                   ("quota_auto_approve_max_slow_tokens", "quota_auto_approve_max_slow_credits"),
                   ("quota_auto_approve_max_weekly_tokens", "quota_auto_approve_max_weekly_credits"),
                   ("music_bonus_fixed_tokens", "music_bonus_fixed_credits"),
                   ("music_bonus_fixed_slow_tokens", "music_bonus_fixed_slow")]
    for target, source in conversions:
        if source in settings:
            conn.execute(f"UPDATE site_settings SET {target}=CAST(ROUND({source} * {TOKENS_PER_CREDIT}) AS INTEGER) "
                         f"WHERE {source} IS NOT NULL")

    add_columns(conn, "user_limits", {"effort_gating_off": "INTEGER NOT NULL DEFAULT 0"})
