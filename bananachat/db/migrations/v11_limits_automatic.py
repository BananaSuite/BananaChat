"""Version 11: custom limits remember which amounts an automatic approval set.

Additive only, and safe to run again. ``user_limit_overrides.automatic`` lists
the columns (``window_tokens``, ``window_slow_tokens``, ``weekly_tokens``,
comma-separated) whose value came from a request approved automatically. Such
an amount is a floor: the account gets it or its tier's amount, whichever is
higher, so a small raise never holds it below later tier promotions. Amounts
an administrator set stay exact. Existing rows keep NULL (exact).
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "user_limit_overrides", {"automatic": "TEXT"})
