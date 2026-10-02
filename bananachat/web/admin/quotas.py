"""Limits: service policies (request-rate rules, 5-hour and weekly tokens), automatic approval, global actions
and the quota-request queue. Models, reasoning effort, tiers, grants and per-account limits live in
:mod:`.limits`."""

from __future__ import annotations

from flask import abort, flash, render_template, request

from bananachat import db
from bananachat.db import credits
from bananachat.db import limits as limits_db
from bananachat.db import settings as site_settings
from bananachat.security import admin_required
from bananachat.services import community, limits

from . import bp
from ._helpers import FormError, audit, back, flag, integer, me, text, tokens
from ._quota_ui import (
    POOL_LABELS, policy_summary, quota_tabs_context, rules_en, rules_from_form, tokens_en, typed, typed_rules,
)

TOKENS_MAX = limits_db.TOKENS_MAX
REQUEST_MAX = credits.REQUEST_TOKENS_MAX  # the largest 5-hour amount users can ask for
WEEKLY_REQUEST_MAX = credits.REQUEST_WEEKLY_MAX
REQUEST_STATUSES = ("pending", "approved", "denied", "cancelled", "all")
KIND_LABELS = {"window": "5-hour tokens", "weekly": "Weekly tokens", "rate": "Request rate",
               "temporary": "Temporary increases", "effort": "Reasoning effort"}


# ----- policies -------------------------------------------------------------------------

@bp.get("/quotas", endpoint="quotas")
@admin_required
def quotas():
    return _quotas_page()


def _quotas_page(refused: dict | None = None):
    """The services page; *refused* holds the form that was refused (``{"pool" or "options": ..., values}``) so
    it shows again with what was typed."""
    settings = site_settings.get()
    policies = limits_db.all_policies()
    people, slots = limits.current_people()
    return render_template(
        "admin/quotas.html", tab="policies", s=settings, policies=policies, refused=refused or {},
        summaries={pool: policy_summary(policy) for pool, policy in policies.items()},
        custom_count=limits_db.count_custom(), grant_count=limits_db.count_active_grants(),
        demand=limits.current_demand(), people=people, slots=slots, peak_hours=limits.peak_hours(),
        community=community.settings(settings), community_bounds=community.BOUNDS, kind_labels=KIND_LABELS,
        community_open=community.count_open(),
        limits={"tokens": TOKENS_MAX, "request": REQUEST_MAX, "weekly_request": WEEKLY_REQUEST_MAX,
                "requests": limits_db.REQUESTS_MAX, "burst": limits_db.BURST_MAX},
        **quota_tabs_context())


def _typed_policy() -> dict:
    """A refused policy form as typed, in the shape of a policy."""
    return {"rate": {"enabled": flag("rate_enabled"), "rules": typed_rules(), "dynamic": flag("rate_dynamic")},
            "window": {"enabled": flag("window_enabled"), "tokens": typed("window_tokens"),
                       "dynamic": flag("window_dynamic"),
                       "auto_tiers": flag("window_auto_tiers")},
            "weekly": {"enabled": flag("weekly_enabled"), "tokens": typed("weekly_tokens"),
                       "dynamic": flag("weekly_dynamic"), "auto_tiers": flag("weekly_auto_tiers")}}


def _policy_from_form() -> dict:
    return {
        "rate": {"enabled": flag("rate_enabled"), "rules": rules_from_form(), "dynamic": flag("rate_dynamic")},
        "window": {"enabled": flag("window_enabled"),
                   "tokens": tokens("window_tokens", label="Tokens per 5 hours"),
                   "dynamic": flag("window_dynamic"), "auto_tiers": flag("window_auto_tiers")},
        "weekly": {"enabled": flag("weekly_enabled"),
                   "tokens": tokens("weekly_tokens", label="Tokens per week"),
                   "dynamic": flag("weekly_dynamic"), "auto_tiers": flag("weekly_auto_tiers")},
    }


