"""Shared constants."""

import re

from helpers._passwords import generate_password_hash

ROLE_LABELS = {
    "user": "Member",
    "admin": "Administrator",
}

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_PASSWORD_LENGTH = 1024
MIN_PASSWORD_LENGTH = 8


def _get_dummy_hash():
    """Lazily computed dummy hash for constant-time login guards."""
    if not hasattr(_get_dummy_hash, "_cache"):
        _get_dummy_hash._cache = generate_password_hash("dummy-constant-time-check")
    return _get_dummy_hash._cache
