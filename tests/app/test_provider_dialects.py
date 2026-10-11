"""External API dialects (OpenAI, Anthropic, OpenRouter, Mistral, Groq, Together, xAI, local servers) against fake
HTTP servers.

Every server here is a local ``ThreadingHTTPServer``; no test touches the network.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from bananachat import db
from bananachat.db import catalog, external_providers as store
from bananachat.services import external_api as api, external_providers as service, provider_secrets
from bananachat.services.upstream import UpstreamError

KEY = 'sk-synthetic-dialect-key-0123456789abcdef'


class FakeProvider:
    """A provider endpoint: ``models`` answers GET */models, ``events`` streams POST bodies as SSE.

    ``responses`` maps a path (without the query) to ``(status, headers, body)`` to answer
    with an error or a custom document instead. ``close_after`` drops the connection after
    that many events, without a terminal marker.
    """

    def __init__(self, prefix='/v1'):
        owner = self
        self.prefix = prefix
        self.requests = []
        self.models = {'data': [{'id': 'model-a'}]}
        self.pages = None  # Anthropic-style pages, keyed by after_id ('' for the first page)
        self.events = []
        self.raw_tail = b''
        self.done_marker = True
        self.responses = {}
        self.content_type = 'text/event-stream'
        self.close_after = None
        self.delay = 0

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def _send(self, status, headers, body):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def answer(self):
                size = int(self.headers.get('Content-Length', '0'))
                data = self.rfile.read(size)
                parts = urlsplit(self.path)
                owner.requests.append({'path': parts.path, 'query': parse_qs(parts.query), 'method': self.command,
                                       'headers': {k.lower(): v for k, v in self.headers.items()},
                                       'body': json.loads(data) if data else None})
                if parts.path in owner.responses:
                    status, headers, body = owner.responses[parts.path]
                    self._send(status, {'Content-Type': 'application/json', **headers},
                               body if isinstance(body, bytes) else json.dumps(body).encode())
                    return
                if parts.path.endswith('/models') and self.command == 'GET':
                    document = owner.models
                    if owner.pages is not None:
                        document = owner.pages[parse_qs(parts.query).get('after_id', [''])[0]]
                    self._send(200, {'Content-Type': 'application/json'}, json.dumps(document).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', owner.content_type)
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    for position, event in enumerate(owner.events):
                        if owner.close_after is not None and position >= owner.close_after:
                            self.wfile.flush()
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        if isinstance(event, bytes):
                            self.wfile.write(event)
                            continue
                        name = event.get('type') if isinstance(event, dict) else None
                        line = (f'event: {name}\n' if name else '') + 'data: ' + json.dumps(event) + '\n\n'
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                        if owner.delay:
                            time.sleep(owner.delay)
                    if owner.done_marker:
                        self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.write(owner.raw_tail)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                self.close_connection = True

            do_GET = answer
            do_POST = answer

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}{prefix}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self):
        return [request for request in self.requests if request['method'] == 'POST']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@pytest.fixture
def fake():
    server = FakeProvider()
    yield server
    server.close()


def make_provider(app, fake, *, protocol='openai', name='Dialect', **fields):
    fields.setdefault('auth_style', 'x-api-key' if protocol == 'anthropic' else 'bearer')
    with app.app_context():
        provider_id = store.save(name=name, base_url=fake.url, protocol=protocol, secret_ref=provider_secrets.save(KEY),
                                 enabled=1, allow_private=1, **fields)
        return store.get(provider_id)


def make_model(app, provider, model='model-a', **capabilities):
    with app.app_context():
        row = service.enroll(store.get(provider['id']), model, manual=True, **capabilities)
        catalog.set_rollout(row['id'], True)
        return catalog.get(row['id'])


def run(app, model, messages=None, **kwargs):
    with app.app_context():
        return list(service.stream(catalog.get(model['id']), messages or [{'role': 'user', 'content': 'Hi'}], **kwargs))


def failure(app, model, messages=None, **kwargs):
    with pytest.raises(UpstreamError) as caught:
        run(app, model, messages, **kwargs)
    return caught.value


def openai_text(*parts, finish='stop', usage=(10, 4)):
    events = [{'choices': [{'delta': {'content': part}, 'finish_reason': None}]} for part in parts]
    events.append({'choices': [{'delta': {}, 'finish_reason': finish}]})
    if usage:
        events.append({'choices': [], 'usage': {'prompt_tokens': usage[0], 'completion_tokens': usage[1]}})
    return events


def anthropic_text(text='Hello', stop='end_turn', input_tokens=10, output_tokens=4):
    return [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': input_tokens, 'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'ping'},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': {'output_tokens': output_tokens}},
        {'type': 'message_stop'},
    ]


def done(chunks):
    assert chunks[-1].done
    return chunks[-1]


# ----- model lists ------------------------------------------------------------------------------------------

def test_one_listing_without_a_model_does_not_withdraw_it(app, fake):
    """A provider listing can briefly omit a model; only repeated omissions withdraw it (as for Ollama)."""
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    with app.app_context():
        db.execute("UPDATE ai_models SET external_config=? WHERE id=?", (json.dumps({'manual': False}), model['id']))
        fake.models = {'data': [{'id': 'other'}]}
        service.sync(provider['id'])
        assert catalog.get(model['id'])['backend_available']
        service.sync(provider['id'])
        assert not catalog.get(model['id'])['backend_available']
        assert catalog.get(model['id'])['missing_at']
        fake.models = {'data': [{'id': 'model-a'}]}
        service.sync(provider['id'])
        assert catalog.get(model['id'])['backend_available'] and not catalog.get(model['id'])['missing_at']
        fake.models = {'data': [{'id': 'other'}]}
        service.sync(provider['id'])
        assert catalog.get(model['id'])['backend_available'], 'a reappearance resets the count'


def test_list_shaped_model_listings_are_accepted(app, fake):
    """Together.ai answers GET /models with a bare JSON array."""
    provider = make_provider(app, fake)
    fake.models = [{'id': 'meta-llama/Llama-3.3-70B-Instruct-Turbo', 'display_name': 'Llama 3.3 70B', 'type': 'chat',
                    'context_length': 131072, 'pricing': {'input': 0.88, 'output': 0.88}},
                   {'id': 'BAAI/bge-large-en-v1.5', 'type': 'embedding'}]
    with app.app_context():
        models = service.sync(provider['id'])
    by_id = {item['id']: item for item in models}
    llama = by_id['meta-llama/Llama-3.3-70B-Instruct-Turbo']
    assert llama['context'] == 131072 and llama['chat'] is True and llama['display'] == 'Llama 3.3 70B'
    assert llama['pricing'] == {'input': 0.88, 'output': 0.88}
    assert by_id['BAAI/bge-large-en-v1.5']['chat'] is False


def test_anthropic_discovery_follows_pages_and_reads_capabilities(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    adaptive = {'image_input': {'supported': True},
                'thinking': {'supported': True, 'types': {'adaptive': {'supported': True},
                                                          'enabled': {'supported': False}}},
                'effort': {'supported': True, 'low': {'supported': True}, 'medium': {'supported': True},
                           'high': {'supported': True}, 'xhigh': {'supported': True}, 'max': {'supported': True}}}
    budget = {'image_input': {'supported': True},
              'thinking': {'supported': True, 'types': {'adaptive': {'supported': False},
                                                        'enabled': {'supported': True}}},
              'effort': {'supported': False}}
    fake.pages = {
        '': {'data': [{'id': 'claude-new', 'display_name': 'Claude New', 'max_input_tokens': 1_000_000,
                       'max_tokens': 128_000, 'capabilities': adaptive}], 'has_more': True, 'last_id': 'claude-new'},
        'claude-new': {'data': [{'id': 'claude-old', 'display_name': 'Claude Old', 'max_input_tokens': 200_000,
                                 'max_tokens': 64_000, 'capabilities': budget}], 'has_more': False,
                       'last_id': 'claude-old'}}
    with app.app_context():
        models = {item['id']: item for item in service.sync(provider['id'])}
    assert [r['query'].get('after_id') for r in fake.requests] == [None, ['claude-new']]
    assert all(r['headers']['x-api-key'] == KEY and r['headers']['anthropic-version'] == '2023-06-01'
               for r in fake.requests)
    new, old = models['claude-new'], models['claude-old']
    assert (new['context'], new['max_output'], new['vision'], new['tools']) == (1_000_000, 128_000, True, True)
    assert new['thinking'] == 'adaptive' and new['reasoning'] == ['low', 'medium', 'high', 'extra', 'max']
    assert old['thinking'] == 'budget' and old['reasoning'] == ['off', 'low', 'medium', 'high']


def test_openrouter_discovery_reads_context_modalities_pricing_and_parameters(app):
    server = FakeProvider(prefix='/api/v1')
    try:
        provider = make_provider(app, server, name='OpenRouter', preset='openrouter')
        server.models = {'data': [
            {'id': 'anthropic/claude-sonnet', 'name': 'Anthropic: Claude Sonnet', 'context_length': 1_000_000,
             'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
             'pricing': {'prompt': '0.000003', 'completion': '0.000015'},
             'top_provider': {'max_completion_tokens': 64_000},
             'supported_parameters': ['tools', 'reasoning', 'max_tokens']},
            {'id': 'openrouter/auto', 'name': 'Auto Router', 'context_length': 2_000_000,
             'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
             'pricing': {'prompt': '-1', 'completion': '-1'}, 'supported_parameters': []},
            {'id': 'black-forest-labs/flux', 'name': 'FLUX', 'architecture': {'output_modalities': ['image']}}]}
        with app.app_context():
            models = {item['id']: item for item in service.sync(provider['id'])}
    finally:
        server.close()
    sonnet = models['anthropic/claude-sonnet']
    assert sonnet['display'] == 'Anthropic: Claude Sonnet' and sonnet['context'] == 1_000_000
    assert sonnet['max_output'] == 64_000 and sonnet['vision'] is True and sonnet['tools'] is True
    assert sonnet['pricing'] == {'input': 3.0, 'output': 15.0}
    assert sonnet['reasoning'] == ['off', 'low', 'medium', 'high'] and sonnet['chat'] is True
    assert models['openrouter/auto']['pricing'] is None and models['openrouter/auto']['tools'] is False
    assert models['black-forest-labs/flux']['chat'] is False


def test_gemini_model_ids_drop_the_resource_prefix(app, fake):
    """Gemini's OpenAI-compatible list names models "models/<id>"; its chat endpoint takes the bare ID."""
    provider = make_provider(app, fake, preset='gemini')
    fake.models = {'object': 'list', 'data': [{'id': 'models/gemini-flash', 'object': 'model', 'owned_by': 'google'},
                                              {'id': 'models/text-embedding-004', 'object': 'model'}]}
    with app.app_context():
        models = {item['id']: item for item in service.sync(provider['id'])}
    assert set(models) == {'gemini-flash', 'text-embedding-004'}
    assert models['gemini-flash']['chat'] and not models['text-embedding-004']['chat']


