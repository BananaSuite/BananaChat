"""Claude subscription pool: accounts, model selection and strict limits.

Claude models do not use the Ollama server. Administrators pool the site's
subscription accounts here, choose which Claude models to offer (or ignore),
and set per-model token limits (5-hour window, optional weekly window) and
request rates. Enrollment of new Claude models follows the model settings
(manual review or automatic with strict limits).
"""

from __future__ import annotations

from flask import flash, request

from bananachat import db
from bananachat.db import catalog
from bananachat.db import claude_pool as pool_db
from bananachat.db import limits as limits_db
from bananachat.security import admin_required
from bananachat.services import claude_pool, model_lifecycle

from . import bp
from ._helpers import FormError, audit, back, choice, integer, me, text, tokens


def _context() -> dict:
    accounts = pool_db.list_accounts()
    quota = claude_pool.pooled_quota()
    models = [row for row in catalog.list_models() if row["backend"] == "claude"]
    policies = limits_db.model_policies(models)
    curated = []
    for item in claude_pool.CURATED:
        row = catalog.get_by_name(item["name"])
        curated.append({"item": item, "row": row,
                        "policy": policies.get(row["id"]) if row else None,
                        "strict": claude_pool.strict_policy(item["name"]),
                        "ignored": claude_pool.family_of(item["name"]) and
                        model_lifecycle.matches(item["name"], model_lifecycle.patterns())})
    resting = {row["id"]: row["cooldown_until"] for row in accounts if claude_pool._resting(row)}
    return {"accounts": accounts, "quota": quota, "models": models, "policies": policies, "resting": resting,
            "has_client": claude_pool._site_chat is not None,
            "curated": curated, "enrollment": model_lifecycle.policy().get("enrollment"),
            "strictness": claude_pool.STRICTNESS}


@bp.get("/models/claude", endpoint="claude_pool")
@admin_required
def pool_page():
    from bananachat.web.admin.models import _render
    return _render("admin/claude.html", tab="catalog", **_context())


@bp.post("/models/claude/accounts", endpoint="claude_account_add")
@admin_required
def account_add():
    try:
        label = text("label", max_length=80, required=True, label="Account name")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority", default=0)
        window = tokens("window_limit", label="5-hour tokens", optional=True)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True)
        account_id = pool_db.add_account(label, priority=priority, window_limit=window, weekly_limit=weekly)
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
            pool_db.remove_account(account_id)
            audit("claude_account_delete", row["label"], {"id": account_id})
            flash(f"Subscription account {row['label']} removed.", "success")
            return back("admin.claude_pool")
        status = choice("status", pool_db.STATUSES, label="Status")
        priority = integer("priority", minimum=-100, maximum=100, label="Priority")
        note = text("note", max_length=500, label="Note")
        window = tokens("window_limit", label="5-hour tokens", optional=True)
        weekly = tokens("weekly_limit", label="Weekly tokens", optional=True)
        pool_db.update_account(account_id, status=status, priority=priority, note=note)
        # Empty fields clear a limit (update_account leaves None alone), so they are written here.
        db.execute("UPDATE claude_accounts SET window_limit=?, weekly_limit=?, updated_at=? WHERE id=?",
                   (window, weekly, db.now(), account_id))
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
    # Empty selection with the field present means "offer none"; absent means all curated.
    wanted = selected if "models" in request.form else None
    try:
        state = claude_pool.sync_catalog(selected=wanted, source="admin")
    except ValueError as error:
        flash(str(error), "error")
        return back("admin.claude_pool")
    # New auto-enrolled models already carry strict policies; manual ones wait for review.
    # Apply strict defaults to any selected model that has no custom policy yet.
    applied = []
    for name in (wanted or [item["name"] for item in claude_pool.CURATED]):
        row = catalog.get_by_name(name)
        if row is not None and not limits_db.has_model_policy(row["id"]):
            try:
                limits_db.set_model_policy(row["id"], claude_pool.strict_policy(name), me()["id"])
                applied.append(name)
            except ValueError:
                pass
    audit("claude_sync", "catalog", {"selected": wanted, **state})
    parts = []
    if state.get("new"):
        parts.append(f"New: {', '.join(state['new'][:10])}.")
    if state.get("enabled"):
        parts.append(f"Enabled automatically with strict limits: {', '.join(state['enabled'][:10])}.")
    if applied:
        parts.append(f"Strict limits applied: {', '.join(applied[:10])}.")
    flash("Claude catalog refreshed. " + (" ".join(parts) if parts else "Nothing changed."), "success")
    return back("admin.claude_pool")


@bp.post("/models/claude/refresh", endpoint="claude_refresh")
@admin_required
def refresh():
    quota = claude_pool.refresh_quota()
    audit("claude_refresh", "pool", quota)
    if quota["window_left"] is None and quota["weekly_left"] is None:
        flash("No quota reported yet: set each account's 5-hour/weekly tokens or register a site reporter.", "info")
    else:
        flash("Claude pool quota re-read.", "success")
    return back("admin.claude_pool")
