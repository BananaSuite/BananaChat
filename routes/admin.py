"""Admin panel routes: users, model catalog, categories, quota requests, metrics, audit."""

import json
import io
import csv
import re
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash, jsonify, Response, abort

import config
import db
from helpers import admin_required, get_current_user
from services import checkpoint_agent, comfyui, model_recovery, ollama
from logger import log_action


def _format_memory_mb(value):
    if value is None:
        return "-"
    value = float(value)
    if value >= 1024:
        return f"{value / 1024:.1f} GB"
    return f"{value:.0f} MB"


def _decorate_snapshot(snapshot, relative_max_mb=None):
    if not snapshot:
        return None
    item = dict(snapshot)
    used = item.get("gpu_memory_used_mb")
    total = item.get("gpu_memory_total_mb")
    item["gpu_memory_used_display"] = _format_memory_mb(used)
    item["gpu_memory_total_display"] = _format_memory_mb(total)
    item["system_memory_used_display"] = _format_memory_mb(item.get("system_memory_used_mb"))
    item["system_memory_total_display"] = _format_memory_mb(item.get("system_memory_total_mb"))
    item["gpu_percent"] = (
        max(0.0, min(100.0, float(used) / float(total) * 100.0))
        if used is not None and total not in (None, 0) else None
    )
    item["gpu_relative_percent"] = (
        max(2.0, min(100.0, float(used) / float(relative_max_mb) * 100.0))
        if used is not None and relative_max_mb not in (None, 0) else 0
    )
    source = item.get("metrics_source")
    item["source_label"] = {
        "nvidia-smi": "NVIDIA hardware telemetry",
        "ollama-allocation": "Ollama model allocation",
        "unavailable": "GPU telemetry unavailable",
        "legacy-system-ram": "Legacy RAM sample (migrated)",
    }.get(source, source or "Unknown")
    try:
        recorded = datetime.fromisoformat(str(item.get("recorded_at")).replace("Z", "+00:00"))
        item["stale"] = (
            datetime.now(timezone.utc) - recorded
        ).total_seconds() > max(90, config.COMPUTE_SNAPSHOT_INTERVAL * 3)
    except (TypeError, ValueError):
        item["stale"] = True
    return item


