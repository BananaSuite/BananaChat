#!/usr/bin/env python3
"""Check or synchronize the lifecycle source shared by BananaWiki and BananaChat."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "banana_ops/shared-files.json"
FIXED_FILES = {
    "http_transport.py",
    "tests/test_http_transport.py",
    "private_logs.py",
    "ops_observability.py",
    "tests/test_private_logs.py",
    "sqlite_runtime.py",
    "sqlite_migrations.py",
    "sqlite_snapshot.py",
    "tests/test_sqlite_runtime.py",
    "tests/test_sqlite_migrations.py",
    "tests/test_sqlite_snapshot.py",
    "banana",
    "scripts/sync_lifecycle.py",
    "tests/test_managed_lifecycle.py",
    "tests/test_private_update_source.py",
    "tests/test_release_permissions.py",
    "tests/test_shared_lifecycle.py",
}


def safe_file(root, relative):
    """Resolve a repository file without following links or escaping its root."""
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Invalid shared file path: {relative}")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Shared files must not use symbolic links: {relative}")
    return current


def product(root):
    """Read the product marker as data without importing application code."""
    path = safe_file(root, "banana_ops/product.py")
    tree = ast.parse(path.read_text())
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "PRODUCT"
        and isinstance(node.value, ast.Constant)
    ]
    if len(values) != 1 or values[0] not in {"BananaWiki", "BananaChat"}:
        raise ValueError("The target needs a BananaWiki or BananaChat product marker.")
    return values[0]


def inventory(root):
    """Hash common implementation, launcher, maintenance tool, and regressions."""
    product(root)
    # Read each checkout's declarations as data, so an older recorded copy can
    # receive a newly shared file without accepting unreviewed local changes.
    tree = ast.parse(safe_file(root, "scripts/sync_lifecycle.py").read_text())
    declarations = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "FIXED_FILES" for target in node.targets)]
    fixed = ast.literal_eval(declarations[0]) if len(declarations) == 1 else None
    if not isinstance(fixed, set) or not all(isinstance(name, str) for name in fixed):
        raise ValueError("The shared source tool needs a literal FIXED_FILES set.")
    package = safe_file(root, "banana_ops")
    files = fixed | {
        path.relative_to(root).as_posix()
        for path in package.rglob("*.py")
        if path != package / "product.py" and "__pycache__" not in path.parts
    }
    return {
        relative: hashlib.sha256(safe_file(root, relative).read_bytes()).hexdigest()
        for relative in sorted(files)
    }


def read_manifest(root):
    """Load the recorded inventory and reject unexpected manifest formats."""
    record = json.loads(safe_file(root, MANIFEST).read_text())
    if record.get("schema") != 1 or not isinstance(record.get("files"), dict):
        raise ValueError("Unsupported lifecycle source manifest.")
    return record


def check(root):
    """Fail if a shared file changed without an explicit manifest update."""
    record = read_manifest(root)
    current = inventory(root)
    changed = sorted(
        name
        for name in record["files"].keys() | current.keys()
        if record["files"].get(name) != current.get(name)
    )
    if changed:
        raise ValueError(
            "Shared lifecycle changes need review and --record: " + ", ".join(changed)
        )
    return record


def atomic_write(path, data, mode=0o644):
    """Replace a regular source file after preparing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".lifecycle-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record(root):
    """Record reviewed common files; this command deliberately changes metadata."""
    value = {"schema": 1, "files": inventory(root)}
    atomic_write(
        safe_file(root, MANIFEST), (json.dumps(value, indent=2) + "\n").encode()
    )
    return value


def synchronize(source, target):
    """Copy reviewed common files while preserving product and application data."""
    expected, previous = check(source), check(target)
    if source == target:
        raise ValueError("Choose a different checkout as the synchronization target.")
    # Resolve every destination before writing so a linked directory cannot
    # redirect a partial copy into another part of the checkout.
    destinations = {
        name: safe_file(target, name)
        for name in expected["files"].keys() | previous["files"].keys() | {MANIFEST}
    }
    for name in expected["files"]:
        original = safe_file(source, name)
        atomic_write(
            destinations[name], original.read_bytes(), original.stat().st_mode & 0o777
        )
    for name in previous["files"].keys() - expected["files"].keys():
        destinations[name].unlink()
    atomic_write(destinations[MANIFEST], safe_file(source, MANIFEST).read_bytes())
    check(target)


def main(argv=None):
    """Run a local check by default, or an explicitly selected maintenance step."""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check",
        action="store_true",
        help="Check local files and optionally compare another checkout",
    )
    actions.add_argument(
        "--record", action="store_true", help="Record reviewed changes in this checkout"
    )
    actions.add_argument(
        "--write",
        action="store_true",
        help="Copy reviewed common files to another clean shared copy",
    )
    parser.add_argument(
        "target", nargs="?", type=Path, help="Local checkout to compare or synchronize"
    )
    args = parser.parse_args(argv)
    if args.record and args.target:
        parser.error("--record applies only to the checkout containing this script")
    if args.write and args.target is None:
        parser.error("--write requires a target checkout")
    try:
        target = args.target.resolve() if args.target is not None else None
        if args.record:
            record(ROOT)
        elif args.write:
            synchronize(ROOT, target)
        else:
            expected = check(ROOT)
            if target is not None and check(target) != expected:
                raise ValueError(
                    "The shared copies differ; review them before using --write."
                )
        print(
            "Shared lifecycle source verified"
            + (f" for {product(target)}" if target else "")
            + "."
        )
    except (OSError, ValueError, SyntaxError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
