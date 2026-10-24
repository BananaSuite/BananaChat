"""External-provider metadata. API secrets are never part of these rows."""
import sqlite3
import threading
import time

from bananachat import db

LOCAL_CACHE_SECONDS = 15
_local_cache: dict = {}
_local_lock = threading.Lock()


def local_ids() -> frozenset:
    """IDs of the providers that run on the operator's own hardware (``local_backend``), cached for a few seconds
    per database: :func:`bananachat.services.limits.is_local` asks for every request."""
    key = str(db.path())
    now = time.monotonic()
    with _local_lock:
        cached = _local_cache.get(key)
        if cached is not None and now - cached[0] < LOCAL_CACHE_SECONDS:
            return cached[1]
    try:
        ids = frozenset(row[0] for row in db.query("SELECT id FROM external_providers WHERE local_backend=1"))
    except sqlite3.OperationalError:
        ids = frozenset()  # a database before schema version 27
    with _local_lock:
        _local_cache[key] = (now, ids)
    return ids


def forget_local_ids() -> None:
    with _local_lock:
        _local_cache.clear()


def get(provider_id):
    return db.one('SELECT * FROM external_providers WHERE id=?', (provider_id,))


def list_providers(*, enabled_only=False):
    return db.query('SELECT * FROM external_providers ' + ('WHERE enabled=1 ' if enabled_only else '') +
                    'ORDER BY name COLLATE NOCASE, id')


def save(provider_id=None, **fields):
    allowed = {'name', 'base_url', 'protocol', 'secret_ref', 'enabled', 'allow_private', 'auto_enroll', 'token_parameter',
               'stream_usage', 'preset', 'auth_style', 'extra_headers', 'local_backend'}
    if not fields or not fields.keys() <= allowed:
        raise ValueError('Invalid provider fields.')
    with db.transaction():
        if provider_id is None:
            if 'auth_style' not in fields:
                fields['auth_style'] = 'x-api-key' if fields.get('protocol') == 'anthropic' else 'bearer'
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
        if fields.get('preset') == 'gemini':
            use_bare_gemini_ids(provider_id)
    return provider_id


GEMINI_RESOURCE = 'models/'


def _bare(name):
    return name[len(GEMINI_RESOURCE):] if isinstance(name, str) and name.startswith(GEMINI_RESOURCE) \
        and len(name) > len(GEMINI_RESOURCE) else name


def use_bare_gemini_ids(provider_id):
    """Call a Gemini-preset provider's models by the bare ID its listing gives (``gemini-…``, not ``models/gemini-…``).

    Migration v23 does this for providers it recognises as Gemini by their address; this covers one that gets the
    Gemini preset later (for example one reached through another host). A model keeps its row, public ID,
    publication, limits and access; one whose bare ID is already enrolled is left alone. Call inside a transaction.
    """
    taken = {row['backend_model_name'] for row in db.query(
        "SELECT backend_model_name FROM ai_models WHERE backend='external' AND external_provider_id=?",
        (provider_id,))}
    rows = db.query("SELECT id, backend_model_name, display_name FROM ai_models WHERE backend='external' "
                    "AND external_provider_id=? AND substr(backend_model_name, 1, 7)=? ORDER BY id",
                    (provider_id, GEMINI_RESOURCE))
    for row in rows:
        name, bare = row['backend_model_name'], _bare(row['backend_model_name'])
        if bare == name or bare in taken:
            continue
        db.execute('UPDATE ai_models SET backend_model_name=?, display_name=? WHERE id=?',
                   (bare, bare if row['display_name'] == name else row['display_name'], row['id']))
        taken.add(bare)


def model(provider_id, model_id):
    """The catalog row of a provider's model, found by the ID the provider is called with.

    That ID usually ends the public one (``external:<provider>:<model ID>``), but Gemini models enrolled
    before 1.7 keep their public ``…:models/<ID>`` name while the provider is called with the bare ID
    (migration v23).
    """
    return db.one("SELECT * FROM ai_models WHERE backend='external' AND external_provider_id=? "
                  "AND backend_model_name=? ORDER BY id LIMIT 1", (provider_id, model_id))
