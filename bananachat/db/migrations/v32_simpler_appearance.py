"""Version 32: fewer appearance choices, the same look for everyone who chose one.

Appearance offered six contrast levels (Standard, +1 to +4, Maximum) and five separate highlighting
choices for answers (bold, italic, inline code, links, headings; each Off, Subtle or Strong). It now
offers three contrast levels (Standard, High, Maximum) and one highlighting choice for all five.

Each account's stored preferences (``users.accessibility``, JSON) are converted so nobody gets less
than they chose: contrast +1 to +4 becomes High (as strong as the former +4), Maximum stays Maximum;
the highlighting level is the strongest of the five former choices. The five former keys are removed.

A preference object with a ``highlight`` key is already converted, so this is safe to run again.
Frozen: do not import live defaults or validators.
"""

import json

LEGACY_SEMANTIC_KEYS = ("semantic_bold", "semantic_italic", "semantic_code", "semantic_link", "semantic_heading")
CONTRAST = {0: 0, 1: 1, 2: 1, 3: 1, 4: 1, 5: 2}


def _level(value, high):
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if 0 <= number <= high else 0


def convert(prefs: dict) -> dict:
    """The preferences of a release before version 32 in today's form (unknown keys are kept)."""
    if "highlight" in prefs:
        return prefs
    converted = {key: value for key, value in prefs.items() if key not in LEGACY_SEMANTIC_KEYS}
    converted["contrast"] = CONTRAST[_level(prefs.get("contrast"), 5)]
    converted["highlight"] = max(_level(prefs.get(key), 2) for key in LEGACY_SEMANTIC_KEYS)
    return converted


def upgrade(conn):
    rows = conn.execute("SELECT id, accessibility FROM users WHERE accessibility IS NOT NULL "
                        "AND accessibility <> ''").fetchall()
    for user_id, stored in rows:
        try:
            prefs = json.loads(stored)
        except (TypeError, ValueError, RecursionError):
            continue  # read as the defaults, as before
        if not isinstance(prefs, dict) or "highlight" in prefs:
            continue
        conn.execute("UPDATE users SET accessibility=? WHERE id=?", (json.dumps(convert(prefs)), user_id))
