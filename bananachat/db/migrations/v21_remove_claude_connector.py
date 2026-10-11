"""Version 21: remove the Claude subscription connector.

The connector pooled consumer subscriptions, which the provider's terms do not
allow, so it was removed. Anthropic models remain available through an
external API provider with an API key.

* Catalog models it served (``backend='claude'``) are withdrawn: hidden,
  unpublished and retired, so pickers and ``/v1/models`` no longer offer them
  and the API refuses them as retired. The rows stay, so chat history keeps
  each answer's model; an administrator can remove them from the catalog.
  Personalities that preferred them use the default model again.
* The account pool tables (``claude_accounts``, ``claude_account_leases``,
  ``claude_account_usage``) and the connector's runtime state are dropped.
  No released lifecycle tool reads them. The widened ``backend`` CHECK of
  version 13 stays: rebuilding ``ai_models`` again would gain nothing.

Safe to run again.
"""

from datetime import datetime, timezone

REASON = "The Claude subscription connector was removed from BananaChat."

_RUNTIME_KEYS = ("claude_pool_quota", "claude_code_profiles")


def upgrade(conn):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    models = conn.execute("SELECT id, ollama_name FROM ai_models WHERE backend='claude' AND "
                          "(is_rolled_out=1 OR backend_available=1 OR retired_at IS NULL)").fetchall()
    for model_id, name in models:
        conn.execute("UPDATE ai_models SET is_rolled_out=0, backend_available=0, "
                     "retired_at=COALESCE(retired_at, ?), deprecated_at=COALESCE(deprecated_at, ?), "
                     "state_reason=?, state_changed_at=?, updated_at=? WHERE id=?",
                     (now, now, REASON, now, now, model_id))
        conn.execute("INSERT INTO model_lifecycle_events (model_id, model_name, event, reason, created_at) "
                     "VALUES (?, ?, 'retired', ?, ?)", (model_id, name[:300], REASON, now))
    conn.execute("UPDATE personalities SET preferred_model='' WHERE preferred_model IN "
                 "(SELECT ollama_name FROM ai_models WHERE backend='claude')")
    for table in ("claude_account_usage", "claude_account_leases", "claude_accounts"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    placeholders = ", ".join("?" for _ in _RUNTIME_KEYS)
    conn.execute(f"DELETE FROM runtime_state WHERE key IN ({placeholders}) OR key LIKE 'claude.%'", _RUNTIME_KEYS)
