"""Use age's authenticated streaming format; keep recovery identities off Git."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .files import digest, private_bytes, publish

MAGIC = b"BananaSuite backup v1\n"


def binary(name):
    result = shutil.which(name)
    if not result:
        raise ValueError("Install age and age-keygen first (Debian/Ubuntu: apt install age).")
    return result


def recipient(identity):
    lines = private_bytes(identity, 4096).decode("ascii").splitlines()
    secrets = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if len(secrets) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", secrets[0]):
        raise ValueError("Use a native age recovery identity created by backups keygen.")
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
    """Verify age authentication and the whole payload before publishing it."""
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
