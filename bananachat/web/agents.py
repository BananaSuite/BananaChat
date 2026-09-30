"""Agents: coding agents working in cloud sandboxes (``/agents``).

* ``/agents`` lists the person's tasks and starts new ones (prompt, model,
  optional swarm, optional files or an archive copied into the workspace,
  optionally a public Git repository the task starts from).
* ``/agents/<id>`` shows a task with a live timeline: the page polls
  ``/agents/<id>/events?after=<step>`` (cheap, reconnect-safe), shows tool
  calls with their output, sub-agent lanes, Stop, a follow-up box and the
  workspace browser (files, download of the workspace as an archive, and of
  the changes to an imported repository as a ``.patch``).

Only available while an administrator has enabled the feature and to people
holding the ``agents`` capability (administrators always). People who lost
access can still read and delete their tasks. Starting work obeys
``status.guard()``: maintenance and outages refuse new tasks and follow-ups.
"""

from __future__ import annotations

import json
import time

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   stream_with_context, url_for)

from bananachat import security
from bananachat.db import agents as agents_db
from bananachat.db import catalog
from bananachat.i18n import translate
from bananachat.services import status
from bananachat.services.access import AccessContext
from bananachat.services.agents import gitfetch, service
from bananachat.services.agents import runner as runner_mod
from bananachat.services.agents import settings as agent_settings

bp = Blueprint("agents", __name__, url_prefix="/agents")

EVENTS_LIMIT = 300


def _t(key, **params):
    return translate(g.lang, key, **params)


def render_text(value, lang: str) -> str:
    """Stored messages are ``{"key", "params"}`` JSON (translated here) or plain text."""
    if isinstance(value, str) and value.startswith("{"):
        try:
            data = json.loads(value)
        except ValueError:
            return value
        if isinstance(data, dict) and isinstance(data.get("key"), str) and data["key"].startswith("agents."):
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            return translate(lang, data["key"], **params)
    return value or ""


def _json_field(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def step_view(row, lang: str) -> dict:
    kind = row["kind"]
    content = render_text(row["content"], lang) if kind in ("notice", "error") else row["content"]
    return {
        "id": row["id"], "agent": row["agent"], "kind": kind, "content": content, "thinking": row["thinking"] or "",
        "tool_name": row["tool_name"], "tool_args": _json_field(row["tool_args"]),
        "tool_calls": _json_field(row["tool_calls"]), "tool_result": row["tool_result"],
        "tool_status": row["tool_status"], "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"],
        "duration_ms": row["duration_ms"], "created_at": row["created_at"],
    }


def lane_view(row) -> dict:
    return {"agent": row["agent"], "title": row["title"], "status": row["status"], "steps_used": row["steps_used"],
            "summary": row["summary"], "instructions": row["instructions"]}


def task_view(task, lang: str, *, settings=None) -> dict:
    settings = settings or agent_settings.current()
    active = service.is_active(task)
    stale = task["status"] in agents_db.ACTIVE_STATES and not active
    model = catalog.get(task["model_id"]) if task["model_id"] else None
    repository = service.repository(task)
    return {
        "id": task["id"], "title": task["title"] or translate(lang, "agents.untitled"),
        "status": "interrupted" if stale else task["status"],
        "active": active, "stopping": bool(active and task["stop_requested"]), "swarm": bool(task["swarm"]),
        "model": task["model_name"], "model_label": (model["display_name"] if model else task["model_name"]) or "",
        "steps_used": task["steps_used"], "tool_calls": task["tool_calls"],
        "tokens": (task["tokens_in"] or 0) + (task["tokens_out"] or 0), "runs": task["runs"],
        "limits": {"steps": settings.max_steps, "minutes": settings.max_minutes, "tokens": settings.max_tokens},
        "error": render_text(task["error"], lang), "notice": render_text(task["notice"], lang),
        "summary": task["summary"] or "", "workspace": bool(task["sandbox_id"]),
        "repository": {"label": repository["repo"], "dir": repository["dir"], "git": repository["git"]}
        if repository else None,
        "workspace_until": task["sandbox_expires_at"] if not active else None,
        "pending_messages": agents_db.pending_messages(task["id"]),
        "created_at": task["created_at"], "finished_at": task["finished_at"], "updated_at": task["updated_at"],
    }


def events_payload(task, *, after: int, lang: str) -> dict:
    rows = agents_db.steps(task["id"], after=after, limit=EVENTS_LIMIT)
    return {
        "task": task_view(task, lang),
        "steps": [step_view(row, lang) for row in rows],
        "lanes": [lane_view(row) for row in agents_db.lanes(task["id"])],
        "more": len(rows) >= EVENTS_LIMIT,
    }


# ----- access -----------------------------------------------------------------------------------

