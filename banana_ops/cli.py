"""One server command for installation, maintenance, migration, and removal."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import subprocess

from .files import atomic_write, digest_file, maintenance_lock, read_json, write_json
from .manager import Manager
from . import profile


def source_options(parser):
    parser.add_argument("--repo", help="HTTPS or SSH source URL, including a private repository or fork")
    parser.add_argument("--branch")
    parser.add_argument("--fallback-branch", help="Use this branch only if the selected branch disappears; divergence still pauses automatic updates")
    parser.add_argument("--clear-fallback", action="store_true")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--token-file", type=Path, help="Read an HTTPS token from a file and store it privately")
    auth.add_argument("--ssh-key", type=Path, help="Read an SSH deploy key from a file")
    auth.add_argument("--clear-credentials", action="store_true")
    parser.add_argument("--username", default="git", help="HTTPS repository username")
    parser.add_argument("--known-hosts", type=Path, help="Verified SSH host keys; required with --ssh-key")
    signatures = parser.add_mutually_exclusive_group()
    signatures.add_argument("--require-signatures", type=Path, metavar="ALLOWED_SIGNERS",
                            help="Deploy only revisions signed by a key in this SSH allowed-signers file")
    signatures.add_argument("--clear-signatures", action="store_true",
                            help="Stop requiring signed revisions")


def source_values(args):
    return {"url": args.repo, "branch": args.branch, "fallback": args.fallback_branch, "clear_fallback": args.clear_fallback,
            "token_file": args.token_file, "ssh_key": args.ssh_key, "known_hosts": args.known_hosts,
            "username": args.username, "clear_credentials": args.clear_credentials,
            "signers_file": args.require_signatures, "clear_signatures": args.clear_signatures}


def parser():
    result = argparse.ArgumentParser(description=profile.PRODUCT + " server lifecycle. Automatic source updates are disabled until explicitly enabled.")
    result.add_argument("--root", default="/opt/" + profile.PRODUCT.lower(), help="Managed installation directory")
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install a remembered deployment mode, optionally restoring a package")
    install.add_argument("--mode", choices=profile.modes())
    install.add_argument("--name", help="Service and installed command name")
    install.add_argument("--domain", help="Public HTTPS hostname; omit for a loopback-only installation")
    install.add_argument("--portal-domain", default="")
    install.add_argument("--port", type=int)
    install.add_argument("--backend-url", default="", help="BananaChat web mode: HTTPS compute URL or loopback SSH tunnel")
    install.add_argument("--backend-token-file", type=Path)
    install.add_argument("--ollama-binary", default="")
    install.add_argument("--restore", type=Path, metavar="PACKAGE")
    install.add_argument("--reuse-data", action="store_true", help="Reuse saved configuration/data after uninstall or an interrupted installation")
    source_options(install)
    update = commands.add_parser("update", help="Back up, deploy, check readiness, and roll back on failure")
    update.add_argument("--automatic", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--allow-divergent", action="store_true", help="Explicitly permit a reviewed switch to unrelated or rewritten source history")
    update.add_argument("--retry-failed", action="store_true")
    backup = commands.add_parser("backup", aliases=["migrate"], help="Create a private portable package with code, data, configuration, and repository credentials")
    backup.add_argument("--output", type=Path)
    backup.add_argument("--exclude-model-weights", action="store_true", help="BananaChat: keep download recipes instead of model caches")
    restore = commands.add_parser("restore", help="Back up the current data, then restore a portable package")
    restore.add_argument("package", type=Path)
    restore.add_argument("--domain")
    restore.add_argument("--port", type=int)
    if profile.PRODUCT == "BananaChat":
        restore.add_argument("--legacy-database", action="store_true", help="Import an old database or v1 export after stopping all services and saving a rollback package")
    rollback = commands.add_parser("rollback", help="Restore the package saved before the last successful update")
    rollback.add_argument("--package", type=Path)
    for name in ("status", "start", "stop", "restart", "recover"):
        commands.add_parser(name)
    updates = commands.add_parser("updates", help="Opt into or out of automatic source updates")
    updates.add_argument("action", choices=("status", "enable", "disable"))
    updates.add_argument("--interval", type=int, help="Check interval in minutes (5 to 10080)")
    updates.add_argument("--keep-backups", type=int)
    source_options(updates)
    source = commands.add_parser("source", help="Inspect or change the update source and private-repository authentication")
    source.add_argument("action", choices=("show", "set", "check"))
    source_options(source)
    proxy = commands.add_parser("proxy", help="Print or install the matching HTTPS Caddy configuration")
    proxy.add_argument("--install", action="store_true")
    proxy.add_argument("--replace", action="store_true", help="Explicitly replace an existing Caddyfile, retaining a private backup")
    uninstall = commands.add_parser("uninstall", help="Remove services and updater; retain files unless --purge is selected")
    uninstall.add_argument("--purge", action="store_true")
    uninstall.add_argument("--confirm", default="", metavar="SERVICE_NAME")
    from banana_backup.cli import add_commands
    add_commands(commands)
    if profile.PRODUCT == "BananaChat":
        models = commands.add_parser("models", help="Review or download the model inventory after a compute-server restore")
        models.add_argument("action", choices=("status", "restore", "skip"))
        models.add_argument("--yes", action="store_true", help="Explicitly approve model downloads from the reviewed inventory")
    return result


def configure_proxy(manager, args):
    text = profile.caddyfile(manager.settings())
    if not args.install:
        print(text, end="")
        return None
    destination = manager.system.proxy_file
    previous = destination.read_bytes() if destination.exists() else None
    record = read_json(manager.config_dir / "proxy.json", {})
    owned = destination.is_file() and record.get("installed_sha256") == digest_file(destination)
    if previous and not owned and not args.replace:
        raise ValueError("Caddy already has a configuration. Merge the output of proxy into it, or use proxy --install --replace for an intentional replacement.")
    candidate = manager.config_dir / "Caddyfile.next"
    atomic_write(candidate, text)
    manager.system.run(["caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"])
    backup = record.get("previous") if owned else None
    if previous and not owned:
        backup = manager.root / "backups" / ("Caddyfile-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        atomic_write(backup, previous)
    atomic_write(destination, text, 0o644)
    try:
        manager.system.run(["systemctl", "reload-or-restart", "caddy"])
    except BaseException:
        if previous is not None:
            atomic_write(destination, previous, 0o644)
            manager.system.run(["systemctl", "reload-or-restart", "caddy"], check=False)
        else:
            destination.unlink(missing_ok=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)
    write_json(manager.config_dir / "proxy.json", {"installed_sha256": digest_file(destination), "previous": str(backup) if backup else None})
    return {"outcome": "proxy configured", "domain": manager.settings()["domain"]}


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print("Server maintenance needs root. Run this command with sudo.", file=sys.stderr)
        return 1
    if sys.version_info < (3, 12):
        print("Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    manager = Manager(args.root)
    try:
        if args.command == "install":
            if args.restore:
                result = manager.restore(args.restore, new=True, name=args.name, domain=args.domain, port=args.port)
            else:
                if not args.mode:
                    raise ValueError("Choose --mode, or use --restore PACKAGE to recover the saved deployment mode.")
                result = manager.install(Path(__file__).resolve().parents[1], mode=args.mode, name=args.name, domain=args.domain or "",
                                         portal_domain=args.portal_domain, port=args.port, backend_url=args.backend_url,
                                         backend_token_file=args.backend_token_file, ollama_binary=args.ollama_binary,
                                         source_options=source_values(args), reuse_data=args.reuse_data)
        elif args.command == "update":
            result = manager.update(automatic=args.automatic, allow_divergent=args.allow_divergent, retry_failed=args.retry_failed)
        elif args.command in {"backup", "migrate"}:
            result = {"package": str(manager.backup(args.output, exclude_model_weights=args.exclude_model_weights)), "contains_secrets": True}
        elif args.command == "backups":
            from banana_backup.cli import handle
            from banana_backup.store import Store
            def schedule(policy):
                if (manager.config_dir / "installation.json").exists():
                    manager.system.install_backup_timer(manager.settings(), policy)
                elif policy["enabled"]:
                    raise ValueError("Install or restore the application before enabling its backup schedule.")
            result = handle(args, Store(manager.config_dir / "remote-backup", manager.product),
                            create_package=lambda: manager.backup(manager.backup_name("remote"), exclude_model_weights=True),
                            restore_package=lambda package, options: manager.restore(package, name=options.name,
                                                                                     domain=options.domain, port=options.port),
                            schedule=schedule)
        elif args.command == "models":
            from .models import compute_recovery
            if manager.settings()["mode"] != "compute":
                raise ValueError("Use Admin → Models in BananaChat to review Ollama and Hugging Face downloads.")
            result = compute_recovery(manager.root / "data", args.action, assume_yes=args.yes)
        elif args.command == "restore":
            if getattr(args, "legacy_database", False):
                if args.domain is not None or args.port is not None:
                    raise ValueError("Legacy database import keeps the installed deployment configuration")
                from .legacy_ai import restore_database
                result = restore_database(manager, args.package)
            else:
                result = manager.restore(args.package, domain=args.domain, port=args.port)
        elif args.command == "rollback":
            recent = read_json(manager.config_dir / "last-update.json", {})
            archive = args.package or recent.get("backup")
            if not archive:
                raise ValueError("No previous update package is recorded. Use rollback --package PATH.")
            result = manager.restore(archive)
        elif args.command == "status":
            result = manager.status()
        elif args.command == "updates":
            if args.action == "status":
                result = {"source": manager.source(), "updates": manager.policy()}
            else:
                changes = source_values(args)
                if any(value for key, value in changes.items() if key != "username"):
                    with maintenance_lock(manager.root):
                        manager.configure_source(**changes)
                result = manager.set_updates(args.action == "enable", interval=args.interval, keep=args.keep_backups)
                if args.action == "enable":
                    print("Automatic updates are now enabled. Changes on the configured branch can alter or remove features. Use updates disable to opt out.")
                else:
                    print("Automatic updates are disabled. An update already applying its changes will finish safely.")
        elif args.command == "source":
            if args.action == "set":
                with maintenance_lock(manager.root):
                    result = manager.configure_source(**source_values(args))
            elif args.action == "check":
                from .source import GitSource
                with maintenance_lock(manager.root):
                    revision, branch, forward = GitSource(manager.root, manager.source()).resolve(manager.settings()["revision"])
                result = {"revision": revision, "selected_branch": branch, "fast_forward": forward, "deployed": False}
            else:
                result = manager.source()
        elif args.command in {"start", "stop", "restart", "recover"}:
            with maintenance_lock(manager.root):
                recovered = manager.recover()
                settings = manager.settings()
                services = list(profile.service_commands(settings))
                containers = manager.system.containers(settings)
                if args.command in {"stop", "restart"}:
                    manager.system.stop(services)
                    manager.system.stop_containers(containers)
                if args.command in {"start", "restart"}:
                    manager.system.start(services)
                    if not manager.system.healthy(settings, containers):
                        raise RuntimeError("The service failed readiness checks. Inspect its journal before allowing traffic.")
                    (manager.root / "data/.banana-maintenance").unlink(missing_ok=True)
                result = manager.event(args.command, "complete", recovered=recovered)
        elif args.command == "proxy":
            with maintenance_lock(manager.root):
                result = configure_proxy(manager, args)
        elif args.command == "uninstall":
            result = manager.uninstall(purge=args.purge, confirm=args.confirm)
        else:
            raise ValueError("Unsupported command.")
        if result is not None:
            print(json.dumps(result, indent=2))
        if args.command == "install":
            name = manager.settings()["service"]
            print(f"Next: review {manager.config_dir / 'app.env'}. Use '{name} proxy' for the HTTPS configuration, and '{name} updates enable' only if you want automatic updates.")
            if manager.settings()["mode"] == "compute":
                print(f"The compute API token is in {manager.root / 'data/.compute-api-token'}. Configure it on the web server using --backend-token-file.")
        if args.command in {"install", "restore", "backups"} and manager.product == "BananaChat" and (manager.root / "data/.model-recovery.json").exists():
            print("Model weights were excluded. Review the saved inventory in Admin → Models (compute mode: models status). Downloads wait for administrator approval; you can defer them.")
        return 0 if not isinstance(result, dict) or result.get("outcome") not in {"failed", "paused", "rolled_back"} else 2
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"{profile.PRODUCT}: {error}", file=sys.stderr)
        if manager.config_dir.exists():
            manager.event(args.command, "failed", reason=str(error))
        return 1
