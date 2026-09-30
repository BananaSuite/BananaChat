"""Personalities: moderation of every kind, featured personalities, and creating one for a user.

* Moderation: disable (optionally until a date, with a reason shown to the
  owner), re-enable, delete, and revoke a share link. It works the same for
  users' personalities and featured ones.
* Featured personalities are written and published here and visible to every
  user who may use personalities (read-only, with "Duplicate to my
  personalities"). None exist until an administrator creates one; drafts stay
  hidden until published.
"""

from __future__ import annotations

from flask import abort, flash, render_template, request

from bananachat.db import personalities as personalities_db
from bananachat.db import users
from bananachat.security import admin_required
from bananachat.services import personalities as service
from bananachat.services.access import AccessContext, usable_models

from . import bp
from ._helpers import FormError, audit, back, flag, me, page, text, utc_datetime, utc_input_min

PAGE_SIZE = 40
FILTERS = ("", "user", "featured", "shared")


def _personality_or_404(personality_id: int):
    row = personalities_db.get(personality_id)
    if row is None:
        abort(404)
    return row


def _featured_or_404(personality_id: int):
    row = _personality_or_404(personality_id)
    if row["kind"] != "featured":
        abort(404)
    return row


def _label(row) -> str:
    return f"featured/{row['name']}" if row["kind"] == "featured" else f"{row['username']}/{row['name']}"


def _who(row) -> str:
    return f"Featured personality “{row['name']}”" if row["kind"] == "featured" else \
        f"“{row['name']}” of {row['username']}"


def _back():
    kind = request.form.get("kind") or ""
    return back("admin.personalities", q=request.form.get("q") or None, page=request.form.get("page", type=int),
                kind=kind if kind in FILTERS and kind else None)


@bp.get("/personalities", endpoint="personalities")
@admin_required
def list_personalities():
    search = (request.args.get("q") or "").strip()[:64]
    kind = request.args.get("kind") or ""
    kind = kind if kind in FILTERS else ""
    total, _rows = personalities_db.admin_page(1, 0, search=search, kind=kind)
    current = page(total, PAGE_SIZE)
    _total, rows = personalities_db.admin_page(PAGE_SIZE, current.offset, search=search, kind=kind)
    return render_template("admin/personalities.html", section="personalities", rows=rows, search=search, kind=kind,
                           page=current, is_active=personalities_db.is_active, is_blocked=personalities_db.is_blocked,
                           min_datetime=utc_input_min(), featured_count=personalities_db.count_featured(),
                           max_featured=personalities_db.MAX_FEATURED, max_name=personalities_db.MAX_NAME,
                           max_instructions=personalities_db.MAX_INSTRUCTIONS)


# ----- moderation (every kind) -----------------------------------------------------------

@bp.post("/personalities/<int:personality_id>/disable", endpoint="personality_disable")
@admin_required
def disable(personality_id):
    row = _personality_or_404(personality_id)
    try:
        until = utc_datetime("until", label="Disabled until")
        reason = text("reason", max_length=500, label="Reason")
    except FormError as error:
        flash(str(error), "error")
        return _back()
    personalities_db.moderate(personality_id, disabled=True, updated_by=me()["id"], until=until, reason=reason)
    audit("personality_disable", _label(row), {"id": personality_id, "reason": reason,
                                               "until": str(until) if until else None})
    flash(f"{_who(row)} is disabled" + (f" until {until:%Y-%m-%d %H:%M} UTC." if until else "."), "success")
    return _back()


@bp.post("/personalities/<int:personality_id>/enable", endpoint="personality_enable")
@admin_required
def enable(personality_id):
    row = _personality_or_404(personality_id)
    personalities_db.moderate(personality_id, disabled=False, updated_by=me()["id"])
    audit("personality_enable", _label(row), {"id": personality_id})
    flash(f"{_who(row)} is available again.", "success")
    return _back()


@bp.post("/personalities/<int:personality_id>/delete", endpoint="personality_delete")
@admin_required
def delete(personality_id):
    row = _personality_or_404(personality_id)
    personalities_db.delete(personality_id)
    audit("personality_delete", _label(row), {"id": personality_id, "kind": row["kind"]})
    flash(f"{_who(row)} was deleted.", "success")
    return _back()


@bp.post("/personalities/<int:personality_id>/unshare", endpoint="personality_unshare")
@admin_required
def unshare(personality_id):
    row = _personality_or_404(personality_id)
    personalities_db.revoke_share(personality_id)
    audit("personality_unshare", _label(row), {"id": personality_id})
    flash(f"The share link of {_who(row)} no longer works.", "success")
    return _back()


@bp.post("/personalities/create", endpoint="personality_create")
@admin_required
def create():
    """Create a personality in a user's own list (it counts towards their limit)."""
    try:
        username = text("username", max_length=32, required=True, label="Owner")
        owner = users.get_by_username(username)
        if owner is None:
            raise FormError(f"There is no user called {username}.")
        name = text("name", max_length=personalities_db.MAX_NAME, required=True, label="Name")
        instructions = text("instructions", max_length=personalities_db.MAX_INSTRUCTIONS, required=True,
                            label="Instructions")
        if personalities_db.name_taken(owner["id"], name):
            raise FormError(f"{owner['username']} already has a personality called “{name}”.")
        personality_id = personalities_db.create(owner["id"], name, instructions, created_by=me()["id"],
                                                 enabled=not flag("disabled"))
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.personalities")
    audit("personality_create", f"{owner['username']}/{name}", {"id": personality_id})
    flash(f"Personality “{name}” created for {owner['username']}.", "success")
    return back("admin.personalities", q=owner["username"])