def _access_state(user) -> str:
    """``ok``, ``disabled`` (feature off) or ``denied`` (no capability)."""
    if not agent_settings.enabled():
        return "disabled"
    return "ok" if AccessContext.load(user).allows("agents") else "denied"


@bp.app_context_processor
def agents_context():
    user = getattr(g, "user", None)
    if user is None:
        return {"agents_nav_visible": False}
    cached = getattr(g, "_agents_nav", None)
    if cached is None:
        try:
            cached = g._agents_nav = _access_state(user) == "ok"
        except Exception:  # noqa: BLE001 - navigation must never break a page
            cached = False
    return {"agents_nav_visible": cached}


def _owned(task_id: str):
    task = agents_db.get(task_id)
    if task is None or task["user_id"] != security.current_user()["id"]:
        abort(404)
    return task


def _error_response(error: service.AgentError):
    message = error.message(g.lang)
    response = security.json_error(message, error.status, error.code)
    if error.retry_after:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


def _fail(error: service.AgentError, endpoint: str, **values):
    if security.wants_json():
        return _error_response(error)
    flash(error.message(g.lang), "error")
    return redirect(url_for(endpoint, **values))


# ----- pages --------------------------------------------------------------------------------

@bp.get("", endpoint="index")
@security.login_required
def index():
    user = security.current_user()
    access = _access_state(user)
    settings = agent_settings.current()
    tasks = [{"row": row, "status": "interrupted" if row["status"] in agents_db.ACTIVE_STATES
              and agents_db.is_stale(row) else row["status"]} for row in agents_db.list_for_user(user["id"])]
    models = []
    if access == "ok":
        models = [{"name": model["ollama_name"], "label": model["display_name"] or model["ollama_name"]}
                  for model in agent_settings.agent_models(AccessContext.load(user), settings)]
    config = current_app.config["BC"]
    return render_template(
        "agents/index.html", access=access, tasks=tasks, models=models, settings=settings,
        git_hosts=gitfetch.host_names(settings.git_hosts),
        runner_ready=runner_mod.configured(), max_prompt=agent_settings.MAX_PROMPT_CHARS,
        max_upload_mb=config.agents_max_upload_bytes // (1024 * 1024),
        blocked=status.inference_block(user) is not None)


@bp.post("", endpoint="create")
@security.login_required
@security.body_limit(lambda config: config.agents_max_upload_bytes + 1024 * 1024)
def create():
    user = security.current_user()
    blocked = status.guard()
    if blocked:
        if security.wants_json():
            return blocked
        flash(_t("agents.error_paused"), "error")
        return redirect(url_for("agents.index"))
    try:
        started = service.start_task(user, prompt=request.form.get("prompt", ""),
                                     model_name=request.form.get("model"), swarm=request.form.get("swarm") == "1",
                                     files=request.files.getlist("files"), repo_url=request.form.get("repo_url"),
                                     repo_ref=request.form.get("repo_ref"))
    except service.AgentError as error:
        return _fail(error, "agents.index")
    target = url_for("agents.task", task_id=started.task_id)
    if security.wants_json():
        return jsonify({"id": started.task_id, "url": target}), 201
    return redirect(target)


@bp.get("/<task_id>", endpoint="task")
@security.login_required
def task(task_id):
    user = security.current_user()
    row = _owned(task_id)
    lang = g.lang
    rows = agents_db.steps(row["id"], limit=1000)
    access = _access_state(user)
    data = {
        "task": task_view(row, lang),
        "steps": [step_view(step, lang) for step in rows],
        "lanes": [lane_view(lane) for lane in agents_db.lanes(row["id"])],
        "more": len(rows) >= 1000,
        "can_send": access == "ok",
        "urls": {
            "index": url_for("agents.index"),
            "events": url_for("agents.events", task_id=row["id"]),
            "stop": url_for("agents.stop", task_id=row["id"]),
            "message": url_for("agents.message", task_id=row["id"]),
            "workspace": url_for("agents.workspace", task_id=row["id"]),
            "file": url_for("agents.workspace_file", task_id=row["id"]),
            "archive": url_for("agents.workspace_archive", task_id=row["id"]),
            "patch": url_for("agents.workspace_patch", task_id=row["id"]),
            "upload": url_for("agents.workspace_upload", task_id=row["id"]),
        },
    }
    return render_template("agents/task.html", task=data["task"], data=data, access=access,
                           max_message=agent_settings.MAX_FOLLOW_UP_CHARS)


@bp.get("/<task_id>/events", endpoint="events")
@security.login_required
def events(task_id):
    row = _owned(task_id)
    after = request.args.get("after", 0, type=int) or 0
    response = jsonify(events_payload(row, after=max(0, after), lang=g.lang))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/<task_id>/stop", endpoint="stop")
@security.login_required
def stop(task_id):
    row = _owned(task_id)
    service.stop(row)
    if security.wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("agents.task", task_id=row["id"]))


