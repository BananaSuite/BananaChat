"""OpenAI-compatible and Anthropic HTTP protocols with pinned endpoints and bounded SSE.

Two dialects cover the supported services: OpenAI Chat Completions (OpenAI,
Azure OpenAI, OpenRouter, Google Gemini, Mistral, Groq, DeepSeek, xAI, Together,
Fireworks and local servers such as LM Studio, vLLM, llama.cpp or LiteLLM) and
native Anthropic Messages. ``services.provider_presets`` fills in each service's
connection; small differences (how reasoning effort and the output limit are
sent, where usage and reasoning text arrive) are handled here. For agents, a
streamed Claude turn is kept as a record (``Finished.replay``) and sent back
unchanged with its tool results, signed thinking included, while Claude still
accepts it (:func:`_replay`).

Errors become :class:`UpstreamError` with a ``provider_*`` code and a fixed,
credential-free message. ``detail`` keeps the provider's own message, with
the key and key-like strings removed, for administrators only.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import re
import socket
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ResolverTimeout
from urllib.parse import quote, urlsplit, urlunsplit

from flask import current_app

from bananachat import db
from bananachat.db import catalog, external_providers as store
from bananachat.services import provider_presets, provider_secrets, supervisor
from bananachat.services.ollama import TOO_DEEP, Chunk, nesting_too_deep
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError, open_request

log = logging.getLogger("bananachat.providers")

MAX_MODELS = 2000
MAX_RECORD = 1024 * 1024
MAX_TOKENS = 1_000_000_000
ANTHROPIC_VERSION = '2023-06-01'
PROBE_PROMPT = 'Reply with the single word OK.'
PROBE_TOKENS = 32
# The result sent for a tool call that has none in the history (the run ended before it ran).
TOOL_NOT_RUN = 'Not run: the run stopped before this call.'
# How often a streaming answer rereads its provider's row (disabled or edited meanwhile: the answer ends).
PROVIDER_RECHECK_SECONDS = 2.0
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

# Extra headers are visible administrator settings, never a place for credentials.
_HEADER_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z')
RESERVED_HEADERS = frozenset({
    'authorization', 'proxy-authorization', 'x-api-key', 'api-key', 'x-goog-api-key', 'ocp-apim-subscription-key',
    'cookie', 'host', 'content-length', 'content-type', 'transfer-encoding', 'connection', 'keep-alive', 'te',
    'upgrade', 'expect', 'accept', 'accept-encoding', 'user-agent', 'anthropic-version'})
MAX_EXTRA_HEADERS = 10

# Reasoning effort levels (services.limits) in each API's terms.
OPENAI_EFFORT = {'off': 'none', 'low': 'low', 'medium': 'medium', 'high': 'high', 'extra': 'xhigh', 'max': 'xhigh'}
ANTHROPIC_EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high', 'extra': 'xhigh', 'max': 'max'}
# Thinking budgets for Claude models that predate adaptive thinking; max_tokens grows by the budget.
THINKING_BUDGETS = {'low': 2048, 'medium': 6144, 'high': 12288, 'extra': 20480, 'max': 32000}
# Signed thinking sent back within one tool round (thinking text, signatures and redacted data, in characters).
# Claude counts that reasoning against its context window, so past this the round continues without thinking.
REPLAY_THINKING_CHARS = 300_000
_THINKING_BLOCKS = ('thinking', 'redacted_thinking')
_TOOL_USE_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')

MESSAGES = {
    'provider_auth_failed': 'The provider rejected the API key. Check the key in the provider settings.',
    'provider_forbidden': 'The provider refused access with this key. Check its permissions and model access.',
    'provider_quota_exhausted': 'The provider account has no remaining credit or quota.',
    'provider_model_not_found': 'The provider does not offer this model.',
    'provider_not_found': 'The provider endpoint was not found. Check the base URL.',
    'provider_rate_limited': 'The provider is rate limiting requests.',
    'provider_overloaded': 'The provider is overloaded.',
    'provider_unavailable': 'The provider had a server error.',
    'provider_rejected_request': 'The provider rejected the request.',
    'provider_timeout': 'The provider did not answer in time.',
    'provider_unreachable': 'The provider could not be reached.',
    'provider_stream_error': 'The provider reported an error during the answer.',
    'provider_protocol_error': 'The provider sent a response that could not be read.',
    'provider_key_unreadable': provider_secrets.UNREADABLE,
    'provider_disabled': 'This external provider is disabled.',
}
_CAPACITY = ('provider_rate_limited', 'provider_overloaded')
_KEYLIKE = re.compile(r'\b(?:sk|pk|rk|gsk|xai|key|api)[-_][A-Za-z0-9*._\-]{8,}|\bAIza[0-9A-Za-z_\-]{20,}', re.I)


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


def parse_headers(text):
    """Extra request headers from ``Name: value`` lines (administrator input). Raises ValueError."""
    headers = {}
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(':')
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise ValueError('Write each extra header on its own line as “Name: value”.')
        if name.lower() in RESERVED_HEADERS:
            raise ValueError(f'{name} cannot be an extra header. Put the provider key in the API key field.')
        if not value or len(value) > 500 or any(not 32 <= ord(char) <= 126 for char in value):
            raise ValueError(f'The value of {name} must be printable ASCII text of at most 500 characters.')
        if name.lower() in {existing.lower() for existing in headers}:
            raise ValueError(f'{name} is listed twice.')
        headers[name] = value
    if len(headers) > MAX_EXTRA_HEADERS:
        raise ValueError(f'Use at most {MAX_EXTRA_HEADERS} extra headers.')
    return headers


def _extra_headers(provider):
    try:
        stored = json.loads(provider['extra_headers'] or '{}')
        return parse_headers('\n'.join(f'{name}: {value}' for name, value in stored.items()))
    except (ValueError, TypeError, AttributeError, IndexError, KeyError):
        return {}


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
        # ::1 lies in the IPv4-compatible ::/96 and reserved ::/8 blocks: it is a private (loopback) address.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is None and (
                ip.sixtofour is not None or ip.teredo is not None
                or any(ip in network for network in _NAT64_NETWORKS)
                or (ip in _IPV4_COMPATIBLE and not ip.is_loopback)):
            raise UpstreamError('The provider resolves to a restricted address. Use a public endpoint or explicitly '
                                'allow a trusted private server.', code='provider_address_refused')
        ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if ip in _METADATA_ADDRESSES or ip.is_link_local or ip.is_multicast or ip.is_unspecified \
                or (ip.is_reserved and not ip.is_loopback) or (not ip.is_global and not allow_private):
            raise UpstreamError('The provider resolves to a restricted address. Use a public endpoint or explicitly '
                                'allow a trusted private server.', code='provider_address_refused')
        if family in (socket.AF_INET, socket.AF_INET6):
            approved.append((family, socktype, proto, address))
    if not approved:
        raise UpstreamError('The provider hostname did not resolve to a usable address.', kind='connect')
    return approved


def _connection_factory(provider, cancel=None):
    def factory(url, timeout):
        parts = urlsplit(base_url(url, allow_private=bool(provider['allow_private'])))
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        approved = _addresses(parts.hostname, port, bool(provider['allow_private']))
        if parts.scheme == 'http' and any(ipaddress.ip_address(address[0]).is_global for _, _, _, address in approved):
            raise UpstreamError('Public API providers require HTTPS, including when private endpoints are allowed.',
                                code='provider_address_refused')
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


def _key(provider):
    try:
        return provider_secrets.load(provider['secret_ref'])
    except (ValueError, OSError):
        raise UpstreamError(MESSAGES['provider_key_unreadable'], code='provider_key_unreadable') from None


def _headers(provider):
    key = _key(provider)
    headers = _extra_headers(provider)
    if provider['protocol'] == 'anthropic':
        headers['anthropic-version'] = ANTHROPIC_VERSION
    if key:
        style = provider['auth_style']
        if style == 'x-api-key':
            headers['x-api-key'] = key
        elif style == 'api-key':
            headers['api-key'] = key
        else:
            headers['Authorization'] = 'Bearer ' + key
    return headers


def redact(text, provider=None):
    """A provider message without the key or anything that looks like one, on one short line."""
    text = str(text or '')
    if provider is not None:
        try:
            key = provider_secrets.load(provider['secret_ref'])
        except (ValueError, OSError):
            key = ''
        if key:
            text = text.replace(key, '[redacted]')
    text = _KEYLIKE.sub('[redacted]', text)
    text = ''.join(char if char.isprintable() else ' ' for char in text)
    return ' '.join(text.split())[:300]


def _model_specific(code, error_type, message):
    lowered = (message or '').lower()
    return code in ('model_not_found', 'model_not_supported') or (
        error_type == 'not_found_error' and lowered.startswith('model')) or (
        'model' in lowered and any(words in lowered for words in ('not found', 'does not exist', 'no endpoints',
                                                                   'not available', 'unknown model')))


def _classify(error):
    """The ``(code, kind)`` of an HTTP error from a provider."""
    status = error.status
    code = (error.code or '').lower()
    error_type = (error.error_type or '').lower()
    message = str(error).lower()
    # Anthropic reports an empty balance as HTTP 400 "Your credit balance is too low ...".
    if status == 402 or error_type == 'billing_error' or code in ('insufficient_quota', 'billing_hard_limit_reached',
                                                                    'insufficient_credits') \
            or any(words in message for words in ('credit balance', 'insufficient credit', 'exceeded your current quota')):
        return 'provider_quota_exhausted', None
    if status == 401:
        return 'provider_auth_failed', None
    if status == 403:
        return 'provider_forbidden', None
    if status == 404:
        return ('provider_model_not_found' if _model_specific(code, error_type, str(error)) else 'provider_not_found',
                None)
    if status == 429 or error_type == 'rate_limit_error':
        return 'provider_rate_limited', 'capacity'
    if status == 529 or error_type == 'overloaded_error':
        return 'provider_overloaded', 'capacity'
    if status == 408:
        return 'provider_timeout', 'timeout'
    if status is not None and status >= 500:
        return 'provider_unavailable', None
    return 'provider_rejected_request', None


def _error(error, provider=None):
    """A credential-free :class:`UpstreamError` with a ``provider_*`` code for any failure of a provider call."""
    if isinstance(error, UpstreamError) and (error.code or '').startswith('provider_'):
        error.detail = redact(error.detail, provider) if error.detail else ''
        return error
    if not isinstance(error, UpstreamError):
        # Exception texts can quote request data; only the type is logged.
        log.warning('Unexpected %s while reading an external provider response', type(error).__name__)
        return UpstreamError(MESSAGES['provider_protocol_error'], code='provider_protocol_error')
    if error.status is not None:
        code, kind = _classify(error)
        return UpstreamError(f'{MESSAGES[code]} (HTTP {error.status})', status=error.status, code=code, kind=kind,
                             retry_after=error.retry_after, error_type=error.error_type,
                             detail=redact(str(error), provider))
    if error.kind == 'timeout':
        return UpstreamError(MESSAGES['provider_timeout'], code='provider_timeout', kind='timeout')
    if error.kind == 'connect':
        return UpstreamError(MESSAGES['provider_unreachable'], code='provider_unreachable', kind='connect',
                             detail=redact(str(error), provider))
    # Our own protocol checks: their texts never contain provider data.
    return UpstreamError(str(error) or MESSAGES['provider_protocol_error'], code='provider_protocol_error')


def _stream_error(record):
    """An error reported inside an event stream (OpenAI ``error`` objects, Anthropic ``error`` events)."""
    record = record if isinstance(record, dict) else {}
    error_type = record.get('type') if isinstance(record.get('type'), str) else ''
    code = record.get('code') if isinstance(record.get('code'), str) else ''
    if error_type == 'overloaded_error' or code in ('overloaded', 'overloaded_error'):
        name, kind = 'provider_overloaded', 'capacity'
    elif error_type == 'rate_limit_error' or code in ('rate_limit_exceeded', 'rate_limit'):
        name, kind = 'provider_rate_limited', 'capacity'
    else:
        name, kind = 'provider_stream_error', None
    message = record.get('message') if isinstance(record.get('message'), str) else ''
    return UpstreamError(MESSAGES[name], code=name, kind=kind, error_type=error_type or None, detail=message[:500])


@contextmanager
def _open(provider, method, path, *, body=None, cancel=None, timeouts=None):
    """``timeouts``: ``(first byte, gap between reads, total)``; a generation uses the configured limits."""
    config = current_app.config['BC']
    own_cancel = cancel is None
    cancel = cancel or CancelToken()
    if timeouts is None:
        # Reasoning models can stay silent for minutes before (and between) visible tokens.
        wait = min(config.first_token_timeout, config.generation_timeout)
        timeouts = (wait, wait, config.generation_timeout)
    first_byte, gap, budget = timeouts
    handle = supervisor.cancel_at(time.monotonic() + budget, cancel)
    try:
        with open_request(method, provider['base_url'], path, headers=_headers(provider), body=body, cancel=cancel,
                          connect_timeout=10, first_byte_timeout=first_byte, read_timeout=gap,
                          total_timeout=budget, max_bytes=16 * 1024 * 1024,
                          connection_factory=_connection_factory(provider, cancel)) as response:
            yield response
    except Cancelled:
        if own_cancel:
            raise UpstreamError(MESSAGES['provider_timeout'], code='provider_timeout', kind='timeout') from None
        raise
    finally:
        supervisor.clear_deadline(handle)


_DISCOVERY = (15, 15, 30)


def _get_json(provider, path, cancel):
    with _open(provider, 'GET', path, cancel=cancel, timeouts=_DISCOVERY) as response:
        return response.json(5 * 1024 * 1024)


def discover(provider):
    """The provider's models as normalized entries (see :func:`_model_entry`).

    A failed/incomplete listing changes no availability or enrollment state.
    """
    models, seen = [], set()
    cancel = CancelToken()
    deadline = supervisor.cancel_at(time.monotonic() + 30, cancel)
    path = '/models?limit=100' if provider['protocol'] == 'anthropic' else '/models'
    try:
        for _ in range(20):
            data = _get_json(provider, path, cancel)
            # Together.ai answers with a bare list; everyone else with {"data": [...]}.
            items = data if isinstance(data, list) else data.get('data') if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise UpstreamError('Invalid model list.')
            for item in items:
                entry = _model_entry(item, provider['protocol'])
                if provider['preset'] == 'gemini' and entry['id'].startswith('models/') and len(entry['id']) > 7:
                    # Gemini lists resource names; its chat endpoint takes the bare model ID.
                    entry['id'] = entry['id'][len('models/'):]
                    entry['display'] = entry['id'] if entry['display'].startswith('models/') else entry['display']
                if entry['id'] not in seen:
                    models.append(entry)
                    seen.add(entry['id'])
                if len(models) > MAX_MODELS:
                    raise UpstreamError('Model list too large.')
            if provider['protocol'] != 'anthropic' or not data.get('has_more'):
                return _with_native_details(provider, models, cancel)
            last = data.get('last_id')
            if not isinstance(last, str) or not MODEL_ID.fullmatch(last) or not items:
                raise UpstreamError('Invalid model pagination.')
            path = '/models?limit=100&after_id=' + quote(last, safe='')
        raise UpstreamError('Model listing exceeded its page limit.')
    except Cancelled:
        raise UpstreamError(MESSAGES['provider_timeout'], code='provider_timeout', kind='timeout') from None
    except Exception as error:
        raise _error(error, provider) from None
    finally:
        supervisor.clear_deadline(deadline)


# ----- what local servers add to their OpenAI-compatible model list ---------------------------------------------

_LM_STUDIO_LEVELS = ('off', 'low', 'medium', 'high')


def _lm_studio_details(data):
    """LM Studio's own model list (``/api/v1/models`` from 0.4, ``/api/v0/models`` before) by model ID."""
    details = {}
    if isinstance(data, dict) and isinstance(data.get('models'), list):  # 0.4 and later
        for item in data['models']:
            if not isinstance(item, dict) or not isinstance(item.get('key'), str) \
                    or item.get('type') not in ('llm', 'embedding', 'decision'):
                continue
            capabilities = item.get('capabilities') if isinstance(item.get('capabilities'), dict) else {}
            loaded = [instance.get('config') for instance in item.get('loaded_instances') or []
                      if isinstance(instance, dict) and isinstance(instance.get('config'), dict)]
            context = next((value for value in (_count(config.get('context_length')) for config in loaded) if value),
                           None) or _count(item.get('max_context_length'))
            reasoning = capabilities.get('reasoning') if isinstance(capabilities.get('reasoning'), dict) else {}
            allowed = reasoning.get('allowed_options') if isinstance(reasoning.get('allowed_options'), list) else []
            levels = [level for level in _LM_STUDIO_LEVELS if level in allowed]
            details[item['key']] = {
                'chat': item.get('type') == 'llm', 'context': context,
                'vision': capabilities['vision'] if isinstance(capabilities.get('vision'), bool) else None,
                'tools': capabilities['trained_for_tool_use']
                if isinstance(capabilities.get('trained_for_tool_use'), bool) else None,
                'reasoning': levels if len(levels) > 2 else None,
                'display': item['display_name'] if isinstance(item.get('display_name'), str) else None}
    elif isinstance(data, dict) and isinstance(data.get('data'), list):  # 0.3: type llm, vlm or embeddings
        for item in data['data']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                continue
            kind = item.get('type')
            if kind not in ('llm', 'vlm', 'embeddings'):
                continue  # not LM Studio's answer (another server behind the preset)
            details[item['id']] = {'chat': kind != 'embeddings', 'vision': kind == 'vlm',
                                   'context': _count(item.get('max_context_length'))}
    return details


