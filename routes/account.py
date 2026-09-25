"""Account management: profile, password, quota requests, deletion."""

import json
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash, Response, session

import db
from helpers import login_required, get_current_user, MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from helpers._passwords import generate_password_hash, check_password_hash
from logger import log_action


def register_account_routes(app):

    @app.route("/account")
    @login_required
    def account():
        user = get_current_user()
        from routes.access import get_account_access_policies

        quota = db.get_user_quota(user["id"])
        reg_used, slow_used = db.get_today_usage(user["id"], pool="api")
        pending_request = db.get_pending_quota_request(user["id"])
        quota_requests = db.list_user_quota_requests(user["id"])

        # Chat quota (only when chat daily limits are enabled)
        settings = db.get_site_settings() or {}
        chat_limit_enabled = bool(settings.get("chat_daily_limit_enabled"))
        chat_reg_used = chat_slow_used = 0
        chat_reg_limit = chat_slow_limit = 0
        if chat_limit_enabled:
            chat_ok, _, chat_reg_used, chat_slow_used, chat_reg_limit, chat_slow_limit = \
                db.check_chat_credits_available(user["id"], role=user["role"])

        return render_template(
            "account/index.html",
            user=dict(user),
            quota=dict(quota),
            reg_used=reg_used,
            slow_used=slow_used,
            pending_request=dict(pending_request) if pending_request else None,
            quota_requests=[dict(row) for row in quota_requests],
            chat_limit_enabled=chat_limit_enabled,
            chat_reg_used=chat_reg_used,
            chat_slow_used=chat_slow_used,
            chat_reg_limit=chat_reg_limit,
            chat_slow_limit=chat_slow_limit,
            access_policies=get_account_access_policies(user),
        )

    @app.route("/account/password", methods=["POST"])
    @login_required
    def account_change_password():
        user = get_current_user()
        current = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not check_password_hash(user["password"], current):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("account"))
        if len(new_pw) < MIN_PASSWORD_LENGTH:
            flash(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
            return redirect(url_for("account"))
        if len(new_pw) > MAX_PASSWORD_LENGTH:
            flash("Password is too long.", "error")
            return redirect(url_for("account"))
        if new_pw != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("account"))

        db.change_password(user["id"], generate_password_hash(new_pw))
        session["session_version"] = db.get_user_by_id(user["id"])["session_version"]
        log_action("change_password", request, user=user, user_id=user["id"])
        flash("Password changed successfully.", "success")
        return redirect(url_for("account"))

    @app.route("/account/delete", methods=["POST"])
    @login_required
    def account_delete():
        user = get_current_user()
        confirm = request.form.get("confirm_username", "").strip()
        password = request.form.get("password", "")

        if confirm != user["username"]:
            flash("Username confirmation does not match.", "error")
            return redirect(url_for("account"))
        if not check_password_hash(user["password"], password):
            flash("Password is incorrect.", "error")
            return redirect(url_for("account"))
        if user["role"] == "admin" and db.count_admins() <= 1:
            flash("Cannot delete the last admin account.", "error")
            return redirect(url_for("account"))

        uid = user["id"]
        session.clear()
        db.delete_user(uid)
        log_action("delete_account", request, user=user, user_id=uid)
        flash("Your account has been deleted.", "info")
        return redirect(url_for("login"))

    @app.route("/account/export/gdpr")
    @login_required
    def account_export_gdpr():
        user = get_current_user()
        uid = user["id"]

        data = db.collect_user_gdpr_data(uid)
        payload = {
            "export_type": "gdpr_data_export",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "created_at": user["created_at"],
                "last_login_at": user["last_login_at"],
            },
            **data,
        }

        filename = f"bananachat_gdpr_{user['username']}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
        log_action("export_gdpr", request, user=user, user_id=uid)
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/account/export/chats")
    @login_required
    def account_export_chats():
        user = get_current_user()
        uid = user["id"]

        sessions = db.list_user_sessions_for_export(uid, include_deleted=False)
        chats = []
        for sess in sessions:
            messages = db.list_messages(sess["id"])
            chats.append({
                "id": sess["id"],
                "title": sess["title"],
                "created_at": sess["created_at"],
                "updated_at": sess["updated_at"],
                "messages": [
                    {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                    for m in messages
                ],
            })

        payload = {
            "export_type": "chats_export",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "username": user["username"],
            "chats": chats,
        }

        filename = f"bananachat_chats_{user['username']}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
        log_action("export_chats", request, user=user, user_id=uid)
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/account/chats/delete-all", methods=["POST"])
    @login_required
    def account_delete_all_chats():
        user = get_current_user()
        password = request.form.get("password", "")

        if not check_password_hash(user["password"], password):
            flash("Password is incorrect.", "error")
            return redirect(url_for("account"))

        db.delete_all_user_sessions(user["id"])
        log_action("delete_all_chats", request, user=user, user_id=user["id"])
        flash("All your chats have been deleted.", "success")
        return redirect(url_for("chat_index"))

    @app.route("/account/quota-request", methods=["POST"])
    @login_required
    def account_quota_request():
        user = get_current_user()

        try:
            new_credits = int(request.form.get("new_credits", 0))
            new_slow = int(request.form.get("new_slow_credits", 0))
        except (ValueError, TypeError):
            flash("Invalid credit values.", "error")
            return redirect(url_for("account"))

        reason = request.form.get("reason", "").strip()[:1000]
        duration_type = request.form.get("duration_type", "permanent")
        if duration_type not in ("one_day", "permanent"):
            flash("Invalid quota request duration.", "error")
            return redirect(url_for("account"))

        if new_credits < 1 or new_credits > 10000 or new_slow < 0 or new_slow > 10000:
            flash("Invalid credit values.", "error")
            return redirect(url_for("account"))
        if not reason:
            flash("A reason is required.", "error")
            return redirect(url_for("account"))

        try:
            outcome = db.submit_quota_request(
                user["id"], new_credits, new_slow, reason, duration_type
            )
        except ValueError as exc:
            flash(str(exc) + ".", "error")
            return redirect(url_for("account"))
        log_action("submit_quota_request", request, user=user, user_id=user["id"])
        if outcome["status"] == "approved":
            flash(outcome["admin_message"], "success")
        elif duration_type == "one_day":
            flash("One-day quota request submitted. It always awaits admin review.", "success")
        else:
            flash("Quota increase request submitted. An admin will review it.", "success")
        return redirect(url_for("account"))
