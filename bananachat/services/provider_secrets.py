"""Private server credential files; database and browser receive opaque refs only.

Each key is encrypted (Fernet: AES-128-CBC with HMAC-SHA256) with a key derived
from the instance secret (``BC_SECRET_KEY`` or ``<instance>/.secret_key``), and
the file is also private to the service user (mode 0600 in a 0700 directory).
A copy of the key files without the instance secret does not reveal the keys.
Files written by older versions hold the plain key; they are encrypted in place
the first time they are read.
"""
import base64
import os
import re
import secrets
import tempfile
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app
from banana_backup.files import directory, private_bytes, sync_dir

PREFIX = b'bcpk1:'
MAX_KEY = 8192
_MAX_FILE = 16384
UNREADABLE = ('The stored API key cannot be decrypted: the server secret key changed or the key file was altered. '
              'Enter the provider key again.')


def _root():
    return directory(current_app.config['BC'].instance_dir / '.provider-keys')


def _path(reference):
    if not isinstance(reference, str) or not re.fullmatch(r'[a-f0-9]{32}', reference):
        raise ValueError('The provider credential reference is invalid.')
    return _root() / reference


@lru_cache(maxsize=4)
def _cipher(secret_key):
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'BananaChat external provider keys',
                   info=b'provider-secrets-v1').derive(secret_key.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(derived))


def _encrypt(key):
    secret = current_app.config['BC'].secret_key
    if not secret:
        raise ValueError('The server secret key is not loaded, so provider keys cannot be stored.')
    return PREFIX + _cipher(secret).encrypt(key.encode('ascii'))


def _decrypt(data):
    secret = current_app.config['BC'].secret_key
    try:
        if not secret:
            raise ValueError
        return _cipher(secret).decrypt(data[len(PREFIX):]).decode('ascii')
    except (InvalidToken, ValueError, UnicodeError):
        raise ValueError(UNREADABLE) from None


def _write(path, data, *, replace=False):
    """Write a 0600 file durably: a new file exclusively, or atomically over an existing one."""
    root = path.parent
    if replace:
        descriptor, temporary = tempfile.mkstemp(prefix='.write-', dir=root)
    else:
        descriptor, temporary = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), None
    try:
        with os.fdopen(descriptor, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if temporary is not None:
            os.replace(temporary, path)
        sync_dir(root)
    except BaseException:
        Path(temporary or path).unlink(missing_ok=True)
        raise


def save(key):
    if not isinstance(key, str) or len(key) > MAX_KEY or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError('Use an API key without spaces or control characters (maximum 8192 characters).')
    if not key:
        return None
    reference = secrets.token_hex(16)
    _write(_path(reference), _encrypt(key))
    return reference


def load(reference):
    if not reference:
        return ''
    data = private_bytes(_path(reference), _MAX_FILE)
    if data.startswith(PREFIX):
        return _decrypt(data)
    key = data.decode('ascii', 'replace')
    if not key or len(key) > MAX_KEY or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError(UNREADABLE)
    try:
        _write(_path(reference), _encrypt(key), replace=True)
    except (OSError, ValueError):
        pass  # Still readable; the next read or the background refresh encrypts it.
    return key


def encrypt_legacy(reference):
    """Encrypt a key file written by an older version; True when it was rewritten."""
    if not reference:
        return False
    path = _path(reference)
    if not path.exists() or private_bytes(path, _MAX_FILE).startswith(PREFIX):
        return False
    load(reference)
    return private_bytes(path, _MAX_FILE).startswith(PREFIX)


def remove(reference):
    if reference:
        _path(reference).unlink(missing_ok=True)
