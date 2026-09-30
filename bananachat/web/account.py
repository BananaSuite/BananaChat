"""The account page: profile, usage, password, sign-in sessions, personal data,
quota and access requests, and account deletion.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from bananachat import db, security
from bananachat.db import access as access_db
from bananachat.db import catalog, credits, users
from bananachat.db import limits as limits_db
from bananachat.formatting import parse_amount, tokens_text
from bananachat.i18n import translate
from bananachat.services import exports, limits
from bananachat.services.access import AccessContext, is_image_model, is_text_model

bp = Blueprint("account", __name__)
log = logging.getLogger("bananachat.account")

USE_CASE_MIN, USE_CASE_MAX = 10, 2000
QUOTA_REASON_MIN, QUOTA_REASON_MAX = 5, 1000
QUOTA_REQUEST_MAX = 100_000_000  # tokens per 5 hours (see db.credits.REQUEST_TOKENS_MAX)
EXPORTS_PER_HOUR = 10
REQUESTABLE_SCOPES = ("category", "model")


def _t(key, **params):
    return translate(g.lang, key, **params)


def _back(anchor: str | None = None):
    return redirect(url_for("account.index", _anchor=anchor) if anchor else url_for("account.index"))


# ----- usage ----------------------------------------------------------------

def _meter(used: float, limit: float) -> dict:
    ratio = (used / limit) if limit > 0 else (1.0 if used > 0 else 0.0)
    return {"used": used, "limit": limit, "left": max(0.0, limit - used), "value": min(used, limit) if limit else 0,
            "max": limit if limit > 0 else 1, "percent": round(min(ratio, 1.0) * 100),
            "state": "full" if ratio >= 1 else "warn" if ratio >= 0.8 else ""}


_QUIET_REASONS = ("default", "not_limited", "admin")


def _reasons(*groups) -> list[str]:
    """Distinct explanations worth showing (not "site default")."""
    texts = []
    for reason in (reason for group in groups for reason in group):
        if reason.code not in _QUIET_REASONS:
            text = reason.text(g.lang)
            if text not in texts:
                texts.append(text)
    return texts


def _window(window) -> dict | None:
    """A 5-hour or weekly limit for the page: meter, whether a window is open and when it ends."""
    if not window.limited:
        return None
    return {"meter": _meter(window.used, window.tokens), "open": window.open,
            "resets_at": db.timestamp(window.resets_at) if window.resets_at else None,
            "slow": _meter(window.slow_used, window.slow_tokens) if window.slow_tokens > 0 else None,
            "blocked": window.left <= 0 and window.slow_left <= 0}


def limits_summary(user) -> list[dict]:
    """Per pool: request rate, 5-hour and weekly tokens with usage and resets, and why (bonus, tier, grants...)."""
    result = []
    for pool in limits.visible_pools(user):
        current = limits.effective(user, pool)
        window, weekly, rate = current.window, current.weekly, current.rate
        result.append({"pool": pool, "unlimited": current.unlimited, "admin": current.admin,
                       "rate": limits.rate_text(g.lang, rate.rules) if rate.limited else None,
                       "window": _window(window), "weekly": _window(weekly),
                       "reasons": [] if current.admin else _reasons(rate.reasons, window.reasons, weekly.reasons)})
    return result


def usage_summary(user, settings: dict | None = None) -> list[dict]:
    """Kept for callers of the previous release's helper: see :func:`limits_summary`."""
    return limits_summary(user)


