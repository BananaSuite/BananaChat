#!/usr/bin/env python3
"""BananaChat Worker: personal PC inference daemon.

This script is the single entry point for the worker daemon.  Run it
directly for quick testing, or use the install command to set it up as a
system service.

Usage
-----
  # Run the daemon in the foreground (Ctrl+C to stop)
  python bananachat_worker.py run

  # Install as a system service (run once, then start/stop/status below)
  python bananachat_worker.py install [--system]   # --system = system-wide on Linux
  python bananachat_worker.py uninstall [--system]
  python bananachat_worker.py start
  python bananachat_worker.py stop
  python bananachat_worker.py status

  # Show effective configuration (useful for debugging)
  python bananachat_worker.py config

Required environment variables
-------------------------------
  BC_SERVER_URL    Full URL of the BananaChat server, e.g. https://ai.example.com
  BC_WORKER_TOKEN  Worker token from Admin → Workers in the admin panel

Optional environment variables
-------------------------------
  BC_WORKER_NAME           Human-readable name (default: hostname)
  BC_OLLAMA_HOST           Local Ollama URL  (default: http://127.0.0.1:11434)
  BC_OLLAMA_BINARY         Path to ollama binary  (default: ollama)
  BC_OLLAMA_IDLE_TIMEOUT   Seconds before auto-stopping Ollama when idle  (default: 600)
  BC_OLLAMA_KEEP_ALIVE     Seconds to keep model in VRAM after inference  (default: 0)
  BC_IDLE_THRESHOLD        Seconds without input = "idle" state  (default: 300)
  BC_GPU_GAMING_THRESHOLD  GPU % above which we skip inference  (default: 70)
  BC_GPU_ACTIVE_THRESHOLD  GPU % above which we run at low priority  (default: 50)
  BC_WORKER_LOG_LEVEL      Logging level: DEBUG/INFO/WARNING/ERROR  (default: INFO)
"""

import logging
import os
import sys

# Path setup: add the worker/ directory to sys.path so the worker modules
# (config, activity, etc.) can be imported without a package prefix.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import config  # noqa: E402  (must come after path setup)


def _setup_logging():
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    handlers = None
    if config.LOG_FILE:
        # systemd journals stdout and the macOS agent redirects it, but a
        # Windows task has nowhere to put it. Rotate so an unattended worker
        # cannot fill the disk.
        from logging.handlers import RotatingFileHandler

        try:
            directory = os.path.dirname(config.LOG_FILE)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers = [RotatingFileHandler(
                config.LOG_FILE, maxBytes=config.LOG_MAX_BYTES,
                backupCount=config.LOG_BACKUPS, encoding="utf-8",
            )]
        except OSError:
            # An unwritable log path must not stop the worker from running.
            handlers = None
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    if config.LOG_FILE and handlers is None:
        logging.getLogger("bananachat.worker").warning(
            "Could not open %s for logging; logging to the console instead.", config.LOG_FILE)


def _cmd_run():
    _setup_logging()
    import daemon
    daemon.run()


