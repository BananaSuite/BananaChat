"""BananaChat's icons are BananaWiki's yellow banana, the mascot of the wiki drawn as pixel art.

BananaWiki's scripts/make_icons.py draws them (python scripts/make_icons.py in
the BananaWiki tree writes BananaChat's copies when this tree sits next to it).
The favicon is 32x32, twice the 16x16 sprite; the home-screen icon is the
banana on an opaque light square, which iOS rounds itself. The comparisons
with BananaWiki's files are skipped when BananaWiki is not next to this tree
(or at BANANAWIKI_SRC).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
IMG = ROOT / "bananachat" / "static" / "img"
WIKI = Path(os.environ.get("BANANAWIKI_SRC") or ROOT.parent / "BananaWiki")
WIKI_ICONS = {"favicon.png": "banana_yellow.png", "apple-touch-icon.png": "apple-touch-icon.png"}


def _blocks_are_whole(image: Image.Image, scale: int) -> bool:
    image = image.convert("RGBA")
    return all(len(image.crop((x, y, x + scale, y + scale)).getcolors(scale * scale)) == 1
               for x in range(0, image.width, scale) for y in range(0, image.height, scale))


def test_the_favicon_is_a_32_pixel_banana_of_whole_sprite_pixels():
    image = Image.open(IMG / "favicon.png")
    assert (image.format, image.size, image.mode) == ("PNG", (32, 32), "RGBA")
    assert _blocks_are_whole(image, 2)
    assert image.getpixel((0, 0))[3] == 0, "transparent around the banana"


def test_the_home_screen_icon_is_opaque_and_180_pixels():
    image = Image.open(IMG / "apple-touch-icon.png").convert("RGBA")
    assert image.size == (180, 180)
    assert {alpha for _count, (*_colour, alpha) in image.getcolors(180 * 180)} == {255}


@pytest.mark.parametrize("name", sorted(WIKI_ICONS))
def test_the_icons_are_bananawiki_s(name):
    source = WIKI / "bananawiki" / "wiki" / "static" / "favicons" / WIKI_ICONS[name]
    if not source.is_file():
        pytest.skip(f"{source} is not next to BananaChat")
    assert (IMG / name).read_bytes() == source.read_bytes(), "run python scripts/make_icons.py in BananaWiki"


def test_pages_link_the_favicon_and_the_home_screen_icon(client):
    html = client.get("/login").get_data(as_text=True)
    head = html.split("<body")[0]
    icon = re.search(r'<link rel="icon" href="([^"]+/img/favicon\.png)">', head)
    touch = re.search(r'<link rel="apple-touch-icon" href="([^"]+/img/apple-touch-icon\.png)">', head)
    assert icon and touch
    for url, name in ((icon.group(1), "favicon.png"), (touch.group(1), "apple-touch-icon.png")):
        response = client.get(url)
        assert (response.status_code, response.mimetype) == (200, "image/png")
        assert response.data == (IMG / name).read_bytes()


def test_logos_are_twice_the_sprite_and_scaled_without_smoothing():
    templates = ROOT / "bananachat" / "templates"
    logos = [(path.name, tag) for path in sorted(templates.rglob("*.html"))
             for tag in re.findall(r"<img[^>]*img/favicon\.png[^>]*>", path.read_text(encoding="utf-8"))]
    assert {name for name, _tag in logos} == {"topbar.html", "_layout.html"}
    assert [tag for _name, tag in logos if 'width="32" height="32"' not in tag] == []
    css = (ROOT / "bananachat" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.brand img \{ width: 32px; height: 32px; image-rendering: pixelated; \}", css)
    assert not re.search(r"\.auth-card \.brand img \{", css)