def _llama_cpp_details(data):
    """llama.cpp's ``/props``: the context the server was started with (``-c``) and whether it reads images."""
    if not isinstance(data, dict):
        return {}
    settings = data.get('default_generation_settings')
    context = _count(settings.get('n_ctx')) if isinstance(settings, dict) else None
    modalities = data.get('modalities') if isinstance(data.get('modalities'), dict) else {}
    vision = modalities.get('vision') if isinstance(modalities.get('vision'), bool) else None
    return {'*': {'chat': True, 'context': context, 'vision': vision}}


def _server_root(provider):
    """The provider with its address at the server's root (``…/v1`` removed), for the server's own API."""
    root = dict(provider)
    base = (provider['base_url'] or '').rstrip('/')
    if not base.endswith('/v1'):
        return None
    root['base_url'] = base[:-3]
    return root


def _with_native_details(provider, models, cancel):
    """Complete a local server's listing with what its own API says (context length, vision, embeddings...).

    Optional: an older server, another product behind the same preset, or any failure leaves the listing as is."""
    preset = provider['preset'] if 'preset' in provider.keys() else ''
    if preset not in ('lmstudio', 'llamacpp') or not models:
        return models
    root = _server_root(provider)
    if root is None:
        return models
    details = {}
    paths = ('/api/v1/models', '/api/v0/models') if preset == 'lmstudio' else ('/props',)
    for path in paths:
        try:
            data = _get_json(root, path, cancel)
            details = _lm_studio_details(data) if preset == 'lmstudio' else _llama_cpp_details(data)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001 - the extra details are optional (and may be malformed)
            log.debug('%s%s did not answer usably for provider %s: %s', provider['preset'], path, provider['id'],
                      type(error).__name__)
            details = {}
            continue
        if details:
            break
    for entry in models:
        found = details.get(entry['id']) or details.get('*')
        if not found:
            continue
        for key in ('chat', 'context', 'vision', 'tools', 'reasoning'):
            if found.get(key) is not None:
                entry[key] = found[key]
        if found.get('display') and entry['display'] == entry['id']:
            entry['display'] = ' '.join(found['display'].split())[:120]
    return models


