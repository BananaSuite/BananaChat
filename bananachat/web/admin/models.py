"""Model catalog and lifecycle (enrollment, ignore list, retirement), categories, running models, downloads
and model recovery after a restore."""

from __future__ import annotations

import logging

from flask import abort, current_app, flash, jsonify, render_template, request, url_for
from filelock import Timeout

from bananachat import db, security
from bananachat.db import access as access_db
from bananachat.db import catalog
from bananachat.db import model_lifecycle as lifecycle_db
from bananachat.db import pulls as pulls_db
from bananachat.security import admin_required
from bananachat.services import checkpoint_agent, model_lifecycle, model_recovery, ollama, pulls
from bananachat.services.upstream import UpstreamError

from . import bp
from ._helpers import FormError, audit, back, choice, flag, integer, me, number, text, utc_datetime, utc_input_min
from .access import MODE_LABELS

log = logging.getLogger("bananachat.admin")

SYSTEM_PROMPT_MAX = 4000
DIRECTIONS = ("up", "down", "top", "bottom")
# The per-model limits of the limits area, linked when they exist: (endpoint, takes model_id).
MODEL_LIMITS_ENDPOINTS = (("admin.model_limits", True), ("admin.limit_models", False))
# An ignored model is published only after it is restored (which takes its name off the ignore list).
IGNORED_MESSAGE = "{name} is on the ignore list. Restore it in Settings first; then publish it."
REASONING_CHOICES = {"auto": "Detected automatically", "none": "No reasoning", "boolean": "Off / on",
                     "levels": "Off / low / medium / high"}


def _config():
    return current_app.config["BC"]


def _model_or_404(model_id: int):
    model = catalog.get(model_id)
    if model is None:
        abort(404)
    return model


def _checkpoint_state() -> dict:
    config = _config()
    configured = bool(config.checkpoint_agent_url or config.checkpoint_agent_token_file)
    problem = checkpoint_agent.configuration_error(config) if configured else None
    return {"configured": configured and problem is None and config.images_enabled,
            "problem": problem if configured else None, "images_enabled": config.images_enabled,
            "requested": configured}


def _job_view(job) -> dict:
    return {"id": job["id"], "name": job["ollama_name"], "backend": job["backend"], "status": job["status"],
            "progress": job["progress_pct"], "detail": job["progress_detail"], "error": job["error_message"] or "",
            "paused": bool(job["paused"]), "attempts": job["attempts"], "retry_at": job["next_attempt_at"],
            "bytes_done": job["bytes_completed"], "bytes_total": job["bytes_total"], "digest": job["digest"],
            "created_at": job["created_at"], "finished_at": job["finished_at"],
            "requested_by": job["requested_by_name"] if "requested_by_name" in job.keys() else None}


def _model_limits_url(model_id: int) -> str | None:
    for endpoint, per_model in MODEL_LIMITS_ENDPOINTS:
        if endpoint not in current_app.view_functions:
            continue
        try:
            return url_for(endpoint, model_id=model_id) if per_model else \
                url_for(endpoint, _anchor=f"model-{model_id}")
        except Exception:  # noqa: BLE001 - the limits area names its parameters differently
            continue
    return None


def _tab_counts() -> dict:
    return {"downloads": pulls_db.active_count(),
            "new": db.scalar("SELECT COUNT(*) FROM ai_models WHERE enrollment='new'", default=0)}


def _render(template: str, **values):
    return render_template(template, section="models", counts=_tab_counts(), **values)


# ----- catalog ------------------------------------------------------------------

@bp.get("/models", endpoint="models")
@admin_required
def models():
    config = _config()
    model_recovery.refresh(config)
    rows = catalog.list_models()
    visible = [row for row in rows if row["enrollment"] != "ignored"]
    new_models = [row for row in rows if row["enrollment"] == "new"]
    attention = [row for row in rows if row["enrollment"] != "ignored" and row["enrollment"] != "new" and (
        row["enrollment"] == "auto" or row["missing_at"] or row["failing_at"] or row["deprecated_at"]
        or row["delete_requested_at"])]
    return _render(
        "admin/models.html", tab="catalog", rows=visible, categories=catalog.categories_by_model(),
        new_models=new_models, attention=attention, states={row["id"]: model_lifecycle.badges(row) for row in rows},
        caps={row["id"]: model_lifecycle.capabilities(row) for row in rows}, sync=model_lifecycle.sync_status(),
        policy=model_lifecycle.policy(), recovery=model_recovery.status(), images_enabled=config.images_enabled,
        checkpoint=_checkpoint_state(),
        presets=model_lifecycle.PRESET_LABELS, suggested={row["id"]: model_lifecycle.preset_for(row) for row in rows},
        page_data={
            "running_url": url_for("admin.models_running"),
            "unload_url": url_for("admin.models_unload"),
            "unload_all_url": url_for("admin.models_unload_all"),
        })


