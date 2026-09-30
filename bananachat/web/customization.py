"""Appearance and accessibility preferences, and the personal background image.

Preferences are stored per account (``users.accessibility``) and validated by
``users.clean_preferences`` on every read and write. The page previews every
change live and saves it through the JSON API; the endpoints of the previous
release (``/api/accessibility...``) stay available for cached pages.

Background images are decoded with Pillow, re-encoded as JPEG (first frame
of animations, EXIF orientation applied, at most
``config.background_max_dimension`` pixels per side) and stored as
``<uuid4 hex>.jpg`` in ``config.background_upload_dir``. Only the owner can
fetch their image. Replacing an image swaps the reference inside a
transaction and then deletes the previous file, so concurrent uploads never
leave a file behind that is still referenced or one that is not; files left by
a crash are removed by a periodic job.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, render_template, request, send_from_directory, session, url_for
from werkzeug.exceptions import NotFound

from bananachat import db, security
from bananachat.db import settings as site_settings
from bananachat.db import users
from bananachat.i18n import translate
from bananachat.services import background

bp = Blueprint("customization", __name__)
log = logging.getLogger("bananachat.customization")

BACKGROUND_RE = re.compile(r"^[0-9a-f]{32}\.jpe?g$")
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
# Multipart overhead allowed on top of the configured image size.
UPLOAD_OVERHEAD = 256 * 1024
ORPHAN_GRACE_SECONDS = 3600
JPEG_QUALITY = 85
# Transparent areas are flattened onto a dark neutral that suits both themes behind the overlay.
FLATTEN_COLOUR = (24, 24, 24)
# Keys a client may change through the API (the background has its own endpoints).
CLIENT_KEYS = frozenset(users.PREFERENCE_DEFAULTS) - {"background_image"}

# Colour presets: (bg, text, primary, secondary, accent, sidebar) for dark and light themes.
PRESETS = {
    "ocean": {"dark": ("#0b1a2e", "#c8ddf0", "#5b9bd5", "#112640", "#a3c4f3", "#091526"),
              "light": ("#f3f8fd", "#17324a", "#256fa8", "#ffffff", "#4f90c7", "#dbeaf7")},
    "forest": {"dark": ("#0f1e12", "#c8dcc8", "#4caf50", "#162a19", "#8fbf9f", "#0b180e"),
               "light": ("#f2f8f1", "#1f3a24", "#2f7d34", "#ffffff", "#5c9a68", "#dcebdd")},
    "sunset": {"dark": ("#1f1017", "#f0ddd0", "#e76f51", "#2a1520", "#f4a261", "#1a0d14"),
               "light": ("#fff4ed", "#513026", "#b8472d", "#ffffff", "#de7d3c", "#f5dfd4")},
    "lavender": {"dark": ("#1a1428", "#d8d0e8", "#9b7fd4", "#221a34", "#c4b5e0", "#151020"),
                 "light": ("#f7f3fc", "#32274a", "#6c4fae", "#ffffff", "#9a7acb", "#e7def4")},
    "midnight": {"dark": ("#0a0a12", "#e0e0e8", "#00d4ff", "#10101c", "#7ee8ff", "#08080e"),
                 "light": ("#f2f6fb", "#202535", "#136f91", "#ffffff", "#2ca7c9", "#dce5ef")},
    "copper": {"dark": ("#1c1410", "#e8dcd0", "#c98545", "#241c16", "#d4a574", "#16100c"),
               "light": ("#fbf3ed", "#453028", "#8f5625", "#ffffff", "#bd814c", "#ecded4")},
    "rose": {"dark": ("#1a0e14", "#f0d8e0", "#f06aa8", "#240a18", "#f48fb1", "#140a10"),
             "light": ("#fff1f6", "#4c2636", "#b02a6d", "#ffffff", "#df6796", "#f4d9e4")},
    "slate": {"dark": ("#0e1220", "#d0d8f0", "#8c9fd4", "#141826", "#a8b8e0", "#0a0e1a"),
              "light": ("#f4f6fb", "#253044", "#52638e", "#ffffff", "#7688b4", "#e0e5f0")},
}
COLOR_ORDER = ("custom_bg", "custom_text", "custom_primary", "custom_secondary", "custom_accent", "custom_sidebar")


class BackgroundRejected(ValueError):
    def __init__(self, key: str, status: int = 400, **params):
        super().__init__(key)
        self.key, self.status, self.params = key, status, params


def _t(key, **params):
    return translate(g.lang, key, **params)


def _config():
    return current_app.config["BC"]


# ----- background files -----------------------------------------------------

def background_path(filename: str) -> Path | None:
    """The stored file for a background name, or None for anything that is not one of ours."""
    if not isinstance(filename, str) or not BACKGROUND_RE.match(filename):
        return None
    return _config().background_upload_dir / filename


def remove_background_file(filename: str) -> None:
    """Delete a stored background image (ignored for invalid or missing names)."""
    path = background_path(filename)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("Could not delete background image %s", filename, exc_info=True)


def process_background(data: bytes, config) -> bytes:
    """Validate an uploaded image and return it re-encoded as JPEG bytes."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise BackgroundRejected("customize.background_invalid")
    if len(data) > config.background_max_bytes:
        raise BackgroundRejected("customize.background_too_large", 413,
                                 size=config.background_max_bytes // (1024 * 1024))
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in ACCEPTED_FORMATS:
            raise BackgroundRejected("customize.background_invalid")
        width, height = image.size
        if width < 1 or height < 1 or width * height > config.background_max_pixels:
            raise BackgroundRejected("customize.background_too_many_pixels", 413,
                                     megapixels=round(config.background_max_pixels / 1_000_000, 1))
        image.seek(0)  # the first frame of an animation
        image.load()
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, (*FLATTEN_COLOUR, 255))
            canvas.alpha_composite(rgba)
            image = canvas.convert("RGB")
        else:
            image = image.convert("RGB")
        limit = config.background_max_dimension
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    except BackgroundRejected:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, EOFError):
        raise BackgroundRejected("customize.background_invalid") from None
    return output.getvalue()


