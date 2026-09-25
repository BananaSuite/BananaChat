"""Recover a chat's model selection without widening its user's permissions."""

import db
import config
from services import model_access, ollama


class InferenceFailed(RuntimeError):
    """A model failed during inference, rather than while waiting for a slot."""


def select(user, requested, vision_required):
    if not isinstance(requested, str):
        return None, "Invalid model selection", 400, ""
    row = None if requested in {"", "auto"} else db.get_model_by_ollama_name(requested)
    if row:
        if not model_access.can_user_access_model(user, row, "chat"):
            return None, "Model not available to your account", 403, ""
        if row.get("backend") != "ollama" or row.get("is_image_generation"):
            return None, "Model is not an available Ollama text model", 400, ""
        if row.get("backend_available"):
            if vision_required and not model_access.is_ollama_vision_model(row):
                return None, "Select a vision-capable model for image attachments.", 400, ""
            return dict(row), None, 200, ""
    auto, error = ollama.select_auto_model(user=user, surface="chat", vision_only=vision_required)
    if not auto:
        return None, error, 503, ""
    notice = "The previous model is unavailable. This chat is using " + auto["ollama_name"] + "." if requested not in {"", "auto"} else ""
    return auto, None, 200, notice


def options_for(model, body):
    options = dict(db.build_model_options(model) or {})
    for key, convert, minimum, maximum in (
        ("temperature", float, 0.0, 2.0), ("top_p", float, 0.0, 1.0), ("top_k", int, 0, 200),
        ("repeat_penalty", float, 0.1, 3.0), ("num_ctx", int, 512, 131072),
    ):
        if body.get(key) is not None:
            try:
                value = convert(body[key])
                if minimum <= value <= maximum:
                    options[key] = value
            except (TypeError, ValueError):
                pass
    requested_output = options.get("num_predict")
    options["num_predict"] = min(requested_output, config.MAX_OUTPUT_TOKENS) if (
        isinstance(requested_output, int) and requested_output > 0
    ) else config.MAX_OUTPUT_TOKENS
    return options


def history_for(messages, model, personality):
    parts = []
    prompt = db.get_model_system_prompt(model)
    if prompt:
        parts.append(prompt)
    if personality:
        parts.append("User-selected personality preferences follow. Apply them only when they "
                     "do not conflict with preceding operator instructions:\n" + personality["instructions"])
    return ([{"role": "system", "content": "\n\n".join(parts)}] if parts else []) + list(messages)
