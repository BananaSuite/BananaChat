"""Limits: models (weight, per-model limits, presets), reasoning effort, tiers, grants and the limits of one
account (custom limits per service and per model, effort levels, usage resets, speed, tier, exemption)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from flask import abort, flash, render_template, request

from bananachat import db
from bananachat.db import catalog, users
from bananachat.db import limits as limits_db
from bananachat.db import settings as site_settings
from bananachat.numbers import ascii_int
from bananachat.security import admin_required
from bananachat.services import limits

from . import bp
from ._helpers import (
    FormError,
    audit,
    back,
    choice,
    flag,
    integer,
    me,
    number,
    text,
    tokens,
    user_or_404,
    utc_datetime,
    utc_input_min,
)
from ._quota_ui import (
    POOL_LABELS,
    quota_tabs_context,
    rate_form_context,
    rules_en,
    rules_from_form,
    tokens_en,
    typed,
    typed_rules,
)

DURATIONS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
PRESET_HELP = {
    "light": "Small, fast models. Usage counts half (×0.5) against the service limits; no model-specific limits.",
    "standard": "Most models. Usage counts ×1; no model-specific limits.",
    "heavy": "Large or slow models. Usage counts ×3, at most 6 requests a minute and 200 a day, 200k tokens per "
             "5 hours, follows demand twice as strongly, reasoning effort up to Low unless unlocked.",
}
SCOPE_LABELS = {"rate": "request rate", "window": "5-hour tokens", "weekly": "weekly tokens"}


# The account page speaks to the account holder ("your account"); here an administrator reads about someone else.
_ADMIN_REASONS = {"custom": "Custom limit for this account", "custom_floor": "Custom limit, never lowered by demand"}


def _reason_texts(reasons) -> list[str]:
    return [_ADMIN_REASONS.get(reason.code) or reason.text("en") for reason in reasons
            if reason.code not in ("not_limited",)]


def _models():
    """Catalog models that limits apply to (text and image models, local, through an external API or a subscription
    pool), in catalog order. External and subscription models have policies too (their presets fill them), and
    their pages link here."""
    return [model for model in catalog.list_models()
            if model["backend"] in ("ollama", "comfyui", "external", "claude", "codex")]


def _model_or_404(model_id: int):
    model = catalog.get(model_id)
    if model is None:
        abort(404)
    return model


# ----- tiers ---------------------------------------------------------------------------------

@bp.get("/quotas/tiers", endpoint="limit_tiers")
@admin_required
def tiers():
    return _tiers_page()


def _tiers_page(typed_tiers: dict | None = None):
    """The tiers page; *typed_tiers* (tier id: values as typed) shows a refused Save again."""
    policies = limits_db.all_policies()
    labels = {"window": "5-hour", "weekly": "weekly"}
    applies = [f"{POOL_LABELS[pool]} ({', '.join(labels[period] for period in limits_db.PERIODS if policy[period]['auto_tiers'])})"
               for pool, policy in policies.items() if policy["window"]["auto_tiers"] or policy["weekly"]["auto_tiers"]]
    applies += [f"{model['display_name']} (model limits)" for model in _models()
                if (policy := limits_db.get_model_policy(model))["auto_tiers"] and policy["enabled"]]
    tiers = [{**tier.to_dict(), **(typed_tiers or {}).get(tier["id"], {})} for tier in limits_db.list_tiers()]
    return render_template("admin/limit_tiers.html", tab="tiers", tiers=tiers, typed=bool(typed_tiers),
                           counts=limits_db.tier_counts(), applies=applies, **quota_tabs_context())


TIER_FIELDS = ("name", "multiplier", "min_account_days", "min_active_days", "min_tokens_30d", "clean_days")


def _tier_form(prefix: str = "") -> dict:
    """A tier's fields; on the tiers page's one form each tier's start with ``tier-<id>-`` (*prefix*)."""
    def field(name):
        return prefix + name
    return {"name": text(field("name"), max_length=limits_db.TIER_NAME_MAX, required=True, label="Name"),
            "multiplier": number(field("multiplier"), minimum=0, maximum=limits_db.TIER_MULTIPLIER_MAX,
                                 label="Multiplier"),
            "min_account_days": integer(field("min_account_days"), minimum=0, maximum=3650, label="Account age",
                                        default=0),
            "min_active_days": integer(field("min_active_days"), minimum=0, maximum=30, label="Active days",
                                       default=0),
            "min_tokens_30d": tokens(field("min_tokens_30d"), label="Tokens used", default=0),
            "clean_days": integer(field("clean_days"), minimum=0, maximum=3650, label="Days without suspension",
                                  default=0)}


@bp.post("/quotas/tiers/save", endpoint="limit_tiers_save")
@admin_required
def save_tiers():
    """The tiers page's one Save: every tier, each checked before any is saved; only changed tiers are written
    and audited."""
    stored = {tier["id"]: tier for tier in limits_db.list_tiers()}
    changes, errors = {}, []
    for tier_id, tier in stored.items():
        prefix = f"tier-{tier_id}-"
        if prefix + "name" not in request.form:
            continue  # added after the page was loaded: kept as it is
        try:
            values = limits_db.validate_tier(_tier_form(prefix))
        except (FormError, ValueError) as error:
            errors.append(f"{tier['name']}: {error}")
            continue
        if any(values[name] != limits_db.validate_tier(tier.to_dict())[name] for name in TIER_FIELDS):
            changes[tier_id] = values
    if errors:
        for error in errors:
            flash(error, "error")
        typed_tiers = {tier_id: {name: typed(f"tier-{tier_id}-{name}") for name in TIER_FIELDS}
                       for tier_id in stored if f"tier-{tier_id}-name" in request.form}
        return _tiers_page(typed_tiers), 400
    with db.transaction():
        for tier_id, values in changes.items():
            limits_db.update_tier(tier_id, values)
    for tier_id, values in changes.items():
        audit("limits_tier_update", stored[tier_id]["name"], values)
    flash("Tiers saved." if changes else "No changes to save.", "success")
    return back("admin.limit_tiers")


