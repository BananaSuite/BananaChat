"""Version 31: easter eggs the administrator designs (BananaSuite ``banana-eggs/v1``).

``site_settings.easter_egg_configs`` holds the egg list as JSON (``bananachat.eggs``); NULL, the
default, means the built-in Konami egg, so nothing changes until an administrator edits the list.
``easter_egg_finds`` remembers which eggs each account has found, once per egg, and goes with the
account. The ``easter_konami`` switch (version 27) stays the on/off for every key-sequence egg.

Additive only, and safe to run again.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    add_columns(conn, "site_settings", {"easter_egg_configs": "TEXT"})
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS easter_egg_finds (
            user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            egg_id   TEXT NOT NULL,
            found_at TEXT NOT NULL,
            PRIMARY KEY (user_id, egg_id)
        );
    """)
