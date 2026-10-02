"""Customize page: preferences API, legacy endpoints and the background image."""

from __future__ import annotations

import io
import os
import time

import pytest

from tests.app.conftest import TEST_CSRF, Browser


def _signed_in(app, username):
    browser = Browser(app)
    browser.login(username)
    with browser.client.session_transaction() as session:
        session["csrf"] = TEST_CSRF
    return browser


def _image(fmt="PNG", size=(64, 48), mode="RGBA", frames=1):
    from PIL import Image

    output = io.BytesIO()
    image = Image.new(mode, size, (200, 40, 40, 128) if mode == "RGBA" else (200, 40, 40))
    if frames > 1:
        others = [Image.new("RGB", size, (0, 0, 255)) for _ in range(frames - 1)]
        image.save(output, format=fmt, save_all=True, append_images=others)
    else:
        image.save(output, format=fmt)
    return output.getvalue()


def _upload(browser, data, url="/api/preferences/background", name="picture.png"):
    return browser.client.post(url, data={"file": (io.BytesIO(data), name)},
                               headers={"X-CSRF-Token": TEST_CSRF, "X-Requested-With": "fetch",
                                        "Accept": "application/json"})


def test_page_renders_with_current_preferences(app, make_user):
    make_user("pia")
    browser = _signed_in(app, "pia")
    response = browser.get("/customize")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="page-data"' in html and 'name="theme_mode"' in html
    assert browser.get("/account/customize").status_code == 200
    assert Browser(app).get("/customize").status_code == 302


def test_save_merges_validates_and_applies(app, make_user):
    from bananachat.db import users

    user = make_user("quinn")
    browser = _signed_in(app, "quinn")
    response = browser.post_json("/api/preferences", {"theme_mode": "light", "contrast": 3, "font_scale": 1.18,
                                                      "custom_primary": "#AABBCC", "semantic_code": 2,
                                                      "custom_bg": "red", "sidebar_width": 9999,
                                                      "background_image": "0" * 32 + ".jpg", "unknown": 1})
    assert response.status_code == 200
    saved = response.get_json()["preferences"]
    assert saved["theme_mode"] == "light" and saved["contrast"] == 3 and saved["font_scale"] == 1.2
    assert saved["custom_primary"] == "#aabbcc" and saved["custom_bg"] == ""
    assert saved["sidebar_width"] == users.PREFERENCE_DEFAULTS["sidebar_width"]
    assert saved["background_image"] == "" and "unknown" not in saved
    # A partial update keeps the other values.
    browser.post_json("/api/preferences", {"reduce_motion": True})
    with app.app_context():
        prefs = users.get_preferences(user["id"])
    assert prefs["reduce_motion"] is True and prefs["contrast"] == 3
    html = browser.get("/account").get_data(as_text=True)
    assert 'data-theme="light"' in html and "contrast-3" in html and "sem-code-2" in html and "reduce-motion" in html
    assert "--palette-primary: #aabbcc" in html
    assert browser.fetch("/api/preferences").get_json()["preferences"]["contrast"] == 3
    assert browser.post_json("/api/preferences", ["not", "an", "object"]).status_code == 400


def test_language_preference_switches_the_interface(app, make_user):
    make_user("rhea")
    browser = _signed_in(app, "rhea")
    response = browser.post_json("/api/preferences", {"interface_language": "en"})
    assert response.get_json()["reload"] is True
    assert 'lang="en"' in browser.get("/customize").get_data(as_text=True)


@pytest.mark.parametrize(("theme", "primary", "foreground"), [
    ("light", "#e6be32", "#000000"),
    ("dark", "#112244", "#ffffff"),
    ("light", "#ffffff", "#000000"),
    ("dark", "#000000", "#ffffff"),
])
def test_primary_button_text_follows_custom_color(app, make_user, theme, primary, foreground):
    make_user("palette-user")
    browser = _signed_in(app, "palette-user")
    assert browser.post_json("/api/preferences", {"theme_mode": theme, "custom_primary": primary}).status_code == 200
    html = browser.get("/account").get_data(as_text=True)
    assert f"--palette-primary: {primary};" in html
    assert f"--palette-on-primary: {foreground};" in html


def test_legacy_endpoints_still_work(app, make_user):
    make_user("sam")
    browser = _signed_in(app, "sam")
    assert browser.post_json("/api/accessibility", {"contrast": 2}).get_json()["ok"] is True
    assert browser.fetch("/api/accessibility").get_json()["contrast"] == 2
    reset = browser.post_json("/api/accessibility/reset", {})
    assert reset.get_json()["defaults"]["contrast"] == 0
    assert _upload(browser, _image(), url="/api/accessibility/background").status_code == 200


