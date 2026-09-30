"""Agents: enable the feature, limits, Git imports, models, the runner's status, every task and its full log.

Disabling the feature stops new tasks (running ones continue); the kill
switch asks every running task to stop. Every change is recorded in the audit
log (``admin.agents.*``).
"""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from bananachat.db import agents as agents_db
from bananachat.db import catalog
from bananachat.i18n import translate
from bananachat.security import admin_required
from bananachat.services.access import is_text_model
from bananachat.services.agents import gitfetch, service
from bananachat.services.agents import runner as runner_mod
from bananachat.services.agents import settings as agent_settings
from bananachat.web.agents import lane_view, render_text, step_view

from . import bp
from ._helpers import FormError, audit, back, flag, integer, me, page, text

LIMIT_LABELS = {
    "max_steps": ("Steps per run", "Model calls per run, all agents of a swarm together."),
    "max_minutes": ("Minutes per run", "Wall time per run; time paused for maintenance does not count."),
    "max_tokens": ("Tokens per run", "Prompt and answer tokens per run (also counted against the agent token limits)."),
    "max_tasks_per_user": ("Running tasks per user", "Administrators are limited only by the site limit."),
    "max_tasks_total": ("Running tasks on the site", "Also capped by the runner's sandbox limit."),
    "command_timeout": ("Command timeout (seconds)", "Longest single command; the runner applies its own cap too."),
    "keep_workspace_minutes": ("Keep workspace (minutes)", "After a run ends, keep the sandbox for downloads and "
                                                           "follow-ups (0 deletes it at once)."),
    "starts_per_hour": ("Starts per user per hour", "New tasks and follow-ups that start a run."),
    "retention_days": ("Keep tasks (days)", "Ended tasks and their logs are deleted after this many days."),
    "max_subagents": ("Sub-agents per swarm run", "Only used when swarms are enabled."),
    "max_concurrent_subagents": ("Sub-agents at the same time", "At most this many sub-agents work in parallel."),
}
STATUS_FILTERS = agents_db.STATES
GIT_LABELS = {
    "git_max_mb": ("Largest repository archive (MB)", "Downloads stop as soon as they exceed this; the runner accepts "
                                                        "at most 50 MB."),
    "git_imports_per_hour": ("Imports per user per hour", "Repository downloads each person may start."),
}


def image_status(health: dict) -> dict:
    """What is known about Git in the runner's default image (from the last import or check)."""
    state = agents_db.image_state() or {}
    images = health.get("images") if isinstance(health.get("images"), list) else []
    default = str(images[0]) if images else ""
    current = bool(state) and (not default or not state.get("image") or state.get("image") == default)
    return {"default": default, "known": current, "git": bool(state.get("git")) if current else None,
            "version": state.get("version") or "", "checked_at": state.get("checked_at") if current else None,
            "image": state.get("image") or ""}


def _runner_warnings(health: dict) -> list[str]:
    warnings = []
    if health.get("ok") and health.get("network") not in (None, "", "none"):
        warnings.append(f"Sandboxes have network access ({health.get('network')}). Agents can reach the internet "
                        "and your network; use 'none' unless you really need it.")
    if health.get("ok") and health.get("rootless") is False:
        warnings.append("The container engine runs as root: a container escape would give root on the compute "
                        "host. Prefer rootless Podman/Docker or the gVisor runtime.")
    return warnings


@bp.get("/agents", endpoint="agents")
@admin_required
def overview():
    settings = agent_settings.current(fresh=True)
    health = runner_mod.health(fresh=request.args.get("refresh") == "1")
    caps = agents_db.model_caps()
    models = []
    for model in catalog.list_models(backend="ollama"):
        info = caps.get(model["id"])
        models.append({"row": model, "caps": info, "text": is_text_model(model),
                       "override": settings.model_overrides.get(str(model["id"]), ""),
                       "usable": is_text_model(model) and agent_settings.supports_tools(model, caps,
                                                                                        settings.model_overrides)})
    status_filter = request.args.get("status") if request.args.get("status") in STATUS_FILTERS else None
    listing = page(agents_db.count_all(status=status_filter), 30)
    tasks = [{"row": row, "stale": row["status"] in agents_db.ACTIVE_STATES and agents_db.is_stale(row)}
             for row in agents_db.list_all(status=status_filter, limit=listing.size, offset=listing.offset)]
    image = image_status(health)
    warnings = _runner_warnings(health)
    if settings.git_enabled and image["git"] is False:
        warnings.append("The sandbox image has no Git: repositories are still imported, but people cannot "
                        "download their changes as a patch. Build and allow the agent image "
                        "(compute/sandbox-image, see docs/agents.md).")
    return render_template(
        "admin/agents.html", section="agents", settings=settings, health=health, models=models, tasks=tasks,
        image=image, git_labels=GIT_LABELS, git_hosts_text="\n".join(settings.git_hosts),
        listing=listing, status_filter=status_filter, statuses=STATUS_FILTERS, limit_labels=LIMIT_LABELS,
        limit_ranges=agent_settings.INTEGER_FIELDS, warnings=warnings,
        active=agents_db.active_count(), runner_configured=runner_mod.configured())


