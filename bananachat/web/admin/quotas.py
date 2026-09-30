"""Limits: service policies (request-rate rules, 5-hour and weekly tokens), automatic approval, global actions
and the quota-request queue. Models, reasoning effort, tiers, grants and per-account limits live in
:mod:`.limits`."""

from __future__ import annotations

from flask import abort, flash, render_template, request

from bananachat import db
from bananachat.db import credits
from bananachat.db import limits as limits_db
from bananachat.db import settings as site_settings
from bananachat.formatting import compact
from bananachat.security import admin_required
from bananachat.services import limits

from . import bp
from ._helpers import FormError, audit, back, flag, integer, me, text, tokens

TOKENS_MAX = limits_db.TOKENS_MAX
REQUEST_MAX = credits.REQUEST_TOKENS_MAX  # the largest 5-hour amount users can ask for
WEEKLY_REQUEST_MAX = credits.REQUEST_WEEKLY_MAX
REQUEST_STATUSES = ("pending", "approved", "denied", "all")
POOL_LABELS = {"api": "API, playground and images", "chat": "Chat", "agent": "Agents"}
UNIT_LABELS = {"second": "second", "minute": "minute", "hour": "hour", "day": "day"}
RULE_ROWS = limits_db.RULES_MAX


def _tabs_context() -> dict:
    return {"section": "quotas", "pool_labels": POOL_LABELS, "units": UNIT_LABELS, "rule_rows": RULE_ROWS,
            "pending_count": db.scalar("SELECT COUNT(*) FROM quota_requests WHERE status='pending'", default=0)}


def tokens_en(value) -> str:
    return f"{compact(value, 'en')} tokens"


def rules_en(rules) -> str:
    return limits.rate_text("en", rules) if rules else "no request-rate limit"


def rules_from_form(prefix: str = "rule") -> list[dict]:
    """Rate rules from rows of ``<prefix>_requests_N``, ``<prefix>_per_N``, ``<prefix>_burst_N`` (empty rows skipped)."""
    rules = []
    for index in range(RULE_ROWS):
        requests_raw = (request.form.get(f"{prefix}_requests_{index}") or "").strip()
        if not requests_raw:
            continue
        per = request.form.get(f"{prefix}_per_{index}")
        if per not in limits_db.UNITS:
            raise FormError("Choose second, minute, hour or day for every request-rate rule.")
        label = f"Requests per {per}"
        count = integer(f"{prefix}_requests_{index}", minimum=1, maximum=limits_db.REQUESTS_MAX, label=label)
        burst = integer(f"{prefix}_burst_{index}", minimum=1, maximum=limits_db.BURST_MAX,
                        label=f"Burst of the {per} rule", optional=True)
        rules.append({"requests": count, "per": per, "burst": burst})
    try:
        return limits_db.validate_rules(rules)
    except ValueError as error:
        raise FormError(str(error)) from None


def typed_rules(prefix: str = "rule") -> list[dict]:
    """The request-rate rows as typed, to show a refused form again (see :func:`rules_from_form`)."""
    rows = []
    for index in range(RULE_ROWS):
        count = (request.form.get(f"{prefix}_requests_{index}") or "").strip()[:20]
        if count:
            rows.append({"requests": count, "per": request.form.get(f"{prefix}_per_{index}") or "minute",
                         "burst": (request.form.get(f"{prefix}_burst_{index}") or "").strip()[:20]})
    return rows


def typed(name: str) -> str:
    """A field as typed (shortened), to show a refused form again."""
    return (request.form.get(name) or "").strip()[:100]


def policy_summary(policy: dict) -> str:
    """One line for a service policy: "1 request per second (bursts of up to 10) · 30k tokens per 5 hours"."""
    parts = [rules_en(policy["rate"]["rules"]) if policy["rate"]["enabled"] else "no request-rate limit"]
    window, weekly = policy["window"], policy["weekly"]
    parts.append(f"{tokens_en(window['tokens'])} per 5 hours" + (f" + {tokens_en(window['slow_tokens'])} slow"
                                                                if window["slow_tokens"] else "")
                 if window["enabled"] else "no 5-hour limit")
    if weekly["enabled"]:
        parts.append(f"{tokens_en(weekly['tokens'])} per week")
    return " · ".join(parts)


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
        limits={"tokens": TOKENS_MAX, "request": REQUEST_MAX, "weekly_request": WEEKLY_REQUEST_MAX,
                "requests": limits_db.REQUESTS_MAX, "burst": limits_db.BURST_MAX},
        **_tabs_context())


def _typed_policy() -> dict:
    """A refused policy form as typed, in the shape of a policy."""
    return {"rate": {"enabled": flag("rate_enabled"), "rules": typed_rules(), "dynamic": flag("rate_dynamic")},
            "window": {"enabled": flag("window_enabled"), "tokens": typed("window_tokens"),
                       "slow_tokens": typed("window_slow_tokens"), "dynamic": flag("window_dynamic"),
                       "auto_tiers": flag("window_auto_tiers")},
            "weekly": {"enabled": flag("weekly_enabled"), "tokens": typed("weekly_tokens"),
                       "dynamic": flag("weekly_dynamic"), "auto_tiers": flag("weekly_auto_tiers")}}


