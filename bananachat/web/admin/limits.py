"""Limits: models (weight, per-model limits, presets), reasoning effort, tiers, grants and the limits of one
account (custom limits per service and per model, effort levels, usage resets, speed, tier, exemption)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import abort, flash, render_template, request

from bananachat import db
from bananachat.db import catalog, users
from bananachat.db import limits as limits_db
from bananachat.db import settings as site_settings
from bananachat.security import admin_required
from bananachat.services import limits

from . import bp
from ._helpers import FormError, audit, back, choice, flag, integer, me, number, text, tokens, user_or_404, \
    utc_datetime, utc_input_min
from .quotas import POOL_LABELS, _tabs_context, rules_en, rules_from_form, tokens_en, typed, typed_rules

DURATIONS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
PRESET_HELP = {
    "light": "Small, fast models. Usage counts half (×0.5) against the service limits; no model-specific limits.",
    "standard": "Most models. Usage counts ×1; no model-specific limits.",
    "heavy": "Large or slow models. Usage counts ×3, at most 6 requests a minute and 200 a day, 200k tokens per "
             "5 hours, follows demand twice as strongly, reasoning effort up to Low unless unlocked.",
}
SCOPE_LABELS = {"rate": "request rate", "window": "5-hour tokens", "weekly": "weekly tokens"}


def _reason_texts(reasons) -> list[str]:
    return [reason.text("en") for reason in reasons if reason.code not in ("not_limited",)]


def _models():
    """Catalog models that limits apply to (text and image models), in catalog order."""
    return [model for model in catalog.list_models() if model["backend"] in ("ollama", "comfyui")]


def _model_or_404(model_id: int):
    model = catalog.get(model_id)
    if model is None:
        abort(404)
    return model


# ----- tiers ---------------------------------------------------------------------------------

@bp.get("/quotas/tiers", endpoint="limit_tiers")
@admin_required
def tiers():
    policies = limits_db.all_policies()
    labels = {"window": "5-hour", "weekly": "weekly"}
    applies = [f"{POOL_LABELS[pool]} ({', '.join(labels[period] for period in limits_db.PERIODS if policy[period]['auto_tiers'])})"
               for pool, policy in policies.items() if policy["window"]["auto_tiers"] or policy["weekly"]["auto_tiers"]]
    applies += [f"{model['display_name']} (model limits)" for model in _models()
                if (policy := limits_db.get_model_policy(model))["auto_tiers"] and policy["enabled"]]
    return render_template("admin/limit_tiers.html", tab="tiers", tiers=limits_db.list_tiers(),
                           counts=limits_db.tier_counts(), applies=applies, **_tabs_context())


def _tier_form() -> dict:
    return {"name": text("name", max_length=limits_db.TIER_NAME_MAX, required=True, label="Name"),
            "multiplier": number("multiplier", minimum=0, maximum=limits_db.TIER_MULTIPLIER_MAX, label="Multiplier"),
            "min_account_days": integer("min_account_days", minimum=0, maximum=3650, label="Account age", default=0),
            "min_active_days": integer("min_active_days", minimum=0, maximum=30, label="Active days", default=0),
            "min_tokens_30d": tokens("min_tokens_30d", label="Tokens used", default=0),
            "clean_days": integer("clean_days", minimum=0, maximum=3650, label="Days without suspension", default=0)}


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
    flash({"delete": f"Tier {tier['name']} deleted; its accounts are back in the first tier.",
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
        **_tabs_context())


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
        try:
            model = catalog.get(int(where.partition(":")[2]))
        except ValueError:
            model = None
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
    """Short facts about a model's policy, for its card."""
    facts = [f"counts ×{policy['weight']:g}"]
    if not limits.counts_toward_pool(model, policy):
        facts = ["not counted toward service limits"]
    if policy["enabled"]:
        if policy["rate_rules"]:
            facts.append(rules_en(policy["rate_rules"]))
        if policy["window_tokens"] is not None:
            facts.append(f"{tokens_en(policy['window_tokens'])} per 5 hours")
        if policy["weekly_tokens"] is not None:
            facts.append(f"{tokens_en(policy['weekly_tokens'])} per week")
        if not (policy["rate_rules"] or policy["window_tokens"] is not None or policy["weekly_tokens"] is not None):
            facts.append("model limits on, none set")
    if limits.supported_efforts(model):
        default = policy["effort_default"] or limits.effort_settings()["default"]
        facts.append(f"reasoning up to {limits.effort_label(default)} by default")
    return facts


@bp.get("/quotas/models", endpoint="limit_models")
@admin_required
def models_page():
    return _models_page()