@bp.post("/models/sync", endpoint="models_sync")
@admin_required
def sync():
    config = _config()
    messages, failed = [], False
    try:
        result = model_lifecycle.sync(config, source="admin")
        count = result["count"]
        messages.append(result["note"] or f"Ollama reports {count} model{'s' if count != 1 else ''}.")
        if result["new"]:
            messages.append(f"New: {', '.join(result['new'][:10])}.")
        if result["enabled"]:
            messages.append(f"Enabled automatically: {', '.join(result['enabled'][:10])}.")
        if result["missing"]:
            messages.append(f"Now missing: {', '.join(result['missing'][:10])}.")
        if result["errors"]:
            failed = True
            messages.append(f"{len(result['errors'])} model(s) could not be inspected: "
                            + "; ".join(f"{item['model']}: {item['error']}" for item in result["errors"][:3]))
    except (UpstreamError, OSError) as error:
        failed = True
        messages.append(f"Ollama could not be reached: {error}")
    if config.images_enabled:
        try:
            from bananachat.services import comfyui
            comfyui.sync_models()
            messages.append("ComfyUI checkpoints refreshed.")
        except ImportError:
            messages.append("ComfyUI support is not installed.")
        except Exception as error:  # noqa: BLE001 - shown to the administrator
            failed = True
            messages.append(f"ComfyUI could not be refreshed: {error}")
    audit("models_sync", "catalog", {"result": " ".join(messages)[:300]})
    flash(" ".join(messages), "warning" if failed else "success")
    return back("admin.models")


@bp.post("/models/<int:model_id>/rollout", endpoint="model_rollout")
@admin_required
def rollout(model_id):
    model = _model_or_404(model_id)
    enabled = flag("enabled")
    if enabled and model["retired_at"]:
        flash(f"{model['display_name']} is retired; restore it on its edit page before publishing it.", "error")
        return back("admin.models", _anchor=f"model-{model_id}")
    if enabled and model["enrollment"] == "ignored":
        flash(IGNORED_MESSAGE.format(name=model["ollama_name"]), "error")
        return back("admin.model_settings", _anchor="ignored")
    try:
        with db.transaction():
            fresh = _model_or_404(model_id)
            if enabled and fresh["enrollment"] == "new":
                model_lifecycle.enable(fresh, me())
            else:
                if enabled and (fresh["retired_at"] or fresh["delete_requested_at"] or
                                fresh["enrollment"] == "ignored"):
                    raise ValueError("This model is retired, ignored or being deleted; restore it before publishing it.")
                catalog.set_rollout(model_id, enabled)
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.models", _anchor=f"model-{model_id}")
    audit("model_rollout", model["ollama_name"], {"rolled_out": enabled})
    flash(f"{model['display_name']} is {'now visible to users' if enabled else 'hidden from users'}.", "success")
    return back("admin.models", _anchor=f"model-{model_id}")


@bp.post("/models/<int:model_id>/move", endpoint="model_move")
@admin_required
def move(model_id):
    _model_or_404(model_id)
    direction = request.form.get("direction")
    if direction not in DIRECTIONS:
        abort(400)
    ids = [row["id"] for row in catalog.list_models()]
    catalog.reorder(_moved(ids, model_id, direction))
    return back("admin.models", _anchor=f"model-{model_id}")


def _moved(ids: list[int], item: int, direction: str) -> list[int]:
    index = ids.index(item)
    ids.pop(index)
    target = {"up": max(0, index - 1), "down": min(len(ids), index + 1), "top": 0, "bottom": len(ids)}[direction]
    ids.insert(target, item)
    return ids


def _edit_context(model) -> dict:
    others = [row for row in catalog.list_models(backend=model["backend"])
              if row["id"] != model["id"] and not row["retired_at"] and row["enrollment"] != "ignored"]
    replacement = catalog.get(model["replacement_id"]) if model["replacement_id"] else None
    levels = model_lifecycle.reasoning_levels(model)
    level_choice = "auto" if not model["reasoning_levels_locked"] else (
        "levels" if len(levels) > 2 else "boolean" if levels else "none")
    return {"events": lifecycle_db.events_for(model["id"], 30), "badges": model_lifecycle.badges(model),
            "caps": model_lifecycle.capabilities(model), "levels": levels, "level_choice": level_choice,
            "level_choices": REASONING_CHOICES, "replacements": others, "replacement": replacement,
            "presets": model_lifecycle.PRESET_LABELS, "suggested_preset": model_lifecycle.preset_for(model),
            "limits_url": _model_limits_url(model["id"]), "utc_min": utc_input_min(),
            "in_use": model_lifecycle.in_use(model) if model["backend"] == "ollama" else 0}