@pytest.mark.parametrize('item,expected', [
    ({'id': 'mistral-large-latest', 'max_context_length': 131072,
      'capabilities': {'completion_chat': True, 'function_calling': True, 'vision': False}},
     {'chat': True, 'tools': True, 'vision': False, 'context': 131072}),
    ({'id': 'mistral-embed', 'capabilities': {'completion_chat': False}}, {'chat': False}),
    ({'id': 'llama-3.3-70b-versatile', 'context_window': 131072, 'max_completion_tokens': 32768},
     {'chat': True, 'context': 131072, 'max_output': 32768, 'vision': None}),
    ({'id': 'Qwen/Qwen3-8B', 'max_model_len': 40960}, {'context': 40960, 'chat': True}),
    ({'id': 'whisper-large-v3'}, {'chat': False}),
    ({'id': 'text-embedding-3-small'}, {'chat': False}),
])
def test_compatible_model_metadata_is_normalized(item, expected):
    entry = api._model_entry(item, 'openai')
    assert {key: entry[key] for key in expected} == expected


# ----- streaming ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_tool_call_arguments_arrive_decoded_for_the_agent_history(app, fake, protocol):
    """The agent loop keeps only dict arguments in its history; JSON strings were replaced by {}."""
    provider = make_provider(app, fake, protocol=protocol)
    model = make_model(app, provider, tools=True)
    if protocol == 'openai':
        fake.events = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'read_file',
                                                                                         'arguments': '{"pa'}}]},
                          'finish_reason': None}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'th": "b"}'}}]},
                          'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}]
    else:
        fake.events = [anthropic_text()[0],
                       {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 't',
                                                                                    'name': 'read_file', 'input': {}}},
                       {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta',
                                                                             'partial_json': '{"path": "b"}'}},
                       {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 2}},
                       {'type': 'message_stop'}]
    tool = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
    chunks = run(app, model, tools=[tool])
    assert done(chunks).tool_calls == [{'name': 'read_file', 'arguments': {'path': 'b'}}]