def model_summary(user) -> list[dict]:
    """Models the account can use that have something to say: a weight, their own limits, a lock, or reasoning
    effort levels (with what is unlocked and how to ask for more)."""
    if user["role"] == "admin":
        return []
    config = current_app.config["BC"]
    context = AccessContext.load(user)
    models = [model for model in catalog.list_models(rolled_out_only=True)
              if (is_text_model(model) or is_image_model(model, config.images_enabled))
              and (context.can_use(model, "chat") or context.can_use(model, "api"))]
    policies = limits_db.model_policies(models)
    overrides = limits_db.model_overrides_for(user["id"])
    own = limits_db.effort_levels(user["id"])
    prefs = limits_db.user_settings(user["id"])
    result = []
    for model in models:
        policy = policies[model["id"]]
        override = overrides.get(model["id"])
        counted = limits.counts_toward_pool(model, policy)
        effort = limits.effort_summary(user, model, lang=g.lang, policy=policy, own=own, prefs=prefs)
        own_limits = policy["enabled"] or override is not None
        if not (own_limits or effort or not counted or policy["weight"] != 1):
            continue
        weight = f"{policy['weight']:g}"
        entry = {"name": model["display_name"], "id": model["ollama_name"], "weight": policy["weight"],
                 "weight_text": weight.replace(".", ",") if g.lang == "it" else weight,
                 "counted": counted, "locked": bool(override and override.locked), "rate": None, "window": None,
                 "weekly": None, "effort": effort, "reasons": []}
        if own_limits:
            current = limits.model_limits(user, model, policy=policy)
            entry.update(rate=limits.rate_text(g.lang, current.rate.rules) if current.rate.limited else None,
                         window=_window(current.window), weekly=_window(current.weekly),
                         reasons=_reasons(current.rate.reasons, current.window.reasons, current.weekly.reasons))
        if effort:
            entry["effort_allowed"] = [level["label"] for level in effort["levels"] if level["allowed"]
                                       and level["value"] != "off"]
            entry["effort_locked"] = [level for level in effort["levels"] if not level["allowed"]]
        result.append(entry)
    return result


def describe_grant(grant) -> str:
    """One line for a grant: what it gives, where, and until when."""
    kind = grant["kind"]
    if kind == "unlimited":
        what = _t("account.grant_kind_unlimited")
    elif kind == "multiplier":
        what = _t("account.grant_kind_multiplier", factor=f"{grant['amount']:g}")
    else:
        what = _t("account.grant_kind_extra", amount=tokens_text(grant["amount"], g.lang))
    scope = _t(f"account.grant_scope_{grant['scope'] or 'all'}")
    if grant["model_id"]:
        where = _t("account.grant_model", model=grant["model_name"] or "—")
    else:
        where = _t(f"account.pool_{grant['pool']}") if grant["pool"] else _t("account.grant_pool_all")
    return _t("account.grant_line", what=what, scope=scope, pool=where)


def _grants(user) -> list[dict]:
    if user["role"] == "admin":
        return []
    now = db.now()
    return [{"text": describe_grant(row), "everyone": row["user_id"] is None, "reason": row["reason"],
             "starts_at": row["starts_at"] if row["starts_at"] > now else None, "ends_at": row["ends_at"]}
            for row in limits_db.user_grants(user["id"])]


def _dynamic(user) -> dict | None:
    """The current dynamic adjustment, when the 5-hour or weekly tokens of the account follow demand."""
    if user["role"] == "admin":
        return None
    for pool in limits.visible_pools(user):
        policy = limits_db.get_policy(pool)
        if not any(policy[period]["enabled"] and policy[period]["dynamic"] for period in limits_db.PERIODS):
            continue
        current = limits.effective(user, pool, usage=False)
        if current.dynamic_applies and current.dynamic is not None:
            dynamic = current.dynamic
            return {"multiplier": dynamic.multiplier, "percent": round(abs(dynamic.multiplier - 1) * 100),
                    "reasons": [reason.text(g.lang) for reason in dynamic.reasons]}
    return None


def _tier(user) -> dict | None:
    progress = limits.tier_progress(user)
    if progress is None or not progress["auto"]:
        return None
    return progress


# ----- sign-in sessions -----------------------------------------------------

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
             ("Chromium/", "Chromium"), ("Safari/", "Safari"))
