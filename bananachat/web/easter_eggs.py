"""Easter eggs typed on the keyboard (BananaSuite ``banana-eggs/v1``, see :mod:`bananachat.eggs`).

Every page carries the enabled eggs in ``<script type="application/json" id="banana-eggs">`` in its
head (:func:`page_config`, from the layout), unless the site's ``easter_konami`` switch is off or the
person turned key-sequence eggs off in Appearance. ``static/banana-eggs/banana-eggs.js`` plays them
and reports a signed-in person's find to :func:`found`; the easter egg page lists the finds.
Finds are cosmetic and trivially forgeable: they unlock nothing.
"""

from __future__ import annotations

from flask import Blueprint, g, has_request_context, jsonify, render_template, url_for
from markupsafe import Markup

from bananachat import eggs, security
from bananachat.db import easter_eggs, users
from bananachat.i18n import translate

bp = Blueprint("easter_eggs", __name__)

FINDS_PER_MINUTE = 10


def _offered(settings: dict, prefs: dict) -> bool:
    """Whether key-sequence eggs play for this person: the site offers them and the person wants them."""
    return users.easter_eggs(prefs, settings)["konami"]


def page_config(settings: dict, user, prefs: dict, language: str) -> Markup | None:
    """The ``#banana-eggs`` JSON for this page, or None when no egg plays here."""
    if not has_request_context() or not _offered(settings, prefs):
        return None
    config = easter_eggs.config(settings)
    if not any(egg["enabled"] for egg in config["eggs"]):
        return None
    signed_in = user is not None
    found_url = url_for("easter_eggs.found", egg_id="{id}").replace("%7Bid%7D", "{id}")
    data = eggs.client_config(
        config, found_url=found_url, page_url=url_for("easter_eggs.index"), signed_in=signed_in,
        csrf_header="X-CSRF-Token" if signed_in else None, csrf_token=security.csrf_token() if signed_in else "",
        reduce_motion=bool(prefs.get("reduce_motion")),
        strings={"found": translate(language, "eggs.found"), "open_page": translate(language, "eggs.open_page")})
    return Markup(eggs.script_json(data))  # noqa: S704 - script_json escapes <, > and & for a <script> element


@bp.get("/easter-eggs", endpoint="index")
def index():
    """Every egg the site offers: found ones with the date, the others as "?"."""
    user = security.current_user()
    settings = g.settings
    prefs = users.get_preferences(user["id"]) if user is not None else dict(users.PREFERENCE_DEFAULTS)
    site_on = bool(settings.get("easter_konami", 1))
    offered = [egg for egg in easter_eggs.config(settings)["eggs"] if egg["enabled"]] if site_on else []
    found = easter_eggs.finds(user["id"]) if user is not None else {}
    items = [{"egg": egg, "found_at": found.get(egg["id"])} for egg in offered]
    return render_template("easter_eggs/index.html", items=items, site_on=site_on,
                           found_count=sum(1 for item in items if item["found_at"]),
                           personal_off=user is not None and site_on and not _offered(settings, prefs))


@bp.post("/api/easter-eggs/<egg_id>/found", endpoint="found")
@security.login_required
@security.rate_limit("easter-egg-found", FINDS_PER_MINUTE, 60, per_user=True)
def found(egg_id: str):
    """Record a find (idempotent): ``{"ok": true, "new": true}`` the first time, ``"new": false`` after."""
    user = security.current_user()
    prefs = users.get_preferences(user["id"])
    egg = easter_eggs.enabled(g.settings, egg_id) if _offered(g.settings, prefs) else None
    if egg is None:
        return security.json_error(translate(security.error_language(), "errors.not_found_text"), 404, "not_found")
    new = easter_eggs.record_find(user["id"], egg["id"])
    if new:
        users.audit(user, "easter_egg.found", egg["id"], None, security.client_ip())
    return jsonify({"ok": True, "new": new})