def _policy_from_form() -> dict:
    return {
        "rate": {"enabled": flag("rate_enabled"), "rules": rules_from_form(), "dynamic": flag("rate_dynamic")},
        "window": {"enabled": flag("window_enabled"),
                   "tokens": tokens("window_tokens", label="Tokens per 5 hours"),
                   "slow_tokens": tokens("window_slow_tokens", label="Slow tokens per 5 hours", default=0),
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
    """Site-wide options: slow tokens and automatic approval of requests."""
    try:
        values = {
            "slow_credits_enabled": 1 if flag("slow_credits_enabled") else 0,
            "quota_auto_approve_enabled": 1 if flag("quota_auto_approve_enabled") else 0,
            "quota_auto_approve_max_tokens": tokens("quota_auto_approve_max_tokens", maximum=REQUEST_MAX,
                                                    label="Automatic approval up to", default=0),
            "quota_auto_approve_max_slow_tokens": tokens("quota_auto_approve_max_slow_tokens", maximum=REQUEST_MAX,
                                                         label="Automatic approval of slow tokens up to", default=0),
            "quota_auto_approve_max_weekly_tokens": tokens("quota_auto_approve_max_weekly_tokens",
                                                           maximum=WEEKLY_REQUEST_MAX,
                                                           label="Automatic approval of weekly tokens up to",
                                                           default=0),
        }
    except FormError as error:
        flash(str(error), "error")
        return _quotas_page({"options": {
            "slow_credits_enabled": flag("slow_credits_enabled"),
            "quota_auto_approve_enabled": flag("quota_auto_approve_enabled"),
            **{name: typed(name) for name in ("quota_auto_approve_max_tokens", "quota_auto_approve_max_slow_tokens",
                                              "quota_auto_approve_max_weekly_tokens")}}}), 400
    site_settings.update(**values)
    audit("quotas_save", "site", values)
    flash("Options saved.", "success")
    return back("admin.quotas", _anchor="options")


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
    flash(f"Custom limits and speed settings removed from {count} account{'s' if count != 1 else ''}.", "success")
    return back("admin.quotas", _anchor="global")


# ----- requests ---------------------------------------------------------------------------

def _request_tokens(row, name: str, legacy: str):
    value = row[name]
    return value if value is not None else (row[legacy] or 0) * credits.TOKENS_PER_CREDIT


def request_summary(row) -> str:
    """What a request asks for, in English."""
    kind, pool = credits.request_kind(row), POOL_LABELS.get(row["pool"] or "api", row["pool"])
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
    return (f"{tokens_en(_request_tokens(row, 'new_tokens', 'new_credits'))} + "
            f"{tokens_en(_request_tokens(row, 'new_slow_tokens', 'new_slow_credits'))} slow per 5 hours · {pool}")


def current_summary(row, base: dict | None = None) -> str:
    """The account's own limit that a pending request would change (*base*: its ``limits.base_limits``)."""
    kind = credits.request_kind(row)
    if kind in ("temporary", "effort"):
        return ""
    base = base or limits.base_limits(row["user_id"], row["pool"] or "api")
    if kind == "weekly":
        return f"now {tokens_en(base['weekly_tokens'])} per week" + ("" if base["weekly_enabled"] else " (not limited)")
    if kind == "rate":
        return f"now {rules_en(base['rate_rules'])}" + ("" if base["rate_enabled"] else " (not limited)")
    return f"now {tokens_en(base['window_tokens'])} + {tokens_en(base['window_slow_tokens'])} slow" + \
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
    pending = [row for row in rows if row["status"] == "pending" and credits.request_kind(row) != "effort"]
    bases = limits.base_limits_many((row["user_id"], row["pool"] or "api") for row in pending)
    efforts = effort_currents(row for row in rows if row["status"] == "pending" and credits.request_kind(row) == "effort")
    items = []
    for row in rows:
        current = ""
        if row["status"] == "pending":
            current = efforts.get(row["id"], "") if credits.request_kind(row) == "effort" else \
                current_summary(row, bases[(row["user_id"], row["pool"] or "api")])
        items.append({"row": row, "kind": credits.request_kind(row), "summary": request_summary(row),
                      "current": current})
    return render_template("admin/quota_requests.html", tab="requests", requests=items, status=status,
                           **_tabs_context())


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
           "slow": row["new_slow_tokens"], "weekly": row["new_weekly_tokens"], "rules": credits.request_rules(row),
           "hours": row["grant_hours"], "unlimited": bool(row["grant_unlimited"]), "model": row["model_id"],
           "effort": row["effort_level"]})
    flash(f"Request from {row['username']} {'approved' if decision == 'approve' else 'denied'}.", "success")
    return back("admin.quota_requests")
