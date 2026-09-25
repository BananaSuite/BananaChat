"""Chat routes: session management, streaming inference, share links."""

import re
import threading

import config
from flask import (
    render_template, request, redirect, url_for, jsonify, Response, abort,
)

import db
from services.http_capacity import limit_inference
from helpers import login_required, get_current_user
from logger import log_action
from services import queue as q, model_access
from services import chat_files, chat_routing
from services.chat_execution import ChatExecution
from db import _chat_runs as runs
from db._runtime import StreamBusyError


def _stop_stream(session_id: str) -> None:
    db.request_stop_stream(session_id)


def _get_session_or_404(session_id, user):
    sess = db.get_session(session_id)
    if not sess or sess["user_id"] != user["id"] or sess.get("deleted_at"):
        abort(404)
    return sess


def _history_page(history_session_id, endpoint, **route_values):
    cursors = {}
    for key in ("before", "after"):
        value = request.args.get(key)
        if value is not None:
            if not value.isascii() or not value.isdecimal() or len(value) > 19 or not 0 < int(value) < 2 ** 63:
                abort(400)
            cursors[key] = int(value)
    if len(cursors) > 1:
        abort(400)
    messages, older, newer = db.list_message_page(history_session_id, **cursors)
    return messages, {
        "older": url_for(endpoint, **route_values, before=messages[0]["id"]) if older else None,
        "newer": url_for(endpoint, **route_values, after=messages[-1]["id"]) if newer else None,
        "latest": url_for(endpoint, **route_values) if cursors else None,
    }


def _chat_download_response(sess):
    """Avoid a full in-memory copy when downloading a long conversation."""
    def content():
        yield "# " + (sess.get("title") or "Chat") + "\n\n"
        if sess.get("is_incognito"):
            yield "[Incognito session]\n\n"
        for message in db.iter_messages(sess["id"]):
            role = "You" if message["role"] == "user" else (message.get("model_name") or "Assistant")
            yield "### " + role + "\n"
            yield message["content"]
            yield "\n\n"
    safe_title = re.sub(r'[^\w\-]', '_', (sess.get("title") or "chat"))[:40] or "chat"
    return Response(content(), mimetype="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{safe_title}.txt"', "Cache-Control": "no-store",
    })


