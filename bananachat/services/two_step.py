"""Two-step verification: a code from an authenticator app after the password.

Any account can turn it on from the account page. Administrators can also
require it of every administrator (``site_settings.admin_two_step_required``,
off by default); an administrator without it is then sent to set it up
before the administration pages open (:func:`missing`). Turning it on takes
the password and a code from the app, which proves the app was set up, shows
ten one-time recovery codes once and signs the account's other sessions out.
Turning it off and making new recovery codes take the password and a code
from the app or a recovery code. An administrator can reset it for another
account that lost both (audited; that account's sessions end). API tokens
are not affected.

Storage (``user_two_step``, schema version 24; a row means it is on): the
authenticator secret is encrypted (Fernet) with a key derived from the
instance secret key, recovery codes are kept as SHA-256 digests (see
:mod:`bananachat.totp`), and ``last_counter`` is the last 30-second step
accepted, so a code works only once, even for concurrent sign-ins. When the
secret key changes (a database export moved to a server that has another
one), app codes stop working but recovery codes still do: the account signs
in with one, turns two-step verification off and sets it up again.

Wrong codes are counted (:func:`check_code`) per account and browser that
signed in to it before (its device cookie), or per account for every other
browser, at sign-in and on the account page alike: only someone holding the
password gets that far, and they cannot use up a known browser's attempts.
Device cookies count only when they were issued after the current set-up
(turning it on, off or resetting it retires them, see :mod:`bananachat.security`),
and every account also has a daily ceiling of wrong codes whatever the browser
(:data:`FAILURES_PER_ACCOUNT_DAY`). Wrong codes given to confirm an
administrator action count against the account's own budget
(:func:`reserve_code_attempt`).
The sign-in step is in :mod:`bananachat.web.auth`, the account pages in
:mod:`bananachat.web.account`.
"""

from __future__ import annotations

import base64
import hmac
import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app, g

from bananachat import db, totp
from bananachat.db import settings as site_settings
from bananachat.db import users

REQUIRED_SETTING = "admin_two_step_required"
# Enough for typing mistakes, too few to guess one of a million codes.
FAILURE_WINDOW = 5 * 60
FAILURES = 10
FAILURES_PER_DAY = 50
# Whatever the browser: known browsers together cannot try more than this a day.
FAILURES_PER_ACCOUNT_DAY = 200


class Refused(Exception):
    """A code was not accepted; ``key`` is a translation key, ``status`` the HTTP status."""

    def __init__(self, key: str, status: int):
        super().__init__(key)
        self.key = key
        self.status = status


# ----- secrets ----------------------------------------------------------------

def _secret_key() -> str:
    secret = current_app.config["BC"].secret_key
    if not secret:
        raise ValueError("The server secret key is not loaded.")
    return secret