def check_key(provider):
    """Validate the key where the model list is public (OpenRouter); returns the provider's key details or None."""
    path = provider_presets.get(provider['preset']).key_check
    if not path:
        return None
    try:
        data = _get_json(provider, path, None)
    except Exception as error:
        raise _error(error, provider) from None
    details = data.get('data') if isinstance(data, dict) else None
    return details if isinstance(details, dict) else {}


# ----- model metadata ------------------------------------------------------------------------------------------

_NOT_CHAT = ('embed', 'tts', 'whisper', 'transcrib', 'moderation', 'dall-e', 'image', 'realtime', 'audio', 'rerank',
             'speech', 'babbage', 'davinci')


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100_000_000 else None


def _supported(tree, *path):
    for name in path:
        tree = tree.get(name) if isinstance(tree, dict) else None
    return tree.get('supported') if isinstance(tree, dict) and isinstance(tree.get('supported'), bool) else None


def _price(value, per_token):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number * 1_000_000 if per_token else number, 4)


def _pricing(item):
    """USD per million input/output tokens, when the listing says (OpenRouter: per token; Together: per million)."""
    pricing = item.get('pricing')
    if not isinstance(pricing, dict):
        return None
    if 'prompt' in pricing:
        values = _price(pricing.get('prompt'), True), _price(pricing.get('completion'), True)
    else:
        values = _price(pricing.get('input'), False), _price(pricing.get('output'), False)
    return {'input': values[0], 'output': values[1]} if None not in values else None


