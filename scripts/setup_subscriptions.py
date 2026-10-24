#!/usr/bin/env python3
"""Prepare a managed BananaChat server for Claude and Codex subscription accounts signed in from its pages.

Run it once, as root, from the installed release::

    sudo python3 /opt/bananachat/current/scripts/setup_subscriptions.py

It then:

1. finds the official Claude Code and Codex CLIs already installed on the
   machine (``--claude`` / ``--codex`` name them explicitly) and copies their
   native executables to ``/opt/bananachat-cli`` (root-owned, not writable by
   the service, as the sandbox requires);
2. creates ``/srv/bananachat-subscriptions`` (root-owned) with one private
   folder per provider owned by the service user, in which BananaChat keeps
   each pooled account's own profile;
3. writes (or extends) the connector configuration files and sets
   ``BC_CLAUDE_EXTENSION``/``BC_CLAUDE_CODE_CONFIG`` and
   ``BC_CODEX_EXTENSION``/``BC_CODEX_CLI_CONFIG`` in ``config/app.env``;
4. checks that Bubblewrap can start sandboxes for the service user (and, with
   ``--allow-bwrap-userns``, installs the AppArmor profile Ubuntu 23.10 and
   later need for that);
5. restarts BananaChat (``--no-restart`` skips it).

Then open Administration → Models → Claude (or Codex), add an account with a
new profile and click **Sign in**. Nothing here signs anything in, reads a
credential or contacts a provider. Run it again after updating a CLI: it
copies the new version. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from banana_ops.files import atomic_write, read_environment, write_environment  # noqa: E402
from bananachat.services import claude_sandbox  # noqa: E402  (standard library only)

CLI_DIR = Path("/opt/bananachat-cli")
PROVIDERS = {
    "claude": {"label": "Claude Code", "command": "claude", "extension": "BC_CLAUDE_EXTENSION",
               "module": "bananachat.services.claude_code", "setting": "BC_CLAUDE_CODE_CONFIG",
               "install": "Install Claude Code first (https://claude.com/product/claude-code: the native installer, "
                          "or npm install -g @anthropic-ai/claude-code), or pass --claude PATH."},
    "codex": {"label": "Codex CLI", "command": "codex", "extension": "BC_CODEX_EXTENSION",
              "module": "bananachat.services.codex_cli", "setting": "BC_CODEX_CLI_CONFIG",
              "install": "Install the Codex CLI first (npm install -g @openai/codex, or the native binary from "
                         "https://github.com/openai/codex/releases), or pass --codex PATH."},
}
APPARMOR_PROFILE = Path("/etc/apparmor.d/bananachat-bwrap")
APPARMOR_TEXT = """# Managed by BananaChat (scripts/setup_subscriptions.py): lets Bubblewrap create the unprivileged user
# namespaces that sandbox the Claude Code and Codex CLIs. Ubuntu 23.10 and later restrict them by default.
abi <abi/4.0>,
include <tunables/global>

profile bananachat-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bananachat-bwrap>
}
"""


def say(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(4) == b"\x7fELF"
    except OSError:
        return False


def _search_path(command: str) -> list[Path]:
    """Where *command* may be installed: PATH, the invoking user's PATH and common locations."""
    found = []
    paths = [os.environ.get("PATH", "")]
    user = os.environ.get("SUDO_USER")
    homes = []
    if user:
        try:
            homes.append(Path(pwd.getpwnam(user).pw_dir))
        except KeyError:
            pass
    homes.append(Path("/root"))
    for home in homes:
        paths.append(":".join(str(home / part) for part in (".local/bin", ".npm-global/bin", "bin")))
    for path in paths:
        hit = shutil.which(command, path=path)
        if hit:
            found.append(Path(hit))
    for pattern in (f"/usr/local/bin/{command}", f"/usr/bin/{command}", f"/opt/*/bin/{command}",
                    f"/usr/lib/node_modules/@*/{command}*/package.json", f"/usr/local/lib/node_modules/@*/{command}*/package.json"):
        found += [Path(item) for item in glob.glob(pattern)]
    return found


def native_executable(candidate: Path, provider: str) -> Path | None:
    """The native build behind a command: npm installs a JavaScript launcher next to the real executable."""
    try:
        path = candidate.resolve(strict=True)
    except OSError:
        return None
    if path.is_file() and _is_elf(path):
        return path
    package = path if path.is_dir() else path.parent
    for _ in range(4):  # from .../bin/codex.js or .../cli-wrapper.cjs up to the package folder
        if (package / "package.json").exists():
            break
        package = package.parent
    patterns = {
        "claude": ["bin/claude.exe", "bin/claude"],
        "codex": ["../codex-linux-*/vendor/*/bin/codex", "node_modules/@openai/codex-linux-*/vendor/*/bin/codex",
                  "vendor/*/codex/codex", "vendor/*/bin/codex"],
    }[provider]
    for pattern in patterns:
        for item in sorted(glob.glob(str(package / pattern))):
            native = Path(item).resolve()
            if native.is_file() and _is_elf(native):
                return native
    return None


