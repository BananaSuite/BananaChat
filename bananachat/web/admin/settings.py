"""Settings: name, sign-up, sign-in mode (BananaWiki Hosting), maintenance, warning banner, two-step
verification for administrators, default theme and palettes.

Changing the two-step requirement for administrators or the sign-in mode, or opening sign-up to
everyone, needs a recent confirmation (:mod:`._reauth`). Only an administrator whose own account is
connected to BananaWiki Hosting can switch to hosting-only sign-in."""

from __future__ import annotations

from flask import current_app, flash, render_template, request

from bananachat.db import hosting_links, users
from bananachat.db import settings as site_settings
from bananachat.security import admin_required
from bananachat.services import hosting_signin, two_step

from . import _reauth, bp
from ._helpers import FormError, audit, back, choice, flag, me, text

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
    config = current_app.config["BC"]
    return render_template("admin/settings.html", section="settings", s=settings, palettes=palettes,
                           admins_without_two_step=two_step.admins_without(),
                           own_two_step=two_step.enabled(me()["id"]),
                           hosting={"configured": hosting_signin.configured(),
                                    "mode": hosting_signin.stored_mode(settings),
                                    "effective": hosting_signin.effective_mode(settings),
                                    "url": config.hosting_oauth_url, "client_id": config.hosting_oauth_client_id,
                                    "callback": hosting_signin.callback_url(),
                                    "own_link": hosting_links.link_of(me()["id"])},
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
        # Cached forms from an older release do not carry this checkbox.
        # Only the current form's sentinel makes an unchecked value explicit.
        if request.form.get("worker_offline_warning_present") == "1":
            values["worker_offline_warning_enabled"] = 1 if flag("worker_offline_warning_enabled") else 0
        if request.form.get("easter_eggs_present") == "1":
            for egg in site_settings.EASTER_EGGS:
                values[f"easter_{egg}"] = 1 if flag(f"easter_{egg}") else 0
        if request.form.get("bm_mode_present") == "1":
            values["bm_mode_global"] = 1 if request.form.get("bm_mode_global") == "1" else 0
            values["bm_mode_self_service"] = 1 if flag("bm_mode_self_service") else 0
        if request.form.get("admin_two_step_present") == "1":
            values[two_step.REQUIRED_SETTING] = 1 if flag(two_step.REQUIRED_SETTING) else 0
            if values[two_step.REQUIRED_SETTING] and not two_step.enabled(me()["id"]):
                raise FormError("Turn on two-step verification for your own account first (Account → Two-step "
                                "verification).")
        # Only offered (and only kept) while the portal is configured; the mode stays as it was otherwise.
        if request.form.get("signin_mode_present") == "1" and hosting_signin.configured():
            values["signin_mode"] = choice("signin_mode", hosting_signin.MODES, label="Sign-in mode")
            if (values["signin_mode"] == "hosting" and hosting_signin.stored_mode() != "hosting"
                    and hosting_links.link_of(me()["id"]) is None):
                raise FormError("Connect your own account to BananaWiki Hosting first (Account → Security), "
                                "so you can still sign in once only BananaWiki Hosting accounts can.")
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
    requirement = values.get(two_step.REQUIRED_SETTING)
    mode = values.get("signin_mode")
    sensitive = (requirement is not None and requirement != before.get(two_step.REQUIRED_SETTING)) or (
        values["signup_mode"] == "open" and before.get("signup_mode") != "open") or (
        mode is not None and mode != hosting_signin.stored_mode(before))
    if sensitive and not _reauth.confirmed():
        return back("admin.settings")
    site_settings.update(**values)
    changed = {key: value for key, value in values.items() if before.get(key) != value}
    audit("settings_save", "site", changed)
    flash("Settings saved." if changed else "Nothing changed.", "success")
    return back("admin.settings")


@bp.post("/settings/bm-mode/all", endpoint="settings_bm_mode_all")
@admin_required
def bm_mode_all():
    """Turn BM mode on or off for every account at once (who may switch it keeps its own rule)."""
    on = request.form.get("state") == "on"
    count = users.set_bm_mode_all(on)
    audit("settings_bm_mode_all", "site", {"on": on, "changed": count})
    flash(f"BM mode is now {'on' if on else 'off'} for every account ({count} changed).", "success")
    return back("admin.settings", _anchor="bm-mode")


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
