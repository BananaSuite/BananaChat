"""External API provider configuration; every mutation is admin-only and CSRF protected.

Adding, changing or removing a provider needs a recent confirmation (:mod:`._reauth`), with or
without an API key: its connection decides where users' prompts (and the key) are sent, and
removing it deletes the stored key. Testing it, refreshing its model list and choosing its models
use the connection already confirmed."""
import json
import sqlite3

from flask import abort, flash, request

from bananachat import db
from bananachat.db import catalog, external_providers as store, settings as site_settings
from bananachat.security import admin_required
from bananachat.services import external_api, external_providers as service, provider_presets, provider_secrets
from bananachat.services.upstream import UpstreamError

from . import _reauth, bp
from ._helpers import FormError, audit, back, choice, flag, me, text


def _provider(provider_id):
    row = store.get(provider_id)
    if row is None:
        abort(404)
    return row


def _site():
    return request.url_root.rstrip('/'), site_settings.get().get('site_name') or 'BananaChat'


def _header_text(row):
    try:
        headers = json.loads(row['extra_headers'] or '{}')
    except ValueError:
        headers = {}
    return '\n'.join(f'{name}: {value}' for name, value in headers.items()) if isinstance(headers, dict) else ''


def _preset_data():
    site_url, site_name = _site()
    return {key: {'label': preset.label, 'protocol': preset.protocol, 'base_url': preset.base_url,
                  'auth_style': preset.auth_style, 'local': preset.local, 'own_hardware': preset.own_hardware,
                  'key_required': preset.key_required,
                  'hint': preset.hint,
                  'headers': provider_presets.headers_for(preset, site_url=site_url, site_name=site_name)}
            for key, preset in provider_presets.PRESETS.items()}


def _failure(error):
    """An administrator's account of a provider failure: our message plus the provider's own, without secrets."""
    message = str(error)
    if getattr(error, 'detail', ''):
        message += f' Provider message: “{error.detail}”'
    if getattr(error, 'retry_after', None):
        message += f' The provider asked to wait {error.retry_after} s.'
    return message


@bp.get('/models/providers', endpoint='external_providers')
@admin_required
def page():
    from .models import _render, _model_limits_url
    rows = store.list_providers()
    models = catalog.list_models(backend='external')
    bound = {row['id']: [m for m in models if m['external_provider_id'] == row['id']] for row in rows}
    return _render('admin/providers.html', tab='providers', providers=rows,
                   discovered={row['id']: list(service.discovered(row).values()) for row in rows},
                   bound=bound, enrolled={row['id']: {m['backend_model_name']: m for m in bound[row['id']]}
                                          for row in rows},
                   header_text={row['id']: _header_text(row) for row in rows},
                   presets=provider_presets.PRESETS, auth_styles=provider_presets.AUTH_STYLES,
                   model_levels={m['id']: json.loads(m['reasoning_levels']) for m in models},
                   model_settings={m['id']: external_api.model_settings(m) for m in models},
                   model_manual={m['id']: bool(json.loads(m['external_config']).get('manual')) for m in models},
                   model_limits_urls={m['id']: _model_limits_url(m['id']) for m in models},
                   page_data={'presets': _preset_data()})


