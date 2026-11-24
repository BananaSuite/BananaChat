"""Settings: name, sign-up, sign-in mode (BananaWiki Hosting), maintenance, warning banner, two-step
verification for administrators, default theme and palettes, and the easter eggs (the shared BananaSuite
editor, ``static/banana-eggs/banana-eggs-admin.js``; :mod:`bananachat.eggs` checks what it sends).

Changing the two-step requirement for administrators or the sign-in mode, or opening sign-up to
everyone, needs a recent confirmation (:mod:`._reauth`). Only an administrator whose own account is
connected to BananaWiki Hosting can switch to hosting-only sign-in."""

from __future__ import annotations

from flask import current_app, flash, render_template, request
from markupsafe import Markup

from bananachat import eggs
from bananachat.db import easter_eggs, hosting_links, users
from bananachat.db import settings as site_settings
from bananachat.security import admin_required
from bananachat.services import hosting_signin, two_step

from . import _reauth, bp
from ._helpers import FormError, audit, back, choice, flag, me, text

SIGNUP_MODES = ("invite", "open", "disabled")
THEMES = ("dark", "light")
COLOR_LABELS = {"bg": "Background", "sidebar": "Panels and sidebar", "secondary": "Raised surfaces",
                "text": "Text", "primary": "Primary (buttons, links)", "accent": "Accent"}


# The egg editor's labels (SPEC.md of banana-eggs/v1); the administration is English-only.
EGG_EDITOR_STRINGS = {
    "title": "Easter egg {n}", "add": "Add an easter egg", "remove": "Remove", "preview": "Preview",
    "reset": "Back to the default egg", "record": "Record keys", "stop": "Stop ({s})", "konami": "Konami code",
    "keys": "Key sequence", "keys_hint": "Press Record, type 2 to 32 keys, then Stop (Escape also stops).",
    "keys_empty": "No keys yet", "key_space": "Space", "id": "Name (a-z, 0-9, -)", "enabled": "On",
    "sprite": "What falls", "sprite_emoji": "Emoji", "sprite_mascot": "The mascot", "emoji": "Emoji",
    "emoji_pick": "Pick an emoji", "count": "How many", "speed_min": "Fastest fall (s)",
    "speed_max": "Slowest fall (s)", "delay": "Between drops (ms)", "unlock": "Afterwards",
    "unlock_page": "Open the easter egg page", "unlock_toast": "Show a message", "unlock_none": "Nothing else",
    "unlock_delay": "Wait (ms)", "record_find": "Remember who found it", "badge": "Badge", "badge_none": "No badge",
    "message": "Message (optional)", "limit": "Up to {n} easter eggs.",
}
EGG_FIELDS = {
    "egg": "Easter egg", "id": "Name", "keys": "Key sequence", "sprite": "What falls", "sprite.value": "Emoji",
    "count": "How many", "speed_min": "Fastest fall", "speed_max": "Slowest fall", "delay": "Between drops",
    "unlock": "Afterwards", "unlock.action": "Afterwards", "unlock.delay": "Wait", "enabled": "On",
    "record": "Remember who found it", "badge": "Badge", "message": "Message",
}


def _egg_error(index: int | None, field: str, code: str) -> str:
    """One problem :func:`eggs.clean` found, as a sentence for the administrator."""
    if index is None:
        return {"too_many": f"Keep at most {eggs.MAX_EGGS} easter eggs.",
                "size": f"The easter egg list is too long (at most {eggs.MAX_BYTES // 1024} KB).",
                }.get(code, "The easter egg list could not be read. Reload the page and try again.")
    label = EGG_FIELDS.get(field, field)
    if code == "range":
        name = field.removeprefix("unlock.")
        low, high = eggs.UNLOCK_DELAY[:2] if field == "unlock.delay" else eggs.NUMBERS[name][:2]
        problem = f"must be between {low:g} and {high:g}"
    elif field == "keys" and code == "length":
        problem = f"needs {eggs.MIN_KEYS} to {eggs.MAX_KEYS} keys"
    elif field == "id":
        problem = "is used by another easter egg" if code == "duplicate" else \
            "must be 1 to 32 lower-case letters, digits or hyphens"
    elif field == "sprite.value":
        problem = "must be one emoji or a few characters"
    elif field == "message":
        problem = f"can be at most {eggs.MAX_MESSAGE} characters on one line"
    else:
        problem = "is not valid"
    return f"Easter egg {index + 1}, {label.lower()}: {problem}."


def _egg_editor(settings: dict, draft: str | None = None, errors: list | None = None) -> dict:
    # "egg" is what "Add" starts from (its keys are cleared), "reset" what "Back to the default egg" restores.
    # BananaChat has no badges.
    options = {"strings": EGG_EDITOR_STRINGS, "emojis": eggs.EMOJIS, "badges": [], "mascot": True,
               "egg": eggs.default_egg(), "reset": eggs.default_config()["eggs"], "buttonClass": "btn btn-sm"}
    return {"value": draft if draft is not None else easter_eggs.stored_text(settings),
            "options": Markup(eggs.script_json(options)),  # noqa: S704 - escaped for a <script> element
            "errors": [_egg_error(*error) for error in errors or []]}


def _palette_fields(mode: str) -> dict:
    prefix = "light_" if mode == "light" else ""
    return {key: f"{prefix}{key}_color" for key in site_settings.PALETTE_KEYS}


@bp.get("/settings", endpoint="settings")
@admin_required
def show():
    return _page(site_settings.get())


def _page(settings: dict, *, egg_draft: str | None = None, egg_errors: list | None = None, status: int = 200):
    palettes = {mode: site_settings.palette(settings, mode) for mode in THEMES}
    config = current_app.config["BC"]
    return render_template("admin/settings.html", section="settings", s=settings, palettes=palettes,
                           egg_editor=_egg_editor(settings, egg_draft, egg_errors),
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
                                                   "light": site_settings.LIGHT_PALETTE}}), status


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
    # The egg editor posts the whole list as JSON. Problems are shown next to the editor, with the
    # administrator's edits (of the whole form) kept for another try.
    egg_list = request.form.get("easter_egg_configs")
    if egg_list is not None:
        try:
            cleaned = eggs.dumps(eggs.clean(egg_list, strict=True))
        except eggs.EggError as error:
            flash("Nothing was saved. Correct the keyboard easter eggs under Appearance.", "error")
            return _page({**before, **values}, egg_draft=egg_list, egg_errors=error.errors, status=400)
        if cleaned != easter_eggs.stored_text(before):
            values["easter_egg_configs"] = cleaned
    requirement = values.get(two_step.REQUIRED_SETTING)
    mode = values.get("signin_mode")
    sensitive = (requirement is not None and requirement != before.get(two_step.REQUIRED_SETTING)) or (
        values["signup_mode"] == "open" and before.get("signup_mode") != "open") or (
        mode is not None and mode != hosting_signin.stored_mode(before))
    if sensitive and not _reauth.confirmed():
        return back("admin.settings")
    site_settings.update(**values)
    changed = {key: value for key, value in values.items() if before.get(key) != value}
    if "easter_egg_configs" in changed:  # the egg ids, not the whole list (audit details are short)
        changed["easter_egg_configs"] = [egg["id"] + ("" if egg["enabled"] else " (off)")
                                         for egg in eggs.clean(changed["easter_egg_configs"])["eggs"]]
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
