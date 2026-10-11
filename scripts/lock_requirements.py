#!/usr/bin/env python3
"""Regenerate and verify the hash-pinned dependency locks that production installs use.

``requirements.txt`` keeps compatible ranges, for development and for anyone
installing the package as a library. Production installs (the managed
updater's release environments and the Docker images) install
``requirements.lock`` instead: every package, dependencies included, at one
exact version, accepted only with the SHA-256 of a file PyPI published for
it, and only as a wheel::

    pip install --require-hashes --only-binary=:all: -r requirements.lock

``requirements-build.lock`` pins the installer itself (pip, setuptools) the
same way. Both are universal: one file serves every supported target, CPython
3.12 to 3.14 on Linux x86_64 and aarch64 (glibc 2.39 or newer). The compute
node, checkpoint agent and worker use only the standard library.

    python scripts/lock_requirements.py             regenerate both (needs uv: pip install uv), then verify
    python scripts/lock_requirements.py --upgrade   the same, moving every pin to the newest version in range
    python scripts/lock_requirements.py --verify    check that every target has a wheel with a pinned hash
    python scripts/lock_requirements.py --check     offline: the locks are complete and satisfy requirements.txt

Regenerate after changing requirements.txt, and on a schedule for fixes;
review the diff like code. ``--check`` runs in the tests, so a requirement
added without regenerating the lock fails CI.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = "requirements.txt"
LOCK = "requirements.lock"
BUILD_LOCK = "requirements-build.lock"
BUILD_REQUIREMENTS = ("pip>=26.2.1", "setuptools>=83")
OLDEST_PYTHON = "3.12"
PYTHONS = ((3, 12), (3, 13), (3, 14))
ARCHITECTURES = ("x86_64", "aarch64")
GLIBC = (2, 39)  # Ubuntu 24.04; Debian 13 is newer
COMMAND = "python scripts/lock_requirements.py"


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> list[tuple[str, str, str, list[str]]]:
    """``(canonical name, version, marker, sha256 hashes)`` of each entry of a lock file."""
    logical, current = [], ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current += " " + line.removesuffix("\\")
        if not line.endswith("\\"):
            logical.append(current.strip())
            current = ""
    entries = []
    for line in [*logical, *([current.strip()] if current.strip() else [])]:
        spec, _, marker = line.partition(" --hash=")[0].partition(";")
        name, _, version = spec.strip().partition("==")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?=\s|$)", line)
        if len(hashes) != line.count("--hash="):
            hashes = []  # only SHA-256, so check() reports the entry
        entries.append((canonical(name), version.strip(), marker.strip(), hashes))
    return entries


def check(root: Path = ROOT) -> list[str]:
    """Problems with the locks that need no network: missing hashes or pins, ranges they do not satisfy."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    problems = []
    for lock, wanted in ((root / LOCK, None), (root / BUILD_LOCK, BUILD_REQUIREMENTS)):
        if not lock.is_file():
            problems.append(f"{lock.name} is missing: run {COMMAND}")
            continue
        entries = pins(lock)
        versions: dict[str, list[str]] = {}
        for name, version, _marker, hashes in entries:
            if not version or not hashes:
                problems.append(f"{lock.name}: {name} is not pinned to one version with SHA-256 hashes")
            versions.setdefault(name, []).append(version)
        if wanted is None:
            lines = (root / REQUIREMENTS).read_text(encoding="utf-8").splitlines()
            wanted = tuple(line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip())
        for text in wanted:
            requirement = Requirement(text)
            found = versions.get(canonical(requirement.name))
            if not found:
                problems.append(f"{lock.name} has no pin for {requirement.name}: run {COMMAND}")
            elif not all(requirement.specifier.contains(Version(version), prereleases=True) for version in found):
                problems.append(f"{lock.name} pins {requirement.name} {', '.join(found)}, outside {requirement}: "
                                f"run {COMMAND}")
    return problems


def _platforms(architecture: str) -> list[str]:
    tags = [f"manylinux_{GLIBC[0]}_{minor}_{architecture}" for minor in range(GLIBC[1], 16, -1)]
    tags.append(f"manylinux2014_{architecture}")
    if architecture == "x86_64":
        tags += ["manylinux_2_12_x86_64", "manylinux2010_x86_64", "manylinux_2_5_x86_64", "manylinux1_x86_64"]
    return tags


def verify(root: Path = ROOT) -> list[str]:
    """Problems with wheels on PyPI: a target with no wheel whose hash the lock pins (network)."""
    from packaging.markers import Marker
    from packaging.tags import compatible_tags, cpython_tags
    from packaging.utils import parse_wheel_filename

    problems = []
    files: dict[tuple[str, str], list[dict]] = {}
    for lock in (root / LOCK, root / BUILD_LOCK):
        entries = pins(lock)
        for python in PYTHONS:
            for architecture in ARCHITECTURES:
                platforms = _platforms(architecture)
                supported = {*cpython_tags(python, platforms=platforms),
                             *compatible_tags(python, f"cp{python[0]}{python[1]}", platforms)}
                environment = {
                    "python_version": f"{python[0]}.{python[1]}", "python_full_version": f"{python[0]}.{python[1]}.0",
                    "sys_platform": "linux", "platform_system": "Linux", "platform_machine": architecture,
                    "os_name": "posix", "implementation_name": "cpython", "platform_python_implementation": "CPython",
                }
                target = f"CPython {python[0]}.{python[1]} on Linux {architecture}"
                chosen: dict[str, str] = {}
                for name, version, marker, hashes in entries:
                    if marker and not Marker(marker).evaluate(environment):
                        continue
                    if name in chosen:
                        problems.append(f"{lock.name}: {name} is pinned twice for {target}")
                    chosen[name] = version
                    if (name, version) not in files:
                        url = f"https://pypi.org/pypi/{name}/{version}/json"
                        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
                            files[(name, version)] = json.load(response)["urls"]
                    if not any(item["filename"].endswith(".whl") and item["digests"]["sha256"] in hashes
                               and supported & set(parse_wheel_filename(item["filename"])[3])
                               for item in files[(name, version)]):
                        problems.append(f"{lock.name}: no wheel of {name} {version} for {target}")
    return problems


def generate(root: Path = ROOT, *, upgrade: bool = False) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Regenerating the locks needs uv: python -m pip install uv")
    common = ["pip", "compile", "--quiet", "--universal", "--python-version", OLDEST_PYTHON, "--generate-hashes",
              "--only-binary", ":all:", "--custom-compile-command", COMMAND, *(["--upgrade"] if upgrade else [])]
    subprocess.run([uv, *common, REQUIREMENTS, "--output-file", LOCK], cwd=root, check=True)
    with tempfile.TemporaryDirectory() as directory:
        wanted = Path(directory) / "requirements-build.in"
        wanted.write_text("\n".join(BUILD_REQUIREMENTS) + "\n", encoding="utf-8")
        subprocess.run([uv, *common, "--no-annotate", str(wanted), "--output-file", str(root / BUILD_LOCK)],
                       cwd=root, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="offline consistency check only")
    action.add_argument("--verify", action="store_true", help="check wheels on PyPI only")
    parser.add_argument("--upgrade", action="store_true", help="move every pin to the newest version in range")
    args = parser.parse_args(argv)
    if not args.check and not args.verify:
        generate(upgrade=args.upgrade)
    problems = check() + ([] if args.check else verify())
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print("The dependency locks are complete" + ("." if args.check else ", and every target has wheels."))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