def _models_page(refused: dict | None = None):
    """The models page; *refused* (``{"model_id", "policy"}``) shows a refused form again as typed."""
    models = _models()
    policies = limits_db.model_policies(models)
    stored = {row[0] for row in db.query("SELECT model_id FROM model_limit_policy")}
    items = [{"model": model, "policy": policies[model["id"]], "configured": model["id"] in stored,
              "facts": model_summary(model, policies[model["id"]]),
              "counts_default": limits.is_local(model),
              "efforts": limits.supported_efforts(model)} for model in models]
    for item in items:
        if refused and item["model"]["id"] == refused["model_id"]:
            item["policy"] = refused["policy"]
    return render_template("admin/limit_models.html", tab="models", items=items, presets=limits_db.PRESETS,
                           preset_help=PRESET_HELP, efforts=limits_db.EFFORT_LEVELS, refused=refused or {},
                           site_effort=limits.effort_settings()["default"], **_tabs_context())


def _model_policy_form(current: dict) -> dict:
    counts = choice("counts_toward_pool", ("default", "yes", "no"), label="Counts toward service limits",
                    default="default")
    effort = choice("effort_default", ("", *limits_db.EFFORT_LEVELS), label="Default reasoning effort", default="")
    return {**current, "preset": "custom", "enabled": flag("enabled"),
            "weight": number("weight", minimum=limits_db.WEIGHT_MIN, maximum=limits_db.WEIGHT_MAX, label="Weight"),
            "counts_toward_pool": None if counts == "default" else counts == "yes",
            "rate_rules": rules_from_form(),
            "window_tokens": tokens("window_tokens", label="Tokens per 5 hours", optional=True),
            "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
            "dynamic": flag("dynamic"),
            "sensitivity": number("sensitivity", minimum=0, maximum=limits_db.SENSITIVITY_MAX, label="Sensitivity"),
            "auto_tiers": flag("auto_tiers"), "effort_default": effort or None}


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
            return back("admin.limit_models", _anchor=f"model-{model_id}")
        counts = request.form.get("counts_toward_pool")
        return _models_page({"model_id": model_id, "policy": {
            **current, "weight": typed("weight"), "sensitivity": typed("sensitivity"), "enabled": flag("enabled"),
            "dynamic": flag("dynamic"), "auto_tiers": flag("auto_tiers"), "rate_rules": typed_rules(),
            "window_tokens": typed("window_tokens"), "weekly_tokens": typed("weekly_tokens"),
            "counts_toward_pool": {"yes": True, "no": False}.get(counts),
            "effort_default": request.form.get("effort_default") or None}}), 400
    audit("limits_model", model["ollama_name"], {"action": action, **config})
    flash(message, "success")
    return back("admin.limit_models", _anchor=f"model-{model_id}")


# ----- reasoning effort ---------------------------------------------------------------------

@bp.get("/quotas/effort", endpoint="limit_effort")
@admin_required
def effort_page():
    return _effort_page()


def _effort_page(refused: dict | None = None):
    """The reasoning-effort page; *refused* is the settings form as typed, when it was refused."""
    rows = db.query("SELECT e.*, u.username, m.display_name AS model_name, a.username AS updated_by_name "
                    "FROM user_effort_levels e JOIN users u ON u.id=e.user_id "
                    "LEFT JOIN ai_models m ON m.id=e.model_id LEFT JOIN users a ON a.id=e.updated_by "
                    "ORDER BY e.updated_at DESC, e.id DESC LIMIT 100")
    reasoning = [model for model in _models() if limits.supported_efforts(model)]
    policies = limits_db.model_policies(reasoning)
    return render_template("admin/limit_effort.html", tab="effort", settings=limits.effort_settings(), typed_settings=refused,
                           levels=limits_db.EFFORT_LEVELS, unlocks=rows,
                           models=[{"model": model, "levels": limits.supported_efforts(model),
                                    "default": policies[model["id"]]["effort_default"]} for model in reasoning],
                           **_tabs_context())


@bp.post("/quotas/effort", endpoint="limit_effort_save")
@admin_required
def save_effort():
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
        flash(str(error), "error")
        return _effort_page({
            "gating": flag("effort_gating_enabled"), "default": request.form.get("effort_default_level"),
            "auto": flag("effort_auto_unlock"), "active_days": typed("effort_auto_active_days"),
            "tokens": typed("effort_auto_tokens"), "period_days": typed("effort_auto_period_days"),
            "clean_days": typed("effort_auto_clean_days"), "ceiling": request.form.get("effort_auto_ceiling")}), 400
    site_settings.update(**values)
    audit("limits_effort_settings", "site", values)
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
        limits_db.set_model_policy(model_id, {**current, "effort_default": level}, me()["id"])
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.limit_effort")
    audit("limits_effort_model", model["ollama_name"], {"default": level})
    flash(f"{model['display_name']}: everyone may use up to "
          f"{limits.effort_label(level or limits.effort_settings()['default'])} unless unlocked further.", "success")
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
        views.append({"model": model, "ceiling": ceiling, "own": own.get(model["id"]),
                      "levels": limits.supported_efforts(model)})
    return views