@lru_cache(maxsize=4)
def _cipher(secret_key: str) -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"BananaChat two-step verification",
                   info=b"two-step-secrets-v1").derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(secret: str) -> str:
    return _cipher(_secret_key()).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(token: str | None) -> str:
    """The secret, or ``""`` when *token* does not decrypt (another secret key, damaged data)."""
    try:
        return _cipher(_secret_key()).decrypt((token or "").encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return ""


def _hashes(stored: str | None) -> list[str]:
    try:
        values = json.loads(stored or "[]")
    except ValueError:
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


# ----- state ------------------------------------------------------------------

def state(user_id: str) -> dict | None:
    """``{"enabled_at", "codes_left", "readable"}`` while it is on for the account, else None.

    ``readable`` is False when the authenticator secret no longer decrypts
    (the server's secret key changed): only recovery codes work then.
    """
    row = db.one("SELECT secret_encrypted, recovery_hashes, enabled_at FROM user_two_step WHERE user_id=?",
                 (user_id,))
    if row is None:
        return None
    return {"enabled_at": row["enabled_at"], "codes_left": len(_hashes(row["recovery_hashes"])),
            "readable": bool(decrypt_secret(row["secret_encrypted"]))}


def enabled(user_id: str) -> bool:
    return bool(db.one("SELECT 1 FROM user_two_step WHERE user_id=?", (user_id,)))


def required_for_admins(settings: dict | None = None) -> bool:
    if settings is None:
        settings = getattr(g, "settings", None) or site_settings.get()
    return bool(settings.get(REQUIRED_SETTING))


def required(user) -> bool:
    """Whether the site requires two-step verification of *user* (administrators, when switched on)."""
    return bool(user) and user["role"] == "admin" and required_for_admins()


def missing(user) -> bool:
    """Required of *user* but not turned on yet."""
    return required(user) and not enabled(user["id"])


def fingerprint(user) -> str:
    """Changes when the password changes or two-step verification is turned off, reset or on again."""
    enabled_at = db.scalar("SELECT enabled_at FROM user_two_step WHERE user_id=?", (user["id"],), "")
    message = f"two-step:{user['id']}:{user['password']}:{enabled_at}".encode()
    return hmac.new(_secret_key().encode(), message, "sha256").hexdigest()[:32]


# ----- codes ------------------------------------------------------------------

def verify_app_code(user_id: str, code: str | None) -> bool:
    """Accept an authenticator code once per 30-second step, even for concurrent attempts."""
    if not totp.looks_like_code(code):
        return False
    with db.transaction():
        row = db.one("SELECT secret_encrypted, last_counter FROM user_two_step WHERE user_id=?", (user_id,))
        if row is None:
            return False
        counter = totp.matching_counter(decrypt_secret(row["secret_encrypted"]), code)
        if counter is None or counter <= int(row["last_counter"]):
            return False
        db.execute("UPDATE user_two_step SET last_counter=? WHERE user_id=?", (counter, user_id))
        return True


def _use_recovery_code(user_id: str, code: str) -> bool:
    candidate = totp.recovery_hash(user_id, code)
    with db.transaction():
        hashes = _hashes(db.scalar("SELECT recovery_hashes FROM user_two_step WHERE user_id=?", (user_id,)))
        match = next((value for value in hashes if hmac.compare_digest(value, candidate)), None)
        if match is None:
            return False
        hashes.remove(match)
        db.execute("UPDATE user_two_step SET recovery_hashes=? WHERE user_id=?", (json.dumps(hashes), user_id))
        return True


def verify(user_id: str, code: str | None) -> str | None:
    """``"totp"`` or ``"recovery_code"`` when *code* is accepted (and used up), else None.

    Callers count the attempts.
    """
    code = str(code or "").strip()[:128]
    if not code:
        return None
    if totp.looks_like_code(code):
        return "totp" if verify_app_code(user_id, code) else None
    if len(totp.normalize_recovery_code(code)) == 16 and _use_recovery_code(user_id, code):
        return "recovery_code"
    return None


def reserve_code_attempt(user, device: str | None = None) -> list[int] | None:
    """Count one code attempt of *user* (from a known browser's *device* cookie, or any other); None when refused.

    Release the result with ``users.release`` when the code was right.
    """
    key = f"{user['id']}:{device}" if device else user["id"]
    return users.reserve([("two-step-fail:" + key, FAILURES),
                          ("two-step-fail-day:" + key, FAILURES_PER_DAY, 86400),
                          ("two-step-fail-account-day:" + user["id"], FAILURES_PER_ACCOUNT_DAY, 86400)],
                         FAILURE_WINDOW)


def check_code(user, code: str | None) -> str:
    """Accept a code of *user*: ``"totp"`` or ``"recovery_code"``, else :class:`Refused`.

    The attempt is counted before the code is checked, atomically with the
    limit check, and forgotten when the code is right.
    """
    from bananachat import security

    attempt = reserve_code_attempt(user, security.device_cookie_nonce(user))
    if attempt is None:
        raise Refused("two_step.too_many", 429)
    method = verify(user["id"], code)
    if method is None:
        raise Refused("two_step.invalid_code", 401)
    users.release(attempt)
    return method


# ----- changes ----------------------------------------------------------------

def _new_codes(user_id: str) -> tuple[list[str], str]:
    codes = totp.generate_recovery_codes()
    return codes, json.dumps([totp.recovery_hash(user_id, code) for code in codes])


def enable(user_id: str, secret: str, counter: int) -> list[str] | None:
    """Turn it on with *secret*, whose code for step *counter* was just entered.

    Returns the recovery codes to show once, or None when it was already on.
    """
    codes, hashes = _new_codes(user_id)
    inserted = db.execute(
        "INSERT INTO user_two_step (user_id, secret_encrypted, recovery_hashes, last_counter, enabled_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
        (user_id, encrypt_secret(secret), hashes, counter, db.now())).rowcount
    return codes if inserted else None


def renew_codes(user_id: str) -> list[str] | None:
    """Replace the recovery codes; returns the new ones, or None when it is off."""
    codes, hashes = _new_codes(user_id)
    changed = db.execute("UPDATE user_two_step SET recovery_hashes=? WHERE user_id=?", (hashes, user_id)).rowcount
    return codes if changed else None


def disable(user_id: str) -> bool:
    """Turn it off; False when it was not on."""
    return db.execute("DELETE FROM user_two_step WHERE user_id=?", (user_id,)).rowcount == 1


def admins_without() -> int:
    """Active administrators who have not turned it on."""
    return db.scalar("SELECT COUNT(*) FROM users u WHERE u.role='admin' AND u.suspended=0 "
                     "AND NOT EXISTS (SELECT 1 FROM user_two_step t WHERE t.user_id=u.id)", default=0)


def provisioning_qr(secret: str, username: str, issuer: str) -> str:
    """The QR code (a ``data:`` URL) that adds *secret* to an authenticator app."""
    return totp.qr_data_uri(totp.provisioning_uri(issuer, username, secret))