@bp.post("/agents/settings", endpoint="agents_settings")
@admin_required
def save_settings():
    current = agent_settings.current(fresh=True)
    values = current.to_dict()
    try:
        values["enabled"] = flag("enabled")
        values["swarms_enabled"] = flag("swarms_enabled")
        for name, (label, _) in LIMIT_LABELS.items():
            _, low, high = agent_settings.INTEGER_FIELDS[name]
            values[name] = integer(name, minimum=low, maximum=high, label=label)
    except FormError as error:
        flash(str(error), "error")
        return back("admin.agents")
    saved = agent_settings.save(values, me()["id"])
    changes = {key: value for key, value in saved.to_dict().items()
               if key != "model_overrides" and current.to_dict().get(key) != value}
    audit("agents.settings", "agents", changes)
    flash("Agent settings saved." + (" Agents are now enabled." if saved.enabled and not current.enabled else ""),
          "success")
    return back("admin.agents")


@bp.post("/agents/git", endpoint="agents_git")
@admin_required
def save_git_settings():
    current = agent_settings.current(fresh=True)
    values = current.to_dict()
    try:
        values["git_enabled"] = flag("git_enabled")
        for name, (label, _) in GIT_LABELS.items():
            _, low, high = agent_settings.INTEGER_FIELDS[name]
            values[name] = integer(name, minimum=low, maximum=high, label=label)
        entries = [line.strip() for line in text("git_hosts", max_length=4000, label="Allowed hosts").splitlines()
                   if line.strip()]
        if len(entries) > gitfetch.MAX_HOSTS:
            raise FormError(f"List at most {gitfetch.MAX_HOSTS} hosts.")
        for entry in entries:
            try:
                gitfetch.parse_host_entry(entry)
            except ValueError as error:
                raise FormError(f"Allowed hosts: {entry[:80]!r} is not valid ({error}).") from None
        values["git_hosts"] = entries
    except FormError as error:
        flash(str(error), "error")
        return redirect(url_for("admin.agents", _anchor="git"))
    saved = agent_settings.save(values, me()["id"])
    before = current.to_dict()
    changes = {key: value for key, value in saved.to_dict().items() if key.startswith("git_")
               and before.get(key) != value}
    audit("agents.git", "agents", changes)
    flash("Git import settings saved.", "success")
    return redirect(url_for("admin.agents", _anchor="git"))


@bp.post("/agents/image-check", endpoint="agents_image_check")
@admin_required
def check_image():
    try:
        state = service.probe_image()
    except service.AgentError as error:
        flash("The image could not be checked: " + translate("en", error.key, **error.params), "error")
        return redirect(url_for("admin.agents", _anchor="git"))
    audit("agents.image_check", "agents", {"image": state.get("image"), "git": state.get("git")})
    if state.get("git"):
        flash(f"The sandbox image has Git ({state.get('version') or 'git'}).", "success")
    else:
        flash("The sandbox image has no Git: imports work, patch export does not.", "warning")
    return redirect(url_for("admin.agents", _anchor="git"))


@bp.post("/agents/models", endpoint="agents_models")
@admin_required
def save_models():
    current = agent_settings.current(fresh=True)
    values = current.to_dict()
    overrides = {}
    for model in catalog.list_models(backend="ollama"):
        choice = request.form.get(f"override_{model['id']}", "")
        if choice in agent_settings.OVERRIDES:
            overrides[str(model["id"])] = choice
    values["model_overrides"] = overrides
    agent_settings.save(values, me()["id"])
    audit("agents.models", "agents", {"overrides": overrides})
    flash("Model choices saved.", "success")
    return redirect(url_for("admin.agents", _anchor="models"))


@bp.post("/agents/models/refresh", endpoint="agents_models_refresh")
@admin_required
def refresh_models():
    count = service.refresh_model_capabilities(force=True)
    audit("agents.models_refresh", "agents", {"checked": count})
    flash(f"Checked {count} model(s) for tool support.", "success")
    return redirect(url_for("admin.agents", _anchor="models"))


@bp.post("/agents/stop-all", endpoint="agents_stop_all")
@admin_required
def stop_all():
    count = service.stop_all()
    audit("agents.stop_all", "agents", {"tasks": count})
    flash(f"Asked {count} running task(s) to stop.", "success")
    return back("admin.agents")


def _task_or_404(task_id: str):
    task = agents_db.get(task_id)
    if task is None:
        abort(404)
    return task


@bp.get("/agents/tasks/<task_id>", endpoint="agents_task")
@admin_required
def task_detail(task_id):
    task = _task_or_404(task_id)
    after = max(0, request.args.get("after", 0, type=int) or 0)
    rows = agents_db.steps(task["id"], after=after, limit=500)
    steps = [step_view(row, "en") for row in rows]
    return render_template(
        "admin/agent_task.html", section="agents", task=task, steps=steps,
        lanes=[lane_view(row) for row in agents_db.lanes(task["id"])], active=service.is_active(task),
        stale=task["status"] in agents_db.ACTIVE_STATES and agents_db.is_stale(task),
        error=render_text(task["error"], "en"), notice=render_text(task["notice"], "en"),
        next_after=rows[-1]["id"] if len(rows) >= 500 else None, total_steps=agents_db.step_count(task["id"]),
        pending=agents_db.pending_messages(task["id"]))


@bp.post("/agents/tasks/<task_id>/stop", endpoint="agents_task_stop")
@admin_required
def stop_task(task_id):
    task = _task_or_404(task_id)
    service.stop(task, by_admin=True)
    audit("agents.stop", task["id"], {"owner": task["username"], "title": task["title"]})
    flash("Asked the task to stop.", "success")
    return back("admin.agents_task", task_id=task["id"])


@bp.post("/agents/tasks/<task_id>/delete", endpoint="agents_task_delete")
@admin_required
def delete_task(task_id):
    task = _task_or_404(task_id)
    service.delete_task(task)
    audit("agents.delete", task["id"], {"owner": task["username"], "title": task["title"]})
    flash("Task deleted.", "success")
    return back("admin.agents")
