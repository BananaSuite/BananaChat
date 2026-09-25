"""Session-authenticated image-generation testing UI."""

from flask import jsonify, render_template, request

import config
import db
from helpers import get_current_user, login_required
from logger import log_action
from services import comfyui, image_generation, model_access, queue as q


def register_image_routes(app):

    @app.route("/images")
    @login_required
    def images_index():
        user = get_current_user()
        models = model_access.list_accessible_models(
            user, "api", image_only=True
        ) if comfyui.is_enabled() else []
        return render_template(
            "images/index.html",
            models=models,
            image_backend_enabled=comfyui.is_enabled(),
        )

    @app.route("/images/generate", methods=["POST"])
    @login_required
    def images_generate():
        user = get_current_user()
        if not comfyui.is_enabled():
            return jsonify({"error": "Image generation is not configured"}), 503
        body = request.get_json(silent=True) or {}
        prompt = str(body.get("prompt") or "").strip()
        model_name = str(body.get("model") or "").strip()
        size = str(body.get("size") or "1024x1024").strip()
        if not prompt:
            return jsonify({"error": "Prompt is required"}), 400
        if len(prompt) > 10_000:
            return jsonify({"error": "Prompt is too long (maximum 10,000 characters)"}), 400
        if not model_name:
            return jsonify({"error": "Model is required"}), 400

        is_admin = user["role"] == "admin"
        if not image_generation.check_rate_limit(user):
            return jsonify({"error": "Image generation rate limit exceeded"}), 429

        reservation_id = None
        try:
            model = image_generation.resolve_model(user, model_name)
            if is_admin:
                priority = q.PRIORITY_ADMIN
            else:
                reservation_id, is_slow = db.reserve_image_credits(
                    user["id"], config.IMAGE_CREDITS_PER_GENERATION,
                    model_id=model["id"],
                )
                priority = q.PRIORITY_SLOW if is_slow else q.PRIORITY_API
            generation_kwargs = {"size": size}
            if reservation_id is not None:
                generation_kwargs["reservation_heartbeat"] = lambda: (
                    db.touch_image_credit_reservation(reservation_id, user["id"])
                )
            result = image_generation.generate(
                model, prompt, priority, **generation_kwargs
            )
            if reservation_id is not None:
                db.finalize_image_credit_reservation(reservation_id, user["id"])
        except db.InsufficientImageCreditsError as exc:
            if reservation_id is not None:
                db.refund_image_credit_reservation(reservation_id, user["id"])
            return jsonify({"error": str(exc)}), 429
        except image_generation.ImageGenerationError as exc:
            if reservation_id is not None:
                db.refund_image_credit_reservation(reservation_id, user["id"])
            return jsonify({"error": str(exc)}), exc.status_code
        except db.ImageCreditReservationError:
            return jsonify({"error": "Image generation billing could not be completed"}), 500

        if is_admin:
            db.deduct_credits(
                user["id"], 0, image_generation.billable_tokens(result),
                model_id=model["id"], request_type="api",
            )
        db.record_request_metric(
            request_type="image", model_id=model["id"], user_id=user["id"],
            tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
            duration_ms=result["duration_ms"], queue_wait_ms=result["wait_ms"],
            status="ok",
        )
        log_action(
            "image_generate", request, user=user, model=model["ollama_name"]
        )
        return jsonify({
            "model": model["ollama_name"],
            "data": result["images"],
            "usage": {
                "prompt_tokens": result["tokens_in"],
                "completion_tokens": result["tokens_out"],
                "total_tokens": result["tokens_in"] + result["tokens_out"],
            },
        })
