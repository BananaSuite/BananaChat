"""Centralized, compositional authorization for the AI model catalog."""

from collections.abc import Mapping

import config
import db


def _resolve_user(user):
    if user is None:
        return None
    if isinstance(user, Mapping) or hasattr(user, "keys"):
        return db.get_user_by_id(user.get("id")) if user.get("id") else None
    return db.get_user_by_id(user)


def _resolve_model(model):
    if model is None:
        return None
    if isinstance(model, Mapping) or hasattr(model, "keys"):
        model_id = model.get("id")
        return db.get_model_by_id(model_id) if model_id is not None else None
    if isinstance(model, int):
        return db.get_model_by_id(model)
    return db.get_model_by_ollama_name(model)


def _gate(scope, resource_id, label, user_id):
    policy = db.get_access_policy(scope, resource_id)
    allowed = db.is_user_allowed_by_policy(scope, resource_id, user_id)
    return {
        "scope": scope,
        "resource_id": resource_id,
        "label": label,
        "mode": policy["mode"],
        "requests_enabled": bool(policy["requests_enabled"]),
        "allowed": allowed,
    }


def _denial_reason(gate):
    if gate["scope"] == "catalog":
        return "Model does not exist in the catalog."
    if gate["scope"] == "rollout":
        return "Model has not been rolled out."
    if gate["mode"] == "deny_except_allowlist":
        return f"Access to {gate['label']} requires allowlist approval."
    return f"Access to {gate['label']} is denied for this user."


def _model_gates(user, model, surface):
    if surface not in ("chat", "api"):
        raise ValueError("surface must be 'chat' or 'api'")
    user = _resolve_user(user)
    model = _resolve_model(model)
    if not model:
        return [{
            "scope": "catalog",
            "resource_id": 0,
            "label": "model catalog",
            "mode": None,
            "requests_enabled": False,
            "allowed": False,
        }]
    if user and user.get("role") == "admin":
        return []

    user_id = user.get("id") if user else None
    gates = [{
        "scope": "rollout",
        "resource_id": model["id"],
        "label": model["display_name"],
        "mode": None,
        "requests_enabled": False,
        "allowed": bool(model["is_rolled_out"]),
    }]
    if model.get("is_uncensored"):
        gates.append(_gate("uncensored", 0, "uncensored models", user_id))
    if model.get("is_image_generation"):
        gates.append(_gate("image_generation", 0, "image generation", user_id))

    for category in db.get_model_categories(model["id"]):
        if category["scope"] in (surface, "both"):
            gates.append(
                _gate("category", category["id"], category["name"], user_id)
            )
    gates.append(_gate("model", model["id"], model["display_name"], user_id))
    return gates


def get_denial_reasons(user, model, surface):
    """Return every failed gate; authorization uses hard AND semantics."""
    denials = []
    for gate in _model_gates(user, model, surface):
        if not gate["allowed"]:
            item = dict(gate)
            item["reason"] = _denial_reason(gate)
            denials.append(item)
    return denials


def get_requestable_gates(user, model, surface):
    """Return denied policy gates for which a user may submit a request."""
    user = _resolve_user(user)
    user_id = user.get("id") if user else None
    result = []
    for denial in get_denial_reasons(user, model, surface):
        if denial["scope"] not in db.ACCESS_SCOPES or not denial["requests_enabled"]:
            continue
        item = dict(denial)
        item["pending_request"] = bool(
            user_id and db.get_pending_access_request(
                denial["scope"], denial["resource_id"], user_id
            )
        )
        result.append(item)
    return result


def can_user_access_model(user, model, surface):
    """Return whether the catalog model passes every applicable gate."""
    return not get_denial_reasons(user, model, surface)


def can_user_use_custom_personalities(user):
    """Return whether a user may create or apply custom personalities."""
    user = _resolve_user(user)
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    return db.is_user_allowed_by_policy("custom_personality", 0, user["id"])


def get_usable_personality(user, personality_id):
    """Resolve an active, owned personality after checking the feature gate."""
    if not personality_id or not can_user_use_custom_personalities(user):
        return None
    user = _resolve_user(user)
    personality = db.get_personality(personality_id)
    if not personality or personality["user_id"] != user["id"]:
        return None
    return dict(personality) if db.personality_is_active(personality) else None


def is_ollama_text_model(model):
    """Return whether a catalog row may enter an Ollama text inference path."""
    return bool(
        model
        and model.get("backend") == "ollama"
        and model.get("backend_available")
        and not model.get("is_image_generation")
    )


def is_ollama_vision_model(model):
    return is_ollama_text_model(model) and bool(model.get("supports_vision"))


def list_accessible_models(user, surface, image_only=False, include_image=False, *, available_only=True):
    """Return authorized model dicts with surface-applicable categories."""
    if surface not in ("chat", "api"):
        raise ValueError("surface must be 'chat' or 'api'")
    user = _resolve_user(user)
    is_admin = bool(user and user.get("role") == "admin")
    result = []
    for model in db.list_models(rolled_out_only=not is_admin):
        if image_only:
            if config.IMAGE_BACKEND != "comfyui":
                continue
            if (
                model.get("backend") != "comfyui"
                or not model.get("backend_available")
                or not model.get("is_image_generation")
            ):
                continue
        elif model.get("backend") == "comfyui" or model.get("is_image_generation"):
            if not include_image or config.IMAGE_BACKEND != "comfyui":
                continue
            if (
                model.get("backend") != "comfyui"
                or not model.get("backend_available")
                or not model.get("is_image_generation")
            ):
                continue
        elif model.get("backend") != "ollama" or (available_only and not model.get("backend_available")):
            continue
        if not can_user_access_model(user, model, surface):
            continue
        item = dict(model)
        item["categories"] = [
            dict(category)
            for category in db.get_model_categories(model["id"])
            if category["scope"] in (surface, "both")
        ]
        result.append(item)
    return result


# Explicit names are convenient for callers that prefer model-qualified APIs.
get_model_denial_reasons = get_denial_reasons
get_requestable_model_gates = get_requestable_gates
