"""External provider model enrollment and synchronization, independent of Ollama."""
from __future__ import annotations

import json
import re

from bananachat import db
from bananachat.db import catalog, external_providers as store, limits as limits_db
from bananachat.services.external_api import MODEL_ID, base_url, discover, stream  # noqa: F401
from bananachat.services.upstream import UpstreamError


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


def _chat_candidate(name):
    low = name.lower().rsplit('/', 1)[-1]
    return bool(re.match(r'(gpt-|chatgpt-|grok-|claude-|deepseek-|mistral-|codestral-|llama|qwen|gemini-|o[1-9])', low)) \
        and not any(part in low for part in ('embedding', 'tts', 'audio', 'image', 'realtime', 'transcrib', 'moderation'))


def enroll(provider, model_id, *, display=None, vision=False, reasoning=(), tools=False, manual=False):
    from bananachat.db import model_lifecycle as lifecycle_db
    from bananachat.services import model_lifecycle
    if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id):
        raise ValueError('Use the exact provider model ID (maximum 200 characters, without spaces).')
    levels = set(reasoning)
    if not levels <= {'off', 'low', 'medium', 'high', 'extra', 'max'}:
        raise ValueError('Invalid reasoning levels.')
    caps = ['completion'] + (['vision'] if vision else []) + (['thinking'] if levels else []) + (['tools'] if tools else [])
    name = public_id(provider['id'], model_id)
    ignored = model_lifecycle.matches(name, model_lifecycle.patterns()) or model_lifecycle.matches(model_id, model_lifecycle.patterns())
    with db.transaction():
        fresh = store.get(provider['id'])
        if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
            raise ValueError('The provider changed or was disabled. Reload before enrolling models.')
        row = catalog.get_by_name(name)
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
        db.execute('UPDATE ai_models SET external_config=?, capabilities=?, supports_vision=?, is_reasoning=?, '
                   'reasoning_levels=?, backend_available=1, backend_last_seen_at=?, missing_at=NULL, missing_reason=NULL '
                   'WHERE id=?', (json.dumps({'manual': manual}), json.dumps(caps), int(vision), int(bool(levels)),
                                 json.dumps(sorted(levels, key=limits_db.effort_rank)), db.now(), row['id']))
        if not limits_db.has_model_policy(row['id']):
            policy = model_lifecycle.apply_limit_preset(row['id'], preset_for_name(model_id))
            if not policy:
                raise ValueError('The initial token limits could not be installed.')
            limits_db.set_model_policy(row['id'], {**limits_db.get_model_policy(catalog.get(row['id'])),
                                                   'counts_toward_pool': True}, None)
        if ignored:
            catalog.set_lifecycle(row['id'], enrollment='ignored', is_rolled_out=0, state_reason='Matches the ignore list.')
        elif provider['auto_enroll'] and _chat_candidate(model_id) and not row['enrolled_at']:
            catalog.set_lifecycle(row['id'], enrollment='auto', enrolled_at=db.now(), is_rolled_out=1,
                                  state_reason='Enabled by this provider’s automatic enrollment; review its capabilities.')
    return catalog.get(row['id'])


def sync(provider_id):
    provider = store.get(provider_id)
    if provider is None or not provider['enabled']:
        raise ValueError('Enable the provider before checking its connection.')
    try:
        models = discover(provider)
    except UpstreamError as error:
        with db.transaction():
            db.execute('UPDATE external_providers SET last_error=? WHERE id=? AND revision=?',
                       (str(error), provider_id, provider['revision']))
        raise
    with db.transaction():
        fresh = store.get(provider_id)
        if fresh is None or not fresh['enabled'] or fresh['revision'] != provider['revision']:
            raise ValueError('The provider changed while its models were being checked. Try again.')
        db.execute('UPDATE external_providers SET discovered_models=?, last_checked_at=?, last_error=? WHERE id=?',
                   (json.dumps(models), db.now(), '', provider_id))
        offered = {item['id']: item for item in models}
        for row in catalog.list_models(backend='external'):
            if row['external_provider_id'] != provider_id:
                continue
            metadata = json.loads(row['external_config'])
            if row['backend_model_name'] in offered or metadata.get('manual'):
                db.execute('UPDATE ai_models SET backend_available=1, backend_last_seen_at=?, missing_at=NULL, '
                           'missing_reason=NULL WHERE id=?', (db.now(), row['id']))
            else:
                catalog.set_lifecycle(row['id'], backend_available=0, missing_at=db.now(),
                                      missing_reason='Not listed by the external API provider.')
        if provider['auto_enroll']:
            for item in models:
                if catalog.get_by_name(public_id(provider_id, item['id'])) is None:
                    enroll(provider, item['id'], display=item['display'])
    return models


from bananachat.services.background import job  # noqa: E402


@job('external-provider-models', every=900, initial_delay=30)
def refresh_models(app):
    for provider in store.list_providers(enabled_only=True):
        try:
            sync(provider['id'])
        except (ValueError, UpstreamError):
            continue  # Failed discovery never marks existing models as absent.
