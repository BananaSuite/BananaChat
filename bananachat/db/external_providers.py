"""External-provider metadata. API secrets are never part of these rows."""
from bananachat import db


def get(provider_id):
    return db.one('SELECT * FROM external_providers WHERE id=?', (provider_id,))


def list_providers(*, enabled_only=False):
    return db.query('SELECT * FROM external_providers ' + ('WHERE enabled=1 ' if enabled_only else '') +
                    'ORDER BY name COLLATE NOCASE, id')


def save(provider_id=None, **fields):
    allowed = {'name', 'base_url', 'protocol', 'secret_ref', 'enabled', 'allow_private', 'auto_enroll', 'token_parameter', 'stream_usage'}
    if not fields or not fields.keys() <= allowed:
        raise ValueError('Invalid provider fields.')
    with db.transaction():
        if provider_id is None:
            keys = ', '.join(fields)
            marks = ', '.join('?' for _ in fields)
            return db.execute(f'INSERT INTO external_providers ({keys}) VALUES ({marks})', tuple(fields.values())).lastrowid
        if get(provider_id) is None:
            raise ValueError('The provider no longer exists.')
        assignments = ', '.join(f'{key}=?' for key in fields)
        db.execute(f'UPDATE external_providers SET {assignments}, revision=revision+1, updated_at=? WHERE id=?',
                   (*fields.values(), db.now(), provider_id))
        # A changed connection needs a fresh observation; keep models hidden until checked.
        db.execute('UPDATE ai_models SET backend_available=0 WHERE external_provider_id=?', (provider_id,))
    return provider_id
