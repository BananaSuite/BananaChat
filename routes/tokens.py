"""API token management, usage history, and playground."""

import json

from flask import render_template, request, redirect, url_for, flash, jsonify, Response

import db
from services.http_capacity import limit_inference
from helpers import login_required, get_current_user
from services import ollama, queue as q, model_access, inference


def register_token_routes(app):

    @app.route("/api")
    @login_required
    def api_index():
        user = get_current_user()
        tokens = db.list_tokens(user["id"])
        quota = db.get_user_quota(user["id"])
        reg_used, slow_used = db.get_today_usage(user["id"])
        models = model_access.list_accessible_models(user, "api")
        return render_template(
            "api/index.html",
            tokens=[dict(t) for t in tokens],
            quota=dict(quota),
            reg_used=reg_used,
            slow_used=slow_used,
            models=models,
            max_tokens=db.MAX_TOKENS_PER_USER,
        )

    @app.route("/api/tokens/create", methods=["POST"])
    @login_required
    def api_token_create():
        user = get_current_user()
        name = request.form.get("name", "").strip()[:64]
        try:
            token_id, raw_token = db.create_token(user["id"], name=name)
            flash(f"Token created. Copy it now. It will not be shown again: {raw_token}", "token")
            return redirect(url_for("api_index"))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("api_index"))

    @app.route("/api/tokens/<int:token_id>/rename", methods=["POST"])
    @login_required
    def api_token_rename(token_id):
        user = get_current_user()
        name = request.form.get("name", "").strip()[:64]
        if not name:
            flash("Token name cannot be empty.", "error")
            return redirect(url_for("api_index"))
        db.rename_token(token_id, user["id"], name)
        return redirect(url_for("api_index"))

    @app.route("/api/tokens/<int:token_id>/rotate", methods=["POST"])
    @login_required
    def api_token_rotate(token_id):
        user = get_current_user()
        try:
            _, raw_token = db.rotate_token(token_id, user["id"])
            flash(f"Token rotated. Copy it now. It will not be shown again: {raw_token}", "token")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("api_index"))

    @app.route("/api/tokens/<int:token_id>/delete", methods=["POST"])
    @login_required
    def api_token_delete(token_id):
        user = get_current_user()
        db.revoke_token(token_id, user["id"])
        flash("Token deleted.", "info")
        return redirect(url_for("api_index"))

    @app.route("/api/usage")
    @login_required
    def api_usage():
        user = get_current_user()
        history = db.get_usage_history(user["id"], limit=200)
        quota = db.get_user_quota(user["id"])
        reg_used, slow_used = db.get_today_usage(user["id"])
        return render_template(
            "api/usage.html",
            history=[dict(h) for h in history],
            quota=dict(quota),
            reg_used=reg_used,
            slow_used=slow_used,
        )

    @app.route("/api/playground")
    @login_required
    def api_playground():
        user = get_current_user()
        models = model_access.list_accessible_models(user, "api")
        quota = db.get_user_quota(user["id"])
        reg_used, slow_used = db.get_today_usage(user["id"])
        return render_template(
            "api/playground.html",
            models=models,
            quota=dict(quota),
            reg_used=reg_used,
            slow_used=slow_used,
        )

    @app.route("/api/playground/send", methods=["POST"])
    @login_required
    @limit_inference
    def api_playground_send():
        """Streaming playground endpoint: charges credits like an API call."""
        user = get_current_user()
        is_admin = user["role"] == "admin"

        body = request.get_json(silent=True)
        try:
            model_name, messages = inference.validate_request(body)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        if model_name == "auto":
            if user["role"] != "admin" and not model_access.list_accessible_models(
                user, "api"
            ):
                return jsonify({"error": "No models are available to your account"}), 403
            auto, auto_err = ollama.select_auto_model(user=user, surface="api")
            if not auto:
                return jsonify({"error": auto_err}), 503
            model_name = auto["ollama_name"]
            backend_model_name = auto["backend_model_name"]
            model_id = auto["id"]
            model_row = auto
        else:
            model_row = db.get_model_by_ollama_name(model_name)
            if not model_row:
                return jsonify({"error": "Model not found"}), 404
            if not model_access.can_user_access_model(user, model_row, "api"):
                return jsonify({"error": "Model not available to your account"}), 403
            if not model_access.is_ollama_text_model(model_row):
                return jsonify({"error": "Model is not an available Ollama text model"}), 400
            model_id = model_row["id"]
            backend_model_name = model_row["backend_model_name"]

        options = inference.options_for(model_row, body)

        # Prepend model system prompt if caller didn't provide one
        model_sys_prompt = db.get_model_system_prompt(model_row)
        if model_sys_prompt and not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": model_sys_prompt}] + list(messages)

        if not is_admin:
            ok, is_slow, _, _, reg_limit, slow_limit = db.check_credits_available(
                user["id"], role=user["role"]
            )
            if not ok:
                return jsonify({"error": f"Credit limit reached ({reg_limit}+{slow_limit}/day)"}), 429
            priority = q.PRIORITY_SLOW if is_slow else q.PRIORITY_API
        else:
            priority = q.PRIORITY_ADMIN
            is_slow = False

        try:
            work = inference.TextInference(user, model_id, backend_model_name, messages,
                options, priority, request_type="playground")
        except q.QueueFullError as error:
            return jsonify({"error": str(error)}), 503, {"Retry-After": "5"}

        def generate():
            iterator = work.stream()
            try:
                for chunk, done, _ in iterator:
                    if chunk:
                        yield "data: " + json.dumps({"type": "delta", "content": chunk}) + "\n\n"
                    if done:
                        yield "data: " + json.dumps({"type": "done", "tokens_in": work.tokens_in,
                            "tokens_out": work.tokens_out, "finish_reason": work.finish_reason}) + "\n\n"
            except (inference.InferenceError, q.QueueCancelledError, TimeoutError) as error:
                yield "data: " + json.dumps({"type": "error", "message": str(error)}) + "\n\n"
            finally:
                iterator.close()
                work.close()

        response = Response(generate(), mimetype="text/event-stream",
                            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"})
        response.call_on_close(work.close)
        return response