@bp.post("/quotas/policy/<pool>", endpoint="quotas_policy")
@admin_required
def save_policy(pool):
    if pool not in limits_db.POOLS:
        abort(404)
    try:
        config = limits_db.set_policy(pool, _policy_from_form(), me()["id"])
    except (FormError, ValueError) as error:
        flash(f"{POOL_LABELS[pool]}: {error}", "error")
        return _quotas_page({"pool": pool, "policy": _typed_policy()}), 400
    audit("limits_policy", pool, config)
    flash(f"{POOL_LABELS[pool]}: limits saved ({policy_summary(config)}).", "success")
    return back("admin.quotas", _anchor=f"policy-{pool}")


@bp.post("/quotas", endpoint="quotas_save")
@admin_required
def save():
    """Site-wide options for automatic approval of requests."""
    try:
        values = {
            "quota_auto_approve_enabled": 1 if flag("quota_auto_approve_enabled") else 0,
            "quota_auto_approve_max_tokens": tokens("quota_auto_approve_max_tokens", maximum=REQUEST_MAX,
                                                    label="Automatic approval up to", default=0),
            "quota_auto_approve_max_weekly_tokens": tokens("quota_auto_approve_max_weekly_tokens",
                                                           maximum=WEEKLY_REQUEST_MAX,
                                                           label="Automatic approval of weekly tokens up to",
                                                           default=0),
        }
    except FormError as error:
        flash(str(error), "error")
        return _quotas_page({"options": {
            "quota_auto_approve_enabled": flag("quota_auto_approve_enabled"),
            **{name: typed(name) for name in ("quota_auto_approve_max_tokens",
                                              "quota_auto_approve_max_weekly_tokens")}}}), 400
    site_settings.update(**values)
    audit("quotas_save", "site", values)
    flash("Options saved.", "success")
    return back("admin.quotas", _anchor="options")


@bp.post("/quotas/community", endpoint="quotas_community")
@admin_required
def save_community():
    """Community consent: on or off, which kinds of request, and how much consent approves one."""
    try:
        values = {"community_quota_enabled": 1 if flag("community_quota_enabled") else 0,
                  "community_kinds": ",".join(kind for kind in community.KINDS if flag(f"community_kind_{kind}"))}
        labels = {"min_supporters": "Supporters needed", "approval_percent": "Share in favour",
                  "coverage_percent": "Tokens covered", "hours": "Voting hours", "boost_hours": "Increase lasts",
                  "min_account_days": "Account age to vote", "max_pledge_percent": "Most a person can renounce"}
        for name, (low, high) in community.BOUNDS.items():
            values[f"community_{name}"] = integer(f"community_{name}", minimum=low, maximum=high, label=labels[name])
    except FormError as error:
        flash(str(error), "error")
        return back("admin.quotas", _anchor="community")
    site_settings.update(**values)
    audit("quotas_community", "site", values)
    flash("Community consent saved.", "success")
    return back("admin.quotas", _anchor="community")


@bp.post("/quotas/fallback", endpoint="quotas_fallback")
@admin_required
def save_fallback():
    """Which way chat may switch models when someone's quota for the chosen model runs out."""
    values = {"quota_fallback_to_local": 1 if flag("quota_fallback_to_local") else 0,
              "quota_fallback_to_cloud": 1 if flag("quota_fallback_to_cloud") else 0}
    site_settings.update(**values)
    audit("quotas_fallback", "site", values)
    flash("Model fallback saved.", "success")
    return back("admin.quotas", _anchor="fallback")


@bp.post("/quotas/chat-consumption", endpoint="quotas_chat_consumption")
@admin_required
def save_chat_consumption():
    """Choose which chats consume token allowances without changing request-rate protection."""
    values = {"chat_local_token_consumption": int(flag("chat_local_token_consumption")),
              "chat_cloud_token_consumption": int(flag("chat_cloud_token_consumption"))}
    with db.transaction():
        site_settings.update(**values)
        audit("chat_token_consumption", "chat", values)
    flash("Chat token consumption saved.", "success")
    return back("admin.quotas", _anchor="chat-consumption")