def _write_background(encoded: bytes, config) -> str:
    directory = config.background_upload_dir
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.jpg"
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".upload-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, directory / filename)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return filename


def replace_background(user_id: str, upload_data: bytes) -> str:
    """Store a new background for *user_id* and delete the previous one. Returns the new file name."""
    config = _config()
    encoded = process_background(upload_data, config)
    filename = _write_background(encoded, config)
    try:
        with db.transaction():
            prefs = users.get_preferences(user_id)
            previous = prefs["background_image"]
            prefs["background_image"] = filename
            users.save_preferences(user_id, prefs)
    except BaseException:
        remove_background_file(filename)
        raise
    if previous and previous != filename:
        remove_background_file(previous)
    return filename


def clear_background(user_id: str) -> None:
    with db.transaction():
        prefs = users.get_preferences(user_id)
        previous = prefs["background_image"]
        prefs["background_image"] = ""
        users.save_preferences(user_id, prefs)
    remove_background_file(previous)


@background.job("customization-orphan-backgrounds", every=6 * 3600, initial_delay=120)
def purge_orphan_backgrounds(app) -> int:
    """Delete background files no account references (left by crashes or old releases)."""
    directory = app.config["BC"].background_upload_dir
    if not directory.is_dir():
        return 0
    referenced = set()
    for row in db.query("SELECT accessibility FROM users WHERE accessibility LIKE '%background_image%'"):
        try:
            name = json.loads(row["accessibility"]).get("background_image")
        except (ValueError, AttributeError):
            continue
        if isinstance(name, str):
            referenced.add(name)
    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    removed = 0
    for path in directory.iterdir():
        name = path.name
        ours = BACKGROUND_RE.match(name) or (name.startswith(".upload-") and name.endswith(".tmp"))
        if not ours or name in referenced:
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("Removed %d unreferenced background image(s)", removed)
    return removed


# ----- page -----------------------------------------------------------------

def _background_url(prefs: dict) -> str | None:
    name = prefs.get("background_image")
    return url_for("customization.background", filename=name) if name else None


@bp.get("/customize", endpoint="index")
@bp.get("/account/customize", endpoint="index_alias")
@security.login_required
def index():
    user = security.current_user()
    prefs = users.get_preferences(user["id"])
    settings = g.settings
    default_mode = settings.get("default_theme_mode") if settings.get("default_theme_mode") in ("dark", "light") \
        else "dark"
    config = _config()
    data = {
        "preferences": prefs,
        "defaults": users.PREFERENCE_DEFAULTS,
        "site_palettes": {"dark": site_settings.palette(settings, "dark"),
                          "light": site_settings.palette(settings, "light")},
        "site_theme": default_mode,
        "presets": {name: {mode: dict(zip(COLOR_ORDER, colours, strict=True)) for mode, colours in variants.items()}
                    for name, variants in PRESETS.items()},
        "font_scales": users.FONT_SCALES,
        "background_url": _background_url(prefs),
        "background_max_bytes": config.background_max_bytes,
        "urls": {
            "save": url_for("customization.save_preferences"),
            "reset": url_for("customization.reset_preferences"),
            "background": url_for("customization.upload_background"),
        },
    }
    return render_template("customization/index.html", prefs=prefs, data=data, presets=PRESETS,
                           color_order=COLOR_ORDER, font_scales=users.FONT_SCALES,
                           background_max_mb=config.background_max_bytes // (1024 * 1024),
                           background_url=_background_url(prefs))