@bp.post("/quotas/tiers", endpoint="limit_tier_create")
@admin_required
def create_tier():
    try:
        values = _tier_form()
        tier_id = limits_db.create_tier(values)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.limit_tiers")
    audit("limits_tier_create", values["name"], {"id": tier_id, **values})
    flash(f"Tier {values['name']} added.", "success")
    return back("admin.limit_tiers")


@bp.post("/quotas/tiers/<int:tier_id>", endpoint="limit_tier_update")
@admin_required
def update_tier(tier_id):
    tier = limits_db.get_tier(tier_id)
    if tier is None:
        abort(404)
    action = request.form.get("action", "save")
    try:
        if action == "delete":
            limits_db.delete_tier(tier_id)
            details = {"id": tier_id}
        elif action in ("up", "down"):
            limits_db.move_tier(tier_id, -1 if action == "up" else 1)
            details = {"id": tier_id, "move": action}
        elif action == "save":
            details = _tier_form()
            limits_db.update_tier(tier_id, details)
        else:
            abort(400)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.limit_tiers")
    audit(f"limits_tier_{'update' if action == 'save' else action}", tier["name"], details)
    flash({"delete": f"Tier {tier['name']} deleted. Its accounts moved to the first tier.",
           "save": f"Tier {tier['name']} saved."}.get(action, "Tier order changed."), "success")
    return back("admin.limit_tiers")


@bp.post("/quotas/tiers/promote", endpoint="limit_tiers_promote")
@admin_required
def promote_now():
    promoted = limits.promote()
    audit("limits_tiers_promote", "everyone", {"promoted": len(promoted)})
    if not limits.auto_tiers_enabled():
        flash("No service raises limits with tiers, so nobody was promoted.", "info")
    else:
        flash(f"{len(promoted)} account{'s' if len(promoted) != 1 else ''} moved up.", "success")
    return back("admin.limit_tiers")


# ----- grants -------------------------------------------------------------------------------

def _grant_view(row) -> dict:
    target = row["username"] if row["user_id"] else "Everyone"
    if row["kind"] == "unlimited":
        what = "Unlimited"
    elif row["kind"] == "multiplier":
        what = f"×{row['amount']:g}"
    else:
        what = f"+{tokens_en(row['amount'])}"
    scope = SCOPE_LABELS.get(row["scope"], "all limits")
    if row["kind"] not in ("unlimited", "multiplier") and row["scope"] in ("window", "weekly"):
        # "+50k tokens per 5 hours", not "+50k tokens · 5-hour tokens": the amount names its period.
        what, scope = f"{what} {'per 5 hours' if row['scope'] == 'window' else 'per week'}", None
    if row["model_id"]:
        where = f"model {row['model_name'] or '(removed)'}"
    else:
        where = POOL_LABELS.get(row["pool"], "every service") if row["pool"] else "every service"
    return {"row": row, "target": target, "what": what, "scope": scope, "pool": where}


@bp.get("/quotas/grants", endpoint="limit_grants")
@admin_required
def grants():
    return _grants_page()


def _grants_page(form=None):
    """The grants page; *form* holds what was typed when a new grant was refused, so nothing is lost."""
    prefill = (request.args.get("user") or "").strip()[:64]
    return render_template(
        "admin/limit_grants.html", tab="grants",
        active=[_grant_view(row) for row in limits_db.list_grants("active")],
        upcoming=[_grant_view(row) for row in limits_db.list_grants("upcoming")],
        ended=[_grant_view(row) for row in limits_db.list_grants("ended", limit=50)],
        prefill=prefill, form=form, pools=limits_db.POOLS, models=_models(), min_datetime=utc_input_min(),
        **quota_tabs_context())


def _grant_form() -> dict:
    target = choice("target", ("user", "everyone"), label="Who")
    user_id = None
    username = ""
    if target == "user":
        username = text("username", max_length=64, required=True, label="Username")
        user = users.get_by_username(username)
        if user is None:
            raise FormError(f"There is no account called {username}.")
        user_id, username = user["id"], user["username"]
    where = request.form.get("pool") or ""
    pool = model = None
    if where.startswith("model:"):
        model = catalog.get(ascii_int(where.partition(":")[2], 18) or 0)
        if model is None:
            raise FormError("That model does not exist.")
    elif where:
        pool = choice("pool", limits_db.POOLS, label="Service")
    scope = choice("scope", ("", *limits_db.SCOPES), label="Limit", default="") or None
    kind = choice("kind", limits_db.GRANT_KINDS, label="Kind")
    if kind == "unlimited":
        amount = 0.0
    elif kind == "multiplier":
        amount = number("amount", minimum=1.01, maximum=limits_db.MULTIPLIER_MAX, label="Multiplier")
    else:
        amount = tokens("amount", label="Extra tokens")
    starts = utc_datetime("starts_at", label="Start") or datetime.now(timezone.utc)
    duration = choice("duration", (*DURATIONS, "custom", "forever"), label="Duration")
    if duration in DURATIONS:
        try:
            ends = starts + DURATIONS[duration]
        except OverflowError:
            raise FormError("The end of this grant is out of range.") from None
    elif duration == "custom":
        ends = utc_datetime("ends_at", label="End")
        if ends is None:
            raise FormError("Choose when a custom grant ends.")
    else:
        ends = None
    if kind == "extra" and scope in ("window", "weekly") and not limits.limit_on(user_id, pool, scope, model):
        what = {"window": "5-hour", "weekly": "weekly"}[scope]
        place = model["display_name"] if model else (POOL_LABELS[pool] if pool else "No service")
        raise FormError(f"{place} has no {what} limit{' for ' + username if username else ''}, "
                        "so extra tokens would do nothing.")
    return {"user_id": user_id, "username": username, "pool": pool, "model_id": model["id"] if model else None,
            "scope": scope, "kind": kind, "amount": amount, "starts_at": db.timestamp(starts),
            "ends_at": db.timestamp(ends) if ends else None,
            "reason": text("reason", max_length=limits_db.REASON_MAX, label="Reason")}


