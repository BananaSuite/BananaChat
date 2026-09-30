"""Personalities: instructions and a presentation a person can apply to their chats.

Creating, editing, copying, sharing and importing need the ``custom_personality``
capability. People who lost it may still switch their saved personalities off,
delete or export them. Administrators can disable a personality (optionally
until a date, with a reason); the owner sees that state but cannot lift it,
and its share link stops working meanwhile.

Featured personalities are published by administrators for everyone: users
see them read-only and can copy them ("Duplicate to my personalities").
A share link lets another signed-in user preview a personality and save a
copy; copies never follow later changes of the original. The same fields and
limits apply to the editor, copies and imported files
(:func:`bananachat.services.personalities.clean`).
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from bananachat import security
from bananachat.db import personalities, users
from bananachat.i18n import translate
from bananachat.services import personalities as service
from bananachat.services.access import AccessContext, usable_models
from bananachat.web.chat import _disposition as attachment_disposition

bp = Blueprint("personalities", __name__)

# Uploads larger than an exported file can be are refused before they are parsed.
IMPORT_BODY_LIMIT = service.MAX_IMPORT_BYTES + 16 * 1024


def _t(key, **params):
    return translate(g.lang, key, **params)


def _user():
    return security.current_user()


def _owned(personality_id: int):
    row = personalities.get(personality_id)
    if row is None or row["kind"] != "user" or row["user_id"] != _user()["id"]:
        abort(404)
    return row


def _back(anchor: str | None = None):
    return redirect(url_for("personalities.index", _anchor=anchor))


def _card_anchor(personality_id) -> str:
    return f"personality-{personality_id}"


def _moderation(row) -> dict:
    """How an administrator restricted a personality, if at all."""
    if not personalities.is_blocked(row):
        return {"blocked": False}
    return {"blocked": True, "until": row["disabled_until"] if row["disabled_until"] else None,
            "reason": row["disabled_reason"] or ""}


def _models(context: AccessContext) -> list[dict]:
    return [{"name": model["ollama_name"], "label": model["display_name"] or model["ollama_name"]}
            for model in usable_models(context, "chat", kind="text")]


def _card(row, *, model_labels: dict, default_id) -> dict:
    preferred = row["preferred_model"] or ""
    return {
        "row": row, "active": personalities.is_active(row), "moderation": _moderation(row),
        "starters": personalities.starters(row), "is_default": row["id"] == default_id,
        "model_label": model_labels.get(preferred, preferred), "model_missing": bool(preferred)
        and preferred not in model_labels,
        "share_url": url_for("personalities.shared", token=row["share_token"], _external=True)
        if row["share_token"] else None,
    }


def _refuse(message_key: str = "personality.no_access_action", **params):
    flash(_t(message_key, **params), "error")
    return _back()


def _audit(action: str, target, details=None) -> None:
    users.audit(_user(), f"personality.{action}", target, details, security.client_ip())


# ----- pages ----------------------------------------------------------------------

@bp.get("/personalities", endpoint="index")
@security.login_required
def index():
    user = _user()
    context = AccessContext.load(user)
    allowed = context.allows("custom_personality")
    models = _models(context)
    labels = {model["name"]: model["label"] for model in models}
    default_id = personalities.default_id(user["id"])
    rows = personalities.list_for(user["id"])
    featured = [row for row in personalities.list_featured() if personalities.is_active(row)] if allowed else []
    choices = service.choices(user, context)
    if default_id and not any(row["id"] == default_id for row in choices):
        default_id = None  # a default that can no longer be used behaves like "none"
    return render_template(
        "personalities/index.html",
        items=[_card(row, model_labels=labels, default_id=default_id) for row in rows],
        featured=[_card(row, model_labels=labels, default_id=default_id) for row in featured],
        allowed=allowed, count=len(rows), max_count=personalities.MAX_PER_USER,
        default_id=default_id, default_choices=choices, templates=service.templates(g.lang) if allowed else [],
        max_import_kb=service.MAX_IMPORT_BYTES // 1024,
    )


def _editor(*, row=None, values: dict | None = None, status: int = 200, template_key: str = ""):
    """The editor page; *values* keeps what was typed into a form that failed validation."""
    user = _user()
    context = AccessContext.load(user)
    models = _models(context)
    if values is None:
        values = {**service.fields_of(row), "enabled": bool(row["is_enabled"])} if row is not None else {
            **(service.template(template_key, g.lang) or {"response_length": "balanced", "creativity": "balanced"}),
            "enabled": True}
    starters = [item for item in (values.get("starters") or []) if isinstance(item, str)]
    values = {**values, "starters": (starters + [""] * service.MAX_STARTERS)[:service.MAX_STARTERS]}
    preferred = values.get("preferred_model") or ""
    missing_model = bool(preferred) and all(model["name"] != preferred for model in models)
    return render_template(
        "personalities/edit.html", row=row, values=values, models=models, missing_model=missing_model,
        templates=service.templates(g.lang) if row is None else [], template_key=template_key,
        avatar_choices=service.AVATAR_CHOICES, colors=service.COLORS, lengths=service.LENGTHS,
        creativity_levels=service.CREATIVITY,
        limits={"name": service.MAX_NAME, "instructions": service.MAX_INSTRUCTIONS,
                "description": service.MAX_DESCRIPTION, "greeting": service.MAX_GREETING,
                "starter": service.MAX_STARTER},
    ), status


@bp.get("/personalities/new", endpoint="new")
@security.login_required
def new():
    user = _user()
    if not AccessContext.load(user).allows("custom_personality"):
        return _refuse()
    if personalities.count_for(user["id"]) >= personalities.MAX_PER_USER:
        return _refuse("personality.limit_reached", max=personalities.MAX_PER_USER)
    key = request.args.get("template") or ""
    return _editor(template_key=key if key in service.TEMPLATES else "")


@bp.get("/personalities/<int:personality_id>/edit", endpoint="edit")
@security.login_required
def edit(personality_id):
    row = _owned(personality_id)
    if not AccessContext.load(_user()).allows("custom_personality"):
        return _refuse()
    return _editor(row=row)


# ----- create and edit -----------------------------------------------------------------

def _submitted(row=None):
    """(fields, None) for a valid form, or (None, response) re-showing the editor with the problem."""
    context = AccessContext.load(_user())
    raw = service.from_form(request.form)
    try:
        fields = service.clean(raw, allowed_models=service.allowed_model_names(context),
                               keep_model=row["preferred_model"] if row is not None else "")
        if personalities.name_taken(_user()["id"], fields["name"], exclude_id=row["id"] if row is not None else None):
            raise service.Invalid("personality.name_taken")
    except service.Invalid as problem:
        flash(problem.message(g.lang), "error")
        return None, _editor(row=row, values={**raw, "enabled": request.form.get("enabled") == "1"}, status=400)
    return fields, None


def _extras(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if key not in ("name", "instructions")}


@bp.post("/personalities", endpoint="create")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def create():
    user = _user()
    if not AccessContext.load(user).allows("custom_personality"):
        return _refuse()
    if personalities.count_for(user["id"]) >= personalities.MAX_PER_USER:
        return _refuse("personality.limit_reached", max=personalities.MAX_PER_USER)
    fields, rejected = _submitted()
    if rejected:
        return rejected
    try:
        personality_id = personalities.create(user["id"], fields["name"], fields["instructions"],
                                              created_by=user["id"], enabled=request.form.get("enabled") == "1",
                                              **_extras(fields))
    except ValueError:
        return _refuse("personality.save_failed")
    _audit("create", personality_id, {"name": fields["name"]})
    flash(_t("personality.created", name=fields["name"]), "success")
    return _back(_card_anchor(personality_id))


@bp.post("/personalities/<int:personality_id>", endpoint="update")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def update(personality_id):
    user = _user()
    row = _owned(personality_id)
    if not AccessContext.load(user).allows("custom_personality"):
        return _refuse()
    fields, rejected = _submitted(row)
    if rejected:
        return rejected
    try:
        personalities.update(row["id"], name=fields["name"], instructions=fields["instructions"],
                             enabled=request.form.get("enabled") == "1", updated_by=user["id"], **_extras(fields))
    except ValueError:
        return _refuse("personality.save_failed")
    _audit("update", row["id"])
    flash(_t("personality.updated", name=fields["name"]), "success")
    return _back(_card_anchor(row["id"]))


@bp.post("/personalities/<int:personality_id>/toggle", endpoint="toggle")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def toggle(personality_id):
    user = _user()
    row = _owned(personality_id)
    enable = request.form.get("enabled") == "1"
    if enable and not AccessContext.load(user).allows("custom_personality"):
        return _refuse()
    personalities.set_enabled(row["id"], enable, user["id"])
    flash(_t("personality.enabled_now" if enable else "personality.disabled_now", name=row["name"]), "success")
    return _back(_card_anchor(row["id"]))


@bp.post("/personalities/<int:personality_id>/delete", endpoint="delete")
@security.login_required
def delete(personality_id):
    row = _owned(personality_id)
    personalities.delete(row["id"])
    _audit("delete", row["id"], {"name": row["name"]})
    flash(_t("personality.deleted", name=row["name"]), "success")
    return _back()


# ----- default for new chats ------------------------------------------------------------

@bp.post("/personalities/default", endpoint="set_default")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def set_default():
    user = _user()
    raw = (request.form.get("personality_id") or "").strip()
    if not raw:
        personalities.set_default(user["id"], None)
        flash(_t("personality.default_cleared"), "success")
        return _back("default")
    row = service.usable(user, raw)
    if row is None:
        return _refuse("personality.default_unavailable")
    personalities.set_default(user["id"], row["id"])
    flash(_t("personality.default_set", name=row["name"]), "success")
    return _back("default")


# ----- copies --------------------------------------------------------------------------------

def _copy(user, source, *, action: str, name_model: bool = True):
    """Save a copy of *source* (a stored personality) for *user*; a redirect with the outcome."""
    context = AccessContext.load(user)
    if not context.allows("custom_personality"):
        return _refuse()
    if personalities.count_for(user["id"]) >= personalities.MAX_PER_USER:
        return _refuse("personality.limit_reached", max=personalities.MAX_PER_USER)
    try:
        fields = service.clean(service.fields_of(source), allowed_models=None)
    except service.Invalid:
        return _refuse("personality.save_failed")
    return _save_copy(user, context, fields, action=action, source_id=source["id"], name_model=name_model)


def _save_copy(user, context, fields: dict, *, action: str, source_id=None, done_key: str = "personality.copied",
               name_model: bool = True):
    if fields["preferred_model"] and fields["preferred_model"] not in service.allowed_model_names(context):
        flash(_t("personality.model_dropped", model=fields["preferred_model"]) if name_model else
              _t("personality.model_dropped_unnamed"), "warning")
        fields = {**fields, "preferred_model": ""}
    name = personalities.free_name(user["id"], fields["name"])
    try:
        personality_id = personalities.create(user["id"], name, fields["instructions"], created_by=user["id"],
                                              **_extras(fields))
    except ValueError:
        return _refuse("personality.save_failed")
    _audit(action, personality_id, {"name": name, "source": source_id})
    flash(_t(done_key, name=name), "success")
    return _back(_card_anchor(personality_id))


@bp.post("/personalities/<int:personality_id>/duplicate", endpoint="duplicate")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def duplicate(personality_id):
    """Copy one of the user's personalities, or a published featured one, into the user's list."""
    user = _user()
    row = personalities.get(personality_id)
    if row is None or not ((row["kind"] == "user" and row["user_id"] == user["id"]) or
                           (row["kind"] == "featured" and personalities.is_active(row))):
        abort(404)
    if personalities.is_blocked(row):
        return _refuse("personality.save_failed")
    return _copy(user, row, action="duplicate")


# ----- share links ----------------------------------------------------------------------------

@bp.post("/personalities/<int:personality_id>/share", endpoint="share")
@security.login_required
@security.rate_limit("personality_share", 20, 3600, per_user=True)
def share(personality_id):
    user = _user()
    row = _owned(personality_id)
    if not AccessContext.load(user).allows("custom_personality"):
        return _refuse()
    if personalities.is_blocked(row):
        return _refuse("personality.share_blocked")
    token = personalities.share(row["id"])
    url = url_for("personalities.shared", token=token, _external=True)
    _audit("share", row["id"], {"name": row["name"]})
    if security.wants_json():
        return {"ok": True, "url": url}
    flash(_t("personality.share_created", name=row["name"]), "success")
    return _back(_card_anchor(row["id"]))


@bp.post("/personalities/<int:personality_id>/unshare", endpoint="unshare")
@security.login_required
def unshare(personality_id):
    row = _owned(personality_id)
    personalities.revoke_share(row["id"])
    _audit("unshare", row["id"], {"name": row["name"]})
    if security.wants_json():
        return {"ok": True}
    flash(_t("personality.share_revoked", name=row["name"]), "success")
    return _back(_card_anchor(row["id"]))


def _shared_or_404(token):
    """The shared personality, while neither an administrator nor the owner's account stops sharing it."""
    row = personalities.by_share_token(token)
    if row is None or personalities.is_blocked(row):
        abort(404)
    owner = users.get(row["user_id"])
    if row["user_id"] != _user()["id"] and (owner is None or users.is_suspended(owner) or
                                            not AccessContext.load(owner).allows("custom_personality")):
        abort(404)
    return row


