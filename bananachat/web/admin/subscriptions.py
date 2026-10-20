"""Claude and Codex subscription pools: accounts, caps, model selection and strict limits.

Subscription models do not use the Ollama server. Administrators pool each
provider's subscription accounts here, bind each to a private profile of the
operator's adapter, cap how much of the provider's own allowance it may use and
choose which discovered models to offer. Adding an account, changing its profile
or removing it needs a recent confirmation (:mod:`._reauth`): the binding decides
which subscription answers users' prompts. Checks and refreshes do not.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

from flask import abort, current_app, flash, request

from bananachat import db
from bananachat.db import catalog
from bananachat.db import settings as site_settings
from bananachat.db import subscription_pool as pool_db
from bananachat.security import admin_required
from bananachat.services import model_lifecycle, subscription_pool

from . import _reauth, bp
from ._helpers import FormError, audit, back, choice, integer, text, tokens

log = logging.getLogger("bananachat.admin")

# What each page calls the provider, and the server settings that connect its adapter.
SETUP = {
    "claude": {"name": "Claude", "client": "Claude Code", "module": "bananachat.services.claude_code",
               "extension": "BC_CLAUDE_EXTENSION", "manifest": "BC_CLAUDE_CODE_CONFIG", "example": "claude-1"},
    "codex": {"name": "Codex", "client": "Codex CLI", "module": "bananachat.services.codex_cli",
              "extension": "BC_CODEX_EXTENSION", "manifest": "BC_CODEX_CLI_CONFIG", "example": "codex-1"},
}
# Codex models without a tier name share the "gpt" family.
FAMILY_NAMES = {"gpt": "Other GPT"}
NO_CAP = {"cap_pct": None, "window_cap": None, "weekly_cap": None}


def _adapter(provider: str):
    """The provider's adapter when it binds accounts to the operator's private profiles (the built-in CLIs)."""
    adapter = current_app.extensions.get(f"{provider}_adapter")
    if isinstance(getattr(adapter, "bindings_key", None), str) and isinstance(getattr(adapter, "profiles", None), dict):
        return adapter
    return None


def _shown_errors(adapter) -> tuple:
    """The adapter module's credential-free ``ConnectorError``, whose messages are written for administrators."""
    error = getattr(sys.modules.get(type(adapter).__module__), "ConnectorError", None)
    return (error,) if isinstance(error, type) and issubclass(error, Exception) else ()


def _account(provider: str, account_id: int):
    row = pool_db.get(account_id)
    if row is None or row["provider"] != provider:
        abort(404)
    return row


def _families(provider: str) -> dict:
    return {family: FAMILY_NAMES.get(family, family.capitalize()) for family in subscription_pool.STRICTNESS[provider]}


def _stamp(epoch):
    return db.timestamp(datetime.fromtimestamp(epoch, timezone.utc)) if epoch is not None else None


def _usage_view(account, report) -> dict:
    view = dict(report)
    view["fresh"] = subscription_pool.subscription_fresh(account)
    observed = report.get("observed_at")
    view["has_observation"] = observed is not None and observed <= time.time() \
        and any(report.get(key) is not None for key in ("window_left", "weekly_left"))
    for key in ("observed_at", "window_resets_at", "weekly_resets_at"):
        view[key] = _stamp(report.get(key))
    view["model_limits"] = {family: {**scope, "resets_at": _stamp(scope.get("resets_at"))}
                            for family, scope in report.get("model_limits", {}).items()}
    return view


