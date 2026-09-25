"""Synchronized per-user UI customization routes."""

import os
import uuid

from flask import jsonify, request, send_from_directory, session
from PIL import Image, ImageOps

import config
import db
from helpers import get_current_user, login_required, rate_limit
from logger import log_action


_VALID_FONT_SCALES = {0.85, 0.9, 1.0, 1.1, 1.2, 1.35}
_VALID_LEVELS = {0, 1, 2}
_VALID_CONTRASTS = {0, 1, 2, 3, 4, 5}
_SEMANTIC_KEYS = (
    "semantic_bold", "semantic_italic", "semantic_code",
    "semantic_link", "semantic_heading",
)
_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _integer(value, default, allowed):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed in allowed else default


def _color(value):
    return db._clean_a11y_pref("custom_color", value)


def _remove_background(filename):
    if not db._clean_a11y_pref("background_image", filename):
        return
    path = os.path.abspath(os.path.join(config.BACKGROUND_UPLOAD_DIR, filename))
    root = os.path.abspath(config.BACKGROUND_UPLOAD_DIR)
    if os.path.commonpath([root, path]) == root:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _save_background(upload):
    filename = upload.filename or ""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in _ALLOWED_EXTENSIONS:
        return None, "Invalid image type. Use PNG, JPG, GIF, or WebP."

    upload.stream.seek(0, os.SEEK_END)
    upload_size = upload.stream.tell()
    upload.stream.seek(0)
    if upload_size > config.BACKGROUND_IMAGE_MAX_UPLOAD_SIZE:
        return None, "Background image is too large."

    try:
        probe = Image.open(upload.stream)
        probe.verify()
        upload.stream.seek(0)
        image = Image.open(upload.stream)
        width, height = image.size
        if width < 1 or height < 1 or width * height > config.BACKGROUND_IMAGE_MAX_PIXELS:
            return None, "Background image dimensions are too large."
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            canvas.alpha_composite(rgba)
            image = canvas.convert("RGB")
        else:
            image = image.convert("RGB")
        resampling = getattr(Image, "Resampling", Image)
        image.thumbnail(
            (config.BACKGROUND_IMAGE_MAX_DIMENSION, config.BACKGROUND_IMAGE_MAX_DIMENSION),
            resampling.LANCZOS,
        )
    except Exception:
        return None, "File is not a valid image."

    stored_name = f"{uuid.uuid4().hex}.jpg"
    os.makedirs(config.BACKGROUND_UPLOAD_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(config.BACKGROUND_UPLOAD_DIR, stored_name)
    try:
        image.save(path, format="JPEG", quality=82, optimize=True, progressive=True)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        return None, "Failed to save background image."
    return stored_name, None


def register_customization_routes(app):

    @app.route("/api/accessibility", methods=["GET"])
    @login_required
    def api_get_accessibility():
        user = get_current_user()
        return jsonify(db.get_user_accessibility(user["id"]))

    @app.route("/api/accessibility", methods=["POST"])
    @login_required
    @rate_limit(60, 60)
    def api_save_accessibility():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid request"}), 400

        user = get_current_user()
        current = db.get_user_accessibility(user["id"])
        try:
            font_scale = float(data.get("font_scale", current["font_scale"]))
        except (TypeError, ValueError):
            font_scale = 1.0
        if font_scale not in _VALID_FONT_SCALES:
            font_scale = min(_VALID_FONT_SCALES, key=lambda item: abs(item - font_scale))

        theme_mode = str(data.get("theme_mode", current["theme_mode"])).lower().strip()
        if theme_mode not in {"default", "dark", "light"}:
            theme_mode = "default"
        language = str(data.get("interface_language", current["interface_language"])).lower().strip()
        if language not in {"default", "it", "en"}:
            language = "default"

        prefs = {
            "theme_mode": theme_mode,
            "interface_language": language,
            "font_scale": font_scale,
            "contrast": _integer(data.get("contrast", current["contrast"]), 0, _VALID_CONTRASTS),
            "line_height": _integer(data.get("line_height", current["line_height"]), 0, _VALID_LEVELS),
            "letter_spacing": _integer(data.get("letter_spacing", current["letter_spacing"]), 0, _VALID_LEVELS),
            "reduce_motion": 1 if str(data.get("reduce_motion", current["reduce_motion"])) in {"1", "true", "True"} else 0,
            "background_image": current.get("background_image", ""),
        }
        try:
            prefs["sidebar_width"] = max(200, min(420, int(data.get("sidebar_width", current["sidebar_width"]))))
        except (TypeError, ValueError):
            prefs["sidebar_width"] = 260
        for key in (
            "custom_bg", "custom_text", "custom_primary", "custom_secondary",
            "custom_accent", "custom_sidebar",
        ):
            prefs[key] = _color(data.get(key, current.get(key, "")))
        for key in _SEMANTIC_KEYS:
            prefs[key] = _integer(data.get(key, current.get(key, 0)), 0, _VALID_LEVELS)

        db.save_user_accessibility(user["id"], prefs)
        if language in {"it", "en"}:
            session["language"] = language
        log_action("update_ui_customization", request, user=user)
        return jsonify({"ok": True, "preferences": prefs})

    @app.route("/api/accessibility/reset", methods=["POST"])
    @login_required
    @rate_limit(10, 60)
    def api_reset_accessibility():
        user = get_current_user()
        current = db.get_user_accessibility(user["id"])
        _remove_background(current.get("background_image", ""))
        defaults = dict(db._A11Y_DEFAULTS)
        db.save_user_accessibility(user["id"], defaults)
        log_action("reset_ui_customization", request, user=user)
        return jsonify({"ok": True, "defaults": defaults})

    @app.route("/api/accessibility/background", methods=["POST", "DELETE"])
    @login_required
    @rate_limit(10, 60)
    def api_accessibility_background():
        user = get_current_user()
        prefs = db.get_user_accessibility(user["id"])
        old_background = prefs.get("background_image", "")
        if request.method == "DELETE":
            prefs["background_image"] = ""
            db.save_user_accessibility(user["id"], prefs)
            _remove_background(old_background)
            return jsonify({"ok": True, "background_image": "", "url": ""})

        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "No file provided"}), 400
        stored_name, error = _save_background(upload)
        if error:
            status = 413 if "large" in error else 400
            return jsonify({"error": error}), status
        prefs["background_image"] = stored_name
        db.save_user_accessibility(user["id"], prefs)
        _remove_background(old_background)
        log_action("update_ui_background", request, user=user)
        return jsonify({
            "ok": True,
            "background_image": stored_name,
            "url": f"/customization/background/{stored_name}",
        })

    @app.route("/customization/background/<filename>")
    @login_required
    def customization_background(filename):
        user = get_current_user()
        prefs = db.get_user_accessibility(user["id"])
        if filename != prefs.get("background_image"):
            return jsonify({"error": "Not found"}), 404
        response = send_from_directory(config.BACKGROUND_UPLOAD_DIR, filename)
        response.cache_control.private = True
        response.cache_control.max_age = 86400
        return response
