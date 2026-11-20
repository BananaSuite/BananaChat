"""BananaWiki Hosting links in the administration: removing an account's link (audited).

Removing a link signs the account out everywhere: its sessions may have come through that link. The
person can connect again (with the account's password) on their next hosting sign-in. An administrator
cannot remove their own link while only BananaWiki Hosting signs in.
"""

from __future__ import annotations

from flask import flash

from bananachat.db import hosting_links, users
from bananachat.security import admin_required
from bananachat.services import hosting_signin

from . import bp
from ._helpers import audit, back, fail, me, user_or_404


@bp.post("/users/<user_id>/hosting/unlink", endpoint="user_hosting_unlink")
@admin_required
def unlink(user_id):
    user = user_or_404(user_id)
    if user_id == me()["id"] and hosting_signin.effective_mode() == "hosting":
        return fail("Only BananaWiki Hosting accounts can sign in: removing your own link would lock you out. "
                    "Choose another sign-in mode first.", "admin.user_detail", user_id=user_id)
    removed = hosting_links.remove(user_id)
    if removed is None:
        flash(f"{user['username']} is not connected to BananaWiki Hosting.", "info")
        return back("admin.user_detail", user_id=user_id)
    if user_id != me()["id"]:
        users.revoke_sessions(user_id)
    audit("user_hosting_unlink", user["username"], {"portal_username": removed["portal_username"]})
    flash(f"{user['username']} is no longer connected to BananaWiki Hosting"
          + ("." if user_id == me()["id"] else " and was signed out everywhere."), "success")
    return back("admin.user_detail", user_id=user_id)