def _model_entry(item, protocol):
    """One listed model: ``id``, ``display`` and what the listing says (``None``: not stated).

    ``chat`` (answers conversations), ``context`` and ``max_output`` (tokens), ``vision``, ``tools``,
    ``reasoning`` (suggested effort levels), ``thinking`` (``adaptive`` or ``budget``, Anthropic) and
    ``pricing`` (USD per million tokens).
    """
    if not isinstance(item, dict):
        raise UpstreamError('Invalid model entry.')
    name = item.get('id')
    if not isinstance(name, str) or not MODEL_ID.fullmatch(name):
        raise UpstreamError('Invalid model ID.')
    label = item.get('display_name') or item.get('name')
    display = ' '.join(str(label).split())[:120] if isinstance(label, str) and label.strip() else name[:120]
    top = item.get('top_provider') if isinstance(item.get('top_provider'), dict) else {}
    context = next((value for value in map(_count, (
        item.get('context_length'), item.get('context_window'), item.get('max_context_length'),
        item.get('max_model_len'), item.get('max_input_tokens'), top.get('context_length'))) if value), None)
    if protocol == 'anthropic':
        max_output = _count(item.get('max_tokens'))
    else:
        max_output = _count(item.get('max_completion_tokens')) or _count(top.get('max_completion_tokens')) \
            or _count(item.get('max_output_tokens'))
    capabilities = item.get('capabilities') if isinstance(item.get('capabilities'), dict) else {}
    architecture = item.get('architecture') if isinstance(item.get('architecture'), dict) else {}
    inputs = architecture.get('input_modalities')
    outputs = architecture.get('output_modalities')
    parameters = item.get('supported_parameters')
    entry = {'id': name, 'display': display, 'context': context, 'max_output': max_output, 'vision': None,
             'tools': None, 'reasoning': None, 'thinking': None, 'pricing': _pricing(item)}
    if protocol == 'anthropic':
        entry.update(chat=True, tools=True, vision=_supported(capabilities, 'image_input'))
        if _supported(capabilities, 'thinking', 'types', 'adaptive'):
            entry['thinking'] = 'adaptive'
            entry['reasoning'] = [level for level, name in (('low', 'low'), ('medium', 'medium'), ('high', 'high'),
                                                             ('extra', 'xhigh'), ('max', 'max'))
                                  if _supported(capabilities, 'effort', name)]
        elif _supported(capabilities, 'thinking', 'types', 'enabled'):
            entry.update(thinking='budget', reasoning=['off', 'low', 'medium', 'high'])
        return entry
    if isinstance(inputs, list):
        entry['vision'] = 'image' in inputs
    elif isinstance(capabilities.get('vision'), bool):
        entry['vision'] = capabilities['vision']
    if isinstance(parameters, list):
        entry['tools'] = 'tools' in parameters
        if 'reasoning' in parameters:
            entry['reasoning'] = ['off', 'low', 'medium', 'high']
    elif isinstance(capabilities.get('function_calling'), bool):
        entry['tools'] = capabilities['function_calling']
    if isinstance(outputs, list):
        entry['chat'] = 'text' in outputs
    elif isinstance(item.get('type'), str):
        entry['chat'] = item['type'] == 'chat'
    elif isinstance(capabilities.get('completion_chat'), bool):
        entry['chat'] = capabilities['completion_chat']
    else:
        lowered = name.lower()
        entry['chat'] = not any(word in lowered for word in _NOT_CHAT)
    return entry


# ----- requests ------------------------------------------------------------------------------------------------

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


def _arguments(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) and value.strip() else {}
    except ValueError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _messages(messages, protocol):
    converted, system, _records = _convert(messages, protocol)
    return converted, system


