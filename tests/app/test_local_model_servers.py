"""OpenAI-compatible model servers on the operator's own hardware (LM Studio, llama.cpp, vLLM) as local backends."""
from __future__ import annotations

import pytest

from bananachat.db import catalog, external_providers as store
from bananachat.services import external_providers as service, limits
from tests.app.fixtures import Browser
from tests.app.test_provider_dialects import KEY, FakeProvider, make_provider


@pytest.fixture
def fake():
    server = FakeProvider()
    yield server
    server.close()


LM_STUDIO_V1 = {'models': [
    {'type': 'llm', 'key': 'qwen3-8b', 'display_name': 'Qwen3 8B', 'max_context_length': 40960,
     'loaded_instances': [{'id': 'qwen3-8b', 'config': {'context_length': 8192}}],
     'capabilities': {'vision': False, 'trained_for_tool_use': True,
                      'reasoning': {'allowed_options': ['off', 'low', 'medium', 'high'], 'default': 'medium'}}},
    {'type': 'llm', 'key': 'gemma-3-12b', 'display_name': 'Gemma 3 12B', 'max_context_length': 131072,
     'loaded_instances': [], 'capabilities': {'vision': True, 'trained_for_tool_use': False}},
    {'type': 'embedding', 'key': 'text-embedding-nomic-embed-text-v1.5', 'display_name': 'Nomic Embed',
     'max_context_length': 2048, 'loaded_instances': []},
]}


def _listing(app, provider_id):
    with app.app_context():
        return service.discovered(store.get(provider_id))


def test_lm_studio_listings_take_context_vision_and_tools_from_its_own_api(app, fake):
    fake.models = {'data': [{'id': model['key'], 'object': 'model'} for model in LM_STUDIO_V1['models']]}
    fake.responses['/api/v1/models'] = (200, {}, LM_STUDIO_V1)
    provider = make_provider(app, fake, preset='lmstudio', local_backend=1)
    with app.app_context():
        service.sync(provider['id'])
    listing = _listing(app, provider['id'])
    assert listing['qwen3-8b']['context'] == 8192  # the loaded instance's context, not the model's maximum
    assert listing['qwen3-8b']['tools'] is True and listing['qwen3-8b']['reasoning'] == ['off', 'low', 'medium', 'high']
    assert listing['gemma-3-12b']['vision'] is True and listing['gemma-3-12b']['context'] == 131072
    assert listing['gemma-3-12b']['display'] == 'Gemma 3 12B'
    assert listing['text-embedding-nomic-embed-text-v1.5']['chat'] is False


def test_older_lm_studio_versions_answer_on_api_v0(app, fake):
    fake.models = {'data': [{'id': 'qwen2-vl-7b-instruct'}, {'id': 'text-embedding-nomic-embed-text-v1.5'}]}
    fake.responses['/api/v1/models'] = (404, {}, {'error': 'Unexpected endpoint'})
    fake.responses['/api/v0/models'] = (200, {}, {'data': [
        {'id': 'qwen2-vl-7b-instruct', 'type': 'vlm', 'state': 'not-loaded', 'max_context_length': 32768},
        {'id': 'text-embedding-nomic-embed-text-v1.5', 'type': 'embeddings', 'max_context_length': 2048}]})
    provider = make_provider(app, fake, preset='lmstudio', local_backend=1)
    with app.app_context():
        service.sync(provider['id'])
    listing = _listing(app, provider['id'])
    assert listing['qwen2-vl-7b-instruct']['vision'] is True and listing['qwen2-vl-7b-instruct']['context'] == 32768
    assert listing['text-embedding-nomic-embed-text-v1.5']['chat'] is False


def test_llama_cpp_reports_the_context_it_was_started_with(app, fake):
    fake.models = {'data': [{'id': 'model.gguf', 'meta': {'n_ctx_train': 131072}}]}
    fake.responses['/props'] = (200, {}, {'default_generation_settings': {'n_ctx': 16384},
                                          'modalities': {'vision': False}})
    provider = make_provider(app, fake, preset='llamacpp', local_backend=1)
    with app.app_context():
        service.sync(provider['id'])
    entry = _listing(app, provider['id'])['model.gguf']
    assert entry['context'] == 16384 and entry['vision'] is False and entry['chat'] is True