@bp.post("/<task_id>/messages", endpoint="message")
@security.login_required
def message(task_id):
    user = security.current_user()
    row = _owned(task_id)
    blocked = status.guard()
    if blocked:
        return blocked
    payload = request.get_json(silent=True) if request.is_json else None
    content = payload.get("content") if isinstance(payload, dict) else request.form.get("content", "")
    try:
        started = service.follow_up(user, row, content)
    except service.AgentError as error:
        return _fail(error, "agents.task", task_id=row["id"])
    if security.wants_json():
        return jsonify({"ok": True, "queued": started.queued_message}), 202
    return redirect(url_for("agents.task", task_id=row["id"]))


@bp.post("/<task_id>/delete", endpoint="delete")
@security.login_required
def delete(task_id):
    row = _owned(task_id)
    service.delete_task(row)
    if security.wants_json():
        return jsonify({"ok": True, "url": url_for("agents.index")})
    flash(_t("agents.deleted"), "success")
    return redirect(url_for("agents.index"))


# ----- workspace ------------------------------------------------------------------------------

@bp.get("/<task_id>/workspace", endpoint="workspace")
@security.login_required
def workspace(task_id):
    row = _owned(task_id)
    try:
        data = service.browse(row, request.args.get("path") or "/workspace")
    except service.AgentError as error:
        return _error_response(error)
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-store"
    return response


def _attachment_headers(response: Response, filename: str) -> Response:
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/<task_id>/workspace/file", endpoint="workspace_file")
@security.login_required
def workspace_file(task_id):
    row = _owned(task_id)
    try:
        name, content = service.file_bytes(row, request.args.get("path") or "")
    except service.AgentError as error:
        return _error_response(error)
    return _attachment_headers(Response(content, mimetype="application/octet-stream"), name)


@bp.get("/<task_id>/workspace/archive", endpoint="workspace_archive")
@security.login_required
@security.long_request
def workspace_archive(task_id):
    """One download per person at a time (every process): the archive is fetched from the runner into a
    temporary file first, so a slow browser never holds one of the runner's two archive slots."""
    row = _owned(task_id)
    user_id = security.current_user()["id"]
    token = agents_db.claim_download(user_id)
    if token is None:
        return _error_response(service.AgentError("agents.error_download_busy", 429, "busy", retry_after=30))
    app = current_app._get_current_object()

    def release():
        with app.app_context():
            agents_db.release_download(user_id, token)

    try:
        spool = service.fetch_archive(row, renew=lambda: agents_db.renew_download(user_id, token))
    except service.AgentError as error:
        release()
        return _error_response(error)
    except BaseException:
        release()
        raise
    size = spool.seek(0, 2)
    spool.seek(0)

    def generate():
        renewed = time.monotonic()
        try:
            while True:
                chunk = spool.read(64 * 1024)
                if not chunk:
                    break
                if time.monotonic() - renewed > service.LEASE_RENEW_SECONDS:
                    agents_db.renew_download(user_id, token)
                    renewed = time.monotonic()
                yield chunk
        finally:
            spool.close()

    response = Response(stream_with_context(generate()), mimetype="application/gzip")
    response.headers["Content-Length"] = str(size)
    response.call_on_close(spool.close)
    response.call_on_close(release)
    return _attachment_headers(response, f"agent-{row['id'][:8]}-workspace.tar.gz")


@bp.get("/<task_id>/workspace/patch", endpoint="workspace_patch")
@security.login_required
@security.long_request
def workspace_patch(task_id):
    """The changes to the imported repository as a Git patch (Git runs in the sandbox, never here).

    Counts as the person's one download at a time. The patch is untrusted text written by the agent:
    it is served as an attachment with ``nosniff`` and a sandboxing CSP.
    """
    user = security.current_user()
    row = _owned(task_id)
    try:
        service.check_access(user)
    except service.AgentError as error:
        return _error_response(error)
    token = agents_db.claim_download(user["id"])
    if token is None:
        return _error_response(service.AgentError("agents.error_download_busy", 429, "busy", retry_after=30))
    try:
        name, patch = service.export_patch(row)
    except service.AgentError as error:
        return _error_response(error)
    finally:
        agents_db.release_download(user["id"], token)
    response = Response(patch, content_type="text/x-diff")
    response.headers["Content-Length"] = str(len(patch))
    return _attachment_headers(response, name)


@bp.post("/<task_id>/workspace/upload", endpoint="workspace_upload")
@security.login_required
@security.body_limit(lambda config: config.agents_max_upload_bytes + 1024 * 1024)
def workspace_upload(task_id):
    user = security.current_user()
    row = _owned(task_id)
    try:
        service.check_access(user)
        count = service.upload_to_workspace(row, request.files.getlist("files"))
    except service.AgentError as error:
        return _error_response(error)
    return jsonify({"ok": True, "count": count})
