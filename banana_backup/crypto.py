"""Use age's authenticated streaming format; keep recovery identities off Git.

age authenticates a ciphertext against its *recipient*, which is public (it
is in the series' identity branch), so anyone who can push to the backup
repository could encrypt a package of their own. Snapshot indexes therefore
carry an HMAC-SHA256 keyed from the recovery identity, which never leaves
the server and the operator's offline copy: whoever holds the recovery key
(and so can restore) can also authenticate, and nobody else can forge.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .files import digest, private_bytes, publish

MAGIC = b"BananaSuite backup v1\n"
AUTHENTICATION = "hmac-sha256-v1"
_KEY_LABEL = b"BananaSuite backup index authentication v1"


def binary(name):
    result = shutil.which(name)
    if not result:
        raise ValueError("Install age and age-keygen first (Debian/Ubuntu: apt install age).")
    return result


def _secret(identity):
    lines = private_bytes(identity, 4096).decode("ascii").splitlines()
    secrets = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if len(secrets) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", secrets[0]):
        raise ValueError("Use a native age recovery identity created by backups keygen.")
    return secrets[0]


def recipient(identity):
    _secret(identity)
    result = subprocess.run([binary("age-keygen"), "-y", str(identity)], capture_output=True, text=True, timeout=15)
    value = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"age1[0-9a-z]{58}", value):
        raise ValueError("The age recovery identity is invalid.")
    return value


def keygen(destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new key filename. Existing recovery identities are never overwritten.")
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("Recovery key paths cannot traverse symbolic links.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = subprocess.run([binary("age-keygen"), "-o", str(destination)], capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Could not create the age recovery identity.")
    destination.chmod(0o600)
    return {"key_file": str(destination), "recipient": recipient(destination),
            "next": "Keep an offline copy of this key. Repository access alone cannot restore a backup."}


def _mac(identity, index):
    key = hmac.new(_secret(identity).encode("ascii"), _KEY_LABEL, hashlib.sha256).digest()
    body = {name: value for name, value in index.items() if name != "authentication"}
    message = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def authenticate(index, identity):
    """*index* with an ``authentication`` tag over everything else in it (context and part checksums)."""
    return {**index, "authentication": {"scheme": AUTHENTICATION, "mac": _mac(identity, index)}}


def check_authentication(index, identity, *, allow_unauthenticated=False):
    """Verify a snapshot index before any of its ciphertext is decrypted; return whether it was authenticated.

    The index binds the product, series, snapshot ID and every encrypted
    part's SHA-256, so the tag covers the whole ciphertext. Snapshots made
    before authentication existed are refused unless *allow_unauthenticated*;
    a forgery looks the same, so the store also refuses one that cannot be
    that old.
    """
    tag = index.get("authentication")
    if tag is None:
        if allow_unauthenticated:
            return False
        raise ValueError("This snapshot is not authenticated: either a release from before backups were "
                         "authenticated made it, or someone else with write access to the backup repository did, "
                         "and nothing tells which. Restore it with --allow-unauthenticated only if you know it is "
                         "one of your own older backups and trust everyone who could push to that repository.")
    if (not isinstance(tag, dict) or tag.get("scheme") != AUTHENTICATION or not isinstance(tag.get("mac"), str)
            or not hmac.compare_digest(tag["mac"].encode(), _mac(identity, index).encode())):
        raise ValueError("Backup authentication failed: this snapshot was not made with this recovery key, or was "
                         "changed in the repository. Do not restore it.")
    return True


def encrypt(package, destination, identity, context):
    """Bind the selected product, series, and snapshot ID inside the ciphertext."""
    package, destination = Path(package), Path(destination)
    metadata = {**context, "schema": 1, "bytes": package.stat().st_size, "sha256": digest(package)}
    public_key = recipient(identity)
    with destination.open("xb") as output:
        destination.chmod(0o600)
        process = subprocess.Popen([binary("age"), "--encrypt", "--recipient", public_key],
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL)
        try:
            process.stdin.write(MAGIC + json.dumps(metadata, sort_keys=True).encode() + b"\n")
            with package.open("rb") as source:
                shutil.copyfileobj(source, process.stdin, 1024 * 1024)
            process.stdin.close()
            if process.wait(timeout=600):
                raise RuntimeError("Backup encryption failed.")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            process.kill()
            process.wait()
            raise
    return metadata


def decrypt(ciphertext, destination, identity, context, maximum):
    """Decrypt and check the whole payload before publishing it.

    age only proves the ciphertext was encrypted to our public recipient:
    callers check the snapshot index with :func:`check_authentication` first.
    """
    recipient(identity)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new download filename; existing files are never overwritten.")
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("Download paths cannot traverse symbolic links.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".decrypt-", dir=destination.parent) as temporary:
        wrapped = Path(temporary) / "verified"
        with wrapped.open("xb") as output:
            wrapped.chmod(0o600)
            result = subprocess.run([binary("age"), "--decrypt", "--identity", str(identity), str(ciphertext)],
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=600)
        if result.returncode:
            raise ValueError("Backup authentication failed. Check the recovery key and repository snapshot.")
        if wrapped.stat().st_size > maximum + 16384:
            raise ValueError("The decrypted backup exceeds the configured size limit.")
        payload = Path(temporary) / "payload"
        with wrapped.open("rb") as source:
            if source.readline(128) != MAGIC:
                raise ValueError("Unsupported encrypted backup format.")
            metadata = json.loads(source.readline(16384))
            if (not isinstance(metadata, dict) or metadata.get("schema") != 1
                    or any(metadata.get(key) != value for key, value in context.items())
                    or type(metadata.get("bytes")) is not int or not 0 < metadata["bytes"] <= maximum):
                raise ValueError("This backup belongs to a different product, series, or snapshot, or is too large.")
            with payload.open("xb") as output:
                payload.chmod(0o600)
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        if payload.stat().st_size != metadata["bytes"] or digest(payload) != metadata.get("sha256"):
            raise ValueError("The decrypted backup is incomplete or damaged.")
        publish(payload, destination)
    return metadata