def _save(provider_id=None):
    existing = _provider(provider_id) if provider_id is not None else None
    reference = None
    try:
        name = text('name', max_length=80, required=True, label='Provider name')
        preset = provider_presets.get(choice('preset', tuple(provider_presets.PRESETS), label='Preset',
                                             default=existing['preset'] if existing else 'custom'))
        raw_url = text('base_url', max_length=1000, label='Base URL')
        # A blank base URL takes the preset's whole connection (for forms sent without the page script).
        defaults = not raw_url
        if defaults and (not preset.base_url or 'YOUR-RESOURCE' in preset.base_url):
            raise FormError('Enter the API base URL of this provider.')
        protocol = preset.protocol if defaults else choice('protocol', ('openai', 'anthropic'), label='API format',
                                                           default=preset.protocol)
        auth_style = preset.auth_style if defaults else choice('auth_style', tuple(provider_presets.AUTH_STYLES),
                                                               label='Key header', default=preset.auth_style)
        private = flag('allow_private') or (defaults and preset.local)
        url = service.base_url(raw_url or preset.base_url, allow_private=private)
        header_text = request.form.get('extra_headers') or ''
        if len(header_text) > 6000:
            raise FormError('The extra headers are too long.')
        headers = service.parse_headers(header_text)
        if defaults and not headers:
            site_url, site_name = _site()
            headers = provider_presets.headers_for(preset, site_url=site_url, site_name=site_name)
        key = text('api_key', max_length=8192, label='API key')
        reference = provider_secrets.save(key) if key else None
        with db.transaction():
            fields = dict(name=name, preset=preset.key, protocol=protocol, base_url=url, auth_style=auth_style,
                          extra_headers=json.dumps(headers), enabled=int(flag('enabled')), allow_private=int(private),
                          auto_enroll=int(flag('auto_enroll')),
                          token_parameter=choice('token_parameter', ('auto', 'max_tokens', 'max_completion_tokens'),
                                                 label='Output limit field', default='auto'),
                          stream_usage=int(request.form.get('stream_usage', 'on') in ('on', '1')),
                          local_backend=int(preset.own_hardware if defaults else flag('local_backend')))
            if reference or flag('clear_key') or existing is None:
                fields['secret_ref'] = reference
            provider_id = store.save(provider_id, **fields)
    except (FormError, ValueError, OSError, sqlite3.IntegrityError) as error:
        if reference:
            provider_secrets.remove(reference)
        flash('A provider with that name already exists.' if isinstance(error, sqlite3.IntegrityError) else
              'The private credential file could not be saved. Check server ownership and permissions.' if isinstance(error, OSError)
              else str(error), 'error')
        return back('admin.external_providers')
    if existing and (reference or flag('clear_key')):
        provider_secrets.remove(existing['secret_ref'])
    audit('external_provider_save', name, {'id': provider_id, 'preset': preset.key, 'protocol': protocol,
                                           'enabled': fields['enabled'], 'local': fields['local_backend']})
    service.forget_local_providers()
    flash('Provider saved. Test the connection, then refresh the model list and choose the models to offer.',
          'success')
    return back('admin.external_providers', _anchor=f'provider-{provider_id}')


@bp.post('/models/providers', endpoint='external_provider_add')
@admin_required
def add():
    if not _reauth.confirmed():
        return back('admin.external_providers')
    return _save()


@bp.post('/models/providers/<int:provider_id>', endpoint='external_provider_save')
@admin_required
def save(provider_id):
    _provider(provider_id)
    if not _reauth.confirmed():
        return back('admin.external_providers', _anchor=f'provider-{provider_id}')
    return _save(provider_id)


def _test_summary(row, result, model_id):
    count = result['models']
    opening = 'Connection works. ' + ('The API key was accepted' if row['secret_ref'] else 'No API key is configured')
    parts = [opening + (f' and the provider lists {count} model{"" if count == 1 else "s"}.' if count is not None
                        else '. The provider has no model list; add model IDs manually.')]
    remaining = (result['key'] or {}).get('limit_remaining')
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        parts.append(f'Remaining credit on this key: ${remaining:,.2f}.')
    probe = result['probe']
    if probe:
        said = f'“{probe["text"]}”' if probe['text'] else \
            'no visible text (a reasoning model may spend the whole test allowance thinking)'
        parts.append(f'{model_id} answered in {probe["seconds"]} s with {said}.')
        if probe['prompt_tokens'] is None:
            parts.append('The provider did not report token usage, so BananaChat records estimates for this endpoint.'
                         + ('' if row['stream_usage'] else ' Turn on “Request provider usage” if it supports it.'))
        else:
            parts.append(f'Usage reported: {probe["prompt_tokens"]} input and {probe["completion_tokens"] or 0} '
                         'output tokens.')
    return ' '.join(parts)


@bp.post('/models/providers/<int:provider_id>/test', endpoint='external_provider_test')
@admin_required
def test(provider_id):
    row = _provider(provider_id)
    model_id = (request.form.get('model_id') or '').strip() or None
    try:
        result = service.test_connection(provider_id, model_id)
    except UpstreamError as error:
        flash(_failure(error), 'error')
    except ValueError as error:
        flash(str(error), 'error')
    else:
        flash(_test_summary(row, result, model_id), 'success')
    audit('external_provider_test', row['name'], {'id': provider_id, 'model': model_id})
    return back('admin.external_providers', _anchor=f'provider-{provider_id}')