def _convert(messages, protocol):
    """*messages* in the provider's terms: ``(messages, system, (records, round_start))``.

    An assistant turn that carries the ``replay`` record of a streamed Claude turn (agents keep them, see
    :func:`_anthropic`) goes back block for block with the provider's own tool ids; ``records`` lists the
    positions of those turns and ``round_start`` where the current tool round begins (after the last user
    message). :func:`_replay` then decides which signed thinking blocks are kept.

    Both APIs refuse a tool call without a result and a result without a call. A stored agent history can have
    either (a run stopped between the calls of one step, a history window that begins with a result), so calls
    left unanswered get :data:`TOOL_NOT_RUN` before the next turn and results without a call are left out.
    """
    result, system, pending = [], [], []
    records, round_start = [], 0

    def answer(call_id, content):
        if protocol != 'anthropic':
            result.append({'role': 'tool', 'content': content, 'tool_call_id': call_id})
            return
        block = {'type': 'tool_result', 'tool_use_id': call_id, 'content': content}
        previous = result[-1] if result else None
        # Parallel tool results belong in one user turn.
        if previous and previous['role'] == 'user' and isinstance(previous['content'], list) and \
                previous['content'] and all(item.get('type') == 'tool_result' for item in previous['content']):
            previous['content'].append(block)
        else:
            result.append({'role': 'user', 'content': [block]})

    for index, message in enumerate(messages):
        role = message.get('role')
        content = message.get('content') or ''
        if role not in ('system', 'user', 'assistant', 'tool') or not isinstance(content, str):
            raise UpstreamError('Unsupported conversation format.')
        if role != 'tool':
            for _name, call_id in pending:
                answer(call_id, TOOL_NOT_RUN)
            pending = []
        if protocol == 'anthropic' and role == 'system':
            if content:
                system.append(content)
            continue
        item = {'role': role, 'content': content}
        images = message.get('images') or []
        if images:
            blocks = [{'type': 'text', 'text': content}] if content or protocol != 'anthropic' else []
            for image in images:
                mime = _image(image)
                blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': image}}
                              if protocol == 'anthropic' else {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image}'}})
            item['content'] = blocks
        record = message.get('replay') if protocol == 'anthropic' and role == 'assistant' else None
        if record is not None and not _replay_matches(record, message.get('tool_calls') or []):
            record = None
        if record is not None:
            item['content'] = [dict(block) for block in record['content']]
            if message.get('tool_calls'):
                pending = [(block['name'], block['id']) for block in item['content'] if block['type'] == 'tool_use']
        elif role == 'assistant' and message.get('tool_calls'):
            calls = []
            for position, call in enumerate(message['tool_calls']):
                function = call.get('function', call)
                call_id = f'bc_call_{index}_{position}'
                pending.append((function.get('name'), call_id))
                args = function.get('arguments', {})
                if protocol == 'anthropic':
                    calls.append({'type': 'tool_use', 'id': call_id, 'name': function['name'], 'input': _arguments(args)})
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
            if found is not None:
                pending.remove(found)
                answer(found[1], item['content'])
            continue
        if protocol == 'anthropic' and not item['content']:
            continue  # Claude refuses empty turns; consecutive turns of one role are merged by the API.
        result.append(item)
        if record is not None:
            records.append((len(result) - 1, record))
        elif role == 'user':
            round_start = len(result)
    for _name, call_id in pending:
        answer(call_id, TOOL_NOT_RUN)
    return result, '\n\n'.join(system), (records, round_start)


def _replay_matches(record, calls):
    """Whether *record* is a streamed Claude turn (:func:`_anthropic`) that asked for exactly these tool calls."""
    if not isinstance(record, dict) or record.get('protocol') != 'anthropic' or not isinstance(record.get('prefix'), str):
        return False
    content = record.get('content')
    if not isinstance(content, list) or not all(isinstance(block, dict) and block.get('type') in
                                                (*_THINKING_BLOCKS, 'text', 'tool_use') for block in content):
        return False
    return [block.get('name') for block in content if block['type'] == 'tool_use'] == \
        [call.get('function', call).get('name') for call in calls]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'


def _replay_header(provider, name, system, tools):
    """The start of a request's prefix digest: everything Claude binds a thinking block to besides messages."""
    digest = hashlib.sha256()
    digest.update(_canonical({'provider': provider['id'], 'revision': provider['revision'], 'model': name,
                              'system': system, 'tools': tools}))
    return digest


def _replay(converted, records, round_start, header, thinking):
    """Keep the signed thinking of the current tool round where Claude accepts it; drop every other block.

    Claude needs the thinking blocks of a tool turn, unchanged, before it thinks again in the same round, and
    newer models bind each block to everything sent before it: the system prompt, the tools, the earlier
    messages and the previous thinking block. A record therefore stores a digest of all that as it was sent
    (``prefix``; the model and the provider's settings too), and its blocks are kept only while the same
    digest comes out here and every earlier block of the round was kept;
    a turn that fails, or a round whose thinking exceeds :data:`REPLAY_THINKING_CHARS`, loses its blocks from
    there on (dropping the last blocks or all of them is allowed, dropping some in the middle is not).
    Earlier rounds never keep theirs. Without a *thinking* request (``None``) no block is sent.

    Returns ``(messages, prefix, resumable)``: the messages to send, the digest for the turn this request
    produces, and whether the last assistant turn goes back exactly as streamed, so thinking may be requested.
    """
    records = dict(records)
    size = sum(len(block.get('thinking') or '') + len(block.get('signature') or '') + len(block.get('data') or '')
               for position, record in records.items() if position >= round_start
               for block in record['content'] if block['type'] in _THINKING_BLOCKS)
    keep = thinking is not None and size <= REPLAY_THINKING_CHARS
    digest = header.copy()
    result, resumable = [], False
    for position, message in enumerate(converted):
        record = records.get(position)
        if record is not None:
            kept = keep and position >= round_start and digest.hexdigest() == record['prefix']
            if not kept:
                keep = keep and position < round_start
                message = {**message, 'content': [block for block in message['content']
                                                  if block['type'] not in _THINKING_BLOCKS]}
                if not message['content']:
                    continue
            resumable = kept and (thinking != 'budget' or message['content'][0]['type'] in _THINKING_BLOCKS)
        elif message['role'] == 'assistant':
            resumable = False
        result.append(message)
        digest.update(_canonical(message))
    return result, digest.hexdigest(), resumable


def _tool_continuation(messages):
    """Whether the request continues a tool round: the last assistant turn asked for tools."""
    for message in reversed(messages):
        if message.get('role') == 'tool':
            continue
        return message.get('role') == 'assistant' and bool(message.get('tool_calls'))
    return False


def model_settings(model):
    """Per-model request options stored at enrollment (``external_config``)."""
    try:
        config = json.loads(model['external_config'] or '{}')
    except (TypeError, ValueError):
        config = {}
    config = config if isinstance(config, dict) else {}
    return {'sampling': config.get('sampling') if isinstance(config.get('sampling'), bool) else None,
            'thinking': 'budget' if config.get('thinking') == 'budget' else 'adaptive',
            'max_output': _count(config.get('max_output'))}


def _output_limit(options, settings):
    limit = options.get('num_predict')
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return None
    return min(limit, settings['max_output']) if settings['max_output'] else limit


def _token_parameter(provider):
    parameter = provider['token_parameter']
    if parameter != 'auto':
        return parameter
    preset = provider_presets.get(provider['preset'])
    if preset.key != 'custom':
        return preset.token_parameter
    return 'max_completion_tokens' if urlsplit(provider['base_url']).hostname == 'api.openai.com' else 'max_tokens'


