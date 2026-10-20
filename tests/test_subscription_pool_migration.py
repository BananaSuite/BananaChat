"""Schema 26 adds the subscription pool, widens the backend and keeps old data."""
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from sqlite_migrations import apply_migrations


def test_version_26_widens_backend_and_creates_pool_tables(tmp_path):
    with closing(sqlite3.connect(tmp_path / "v26.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:25])
        conn.execute("INSERT INTO ai_models (ollama_name, display_name) VALUES ('kept-model', 'Kept')")
        model_id = conn.execute("SELECT id FROM ai_models").fetchone()[0]
        conn.execute("CREATE TABLE migration_test_events (model_id INTEGER)")
        conn.execute("CREATE TRIGGER keep_model_trigger AFTER UPDATE ON ai_models "
                     "BEGIN INSERT INTO migration_test_events VALUES (NEW.id); END")
        conn.execute("CREATE INDEX keep_model_index ON ai_models(display_name)")

        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='keep_model_index'").fetchone()
        conn.execute("UPDATE ai_models SET display_name='Still kept' WHERE id=?", (model_id,))
        assert conn.execute("SELECT model_id FROM migration_test_events").fetchone()[0] == model_id

        # Both subscription backends are now accepted; an unknown one is still rejected.
        conn.execute("INSERT INTO ai_models (ollama_name, display_name, backend) VALUES ('c', 'Claude', 'claude')")
        conn.execute("INSERT INTO ai_models (ollama_name, display_name, backend) VALUES ('x', 'Codex', 'codex')")
        try:
            conn.execute("INSERT INTO ai_models (ollama_name, display_name, backend) VALUES ('n', 'No', 'nope')")
            raise AssertionError("an unknown backend should be rejected")
        except sqlite3.IntegrityError:
            pass

        account = conn.execute("INSERT INTO subscription_accounts (provider, label) VALUES ('claude', 'claude-1')")
        account_id = account.lastrowid
        conn.execute("INSERT INTO subscription_account_leases (account_id, lease_id, heartbeat_at, expires_at) "
                     "VALUES (?, 'lease-a', 0, 9999999999)", (account_id,))
        try:
            conn.execute("INSERT INTO subscription_account_leases (account_id, lease_id, heartbeat_at, expires_at) "
                         "VALUES (?, 'lease-b', 0, 9999999999)", (account_id,))
            raise AssertionError("an account may hold only one lease")
        except sqlite3.IntegrityError:
            pass
        conn.execute("INSERT INTO subscription_account_caps (account_id, family, weekly_cap) VALUES (?, 'fable', 10)",
                     (account_id,))
        conn.execute("INSERT INTO subscription_usage (lease_id, account_id, provider, family, tokens_in, tokens_out, "
                     "created_epoch) VALUES ('lease-a', ?, 'claude', 'fable', 30, 70, 1000.0)", (account_id,))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_version_26_keeps_the_model_id_high_water_mark(tmp_path):
    with closing(sqlite3.connect(tmp_path / "empty-v26.db")) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:25])
        conn.execute("INSERT INTO ai_models (id, ollama_name, display_name) VALUES (100, 'gone', 'Gone')")
        conn.execute("DELETE FROM ai_models WHERE id=100")
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        conn.execute("INSERT INTO ai_models (ollama_name, display_name) VALUES ('next', 'Next')")
        assert conn.execute("SELECT id FROM ai_models").fetchone()[0] == 101
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
