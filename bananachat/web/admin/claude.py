"""Claude subscription pool: accounts, model selection and strict limits.

Claude models do not use the Ollama server. Administrators pool the site's
subscription accounts here, choose which Claude models to offer (or ignore),
and set per-model token limits (5-hour window, optional weekly window) and
request rates. Enrollment of new Claude models follows the model settings
(manual review or automatic with strict limits).
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import current_app, flash, request

from bananachat import db
from bananachat.db import catalog
from bananachat.db import claude_pool as pool_db
from bananachat.db import limits as limits_db
from bananachat.security import admin_required
from bananachat.services import claude_pool, model_lifecycle
from bananachat.services.claude_code import Adapter, BINDINGS_KEY, ConnectorError

from . import bp
from ._helpers import FormError, audit, back, choice, integer, text, tokens


def _context() -> dict:
    accounts = pool_db.list_accounts()
    quota = claude_pool.pooled_quota()
    models = [row for row in catalog.list_models() if row["backend"] == "claude"]
    policies = limits_db.model_policies(models)
    discovery = claude_pool.discovered_models()
    available = []
    from bananachat.db import settings as site_settings
    selected = site_settings.state_get(claude_pool.SELECTION_KEY)
    automatic = claude_pool.automatic_enrollment()
    excluded = site_settings.state_get(claude_pool.EXCLUDED_MODELS_KEY)
    for item in discovery["models"]:
        row = catalog.get_by_name(item["name"])
        available.append({"item": item, "row": row,
                        "selected": item["name"] not in excluded if automatic and isinstance(excluded, list)
                                    else item["name"] in selected if isinstance(selected, list) else row is not None,
                        "policy": policies.get(row["id"]) if row else None,
                        "strict": claude_pool.strict_policy(item["name"], family=item["family"]),
                        "ignored": model_lifecycle.matches(item["name"], model_lifecycle.patterns())})
    resting = {row["id"]: row["cooldown_until"] for row in accounts if claude_pool._resting(row)}
    stale = {row["id"] for row in accounts if not claude_pool.quota_fresh(row)}
    transport = current_app.extensions.get("claude_transport", {})
    adapter = current_app.extensions.get("claude_adapter")
    code_adapter = adapter if isinstance(adapter, Adapter) else None
    bindings = code_adapter.bindings() if code_adapter else {}
    usage = claude_pool.account_reports() or {}
    usage_views = {}
    for account in accounts:
        report = usage.get(str(account["id"]))
        if not isinstance(report, dict) or report.get("source") == "local":
            continue
        view = dict(report)
        view["fresh"] = claude_pool.subscription_fresh(account)
        observed = report.get("observed_at")
        view["has_observation"] = observed is not None and observed <= datetime.now(timezone.utc).timestamp() \
            and any(report.get(key) is not None for key in ("window_left", "weekly_left"))
        for key in ("observed_at", "window_resets_at", "weekly_resets_at"):
            stamp = report.get(key)
            view[key] = db.timestamp(datetime.fromtimestamp(stamp, timezone.utc)) if stamp is not None else None
        view["model_limits"] = {family: {**scope, "resets_at": db.timestamp(datetime.fromtimestamp(scope["resets_at"], timezone.utc))
                                           if scope.get("resets_at") is not None else None}
                                for family, scope in report.get("model_limits", {}).items()}
        usage_views[account["id"]] = view
    return {"code_profiles": sorted(code_adapter.profiles) if code_adapter else [],
            "code_bindings": {str(row["id"]): bindings.get(str(row["id"]), row["label"]) for row in accounts},
            "is_code_adapter": code_adapter is not None, "accounts": accounts, "quota": quota, "models": models, "policies": policies, "resting": resting,
            "has_client": claude_pool._site_chat is not None,
            "has_discovery": claude_pool._site_discovery is not None, "extension_error": bool(transport.get("error")),
            "discovery": discovery, "available": available, "stale": stale, "tokens_max": pool_db.TOKENS_MAX,
            "enrollment": "automatic" if automatic else "manual",
            "automatic_enrollment": automatic, "usage_views": usage_views,
            "local_usage": {row["id"]: claude_pool._current(row) for row in accounts},
            "strictness": claude_pool.STRICTNESS}


@bp.get("/models/claude", endpoint="claude_pool")
@admin_required
def pool_page():
    from bananachat.web.admin.models import _render
    return _render("admin/claude.html", tab="claude", **_context())


def _save_profile(account_id):
    adapter = current_app.extensions.get("claude_adapter")
    if not isinstance(adapter, Adapter):
        return
    name = choice("profile", tuple(adapter.profiles), label="Claude Code profile")
    bindings = adapter.bindings()
    for row in pool_db.list_accounts():
        if row["id"] != account_id and bindings.get(str(row["id"]), row["label"]) == name:
            raise FormError("This profile already belongs to another pooled account.")
    row = pool_db.get(account_id)
    previous = bindings.get(str(account_id), row["label"] if row else "")
    if name != previous and pool_db.leased(account_id):
        raise FormError("Wait for this account's current request to finish before changing its profile.")
    bindings[str(account_id)] = name
    from bananachat.db import settings
    settings.state_set(BINDINGS_KEY, bindings)
    if name != previous:
        adapter.invalidate_models()
    claude_pool._discovery_cache.update(at=0.0, value=None)


@bp.post("/models/claude/accounts/<int:account_id>/check", endpoint="claude_account_check")
@admin_required
def account_check(account_id):
    from flask import abort
    row = pool_db.get(account_id)
    if row is None:
        abort(404)
    adapter = current_app.extensions.get("claude_adapter")
    if not isinstance(adapter, Adapter):
        abort(400)
    try:
        name, _ = adapter.profile_for(row)
        adapter.check_profile(name)
    except ConnectorError as error:
        flash(str(error), "error")
    except Exception:
        flash("The profile could not be checked. Verify its private server configuration.", "error")
    else:
        flash("Subscription login verified. Model access is checked when a request runs.", "success")
    audit("claude_account_check", row["label"], {"id": account_id})
    return back("admin.claude_pool")


@bp.post("/models/claude/accounts", endpoint="claude_account_add")
@admin_required
def account_add():
    try:
        label = text("label", max_length=80, required=True, label="Account name")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority", default=0)
        window = tokens("window_limit", label="5-hour tokens", maximum=pool_db.TOKENS_MAX)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        with db.transaction():
            account_id = pool_db.add_account(label, priority=priority, window_limit=window, weekly_limit=weekly)
            _save_profile(account_id)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.claude_pool")
    audit("claude_account_add", label, {"id": account_id})
    flash(f"Subscription account {label} added to the pool.", "success")
    return back("admin.claude_pool")


@bp.post("/models/claude/accounts/<int:account_id>", endpoint="claude_account_save")
@admin_required
def account_save(account_id):
    row = pool_db.get(account_id)
    if row is None:
        from flask import abort
        abort(404)
    action = request.form.get("action", "save")
    try:
        if action == "delete":
            with db.transaction():
                pool_db.remove_account(account_id)
                from bananachat.db import settings
                bindings = settings.state_get(BINDINGS_KEY, {})
                if isinstance(bindings, dict):
                    bindings.pop(str(account_id), None)
                    settings.state_set(BINDINGS_KEY, bindings)
            claude_pool._discovery_cache.update(at=0.0, value=None)
            audit("claude_account_delete", row["label"], {"id": account_id})
            flash(f"Subscription account {row['label']} removed.", "success")
            return back("admin.claude_pool")
        status = choice("status", pool_db.STATUSES, label="Status")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority")
        note = text("note", max_length=500, label="Note")
        window = tokens("window_limit", label="5-hour tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        with db.transaction():
            _save_profile(account_id)
            pool_db.update_account(account_id, status=status, priority=priority, note=note,
                                   window_limit=window, weekly_limit=weekly)
            if status == "active":
                db.execute("UPDATE claude_accounts SET cooldown_until=NULL WHERE id=?", (account_id,))
        claude_pool.refresh_quota()
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back("admin.claude_pool")
    audit("claude_account_save", row["label"], {"id": account_id, "status": status})
    flash(f"Subscription account {row['label']} saved.", "success")
    return back("admin.claude_pool")


@bp.post("/models/claude/sync", endpoint="claude_sync")
@admin_required
def sync():
    """Enroll selected Claude models (checkboxes) with strict limits."""
    selected = request.form.getlist("models")
    try:
        automatic = choice("enrollment", ("manual", "automatic"), label="New model enrollment") == "automatic" if "enrollment" in request.form else None
        state = claude_pool.sync_catalog(selected=selected, source="admin", auto_enroll=automatic)
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.claude_pool")
    audit("claude_sync", "catalog", {"selected": selected, **state})
    parts = []
    if state.get("new"):
        parts.append(f"New: {', '.join(state['new'][:10])}.")
    if state.get("enabled"):
        parts.append(f"Enabled automatically with strict limits: {', '.join(state['enabled'][:10])}.")
    flash("Claude catalog refreshed. " + (" ".join(parts) if parts else "Nothing changed."), "success")
    return back("admin.claude_pool")


@bp.post("/models/claude/discover", endpoint="claude_discover")
@admin_required
def discover():
    adapter = current_app.extensions.get("claude_adapter")
    if isinstance(adapter, Adapter):
        adapter.invalidate_models()
    state = claude_pool.discovered_models(force=True)
    if state["ok"]:
        claude_pool.sync_catalog(source="admin" if claude_pool.automatic_enrollment() else "background")
    audit("claude_discover", "catalog", {"ok": state["ok"], "count": len(state["models"])})
    flash(f"The adapter reports {len(state['models'])} available Claude models." if state["ok"] else state["note"],
          "success" if state["ok"] else "error")
    return back("admin.claude_pool")


@bp.post("/models/claude/refresh", endpoint="claude_refresh")
@admin_required
def refresh():
    adapter = current_app.extensions.get("claude_adapter")
    if isinstance(adapter, Adapter):
        adapter.usage_reports(force=True)
    quota = claude_pool.refresh_quota()
    audit("claude_refresh", "pool", quota)
    if quota["reporter_error"]:
        flash("The quota report could not be verified. Claude requests are paused until a valid refresh.", "error")
    elif quota["stale"]:
        flash("Some accounts are waiting for a fresh subscription report. Last-known figures are marked stale.", "info")
    elif not quota["usable"]:
        flash("No Claude account has available quota. Check subscription usage and local token budgets.", "info")
    else:
        flash("Claude pool quota re-read.", "success")
    return back("admin.claude_pool")
