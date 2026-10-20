"""Every setting the web application reads is documented and listed in ``.env.example``.

``test_interface_contracts.test_configuration_reference_lists_every_setting`` covers the
settings parsed by ``bananachat/config.py``'s typed readers; this covers the
others: the optional flag reader and the few settings read straight from the
environment (the development server, the launcher, Gunicorn, Git downloads).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Read for compatibility only, or the names of removed settings that are reported when still present.
IGNORED = {"BC_DEBUG"}


def _read_settings() -> set[str]:
    names = set(re.findall(r'r\.optional_flag\("(BC_[A-Z0-9_]+)"', (ROOT / "bananachat" / "config.py").read_text()))
    sources = [ROOT / "gunicorn.conf.py", ROOT / "wsgi.py", *(ROOT / "bananachat").rglob("*.py")]
    pattern = re.compile(r'(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault)\(\s*"(BC_[A-Z0-9_]+)"'
                         r'|os\.environ\[\s*"(BC_[A-Z0-9_]+)"\s*\]')
    for path in sources:
        if path.name == "config.py" and path.parent.name == "bananachat":
            continue
        for match in pattern.finditer(path.read_text()):
            names.add(match.group(1) or match.group(2))
    return names - IGNORED


def test_settings_read_outside_the_typed_readers_are_documented_and_in_the_sample():
    names = _read_settings()
    assert {"BC_INFERENCE_LOCAL", "BC_AGENTS_GIT_PROXY", "BC_WORKERS"} <= names  # the scan finds them
    documented = (ROOT / "docs" / "configuration.md").read_text()
    sample = (ROOT / ".env.example").read_text()
    for name in sorted(names):
        assert f"`{name}`" in documented, f"{name} is missing from docs/configuration.md"
        assert f"# {name}=" in sample, f"{name} is missing from .env.example"