def _openai_body(provider, name, converted, options, effort, tools, settings):
    body = {'model': name, 'messages': converted, 'stream': True}
    if tools:
        body['tools'] = tools
    if options.get('stop'):
        body['stop'] = options['stop']
    if provider['stream_usage']:
        body['stream_options'] = {'include_usage': True}
    # Reasoning models reject sampling parameters.
    if settings['sampling'] is not False and not effort:
        for key in ('temperature', 'top_p'):
            if key in options:
                body[key] = options[key]
    limit = _output_limit(options, settings)
    if limit:
        body[_token_parameter(provider)] = limit
    if effort:
        value = OPENAI_EFFORT.get(effort, 'medium')
        if provider_presets.get(provider['preset']).reasoning == 'openrouter':
            body['reasoning'] = {'effort': value}
        else:
            body['reasoning_effort'] = value
    return body


def _anthropic_tools(tools):
    return [{'name': tool['function']['name'], 'description': tool['function'].get('description', ''),
             'input_schema': tool['function'].get('parameters', {'type': 'object'})} for tool in tools or ()]


def _thinking_budget(options, effort, settings):
    """``(budget_tokens, max_tokens)`` for Claude models with a thinking budget."""
    limit = _output_limit(options, settings) or 4096
    budget = THINKING_BUDGETS.get(effort, THINKING_BUDGETS['medium'])
    ceiling = limit + budget
    if settings['max_output']:
        ceiling = min(ceiling, settings['max_output'])
    return min(budget, ceiling - 1), ceiling


def _thinking_mode(options, effort, settings):
    """The thinking a Claude request asks for when it does not continue a tool round: None, adaptive or budget."""
    if not effort or effort == 'off':
        return None
    if settings['thinking'] == 'budget':
        return 'budget' if _thinking_budget(options, effort, settings)[0] >= 1024 else None
    return 'adaptive'


def _anthropic_body(name, converted, system, messages, options, effort, tools, settings, *, resume=False):
    limit = _output_limit(options, settings) or 4096
    body = {'model': name, 'messages': converted, 'stream': True, 'max_tokens': limit}
    if system:
        body['system'] = system
    if tools:
        body['tools'] = _anthropic_tools(tools)
    if options.get('stop'):
        body['stop_sequences'] = options['stop']
    thinking = False
    if effort and effort != 'off':
        # Claude refuses a thinking request whose last tool turn lost its signed thinking blocks, so a tool
        # round asks for thinking again only when that turn goes back exactly as streamed (*resume*, _replay).
        continuation = _tool_continuation(messages) and not resume
        if settings['thinking'] == 'budget':
            budget, ceiling = _thinking_budget(options, effort, settings)
            if not continuation and budget >= 1024:
                body['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
                body['max_tokens'] = ceiling
                thinking = True
        else:
            body['output_config'] = {'effort': ANTHROPIC_EFFORT.get(effort, 'medium')}
            if not continuation:
                # Newer models return empty thinking text unless a summary is requested.
                body['thinking'] = {'type': 'adaptive', 'display': 'summarized'}
                thinking = True
    # Current Claude models refuse sampling parameters, and thinking never takes them; older models accept one.
    if settings['sampling'] and not thinking:
        for key in ('temperature', 'top_p'):
            if key in options:
                body[key] = options[key]
                break
    return body


# ----- streams -------------------------------------------------------------------------------------------------

def _sse(response):
    parts, size = [], 0
    while True:
        line = response.readline(MAX_RECORD)
        if not line:
            if parts and b'\n'.join(parts) != b'[DONE]':
                raise UpstreamError('The provider stopped inside an event.')
            if parts:
                yield None
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
                    except (ValueError, UnicodeError, RecursionError):
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


def _tool_calls(calls):
    """Collected calls as ``{"name", "arguments"}``; JSON arguments decoded for the agent history."""
    result = []
    for index in sorted(calls):
        call = calls[index]
        if not call['name']:
            continue
        raw = call['arguments']
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except (ValueError, RecursionError):
            arguments = None
        if isinstance(arguments, dict):
            if nesting_too_deep(arguments):
                result.append({'name': call['name'], 'arguments': {}, 'error': TOO_DEEP})
                continue
            result.append({'name': call['name'], 'arguments': arguments})
        else:
            result.append({'name': call['name'], 'arguments': raw})  # the agent engine reports invalid JSON
    return result


def _finish(reason):
    if reason in ('length', 'max_tokens', 'model_length', 'model_context_window_exceeded'):
        return 'length'
    return 'content_filter' if reason in ('content_filter', 'refusal') else 'stop'


def _usage(usage, prompt, completion):
    """OpenAI-style usage; reasoning tokens that some servers count only in ``total_tokens`` are included."""
    values = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        if usage.get(key) is not None:
            values[key] = _tokens(usage[key])
    prompt = values.get('prompt_tokens', prompt)
    completion = values.get('completion_tokens', completion)
    total = values.get('total_tokens')
    if total is not None and prompt is not None and total > prompt + (completion or 0):
        completion = total - prompt
    return prompt, completion


def _delta_text(delta):
    text = thinking = ''
    content = delta.get('content')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):  # Mistral reasoning models send typed chunks.
        for part in content:
            if not isinstance(part, dict):
                raise UpstreamError('Invalid completion delta.')
            if part.get('type') == 'text' and isinstance(part.get('text'), str):
                text += part['text']
            elif part.get('type') == 'thinking':
                inner = part.get('thinking')
                for piece in inner if isinstance(inner, list) else [{'text': inner}]:
                    if isinstance(piece, dict) and isinstance(piece.get('text'), str):
                        thinking += piece['text']
    elif content is not None:
        raise UpstreamError('Invalid completion delta.')
    reasoning = delta.get('reasoning_content') or delta.get('reasoning')
    if reasoning:
        if not isinstance(reasoning, str):
            raise UpstreamError('Invalid completion delta.')
        thinking += reasoning
    elif isinstance(delta.get('reasoning_details'), list):  # OpenRouter's structured reasoning, when alone
        for detail in delta['reasoning_details']:
            if isinstance(detail, dict):
                piece = detail.get('text') if detail.get('type') == 'reasoning.text' else \
                    detail.get('summary') if detail.get('type') == 'reasoning.summary' else None
                if isinstance(piece, str):
                    thinking += piece
    return text, thinking