@bp.post("/quotas/grants", endpoint="limit_grant_create")
@admin_required
def create_grant():
    try:
        values = _grant_form()
        username = values.pop("username")
        grant_id = limits_db.create_grant(created_by=me()["id"], **values)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return _grants_page(request.form), 400
    audit("limits_grant_create", username or "everyone", {"id": grant_id, **values})
    flash(f"Grant #{grant_id} created for {username or 'everyone'}.", "success")
    if values["user_id"] and request.form.get("next") == "user":
        return back("admin.user_limits", user_id=values["user_id"])
    return back("admin.limit_grants")


@bp.post("/quotas/grants/<int:grant_id>/revoke", endpoint="limit_grant_revoke")
@admin_required
def revoke_grant(grant_id):
    grant = limits_db.get_grant(grant_id)
    if grant is None:
        abort(404)
    if limits_db.revoke_grant(grant_id, me()["id"]):
        audit("limits_grant_revoke", grant["username"] or "everyone", {"id": grant_id})
        flash(f"Grant #{grant_id} revoked.", "success")
    else:
        flash("That grant was already revoked.", "info")
    if request.form.get("next") == "user" and grant["user_id"]:
        return back("admin.user_limits", user_id=grant["user_id"])
    return back("admin.limit_grants")


# ----- models -------------------------------------------------------------------------------

def model_summary(model, policy: dict) -> list[str]:
    """Scan-friendly facts for a closed model row; exact rates stay in its editor."""
    facts = [f"Token weight ×{policy['weight']:g}"]
    if not limits.counts_toward_pool(model, policy):
        facts = ["Service tokens excluded"]
    if not policy["tokens_enabled"]:
        facts.append("Model tokens off")
    if not policy["rate_enabled"]:
        facts.append("Model rates off")
    if policy["enabled"]:
        if policy["rate_enabled"] and policy["rate_rules"]:
            count = len(policy["rate_rules"])
            facts.append(f"{count} rate rule{'s' if count != 1 else ''}")
        if policy["tokens_enabled"] and policy["window_tokens"] is not None:
            facts.append(f"{tokens_en(policy['window_tokens'])} / 5h")
        if policy["tokens_enabled"] and policy["weekly_tokens"] is not None:
            facts.append(f"{tokens_en(policy['weekly_tokens'])} / week")
        if not (policy["rate_rules"] or policy["window_tokens"] is not None or policy["weekly_tokens"] is not None):
            facts.append("No model limits set")
    if limits.supported_efforts(model):
        effort = limits.effort_settings()
        default = policy["effort_default"] or effort["default"]
        facts.append("All reasoning levels" if not effort["gating"] or policy["effort_gating_off"] else
                     f"Reasoning up to {limits.effort_label(default)}")
    return facts


@bp.get("/quotas/models", endpoint="limit_models")
@admin_required
def models_page():
    return _models_page()


def _models_page():
    """Every model's limits in one line each; each model's settings have their own page."""
    models = _models()
    policies = limits_db.model_policies(models)
    stored = {row[0] for row in db.query("SELECT model_id FROM model_limit_policy")}
    items = [{"model": model, "policy": policies[model["id"]], "configured": model["id"] in stored,
              "facts": model_summary(model, policies[model["id"]])} for model in models]
    return render_template("admin/limit_models.html", tab="models", items=items, presets=limits_db.PRESETS,
                           preset_help=PRESET_HELP, **quota_tabs_context())


@bp.get("/quotas/models/<int:model_id>", endpoint="limit_model")
@admin_required
def model_page(model_id):
    return _model_page(_model_or_404(model_id))


def _model_page(model, refused: dict | None = None):
    """One model's limits; *refused* (a policy as typed) shows a refused Save again."""
    if model not in _models():
        abort(404)
    stored = db.scalar("SELECT 1 FROM model_limit_policy WHERE model_id=?", (model["id"],))
    return render_template("admin/limit_model.html", tab="models", model=model,
                           policy=refused or limits_db.get_model_policy(model), refused=bool(refused),
                           configured=bool(stored), counts_default=limits.is_local(model),
                           efforts_supported=limits.supported_efforts(model), presets=limits_db.PRESETS,
                           preset_help=PRESET_HELP, efforts=limits_db.EFFORT_LEVELS,
                           site_effort=limits.effort_settings()["default"], **quota_tabs_context())


def _model_policy_form(current: dict) -> dict:
    counts = choice("counts_toward_pool", ("default", "yes", "no"), label="Counts towards service limits",
                    default="default")
    effort = choice("effort_default", ("", *limits_db.EFFORT_LEVELS), label="Default reasoning effort", default="")
    controls = _model_controls(current)
    return {**current, **controls, "preset": "custom", "enabled": flag("enabled"),
            "weight": number("weight", minimum=limits_db.WEIGHT_MIN, maximum=limits_db.WEIGHT_MAX, label="Weight"),
            "counts_toward_pool": None if counts == "default" else counts == "yes",
            "rate_rules": rules_from_form(),
            "window_tokens": tokens("window_tokens", label="Tokens per 5 hours", optional=True),
            "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
            "dynamic": flag("dynamic"),
            "sensitivity": number("sensitivity", minimum=0, maximum=limits_db.SENSITIVITY_MAX, label="Sensitivity"),
            "auto_tiers": flag("auto_tiers"), "effort_default": effort or None}