@bp.route("/models/<int:model_id>/edit", methods=["GET", "POST"], endpoint="model_edit")
@admin_required
def edit(model_id):
    model = _model_or_404(model_id)
    all_categories = catalog.list_categories()
    selected = [row["id"] for row in catalog.model_categories(model_id)]
    if request.method == "POST":
        try:
            fields, category_ids = _edit_form(model, {row["id"] for row in all_categories})
        except FormError as error:
            flash(str(error), "error")
            return _render("admin/model_edit.html", model=model, form=request.form, all_categories=all_categories,
                           selected=_form_categories(), limits=_limits(), system_prompt_max=SYSTEM_PROMPT_MAX,
                           **_edit_context(model)), 400
        with db.transaction():
            catalog.update(model_id, **fields)
            catalog.set_model_categories(model_id, category_ids)
        audit("model_edit", model["ollama_name"], {key: value for key, value in fields.items()
                                                   if key != "system_prompt"})
        flash(f"{fields['display_name']} saved.", "success")
        return back("admin.models", _anchor=f"model-{model_id}")
    return _render("admin/model_edit.html", model=model, form=None, all_categories=all_categories, selected=selected,
                   limits=_limits(), system_prompt_max=SYSTEM_PROMPT_MAX, **_edit_context(model))


def _limits() -> dict:
    return {"temperature": (0.0, 2.0), "top_p": (0.0, 1.0), "top_k": (0, 200), "num_ctx": (512, _config().max_num_ctx),
            "repeat_penalty": (0.1, 3.0)}


def _form_categories() -> list[int]:
    return [int(value) for value in request.form.getlist("categories") if value.isdigit()]


def _edit_form(model, known_categories: set[int]):
    limits = _limits()
    text_model = model["backend"] == "ollama"
    fields = {
        "display_name": text("display_name", max_length=100, required=True, label="Display name"),
        "description": text("description", max_length=1000, label="Description"),
        "is_uncensored": 1 if flag("is_uncensored") else 0,
    }
    if text_model:
        prompt = text("system_prompt", max_length=SYSTEM_PROMPT_MAX, label="System prompt", strip=False).strip()
        fields.update(
            system_prompt=prompt or None,
            temperature=number("temperature", minimum=limits["temperature"][0], maximum=limits["temperature"][1],
                               label="Temperature", optional=True),
            top_p=number("top_p", minimum=limits["top_p"][0], maximum=limits["top_p"][1], label="Top P", optional=True),
            top_k=integer("top_k", minimum=limits["top_k"][0], maximum=limits["top_k"][1], label="Top K",
                          optional=True),
            num_ctx=integer("num_ctx", minimum=limits["num_ctx"][0], maximum=limits["num_ctx"][1],
                            label="Context length", optional=True),
            repeat_penalty=number("repeat_penalty", minimum=limits["repeat_penalty"][0],
                                  maximum=limits["repeat_penalty"][1], label="Repeat penalty", optional=True),
            is_reasoning=1 if flag("is_reasoning") else 0,
            supports_vision=1 if flag("supports_vision") else 0,
        )
    categories = _form_categories()
    if not set(categories) <= known_categories:
        raise FormError("A selected category no longer exists; reload the page.")
    return fields, categories


@bp.post("/models/<int:model_id>/remove", endpoint="model_remove")
@admin_required
def remove(model_id):
    model = _model_or_404(model_id)
    try:
        with model_lifecycle.operation_lock(model["backend_model_name"] or model["ollama_name"],
                                            backend=model["backend"]):
            with db.transaction():
                model = _model_or_404(model_id)
                count = model_lifecycle.in_use(model) if model["backend"] == "ollama" else 0
                if count:
                    raise model_lifecycle.ModelInUse(count)
                catalog.delete(model_id)
    except (model_lifecycle.ModelInUse, Timeout) as error:
        reason = str(error) if isinstance(error, model_lifecycle.ModelInUse) else "A model operation is still finishing."
        flash(f"{model['display_name']} was not removed: {reason} Hide it or try again when they finish.", "error")
        return back("admin.models", _anchor=f"model-{model_id}")
    audit("model_remove", model["ollama_name"])
    flash(f"{model['display_name']} was removed from the catalog. If the model is still installed, the next sync adds "
          "it back as a new entry waiting for review.", "success")
    return back("admin.models")


