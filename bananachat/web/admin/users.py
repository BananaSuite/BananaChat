"""User management: search, create, roles, suspension, passwords, sessions, exports, deletion.

An account's limits have their own page (:mod:`.limits`), its two-step verification reset
:mod:`.two_step`. Creating an administrator, role changes, password resets, and suspending,
reinstating or deleting an administrator need a recent confirmation (:mod:`._reauth`)."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from flask import flash, render_template, request

from bananachat import db, security
from bananachat.db import chats, credits, tokens, users
from bananachat.db import settings as site_settings
from bananachat.db import limits as limits_db
from bananachat.security import admin_required
from bananachat.services import limits, two_step
from bananachat.services.agents import service as agents_service
from bananachat.web.account import download_export
from . import _reauth, bp

from ._helpers import FormError, audit, back, choice, fail, me, page, text, user_or_404, utc_datetime, utc_input_min
from ._quota_ui import POOL_LABELS

PAGE_SIZE = 50


@bp.get("/users", endpoint="users")
@admin_required
def list_users():
    search = (request.args.get("q") or "").strip()[:64]
    current = page(users.count(search), PAGE_SIZE)
    rows = users.list_page(PAGE_SIZE, current.offset, search)
    return render_template("admin/users.html", section="users", rows=rows, search=search, page=current,
                           defaults=credits.site_defaults(), min_password=security.PASSWORD_MIN,
                           now=db.now(), two_step_required=two_step.required_for_admins())


@bp.post("/users/create", endpoint="user_create")
@admin_required
def create_user():
    try:
        username = text("username", max_length=32, required=True, label="Username")
        if not users.USERNAME_RE.match(username):
            raise FormError("Usernames use 3-32 letters, digits, dots, hyphens or underscores.")
        password = request.form.get("password") or ""
        if security.password_problem(password, request.form.get("confirm_password")):
            raise FormError(f"Passwords need {security.PASSWORD_MIN}-{security.PASSWORD_MAX} characters "
                            "and both entries must match.")
        role = choice("role", users.ROLES, label="Role", default="user")
    except FormError as error:
        flash(str(error), "error")
        return back("admin.users")
    # Any new account, with a password the session chose, keeps working after the session ends.
    if not _reauth.confirmed():
        return back("admin.users")
    try:
        with db.transaction():
            if users.get_by_username(username):
                raise FormError("That username is already taken.")
            user_id = users.create(username, security.hash_password(password), role=role)
    except (FormError, sqlite3.IntegrityError) as error:
        flash(str(error) if isinstance(error, FormError) else "That username is already taken.", "error")
        return back("admin.users")
    audit("user_create", username, {"role": role})
    flash(f"Account {username} created.", "success")
    return back("admin.user_detail", user_id=user_id)


@bp.get("/users/<user_id>", endpoint="user_detail")
@admin_required
def user_detail(user_id):
    user = user_or_404(user_id)
    usage = {pool: credits.usage_today(user_id, pool) for pool in ("api", "chat", "agent")}
    limit_settings = limits_db.user_settings(user_id)
    return render_template(
        "admin/user_detail.html", section="users", user=user, usage=usage, limit_summary=_limit_summary(user),
        limit_settings=limit_settings, limit_tier=limits.resolve_tier(limit_settings.tier_id),
        sessions=users.list_sessions(user_id), is_self=user_id == me()["id"],
        two_step=two_step.state(user_id), two_step_required=two_step.required_for_admins(),
        current_session=security.current_session_hash(), min_password=security.PASSWORD_MIN,
        suspended_now=users.is_suspended(user), min_datetime=utc_input_min(),
        api_tokens=tokens.count_active(user_id), share_links=chats.count_shared(user_id),
        bm_mode_global=bool(site_settings.get().get("bm_mode_global")),
        bm_mode_self_service=bool(site_settings.get().get("bm_mode_self_service", 1)),
        chats=db.scalar("SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND deleted_at IS NULL", (user_id,), 0),
    )


def _limit_summary(user) -> list[dict]:
    if user["role"] == "admin":
        return []
    result = []
    for pool, label in POOL_LABELS.items():
        current = limits.effective(user, pool, usage=False)
        result.append({"label": label, "window": current.window, "weekly": current.weekly,
                       "custom": current.rate.custom or current.window.custom or current.weekly.custom,
                       "rate": limits.rate_text("en", current.rate.rules)
                       if current.rate.limited else "No request-rate limit"})
    return result


def _guard_self(user, action: str):
    if user["id"] == me()["id"]:
        raise FormError(f"You cannot {action} your own account here.")


@bp.post("/users/<user_id>/bm-mode", endpoint="user_bm_mode")
@admin_required
def user_bm_mode(user_id):
    """An account's BM mode (the easy mode) and who may switch it: the site's rule, the person, or administrators."""
    user = user_or_404(user_id)
    try:
        state = choice("state", ("on", "off"), label="BM mode")
        control = choice("control", users.BM_MODE_CONTROLS, label="Who switches it")
    except FormError as error:
        flash(str(error), "error")
        return back("admin.user_detail", user_id=user_id, _anchor="bm-mode")
    with db.transaction():
        users.set_bm_mode(user_id, state == "on")
        users.set_bm_mode_control(user_id, control)
    audit("user_bm_mode", user["username"], {"on": state == "on", "control": control})
    flash(f"BM mode saved for {user['username']}.", "success")
    return back("admin.user_detail", user_id=user_id, _anchor="bm-mode")


@bp.post("/users/<user_id>/role", endpoint="user_role")
@admin_required
def change_role(user_id):
    user = user_or_404(user_id)
    try:
        _guard_self(user, "change the role of")
        role = choice("role", users.ROLES, label="Role")
        if role != user["role"] and not _reauth.confirmed():
            return back("admin.user_detail", user_id=user_id)
        users.set_role(user_id, role)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.user_detail", user_id=user_id)
    audit("user_role", user["username"], {"from": user["role"], "to": role})
    flash(f"{user['username']} is now {'an administrator' if role == 'admin' else 'a regular user'}.", "success")
    return back("admin.user_detail", user_id=user_id)


@bp.post("/users/<user_id>/suspend", endpoint="user_suspend")
@admin_required
def suspend(user_id):
    user = user_or_404(user_id)
    try:
        _guard_self(user, "suspend")
        until = utc_datetime("until", label="Suspended until")
        if user["role"] == "admin" and not _reauth.confirmed():
            return back("admin.user_detail", user_id=user_id)
        users.suspend(user_id, until)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.user_detail", user_id=user_id)
    audit("user_suspend", user["username"], {"until": db.timestamp(until) if until else None})
    flash(f"{user['username']} is suspended" + (f" until {db.timestamp(until)} UTC." if until else "."), "success")
    return back("admin.user_detail", user_id=user_id)


@bp.post("/users/<user_id>/unsuspend", endpoint="user_unsuspend")
@admin_required
def unsuspend(user_id):
    user = user_or_404(user_id)
    if user["role"] == "admin" and not _reauth.confirmed():
        return back("admin.user_detail", user_id=user_id)
    users.unsuspend(user_id)
    audit("user_unsuspend", user["username"])
    flash(f"{user['username']} can sign in again.", "success")
    return back("admin.user_detail", user_id=user_id)


@bp.post("/users/<user_id>/password", endpoint="user_password")
@admin_required
def reset_password(user_id):
    user = user_or_404(user_id)
    try:
        _guard_self(user, "reset the password of")
        password = request.form.get("password") or ""
        if security.password_problem(password, request.form.get("confirm_password")):
            raise FormError(f"Passwords need {security.PASSWORD_MIN}-{security.PASSWORD_MAX} characters "
                            "and both entries must match.")
    except FormError as error:
        flash(str(error), "error")
        return back("admin.user_detail", user_id=user_id)
    if not _reauth.confirmed():
        return back("admin.user_detail", user_id=user_id)
    users.set_password(user_id, security.hash_password(password))
    tokens.revoke_all(user_id)
    audit("user_password", user["username"])
    flash(f"Password of {user['username']} changed. Their sessions and API tokens no longer work.", "success")
    stop_share_links_if_asked(user)
    return back("admin.user_detail", user_id=user_id)


@bp.post("/users/<user_id>/sessions/revoke", endpoint="user_session_revoke")
@admin_required
def revoke_session(user_id):
    user = user_or_404(user_id)
    session_hash = (request.form.get("session") or "")[:128]
    if user_id == me()["id"] and session_hash == security.current_session_hash():
        flash("That is the session you are using; sign out instead.", "error")
    elif users.revoke_session(user_id, session_hash):
        audit("user_session_revoke", user["username"])
        flash("Session ended.", "success")
    else:
        flash("That session had already ended.", "info")
    return back("admin.user_detail", user_id=user_id)


@bp.post("/users/<user_id>/sessions/revoke-all", endpoint="user_sessions_revoke_all")
@admin_required
def revoke_all_sessions(user_id):
    user = user_or_404(user_id)
    keep = security.current_session_hash() if user_id == me()["id"] else None
    users.revoke_sessions(user_id, except_hash=keep)
    if keep:
        security.renew_device_cookie(user_id)
    audit("user_sessions_revoke_all", user["username"])
    flash(f"{user['username']} has been signed out everywhere" + (" else." if keep else "."), "success")
    revoke_api_tokens_if_asked(user)
    stop_share_links_if_asked(user)
    return back("admin.user_detail", user_id=user_id)


def stop_share_links_if_asked(user) -> None:
    """Take the account's chat share links offline when the form asked to (the box is ticked by default).

    Someone who held one of its sessions may have shared its chats; a share link shows every later message.
    """
    if request.form.get("revoke_share_links") != "1":
        return
    stopped = chats.revoke_all_shares(user["id"])
    if stopped:
        audit("user_share_links_revoke_all", user["username"], {"count": stopped})
        flash(f"{stopped} chat share link{'s' if stopped != 1 else ''} of {user['username']} "
              f"{'were' if stopped != 1 else 'was'} stopped.", "success")


def revoke_api_tokens_if_asked(user) -> None:
    """Revoke the account's API keys when the form asked to (the box is ticked by default).

    Someone who held one of its sessions may have created a key; ending the sessions leaves it working.
    """
    if request.form.get("revoke_api_tokens") != "1":
        return
    revoked = tokens.revoke_all(user["id"])
    if revoked:
        audit("user_api_tokens_revoke_all", user["username"], {"count": revoked})
        flash(f"{revoked} API key{'s' if revoked != 1 else ''} of {user['username']} "
              f"{'were' if revoked != 1 else 'was'} revoked.", "success")


@bp.route("/users/<user_id>/export/<kind>", methods=["GET", "POST"], endpoint="user_export")
@admin_required
@security.download_first
def export(user_id, kind):
    user = user_or_404(user_id)
    # Exports start from the buttons on the user's page (POST), never from a link.
    if kind not in ("gdpr", "chats") or request.method != "POST":
        return back("admin.user_detail", user_id=user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = "personal-data" if kind == "gdpr" else "chats"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", user["username"])
    response = download_export("data" if kind == "gdpr" else "chats", user, f"{safe_name}-{name}-{stamp}.json")
    if response is None:
        return fail(f"An export of {user['username']} is still being downloaded. Try again when it has finished.",
                    "admin.user_detail", user_id=user_id)
    audit("user_export", user["username"], {"kind": kind})
    return response


@bp.post("/users/<user_id>/delete", endpoint="user_delete")
@admin_required
def delete(user_id):
    user = user_or_404(user_id)
    try:
        _guard_self(user, "delete")
        if (request.form.get("confirm_username") or "").strip() != user["username"]:
            raise FormError("Type the username exactly to confirm the deletion.")
        if user["role"] == "admin" and not _reauth.confirmed():
            return back("admin.user_detail", user_id=user_id)
        background_name = users.get_preferences(user_id)["background_image"]
        with db.transaction():  # a task started meanwhile is deleted with the account
            workspaces = agents_service.workspaces_of_user(user_id)
            users.delete(user_id)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.user_detail", user_id=user_id)
    agents_service.discard_workspaces(workspaces)
    if background_name:
        from bananachat.web.customization import remove_background_file
        remove_background_file(background_name)
    audit("user_delete", user["username"], {"role": user["role"]})
    flash(f"Account {user['username']} and all of its data were deleted.", "success")
    return back("admin.users")
