"""External provider model enrollment and synchronization, independent of Ollama."""
from __future__ import annotations

import json
import logging
import re

from bananachat import db
from bananachat.db import catalog, external_providers as store, limits as limits_db
from bananachat.services import provider_secrets
from bananachat.services.external_api import (MODEL_ID, base_url, check_key, discover, parse_headers,  # noqa: F401
                                              probe, stream)
from bananachat.services.upstream import UpstreamError

log = logging.getLogger("bananachat.providers")

# A listed model is withdrawn only after this many consecutive successful listings omit it:
# listings can briefly leave one out (regional or truncated answers).
MISSING_LISTINGS = 2
MAX_BULK = 100
THINKING_STYLES = ('adaptive', 'budget')


def preset_for_name(name):
    lowered = name.lower()
    return 'heavy' if any(word in lowered for word in ('opus', 'fable', 'mythos', 'o1-pro', 'o3-pro')) else \
        'light' if 'haiku' in lowered else 'standard'


def preset_limits(preset):
    budgets = {'light': (300_000, 60, 0.5, 0.7), 'standard': (100_000, 30, 1.0, 1.0),
               'heavy': (50_000, 6, 3.0, 2.0)}
    tokens, requests, weight, sensitivity = budgets[preset]
    return {'enabled': True, 'window_tokens': tokens, 'weekly_tokens': None, 'weight': weight,
            'sensitivity': sensitivity, 'dynamic': True, 'rate_rules': [{'requests': requests, 'per': 'minute',
                                                       'burst': 3 if preset == 'heavy' else min(requests, 10)}]}


def public_id(provider_id, model_id):
    return f'external:{provider_id}:{model_id}'


def enrolled(provider_id, model_id):
    """The catalog row of a provider model already enrolled, by the ID the provider is called with (see
    :func:`bananachat.db.external_providers.model`), or by its public ID."""
    return store.model(provider_id, model_id) or catalog.get_by_name(public_id(provider_id, model_id))


def _chat_candidate(name):
    low = name.lower().rsplit('/', 1)[-1]
    return bool(re.match(r'(gpt-|chatgpt-|grok-|claude-|deepseek-|mistral-|codestral-|llama|qwen|gemini-|o[1-9])', low)) \
        and not any(part in low for part in ('embedding', 'tts', 'audio', 'image', 'realtime', 'transcrib', 'moderation'))


def discovered(provider):
    """The provider's last model listing, by model ID."""
    try:
        items = json.loads(provider['discovered_models'] or '[]')
    except (TypeError, ValueError):
        items = []
    return {item['id']: item for item in items if isinstance(item, dict) and isinstance(item.get('id'), str)}


def _listing_metadata(config, listing):
    """Copy what a listing states about limits and prices into a model's ``external_config``."""
    if not listing:
        return config
    for key in ('max_output', 'pricing'):
        if listing.get(key) is not None:
            config[key] = listing[key]
    return config