@bp.post("/models/<int:model_id>/delete-server", endpoint="model_delete_server")
@admin_required
def delete_from_server(model_id):
    model = _model_or_404(model_id)
    if model["backend"] != "ollama":
        flash("Only Ollama models can be deleted from here; remove checkpoints on the ComfyUI server.", "error")
        return back("admin.models")
    name = model["backend_model_name"] or model["ollama_name"]
    when_idle = flag("when_idle")
    try:
        outcome = model_lifecycle.delete_from_server(model, me(), when_idle=when_idle)
    except model_lifecycle.ModelInUse as error:
        flash(f"{name} was not deleted: {error} Try again when they finish, or choose “Delete after current "
              "requests” on its edit page.", "error")
        return back("admin.model_edit", model_id=model_id, _anchor="lifecycle")
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.models")
    except (UpstreamError, OSError) as error:
        flash(f"The model could not be deleted: {error}", "error")
        return back("admin.models")
    audit("model_delete_server", name, {"outcome": outcome, "existed": outcome == "deleted"})
    flash({"deleted": f"{name} was deleted from the Ollama server.",
           "absent": f"{name} was not installed on the server.",
           "scheduled": f"{name} is hidden and will be deleted when its running requests finish."}[outcome], "success")
    return back("admin.models")


# ----- enrollment and lifecycle ----------------------------------------------------

@bp.route("/models/<int:model_id>/enable", methods=["GET", "POST"], endpoint="model_enable")
@admin_required
def enable(model_id):
    """Review a new model: name, description, categories, access, features and limits, then publish it."""
    model = _model_or_404(model_id)
    all_categories = catalog.list_categories()
    policy = access_db.get_policy("model", model_id)
    context = {"model": model, "all_categories": all_categories, "modes": MODE_LABELS, "policy": policy,
               "presets": model_lifecycle.PRESET_LABELS, "suggested_preset": model_lifecycle.preset_for(model),
               "caps": model_lifecycle.capabilities(model), "levels": model_lifecycle.reasoning_levels(model),
               "limits_url": _model_limits_url(model_id), "badges": model_lifecycle.badges(model)}
    if request.method == "POST":
        try:
            if model["retired_at"]:
                raise FormError("This model is retired; restore it on its edit page first.")
            if model["enrollment"] == "ignored":
                raise FormError(IGNORED_MESSAGE.format(name=model["ollama_name"]))
            fields = {
                "display_name": text("display_name", max_length=100, required=True, label="Display name"),
                "description": text("description", max_length=1000, label="Description"),
                "is_uncensored": 1 if flag("is_uncensored") else 0,
            }
            if model["backend"] == "ollama":
                fields.update(is_reasoning=1 if flag("is_reasoning") else 0,
                              supports_vision=1 if flag("supports_vision") else 0)
            categories = _form_categories()
            if not set(categories) <= {row["id"] for row in all_categories}:
                raise FormError("A selected category no longer exists; reload the page.")
            mode = choice("access_mode", access_db.MODES, label="Access")
            preset = choice("limit_preset", model_lifecycle.PRESETS, label="Limit preset")
        except FormError as error:
            flash(str(error), "error")
            return _render("admin/model_enable.html", form=request.form, selected=_form_categories(), **context), 400
        try:
            with db.transaction():
                catalog.update(model_id, **fields)
                catalog.set_model_categories(model_id, categories)
                if mode != policy["mode"] or not policy["persisted"]:
                    access_db.set_policy("model", model_id, mode, bool(policy["requests_enabled"]), me()["id"])
                model_lifecycle.enable(catalog.get(model_id), me(), preset=preset)
        except ValueError as error:
            flash(str(error), "error")
            return _render("admin/model_enable.html", form=request.form, selected=categories, **context), 400
        audit("model_enable", model["ollama_name"], {"access": mode, "preset": preset, "categories": categories,
                                                     **{key: value for key, value in fields.items()}})
        flash(f"{fields['display_name']} is enabled and visible to the people its access rule allows.", "success")
        return back("admin.models", _anchor=f"model-{model_id}")
    return _render("admin/model_enable.html", form=None, selected=[row["id"] for row in
                                                                    catalog.model_categories(model_id)], **context)


@bp.post("/models/<int:model_id>/ignore", endpoint="model_ignore")
@admin_required
def ignore(model_id):
    model = _model_or_404(model_id)
    model_lifecycle.ignore_model(model, me())
    audit("model_ignore", model["ollama_name"])
    flash(f"{model['ollama_name']} is ignored: it is hidden and never offered again. Restore it in Settings.",
          "success")
    return back("admin.models")


@bp.post("/models/<int:model_id>/restore", endpoint="model_restore")
@admin_required
def restore(model_id):
    model = _model_or_404(model_id)
    if model["enrollment"] != "ignored":
        flash(f"{model['ollama_name']} is not ignored.", "info")
        return back("admin.model_settings", _anchor="ignored")
    model_lifecycle.restore_model(model, me())
    audit("model_restore", model["ollama_name"])
    flash(f"{model['ollama_name']} is back in the catalog, waiting for review.", "success")
    return back("admin.model_settings", _anchor="ignored")