def _model_controls(current: dict) -> dict:
    """Older forms leave new independent controls unchanged."""
    names = ("tokens_enabled", "rate_enabled", "effort_gating_off", "effort_auto_unlock_off")
    return {name: flag(name) if "limit_controls" in request.form else current[name] for name in names}


@bp.post("/quotas/models/<int:model_id>", endpoint="limit_model_save")
@admin_required
def save_model(model_id):
    model = _model_or_404(model_id)
    action = request.form.get("action", "save")
    current = limits_db.get_model_policy(model)
    try:
        if action in limits_db.PRESETS:
            config = limits.apply_model_preset(model_id, action, updated_by=me()["id"])
            message = f"{model['display_name']}: {action} preset applied."
        elif action == "reset":
            limits_db.clear_model_policy(model_id)
            config = limits_db.get_model_policy(model)
            message = f"{model['display_name']} follows its default settings again."
        elif action == "save":
            config = limits_db.set_model_policy(model_id, _model_policy_form(current), me()["id"])
            message = f"{model['display_name']}: settings saved."
        else:
            abort(400)
    except (FormError, ValueError) as error:
        flash(f"{model['display_name']}: {error}", "error")
        if action != "save":
            return back("admin.limit_model", model_id=model_id)
        counts = request.form.get("counts_toward_pool")
        return _model_page(model, {
            **current, **_model_controls(current), "weight": typed("weight"), "sensitivity": typed("sensitivity"),
            "enabled": flag("enabled"),
            "dynamic": flag("dynamic"), "auto_tiers": flag("auto_tiers"), "rate_rules": typed_rules(),
            "window_tokens": typed("window_tokens"), "weekly_tokens": typed("weekly_tokens"),
            "counts_toward_pool": {"yes": True, "no": False}.get(counts),
            "effort_default": request.form.get("effort_default") or None}), 400
    audit("limits_model", model["ollama_name"], {"action": action, **config})
    flash(message, "success")
    if action == "save":
        return back("admin.limit_models", _anchor=f"model-{model_id}")
    return back("admin.limit_model", model_id=model_id)


# ----- reasoning effort ---------------------------------------------------------------------

@bp.get("/quotas/effort", endpoint="limit_effort")
@admin_required
def effort_page():
    return _effort_page()


def _effort_page(refused: dict | None = None, typed_models: dict | None = None):
    """The reasoning-effort page; *refused* is the settings form as typed, when it was refused, and
    *typed_models* (model id: ``{"default", "policy"}``) the model defaults as typed."""
    rows = db.query("SELECT e.*, u.username, m.display_name AS model_name, a.username AS updated_by_name "
                    "FROM user_effort_levels e JOIN users u ON u.id=e.user_id "
                    "LEFT JOIN ai_models m ON m.id=e.model_id LEFT JOIN users a ON a.id=e.updated_by "
                    "ORDER BY e.updated_at DESC, e.id DESC LIMIT 100")
    reasoning = [model for model in _models() if limits.supported_efforts(model)]
    policies = limits_db.model_policies(reasoning)
    return render_template("admin/limit_effort.html", tab="effort", settings=limits.effort_settings(), typed_settings=refused,
                           levels=limits_db.EFFORT_LEVELS, unlocks=rows,
                           models=[{"model": model, "levels": limits.supported_efforts(model),
                                    "default": policies[model["id"]]["effort_default"],
                                    "policy": policies[model["id"]],
                                    **(typed_models or {}).get(model["id"], {})} for model in reasoning],
                           **quota_tabs_context())


EFFORT_MODEL_FLAGS = ("effort_gating_off", "effort_auto_unlock_off")


def _effort_model_changes(errors: list) -> dict:
    """The model defaults of the effort page's form that differ from the saved ones: {model: changes}. Each
    model's fields start with ``model-<id>-``. Refused values go to *errors*."""
    changes = {}
    for raw in request.form.getlist("effort_models"):
        model = catalog.get(ascii_int(raw, 18) or 0)
        if model is None or not limits.supported_efforts(model):
            continue
        prefix = f"model-{model['id']}-"
        try:
            level = choice(prefix + "effort_default", ("", *limits_db.EFFORT_LEVELS), label="Level", default="")
        except FormError as error:
            errors.append(f"{model['display_name']}: {error}")
            continue
        wanted = {"effort_default": level or None, **{name: flag(prefix + name) for name in EFFORT_MODEL_FLAGS}}
        current = limits_db.get_model_policy(model)
        if any(current[name] != value for name, value in wanted.items()):
            changes[model["id"]] = (model, current, wanted)
    return changes


def _typed_effort_models() -> dict:
    typed_models = {}
    for raw in request.form.getlist("effort_models"):
        model_id = ascii_int(raw, 18)
        if model_id is None:
            continue
        prefix = f"model-{model_id}-"
        level = request.form.get(prefix + "effort_default") or None
        typed_models[model_id] = {"default": level, "policy": {
            "effort_default": level, **{name: flag(prefix + name) for name in EFFORT_MODEL_FLAGS}}}
    return typed_models