def find_cli(provider: str, explicit: str | None) -> Path | None:
    if explicit:
        native = native_executable(Path(explicit), provider)
        if native is None:
            fail(f"{explicit} is not the native {PROVIDERS[provider]['label']} executable (or a launcher next to it).")
        return native
    for candidate in _search_path(PROVIDERS[provider]["command"]):
        native = native_executable(candidate, provider)
        if native is not None:
            return native
    return None


def version(binary: Path, uid: int, gid: int) -> str:
    """The CLI's version, asked as the service user (never as root) from the root-owned copy."""
    with tempfile.TemporaryDirectory(prefix="bc-cli-") as home:
        os.chown(home, uid, gid)
        environment = {"PATH": "/usr/bin:/bin", "HOME": home, "CODEX_HOME": home, "CLAUDE_CONFIG_DIR": home,
                       "DISABLE_AUTOUPDATER": "1", "NO_COLOR": "1"}
        identity = {"user": uid, "group": gid, "extra_groups": []} if os.geteuid() == 0 else {}
        result = subprocess.run([str(binary), "--version"], env=environment, cwd=home, capture_output=True,
                                timeout=60, check=False, **identity)
    text = result.stdout.decode("utf-8", "replace").strip().splitlines()
    if result.returncode != 0 or not text:
        fail(f"{binary} --version failed; it does not look like a working CLI.")
    return text[-1].strip()


def install_cli(provider: str, source: Path, dry_run: bool) -> Path:
    target = CLI_DIR / PROVIDERS[provider]["command"]
    if dry_run:
        say(f"would copy {source} to {target}")
        return target
    CLI_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(CLI_DIR, 0, 0)
    CLI_DIR.chmod(0o755)
    if source.resolve() != target.resolve():
        handle, temporary = tempfile.mkstemp(prefix=".install-", dir=CLI_DIR)
        os.close(handle)
        try:
            shutil.copyfile(source, temporary)
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o755)  # noqa: S103 - the sandbox mounts it read-only for the service user
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    claude_sandbox.trusted_executable(str(target))
    return target


def private_dir(path: Path, uid: int, gid: int, dry_run: bool) -> None:
    if dry_run:
        say(f"would create {path} (owned by the service user, mode 0700)")
        return
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink():
        fail(f"{path} is a link; remove it first.")
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod(0o700)


def write_manifest(provider: str, path: Path, binary: Path, managed: Path, uid: int, gid: int,
                   dry_run: bool) -> None:
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fail(f"{path} is not valid JSON; fix or remove it first.")
        if not isinstance(manifest, dict):
            fail(f"{path} is not a JSON object.")
    manifest["binary"] = str(binary)
    manifest["managed_root"] = str(managed)
    manifest.setdefault("timeout_seconds", 180)
    manifest.setdefault("profiles", {})
    if dry_run:
        say(f"would write {path}: {json.dumps(manifest)}")
        return
    atomic_write(path, json.dumps(manifest, indent=2) + "\n", 0o600, owner=(uid, gid))


def check_sandbox(service: str) -> tuple[bool, str]:
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.exists():
        return False, "Bubblewrap is not installed: sudo apt install bubblewrap"
    try:
        claude_sandbox.trusted_executable(str(bwrap))
    except (OSError, claude_sandbox.SandboxError) as error:
        return False, str(error)
    true = shutil.which("true", path="/usr/bin:/bin") or "/usr/bin/true"
    identity = pwd.getpwnam(service)
    result = subprocess.run(claude_sandbox.self_test_command(true), capture_output=True, timeout=60, check=False,
                            env={"PATH": "/usr/bin:/bin"}, cwd="/", user=identity.pw_uid, group=identity.pw_gid,
                            extra_groups=[])
    if result.returncode == 0:
        return True, "Bubblewrap starts sandboxes for the service user."
    return False, " ".join(result.stderr.decode("utf-8", "replace").split())[:300] or "Bubblewrap failed."


