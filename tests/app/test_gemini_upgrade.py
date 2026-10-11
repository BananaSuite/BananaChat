"""Gemini models enrolled by 1.6 survive the upgrade to 1.7 (CP-1).

1.6 enrolled Gemini's listed resource names (``models/gemini-…``) as they came; 1.7 lists the bare
model ID. The upgrade (migration v23) calls the existing models by the bare ID, so the next listings
still find them: they keep their public ID, publication and limits, and automatic enrollment adds no
second copy of them.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from bananachat import db
from bananachat.db import catalog, external_providers as store
from bananachat.db.migrations import APPLICATION_ID, MIGRATIONS
from bananachat.services import external_providers as service
from sqlite_migrations import apply_migrations
from tests.app.test_provider_dialects import FakeProvider, openai_text, run

GEMINI = 'https://generativelanguage.googleapis.com/v1beta/openai'
OLD = 'external:1:models/gemini-2.5-flash'
LIMITS = json.dumps({'enabled': True, 'window_tokens': 12345, 'weekly_tokens': None}, sort_keys=True)


def _database_from_1_6(path):
    """Schema 20 (1.6, whose migrations are unchanged) with what 1.6's enrollment stored for a Gemini model."""
    with closing(sqlite3.connect(path)) as conn:
        conn.isolation_level = None
        apply_migrations(conn, APPLICATION_ID, MIGRATIONS[:20])
        listing = [{'id': 'models/gemini-2.5-flash', 'display': 'models/gemini-2.5-flash'},
                   {'id': 'models/text-embedding-004', 'display': 'Text Embedding 004'}]
        conn.execute("INSERT INTO external_providers (id, name, base_url, protocol, auto_enroll, discovered_models) "
                     "VALUES (1, 'Gemini', ?, 'openai', 1, ?)", (GEMINI, json.dumps(listing)))
        model_id = conn.execute(
            "INSERT INTO ai_models (ollama_name, backend, provider, external_provider_id, backend_model_name, "
            "display_name, backend_available, enrollment, enrolled_at, is_rolled_out, capabilities, external_config) "
            "VALUES (?, 'external', 'external:1', 1, 'models/gemini-2.5-flash', 'Gemini Flash', 1, 'auto', "
            "datetime('now'), 1, '[\"completion\"]', '{\"manual\": false}')", (OLD,)).lastrowid
        conn.execute("INSERT INTO model_limit_policy (model_id, config, updated_at) VALUES (?, ?, datetime('now'))",
                     (model_id, LIMITS))
    return model_id


def test_gemini_models_enrolled_by_1_6_keep_their_id_publication_and_limits(make_app, tmp_path):
    path = tmp_path / 'chat.db'
    model_id = _database_from_1_6(path)
    app = make_app(BC_DATABASE_PATH=str(path))  # starting 1.7 upgrades the database
    server = FakeProvider(prefix='/v1beta/openai')
    try:
        # Gemini lists resource names; a model added there since is picked up by automatic enrollment.
        server.models = {'data': [{'id': 'models/gemini-2.5-flash'}, {'id': 'models/gemini-2.5-pro'}]}
        server.events = openai_text('Hi')
        with app.app_context():
            assert store.get(1)['preset'] == 'gemini'
            db.execute('UPDATE external_providers SET base_url=?, allow_private=1 WHERE id=1', (server.url,))
            for _ in range(service.MISSING_LISTINGS + 1):
                service.sync(1)
            rows = {row['ollama_name']: row for row in catalog.list_models(backend='external')}
            policy = db.scalar('SELECT config FROM model_limit_policy WHERE model_id=?', (model_id,))
        assert set(rows) == {OLD, 'external:1:gemini-2.5-pro'}
        old = rows[OLD]
        assert old['id'] == model_id and old['backend_model_name'] == 'gemini-2.5-flash'
        assert old['backend_available'] and old['is_rolled_out'] and not old['missing_at']
        assert old['display_name'] == 'Gemini Flash' and policy == LIMITS
        run(app, old)
        assert server.posts()[-1]['body']['model'] == 'gemini-2.5-flash'
    finally:
        server.close()


def test_the_admin_page_shows_old_gemini_models_as_enrolled(make_app, tmp_path):
    from tests.app.fixtures import Browser

    path = tmp_path / 'chat.db'
    _database_from_1_6(path)
    app = make_app(BC_DATABASE_PATH=str(path))
    browser = Browser(app)
    browser.login('admin')
    # The listing saved by 1.6 is read with the IDs 1.7 lists.
    browser.post('/admin/models/providers/1/models/bulk', {'model_ids': ['gemini-2.5-flash']})
    with app.app_context():
        assert [row['ollama_name'] for row in catalog.list_models(backend='external')] == [OLD]
        assert set(service.discovered(store.get(1))) == {'gemini-2.5-flash', 'text-embedding-004'}


def test_a_provider_switched_to_the_gemini_preset_after_the_upgrade_keeps_its_1_6_models(make_app, tmp_path):
    """Final verification of CP-1: a 1.6 Gemini provider reached through another host keeps the 'custom' preset at
    the upgrade, so v23 does not rename its models. Choosing the Gemini preset later must not withdraw them and
    publish copies with default limits."""
    path = tmp_path / 'chat.db'
    model_id = _database_from_1_6(path)
    app = make_app(BC_DATABASE_PATH=str(path))
    server = FakeProvider(prefix='/v1beta/openai')
    try:
        server.models = {'data': [{'id': 'models/gemini-2.5-flash'}]}
        with app.app_context():
            # As v23 leaves a provider behind a proxy host: preset 'custom', the 1.6 ID unchanged.
            db.execute("UPDATE external_providers SET base_url=?, allow_private=1, preset='custom' WHERE id=1",
                       (server.url,))
            db.execute("UPDATE ai_models SET backend_model_name='models/gemini-2.5-flash' WHERE id=?", (model_id,))
            service.sync(1)
            store.save(1, preset='gemini')  # the administrator picks the Gemini preset
            for _ in range(service.MISSING_LISTINGS + 1):
                service.sync(1)
            rows = {row['ollama_name']: row for row in catalog.list_models(backend='external')}
            policy = db.scalar('SELECT config FROM model_limit_policy WHERE model_id=?', (model_id,))
        assert set(rows) == {OLD}
        old = rows[OLD]
        assert old['backend_model_name'] == 'gemini-2.5-flash' and old['display_name'] == 'Gemini Flash'
        assert old['backend_available'] and old['is_rolled_out'] and not old['missing_at'] and policy == LIMITS
    finally:
        server.close()
