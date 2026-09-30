"""The chat interface, conversation exports and read-only shared links.

Every ``/chat/<sid>`` route answers 404 unless the chat belongs to the signed-in
user and is not deleted. The streaming protocol of ``/chat/<sid>/send`` is
described in :mod:`bananachat.services.chat`.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

from flask import (Blueprint, Response, abort, current_app, g, redirect, render_template, request, stream_with_context,
                   url_for)

from bananachat import db, security
from bananachat.config import Config
from bananachat.db import chats, runs
from bananachat.i18n import translate
from bananachat.security import login_required
from bananachat.services import attachments, limits
from bananachat.services import status as service_status
from bananachat.services import chat as chat_service
from bananachat.services import personalities as personality_service
from bananachat.services.access import AccessContext

bp = Blueprint("chat", __name__)

SIDEBAR_PAGE = 30
MESSAGE_PAGE = 50
SHARED_PAGE = 200
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


# ----- helpers ------------------------------------------------------------------

def _t(key: str, **params) -> str:
    return translate(g.lang, key, **params)


def _owned(session_id: str):
    session = chats.get_owned(session_id, g.user["id"])
    if session is None:
        abort(404)
    return session


def _iso(value) -> str | None:
    moment = db.parse_timestamp(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def message_json(row, attachment_rows, session_id: str | None) -> dict:
    """A message for the browser. *session_id* None hides owner-only links (shared view)."""
    item = {
        "id": row["id"], "role": row["role"], "content": row["content"], "model": row["model"],
        "model_name": row["model_name"] or row["model"], "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"], "state": row["generation_state"] or "completed",
        "created_at": _iso(row["created_at"]),
        "attachments": [{
            "id": attachment["id"], "kind": attachment["kind"], "filename": attachment["filename"],
            "size": attachment["size_bytes"],
            "url": url_for("chat.attachment", session_id=session_id, attachment_id=attachment["id"])
            if session_id else None,
        } for attachment in attachment_rows],
    }
    if session_id and row["role"] == "assistant":
        item["pdf_url"] = url_for("chat.message_pdf", session_id=session_id, message_id=row["id"])
    return item


def messages_json(rows, session_id: str | None) -> list[dict]:
    grouped = chats.attachments_for(row["id"] for row in rows)
    return [message_json(row, grouped.get(row["id"], []), session_id) for row in rows]


def _session_json(row) -> dict:
    return {"id": row["id"], "title": chat_service.display_title(row["title"], g.lang),
            "url": url_for("chat.session", session_id=row["id"]), "shared": bool(row["shared"]),
            "updated_at": _iso(row["updated_at"]), "snippet": row.get("snippet") or ""}


def _cursor(row) -> str:
    return f"{row['updated_at']}~{row['id']}"


def _parse_cursor(value: str | None):
    if not value:
        return None
    stamp, _, session_id = value.partition("~")
    if not _TIMESTAMP.match(stamp) or not chats.valid_id(session_id):
        abort(400)
    return stamp, session_id


def _payload():
    """The submitted form, or the JSON body when it is an object (400 otherwise)."""
    if not request.is_json:
        return request.form
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    return payload


def _int_arg(name: str) -> int | None:
    value = request.args.get(name)
    if value is None or value == "":
        return None
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        abort(400)
    return int(value)


def _disposition(filename: str, fallback: str) -> str:
    """``attachment`` with an ASCII filename and the RFC 5987 UTF-8 form for other scripts."""
    clean = " ".join(re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', " ", filename).split()).strip(" .")[:120] or fallback
    ascii_name = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "", ascii_name).strip(" .") or fallback
    if "." in clean and not ascii_name.endswith(clean.rsplit(".", 1)[1]):
        ascii_name = f"{ascii_name}.{clean.rsplit('.', 1)[1]}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def _export(session):
    """The conversation as Markdown, streamed in batches."""
    site = g.settings.get("site_name") or "BananaChat"
    title = chat_service.display_title(session["title"], g.lang)
    you, assistant = _t("chat.you"), _t("chat.assistant")
    reasoning_label = _t("chat.reasoning")
    session_id = session["id"]

    def generate():
        yield f"# {title}\n\n_{_t('chat.export_note', site=site, date=db.now()[:16] + ' UTC')}_\n\n"
        for row in chats.iter_messages(session_id):
            who = you if row["role"] == "user" else (row["model_name"] or assistant)
            yield f"---\n\n### {who} · {row['created_at'][:16]} UTC\n\n"
            # Only answers carry a reasoning block; a user's message is exported as typed.
            reasoning, answer = attachments.split_reasoning(row["content"]) if row["role"] == "assistant" \
                else ("", row["content"])
            if reasoning:
                quoted = "\n".join("> " + line for line in reasoning.splitlines())
                yield f"> **{reasoning_label}**\n>\n{quoted}\n\n"
            yield answer.rstrip() + "\n\n"
            names = [item["filename"] for item in chats.attachments_for([row["id"]]).get(row["id"], [])]
            if names:
                yield f"_{_t('chat.export_attachments', names=', '.join(names))}_\n\n"

    filename = f"{title}.md"
    return Response(stream_with_context(generate()), mimetype="text/markdown", headers={
        "Content-Disposition": _disposition(filename, "chat.md"), "Cache-Control": "private, no-store"})


def _json(payload, status: int = 200):
    response = current_app.response_class(
        current_app.json.dumps(payload), status=status, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


# ----- pages --------------------------------------------------------------------

def _empty_chat(*, incognito: bool, personality=False) -> str:
    """The user's newest empty chat of this kind (renewed so retention keeps it), or a new one.

    A new chat starts with the user's default personality. *personality* (a usable
    personality row or None) replaces the personality of the chat that is returned;
    False keeps a reused chat as it is.
    """
    existing = chats.latest_empty(g.user["id"], incognito=incognito)
    if existing is None:
        session_id = chats.create(g.user["id"], incognito=incognito)
        chosen = personality_service.default_for(g.user) if personality is False else personality
    else:
        session_id = existing["id"]
        chats.touch(session_id)
        if personality is False:
            return session_id
        chosen = personality
    chats.set_personality(session_id, chosen["id"] if chosen is not None else None)
    return session_id


@bp.get("/chat", endpoint="index")
@login_required
def index():
    return redirect(url_for("chat.session", session_id=_empty_chat(incognito=False)))


@bp.post("/chat/new", endpoint="new")
@login_required
def new():
    """A fresh chat: with the personality asked for ("Try it"), else with the default personality."""
    context = AccessContext.load(g.user)
    requested = request.form.get("personality_id")
    personality = personality_service.usable(g.user, requested, context) if requested else None
    if personality is None:
        personality = personality_service.default_for(g.user, context)
    session_id = _empty_chat(incognito=request.form.get("incognito") == "1", personality=personality)
    return redirect(url_for("chat.session", session_id=session_id))


@bp.get("/chat/<session_id>", endpoint="session")
@login_required
def session_page(session_id):
    session = _owned(session_id)
    runs.recover_stale(session_id)
    user = g.user
    config = current_app.config["BC"]
    context = AccessContext.load(user)
    rows, has_more = chats.message_page(session_id, limit=MESSAGE_PAGE)
    status = runs.status(session_id)
    sidebar = chats.list_for_user(user["id"], limit=SIDEBAR_PAGE)
    personality_allowed = context.allows("custom_personality")
    model_list = limits.composer_choices(
        user, chat_service.model_choices(context, g.lang), lang=g.lang,
        request_url=lambda name, level: url_for("account.index", request="effort", model=name, level=level,
                                                _anchor="quota"))
    model_names = {model["name"] for model in model_list}
    personality_options = [personality_service.chat_json(row, model_names)
                           for row in personality_service.choices(user, context)]
    title = chat_service.display_title(session["title"], g.lang)
    data = {
        "session": {
            "id": session_id, "title": title, "untitled": chats.is_untitled(session["title"]),
            "incognito": bool(session["is_incognito"]),
            "shared_url": url_for("chat.shared", token=session["shared_token"], _external=True)
            if session["shared_token"] else None,
            "personality_id": session["personality_id"],
            "last_model": chats.last_model_name(session_id),
        },
        "site_name": g.settings.get("site_name") or "BananaChat",
        "messages": messages_json(rows, session_id),
        "has_more": has_more,
        "run": {"generating": status["active"], "state": status["state"], "partial": status["partial"],
                "stopping": status["stopping"], "error": status["error"]},
        "models": model_list,
        "personalities": personality_options if personality_allowed else None,
        "sidebar_next": _cursor(sidebar[-1]) if len(sidebar) == SIDEBAR_PAGE else None,
        "retention_days": config.deleted_chat_retention_days,
        "no_history_hours": config.no_history_ttl_hours,
        "limits": {"max_files": config.chat_max_files, "max_image_bytes": config.chat_max_image_bytes,
                   "max_document_bytes": config.chat_max_document_bytes, "max_text_bytes": config.chat_max_text_bytes,
                   "max_message_bytes": config.chat_max_message_bytes,
                   "max_request_bytes": config.chat_max_request_bytes, "accept": attachments.ACCEPT},
        "urls": {"root": url_for("chat.index"), "search": url_for("chat.search"),
                 "personalities": url_for("personalities.index") if personality_allowed else None,
                 "sessions": url_for("chat.sessions"), "messages": url_for("chat.messages", session_id=session_id)},
    }
    return render_template(
        "chat/session.html", session=session, title=title, data=data, sidebar=sidebar,
        paused=service_status.inference_block() is not None,
        personality_allowed=personality_allowed, personality_options=personality_options,
        retention_days=config.deleted_chat_retention_days, no_history_hours=config.no_history_ttl_hours)


# ----- sending and run control -----------------------------------------------------

@bp.post("/chat/<session_id>/send", endpoint="send")
@security.body_limit(Config.chat_max_request_bytes)
@login_required
@security.long_request
def send(session_id):
    session = _owned(session_id)
    blocked = service_status.guard()
    if blocked:
        return blocked
    if request.is_json:
        form = request.get_json(silent=True)
        if not isinstance(form, dict):
            return security.json_error(_t("chat.error_content"), 400)
        files = []
    else:
        form, files = request.form, request.files.getlist("files")
    try:
        prepared = chat_service.prepare(g.user, session, form, files, lang=g.lang)
        run = chat_service.start(prepared)
    except chat_service.SendError as error:
        response = security.json_error(error.message, error.status, error.code)
        if error.retry_after:
            response.headers["Retry-After"] = str(error.retry_after)
        return response
    response = Response(run.channel.stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    response.call_on_close(run.channel.detach)
    return response


@bp.get("/chat/<session_id>/status", endpoint="status")
@login_required
def status(session_id):
    """The run's state; never writes (stale runs are recovered by a background job and on page load)."""
    session = _owned(session_id)
    state = runs.status(session_id)
    after = _int_arg("after")
    rows = chats.messages_after(session_id, after) if after is not None else []
    return _json({
        "generating": state["active"], "state": state["state"], "error": state["error"],
        "stopping": state["stopping"], "partial": state["partial"] if (state["active"] or state["stale"]) else "",
        "title": chat_service.display_title(session["title"], g.lang),
        "messages": messages_json(rows, session_id),
    })


