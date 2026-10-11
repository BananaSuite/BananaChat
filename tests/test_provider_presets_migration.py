"""Schema 23 gives existing external providers a preset, a key header style and empty extra headers."""
import sqlite3
from contextlib import closing

from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from sqlite_migrations import apply_migrations


def test_existing_providers_keep_working_after_the_upgrade(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'v22.db')) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:22])
        rows = [('OpenAI', 'https://api.openai.com/v1', 'openai'),
                ('Claude', 'https://api.anthropic.com/v1', 'anthropic'),
                ('Gateway', 'https://llm.internal.example/v1', 'openai'),
                ('Claude gateway', 'https://llm.internal.example/anthropic', 'anthropic'),
                ('Grok', 'https://api.x.ai/v1', 'openai')]
        conn.executemany('INSERT INTO external_providers(name, base_url, protocol, secret_ref) VALUES (?,?,?,NULL)', rows)
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        result = {name: (preset, auth, headers) for name, preset, auth, headers in conn.execute(
            'SELECT name, preset, auth_style, extra_headers FROM external_providers')}
        assert result == {'OpenAI': ('openai', 'bearer', '{}'), 'Claude': ('anthropic', 'x-api-key', '{}'),
                          'Gateway': ('custom', 'bearer', '{}'), 'Claude gateway': ('anthropic', 'x-api-key', '{}'),
                          'Grok': ('xai', 'bearer', '{}')}
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        conn.execute("INSERT INTO external_providers(name, base_url, protocol) VALUES ('New', 'https://n.example', 'openai')")
        assert conn.execute("SELECT preset, auth_style, extra_headers FROM external_providers WHERE name='New'"
                            ).fetchone() == ('custom', 'bearer', '{}')


def test_gemini_models_are_called_by_the_bare_id_and_keep_their_public_id(tmp_path):
    """1.6 enrolled Gemini's resource names ("models/…"); 1.7 lists bare IDs (CP-1)."""
    import json

    with closing(sqlite3.connect(tmp_path / 'v22.db')) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:22])
        listing = [{'id': 'models/gemini-2.5-flash', 'display': 'models/gemini-2.5-flash'},
                   {'id': 'models/gemini-2.5-pro', 'display': 'Gemini 2.5 Pro'}, {'id': 'gemini-2.5-pro'}]
        conn.execute("INSERT INTO external_providers(id, name, base_url, protocol, discovered_models) VALUES "
                     "(1, 'Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', 'openai', ?)",
                     (json.dumps(listing),))
        conn.execute("INSERT INTO external_providers(id, name, base_url, protocol) VALUES "
                     "(2, 'Gateway', 'https://llm.internal.example/v1', 'openai')")
        models = [('external:1:models/gemini-2.5-flash', 1, 'models/gemini-2.5-flash', 'models/gemini-2.5-flash'),
                  ('external:1:models/gemini-2.5-pro', 1, 'models/gemini-2.5-pro', 'Pro'),
                  ('external:1:gemini-2.5-pro', 1, 'gemini-2.5-pro', 'Pro (bare)'),
                  ('external:2:models/house-model', 2, 'models/house-model', 'House')]
        conn.executemany("INSERT INTO ai_models (ollama_name, backend, external_provider_id, backend_model_name, "
                         "display_name) VALUES (?, 'external', ?, ?, ?)", models)
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS)
        rows = {name: (backend, display) for name, backend, display in conn.execute(
            "SELECT ollama_name, backend_model_name, display_name FROM ai_models")}
        assert rows == {
            'external:1:models/gemini-2.5-flash': ('gemini-2.5-flash', 'gemini-2.5-flash'),
            # Also enrolled by its bare ID already: both stay as they were.
            'external:1:models/gemini-2.5-pro': ('models/gemini-2.5-pro', 'Pro'),
            'external:1:gemini-2.5-pro': ('gemini-2.5-pro', 'Pro (bare)'),
            # Only Gemini lists resource names.
            'external:2:models/house-model': ('models/house-model', 'House'),
        }
        saved = json.loads(conn.execute('SELECT discovered_models FROM external_providers WHERE id=1').fetchone()[0])
        assert saved == [{'id': 'gemini-2.5-flash', 'display': 'gemini-2.5-flash'},
                         {'id': 'gemini-2.5-pro', 'display': 'Gemini 2.5 Pro'}]
