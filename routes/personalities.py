"""User personality editor and administrator moderation."""

from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, url_for

import db
from helpers import admin_required, get_current_user, login_required
from logger import log_action
from services import model_access


def _personality_id(values):
    try:
        value = int(values.get("personality_id", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid personality.") from exc
    if value <= 0:
        raise ValueError("Invalid personality.")
    return value


def _owned_personality(values, user):
    personality = db.get_personality(_personality_id(values))
    if not personality or personality["user_id"] != user["id"]:
        raise ValueError("Personality not found.")
    return personality


def _parse_until(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid disable-until date.") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if value <= datetime.now(timezone.utc):
        raise ValueError("Disable-until date must be in the future.")
    return value.astimezone(timezone.utc).isoformat()


def register_personality_routes(app):

    @app.route("/personalities")
    @login_required
    def personalities():
        user = get_current_user()
        allowed = model_access.can_user_use_custom_personalities(user)
        rows = [dict(row) for row in db.list_user_personalities(user["id"])]
        for row in rows:
            row["active"] = db.personality_is_active(row)
        return render_template(
            "personalities/index.html", personalities=rows, access_allowed=allowed,
            max_name=db.MAX_PERSONALITY_NAME,
            max_instructions=db.MAX_PERSONALITY_INSTRUCTIONS,
            max_personalities=db.MAX_PERSONALITIES_PER_USER,
        )

    @app.route("/personalities/create", methods=["POST"])
    @login_required
    def personality_create():
        user = get_current_user()
        if not model_access.can_user_use_custom_personalities(user):
            flash("You do not currently have access to custom personalities.", "error")
            return redirect(url_for("personalities"))
        try:
            personality_id = db.create_personality(
                user["id"], request.form.get("name"), request.form.get("instructions"),
                created_by=user["id"], enabled=request.form.get("enabled") == "1",
            )
            log_action(
                "create_personality", request, user=user,
                personality_id=personality_id,
            )
            flash("Personality created.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("personalities"))

    @app.route("/personalities/update", methods=["POST"])
    @login_required
    def personality_update():
        user = get_current_user()
        try:
            personality = _owned_personality(request.form, user)
            wants_enabled = request.form.get("enabled") == "1"
            if not model_access.can_user_use_custom_personalities(user):
                if wants_enabled or request.form.get("name") != personality["name"] \
                        or request.form.get("instructions") != personality["instructions"]:
                    raise ValueError("Access is disabled. You may only disable or delete saved personalities.")
            db.update_personality(
                personality["id"], request.form.get("name"),
                request.form.get("instructions"), user["id"], enabled=wants_enabled,
            )
            log_action(
                "update_personality", request, user=user,
                personality_id=personality["id"], enabled=wants_enabled,
            )
            flash("Personality updated.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("personalities"))

    @app.route("/personalities/delete", methods=["POST"])
    @login_required
    def personality_delete():
        user = get_current_user()
        try:
            personality = _owned_personality(request.form, user)
            db.delete_personality(personality["id"])
            log_action(
                "delete_personality", request, user=user,
                personality_id=personality["id"],
            )
            flash("Personality deleted.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("personalities"))

    @app.route("/admin/personalities", methods=["GET", "POST"])
    @admin_required
    def admin_personalities():
        admin = get_current_user()
        if request.method == "POST":
            action = (request.form.get("action") or "").strip()
            try:
                if action == "create":
                    target = db.get_user_by_username(
                        (request.form.get("username") or "").strip()
                    )
                    if not target:
                        raise ValueError("User not found.")
                    personality_id = db.create_personality(
                        target["id"], request.form.get("name"),
                        request.form.get("instructions"), created_by=admin["id"],
                        enabled=request.form.get("enabled") == "1",
                    )
                    target_user_id = target["id"]
                else:
                    personality_id = _personality_id(request.form)
                    personality = db.get_personality(personality_id)
                    if not personality:
                        raise ValueError("Personality not found.")
                    target_user_id = personality["user_id"]
                    if action == "update":
                        db.update_personality(
                            personality_id, request.form.get("name"),
                            request.form.get("instructions"), admin["id"],
                            enabled=request.form.get("enabled") == "1",
                        )
                    elif action == "moderate":
                        disabled = request.form.get("disabled") == "1"
                        db.set_personality_moderation(
                            personality_id, disabled, admin["id"],
                            disabled_until=_parse_until(request.form.get("disabled_until"))
                            if disabled else None,
                            reason=(request.form.get("reason") or "")[:1000],
                        )
                    elif action == "delete":
                        db.delete_personality(personality_id)
                    else:
                        raise ValueError("Invalid personality action.")
                log_action(
                    f"admin_{action}_personality", request, user=admin,
                    personality_id=personality_id, target_user_id=target_user_id,
                )
                flash("Personality action completed.", "success")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_personalities"))

        rows = [dict(row) for row in db.list_all_personalities()]
        for row in rows:
            row["active"] = db.personality_is_active(row)
        return render_template(
            "admin/personalities.html", personalities=rows,
            max_name=db.MAX_PERSONALITY_NAME,
            max_instructions=db.MAX_PERSONALITY_INSTRUCTIONS,
        )