@bp.post("/models/<int:model_id>/reviewed", endpoint="model_reviewed")
@admin_required
def reviewed(model_id):
    model = _model_or_404(model_id)
    model_lifecycle.mark_reviewed(model, me())
    audit("model_reviewed", model["ollama_name"])
    flash(f"{model['display_name']} is marked as reviewed.", "success")
    return back("admin.models", _anchor="attention")


def _lifecycle_redirect(model_id: int):
    if request.form.get("return_to") == "catalog":
        return back("admin.models", _anchor="attention")
    return back("admin.model_edit", model_id=model_id, _anchor="lifecycle")


@bp.post("/models/<int:model_id>/lifecycle", endpoint="model_lifecycle")
@admin_required
def lifecycle(model_id):
    """Health, deprecation, deletion and limit actions from the model's page or the attention card."""
    model = _model_or_404(model_id)
    action = request.form.get("action")
    actor = me()
    try:
        if action == "retry_check":
            if not model["failing_at"]:
                raise FormError("This model is not failing.")
            model_lifecycle.retry_check(model, actor)
            message = f"A test prompt is sent to {model['display_name']} within a minute."
        elif action == "force_enable":
            model_lifecycle.force_enable(model, actor)
            message = f"{model['display_name']} is offered again."
        elif action == "deprecate":
            replacement = request.form.get("replacement_id") or ""
            if replacement and not replacement.isdigit():
                raise FormError("Choose a valid replacement.")
            moment = utc_datetime("retire_at", label="Retirement date")
            note = text("note", max_length=500, label="Note")
            model_lifecycle.deprecate(model, actor, replacement_id=int(replacement) if replacement else None,
                                      retire_at=db.timestamp(moment) if moment else None, note=note)
            message = f"{model['display_name']} is deprecated."
        elif action == "undeprecate":
            model_lifecycle.undeprecate(model, actor)
            message = f"{model['display_name']} is no longer deprecated or retired."
        elif action == "retire":
            model_lifecycle.retire(model, actor)
            message = f"{model['display_name']} is retired."
        elif action == "cancel_delete":
            model_lifecycle.cancel_delete(model, actor)
            message = f"{model['display_name']} will not be deleted."
        elif action == "preset":
            preset = choice("limit_preset", model_lifecycle.PRESETS, label="Limit preset")
            applied = model_lifecycle.set_preset(model, preset, actor)
            message = f"Limit preset set to {preset}." + ("" if applied else " It is stored on the model; the "
                                                                              "limits do not apply presets yet.")
        elif action == "reasoning":
            model_lifecycle.set_reasoning_levels(model, choice("reasoning_levels", REASONING_CHOICES,
                                                               label="Reasoning levels"), actor)
            message = "Reasoning levels saved."
        else:
            abort(400)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return _lifecycle_redirect(model_id)
    audit(f"model_{action}", model["ollama_name"], {key: request.form.get(key) for key in (
        "replacement_id", "retire_at", "note", "limit_preset", "reasoning_levels") if request.form.get(key)})
    flash(message, "success")
    return _lifecycle_redirect(model_id)


# ----- settings -------------------------------------------------------------------

SETTING_FIELDS = (
    ("missing_syncs", "Missing after this many syncs"),
    ("missing_minutes", "Or after this many minutes"),
    ("retention_days", "Remove missing models after (days)"),
    ("failure_threshold", "Failing after this many failures"),
    ("failure_window_minutes", "Within (minutes)"),
    ("recheck_minutes", "First re-check after (minutes)"),
    ("download_concurrency", "Downloads at the same time"),
    ("download_retries", "Retries of a failed download"),
    ("stall_minutes", "Retry a download without progress for (minutes)"),
)


@bp.get("/models/settings", endpoint="model_settings")
@admin_required
def settings():
    return _render("admin/model_settings.html", tab="settings", policy=model_lifecycle.policy(),
                   fields=SETTING_FIELDS, bounds=model_lifecycle.NUMBERS, rules=lifecycle_db.ignore_rules(),
                   ignored=catalog.with_enrollment("ignored"), events=lifecycle_db.recent_events(30))


@bp.post("/models/settings", endpoint="model_settings_save")
@admin_required
def settings_save():
    try:
        changes = {"enrollment": choice("enrollment", model_lifecycle.ENROLLMENT_POLICIES, label="Enrollment")}
        for key, label in SETTING_FIELDS:
            _default, low, high = model_lifecycle.NUMBERS[key]
            changes[key] = integer(key, minimum=low, maximum=high, label=label)
        saved = model_lifecycle.save_policy(changes, me())
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.model_settings")
    audit("model_settings", "models", {key: saved[key] for key in changes})
    enabled = []
    if saved["enrollment"] == "automatic":
        enabled = model_lifecycle.enroll_pending(saved)
    flash("Model settings saved." + (f" Enabled automatically: {', '.join(enabled[:10])}." if enabled else ""),
          "success")
    return back("admin.model_settings")


