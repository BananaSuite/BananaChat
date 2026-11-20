"""Version 30: sign in with BananaWiki Hosting.

``hosting_links`` connects an account to one BananaWiki Hosting account, by
the portal's stable account id (``sub``), never by user name or email: each
account has at most one link and each portal account links at most one
account. ``portal_username`` is the portal's name when the link was made (for
display only). ``site_settings.signin_mode`` is ``local`` (password sign-in
only, as before), ``both`` or ``hosting``; it defaults to ``local``, so nothing
changes until an administrator chooses otherwise, and it only applies while
the portal is configured (``BC_HOSTING_OAUTH_*``).

Additive only, and safe to run again.
"""

from sqlite_migrations import add_columns, execute_script


def upgrade(conn):
    execute_script(conn, """
        CREATE TABLE IF NOT EXISTS hosting_links (
            user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL UNIQUE,
            portal_username TEXT NOT NULL DEFAULT '',
            linked_at       TEXT NOT NULL
        );
    """)
    add_columns(conn, "site_settings", {
        "signin_mode": "TEXT NOT NULL DEFAULT 'local' CHECK(signin_mode IN ('local','both','hosting'))",
    })