def enroll(provider, model_id, *, display=None, vision=False, reasoning=(), tools=False, manual=False, sampling=None,
           thinking=None, listing=None):
    """Add or update a provider model; ``listing`` is its entry from the model list (limits and prices).

    ``sampling`` (send temperature/top_p) defaults to on for OpenAI-compatible and off for Anthropic models;
    ``thinking`` selects adaptive thinking or a thinking budget (older Claude models).
    """
    from bananachat.db import model_lifecycle as lifecycle_db
    from bananachat.services import model_lifecycle
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ValueError('Use the exact provider model ID (maximum 200 characters, without spaces).')
    levels = set(reasoning)
    if not levels <= {'off', 'low', 'medium', 'high', 'extra', 'max'}:
        raise ValueError('Invalid reasoning levels.')
    if thinking is not None and thinking not in THINKING_STYLES:
        raise ValueError('Invalid thinking style.')
    caps = ['completion'] + (['vision'] if vision else []) + (['thinking'] if levels else []) + (['tools'] if tools else [])
    name = public_id(provider['id'], model_id)
    ignored = model_lifecycle.matches(name, model_lifecycle.patterns()) or model_lifecycle.matches(model_id, model_lifecycle.patterns())
    with db.transaction():
        fresh = store.get(provider['id'])
        if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
            raise ValueError('The provider changed or was disabled. Reload before enrolling models.')
        row = enrolled(provider['id'], model_id)
        if row is not None and (row['backend'] != 'external' or row['external_provider_id'] != provider['id']):
            raise ValueError('This public model ID already belongs to a different backend.')
        if row is None:
            reason = 'External API model; waiting for review.'
            cursor = db.execute(
                "INSERT INTO ai_models (ollama_name, backend, provider, external_provider_id, backend_model_name, "
                "display_name, backend_available, enrollment, capabilities, details_at, state_reason) "
                "VALUES (?, 'external', ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (name, 'external:' + str(provider['id']), provider['id'], model_id, display or model_id,
                 'ignored' if ignored else 'new', json.dumps(caps), db.now(), reason))
            row = catalog.get(cursor.lastrowid)
            lifecycle_db.add_event(row['id'], name, 'detected', reason)
        try:
            config = json.loads(row['external_config'] or '{}')
        except ValueError:
            config = {}
        config = _listing_metadata(config if isinstance(config, dict) else {}, listing)
        config['manual'] = bool(manual)
        if sampling is not None:
            config['sampling'] = bool(sampling)
        if thinking is not None:
            config['thinking'] = thinking
        db.execute('UPDATE ai_models SET external_config=?, capabilities=?, supports_vision=?, is_reasoning=?, '
                   'reasoning_levels=?, backend_available=1, backend_last_seen_at=?, missing_at=NULL, missing_reason=NULL, '
                   'absent_syncs=0, absent_since=NULL, absent_counted_at=NULL, '
                   'context_length=COALESCE(?, context_length) WHERE id=?',
                   (json.dumps(config), json.dumps(caps), int(vision), int(bool(levels)),
                    json.dumps(sorted(levels, key=limits_db.effort_rank)), db.now(),
                    (listing or {}).get('context'), row['id']))
        if not limits_db.has_model_policy(row['id']):
            policy = model_lifecycle.apply_limit_preset(row['id'], preset_for_name(model_id))
            if not policy:
                raise ValueError('The initial token limits could not be installed.')
            limits_db.set_model_policy(row['id'], {**limits_db.get_model_policy(catalog.get(row['id'])),
                                                   'counts_toward_pool': True}, None)
        if ignored:
            catalog.set_lifecycle(row['id'], enrollment='ignored', is_rolled_out=0, state_reason='Matches the ignore list.')
        elif provider['auto_enroll'] and _chat_candidate(model_id) and (listing or {}).get('chat', True) \
                and not row['enrolled_at']:
            catalog.set_lifecycle(row['id'], enrollment='auto', enrolled_at=db.now(), is_rolled_out=1,
                                  state_reason='Enabled by this provider’s automatic enrolment; review its capabilities.')
    return catalog.get(row['id'])


def _enroll_listed(provider, entry):
    return enroll(provider, entry['id'], display=entry.get('display'), vision=bool(entry.get('vision')),
                  tools=bool(entry.get('tools')), reasoning=entry.get('reasoning') or (),
                  thinking=entry.get('thinking'), listing=entry)


def enroll_many(provider, model_ids, *, publish=False, actor=None):
    """Enroll listed models with the capabilities their listing states; optionally publish them.

    Models enrolled before keep their settings (they are only published when asked). Returns
    ``(models, problems)``: the catalog rows and ``(model ID, reason)`` for those that were skipped.
    """
    from bananachat.services import model_lifecycle
    listing = discovered(provider)
    models, problems = [], []
    for model_id in list(dict.fromkeys(model_ids))[:MAX_BULK]:
        entry = listing.get(model_id)
        if entry is None:
            problems.append((model_id, 'not in the last model list'))
            continue
        try:
            row = enrolled(provider['id'], model_id)
            if row is None:
                row = _enroll_listed(provider, entry)
            if publish and not row['is_rolled_out']:
                model_lifecycle.enable(row, actor)
        except ValueError as error:
            problems.append((model_id, str(error)))
            continue
        models.append(catalog.get(row['id']))
    return models, problems


def _record_error(provider, error):
    message = str(error) + (f' Provider message: {error.detail}' if getattr(error, 'detail', '') else '')
    with db.transaction():
        db.execute('UPDATE external_providers SET last_error=? WHERE id=? AND revision=?',
                   (message[:500], provider['id'], provider['revision']))


