"""What a subscription pool still needs on this server, as a checklist for Administration → Models.

Each check says what is wrong and how to fix it, so that an operator whose
pool "does nothing" sees why: the connector is not enabled, its configuration
cannot be read, the official CLI is missing, the sandbox cannot start, or
signing in from the page is not set up. The checks run the CLI's ``--version``
and a sandbox self-test at most every :data:`CACHE_SECONDS`; they never read a
profile or start a sign-in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time

from flask import current_app

from bananachat.services import claude_sandbox

CACHE_SECONDS = 600
SETUP_SCRIPT = "sudo python3 /opt/bananachat/current/scripts/setup_subscriptions.py"
_cache: dict = {}
_lock = threading.Lock()

NAMES = {
    "claude": {"name": "Claude", "client": "Claude Code", "module": "bananachat.services.claude_code",
               "extension": "BC_CLAUDE_EXTENSION", "manifest": "BC_CLAUDE_CODE_CONFIG", "config": "claude_code_config",
               "install": "the official Claude Code native build (https://claude.com/product/claude-code)"},
    "codex": {"name": "Codex", "client": "Codex CLI", "module": "bananachat.services.codex_cli",
              "extension": "BC_CODEX_EXTENSION", "manifest": "BC_CODEX_CLI_CONFIG", "config": "codex_cli_config",
              "install": "the official Codex CLI native binary (https://github.com/openai/codex/releases)"},
}


def _cached(key, compute):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = compute()
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def cli_version(binary: str) -> tuple[bool, str]:
    """``(runs, first line of --version)`` for an executable, without any profile or credential."""
    try:
        info = os.stat(binary)
    except OSError:
        return False, "not found"

    def run():
        with tempfile.TemporaryDirectory(prefix="bc-cli-version-") as home:
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "CODEX_HOME": home,
                           "CLAUDE_CONFIG_DIR": home, "DISABLE_AUTOUPDATER": "1", "NO_COLOR": "1"}
            try:
                result = subprocess.run([binary, "--version"], env=environment, cwd=home, capture_output=True,
                                        timeout=20, check=False, stdin=subprocess.DEVNULL, start_new_session=True)
            except (OSError, subprocess.TimeoutExpired) as error:
                return False, type(error).__name__
        lines = [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        text = next((line for line in lines if any(char.isdigit() for char in line)), "")
        return result.returncode == 0 and bool(text), "".join(c for c in text if c.isprintable())[:80] or \
            f"exit status {result.returncode}"

    return _cached(("version", binary, info.st_mtime_ns, info.st_size), run)


def sandbox_test() -> tuple[bool, str]:
    """Whether Bubblewrap can start an unprivileged sandbox for this service user (the production transport)."""
    def run():
        bwrap = "/usr/bin/bwrap"
        if not os.path.exists(bwrap):
            return False, "Bubblewrap is not installed (/usr/bin/bwrap is missing)."
        try:
            claude_sandbox.trusted_executable(bwrap)
        except (OSError, claude_sandbox.SandboxError) as error:
            return False, str(error)
        true = shutil.which("true", path="/usr/bin:/bin") or "/usr/bin/true"
        command = claude_sandbox.self_test_command(true)
        try:
            result = subprocess.run(command, capture_output=True, timeout=20, check=False,
                                    stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"Bubblewrap could not run ({type(error).__name__})."
        if result.returncode == 0:
            return True, "Bubblewrap starts unprivileged sandboxes."
        detail = " ".join(result.stderr.decode("utf-8", "replace").split())[:200]
        return False, f"Bubblewrap failed: {detail or f'exit status {result.returncode}'}"

    return _cached(("sandbox",), run)


def _check(label, ok, detail, fix=""):
    return {"label": label, "ok": ok, "detail": detail, "fix": fix}


def checks(provider: str) -> list[dict]:
    """The provider's setup checklist (``ok`` True, False, or None when not applicable yet)."""
    app = current_app
    config = app.config["BC"]
    names = NAMES[provider]
    result = []
    module = getattr(config, f"{provider}_extension", "") or ""
    manifest = getattr(config, names["config"], "") or ""
    transport = app.extensions.get(f"{provider}_transport", {}) or {}
    adapter = app.extensions.get(f"{provider}_adapter")
    production = getattr(config, "environment", "production") not in ("development", "testing")
    if not module:
        result.append(_check("Connector enabled", False, f"{names['extension']} is not set, so {names['name']} "
                             "stays disconnected.", f"Run {SETUP_SCRIPT} (or set {names['extension']}="
                             f"{names['module']} and {names['manifest']}), then restart BananaChat."))
        return result
    result.append(_check("Connector enabled", True, f"{names['extension']}={module}"))
    if module != names["module"]:
        result.append(_check("Configuration", None, "A custom adapter is configured; its own documentation "
                             "describes its setup."))
        if transport.get("error"):
            result.append(_check("Adapter", False, "The adapter could not start. See the server log.", ""))
        return result
    if transport.get("error"):
        message = transport.get("message") or "The connector could not start. See the server log."
        result.append(_check("Configuration", False, message, f"Fix it (or run {SETUP_SCRIPT}), then restart "
                             "BananaChat."))
    elif adapter is None:
        result.append(_check("Configuration", False, f"{names['manifest']} is not loaded.",
                             "Restart BananaChat after changing the configuration."))
    else:
        result.append(_check("Configuration", True, f"{names['manifest']}={manifest}"))
    binary = getattr(adapter, "binary", None)
    if binary:
        runs, version = cli_version(binary)
        result.append(_check(f"Official {names['client']}", runs, f"{binary}: {version}",
                             "" if runs else f"Install {names['install']} and run {SETUP_SCRIPT} again."))
    if production:
        ok, detail = sandbox_test()
        result.append(_check("Sandbox", ok, detail, "" if ok else
                             "Install Bubblewrap (sudo apt install bubblewrap). On Ubuntu 23.10 and later, also "
                             "allow it unprivileged user namespaces (see docs/subscription-router.md, "
                             "“Sandbox on Ubuntu”)."))
    else:
        result.append(_check("Sandbox", None, "Development mode: the CLI runs without Bubblewrap."))
    if adapter is not None:
        if getattr(adapter, "managed_root", None):
            result.append(_check("Sign in from this page", True, f"Profiles are kept in {adapter.managed_root}."))
        else:
            result.append(_check("Sign in from this page", False, "The configuration has no managed_root, so "
                                 "accounts can only use profiles signed in on the server's command line.",
                                 f"Run {SETUP_SCRIPT}, which adds one, then restart BananaChat."))
    return result


def ready(provider: str) -> bool:
    return all(item["ok"] is not False for item in checks(provider))
