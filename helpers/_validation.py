"""Input validation helpers."""

from urllib.parse import urlparse, unquote

from flask import request

from ._constants import _USERNAME_RE


def _is_valid_username(value):
    return bool(_USERNAME_RE.fullmatch(value))


def _safe_referrer():
    ref = request.referrer
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    safe = parsed.path or "/"
    if parsed.query:
        safe = f"{safe}?{parsed.query}"
    decoded_path = parsed.path or "/"
    while True:
        next_decoded = unquote(decoded_path)
        if next_decoded == decoded_path:
            break
        decoded_path = next_decoded
    if (
        not safe.startswith("/")
        or safe.startswith("//")
        or "\\" in safe
        or decoded_path.startswith("//")
        or "\\" in decoded_path
    ):
        return None
    return safe


def get_safe_next_url(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    safe = parsed.path or "/"
    if parsed.query:
        safe = f"{safe}?{parsed.query}"
    decoded_path = parsed.path or "/"
    while True:
        next_decoded = unquote(decoded_path)
        if next_decoded == decoded_path:
            break
        decoded_path = next_decoded
    if (
        not safe.startswith("/")
        or safe.startswith("//")
        or "\\" in safe
        or decoded_path.startswith("//")
        or "\\" in decoded_path
    ):
        return None
    return safe
