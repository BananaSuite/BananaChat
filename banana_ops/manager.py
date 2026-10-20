"""Install, checkpoint, update, restore, and remove one remembered deployment."""

from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tarfile

from .files import (absolute_path, atomic_write, extract_archive, maintenance_lock, read_environment,
                    read_json, read_package, regular_files, write_environment, write_json, write_package)
from . import profile
from .source import GitSource, valid_branch, valid_track, valid_url
from .system import System


DEFAULT_POLICY = {"enabled": False, "interval_minutes": 60, "keep_backups": 3}
# Packages this tool creates on its own; each prefix keeps the keep_backups
# newest. Operator-requested "manual-" packages are never pruned.
AUTOMATIC_BACKUPS = ("auto", "before-update", "before-restore", "before-legacy-import", "remote")
HISTORY_LIMIT = 1000
# Refuse to purge these even when they look like a managed installation.
PROTECTED_ROOTS = ("/", "/usr", "/usr/local", "/opt", "/home", "/var", "/etc", "/srv", "/root")


def declared_version(release, package):
    """The ``<package>.__version__`` a release tree declares (read as data, never imported), or None."""
    import ast
    try:
        tree = ast.parse((release / package / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None
    for node in tree.body:
        # ``__version__ = "X.Y.Z"`` or, annotated, ``__version__: str = "X.Y.Z"``.
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if (any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            match = re.match(r"(\d+)\.(\d+)\.(\d+)", node.value.value)
            return (int(match[1]), int(match[2]), int(match[3])) if match else None
    return None


def check_dependency_locks(release, package, *, allow_unpinned=False):
    """Refuse a release that should ship hash-pinned dependency locks and does not; return whether it is unpinned.

    1.7 and later releases install exactly the files ``requirements.lock``
    and ``requirements-build.lock`` pin. Without them the installer
    resolves the newest versions ``requirements.txt`` allows, from PyPI,
    with no hash: right for a release from before 1.7 (a rollback), which
    never had locks, never for a later one missing them by mistake. Only
    the operator's explicit *allow_unpinned* deploys such a release. A tree
    that declares no version is from before 1.7.
    """
    names = ("requirements.lock", "requirements-build.lock")
    present = [(release / name).is_file() for name in names]
    if all(present):
        return False
    version = declared_version(release, package)
    # One lock without the other, or none in a release that says it is 1.7 or later: a release made by mistake.
    if not any(present) and (version is None or version < (1, 7, 0)) or allow_unpinned:
        return True
    missing = " and ".join(name for name, found in zip(names, present, strict=True) if not found)
    raise ValueError(f"This release lacks {missing}: since 1.7 a release installs exactly the dependency versions "
                     "and files its locks pin, and without them every dependency would be resolved anew from PyPI "
                     "with no hash check. Nothing was deployed. Ask its maintainers for a release with its locks; "
                     "to install this one from its requirements.txt ranges anyway, run the update (with "
                     "--retry-failed) or the restore again with --allow-unpinned.")


class Manager:
    def __init__(self, root, *, product=profile.PRODUCT, system=None):
        self.root = absolute_path(root)
        self.product = product
        self.system = system or System()
        self.config_dir = self.root / "config"
        if isinstance(self.system, System):
            self.system.log_dir = self.config_dir

    def settings(self):
        data = read_json(self.config_dir / "installation.json")
        if not data or data.get("schema") != 1 or data.get("product") != self.product:
            raise ValueError("No matching managed installation was found. Use install or install --restore PACKAGE.")
        if data.get("mode") not in profile.modes(self.product) or data.get("root") != str(self.root):
            raise ValueError("Installation metadata does not match this root or deployment mode.")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,45}", data.get("service", "")):
            raise ValueError("Invalid service name in installation metadata.")
        return data

    def save(self, settings):
        write_json(self.config_dir / "installation.json", settings)

    def policy(self):
        data = read_json(self.config_dir / "updates.json", DEFAULT_POLICY.copy())
        if type(data.get("enabled")) is not bool or not 5 <= int(data.get("interval_minutes", 0)) <= 10080:
            raise ValueError("Invalid automatic-update policy.")
        if not 2 <= int(data.get("keep_backups", 0)) <= 30:
            raise ValueError("Keep between 2 and 30 automatic backups.")
        return data

    def source(self):
        """``config/source.json``; one written before 1.7 has no ``track`` and keeps following its branch."""
        data = read_json(self.config_dir / "source.json")
        if not data:
            raise ValueError("No update source is configured.")
        valid_branch(data["branch"])
        if data.get("fallback_branch"):
            valid_branch(data["fallback_branch"])
        return {**data, "track": valid_track(data.get("track", "branch"))}

    def event(self, operation, outcome, **details):
        value = {"time": datetime.now(timezone.utc).isoformat(), "operation": operation, "outcome": outcome, **details}
        write_json(self.config_dir / "status.json", value)
        path = self.config_dir / "history.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as target:
            target.write(json.dumps(value) + "\n")
        self.trim_history(path)
        return value

    @staticmethod
    def trim_history(path, limit=HISTORY_LIMIT):
        """Keep only the newest events, replacing the log atomically."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            count, tail = 0, deque(maxlen=limit)
            for line in source:
                count += 1
                tail.append(line)
        if count > limit:
            atomic_write(path, b"".join(tail))

    def layout(self):
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in ("config", "backups", "staging", "releases", "data", "site"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("The managed installation contains a directory symlink.")
            path.mkdir(mode=0o755 if name in {"releases", "site"} else 0o700, exist_ok=True)

    def configure_source(self, *, url=None, branch=None, fallback=None, clear_fallback=False, token_file=None,
                         username="git", ssh_key=None, known_hosts=None, clear_credentials=False, allow_local=False,
                         signers_file=None, clear_signatures=False, track=None):
        # A new source follows release tags; one configured before 1.7 keeps its branch unless told otherwise.
        old = read_json(self.config_dir / "source.json", {"url": "https://github.com/BananaSuite/" + self.product + ".git", "branch": "main", "fallback_branch": None, "auth": "none", "signing": "none", "track": "tags"})
        new = dict(old)
        if track:
            new["track"] = valid_track(track)
        if url:
            new["url"] = valid_url(url, allow_local=allow_local)
            from urllib.parse import urlsplit
            def credential_host(value):
                return value.split('@', 1)[-1].split(':', 1)[0] if '@' in value and '://' not in value else urlsplit(value).hostname
            if credential_host(old["url"]) != credential_host(new["url"]) and not token_file and not ssh_key:
                new["auth"] = "none"
        if branch:
            new["branch"] = valid_branch(branch)
        if fallback:
            new["fallback_branch"] = valid_branch(fallback)
        elif clear_fallback:
            new["fallback_branch"] = None
        if sum(bool(item) for item in (token_file, ssh_key, clear_credentials)) > 1:
            raise ValueError("Choose one repository authentication method.")
        if token_file:
            secret = self.read_secret(token_file)
            if any(char.isspace() for char in username) or not username:
                raise ValueError("Invalid repository username.")
            if not new["url"].startswith("https://"):
                raise ValueError("HTTPS access tokens require an HTTPS repository URL.")
            atomic_write(self.config_dir / "repo.token", secret + "\n")
            for name in ("repo.key", "repo.known_hosts"):
                (self.config_dir / name).unlink(missing_ok=True)
            new.update(auth="token", username=username)
        elif ssh_key:
            if not known_hosts:
                raise ValueError("Supply a verified --known-hosts file with an SSH deploy key.")
            if new["url"].startswith("https://"):
                raise ValueError("SSH deploy keys require an SSH repository URL.")
            atomic_write(self.config_dir / "repo.key", self.read_secret(ssh_key, multiline=True) + "\n")
            atomic_write(self.config_dir / "repo.known_hosts", self.read_secret(known_hosts, multiline=True) + "\n")
            (self.config_dir / "repo.token").unlink(missing_ok=True)
            new.update(auth="ssh")
        elif clear_credentials:
            for filename in ("repo.token", "repo.key", "repo.known_hosts", "git-askpass"):
                (self.config_dir / filename).unlink(missing_ok=True)
            new.update(auth="none")
        if signers_file and clear_signatures:
            raise ValueError("Choose either an allowed signers file or clearing signature checks.")
        if signers_file:
            atomic_write(self.config_dir / "repo.allowed_signers",
                         self.read_secret(signers_file, multiline=True) + "\n")
            new["signing"] = "ssh"
        elif clear_signatures:
            (self.config_dir / "repo.allowed_signers").unlink(missing_ok=True)
            new["signing"] = "none"
        new.setdefault("signing", "none")
        write_json(self.config_dir / "source.json", new)
        return new

    @staticmethod
    def read_secret(path, multiline=False):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Use a regular credential file smaller than 1 MiB.")
        value = path.read_text().strip()
        if not value or "\0" in value or (not multiline and any(char.isspace() for char in value)):
            raise ValueError("Invalid credential file.")
        return value

    def stage(self, settings, *, archive=None, allow_unpinned=False):
        revision = settings["revision"]
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ValueError("Invalid deployment revision.")
        release = self.root / "releases" / revision
        if (release / ".release.json").exists():
            return release
        if release.exists():
            if (self.root / "current").resolve() == release.resolve():
                raise ValueError("The current release is incomplete; restore a backup before updating.")
            shutil.rmtree(release)
        release.mkdir(mode=0o755)
        archive_file = self.root / "staging" / (revision + ".tar.gz")
        if archive:
            shutil.copyfile(archive, archive_file)
        else:
            GitSource(self.root, self.source()).archive(revision, archive_file)
        try:
            try:
                extract_archive(archive_file, release, max_bytes=4 * 1024 ** 3)
            except tarfile.TarError as exc:
                # A revision that deleted every tracked file archives to
                # something tarfile refuses to open. Report the revision
                # rather than the tar internals; nothing has been deployed.
                raise ValueError("The selected revision produced no readable source. Choose a compatible branch.") from exc
            if not (release / "banana").is_file() or not (release / "LICENSE").is_file():
                raise ValueError("The selected revision does not support the managed lifecycle. Choose a compatible branch.")
            unpinned = check_dependency_locks(release, self.product.lower(), allow_unpinned=allow_unpinned)
            if unpinned:
                print("Warning: this release has no dependency locks, so its dependencies are installed from the "
                      "ranges in requirements.txt, without hash checks.", file=sys.stderr, flush=True)
            self.system.prepare_release(settings, release)
            shutil.copyfile(archive_file, release / ".source.tar.gz")
            write_json(release / ".release.json", {"revision": revision, "product": self.product,
                                                   **({"unpinned": True} if unpinned else {})})
            return release
        except BaseException:
            if release.exists():
                shutil.rmtree(release)
            raise
        finally:
            archive_file.unlink(missing_ok=True)

    def switch(self, revision):
        release = self.root / "releases" / revision
        if not (release / ".release.json").exists():
            raise ValueError("The requested release has not been prepared.")
        temporary = self.root / (".current-" + secrets.token_hex(5))
        temporary.symlink_to(Path("releases") / revision)
        os.replace(temporary, self.root / "current")

    def write_runtime(self, settings, previous=None):
        current = read_environment(self.config_dir / "app.env") if previous is None else previous
        values = profile.environment(settings, current)
        write_environment(self.config_dir / "app.env", values)
        (self.root / "data/logs").mkdir(mode=0o700, parents=True, exist_ok=True)
        if settings["mode"] == "compute":
            # The gateway's token, and its model management token where the configuration names the
            # managed one (new installations; an operator-chosen file is the operator's to create).
            tokens = [self.root / "data/.compute-api-token"]
            management = self.root / "data/.compute-management-token"
            if values.get("BC_COMPUTE_MANAGEMENT_TOKEN_FILE") == str(management):
                tokens.append(management)
            for path in tokens:
                if not path.exists():
                    atomic_write(path, secrets.token_hex(32) + "\n")
        if settings["mode"] == "hosting":
            (self.root / "data/instances").mkdir(mode=0o700, parents=True, exist_ok=True)
            profile.prepare_hosting_storage(self.root / "data")
            if not any((self.root / "site").iterdir()):
                site = self.root / "current/site"
                if site.is_dir():
                    shutil.copytree(site, self.root / "site", dirs_exist_ok=True)
        self.system.data_permissions(settings)

    def install(self, checkout, *, mode, name=None, domain="", portal_domain="", port=None, backend_url="",
                backend_token_file=None, ollama_binary="", source_options=None, reuse_data=False,
                backend_token=None, ollama_url="", backend_management_token=None):
        self.layout()
        with maintenance_lock(self.root):
            existing = read_json(self.config_dir / "installation.json")
            if existing and (existing.get("installed") or not reuse_data):
                raise ValueError("An installation already exists. Use update, restore, or install --reuse-data after uninstalling.")
            if existing and existing.get("mode") != mode:
                raise ValueError("Changing deployment mode requires a separate root and an explicit data migration.")
            if mode not in profile.modes(self.product):
                raise ValueError("Choose a supported deployment mode.")
            name = name or self.product.lower()
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,45}", name):
                raise ValueError("Use a lowercase service name with letters, digits, and hyphens.")
            domain = profile.validate_domain(domain)
            portal_domain = profile.validate_domain(portal_domain or ("portal." + domain if mode == "hosting" and domain else ""))
            if mode == "web" and not backend_url:
                raise ValueError("A web server needs its compute server: use --pair with the code printed by "
                                 "'bananachat compute pairing-code' there, or --backend-url with --backend-token-file.")
            if backend_url:
                from .backend import backend_url as checked_backend_url
                backend_url = checked_backend_url(backend_url)
            if ollama_url:
                from .backend import ollama_url as checked_ollama_url
                if self.product != "BananaChat" or mode not in {"single", "compute"}:
                    raise ValueError("--ollama-url applies to BananaChat single and compute servers.")
                ollama_url, ollama_binary = checked_ollama_url(ollama_url), ""
            elif mode in {"single", "compute"}:
                ollama_binary = ollama_binary or shutil.which("ollama") or ""
                if not ollama_binary or not Path(ollama_binary).is_absolute() or not Path(ollama_binary).is_file():
                    raise ValueError("Install Ollama from its official packages and provide --ollama-binary if it is "
                                     "outside PATH, or use an Ollama that already runs here with --ollama-url.")
            else:
                ollama_binary = ""
            port = int(port or {"wiki": 5001, "hosting": 5099, "single": 8000, "web": 8000, "compute": 11435}[mode])
            if not 1024 <= port <= 65535:
                raise ValueError("Choose an application port between 1024 and 65535.")
            # Read every operator-supplied secret before changing any state, so
            # a bad file cannot leave a half-installed root behind.
            if backend_token:
                from .backend import check_token
                backend_token = check_token(backend_token)
            elif backend_token_file:
                backend_token = self.read_secret(backend_token_file)
            if backend_management_token:
                from .backend import check_management_token
                backend_management_token = check_management_token(backend_management_token, backend_token)
            source = self.configure_source(**(source_options or {}))
            settings = {"schema": 1, "product": self.product, "mode": mode, "root": str(self.root), "service": name,
                        "domain": domain, "portal_domain": portal_domain, "port": port, "backend_url": backend_url,
                        "ollama_binary": ollama_binary, "source_url": source["url"], "installed": False, "revision": ""}
            if self.product == "BananaChat" and mode in {"single", "compute"}:
                # Empty: this installation runs and manages its own Ollama.
                settings["ollama_url"] = ollama_url
            self.system.preflight(settings)
            self.system.account(settings)
            settings["revision"] = GitSource(self.root, source).seed(checkout)
            self.stage(settings)
            self.save(settings)
            self.switch(settings["revision"])
            environment = read_environment(self.config_dir / "app.env")
            if backend_token:
                environment["BC_OLLAMA_API_KEY"] = backend_token
            if backend_management_token:
                environment["BC_OLLAMA_MANAGEMENT_API_KEY"] = backend_management_token
            self.write_runtime(settings, environment)
            write_json(self.config_dir / "updates.json", DEFAULT_POLICY.copy())
            self.system.install_units(settings)
            self.system.install_timer(settings, self.policy())
            atomic_write(self.root / "data/.banana-maintenance", "Installing\n")
            self.system.start(list(profile.service_commands(settings)))
            if not self.system.healthy(settings):
                self.event("install", "failed", revision=settings["revision"], reason="Initial health checks failed; maintenance mode remains active.")
                raise RuntimeError("Initial health checks failed. Inspect the service journal, correct configuration, and run install --reuse-data.")
            settings["installed"] = True
            self.save(settings)
            (self.root / "data/.banana-maintenance").unlink(missing_ok=True)
            return self.event("install", "complete", revision=settings["revision"], automatic_updates=False)

    def snapshot_state(self, settings):
        return {"settings": settings, "active": [name for name in profile.service_commands(settings) if self.system.active(name)],
                "containers": self.system.containers(settings), "backup": None, "candidate": None, "phase": "preparing"}

    def tenant_maintenance(self, settings, enabled):
        if settings["mode"] != "hosting" or not (self.root / "data/instances").is_dir():
            return
        for directory in (self.root / "data/instances").iterdir():
            if directory.is_dir() and not directory.is_symlink():
                marker = directory / ".banana-maintenance"
                if enabled:
                    atomic_write(marker, "Hosting maintenance in progress\n", 0o644)
                else:
                    marker.unlink(missing_ok=True)

    def quiesce(self, journal):
        write_json(self.config_dir / "transaction.json", journal)
        atomic_write(self.root / "data/.banana-maintenance", "Maintenance in progress\n")
        self.tenant_maintenance(journal["settings"], True)
        self.system.stop(list(profile.service_commands(journal["settings"])))
        self.system.stop_containers(journal["containers"])

    def package_inputs(self, settings, *, exclude_model_weights=False, rollback=False):
        """What :meth:`package` writes: ``(inputs, excluded weight paths, model inventory or None)``."""
        data = self.root / "data"
        excluded, model_inventory = [], None
        if self.product == "BananaChat" and (exclude_model_weights or rollback):
            from .models import inventory
            environment = read_environment(self.config_dir / "app.env")
            database = Path(environment.get("BC_DATABASE_PATH", data / "bananachat.db")).absolute()
            unmanaged = settings["mode"] in {"single", "compute"} and not profile.managed_ollama(settings)
            if not database.is_relative_to(data):
                if not rollback:
                    raise ValueError("Move the BananaChat database inside the managed data directory before making a weight-free package; external databases need a separate backup.")
            else:
                try:
                    # A rollback package never asks another Ollama: the weights stay where they are.
                    model_inventory = inventory(data, environment, upstream=profile.ollama_upstream(settings, environment)
                                                if unmanaged and not rollback else None)
                except ValueError:
                    if not rollback:
                        raise
                excluded = model_inventory["excluded_paths"] if model_inventory else []
        allowed_link = (lambda path: profile.hosting_storage_link(path, data)) if settings["mode"] == "hosting" else None
        inputs = [(path, "data/" + name) for path, name in regular_files(data, allowed_link=allowed_link, excluded=excluded)]
        inputs.extend((path, "site/" + name) for path, name in regular_files(self.root / "site"))
        for name in ("installation.json", "source.json", "app.env", "repo.token", "repo.key", "repo.known_hosts",
                     "repo.allowed_signers", "updates.json"):
            path = self.config_dir / name
            if path.is_file() and not path.is_symlink():
                inputs.append((path, "config/" + name))
        inputs.append((self.root / "releases" / settings["revision"] / ".source.tar.gz", "source.tar.gz"))
        return inputs, excluded, model_inventory

    def package(self, settings, destination, *, exclude_model_weights=False, rollback=False):
        """Write a portable package of this installation.

        *exclude_model_weights* leaves re-downloadable model weights out and
        records what to download again. *rollback* does the same for the
        package taken before an update, restore or import: those never change
        the weights, so a rollback keeps them where they are (see
        :meth:`apply_package`) and only a restore on another server downloads
        them again. Gzipping a model library into every such package would
        keep the service down for as long and need its size again in free space.
        """
        inputs, excluded, model_inventory = self.package_inputs(settings, exclude_model_weights=exclude_model_weights,
                                                                rollback=rollback)
        data = self.root / "data"
        manifest = {"product": self.product, "mode": settings["mode"], "revision": settings["revision"],
                    "created_at": datetime.now(timezone.utc).isoformat(), "old_root": str(self.root),
                    "model_weights_excluded": model_inventory is not None, "excluded_paths": excluded}
        if rollback and model_inventory is not None:
            manifest["weights_in_place"] = [name for name in excluded if (data / name).exists()]
        inventory_file = self.root / "staging" / ("model-inventory-" + secrets.token_hex(6) + ".json")
        try:
            if model_inventory is not None:
                write_json(inventory_file, model_inventory)
                inputs.append((inventory_file, "model-inventory.json"))
            return write_package(destination, manifest, inputs)
        finally:
            inventory_file.unlink(missing_ok=True)

    def check_package_space(self, settings, *, copies, exclude_model_weights=False, rollback=False):
        """Refuse, before anything stops, an operation whose package (and rollback) cannot fit.

        *copies* is 2 when a rollback may follow: its package, and that package
        extracted again to put the data back.
        """
        inputs = self.package_inputs(settings, exclude_model_weights=exclude_model_weights, rollback=rollback)[0]
        total = sum(path.stat().st_size for path, _ in inputs)
        needed = copies * total + 128 * 1024 * 1024
        free = shutil.disk_usage(self.root / "backups").free
        if free < needed:
            raise ValueError(f"Not enough free space: this needs about {needed // 1024 ** 2:,} MiB in {self.root} "
                             f"for its backup{' and a possible rollback' if copies > 1 else ''}, and "
                             f"{free // 1024 ** 2:,} MiB are free. Nothing was stopped or changed.")

    def finish(self, journal, settings, *, old_containers=False):
        if old_containers:
            self.system.resume_containers(journal["containers"])
        self.system.start(list(profile.service_commands(settings)))
        if not self.system.healthy(settings, journal["containers"]):
            raise RuntimeError("The application or one of its hosted instances failed its readiness checks.")
        inactive = [name for name in profile.service_commands(settings) if name not in journal["active"]]
        self.system.stop(inactive)
        self.tenant_maintenance(settings, False)
        (self.root / "data/.banana-maintenance").unlink(missing_ok=True)
        (self.config_dir / "transaction.json").unlink(missing_ok=True)

    def backup_name(self, prefix):
        return self.root / "backups" / (prefix + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3) + ".tar.gz")

    def backup(self, destination=None, *, exclude_model_weights=False):
        with maintenance_lock(self.root):
            self.recover()
            settings = self.settings()
            destination = Path(destination).absolute() if destination else self.backup_name("manual")
            for directory in (self.root / "data", self.root / "site", self.root / "releases"):
                if destination.is_relative_to(directory):
                    raise ValueError("Write backup packages outside the data, site, and release directories.")
            self.check_package_space(settings, copies=1, exclude_model_weights=exclude_model_weights)
            journal = self.snapshot_state(settings)
            try:
                self.quiesce(journal)
                result = self.package(settings, destination, exclude_model_weights=exclude_model_weights)
                self.finish(journal, settings, old_containers=True)
            except BaseException:
                self.recover()
                raise
            warnings = self.prune_backups_safely()
            self.event("backup", "complete", package=str(result), **({"pruning_warnings": warnings} if warnings else {}))
            return result

    def update(self, *, automatic=False, allow_divergent=False, retry_failed=False, allow_unpinned=False):
        with maintenance_lock(self.root):
            self.recover()
            settings = self.settings()
            if automatic and not self.policy()["enabled"]:
                return {"outcome": "disabled"}
            if automatic and not self.system.active(settings["service"]):
                return self.event("update", "skipped", reason="The application was intentionally stopped.")
            configured = self.source()
            source = GitSource(self.root, configured)
            sha, selected, forward = source.resolve(settings["revision"])
            # What was deployed: the release, or the branch (and whether it was the fallback).
            followed = ({"release": selected} if configured["track"] == "tags"
                        else {"branch": selected, "used_fallback": selected != configured["branch"]})
            if source.skipped_releases:
                # Anyone who can push tags can push one that does not verify: reported, never deployed.
                followed["skipped_releases"] = source.skipped_releases
                print(f"Warning: the update source has release tags above {selected} that are not signed by a key "
                      f"in the allowed signers file, so they were skipped: {', '.join(source.skipped_releases)}. "
                      "Check who pushed them.", file=sys.stderr, flush=True)
            if sha == settings["revision"]:
                return self.event("update", "current", revision=sha,
                                  **{key: value for key, value in followed.items() if key != "used_fallback"})
            if not forward and (automatic or not allow_divergent):
                return self.event("update", "paused", revision=sha, reason="The selected source diverges from the installed revision. Review it and use update --allow-divergent for an intentional switch.")
            failed = read_json(self.config_dir / "failed-revision.json", {})
            if failed.get("revision") == sha and not retry_failed:
                # Neither path repeats a failed revision silently; an operator
                # opts in with --retry-failed after correcting the cause.
                reason = ("This revision previously failed; a maintainer must retry it or select a newer revision." if automatic
                          else "This revision previously failed. Correct the cause, then run update --retry-failed to try it again, or publish a newer revision.")
                return self.event("update", "paused", revision=sha, reason=reason)
            candidate = {**settings, "revision": sha, "source_url": self.source()["url"]}
            try:
                self.stage(candidate, allow_unpinned=allow_unpinned and not automatic)
            except BaseException:
                write_json(self.config_dir / "failed-revision.json", {"revision": sha})
                self.event("update", "failed", revision=sha, reason="Release preparation failed; the running version was left in place.")
                raise
            if automatic and not self.policy()["enabled"]:
                return self.event("update", "cancelled", reason="Automatic updates were disabled while preparing the release.")
            try:
                self.check_package_space(settings, copies=2, rollback=True)
            except ValueError as error:
                # Not this revision's failure: the next run tries it again.
                self.event("update", "failed", revision=sha, reason=str(error))
                raise
            journal = self.snapshot_state(settings)
            journal["candidate"] = sha
            try:
                self.quiesce(journal)
                archive = self.package(settings, self.backup_name("auto" if automatic else "before-update"),
                                       rollback=True)
                journal.update(backup=str(archive), phase="backed_up")
                write_json(self.config_dir / "transaction.json", journal)
                self.system.remove_containers(journal["containers"])
                journal["containers_removed"] = True
                write_json(self.config_dir / "transaction.json", journal)
                self.switch(sha)
                environment = read_environment(self.config_dir / "app.env")
                source_key = "BW_SOURCE_URL" if self.product == "BananaWiki" else "BC_SOURCE_URL"
                if environment.get(source_key) == profile.source_link(settings["source_url"]):
                    environment[source_key] = profile.source_link(candidate["source_url"])
                self.write_runtime(candidate, environment)
                self.system.install_units(candidate)
                self.save(candidate)
                self.finish(journal, candidate)
            except BaseException:
                write_json(self.config_dir / "failed-revision.json", {"revision": sha})
                self.recover()
                self.event("update", "rolled_back", revision=sha, restored_revision=settings["revision"])
                raise
            # The update has succeeded: record it before any housekeeping, so a
            # pruning problem cannot report a working deployment as failed.
            write_json(self.config_dir / "last-update.json", {"backup": str(archive), "previous_revision": settings["revision"], "revision": sha})
            if failed.get("revision") == sha:
                (self.config_dir / "failed-revision.json").unlink(missing_ok=True)
            warnings = self.prune_backups_safely()
            try:
                self.prune_releases({settings["revision"], sha})
            except (OSError, ValueError) as error:
                warnings.append(f"Release pruning failed: {error}")
            unpinned = (read_json(self.root / "releases" / sha / ".release.json", {}) or {}).get("unpinned")
            return self.event("update", "complete", revision=sha, previous_revision=settings["revision"], **followed,
                              backup=str(archive), **({"dependencies_unpinned": True} if unpinned else {}),
                              **({"pruning_warnings": warnings} if warnings else {}))

    def restored_settings(self, extracted, manifest, *, name=None, domain=None, port=None, ollama_url=None,
                          existing=None):
        saved = read_json(extracted / "config/installation.json")
        if not saved or saved.get("product") != self.product or saved.get("mode") not in profile.modes(self.product) or saved.get("revision") != manifest["revision"]:
            raise ValueError("The package's installation metadata is inconsistent.")
        saved.update(root=str(self.root), installed=True)
        if name:
            saved["service"] = name
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,45}", saved.get("service", "")):
            raise ValueError("Invalid restored service name.")
        if (not ollama_url and existing and existing.get("mode") == saved["mode"] and self.product == "BananaChat"
                and saved["mode"] in {"single", "compute"}):
            # Which Ollama runs here belongs to this machine, like the service
            # name: a restored package neither replaces an existing Ollama with
            # a managed one nor the other way round.
            if existing.get("ollama_url"):
                ollama_url = existing["ollama_url"]
            else:
                saved.update(ollama_url="", ollama_binary=existing.get("ollama_binary") or "")
        if ollama_url:
            from .backend import ollama_url as checked_ollama_url
            if self.product != "BananaChat" or saved["mode"] not in {"single", "compute"}:
                raise ValueError("--ollama-url applies to BananaChat single and compute servers.")
            saved.update(ollama_url=checked_ollama_url(ollama_url), ollama_binary="")
        if profile.managed_ollama(saved) and not Path(saved.get("ollama_binary", "")).is_file():
            saved["ollama_binary"] = shutil.which("ollama") or ""
            if not saved["ollama_binary"]:
                raise ValueError("Install Ollama on the new server before restoring this deployment mode, or use an "
                                 "Ollama that already runs there with --ollama-url.")
        if domain is not None:
            saved["domain"] = profile.validate_domain(domain)
            if saved["mode"] == "hosting":
                saved["portal_domain"] = "portal." + saved["domain"] if saved["domain"] else ""
        if port is not None:
            if not 1024 <= int(port) <= 65535:
                raise ValueError("Choose an application port between 1024 and 65535.")
            saved["port"] = int(port)
        return saved

    @staticmethod
    def weight_names(manifest):
        """The model weight paths a package left out, checked: they come from the package."""
        names = manifest.get("excluded_paths") if manifest.get("model_weights_excluded") else None
        if not isinstance(names, list) or len(names) > 100:
            return []
        valid = []
        for name in names:
            path = PurePosixPath(name) if isinstance(name, str) else None
            if (path is None or path.is_absolute() or not path.parts or "\\" in name
                    or any(part in {"", ".", ".."} for part in path.parts)):
                raise ValueError("The package lists an invalid model weight path.")
            valid.append(path.as_posix())
        return sorted(set(valid))

    def set_weights_aside(self, stash):
        """Move this installation's model weights to *stash* while a restore replaces ``data/``."""
        if self.product != "BananaChat":
            return
        from .models import weight_paths
        data = self.root / "data"
        for name in weight_paths(data, read_environment(self.config_dir / "app.env")):
            origin = data / name
            if origin.exists() and not origin.is_symlink() and not (stash / name).exists():
                (stash / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(origin, stash / name)

    @staticmethod
    def carry_weights(names, sources, target):
        """Move the weights a package left out from *sources* (in order) into the new data *target*."""
        carried = set()
        for name in names:
            destination = target / name
            if destination.exists() or destination.is_symlink():
                continue
            for source in sources:
                origin = source / name
                if any(parent.is_symlink() for parent in (origin, *origin.parents) if parent.is_relative_to(source)):
                    continue
                if origin.exists():
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.replace(origin, destination)
                    carried.add(name)
                    break
        return carried

    def apply_package(self, extracted, manifest, settings, *, copy_source=True, preserve_policy=True, public_changed=False,
                      weights=None):
        """Replace ``data/`` and ``site/`` from an extracted package, then fix the configuration.

        The extracted trees are moved into place (staging is inside the root),
        so a rollback needs no room for another copy. Model weights the package
        left out are taken over from the data being replaced, or from
        *weights*, where a restore set them aside, instead of downloading them again.
        """
        old_root = manifest["old_root"]
        names = self.weight_names(manifest)
        carried = set()
        for name in ("data", "site"):
            source = extracted / name
            temporary = self.root / (".restore-" + name + "-" + secrets.token_hex(4))
            if source.is_dir() and not source.is_symlink():
                try:
                    os.replace(source, temporary)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    shutil.copytree(source, temporary)
            else:
                temporary.mkdir(mode=0o700)
            if name == "data" and names:
                carried = self.carry_weights(names, ([Path(weights)] if weights else []) + [self.root / "data"],
                                             temporary)
            previous = self.root / (".previous-" + name + "-" + secrets.token_hex(4))
            if (self.root / name).exists():
                os.replace(self.root / name, previous)
            os.replace(temporary, self.root / name)
            if previous.exists():
                shutil.rmtree(previous)
        atomic_write(self.root / "data/.banana-maintenance", "Restoring\n")
        self.tenant_maintenance(settings, True)
        if copy_source:
            for name in ("source.json", "repo.token", "repo.key", "repo.known_hosts", "repo.allowed_signers"):
                file = extracted / "config" / name
                if file.exists():
                    atomic_write(self.config_dir / name, file.read_bytes())
                else:
                    (self.config_dir / name).unlink(missing_ok=True)
        values = read_environment(extracted / "config/app.env")
        values = {key: str(self.root) + value[len(old_root):] if value == old_root or value.startswith(old_root + "/") else value for key, value in values.items()}
        if settings["mode"] == "hosting":
            values.update(BASE_DOMAIN=settings["domain"], PORTAL_DOMAIN=settings["portal_domain"])
        prefix = ("HOSTING" if settings["mode"] == "hosting" else "BW") if self.product == "BananaWiki" else ("BC_COMPUTE" if settings["mode"] == "compute" else "BC")
        values[prefix + "_PORT"] = str(settings["port"])
        if self.product == "BananaWiki" and settings["mode"] == "wiki":
            values["BW_SYSTEMD_SERVICE"] = settings["service"] + ".service"
        if public_changed:
            proxy = "1" if settings.get("domain") else "0"
            if prefix != "BC_COMPUTE":
                values[prefix + "_PROXY_MODE"] = proxy
            if prefix == "BC":
                values["BC_SECURE_COOKIES"] = proxy
            elif prefix == "BW":
                values["BW_PREFERRED_URL_SCHEME"] = "https" if settings.get("domain") else "http"
            elif prefix == "HOSTING":
                values["HOSTING_MODE"] = "subdomain" if settings.get("domain") else "port"
                values["HOSTING_PUBLIC_SCHEME"] = "https" if settings.get("domain") else "http"
        if self.product == "BananaChat":
            from .models import prepare_recovery, reset_restored_jobs
            restored_data = self.root / "data"
            database = Path(values.get("BC_DATABASE_PATH", restored_data / "bananachat.db"))
            kept = manifest.get("weights_in_place")
            if manifest.get("model_weights_excluded") and not (isinstance(kept, list) and set(kept) <= carried):
                # Only a weight-free package needs an approved re-download; a
                # rollback that found its weights in place does not.
                prepare_recovery(restored_data, read_json(extracted / "model-inventory.json"), database=database)
            elif database.absolute().is_relative_to(restored_data.absolute()):
                # A database outside the data directory is not in the package,
                # so it holds no restored work to stop.
                reset_restored_jobs(restored_data, database)
        self.write_runtime(settings, values)
        if not preserve_policy:
            saved_policy = read_json(extracted / "config/updates.json", DEFAULT_POLICY.copy())
            write_json(self.config_dir / "updates.json", {**saved_policy, "enabled": False})
        self.save(settings)

    def recover(self):
        journal = read_json(self.config_dir / "transaction.json")
        if not journal:
            return False
        settings = journal["settings"]
        self.system.stop(list(profile.service_commands(settings)))
        current = self.system.containers(settings)
        self.system.stop_containers(current)
        if journal.get("backup"):
            self.system.remove_containers(current)
            with read_package(journal["backup"], self.product, self.root / "staging") as (extracted, manifest):
                self.apply_package(extracted, manifest, settings, copy_source=journal.get("restore_source", False),
                                   weights=self.stash(journal))
            self.drop_stash(journal)
            self.switch(settings["revision"])
            self.system.install_units(settings)
            self.finish(journal, settings)
        else:
            self.finish(journal, settings, old_containers=True)
        self.event("recover", "complete", restored_revision=settings["revision"])
        return True

    def stash(self, journal):
        """Where a restore set the model weights aside (``journal["weights"]``), checked, or None."""
        value = journal.get("weights")
        if not value:
            return None
        path = Path(value)
        if path.parent != self.root or not re.fullmatch(r"\.rollback-weights-[0-9a-f]{8}", path.name) or path.is_symlink():
            raise ValueError("The recovery journal names an unexpected model weight directory.")
        return path

    def drop_stash(self, journal):
        stash = self.stash(journal)
        if stash is not None and stash.is_dir():
            shutil.rmtree(stash)

    def restore_source(self, revision, extracted, manifest, *, trust_source=None):
        """Where a restore takes the code it deploys from; never a package's bundled source unchecked.

        That source runs as the service and, through the installed command,
        as root. Returns the archive to stage, or None for the release this
        server already prepared or the revision taken from this server's own
        update source. When that source requires signatures, the revision
        must carry one updates accept: its own, or a signed release tag
        naming it (:meth:`GitSource.verify_restorable`). Only the operator's
        explicit *trust_source* deploys the bundled source, and never on a
        server whose update source requires signatures: that takes turning
        them off first, which the refusal explains.
        """
        if (self.root / "releases" / revision / ".release.json").exists():
            return None
        source = read_json(self.config_dir / "source.json")
        signed = bool(source) and source.get("signing", "none") == "ssh"
        unsigned_steps = ("To deploy it anyway, turn signature checks off with 'source set --clear-signatures' "
                          "(add --allow-unsigned while automatic updates are on), run the restore again with "
                          f"--trust-source {revision}, then turn them back on with 'source set "
                          "--require-signatures ALLOWED_SIGNERS': a restore keeps this server's update source "
                          "settings. Nothing was changed.")
        if source:
            git = GitSource(self.root, source)
            if self.source_has(git, source, revision):
                try:
                    git.verify_restorable(revision)
                except (RuntimeError, ValueError) as error:
                    raise ValueError(f"{error} This server's update source requires signed revisions, so it "
                                     f"was not deployed. {unsigned_steps}") from None
                return None
            if signed:
                raise ValueError(f"Revision {revision} is not available from this server's update source, which "
                                 "requires signed revisions, so the source bundled in the package was not "
                                 f"deployed. {unsigned_steps}")
        if trust_source == revision:
            return extracted / "source.tar.gz"
        recorded = read_json(extracted / "config/source.json") or {}
        raise ValueError(f"This package deploys source revision {revision} (recorded source: "
                         f"{recorded.get('url') or 'unknown'}, branch {recorded.get('branch') or 'unknown'}; package "
                         f"created {manifest.get('created_at') or 'at an unknown time'}). It could not be checked "
                         "against this server's update source, and the restored code runs on this server with root "
                         "privileges for maintenance. If you made this package or trust whoever did, run the restore "
                         f"again with --trust-source {revision}. Nothing was changed.")

    @staticmethod
    def source_has(git, source, revision):
        """Whether *revision* is a commit this server has fetched from (or can fetch from) its update source.

        Its branches are searched, then its release tags: a release may be
        reachable from a tag only (a maintenance branch deleted since).
        """
        def present():
            return git.git("cat-file", "-e", revision + "^{commit}", check=False).returncode == 0
        try:
            git.initialize()
            if present():
                return True
            url = valid_url(source["url"], allow_local=Path(source["url"]).is_absolute())
            for branch in dict.fromkeys(filter(None, (source.get("branch"), source.get("fallback_branch")))):
                git.git("fetch", "--no-tags", "--", url, "refs/heads/" + valid_branch(branch), check=False)
                if present():
                    return True
            for name, commit in git.release_tags(url):
                if commit == revision:
                    git.git("fetch", "--no-tags", "--", url, "refs/tags/" + name, check=False)
                    return present()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        return False

    def kept_source(self, extracted):
        """``source_warning`` when the package's update source settings differ from the ones this server keeps."""
        keys = ("url", "track", "branch", "fallback_branch", "signing")
        package = read_json(extracted / "config/source.json") or {}
        current = read_json(self.config_dir / "source.json") or {}
        if not package or all(package.get(key) == current.get(key) for key in keys):
            return {}
        return {"source_warning": (
            f"The package's update source ({package.get('url') or 'unknown'}, signatures "
            f"{package.get('signing') or 'none'}) differs from this server's ({current.get('url')}, signatures "
            f"{current.get('signing') or 'none'}), which was kept with its credentials and allowed signers. "
            "Use 'source set' to change it.")}

    def restore(self, package, *, new=False, name=None, domain=None, port=None, ollama_url=None, trust_source=None,
                allow_unpinned=False):
        self.layout()
        with maintenance_lock(self.root):
            if not new:
                self.recover()
            existing = read_json(self.config_dir / "installation.json")
            if new and existing:
                raise ValueError("Use restore on an existing installation, or choose an empty --root.")
            with read_package(package, self.product, self.root / "staging") as (extracted, manifest):
                if existing and ollama_url:
                    raise ValueError("--ollama-url is chosen when restoring into a new installation root.")
                restored = self.restored_settings(extracted, manifest, name=name, domain=domain, port=port,
                                                  ollama_url=ollama_url, existing=existing)
                if existing and restored["mode"] != existing["mode"]:
                    raise ValueError("A restore cannot change deployment mode. Use a separate installation root.")
                # The package used another Ollama than the one chosen for this machine.
                switched = (restored.get("ollama_url") or "") != (
                    (read_json(extracted / "config/installation.json") or {}).get("ollama_url") or "")
                machine = read_environment(self.config_dir / "app.env") if existing and switched else None
                if existing:
                    restored["service"] = existing["service"]
                # Before anything changes: which code may run here at all, and room for a rollback.
                archive = self.restore_source(restored["revision"], extracted, manifest, trust_source=trust_source)
                # That was decided with this server's update source and signature trust: they stay. Only a
                # server without any (a new one) takes the package's, credentials included.
                keep_source = (self.config_dir / "source.json").is_file()
                source_details = self.kept_source(extracted) if keep_source else {}
                if existing:
                    self.check_package_space(existing, copies=2, rollback=True)
                self.system.preflight(restored)
                self.system.account(restored)
                self.stage(restored, archive=archive, allow_unpinned=allow_unpinned)
                journal = self.snapshot_state(existing) if existing else None
                try:
                    if journal:
                        # A failed restore that replaces the source and trust
                        # configuration must also undo it.
                        journal["restore_source"] = not keep_source
                        self.quiesce(journal)
                        before = self.package(existing, self.backup_name("before-restore"), rollback=True)
                        stash = self.root / (".rollback-weights-" + secrets.token_hex(4))
                        journal.update(backup=str(before), phase="backed_up", weights=str(stash))
                        write_json(self.config_dir / "transaction.json", journal)
                        self.system.remove_containers(journal["containers"])
                        # The package left them out: keep them until the restore succeeded.
                        self.set_weights_aside(stash)
                    self.apply_package(extracted, manifest, restored, copy_source=not keep_source,
                                       preserve_policy=False, public_changed=domain is not None,
                                       weights=self.stash(journal) if journal else None)
                    if switched:
                        # The saved configuration pointed at another Ollama:
                        # keep this machine's Ollama settings (or the chosen URL).
                        values = read_environment(self.config_dir / "app.env")
                        upstream = "BC_COMPUTE_UPSTREAM" if restored["mode"] == "compute" else "BC_OLLAMA_URL"
                        for key in (upstream, "OLLAMA_HOST", "OLLAMA_MODELS"):
                            if machine is not None and key in machine:
                                values[key] = machine[key]
                            elif key == upstream:
                                values[key] = restored["ollama_url"] or profile.DEFAULT_OLLAMA_URL
                            else:
                                values.pop(key, None)
                        write_environment(self.config_dir / "app.env", profile.environment(restored, values))
                    self.switch(restored["revision"])
                    self.system.install_units(restored)
                    self.system.install_timer(restored, self.policy())
                    from banana_backup.store import Store
                    remote = Store(self.config_dir / "remote-backup", self.product)
                    if remote.status()["configured"]:
                        self.system.install_backup_timer(restored, remote.set_schedule(False))
                    if journal:
                        self.finish(journal, restored)
                    else:
                        self.system.start(list(profile.service_commands(restored)))
                        if not self.system.healthy(restored):
                            raise RuntimeError("Restored service failed its health checks. Maintenance mode remains active.")
                        self.tenant_maintenance(restored, False)
                        (self.root / "data/.banana-maintenance").unlink(missing_ok=True)
                except BaseException:
                    if journal:
                        self.recover()
                    raise
                if journal:
                    self.drop_stash(journal)
            warnings = self.prune_backups_safely() if existing else []
            return self.event("restore", "complete", revision=restored["revision"], automatic_updates=False,
                              **source_details, **({"pruning_warnings": warnings} if warnings else {}))

    @staticmethod
    def require_signed_updates(signing, *, allow_unsigned=False):
        """Refuse automatic updates of revisions nobody signed, unless the operator explicitly accepts them.

        Automatic updates run what they fetch as root, unattended: anyone who
        can push to the source (a stolen maintainer account or token) would
        otherwise own every server that enabled them.
        """
        if signing != "ssh" and not allow_unsigned:
            raise ValueError("Automatic updates install new code as root without anyone reviewing it, so they "
                             "require signed releases: add --require-signatures ALLOWED_SIGNERS (the maintainers' "
                             "SSH keys, see docs/deployment.md). To accept unsigned revisions anyway, add "
                             "--allow-unsigned.")

    def set_updates(self, enabled, *, interval=None, keep=None, allow_unsigned=False):
        settings = self.settings()
        policy = self.policy()
        if enabled:
            signing = self.source().get("signing", "none")
            self.require_signed_updates(signing, allow_unsigned=allow_unsigned)
            policy["unsigned"] = signing != "ssh"
        policy["enabled"] = bool(enabled)
        if interval is not None:
            if not 5 <= interval <= 10080:
                raise ValueError("Choose a check interval between 5 minutes and one week.")
            policy["interval_minutes"] = interval
        if keep is not None:
            if not 2 <= keep <= 30:
                raise ValueError("Keep between 2 and 30 automatic backups.")
            policy["keep_backups"] = keep
        # This file is separate so a running updater cannot overwrite a later opt-out.
        write_json(self.config_dir / "updates.json", policy)
        self.system.install_timer(settings, policy)
        source = self.source()
        return self.event("updates", "enabled" if enabled else "disabled", source=source["url"], track=source["track"],
                          branch=source["branch"], fallback_branch=source.get("fallback_branch"),
                          signing=source.get("signing", "none"), interval_minutes=policy["interval_minutes"])

    def prune_backups(self):
        """Apply keep_backups to each kind of automatically created package."""
        keep = self.policy()["keep_backups"]
        directory = self.root / "backups"
        protected = set()
        for record in ("last-update.json", "transaction.json"):
            package = (read_json(self.config_dir / record, {}) or {}).get("backup")
            if package:
                protected.add(os.path.abspath(package))
        removed = []
        for prefix in AUTOMATIC_BACKUPS:
            # Only names generated by backup_name(); "before-update-" never
            # matches "auto-" and operator-named files are left alone.
            pattern = re.compile(re.escape(prefix) + r"-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\.tar\.gz")
            files = sorted((path for path in directory.glob(prefix + "-*.tar.gz") if pattern.fullmatch(path.name)),
                           key=lambda path: path.name, reverse=True)
            for path in files[keep:]:
                if str(path) in protected or path.is_symlink() or not path.is_file():
                    continue
                path.unlink()
                removed.append(path.name)
        return removed

    def prune_backups_safely(self):
        """Housekeeping must never turn a completed operation into a failure."""
        try:
            self.prune_backups()
        except (OSError, ValueError) as error:
            return [f"Backup pruning failed: {error}"]
        return []

    def prune_releases(self, preserve):
        releases = sorted((self.root / "releases").iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
        preserve = set(preserve) | {path.name for path in releases[:self.policy()["keep_backups"]]}
        for path in releases:
            if path.name not in preserve and not path.is_symlink() and path.is_dir() and re.fullmatch(r"[a-f0-9]{40,64}", path.name):
                shutil.rmtree(path)

    def status(self):
        from banana_backup.store import Store
        settings = self.settings()
        source, policy = self.source(), self.policy()
        output = {"product": self.product, "mode": settings["mode"], "revision": settings["revision"], "root": str(self.root),
                  "service": settings["service"], "installed": settings.get("installed", False), "source": source, "updates": policy,
                  "maintenance": (self.root / "data/.banana-maintenance").exists(), "recovery_pending": bool(read_json(self.config_dir / "transaction.json")),
                  "last_operation": read_json(self.config_dir / "status.json"), "environment_file": str(self.config_dir / "app.env"),
                  "remote_backups": Store(self.config_dir / "remote-backup", self.product).status()}
        if policy["enabled"] and source.get("signing", "none") != "ssh":
            output["warnings"] = ["Automatic updates install unsigned revisions as root: whoever can push to the update "
                                  "source can run code on this server. Run 'updates enable --require-signatures "
                                  "ALLOWED_SIGNERS'."]
        return output

    def uninstall(self, *, purge=False, confirm=""):
        from banana_backup.files import lock as backup_lock
        from banana_backup.store import Store
        remote = Store(self.config_dir / "remote-backup", self.product)
        with maintenance_lock(self.root), (backup_lock(remote.root) if remote.status()["configured"] else nullcontext()):
            self.recover()
            settings = self.settings()
            if purge and confirm != settings["service"]:
                raise ValueError("For permanent deletion, pass --purge --confirm SERVICE_NAME.")
            if purge:
                self.check_purge()
            self.set_updates(False)
            if remote.status()["configured"]:
                self.system.install_backup_timer(settings, remote.set_schedule(False))
            self.system.stop(list(profile.service_commands(settings)))
            containers = self.system.containers(settings)
            self.system.stop_containers(containers)
            self.system.remove_containers(containers)
            self.system.uninstall(settings)
            settings["installed"] = False
            self.save(settings)
            self.event("uninstall", "complete", data_preserved=not purge)
            if purge:
                self.check_purge()
                shutil.rmtree(self.root)
        return {"outcome": "uninstalled", "data_preserved": not purge}

    def check_purge(self):
        """Delete only a directory that is recognisably this managed installation."""
        import pwd
        root = self.root
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Refusing to purge: the installation root is not a real directory.")
        protected = {Path(path) for path in PROTECTED_ROOTS}
        try:
            protected.add(Path.home())
        except (KeyError, RuntimeError):
            pass
        for name in filter(None, (os.environ.get("SUDO_USER"), os.environ.get("USER"))):
            try:
                protected.add(Path(pwd.getpwnam(name).pw_dir))
            except KeyError:
                pass
        if any(os.path.realpath(root) == os.path.realpath(path) for path in protected):
            raise ValueError(f"Refusing to purge the system or home directory {root}.")
        saved = read_json(self.config_dir / "installation.json")
        releases = root / "releases"
        if (not saved or saved.get("product") != self.product or saved.get("root") != str(root)
                or releases.is_symlink() or not releases.is_dir()):
            raise ValueError(f"Refusing to purge {root}: it does not contain a managed {self.product} installation.")
