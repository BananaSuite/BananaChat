"""Authenticator-app codes (TOTP, RFC 6238) and one-time recovery codes.

The arithmetic matches BananaWiki's (``bananawiki.core.totp``): six-digit
codes for 30-second steps with SHA-1, what every authenticator app accepts by
default, and one step either side for clock drift. Storage and keys live in
:mod:`bananachat.services.two_step`.

Recovery codes carry 80 random bits (16 characters of Crockford's base 32),
so a plain SHA-256 digest of them cannot be reversed by trying every code.
They therefore keep working when the server's secret key changes, for
example after a database export is moved to a new server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
RECOVERY_CODES = 10
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_ALIASES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def generate_secret() -> str:
    """A new 160-bit secret in base 32 without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, when: float | None = None) -> str:
    counter = int((time.time() if when is None else when) // STEP_SECONDS)
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    digest = hmac.new(base64.b32decode(padded, casefold=True), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def looks_like_code(code: str | None) -> bool:
    """True for six digits (spaces allowed), the shape of an authenticator code."""
    code = str(code or "").replace(" ", "")
    return len(code) == 6 and code.isascii() and code.isdigit()


def matching_counter(secret: str, code: str | None, when: float | None = None, window: int = 1) -> int | None:
    """The time step whose code *code* is (within *window* steps of now), or None."""
    if not secret or not looks_like_code(code):
        return None
    code = str(code).replace(" ", "")
    now = time.time() if when is None else float(when)
    try:
        for offset in range(-window, window + 1):
            moment = now + offset * STEP_SECONDS
            if moment >= 0 and hmac.compare_digest(totp_code(secret, moment), code):
                return int(moment // STEP_SECONDS)
    except (ValueError, TypeError):  # a damaged secret
        return None
    return None


def generate_recovery_codes(count: int = RECOVERY_CODES) -> list[str]:
    """Codes such as ``7K3M-Q9TD-X2HB-R4NV`` (80 random bits each)."""
    codes = []
    for _ in range(count):
        text = "".join(secrets.choice(_CROCKFORD) for _ in range(16))
        codes.append("-".join(text[index:index + 4] for index in range(0, 16, 4)))
    return codes


def normalize_recovery_code(code: str | None) -> str:
    """Upper case without separators; O, I and L read as 0, 1 and 1 (Crockford's base 32)."""
    text = str(code or "").replace("-", "").replace(" ", "").upper()[:64]
    return text.translate(_CROCKFORD_ALIASES)


def recovery_hash(account: str, code: str | None) -> str:
    """SHA-256 (hex) of a normalised recovery code, bound to *account*."""
    message = f"bananachat-recovery-v1\0{account}\0{normalize_recovery_code(code)}"
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def provisioning_uri(issuer: str, account: str, secret: str) -> str:
    """The ``otpauth://`` link authenticator apps read from the QR code."""
    issuer = issuer.replace(":", " ").strip() or "BananaChat"
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={quote(secret)}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def qr_data_uri(uri: str) -> str:
    """*uri* as a QR code PNG in a ``data:`` URL."""
    import qrcode

    image = qrcode.make(uri, border=2)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
