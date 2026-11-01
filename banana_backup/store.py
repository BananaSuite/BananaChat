"""Independent encrypted snapshots, verified uploads, and scoped retention."""

from contextlib import contextmanager
import copy
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
# How far ahead of this server's clock a snapshot ID may be dated and still count (another server's clock).
FUTURE_SKEW = timedelta(days=1)
# Unauthenticated snapshot branches one retention or upload run reads before it stops looking: each costs a
# fetch of up to a snapshot's size, and anyone who can push may make thousands. Retention by a release from
# before authentication left at most --keep (30) of them in a series.
MAX_UNVERIFIED = 40


def _snapshot_order(identifier):
    """Order old second-resolution and new microsecond-resolution identifiers."""
    stamp = identifier.split("Z-", 1)[0]
    return stamp if "." in stamp else stamp + ".000000"


def _snapshot_time(identifier):
    """When *identifier* says its snapshot was made, or None for an ID that is no date.

    Snapshot IDs are branch names: whoever can push to the repository chooses them.
    """
    try:
        return datetime.strptime(_snapshot_order(identifier), "%Y%m%dT%H%M%S.%f").replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _plausible(identifier, now):
    """Whether *identifier* is a date a snapshot could have been made at: not in the future (beyond clock skew)."""
    when = _snapshot_time(identifier)
    return when is not None and when <= now + FUTURE_SKEW


def _next_snapshot(previous):
    """Keep series order monotonic even within one second or after clock drift.

    IDs that are no dates, or dated in the future, are ignored: anyone who
    can push could otherwise make every later ID overflow.
    """
    now = datetime.now(timezone.utc)
    times = [_snapshot_time(name) for name in previous if _plausible(name, now)]
    if times:
        now = max(now, max(times) + timedelta(microseconds=1))
    return now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + secrets.token_hex(4)


def _too_many_unverified():
    return (f"More than {MAX_UNVERIFIED} of the newest snapshot branches are not authenticated with this series' "
            "recovery key, so older snapshots were not read. Check who pushed them to the backup repository, and "
            "delete them there.")


