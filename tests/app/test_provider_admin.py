"""External API provider administration: presets, headers, connection tests, model selection and the errors
people see in the chat."""
from __future__ import annotations

import json
import re

import pytest

from bananachat.db import catalog, external_providers as store
from bananachat.services import external_providers as service, provider_presets
from tests.app.fixtures import Browser
from tests.app.test_provider_dialects import (KEY, FakeProvider, anthropic_text, make_model, make_provider,
                                              openai_text)


@pytest.fixture
def fake():
    server = FakeProvider()
    yield server
    server.close()


@pytest.fixture
def admin_browser(app):
    browser = Browser(app)
    browser.login('admin')
    return browser


def flashes(browser):
    page = browser.get('/admin/models/providers').get_data(as_text=True)
    return page


def page_data(html):
    match = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


def form(**fields):
    base = {'name': 'Provider', 'preset': 'custom', 'protocol': 'openai', 'base_url': '', 'auth_style': 'bearer',
            'api_key': '', 'extra_headers': '', 'enabled': 'on', 'token_parameter': 'auto', 'stream_usage': '1'}
    base.update(fields)
    return {key: value for key, value in base.items() if value is not None}


# ----- presets ------------------------------------------------------------------------------------------------

def test_page_offers_every_preset_with_its_defaults(app, admin_browser):
    html = admin_browser.get('/admin/models/providers').get_data(as_text=True)
    data = page_data(html)
    for preset in provider_presets.PRESETS.values():
        assert preset.label in html
        assert data['presets'][preset.key]['base_url'] == preset.base_url
    assert data['presets']['openrouter']['headers']['X-Title']
    assert data['presets']['anthropic']['auth_style'] == 'x-api-key'
    assert data['presets']['lmstudio']['local'] is True


@pytest.mark.parametrize('preset,url,protocol,auth,private', [
    ('openrouter', 'https://openrouter.ai/api/v1', 'openai', 'bearer', 0),
    ('anthropic', 'https://api.anthropic.com/v1', 'anthropic', 'x-api-key', 0),
    ('lmstudio', 'http://localhost:1234/v1', 'openai', 'bearer', 1),
])
def test_a_blank_base_url_takes_the_presets_connection(app, admin_browser, preset, url, protocol, auth, private):
    response = admin_browser.post('/admin/models/providers', form(name=preset, preset=preset, protocol=None,
                                                                   auth_style=None, api_key=KEY))
    assert response.status_code == 302
    with app.app_context():
        row = store.list_providers()[0]
    assert (row['preset'], row['base_url'], row['protocol'], row['auth_style'], row['allow_private']) == \
        (preset, url, protocol, auth, private)
    headers = json.loads(row['extra_headers'])
    if preset == 'openrouter':
        assert headers['X-Title'] and headers['HTTP-Referer'].startswith('http')
    else:
        assert headers == {}


def test_extra_headers_are_validated_and_kept_out_of_the_secret_slot(app, admin_browser):
    for bad in ('Authorization: Bearer abc', 'x-api-key: abc', 'Bad Header: x', 'X-Ok: line\x01break', 'no colon'):
        admin_browser.post('/admin/models/providers', form(base_url='https://gateway.example/v1', extra_headers=bad))
    with app.app_context():
        assert store.list_providers() == []
    admin_browser.post('/admin/models/providers', form(base_url='https://gateway.example/v1',
                                                       extra_headers='X-Title: Banana\r\nHTTP-Referer: https://x.example'))
    with app.app_context():
        row = store.list_providers()[0]
    assert json.loads(row['extra_headers']) == {'X-Title': 'Banana', 'HTTP-Referer': 'https://x.example'}
    assert 'HTTP-Referer: https://x.example' in admin_browser.get('/admin/models/providers').get_data(as_text=True)


# ----- connection test ----------------------------------------------------------------------------------------

def test_connection_test_validates_the_key_and_probes_a_model(app, fake, admin_browser):
    provider = make_provider(app, fake)
    fake.events = openai_text('OK', usage=(12, 1))
    response = admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {'model_id': 'model-a'})
    assert response.status_code == 302
    page = flashes(admin_browser)
    assert 'The API key was accepted' in page and '1 model' in page
    assert 'model-a answered' in page and '12 input' in page
    probe = fake.posts()[-1]['body']
    assert probe['model'] == 'model-a' and probe['max_tokens'] <= 64 and probe['stream'] is True
    with app.app_context():
        assert catalog.list_models(backend='external') == [], 'a test enrolls nothing'


def test_connection_test_explains_a_rejected_key_without_echoing_it(app, fake, admin_browser):
    provider = make_provider(app, fake)
    fake.responses['/v1/models'] = (401, {}, {'error': {'message': 'Incorrect API key provided: ' + KEY}})
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {})
    page = flashes(admin_browser)
    assert 'rejected the API key' in page and KEY not in page


def test_connection_test_reports_missing_usage(app, fake, admin_browser):
    provider = make_provider(app, fake)
    fake.events = openai_text('OK', usage=None)
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {'model_id': 'model-a'})
    assert 'did not report token usage' in flashes(admin_browser)


def test_openrouter_connection_test_checks_the_key_endpoint(app, admin_browser):
    """OpenRouter's model list is public, so it cannot prove that the key works."""
    server = FakeProvider(prefix='/api/v1')
    try:
        provider = make_provider(app, server, name='OpenRouter', preset='openrouter')
        server.responses['/api/v1/key'] = (401, {}, {'error': {'message': 'No auth credentials found', 'code': 401}})
        admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {})
        page = flashes(admin_browser)
        assert 'rejected the API key' in page
        server.responses['/api/v1/key'] = (200, {}, {'data': {'label': 'sk-or-v1-abc...', 'limit_remaining': 4.5}})
        admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {})
        assert 'The API key was accepted' in flashes(admin_browser)
    finally:
        server.close()


