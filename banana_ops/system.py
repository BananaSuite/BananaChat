"""Linux/systemd operations, kept separate from update and restore decisions."""

import errno
import grp
import ipaddress
import json
import os
from pathlib import Path
import pwd
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .files import atomic_write, digest_file, read_environment, read_json
from . import profile


def set_release_owner(path, uid, gid, *, readonly=False):
    """Change release ownership through directory descriptors, never following links.

    Seal parent directories before inspecting a service-writable build tree.
    When granting build access, transfer directories after their contents.
    """
    def visit(descriptor):
        if readonly:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o750)
        for name in os.listdir(descriptor):
            try:
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    continue  # Virtual environments link to the system Python.
                raise
            try:
                info = os.fstat(child)
                if stat.S_ISDIR(info.st_mode):
                    visit(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.fchown(child, uid, gid)
                    if readonly:
                        os.fchmod(child, 0o750 if info.st_mode & 0o111 else 0o640)
                else:
                    raise ValueError("Release builds cannot contain special files or hard links.")
            finally:
                os.close(child)
        if not readonly:
            os.fchown(descriptor, uid, gid)

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor)
    finally:
        os.close(descriptor)


class System:
    unit_dir = Path("/etc/systemd/system")
    bin_dir = Path("/usr/local/bin")
    proxy_file = Path("/etc/caddy/Caddyfile")
    log_dir = None

    def preflight(self, settings):
        root = Path(settings["root"])
        for name in profile.service_commands(settings):
            unit = self.unit_dir / (name + ".service")
            if unit.exists() and ("# Managed by BananaSuite" not in unit.read_text()
                                  or f"WorkingDirectory={root / 'current'}\n" not in unit.read_text()):
                raise ValueError("A service with this name already exists. Choose another --name or migrate it explicitly.")
        command = self.bin_dir / settings["service"]
        if command.exists() and ("# Managed by BananaSuite" not in command.read_text()
                                  or str(root / "current/banana") not in command.read_text()):
            raise ValueError("An installed command already uses this name. Choose another --name.")
        ports = [(settings["port"], settings["service"])]
        if settings["mode"] in {"single", "compute"}:
            ports.append((11434, settings["service"] + "-ollama"))
        for port, service in ports:
            with socket.socket() as probe:
                probe.settimeout(1)
                occupied = probe.connect_ex(("127.0.0.1", port)) == 0
            if occupied and not self.active(service):
                detail = " Stop the existing Ollama service first, or use web mode with that backend." if port == 11434 else " Choose another --port."
                raise ValueError(f"Port {port} is already occupied." + detail)

    def run(self, command, *, check=True, timeout=600, cwd=None):
        result = subprocess.run([str(part) for part in command], capture_output=True, text=True, timeout=timeout, cwd=cwd)
        if check and result.returncode:
            detail = ""
            if self.log_dir:
                log = self.log_dir / "last-command.log"
                atomic_write(log, (result.stdout + "\n" + result.stderr)[-100000:])
                detail = f" Details are in the private log {log}."
            raise RuntimeError(f"{Path(str(command[0])).name} {command[1] if len(command) > 1 else ''} failed (exit {result.returncode})." + detail)
        return result

    def account(self, settings):
        name = settings["service"]
        try:
            identity = pwd.getpwnam(name)
        except KeyError:
            self.run(["useradd", "--system", "--user-group", "--home-dir", str(Path(settings['root']) / 'data'), "--shell", "/usr/sbin/nologin", name])
            identity = pwd.getpwnam(name)
        if identity.pw_uid == 0:
            raise ValueError("The application cannot use the root account.")
        if settings["mode"] == "hosting":
            self.run(["docker", "info"], timeout=30)
            self.run(["usermod", "-aG", "docker", name])
        elif settings["product"] == "BananaChat" and settings["mode"] in {"single", "compute"}:
            for group in ("render", "video"):
                try:
                    grp.getgrnam(group)
                except KeyError:
                    continue
                self.run(["usermod", "-aG", group, name])
        return identity

    def data_permissions(self, settings):
        identity = pwd.getpwnam(settings["service"])
        data = Path(settings["root"]) / "data"
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        for root, directories, files in os.walk(data):
            for path in (Path(root), *(Path(root) / name for name in (*directories, *files))):
                if path.is_symlink():
                    if settings["mode"] == "hosting" and profile.hosting_storage_link(path, data):
                        os.chown(path, identity.pw_uid, identity.pw_gid, follow_symlinks=False)
                        continue
                    raise ValueError("Managed data must not contain symlinks.")
                os.chown(path, identity.pw_uid, identity.pw_gid)
                path.chmod(0o700 if path.is_dir() else 0o600)
        site = Path(settings["root"]) / "site"
        for root, directories, files in os.walk(site):
            for path in (Path(root), *(Path(root) / name for name in (*directories, *files))):
                if path.is_symlink():
                    raise ValueError("The static site must not contain symlinks.")
                path.chmod(0o755 if path.is_dir() else 0o644)

    def prepare_release(self, settings, release):
        identity = pwd.getpwnam(settings["service"])
        print(f"Preparing {settings['product']} {settings['mode']} at {settings['revision'][:12]}.", file=sys.stderr, flush=True)
        # Only the venv is writable by the service account during dependency installation.
        for root, _dirs, files in os.walk(release):
            Path(root).chmod(0o755)
            for name in files:
                path = Path(root) / name
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        self.run(["python3", "-m", "venv", str(release / ".venv")])
        set_release_owner(release / ".venv", identity.pw_uid, identity.pw_gid)
        # Distribution-provided ensurepip wheels can predate security fixes.
        # Bootstrap only the installer wheel before it handles application packages.
        self.run(["runuser", "-u", settings["service"], "--", str(release / ".venv/bin/python"), "-m", "pip", "install",
                  "--disable-pip-version-check", "--no-cache-dir", "--only-binary=:all:", "--no-deps",
                  "--upgrade", "pip>=26.2.1"], timeout=600, cwd=release)
        if settings["mode"] != "compute":
            print("Installing Python dependencies; this may take several minutes.", file=sys.stderr, flush=True)
            self.run(["runuser", "-u", settings["service"], "--", str(release / ".venv/bin/python"), "-m", "pip", "install",
                      "--disable-pip-version-check", "--no-cache-dir", "-r", str(release / "requirements.txt")], timeout=1800, cwd=release)
        if settings["mode"] == "hosting":
            print("Building the tenant image from this source revision.", file=sys.stderr, flush=True)
            self.run(["docker", "build", "-f", str(release / "Dockerfile.tenant"), "-t", "bananawiki-tenant:" + settings["revision"], str(release)], timeout=1800)
        set_release_owner(release, 0, identity.pw_gid, readonly=True)

    def install_units(self, settings):
        root = Path(settings["root"])
        commands = profile.service_commands(settings)
        for name, command in commands.items():
            after = "network-online.target"
            extra = ""
            if settings["mode"] == "hosting":
                after += " docker.service"
                extra = "SupplementaryGroups=docker\n"
            if name == settings["service"] and name + "-ollama" in commands:
                after += " " + name + "-ollama.service"
            unit = ("# Managed by BananaSuite\n[Unit]\nDescription=" + settings["product"] + " " + settings["mode"] + "\n"
                    f"After={after}\nWants=network-online.target\n\n[Service]\nUser={settings['service']}\nGroup={settings['service']}\n"
                    f"WorkingDirectory={root / 'current'}\nEnvironmentFile={root / 'config/app.env'}\n"
                    "ExecStart=" + " ".join(json.dumps(str(arg)) for arg in command) + "\n"
                    "Restart=always\nRestartSec=5\nTimeoutStartSec=180\nTimeoutStopSec=180\nKillMode=mixed\nUMask=0077\n"
                    "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\n"
                    f"ReadWritePaths={root / 'data'}\n" + extra + "\n[Install]\nWantedBy=multi-user.target\n")
            destination = self.unit_dir / (name + ".service")
            if destination.exists() and ("# Managed by BananaSuite" not in destination.read_text()
                                          or f"WorkingDirectory={root / 'current'}\n" not in destination.read_text()):
                raise ValueError("A service with this name already exists. Use a different --name or migrate the previous service explicitly.")
            atomic_write(destination, unit, 0o644)
        command = self.bin_dir / settings["service"]
        if command.exists() and ("# Managed by BananaSuite" not in command.read_text()
                                or str(root / "current/banana") not in command.read_text()):
            raise ValueError("An unrelated command already uses this service name.")
        atomic_write(command, "#!/bin/sh\n# Managed by BananaSuite\nexec /usr/bin/python3 " + str(root / "current/banana") + " --root " + str(root) + ' "$@"\n', 0o755)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable", *commands])

    def install_timer(self, settings, policy):
        root, name = Path(settings["root"]), settings["service"] + "-update"
        service = ("# Managed by BananaSuite\n[Unit]\nDescription=Optional " + settings["product"] + " source update\n"
                   "After=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nUMask=0077\n"
                   f"ExecStart=/usr/bin/python3 {root / 'current/banana'} --root {root} update --automatic\n"
                   "TimeoutStartSec=3600\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\n")
        timer = ("# Managed by BananaSuite\n[Unit]\nDescription=Optional " + settings["product"] + " update schedule\n\n[Timer]\n"
                 f"OnBootSec=5min\nOnUnitActiveSec={policy['interval_minutes']}min\nRandomizedDelaySec=120\nPersistent=true\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        atomic_write(self.unit_dir / (name + ".service"), service, 0o644)
        atomic_write(self.unit_dir / (name + ".timer"), timer, 0o644)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable" if policy["enabled"] else "disable", "--now", name + ".timer"])

    def active(self, service):
        return self.run(["systemctl", "is-active", "--quiet", service], check=False, timeout=15).returncode == 0

    def install_backup_timer(self, settings, policy):
        root, name = Path(settings["root"]), settings["service"] + "-backup"
        service = ("# Managed by BananaSuite\n[Unit]\nDescription=Optional encrypted " + settings["product"] + " backup\n"
                   "After=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nUMask=0077\n"
                   f"WorkingDirectory={root / 'current'}\nExecStart=/usr/bin/python3 {root / 'current/banana'} --root {root} backups run --automatic\n"
                   "TimeoutStartSec=7200\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=7\n")
        timer = ("# Managed by BananaSuite\n[Unit]\nDescription=Optional " + settings["product"] + " backup schedule\n\n[Timer]\n"
                 f"OnBootSec=15min\nOnUnitActiveSec={policy['interval_minutes']}min\nRandomizedDelaySec=120\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        for suffix, _text in ((".service", service), (".timer", timer)):
            destination = self.unit_dir / (name + suffix)
            if destination.exists() and ("# Managed by BananaSuite" not in destination.read_text()
                                          or (suffix == ".service" and f"WorkingDirectory={root / 'current'}\n" not in destination.read_text())):
                raise ValueError("An unrelated backup unit already uses this name.")
        for suffix, text in ((".service", service), (".timer", timer)):
            atomic_write(self.unit_dir / (name + suffix), text, 0o644)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "enable" if policy["enabled"] else "disable", "--now", name + ".timer"])

    def stop(self, services):
        if services:
            self.run(["systemctl", "stop", *reversed(services)], timeout=300)

    def start(self, services):
        if services:
            self.run(["systemctl", "start", *services], timeout=300)

    def containers(self, settings):
        if settings["mode"] != "hosting":
            return []
        ids = self.run(["docker", "ps", "--all", "--quiet", "--filter", "label=org.bananawiki.role=tenant"]).stdout.split()
        if not ids:
            return []
        data = json.loads(self.run(["docker", "inspect", *ids]).stdout)
        root = (Path(settings["root"]) / "data/instances").resolve()
        output = []
        for item in data:
            labels = item.get("Config", {}).get("Labels", {}) or {}
            directory = Path(labels.get("org.bananawiki.data-dir", "/"))
            if directory.is_absolute() and directory.resolve().is_relative_to(root):
                output.append({"id": item["Id"], "running": bool(item.get("State", {}).get("Running")),
                               "ports": list((item.get("NetworkSettings", {}).get("Ports", {}).get("5001/tcp") or [])),
                               "addresses": [network["IPAddress"] for network in item.get("NetworkSettings", {}).get("Networks", {}).values()
                                             if network.get("IPAddress")],
                               "internal_port": int(labels.get("org.bananawiki.internal-port", "5001")),
                               "data_dir": str(directory)})
        return output

    def expected_tenant_directories(self, settings):
        """Include restored running tenants before their containers are recreated."""
        root = Path(settings["root"])
        environment = read_environment(root / "config/app.env")
        database = Path(environment.get("HOSTING_DATABASE_PATH", root / "data/hosting.db"))
        base = Path(environment.get("INSTANCES_DIR", root / "data/instances")).resolve()
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute("SELECT subdomain, domain_mode FROM instances WHERE status='running'").fetchall()
        finally:
            connection.close()
        output = set()
        for slug, mode in rows:
            directory = (base / (slug + ("__apex" if mode == "apex" else ""))).resolve()
            if not directory.is_relative_to(base) or directory == base:
                raise ValueError("Invalid tenant path in the hosting database.")
            output.add(str(directory))
        return output

    def stop_containers(self, containers):
        ids = [item["id"] for item in containers if item["running"]]
        if ids:
            self.run(["docker", "stop", "--time", "30", *ids], timeout=300)

    def resume_containers(self, containers):
        ids = [item["id"] for item in containers if item["running"]]
        if ids:
            self.run(["docker", "start", *ids], timeout=300)

    def remove_containers(self, containers):
        ids = [item["id"] for item in containers]
        if ids:
            self.run(["docker", "rm", *ids], timeout=300)

    def readiness_timeout(self, settings, containers=()):
        """Allow bounded recovery waves within the operator's startup limit."""
        if settings["mode"] != "hosting":
            return 180
        environment = read_environment(Path(settings["root"]) / "config/app.env")
        startup = max(30, min(600, int(environment.get("INSTANCE_STARTUP_TIMEOUT_SECONDS", "120"))))
        workers = max(1, min(16, int(environment.get("BW_HOSTING_RECOVERY_MAX_WORKERS", "2"))))
        try:
            count = max(len(containers), len(self.expected_tenant_directories(settings)))
        except (OSError, sqlite3.Error):
            count = len(containers)
        waves = max(1, (count + workers - 1) // workers)
        return min(7200, max(180, waves * (2 * startup + 120)))

    def healthy(self, settings, containers=(), timeout=None):
        timeout = self.readiness_timeout(settings, containers) if timeout is None else timeout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        issues = []
        while time.monotonic() < deadline:
            urls = profile.health_urls(settings)
            issues = [f"Service is not active: {name}" for name in profile.service_commands(settings) if not self.active(name)]
            healthy = not issues
            if settings["mode"] == "hosting":
                try:
                    expected = self.expected_tenant_directories(settings)
                    expected.update(item["data_dir"] for item in containers if item["running"])
                    current = {item["data_dir"]: item for item in self.containers(settings)}
                    for directory in expected:
                        item = current.get(directory)
                        if not item or not item["running"] or not item["addresses"]:
                            healthy = False
                            issues.append(f"Tenant has no running container: {directory}")
                            continue
                        # Internal Docker networks may omit published host ports.
                        # Resolve each recreated tenant's current bridge address,
                        # as the portal does, instead of probing stale mappings.
                        address = str(ipaddress.IPv4Address(item["addresses"][0]))
                        port = item["internal_port"]
                        if not 1 <= port <= 65535:
                            raise ValueError("Invalid tenant HTTP port.")
                        urls.append(f"http://{address}:{port}/health")
                except (OSError, sqlite3.Error, ValueError) as error:
                    healthy = False
                    issues.append(f"Cannot inspect required tenants: {type(error).__name__}")
            for url in urls:
                try:
                    with opener.open(url, timeout=5) as response:
                        healthy = healthy and response.status == 200
                        if response.status != 200:
                            issues.append(f"{url}: HTTP {response.status}")
                except (OSError, urllib.error.URLError) as error:
                    healthy = False
                    issues.append(f"{url}: {type(error).__name__}")
            if healthy:
                return True
            time.sleep(3)
        if self.log_dir:
            atomic_write(self.log_dir / "last-readiness-failure.json", json.dumps({
                "checked_at": time.time(), "revision": settings["revision"],
                "timeout_seconds": timeout, "issues": issues[:100],
                "omitted_issues": max(0, len(issues) - 100),
            }, indent=2) + "\n")
        return False

    def uninstall(self, settings):
        self.run(["systemctl", "disable", "--now", settings["service"] + "-update.timer"], check=False)
        self.run(["systemctl", "disable", "--now", settings["service"] + "-backup.timer"], check=False)
        self.run(["systemctl", "stop", settings["service"] + "-backup.service"], check=False)
        names = [*profile.service_commands(settings), settings["service"] + "-update", settings["service"] + "-backup"]
        for name in names:
            self.run(["systemctl", "disable", name], check=False)
            for suffix in (".service", ".timer"):
                path = self.unit_dir / (name + suffix)
                if path.exists() and "# Managed by BananaSuite" in path.read_text():
                    path.unlink()
        command = self.bin_dir / settings["service"]
        if command.exists() and "# Managed by BananaSuite" in command.read_text():
            command.unlink()
        self.remove_proxy(settings)
        self.run(["systemctl", "daemon-reload"])

    def remove_proxy(self, settings):
        record = read_json(Path(settings["root"]) / "config/proxy.json", {})
        # Keep a proxy edited by the operator, including configurations for
        # other applications. Only undo the exact file installed by this root.
        if not record or not self.proxy_file.is_file() or digest_file(self.proxy_file) != record.get("installed_sha256"):
            return
        backup = record.get("previous")
        if backup:
            old = Path(backup)
            if not old.is_relative_to(Path(settings["root"]) / "backups") or not old.is_file():
                raise ValueError("The saved proxy configuration is missing. Restore it before uninstalling.")
            atomic_write(self.proxy_file, old.read_bytes(), 0o644)
        else:
            atomic_write(self.proxy_file, "# BananaSuite application uninstalled; no sites configured.\n", 0o644)
        self.run(["systemctl", "reload-or-restart", "caddy"])
