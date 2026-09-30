"""Site settings: name, sign-up, maintenance, warning banner, default theme and palettes."""

from __future__ import annotations

from flask import flash, render_template, request

from bananachat.db import settings as site_settings
from bananachat.security import admin_required

from . import bp
from ._helpers import FormError, audit, back, choice, flag, text

SIGNUP_MODES = ("invite", "open", "disabled")
THEMES = ("dark", "light")
COLOR_LABELS = {"bg": "Background", "sidebar": "Panels and sidebar", "secondary": "Raised surfaces",
                "text": "Text", "primary": "Primary (buttons, links)", "accent": "Accent"}


def _palette_fields(mode: str) -> dict:
    prefix = "light_" if mode == "light" else ""
    return {key: f"{prefix}{key}_color" for key in site_settings.PALETTE_KEYS}


@bp.get("/settings", endpoint="settings")
@admin_required
def show():
    settings = site_settings.get()
    palettes = {mode: site_settings.palette(settings, mode) for mode in THEMES}
    return render_template("admin/settings.html", section="settings", s=settings, palettes=palettes,
                           defaults={"dark": site_settings.DARK_PALETTE, "light": site_settings.LIGHT_PALETTE},
                           color_labels=COLOR_LABELS, fields={mode: _palette_fields(mode) for mode in THEMES},
                           page_data={"defaults": {"dark": site_settings.DARK_PALETTE,
                                                   "light": site_settings.LIGHT_PALETTE}})


@bp.post("/settings", endpoint="settings_save")
@admin_required
def save():
    try:
        values = {
            "site_name": text("site_name", max_length=80, required=True, label="Site name"),
            "signup_mode": choice("signup_mode", SIGNUP_MODES, label="Sign-up"),
            "maintenance_mode": 1 if flag("maintenance_mode") else 0,
            "maintenance_message": text("maintenance_message", max_length=1000, label="Maintenance message"),
            "warning_banner_enabled": 1 if flag("warning_banner_enabled") else 0,
            "warning_banner_dismissible": 1 if flag("warning_banner_dismissible") else 0,
            "warning_banner_message": text("warning_banner_message", max_length=500, label="Banner message"),
            "default_theme_mode": choice("default_theme_mode", THEMES, label="Default theme"),
        }
        if values["warning_banner_enabled"] and not values["warning_banner_message"]:
            raise FormError("Write the banner message before turning the banner on.")
        for mode in THEMES:
            for key, column in _palette_fields(mode).items():
                value = (request.form.get(column) or "").strip()
                if not site_settings.HEX_COLOR.match(value):
                    label = f"{mode.capitalize()} theme {COLOR_LABELS[key].lower()}"
                    raise FormError(f"{label} must be a colour like #1a2b3c.")
                values[column] = value.lower()
    except FormError as error:
        flash(str(error), "error")
        return back("admin.settings")
    before = site_settings.get()
    site_settings.update(**values)
    changed = {key: value for key, value in values.items() if before.get(key) != value}
    audit("settings_save", "site", changed)
    flash("Settings saved." if changed else "Nothing changed.", "success")
    return back("admin.settings")


@bp.post("/settings/palette/reset", endpoint="settings_palette_reset")
@admin_required
def reset_palette():
    mode = request.form.get("mode")
    if mode not in THEMES:
        flash("Choose the dark or light palette.", "error")
        return back("admin.settings")
    defaults = site_settings.LIGHT_PALETTE if mode == "light" else site_settings.DARK_PALETTE
    site_settings.update(**{column: defaults[key] for key, column in _palette_fields(mode).items()})
    audit("settings_palette_reset", mode)
    flash(f"The {mode} palette is back to the default colours.", "success")
    return back("admin.settings", _anchor=f"palette-{mode}")