def test_anthropic_connection_test_probes_with_the_messages_api(app, fake, admin_browser):
    provider = make_provider(app, fake, protocol='anthropic')
    fake.models = {'data': [{'id': 'claude-x', 'display_name': 'Claude X'}], 'has_more': False}
    fake.events = anthropic_text(text='OK', input_tokens=9)
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/test', {'model_id': 'claude-x'})
    assert 'claude-x answered' in flashes(admin_browser)
    assert fake.posts()[-1]['path'] == '/v1/messages' and 'thinking' not in fake.posts()[-1]['body']


# ----- model selection ----------------------------------------------------------------------------------------

def test_selected_models_are_enrolled_with_discovered_capabilities_and_published(app, admin_browser):
    server = FakeProvider(prefix='/api/v1')
    try:
        provider = make_provider(app, server, name='OpenRouter', preset='openrouter')
        server.models = {'data': [
            {'id': 'vendor/vision-chat', 'name': 'Vision Chat', 'context_length': 200_000,
             'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
             'pricing': {'prompt': '0.000001', 'completion': '0.000002'},
             'supported_parameters': ['tools', 'reasoning']},
            {'id': 'vendor/plain', 'name': 'Plain', 'architecture': {'output_modalities': ['text']}},
            {'id': 'vendor/skipped', 'name': 'Skipped'}]}
        admin_browser.post(f'/admin/models/providers/{provider["id"]}/check')
        page = admin_browser.get('/admin/models/providers').get_data(as_text=True)
        assert 'Vision Chat' in page and '$1.00' in page and '200K' in page
        response = admin_browser.post(f'/admin/models/providers/{provider["id"]}/models/bulk',
                                      {'model_ids': ['vendor/vision-chat', 'vendor/plain', 'not/listed'],
                                       'publish': 'on'})
        assert response.status_code == 302
    finally:
        server.close()
    with app.app_context():
        rows = {row['backend_model_name']: row for row in catalog.list_models(backend='external')}
    assert set(rows) == {'vendor/vision-chat', 'vendor/plain'}
    vision = rows['vendor/vision-chat']
    assert vision['supports_vision'] and vision['is_rolled_out'] and vision['context_length'] == 200_000
    assert 'tools' in json.loads(vision['capabilities'])
    assert json.loads(vision['reasoning_levels']) == ['off', 'low', 'medium', 'high']
    assert json.loads(vision['external_config'])['pricing'] == {'input': 1.0, 'output': 2.0}
    assert not rows['vendor/plain']['supports_vision'] and rows['vendor/plain']['is_rolled_out']


def test_selected_models_wait_for_review_unless_published(app, fake, admin_browser):
    provider = make_provider(app, fake)
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/check')
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/models/bulk', {'model_ids': ['model-a']})
    with app.app_context():
        row = catalog.get_by_name(service.public_id(provider['id'], 'model-a'))
    assert row and not row['is_rolled_out'] and row['enrollment'] == 'new'


def test_model_lists_saved_by_the_previous_version_still_work(app, fake, admin_browser):
    from bananachat import db
    provider = make_provider(app, fake)
    with app.app_context():
        db.execute('UPDATE external_providers SET discovered_models=? WHERE id=?',
                   (json.dumps([{'id': 'old-model', 'display': 'Old model'}]), provider['id']))
    page = admin_browser.get('/admin/models/providers')
    assert page.status_code == 200 and 'Old model' in page.get_data(as_text=True)
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/models/bulk', {'model_ids': ['old-model']})
    with app.app_context():
        row = catalog.get_by_name(service.public_id(provider['id'], 'old-model'))
    assert row and row['display_name'] == 'Old model' and not row['supports_vision']


def test_capabilities_form_saves_sampling_and_thinking_style(app, fake, admin_browser):
    provider = make_provider(app, fake, protocol='anthropic')
    admin_browser.post(f'/admin/models/providers/{provider["id"]}/models',
                       {'manual': '1', 'model_id': 'claude-old', 'reasoning': ['off', 'low'], 'sampling': 'on',
                        'thinking': 'budget'})
    with app.app_context():
        row = catalog.get_by_name(service.public_id(provider['id'], 'claude-old'))
    config = json.loads(row['external_config'])
    assert config['sampling'] is True and config['thinking'] == 'budget' and config['manual'] is True


# ----- what chat users see ------------------------------------------------------------------------------------

@pytest.mark.parametrize('language,status,expected', [
    ('it', 429, 'chat.error_provider_busy'),
    ('en', 401, 'chat.error_provider_setup'),
    ('it', 400, 'chat.error_provider_rejected'),
    ('en', 503, 'chat.error_provider_down'),
])
def test_chat_explains_provider_failures_in_the_readers_language(app, fake, make_user, language, status, expected):
    from bananachat.db import users
    from bananachat.i18n import translate
    from tests.app.test_chat import new_chat, parse_sse, send

    provider = make_provider(app, fake)
    model = make_model(app, provider)
    make_user('reader')
    with app.app_context():
        user = users.get_by_username('reader')
        users.save_preferences(user['id'], {**users.get_preferences(user['id']), 'interface_language': language})
    browser = Browser(app)
    browser.login('reader')
    fake.responses['/v1/chat/completions'] = (status, {'Retry-After': '9'},
                                              {'error': {'message': 'detail with ' + KEY}})
    session_id = new_chat(browser)
    events = parse_sse(send(browser, session_id, model=model['ollama_name']).get_data(as_text=True))
    final = events[-1]
    assert final['type'] == 'error'
    assert final['message'] == translate(language, expected, model=model['display_name'])
    assert KEY not in json.dumps(events)