def test_a_server_without_the_native_api_keeps_its_plain_listing(app, fake):
    fake.models = {'data': [{'id': 'qwen3-8b'}]}
    provider = make_provider(app, fake, preset='lmstudio', local_backend=1)  # /api/* answers with a stream here
    with app.app_context():
        assert [entry['id'] for entry in service.sync(provider['id'])] == ['qwen3-8b']


def test_own_hardware_models_are_local_models(app, fake):
    fake.models = {'data': [{'id': 'qwen3-8b'}]}
    local = make_provider(app, fake, name='LM Studio', preset='lmstudio', local_backend=1, auto_enroll=1)
    cloud = make_provider(app, fake, name='Gateway', preset='litellm', auto_enroll=1)
    with app.app_context():
        service.sync(local['id'])
        service.sync(cloud['id'])
        local_model = store.model(local['id'], 'qwen3-8b')
        cloud_model = store.model(cloud['id'], 'qwen3-8b')
        assert limits.is_local(local_model) and not limits.is_local(cloud_model)
        # Every chat model a local server lists is published by automatic enrolment, whatever its name.
        assert local_model['is_rolled_out'] == 1
        assert limits.is_local({'provider': 'ollama'}) and not limits.is_local({'provider': 'external:x'})


def test_a_local_server_that_is_off_hides_its_models_until_it_answers_again(app, fake):
    from bananachat.services.upstream import UpstreamError

    fake.models = {'data': [{'id': 'qwen3-8b'}]}
    provider = make_provider(app, fake, preset='lmstudio', local_backend=1, auto_enroll=1)
    with app.app_context():
        service.sync(provider['id'])
        assert store.model(provider['id'], 'qwen3-8b')['backend_available'] == 1
    fake.close()
    with app.app_context():
        for attempt in range(service.DOWN_CHECKS):
            with pytest.raises(UpstreamError):
                service.sync(provider['id'])
            row = store.model(provider['id'], 'qwen3-8b')
            assert row['backend_available'] == (0 if attempt == service.DOWN_CHECKS - 1 else 1)
        assert 'not answering' in row['missing_reason'] and row['missing_at'] is None
    back = FakeProvider()
    try:
        back.models = {'data': [{'id': 'qwen3-8b'}]}
        with app.app_context():
            store.save(provider['id'], base_url=back.url)
            service.forget_local_providers()
            service.sync(provider['id'])
            row = store.model(provider['id'], 'qwen3-8b')
            assert row['backend_available'] == 1 and row['missing_reason'] is None
    finally:
        back.close()


def test_a_cloud_provider_that_fails_keeps_its_models(app, fake):
    from bananachat.services.upstream import UpstreamError

    fake.models = {'data': [{'id': 'gpt-test'}]}
    provider = make_provider(app, fake, preset='custom', auto_enroll=1)
    with app.app_context():
        service.sync(provider['id'])
    fake.close()
    with app.app_context():
        for _ in range(service.DOWN_CHECKS + 1):
            with pytest.raises(UpstreamError):
                service.sync(provider['id'])
        assert store.model(provider['id'], 'gpt-test')['backend_available'] == 1


def test_the_provider_form_marks_own_hardware(app):
    admin = Browser(app)
    admin.login('admin')
    form = {'name': 'Studio', 'preset': 'lmstudio', 'protocol': 'openai', 'base_url': 'http://localhost:1234/v1',
            'auth_style': 'bearer', 'api_key': '', 'extra_headers': '', 'enabled': 'on', 'token_parameter': 'auto',
            'stream_usage': '1', 'allow_private': 'on', 'local_backend': 'on'}
    assert admin.post('/admin/models/providers', form).status_code == 302
    form.update(name='Proxy', preset='litellm', base_url='http://localhost:4000/v1')
    form.pop('local_backend')
    assert admin.post('/admin/models/providers', form).status_code == 302
    blank = dict(form, name='Blank', preset='vllm', base_url='', api_key=KEY)
    assert admin.post('/admin/models/providers', blank).status_code == 302
    with app.app_context():
        rows = {row['name']: row for row in store.list_providers()}
    assert (rows['Studio']['local_backend'], rows['Proxy']['local_backend'], rows['Blank']['local_backend']) == (1, 0, 1)
    page = admin.get('/admin/models/providers').get_data(as_text=True)
    assert 'Runs on our own hardware' in page and '"own_hardware": true' in page
    with app.app_context():
        assert catalog.list_models(backend='external') == []
