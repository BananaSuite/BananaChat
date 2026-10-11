"""Two-step verification in the administration: the requirement for administrators, and the reset
for an account that lost both its authenticator app and its recovery codes (see services.two_step).
The reset signs the account out everywhere and, unless the box is unticked, revokes its API keys."""

from __future__ import annotations

from flask import flash, g, redirect, url_for

from bananachat import db, security
from bananachat.db import users
from bananachat.i18n import translate
from bananachat.security import admin_required
from bananachat.services import two_step

from . import _reauth, bp
from ._helpers import audit, back, fail, me, user_or_404
from .users import revoke_api_tokens_if_asked


@bp.before_request
def require_two_step():
    """When the site requires it, administrators set it up before the administration pages open."""
    user = security.current_user()
    if user is None or not two_step.missing(user):
        return None
    if security.wants_json():
        return security.json_error("Set up two-step verification on your account page first.", 403,
                                   "two_step_required")
    # Shown on the account pages, which are translated.
    flash(translate(g.lang, "two_step.required_notice"), "info")
    return redirect(url_for("account.two_step"))


@bp.post("/users/<user_id>/two-step/reset", endpoint="user_two_step_reset")
@admin_required
def reset(user_id):
    user = user_or_404(user_id)
    if user_id == me()["id"]:
        return fail("Turn off your own two-step verification on your account page.", "admin.user_detail",
                    user_id=user_id)
    if not _reauth.confirmed():
        return back("admin.user_detail", user_id=user_id)
    with db.transaction():
        if not two_step.disable(user_id):
            flash(f"{user['username']} does not use two-step verification.", "info")
            return back("admin.user_detail", user_id=user_id)
        users.revoke_sessions(user_id)
    audit("user_two_step_reset", user["username"])
    flash(f"Two-step verification was reset for {user['username']}, who was signed out everywhere. They can sign "
          "in with their password alone and set it up again.", "success")
    revoke_api_tokens_if_asked(user)
    return back("admin.user_detail", user_id=user_id)