@bp.post("/chat/<session_id>/stop", endpoint="stop")
@login_required
def stop(session_id):
    _owned(session_id)
    return _json({"ok": True, "stopping": chat_service.stop(session_id)})


@bp.get("/chat/<session_id>/messages", endpoint="messages")
@login_required
def messages(session_id):
    _owned(session_id)
    rows, has_more = chats.message_page(session_id, before=_int_arg("before"), limit=MESSAGE_PAGE)
    return _json({"messages": messages_json(rows, session_id), "has_more": has_more})


# ----- managing chats ------------------------------------------------------------

@bp.post("/chat/<session_id>/title", endpoint="title")
@login_required
def rename(session_id):
    _owned(session_id)
    value = _payload().get("title")
    try:
        title = chats.rename(session_id, value if isinstance(value, str) else "")
    except ValueError:
        return security.json_error(_t("chat.error_title"), 400)
    return _json({"ok": True, "title": title})


@bp.post("/chat/<session_id>/personality", endpoint="personality")
@login_required
def set_personality(session_id):
    _owned(session_id)
    raw = _payload().get("personality_id")
    if raw in (None, "", 0, "0"):
        chats.set_personality(session_id, None)
        return _json({"ok": True, "personality_id": None})
    try:
        personality_id = int(raw)
    except (TypeError, ValueError):
        return security.json_error(_t("chat.error_personality"), 400)
    if chat_service.usable_personality(g.user, personality_id) is None:
        return security.json_error(_t("chat.error_personality"), 403, "forbidden")
    chats.set_personality(session_id, personality_id)
    return _json({"ok": True, "personality_id": personality_id})