def _staging(size):
    """The free space a download of *size* encrypted bytes needs: its ciphertext, decrypted copy and package."""
    return size * 3 + 128 * 1024 * 1024


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
        if type(value.get("append_only", False)) is not bool:
            raise ValueError("Invalid append-only backup setting.")
        return value

    def schedule(self):
        value = read_json(self.root / "schedule.json", DEFAULT_SCHEDULE.copy())
        if (type(value.get("enabled")) is not bool or type(value.get("interval_minutes")) is not int
                or not 60 <= value["interval_minutes"] <= 10080):
            raise ValueError("Invalid backup schedule.")
        return value

    def configure(self, *, repo, forge, name, token_file, key_file=None, username="git", keep=7, max_mib=512,
                  append_only=False, replace_key=False):
        """Select the series. *append_only* uploads never delete snapshots (see :meth:`prune`).

        Without *key_file* the recovery key this server holds stays. Another
        key replaces it only with *replace_key*: the snapshots made with it
        cannot be restored without it.
        """
        config = {**location(repo, forge), "schema": 1, "product": self.product, "name": name,
                  "username": username, "keep": keep, "max_mib": max_mib, "append_only": bool(append_only)}
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name) or not 2 <= keep <= 30
                or not 1 <= max_mib <= 1024 or not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,100}", username)):
            raise ValueError("Use a lowercase series name, 2–30 snapshots, and a size limit of 1–1024 MiB.")
        if key_file is None:
            if not self.identity.exists():
                raise ValueError("Pass the recovery key with --key-file.")
            key_file = self.identity
        public = crypto.recipient(key_file)
        token = private_bytes(token_file)
        key = private_bytes(key_file, 4096)
        check_private(config, token_file)
        with lock(self.root):
            if self.identity.exists() and not replace_key:
                try:
                    stored = crypto.recipient(self.identity)
                except (OSError, ValueError):
                    stored = None
                if stored != public:
                    raise ValueError("This server holds another recovery key for its backups, and only that key "
                                     "restores the snapshots made with it. Configure with that key (or without "
                                     "--key-file to keep it), or save an offline copy of it and add --replace-key.")
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

    def _download(self, git, config, identifier, sha, output, *, allow_unauthenticated=False, heads=None, size=None):
        """Fetch, check and decrypt a snapshot into *output*; *size* is its encrypted size, when known."""
        maximum = config["max_mib"] * 1024 * 1024
        if shutil.disk_usage(git.work).free < _staging(maximum if size is None else size):
            raise ValueError("Not enough staging space: reserve three times the configured backup limit plus 128 MiB.")
        entries = git.fetch(self.prefix(config) + identifier, sha)
        index = json.loads(git.run("cat-file", "blob", entries["index.json"][0]))
        expected = self.context(config, identifier)
        if (not isinstance(index, dict) or index.get("schema") != 1
                or any(index.get(key) != value for key, value in expected.items())
                or not isinstance(index.get("parts"), list) or not 1 <= len(index["parts"]) <= 34):
            raise ValueError("Invalid backup index.")
        # Before anything is decrypted: age alone cannot tell our snapshots from ones
        # encrypted to the (public) recipient by someone else who can push.
        authenticated = crypto.check_authentication(index, self.identity,
                                                    allow_unauthenticated=allow_unauthenticated)
        if not authenticated:
            self._refuse_downgrade(git, config, identifier, heads or {})
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
        git.discard()
        metadata = crypto.decrypt(ciphertext, output, self.identity, expected, maximum)
        return {**metadata, "authenticated": authenticated}

    def _authenticated_index(self, git, config, identifier, sha):
        """The index of the snapshot on branch *identifier* (head *sha*) if this recovery key authenticated it, else None.

        Only its index is fetched where the repository allows partial fetches.
        Anything wrong with it (no such snapshot, a forged or missing tag, an
        index for another snapshot) is simply "not authenticated".
        """
        try:
            index = json.loads(git.run("cat-file", "blob", git.index(self.prefix(config) + identifier, sha), bounded=True))
            if (not isinstance(index, dict) or index.get("schema") != 1
                    or any(index.get(key) != value for key, value in self.context(config, identifier).items())):
                return None
            return index if crypto.check_authentication(index, self.identity, allow_unauthenticated=True) else None
        except (ValueError, RuntimeError):
            return None

    def _authenticated(self, git, config, identifier, sha):
        """Whether the snapshot on branch *identifier* (head *sha*) has an index this recovery key authenticated."""
        return self._authenticated_index(git, config, identifier, sha) is not None

    def _authenticated_since(self, git, config, heads):
        """The ID of the series' first authenticated snapshot, as its authenticated snapshots in *heads* record it.

        Every authenticated index carries it (``authenticated_since``), so it
        outlives the snapshot itself, which retention deletes in time. An
        index without it (development builds) counts as its own start. None
        when no snapshot in *heads* is authenticated.

        Refused when more than MAX_UNVERIFIED unauthenticated snapshots come
        before the answer: their date would otherwise be taken for the
        series' start, and anyone who can push makes such branches.
        """
        now = datetime.now(timezone.utc)
        oldest, unverified = None, 0
        for name in sorted((name for name in heads if _plausible(name, now)), key=_snapshot_order, reverse=True):
            index = self._authenticated_index(git, config, name, heads[name])
            if index is None:
                unverified += 1
                if unverified > MAX_UNVERIFIED:
                    if oldest is None:
                        raise ValueError(_too_many_unverified())
                    return oldest
                continue
            since = index.get("authenticated_since")
            if isinstance(since, str) and _plausible(since, now) and _snapshot_order(since) <= _snapshot_order(name):
                return since
            oldest = name
        return oldest

    def _refuse_downgrade(self, git, config, identifier, heads):
        """Refuse an unauthenticated snapshot that cannot be one made before snapshots were authenticated.

        ``--allow-unauthenticated`` exists for those. Once a series' uploads
        are authenticated, they all are, and series IDs only grow: an
        unauthenticated snapshot dated at or after the series' first
        authenticated one, or dated in the future, was made by someone else
        who could push to the repository.
        """
        if not _plausible(identifier, datetime.now(timezone.utc)):
            raise ValueError("This snapshot is not authenticated and its ID is not a date it could have been made "
                             "at (dated in the future, or no date at all), so it was not made by this series' "
                             "backups. Do not restore it.")
        since = self._authenticated_since(git, config, {name: sha for name, sha in heads.items() if name != identifier})
        if since is not None and _snapshot_time(identifier) >= _snapshot_time(since):
            raise ValueError(f"This snapshot is not authenticated, but this series' backups have been "
                             f"authenticated since {since}, and this snapshot is not older than that: someone else "
                             "with write access to the backup repository made it, or a release from before "
                             "authentication did after this server went back to it. Do not restore it unless you "
                             "know it is the latter; the backup documentation explains how to recover such a "
                             "snapshot by hand.")

    def _fetch(self, identifier, output, *, allow_unauthenticated):
        config = self.settings()
        self.context(config, identifier)
        with self.transport(config) as git:
            heads = git.heads(self.prefix(config))
            if identifier not in heads:
                raise ValueError("The selected backup no longer exists. Use backups list.")
            metadata = self._download(git, config, identifier, heads[identifier], output,
                                      allow_unauthenticated=allow_unauthenticated, heads=heads)
        return {**metadata, "package": str(Path(output).absolute()), "verified": True}

    def download(self, identifier, output, *, allow_unauthenticated=False):
        with lock(self.root):
            return self._fetch(identifier, output, allow_unauthenticated=allow_unauthenticated)

    @contextmanager
    def fetched(self, identifier, *, allow_unauthenticated=False):
        """Download and check a snapshot into a working directory, holding the lock until the caller is done.

        Yields the result of :meth:`download`; its ``package`` is removed afterwards.
        """
        with lock(self.root), tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as name:
            yield self._fetch(identifier, Path(name) / "package.tar.gz", allow_unauthenticated=allow_unauthenticated)

    def verify(self, identifier, *, allow_unauthenticated=False):
        with self.fetched(identifier, allow_unauthenticated=allow_unauthenticated) as result:
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
            # Checked before anything is pushed: the upload keeps its committed
            # objects (about the encrypted size) while it verifies.
            encrypted = package.stat().st_size + OVERHEAD
            if shutil.disk_usage(self.root).free < encrypted + _staging(encrypted):
                raise ValueError("Not enough free space to encrypt and verify the backup.")
            with self.transport(config) as git:
                self.ensure_identity(git, config)
                old = git.heads(self.prefix(config))
                identifier = _next_snapshot(old)
                context = self.context(config, identifier)
                since = self._authenticated_since(git, config, old) or identifier
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
                sealed.unlink()
                index = git.work / "index.json"
                write_json(index, crypto.authenticate({**context, "schema": 1, "parts": parts,
                                                       "authenticated_since": since}, self.identity))
                sha = git.commit([*paths, index])
                for path in paths:
                    path.unlink()
                if automatic and not self.schedule()["enabled"]:
                    return {"outcome": "disabled", "package": str(package)}
                # Again with what this upload holds by now (snapshots read whole where the repository
                # allows no partial fetches): a snapshot that cannot be verified is not pruned either.
                size = sum(part["bytes"] for part in parts)
                if shutil.disk_usage(self.root).free < _staging(size):
                    raise ValueError("Not enough free space to encrypt and verify the backup.")
                git.push(sha, self.prefix(config) + identifier)
                # Use another bare repository: verification must actually fetch
                # the uploaded objects instead of reusing the local originals.
                with self.transport(config) as reader:
                    verified = reader.work / "package.tar.gz"
                    downloaded = self._download(reader, config, identifier, sha, verified, size=size)
                    if downloaded["sha256"] != metadata["sha256"]:
                        raise ValueError("The uploaded backup did not match the local package.")
                # An append-only series is pruned only by an explicit prune, so its
                # token needs no right to delete branches.
                warnings = [] if config.get("append_only") else self._prune(git, config, old, config["keep"] - 1)[1]
                result = {**context, "outcome": "complete", "commit": sha, "bytes": metadata["bytes"],
                          "sha256": metadata["sha256"], "verified": True,
                          "completed_at": datetime.now(timezone.utc).isoformat(), "retention_warnings": warnings}
                write_json(self.root / "last-backup.json", result)
        return result

    def _prune(self, git, config, heads, keep):
        """Delete all but the *keep* newest authenticated snapshots of *heads*; return ``(removed, warnings)``.

        Snapshot IDs are branch names anyone who can push chooses, so only
        snapshots this series' recovery key authenticated count, newest
        first, and nothing is deleted while fewer than *keep* of them would
        remain: everything older than the *keep*-th is deleted. Snapshots
        that are not authenticated but newer (made before snapshots were
        authenticated, or by someone else) are kept, uncounted, until enough
        authenticated ones replace them. IDs dated in the future or that are
        no dates are neither counted nor deleted. Both are reported. Past
        MAX_UNVERIFIED unauthenticated snapshots, the older ones are not read
        and nothing more is deleted.
        """
        removed, warnings, unverified = [], [], []
        now = datetime.now(timezone.utc)
        implausible = sorted(name for name in heads if not _plausible(name, now))
        if implausible:
            warnings.append("Not counted and not deleted, as no backup of this series can have made them (dated in "
                            "the future, or no date): " + ", ".join(implausible) + ". Check who pushed them to the "
                            "backup repository, and delete them there.")
        counted = 0
        # Only this product and explicitly configured series are owned.
        # Each retained snapshot is an independent root commit, so old
        # payloads are not kept reachable through its Git ancestry.
        for name in sorted((name for name in heads if _plausible(name, now)), key=_snapshot_order, reverse=True):
            if counted < keep:
                if len(unverified) >= MAX_UNVERIFIED:
                    warnings.append(_too_many_unverified() + " Retention deleted nothing older.")
                    break
                if self._authenticated(git, config, name, heads[name]):
                    counted += 1
                else:
                    unverified.append(name)
                continue
            try:
                git.remove(self.prefix(config) + name, heads[name])
                removed.append(name)
            except RuntimeError as error:
                warnings.append(str(error))
        if unverified:
            warnings.append("Not authenticated with this series' recovery key, so not counted and kept: "
                            + ", ".join(unverified) + ". They were made before backups were authenticated, or by "
                            "someone else with write access to the backup repository; once enough authenticated "
                            "snapshots are newer, retention deletes them.")
        return removed, warnings

    def prune(self, token_file=None):
        """Apply the retention setting now, optionally with a token used for this command only.

        Append-only series are pruned this way: the stored upload token can
        then lack the right to delete branches (or the snapshot branches can
        be protected), and the operator supplies a deleting token only here.
        """
        with lock(self.root):
            config = self.settings()
            pruner = copy.copy(self)
            if token_file is not None:
                private_bytes(token_file)
                pruner.token = Path(token_file).absolute()
            with pruner.transport(config) as git:
                removed, warnings = self._prune(git, config, git.heads(self.prefix(config)), config["keep"])
        return {"removed": sorted(removed, key=_snapshot_order), "kept": config["keep"], "warnings": warnings}