def allow_bwrap_userns(dry_run: bool) -> None:
    if dry_run:
        say(f"would write {APPARMOR_PROFILE} and reload AppArmor")
        return
    atomic_write(APPARMOR_PROFILE, APPARMOR_TEXT, 0o644)
    subprocess.run(["apparmor_parser", "-r", str(APPARMOR_PROFILE)], check=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default="/opt/bananachat", help="the managed installation (default %(default)s)")
    parser.add_argument("--dir", default="/srv/bananachat-subscriptions",
                        help="where the signed-in profiles live (default %(default)s)")
    parser.add_argument("--only", choices=sorted(PROVIDERS), action="append",
                        help="set up only this provider (repeatable; default: every CLI found)")
    parser.add_argument("--claude", help="the claude executable (or npm launcher) to use")
    parser.add_argument("--codex", help="the codex executable (or npm launcher) to use")
    parser.add_argument("--allow-bwrap-userns", action="store_true",
                        help="install an AppArmor profile so Bubblewrap may create user namespaces (Ubuntu 23.10+)")
    parser.add_argument("--no-restart", action="store_true", help="do not restart BananaChat afterwards")
    parser.add_argument("--dry-run", action="store_true", help="only print what would change")
    args = parser.parse_args(argv)

    if sys.platform != "linux":
        fail("This setup is for the Linux server running BananaChat.")
    if os.geteuid() != 0 and not args.dry_run:
        fail("Run it as root: sudo python3 scripts/setup_subscriptions.py")
    root = Path(args.root)
    try:
        installation = json.loads((root / "config" / "installation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail(f"No managed installation at {root} (config/installation.json). Use --root.")
    service = installation.get("service")
    if not isinstance(service, str) or not service:
        fail("The installation metadata names no service.")
    if installation.get("mode") == "compute":
        fail("This is a compute server; set up subscriptions on the web (or single) server.")
    identity = pwd.getpwnam(service)
    uid, gid = identity.pw_uid, identity.pw_gid
    base = Path(args.dir)
    refused = ("/home", "/root", "/tmp", "/var/tmp")  # noqa: S108 - refused locations, not used
    if not base.is_absolute() or any(base == Path(prefix) or base.is_relative_to(prefix) for prefix in refused):
        fail("Keep the profiles under /srv (or another absolute path outside /home, /root and /tmp).")
    if len(base.parts) < 3 or base.is_relative_to(root) or root.is_relative_to(base):
        fail("Choose a folder of its own for the profiles, such as /srv/bananachat-subscriptions.")
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        fail(f"{base} is not a plain folder.")
    if base.is_dir():
        # It is made root-owned and listable below: only a folder this script made (or an empty one) qualifies.
        known = {*PROVIDERS, *(f"{provider}.json" for provider in PROVIDERS)}
        strangers = sorted(child.name for child in base.iterdir() if child.name not in known)
        if strangers:
            fail(f"{base} already holds other files ({', '.join(strangers[:5])}); choose an empty or new folder.")

    wanted = args.only or sorted(PROVIDERS)
    found = {}
    for provider in wanted:
        cli = find_cli(provider, getattr(args, provider))
        if cli is None:
            say(f"{PROVIDERS[provider]['label']}: not found. {PROVIDERS[provider]['install']}")
            continue
        found[provider] = cli
    if not found:
        fail("No official CLI was found; nothing to set up.")

    if not args.dry_run:
        base.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chown(base, 0, 0)
        base.chmod(0o755)
    environment_path = root / "config" / "app.env"
    environment = read_environment(environment_path)
    for provider, cli in found.items():
        names = PROVIDERS[provider]
        binary = install_cli(provider, cli, args.dry_run)
        say(f"{names['label']}: {cli}" + ("" if args.dry_run else f" ({version(binary, uid, gid)})"))
        managed = base / provider
        private_dir(managed, uid, gid, args.dry_run)
        manifest = Path(environment.get(names["setting"]) or base / f"{provider}.json")
        write_manifest(provider, manifest, binary, managed, uid, gid, args.dry_run)
        environment[names["extension"]] = names["module"]
        environment[names["setting"]] = str(manifest)
        if not args.dry_run:
            claude_sandbox.managed_paths(manifest, uid=uid, root=root,
                                         keys=("home", "config_dir") if provider == "claude" else ("codex_home",))
        say(f"{names['label']}: profiles in {managed}, configuration {manifest}")
    if args.dry_run:
        say(f"would set {', '.join(key for key in sorted(environment) if key.startswith(('BC_CLAUDE', 'BC_CODEX')))} "
            f"in {environment_path}")
    else:
        write_environment(environment_path, environment)

    if args.allow_bwrap_userns:
        allow_bwrap_userns(args.dry_run)
    if not args.dry_run:
        ok, detail = check_sandbox(service)
        say(("Sandbox: " if ok else "Sandbox problem: ") + detail)
        if not ok:
            say("On Ubuntu 23.10 and later, run this again with --allow-bwrap-userns (or allow Bubblewrap "
                "user namespaces another way). Subscription answers stay off until the sandbox works.")
    if not args.no_restart and not args.dry_run:
        command = shutil.which(service, path="/usr/local/bin:/usr/bin") or f"/usr/local/bin/{service}"
        say(f"Restarting: {command} restart")
        result = subprocess.run([command, "restart"], check=False)
        if result.returncode != 0:
            fail(f"The restart failed; see '{command} status' and the service journal.")
    say("Done. Open Administration → Models → Claude or Codex, add an account with a new profile and click "
        "Sign in.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