@bp.post("/quotas/effort", endpoint="limit_effort_save")
@admin_required
def save_effort():
    """The effort page's one Save: the default access and, when the form has them, the model defaults. All are
    checked before anything is saved."""
    errors = []
    try:
        values = {
            "effort_gating_enabled": 1 if flag("effort_gating_enabled") else 0,
            "effort_default_level": choice("effort_default_level", limits_db.EFFORT_LEVELS, label="Default level"),
            "effort_auto_unlock": 1 if flag("effort_auto_unlock") else 0,
            "effort_auto_active_days": integer("effort_auto_active_days", minimum=1, maximum=365,
                                               label="Active days"),
            "effort_auto_tokens": tokens("effort_auto_tokens", label="Tokens with the model"),
            "effort_auto_period_days": integer("effort_auto_period_days", minimum=1, maximum=365,
                                               label="Period"),
            "effort_auto_clean_days": integer("effort_auto_clean_days", minimum=0, maximum=3650,
                                              label="Days without suspension"),
            "effort_auto_ceiling": choice("effort_auto_ceiling", limits_db.EFFORT_LEVELS[1:],
                                          label="Highest automatic level"),
        }
        if values["effort_auto_active_days"] > values["effort_auto_period_days"]:
            raise FormError("The active days cannot be more than the days of the period.")
    except FormError as error:
        errors.append(str(error))
    model_changes = _effort_model_changes(errors)
    if errors:
        for error in errors:
            flash(error, "error")
        return _effort_page({
            "gating": flag("effort_gating_enabled"), "default": request.form.get("effort_default_level"),
            "auto": flag("effort_auto_unlock"), "active_days": typed("effort_auto_active_days"),
            "tokens": typed("effort_auto_tokens"), "period_days": typed("effort_auto_period_days"),
            "clean_days": typed("effort_auto_clean_days"), "ceiling": request.form.get("effort_auto_ceiling")},
            _typed_effort_models()), 400
    current = site_settings.get()
    try:
        with db.transaction():
            site_settings.update(**values)
            for model, policy, wanted in model_changes.values():
                limits_db.set_model_policy(model["id"], {**policy, **wanted}, me()["id"])
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.limit_effort")
    if any(current.get(name) != value for name, value in values.items()):
        audit("limits_effort_settings", "site", values)
    for model, _policy, wanted in model_changes.values():
        audit("limits_effort_model", model["ollama_name"], {"default": wanted["effort_default"],
                                                            **{name: wanted[name] for name in EFFORT_MODEL_FLAGS}})
    flash("Reasoning effort settings saved.", "success")
    return back("admin.limit_effort")


@bp.post("/quotas/effort/unlock-now", endpoint="limit_effort_auto")
@admin_required
def effort_auto_now():
    unlocked = limits.auto_unlock_effort()
    audit("limits_effort_auto", "everyone", {"unlocked": len(unlocked)})
    if not limits.effort_settings()["auto"] or not limits.effort_settings()["gating"]:
        flash("Automatic unlocks are off, so nothing changed.", "info")
    else:
        flash(f"{len(unlocked)} level{'s' if len(unlocked) != 1 else ''} unlocked.", "success")
    return back("admin.limit_effort")


@bp.post("/quotas/effort/models/<int:model_id>", endpoint="limit_effort_model")
@admin_required
def effort_model(model_id):
    """Everyone's default level for one model (the model policy's ``effort_default``)."""
    model = _model_or_404(model_id)
    try:
        level = choice("effort_default", ("", *limits_db.EFFORT_LEVELS), label="Level", default="") or None
        current = limits_db.get_model_policy(model)
        controls = {name: flag(name) for name in ("effort_gating_off", "effort_auto_unlock_off")} \
            if "limit_controls" in request.form else {}
        limits_db.set_model_policy(model_id, {**current, **controls, "effort_default": level}, me()["id"])
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.limit_effort")
    audit("limits_effort_model", model["ollama_name"], {"default": level, **controls})
    flash(f"Reasoning settings for {model['display_name']} saved.", "success")
    return back("admin.limit_effort", _anchor="models")


# ----- one account --------------------------------------------------------------------------

def _pool_view(user, pool: str) -> dict:
    current = limits.effective(user, pool)
    override = limits_db.get_override(user["id"], pool) or limits_db.Override(pool)
    return {"pool": pool, "label": POOL_LABELS[pool], "current": current, "override": override,
            "policy": limits_db.get_policy(pool),
            "rate_text": rules_en(current.rate.rules) if current.rate.limited else "Not limited",
            "reasons": {"rate": _reason_texts(current.rate.reasons), "window": _reason_texts(current.window.reasons),
                        "weekly": _reason_texts(current.weekly.reasons)}}


def _model_views(user, models, policies) -> list[dict]:
    """Models with limits for this account: model limits on, a custom limit, a lock, or a weight other than 1."""
    overrides = limits_db.model_overrides_for(user["id"])
    views = []
    for model in models:
        policy = policies[model["id"]]
        override = overrides.get(model["id"])
        if not (policy["enabled"] or override is not None):
            continue
        current = limits.model_limits(user, model, policy=policy)
        views.append({"model": model, "policy": policy, "override": override or limits_db.ModelOverride(model["id"]),
                      "current": current,
                      "rate_text": rules_en(current.rate.rules) if current.rate.limited else "Not limited",
                      "reasons": {"rate": _reason_texts(current.rate.reasons),
                                  "window": _reason_texts(current.window.reasons),
                                  "weekly": _reason_texts(current.weekly.reasons)}})
    return views


def _effort_views(user, models, policies, prefs) -> list[dict]:
    own = limits_db.effort_levels(user["id"])
    views = []
    for model in models:
        if not limits.supported_efforts(model):
            continue
        ceiling = limits.effort_ceiling(user, model, policy=policies[model["id"]], own=own, prefs=prefs)
        allowed = limits.allowed_efforts(user, model, policy=policies[model["id"]], own=own, prefs=prefs)
        # "Up to" in the model's own levels (an off/on model shows On or Off, not the account-wide Low).
        views.append({"model": model, "ceiling": ceiling, "top": allowed[-1] if allowed else None,
                      "own": own.get(model["id"]), "levels": limits.supported_efforts(model)})
    return views


@bp.get("/users/<user_id>/limits", endpoint="user_limits")
@admin_required
def user_limits(user_id):
    return _user_limits_page(user_or_404(user_id))


