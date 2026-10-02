"""External API provider configuration; every mutation is admin-only and CSRF protected."""
import json
import sqlite3

from flask import abort, flash, request

from bananachat import db
from bananachat.db import catalog, external_providers as store
from bananachat.security import admin_required
from bananachat.services import external_providers as service, provider_secrets
from bananachat.services.upstream import UpstreamError

from . import bp
from ._helpers import FormError, audit, back, choice, flag, text


def _provider(provider_id):
    row = store.get(provider_id)
    if row is None:
        abort(404)
    return row


@bp.get('/models/providers', endpoint='external_providers')
@admin_required
def page():
    from .models import _render, _model_limits_url
    rows = store.list_providers()
    models = catalog.list_models(backend='external')
    return _render('admin/providers.html', tab='providers', providers=rows,
                   discovered={row['id']: json.loads(row['discovered_models']) for row in rows},
                   bound={row['id']: [m for m in models if m['external_provider_id'] == row['id']] for row in rows},
                   model_levels={m['id']: json.loads(m['reasoning_levels']) for m in models},
                   model_manual={m['id']: bool(json.loads(m['external_config']).get('manual')) for m in models},
                   model_limits_urls={m['id']: _model_limits_url(m['id']) for m in models})


def _save(provider_id=None):
    existing = _provider(provider_id) if provider_id is not None else None
    reference = None
    try:
        name = text('name', max_length=80, required=True, label='Provider name')
        protocol = choice('protocol', ('openai', 'anthropic'), label='API format')
        private = flag('allow_private')
        url = service.base_url(text('base_url', max_length=1000, required=True, label='Base URL'), allow_private=private)
        key = text('api_key', max_length=8192, label='API key')
        reference = provider_secrets.save(key) if key else None
        with db.transaction():
            fields = dict(name=name, protocol=protocol, base_url=url, enabled=int(flag('enabled')),
                          allow_private=int(private), auto_enroll=int(flag('auto_enroll')),
                          token_parameter=choice('token_parameter', ('auto', 'max_tokens', 'max_completion_tokens'),
                                                 label='Output limit field', default='auto'),
                          stream_usage=int(request.form.get('stream_usage', 'on') in ('on', '1')))
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
    audit('external_provider_save', name, {'id': provider_id, 'protocol': protocol, 'enabled': fields['enabled']})
    flash('Provider saved. Check its connection before publishing models.', 'success')
    return back('admin.external_providers')


@bp.post('/models/providers', endpoint='external_provider_add')
@admin_required
def add():
    return _save()


@bp.post('/models/providers/<int:provider_id>', endpoint='external_provider_save')
@admin_required
def save(provider_id):
    return _save(provider_id)


@bp.post('/models/providers/<int:provider_id>/check', endpoint='external_provider_check')
@admin_required
def check(provider_id):
    _provider(provider_id)
    try:
        models = service.sync(provider_id)
    except (ValueError, UpstreamError) as error:
        flash(str(error), 'error')
    else:
        flash(f'Connection verified. The API lists {len(models)} model(s). Select models to enroll.', 'success')
    audit('external_provider_check', 'provider', {'id': provider_id})
    return back('admin.external_providers')


@bp.post('/models/providers/<int:provider_id>/models', endpoint='external_provider_enroll')
@admin_required
def enroll(provider_id):
    provider = _provider(provider_id)
    try:
        manual = flag('manual')
        name = text('model_id', required=True, max_length=200, label='Model ID')
        available = {item['id'] for item in json.loads(provider['discovered_models'])}
        if not manual and name not in available:
            raise FormError('Check the connection and select a listed model, or use a verified model ID manually.')
        display = text('display_name', max_length=120, label='Display name')
        row = service.enroll(provider, name, display=display or None, vision=flag('vision'), tools=flag('tools'),
                             reasoning=request.form.getlist('reasoning'), manual=manual)
    except (ValueError, UpstreamError) as error:
        flash(str(error), 'error')
    else:
        audit('external_model_enroll', row['ollama_name'], {'provider_id': provider_id, 'model_id': row['id']})
        flash('Model saved. Review and publish it in the catalog; its limits are already installed.', 'success')
    return back('admin.external_providers')


@bp.post('/models/providers/<int:provider_id>/remove', endpoint='external_provider_remove')
@admin_required
def remove(provider_id):
    row = _provider(provider_id)
    with db.transaction():
        db.execute('UPDATE ai_models SET backend_available=0, is_rolled_out=0, retired_at=?, state_reason=? '
                   'WHERE external_provider_id=?', (db.now(), 'External API provider removed.', provider_id))
        db.execute('DELETE FROM external_providers WHERE id=?', (provider_id,))
    provider_secrets.remove(row['secret_ref'])
    audit('external_provider_remove', row['name'], {'id': provider_id})
    flash('Provider and its credential removed. Its model history is retained; models are retired.', 'success')
    return back('admin.external_providers')
