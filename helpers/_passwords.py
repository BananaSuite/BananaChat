"""Password hashing helpers with cross-platform defaults."""

import hashlib
import os
import sys
from functools import lru_cache

from werkzeug.security import (
    check_password_hash as _werkzeug_check_password_hash,
    generate_password_hash as _werkzeug_generate_password_hash,
)


_AUTO_METHOD = "auto"
_DEFAULT_METHOD = "scrypt"
_FALLBACK_METHOD = "pbkdf2:sha256"


def _normalize_method(method):
    """Return a Werkzeug password hash method string."""
    value = (method or _AUTO_METHOD).strip()
    if not value:
        return _AUTO_METHOD
    if value == "pbkdf2":
        return _FALLBACK_METHOD
    return value


def _configured_method():
    """Read the configured password hash method without importing config."""
    env_method = os.environ.get("BC_PASSWORD_HASH_METHOD")
    if env_method is None:
        env_method = os.environ.get("HASH_METHOD")
    if env_method is not None:
        return _normalize_method(env_method)

    for module_name in ("config", "hosting.config"):
        config_module = sys.modules.get(module_name)
        if config_module is not None:
            return _normalize_method(
                getattr(config_module, "PASSWORD_HASH_METHOD", _AUTO_METHOD)
            )
    return _AUTO_METHOD


@lru_cache(maxsize=1)
def is_scrypt_supported():
    """Return whether this Python/OpenSSL build can run hashlib.scrypt."""
    scrypt = getattr(hashlib, "scrypt", None)
    if scrypt is None:
        return False
    try:
        scrypt(b"probe", salt=b"salt", n=2, r=1, p=1, dklen=8)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return True


def get_password_hash_method():
    """Return the method to use for new password hashes."""
    method = _configured_method()
    if method != _AUTO_METHOD:
        return method
    if is_scrypt_supported():
        return _DEFAULT_METHOD
    return _FALLBACK_METHOD


def generate_password_hash(password, method=None, salt_length=16):
    """Generate a password hash using the configured portable default."""
    selected_method = _normalize_method(method) if method else get_password_hash_method()
    return _werkzeug_generate_password_hash(
        password,
        method=selected_method,
        salt_length=salt_length,
    )


def check_password_hash(pwhash, password):
    """Check a password hash without turning unsupported algorithms into 500s."""
    try:
        return _werkzeug_check_password_hash(pwhash, password)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