@bp.post("/chat/<session_id>/delete", endpoint="delete")
@login_required
def delete(session_id):
    session = _owned(session_id)
    chat_service.delete_chat(session)
    if security.wants_json():
        return _json({"ok": True, "redirect": url_for("chat.index")})
    return redirect(url_for("chat.index"))


@bp.post("/chat/<session_id>/share", endpoint="share")
@login_required
def share(session_id):
    session = _owned(session_id)
    action = _payload().get("action", "create")
    if action == "revoke":
        chats.revoke_share(session_id)
        return _json({"ok": True, "shared": False, "url": None})
    if session["is_incognito"]:
        return security.json_error(_t("chat.error_share_no_history"), 400)
    if not chats.count_messages(session_id):
        return security.json_error(_t("chat.error_share_empty"), 400)
    token = chats.share(session_id)
    return _json({"ok": True, "shared": True, "url": url_for("chat.shared", token=token, _external=True)})


# ----- exports and files --------------------------------------------------------------

@bp.get("/chat/<session_id>/download", endpoint="download")
@login_required
def download(session_id):
    return _export(_owned(session_id))


@bp.get("/chat/<session_id>/attachments/<attachment_id>", endpoint="attachment")
@login_required
def attachment(session_id, attachment_id):
    _owned(session_id)
    row = chats.get_attachment(session_id, attachment_id)
    if row is None:
        abort(404)
    if row["kind"] == "image":
        response = Response(row["image_data"], mimetype=row["media_type"])
        response.headers["Content-Disposition"] = _disposition(row["filename"], "image").replace(
            "attachment;", "inline;", 1)
    else:
        response = Response(row["extracted_text"] or "", mimetype="text/plain")
        response.headers["Content-Disposition"] = _disposition(row["filename"] + ".txt", "document.txt")
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@bp.get("/chat/<session_id>/messages/<int:message_id>/pdf", endpoint="message_pdf")
@login_required
def message_pdf(session_id, message_id):
    session = _owned(session_id)
    row = chats.get_message(session_id, message_id)
    if row is None or row["role"] != "assistant":
        abort(404)
    pdf = attachments.render_pdf(row["content"], chat_service.display_title(session["title"], g.lang))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": _disposition(f"response-{message_id}.pdf", "response.pdf"),
        "Cache-Control": "private, no-store"})