# ----- JSON API -------------------------------------------------------------

def _payload(prefs: dict, **extra):
    return jsonify({"ok": True, "preferences": prefs, "background_url": _background_url(prefs), **extra})


@bp.get("/api/preferences", endpoint="get_preferences")
@security.login_required
def get_preferences():
    return _payload(users.get_preferences(security.current_user()["id"]))


@bp.post("/api/preferences", endpoint="save_preferences")
@bp.post("/api/accessibility", endpoint="legacy_save")
@security.login_required
@security.rate_limit("preferences", 120, 60, per_user=True)
def save_preferences():
    changes = request.get_json(silent=True)
    if not isinstance(changes, dict):
        return security.json_error(_t("customize.invalid_request"), 400)
    user = security.current_user()
    with db.transaction():
        current = users.get_preferences(user["id"])
        merged = {**current, **{key: value for key, value in changes.items() if key in CLIENT_KEYS}}
        saved = users.save_preferences(user["id"], merged)
    language_changed = saved["interface_language"] != current["interface_language"]
    if saved["interface_language"] in ("it", "en"):
        session["language"] = saved["interface_language"]
    elif language_changed:
        # Back to "default": forget the choice copied into the session, so the browser's language applies.
        session.pop("language", None)
    return _payload(saved, reload=language_changed)


@bp.get("/api/accessibility", endpoint="legacy_get")
@security.login_required
def legacy_get():
    """The previous release returned the bare preferences object."""
    return jsonify(users.get_preferences(security.current_user()["id"]))


@bp.post("/api/preferences/reset", endpoint="reset_preferences")
@bp.post("/api/accessibility/reset", endpoint="legacy_reset")
@security.login_required
@security.rate_limit("preferences-reset", 20, 60, per_user=True)
def reset_preferences():
    user = security.current_user()
    with db.transaction():
        previous = users.get_preferences(user["id"])
        defaults = users.save_preferences(user["id"], dict(users.PREFERENCE_DEFAULTS))
    remove_background_file(previous["background_image"])
    if previous["interface_language"] != "default":
        session.pop("language", None)
    return _payload(defaults, defaults=defaults, reload=previous["interface_language"] != "default")


@bp.route("/api/preferences/background", methods=["POST", "DELETE"], endpoint="upload_background")
@bp.route("/api/accessibility/background", methods=["POST", "DELETE"], endpoint="legacy_background")
# The configured image size plus multipart overhead, applied before anything
# (the CSRF check included) reads the body.
@security.body_limit(lambda config: config.background_max_bytes + UPLOAD_OVERHEAD)
@security.login_required
@security.rate_limit("preferences-background", 12, 300, per_user=True)
def upload_background():
    user = security.current_user()
    if request.method == "DELETE":
        clear_background(user["id"])
        return _payload(users.get_preferences(user["id"]), url="")
    upload = request.files.get("file") or request.files.get("image")
    if upload is None or not upload.filename:
        return security.json_error(_t("customize.background_missing"), 400)
    limit = _config().background_max_bytes
    data = upload.stream.read(limit + 1)
    try:
        filename = replace_background(user["id"], data)
    except BackgroundRejected as problem:
        code = "too_large" if problem.status == 413 else "invalid_image"
        return security.json_error(_t(problem.key, **problem.params), problem.status, code)
    prefs = users.get_preferences(user["id"])
    return _payload(prefs, background_image=filename, url=_background_url(prefs))


# ----- serving --------------------------------------------------------------

@bp.get("/customization/background/<filename>", endpoint="background")
@security.login_required
def serve_background(filename):
    user = security.current_user()
    if not BACKGROUND_RE.match(filename) or users.get_preferences(user["id"])["background_image"] != filename:
        raise NotFound()
    response = send_from_directory(_config().background_upload_dir, filename, mimetype="image/jpeg",
                                   conditional=True, max_age=86400)
    # File names are unique per upload, so the image never changes; only this account may see it.
    response.cache_control.private = True
    response.cache_control.public = False
    return response
