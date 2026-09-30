"""Invitation codes: create, list with usage, copy the sign-up link, delete."""

from __future__ import annotations

from datetime import timedelta

from flask import flash, render_template, request, url_for

from bananachat import db
from bananachat.db import admin_overview, invites
from bananachat.security import admin_required

from . import bp
from ._helpers import FormError, audit, back, choice, integer, me, text, utc_datetime, utc_input_min

EXPIRY_PRESETS = {"": None, "1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


@bp.get("/invites", endpoint="invites")
@admin_required
def list_invites():
    active, inactive = invites.list_all()
    usage = admin_overview.invite_usage([row["id"] for row in [*active, *inactive]])
    links = {row["id"]: url_for("auth.signup", invite=row["code"], _external=True) for row in active}
    return render_template("admin/invites.html", section="invites", active=active, inactive=inactive, usage=usage,
                           links=links, min_datetime=utc_input_min(), now=db.now())


@bp.post("/invites/create", endpoint="invite_create")
@admin_required
def create_invite():
    try:
        uses = integer("max_uses", minimum=0, maximum=100000, label="Uses", default=1)
        role = choice("role", ("user", "admin"), label="Role", default="user")
        code = text("code", max_length=32, label="Custom code") or None
        preset = request.form.get("expires", "")
        if preset == "custom":
            expires = utc_datetime("expires_at", label="Expiry")
            if expires is None:
                raise FormError("Choose the expiry date and time.")
        elif preset in EXPIRY_PRESETS:
            offset = EXPIRY_PRESETS[preset]
            expires = db.parse_timestamp(db.now(offset)) if offset else None
        else:
            raise FormError("Choose a valid expiry.")
        created = invites.create(me()["id"], max_uses=uses, expires_at=expires, assigned_role=role, code=code)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.invites")
    audit("invite_create", created, {"uses": uses, "role": role,
                                     "expires": db.timestamp(expires) if expires else None})
    flash(f"Invitation {created} created. Copy its link below and send it to the person you invite.", "success")
    return back("admin.invites")


@bp.post("/invites/<int:invite_id>/delete", endpoint="invite_delete")
@admin_required
def delete_invite(invite_id):
    row = db.one("SELECT code FROM invite_codes WHERE id=? AND deleted=0", (invite_id,))
    if row is None:
        flash("That invitation no longer exists.", "info")
        return back("admin.invites")
    invites.delete(invite_id)
    audit("invite_delete", row["code"])
    flash(f"Invitation {row['code']} deleted; it can no longer be used.", "success")
    return back("admin.invites")
