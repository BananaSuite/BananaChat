"""Music program routes: user opt-in/out, admin management, track upload."""

import os
import uuid

from flask import render_template, request, redirect, url_for, flash, jsonify, send_from_directory

import config

import db
from helpers import login_required, admin_required, get_current_user
from logger import log_action

_ALLOWED_AUDIO_EXT = {".mp3", ".ogg", ".wav", ".m4a"}
_MAX_TRACK_SIZE_MB = 50


def _audio_dir():
    return os.path.join(config.INSTANCE_DIR, "audio")


def register_music_routes(app):

    @app.route("/music/audio/<filename>")
    @login_required
    def music_audio(filename):
        return send_from_directory(_audio_dir(), filename)

    @app.route("/free-quota")
    @login_required
    def music_free_quota():
        user = get_current_user()
        music = db.get_music_settings()

        if not music["music_enabled"] or not music["music_visible"]:
            flash("The Music Program is not currently available.", "info")
            return redirect(url_for("chat_index"))

        opted_in = db.is_user_music_opted_in(user["id"])
        forced = db.is_user_music_forced(user["id"])

        # Compute effective quota with multiplier for display
        quota = db.get_user_quota(user["id"])
        multiplier = db.get_effective_quota_multiplier(user["id"])

        return render_template(
            "music/free_quota.html",
            music=music,
            opted_in=opted_in,
            forced=forced,
            quota=dict(quota),
            multiplier=multiplier,
        )

    @app.route("/free-quota/opt-in", methods=["POST"])
    @login_required
    def music_opt_in():
        user = get_current_user()
        music = db.get_music_settings()

        if not music["music_enabled"]:
            flash("The Music Program is not currently available.", "error")
            return redirect(url_for("chat_index"))
        if not music["music_opt_in_allowed"]:
            flash("Opt-ins are temporarily suspended.", "error")
            return redirect(url_for("music_free_quota"))
        if db.is_user_music_opted_in(user["id"]):
            flash("You are already opted in.", "info")
            return redirect(url_for("music_free_quota"))

        db.set_user_music_opt_in(user["id"], True)
        log_action("music_opt_in", request, user=user)
        flash("You are now enrolled in the Music Program!", "success")
        return redirect(url_for("music_free_quota"))

    @app.route("/free-quota/opt-out", methods=["POST"])
    @login_required
    def music_opt_out():
        user = get_current_user()
        music = db.get_music_settings()

        if not music["music_enabled"]:
            flash("The Music Program is not currently available.", "error")
            return redirect(url_for("chat_index"))
        if not music["music_opt_out_allowed"]:
            flash("Opt-outs are temporarily suspended.", "error")
            return redirect(url_for("music_free_quota"))
        if db.is_user_music_forced(user["id"]):
            flash("An administrator enrolled you. You cannot opt out.", "error")
            return redirect(url_for("music_free_quota"))
        if not db.is_user_music_opted_in(user["id"]):
            flash("You are not opted in.", "info")
            return redirect(url_for("music_free_quota"))

        db.set_user_music_opt_in(user["id"], False)
        log_action("music_opt_out", request, user=user)
        flash("You have left the Music Program.", "info")
        return redirect(url_for("music_free_quota"))

    @app.route("/admin/music")
    @admin_required
    def admin_music():
        music = db.get_music_settings()
        opted_in_count = db.count_music_opted_in()
        users = db.list_users(limit=500)
        tracks = db.list_music_tracks()
        return render_template(
            "admin/music.html",
            music=music,
            opted_in_count=opted_in_count,
            users=[dict(u) for u in users],
            tracks=[dict(t) for t in tracks],
        )

    @app.route("/admin/music/settings", methods=["POST"])
    @admin_required
    def admin_music_settings():
        music_enabled = 1 if request.form.get("music_enabled") else 0
        music_visible = 1 if request.form.get("music_visible") else 0
        music_opt_in_allowed = 1 if request.form.get("music_opt_in_allowed") else 0
        music_opt_out_allowed = 1 if request.form.get("music_opt_out_allowed") else 0

        playback_mode = request.form.get("music_playback_mode", "sequential")
        if playback_mode not in ("sequential", "shuffle"):
            playback_mode = "sequential"

        try:
            multiplier = float(request.form.get("music_credit_multiplier", 2.0))
            multiplier = max(1.0, min(10.0, multiplier))
        except (ValueError, TypeError):
            multiplier = 2.0

        db.update_music_settings(
            music_enabled=music_enabled,
            music_visible=music_visible,
            music_opt_in_allowed=music_opt_in_allowed,
            music_opt_out_allowed=music_opt_out_allowed,
            music_credit_multiplier=multiplier,
            music_playback_mode=playback_mode,
        )
        log_action("admin_music_settings", request, user=get_current_user())
        flash("Music program settings saved.", "success")
        return redirect(url_for("admin_music"))

    # ── Track management ──

    @app.route("/admin/music/tracks/upload", methods=["POST"])
    @admin_required
    def admin_music_upload_track():
        admin = get_current_user()
        uploaded = request.files.get("track_file")
        if not uploaded or not uploaded.filename:
            flash("No file selected.", "error")
            return redirect(url_for("admin_music"))

        # Validate extension
        original = uploaded.filename.strip()
        ext = os.path.splitext(original)[1].lower()
        if ext not in _ALLOWED_AUDIO_EXT:
            flash(f"Invalid file type. Allowed: {', '.join(sorted(_ALLOWED_AUDIO_EXT))}", "error")
            return redirect(url_for("admin_music"))

        # Validate size (read limit)
        uploaded.seek(0, os.SEEK_END)
        size = uploaded.tell()
        uploaded.seek(0)
        if size > _MAX_TRACK_SIZE_MB * 1024 * 1024:
            flash(f"File too large. Maximum: {_MAX_TRACK_SIZE_MB} MB.", "error")
            return redirect(url_for("admin_music"))

        display_name = os.path.splitext(original)[0][:200].strip() or "Untitled"

        # Save with a UUID filename to avoid collisions
        safe_filename = f"{uuid.uuid4().hex}{ext}"
        audio_path = _audio_dir()
        os.makedirs(audio_path, exist_ok=True)
        dest = os.path.join(audio_path, safe_filename)
        uploaded.save(dest)

        db.add_music_track(safe_filename, display_name, uploaded_by=admin["id"])
        log_action("admin_music_upload_track", request, user=admin)
        flash(f"Track '{display_name}' uploaded.", "success")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/tracks/<int:track_id>/rename", methods=["POST"])
    @admin_required
    def admin_music_rename_track(track_id):
        track = db.get_music_track(track_id)
        if not track:
            flash("Track not found.", "error")
            return redirect(url_for("admin_music"))
        new_name = request.form.get("display_name", "").strip()[:200]
        if not new_name:
            flash("Name cannot be empty.", "error")
            return redirect(url_for("admin_music"))
        db.rename_music_track(track_id, new_name)
        flash(f"Track renamed to '{new_name}'.", "success")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/tracks/<int:track_id>/delete", methods=["POST"])
    @admin_required
    def admin_music_delete_track(track_id):
        filename = db.delete_music_track(track_id)
        if filename:
            path = os.path.join(_audio_dir(), filename)
            try:
                os.unlink(path)
            except OSError:
                pass
            log_action("admin_music_delete_track", request, user=get_current_user())
            flash("Track deleted.", "info")
        else:
            flash("Track not found.", "error")
        return redirect(url_for("admin_music"))

    @app.route("/admin/api/music/tracks/reorder", methods=["POST"])
    @admin_required
    def admin_api_music_reorder_tracks():
        data = request.get_json(silent=True) or {}
        ordered_ids = data.get("ids", [])
        if not isinstance(ordered_ids, list):
            return jsonify({"ok": False, "error": "Invalid data"}), 400
        try:
            ordered_ids = [int(x) for x in ordered_ids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid IDs"}), 400
        db.reorder_music_tracks(ordered_ids)
        return jsonify({"ok": True})

    # ── Per-user management ──

    @app.route("/admin/music/user/<user_id>/force-opt-in", methods=["POST"])
    @admin_required
    def admin_music_force_opt_in(user_id):
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_music"))
        db.set_user_music_opt_in(user_id, True, forced=True)
        log_action("admin_music_force_opt_in", request, user=get_current_user(),
                   target_user_id=user_id)
        flash(f"'{target['username']}' force-opted into the Music Program.", "success")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/user/<user_id>/force-opt-out", methods=["POST"])
    @admin_required
    def admin_music_force_opt_out(user_id):
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_music"))
        db.set_user_music_opt_in(user_id, False, forced=False)
        log_action("admin_music_force_opt_out", request, user=get_current_user(),
                   target_user_id=user_id)
        flash(f"'{target['username']}' force-opted out of the Music Program.", "info")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/opt-in-all", methods=["POST"])
    @admin_required
    def admin_music_opt_in_all():
        count = db.opt_in_all_users()
        log_action("admin_music_opt_in_all", request, user=get_current_user())
        flash(f"Opted in {count} user(s).", "success")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/opt-out-all", methods=["POST"])
    @admin_required
    def admin_music_opt_out_all():
        count = db.opt_out_all_users()
        log_action("admin_music_opt_out_all", request, user=get_current_user())
        flash(f"Opted out {count} user(s).", "info")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/hide", methods=["POST"])
    @admin_required
    def admin_music_hide():
        """Hide the program from users but keep current opt-ins active."""
        db.update_music_settings(music_visible=0)
        log_action("admin_music_hide", request, user=get_current_user())
        flash("Music program hidden from users. Current opt-ins remain active.", "info")
        return redirect(url_for("admin_music"))

    @app.route("/admin/music/remove", methods=["POST"])
    @admin_required
    def admin_music_remove():
        """Fully remove the program: disable, opt everyone out, reset flags."""
        db.remove_program()
        log_action("admin_music_remove", request, user=get_current_user())
        flash("Music program removed. All users opted out, settings reset.", "info")
        return redirect(url_for("admin_music"))