def _openai(response):
    prompt = completion = None
    finish = None
    calls = {}
    for item in _sse(response):
        if item is None:
            break
        if item.get('error'):
            raise _stream_error(item['error'])
        usage = item.get('usage')
        groq = item.get('x_groq')
        if usage is None and isinstance(groq, dict):
            usage = groq.get('usage')
        reported = False
        if usage is not None:
            if not isinstance(usage, dict):
                raise UpstreamError('Invalid usage record.')
            prompt, completion = _usage(usage, prompt, completion)
            reported = True
        choices = item.get('choices') or []
        if not isinstance(choices, list) or len(choices) > 1:
            raise UpstreamError('Unexpected completion choices.')
        text = thinking = ''
        if choices:
            choice = choices[0]
            delta = choice.get('delta') if isinstance(choice, dict) else None
            delta = {} if delta is None and isinstance(choice, dict) else delta
            if not isinstance(delta, dict):
                raise UpstreamError('Invalid completion delta.')
            text, thinking = _delta_text(delta)
            if finish is not None and (text or thinking):
                raise UpstreamError('Output followed the provider finish marker.')
            for call in delta.get('tool_calls') or []:
                index = call.get('index') if isinstance(call, dict) else None
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 32:
                    raise UpstreamError('Invalid tool call index.')
                stored = calls.setdefault(index, {'name': '', 'arguments': ''})
                function = call.get('function') or {}
                for key in stored:
                    value = function.get(key) or ''
                    if not isinstance(value, str):
                        raise UpstreamError('Invalid tool delta.')
                    stored[key] += value
                    if len(stored[key]) > MAX_RECORD:
                        raise UpstreamError('Tool call exceeds size limit.')
            reason = choice.get('finish_reason')
            if reason is not None:
                if not isinstance(reason, str):
                    raise UpstreamError('Unsupported provider finish reason.')
                finish = _finish(reason)
        if text or thinking or reported:
            yield Chunk(content=text, thinking=thinking, prompt_tokens=prompt if reported else None,
                        completion_tokens=completion if reported else None)
    # [DONE], or the end of the stream after a finish reason (llama.cpp and some gateways omit [DONE]).
    if finish is None:
        raise UpstreamError('The provider stopped without a finish reason.')
    yield Chunk(done=True, finish_reason=finish, prompt_tokens=prompt, completion_tokens=completion,
                tool_calls=_tool_calls(calls))


_INPUT_KEYS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
_TURN_DELTAS = {'thinking_delta': ('thinking', 'thinking'), 'signature_delta': ('thinking', 'signature'),
                'text_delta': ('text', 'text')}


class _Turn:
    """A Claude turn as streamed, block for block, so it can go back with its signed thinking (see _replay).

    Only thinking, redacted thinking, text and tool calls are kept; any other block (server tools, citations,
    later additions) or a malformed one means the turn is not sent back as streamed.
    """

    def __init__(self):
        self.blocks, self.thinking, self.usable = {}, 0, True

    def _drop(self):
        self.blocks, self.usable = {}, False

    def start(self, index, block):
        if not self.usable:
            return
        if isinstance(index, bool) or not isinstance(index, int) or index in self.blocks or not isinstance(block, dict):
            return self._drop()
        kind = block.get('type')
        if kind == 'thinking':
            self.blocks[index] = {'type': kind, 'thinking': block.get('thinking') or '',
                                  'signature': block.get('signature') or ''}
        elif kind == 'redacted_thinking':
            self.blocks[index] = {'type': kind, 'data': block.get('data')}
        elif kind == 'text' and not block.get('citations'):
            self.blocks[index] = {'type': kind, 'text': block.get('text') or ''}
        elif kind == 'tool_use':
            self.blocks[index] = {'type': kind, 'id': block.get('id'), 'name': block.get('name')}
        else:
            return self._drop()
        self._grow(self.blocks[index], ('thinking', 'signature', 'data'))

    def delta(self, index, delta):
        if not self.usable:
            return
        kind = delta.get('type')
        block = self.blocks.get(index) if isinstance(index, int) and not isinstance(index, bool) else None
        if kind == 'input_json_delta' and block is not None and block['type'] == 'tool_use':
            return
        if kind not in _TURN_DELTAS or block is None or block['type'] != _TURN_DELTAS[kind][0]:
            return self._drop()
        field = _TURN_DELTAS[kind][1]
        value = delta.get(field)
        if not isinstance(value, str) or not isinstance(block[field], str):
            return self._drop()
        block[field] += value
        if field != 'text':
            self._grow({field: value}, (field,))

    def _grow(self, block, fields):
        self.thinking += sum(len(block.get(name) or '') for name in fields if isinstance(block.get(name), str))
        if self.thinking > REPLAY_THINKING_CHARS:  # it could never be sent back: keep nothing
            self._drop()

    def record(self, calls):
        """``{"protocol", "content"}`` for the agent history, or None; :func:`_stream` adds the ``prefix``."""
        if not self.usable:
            return None
        content = []
        for index in sorted(self.blocks):
            block = dict(self.blocks[index])
            kind = block['type']
            if kind == 'thinking' and not (block['signature'] and isinstance(block['thinking'], str)):
                return None
            if kind == 'redacted_thinking' and not (isinstance(block['data'], str) and block['data']):
                return None
            if kind == 'text':
                if not isinstance(block['text'], str):
                    return None
                if not block['text'].strip():
                    continue  # Claude refuses empty text blocks in requests
            if kind == 'tool_use':
                raw = calls[index]['arguments'] if index in calls else None
                try:
                    block['input'] = json.loads(raw) if raw and raw.strip() else {}
                except (TypeError, ValueError, RecursionError):
                    return None
                if not (isinstance(block['id'], str) and _TOOL_USE_ID.match(block['id']) and block['name']
                        and isinstance(block['input'], dict) and not nesting_too_deep(block['input'])):
                    return None
            content.append(block)
        return {'protocol': 'anthropic', 'content': content}