def register_admin_routes(app):

    @app.route("/admin")
    @admin_required
    def admin_index():
        stats = {
            "user_count": db.count_users(),
            "model_count": len(db.list_models()),
            "rolled_out_count": len(db.list_models(rolled_out_only=True)),
        }
        latest = _decorate_snapshot(db.get_latest_snapshot())
        req_stats = db.get_request_stats(hours=24)
        return render_template(
            "admin/index.html",
            stats=stats,
            latest_snapshot=latest,
            req_stats=[dict(r) for r in req_stats],
            db_health=db.get_database_health(),
            model_recovery=model_recovery.read(),
        )

    _USERS_PAGE_SIZE = 50

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        page = max(1, request.args.get("page", 1, type=int))
        offset = (page - 1) * _USERS_PAGE_SIZE
        users = db.list_users(limit=_USERS_PAGE_SIZE, offset=offset)
        total = db.count_users()
        total_pages = max(1, (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE)
        return render_template(
            "admin/users.html",
            users=[dict(u) for u in users],
            page=page,
            total_pages=total_pages,
            total=total,
        )

    @app.route("/admin/users/create", methods=["GET", "POST"])
    @admin_required
    def admin_create_user():
        if request.method == "POST":
            from helpers._passwords import generate_password_hash
            from helpers import _is_valid_username, MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "user")
            if role not in ("user", "admin"):
                role = "user"
            if not _is_valid_username(username):
                flash("Invalid username.", "error")
                return render_template("admin/create_user.html"), 400
            if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
                flash(f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} chars.", "error")
                return render_template("admin/create_user.html"), 400
            if db.get_user_by_username(username):
                flash("Username already taken.", "error")
                return render_template("admin/create_user.html"), 409
            try:
                uid = db.create_user(username, generate_password_hash(password), role=role)
                log_action("admin_create_user", request, user=get_current_user(), target_user_id=uid)
                flash(f"User '{username}' created.", "success")
                return redirect(url_for("admin_users"))
            except Exception as exc:
                flash(f"Error: {exc}", "error")
        return render_template("admin/create_user.html")

    @app.route("/admin/users/<user_id>/set-role", methods=["POST"])
    @admin_required
    def admin_set_role(user_id):
        admin = get_current_user()
        if user_id == admin["id"]:
            flash("Cannot change your own role.", "error")
            return redirect(url_for("admin_users"))
        new_role = request.form.get("role", "user")
        if new_role not in ("user", "admin"):
            flash("Invalid role.", "error")
            return redirect(url_for("admin_users"))
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        if new_role == "user" and target["role"] == "admin" and db.count_admins() <= 1:
            flash("Cannot demote the last admin.", "error")
            return redirect(url_for("admin_users"))
        db.set_user_role(user_id, new_role)
        log_action("admin_set_role", request, user=admin, target_user_id=user_id)
        flash(f"'{target['username']}' is now {new_role}.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<user_id>/reset-password", methods=["POST"])
    @admin_required
    def admin_reset_password(user_id):
        from helpers._passwords import generate_password_hash
        from helpers import MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH
        admin = get_current_user()
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        new_pw = request.form.get("new_password", "")
        if len(new_pw) < MIN_PASSWORD_LENGTH or len(new_pw) > MAX_PASSWORD_LENGTH:
            flash(f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.", "error")
            return redirect(url_for("admin_users"))
        db.change_password(user_id, generate_password_hash(new_pw))
        log_action("admin_reset_password", request, user=admin, target_user_id=user_id)
        flash(f"Password for '{target['username']}' has been reset.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<user_id>/suspend", methods=["POST"])
    @admin_required
    def admin_suspend_user(user_id):
        admin = get_current_user()
        if user_id == admin["id"]:
            flash("Cannot suspend yourself.", "error")
            return redirect(url_for("admin_users"))
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        db.suspend_user(user_id)
        log_action("admin_suspend", request, user=admin, target_user_id=user_id)
        flash(f"'{target['username']}' suspended.", "info")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<user_id>/unsuspend", methods=["POST"])
    @admin_required
    def admin_unsuspend_user(user_id):
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        db.unsuspend_user(user_id)
        log_action("admin_unsuspend", request, user=get_current_user(), target_user_id=user_id)
        flash(f"'{target['username']}' unsuspended.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        admin = get_current_user()
        if user_id == admin["id"]:
            flash("Cannot delete yourself.", "error")
            return redirect(url_for("admin_users"))
        target = db.get_user_by_id(user_id)
        if not target:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and db.count_admins() <= 1:
            flash("Cannot delete the last admin account.", "error")
            return redirect(url_for("admin_users"))
        db.delete_user(user_id)
        log_action("admin_delete_user", request, user=admin, target_user_id=user_id)
        flash(f"'{target['username']}' deleted.", "info")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<user_id>/export/gdpr")
    @admin_required
    def admin_export_user_gdpr(user_id):
        admin = get_current_user()
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)

        data = db.collect_user_gdpr_data(user_id)
        payload = {
            "export_type": "gdpr_data_export",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "exported_by_admin": admin["username"],
            "user": {
                "id": target["id"],
                "username": target["username"],
                "role": target["role"],
                "created_at": target["created_at"],
                "last_login_at": target["last_login_at"],
            },
            **data,
        }

        filename = f"bananachat_gdpr_{target['username']}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
        log_action("admin_export_user_gdpr", request, user=admin, target_user_id=user_id)
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/admin/users/<user_id>/export/chats")
    @admin_required
    def admin_export_user_chats(user_id):
        admin = get_current_user()
        target = db.get_user_by_id(user_id)
        if not target:
            abort(404)

        sessions = db.list_user_sessions_for_export(user_id, include_deleted=False)
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
            "exported_by_admin": admin["username"],
            "username": target["username"],
            "chats": chats,
        }

        filename = f"bananachat_chats_{target['username']}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
        log_action("admin_export_user_chats", request, user=admin, target_user_id=user_id)
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/admin/users/<user_id>/quota", methods=["POST"])
    @admin_required
    def admin_set_quota(user_id):
        admin = get_current_user()
        if not db.get_user_by_id(user_id):
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        try:
            credits = int(request.form.get("daily_credits", 30))
            slow = int(request.form.get("daily_slow_credits", 15))
        except (ValueError, TypeError):
            flash("Invalid values.", "error")
            return redirect(url_for("admin_users"))
        if credits < 0 or credits > 100000 or slow < 0 or slow > 100000:
            flash("Credit values must be between 0 and 100,000.", "error")
            return redirect(url_for("admin_users"))
        db.set_user_quota(user_id, credits, slow, updated_by=admin["id"])
        flash("Quota updated.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/invites")
    @admin_required
    def admin_invites():
        invites = db.list_invite_codes(active_only=True)
        expired = db.list_expired_codes()
        return render_template(
            "admin/invites.html",
            invites=[dict(i) for i in invites],
            expired=[dict(i) for i in expired],
        )

    @app.route("/admin/invites/create", methods=["POST"])
    @admin_required
    def admin_invite_create():
        admin = get_current_user()
        try:
            max_uses = int(request.form.get("max_uses", 1))
        except (ValueError, TypeError):
            max_uses = 1
        assigned_role = request.form.get("assigned_role", "user")
        if assigned_role not in ("user", "admin"):
            assigned_role = "user"
        custom_code = request.form.get("custom_code", "").strip() or None
        try:
            code = db.generate_invite_code(
                admin["id"], max_uses=max_uses,
                assigned_role=assigned_role, custom_code=custom_code
            )
            flash(f"Invite code created: {code}", "success")
        except Exception as exc:
            flash(f"Error: {exc}", "error")
        return redirect(url_for("admin_invites"))

    @app.route("/admin/invites/<int:code_id>/delete", methods=["POST"])
    @admin_required
    def admin_invite_delete(code_id):
        db.delete_invite_code(code_id)
        flash("Invite code deleted.", "info")
        return redirect(url_for("admin_invites"))

    # Catalog pages, plus the pull queue that stocks them.
    @app.route("/admin/models")
    @admin_required
    def admin_models():
        models = db.list_models_with_categories()
        categories = db.list_categories()
        recovery = model_recovery.read()
        return render_template(
            "admin/models.html",
            models=models,
            categories=[dict(c) for c in categories],
            checkpoint_agent_configured=checkpoint_agent.is_configured(),
            model_recovery=recovery,
            recovery_choices=model_recovery.choices(recovery) if recovery else [],
        )

    @app.route("/admin/models/recovery", methods=["POST"])
    @admin_required
    def admin_model_recovery():
        try:
            result = model_recovery.decide(request.form.get("action"), request.form.getlist("models"), get_current_user()["id"])
            if result["deferred"]:
                flash("Model downloads deferred. Your chats remain available; choose models whenever you are ready.", "success")
            else:
                flash(f"Queued {len(result['queued'])} download(s); {len(result['skipped'])} model(s) are already installed.", "success")
                for error in result["errors"]:
                    flash(f"{error['model']}: {error['reason']}", "error")
            log_action("admin_model_recovery", request, user=get_current_user(), decision=request.form.get("action"))
        except (ValueError, RuntimeError, OSError) as error:
            flash(str(error), "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/sync", methods=["POST"])
    @admin_required
    def admin_models_sync():
        try:
            synced = ollama.sync_models()
            flash(f"Synced {len(synced)} model(s) from Ollama.", "success")
        except Exception as exc:
            flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/sync-comfyui", methods=["POST"])
    @admin_required
    def admin_models_sync_comfyui():
        if not comfyui.is_enabled():
            flash("ComfyUI image generation is disabled.", "error")
            return redirect(url_for("admin_models"))
        try:
            synced = comfyui.sync_models()
            log_action(
                "admin_sync_comfyui_models", request, user=get_current_user(),
                model_count=len(synced),
            )
            flash(f"Synced {len(synced)} model(s) from ComfyUI.", "success")
        except Exception as exc:
            flash(f"ComfyUI sync failed: {exc}", "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/<int:model_id>/rollout", methods=["POST"])
    @admin_required
    def admin_model_rollout(model_id):
        rolled_out = request.form.get("rolled_out") == "1"
        db.set_model_rollout(model_id, rolled_out)
        status = "rolled out" if rolled_out else "restricted to admins"
        flash(f"Model {status}.", "success")
        return redirect(url_for("admin_models"))

    def _opt_float(val, min_v, max_v):
        s = (val or "").strip()
        if not s:
            return None
        try:
            f = float(s)
            return f if min_v <= f <= max_v else None
        except (TypeError, ValueError):
            return None

    def _opt_int(val, min_v, max_v):
        s = (val or "").strip()
        if not s:
            return None
        try:
            i = int(s)
            return i if min_v <= i <= max_v else None
        except (TypeError, ValueError):
            return None

    @app.route("/admin/models/<int:model_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_model_edit(model_id):
        model = db.get_model_by_id(model_id)
        if not model:
            abort(404)
        categories = db.list_categories()
        model_cats = [c["id"] for c in db.get_model_categories(model_id)]

        if request.method == "POST":
            is_comfyui = model["backend"] == "comfyui"
            display_name = request.form.get("display_name", "").strip()[:200]
            description = request.form.get("description", "").strip()[:1000]
            new_cats = [int(x) for x in request.form.getlist("categories") if x.isdigit()]

            # Inference parameters (all optional: None = use Ollama default)
            system_prompt = None if is_comfyui else request.form.get("system_prompt", "").strip()[:4000] or None
            temperature = None if is_comfyui else _opt_float(request.form.get("temperature"), 0.0, 2.0)
            top_p = None if is_comfyui else _opt_float(request.form.get("top_p"), 0.0, 1.0)
            top_k = None if is_comfyui else _opt_int(request.form.get("top_k"), 0, 200)
            num_ctx = None if is_comfyui else _opt_int(request.form.get("num_ctx"), 512, 131072)
            repeat_penalty = None if is_comfyui else _opt_float(request.form.get("repeat_penalty"), 0.1, 3.0)
            is_reasoning = 0 if is_comfyui else 1 if request.form.get("is_reasoning") else 0
            supports_vision = 0 if is_comfyui else 1 if request.form.get("supports_vision") else 0
            is_uncensored = 1 if request.form.get("is_uncensored") else 0
            is_image_generation = 1 if is_comfyui else 0

            db.update_model(
                model_id,
                display_name=display_name,
                description=description,
                system_prompt=system_prompt,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                num_ctx=num_ctx,
                repeat_penalty=repeat_penalty,
                is_reasoning=is_reasoning,
                is_uncensored=is_uncensored,
                is_image_generation=is_image_generation,
                supports_vision=supports_vision,
            )

            # Sync category assignments
            for cat_id in model_cats:
                if cat_id not in new_cats:
                    db.remove_model_category(model_id, cat_id)
            for cat_id in new_cats:
                if cat_id not in model_cats:
                    db.assign_model_category(model_id, cat_id)

            flash("Model updated.", "success")
            return redirect(url_for("admin_models"))

        return render_template(
            "admin/model_edit.html",
            model=dict(model),
            categories=[dict(c) for c in categories],
            model_cats=model_cats,
        )

    @app.route("/admin/models/<int:model_id>/delete", methods=["POST"])
    @admin_required
    def admin_model_delete(model_id):
        db.delete_model(model_id)
        flash("Model removed from catalog.", "info")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/<int:model_id>/delete-ollama", methods=["POST"])
    @admin_required
    def admin_model_delete_ollama(model_id):
        model = db.get_model_by_id(model_id)
        if not model:
            flash("Model not found.", "error")
            return redirect(url_for("admin_models"))
        model = dict(model)
        if model.get("backend", "ollama") != "ollama":
            flash("ComfyUI checkpoints can only be removed from the catalog here.", "error")
            return redirect(url_for("admin_models"))
        try:
            ollama_name = model.get("backend_model_name") or model["ollama_name"]
            found = ollama.delete_ollama_model(ollama_name)
            db.delete_model(model_id)
            if found:
                log_action("admin_delete_ollama_model", request, user=get_current_user())
                flash(f"'{model['ollama_name']}' deleted from Ollama and removed from catalog.", "success")
            else:
                flash(f"'{model['ollama_name']}' was not in Ollama (already deleted?). Removed from catalog.", "info")
        except Exception as exc:
            flash(f"Delete failed: {exc}", "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/pull-checkpoint", methods=["POST"])
    @admin_required
    def admin_models_pull_checkpoint():
        if not comfyui.is_enabled():
            flash("ComfyUI image generation is disabled.", "error")
            return redirect(url_for("admin_models"))
        agent_error = checkpoint_agent.configuration_error()
        if agent_error:
            flash(agent_error, "error")
            return redirect(url_for("admin_models"))

        repo_id = request.form.get("repo_id", "").strip()
        source_filename = request.form.get("source_filename", "").strip()
        revision = request.form.get("revision", "").strip()
        target_name = request.form.get("target_name", "").strip()
        expected_sha256 = request.form.get("expected_sha256", "").strip()
        expected_size_raw = request.form.get("expected_size", "").strip()
        if expected_size_raw:
            if not expected_size_raw.isascii() or not expected_size_raw.isdigit():
                flash("Expected size must be a positive byte count.", "error")
                return redirect(url_for("admin_models"))
            expected_size = int(expected_size_raw)
        else:
            expected_size = None

        try:
            job_id = db.enqueue_pull_job(
                target_name,
                get_current_user()["id"],
                backend="comfyui",
                repo_id=repo_id,
                source_filename=source_filename,
                revision=revision,
                target_name=target_name,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
            log_action(
                "admin_queue_checkpoint_pull", request, user=get_current_user(),
                job_id=job_id, target_name=target_name, repo_id=repo_id,
            )
            flash(f"Checkpoint pull queued for '{target_name}' (job #{job_id}).", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/models/pull", methods=["POST"])
    @admin_required
    def admin_models_pull():
        admin = get_current_user()
        model_name = request.form.get("model_name", "").strip()
        if not model_name:
            flash("Model name is required.", "error")
            return redirect(url_for("admin_models"))
        # Accept: ollama names (llama3:latest), HuggingFace (hf.co/user/repo:Q4_K_M),
        # and full HF URLs (https://huggingface.co/user/repo)
        if model_name.startswith("https://huggingface.co/"):
                model_name = model_name.replace("https://huggingface.co/", "hf.co/", 1)
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9:._/\-]{0,299}$', model_name):
            flash("Invalid model name.", "error")
            return redirect(url_for("admin_models"))
        disk_ok, disk_msg = ollama.check_disk_space()
        if not disk_ok:
            flash(f"Insufficient disk space: {disk_msg}", "error")
            return redirect(url_for("admin_models"))
        try:
            job_id = db.enqueue_pull_job(model_name, admin["id"])
            flash(f"Pull queued for '{model_name}' (job #{job_id}).", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_models"))

    @app.route("/admin/api/pull-queue")
    @admin_required
    def admin_api_pull_queue():
        jobs = db.list_pull_jobs(limit=30)
        disk_ok, disk_msg = ollama.check_disk_space()
        return jsonify({
            "jobs": [dict(j) for j in jobs],
            "disk_ok": disk_ok,
            "disk_msg": disk_msg,
        })

    @app.route("/admin/api/pull/<int:job_id>/cancel", methods=["POST"])
    @admin_required
    def admin_api_pull_cancel(job_id):
        job = db.get_pull_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        was_pulling = dict(job).get("status") == "pulling"
        backend = dict(job).get("backend", "ollama")
        cancelled = db.cancel_pull_job(job_id)
        if cancelled and was_pulling and backend == "ollama":
            ollama.cancel_current_pull()
        if cancelled:
            log_action(
                "admin_cancel_model_pull", request, user=get_current_user(),
                job_id=job_id, backend=backend,
            )
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.route("/admin/api/pull/<int:job_id>/delete", methods=["POST"])
    @admin_required
    def admin_api_pull_delete(job_id):
        deleted = db.delete_pull_job(job_id)
        if not deleted:
            return jsonify({"ok": False, "error": "Job not found or still active"}), 404
        return jsonify({"ok": True})

    @app.route("/admin/api/pull/clear", methods=["POST"])
    @admin_required
    def admin_api_pull_clear():
        count = db.clear_pull_history()
        return jsonify({"ok": True, "deleted": count})

    @app.route("/admin/categories")
    @admin_required
    def admin_categories():
        categories = db.list_categories()
        return render_template("admin/categories.html", categories=[dict(c) for c in categories])

    @app.route("/admin/categories/create", methods=["POST"])
    @admin_required
    def admin_category_create():
        name = request.form.get("name", "").strip()[:100]
        description = request.form.get("description", "").strip()[:500]
        scope = request.form.get("scope", "both")
        try:
            sort_order = int(request.form.get("sort_order", 0) or 0)
        except (ValueError, TypeError):
            sort_order = 0
        if not name:
            flash("Category name is required.", "error")
        else:
            db.create_category(name, description=description, scope=scope, sort_order=sort_order)
            flash("Category created.", "success")
        return redirect(url_for("admin_categories"))

    @app.route("/admin/categories/<int:cat_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_category_edit(cat_id):
        cats = db.list_categories()
        cat = next((c for c in cats if c["id"] == cat_id), None)
        if not cat:
            abort(404)
        if request.method == "POST":
            db.update_category(
                cat_id,
                name=request.form.get("name", "").strip()[:100],
                description=request.form.get("description", "").strip()[:500],
                scope=request.form.get("scope", "both"),
            )
            flash("Category updated.", "success")
            return redirect(url_for("admin_categories"))
        return render_template("admin/category_edit.html", cat=dict(cat))

    @app.route("/admin/categories/<int:cat_id>/delete", methods=["POST"])
    @admin_required
    def admin_category_delete(cat_id):
        db.delete_category(cat_id)
        flash("Category deleted.", "info")
        return redirect(url_for("admin_categories"))

    @app.route("/admin/quota-requests")
    @admin_required
    def admin_quota_requests():
        pending = db.list_quota_requests(status="pending")
        all_requests = db.list_quota_requests(limit=200)
        return render_template(
            "admin/quota_requests.html",
            pending=[dict(r) for r in pending],
            all_requests=[dict(r) for r in all_requests],
        )

    @app.route("/admin/quota-requests/<int:req_id>/resolve", methods=["POST"])
    @admin_required
    def admin_quota_resolve(req_id):
        admin = get_current_user()
        action = request.form.get("action")
        if action not in ("approve", "deny"):
            flash("Invalid quota review action.", "error")
            return redirect(url_for("admin_quota_requests"))
        approved = action == "approve"
        message = request.form.get("admin_message", "").strip()[:1000]
        try:
            db.resolve_quota_request(req_id, admin["id"], approved, admin_message=message or None)
            log_action(
                "admin_approve_quota" if approved else "admin_deny_quota",
                request, user=admin, request_id=req_id,
            )
            flash("Quota request " + ("approved" if approved else "denied") + ".", "success")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("admin_quota_requests"))

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        if request.method == "POST":
            site_name = request.form.get("site_name", config.DISPLAY_NAME).strip()[:100] or config.DISPLAY_NAME
            signup_mode = request.form.get("signup_mode", "invite")
            if signup_mode not in ("invite", "open", "disabled"):
                signup_mode = "invite"
            maintenance_mode = 1 if request.form.get("maintenance_mode") else 0
            maintenance_message = request.form.get("maintenance_message", "").strip()[:500]
            warning_banner_enabled = 1 if request.form.get("warning_banner_enabled") else 0
            warning_banner_dismissible = 1 if request.form.get("warning_banner_dismissible") else 0
            warning_banner_message = request.form.get("warning_banner_message", "").strip()[:500]
            default_theme_mode = request.form.get("default_theme_mode", "dark").strip().lower()
            if default_theme_mode not in ("dark", "light"):
                default_theme_mode = "dark"
            theme_defaults = {
                "primary_color": "#e6be32", "secondary_color": "#1d1d1d",
                "accent_color": "#cda624", "text_color": "#ededed",
                "sidebar_color": "#181818", "bg_color": "#141414",
                "light_primary_color": "#8a6500", "light_secondary_color": "#ffffff",
                "light_accent_color": "#6f5000", "light_text_color": "#202124",
                "light_sidebar_color": "#f4f1e8", "light_bg_color": "#faf9f5",
            }
            theme_colors = {
                key: request.form.get(key, default).strip().lower()
                for key, default in theme_defaults.items()
            }
            invalid_color = next(
                (key for key, value in theme_colors.items()
                 if not re.fullmatch(r"#[0-9a-f]{6}", value)),
                None,
            )
            if invalid_color:
                flash(f"Invalid theme color: {invalid_color}.", "error")
                return redirect(url_for("admin_settings"))
            db.update_site_settings(
                site_name=site_name,
                signup_mode=signup_mode,
                maintenance_mode=maintenance_mode,
                maintenance_message=maintenance_message,
                warning_banner_enabled=warning_banner_enabled,
                warning_banner_dismissible=warning_banner_dismissible,
                warning_banner_message=warning_banner_message,
                default_theme_mode=default_theme_mode,
                **theme_colors,
            )
            log_action("admin_update_settings", request, user=get_current_user())
            flash("Settings saved.", "success")
            return redirect(url_for("admin_settings"))

        settings = db.get_site_settings()
        return render_template("admin/settings.html", settings=settings)

    @app.route("/admin/quotas", methods=["GET", "POST"])
    @admin_required
    def admin_quotas():
        from db._credits import DEFAULT_DAILY_CREDITS, DEFAULT_DAILY_SLOW_CREDITS

        if request.method == "POST":
            def _safe_int(key, default=None, min_v=0, max_v=100000):
                raw = request.form.get(key, "").strip()
                if raw == "" or raw is None:
                    return default
                try:
                    return max(min_v, min(max_v, int(raw)))
                except (ValueError, TypeError):
                    return default

            db.update_site_settings(
                    default_daily_credits=_safe_int("default_daily_credits", DEFAULT_DAILY_CREDITS),
                default_slow_credits=_safe_int("default_slow_credits", DEFAULT_DAILY_SLOW_CREDITS),
                    slow_credits_enabled=1 if request.form.get("slow_credits_enabled") else 0,
                    api_rpm=_safe_int("api_rpm", None, 0, 10000),
                    chat_rpm=_safe_int("chat_rpm", None, 0, 10000),
                    chat_daily_limit_enabled=1 if request.form.get("chat_daily_limit_enabled") else 0,
                chat_daily_credits=_safe_int("chat_daily_credits", None),
                chat_daily_slow_credits=_safe_int("chat_daily_slow_credits", None),
                    music_bonus_mode=request.form.get("music_bonus_mode", "multiplier")
                    if request.form.get("music_bonus_mode") in ("multiplier", "fixed")
                    else "multiplier",
                music_bonus_fixed_credits=_safe_int("music_bonus_fixed_credits", 30),
                music_bonus_fixed_slow=_safe_int("music_bonus_fixed_slow", 15),
                    quota_auto_approve_enabled=1 if request.form.get("quota_auto_approve_enabled") else 0,
                quota_auto_approve_max_credits=_safe_int("quota_auto_approve_max_credits", 0, 0, 10000),
                quota_auto_approve_max_slow_credits=_safe_int("quota_auto_approve_max_slow_credits", 0, 0, 10000),
            )
            log_action("admin_update_quotas", request, user=get_current_user())
            flash("Quota & rate-limit settings saved.", "success")
            return redirect(url_for("admin_quotas"))

        settings = db.get_site_settings()
        return render_template(
            "admin/quotas.html",
            settings=settings,
            fallback_credits=DEFAULT_DAILY_CREDITS,
            fallback_slow=DEFAULT_DAILY_SLOW_CREDITS,
        )

    @app.route("/admin/metrics")
    @admin_required
    def admin_metrics():
        try:
            hours = int(request.args.get("hours", 24))
        except (ValueError, TypeError):
            hours = 24
        history = db.get_compute_history(hours=hours)
        max_gpu_mb = max(
            (float(row.get("gpu_memory_used_mb") or 0) for row in history),
            default=0,
        )
        history = [_decorate_snapshot(row, max_gpu_mb) for row in history]
        req_stats = db.get_request_stats(hours=hours)
        latest = _decorate_snapshot(db.get_latest_snapshot(), max_gpu_mb)
        return render_template(
            "admin/metrics.html",
            history=history,
            req_stats=[dict(r) for r in req_stats],
            latest_snapshot=latest,
            hours=hours,
            db_health=db.get_database_health(),
        )

    @app.route("/admin/metrics/export")
    @admin_required
    def admin_metrics_export():
        try:
            hours = int(request.args.get("hours", 24))
        except (ValueError, TypeError):
            hours = 24
        history = db.get_compute_history(hours=hours)
        output = io.StringIO()
        # The snapshot rows carry bookkeeping columns such as the primary key,
        # which the export deliberately omits. Without extrasaction the writer
        # raises on the first row instead of skipping them.
        writer = csv.DictWriter(output, fieldnames=[
            "recorded_at", "gpu_name", "gpu_memory_used_mb", "gpu_memory_total_mb",
            "gpu_utilization_percent", "system_memory_used_mb", "system_memory_total_mb",
            "metrics_source", "cpu_percent", "active_models", "queue_depth"
        ], extrasaction="ignore")
        writer.writeheader()
        for row in history:
            r = dict(row)
            if isinstance(r.get("active_models"), list):
                r["active_models"] = ",".join(r["active_models"])
            writer.writerow(r)
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=metrics_{hours}h.csv"},
        )

    _INCOGNITO_PAGE_SIZE = 100

    @app.route("/admin/incognito")
    @admin_required
    def admin_incognito():
        page = max(1, request.args.get("page", 1, type=int))
        offset = (page - 1) * _INCOGNITO_PAGE_SIZE
        entries = db.list_incognito_audit(limit=_INCOGNITO_PAGE_SIZE, offset=offset)
        total_entries = db.count_incognito_audit()
        total_pages = max(1, (total_entries + _INCOGNITO_PAGE_SIZE - 1) // _INCOGNITO_PAGE_SIZE)

        # Group by session, preserving newest-session-first order
        sessions = {}
        session_order = []
        for entry in [dict(e) for e in entries]:
            sid = entry["session_id"]
            if sid not in sessions:
                sessions[sid] = {
                    "session_id": sid,
                    "username": entry.get("username") or entry.get("user_id", "?"),
                    "entries": [],
                }
                session_order.append(sid)
            sessions[sid]["entries"].append(entry)
        # Reverse entries within each session to chronological order
        for sid in session_order:
            sessions[sid]["entries"].reverse()
        grouped = [sessions[sid] for sid in session_order]
        return render_template(
            "admin/incognito.html",
            sessions=grouped,
            total=len(entries),
            total_entries=total_entries,
            page=page,
            total_pages=total_pages,
        )

    @app.route("/admin/incognito/<int:entry_id>/delete", methods=["POST"])
    @admin_required
    def admin_incognito_delete(entry_id):
        db.delete_incognito_entry(entry_id)
        flash("Entry deleted.", "info")
        return redirect(url_for("admin_incognito"))

    @app.route("/admin/incognito/session/<session_id>/delete", methods=["POST"])
    @admin_required
    def admin_incognito_session_delete(session_id):
        db.delete_incognito_session(session_id)
        flash("Session deleted.", "info")
        return redirect(url_for("admin_incognito"))

    from .admin_migration import register_admin_migration_routes
    register_admin_migration_routes(app)

    # Audit of ordinary sessions; the incognito equivalent is above.
    _CHATS_PAGE_SIZE = 50

    @app.route("/admin/chats")
    @admin_required
    def admin_chats():
        user_filter = request.args.get("user") or None
        # Use filter-specific keys (not `page`) to detect form submission, so
        # a bare ?page=N URL doesn't accidentally flip include_deleted to False.
        filter_submitted = any(k in request.args for k in ("user", "deleted"))
        include_deleted = (not filter_submitted) or (request.args.get("deleted") == "1")
        page = max(1, request.args.get("page", 1, type=int))
        offset = (page - 1) * _CHATS_PAGE_SIZE
        sessions = db.list_all_sessions_admin(
            limit=_CHATS_PAGE_SIZE, offset=offset,
            user_id=user_filter, include_deleted=include_deleted,
        )
        total = db.count_all_sessions_admin(
            user_id=user_filter, include_deleted=include_deleted,
        )
        total_pages = max(1, (total + _CHATS_PAGE_SIZE - 1) // _CHATS_PAGE_SIZE)
        users = db.list_users(limit=500)
        return render_template(
            "admin/chats.html",
            sessions=[dict(s) for s in sessions],
            users=[dict(u) for u in users],
            user_filter=user_filter,
            include_deleted=include_deleted,
            page=page,
            total_pages=total_pages,
            total=total,
        )

    @app.route("/admin/chats/<session_id>")
    @admin_required
    def admin_chat_view(session_id):
        sess = db.get_session(session_id)
        if not sess:
            abort(404)
        messages = db.list_messages(session_id)
        owner = db.get_user_by_id(sess["user_id"])
        return render_template(
            "admin/chat_view.html",
            sess=dict(sess),
            messages=[dict(m) for m in messages],
            owner=dict(owner) if owner else None,
        )

    @app.route("/admin/chats/<session_id>/delete", methods=["POST"])
    @admin_required
    def admin_chat_delete(session_id):
        db.request_stop_stream(session_id)
        db.admin_hard_delete_session(session_id)
        log_action("admin_chat_delete", request, user=get_current_user(), target_session=session_id)
        flash("Chat session permanently deleted.", "info")
        return redirect(url_for("admin_chats"))

    # JSON the dashboard polls, rather than rendered pages.
    @app.route("/admin/api/status")
    @admin_required
    def admin_api_status():
        from services.queue import get_stats as queue_stats
        running = ollama.list_running_models()
        return jsonify({
            "queue": queue_stats(),
            "running_models": [m.get("name") for m in running],
        })

    @app.route("/admin/api/running-models")
    @admin_required
    def admin_api_running_models():
        """Return detailed list of currently loaded models."""
        running = ollama.list_running_models()
        return jsonify([{
            "name": m.get("name"),
            "size_vram_gb": round(float(m.get("size_vram", 0)) / (1024**3), 2) if m.get("size_vram") else None,
            "size_gb": round(float(m.get("size", 0)) / (1024**3), 2) if m.get("size") else None,
        } for m in running])

    @app.route("/admin/api/unload-model", methods=["POST"])
    @admin_required
    def admin_api_unload_model():
        """Unload a specific model or all models from Ollama memory."""
        model_name = (request.json or {}).get("model", "").strip()
        if model_name:
            unloaded = [model_name] if ollama.unload_model(model_name) else []
        else:
            unloaded = ollama.unload_all_models()
        return jsonify({"ok": True, "unloaded": unloaded})

    @app.route("/admin/api/unload-all-models", methods=["POST"])
    @admin_required
    def admin_api_unload_all_models():
        """Unload ALL currently loaded models from Ollama memory."""
        unloaded = ollama.unload_all_models()
        return jsonify({"ok": True, "unloaded": unloaded})

    @app.route("/admin/workers")
    @admin_required
    def admin_workers():
        from datetime import datetime, timezone
        workers_raw = db.list_workers()
        workers = []
        for w in workers_raw:
            d = dict(w)
            # Convert Unix float last_heartbeat to ISO string for time_ago()
            if d.get("last_heartbeat"):
                try:
                    d["last_heartbeat_iso"] = datetime.fromtimestamp(
                        float(d["last_heartbeat"]), tz=timezone.utc
                    ).isoformat()
                except (TypeError, ValueError, OSError):
                    d["last_heartbeat_iso"] = None
            else:
                d["last_heartbeat_iso"] = None
            workers.append(d)
        import config as _cfg
        workers_enabled = _cfg.WORKERS_ENABLED
        return render_template(
            "admin/workers.html",
            workers=workers,
            workers_enabled=workers_enabled,
        )

    @app.route("/admin/workers/create", methods=["POST"])
    @admin_required
    def admin_worker_create():
        name = (request.form.get("name") or "").strip()[:80]
        if not name:
            flash("Worker name is required.", "error")
            return redirect(url_for("admin_workers"))
        worker_id, raw_token = db.create_worker(name)
        log_action("admin_create_worker", request, user=get_current_user(),
                   detail=f"name={name} id={worker_id[:8]}")
        flash(f"Worker '{name}' created. Token shown below: copy it now.", "success")
        # Pass the token back via session so the next page can display it once.
        from flask import session as flask_session
        flask_session["new_worker_token"] = raw_token
        flask_session["new_worker_id"]    = worker_id
        return redirect(url_for("admin_workers"))

    @app.route("/admin/workers/<worker_id>/toggle", methods=["POST"])
    @admin_required
    def admin_worker_toggle(worker_id):
        worker = db.get_worker_by_id(worker_id)
        if not worker:
            abort(404)
        disable = worker["status"] != "disabled"
        db.set_worker_disabled(worker_id, disable)
        action = "disabled" if disable else "enabled"
        log_action(f"admin_worker_{action}", request, user=get_current_user(),
                   detail=f"id={worker_id[:8]}")
        flash(f"Worker '{worker['name']}' {action}.", "success")
        return redirect(url_for("admin_workers"))

    @app.route("/admin/workers/<worker_id>/delete", methods=["POST"])
    @admin_required
    def admin_worker_delete(worker_id):
        worker = db.get_worker_by_id(worker_id)
        if not worker:
            abort(404)
        db.delete_worker(worker_id)
        log_action("admin_delete_worker", request, user=get_current_user(),
                   detail=f"name={worker['name']} id={worker_id[:8]}")
        flash(f"Worker '{worker['name']}' deleted.", "success")
        return redirect(url_for("admin_workers"))

    @app.route("/admin/api/models/reorder", methods=["POST"])
    @admin_required
    def admin_api_reorder_models():
        data = request.get_json(silent=True) or {}
        ordered_ids = data.get("ids", [])
        if not isinstance(ordered_ids, list):
            return jsonify({"ok": False, "error": "Invalid data"}), 400
        try:
            ordered_ids = [int(x) for x in ordered_ids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid IDs"}), 400
        db.reorder_models(ordered_ids)
        return jsonify({"ok": True})

    @app.route("/admin/api/categories/reorder", methods=["POST"])
    @admin_required
    def admin_api_reorder_categories():
        data = request.get_json(silent=True) or {}
        ordered_ids = data.get("ids", [])
        if not isinstance(ordered_ids, list):
            return jsonify({"ok": False, "error": "Invalid data"}), 400
        try:
            ordered_ids = [int(x) for x in ordered_ids]
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid IDs"}), 400
        db.reorder_categories(ordered_ids)
        return jsonify({"ok": True})

    @app.route("/admin/api/workers", methods=["GET"])
    @admin_required
    def admin_api_workers():
        """JSON endpoint for live worker status polling."""
        workers = db.list_workers()
        return jsonify([dict(w) for w in workers])
