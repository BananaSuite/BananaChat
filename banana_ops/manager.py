"""Install, checkpoint, update, restore, and remove one remembered deployment."""

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tarfile

from .files import (absolute_path, atomic_write, extract_archive, maintenance_lock, read_environment,
                    read_json, read_package, regular_files, write_environment, write_json, write_package)
from . import profile
from .source import GitSource, valid_branch, valid_url
from .system import System


DEFAULT_POLICY = {"enabled": False, "interval_minutes": 60, "keep_backups": 3}


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
        data = read_json(self.config_dir / "source.json")
        if not data:
            raise ValueError("No update source is configured.")
        valid_branch(data["branch"])
        if data.get("fallback_branch"):
            valid_branch(data["fallback_branch"])
        return data

    def event(self, operation, outcome, **details):
        value = {"time": datetime.now(timezone.utc).isoformat(), "operation": operation, "outcome": outcome, **details}
        write_json(self.config_dir / "status.json", value)
        path = self.config_dir / "history.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as target:
            target.write(json.dumps(value) + "\n")
        return value

    def layout(self):
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in ("config", "backups", "staging", "releases", "data", "site"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("The managed installation contains a directory symlink.")
            path.mkdir(mode=0o755 if name in {"releases", "site"} else 0o700, exist_ok=True)

    def configure_source(self, *, url=None, branch=None, fallback=None, clear_fallback=False, token_file=None,
                         username="git", ssh_key=None, known_hosts=None, clear_credentials=False, allow_local=False,
                         signers_file=None, clear_signatures=False):
        old = read_json(self.config_dir / "source.json", {"url": "https://github.com/BananaSuite/" + self.product + ".git", "branch": "main", "fallback_branch": None, "auth": "none", "signing": "none"})
        new = dict(old)
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

    def stage(self, settings, *, archive=None):
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
            self.system.prepare_release(settings, release)
            shutil.copyfile(archive_file, release / ".source.tar.gz")
            write_json(release / ".release.json", {"revision": revision, "product": self.product})
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
        if settings["mode"] == "compute" and not (self.root / "data/.compute-api-token").exists():
            atomic_write(self.root / "data/.compute-api-token", secrets.token_hex(32) + "\n")
        if settings["mode"] == "hosting":
            (self.root / "data/instances").mkdir(mode=0o700, parents=True, exist_ok=True)
            profile.prepare_hosting_storage(self.root / "data")
            if not any((self.root / "site").iterdir()):
                site = self.root / "current/site"
                if site.is_dir():
                    shutil.copytree(site, self.root / "site", dirs_exist_ok=True)
        self.system.data_permissions(settings)

    def install(self, checkout, *, mode, name=None, domain="", portal_domain="", port=None, backend_url="",
                backend_token_file=None, ollama_binary="", source_options=None, reuse_data=False):
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
                raise ValueError("The web mode needs --backend-url for its compute server.")
            if backend_url:
                from urllib.parse import urlsplit
                parsed = urlsplit(backend_url)
                if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"})) or parsed.username or parsed.password:
                    raise ValueError("Use an HTTPS backend URL or a loopback SSH-tunnel URL; store its token separately.")
            if mode in {"single", "compute"}:
                ollama_binary = ollama_binary or shutil.which("ollama") or ""
                if not ollama_binary or not Path(ollama_binary).is_absolute() or not Path(ollama_binary).is_file():
                    raise ValueError("Install Ollama from its official packages, then provide --ollama-binary if it is outside PATH.")
            port = int(port or {"wiki": 5001, "hosting": 5099, "single": 8000, "web": 8000, "compute": 11435}[mode])
            if not 1024 <= port <= 65535:
                raise ValueError("Choose an application port between 1024 and 65535.")
            source = self.configure_source(**(source_options or {}))
            settings = {"schema": 1, "product": self.product, "mode": mode, "root": str(self.root), "service": name,
                        "domain": domain, "portal_domain": portal_domain, "port": port, "backend_url": backend_url,
                        "ollama_binary": ollama_binary, "source_url": source["url"], "installed": False, "revision": ""}
            self.system.preflight(settings)
            self.system.account(settings)
            settings["revision"] = GitSource(self.root, source).seed(checkout)
            self.stage(settings)
            self.save(settings)
            self.switch(settings["revision"])
            self.write_runtime(settings)
            if backend_token_file:
                environment = read_environment(self.config_dir / "app.env")
                environment["BC_OLLAMA_API_KEY"] = self.read_secret(backend_token_file)
                write_environment(self.config_dir / "app.env", environment)
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

    def package(self, settings, destination, *, exclude_model_weights=False):
        data = self.root / "data"
        excluded, model_inventory = [], None
        if self.product == "BananaChat" and exclude_model_weights:
            from .models import inventory
            environment = read_environment(self.config_dir / "app.env")
            database = Path(environment.get("BC_DATABASE_PATH", data / "bananachat.db")).absolute()
            if not database.is_relative_to(data):
                raise ValueError("Move the BananaChat database inside the managed data directory before making a weight-free package; external databases need a separate backup.")
            model_inventory = inventory(data, environment)
            excluded = model_inventory["excluded_paths"]
        allowed_link = (lambda path: profile.hosting_storage_link(path, data)) if settings["mode"] == "hosting" else None
        inputs = [(path, "data/" + name) for path, name in regular_files(data, allowed_link=allowed_link, excluded=excluded)]
        inputs.extend((path, "site/" + name) for path, name in regular_files(self.root / "site"))
        for name in ("installation.json", "source.json", "app.env", "repo.token", "repo.key", "repo.known_hosts", "updates.json"):
            path = self.config_dir / name
            if path.is_file() and not path.is_symlink():
                inputs.append((path, "config/" + name))
        source_archive = self.root / "releases" / settings["revision"] / ".source.tar.gz"
        inputs.append((source_archive, "source.tar.gz"))
        inventory_file = self.root / "staging" / ("model-inventory-" + secrets.token_hex(6) + ".json")
        try:
            if model_inventory is not None:
                write_json(inventory_file, model_inventory)
                inputs.append((inventory_file, "model-inventory.json"))
            return write_package(destination, {"product": self.product, "mode": settings["mode"], "revision": settings["revision"],
                                 "created_at": datetime.now(timezone.utc).isoformat(), "old_root": str(self.root),
                                 "model_weights_excluded": model_inventory is not None, "excluded_paths": excluded}, inputs)
        finally:
            inventory_file.unlink(missing_ok=True)

    def finish(self, journal, settings, *, old_containers=False):
        if old_containers:
            self.system.resume_containers(journal["containers"])
        self.system.start(list(profile.service_commands(settings)))
        if not self.system.healthy(settings, journal["containers"]):
            raise RuntimeError("The application or a previously running wiki failed its readiness checks.")
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
            journal = self.snapshot_state(settings)
            try:
                self.quiesce(journal)
                result = self.package(settings, destination, exclude_model_weights=exclude_model_weights)
                self.finish(journal, settings, old_containers=True)
            except BaseException:
                self.recover()
                raise
            self.event("backup", "complete", package=str(result))
            return result

    def update(self, *, automatic=False, allow_divergent=False, retry_failed=False):
        with maintenance_lock(self.root):
            self.recover()
            settings = self.settings()
            if automatic and not self.policy()["enabled"]:
                return {"outcome": "disabled"}
            if automatic and not self.system.active(settings["service"]):
                return self.event("update", "skipped", reason="The application was intentionally stopped.")
            source = GitSource(self.root, self.source())
            sha, selected, forward = source.resolve(settings["revision"])
            if sha == settings["revision"]:
                return self.event("update", "current", revision=sha, branch=selected)
            if not forward and (automatic or not allow_divergent):
                return self.event("update", "paused", revision=sha, reason="The selected source diverges from the installed revision. Review it and use update --allow-divergent for an intentional switch.")
            failed = read_json(self.config_dir / "failed-revision.json", {})
            if automatic and failed.get("revision") == sha and not retry_failed:
                return self.event("update", "paused", revision=sha, reason="This revision previously failed; a maintainer must retry it or select a newer revision.")
            candidate = {**settings, "revision": sha, "source_url": self.source()["url"]}
            try:
                self.stage(candidate)
            except BaseException:
                write_json(self.config_dir / "failed-revision.json", {"revision": sha})
                self.event("update", "failed", revision=sha, reason="Release preparation failed; the running version was left in place.")
                raise
            if automatic and not self.policy()["enabled"]:
                return self.event("update", "cancelled", reason="Automatic updates were disabled while preparing the release.")
            journal = self.snapshot_state(settings)
            journal["candidate"] = sha
            try:
                self.quiesce(journal)
                archive = self.package(settings, self.backup_name("auto" if automatic else "before-update"))
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
            self.prune_backups()
            write_json(self.config_dir / "last-update.json", {"backup": str(archive), "previous_revision": settings["revision"], "revision": sha})
            self.prune_releases({settings["revision"], sha})
            return self.event("update", "complete", revision=sha, previous_revision=settings["revision"], branch=selected,
                              used_fallback=selected != self.source()["branch"], backup=str(archive))

    def restored_settings(self, extracted, manifest, *, name=None, domain=None, port=None):
        saved = read_json(extracted / "config/installation.json")
        if not saved or saved.get("product") != self.product or saved.get("mode") not in profile.modes(self.product) or saved.get("revision") != manifest["revision"]:
            raise ValueError("The package's installation metadata is inconsistent.")
        saved.update(root=str(self.root), installed=True)
        if name:
            saved["service"] = name
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,45}", saved.get("service", "")):
            raise ValueError("Invalid restored service name.")
        if saved["mode"] in {"single", "compute"} and not Path(saved.get("ollama_binary", "")).is_file():
            saved["ollama_binary"] = shutil.which("ollama") or ""
            if not saved["ollama_binary"]:
                raise ValueError("Install Ollama on the new server before restoring this deployment mode.")
        if domain is not None:
            saved["domain"] = profile.validate_domain(domain)
            if saved["mode"] == "hosting":
                saved["portal_domain"] = "portal." + saved["domain"] if saved["domain"] else ""
        if port is not None:
            if not 1024 <= int(port) <= 65535:
                raise ValueError("Choose an application port between 1024 and 65535.")
            saved["port"] = int(port)
        return saved

    def apply_package(self, extracted, manifest, settings, *, copy_source=True, preserve_policy=True, public_changed=False):
        old_root = manifest["old_root"]
        for name in ("data", "site"):
            source = extracted / name
            temporary = self.root / (".restore-" + name + "-" + secrets.token_hex(4))
            if source.exists():
                shutil.copytree(source, temporary)
            else:
                temporary.mkdir(mode=0o700)
            previous = self.root / (".previous-" + name + "-" + secrets.token_hex(4))
            if (self.root / name).exists():
                os.replace(self.root / name, previous)
            os.replace(temporary, self.root / name)
            if previous.exists():
                shutil.rmtree(previous)
        atomic_write(self.root / "data/.banana-maintenance", "Restoring\n")
        self.tenant_maintenance(settings, True)
        if copy_source:
            for name in ("source.json", "repo.token", "repo.key", "repo.known_hosts"):
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
            from .models import inventory, prepare_recovery
            restored_data = self.root / "data"
            saved_inventory = (read_json(extracted / "model-inventory.json") if manifest.get("model_weights_excluded")
                               else inventory(restored_data, values))
            prepare_recovery(restored_data, saved_inventory,
                             database=Path(values.get("BC_DATABASE_PATH", restored_data / "bananachat.db")))
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
                self.apply_package(extracted, manifest, settings, copy_source=False)
            self.switch(settings["revision"])
            self.system.install_units(settings)
            self.finish(journal, settings)
        else:
            self.finish(journal, settings, old_containers=True)
        self.event("recover", "complete", restored_revision=settings["revision"])
        return True

    def restore(self, package, *, new=False, name=None, domain=None, port=None):
        self.layout()
        with maintenance_lock(self.root):
            if not new:
                self.recover()
            existing = read_json(self.config_dir / "installation.json")
            if new and existing:
                raise ValueError("Use restore on an existing installation, or choose an empty --root.")
            with read_package(package, self.product, self.root / "staging") as (extracted, manifest):
                restored = self.restored_settings(extracted, manifest, name=name, domain=domain, port=port)
                if existing and restored["mode"] != existing["mode"]:
                    raise ValueError("A restore cannot change deployment mode. Use a separate installation root.")
                if existing:
                    restored["service"] = existing["service"]
                self.system.preflight(restored)
                self.system.account(restored)
                self.stage(restored, archive=extracted / "source.tar.gz")
                journal = self.snapshot_state(existing) if existing else None
                try:
                    if journal:
                        self.quiesce(journal)
                        before = self.package(existing, self.backup_name("before-restore"))
                        journal.update(backup=str(before), phase="backed_up")
                        write_json(self.config_dir / "transaction.json", journal)
                        self.system.remove_containers(journal["containers"])
                    self.apply_package(extracted, manifest, restored, preserve_policy=False, public_changed=domain is not None)
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
            return self.event("restore", "complete", revision=restored["revision"], automatic_updates=False)

    def set_updates(self, enabled, *, interval=None, keep=None):
        settings = self.settings()
        policy = self.policy()
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
        return self.event("updates", "enabled" if enabled else "disabled", source=self.source()["url"], branch=self.source()["branch"],
                          fallback_branch=self.source().get("fallback_branch"), interval_minutes=policy["interval_minutes"])

    def prune_backups(self):
        files = sorted((self.root / "backups").glob("auto-*.tar.gz"), key=lambda path: path.name, reverse=True)
        for path in files[self.policy()["keep_backups"]:]:
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def prune_releases(self, preserve):
        releases = sorted((self.root / "releases").iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
        preserve = set(preserve) | {path.name for path in releases[:self.policy()["keep_backups"]]}
        for path in releases:
            if path.name not in preserve and not path.is_symlink() and path.is_dir() and re.fullmatch(r"[a-f0-9]{40,64}", path.name):
                shutil.rmtree(path)

    def status(self):
        from banana_backup.store import Store
        settings = self.settings()
        return {"product": self.product, "mode": settings["mode"], "revision": settings["revision"], "root": str(self.root),
                "service": settings["service"], "installed": settings.get("installed", False), "source": self.source(), "updates": self.policy(),
                "maintenance": (self.root / "data/.banana-maintenance").exists(), "recovery_pending": bool(read_json(self.config_dir / "transaction.json")),
                "last_operation": read_json(self.config_dir / "status.json"), "environment_file": str(self.config_dir / "app.env"),
                "remote_backups": Store(self.config_dir / "remote-backup", self.product).status()}

    def uninstall(self, *, purge=False, confirm=""):
        from banana_backup.files import lock as backup_lock
        from banana_backup.store import Store
        remote = Store(self.config_dir / "remote-backup", self.product)
        with maintenance_lock(self.root), (backup_lock(remote.root) if remote.status()["configured"] else nullcontext()):
            self.recover()
            settings = self.settings()
            if purge and confirm != settings["service"]:
                raise ValueError("For permanent deletion, pass --purge --confirm SERVICE_NAME.")
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
                shutil.rmtree(self.root)
        return {"outcome": "uninstalled", "data_preserved": not purge}
