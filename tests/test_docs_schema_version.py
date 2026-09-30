"""The documented schema version follows the migrations."""

import re
from pathlib import Path

from bananachat.db.migrations import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent


def test_the_update_notes_name_the_current_schema_version():
    for name in ("CHANGELOG.md", "docs/deployment.md"):
        found = re.findall(r"schema version (\d+)", (ROOT / name).read_text(encoding="utf-8"))
        assert found, name
        assert {int(n) for n in found} == {SCHEMA_VERSION}, name
