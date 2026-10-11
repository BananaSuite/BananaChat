"""No file of the repository names a sponsor or the organisation where the project started.

BananaChat is open source for anyone to run: NOTICE and the README credit
"Luca Zani and contributors", and only the website's about page tells where
the project started. The check reads every file Git would commit (tracked and
new, ignored ones excepted), binaries included. The names are built from
pieces so that this file does not contain them either.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = {
    "the former sponsor": re.compile(("canale" + r"[\s_.-]*" + "scuola").encode(), re.IGNORECASE),
    "where the project started": re.compile(("offi" + "cina").encode(), re.IGNORECASE),
}


def _git_available():
    if not (ROOT / ".git").exists():
        return False
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _git_available(), reason="not a Git checkout, so there is no file list to check")


def _repository_files():
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=ROOT, capture_output=True, check=True).stdout
    paths = (ROOT / name for name in listed.decode().split("\0") if name)
    return sorted(path for path in paths if path.is_file() and not path.is_symlink())


def test_no_file_names_a_sponsor_or_an_organisation():
    files = _repository_files()
    assert ROOT / "NOTICE" in files and ROOT / "README.md" in files, "nothing was checked"
    found = [f"{path.relative_to(ROOT)}: {what}" for path in files
             for what, pattern in NAMES.items() if pattern.search(path.read_bytes())]
    assert found == []