def test_background_is_reencoded_private_and_replaced(app, make_user):
    from PIL import Image

    from bananachat.db import users

    alice = make_user("tara")
    make_user("uma")
    browser = _signed_in(app, "tara")
    other = _signed_in(app, "uma")
    directory = app.config["BC"].background_upload_dir

    response = _upload(browser, _image())
    assert response.status_code == 200, response.get_data(as_text=True)
    first = response.get_json()["preferences"]["background_image"]
    assert first.endswith(".jpg") and len(first) == 36
    stored = directory / first
    assert Image.open(stored).format == "JPEG"
    assert oct(stored.stat().st_mode & 0o777) == "0o600"

    image = browser.get(f"/customization/background/{first}")
    assert image.status_code == 200 and image.mimetype == "image/jpeg"
    assert "private" in image.headers["Cache-Control"]
    assert other.get(f"/customization/background/{first}").status_code == 404
    assert Browser(app).get(f"/customization/background/{first}").status_code == 302
    assert browser.get("/customization/background/..%2f..%2fbananachat.db").status_code == 404
    assert f"/customization/background/{first}" in browser.get("/account").get_data(as_text=True)

    # An animated GIF: the first frame is kept, the previous file is deleted.
    second = _upload(browser, _image("GIF", mode="RGB", frames=3), name="anim.gif").get_json()
    second_name = second["preferences"]["background_image"]
    assert second_name != first and not stored.exists() and (directory / second_name).exists()

    # Removing it clears the preference and the file.
    assert browser.fetch("/api/preferences/background", method="DELETE").status_code == 200
    assert not (directory / second_name).exists()
    with app.app_context():
        assert users.get_preferences(alice["id"])["background_image"] == ""


def test_background_is_downscaled(app, make_user):
    from PIL import Image

    make_user("vera")
    browser = _signed_in(app, "vera")
    limit = app.config["BC"].background_max_dimension
    name = _upload(browser, _image("JPEG", size=(limit + 500, 400), mode="RGB"), name="wide.jpg") \
        .get_json()["preferences"]["background_image"]
    width, height = Image.open(app.config["BC"].background_upload_dir / name).size
    assert width == limit and height < 400


def test_invalid_backgrounds_are_rejected(app, make_user):
    make_user("wes")
    browser = _signed_in(app, "wes")
    assert _upload(browser, b"not an image at all").status_code == 400
    assert _upload(browser, b"<svg xmlns='http://www.w3.org/2000/svg'/>", name="x.svg").status_code == 400
    assert _upload(browser, _image("BMP", mode="RGB"), name="x.bmp").status_code == 400
    too_big = os.urandom(app.config["BC"].background_max_bytes + 10)
    assert _upload(browser, too_big).status_code == 413
    directory = app.config["BC"].background_upload_dir
    assert not directory.exists() or not [p for p in directory.iterdir() if p.suffix == ".jpg"]


def test_too_many_pixels_are_rejected(make_app, make_user):
    app = make_app(BACKGROUND_IMAGE_MAX_PIXELS="100000")
    from bananachat import security
    from bananachat.db import users

    with app.test_request_context():
        users.create("xena", security.hash_password("xena-password"))
    browser = _signed_in(app, "xena")
    response = _upload(browser, _image("PNG", size=(400, 400), mode="RGB"))
    assert response.status_code == 413


def test_reset_restores_defaults_and_deletes_the_background(app, make_user):
    from bananachat.db import users

    user = make_user("yuri")
    browser = _signed_in(app, "yuri")
    name = _upload(browser, _image()).get_json()["preferences"]["background_image"]
    browser.post_json("/api/preferences", {"contrast": 4})
    assert browser.post_json("/api/preferences/reset", {}).status_code == 200
    with app.app_context():
        assert users.get_preferences(user["id"]) == users.PREFERENCE_DEFAULTS
    assert not (app.config["BC"].background_upload_dir / name).exists()


def test_orphaned_backgrounds_are_purged(app, make_user):
    from bananachat.web.customization import purge_orphan_backgrounds

    make_user("zoe")
    browser = _signed_in(app, "zoe")
    kept = _upload(browser, _image()).get_json()["preferences"]["background_image"]
    directory = app.config["BC"].background_upload_dir
    orphan = directory / ("f" * 32 + ".jpg")
    orphan.write_bytes(b"x")
    fresh = directory / ("e" * 32 + ".jpg")
    fresh.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    os.utime(directory / kept, (old, old))
    with app.app_context():
        assert purge_orphan_backgrounds(app) == 1
    assert not orphan.exists() and fresh.exists() and (directory / kept).exists()
