"""Version 33: sign-in methods, confirmations and acting administrators with BananaWiki Hosting.

* ``users.password_set``: 0 for an account that has no password of its own (made by a first sign-in
  with BananaWiki Hosting, which gave it an unusable random one). Such an account confirms sensitive
  actions with BananaWiki Hosting, and can set a password after that. Accounts made that way before
  this version are found in the audit log (``auth.hosting_link`` with ``via: new account``) unless a
  password was set for them later (``account.password_change``, ``admin.user_password``); every other
  account keeps 1.
* ``auth_sessions.hosting_confirmed_at``: when the session last confirmed it is the person with a fresh
  sign-in at BananaWiki Hosting (instead of the password, for the account page's sensitive actions).
* ``auth_sessions.acting_sub``, ``acting_name`` and ``acting_until``: a session a hosting administrator
  opened while acting as the account (the administrator's portal account id and name, and when acting
  ends: the session ends then too).
* ``site_settings.hosting_allow_acting`` (default 1): hosting administrators may sign in as users while
  the portal lets them act; ``site_settings.hosting_lookup_token``: the portal API token of the
  administrators' linking tool, encrypted with a key derived from the server's secret key (NULL: none).

Additive only, and safe to run again.
"""

from sqlite_migrations import add_columns


def upgrade(conn):
    add_columns(conn, "users", {
        "password_set": "INTEGER NOT NULL DEFAULT 1 CHECK(password_set IN (0,1))",
    })
    add_columns(conn, "auth_sessions", {
        "hosting_confirmed_at": "TEXT",
        "acting_sub": "TEXT",
        "acting_name": "TEXT",
        "acting_until": "TEXT",
    })
    add_columns(conn, "site_settings", {
        "hosting_allow_acting": "INTEGER NOT NULL DEFAULT 1 CHECK(hosting_allow_acting IN (0,1))",
        "hosting_lookup_token": "TEXT",
    })
    conn.execute("""
        UPDATE users SET password_set=0
        WHERE id IN (SELECT target FROM audit_log WHERE action='auth.hosting_link'
                     AND details LIKE '%"via": "new account"%')
          AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action='account.password_change' AND a.target=users.id)
          AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action='admin.user_password' AND a.target=users.username)
    """)