_SYSTEMS = (("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iPadOS"), ("CrOS", "ChromeOS"),
            ("Windows", "Windows"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("Linux", "Linux"))


def describe_agent(agent: str) -> str:
    """A short, human description of a User-Agent header ("Firefox on Linux")."""
    agent = agent or ""
    browser = next((name for marker, name in _BROWSERS if marker in agent), None)
    system = next((name for marker, name in _SYSTEMS if marker in agent), None)
    if browser and system:
        return _t("account.device_browser_on", browser=browser, system=system)
    if browser or system:
        return browser or system
    cleaned = " ".join(agent.split())[:60]
    return cleaned or _t("account.device_unknown")


def _sessions(user_id: str) -> list[dict]:
    current = security.current_session_hash()
    return [{"id": row["id_hash"], "device": describe_agent(row["user_agent"]), "ip": row["ip_address"],
             "created_at": row["created_at"], "last_seen_at": row["last_seen_at"],
             "current": row["id_hash"] == current} for row in users.list_sessions(user_id)]


# ----- access ---------------------------------------------------------------

def _agents_enabled() -> bool:
    from bananachat.services.agents import settings as agent_settings
    return agent_settings.enabled()


def _capability_label(scope: str) -> str:
    return _t(f"account.scope_{scope}")


def access_overview(user) -> dict:
    """What the person may request: capabilities, and categories/models of the catalog that deny them."""
    context = AccessContext.load(user)
    images_enabled = current_app.config["BC"].images_enabled
    if context.is_admin:
        return {"admin": True, "capabilities": [], "resources": [], "requestable": {}}
    capabilities = []
    for scope in access_db.CAPABILITY_SCOPES:
        if scope == "image_generation" and not images_enabled:
            continue
        if scope == "agents" and not _agents_enabled():
            continue
        capabilities.append(context.gate(scope, 0, _capability_label(scope)))
    resources: dict[tuple[str, int], dict] = {}
    for model in catalog.list_models(rolled_out_only=True):
        if not (is_text_model(model) or is_image_model(model, images_enabled)):
            continue
        for surface in ("chat", "api"):
            for gate in context.denied_gates(model, surface):
                if gate.scope not in REQUESTABLE_SCOPES:
                    continue
                entry = resources.setdefault((gate.scope, gate.resource_id), {"gate": gate, "models": set()})
                entry["models"].add(model["display_name"])
    listed = []
    for entry in resources.values():
        gate = entry["gate"]
        if gate.requests_enabled or gate.pending_request:
            listed.append({"gate": gate, "models": sorted(entry["models"], key=str.lower)})
    listed.sort(key=lambda item: (item["gate"].scope, item["gate"].label.lower()))
    requestable = {(gate.scope, gate.resource_id): gate for gate in capabilities if gate.requestable}
    requestable.update({(item["gate"].scope, item["gate"].resource_id): item["gate"]
                        for item in listed if item["gate"].requestable})
    return {"admin": False, "capabilities": capabilities, "resources": listed, "requestable": requestable}


def _request_history(user_id: str) -> list[dict]:
    history = []
    for row in access_db.list_requests(status=None, user_id=user_id, limit=50):
        if row["scope"] in access_db.CAPABILITY_SCOPES:
            label = _capability_label(row["scope"])
        else:
            label = row["resource_name"] or _t("account.resource_removed")
        history.append({"row": row, "label": label,
                        "kind": _t(f"account.scope_kind_{row['scope']}")})
    return history


# ----- page -----------------------------------------------------------------

@bp.get("/account", endpoint="index")
@security.login_required
def index():
    return _page()


def _page(draft: dict | None = None):
    """The account page; *draft* re-fills a rejected request form (answered with HTTP 400)."""
    user = security.current_user()
    settings = g.settings
    config = current_app.config["BC"]
    bonus_mode, bonus_regular, bonus_slow = credits.music_bonus(user["id"], settings)
    access = access_overview(user)
    pending = credits.pending_request(user["id"])
    form = _request_form(user) if user["role"] != "admin" else None
    if draft is None and form is not None and request.args.get("request") == "effort":
        # "Request access" from a locked level in the chat composer.
        draft = {"form": "quota", "kind": "effort", "effort_model": (request.args.get("model") or "")[:300],
                 "effort_level": (request.args.get("level") or "")[:10], "prefill": True}
    return render_template(
        "account/index.html",
        user=user,
        usage=limits_summary(user),
        models=model_summary(user),
        grants=_grants(user),
        dynamic=_dynamic(user),
        tier=_tier(user),
        bonus_mode=bonus_mode,
        bonus_regular=bonus_regular,
        bonus_slow=bonus_slow,
        quota_form=form,
        slow_enabled=bool(settings.get("slow_credits_enabled", 1)),
        auto_approve=bool(settings.get("quota_auto_approve_enabled")),
        auto_max_tokens=int(settings.get("quota_auto_approve_max_tokens") or 0),
        auto_max_slow=int(settings.get("quota_auto_approve_max_slow_tokens") or 0),
        auto_max_weekly=int(settings.get("quota_auto_approve_max_weekly_tokens") or 0),
        pending_quota=pending,
        pending_summary=request_summary(pending) if pending else "",
        quota_history=[{"row": row, "summary": request_summary(row)} for row in credits.user_requests(user["id"])],
        sessions=_sessions(user["id"]),
        access=access,
        requestable=[{"value": f"{scope}:{resource_id}", "label": gate.label,
                      "kind": _t(f"account.scope_kind_{scope}")}
                     for (scope, resource_id), gate in access["requestable"].items()],
        access_history=_request_history(user["id"]),
        retention_days=config.deleted_chat_retention_days,
        use_case_min=USE_CASE_MIN,
        use_case_max=USE_CASE_MAX,
        quota_max=QUOTA_REQUEST_MAX,
        is_last_admin=user["role"] == "admin" and users.count_active_admins() <= 1,
        draft=draft or {},
    ), (400 if draft and not draft.get("prefill") else 200)


def _effort_targets(user) -> list[dict]:
    """Models (and "all models") the account may ask a higher reasoning effort for: those with a locked level."""
    if not limits.effort_gated(user):
        return []
    context = AccessContext.load(user)
    targets = []
    top_rank = -1
    for model in catalog.list_models(rolled_out_only=True):
        if not is_text_model(model) or not context.can_use(model, "chat") and not context.can_use(model, "api"):
            continue
        summary = limits.effort_summary(user, model, lang=g.lang)
        if not summary or not summary["locked"]:
            continue
        top_rank = max(top_rank, max(limits_db.effort_rank(level["value"]) for level in summary["levels"]))
        targets.append({"value": model["ollama_name"], "label": model["display_name"],
                        "levels": [level for level in summary["levels"] if not level["allowed"]
                                   and level["value"] not in ("off", "on")]
                        or [{"value": "medium", "label": limits.effort_label("medium", g.lang)}]})
    if targets:
        own = limits_db.effort_levels(user["id"]).get(None)
        current = own.level if own else limits.effort_settings()["default"]
        levels = [{"value": level, "label": limits.effort_label(level, g.lang)} for level in limits_db.EFFORT_LEVELS
                  if limits_db.effort_rank(current) < limits_db.effort_rank(level) <= max(top_rank, 1)]
        if levels:
            targets.insert(0, {"value": "all", "label": _t("account.effort_all_models"), "levels": levels})
    return targets


def _request_form(user) -> dict:
    """What the request form offers: kinds, pools, and the account's current values per pool (for defaults)."""
    pools = limits.visible_pools(user)
    bases = {pool: limits.base_limits(user["id"], pool) for pool in pools}
    window_pools = [pool for pool in pools if bases[pool]["window_enabled"]]
    weekly_pools = [pool for pool in pools if bases[pool]["weekly_enabled"]]
    rate_pools = [pool for pool in pools if bases[pool]["rate_enabled"]]
    efforts = _effort_targets(user)
    kinds = [kind for kind, offered in (("window", window_pools), ("weekly", weekly_pools), ("rate", rate_pools),
                                        ("temporary", True), ("effort", efforts)) if offered]
    current = {pool: {"tokens": base["window_tokens"], "slow": base["window_slow_tokens"],
                      "weekly": base["weekly_tokens"],
                      "rules": [{"per": rule["per"], "requests": rule["requests"],
                                 "label": limits.rule_text(g.lang, rule)} for rule in base["rate_rules"]]}
               for pool, base in bases.items()}
    return {"pools": pools, "kinds": kinds, "current": current, "extra_pools": window_pools,
            "pools_for": {"window": window_pools, "weekly": weekly_pools, "rate": rate_pools, "temporary": pools,
                          "effort": pools},
            "efforts": efforts,
            "hours_max": credits.REQUEST_HOURS_MAX, "weekly_max": credits.REQUEST_WEEKLY_MAX}


def _request_tokens(row, name: str, legacy: str):
    value = row[name]
    return value if value is not None else (row[legacy] or 0) * credits.TOKENS_PER_CREDIT


def request_summary(row) -> str:
    """What a quota request asks for, in one line."""
    kind = credits.request_kind(row)
    pool = _t(f"account.pool_{row['pool'] or 'api'}")
    if kind == "effort":
        model = row["model_name"] or (_t("account.effort_all_models") if row["effort_all_models"] else "—")
        return _t("account.quota_summary_effort", level=limits.effort_label(row["effort_level"] or "medium", g.lang),
                  model=model)
    if kind == "weekly":
        return _t("account.quota_summary_weekly", tokens=_amount(_request_tokens(row, "new_weekly_tokens",
                                                                               "new_weekly_credits")), pool=pool)
    if kind == "rate":
        rules = credits.request_rules(row)
        rate = ", ".join(limits.rule_text(g.lang, rule) for rule in rules) if rules else "—"
        return _t("account.quota_summary_rate", pool=pool, rate=rate)
    if kind == "temporary":
        if row["grant_unlimited"]:
            return _t("account.quota_summary_unlimited", hours=row["grant_hours"], pool=pool)
        return _t("account.quota_summary_temporary", tokens=_amount(_request_tokens(row, "new_tokens", "new_credits")),
                  hours=row["grant_hours"], pool=pool)
    return _t("account.quota_summary_window", tokens=_amount(_request_tokens(row, "new_tokens", "new_credits")),
              slow=_amount(_request_tokens(row, "new_slow_tokens", "new_slow_credits")), pool=pool)


def _amount(value) -> str:
    return tokens_text(value or 0, g.lang)


# ----- password and sessions ------------------------------------------------

@bp.post("/account/password", endpoint="change_password")
@security.login_required
@security.rate_limit("account-password", 10, 900, per_user=True)
def change_password():
    user = security.current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not security.verify_password(user["password"], current):
        flash(_t("account.password_wrong"), "error")
        return _back("password")
    problem = security.password_problem(new, request.form.get("confirm_password") or "")
    if problem:
        flash(_t(problem), "error")
        return _back("password")
    if new == current:
        flash(_t("account.password_same"), "error")
        return _back("password")
    users.set_password(user["id"], security.hash_password(new), keep_session=security.current_session_hash())
    users.audit(user, "account.password_change", user["id"], ip_address=security.client_ip())
    flash(_t("account.password_changed"), "success")
    return _back("password")


@bp.post("/account/sessions/<id_hash>/revoke", endpoint="revoke_session")
@security.login_required
def revoke_session(id_hash):
    user = security.current_user()
    if not re.fullmatch(r"[0-9a-f]{64}", id_hash):
        flash(_t("account.session_not_found"), "error")
        return _back("sessions")
    if id_hash == security.current_session_hash():
        security.logout()
        flash(_t("auth.signed_out"), "info")
        return redirect(url_for("auth.login"))
    if users.revoke_session(user["id"], id_hash):
        users.audit(user, "account.session_revoke", user["id"], ip_address=security.client_ip())
        flash(_t("account.session_revoked"), "success")
    else:
        flash(_t("account.session_not_found"), "error")
    return _back("sessions")


@bp.post("/account/sessions/revoke-others", endpoint="revoke_other_sessions")
@security.login_required
def revoke_other_sessions():
    user = security.current_user()
    current = security.current_session_hash()
    if current:
        users.revoke_sessions(user["id"], except_hash=current)
        users.audit(user, "account.sessions_revoke_others", user["id"], ip_address=security.client_ip())
    flash(_t("account.sessions_revoked"), "success")
    return _back("sessions")


# ----- personal data --------------------------------------------------------

def _download(payload: dict, kind: str):
    user = security.current_user()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", user["username"])
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    response = current_app.response_class(body, mimetype="application/json")
    response.headers["Content-Disposition"] = f'attachment; filename="bananachat-{kind}-{safe_name}-{stamp}.json"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _export(kind: str, build):
    user = security.current_user()
    if not security.allow("account-export", EXPORTS_PER_HOUR, 3600, key="user:" + user["id"]):
        return security.too_many_requests(600)
    payload = build(user["id"])
    users.audit(user, "account.export", user["id"], {"kind": kind}, security.client_ip())
    return _download(payload, kind)


@bp.route("/account/export/gdpr", methods=["GET", "POST"], endpoint="export_data")
@security.login_required
def export_data():
    return _export("data", exports.gdpr_export)


@bp.route("/account/export/chats", methods=["GET", "POST"], endpoint="export_chats")
@security.login_required
def export_chats():
    return _export("chats", exports.chats_export)


@bp.post("/account/chats/delete-all", endpoint="delete_chats")
@security.login_required
@security.rate_limit("account-danger", 10, 900, per_user=True)
def delete_chats():
    user = security.current_user()
    if not security.verify_password(user["password"], request.form.get("password") or ""):
        flash(_t("account.password_wrong"), "error")
        return _back("data")
    try:
        from bananachat.db import chats as chats_db
        delete_all = chats_db.delete_all_for_user
    except (ImportError, AttributeError):
        log.error("bananachat.db.chats.delete_all_for_user is not available")
        flash(_t("account.chats_delete_unavailable"), "error")
        return _back("data")
    days = current_app.config["BC"].deleted_chat_retention_days
    # Without a retention period the chats are erased at once rather than waiting for the purge job.
    result = delete_all(user["id"], hard=True) if days == 0 else delete_all(user["id"])
    users.audit(user, "account.chats_delete_all", user["id"],
                {"count": result} if isinstance(result, int) else None, security.client_ip())
    flash(_t("account.chats_deleted", count=days) if days else _t("account.chats_deleted_now"), "success")
    return _back("data")


@bp.post("/account/delete", endpoint="delete")
@security.login_required
@security.rate_limit("account-danger", 10, 900, per_user=True)
def delete_account():
    user = security.current_user()
    if (request.form.get("confirm_username") or "").strip() != user["username"]:
        flash(_t("account.delete_username_mismatch"), "error")
        return _back("delete")
    if not security.verify_password(user["password"], request.form.get("password") or ""):
        flash(_t("account.password_wrong"), "error")
        return _back("delete")
    background_name = users.get_preferences(user["id"])["background_image"]
    try:
        with db.transaction():
            users.audit(user, "account.delete", user["id"], {"username": user["username"]}, security.client_ip())
            users.delete(user["id"])
    except ValueError:
        flash(_t("account.delete_last_admin"), "error")
        return _back("delete")
    if background_name:
        from bananachat.web.customization import remove_background_file
        remove_background_file(background_name)
    security.logout()
    flash(_t("account.deleted"), "info")
    return redirect(url_for("auth.login"))


# ----- requests -------------------------------------------------------------

def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


REQUEST_FIELDS = ("kind", "pool", "tokens", "slow_tokens", "weekly_tokens", "rate_per", "rate_requests", "hours",
                  "extra_tokens", "unlimited", "effort_model", "effort_level", "reason")


def _tokens_field(value):
    """A typed token amount (``50000``, ``50k``, ``1,5M``) as a whole number, or None."""
    amount = parse_amount(value)
    return None if amount is None else int(round(amount))


@bp.post("/account/quota-request", endpoint="quota_request")
@security.login_required
@security.rate_limit("quota-request", 10, 3600, per_user=True)
def quota_request():
    """Ask for more 5-hour or weekly tokens, a higher request rate, a temporary increase or a higher reasoning
    effort."""
    user = security.current_user()
    if user["role"] == "admin":
        flash(_t("account.admin_unlimited"), "info")
        return _back("quota")
    form = request.form
    kind = form.get("kind") or "window"
    pool = form.get("pool") or "api"
    draft = {"form": "quota", **{name: (form.get(name) or "").strip() for name in REQUEST_FIELDS}}
    slow_enabled = bool(g.settings.get("slow_credits_enabled", 1))
    model_id = None
    if kind == "effort":
        target = (form.get("effort_model") or "").strip()
        if target != "all":
            model = catalog.get_by_name(target)
            context = AccessContext.load(user)
            if model is None or not (context.can_use(model, "chat") or context.can_use(model, "api")):
                flash(_t("account.quota_effort_model"), "error")
                return _page(draft)
            model_id = model["id"]
    try:
        outcome = credits.submit_request(
            user["id"], _tokens_field(form.get("extra_tokens" if kind == "temporary" else "tokens")),
            _tokens_field(form.get("slow_tokens")) if slow_enabled and (form.get("slow_tokens") or "").strip()
            else None,
            form.get("reason") or "", kind=kind, pool=pool, weekly=_tokens_field(form.get("weekly_tokens")),
            per=form.get("rate_per"), requests=_int(form.get("rate_requests")), hours=_int(form.get("hours")),
            unlimited=form.get("unlimited") == "1", model_id=model_id, level=form.get("effort_level"))
    except credits.RequestError as error:
        params = dict(error.params)
        if "max" in params and error.key == "quota_range":
            params["max"] = tokens_text(params["max"], g.lang)
        flash(_t(f"account.{error.key}", **params), "error")
        return _page(draft)
    except (ValueError, sqlite3.IntegrityError):
        flash(_t("account.quota_failed"), "error")
        return _page(draft)
    row = db.one("SELECT r.*, m.display_name AS model_name FROM quota_requests r "
                 "LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.id=?", (outcome["id"],))
    users.audit(user, "quota.request", outcome["id"], {"kind": credits.request_kind(row), "pool": row["pool"],
                                                       "status": outcome["status"], "tokens": row["new_tokens"],
                                                       "slow": row["new_slow_tokens"],
                                                       "weekly": row["new_weekly_tokens"],
                                                       "rules": credits.request_rules(row),
                                                       "hours": row["grant_hours"], "model": row["model_id"],
                                                       "effort": row["effort_level"]}, security.client_ip())
    if outcome["status"] == "approved":
        flash(_t("account.quota_approved_now", summary=request_summary(row)), "success")
    else:
        flash(_t("account.quota_submitted"), "success")
    return _back("quota")


@bp.post("/account/access-request", endpoint="access_request")
@security.login_required
@security.rate_limit("access-request", 10, 3600, per_user=True)
def access_request():
    user = security.current_user()
    draft = {"form": "access", "resource": request.form.get("resource") or "",
             "use_case": (request.form.get("use_case") or "").strip()}
    if user["role"] == "admin":
        flash(_t("account.access_admin"), "info")
        return _back("access")
    scope, _, raw_id = (request.form.get("resource") or "").partition(":")
    resource_id = _int(raw_id)
    overview = access_overview(user)
    gate = overview["requestable"].get((scope, resource_id))
    if gate is None:
        flash(_t("account.access_not_requestable"), "error")
        return _page(draft)
    use_case = (request.form.get("use_case") or "").strip()
    if not USE_CASE_MIN <= len(use_case) <= USE_CASE_MAX:
        flash(_t("account.access_use_case_length", min=USE_CASE_MIN, max=USE_CASE_MAX), "error")
        return _page(draft)
    confirmed_safe = request.form.get("confirmed_safe") == "1"
    confirmed_logging = request.form.get("confirmed_logging") == "1"
    if not (confirmed_safe and confirmed_logging):
        flash(_t("account.access_confirmations"), "error")
        return _page(draft)
    try:
        request_id = access_db.create_request(scope, resource_id, user["id"], use_case,
                                              confirmed_safe=confirmed_safe, confirmed_logging=confirmed_logging)
    except (ValueError, sqlite3.IntegrityError):
        flash(_t("account.access_failed"), "error")
        return _page(draft)
    users.audit(user, "access.request", request_id, {"scope": scope, "resource_id": resource_id},
                security.client_ip())
    flash(_t("account.access_submitted", name=gate.label), "success")
    return _back("access")