def _anthropic(response):
    inputs = {}
    completion = None
    finish = None
    calls = {}
    turn = _Turn()
    for item in _sse(response):
        if item is None:
            raise UpstreamError('The provider stopped without its terminal event.')
        kind = item.get('type')
        if kind == 'error':
            raise _stream_error(item.get('error'))
        if kind == 'message_start':
            message = item.get('message')
            usage = message.get('usage') if isinstance(message, dict) else None
            if not isinstance(usage, dict) or 'input_tokens' not in usage:
                raise UpstreamError('The provider omitted input usage.')
            inputs = {key: _tokens(usage[key]) for key in _INPUT_KEYS if usage.get(key) is not None}
            completion = _tokens(usage.get('output_tokens') or 0)
            prompt = sum(inputs.values())
            if prompt > MAX_TOKENS:
                raise UpstreamError('Input usage exceeds its limit.')
            yield Chunk(prompt_tokens=prompt)
        elif kind == 'content_block_start':
            block = item.get('content_block')
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                index = item.get('index')
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 32:
                    raise UpstreamError('Invalid tool call index.')
                if not isinstance(block.get('name'), str):
                    raise UpstreamError('Invalid tool call.')
                calls[index] = {'name': block['name'], 'arguments': '', 'input': block.get('input')}
            turn.start(item.get('index'), block)
        elif kind == 'content_block_delta':
            if finish is not None:
                raise UpstreamError('Output followed the provider finish marker.')
            delta = item.get('delta')
            if not isinstance(delta, dict):
                raise UpstreamError('Invalid message delta.')
            kind_of_delta = delta.get('type')
            text = delta.get('text', '') if kind_of_delta == 'text_delta' else ''
            thinking = delta.get('thinking', '') if kind_of_delta == 'thinking_delta' else ''
            if not isinstance(text, str) or not isinstance(thinking, str):
                raise UpstreamError('Invalid message delta.')
            if kind_of_delta == 'input_json_delta':
                stored = calls.get(item.get('index'))
                fragment = delta.get('partial_json', '')
                if stored is None or not isinstance(fragment, str):
                    raise UpstreamError('Invalid tool call delta.')
                stored['arguments'] += fragment
                if len(stored['arguments']) > MAX_RECORD:
                    raise UpstreamError('Tool call exceeds size limit.')
            turn.delta(item.get('index'), delta)
            if text or thinking:
                yield Chunk(content=text, thinking=thinking)
        elif kind == 'message_delta':
            delta = item.get('delta') if isinstance(item.get('delta'), dict) else {}
            reason = delta.get('stop_reason')
            if reason is not None:
                if not isinstance(reason, str):
                    raise UpstreamError('Invalid provider stop reason.')
                finish = _finish(reason)
            usage = item.get('usage')
            if isinstance(usage, dict):  # cumulative counts
                inputs.update({key: _tokens(usage[key]) for key in _INPUT_KEYS if usage.get(key) is not None})
                if usage.get('output_tokens') is not None:
                    completion = _tokens(usage['output_tokens'])
                yield Chunk(prompt_tokens=sum(inputs.values()) if inputs else None, completion_tokens=completion)
        elif kind == 'message_stop':
            if finish is None or not inputs:
                raise UpstreamError('The provider omitted terminal status or usage.')
            for call in calls.values():
                if not call['arguments'] and isinstance(call['input'], dict) and call['input']:
                    call['arguments'] = json.dumps(call['input'])
            record = turn.record(calls)
            yield Chunk(done=True, finish_reason=finish, prompt_tokens=sum(inputs.values()),
                        completion_tokens=completion,
                        tool_calls=_tool_calls({index: {'name': call['name'], 'arguments': call['arguments']}
                                                for index, call in calls.items()}),
                        extra={'replay': record} if record is not None else {})
            return
    raise UpstreamError('The provider stopped without its terminal event.')


def stream(model, messages, *, options=None, effort=None, tools=None, cancel=None):
    provider = store.get(model['external_provider_id'])
    if provider is None or not provider['enabled']:
        raise UpstreamError(MESSAGES['provider_disabled'], code='provider_disabled')
    supported = json.loads(model['reasoning_levels'] or '[]')
    if effort is not None and effort not in supported:
        raise UpstreamError('This external model does not support the selected reasoning level.',
                            code='provider_rejected_request')
    yield from _stream(provider, model['backend_model_name'], model_settings(model), messages, options or {}, effort,
                       tools, cancel, model_id=model['id'])


def _stream(provider, name, settings, messages, options, effort, tools, cancel, *, model_id=None, timeouts=None):
    protocol = provider['protocol']
    try:
        converted, system, (records, round_start) = _convert(messages, protocol)
        prefix = None
        if protocol == 'anthropic':
            resume = False
            if records or tools:  # agents: send back signed thinking, and remember what this turn is bound to
                header = _replay_header(provider, name, system, _anthropic_tools(tools))
                mode = _thinking_mode(options, effort, settings)
                sent, prefix, resume = _replay(converted, records, round_start, header, mode)
                if mode is not None and not resume and _tool_continuation(messages):
                    sent, prefix, _ = _replay(converted, records, round_start, header, None)
                converted = sent
            body = _anthropic_body(name, converted, system, messages, options, effort, tools, settings,
                                   resume=resume)
            path, parser = '/messages', _anthropic
        else:
            body = _openai_body(provider, name, converted, options, effort, tools, settings)
            path, parser = '/chat/completions', _openai
        payload = json.dumps(body).encode('utf-8')
        if len(payload) > 32 * 1024 * 1024:
            raise UpstreamError('The external request exceeded its size limit.', code='provider_rejected_request')
        with _open(provider, 'POST', path, body=payload, cancel=cancel, timeouts=timeouts) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', '').lower():
                raise UpstreamError('The provider did not return an event stream.')
            checked = time.monotonic()
            for chunk in parser(response):
                # A provider disabled or edited meanwhile ends the answer: checked now and then while it streams
                # (not once per token) and always before the last chunk, so an answer never completes against a
                # changed configuration.
                if chunk.done or time.monotonic() - checked >= PROVIDER_RECHECK_SECONDS:
                    checked = time.monotonic()
                    fresh = store.get(provider['id'])
                    if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
                        raise UpstreamError('The external provider configuration changed during this request.',
                                            code='provider_disabled')
                if chunk.done and chunk.extra.get('replay') is not None:
                    if prefix is None:
                        chunk.extra.pop('replay')
                    else:
                        chunk.extra['replay']['prefix'] = prefix
                yield chunk
    except Cancelled:
        raise
    except Exception as error:
        failure = _error(error, provider)
        if failure.code == 'provider_model_not_found' and model_id is not None:
            catalog.set_lifecycle(model_id, backend_available=0, missing_at=db.now(),
                                  missing_reason='The API provider rejected this model as unavailable.')
        raise failure from None


def probe(provider, name):
    """A tiny streamed request to *name*: what came back, for the connection test. Raises UpstreamError."""
    started = time.monotonic()
    text, final = [], None
    settings = {'sampling': False, 'thinking': 'adaptive', 'max_output': None}
    for chunk in _stream(provider, name, settings, [{'role': 'user', 'content': PROBE_PROMPT}],
                         {'num_predict': PROBE_TOKENS}, None, None, None, timeouts=(30, 30, 60)):
        text.append(chunk.content)
        if chunk.done:
            final = chunk
    return {'text': ' '.join(''.join(text).split())[:80], 'finish': final.finish_reason,
            'prompt_tokens': final.prompt_tokens, 'completion_tokens': final.completion_tokens,
            'seconds': round(time.monotonic() - started, 1)}