def register_chat_routes(app):

    @app.route("/chat")
    @login_required
    def chat_index():
        user = get_current_user()
        existing = db.get_latest_empty_session(user["id"])
        if existing:
            return redirect(url_for("chat_session", session_id=existing["id"]))
        sid = db.create_session(user["id"], title="New Chat", is_incognito=False)
        return redirect(url_for("chat_session", session_id=sid))

    @app.route("/chat/new", methods=["POST"])
    @login_required
    def chat_new():
        user = get_current_user()
        incognito = request.form.get("incognito") == "1"
        existing = db.get_latest_empty_session(user["id"], is_incognito=incognito)
        if existing:
            return redirect(url_for("chat_session", session_id=existing["id"]))
        sid = db.create_session(user["id"], title="New Chat", is_incognito=incognito)
        return redirect(url_for("chat_session", session_id=sid))

    @app.route("/chat/<session_id>")
    @login_required
    def chat_session(session_id):
        user = get_current_user()
        sess = _get_session_or_404(session_id, user)
        runs.recover_stale_chat_runs(session_id)
        messages, history = _history_page(session_id, "chat_session", session_id=session_id)
        attachment_rows = [
            dict(item) for item in db.list_session_attachment_metadata(session_id, [m["id"] for m in messages])
        ]
        attachments_by_message = {}
        for item in attachment_rows:
            attachments_by_message.setdefault(item["message_id"], []).append(item)
        sessions = db.list_sessions(user["id"])
        models = model_access.list_accessible_models(user, "chat")
        personality_access = model_access.can_user_use_custom_personalities(user)
        personalities = []
        if personality_access:
            personalities = [
                dict(item) for item in db.list_user_personalities(
                    user["id"], include_disabled=False
                ) if db.personality_is_active(item)
            ]
        return render_template(
            "chat/session.html",
            sess=dict(sess),
            messages=[dict(m) for m in messages],
            history=history,
            sessions=sessions,
            models=models,
            personalities=personalities,
            personality_access=personality_access,
            attachments_by_message=attachments_by_message,
            chat_max_files=config.CHAT_MAX_FILES,
        )

    @app.route("/chat/<session_id>/send", methods=["POST"])
    @login_required
    @limit_inference
    def chat_send(session_id):
        user = get_current_user()
        sess = _get_session_or_404(session_id, user)

        body = (request.get_json(silent=True) or {}) if request.is_json else request.form
        is_incognito = bool(sess["is_incognito"])
        if is_incognito and not request.is_json and any(
            item and item.filename for item in request.files.getlist("files")
        ):
            return jsonify({"error": "Attachments are unavailable in no-history chats."}), 400
        try:
            attachments = chat_files.process_uploads(request.files.getlist("files")) \
                if not request.is_json else []
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not hasattr(body, "get") or not isinstance(body.get("content", ""), str):
            return jsonify({"error": "Content must be text."}), 400
        content = body.get("content", "").strip()
        model_name = body.get("model", "")
        if not content and not attachments:
            return jsonify({"error": "Empty message"}), 400
        if not content:
            content = "Please analyze the attached files."
        if len(content.encode("utf-8")) > 100_000:
            return jsonify({"error": "Message too long (max 100 KB)"}), 400

        is_admin = user["role"] == "admin"
        if attachments and (
            db.get_session_attachment_bytes(session_id) +
            sum(item["size_bytes"] for item in attachments)
        ) > config.CHAT_MAX_SESSION_ATTACHMENT_BYTES:
            return jsonify({"error": "This chat has reached its attachment storage limit."}), 400

        vision_required = any(item["kind"] == "image" for item in attachments) or db.session_has_image_attachments(session_id)

        model_row, error, status, model_notice = chat_routing.select(user, model_name, vision_required)
        if error:
            return jsonify({"error": error}), status
        model_name = model_row["ollama_name"]

        # Chat rate limit (RPM, admin-configurable)
        _settings = db.get_site_settings() or {}
        _chat_rpm = _settings.get("chat_rpm")
        if _chat_rpm and int(_chat_rpm) > 0 and not is_admin:
            _rpm_key = f"chat_rpm:{user['id']}"
            _rpm_ok = db.check_rate_limit(_rpm_key, int(_chat_rpm), 60)
            if not _rpm_ok:
                return jsonify({"error": "Chat rate limit exceeded. Please slow down."}), 429

        # Chat daily credit check (optional hard limit, admin-configurable)
        if not is_admin:
            chat_ok, chat_is_slow, _, _, _, _ = db.check_chat_credits_available(
                user["id"], role=user["role"]
            )
            if not chat_ok:
                return jsonify({"error": "Daily chat limit reached. Try again tomorrow."}), 429

        stop_event = threading.Event()
        priority = q.PRIORITY_ADMIN if is_admin else (q.PRIORITY_SLOW if chat_is_slow else q.PRIORITY_CHAT)
        try:
            slot = q.acquire(priority=priority, timeout=120, stop_ev=stop_event)
        except q.QueueFullError as exc:
            return jsonify({"error": str(exc)}), 429, {"Retry-After": "5"}
        try:
            token = runs.begin_chat_run(session_id, user, content, attachments)
        except StreamBusyError as exc:
            slot.close()
            return jsonify({"error": str(exc)}), 409, {"Retry-After": "2"}
        except runs.ChatLimitError as exc:
            slot.close()
            return jsonify({"error": str(exc)}), 429
        except ValueError as exc:
            slot.close()
            return jsonify({"error": str(exc)}), 400
        except BaseException:
            slot.close()
            raise
        execution = ChatExecution(
            session_id=session_id, token=token, user=user, content=content,
            model=model_row, body=body, vision_required=vision_required,
            notice=model_notice, slot=slot, stop_event=stop_event,
        )
        execution.start()
        log_action(
            "chat_incognito_send" if is_incognito else "chat_send",
            request, user=user, model=model_name, session=session_id,
        )
        response = Response(execution.channel.stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"})
        response.call_on_close(execution.channel.detach)
        return response

    @app.route("/chat/<session_id>/personality", methods=["POST"])
    @login_required
    def chat_personality(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        raw_id = (request.get_json(silent=True) or {}).get("personality_id")
        if raw_id in (None, "", 0, "0"):
            db.set_session_personality(session_id, user["id"], None)
            return jsonify({"ok": True})
        try:
            personality_id = int(raw_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid personality."}), 400
        if not model_access.get_usable_personality(user, personality_id):
            return jsonify({"error": "Personality is unavailable."}), 403
        db.set_session_personality(session_id, user["id"], personality_id)
        return jsonify({"ok": True})

    @app.route("/chat/<session_id>/attachments/<attachment_id>")
    @login_required
    def chat_attachment(session_id, attachment_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        attachment = db.get_attachment(attachment_id)
        if not attachment or attachment["session_id"] != session_id \
                or attachment["user_id"] != user["id"] or attachment["kind"] != "image":
            abort(404)
        return Response(
            attachment["image_data"], mimetype=attachment["media_type"],
            headers={"Cache-Control": "private, no-store"},
        )

    @app.route("/chat/<session_id>/messages/<int:message_id>/pdf")
    @login_required
    def chat_message_pdf(session_id, message_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        message = db.get_message(message_id)
        if not message or message["session_id"] != session_id \
                or message["user_id"] != user["id"] or message["role"] != "assistant":
            abort(404)
        return Response(
            chat_files.render_text_pdf(message["content"]), mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="response-{message_id}.pdf"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.route("/chat/<session_id>/title", methods=["POST"])
    @login_required
    def chat_rename(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        title = (request.json or {}).get("title", "").strip()[:100]
        if title:
            db.update_session_title(session_id, title)
        return jsonify({"ok": True})

    @app.route("/chat/<session_id>/status")
    @login_required
    def chat_status(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        status = runs.get_chat_run_status(session_id)
        last_msg = status.get("last_message")
        return jsonify({
            "generating": bool(status.get("owner_token")),
            "state": status.get("state", "idle"),
            "error": status.get("error", ""),
            "stopping": bool(status.get("stop_requested")),
            "last_role": last_msg["role"] if last_msg else None,
            "message_count": status.get("message_count", 0),
            "last_message": last_msg,
            "partial_content": status.get("partial_content"),
        })

    @app.route("/chat/<session_id>/stop", methods=["POST"])
    @login_required
    def chat_stop(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        _stop_stream(session_id)
        return jsonify({"ok": True})

    @app.route("/chat/<session_id>/incognito-close", methods=["POST"])
    @login_required
    def chat_incognito_close(session_id):
        user = get_current_user()
        sess = db.get_session(session_id)
        if (sess and sess["user_id"] == user["id"]
                and sess.get("is_incognito") and not sess.get("deleted_at")):
            db.close_incognito_session(session_id, user["id"])
        return "", 204

    @app.route("/chat/<session_id>/delete", methods=["POST"])
    @login_required
    def chat_delete(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        _stop_stream(session_id)
        db.delete_session(session_id, user["id"])
        return jsonify({"ok": True})

    @app.route("/chat/<session_id>/share", methods=["POST"])
    @login_required
    def chat_share(session_id):
        user = get_current_user()
        _get_session_or_404(session_id, user)
        action = (request.json or {}).get("action", "create")
        if action == "create":
            try:
                token = db.create_share_token(session_id, user["id"])
                return jsonify({"ok": True, "token": token})
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        else:
            db.revoke_share_token(session_id, user["id"])
            return jsonify({"ok": True})

    @app.route("/chat/<session_id>/download")
    @login_required
    def chat_download(session_id):
        user = get_current_user()
        sess = _get_session_or_404(session_id, user)
        return _chat_download_response(dict(sess))

    @app.route("/chat/search")
    @login_required
    def chat_search():
        user = get_current_user()
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify({"results": []})
        results = db.search_sessions(user["id"], query)
        return jsonify({
            "results": [{"id": r["id"], "title": r["title"] or "New Chat"} for r in results]
        })

    @app.route("/share/<token>")
    def chat_shared(token):
        sess = db.get_session_by_share_token(token)
        if not sess:
            abort(404)
        messages, history = _history_page(sess["id"], "chat_shared", token=token)
        return render_template(
            "chat/shared.html",
            sess=dict(sess),
            messages=[dict(m) for m in messages],
            history=history,
            share_token=token,
        )

    @app.route("/share/<token>/download")
    def chat_shared_download(token):
        sess = db.get_session_by_share_token(token)
        if not sess:
            abort(404)
        return _chat_download_response(dict(sess))
