"""Version the complete static asset tree, including relative module imports."""

from __future__ import annotations

import hashlib
from pathlib import Path


def revision(root: Path | None = None) -> str:
    """Return the same content revision for every worker serving this release."""
    root = root if root is not None else Path(__file__).resolve().parent / "static"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    return digest.hexdigest()[:20]
