"""The browser upgrade check (scripts/check_upgrade.py) starts from the releases servers upgrade from."""

import importlib.util
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_upgrade", ROOT / "scripts/check_upgrade.py")
check_upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_upgrade)


def show(revision, path):
    result = subprocess.run(["git", "-C", str(ROOT), "show", f"{revision}:{path}"], capture_output=True, text=True)
    if result.returncode:
        pytest.skip("needs the release history (a full Git clone)")
    return result.stdout


@pytest.mark.parametrize("name, revision", check_upgrade.PREVIOUS_RELEASES.items())
def test_each_previous_release_has_the_pages_the_check_drives(name, revision):
    version = re.search(r'__version__ = "([^"]+)"', show(revision, "bananachat/__init__.py"))[1]
    assert version == name or version.startswith(name + ".")
    assert 'id="send-button"' in show(revision, "bananachat/templates/chat/_composer.html")
    assert 'id="bc-boot"' in show(revision, "bananachat/templates/partials/boot.html")
    assert '@bp.post("/developer/tokens"' in show(revision, "bananachat/web/developer.py")
    assert '@bp.post("/personalities"' in show(revision, "bananachat/web/personalities.py")
    assert '@bp.post("/api/preferences"' in show(revision, "bananachat/web/customization.py")