def test_invalid_tool_arguments_stay_a_string_for_the_validator(app, fake):
    provider = make_provider(app, fake)
    model = make_model(app, provider, tools=True)
    fake.events = [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'read_file',
                                                                                    'arguments': '{"path": '}}]},
                                 'finish_reason': 'tool_calls'}]}]
    tool = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
    assert done(run(app, model, tools=[tool])).tool_calls == [{'name': 'read_file', 'arguments': '{"path": '}]


def test_openai_effort_levels_map_to_values_the_api_accepts(app, fake):
    provider = make_provider(app, fake)
    model = make_model(app, provider, reasoning=['off', 'high', 'extra', 'max'])
    fake.events = openai_text('Hi')
    sent = []
    for effort in ('off', 'high', 'extra', 'max'):
        run(app, model, effort=effort)
        sent.append(fake.posts()[-1]['body']['reasoning_effort'])
    assert sent == ['none', 'high', 'xhigh', 'xhigh']


def test_openai_requests_streamed_usage_and_the_preset_output_field(app, fake):
    provider = make_provider(app, fake, preset='openai')
    model = make_model(app, provider)
    fake.events = openai_text('Hi')
    run(app, model, options={'num_predict': 300, 'temperature': 0.2})
    body = fake.posts()[-1]['body']
    assert body['stream'] is True and body['stream_options'] == {'include_usage': True}
    assert body['max_completion_tokens'] == 300 and 'max_tokens' not in body and body['temperature'] == 0.2
    assert fake.posts()[-1]['headers']['authorization'] == 'Bearer ' + KEY