@bp.post("/models/ignore-rules", endpoint="model_ignore_rule_add")
@admin_required
def ignore_rule_add():
    try:
        pattern = text("pattern", max_length=200, required=True, label="Name or pattern")
        note = text("note", max_length=200, label="Note")
        _rule_id, affected = model_lifecycle.add_ignore_rule(pattern, me(), note)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.model_settings", _anchor="ignore-list")
    audit("model_ignore_rule_add", pattern, {"ignored": affected})
    flash(f"Models matching {pattern} are never offered or enabled." +
          (f" Ignored now: {', '.join(affected[:10])}." if affected else ""), "success")
    return back("admin.model_settings", _anchor="ignore-list")


@bp.post("/models/ignore-rules/<int:rule_id>/delete", endpoint="model_ignore_rule_delete")
@admin_required
def ignore_rule_delete(rule_id):
    pattern = model_lifecycle.remove_ignore_rule(rule_id)
    if pattern is None:
        abort(404)
    audit("model_ignore_rule_delete", pattern)
    flash(f"The rule {pattern} was removed. Models it ignored stay ignored until you restore them.", "success")
    return back("admin.model_settings", _anchor="ignore-list")


# ----- running models (JSON, polled by the page) ------------------------------------

@bp.get("/api/models/running", endpoint="models_running")
@admin_required
def running():
    try:
        items = ollama.list_running()
    except (UpstreamError, OSError) as error:
        return security.json_error(f"The inference server could not be reached: {error}", 502, "upstream")
    result = []
    for item in items:
        name = item.get("name") or item.get("model")
        if not isinstance(name, str):
            continue
        result.append({"name": name[:300],
                       "size": item.get("size") if isinstance(item.get("size"), (int, float)) else None,
                       "size_vram": item.get("size_vram") if isinstance(item.get("size_vram"), (int, float)) else None,
                       "expires_at": str(item.get("expires_at") or "")[:64]})
    response = jsonify({"models": result})
    response.headers["Cache-Control"] = "no-store"
    return response


def _payload() -> dict:
    data = request.get_json(silent=True) if request.is_json else None
    return data if isinstance(data, dict) else request.form


@bp.post("/api/models/unload", endpoint="models_unload")
@admin_required
def unload():
    name = str(_payload().get("name") or "").strip()
    if not ollama.MODEL_NAME_RE.fullmatch(name):
        return security.json_error("Unknown model.", 400, "invalid_model")
    try:
        ollama.unload(name)
    except (UpstreamError, OSError) as error:
        return security.json_error(f"The model could not be unloaded: {error}", 502, "upstream")
    audit("model_unload", name)
    return jsonify({"ok": True, "message": f"{name} was unloaded from memory."})


@bp.post("/api/models/unload-all", endpoint="models_unload_all")
@admin_required
def unload_all():
    try:
        count = ollama.unload_all()
    except (UpstreamError, OSError) as error:
        return security.json_error(f"The models could not be unloaded: {error}", 502, "upstream")
    audit("model_unload_all", "", {"count": count})
    return jsonify({"ok": True, "message": f"Unloaded {count} model{'s' if count != 1 else ''}."})


# ----- downloads ------------------------------------------------------------------

@bp.get("/models/downloads", endpoint="model_downloads")
@admin_required
def downloads():
    config = _config()
    jobs = pulls_db.list_jobs(100)
    return _render(
        "admin/model_downloads.html", tab="downloads", jobs=jobs, disk=pulls.disk_space(config),
        checkpoint=_checkpoint_state(), queue=pulls.queue_status(), suggestions=pulls.suggestions(config),
        missing=pulls.missing_catalog_models(), max_bulk=pulls.MAX_BULK,
        page_data={"downloads_url": url_for("admin.pulls_status"),
                   "active_downloads": any(job["status"] in pulls_db.ACTIVE for job in jobs)})


@bp.post("/models/downloads", endpoint="pull_create")
@admin_required
def pull_create():
    raw = (request.form.get("name") or "").strip()
    try:
        name = pulls.validate_ollama_name(raw)
        job_id = pulls.enqueue_ollama(name, me()["id"])
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.model_downloads")
    audit("model_pull", name, {"job": job_id})
    flash(f"Download of {name} queued. Progress appears below.", "success")
    return back("admin.model_downloads")


