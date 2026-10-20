""".gitignore keeps keys, certificates, archives and local data out of `git add .`."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MUST_BE_IGNORED = [
    "secret.key",
    "private.pem",
    "client.p12",
    "store.jks",
    "release.keystore",
    "server.crt",
    "backup.tar.gz",
    ".env",
    "node_modules/left-pad/index.js",
    ".venv/bin/python",
    "instance/site.db",
]

MUST_NOT_BE_IGNORED = [
    "README.md",
    "requirements.txt",
    ".gitignore",
]


def _git_available():
    if not (ROOT / ".git").exists():
        return False
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _git_available(), reason="not a Git checkout, so there are no ignore rules to check"
)


def _is_ignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, capture_output=True
    )
    return result.returncode == 0


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_sensitive_paths_are_ignored(path):
    assert _is_ignored(path), f"{path} would be committed by `git add .`"


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_project_files_are_not_ignored(path):
    assert not _is_ignored(path), f"{path} is ignored, so the project would not ship it"