def test_output_limit_never_exceeds_the_models_maximum(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, listing={'id': 'model-a', 'max_output': 2000})
    fake.events = anthropic_text()
    run(app, model, options={'num_predict': 8192})
    assert fake.posts()[-1]['body']['max_tokens'] == 2000


def test_anthropic_sampling_options_are_not_sent_unless_enabled_for_the_model(app, fake):
    """Current Claude models reject temperature/top_p with HTTP 400; send them only when the model allows them."""
    provider = make_provider(app, fake, protocol='anthropic')
    fake.events = anthropic_text()
    plain = make_model(app, provider, model='claude-new')
    run(app, plain, options={'temperature': 0.3, 'top_p': 0.9, 'num_predict': 100})
    body = fake.posts()[-1]['body']
    assert 'temperature' not in body and 'top_p' not in body
    older = make_model(app, provider, model='claude-old', sampling=True)
    run(app, older, options={'temperature': 0.3, 'num_predict': 100})
    assert fake.posts()[-1]['body']['temperature'] == 0.3


@pytest.mark.parametrize('stop,finish', [('model_context_window_exceeded', 'length'), ('pause_turn', 'stop'),
                                         ('refusal', 'content_filter'), ('some_future_reason', 'stop')])
def test_anthropic_complete_answers_with_newer_stop_reasons_succeed(app, fake, stop, finish):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    fake.events = anthropic_text(stop=stop)
    fake.done_marker = False
    assert done(run(app, model)).finish_reason == finish


