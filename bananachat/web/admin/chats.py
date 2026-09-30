"""Chat audit: every user's conversations and the copies kept of no-history chats."""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from bananachat import security
from bananachat.db import chats, users
from bananachat.security import admin_required
from bananachat.web.chat import messages_json

from . import bp

PAGE_SIZE = 50
VIEW_LIMIT = 500


def _page() -> int:
    value = request.args.get("page", "1")
    return max(1, min(int(value), 100_000)) if value.isdigit() else 1


def _user_filter():
    """``(username, user_row, unknown)`` from the ``user`` query argument."""
    username = (request.args.get("user") or "").strip()[:64]
    if not username:
        return "", None, False
    user = users.get_by_username(username)
    return username, user, user is None


def _audit(action: str, target: str, details: dict) -> None:
    users.audit(security.current_user(), action, target, details, security.client_ip())


@bp.get("/chats", endpoint="chats")
@admin_required
def chat_list():
    username, user, unknown = _user_filter()
    include_deleted = request.args.get("deleted") == "1"
    kind = request.args.get("kind", "all")
    incognito = {"normal": False, "no_history": True}.get(kind)
    page = _page()
    if unknown:
        rows, total = [], 0
    else:
        filters = {"user_id": user["id"] if user else None, "include_deleted": include_deleted, "incognito": incognito}
        rows = chats.admin_list(**filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        total = chats.admin_count(**filters)
    return render_template("admin/chats.html", section="chats", rows=rows, total=total, page=page,
                           pages=max(1, -(-total // PAGE_SIZE)), username=username, unknown=unknown,
                           include_deleted=include_deleted, kind=kind)


@bp.get("/chats/<session_id>", endpoint="chat_view")
@admin_required
def chat_view(session_id):
    session = chats.admin_get(session_id)
    if session is None:
        abort(404)
    rows, has_more = chats.message_page(session_id, limit=VIEW_LIMIT)
    return render_template("admin/chats_view.html", section="chats", session=session,
                           messages=messages_json(rows, None), truncated=has_more,
                           untitled=chats.is_untitled(session["title"]))


@bp.post("/chats/<session_id>/delete", endpoint="chat_delete")
@admin_required
def chat_delete(session_id):
    session = chats.admin_get(session_id)
    if session is None:
        abort(404)
    chats.hard_delete(session_id)
    _audit("chat.admin_delete", session_id, {"owner": session["username"], "title": session["title"],
                                             "no_history": bool(session["is_incognito"])})
    flash("The chat was permanently deleted.", "success")
    return redirect(url_for("admin.chats"))


@bp.get("/incognito", endpoint="incognito")
@admin_required
def incognito():
    username, user, unknown = _user_filter()
    page = _page()
    if unknown:
        rows, total = [], 0
    else:
        user_id = user["id"] if user else None
        rows = chats.audit_sessions(user_id=user_id, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        total = chats.audit_session_count(user_id=user_id)
    return render_template("admin/incognito.html", section="chats", rows=rows, total=total, page=page,
                           pages=max(1, -(-total // PAGE_SIZE)), username=username, unknown=unknown)


@bp.get("/incognito/<session_id>", endpoint="incognito_session")
@admin_required
def incognito_session(session_id):
    if not chats.valid_id(session_id):
        abort(404)
    entries = chats.audit_entries(session_id)
    if not entries:
        abort(404)
    return render_template("admin/incognito_session.html", section="chats", session_id=session_id,
                           owner=entries[0]["username"], messages=entries, live=chats.get(session_id) is not None)


@bp.post("/incognito/entries/<int:entry_id>/delete", endpoint="incognito_delete_entry")
@admin_required
def incognito_delete_entry(entry_id):
    entry = chats.audit_entry(entry_id)
    if entry is None:
        abort(404)
    chats.delete_audit_entry(entry_id)
    _audit("chat.no_history_entry_delete", entry["session_id"], {"entry": entry_id, "role": entry["role"]})
    flash("The entry was deleted.", "success")
    if chats.audit_entries(entry["session_id"]):
        return redirect(url_for("admin.incognito_session", session_id=entry["session_id"]))
    return redirect(url_for("admin.incognito"))


@bp.post("/incognito/<session_id>/delete", endpoint="incognito_delete_session")
@admin_required
def incognito_delete_session(session_id):
    if not chats.valid_id(session_id):
        abort(404)
    count = chats.delete_audit_session(session_id)
    if not count:
        abort(404)
    _audit("chat.no_history_delete", session_id, {"entries": count})
    flash(f"Deleted {count} entries and the chat.", "success")
    return redirect(url_for("admin.incognito"))