def _user_limits_page(user, refused: dict | None = None):
    """An account's limits page; *refused* shows a refused form again as typed: ``{"pool": ..., "override":
    ...}`` or ``{"model_id": ..., "override": ...}`` from one section's old form, or ``{"pools": {pool:
    override}, "models": {model_id: override}, "settings": {...}}`` from the page's Save."""
    refused = refused or {}
    user_id = user["id"]
    saved = limits_db.user_settings(user_id)
    settings = replace(saved, **refused["settings"]) if refused.get("settings") else saved
    models = _models()
    policies = limits_db.model_policies(models)
    return render_template(
        "admin/user_limits.html", section="users", user=user, settings=settings, saved=saved, refused=refused,
        pools=[_pool_view(user, pool) for pool in limits_db.POOLS], tiers=limits_db.list_tiers(),
        tier=limits.resolve_tier(settings.tier_id), saved_tier=limits.resolve_tier(saved.tier_id),
        progress=limits.tier_progress(user),
        auto_tiers=limits.auto_tiers_enabled(), models=models,
        model_views=_model_views(user, models, policies),
        effort_views=_effort_views(user, models, policies, settings),
        effort_all=limits_db.effort_levels(user_id).get(None), effort_settings=limits.effort_settings(),
        efforts=limits_db.EFFORT_LEVELS,
        grants=[_grant_view(row) for row in limits_db.list_grants("active", user_id=user_id)] +
        [_grant_view(row) for row in limits_db.list_grants("upcoming", user_id=user_id)],
        limits={"tokens": limits_db.TOKENS_MAX, "requests": limits_db.REQUESTS_MAX, "burst": limits_db.BURST_MAX},
        **rate_form_context())


def _typed_override(prefix: str = "") -> dict:
    """A refused custom-limit form as typed, in the shape of an override."""
    return {"rate_rules": typed_rules(prefix + "rule") or None, "window_tokens": typed(prefix + "window_tokens"),
            "weekly_tokens": typed(prefix + "weekly_tokens")}


# ----- the account limits page's Save -------------------------------------------------------------
# The page's one form holds the exemptions, each service's and each customised model's limits, the reasoning
# switches, the queue lane, the tier and the demand exemption. Every section is read and checked first; then only
# what changed is saved, in one transaction, with each section's own audit entry. Sections a form does not have
# (marked by their hidden fields) are left as they are.

MODEL_OVERRIDE_FLAGS = ("locked", "token_exempt", "rate_exempt")
EFFORT_ACCOUNT_FLAGS = ("effort_gating_off", "effort_auto_unlock_off")


def _pool_values(prefix: str) -> dict:
    return {"rate_rules": rules_from_form(prefix + "rule") or None,
            "window_tokens": tokens(prefix + "window_tokens", maximum=limits_db.OVERRIDE_WINDOW_MAX,
                                    label="Tokens per 5 hours", optional=True),
            "weekly_tokens": tokens(prefix + "weekly_tokens", label="Tokens per week", optional=True)}


def _model_values(prefix: str) -> dict:
    return {"rate_rules": rules_from_form(prefix + "rule") or None,
            "window_tokens": tokens(prefix + "window_tokens", label="Tokens per 5 hours", optional=True),
            "weekly_tokens": tokens(prefix + "weekly_tokens", label="Tokens per week", optional=True),
            **{name: flag(prefix + name) for name in MODEL_OVERRIDE_FLAGS}}


def _changed(current, values: dict) -> bool:
    """Whether *values* differ from *current* (an override or the account's settings)."""
    def same(name, value):
        stored = getattr(current, name)
        if name == "rate_rules":
            return list(stored or []) == list(value or [])
        return stored == value
    return not all(same(name, value) for name, value in values.items())


def _typed_account_settings() -> dict:
    shown = {name: flag(name) for name in ("token_exempt", "rate_exempt", "dynamic_exempt", "tier_locked")}
    if "effort_gating_present" in request.form:
        shown.update({name: flag(name) for name in EFFORT_ACCOUNT_FLAGS})
    if request.form.get("speed") in limits_db.SPEEDS:
        shown["speed"] = request.form["speed"]
    tier_id = ascii_int(request.form.get("tier_id") or "", 18)
    if tier_id is not None and limits_db.get_tier(tier_id) is not None:
        shown["tier_id"] = tier_id
    return shown


