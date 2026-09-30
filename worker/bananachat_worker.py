#!/usr/bin/env python3
"""BananaChat worker: lend this computer's Ollama to a BananaChat server.

Usage:
  python bananachat_worker.py run                  run in the foreground (Ctrl+C stops it)
  python bananachat_worker.py install [--system]   install the background service and settings file
  python bananachat_worker.py uninstall [--system]
  python bananachat_worker.py start|stop|status [--system]
  python bananachat_worker.py config               show the effective settings and readings

Settings come from environment variables and the settings file
(~/.config/bananachat-worker.env, or %APPDATA%\\BananaChat\\bananachat-worker.env
on Windows; BC_WORKER_ENV_FILE points elsewhere). Required: BC_SERVER_URL and
BC_WORKER_TOKEN. See README.md for every option.

--system (Linux and macOS) installs a service that starts at boot and runs as
the account that installs it.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path

PACKAGE_ALIAS = "bananachat_worker_package"
COMMANDS = ("run", "config", "install", "uninstall", "start", "stop", "status")


def _package():
    """The worker package, however this file was started.

    As a script, ``sys.path[0]`` is this folder; its module names (config,
    service, ...) must not shadow anything else, so the folder is removed from
    the path and loaded as a package under a private name instead.
    """
    if __package__:
        return importlib.import_module(__package__)
    here = Path(__file__).resolve().parent
    if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == here:
        sys.path.pop(0)
    if PACKAGE_ALIAS in sys.modules:
        return sys.modules[PACKAGE_ALIAS]
    spec = importlib.util.spec_from_file_location(PACKAGE_ALIAS, here / "__init__.py",
                                                  submodule_search_locations=[str(here)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_ALIAS] = module
    spec.loader.exec_module(module)
    return module


def _module(name: str):
    return importlib.import_module(f"{_package().__name__}.{name}")


def setup_logging(settings) -> None:
    level = getattr(logging, settings.log_level, logging.INFO)
    handlers = None
    problem = None
    if settings.log_file:
        # A Windows scheduled task has nowhere to send output, so it writes a
        # rotated file (an unattended worker must not fill the disk).
        from logging.handlers import RotatingFileHandler
        try:
            directory = os.path.dirname(settings.log_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers = [RotatingFileHandler(settings.log_file, maxBytes=settings.log_max_bytes,
                                            backupCount=settings.log_backups, encoding="utf-8")]
        except OSError as error:
            problem = error
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers, force=True)
    if problem is not None:
        logging.getLogger("bananachat.worker").warning("Cannot write %s (%s); logging to the console.",
                                                       settings.log_file, problem)


def show_config(settings) -> None:
    activity = _module("activity")
    service = _module("service")
    local_ollama = _module("local_ollama")
    rows = [
        ("Settings file", settings.env_file or f"{_module('config').default_env_file()} (not present)"),
        ("Server", settings.server_url or "(not set)"),
        ("Token", "set" if settings.token else "(not set)"),
        ("Name", settings.name),
        ("Ollama", settings.ollama_host),
        ("Ollama program", settings.ollama_binary),
        ("Stop Ollama after", f"{settings.ollama_idle_timeout} s without a job" if settings.ollama_idle_timeout else "never"),
        ("First-token timeout", f"{settings.first_token_timeout} s"),
        ("Idle / light thresholds", f"{settings.idle_threshold} s / {settings.light_threshold} s"),
        ("GPU gaming / active", f"{settings.gpu_gaming_threshold:g} % / {settings.gpu_active_threshold:g} %"),
        ("Log file", settings.log_file or "(console or the service log)"),
        ("Service", service.describe_backend()),
    ]
    for label, value in rows:
        print(f"  {label:<24} {value}")
    idle = activity.get_user_idle_seconds()
    gpu = activity.get_gpu_utilisation()
    print(f"  {'Idle reading':<24} {'unavailable' if idle is None else f'{idle:.0f} s'}")
    print(f"  {'GPU':<24} {activity.get_gpu_name() or 'unknown'} "
          f"({'no utilisation reading' if gpu is None else f'{gpu:.0f} % busy'})")
    print(f"  {'Activity state':<24} {activity.classify(gpu, idle, settings)}")
    for problem in settings.problems():
        print(f"  PROBLEM: {problem}")
    for warning in settings.warnings:
        print(f"  WARNING: {warning}")
    ollama = local_ollama.LocalOllama(settings)
    if ollama.is_alive():
        models = ollama.models()
        print(f"\nOllama {ollama.version() or ''} is running with {len(models)} model(s):")
        for model in models:
            print(f"  - {model}")
    else:
        print("\nOllama is not running (the worker starts it when needed).")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command = args[0]
    if command not in COMMANDS:
        print(f"Unknown command {command!r}. Commands: {', '.join(COMMANDS)}", file=sys.stderr)
        return 1
    system = "--system" in args[1:]
    unknown = [arg for arg in args[1:] if arg != "--system"]
    if unknown:
        print(f"Unknown option(s): {' '.join(unknown)}", file=sys.stderr)
        return 1
    settings = _module("config").load()
    if command == "run":
        setup_logging(settings)
        return _module("daemon").run(settings)
    if command == "config":
        show_config(settings)
        return 0
    service = _module("service")
    try:
        getattr(service, command)(system=system)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"{command} failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # subprocess.CalledProcessError and friends
        print(f"{command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
