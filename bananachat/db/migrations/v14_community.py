"""Version 14: community consent for quota requests, quota fallbacks, Claude account cool-down.

Additive only, and safe to run again.

* ``quota_requests`` is rebuilt with the same columns, rows and indexes, only
  its CHECKs widened: ``status`` also allows ``'cancelled'`` (the requester
  withdrew it) and ``resolution_source`` also allows ``'community'`` (approved
  by other people's consent). New columns: ``community`` (the requester asked
  the community), ``community_until`` (voting closes), ``community_closed_at``
  (voting closed without consent; the request keeps waiting for an
  administrator), ``cancelled_at`` and ``boost_ends_at`` (when a
  community-approved increase ends). A request for one model's limits
  (5-hour, weekly, rate, temporary) names it in ``model_id`` like effort
  requests already did.
* New ``quota_request_votes``: one support or objection per person and request;
  a support may renounce tokens of the supporter's own limit (``tokens`` in
  ``scope`` of ``pool`` or ``model_id``), in force from ``starts_at`` to
  ``ends_at`` once the request is approved by the community.
* ``site_settings``: the community options (on by default) and the quota
  fallbacks between local and cloud models (on by default).
* ``claude_accounts.cooldown_until``: a subscription account the Claude site
  reported as out of quota is skipped until then.
"""

import re

from sqlite_migrations import add_columns, execute_script

_VOTES = """
CREATE TABLE IF NOT EXISTS quota_request_votes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL REFERENCES quota_requests(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stance      TEXT NOT NULL CHECK(stance IN ('support', 'object')),
    tokens      INTEGER NOT NULL DEFAULT 0 CHECK(tokens >= 0),
    pool        TEXT,
    model_id    INTEGER REFERENCES ai_models(id) ON DELETE CASCADE,
    scope       TEXT CHECK(scope IS NULL OR scope IN ('window', 'weekly')),
    starts_at   TEXT,
    ends_at     TEXT,
    released_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(request_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_quota_votes_user ON quota_request_votes(user_id, ends_at);
CREATE INDEX IF NOT EXISTS idx_quota_votes_model ON quota_request_votes(model_id);
"""

_SETTINGS = {
    "community_quota_enabled": "INTEGER NOT NULL DEFAULT 1",
    "community_kinds": "TEXT NOT NULL DEFAULT 'window,weekly,rate,temporary,effort'",
    "community_min_supporters": "INTEGER NOT NULL DEFAULT 3",
    "community_approval_percent": "INTEGER NOT NULL DEFAULT 66",
    "community_coverage_percent": "INTEGER NOT NULL DEFAULT 100",
    "community_hours": "INTEGER NOT NULL DEFAULT 72",
    "community_boost_hours": "INTEGER NOT NULL DEFAULT 168",
    "community_min_account_days": "INTEGER NOT NULL DEFAULT 7",
    "community_max_pledge_percent": "INTEGER NOT NULL DEFAULT 50",
    "quota_fallback_to_local": "INTEGER NOT NULL DEFAULT 1",
    "quota_fallback_to_cloud": "INTEGER NOT NULL DEFAULT 1",
}


def _widen(sql: str) -> str:
    sql = re.sub(r"(status\s+IN\s*\(\s*'pending'\s*,\s*'approved'\s*,\s*'denied')\s*\)", r"\1, 'cancelled')", sql,
                 flags=re.IGNORECASE)
    return re.sub(r"(resolution_source\s+IN\s*\(\s*'manual'\s*,\s*'automatic')\s*\)", r"\1, 'community')", sql,
                  flags=re.IGNORECASE)


def _rebuild_requests(conn) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='quota_requests'").fetchone()
    if row is None or row[0] is None:
        return
    sql = row[0]
    widened = _widen(sql)
    if widened == sql:
        return  # already widened (or no CHECK to widen)
    dependents = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='quota_requests' AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL").fetchall()]
    new_sql = re.sub(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?\"?quota_requests\"?", "CREATE TABLE quota_requests_v14",
                     widened, count=1, flags=re.IGNORECASE)
    conn.execute(new_sql)
    columns = ", ".join(f'"{r[1]}"' for r in conn.execute('PRAGMA table_info("quota_requests")').fetchall())
    conn.execute(f"INSERT INTO quota_requests_v14 ({columns}) SELECT {columns} FROM quota_requests")
    conn.execute("DROP TABLE quota_requests")
    conn.execute("ALTER TABLE quota_requests_v14 RENAME TO quota_requests")
    for statement in dependents:
        conn.execute(statement)


def upgrade(conn):
    _rebuild_requests(conn)
    add_columns(conn, "quota_requests", {
        "community": "INTEGER NOT NULL DEFAULT 0",
        "community_until": "TEXT",
        "community_closed_at": "TEXT",
        "cancelled_at": "TEXT",
        "boost_ends_at": "TEXT",
    })
    execute_script(conn, _VOTES)
    add_columns(conn, "site_settings", _SETTINGS)
    add_columns(conn, "claude_accounts", {"cooldown_until": "TEXT"})
