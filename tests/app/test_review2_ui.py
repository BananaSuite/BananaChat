"""Regressions from the interface, translation, accessibility and documentation review."""

from __future__ import annotations

import re
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "bananachat" / "static"


# ----- translations -----------------------------------------------------------

def _placeholders(text) -> set[str]:
    texts = text.values() if isinstance(text, dict) else [text]
    return {field for value in texts for _, field, _, _ in string.Formatter().parse(value) if field}


def test_translations_use_the_same_placeholders_in_both_languages():
    from bananachat.i18n import catalogs

    tables = catalogs()
    for key, english in tables["en"].items():
        italian = tables["it"][key]
        assert type(english) is type(italian), key
        assert _placeholders(english) == _placeholders(italian), key


def test_every_browser_string_exists():
    from bananachat.i18n import catalogs

    known = {key[3:] for key in catalogs()["en"] if key.startswith("js.")}
    used = set()
    for script in (STATIC / "js").glob("*.js"):
        used |= {(match, script.name) for match in re.findall(r"""\bt\(\s*["']([a-z0-9_]+)["']""", script.read_text())}
    missing = sorted(f"{name}: {key}" for key, name in used if key not in known)
    assert not missing


# ----- personalities keep what was typed --------------------------------------

def test_rejected_personality_keeps_the_typed_text(admin, app):
    browser = admin  # administrators may always write personalities
    assert browser.post("/personalities", {"name": "Teacher", "instructions": "Be patient.", "enabled": "1"}).status_code == 302
    long_text = "Answer like a pirate. " * 40
    response = browser.post("/personalities", {"name": "teacher", "instructions": long_text, "enabled": "1"})
    page = response.get_data(as_text=True)
    assert response.status_code == 400
    assert long_text.strip() in page
    assert 'value="teacher"' in page

    # Editing one personality into another's name keeps the edit on the editor page.
    assert browser.post("/personalities", {"name": "Poet", "instructions": "Rhyme.", "enabled": "1"}).status_code == 302
    with app.app_context():
        from bananachat.db import personalities, users

        poet = next(row for row in personalities.list_for(users.get_by_username("admin")["id"]) if row["name"] == "Poet")
    response = browser.post(f"/personalities/{poet['id']}", {"name": "Teacher", "instructions": "Rhyme, always."})
    page = response.get_data(as_text=True)
    assert response.status_code == 400
    assert "Rhyme, always." in page
    assert f'action="/personalities/{poet["id"]}"' in page


# ----- styles -------------------------------------------------------------------

def _css(name: str) -> str:
    return (STATIC / "css" / name).read_text()


def test_icons_have_a_size_everywhere():
    app_css = _css("app.css")
    # Menus (account menu on every page, music player) and alerts on every page, not only on account pages.
    assert re.search(r"\.menu-panel svg \{[^}]*width: 18px", app_css)
    assert re.search(r"\.alert > svg \{[^}]*width: 20px", app_css)
    assert re.search(r'svg\[viewBox="0 0 24 24"\] \{[^}]*width: 1\.25em', app_css)


def test_hidden_radio_and_file_inputs_show_keyboard_focus():
    app_css = _css("app.css")
    assert ".segmented label:has(input:focus-visible)" in app_css
    assert "label:has(+ input.visually-hidden:focus-visible)" in app_css


def _luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first, second):
    a, b = _luminance(first), _luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _hex(colour):
    return tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))


def _mix(first, second, share):
    return tuple(a * share + b * (1 - share) for a, b in zip(first, second, strict=True))


def test_secondary_text_meets_wcag_aa_on_the_default_palettes():
    from bananachat.db.settings import DARK_PALETTE, LIGHT_PALETTE

    css = _css("app.css")
    root = css.split(':root[data-theme="light"]')[0]
    light = css.split(':root[data-theme="light"]')[1].split("}")[0]

    def share(block, name):
        return int(re.search(rf"--{name}: color-mix\(in srgb, var\(--text\) (\d+)%, var\(--bg\)\)", block).group(1)) / 100

    for palette, block in ((DARK_PALETTE, root), (LIGHT_PALETTE, light)):
        text, bg = _hex(palette["text"]), _hex(palette["bg"])
        for token in ("text-muted", "text-faint"):
            colour = _mix(text, bg, share(block, token))
            for surface in ("bg", "sidebar", "secondary"):
                ratio = _contrast(colour, _hex(palette[surface]))
                assert ratio >= 4.5, f"{token} on {surface}: {ratio:.2f}"

    # Light-theme status colours on their tinted badges.
    for name in ("success", "warning", "danger", "info"):
        colour = _hex(re.search(rf"--{name}: (#[0-9a-f]{{6}})", light).group(1))
        tinted = _mix(colour, _hex(LIGHT_PALETTE["sidebar"]), 0.14)
        assert _contrast(colour, tinted) >= 4.5, name


# ----- documentation ----------------------------------------------------------

def test_configuration_reference_lists_every_setting():
    source = (ROOT / "bananachat" / "config.py").read_text()
    read = set(re.findall(r'r\.(?:text|flag|integer|number|choice)\("(BC_[A-Z_]+)"', source))
    read -= {"BC_DEBUG"}  # parsed for compatibility, has no effect
    documented = (ROOT / "docs" / "configuration.md").read_text()
    sample = (ROOT / ".env.sample").read_text()
    for name in sorted(read):
        parts = name.split("_")
        shortened = {"…_" + "_".join(parts[index:]) for index in range(2, len(parts))}
        assert name in documented or any(f"`{short}`" in documented for short in shortened), \
            f"{name} is missing from docs/configuration.md"
        assert f"# {name}=" in sample, f"{name} is missing from .env.sample"
