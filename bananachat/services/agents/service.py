"""Agent tasks: starting, follow-ups, stopping, deleting, the workspace, and the periodic jobs.

Starting a task (``start_task``) checks, cheapest first: the feature is on and
the person holds the ``agents`` capability, the prompt, the per-user start
rate (``starts_per_hour``, administrators exempt), the ``agent`` token limits,
the model (tool-capable and usable by the person), that the sandbox runner is
configured, the uploads, and finally - atomically with creating the task -
the per-user and site-wide limits on active tasks. A task may start from a
public Git repository (checked here, downloaded and imported by the run: see
``gitfetch`` and ``gitrepo``); its changes can then be exported as a patch. Callers check
``status.guard()`` first (maintenance and outages refuse new tasks).

Periodic jobs (one elected process): recover tasks of processes that died,
delete sandboxes whose keep-time ended, delete old tasks, and detect which
models support tool calling.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from flask import current_app

from bananachat import db
from bananachat.db import agents as agents_db
from bananachat.db import catalog, users
from bananachat.i18n import translate
from bananachat.services import background, limits, ollama, status
from bananachat.services.access import AccessContext
from bananachat.services.agents import gitfetch, gitrepo, loop
from bananachat.services.agents import runner as runner_mod
from bananachat.services.agents import settings as agent_settings
from bananachat.services.agents import uploads
from bananachat.services.upstream import UpstreamError

log = logging.getLogger("bananachat.agents")

TITLE_LENGTH = 80
UNKNOWN_SANDBOX_GRACE = timedelta(minutes=10)
MAX_PATCH_BYTES = 20 * 1024 * 1024


class AgentError(Exception):
    """A request refused before anything ran. ``key`` is an i18n key (or *refusal*, a ``limits.Refusal``)."""

    def __init__(self, key: str, status: int = 400, code: str = "bad_request", retry_after: int | None = None,
                 refusal=None, **params):
        super().__init__(key)
        self.key = key
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.refusal = refusal
        self.params = params

    def message(self, lang: str) -> str:
        """The explanation in *lang* (a limit refusal words its rates in that language too)."""
        if self.refusal is not None:
            return self.refusal.message(lang)
        return translate(lang, self.key, **self.params)


def auto_title(text: str) -> str:
    words = " ".join((text or "").split()).lstrip("#>*-_`~ ").replace("`", "")
    if len(words) <= TITLE_LENGTH:
        return words
    cut = words[:TITLE_LENGTH]
    if " " in cut[TITLE_LENGTH // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"


def is_active(task) -> bool:
    return task is not None and task["status"] in agents_db.ACTIVE_STATES and bool(task["owner_token"]) \
        and not agents_db.is_stale(task)


def site_limit(settings: agent_settings.Settings) -> int:
    return min(settings.max_tasks_total, runner_mod.site_capacity(fallback=settings.max_tasks_total))


# ----- access -----------------------------------------------------------------------------

def check_access(user) -> None:
    """Raise :class:`AgentError` unless *user* may use agents now."""
    if user is None:
        raise AgentError("agents.error_signed_out", 401, "auth_required")
    if not agent_settings.enabled():
        raise AgentError("agents.error_disabled", 403, "disabled")
    if not AccessContext.load(user).allows("agents"):
        raise AgentError("agents.error_no_access", 403, "forbidden")


def _check_start(user, settings, model) -> None:
    """Rates, tokens and the runner: shared by new tasks and follow-ups that start a run.

    Two rate limits apply: the ``agent`` pool's request rate (Limits) and the
    feature's own ``starts_per_hour``; administrators are exempt from both.
    Then the ``agent`` pool's tokens and the model's own limits
    (``limits.admit``, which also takes one request from the model's rate).
    """
    decision = limits.check_rate(user, "agent")
    if not decision.allowed:
        raise AgentError("agents.error_rate", 429, "rate_limited", retry_after=decision.retry_after)
    if user["role"] != "admin" and not users.hit(f"agents:start:{user['id']}", settings.starts_per_hour, 3600):
        raise AgentError("agents.error_rate", 429, "rate_limited", retry_after=600)
    admission = limits.admit(user, "agent", model)
    if not admission.allowed:
        refusal = admission.refusal
        if refusal.key in ("window", "window_none", "weekly"):
            raise AgentError("agents.error_quota", 429, "quota_exhausted", retry_after=refusal.retry_after or 3600)
        raise AgentError(f"account.refusal_{refusal.key}", refusal.status, refusal.code,
                         retry_after=refusal.retry_after, refusal=refusal)
    if not runner_mod.configured():
        raise AgentError("agents.error_runner", 503, "unavailable", retry_after=60)


def choose_model(user, requested: str | None, settings):
    context = AccessContext.load(user)
    requested = (requested or "auto").strip()
    if requested and requested != "auto":
        model = catalog.get_by_name(requested)
        if model is None or not agent_settings.model_allowed(model, context, settings):
            raise AgentError("agents.error_model", 400, "model_unavailable")
        return model
    models = agent_settings.agent_models(context, settings)
    if not models:
        raise AgentError("agents.error_no_model", 503, "model_unavailable")
    return models[0]


# ----- starting and resuming -------------------------------------------------------------------

@dataclass
class Started:
    task_id: str
    run: loop.TaskRun | None = None
    queued_message: bool = False
    extra: dict = field(default_factory=dict)


def _launch(task_id: str, token: str, user, model, swarm: bool, settings) -> loop.TaskRun:
    run = loop.TaskRun(current_app._get_current_object(), task_id, token, user=user, model=model, swarm=swarm,
                       settings=settings, on_end=_after_run)
    try:
        run.start()
    except BaseException:
        agents_db.finish(task_id, token, "failed", error=loop.message("agents.stop_internal"))
        raise
    return run


def import_rate_key(user) -> str:
    return f"agents:import:{user['id']}"


def parse_repository(user, url, ref, settings) -> gitfetch.Source | None:
    """The repository a new task starts from (None without one). Raises :class:`AgentError`.

    Checks that imports are enabled, the address and ref (strictly: see ``gitfetch.parse``) and the
    per-user import rate (only peeked here; recorded once the task exists).
    """
    url = url.strip() if isinstance(url, str) else ""
    ref = ref.strip() if isinstance(ref, str) else ""
    if not url:
        if ref:
            raise AgentError("agents.error_ref_without_url")
        return None
    if not settings.git_enabled:
        raise AgentError("agents.error_import_disabled", 403, "import_disabled")
    try:
        source = gitfetch.parse(url, ref, settings.git_hosts)
    except gitfetch.SourceError as error:
        raise AgentError(error.key, 400, "bad_repository", **error.params) from None
    if not users.hit(import_rate_key(user), settings.git_imports_per_hour, 3600, record=False):
        raise AgentError("agents.error_import_rate", 429, "rate_limited", retry_after=600)
    return source


def start_task(user, *, prompt: str, model_name: str | None, swarm: bool, files=(), repo_url=None,
               repo_ref=None) -> Started:
    check_access(user)
    settings = agent_settings.current()
    prompt = (prompt or "").strip() if isinstance(prompt, str) else ""
    if not prompt:
        raise AgentError("agents.error_empty")
    if len(prompt) > agent_settings.MAX_PROMPT_CHARS:
        raise AgentError("agents.error_too_long", 413, "too_large", max=agent_settings.MAX_PROMPT_CHARS)
    if swarm and not settings.swarms_enabled:
        raise AgentError("agents.error_swarm_disabled", 400, "swarm_disabled")
    source = parse_repository(user, repo_url, repo_ref, settings)
    model = choose_model(user, model_name, settings)
    _check_start(user, settings, model)
    try:
        items = uploads.read_uploads(files or (), current_app.config["BC"].agents_max_upload_bytes)
    except uploads.UploadError as error:
        raise AgentError(error.key, 413 if error.key == "agents.upload_too_large" else 400, "bad_upload",
                         **error.params) from None
    task_id, token = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        agents_db.create(task_id, user_id=user["id"], title=auto_title(prompt), prompt=prompt, model_id=model["id"],
                         model_name=model["ollama_name"], swarm=swarm, owner_token=token,
                         max_user=None if user["role"] == "admin" else settings.max_tasks_per_user,
                         max_site=site_limit(settings))
    except agents_db.Busy as busy:
        if busy.scope == "user":
            raise AgentError("agents.error_busy_user", 409, "busy", retry_after=30,
                             max=settings.max_tasks_per_user) from None
        raise AgentError("agents.error_busy_site", 429, "busy", retry_after=60) from None
    try:
        uploads.store(task_id, items)
        if source is not None:
            gitrepo.store_request(task_id, source)
            users.hit(import_rate_key(user), settings.git_imports_per_hour + 1, 3600)
    except OSError:
        log.exception("Could not store the uploads of agent task %s", task_id)
        agents_db.finish(task_id, token, "failed", error=loop.message("agents.notice_upload_failed", error="storage"))
        raise AgentError("agents.error_upload_storage", 500, "server_error") from None
    run = _launch(task_id, token, user, model, swarm, settings)
    return Started(task_id, run)


def follow_up(user, task, content: str) -> Started:
    """Send a message to a task: queued for a running task, or starts a new run."""
    check_access(user)
    settings = agent_settings.current()
    content = (content or "").strip() if isinstance(content, str) else ""
    if not content:
        raise AgentError("agents.error_empty")
    if len(content) > agent_settings.MAX_FOLLOW_UP_CHARS:
        raise AgentError("agents.error_too_long", 413, "too_large", max=agent_settings.MAX_FOLLOW_UP_CHARS)
    if agents_db.pending_messages(task["id"]) >= agent_settings.MAX_PENDING_FOLLOW_UPS:
        raise AgentError("agents.error_too_many_messages", 429, "rate_limited", retry_after=30)
    if is_active(task):
        agents_db.add_message(task["id"], user["id"], content)
        return Started(task["id"], queued_message=True)
    model = catalog.get(task["model_id"]) if task["model_id"] else None
    if model is None or not agent_settings.model_allowed(model, AccessContext.load(user), settings):
        model = choose_model(user, None, settings)
    _check_start(user, settings, model)
    if task["swarm"] and not settings.swarms_enabled:
        raise AgentError("agents.error_swarm_disabled", 400, "swarm_disabled")
    token = uuid.uuid4().hex
    try:
        started = agents_db.resume(task["id"], owner_token=token,
                                   max_user=None if user["role"] == "admin" else settings.max_tasks_per_user,
                                   max_site=site_limit(settings), model_id=model["id"],
                                   model_name=model["ollama_name"])
    except agents_db.Busy as busy:
        if busy.scope == "user":
            raise AgentError("agents.error_busy_user", 409, "busy", retry_after=30,
                             max=settings.max_tasks_per_user) from None
        raise AgentError("agents.error_busy_site", 429, "busy", retry_after=60) from None
    agents_db.add_message(task["id"], user["id"], content)
    if not started:  # a run began meanwhile: it will read the message
        return Started(task["id"], queued_message=True)
    run = _launch(task["id"], token, user, model, bool(task["swarm"]), settings)
    return Started(task["id"], run)


def _after_run(run: loop.TaskRun) -> None:
    """A run ended: if its owner sent follow-ups meanwhile and it finished normally, continue with them."""
    with run.app.app_context():
        if run.outcome != "finished" or not agents_db.pending_messages(run.task_id):
            return
        task = agents_db.get(run.task_id)
        user = users.get(task["user_id"]) if task else None
        if task is None or user is None or users.is_suspended(user):
            return
        try:
            check_access(user)
            settings = agent_settings.current()
            if status.inference_block(user) is not None:
                return
            model = catalog.get(task["model_id"]) if task["model_id"] else None
            if model is None or not agent_settings.model_allowed(model, AccessContext.load(user), settings):
                return
            _check_start(user, settings, model)  # a new run like any other: start rate, agent rate and tokens
            token = uuid.uuid4().hex
            if agents_db.resume(task["id"], owner_token=token,
                                max_user=None if user["role"] == "admin" else settings.max_tasks_per_user,
                                max_site=site_limit(settings)):
                _launch(task["id"], token, user, model, bool(task["swarm"]), settings)
        except (AgentError, agents_db.Busy):
            return


# ----- stopping and deleting ---------------------------------------------------------------------

def stop(task, *, by_admin: bool = False) -> bool:
    """Ask the task's run to stop (any process; immediate in this one)."""
    requested = agents_db.request_stop(task["id"], by_admin=by_admin)
    run = loop.local_run(task["id"])
    if run is not None:
        run.stop("stopped by admin" if by_admin else "stopped")
    elif agents_db.is_stale(task):
        _recover(task_id=task["id"])
    return requested or run is not None


def stop_all() -> int:
    """The administrator's kill switch."""
    ids = agents_db.request_stop_all()
    for run in loop.local_runs():
        run.stop("stopped by admin")
    return len(ids)


def delete_task(task) -> None:
    run = loop.local_run(task["id"])
    if run is not None:
        run.stop("stopped")
    agents_db.request_stop(task["id"])
    _delete_sandbox(task["sandbox_id"])
    uploads.discard(task["id"])
    agents_db.delete(task["id"])


def _delete_sandbox(sandbox_id: str | None) -> bool:
    if not sandbox_id or not runner_mod.configured():
        return not sandbox_id
    try:
        runner_mod.client().delete(sandbox_id)
        return True
    except runner_mod.RunnerError as error:
        log.warning("Could not delete sandbox %s: %s", sandbox_id, error)
        return False


# ----- workspace (file browser, downloads, uploads into a live sandbox) ------------------------------

def workspace_client(task):
    """``(runner, sandbox_id)`` for a task whose sandbox exists. Raises :class:`AgentError`."""
    if not task["sandbox_id"]:
        raise AgentError("agents.error_no_workspace", 404, "no_workspace")
    try:
        return runner_mod.client(), task["sandbox_id"]
    except runner_mod.RunnerUnavailable:
        raise AgentError("agents.error_runner", 503, "unavailable") from None


def _runner_problem(error: runner_mod.RunnerError) -> AgentError:
    if error.gone:
        return AgentError("agents.error_no_workspace", 404, "no_workspace")
    if error.status == 404:
        return AgentError("agents.error_path", 404, "not_found")
    if error.status == 409:
        return AgentError("agents.error_workspace_busy", 409, "busy", retry_after=5)
    if error.status == 413:
        return AgentError("agents.error_file_too_large", 413, "too_large")
    if error.status in (400, 403, 422):
        return AgentError("agents.error_path", 400, "bad_path")
    return AgentError("agents.error_runner", 503, "unavailable", retry_after=30)


def browse(task, path: str) -> dict:
    from bananachat.services.agents import tools
    try:
        path = tools.resolve_path(path or tools.WORKSPACE)
    except tools.ToolError:
        raise AgentError("agents.error_path", 400, "bad_path") from None
    client, sandbox_id = workspace_client(task)
    try:
        data = client.read(sandbox_id, path)
    except runner_mod.RunnerError as error:
        raise _runner_problem(error) from None
    if isinstance(data.get("entries"), list):
        entries = []
        for entry in data["entries"][:1000]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name or "/" in name or name in (".", "..") or len(name) > 255:
                continue
            kind = "dir" if entry.get("type") == "dir" else "file"
            size = entry.get("size") if isinstance(entry.get("size"), int) else None
            entries.append({"name": name, "type": kind, "size": size})
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        return {"path": path, "type": "dir", "entries": entries}
    content = runner_mod.Runner.file_bytes(data)
    size = data.get("size") if isinstance(data.get("size"), int) else (len(content) if content is not None else None)
    result = {"path": path, "type": "file", "size": size, "text": None}
    if content is not None and len(content) <= 256 * 1024 and b"\x00" not in content[:8192]:
        try:
            result["text"] = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return result


def file_bytes(task, path: str) -> tuple[str, bytes]:
    from bananachat.services.agents import tools
    try:
        path = tools.resolve_path(path)
    except tools.ToolError:
        raise AgentError("agents.error_path", 400, "bad_path") from None
    client, sandbox_id = workspace_client(task)
    try:
        data = client.read(sandbox_id, path)
    except runner_mod.RunnerError as error:
        raise _runner_problem(error) from None
    if "entries" in data:
        raise AgentError("agents.error_path", 400, "bad_path")
    if data.get("truncated"):
        raise AgentError("agents.error_file_download_large", 413, "too_large")
    content = runner_mod.Runner.file_bytes(data)
    if content is None:
        raise AgentError("agents.error_path", 400, "bad_path")
    return uploads.safe_name(path.rsplit("/", 1)[-1]), content


def open_archive(task):
    client, sandbox_id = workspace_client(task)
    try:
        return client.open_archive(sandbox_id)
    except runner_mod.RunnerError as error:
        raise _runner_problem(error) from None


ARCHIVE_SPOOL_MEMORY = 1024 * 1024
LEASE_RENEW_SECONDS = 30


def fetch_archive(task, *, renew=None):
    """The workspace archive, read from the runner completely into a temporary file (rewound).

    The runner's archive slot is then held only as long as the transfer between the two servers takes,
    never as long as a slow browser needs. *renew* is called now and then (the download lease).
    """
    upstream = open_archive(task)
    spool = tempfile.SpooledTemporaryFile(max_size=ARCHIVE_SPOOL_MEMORY)
    try:
        renewed = time.monotonic()
        while True:
            chunk = upstream.read(256 * 1024)
            if not chunk:
                break
            spool.write(chunk)
            if renew is not None and time.monotonic() - renewed > LEASE_RENEW_SECONDS:
                renew()
                renewed = time.monotonic()
    except UpstreamError:
        spool.close()
        raise AgentError("agents.error_runner", 503, "unavailable", retry_after=30) from None
    except BaseException:
        spool.close()
        raise
    finally:
        upstream.close()
    spool.seek(0)
    return spool


def upload_to_workspace(task, files) -> int:
    client, sandbox_id = workspace_client(task)
    try:
        items = uploads.read_uploads(files or (), current_app.config["BC"].agents_max_upload_bytes)
    except uploads.UploadError as error:
        raise AgentError(error.key, 413 if error.key == "agents.upload_too_large" else 400, "bad_upload",
                         **error.params) from None
    if not items:
        raise AgentError("agents.error_no_files")
    try:
        for item in items:
            if item["kind"] in ("zip", "tar"):
                client.put_archive(sandbox_id, item["data"], kind=item["kind"])
            else:
                client.write(sandbox_id, f"/workspace/{item['name']}", item["data"])
    except runner_mod.RunnerError as error:
        raise _runner_problem(error) from None
    return len(items)


# ----- Git: the imported repository, patches, the image ----------------------------------------------

def repository(task) -> dict | None:
    """The repository the task started from (validated ``gitrepo.parse_record``), or None."""
    return gitrepo.parse_record(agents_db.import_record(task["id"]))


_PATCH_ERRORS = {"norepo": "agents.error_patch_norepo", "nobase": "agents.error_patch_nobase",
                 "nogit": "agents.error_no_git"}


def export_patch(task) -> tuple[str, bytes]:
    """The changes since the import as a Git patch (``git diff --binary``, untracked files included).

    Git runs inside the sandbox (``gitrepo.export_command``) without any configuration the agent could
    have written, and without touching the agent's repository; the patch comes back in 1 MB parts
    through the runner's file API and is checked against the size and SHA-256 the script reported.
    """
    record = repository(task)
    if record is None:
        raise AgentError("agents.error_no_repository", 404, "no_repository")
    if not record["git"]:
        raise AgentError("agents.error_no_git", 409, "no_git")
    client, sandbox_id = workspace_client(task)
    out = gitrepo.EXPORT_PREFIX + secrets.token_hex(8)
    try:
        result = client.exec(sandbox_id, gitrepo.export_command(record["dir"], record["commit"], out,
                                                                MAX_PATCH_BYTES), timeout=gitrepo.EXPORT_TIMEOUT)
    except runner_mod.RunnerError as error:
        raise _runner_problem(error) from None
    try:
        outcome = gitrepo.parse_export(result)
        if not outcome.ok:
            if outcome.error == "toolarge":
                raise AgentError("agents.error_patch_too_large", 413, "too_large",
                                 size=MAX_PATCH_BYTES // (1024 * 1024))
            raise AgentError(_PATCH_ERRORS.get(outcome.error, "agents.error_patch_failed"), 409, "export_failed")
        if outcome.size == 0:
            raise AgentError("agents.error_no_changes", 409, "no_changes")
        if outcome.size > MAX_PATCH_BYTES:
            raise AgentError("agents.error_patch_too_large", 413, "too_large", size=MAX_PATCH_BYTES // (1024 * 1024))
        parts = []
        for index in range(outcome.parts):
            try:
                data = client.read(sandbox_id, gitrepo.part_path(out, index))
            except runner_mod.RunnerError as error:
                if error.gone:
                    raise AgentError("agents.error_no_workspace", 404, "no_workspace") from None
                raise AgentError("agents.error_patch_failed", 502, "export_failed") from None
            chunk = runner_mod.Runner.file_bytes(data) if data.get("type") == "file" else None
            if chunk is None or data.get("truncated") or len(chunk) > gitrepo.PART_BYTES:
                raise AgentError("agents.error_patch_failed", 502, "export_failed")
            parts.append(chunk)
        patch = b"".join(parts)
        if len(patch) != outcome.size or hashlib.sha256(patch).hexdigest() != outcome.sha256:
            raise AgentError("agents.error_patch_failed", 502, "export_failed")
    finally:
        try:
            client.exec(sandbox_id, gitrepo.cleanup_command(out), timeout=30)
        except runner_mod.RunnerError as error:
            log.info("Could not remove the patch scratch folder of task %s: %s", task["id"], error)
    return uploads.safe_name(f"{record['name']}-changes.patch"), patch


_GIT_VERSION_RE = re.compile(r"git version [0-9][0-9A-Za-z.+~-]{0,40}")


def probe_image() -> dict:
    """Whether the runner's default image has Git: a short-lived sandbox runs ``git --version``.

    Stores and returns ``{"image", "git", "version", "checked_at"}``. Raises :class:`AgentError`.
    """
    try:
        client = runner_mod.client()
    except runner_mod.RunnerUnavailable:
        raise AgentError("agents.error_runner", 503, "unavailable") from None
    try:
        box = client.create(f"probe-{secrets.token_hex(8)}")
    except runner_mod.RunnerError as error:
        raise AgentError("agents.error_busy_site" if error.capacity else "agents.error_runner", 503,
                         "unavailable") from None
    try:
        result = client.exec(box["id"], "git --version 2>/dev/null || echo BCPROBE nogit", timeout=30)
    except runner_mod.RunnerError:
        raise AgentError("agents.error_runner", 503, "unavailable") from None
    finally:
        _delete_sandbox(box["id"])
    stdout = result.get("stdout") if isinstance(result.get("stdout"), str) else ""
    match = _GIT_VERSION_RE.search(stdout)
    agents_db.set_image_state(image=str(box.get("image") or ""), git=match is not None,
                              version=match.group(0) if match else "")
    return agents_db.image_state() or {}


# ----- periodic jobs ---------------------------------------------------------------------------------

def _recover(task_id: str | None = None) -> list:
    settings = agent_settings.current()
    keep_until = db.now(timedelta(minutes=settings.keep_workspace_minutes))
    ids = agents_db.recover_stale(keep_until=keep_until, error=loop.message("agents.interrupted"),
                                  task_id=task_id)
    _stop_leftovers(ids)
    return ids


def _stop_leftovers(task_ids) -> None:
    """The process running these tasks died: kill whatever their agents left running in the kept sandboxes.

    When that fails the sandbox is marked for removal now (the cleanup job deletes it).
    """
    if not task_ids or not runner_mod.configured():
        return
    try:
        client = runner_mod.client()
    except runner_mod.RunnerUnavailable:
        return
    for task_id in task_ids:
        row = agents_db.get(task_id)
        if row is None or not row["sandbox_id"]:
            continue
        try:
            if not client.interrupt(row["sandbox_id"]).get("sandbox_removed"):
                continue
        except runner_mod.RunnerError as error:
            log.warning("Could not stop leftover processes of agent task %s: %s", task_id, error)
        agents_db.set_sandbox(task_id, None, row["sandbox_id"], db.now())


@background.job("agents-recover", every=30, initial_delay=15)
def recover_tasks(app) -> None:
    """Tasks whose process died (lease expired) end as ``interrupted``; their sandbox is kept briefly."""
    ids = _recover()
    if ids:
        log.warning("Recovered %d interrupted agent task(s)", len(ids))


def _parse_time(value) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return db.parse_timestamp(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return None


def reap_sandboxes() -> int:
    """Delete sandboxes whose keep-time ended, and runner sandboxes of our tasks that we no longer track."""
    if not runner_mod.configured():
        return 0
    removed = 0
    for row in agents_db.expired_sandboxes(db.now()):
        if _delete_sandbox(row["sandbox_id"]):
            agents_db.clear_sandbox(row["id"], row["sandbox_id"])
            removed += 1
    try:
        listed = runner_mod.client().list()
    except runner_mod.RunnerError:
        return removed
    known = agents_db.known_sandboxes()
    cutoff = datetime.now(timezone.utc) - UNKNOWN_SANDBOX_GRACE
    for item in listed:
        sandbox_id, session = str(item.get("id") or ""), str(item.get("session") or "")
        if not sandbox_id or sandbox_id in known or not session:
            continue
        task = agents_db.get(session)
        if task is None:
            continue  # not one of ours (the runner's own reaper handles it)
        created = _parse_time(item.get("created_at"))
        if created is None or created > cutoff:
            continue
        if _delete_sandbox(sandbox_id):
            removed += 1
    return removed


@background.job("agents-reap-sandboxes", every=60, initial_delay=40)
def reap_job(app) -> None:
    count = reap_sandboxes()
    if count:
        log.info("Deleted %d agent sandbox(es)", count)


def purge_old_tasks() -> int:
    settings = agent_settings.current()
    cutoff = db.now(-timedelta(days=settings.retention_days))
    total = 0
    for _ in range(10):
        rows = agents_db.old_tasks(cutoff)
        for row in rows:
            _delete_sandbox(row["sandbox_id"])
            uploads.discard(row["id"])
            agents_db.delete(row["id"])
        total += len(rows)
        if len(rows) < 200:
            break
    return total


@background.job("agents-retention", every=3600, initial_delay=150)
def retention_job(app) -> None:
    count = purge_old_tasks()
    if count:
        log.info("Deleted %d old agent task(s)", count)


def refresh_model_capabilities(*, force: bool = False, limit: int = 50) -> int:
    """Ask Ollama (``/api/show``) which models support tool calling. Returns the number checked."""
    caps = agents_db.model_caps()
    stale = db.now(-timedelta(hours=24))
    checked = 0
    for model in catalog.list_models(backend="ollama"):
        if checked >= limit:
            break
        if not model["backend_available"]:
            continue
        known = caps.get(model["id"])
        if known is not None and not force and known["checked_at"] >= stale:
            continue
        try:
            values = ollama.capabilities(model["backend_model_name"] or model["ollama_name"])
        except (UpstreamError, OSError) as error:
            log.info("Could not read the capabilities of %s: %s", model["ollama_name"], error)
            continue
        agents_db.set_model_caps(model["id"], "tools" in values, values)
        checked += 1
    return checked


@background.job("agents-model-capabilities", every=900, initial_delay=45)
def capabilities_job(app) -> None:
    if agent_settings.enabled():
        refresh_model_capabilities()