def test_anthropic_late_input_usage_and_thinking_blocks_are_counted(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, reasoning=['low', 'high'])
    fake.done_marker = False
    fake.events = [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 5, 'cache_read_input_tokens': 20,
                                                        'output_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'Let me see'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'abc'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'redacted_thinking', 'data': 'xyz'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'text_delta', 'text': 'Answer'}},
        {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'citations_delta', 'citation': {}}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
         'usage': {'input_tokens': 7, 'cache_read_input_tokens': 20, 'output_tokens': 40}},
        {'type': 'message_stop'}]
    chunks = run(app, model, effort='high')
    assert ''.join(chunk.thinking for chunk in chunks) == 'Let me see'
    assert ''.join(chunk.content for chunk in chunks) == 'Answer'
    assert done(chunks).prompt_tokens == 27 and done(chunks).completion_tokens == 40


def test_anthropic_adaptive_thinking_asks_for_summarized_reasoning(app, fake):
    """Newer Claude models omit thinking text unless display is summarized, so users saw an empty pause."""
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, reasoning=['low', 'high', 'extra'])
    fake.events = anthropic_text()
    run(app, model, effort='extra')
    body = fake.posts()[-1]['body']
    assert body['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert body['output_config'] == {'effort': 'xhigh'}


def test_anthropic_budget_thinking_for_models_without_adaptive_thinking(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, model='claude-haiku-old', reasoning=['off', 'low', 'high'], thinking='budget')
    fake.events = anthropic_text()
    run(app, model, effort='high', options={'num_predict': 4000})
    body = fake.posts()[-1]['body']
    assert body['thinking']['type'] == 'enabled' and 1024 <= body['thinking']['budget_tokens'] < body['max_tokens']
    assert body['max_tokens'] == 4000 + body['thinking']['budget_tokens'] and 'output_config' not in body
    run(app, model, effort='off', options={'num_predict': 4000})
    assert 'thinking' not in fake.posts()[-1]['body'] and fake.posts()[-1]['body']['max_tokens'] == 4000


def test_anthropic_tool_continuation_without_thinking_blocks_does_not_request_thinking(app, fake):
    """Claude rejects a thinking request whose last assistant tool turn lacks its signed thinking blocks."""
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, tools=True, reasoning=['high'])
    fake.events = anthropic_text()
    tool = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
    history = [{'role': 'user', 'content': 'Read a file'},
               {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'read_file',
                                                                                  'arguments': {'path': 'a'}}}]},
               {'role': 'tool', 'tool_name': 'read_file', 'content': 'file data'}]
    run(app, model, history, tools=[tool], effort='high')
    body = fake.posts()[-1]['body']
    assert 'thinking' not in body and body['output_config'] == {'effort': 'high'}
    run(app, model, history[:1], tools=[tool], effort='high')
    assert fake.posts()[-1]['body']['thinking']['type'] == 'adaptive'