def _context(provider: str) -> dict:
    accounts = pool_db.list_accounts(provider=provider)
    quota = subscription_pool.pooled_quota(provider)
    discovery = subscription_pool.discovered_models(provider)
    automatic = subscription_pool.automatic_enrollment(provider)
    selected = site_settings.state_get(f"{provider}.selected_models")
    excluded = site_settings.state_get(f"{provider}.excluded_models")
    rules = model_lifecycle.patterns()
    available = []
    for item in discovery["models"]:
        row = catalog.get_by_name(item["name"])
        row = row if row is not None and row["backend"] == provider else None
        if automatic and isinstance(excluded, list):
            chosen = item["name"] not in excluded
        else:
            chosen = item["name"] in selected if isinstance(selected, list) else row is not None
        available.append({"item": item, "row": row, "selected": chosen,
                          "strict": subscription_pool.strict_policy(provider, item["name"], family=item["family"]),
                          "ignored": model_lifecycle.matches(item["name"], rules)})
    reports = subscription_pool.account_reports(provider) or {}
    usage_views, caps, family_capped, blind = {}, {}, set(), set()
    for account in accounts:
        report = reports.get(str(account["id"]))
        if isinstance(report, dict) and report.get("source") != "local":
            usage_views[account["id"]] = _usage_view(account, report)
        scoped = pool_db.scoped_caps(account["id"])
        caps[account["id"]] = {family: scoped.get(family, NO_CAP) for family in subscription_pool.STRICTNESS[provider]}
        if any(cap != NO_CAP for cap in caps[account["id"]].values()):
            family_capped.add(account["id"])
        view = usage_views.get(account["id"])
        reported = {scope for scope in ("window", "weekly")
                    if view and view["fresh"] and view.get(f"{scope}_left") is not None}
        # A share of an allowance nobody reported cannot be honoured: the account waits, it is not idle.
        if account["status"] == "active" and any(account[f"{scope}_cap_pct"] is not None and scope not in reported
                                                 for scope in ("window", "weekly")):
            blind.add(account["id"])
    adapter = _adapter(provider)
    bindings = adapter.bindings() if adapter else {}
    transport = current_app.extensions.get(f"{provider}_transport", {})
    return {"accounts": accounts, "quota": quota, "discovery": discovery, "available": available,
            "enrollment": "automatic" if automatic else "manual", "automatic_enrollment": automatic,
            "connected": subscription_pool.connected(provider),
            "has_discovery": subscription_pool.discovers(provider),
            "extension_error": bool(transport.get("error")), "profiled": adapter is not None,
            "profiles": sorted(adapter.profiles) if adapter else [],
            "bindings": {row["id"]: bindings.get(str(row["id"]), row["label"]) for row in accounts},
            "usage_views": usage_views, "caps": caps, "family_capped": family_capped, "blind": blind,
            "local_usage": {row["id"]: subscription_pool._current(row) for row in accounts},
            "resting": {row["id"]: row["cooldown_until"] for row in accounts if subscription_pool._resting(row)},
            "stale": {row["id"] for row in accounts if not subscription_pool.quota_fresh(row)},
            "waiting": {row["id"] for row in accounts if row["status"] == "active" and not (
                subscription_pool.quota_fresh(row) and subscription_pool.subscription_fresh(row))},
            "busy": {row["id"] for row in accounts if pool_db.leased(row["id"])},
            "families": _families(provider), "strictness": subscription_pool.STRICTNESS[provider]}


@bp.get("/models/claude", endpoint="claude_pool", defaults={"provider": "claude"})
@bp.get("/models/codex", endpoint="codex_pool", defaults={"provider": "codex"})
@admin_required
def pool_page(provider):
    from .models import _render

    return _render("admin/subscriptions.html", tab=provider, provider=provider, setup=SETUP[provider],
                   **_context(provider))


def _allowance_caps() -> dict:
    return {"window_cap_pct": integer("window_cap_pct", minimum=1, maximum=100, label="The 5-hour allowance cap",
                                      optional=True),
            "weekly_cap_pct": integer("weekly_cap_pct", minimum=1, maximum=100, label="The weekly allowance cap",
                                      optional=True)}


def _family_caps(provider: str) -> dict:
    caps = {}
    for family, name in _families(provider).items():
        caps[family] = {
            "cap_pct": integer(f"cap_pct_{family}", minimum=1, maximum=100, label=f"The {name} allowance cap",
                               optional=True),
            "window_cap": tokens(f"window_cap_{family}", label=f"{name} 5-hour tokens", optional=True,
                                 maximum=pool_db.TOKENS_MAX),
            "weekly_cap": tokens(f"weekly_cap_{family}", label=f"{name} weekly tokens", optional=True,
                                 maximum=pool_db.TOKENS_MAX)}
    return caps


def _profile(provider: str) -> str | None:
    adapter = _adapter(provider)
    if adapter is None:
        return None
    return choice("profile", tuple(adapter.profiles), label=f"{SETUP[provider]['client']} profile")


def _bind(adapter, account, name: str) -> None:
    """Bind an account to a private profile: one account per profile, never in the middle of a request."""
    bindings = dict(adapter.bindings())
    for row in pool_db.list_accounts(provider=account["provider"]):
        if row["id"] != account["id"] and bindings.get(str(row["id"]), row["label"]) == name:
            raise FormError("This profile already belongs to another pooled account.")
    if name != bindings.get(str(account["id"]), account["label"]) and pool_db.leased(account["id"]):
        raise FormError("Wait for this account's current request to finish before changing its profile.")
    bindings[str(account["id"])] = name
    site_settings.state_set(adapter.bindings_key, bindings)


