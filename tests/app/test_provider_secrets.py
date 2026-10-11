"""Provider API keys are encrypted at rest with a key derived from the instance secret."""
from __future__ import annotations

import os
import secrets
from dataclasses import replace

import pytest

from bananachat.db import external_providers as store
from bananachat.services import provider_secrets

KEY = 'sk-synthetic-secret-at-rest-0123456789'


def key_file(app, reference):
    return app.config['BC'].instance_dir / '.provider-keys' / reference


def test_keys_are_encrypted_at_rest(app):
    with app.app_context():
        reference = provider_secrets.save(KEY)
        stored = key_file(app, reference).read_bytes()
        assert stored.startswith(b'bcpk1:') and KEY.encode() not in stored
        assert key_file(app, reference).stat().st_mode & 0o777 == 0o600
        assert provider_secrets.load(reference) == KEY


def test_plaintext_key_files_from_older_versions_are_encrypted_when_read(app):
    with app.app_context():
        reference = secrets.token_hex(16)
        path = key_file(app, reference)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(KEY.encode())
        assert provider_secrets.load(reference) == KEY
        assert path.read_bytes().startswith(b'bcpk1:') and path.stat().st_mode & 0o777 == 0o600
        assert provider_secrets.load(reference) == KEY


def test_background_refresh_encrypts_keys_of_disabled_providers(app):
    from bananachat.services import external_providers as service
    with app.app_context():
        reference = secrets.token_hex(16)
        path = key_file(app, reference)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(KEY.encode())
        store.save(name='Old', base_url='https://old.example/v1', protocol='openai', secret_ref=reference, enabled=0)
        service.refresh_models(app)
        assert path.read_bytes().startswith(b'bcpk1:')


def test_a_changed_instance_secret_cannot_read_the_key(app):
    with app.app_context():
        reference = provider_secrets.save(KEY)
    app.config['BC'] = replace(app.config['BC'], secret_key='a-completely-different-secret-key-value')
    with app.app_context(), pytest.raises(ValueError, match='secret key'):
        provider_secrets.load(reference)


def test_a_tampered_key_file_is_refused(app):
    with app.app_context():
        reference = provider_secrets.save(KEY)
        path = key_file(app, reference)
        data = bytearray(path.read_bytes())
        data[-5] = ord('A') if data[-5] != ord('A') else ord('B')
        path.write_bytes(bytes(data))
        with pytest.raises(ValueError):
            provider_secrets.load(reference)