@bp.post("/models/downloads/bulk", endpoint="pulls_bulk")
@admin_required
def pulls_bulk():
    names = [line for line in (request.form.get("names") or "").replace(",", "\n").splitlines()]
    names += request.form.getlist("models")
    try:
        if len(request.form.get("names") or "") > 20000:
            raise ValueError("The list is too long.")
        result = pulls.enqueue_many(names, me()["id"])
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.model_downloads")
    audit("model_pull_bulk", "", {"queued": [name for _id, name in result["queued"]],
                                  "skipped": [name for name, _reason in result["skipped"]],
                                  "errors": [name for name, _reason in result["errors"]]})
    parts = [f"{len(result['queued'])} download{'s' if len(result['queued']) != 1 else ''} queued."]
    if result["skipped"]:
        parts.append("Skipped: " + ", ".join(f"{name} ({reason})" for name, reason in result["skipped"][:10]) + ".")
    for name, reason in result["errors"][:5]:
        parts.append(f"{name}: {reason}")
    flash(" ".join(parts), "warning" if result["errors"] else "success")
    return back("admin.model_downloads")


@bp.post("/models/downloads/checkpoint", endpoint="pull_checkpoint")
@admin_required
def pull_checkpoint():
    form = request.form
    values = {key: (form.get(key) or "").strip() for key in ("repo_id", "source_filename", "revision", "target_name",
                                                            "expected_sha256", "expected_size")}
    try:
        job_id = pulls.enqueue_checkpoint(values, me()["id"])
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.model_downloads")
    job = pulls_db.get(job_id)
    audit("model_pull_checkpoint", job["ollama_name"],
          {"job": job_id, "repo": job["repo_id"], "revision": job["revision"], "sha256": job["expected_sha256"]})
    flash(f"Checkpoint download of {job['ollama_name']} queued.", "success")
    return back("admin.model_downloads")


@bp.get("/api/models/downloads", endpoint="pulls_status")
@admin_required
def pulls_status():
    response = jsonify({"jobs": [_job_view(job) for job in pulls_db.list_jobs(100)],
                        "disk": pulls.disk_space(_config()), "queue": pulls.queue_status()})
    response.headers["Cache-Control"] = "no-store"
    return response


def _job_or_404(job_id: int):
    job = pulls_db.get(job_id)
    if job is None:
        abort(404)
    return job


@bp.post("/models/downloads/<int:job_id>/cancel", endpoint="pull_cancel")
@admin_required
def pull_cancel(job_id):
    job = _job_or_404(job_id)
    cleanup = request.form.get("keep_partial") != "1"
    if pulls.cancel(job_id, cleanup=cleanup):
        audit("model_pull_cancel", job["ollama_name"], {"job": job_id, "remove_partial": cleanup})
        flash(f"Download of {job['ollama_name']} cancelled." + ("" if cleanup else
              " The partial download is kept; downloading it again resumes from there."), "success")
    else:
        flash("That download had already finished.", "info")
    return back("admin.model_downloads")


@bp.post("/models/downloads/<int:job_id>/pause", endpoint="pull_pause")
@admin_required
def pull_pause(job_id):
    job = _job_or_404(job_id)
    if request.form.get("resume") == "1":
        done, verb = pulls.resume(job_id), "resumed"
    else:
        done, verb = pulls.pause(job_id), "paused"
    if done:
        audit(f"model_pull_{'resume' if verb == 'resumed' else 'pause'}", job["ollama_name"], {"job": job_id})
        flash(f"Download of {job['ollama_name']} {verb}.", "success")
    else:
        flash("That download had already finished.", "info")
    return back("admin.model_downloads")


@bp.post("/models/downloads/<int:job_id>/move", endpoint="pull_move")
@admin_required
def pull_move(job_id):
    _job_or_404(job_id)
    direction = request.form.get("direction")
    if direction not in DIRECTIONS:
        abort(400)
    if not pulls.move(job_id, direction):
        flash("Only waiting downloads can be reordered.", "info")
    return back("admin.model_downloads", _anchor=f"job-{job_id}")


@bp.post("/models/downloads/queue", endpoint="pulls_queue")
@admin_required
def pulls_queue():
    action = request.form.get("action")
    if action == "pause_all":
        pulls.set_queue_paused(True, me())
        message = "Downloads paused. Running downloads stop and continue from where they were when you resume."
    elif action == "resume_all":
        pulls.set_queue_paused(False, me())
        message = "Downloads resumed."
    elif action == "cancel_all":
        cancelled = pulls.cancel_all(cleanup=request.form.get("keep_partial") != "1")
        message = f"Cancelled {len(cancelled)} download{'s' if len(cancelled) != 1 else ''}."
    else:
        abort(400)
    audit(f"model_pulls_{action}", "")
    flash(message, "success")
    return back("admin.model_downloads")


@bp.post("/models/downloads/<int:job_id>/retry", endpoint="pull_retry")
@admin_required
def pull_retry(job_id):
    job = _job_or_404(job_id)
    try:
        new_id = pulls.retry(job_id, me()["id"])
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.model_downloads")
    audit("model_pull_retry", job["ollama_name"], {"job": new_id, "previous": job_id})
    flash(f"Download of {job['ollama_name']} queued again.", "success")
    return back("admin.model_downloads")