# ----- global actions -----------------------------------------------------------------------

@bp.post("/quotas/reset-usage", endpoint="quotas_reset_usage")
@admin_required
def reset_usage():
    """Everyone starts counting afresh (the ledger is kept): the 5-hour windows, or also the weekly ones."""
    weekly = request.form.get("period") == "week"
    limits_db.reset_usage(None, weekly=weekly, updated_by=me()["id"])
    audit("limits_reset_usage", "everyone", {"period": "week" if weekly else "window"})
    flash("Everyone's " + ("5-hour and weekly usage" if weekly else "5-hour usage") + " now counts from zero.",
          "success")
    return back("admin.quotas", _anchor="global")


@bp.post("/quotas/restore-defaults", endpoint="quotas_restore_defaults")
@admin_required
def restore_defaults():
    count = limits_db.restore_defaults(None, me()["id"])
    audit("limits_restore_defaults", "everyone", {"accounts": count})
    flash(f"Custom limits, token/rate exemptions and speed settings removed from "
          f"{count} account{'s' if count != 1 else ''}.", "success")
    return back("admin.quotas", _anchor="global")


# ----- requests ---------------------------------------------------------------------------

def _request_tokens(row, name: str, legacy: str):
    value = row[name]
    return value if value is not None else (row[legacy] or 0) * credits.TOKENS_PER_CREDIT


def request_summary(row) -> str:
    """What a request asks for, in English."""
    kind, pool = credits.request_kind(row), POOL_LABELS.get(row["pool"] or "api", row["pool"])
    if kind != "effort" and row["model_id"] is not None:
        pool = f"model {row['model_name'] or '(removed)'}"
    if kind == "effort":
        target = row["model_name"] or ("every model" if row["effort_all_models"] else "a removed model")
        return f"Reasoning effort up to {limits.effort_label(row['effort_level'] or 'medium')} · {target}"
    if kind == "weekly":
        return f"{tokens_en(_request_tokens(row, 'new_weekly_tokens', 'new_weekly_credits'))} per week · {pool}"
    if kind == "rate":
        rules = credits.request_rules(row)
        if rules:
            return f"{', '.join(limits.rule_text('en', rule) for rule in rules)} · {pool}"
        if row["new_rate_per_second"]:
            return f"{float(row['new_rate_per_second']):g} requests per second · {pool}"
        return f"A higher request rate · {pool}"
    if kind == "temporary":
        amount = "Unlimited use" if row["grant_unlimited"] else \
            f"+{tokens_en(_request_tokens(row, 'new_tokens', 'new_credits'))} per 5 hours"
        return f"{amount} for {row['grant_hours']} h · {pool}"
    return f"{tokens_en(_request_tokens(row, 'new_tokens', 'new_credits'))} per 5 hours · {pool}"


def current_summary(row, base: dict | None = None) -> str:
    """The account's own limit that a pending request would change (*base*: its ``limits.base_limits``)."""
    kind = credits.request_kind(row)
    if kind in ("temporary", "effort"):
        return ""
    base = base or community.base_for(row["user_id"], row)
    if kind == "weekly":
        return f"now {tokens_en(base['weekly_tokens'])} per week" + ("" if base["weekly_enabled"] else " (not limited)")
    if kind == "rate":
        return f"now {rules_en(base['rate_rules'])}" + ("" if base["rate_enabled"] else " (not limited)")
    return f"now {tokens_en(base['window_tokens'])} per 5 hours" + \
        ("" if base["window_enabled"] else " (not limited)")


