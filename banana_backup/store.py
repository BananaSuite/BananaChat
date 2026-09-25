"""Independent encrypted snapshots, verified uploads, and scoped retention."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from . import crypto
from .files import atomic_write, digest, directory, lock, private_bytes, read_json, write_json
from .git import Git, OVERHEAD, PART_BYTES, SNAPSHOT, check_private, location

DEFAULT_SCHEDULE = {"enabled": False, "interval_minutes": 1440}


def _snapshot_order(identifier):
    """Order old second-resolution and new microsecond-resolution identifiers."""
    stamp = identifier.split("Z-", 1)[0]
    return stamp if "." in stamp else stamp + ".000000"


def _next_snapshot(previous):
    """Keep series order monotonic even within one second or after clock drift."""
    now = datetime.now(timezone.utc)
    if previous:
        latest = datetime.strptime(max(map(_snapshot_order, previous)), "%Y%m%dT%H%M%S.%f").replace(tzinfo=timezone.utc)
        now = max(now, latest + timedelta(microseconds=1))
    return now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + secrets.token_hex(4)


class Store:
    def __init__(self, root, product):
        if product not in {"BananaWiki", "BananaChat", "BananaVibe"}:
            raise ValueError("Unknown backup product.")
        self.root, self.product = Path(root).absolute(), product
        self.identity = self.root / "recovery.agekey"
        self.token = self.root / "repo.token"

    def settings(self):
        value = read_json(self.root / "repository.json")
        if not value or value.get("schema") != 1 or value.get("product") != self.product:
            raise ValueError("Configure a private backup repository first with backups configure.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value.get("name", "")):
            raise ValueError("Invalid backup series name.")
        if (type(value.get("keep")) is not int or not 2 <= value["keep"] <= 30
                or type(value.get("max_mib")) is not int or not 1 <= value["max_mib"] <= 1024):
            raise ValueError("Invalid backup retention or size limit.")
        return value

    def schedule(self):
        value = read_json(self.root / "schedule.json", DEFAULT_SCHEDULE.copy())
        if (type(value.get("enabled")) is not bool or type(value.get("interval_minutes")) is not int
                or not 60 <= value["interval_minutes"] <= 10080):
            raise ValueError("Invalid backup schedule.")
        return value

    def configure(self, *, repo, forge, name, token_file, key_file, username="git", keep=7, max_mib=512):
        config = {**location(repo, forge), "schema": 1, "product": self.product, "name": name,
                  "username": username, "keep": keep, "max_mib": max_mib}
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name) or not 2 <= keep <= 30
                or not 1 <= max_mib <= 1024 or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,100}", username)):
            raise ValueError("Use a lowercase series name, 2–30 snapshots, and a size limit of 1–1024 MiB.")
        public = crypto.recipient(key_file)
        token = private_bytes(token_file)
        key = private_bytes(key_file, 4096)
        check_private(config, token_file)
        with lock(self.root):
            # Configure always opts out. A restored or repointed installation
            # must not start taking or pruning backups without another opt-in.
            write_json(self.root / "schedule.json", DEFAULT_SCHEDULE.copy())
            atomic_write(self.token, token)
            atomic_write(self.identity, key)
            write_json(self.root / "repository.json", {**config, "recipient": public})
            (self.root / "last-backup.json").unlink(missing_ok=True)
        return self.status()

    def set_schedule(self, enabled, interval=None):
        self.settings()
        policy = self.schedule()
        if interval is not None:
            if not 60 <= interval <= 10080:
                raise ValueError("Choose a backup interval between 60 minutes and one week.")
            policy["interval_minutes"] = interval
        policy["enabled"] = bool(enabled)
        write_json(self.root / "schedule.json", policy)
        return policy

    def status(self):
        if not (self.root / "repository.json").exists():
            return {"configured": False, "schedule": DEFAULT_SCHEDULE.copy()}
        return {"configured": True, "repository": self.settings(), "schedule": self.schedule(),
                "last_backup": read_json(self.root / "last-backup.json")}

    def prefix(self, config):
        return "refs/heads/banana-backups/" + self.product.lower() + "/" + config["name"] + "/"

    def context(self, config, identifier):
        if not SNAPSHOT.fullmatch(identifier):
            raise ValueError("Select a complete snapshot ID from backups list.")
        return {"product": self.product, "series": config["name"], "snapshot": identifier}

    def ensure_identity(self, git, config):
        """A small permanent branch prevents accidental key changes in a series."""
        ref = self.prefix(config) + "identity"
        expected = {"schema": 1, "product": self.product, "series": config["name"],
                    "recipient": crypto.recipient(self.identity)}
        sha = git.ref_sha(ref)
        if sha:
            entries = git.fetch(ref, sha)
            if set(entries) != {"index.json"} or json.loads(git.run("cat-file", "blob", entries["index.json"][0])) != expected:
                raise ValueError("This backup series uses a different recovery key. Restore its original key or choose a new series name.")
        else:
            if git.heads(self.prefix(config)):
                raise ValueError("The backup series identity is missing. Recover that branch before uploading or choose a new series name.")
            index = git.work / "index.json"
            write_json(index, expected)
            git.push(git.commit([index]), ref)
            index.unlink()

    @contextmanager
    def transport(self, config):
        check_private(config, self.token)
        directory(self.root)
        with tempfile.TemporaryDirectory(prefix="transfer-", dir=self.root) as name:
            yield Git(Path(name), config, self.token)

    def list(self):
        with lock(self.root):
            config = self.settings()
            with self.transport(config) as git:
                heads = git.heads(self.prefix(config))
            return [{"snapshot": name, "commit": heads[name]} for name in sorted(heads, key=_snapshot_order, reverse=True)]

    def _download(self, git, config, identifier, sha, output):
        maximum = config["max_mib"] * 1024 * 1024
        if shutil.disk_usage(git.work).free < maximum * 3 + 128 * 1024 * 1024:
            raise ValueError("Not enough staging space: reserve three times the configured backup limit plus 128 MiB.")
        entries = git.fetch(self.prefix(config) + identifier, sha)
        index = json.loads(git.run("cat-file", "blob", entries["index.json"][0]))
        expected = self.context(config, identifier)
        if (not isinstance(index, dict) or index.get("schema") != 1
                or any(index.get(key) != value for key, value in expected.items())
                or not isinstance(index.get("parts"), list) or not 1 <= len(index["parts"]) <= 34):
            raise ValueError("Invalid backup index.")
        parts = index["parts"]
        names = [f"part-{number:05d}.age" for number in range(len(parts))]
        if set(entries) != {*names, "index.json"}:
            raise ValueError("The snapshot contains missing or unexpected files.")
        ciphertext = git.work / ("download-" + secrets.token_hex(4) + ".age")
        with ciphertext.open("xb") as output_file:
            ciphertext.chmod(0o600)
            for name, part in zip(names, parts, strict=False):
                if not isinstance(part, dict) or part.get("name") != name or part.get("bytes") != entries[name][1]:
                    raise ValueError("Invalid encrypted backup part.")
                start = output_file.tell()
                git.run("cat-file", "blob", entries[name][0], output=output_file)
                output_file.flush()
                with ciphertext.open("rb") as check:
                    check.seek(start)
                    actual = hashlib.sha256(check.read(entries[name][1])).hexdigest()
                if actual != part.get("sha256"):
                    raise ValueError("An encrypted backup part failed its checksum.")
        return crypto.decrypt(ciphertext, output, self.identity, expected, maximum)

    def download(self, identifier, output):
        with lock(self.root):
            config = self.settings()
            self.context(config, identifier)
            with self.transport(config) as git:
                heads = git.heads(self.prefix(config))
                if identifier not in heads:
                    raise ValueError("The selected backup no longer exists. Use backups list.")
                metadata = self._download(git, config, identifier, heads[identifier], output)
        return {**metadata, "package": str(Path(output).absolute()), "verified": True}

    def verify(self, identifier):
        directory(self.root)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as name:
            result = self.download(identifier, Path(name) / "package.tar.gz")
        result.pop("package")
        return result

    def upload(self, package, *, automatic=False):
        package = Path(package)
        with lock(self.root):
            config = self.settings()
            if automatic and not self.schedule()["enabled"]:
                return {"outcome": "disabled", "package": str(package)}
            maximum = config["max_mib"] * 1024 * 1024
            if package.is_symlink() or not package.is_file() or not 0 < package.stat().st_size <= maximum:
                raise ValueError("The backup exceeds the repository size limit or is not a regular package. Use local backup storage for larger installations.")
            if shutil.disk_usage(self.root).free < package.stat().st_size * 4 + 128 * 1024 * 1024:
                raise ValueError("Not enough free space to encrypt and verify the backup.")
            with self.transport(config) as git:
                self.ensure_identity(git, config)
                old = git.heads(self.prefix(config))
                identifier = _next_snapshot(old)
                context = self.context(config, identifier)
                sealed = git.work / "sealed.age"
                metadata = crypto.encrypt(package, sealed, self.identity, context)
                if sealed.stat().st_size > maximum + OVERHEAD:
                    raise ValueError("The encrypted backup exceeds the size limit.")
                parts, paths = [], []
                with sealed.open("rb") as source:
                    while chunk := source.read(PART_BYTES):
                        path = git.work / f"part-{len(parts):05d}.age"
                        atomic_write(path, chunk)
                        paths.append(path)
                        parts.append({"name": path.name, "bytes": len(chunk), "sha256": digest(path)})
                index = git.work / "index.json"
                write_json(index, {**context, "schema": 1, "parts": parts})
                sha = git.commit([*paths, index])
                if automatic and not self.schedule()["enabled"]:
                    return {"outcome": "disabled", "package": str(package)}
                git.push(sha, self.prefix(config) + identifier)
                # Use another bare repository: verification must actually fetch
                # the uploaded objects instead of reusing the local originals.
                with self.transport(config) as reader:
                    verified = reader.work / "package.tar.gz"
                    downloaded = self._download(reader, config, identifier, sha, verified)
                    if downloaded["sha256"] != metadata["sha256"]:
                        raise ValueError("The uploaded backup did not match the local package.")
                warnings = []
                # Only this product and explicitly configured series are owned.
                # Each retained snapshot is an independent root commit, so old
                # payloads are not kept reachable through its Git ancestry.
                for previous in sorted(old, key=_snapshot_order, reverse=True)[config["keep"] - 1:]:
                    try:
                        git.remove(self.prefix(config) + previous, old[previous])
                    except RuntimeError as error:
                        warnings.append(str(error))
                result = {**context, "outcome": "complete", "commit": sha, "bytes": metadata["bytes"],
                          "sha256": metadata["sha256"], "verified": True,
                          "completed_at": datetime.now(timezone.utc).isoformat(), "retention_warnings": warnings}
                write_json(self.root / "last-backup.json", result)
        return result
