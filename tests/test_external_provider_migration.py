"""Schema 19 preserves old data, foreign keys, indexes and triggers."""
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from sqlite_migrations import apply_migrations


def test_real_version_18_upgrade_preserves_dependents_and_is_repeatable(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'v18.db')) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:18])
        conn.execute("INSERT INTO users (id, username, password) VALUES ('u', 'existing', 'test-hash')")
        conn.execute("INSERT INTO ai_models (ollama_name, display_name) VALUES ('kept-model', 'Kept')")
        model_id = conn.execute('SELECT id FROM ai_models').fetchone()[0]
        conn.execute("INSERT INTO ai_models (id,ollama_name,display_name) VALUES (100,'deleted-model','Deleted')")
        conn.execute('DELETE FROM ai_models WHERE id=100')
        conn.execute("INSERT INTO credit_ledger (user_id, model_id, tokens_in, tokens_out) VALUES ('u', ?, 12, 4)", (model_id,))
        conn.execute('CREATE TABLE migration_test_events (model_id INTEGER)')
        conn.execute('CREATE TRIGGER keep_model_trigger AFTER UPDATE ON ai_models BEGIN INSERT INTO migration_test_events VALUES (NEW.id); END')
        conn.execute('CREATE INDEX keep_model_index ON ai_models(display_name)')
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('SELECT model_id,tokens_in,tokens_out FROM credit_ledger').fetchone() == (model_id, 12, 4)
        assert conn.execute('SELECT name FROM sqlite_master WHERE name="keep_model_index"').fetchone()
        conn.execute("UPDATE ai_models SET display_name='Still kept' WHERE id=?", (model_id,))
        assert conn.execute('SELECT model_id FROM migration_test_events').fetchone()[0] == model_id
        conn.execute("INSERT INTO external_providers(name,base_url,protocol) VALUES ('External','https://api.example/v1','openai')")
        provider_id = conn.execute('SELECT id FROM external_providers').fetchone()[0]
        conn.execute("INSERT INTO ai_models (ollama_name,display_name,backend,external_provider_id) VALUES ('external-model','External','external',?)", (provider_id,))
        assert conn.execute("SELECT id FROM ai_models WHERE ollama_name='external-model'").fetchone()[0] > 100
        conn.execute('DELETE FROM external_providers WHERE id=?', (provider_id,))
        assert conn.execute('SELECT external_provider_id FROM ai_models WHERE backend="external"').fetchone()[0] is None
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_empty_model_catalog_upgrade_keeps_previously_deleted_ids(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'empty-v18.db')) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:18])
        conn.execute("INSERT INTO ai_models (id,ollama_name,display_name) VALUES (100,'deleted-model','Deleted')")
        conn.execute('DELETE FROM ai_models WHERE id=100')
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        conn.execute("INSERT INTO ai_models (ollama_name,display_name) VALUES ('next-model','Next')")
        assert conn.execute('SELECT id FROM ai_models').fetchone()[0] == 101
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
