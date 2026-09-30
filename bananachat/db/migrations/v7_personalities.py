"""Version 7: richer personalities.

Additive only: nothing earlier releases (or the lifecycle manager of an older
release restoring a backup) read is renamed or removed.

* ``personalities`` gains presentation fields (emoji avatar, accent colour
  key, description, greeting, conversation starters as a JSON list), an
  optional preferred model (by Ollama name), a response style (length and
  creativity keys), a ``kind`` (``user`` or ``featured``: published by an
  administrator for every user) and a revocable share token. Existing rows
  become ``user`` personalities with empty presentation fields and the
  balanced style, which is exactly how they behaved before.
* ``personality_defaults`` records the personality a user's new chats start
  with (one row per user; deleting either side removes the row).
* Featured personalities are stored under the administrator who created
  them. The ``personalities_keep_featured`` trigger hands them to another
  administrator when that account is deleted, so they are not lost with it
  (a name clash appends the personality id). A future migration that
  rebuilds the ``users`` table must recreate this trigger.
"""

from .history import _add_column

_COLUMNS = (
    "ALTER TABLE personalities ADD COLUMN kind TEXT NOT NULL DEFAULT 'user' CHECK(kind IN ('user', 'featured'))",
    "ALTER TABLE personalities ADD COLUMN avatar TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE personalities ADD COLUMN color TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE personalities ADD COLUMN description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE personalities ADD COLUMN greeting TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE personalities ADD COLUMN starters TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE personalities ADD COLUMN preferred_model TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE personalities ADD COLUMN response_length TEXT NOT NULL DEFAULT 'balanced'",
    "ALTER TABLE personalities ADD COLUMN creativity TEXT NOT NULL DEFAULT 'balanced'",
    "ALTER TABLE personalities ADD COLUMN share_token TEXT",
    "ALTER TABLE personalities ADD COLUMN shared_at TEXT",
)

_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_personalities_share_token ON personalities(share_token) "
    "WHERE share_token IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_personalities_kind ON personalities(kind, is_enabled)",
    """CREATE TABLE IF NOT EXISTS personality_defaults (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    personality_id  INTEGER NOT NULL REFERENCES personalities(id) ON DELETE CASCADE,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
)""",
    "CREATE INDEX IF NOT EXISTS idx_personality_defaults_personality ON personality_defaults(personality_id)",
    """CREATE TRIGGER IF NOT EXISTS personalities_keep_featured
BEFORE DELETE ON users
WHEN EXISTS (SELECT 1 FROM personalities WHERE user_id=OLD.id AND kind='featured')
 AND EXISTS (SELECT 1 FROM users WHERE role='admin' AND id<>OLD.id)
BEGIN
    UPDATE OR IGNORE personalities
       SET user_id=(SELECT id FROM users WHERE role='admin' AND id<>OLD.id ORDER BY suspended, created_at, id LIMIT 1)
     WHERE user_id=OLD.id AND kind='featured';
    UPDATE OR IGNORE personalities
       SET user_id=(SELECT id FROM users WHERE role='admin' AND id<>OLD.id ORDER BY suspended, created_at, id LIMIT 1),
           name=substr(name, 1, 64) || ' #' || id
     WHERE user_id=OLD.id AND kind='featured';
END""",
)


def upgrade(conn):
    for statement in _COLUMNS:
        _add_column(conn, statement)
    for statement in _STATEMENTS:
        conn.execute(statement)