@bp.get("/personalities/shared/<token>", endpoint="shared")
@security.login_required
def shared(token):
    row = _shared_or_404(token)
    user = _user()
    context = AccessContext.load(user)
    labels = {model["name"]: model["label"] for model in _models(context)}
    card = _card(row, model_labels=labels, default_id=None)
    # The owner's preferred model is only named to viewers who may use it.
    card["hide_model"] = card["model_missing"] and row["user_id"] != user["id"]
    response = current_app.make_response(render_template(
        "personalities/shared.html", card=card, token=token,
        own=row["user_id"] == user["id"], allowed=context.allows("custom_personality"),
        full=personalities.count_for(user["id"]) >= personalities.MAX_PER_USER, max_count=personalities.MAX_PER_USER))
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@bp.post("/personalities/shared/<token>/save", endpoint="save_shared")
@security.login_required
@security.rate_limit("personalities", 60, 3600, per_user=True)
def save_shared(token):
    row = _shared_or_404(token)
    return _copy(_user(), row, action="save_shared", name_model=False)


# ----- import and export --------------------------------------------------------------------

@bp.get("/personalities/<int:personality_id>/export", endpoint="export")
@security.login_required
def export(personality_id):
    row = _owned(personality_id)
    body = current_app.json.dumps(service.export(row), ensure_ascii=False, indent=2) + "\n"
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": attachment_disposition(service.export_filename(row), "personality.json"),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@bp.post("/personalities/import", endpoint="import_file")
@security.body_limit(IMPORT_BODY_LIMIT)
@security.login_required
@security.rate_limit("personality_import", 20, 3600, per_user=True)
def import_file():
    user = _user()
    context = AccessContext.load(user)
    if not context.allows("custom_personality"):
        return _refuse()
    if personalities.count_for(user["id"]) >= personalities.MAX_PER_USER:
        return _refuse("personality.limit_reached", max=personalities.MAX_PER_USER)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _refuse("personality.import_missing")
    data = upload.stream.read(service.MAX_IMPORT_BYTES + 1)
    try:
        fields = service.clean(service.parse_import(data), allowed_models=None)
    except service.Invalid as problem:
        flash(_t("personality.import_failed", problem=problem.message(g.lang)), "error")
        return _back("import")
    return _save_copy(user, context, fields, action="import", done_key="personality.imported")