@bp.post("/models/claude/accounts/<int:account_id>/check", endpoint="claude_account_check",
         defaults={"provider": "claude"})
@bp.post("/models/codex/accounts/<int:account_id>/check", endpoint="codex_account_check",
         defaults={"provider": "codex"})
@admin_required
def account_check(provider, account_id):
    row = _account(provider, account_id)
    adapter = _adapter(provider)
    if adapter is None:
        abort(400)
    shown = _shown_errors(adapter)
    try:
        name, _ = adapter.profile_for(row)
        adapter.check_profile(name)
    except shown as error:
        flash(str(error), "error")
    except Exception as error:  # noqa: BLE001 - other errors can carry provider output; only their type is logged
        log.warning("Checking a %s profile failed (%s)", provider, type(error).__name__)
        flash("The profile could not be checked. Verify its private server configuration.", "error")
    else:
        flash("Subscription login verified. Model access is checked when a request runs.", "success")
    audit(f"{provider}_account_check", row["label"], {"id": account_id})
    return back(f"admin.{provider}_pool", _anchor=f"account-{account_id}")


@bp.post("/models/claude/accounts", endpoint="claude_account_add", defaults={"provider": "claude"})
@bp.post("/models/codex/accounts", endpoint="codex_account_add", defaults={"provider": "codex"})
@admin_required
def account_add(provider):
    if not _reauth.confirmed():
        return back(f"admin.{provider}_pool")
    adapter = _adapter(provider)
    try:
        label = text("label", max_length=80, required=True, label="Account name")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority", default=0)
        window = tokens("window_limit", label="5-hour tokens", maximum=pool_db.TOKENS_MAX)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        caps = _allowance_caps()
        profile = _profile(provider)
        with db.transaction():
            account_id = pool_db.add_account(label, provider=provider, priority=priority, window_limit=window,
                                             weekly_limit=weekly, **caps)
            if profile is not None:
                _bind(adapter, pool_db.get(account_id), profile)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back(f"admin.{provider}_pool")
    if adapter is not None:
        adapter.invalidate_models()
    subscription_pool.invalidate(provider)
    audit(f"{provider}_account_add", label, {"id": account_id, "profile": profile, **caps})
    flash(f"Subscription account {label} added to the {SETUP[provider]['name']} pool.", "success")
    return back(f"admin.{provider}_pool", _anchor=f"account-{account_id}")


