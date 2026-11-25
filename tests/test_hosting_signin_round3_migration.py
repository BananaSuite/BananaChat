"""Version 33: accounts made through BananaWiki Hosting are found as having no password of their own; the
acting and confirmation columns and the two settings start empty or at their defaults."""

import json
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from bananachat.db.migrations.v33_hosting_signin_round3 import upgrade
from sqlite_migrations import apply_migrations


def _audit(conn, action, target, details=None):
    conn.execute("INSERT INTO audit_log(actor_id, actor_name, action, target, details, ip_address, created_at) "
                 "VALUES (NULL, '', ?, ?, ?, '', '2026-01-01 00:00:00')",
                 (action, target, json.dumps(details, sort_keys=True) if details else ""))


def test_v32_accounts_made_through_hosting_have_no_password_until_one_was_set(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v32.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:32])
        for name in ("ann", "ben", "cat", "dan"):
            conn.execute("INSERT INTO users(id,username,password) VALUES (?,?,'fixture')", (name, name))
        # ann and ben were made by a first hosting sign-in; ben set a password since, cat connected an existing
        # account, dan was made by an administrator who later reset its password.
        _audit(conn, "auth.hosting_link", "ann", {"via": "new account", "portal_username": "ann"})
        _audit(conn, "auth.hosting_link", "ben", {"via": "new account", "portal_username": "ben"})
        _audit(conn, "account.password_change", "ben")
        _audit(conn, "auth.hosting_link", "cat", {"via": "existing account", "portal_username": "cat"})
        _audit(conn, "auth.hosting_link", "dan", {"via": "new account", "portal_username": "dan"})
        _audit(conn, "admin.user_password", "dan")
        conn.execute("INSERT INTO auth_sessions(id_hash,user_id,created_at,last_seen_at,expires_at) "
                     "VALUES ('h','ann','2026-01-01','2026-01-01','2099-01-01')")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert MIGRATIONS[32] is upgrade
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 33
        expected = [("ann", 0), ("ben", 1), ("cat", 1), ("dan", 1)]
        assert conn.execute("SELECT id, password_set FROM users ORDER BY id").fetchall() == expected
        assert conn.execute("SELECT hosting_confirmed_at, acting_sub, acting_name, acting_until FROM auth_sessions"
                            ).fetchall() == [(None, None, None, None)]
        assert conn.execute("SELECT hosting_allow_acting, hosting_lookup_token FROM site_settings").fetchall() \
            == [(1, None)]
        upgrade(conn)  # safe to run again
        assert conn.execute("SELECT id, password_set FROM users ORDER BY id").fetchall() == expected
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
