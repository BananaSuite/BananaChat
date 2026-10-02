"""Private server credential files; database and browser receive opaque refs only."""
import os
import re
import secrets

from flask import current_app
from banana_backup.files import directory, private_bytes, sync_dir


def _root():
    return directory(current_app.config['BC'].instance_dir / '.provider-keys')


def _path(reference):
    if not isinstance(reference, str) or not re.fullmatch(r'[a-f0-9]{32}', reference):
        raise ValueError('The provider credential reference is invalid.')
    return _root() / reference


def save(key):
    if not isinstance(key, str) or len(key) > 8192 or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError('Use an API key without spaces or control characters (maximum 8192 characters).')
    if not key:
        return None
    reference = secrets.token_hex(16)
    path = _path(reference)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(key.encode('ascii'))
            file.flush()
            os.fsync(file.fileno())
        sync_dir(_root())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return reference


def load(reference):
    return private_bytes(_path(reference), 8192).decode('ascii') if reference else ''


def remove(reference):
    if reference:
        _path(reference).unlink(missing_ok=True)
