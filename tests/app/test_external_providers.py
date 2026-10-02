"""External-provider integration through real HTTP, public API and private key files."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from bananachat import db
from bananachat.db import catalog, external_providers as store, limits as limits_db
from bananachat.services import external_providers as service, external_api as api, provider_secrets
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError
from tests.app.fixtures import Browser

KEY = 'synthetic-provider-test-key'


def openai_events():
    return [
        {'choices': [{'delta': {'role': 'assistant', 'content': 'Hello'}, 'finish_reason': None}]},
        {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14}},
        None,
    ]


def anthropic_events():
    return [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'output_tokens': 0,
         'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 2}}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'Hello'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 4}},
        {'type': 'message_stop'},
    ]


class FakeAPI:
    def __init__(self):
        owner = self
        self.requests = []
        self.models = ['gpt-test', 'text-embedding-test']
        self.events = openai_events()
        self.error = None
        self.delay = 0
        self.location = None
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def answer(self):
                size = int(self.headers.get('Content-Length', '0'))
                data = self.rfile.read(size)
                owner.requests.append({'path': self.path, 'method': self.command, 'headers': dict(self.headers),
                                       'body': json.loads(data) if data else None})
                if owner.error:
                    payload = json.dumps({'error': {'message': KEY, 'code': 'model_not_found' if owner.error == 404 else 'bad'}}).encode()
                    self.send_response(owner.error)
                    if owner.location:
                        self.send_header('Location', owner.location)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if urlsplit(self.path).path == '/v1/models':
                    payload = json.dumps({'data': [{'id': name} for name in owner.models]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    for event in owner.events:
                        value = '[DONE]' if event is None else json.dumps(event)
                        self.wfile.write(('data: ' + value + '\n\n').encode())
                        self.wfile.flush()
                        if owner.delay:
                            time.sleep(owner.delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
            do_GET = answer
            do_POST = answer
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@pytest.fixture
def external_api():
    fake = FakeAPI()
    yield fake
    fake.close()


def provider(app, external_api, *, protocol='openai', name='External', auto=False):
    with app.app_context():
        provider_id = store.save(name=name, base_url=external_api.url, protocol=protocol,
                                 secret_ref=provider_secrets.save(KEY), enabled=1, allow_private=1, auto_enroll=int(auto))
        return store.get(provider_id)


def enrolled(app, row, *, model='gpt-test', **capabilities):
    with app.app_context():
        result = service.enroll(row, model, manual=True, **capabilities)
        catalog.set_rollout(result['id'], True)
        return catalog.get(result['id'])


def test_admin_provider_keys_are_private_and_never_rendered(app, external_api):
    browser = Browser(app)
    browser.login('admin')
    response = browser.post('/admin/models/providers', {'name': 'Grok', 'protocol': 'openai',
        'base_url': external_api.url, 'api_key': KEY, 'enabled': 'on', 'allow_private': 'on'})
    assert response.status_code == 302
    with app.app_context():
        row = store.list_providers()[0]
        assert KEY not in str(dict(row))
        path = app.config['BC'].instance_dir / '.provider-keys' / row['secret_ref']
        assert path.read_text() == KEY and path.stat().st_mode & 0o777 == 0o600
        assert provider_secrets.load(row['secret_ref']) == KEY
    page = browser.get('/admin/models/providers')
    assert page.status_code == 200 and KEY.encode() not in page.data
    browser.post(f'/admin/models/providers/{row["id"]}/check')
    with app.app_context():
        assert len(json.loads(store.get(row['id'])['discovered_models'])) == 2
    browser.post(f'/admin/models/providers/{row["id"]}/models', {'model_id': 'gpt-test', 'reasoning': ['low', 'medium'], 'vision': 'on'})
    with app.app_context():
        model = catalog.get_by_name(service.public_id(row['id'], 'gpt-test'))
        assert model['supports_vision'] and json.loads(model['reasoning_levels']) == ['low', 'medium']
        assert limits_db.get_model_policy(model)['counts_toward_pool'] is True
        assert not model['is_rolled_out']
    assert browser.get('/admin/models/providers').status_code == 200
    assert external_api.requests[0]['headers']['Authorization'] == 'Bearer ' + KEY


def test_duplicate_save_rolls_back_key_and_non_admin_cannot_manage(app, external_api, make_user):
    row = provider(app, external_api)
    browser = Browser(app)
    browser.login('admin')
    with app.app_context():
        root = app.config['BC'].instance_dir / '.provider-keys'
        before = set(root.iterdir())
    browser.post('/admin/models/providers', {'name': row['name'], 'protocol': 'openai', 'base_url': external_api.url,
        'allow_private': 'on', 'api_key': 'another-synthetic-key'})
    assert set(root.iterdir()) == before
    user = make_user('ordinary')
    browser.logout()
    browser.login(user['username'])
    assert browser.get('/admin/models/providers').status_code == 403
    assert browser.post(f'/admin/models/providers/{row["id"]}/check').status_code == 403


def test_namespace_avoids_duplicate_upstream_names_and_updates_keep_history(app, external_api):
    first = provider(app, external_api, name='One')
    second = provider(app, external_api, name='Two')
    a, b = enrolled(app, first), enrolled(app, second)
    assert a['ollama_name'] != b['ollama_name']
    with app.app_context():
        assert a['backend_model_name'] == b['backend_model_name']
        store.save(first['id'], enabled=0)
        assert not catalog.get(a['id'])['backend_available']
        assert catalog.get(b['id'])['backend_available']
    browser = Browser(app)
    browser.login('admin')
    browser.post(f'/admin/models/providers/{first["id"]}/remove')
    with app.app_context():
        assert store.get(first['id']) is None
        retired = catalog.get(a['id'])
        assert retired['retired_at'] and retired['external_provider_id'] is None
        assert catalog.get(b['id'])['backend_available']
        assert db.query('PRAGMA foreign_key_check') == []


def test_stream_uses_actual_usage_and_reasoning_without_local_backend(app, external_api):
    row = provider(app, external_api)
    model = enrolled(app, row, reasoning=['low', 'medium', 'extra'])
    with app.app_context():
        chunks = list(service.stream(model, [{'role': 'user', 'content': 'Hi'}], effort='extra'))
    assert chunks[0].content == 'Hello'
    assert chunks[-1].done and chunks[-1].prompt_tokens == 10 and chunks[-1].completion_tokens == 4
    request = external_api.requests[-1]
    assert request['path'] == '/v1/chat/completions'
    assert request['body']['model'] == 'gpt-test' and request['body']['reasoning_effort'] == 'xhigh'


def test_anthropic_native_system_effort_and_cache_usage(app, external_api):
    row = provider(app, external_api, protocol='anthropic')
    external_api.events = anthropic_events()
    model = enrolled(app, row, model='claude-test', reasoning=['low', 'medium', 'high'])
    with app.app_context():
        chunks = list(service.stream(model, [{'role': 'system', 'content': 'Be concise'},
                                            {'role': 'user', 'content': 'Hi'}], effort='high'))
    request = external_api.requests[-1]
    assert request['path'] == '/v1/messages'
    assert request['headers']['x-api-key'] == KEY and 'Authorization' not in request['headers']
    assert request['body']['system'] == 'Be concise' and request['body']['thinking'] == {'type': 'adaptive'}
    assert chunks[-1].prompt_tokens == 15 and chunks[-1].completion_tokens == 4


@pytest.mark.parametrize('events', [
    openai_events()[:-1],
    [{'choices': [], 'usage': {'prompt_tokens': -1, 'completion_tokens': 4}}, None],
    [{'choices': [{'delta': {'content': 'Hi'}, 'finish_reason': None}]}, None],
    [{'error': {'message': KEY}}],
])
def test_failed_streams_never_emit_terminal_success_or_leak_key(app, external_api, events):
    row = provider(app, external_api)
    model = enrolled(app, row)
    external_api.events = events
    observed = []
    with app.app_context(), pytest.raises(UpstreamError) as caught:
        for chunk in service.stream(model, [{'role': 'user', 'content': 'Hi'}]):
            observed.append(chunk)
    assert KEY not in str(caught.value)
    assert not any(chunk.done for chunk in observed)


@pytest.mark.parametrize('status', [401, 429, 500])
def test_discovery_failures_preserve_catalog_and_redact_provider_bodies(app, external_api, status):
    row = provider(app, external_api)
    model = enrolled(app, row)
    external_api.error = status
    with app.app_context(), pytest.raises(UpstreamError) as caught:
        service.sync(row['id'])
    assert KEY not in str(caught.value)
    with app.app_context():
        assert catalog.get(model['id'])['backend_available']
        assert KEY not in store.get(row['id'])['last_error']


def test_verified_missing_models_hide_and_reappear_without_resetting_limits(app, external_api):
    row = provider(app, external_api)
    with app.app_context():
        service.sync(row['id'])
        model = service.enroll(store.get(row['id']), 'gpt-test')
        policy = limits_db.get_model_policy(model)
        external_api.models = ['other-model']
        service.sync(row['id'])
        assert not catalog.get(model['id'])['backend_available']
        external_api.models.append('gpt-test')
        service.sync(row['id'])
        assert catalog.get(model['id'])['backend_available']
        assert limits_db.get_model_policy(catalog.get(model['id'])) == policy


def test_auto_enrollment_keeps_non_chat_models_unpublished(app, external_api):
    row = provider(app, external_api, auto=True)
    with app.app_context():
        service.sync(row['id'])
        chat = catalog.get_by_name(service.public_id(row['id'], 'gpt-test'))
        embedding = catalog.get_by_name(service.public_id(row['id'], 'text-embedding-test'))
        assert chat['is_rolled_out'] and chat['enrollment'] == 'auto'
        assert not embedding['is_rolled_out']
        assert limits_db.get_model_policy(chat)['counts_toward_pool'] is True


@pytest.mark.parametrize('address,private', [('127.0.0.1', False), ('169.254.169.254', True), ('::1', False),
                                          ('fe80::1', True), ('0.0.0.0', True), ('224.0.0.1', True)])
def test_restricted_dns_addresses_never_connect(monkeypatch, address, private):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(UpstreamError):
        api._addresses('provider.example', 443, private)


def test_dns_connection_is_pinned_after_validation(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    connection, parts = api._connection_factory({'allow_private': 0})('https://provider.example/v1', 10)
    connected = []
    class Socket:
        def settimeout(self, _):
            pass
        def connect(self, address):
            connected.append(address)
    monkeypatch.setattr(socket, 'socket', lambda *a: Socket())
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('DNS must not be resolved again'))
    connection._create_connection(('provider.example', 443), 10)
    assert connected == [('8.8.8.8', 443)] and connection.host == 'provider.example'
    assert parts.path == '/v1'


def test_redirect_does_not_forward_credentials(app, external_api):
    row = provider(app, external_api)
    external_api.error = 302
    external_api.location = 'https://untrusted.example/steal'
    with app.app_context(), pytest.raises(UpstreamError):
        service.discover(row)
    assert len(external_api.requests) == 1


def test_cancel_closes_http_stream(app, external_api):
    row = provider(app, external_api)
    model = enrolled(app, row)
    external_api.delay = 1
    cancel = CancelToken()
    with app.app_context():
        stream = service.stream(model, [{'role': 'user', 'content': 'Hi'}], cancel=cancel)
        assert next(stream).content == 'Hello'
        cancel.cancel()
        with pytest.raises(Cancelled):
            next(stream)
        stream.close()


def test_public_api_routes_external_and_local_models_and_charges_owner(app, external_api, make_user):
    from tests.app.test_api import chat, ledger, roll_out, token_for, call
    row = provider(app, external_api)
    model = enrolled(app, row)
    user = make_user('customer')
    roll_out(app)
    first, second = token_for(app, user, 'first'), token_for(app, user, 'second')
    response = chat(app, first, model=model['ollama_name'])
    assert response.status_code == 200, response.data
    assert response.json['choices'][0]['message']['content'] == 'Hello'
    assert response.json['usage']['total_tokens'] == 14
    assert ledger(app, user)[0]['model_id'] == model['id']
    assert ledger(app, user)[0]['tokens_in'] == 10
    models = call(app, 'GET', '/v1/models', second).json['data']
    assert any(m['id'] == model['ollama_name'] for m in models)
    assert any(m['id'] == 'llama3.2:3b' for m in models)
    with app.app_context():
        policy = limits_db.get_policy('api')
        policy['rate']['enabled'] = False
        policy['window'].update(enabled=True, tokens=14)
        limits_db.set_policy('api', policy, None)
    refused = chat(app, second, model=model['ollama_name'])
    assert refused.status_code == 429
    assert len([r for r in external_api.requests if r['method'] == 'POST']) == 1


def test_external_api_remains_available_during_local_outage(app, external_api, make_user, monkeypatch):
    from dataclasses import replace
    from bananachat.services import health, status
    from tests.app.test_api import chat, token_for
    row = provider(app, external_api)
    model = enrolled(app, row)
    user = make_user('hosted-user')
    app.config['BC'] = replace(app.config['BC'], inference_outage_mode='shutdown', inference_local=False)
    monkeypatch.setattr(health, 'inference_down', lambda *a: True)
    monkeypatch.setattr(health, 'status', lambda: {'since': time.time()})
    with app.test_request_context():
        assert status.inference_block(user) is None
        assert status.overall() == 'degraded'
    response = chat(app, token_for(app, user), model=model['ollama_name'])
    assert response.status_code == 200, response.data


def test_secret_rotation_keeps_blank_key_and_clears_explicitly(app, external_api):
    row = provider(app, external_api)
    browser = Browser(app)
    browser.login('admin')
    form = {'name': row['name'], 'protocol': 'openai', 'base_url': external_api.url,
            'enabled': 'on', 'allow_private': 'on'}
    browser.post(f'/admin/models/providers/{row["id"]}', form)
    with app.app_context():
        assert store.get(row['id'])['secret_ref'] == row['secret_ref']
    browser.post(f'/admin/models/providers/{row["id"]}', {**form, 'clear_key': 'on'})
    with app.app_context():
        assert store.get(row['id'])['secret_ref'] is None
        assert not (app.config['BC'].instance_dir / '.provider-keys' / row['secret_ref']).exists()


def test_publish_retains_external_limits_and_heavy_preset(app, external_api):
    from bananachat.services import model_lifecycle
    row = provider(app, external_api)
    with app.app_context():
        model = service.enroll(row, 'claude-opus-test', manual=True)
        before = limits_db.get_model_policy(model)
        assert before['enabled'] and before['window_tokens'] == 50_000 and before['counts_toward_pool']
        model_lifecycle.enable(model)
        after = limits_db.get_model_policy(catalog.get(model['id']))
        assert after['enabled'] and after['window_tokens'] == 50_000 and after['counts_toward_pool']
        assert catalog.get(model['id'])['is_rolled_out']


def test_compatible_gateway_usage_and_output_field_options(app, external_api):
    row = provider(app, external_api)
    model = enrolled(app, row)
    with app.app_context():
        store.save(row['id'], token_parameter='max_tokens', stream_usage=0)
        service.sync(row['id'])
        chunks = list(service.stream(catalog.get(model['id']), [{'role': 'user', 'content': 'Hi'}],
                                     options={'num_predict': 40, 'stop': ['END']}))
    body = external_api.requests[-1]['body']
    assert body['max_tokens'] == 40 and body['stop'] == ['END'] and 'stream_options' not in body
    assert chunks[-1].done


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_tool_streams_and_history_preserve_matching_ids(app, external_api, protocol):
    row = provider(app, external_api, protocol=protocol)
    model = enrolled(app, row, tools=True)
    tool = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
    history = [{'role': 'user', 'content': 'Read a file'},
               {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'read_file', 'arguments': {'path': 'a'}}}]},
               {'role': 'tool', 'tool_name': 'read_file', 'content': 'file data'}]
    if protocol == 'openai':
        external_api.events = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'read_file', 'arguments': '{"path":'}}]}, 'finish_reason': None}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"b"}'}}]}, 'finish_reason': None}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}, None]
    else:
        external_api.events = [anthropic_events()[0],
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'name': 'read_file'}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":"b"}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 4}},
            {'type': 'message_stop'}]
    with app.app_context():
        from bananachat.services.agents import settings as agent_settings
        assert agent_settings.supports_tools(model, {}, {})
        chunks = list(service.stream(model, history, tools=[tool]))
    assert chunks[-1].tool_calls == [{'name': 'read_file', 'arguments': '{"path":"b"}'}]
    body = external_api.requests[-1]['body']
    if protocol == 'openai':
        assert body['messages'][1]['tool_calls'][0]['id'] == body['messages'][2]['tool_call_id']
    else:
        assert body['messages'][1]['content'][0]['id'] == body['messages'][2]['content'][0]['tool_use_id']


def test_revision_change_during_stream_closes_it_without_terminal_success(app, external_api):
    row = provider(app, external_api)
    model = enrolled(app, row)
    with app.app_context():
        stream = service.stream(model, [{'role': 'user', 'content': 'Hi'}])
        assert next(stream).content == 'Hello'
        store.save(row['id'], enabled=0)
        with pytest.raises(UpstreamError):
            next(stream)
        stream.close()


def test_outage_never_bypasses_maintenance_or_user_model_access(app, external_api, make_user, monkeypatch):
    from dataclasses import replace
    from bananachat.db import access, settings
    from bananachat.services import health, status
    row = provider(app, external_api)
    model = enrolled(app, row)
    user = make_user('restricted')
    app.config['BC'] = replace(app.config['BC'], inference_local=False, inference_outage_mode='shutdown')
    monkeypatch.setattr(health, 'inference_down', lambda *a: True)
    monkeypatch.setattr(health, 'status', lambda: {})
    with app.test_request_context():
        settings.update(maintenance_mode=1)
        assert status.inference_block(user).kind == 'maintenance'
    with app.test_request_context():
        settings.update(maintenance_mode=0)
        access.set_policy('model', model['id'], 'deny_except_allowlist', False, None)
        assert status.inference_block(user).kind == 'outage'


def test_unavailable_model_error_hides_model_but_generic_404_does_not(app, external_api):
    row = provider(app, external_api)
    model = enrolled(app, row)
    external_api.error = 404
    with app.app_context(), pytest.raises(UpstreamError):
        list(service.stream(model, [{'role': 'user', 'content': 'Hi'}]))
    with app.app_context():
        assert not catalog.get(model['id'])['backend_available']
        assert catalog.get(model['id'])['missing_at']


def test_external_queue_does_not_require_local_model_memory(app, external_api, monkeypatch):
    from bananachat.services import queue
    row = provider(app, external_api)
    model = enrolled(app, row)
    monkeypatch.setattr(queue, '_memory_ok', lambda: False)
    with app.app_context():
        assert queue._model_memory_ok(model['ollama_name'])
        assert not queue._model_memory_ok('an-unregistered-local-model')


def test_provider_effort_is_resolved_for_fresh_user_and_fallback_model(app, external_api, make_user):
    from bananachat.services import inference
    row = provider(app, external_api)
    model = enrolled(app, row, reasoning=['low', 'medium', 'high'])
    user = make_user('effort-user')
    with app.app_context():
        limits_db.set_effort_level(user['id'], model['id'], 'low')
        request = inference.TextRequest(user=user, model=model, messages=[])
        assert inference._provider_effort(request, model) == 'low'
        request.effort = 'high'
        with pytest.raises(UpstreamError):
            inference._provider_effort(request, model)


def test_native_images_keep_base64_content_and_never_fetch_urls():
    from tests.app.test_api import PNG
    import base64
    data = base64.b64encode(PNG).decode()
    message = {'role': 'user', 'content': 'Describe', 'images': [data]}
    openai, _ = api._messages([message], 'openai')
    anthropic, _ = api._messages([message], 'anthropic')
    assert openai[0]['content'][1]['image_url']['url'] == 'data:image/png;base64,' + data
    assert anthropic[0]['content'][1]['source']['data'] == data
    assert anthropic[0]['content'][1]['source']['media_type'] == 'image/png'