@bp.post("/users/<user_id>/limits", endpoint="user_limits_save")
@admin_required
def save_user_limits(user_id):
    user = user_or_404(user_id)
    settings = limits_db.user_settings(user_id)
    actor = me()["id"]
    errors, pools, models = [], {}, {}
    for pool in [pool for pool in request.form.getlist("pools") if pool in limits_db.POOLS]:
        try:
            values = _pool_values(f"{pool}-")
        except FormError as error:
            errors.append(f"{POOL_LABELS[pool]}: {error}")
            continue
        if _changed(limits_db.get_override(user_id, pool) or limits_db.Override(pool), values):
            pools[pool] = values
    for raw in request.form.getlist("override_models"):
        model = catalog.get(ascii_int(raw, 18) or 0)
        if model is None:
            continue
        try:
            values = _model_values(f"model-{model['id']}-")
        except FormError as error:
            errors.append(f"{model['display_name']}: {error}")
            continue
        current = limits_db.get_model_override(user_id, model["id"]) or limits_db.ModelOverride(model["id"])
        if _changed(current, values):
            models[model["id"]] = (model, values)
    account = {name: flag(name) for name in ("token_exempt", "rate_exempt", "dynamic_exempt")}
    if "effort_gating_present" in request.form:
        account.update({name: flag(name) for name in EFFORT_ACCOUNT_FLAGS})
    speed, tier = settings.speed, None
    try:
        speed = choice("speed", limits_db.SPEEDS, label="Speed", default=settings.speed)
        tier_id = integer("tier_id", minimum=1, maximum=2**31, label="Tier", optional=True)
        if tier_id is not None:
            tier = limits_db.get_tier(tier_id)
            if tier is None:
                raise FormError("That tier does not exist.")
            account.update(tier_locked=flag("tier_locked"))
    except FormError as error:
        errors.append(str(error))
    if errors:
        for error in errors:
            flash(error, "error")
        return _user_limits_page(user, {
            "pools": {pool: _typed_override(f"{pool}-") for pool in request.form.getlist("pools")
                      if pool in limits_db.POOLS},
            "models": {model_id: {**_typed_override(f"model-{model_id}-"),
                                  **{name: flag(f"model-{model_id}-{name}") for name in MODEL_OVERRIDE_FLAGS}}
                       for model_id in map(lambda raw: ascii_int(raw, 18), request.form.getlist("override_models"))
                       if model_id is not None},
            "settings": _typed_account_settings()}), 400
    shown_tier = limits.resolve_tier(settings.tier_id)  # what the page showed: no tier yet is the first one
    tier_changed = tier is not None and (tier["id"] != (shown_tier["id"] if shown_tier else None)
                                         or account["tier_locked"] != settings.tier_locked)
    account = {name: value for name, value in account.items() if getattr(settings, name) != value}
    try:
        with db.transaction():
            if account or tier_changed:
                limits_db.update_user_settings(user_id, actor, **account, **(
                    {"tier_id": tier["id"], "tier_changed_at": db.now()} if tier_changed else {}))
            for pool, values in pools.items():
                limits_db.set_override(user_id, pool, actor, **values)
            for model, values in models.values():
                limits_db.set_model_override(user_id, model["id"], actor, **values)
            if speed != settings.speed:
                limits.set_speed(user_id, speed, adjust_rate=flag("adjust_rate"), updated_by=actor)
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.user_limits", user_id=user_id)
    name = user["username"]
    exemptions = {key: account[key] for key in ("token_exempt", "rate_exempt") if key in account}
    if exemptions:
        audit("limits_user_exemptions", name, exemptions)
    for pool, values in pools.items():
        audit("limits_user_custom", name, {"pool": pool, **values})
    for model, values in models.values():
        audit("limits_user_model", name, {"model": model["ollama_name"], **values})
    gating = {key: account[key] for key in EFFORT_ACCOUNT_FLAGS if key in account}
    if gating:
        audit("limits_user_effort_gating", name, gating)
    if speed != settings.speed:
        audit("limits_user_speed", name, {"speed": speed, "adjust_rate": flag("adjust_rate")})
    if tier_changed:
        audit("limits_user_tier", name, {"tier": tier["name"],
                                         "locked": account.get("tier_locked", settings.tier_locked)})
    if "dynamic_exempt" in account:
        audit("limits_user_dynamic", name, {"exempt": account["dynamic_exempt"]})
    flash(f"Limits of {name} saved.", "success")
    return back("admin.user_limits", user_id=user_id)


# ----- each section's own form (pages open from before the page had one Save) ---------------------


@bp.post("/users/<user_id>/limits/custom", endpoint="user_limits_custom")
@admin_required
def set_custom(user_id):
    user = user_or_404(user_id)
    pool = request.form.get("pool")
    if pool not in limits_db.POOLS:
        abort(400)
    try:
        rules = rules_from_form()
        values = {
            "rate_rules": rules or None,
            "window_tokens": tokens("window_tokens", maximum=limits_db.OVERRIDE_WINDOW_MAX,
                                    label="Tokens per 5 hours", optional=True),
            "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
        }
        limits_db.set_override(user_id, pool, me()["id"], **values)
    except (FormError, ValueError) as error:
        flash(f"{POOL_LABELS[pool]}: {error}", "error")
        return _user_limits_page(user, {"pool": pool, "override": _typed_override()}), 400
    audit("limits_user_custom", user["username"], {"pool": pool, **values})
    flash(f"Custom {POOL_LABELS[pool]} limits for {user['username']} saved.",
          "success")
    return back("admin.user_limits", user_id=user_id, _anchor=f"pool-{pool}")


@bp.post("/users/<user_id>/limits/model", endpoint="user_limits_model")
@admin_required
def set_model_custom(user_id):
    user = user_or_404(user_id)
    model = _model_or_404(ascii_int(request.form.get("model_id", ""), 18) or 0)
    try:
        if request.form.get("action") == "clear":
            values = {"rate_rules": None, "window_tokens": None, "weekly_tokens": None, "locked": False,
                      "token_exempt": False, "rate_exempt": False}
        else:
            values = {"rate_rules": rules_from_form() or None,
                      "window_tokens": tokens("window_tokens", label="Tokens per 5 hours", optional=True),
                      "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
                      "locked": flag("locked")}
            if "limit_controls" in request.form:
                values.update(token_exempt=flag("token_exempt"), rate_exempt=flag("rate_exempt"))
        limits_db.set_model_override(user_id, model["id"], me()["id"], **values)
    except (FormError, ValueError) as error:
        flash(f"{model['display_name']}: {error}", "error")
        current = limits_db.get_model_override(user_id, model["id"]) or limits_db.ModelOverride(model["id"])
        controls = {name: flag(name) if "limit_controls" in request.form else getattr(current, name)
                    for name in ("token_exempt", "rate_exempt")}
        return _user_limits_page(user, {"model_id": model["id"], "override": {
            **_typed_override(), "locked": flag("locked"), **controls}}), 400
    audit("limits_user_model", user["username"], {"model": model["ollama_name"], **values})
    flash(f"{model['display_name']}: " + ("custom limits of " + user["username"] + " removed."
                                          if request.form.get("action") == "clear" else
                                          f"limits of {user['username']} saved" +
                                          (" (locked)." if values["locked"] else ".")), "success")
    return back("admin.user_limits", user_id=user_id, _anchor="models")