def effort_currents(rows) -> dict[int, str]:
    """For pending effort requests: each account's level now, ``{request id: text}``, in a fixed number of
    queries."""
    rows = list(rows)
    if not rows:
        return {}
    user_ids = sorted({row["user_id"] for row in rows})
    marks = ",".join("?" for _ in user_ids)
    accounts = {row["id"]: row for row in db.query(f"SELECT * FROM users WHERE id IN ({marks})", user_ids)}
    own: dict[str, dict] = {}
    for level in db.query(f"SELECT user_id, model_id, level, pinned, source, updated_at FROM user_effort_levels "
                          f"WHERE user_id IN ({marks})", user_ids):
        own.setdefault(level["user_id"], {})[level["model_id"]] = limits_db.EffortLevel(
            level["model_id"], level["level"], bool(level["pinned"]), level["source"], level["updated_at"])
    models = {row["id"]: row for row in db.query("SELECT * FROM ai_models")}
    policies = limits_db.model_policies(models.values())
    prefs = {row["user_id"]: bool(row["effort_gating_off"]) for row in db.query(
        f"SELECT user_id, effort_gating_off FROM user_limits WHERE user_id IN ({marks})", user_ids)}
    settings = site_settings.get()
    result = {}
    for row in rows:
        user = accounts.get(row["user_id"])
        if user is None:
            continue
        mine = own.get(user["id"], {})
        settings_row = limits_db.UserSettings(effort_gating_off=prefs.get(user["id"], False))
        if row["model_id"] is not None and row["model_id"] in models:
            level = limits.effort_ceiling(user, models[row["model_id"]], policy=policies[row["model_id"]],
                                          own=mine, prefs=settings_row, settings=settings)
        elif mine.get(None) is not None:
            level = mine[None].level
        else:
            level = limits.effort_settings(settings)["default"]
        result[row["id"]] = f"now up to {limits.effort_label(level)}" if level else ""
    return result


@bp.get("/quotas/requests", endpoint="quota_requests")
@admin_required
def requests_page():
    status = request.args.get("status", "pending")
    if status not in REQUEST_STATUSES:
        status = "pending"
    rows = credits.list_requests(None if status == "all" else status, limit=200)
    options = community.settings()
    pending = [row for row in rows if row["status"] == "pending" and credits.request_kind(row) != "effort"
               and row["model_id"] is None]
    bases = limits.base_limits_many((row["user_id"], row["pool"] or "api") for row in pending)
    efforts = effort_currents(row for row in rows if row["status"] == "pending" and credits.request_kind(row) == "effort")
    items = []
    for row in rows:
        current = ""
        if row["status"] == "pending":
            current = efforts.get(row["id"], "") if credits.request_kind(row) == "effort" else \
                current_summary(row, bases.get((row["user_id"], row["pool"] or "api")) if row["model_id"] is None
                                else None)
        consent = None
        if row["community"]:
            result = community.consent(row, options)
            consent = {"consent": result, "open": community.voting_open(row, options)}
        items.append({"row": row, "kind": credits.request_kind(row), "summary": request_summary(row),
                      "current": current, "community": consent})
    return render_template("admin/quota_requests.html", tab="requests", requests=items, status=status,
                           **quota_tabs_context())


@bp.post("/quotas/requests/<int:request_id>", endpoint="quota_request_resolve")
@admin_required
def resolve(request_id):
    row = db.one("SELECT r.*, u.username FROM quota_requests r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                 (request_id,))
    if row is None:
        abort(404)
    decision = request.form.get("decision")
    if decision not in ("approve", "deny"):
        abort(400)
    try:
        message = text("message", max_length=1000, label="Message")
        credits.resolve_request(request_id, me()["id"], decision == "approve", message)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.quota_requests")
    audit(f"quota_request_{decision}", row["username"],
          {"request": request_id, "kind": credits.request_kind(row), "pool": row["pool"], "tokens": row["new_tokens"],
           "weekly": row["new_weekly_tokens"], "rules": credits.request_rules(row),
           "hours": row["grant_hours"], "unlimited": bool(row["grant_unlimited"]), "model": row["model_id"],
           "effort": row["effort_level"]})
    flash(f"Request from {row['username']} {'approved' if decision == 'approve' else 'denied'}.", "success")
    return back("admin.quota_requests")