def _cmd_config():
    """Print effective configuration."""
    _setup_logging()
    print("BananaChat Worker: effective configuration")
    print("=" * 50)
    attrs = [
        ("SERVER_URL",            config.SERVER_URL or "(not set)"),
        ("WORKER_TOKEN",          "****" if config.WORKER_TOKEN else "(not set)"),
        ("WORKER_NAME",           config.WORKER_NAME),
        ("OLLAMA_HOST",           config.OLLAMA_HOST),
        ("OLLAMA_BINARY",         config.OLLAMA_BINARY),
        ("OLLAMA_IDLE_TIMEOUT",   f"{config.OLLAMA_IDLE_TIMEOUT} s"),
        ("OLLAMA_KEEP_ALIVE",     f"{config.OLLAMA_KEEP_ALIVE} s"),
        ("IDLE_THRESHOLD",        f"{config.IDLE_THRESHOLD_SECONDS} s"),
        ("LIGHT_THRESHOLD",       f"{config.LIGHT_THRESHOLD_SECONDS} s"),
        ("GPU_GAMING_THRESHOLD",  f"{config.GPU_GAMING_THRESHOLD} %"),
        ("GPU_ACTIVE_THRESHOLD",  f"{config.GPU_ACTIVE_THRESHOLD} %"),
        ("POLL_GAP_SECONDS",      f"{config.POLL_GAP_SECONDS} s"),
        ("HEARTBEAT_INTERVAL",    f"{config.HEARTBEAT_INTERVAL} s"),
        ("LOG_LEVEL",             config.LOG_LEVEL),
    ]
    for name, val in attrs:
        print(f"  {name:<26} {val}")

    import platform as platform_module

    import activity
    import service

    print("\nPlatform")
    print("-" * 50)
    backend = {
        "Linux": "systemd user service (--system for a boot service)",
        "Darwin": "launchd LaunchAgent (--system for a LaunchDaemon)",
        "Windows": "Task Scheduler job at login",
    }.get(service._OS, "not supported; run in the foreground")
    machine = platform_module.machine()
    print(f"  {'OS':<26} {service._OS} ({machine})")
    print(f"  {'Service backend':<26} {backend}")
    print(f"  {'Settings file':<26} {config.ENV_FILE_LOADED or config.DEFAULT_ENV_FILE + ' (not present)'}")
    print(f"  {'Log file':<26} {config.LOG_FILE or '(console, or the platform log)'}")

    idle = activity.get_user_idle_seconds()
    idle_source = {
        "Linux": "xprintidle, GNOME Mutter or the D-Bus screensaver",
        "Darwin": "IOKit HIDIdleTime via ioreg",
        "Windows": "GetLastInputInfo",
    }.get(service._OS, "none")
    print(f"  {'Idle source':<26} {idle_source}")
    print(f"  {'Idle reading':<26} {f'{idle:.0f} s' if idle is not None else 'unavailable'}")

    gpu = activity.get_gpu_utilisation()
    print(f"  {'GPU name':<26} {activity.get_gpu_name() or '(unknown)'}")
    if gpu is None and service._OS == "Darwin":
        note = "unavailable on macOS; idle time decides"
    elif gpu is None:
        note = "unavailable; the worker is treated as idle"
    else:
        note = f"{gpu:.0f} %"
    print(f"  {'GPU utilisation':<26} {note}")

    # Quick connectivity check
    import ollama_mgr
    print("\nOllama status:", "running" if ollama_mgr.is_alive() else "not running")
    if ollama_mgr.is_alive():
        models = ollama_mgr.list_models()
        print(f"Available models ({len(models)}):")
        for m in models:
            print(f"  - {m}")


def _cmd_install(system: bool = False):
    import service
    service.install(system=system)


def _cmd_uninstall(system: bool = False):
    import service
    service.uninstall(system=system)


def _cmd_start(system: bool = False):
    import service
    service.start(system=system)


def _cmd_stop(system: bool = False):
    import service
    service.stop(system=system)


def _cmd_status(system: bool = False):
    import service
    service.status(system=system)


# CLI dispatch

COMMANDS = {
    "run":       _cmd_run,
    "config":    _cmd_config,
    "install":   _cmd_install,
    "uninstall": _cmd_uninstall,
    "start":     _cmd_start,
    "stop":      _cmd_stop,
    "status":    _cmd_status,
}

HELP = __doc__


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        sys.exit(0)

    cmd = args[0]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd!r}")
        print(f"Valid commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    system_flag = "--system" in args
    fn = COMMANDS[cmd]
    import inspect
    sig = inspect.signature(fn)
    if "system" in sig.parameters:
        fn(system=system_flag)
    else:
        fn()


if __name__ == "__main__":
    main()
