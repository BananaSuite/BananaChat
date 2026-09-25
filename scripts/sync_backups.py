#!/usr/bin/env python3
"""Check or synchronize the backup source shared by BananaWiki, BananaChat and BananaVibe."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "banana_backup/shared-files.json"
FIXED = {"scripts/sync_backups.py", "tests/test_git_backups.py"}


def file(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid shared backup source path.")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Shared backup source cannot traverse symbolic links.")
    return path


def inventory(root):
    names = FIXED | {path.relative_to(root).as_posix() for path in file(root, "banana_backup").glob("*.py")}
    return {name: hashlib.sha256(file(root, name).read_bytes()).hexdigest() for name in sorted(names)}


def check(root):
    record = json.loads(file(root, MANIFEST).read_text())
    if record.get("schema") != 1 or record.get("files") != inventory(root):
        raise ValueError("Shared backup changes need review and scripts/sync_backups.py --record.")
    return record


def write(path, content, mode=0o644):
    """Replace *path* atomically, keeping the permissions the caller asked for.

    The copied files include this script, so a fixed 0644 here would leave the
    destination checkout with a shared tool it cannot execute.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--record", action="store_true")
    actions.add_argument("--write", type=Path, metavar="CHECKOUT")
    actions.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.record:
            write(file(ROOT, MANIFEST), (json.dumps({"schema": 1, "files": inventory(ROOT)}, indent=2) + "\n").encode())
        else:
            source = check(ROOT)
            if args.write:
                target = args.write.resolve()
                previous = check(target)  # Never overwrite unreviewed changes.
                destinations = {name: file(target, name) for name in source["files"].keys() | previous["files"].keys() | {MANIFEST}}
                for name in source["files"]:
                    original = file(ROOT, name)
                    write(destinations[name], original.read_bytes(), original.stat().st_mode & 0o777)
                for name in previous["files"].keys() - source["files"].keys():
                    destinations[name].unlink()
                write(destinations[MANIFEST], file(ROOT, MANIFEST).read_bytes())
                check(target)
        print("Shared backup source verified.")
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