def sync(provider_id):
    from bananachat.db import model_lifecycle as lifecycle_db
    provider = store.get(provider_id)
    if provider is None or not provider['enabled']:
        raise ValueError('Enable the provider before checking its connection.')
    try:
        models = discover(provider)
    except UpstreamError as error:
        _record_error(provider, error)
        raise
    with db.transaction():
        fresh = store.get(provider_id)
        if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
            raise ValueError('The provider changed while its models were being checked. Try again.')
        db.execute('UPDATE external_providers SET discovered_models=?, last_checked_at=?, last_error=? WHERE id=?',
                   (json.dumps(models), db.now(), '', provider_id))
        if fresh['preset'] == 'gemini':
            store.use_bare_gemini_ids(provider_id)  # the listing gives bare IDs (discover)
        offered = {item['id']: item for item in models}
        now = db.now()
        for row in catalog.list_models(backend='external'):
            if row['external_provider_id'] != provider_id:
                continue
            try:
                metadata = json.loads(row['external_config'] or '{}')
            except ValueError:
                metadata = {}
            listed = offered.get(row['backend_model_name'])
            if listed is not None or metadata.get('manual'):
                db.execute('UPDATE ai_models SET backend_available=1, backend_last_seen_at=?, missing_at=NULL, '
                           'missing_reason=NULL, absent_syncs=0, absent_since=NULL, absent_counted_at=NULL, '
                           'external_config=?, context_length=COALESCE(?, context_length) WHERE id=?',
                           (now, json.dumps(_listing_metadata(metadata, listed)), (listed or {}).get('context'),
                            row['id']))
                if row['missing_at']:
                    lifecycle_db.add_event(row['id'], row['ollama_name'], 'returned',
                                           'Listed by the external API provider again.')
                continue
            count = (row['absent_syncs'] or 0) + 1
            db.execute('UPDATE ai_models SET absent_syncs=?, absent_since=COALESCE(absent_since, ?), '
                       'absent_counted_at=? WHERE id=?', (count, now, now, row['id']))
            if count >= MISSING_LISTINGS and not row['missing_at']:
                reason = f'Not listed by the external API provider in {count} consecutive checks.'
                catalog.set_lifecycle(row['id'], backend_available=0, missing_at=now, missing_reason=reason)
                lifecycle_db.add_event(row['id'], row['ollama_name'], 'missing', reason)
        if provider['auto_enroll']:
            for item in models:
                if enrolled(provider_id, item['id']) is None:
                    _enroll_listed(provider, item)  # published only for chat models of known families
    return models


def test_connection(provider_id, model_id=None):
    """Check the key and the model list and, with *model_id*, stream a tiny answer. Changes no models.

    Returns ``{"models": count or None, "key": key details or None, "probe": probe result or None}``.
    Raises ValueError or UpstreamError (with a ``provider_*`` code and a credential-free ``detail``).
    """
    provider = store.get(provider_id)
    if provider is None or not provider['enabled']:
        raise ValueError('Enable the provider before testing its connection.')
    if model_id is not None and (not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id)):
        raise ValueError('Use the exact provider model ID (maximum 200 characters, without spaces).')
    result = {'models': None, 'key': None, 'probe': None}
    try:
        result['key'] = check_key(provider)
        try:
            result['models'] = len(discover(provider))
        except UpstreamError as error:
            # Some compatible servers have no model list; a test message still proves the connection.
            if error.code != 'provider_not_found' or not model_id:
                raise
        if model_id:
            result['probe'] = probe(provider, model_id)
    except UpstreamError as error:
        _record_error(provider, error)
        raise
    with db.transaction():
        db.execute("UPDATE external_providers SET last_error='' WHERE id=? AND revision=?",
                   (provider_id, provider['revision']))
    return result


from bananachat.services.background import job  # noqa: E402


@job('external-provider-models', every=900, initial_delay=30)
def refresh_models(app):
    for provider in store.list_providers():
        try:
            provider_secrets.encrypt_legacy(provider['secret_ref'])  # key files written by older versions
        except (OSError, ValueError):
            log.warning('The key file of external provider %s could not be encrypted', provider['id'])
    for provider in store.list_providers(enabled_only=True):
        try:
            sync(provider['id'])
        except (ValueError, UpstreamError):
            continue  # Failed discovery never marks existing models as absent.
