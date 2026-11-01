"""Version 28: device cookies belong to one two-step set-up and one sign-out.

``users.device_epoch`` is a random value signed into every device cookie of
the account (``security.set_device_cookie``). It is cleared when two-step
verification is turned on, off or reset and when the account is signed out
everywhere (``users.revoke_sessions``), so browsers that signed in before no
longer have budgets of their own; the next full sign-in starts a new one.
Existing device cookies stop counting once; their browsers get a new one at
their next sign-in.

Additive only, and safe to run again.
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "users", {"device_epoch": "TEXT"})