# ----- history ---------------------------------------------------------------------

@bp.get("/chat/search", endpoint="search")
@login_required
def search():
    text = " ".join((request.args.get("q") or "").split())[:200]
    if len(text) < 2:
        return _json({"results": []})
    rows = chats.search(g.user["id"], text)
    return _json({"results": [_session_json(row) for row in rows]})


@bp.get("/chat/sessions", endpoint="sessions")
@login_required
def sessions():
    rows = chats.list_for_user(g.user["id"], limit=SIDEBAR_PAGE, before=_parse_cursor(request.args.get("before")))
    return _json({"sessions": [_session_json(row) for row in rows],
                  "next": _cursor(rows[-1]) if len(rows) == SIDEBAR_PAGE else None})


# ----- shared links (no sign-in) -----------------------------------------------------

def _shared_or_404(token: str):
    session = chats.by_share_token(token)
    if session is None:
        abort(404)
    return session


def _public(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/share/<token>", endpoint="shared")
def shared(token):
    session = _shared_or_404(token)
    rows, has_more = chats.message_page(session["id"], before=_int_arg("before"), limit=SHARED_PAGE)
    html = render_template("chat/shared.html", session=session,
                           title=chat_service.display_title(session["title"], g.lang),
                           messages=messages_json(rows, None), has_more=has_more,
                           older_url=url_for("chat.shared", token=token, before=rows[0]["id"])
                           if has_more and rows else None, token=token)
    return _public(current_app.make_response(html))


@bp.get("/share/<token>/download", endpoint="shared_download")
def shared_download(token):
    return _public(_export(_shared_or_404(token)))


# Registered after the routes so the view exists when this runs.
@bp.record_once
def _configure_body_limit(state):
    """The send view accepts uploads up to ``BC_CHAT_MAX_REQUEST_MB``."""
    state.app.view_functions["chat.send"].body_limit = state.app.config["BC"].chat_max_request_bytes
