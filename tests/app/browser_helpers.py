"""Where the Playwright tests find Chromium."""

from __future__ import annotations

import os
from pathlib import Path


def chromium_executable() -> str | None:
    if os.environ.get("BC_CHROMIUM"):
        return os.environ["BC_CHROMIUM"]
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux*/chrome")):
        return str(candidate)
    return None