@bp.get("/users/<user_id>/limits", endpoint="user_limits")
@admin_required
def user_limits(user_id):
    return _user_limits_page(user_or_404(user_id))


def _user_limits_page(user, refused: dict | None = None):
    """An account's limits page; *refused* shows a refused custom-limit form again as typed
    (``{"pool": ..., "override": ...}`` or ``{"model_id": ..., "override": ...}``)."""
    user_id = user["id"]
    settings = limits_db.user_settings(user_id)
    models = _models()
    policies = limits_db.model_policies(models)
    return render_template(
        "admin/user_limits.html", section="users", user=user, settings=settings, refused=refused or {},
        pools=[_pool_view(user, pool) for pool in limits_db.POOLS], tiers=limits_db.list_tiers(),
        tier=limits.resolve_tier(settings.tier_id), progress=limits.tier_progress(user),
        auto_tiers=limits.auto_tiers_enabled(), models=models,
        model_views=_model_views(user, models, policies),
        effort_views=_effort_views(user, models, policies, settings),
        effort_all=limits_db.effort_levels(user_id).get(None), effort_settings=limits.effort_settings(),
        efforts=limits_db.EFFORT_LEVELS,
        grants=[_grant_view(row) for row in limits_db.list_grants("active", user_id=user_id)] +
        [_grant_view(row) for row in limits_db.list_grants("upcoming", user_id=user_id)],
        limits={"tokens": limits_db.TOKENS_MAX, "requests": limits_db.REQUESTS_MAX, "burst": limits_db.BURST_MAX},
        **{key: value for key, value in _tabs_context().items() if key in ("units", "rule_rows")})


def _typed_override() -> dict:
    """A refused custom-limit form as typed, in the shape of an override."""
    return {"rate_rules": typed_rules() or None, "window_tokens": typed("window_tokens"),
            "window_slow_tokens": typed("window_slow_tokens"), "weekly_tokens": typed("weekly_tokens")}


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
            "window_tokens": tokens("window_tokens", label="Tokens per 5 hours", optional=True),
            "window_slow_tokens": tokens("window_slow_tokens", label="Slow tokens per 5 hours", optional=True),
            "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
        }
        limits_db.set_override(user_id, pool, me()["id"], **values)
    except (FormError, ValueError) as error:
        flash(f"{POOL_LABELS[pool]}: {error}", "error")
        return _user_limits_page(user, {"pool": pool, "override": _typed_override()}), 400
    audit("limits_user_custom", user["username"], {"pool": pool, **values})
    flash(f"{POOL_LABELS[pool]}: custom limits of {user['username']} saved (empty fields follow the defaults).",
          "success")
    return back("admin.user_limits", user_id=user_id, _anchor=f"pool-{pool}")


@bp.post("/users/<user_id>/limits/model", endpoint="user_limits_model")
@admin_required
def set_model_custom(user_id):
    user = user_or_404(user_id)
    model = _model_or_404(request.form.get("model_id", type=int) or 0)
    try:
        if request.form.get("action") == "clear":
            values = {"rate_rules": None, "window_tokens": None, "weekly_tokens": None, "locked": False}
        else:
            values = {"rate_rules": rules_from_form() or None,
                      "window_tokens": tokens("window_tokens", label="Tokens per 5 hours", optional=True),
                      "weekly_tokens": tokens("weekly_tokens", label="Tokens per week", optional=True),
                      "locked": flag("locked")}
        limits_db.set_model_override(user_id, model["id"], me()["id"], **values)
    except (FormError, ValueError) as error:
        flash(f"{model['display_name']}: {error}", "error")
        return _user_limits_page(user, {"model_id": model["id"], "override": {
            **_typed_override(), "locked": flag("locked")}}), 400
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
            limits_db.update_user_settings(user_id, me()["id"], effort_gating_off=off)
            audit("limits_user_effort_gating", user["username"], {"off": off})
            flash(f"Reasoning effort is {'no longer limited' if off else 'limited again'} for {user['username']}.",
                  "success")
            return back("admin.user_limits", user_id=user_id, _anchor="effort")
        target = request.form.get("model_id") or ""
        model = None if target in ("", "all") else _model_or_404(int(target) if target.isdigit() else 0)
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