@bp.post("/models/claude/accounts/<int:account_id>", endpoint="claude_account_save", defaults={"provider": "claude"})
@bp.post("/models/codex/accounts/<int:account_id>", endpoint="codex_account_save", defaults={"provider": "codex"})
@admin_required
def account_save(provider, account_id):
    row = _account(provider, account_id)
    pool = f"admin.{provider}_pool"
    adapter = _adapter(provider)
    if request.form.get("action") == "delete":
        if not _reauth.confirmed():
            return back(pool, _anchor=f"account-{account_id}")
        try:
            with db.transaction():
                pool_db.remove_account(account_id)
                if adapter is not None:
                    bindings = dict(adapter.bindings())
                    bindings.pop(str(account_id), None)
                    site_settings.state_set(adapter.bindings_key, bindings)
        except ValueError as error:
            flash(str(error), "error")
            return back(pool, _anchor=f"account-{account_id}")
        subscription_pool.invalidate(provider)
        audit(f"{provider}_account_delete", row["label"], {"id": account_id})
        flash(f"Subscription account {row['label']} removed.", "success")
        return back(pool)
    try:
        status = choice("status", pool_db.STATUSES, label="Status")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority")
        note = text("note", max_length=500, label="Note")
        window = tokens("window_limit", label="5-hour tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True, maximum=pool_db.TOKENS_MAX)
        caps = _allowance_caps()
        family_caps = _family_caps(provider)
        profile = _profile(provider)
        rebinding = profile is not None and profile != adapter.bindings().get(str(account_id), row["label"])
        if rebinding and not _reauth.confirmed():
            return back(pool, _anchor=f"account-{account_id}")
        with db.transaction():
            if profile is not None:
                _bind(adapter, row, profile)
            pool_db.update_account(account_id, status=status, priority=priority, note=note, window_limit=window,
                                   weekly_limit=weekly, **caps)
            for family, values in family_caps.items():
                pool_db.set_scoped_cap(account_id, family, **values)
            if status == "active":
                pool_db.clear_cooldown(account_id)
    except (FormError, ValueError) as error:
        flash(str(error), "error")
        return back(pool, _anchor=f"account-{account_id}")
    if rebinding:
        # The previous profile's percentages must not admit a request if the refresh below fails.
        subscription_pool.forget_report(provider, account_id)
        adapter.invalidate_models()
    subscription_pool.invalidate(provider)
    subscription_pool.refresh_quota(provider)
    audit(f"{provider}_account_save", row["label"], {
        "id": account_id, "status": status, "profile": profile, **caps,
        "family_caps": {family: values for family, values in family_caps.items() if values != NO_CAP}})
    flash(f"Subscription account {row['label']} saved.", "success")
    return back(pool, _anchor=f"account-{account_id}")


@bp.post("/models/claude/sync", endpoint="claude_sync", defaults={"provider": "claude"})
@bp.post("/models/codex/sync", endpoint="codex_sync", defaults={"provider": "codex"})
@admin_required
def sync(provider):
    """Enrol the selected models (checkboxes) with strict limits."""
    selected = request.form.getlist("models")
    try:
        automatic = choice("enrollment", ("manual", "automatic"), label="New model enrolment") == "automatic" \
            if "enrollment" in request.form else None
        state = subscription_pool.sync_catalog(provider, selected=selected, source="admin", auto_enroll=automatic)
    except ValueError as error:
        flash(str(error), "error")
        return back(f"admin.{provider}_pool")
    audit(f"{provider}_sync", "catalog", {"selected": selected, **state})
    parts = []
    if state.get("new"):
        parts.append(f"New: {', '.join(state['new'][:10])}.")
    if state.get("enabled"):
        parts.append(f"Enabled automatically with strict limits: {', '.join(state['enabled'][:10])}.")
    flash(f"{SETUP[provider]['name']} catalogue refreshed. " + (" ".join(parts) if parts else "Nothing changed."),
          "success")
    return back(f"admin.{provider}_pool")


@bp.post("/models/claude/discover", endpoint="claude_discover", defaults={"provider": "claude"})
@bp.post("/models/codex/discover", endpoint="codex_discover", defaults={"provider": "codex"})
@admin_required
def discover(provider):
    adapter = _adapter(provider)
    if adapter is not None:
        adapter.invalidate_models()
    state = subscription_pool.discovered_models(provider, force=True)
    if state["ok"]:
        try:
            subscription_pool.sync_catalog(provider, source="admin" if subscription_pool.automatic_enrollment(provider)
                                           else "background")
        except ValueError as error:
            state = {**state, "ok": False, "note": str(error)}
    audit(f"{provider}_discover", "catalog", {"ok": state["ok"], "count": len(state["models"])})
    flash(f"The adapter reports {len(state['models'])} available {SETUP[provider]['name']} models." if state["ok"]
          else state["note"], "success" if state["ok"] else "error")
    return back(f"admin.{provider}_pool")


@bp.post("/models/claude/refresh", endpoint="claude_refresh", defaults={"provider": "claude"})
@bp.post("/models/codex/refresh", endpoint="codex_refresh", defaults={"provider": "codex"})
@admin_required
def refresh(provider):
    adapter = _adapter(provider)
    if adapter is not None:
        try:
            adapter.usage_reports(force=True)
        except Exception as error:  # noqa: BLE001 - the quota refresh below reports the failure
            log.warning("Refreshing %s usage reports failed (%s)", provider, type(error).__name__)
    quota = subscription_pool.refresh_quota(provider)[provider]
    audit(f"{provider}_refresh", "pool", quota)
    name = SETUP[provider]["name"]
    if quota["reporter_error"]:
        flash(f"The quota report could not be verified. {name} requests are paused until a valid refresh.", "error")
    elif quota["stale"]:
        flash("Some accounts are waiting for a fresh subscription report. Last-known figures are marked stale.",
              "info")
    elif not quota["usable"]:
        flash(f"No {name} account has available quota. Check subscription usage, caps and local token budgets.",
              "info")
    else:
        flash(f"{name} pool quota re-read.", "success")
    return back(f"admin.{provider}_pool")