# ----- featured personalities ---------------------------------------------------------------

def _models():
    return [{"name": model["ollama_name"], "label": model["display_name"] or model["ollama_name"]}
            for model in usable_models(AccessContext.load(me()), "chat", kind="text")]


def _editor(row=None, values: dict | None = None, status: int = 200):
    models = _models()
    if values is None:
        values = {**service.fields_of(row), "enabled": bool(row["is_enabled"])} if row is not None else {
            "response_length": "balanced", "creativity": "balanced", "enabled": False}
    starters = [item for item in (values.get("starters") or []) if isinstance(item, str)]
    values = {**values, "starters": (starters + [""] * service.MAX_STARTERS)[:service.MAX_STARTERS]}
    preferred = values.get("preferred_model") or ""
    return render_template(
        "admin/personality_edit.html", section="personalities", row=row, values=values, models=models,
        missing_model=bool(preferred) and all(model["name"] != preferred for model in models),
        avatar_choices=service.AVATAR_CHOICES, colors=service.COLORS, lengths=service.LENGTHS,
        creativity_levels=service.CREATIVITY,
        limits={"name": service.MAX_NAME, "instructions": service.MAX_INSTRUCTIONS,
                "description": service.MAX_DESCRIPTION, "greeting": service.MAX_GREETING,
                "starter": service.MAX_STARTER},
    ), status


def _featured_fields(row=None):
    """(fields, None) or (None, response re-showing the form with the typed values)."""
    raw = service.from_form(request.form)
    try:
        fields = service.clean(raw, allowed_models=[model["name"] for model in _models()],
                               keep_model=row["preferred_model"] if row is not None else "")
        exclude = row["id"] if row is not None else None
        if personalities_db.featured_name_taken(fields["name"], exclude_id=exclude):
            raise FormError(f"A featured personality called “{fields['name']}” already exists.")
        owner = row["user_id"] if row is not None else me()["id"]
        if personalities_db.name_taken(owner, fields["name"], exclude_id=exclude):
            raise FormError(f"The owning administrator already has a personality called “{fields['name']}”; "
                            "choose another name.")
    except service.Invalid as problem:
        flash(problem.message("en"), "error")
        return None, _editor(row, {**raw, "enabled": flag("enabled")}, 400)
    except FormError as error:
        flash(str(error), "error")
        return None, _editor(row, {**raw, "enabled": flag("enabled")}, 400)
    return fields, None


def _extras(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if key not in ("name", "instructions")}


@bp.get("/personalities/featured/new", endpoint="personality_featured_new")
@admin_required
def featured_new():
    if personalities_db.count_featured() >= personalities_db.MAX_FEATURED:
        flash(f"There can be at most {personalities_db.MAX_FEATURED} featured personalities.", "error")
        return back("admin.personalities", kind="featured")
    return _editor()


@bp.post("/personalities/featured", endpoint="personality_featured_create")
@admin_required
def featured_create():
    fields, rejected = _featured_fields()
    if rejected:
        return rejected
    try:
        personality_id = personalities_db.create(me()["id"], fields["name"], fields["instructions"],
                                                 created_by=me()["id"], enabled=flag("enabled"), kind="featured",
                                                 **_extras(fields))
    except ValueError as error:
        flash(str(error), "error")
        return _editor(None, {**service.from_form(request.form), "enabled": flag("enabled")}, 400)
    audit("personality_featured_create", f"featured/{fields['name']}",
          {"id": personality_id, "published": flag("enabled")})
    flash(f"Featured personality “{fields['name']}” " + ("published." if flag("enabled") else "saved as a draft."),
          "success")
    return back("admin.personalities", kind="featured")


@bp.get("/personalities/<int:personality_id>/edit", endpoint="personality_featured_edit")
@admin_required
def featured_edit(personality_id):
    return _editor(_featured_or_404(personality_id))


@bp.post("/personalities/<int:personality_id>/edit", endpoint="personality_featured_update")
@admin_required
def featured_update(personality_id):
    row = _featured_or_404(personality_id)
    fields, rejected = _featured_fields(row)
    if rejected:
        return rejected
    try:
        personalities_db.update(row["id"], name=fields["name"], instructions=fields["instructions"],
                                enabled=flag("enabled"), updated_by=me()["id"], **_extras(fields))
    except ValueError as error:
        flash(str(error), "error")
        return _editor(row, {**service.from_form(request.form), "enabled": flag("enabled")}, 400)
    audit("personality_featured_update", f"featured/{fields['name']}",
          {"id": row["id"], "published": flag("enabled")})
    flash(f"Featured personality “{fields['name']}” saved" + (" and published." if flag("enabled") else " as a draft."),
          "success")
    return back("admin.personalities", kind="featured")


@bp.post("/personalities/<int:personality_id>/publish", endpoint="personality_publish")
@admin_required
def publish(personality_id):
    row = _featured_or_404(personality_id)
    published = flag("published")
    personalities_db.set_enabled(row["id"], published, me()["id"])
    audit("personality_publish" if published else "personality_unpublish", _label(row), {"id": row["id"]})
    flash(f"{_who(row)} is " + ("published: every user with access to personalities can see it."
                                if published else "unpublished and hidden from users."), "success")
    return _back()
