"""Bot protection: honeypot + form timing."""

import hashlib
import hmac
import os
import time

from flask import current_app

try:
    _MIN_FORM_SECONDS = max(0.0, float(os.environ.get("BC_MIN_FORM_SECONDS", "0.4")))
except (TypeError, ValueError):
    _MIN_FORM_SECONDS = 0.4

_MAX_FORM_SECONDS = 4 * 3600
HONEYPOT_FIELD = "website"
FORM_TIME_FIELD = "_form_time"


def _get_secret():
    key = current_app.secret_key
    if isinstance(key, str):
        return key.encode("utf-8")
    return bytes(key)


def _sign(timestamp):
    return hmac.new(_get_secret(), timestamp.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def _is_testing():
    try:
        if current_app.config.get("TESTING"):
            return True
        if not current_app.config.get("WTF_CSRF_ENABLED", True):
            return True
    except RuntimeError:
        pass
    return False


def generate_form_token():
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def check_bot_protection(request):
    """Return (blocked, reason). Always (False, '') in test mode."""
    if _is_testing():
        return False, ""
    if request.form.get(HONEYPOT_FIELD, ""):
        return True, "honeypot"
    raw_token = request.form.get(FORM_TIME_FIELD, "")
    if not raw_token:
        return True, "missing_token"
    try:
        ts_str, sig = raw_token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return True, "invalid_token"
    expected = _sign(ts_str)
    if not hmac.compare_digest(sig, expected):
        return True, "invalid_token"
    elapsed = time.time() - ts
    if elapsed < _MIN_FORM_SECONDS:
        return True, "too_fast"
    if elapsed > _MAX_FORM_SECONDS:
        return True, "expired_token"
    return False, ""
