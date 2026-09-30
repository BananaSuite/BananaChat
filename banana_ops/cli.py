"""One server command for installation, maintenance, migration, and removal."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
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


CHAT = profile.PRODUCT == "BananaChat"
CHAT_LAYOUT = (" The default layout uses two servers: install --mode compute on the GPU server, then --mode web --pair "
               "on the web server (VPS) with the pairing code it prints; --mode single runs everything on one machine.")


def parser():
    result = argparse.ArgumentParser(description=profile.PRODUCT + " server lifecycle. Automatic source updates are disabled until explicitly enabled."
                                     + (CHAT_LAYOUT if CHAT else ""))
    result.add_argument("--root", default="/opt/" + profile.PRODUCT.lower(), help="Managed installation directory")
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install a remembered deployment mode, optionally restoring a package",
                                  description=CHAT_LAYOUT.strip() if CHAT else None)
    install.add_argument("--mode", choices=profile.modes(),
                         help=("compute: the GPU server; web: the chat server connected to it (implied by --pair); "
                               "single: both on one machine") if CHAT else None)
    install.add_argument("--name", help="Service and installed command name")
    install.add_argument("--domain", help="Public HTTPS hostname; omit for a loopback-only installation")
    install.add_argument("--portal-domain", default="")
    install.add_argument("--port", type=int)
    install.add_argument("--backend-url", default="", help="BananaChat web mode: HTTPS compute URL or loopback SSH tunnel"
                         + (" (overrides the address in --pair)" if CHAT else ""))
    install.add_argument("--backend-token-file", type=Path, help="BananaChat web mode: private file with the compute token")
    install.add_argument("--ollama-binary", default="", help="BananaChat single/compute: the Ollama program for the managed Ollama service")
    install.add_argument("--restore", type=Path, metavar="PACKAGE")
    install.add_argument("--reuse-data", action="store_true", help="Reuse saved configuration/data after uninstall or an interrupted installation")
    if CHAT:
        install.add_argument("--pair", nargs="?", const="-", metavar="CODE",
                             help="web: connect to the compute server with the pairing code printed there "
                                  "(asked for, hidden, when CODE is left out)")
        install.add_argument("--pair-file", type=Path, metavar="FILE", help="web: read the pairing code from a private file")
        install.add_argument("--ollama-url", default="", metavar="URL",
                             help="single/compute: use the Ollama already running on this machine (for example "
                                  "http://127.0.0.1:11434) instead of starting a managed one; it is never started, "
                                  "stopped or reconfigured")
        install.add_argument("--skip-connection-check", action="store_true",
                             help="Do not test the compute server or the existing Ollama before installing")
        install.add_argument("--yes", action="store_true",
                             help="web: use the gateway address in the pairing code without asking (needed without a terminal)")
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
    if CHAT:
        models = commands.add_parser("models", help="Compute server without a web server: review or download the models "
                                                    "missing after a restore")
        models.add_argument("action", choices=("status", "restore", "skip"))
        models.add_argument("--yes", action="store_true", help="Explicitly approve model downloads from the reviewed inventory")
        compute = commands.add_parser("compute", help="Compute server: print the pairing code for the web server, or "
                                                      "replace the gateway token")
        compute.add_argument("action", choices=("pairing-code", "rotate-token"))
        compute.add_argument("--url", help="Address the web server uses to reach this gateway (default: https://DOMAIN, "
                                           "or http://127.0.0.1:PORT through an SSH tunnel)")
        backend = commands.add_parser("backend", help="Web server: show, test or change the connection to the compute server")
        backend.add_argument("action", choices=("status", "test", "connect"))
        backend.add_argument("code", nargs="?", help="connect: the pairing code (asked for, hidden, when left out)")
        backend.add_argument("--pair-file", type=Path, metavar="FILE", help="connect: read the pairing code from a private file")
        backend.add_argument("--url", help="connect: compute address, overriding the one in the pairing code")
        backend.add_argument("--token-file", type=Path, help="connect: private file with the compute token (instead of a pairing code)")
        backend.add_argument("--skip-check", action="store_true", help="connect: store the connection without testing it")
        backend.add_argument("--yes", action="store_true", help="connect: use the gateway address in the pairing code "
                                                                 "without asking (needed without a terminal)")
    return result


def install_connection(args, read_secret):
    """BananaChat: the checked compute connection of a web install and the existing Ollama of single/compute.

    Returns ``(mode, backend_url, backend_token)`` and fails before anything is installed.
    """
    from . import backend
    pairing = args.pair is not None or args.pair_file is not None
    mode = args.mode or ("web" if pairing or args.backend_url else None)
    if not mode:
        return None, args.backend_url, None
    if mode != "web":
        if pairing or args.backend_url or args.backend_token_file:
            raise ValueError("--pair, --backend-url and --backend-token-file are for the web server; "
                             f"a {mode} server uses its own Ollama.")
        if args.ollama_url:
            if args.ollama_binary:
                raise ValueError("Choose either --ollama-url (an existing Ollama) or --ollama-binary (a managed one).")
            url = backend.ollama_url(args.ollama_url)
            if not args.skip_connection_check:
                version = backend.check_ollama(url)
                print(f"Using the existing Ollama {version} at {url}; {profile.PRODUCT} will not manage it.", file=sys.stderr)
        return mode, "", None
    if args.ollama_binary or args.ollama_url:
        raise ValueError("A web server never runs Ollama: --ollama-binary and --ollama-url are for single and compute "
                         "servers. Connect the web server to its compute server with --pair.")
    if pairing:
        if args.backend_token_file:
            raise ValueError("The pairing code already contains the token; leave out --backend-token-file.")
        url, token = backend.read_pairing(None if args.pair_file else args.pair, args.pair_file)
        if args.backend_url:
            url = backend.backend_url(args.backend_url)
        else:
            backend.confirm_address(url, args.yes)
    elif args.backend_url:
        url = backend.backend_url(args.backend_url)
        token = backend.check_token(read_secret(args.backend_token_file)) if args.backend_token_file else None
    else:
        raise ValueError("A web server needs its compute server. On the compute server run 'bananachat compute "
                         "pairing-code', then install this one with --pair (the code is asked for).")
    if not args.skip_connection_check:
        version = backend.check_backend(url, token)
        print(f"Connected to the compute server at {url} (Ollama {version}).", file=sys.stderr)
    return mode, url, token


def next_steps(manager):
    """BananaChat: what an administrator does after installing, one line each."""
    settings = manager.settings()
    name, lines = settings["service"], []
    if settings.get("domain"):
        lines.append(f"HTTPS: point DNS for {settings['domain']} here, then run '{name} proxy --install' "
                     f"(or merge the output of '{name} proxy' into your Caddy configuration).")
    if settings["mode"] == "compute":
        from . import backend
        pairing = backend.pairing_code(manager)
        lines.append("Pairing code for the web server (a secret: it contains the gateway token):")
        lines.append("  " + pairing["pairing_code"])
        lines.append(f"On the web server run: sudo ./banana install --mode web --domain CHAT.EXAMPLE.ORG --pair "
                     f"(it asks for the code). Show it again with '{name} compute pairing-code'.")
        if not settings.get("domain"):
            lines.append(f"Without --domain the code points at {pairing['url']}: forward that port from the web server "
                         "with a supervised SSH tunnel, or print a code for another address with 'compute pairing-code --url'.")
    else:
        lines.append(f"First administrator: open the site and enter the setup token, shown by "
                     f"'sudo grep BC_SETUP_TOKEN {manager.config_dir / 'app.env'}'.")
        if settings["mode"] == "single":
            lines.append("Then download a model in Admin → Models and publish it.")
    lines.append(f"Automatic updates stay off until you run '{name} updates enable'.")
    return lines


def recovery_message(manager):
    """The one sentence printed after a restore that left model downloads to decide."""
    name, mode = manager.settings()["service"], manager.settings()["mode"]
    if mode == "compute":
        return ("Model weights were not in this backup: the web server connected to this compute server will offer "
                f"the missing models for download in Admin → Overview (used without a web server: run '{name} models "
                "restore --yes' here).")
    return ("Model weights were not in this backup: BananaChat checks which saved models are missing on its model "
            "server and, only if some are, Admin → Overview offers to download them.")


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
    manager = None
    try:
        manager = Manager(args.root)
        if args.command == "install":
            if args.restore:
                ignored = [name for name, value in (("--pair", getattr(args, "pair", None) is not None),
                                                    ("--pair-file", getattr(args, "pair_file", None) is not None),
                                                    ("--backend-url", bool(args.backend_url)),
                                                    ("--backend-token-file", args.backend_token_file is not None),
                                                    ("--mode", args.mode is not None),
                                                    ("--skip-connection-check", getattr(args, "skip_connection_check", False)))
                           if value]
                if CHAT and ignored:
                    raise ValueError(", ".join(ignored) + " cannot be combined with --restore: the package keeps its saved "
                                     "mode and compute connection. After the restore, change the connection on the web "
                                     "server with 'bananachat backend connect'.")
                ollama_url = getattr(args, "ollama_url", "")
                result = manager.restore(args.restore, new=True, name=args.name, domain=args.domain, port=args.port,
                                         **({"ollama_url": ollama_url} if ollama_url else {}))
            else:
                mode, backend_url, backend_token = (install_connection(args, manager.read_secret) if CHAT
                                                    else (args.mode, args.backend_url, None))
                if not mode:
                    raise ValueError("Choose --mode, or use --restore PACKAGE to recover the saved deployment mode."
                                     + (CHAT_LAYOUT if CHAT else ""))
                extra = {"backend_token": backend_token, "ollama_url": args.ollama_url} if CHAT else {}
                result = manager.install(Path(__file__).resolve().parents[1], mode=mode, name=args.name, domain=args.domain or "",
                                         portal_domain=args.portal_domain, port=args.port, backend_url=backend_url,
                                         backend_token_file=None if backend_token else args.backend_token_file,
                                         ollama_binary=args.ollama_binary, source_options=source_values(args),
                                         reuse_data=args.reuse_data, **extra)
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
            settings = manager.settings()
            if settings["mode"] != "compute":
                raise ValueError("This server offers missing models in its web interface: Admin → Overview after a "
                                 "restore, and Admin → Models for every download.")
            result = compute_recovery(manager.root / "data", args.action, assume_yes=args.yes,
                                      upstream=profile.ollama_upstream(settings))
        elif args.command == "compute":
            from . import backend
            if args.action == "rotate-token":
                result = backend.rotate_token(manager, args.url)
                print("The old token no longer works. Connect the web server again: on it run "
                      "'bananachat backend connect' and paste the new pairing code.", file=sys.stderr)
            else:
                result = backend.pairing_code(manager, args.url)
                print("Keep this code secret: it contains the gateway token. On the web server run "
                      "'bananachat backend connect' (or install --mode web --pair) and paste it.", file=sys.stderr)
        elif args.command == "backend":
            from . import backend
            if args.action == "connect":
                if args.token_file:
                    if args.code is not None or args.pair_file is not None or not args.url:
                        raise ValueError("Use either a pairing code, or --url with --token-file.")
                    url, token = args.url, manager.read_secret(args.token_file)
                else:
                    url, token = backend.read_pairing(args.code, args.pair_file)
                    if not args.url:
                        backend.confirm_address(url, args.yes)
                    url = args.url or url
                result = backend.connect(manager, url, token, check=not args.skip_check)
            elif args.action == "test":
                result = backend.test(manager)
            else:
                settings = manager.settings()
                if settings["mode"] != "web":
                    raise ValueError("Only a web server has a compute backend; on a compute server use 'compute pairing-code'.")
                result = {"mode": "web", **backend.summary(manager, settings),
                          "test": f"Run '{settings['service']} backend test' to check the connection now."}
        elif args.command == "restore":
            if getattr(args, "legacy_database", False):
                if args.domain is not None or args.port is not None:
                    raise ValueError("Legacy database import keeps the installed deployment configuration")
                from .legacy_import import restore_database
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
            if CHAT:
                from . import backend
                result["inference"] = backend.summary(manager, manager.settings())
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
        if args.command == "update" and not args.automatic and isinstance(result, dict) and result.get("outcome") == "paused":
            print(result.get("reason", "The update is paused."), file=sys.stderr)
        if args.command == "install" and CHAT:
            print("Next:")
            for line in next_steps(manager):
                print(line if line.startswith("  ") else "- " + line)
        elif args.command == "install":
            name = manager.settings()["service"]
            print(f"Next: review {manager.config_dir / 'app.env'}. Use '{name} proxy' for the HTTPS configuration, and '{name} updates enable' only if you want automatic updates.")
        restoring = args.command in {"install", "restore"} or (args.command == "backups" and args.backup_action == "restore")
        if restoring and manager.product == "BananaChat" and model_recovery_pending(manager):
            print(recovery_message(manager))
        return 0 if not isinstance(result, dict) or result.get("outcome") not in {"failed", "paused", "rolled_back"} else 2
    except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        message = f"Missing or invalid setting {error}." if isinstance(error, KeyError) else str(error)
        print(f"{profile.PRODUCT}: {message}", file=sys.stderr)
        # Recording the failure is best effort: it must never replace the
        # original message, for example on a full disk or a damaged config.
        try:
            if manager is not None and manager.config_dir.exists():
                manager.event(args.command, "failed", reason=message)
        except Exception as secondary:
            print(f"{profile.PRODUCT}: the failure could not be recorded in the history: {secondary}", file=sys.stderr)
        return 1


def model_recovery_pending(manager):
    """Only a weight-free restore leaves an inventory waiting for approval."""
    try:
        record = read_json(manager.root / "data/.model-recovery.json")
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("state") == "pending"