@bp.post("/users/<user_id>/limits/effort", endpoint="user_limits_effort")
@admin_required
def set_effort(user_id):
    user = user_or_404(user_id)
    try:
        if request.form.get("action") == "gating":
            off = flag("effort_gating_off")
            controls = {"effort_gating_off": off}
            if "limit_controls" in request.form:
                controls["effort_auto_unlock_off"] = flag("effort_auto_unlock_off")
            limits_db.update_user_settings(user_id, me()["id"], **controls)
            audit("limits_user_effort_gating", user["username"], controls)
            flash(f"Reasoning effort is {'no longer limited' if off else 'limited again'} for {user['username']}.",
                  "success")
            return back("admin.user_limits", user_id=user_id, _anchor="effort")
        target = request.form.get("model_id") or ""
        model = None if target in ("", "all") else _model_or_404(ascii_int(target, 18) or 0)
        level = choice("level", ("", *limits_db.EFFORT_LEVELS), label="Level", default="") or None
        pinned = flag("pinned")
        limits_db.set_effort_level(user_id, model["id"] if model else None, level, pinned=pinned, source="admin",
                                   updated_by=me()["id"])
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.user_limits", user_id=user_id, _anchor="effort")
    name = model["display_name"] if model else "every model"
    audit("limits_user_effort", user["username"], {"model": model["ollama_name"] if model else None, "level": level,
                                                   "pinned": pinned})
    flash(f"{user['username']}: " + (f"reasoning effort up to {limits.effort_label(level)} for {name}"
                                     + (" (kept)." if pinned else ".") if level else
                                     f"{name} follows the default reasoning effort again."), "success")
    return back("admin.user_limits", user_id=user_id, _anchor="effort")


@bp.post("/users/<user_id>/limits/exemptions", endpoint="user_limits_exemptions")
@admin_required
def set_exemptions(user_id):
    user = user_or_404(user_id)
    controls = {name: flag(name) for name in ("token_exempt", "rate_exempt")}
    limits_db.update_user_settings(user_id, me()["id"], **controls)
    audit("limits_user_exemptions", user["username"], controls)
    flash(f"Exemptions for {user['username']} saved.", "success")
    return back("admin.user_limits", user_id=user_id, _anchor="exemptions")


@bp.post("/users/<user_id>/limits/restore", endpoint="user_limits_restore")
@admin_required
def restore(user_id):
    user = user_or_404(user_id)
    limits_db.restore_defaults(user_id, me()["id"])
    audit("limits_user_restore", user["username"])
    flash(f"{user['username']} follows the default limits again (normal speed, no custom limits or locks).",
          "success")
    return back("admin.user_limits", user_id=user_id)


@bp.post("/users/<user_id>/limits/reset", endpoint="user_limits_reset")
@admin_required
def reset_usage(user_id):
    user = user_or_404(user_id)
    weekly = request.form.get("period") == "week"
    limits_db.reset_usage(user_id, weekly=weekly, updated_by=me()["id"])
    audit("limits_user_reset_usage", user["username"], {"period": "week" if weekly else "window"})
    flash(f"{user['username']}'s " + ("5-hour and weekly usage" if weekly else "5-hour usage") +
          " now counts from zero.", "success")
    return back("admin.user_limits", user_id=user_id)


@bp.post("/users/<user_id>/limits/speed", endpoint="user_limits_speed")
@admin_required
def speed(user_id):
    user = user_or_404(user_id)
    try:
        value = choice("speed", limits_db.SPEEDS, label="Speed")
    except FormError as error:
        flash(str(error), "error")
        return back("admin.user_limits", user_id=user_id)
    adjust = flag("adjust_rate")
    limits.set_speed(user_id, value, adjust_rate=adjust, updated_by=me()["id"])
    audit("limits_user_speed", user["username"], {"speed": value, "adjust_rate": adjust})
    flash({"slow": f"{user['username']} is slowed down", "fast": f"{user['username']} is sped up",
           "normal": f"{user['username']} is back to normal speed"}[value] +
          (" (request rate adjusted)." if adjust else "."), "success")
    return back("admin.user_limits", user_id=user_id)


@bp.post("/users/<user_id>/limits/tier", endpoint="user_limits_tier")
@admin_required
def set_tier(user_id):
    user = user_or_404(user_id)
    try:
        tier_id = integer("tier_id", minimum=1, maximum=2**31, label="Tier")
        tier = limits_db.get_tier(tier_id)
        if tier is None:
            raise FormError("That tier does not exist.")
        locked = flag("tier_locked")
        limits_db.update_user_settings(user_id, me()["id"], tier_id=tier_id, tier_locked=locked,
                                       tier_changed_at=db.now())
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.user_limits", user_id=user_id)
    audit("limits_user_tier", user["username"], {"tier": tier["name"], "locked": locked})
    flash(f"{user['username']} is in tier {tier['name']}" + (" (locked)." if locked else "."), "success")
    return back("admin.user_limits", user_id=user_id)


@bp.post("/users/<user_id>/limits/dynamic", endpoint="user_limits_dynamic")
@admin_required
def dynamic(user_id):
    user = user_or_404(user_id)
    exempt = flag("dynamic_exempt")
    limits_db.update_user_settings(user_id, me()["id"], dynamic_exempt=exempt)
    audit("limits_user_dynamic", user["username"], {"exempt": exempt})
    flash(f"Dynamic adjustment {'no longer applies' if exempt else 'applies again'} to {user['username']}.",
          "success")
    return back("admin.user_limits", user_id=user_id)