@bp.post('/models/providers/<int:provider_id>/check', endpoint='external_provider_check')
@admin_required
def check(provider_id):
    _provider(provider_id)
    try:
        models = service.sync(provider_id)
    except UpstreamError as error:
        flash(_failure(error), 'error')
    except ValueError as error:
        flash(str(error), 'error')
    else:
        flash(f'The provider lists {len(models)} model(s). Select the models to offer.', 'success')
    audit('external_provider_check', 'provider', {'id': provider_id})
    return back('admin.external_providers', _anchor=f'provider-{provider_id}')


@bp.post('/models/providers/<int:provider_id>/models', endpoint='external_provider_enroll')
@admin_required
def enroll(provider_id):
    provider = _provider(provider_id)
    try:
        manual = flag('manual')
        name = text('model_id', required=True, max_length=200, label='Model ID')
        listing = service.discovered(provider)
        if not manual and name not in listing:
            raise FormError('Refresh the model list and select a listed model, or add a verified model ID manually.')
        display = text('display_name', max_length=120, label='Display name')
        thinking = choice('thinking', service.THINKING_STYLES, label='Thinking control', default='adaptive') \
            if provider['protocol'] == 'anthropic' else None
        row = service.enroll(provider, name, display=display or None, vision=flag('vision'), tools=flag('tools'),
                             reasoning=request.form.getlist('reasoning'), manual=manual, sampling=flag('sampling'),
                             thinking=thinking, listing=listing.get(name))
    except (ValueError, UpstreamError) as error:
        flash(str(error), 'error')
    else:
        audit('external_model_enroll', row['ollama_name'], {'provider_id': provider_id, 'model_id': row['id']})
        flash('Model saved. Review and publish it in the catalogue; its limits are already installed.', 'success')
    return back('admin.external_providers', _anchor=f'provider-{provider_id}')


@bp.post('/models/providers/<int:provider_id>/models/bulk', endpoint='external_provider_enroll_many')
@admin_required
def enroll_many(provider_id):
    provider = _provider(provider_id)
    names = list(dict.fromkeys(value.strip() for value in request.form.getlist('model_ids') if value.strip()))
    if not provider['enabled']:
        flash('Enable the provider before adding its models.', 'error')
    elif not names:
        flash('Select at least one listed model.', 'error')
    elif len(names) > service.MAX_BULK:
        flash(f'Select at most {service.MAX_BULK} models at a time.', 'error')
    else:
        publish = flag('publish')
        models, problems = service.enroll_many(provider, names, publish=publish, actor=me())
        audit('external_model_enroll_many', provider['name'], {'provider_id': provider_id, 'publish': publish,
                                                               'models': [row['id'] for row in models]})
        if models:
            flash(f'{len(models)} model(s) ' + ('published with their standard limits. Adjust access and limits in '
                                                'the catalogue.' if publish else
                                                'added. Review and publish them in the catalogue.'), 'success')
        if problems:
            flash('Not added: ' + '; '.join(f'{name} ({reason})' for name, reason in problems[:10]), 'error')
    return back('admin.external_providers', _anchor=f'provider-{provider_id}')


@bp.post('/models/providers/<int:provider_id>/remove', endpoint='external_provider_remove')
@admin_required
def remove(provider_id):
    row = _provider(provider_id)
    if not _reauth.confirmed():
        return back('admin.external_providers', _anchor=f'provider-{provider_id}')
    with db.transaction():
        db.execute('UPDATE ai_models SET backend_available=0, is_rolled_out=0, retired_at=?, state_reason=? '
                   'WHERE external_provider_id=?', (db.now(), 'External API provider removed.', provider_id))
        db.execute('DELETE FROM external_providers WHERE id=?', (provider_id,))
    provider_secrets.remove(row['secret_ref'])
    audit('external_provider_remove', row['name'], {'id': provider_id})
    flash('Provider and its credential removed. Its model history is retained; models are retired.', 'success')
    return back('admin.external_providers')