def test_anthropic_parallel_tool_results_share_one_user_turn_and_empty_text_is_omitted(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider, tools=True)
    fake.events = anthropic_text()
    tool = {'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}
    history = [{'role': 'user', 'content': 'Read two files'},
               {'role': 'assistant', 'content': '', 'tool_calls': [
                   {'function': {'name': 'read_file', 'arguments': {'path': 'a'}}},
                   {'function': {'name': 'read_file', 'arguments': {'path': 'b'}}}]},
               {'role': 'tool', 'tool_name': 'read_file', 'content': 'A'},
               {'role': 'tool', 'tool_name': 'read_file', 'content': 'B'}]
    run(app, model, history, tools=[tool])
    messages = fake.posts()[-1]['body']['messages']
    assert [message['role'] for message in messages] == ['user', 'assistant', 'user']
    assert [block['type'] for block in messages[1]['content']] == ['tool_use', 'tool_use']
    assert [block['tool_use_id'] for block in messages[2]['content']] == [b['id'] for b in messages[1]['content']]


@pytest.mark.parametrize('events,done_marker', [
    (openai_text('Hi'), False),  # llama.cpp-style servers can close after the finish chunk
    ([{'choices': [{'delta': None, 'finish_reason': None}]}] + openai_text('Hi'), True),
    (openai_text('Hi', finish='eos'), True),
    (openai_text('Hi', finish='model_length'), True),
    (openai_text('Hi', usage=None) + [{'choices': [], 'usage': {'total_tokens': 9}}], True),
    ([{'choices': [], 'prompt_filter_results': []}, {'choices': [], 'usage': None}] + openai_text('Hi'), True),
])
def test_openai_compatible_server_variations_complete(app, fake, events, done_marker):
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events, fake.done_marker = events, done_marker
    chunks = run(app, model)
    assert ''.join(chunk.content for chunk in chunks) == 'Hi'
    assert done(chunks).finish_reason in ('stop', 'length')


def test_reasoning_tokens_outside_completion_tokens_are_counted(app, fake):
    """xAI reports reasoning tokens beside completion_tokens; total_tokens includes them."""
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events = openai_text('Hi', usage=None) + [{'choices': [], 'usage': {
        'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 214,
        'completion_tokens_details': {'reasoning_tokens': 200}}}]
    final = done(run(app, model))
    assert (final.prompt_tokens, final.completion_tokens) == (10, 204)


def test_groq_usage_in_x_groq_is_used(app, fake):
    provider = make_provider(app, fake, stream_usage=0)
    model = make_model(app, provider)
    fake.events = openai_text('Hi', usage=None) + [{'choices': [], 'x_groq': {'usage': {'prompt_tokens': 21,
                                                                                         'completion_tokens': 3}}}]
    final = done(run(app, model))
    assert final.prompt_tokens == 21 and final.completion_tokens == 3


def test_reasoning_fields_of_compatible_servers_stream_as_thinking(app, fake):
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events = [{'choices': [{'delta': {'reasoning_content': 'A'}, 'finish_reason': None}]},
                   {'choices': [{'delta': {'reasoning': 'B'}, 'finish_reason': None}]},
                   # Mistral reasoning models stream typed content chunks.
                   {'choices': [{'delta': {'content': [{'type': 'thinking', 'thinking': [{'type': 'text', 'text': 'C'}]},
                                                       {'type': 'text', 'text': 'Answer'}]}, 'finish_reason': None}]},
                   ] + openai_text()
    chunks = run(app, model)
    assert ''.join(chunk.thinking for chunk in chunks) == 'ABC'
    assert ''.join(chunk.content for chunk in chunks) == 'Answer'


def test_openrouter_uses_its_reasoning_object_and_streams_reasoning_details(app, fake):
    provider = make_provider(app, fake, name='OpenRouter', preset='openrouter')
    model = make_model(app, provider, reasoning=['off', 'high', 'max'])
    fake.events = [{'choices': [{'delta': {'reasoning_details': [{'type': 'reasoning.text', 'text': 'Think'}]},
                                 'finish_reason': None}]},
                   # OpenRouter sends the same reasoning both ways; it must be shown once.
                   {'choices': [{'delta': {'reasoning': 'More', 'reasoning_details': [
                       {'type': 'reasoning.text', 'text': 'More'}]}, 'finish_reason': None}]}] + openai_text('Hi')
    chunks = run(app, model, effort='max')
    body = fake.posts()[-1]['body']
    assert body['reasoning'] == {'effort': 'xhigh'} and 'reasoning_effort' not in body
    assert ''.join(chunk.thinking for chunk in chunks) == 'ThinkMore'
    run(app, model, effort='off')
    assert fake.posts()[-1]['body']['reasoning'] == {'effort': 'none'}


def test_stream_waits_for_silent_reasoning_up_to_the_first_token_timeout(app, fake, monkeypatch):
    """Reasoning models can be silent for minutes; a 30-second gap limit cut them off."""
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.events = openai_text('Hi')
    seen = {}
    original = api.open_request

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(api, 'open_request', spy)
    app.config['BC'] = replace(app.config['BC'], first_token_timeout=240, generation_timeout=600)
    run(app, model)
    assert seen['read_timeout'] == 240 and seen['first_byte_timeout'] == 240


def test_auth_styles_and_extra_headers_are_sent(app, fake):
    provider = make_provider(app, fake, preset='azure', auth_style='api-key',
                             extra_headers=json.dumps({'X-Title': 'Banana', 'HTTP-Referer': 'https://chat.example'}))
    model = make_model(app, provider)
    fake.events = openai_text('Hi')
    run(app, model)
    headers = fake.posts()[-1]['headers']
    assert headers['api-key'] == KEY and 'authorization' not in headers
    assert headers['x-title'] == 'Banana' and headers['http-referer'] == 'https://chat.example'
    assert fake.posts()[-1]['body']['model'] == 'model-a'


def test_partially_streamed_usage_is_reported_before_the_end(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    fake.events = anthropic_text(input_tokens=1234)
    chunks = run(app, model)
    early = [chunk for chunk in chunks if not chunk.done and chunk.prompt_tokens is not None]
    assert early and early[0].prompt_tokens == 1234


# ----- errors -------------------------------------------------------------------------------------------------

def test_rate_limit_keeps_provider_retry_after_and_is_capacity(app, fake):
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.responses['/v1/chat/completions'] = (429, {'Retry-After': '7'},
                                              {'error': {'message': 'Rate limit for ' + KEY, 'code': 'rate_limit'}})
    error = failure(app, model)
    assert error.kind == 'capacity' and error.retry_after == 7 and error.code == 'provider_rate_limited'
    assert KEY not in str(error) and KEY not in error.detail and 'Rate limit for' in error.detail


@pytest.mark.parametrize('status,body,code', [
    (401, {'error': {'message': 'Incorrect API key provided: ' + KEY, 'type': 'invalid_request_error'}},
     'provider_auth_failed'),
    (403, {'type': 'error', 'error': {'type': 'permission_error', 'message': 'no access'}}, 'provider_forbidden'),
    (402, {'error': {'message': 'Insufficient credits', 'code': 402}}, 'provider_quota_exhausted'),
    (429, {'error': {'message': 'You exceeded your quota', 'code': 'insufficient_quota'}}, 'provider_quota_exhausted'),
    (404, {'error': {'message': 'Not Found'}}, 'provider_not_found'),
    (400, {'error': {'message': 'prompt is too long'}}, 'provider_rejected_request'),
    (500, {'error': {'message': 'boom'}}, 'provider_unavailable'),
    (529, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}, 'provider_overloaded'),
])
def test_http_errors_map_to_clear_codes_without_secrets(app, fake, status, body, code):
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.responses['/v1/chat/completions'] = (status, {}, body)
    error = failure(app, model)
    assert error.code == code and error.status == status
    assert KEY not in str(error) and KEY not in error.detail
    with app.app_context():
        assert catalog.get(model['id'])['backend_available'], 'only a model-specific 404 withdraws a model'


def test_anthropic_not_found_error_withdraws_the_model(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    fake.responses['/v1/messages'] = (404, {}, {'type': 'error', 'error': {'type': 'not_found_error',
                                                                           'message': 'model: model-a'}})
    assert failure(app, model).code == 'provider_model_not_found'
    with app.app_context():
        assert not catalog.get(model['id'])['backend_available']


def test_anthropic_overloaded_stream_error_is_a_capacity_problem(app, fake):
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    fake.done_marker = False
    fake.events = [anthropic_text()[0], {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}]
    error = failure(app, model)
    assert error.kind == 'capacity' and error.code == 'provider_overloaded'


def test_a_provider_that_never_answers_times_out_clearly(app, monkeypatch):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    try:
        class Silent:
            url = f'http://127.0.0.1:{listener.getsockname()[1]}/v1'
        provider = make_provider(app, Silent)
        model = make_model(app, provider)
        app.config['BC'] = replace(app.config['BC'], first_token_timeout=1)
        error = failure(app, model)
    finally:
        listener.close()
    assert error.code == 'provider_timeout' and error.kind == 'timeout'


def test_an_unreadable_key_is_reported_as_such(app, fake):
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    with app.app_context():
        path = app.config['BC'].instance_dir / '.provider-keys' / provider['secret_ref']
        path.write_bytes(b'bcpk1:not-a-valid-token')
    assert failure(app, model).code == 'provider_key_unreadable'
    assert fake.posts() == []


# ----- public API and accounting ---------------------------------------------------------------------------

def test_public_api_reports_provider_rate_limit_with_its_retry_after(app, fake, make_user):
    from tests.app.test_api import chat, token_for
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    fake.responses['/v1/chat/completions'] = (429, {'Retry-After': '12'}, {'error': {'message': 'slow down'}})
    response = chat(app, token_for(app, make_user('limited-api')), model=model['ollama_name'])
    assert response.status_code == 503, response.data
    assert response.headers['Retry-After'] == '12'
    assert response.json['error']['code'] == 'provider_rate_limited'


@pytest.mark.parametrize('status,body,expected_status,expected_code', [
    (401, {'error': {'message': 'bad key ' + KEY}}, 502, 'provider_misconfigured'),
    (404, {'error': {'message': 'The model does not exist', 'code': 'model_not_found'}}, 404, 'model_not_found'),
    (400, {'error': {'message': 'context too long'}}, 400, 'provider_rejected_request'),
    (500, {'error': {'message': 'boom'}}, 503, 'provider_unavailable'),
])
def test_public_api_maps_provider_failures(app, fake, make_user, status, body, expected_status, expected_code):
    from tests.app.test_api import chat, ledger, token_for
    provider = make_provider(app, fake)
    model = make_model(app, provider)
    user = make_user('mapped-api')
    fake.responses['/v1/chat/completions'] = (status, {}, body)
    response = chat(app, token_for(app, user), model=model['ollama_name'])
    assert response.status_code == expected_status, response.data
    assert response.json['error']['code'] == expected_code
    assert KEY.encode() not in response.data and b'127.0.0.1' not in response.data
    assert ledger(app, user) == []


def test_interrupted_answer_is_charged_with_the_providers_input_count(app, fake, make_user):
    from tests.app.test_api import chat, ledger, token_for
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    user = make_user('interrupted-api')
    fake.events = anthropic_text(text='Partial answer', input_tokens=1234)
    fake.close_after = 4  # message_start, block start, ping, the text; then the connection drops
    fake.done_marker = False
    response = chat(app, token_for(app, user), model=model['ollama_name'])
    assert response.status_code == 502, response.data
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]['tokens_in'] == 1234 and rows[0]['tokens_out'] >= 1


def test_input_reported_before_any_output_is_still_charged(app, fake, make_user):
    from tests.app.test_api import chat, ledger, token_for
    provider = make_provider(app, fake, protocol='anthropic')
    model = make_model(app, provider)
    user = make_user('silent-api')
    fake.events = anthropic_text(input_tokens=777)
    fake.close_after = 1
    fake.done_marker = False
    response = chat(app, token_for(app, user), model=model['ollama_name'])
    assert response.status_code >= 500
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]['tokens_in'] == 777 and rows[0]['tokens_out'] == 0
