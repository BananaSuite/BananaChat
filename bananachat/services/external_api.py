"""OpenAI-compatible and Anthropic HTTP protocols with pinned endpoints and bounded SSE."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import re
import socket
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ResolverTimeout
from urllib.parse import quote, urlsplit, urlunsplit

from flask import current_app

from bananachat import db
from bananachat.db import catalog, external_providers as store
from bananachat.services import provider_secrets, supervisor
from bananachat.services.ollama import Chunk
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError, open_request

MAX_MODELS = 2000
MAX_RECORD = 1024 * 1024
MAX_TOKENS = 1_000_000_000
_RESOLVERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='provider-dns')
MODEL_ID = re.compile(r'[^\s\x00-\x1f\x7f]{1,200}\Z')
# These metadata services use carrier-grade NAT/ULA space rather than link-local
# addresses. A trusted-private gateway option must not grant access to them.
_METADATA_ADDRESSES = {ipaddress.ip_address('100.100.100.200'),
                       ipaddress.ip_address('fd00:ec2::254')}
# Transition addresses can reach an embedded IPv4 endpoint through a translator
# or tunnel. Trusting a private gateway must not trust those indirect routes to
# metadata services; their classification also varies across Python releases.
_NAT64_NETWORKS = (ipaddress.ip_network('64:ff9b::/96'),
                   ipaddress.ip_network('64:ff9b:1::/48'))
_IPV4_COMPATIBLE = ipaddress.ip_network('::/96')


def base_url(value, *, allow_private=False):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('Enter an API base URL without spaces or control characters.')
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError('The provider URL has an invalid port.') from None
    if not parts.hostname or parts.scheme not in ('http', 'https') or parts.username or parts.password \
            or parts.query or parts.fragment or '\\' in value or port == 0:
        raise ValueError('Use an HTTP(S) base URL without credentials, query parameters or a fragment.')
    if parts.scheme != 'https' and not allow_private:
        raise ValueError('External providers require HTTPS. Enable trusted private endpoints for an internal HTTP service.')
    if '..' in parts.path.split('/'):
        raise ValueError('The API base path cannot contain parent-directory segments.')
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))


def _addresses(host, port, allow_private):
    future = _RESOLVERS.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    try:
        addresses = future.result(timeout=10)
    except ResolverTimeout:
        future.cancel()
        raise UpstreamError('Provider DNS lookup timed out.', kind='timeout') from None
    approved = []
    for family, socktype, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0].split('%')[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is None and (
                ip.sixtofour is not None or ip.teredo is not None
                or any(ip in network for network in _NAT64_NETWORKS)
                or ip in _IPV4_COMPATIBLE):
            raise UpstreamError('The provider resolves to a restricted address. Use a public endpoint or explicitly allow a trusted private server.')
        ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if ip in _METADATA_ADDRESSES or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved \
                or (not ip.is_global and not allow_private):
            raise UpstreamError('The provider resolves to a restricted address. Use a public endpoint or explicitly allow a trusted private server.')
        if family in (socket.AF_INET, socket.AF_INET6):
            approved.append((family, socktype, proto, address))
    if not approved:
        raise UpstreamError('The provider hostname did not resolve to a usable address.')
    return approved


def _connection_factory(provider, cancel=None):
    def factory(url, timeout):
        parts = urlsplit(base_url(url, allow_private=bool(provider['allow_private'])))
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        approved = _addresses(parts.hostname, port, bool(provider['allow_private']))
        if parts.scheme == 'http' and any(ipaddress.ip_address(address[0]).is_global for _, _, _, address in approved):
            raise UpstreamError('Public API providers require HTTPS, including when private endpoints are allowed.')
        # HTTPSConnection keeps the original hostname for certificate checks and SNI.
        connection = (http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection)(
            parts.hostname, port, timeout=timeout)
        def connect(_address, wait, source_address=None):
            last = None
            for family, socktype, proto, address in approved:
                sock = socket.socket(family, socktype, proto)
                try:
                    if cancel is not None:
                        cancel.check()
                    sock.settimeout(wait)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(address)  # Numeric address from this request's vetted DNS result.
                    if cancel is not None:
                        cancel.check()
                    return sock
                except Cancelled:
                    sock.close()
                    raise
                except OSError as error:
                    last = error
                    sock.close()
            raise last or OSError('Connection failed')
        connection._create_connection = connect
        return connection, parts
    return factory


def _headers(provider):
    key = provider_secrets.load(provider['secret_ref'])
    if provider['protocol'] == 'anthropic':
        return {'x-api-key': key, 'anthropic-version': '2023-06-01'} if key else {'anthropic-version': '2023-06-01'}
    return {'Authorization': 'Bearer ' + key} if key else {}


def _error(error):
    status = getattr(error, 'status', None)
    if status in (401, 403):
        message = 'The provider rejected the server credentials or model access.'
    elif status == 429:
        message = 'The provider request limit or account balance is exhausted.'
    elif status == 404:
        message = 'The provider endpoint or model was not found.'
    else:
        message = 'The external provider could not complete the request.'
    # Never echo upstream bodies, URLs, headers or exception strings containing keys.
    return UpstreamError(message, status=status, kind=getattr(error, 'kind', None))


@contextmanager
def _open(provider, method, path, *, body=None, cancel=None, discovery=False):
    config = current_app.config['BC']
    own_cancel = cancel is None
    cancel = cancel or CancelToken()
    budget = 30 if discovery else config.generation_timeout
    handle = supervisor.cancel_at(time.monotonic() + budget, cancel)
    try:
        with open_request(method, provider['base_url'], path, headers=_headers(provider), body=body, cancel=cancel,
                          connect_timeout=10, first_byte_timeout=15 if discovery else config.first_token_timeout,
                          read_timeout=15 if discovery else min(30, config.generation_timeout),
                          total_timeout=budget, max_bytes=5 * 1024 * 1024 if discovery else 16 * 1024 * 1024,
                          connection_factory=_connection_factory(provider, cancel)) as response:
            yield response
    except Cancelled:
        if own_cancel:
            raise UpstreamError('The provider request timed out.', kind='timeout') from None
        raise
    finally:
        supervisor.clear_deadline(handle)


def discover(provider):
    """A failed/incomplete listing changes no availability or enrollment state."""
    models, seen = [], set()
    cancel = CancelToken()
    deadline = supervisor.cancel_at(time.monotonic() + 30, cancel)
    path = '/models?limit=100' if provider['protocol'] == 'anthropic' else '/models'
    try:
        for _ in range(20):
            with _open(provider, 'GET', path, discovery=True, cancel=cancel) as response:
                data = response.json(5 * 1024 * 1024)
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise UpstreamError('Invalid model list.')
            for item in data['data']:
                name = item.get('id') if isinstance(item, dict) else None
                if not isinstance(name, str) or not MODEL_ID.fullmatch(name):
                    raise UpstreamError('Invalid model ID.')
                if name not in seen:
                    models.append({'id': name, 'display': str(item.get('display_name') or name)[:120]})
                    seen.add(name)
                if len(models) > MAX_MODELS:
                    raise UpstreamError('Model list too large.')
            if provider['protocol'] != 'anthropic' or not data.get('has_more'):
                return models
            last = data.get('last_id')
            if not isinstance(last, str) or not MODEL_ID.fullmatch(last) or not data['data']:
                raise UpstreamError('Invalid model pagination.')
            path = '/models?limit=100&after_id=' + quote(last, safe='')
        raise UpstreamError('Model listing exceeded its page limit.')
    except Cancelled:
        raise UpstreamError("The provider model check timed out.", kind="timeout") from None
    except Exception as error:
        raise _error(error) from None
    finally:
        supervisor.clear_deadline(deadline)


def _image(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise UpstreamError('Invalid context image.') from None
    if raw.startswith(b'\x89PNG'):
        mime = 'image/png'
    elif raw.startswith(b'\xff\xd8'):
        mime = 'image/jpeg'
    elif raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        mime = 'image/webp'
    elif raw.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    else:
        raise UpstreamError('Unsupported context image format.')
    return mime


def _messages(messages, protocol):
    result, system, pending = [], [], []
    for index, message in enumerate(messages):
        role = message.get('role')
        content = message.get('content', '')
        if role not in ('system', 'user', 'assistant', 'tool') or not isinstance(content, str):
            raise UpstreamError('Unsupported conversation format.')
        if protocol == 'anthropic' and role == 'system':
            system.append(content)
            continue
        item = {'role': role, 'content': content}
        images = message.get('images') or []
        if images:
            blocks = [{'type': 'text', 'text': content}]
            for image in images:
                mime = _image(image)
                blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': image}}
                              if protocol == 'anthropic' else {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image}'}})
            item['content'] = blocks
        if role == 'assistant' and message.get('tool_calls'):
            pending = []
            calls = []
            for position, call in enumerate(message['tool_calls']):
                function = call.get('function', call)
                call_id = f'bc_call_{index}_{position}'
                pending.append((function.get('name'), call_id))
                args = function.get('arguments', {})
                if protocol == 'anthropic':
                    if isinstance(args, str):
                        args = json.loads(args)
                    calls.append({'type': 'tool_use', 'id': call_id, 'name': function['name'], 'input': args})
                else:
                    calls.append({'id': call_id, 'type': 'function', 'function': {'name': function['name'],
                                  'arguments': args if isinstance(args, str) else json.dumps(args)}})
            if protocol == 'anthropic':
                item['content'] = ([{'type': 'text', 'text': content}] if content else []) + calls
            else:
                item['tool_calls'] = calls
        if role == 'tool':
            name = message.get('tool_name')
            found = next(((n, i) for n, i in pending if n == name), None)
            if found is None:
                raise UpstreamError('A tool result has no matching call.')
            pending.remove(found)
            if protocol == 'anthropic':
                item = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': found[1], 'content': content}]}
            else:
                item['tool_call_id'] = found[1]
        result.append(item)
    return result, '\n\n'.join(system)


def _sse(response):
    parts, size = [], 0
    while True:
        line = response.readline(MAX_RECORD)
        if not line:
            if parts:
                raise UpstreamError('The provider stopped inside an event.')
            return
        line = line.rstrip(b'\r\n')
        if not line:
            if parts:
                data = b'\n'.join(parts)
                if data == b'[DONE]':
                    yield None
                else:
                    try:
                        item = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise UpstreamError('The provider sent invalid event JSON.') from None
                    if not isinstance(item, dict):
                        raise UpstreamError('The provider sent an invalid event.')
                    yield item
                parts, size = [], 0
        elif line.startswith(b'data:'):
            part = line[5:].lstrip(b' ')
            size += len(part)
            if size > MAX_RECORD:
                raise UpstreamError('The provider event exceeded its size limit.')
            parts.append(part)


def _tokens(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        raise UpstreamError('The provider returned invalid token usage.')
    return value


def _openai(response):
    prompt = completion = None
    finish = None
    calls = {}
    for item in _sse(response):
        if item is None:
            if finish is None:
                raise UpstreamError('The provider stopped without a finish reason.')
            yield Chunk(done=True, finish_reason=finish, prompt_tokens=prompt, completion_tokens=completion,
                        tool_calls=[{'name': c['name'], 'arguments': c['arguments']} for c in calls.values()])
            return
        if 'error' in item:
            raise UpstreamError('The provider reported a stream error.')
        usage = item.get('usage')
        if usage is not None:
            if not isinstance(usage, dict):
                raise UpstreamError('Invalid usage record.')
            prompt, completion = _tokens(usage['prompt_tokens']), _tokens(usage['completion_tokens'])
        choices = item.get('choices', [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise UpstreamError('Unexpected completion choices.')
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta', {})
        text, thinking = delta.get('content') or '', delta.get('reasoning_content') or delta.get('reasoning') or ''
        if not isinstance(text, str) or not isinstance(thinking, str) or finish is not None and (text or thinking):
            raise UpstreamError('Invalid completion delta.')
        for call in delta.get('tool_calls', []):
            index = call.get('index')
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 32:
                raise UpstreamError('Invalid tool call index.')
            stored = calls.setdefault(index, {'name': '', 'arguments': ''})
            function = call.get('function', {})
            for key in stored:
                value = function.get(key) or ''
                if not isinstance(value, str):
                    raise UpstreamError('Invalid tool delta.')
                stored[key] += value
                if len(stored[key]) > MAX_RECORD:
                    raise UpstreamError('Tool call exceeds size limit.')
        reason = choice.get('finish_reason')
        if reason is not None:
            if reason not in ('stop', 'length', 'content_filter', 'tool_calls', 'function_call'):
                raise UpstreamError('Unsupported provider finish reason.')
            finish = 'stop' if reason in ('tool_calls', 'function_call') else reason
        if text or thinking:
            yield Chunk(content=text, thinking=thinking)
    raise UpstreamError('The provider stopped without its terminal event.')


def _anthropic(response):
    prompt = completion = None
    finish = None
    calls = {}
    for item in _sse(response):
        if item is None or item.get('type') == 'error':
            raise UpstreamError('The provider reported a stream error.')
        kind = item.get('type')
        if kind == 'message_start':
            usage = item['message']['usage']
            if 'input_tokens' not in usage:
                raise UpstreamError('The provider omitted input usage.')
            prompt = sum(_tokens(usage.get(key, 0)) for key in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
            if prompt > MAX_TOKENS:
                raise UpstreamError('Input usage exceeds its limit.')
            completion = _tokens(usage.get('output_tokens', 0))
        elif kind == 'content_block_start':
            block = item.get('content_block', {})
            if block.get('type') == 'tool_use':
                index = item.get('index')
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 32:
                    raise UpstreamError('Invalid tool call index.')
                calls[index] = {'name': block['name'], 'arguments': ''}
        elif kind == 'content_block_delta':
            if finish is not None:
                raise UpstreamError('Output followed the provider finish marker.')
            delta = item.get('delta', {})
            text, thinking = delta.get('text', ''), delta.get('thinking', '')
            if not isinstance(text, str) or not isinstance(thinking, str):
                raise UpstreamError('Invalid message delta.')
            if delta.get('type') == 'input_json_delta':
                stored = calls.get(item.get('index'))
                fragment = delta.get('partial_json', '')
                if stored is None or not isinstance(fragment, str):
                    raise UpstreamError('Invalid tool call delta.')
                stored['arguments'] += fragment
                if len(stored['arguments']) > MAX_RECORD:
                    raise UpstreamError('Tool call exceeds size limit.')
            if text or thinking:
                yield Chunk(content=text, thinking=thinking)
        elif kind == 'message_delta':
            reason = item.get('delta', {}).get('stop_reason')
            if reason not in ('end_turn', 'stop_sequence', 'max_tokens', 'tool_use', 'refusal'):
                raise UpstreamError('Invalid provider stop reason.')
            finish = 'length' if reason == 'max_tokens' else 'content_filter' if reason == 'refusal' else 'stop'
            completion = _tokens(item['usage']['output_tokens'])
        elif kind == 'message_stop':
            if finish is None or prompt is None:
                raise UpstreamError('The provider omitted terminal status or usage.')
            yield Chunk(done=True, finish_reason=finish, prompt_tokens=prompt, completion_tokens=completion,
                        tool_calls=list(calls.values()))
            return
    raise UpstreamError('The provider stopped without its terminal event.')


def stream(model, messages, *, options=None, effort=None, tools=None, cancel=None):
    provider = store.get(model['external_provider_id'])
    if provider is None or not provider['enabled']:
        raise UpstreamError('This external provider is disabled.')
    options = options or {}
    protocol = provider['protocol']
    try:
        converted, system = _messages(messages, protocol)
        body = {'model': model['backend_model_name'], 'messages': converted, 'stream': True}
        if tools:
            body['tools'] = ([{'name': tool['function']['name'], 'description': tool['function'].get('description', ''),
                               'input_schema': tool['function'].get('parameters', {'type': 'object'})} for tool in tools]
                             if protocol == 'anthropic' else tools)
        supported = json.loads(model['reasoning_levels'])
        if effort is not None and effort not in supported:
            raise UpstreamError('This external model does not support the selected reasoning level.')
        if options.get('stop'):
            body['stop_sequences' if protocol == 'anthropic' else 'stop'] = options['stop']
        if protocol == 'anthropic':
            body['max_tokens'] = options.get('num_predict', 4096)
            if system:
                body['system'] = system
            if effort and effort != 'off':
                body['thinking'] = {'type': 'adaptive'}
                body['output_config'] = {'effort': 'xhigh' if effort == 'extra' else effort}
            for key in ('temperature', 'top_p'):
                if key in options and not body.get('thinking'):
                    body[key] = options[key]
            path, parser = '/messages', _anthropic
        else:
            if provider['stream_usage']:
                body['stream_options'] = {'include_usage': True}
            for key in ('temperature', 'top_p'):
                if key in options and not effort:
                    body[key] = options[key]
            if options.get('num_predict'):
                parameter = provider['token_parameter']
                if parameter == 'auto':
                    parameter = 'max_completion_tokens' if urlsplit(provider['base_url']).hostname == 'api.openai.com' else 'max_tokens'
                body[parameter] = options['num_predict']
            if effort:
                body['reasoning_effort'] = 'xhigh' if effort == 'extra' else 'none' if effort == 'off' else effort
            path, parser = '/chat/completions', _openai
        payload = json.dumps(body).encode('utf-8')
        if len(payload) > 32 * 1024 * 1024:
            raise UpstreamError('The external request exceeded its size limit.')
        with _open(provider, 'POST', path, body=payload, cancel=cancel) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', '').lower():
                raise UpstreamError('The provider did not return an event stream.')
            for chunk in parser(response):
                fresh = store.get(provider['id'])
                if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
                    raise UpstreamError('The external provider configuration changed during this request.')
                yield chunk
    except Cancelled:
        raise
    except Exception as error:
        if getattr(error, 'status', None) == 404 and getattr(error, 'code', None) in ('model_not_found', 'model_not_supported'):
            catalog.set_lifecycle(model['id'], backend_available=0, missing_at=db.now(),
                                  missing_reason='The API provider rejected this model as unavailable.')
        raise _error(error) from None