@bp.post("/models/downloads/<int:job_id>/delete", endpoint="pull_delete")
@admin_required
def pull_delete(job_id):
    job = _job_or_404(job_id)
    if not pulls_db.delete_finished(job_id):
        flash("Only finished downloads can be removed from the list.", "error")
    else:
        audit("model_pull_delete", job["ollama_name"], {"job": job_id})
    return back("admin.model_downloads")


@bp.post("/models/downloads/clear", endpoint="pulls_clear")
@admin_required
def pulls_clear():
    count = pulls_db.clear_finished()
    audit("model_pulls_clear", "", {"count": count})
    flash(f"Removed {count} finished download{'s' if count != 1 else ''} from the list.", "success")
    return back("admin.model_downloads")


# ----- recovery after a restore ------------------------------------------------------

@bp.post("/models/recovery", endpoint="model_recovery")
@admin_required
def recovery():
    action = request.form.get("action")
    try:
        if action == "defer":
            model_recovery.defer(me()["id"])
            audit("model_recovery_defer", "")
            flash("Model downloads postponed. The list stays until you download or dismiss it.", "success")
        elif action == "dismiss":
            model_recovery.dismiss(me()["id"])
            audit("model_recovery_dismiss", "")
            flash("The list of missing models was dismissed. Download models any time below.", "success")
        elif action in ("download", "download_missing"):
            result = (model_recovery.download(request.form.getlist("items"), me()["id"]) if action == "download"
                      else model_recovery.download_missing(me()["id"]))
            audit("model_recovery_download", "", {"queued": [item["model"] for item in result["queued"]],
                                                   "skipped": result["skipped"]})
            parts = [f"{len(result['queued'])} download(s) queued."]
            if result["skipped"]:
                parts.append(f"Already installed: {', '.join(result['skipped'][:10])}.")
            for item in result["errors"][:5]:
                parts.append(f"{item['model']}: {item['reason']}")
            flash(" ".join(parts), "warning" if result["errors"] else "success")
        else:
            abort(400)
    except ValueError as error:
        flash(str(error), "error")
    if request.form.get("return_to") == "dashboard":
        return back("admin.dashboard")
    if action in ("download", "download_missing"):
        return back("admin.model_downloads")
    return back("admin.models", _anchor="recovery")


# ----- categories -------------------------------------------------------------------

def _category_or_404(category_id: int):
    category = catalog.get_category(category_id)
    if category is None:
        abort(404)
    return category


def _category_form():
    name = text("name", max_length=80, required=True, label="Name")
    description = text("description", max_length=500, label="Description")
    scope = choice("scope", catalog.CATEGORY_SCOPES, label="Where it applies")
    return name, description, scope


@bp.get("/models/categories", endpoint="categories")
@admin_required
def categories():
    return render_template("admin/categories.html", section="models", rows=catalog.list_categories())


@bp.post("/models/categories/create", endpoint="category_create")
@admin_required
def category_create():
    try:
        name, description, scope = _category_form()
        category_id = catalog.create_category(name, description, scope)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.categories")
    audit("category_create", name, {"id": category_id, "scope": scope})
    flash(f"Category {name} created. Assign models to it from their edit page.", "success")
    return back("admin.categories")


@bp.route("/models/categories/<int:category_id>/edit", methods=["GET", "POST"], endpoint="category_edit")
@admin_required
def category_edit(category_id):
    category = _category_or_404(category_id)
    if request.method == "POST":
        try:
            name, description, scope = _category_form()
            catalog.update_category(category_id, name, description, scope)
        except (FormError, ValueError) as error:
            flash(str(error), "error")
            return render_template("admin/category_edit.html", section="models", category=category,
                                   form=request.form), 400
        audit("category_edit", name, {"id": category_id, "scope": scope})
        flash(f"Category {name} saved.", "success")
        return back("admin.categories")
    return render_template("admin/category_edit.html", section="models", category=category, form=None)


@bp.post("/models/categories/<int:category_id>/delete", endpoint="category_delete")
@admin_required
def category_delete(category_id):
    category = _category_or_404(category_id)
    catalog.delete_category(category_id)
    audit("category_delete", category["name"], {"id": category_id})
    flash(f"Category {category['name']} deleted; its models and access rules for it were detached.", "success")
    return back("admin.categories")


@bp.post("/models/categories/<int:category_id>/move", endpoint="category_move")
@admin_required
def category_move(category_id):
    _category_or_404(category_id)
    direction = request.form.get("direction")
    if direction not in DIRECTIONS:
        abort(400)
    ids = [row["id"] for row in catalog.list_categories()]
    catalog.reorder_categories(_moved(ids, category_id, direction))
    return back("admin.categories", _anchor=f"category-{category_id}")
