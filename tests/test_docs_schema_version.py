"""The documented schema version follows the migrations."""

import re
from pathlib import Path

from bananachat.db.migrations import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent


def test_the_update_notes_name_the_current_schema_version():
    for name in ("CHANGELOG.md", "docs/deployment.md"):
        found = re.findall(r"schema version (\d+)", (ROOT / name).read_text(encoding="utf-8"))
        assert found, name
        # The changelog is newest first and its older sections name the schema of their own release.
        assert int(found[0]) == SCHEMA_VERSION, name
