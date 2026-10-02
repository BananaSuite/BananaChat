"""Model downloads: Ollama pulls and Hugging Face checkpoints for ComfyUI.

Administrators queue downloads (``enqueue_ollama``, ``enqueue_many`` for a
bulk list, ``enqueue_checkpoint``); the background leader runs them in queue
order, ``download_concurrency`` at a time (the ``model-pulls`` job; settings in
``services.model_lifecycle.policy``). One active job per model exists across
processes (a unique index), so queueing is idempotent.

Stopping is not cancelling. When the process shuts down or is recycled, or an
administrator pauses a job or the whole queue, the job goes back to the queue
and resumes later (Ollama and the checkpoint agent both resume partial
downloads); nothing is deleted. Jobs left ``pulling`` by a process that died
are re-queued when the next leader starts. Transient errors (the network,
a busy gateway, no progress for ``stall_minutes``) are retried with back-off up
to ``download_retries`` times (while the health probe sees a remote model
server as down, Ollama downloads wait without using a retry); errors that name
the model (it does not exist) or a full disk fail at once with the reason.
Free space is checked before and, when Ollama runs on this machine, during a
download; a remote model server's own "no space left" error is reported the
same way. A finished download counts only once the model server lists the
model with a digest. Only an administrator's explicit cancel ends a job, and
only then (unless they keep it) are partial files of a model that was *not*
installed before removed.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app
from filelock import Timeout

from bananachat import db
from bananachat.db import catalog
from bananachat.db import pulls as pulls_db
from bananachat.services import background, checkpoint_agent, health, ollama
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError

log = logging.getLogger("bananachat.pulls")

TRUSTED_REGISTRIES = ("registry.ollama.ai", "hf.co", "huggingface.co")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_CHECKPOINT_BYTES = 1024 ** 4  # 1 TiB

# Cancellation reasons.
SHUTDOWN = "shutdown"
USER = "user"


def _config(config=None):
    return config or current_app.config["BC"]


# ----- validation -------------------------------------------------------------

def validate_ollama_name(name: str) -> str:
    """Accept Ollama library tags and Hugging Face GGUF names (``hf.co/owner/repo:quant``)."""
    name = (name or "").strip()
    if name.startswith("https://huggingface.co/"):
        name = "hf.co/" + name.removeprefix("https://huggingface.co/")
    if not ollama.MODEL_NAME_RE.fullmatch(name) or ".." in name or "//" in name or name.endswith(("/", ":")):
        raise ValueError("Enter a model name such as llama3.2:3b or hf.co/owner/repository:Q4_K_M.")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first) and first.lower() not in TRUSTED_REGISTRIES:
        raise ValueError("Only the Ollama library and Hugging Face (hf.co/…) can be downloaded from here.")
    if first.lower() in ("hf.co", "huggingface.co") and len(rest.split("/")) < 2:
        raise ValueError("Hugging Face models are named hf.co/owner/repository[:quantization].")
    return pulls_db.canonical_ollama_name(name)


def _checkpoint_path(value, label: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 512 or "\\" in value:
        raise ValueError(f"{label} is invalid.")
    parts = value.split("/")
    if any(not part or part.startswith(".") or not _PATH_PART_RE.fullmatch(part) for part in parts):
        raise ValueError(f"{label} may only contain letters, digits, dots, hyphens, underscores and folders.")
    if not parts[-1].endswith(".safetensors"):
        raise ValueError(f"{label} must end in .safetensors.")
    return value


def validate_checkpoint(repo_id, source_filename, revision, target_name, expected_sha256, expected_size) -> dict:
    """Validate a checkpoint recipe; returns the cleaned values."""
    repo_id = (repo_id or "").strip()
    parts = repo_id.split("/")
    if len(repo_id) > 192 or len(parts) != 2 or any(".." in part or not _REPO_PART_RE.fullmatch(part) for part in parts):
        raise ValueError("The repository must look like owner/repository.")
    revision = (revision or "").strip() or "main"
    if ".." in revision or not _REVISION_RE.fullmatch(revision):
        raise ValueError("The revision must be a branch, tag or commit id.")
    source_filename = _checkpoint_path(source_filename, "The file")
    target_name = _checkpoint_path(target_name or source_filename.rsplit("/", 1)[-1], "The target name")
    expected_sha256 = (expected_sha256 or "").strip()
    if not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("The SHA-256 must be 64 hexadecimal characters (shown on the file's Hugging Face page).")
    size = None
    if expected_size not in (None, ""):
        try:
            size = int(expected_size)
        except (TypeError, ValueError):
            raise ValueError("The size must be a whole number of bytes.") from None
        if isinstance(expected_size, bool) or not 1 <= size <= MAX_CHECKPOINT_BYTES:
            raise ValueError("The size must be between 1 byte and 1 TiB.")
    return {"repo_id": repo_id, "source_filename": source_filename, "revision": revision,
            "target_name": target_name, "expected_sha256": expected_sha256.lower(), "expected_size": size}


# ----- disk space ----------------------------------------------------------------

def disk_space(config=None) -> dict:
    """Free space where Ollama stores models (only checked when Ollama runs on this host)."""
    config = _config(config)
    result = {"checked": False, "ok": True, "free_gb": None, "total_gb": None, "min_gb": config.min_free_disk_gb,
              "message": ""}
    if not config.ollama_is_local:
        result["message"] = "Models are stored on the compute server; its free space is not checked from here."
        return result
    directory = Path(config.ollama_model_dir).expanduser() if config.ollama_model_dir else None
    if directory is None or not directory.is_dir():
        result["message"] = "The Ollama model directory was not found on this server; disk space is not checked."
        return result
    try:
        usage = shutil.disk_usage(directory)
    except OSError as error:
        result["message"] = f"Disk space could not be checked: {error.strerror or error}"
        return result
    free, total = usage.free / 1024 ** 3, usage.total / 1024 ** 3
    ok = config.min_free_disk_gb <= 0 or free >= config.min_free_disk_gb
    result.update(checked=True, ok=ok, free_gb=round(free, 1), total_gb=round(total, 1),
                  message=(f"{free:.1f} GB free of {total:.1f} GB." if ok else
                           f"Only {free:.1f} GB free; downloads need at least {config.min_free_disk_gb:g} GB."))
    return result


# ----- queueing -----------------------------------------------------------------

MAX_BULK = 50
# A small curated list offered for bulk downloads (names from the Ollama library).
SUGGESTED = (
    ("llama3.2:3b", "Meta Llama 3.2, 3B: fast general chat"),
    ("qwen3:8b", "Qwen 3, 8B: general chat with thinking"),
    ("gemma3:4b", "Google Gemma 3, 4B: understands images"),
    ("gpt-oss:20b", "OpenAI gpt-oss, 20B: reasoning with effort levels"),
    ("deepseek-r1:8b", "DeepSeek R1, 8B: step-by-step reasoning"),
    ("qwen2.5-coder:7b", "Qwen 2.5 Coder, 7B: programming"),
    ("mistral-small3.2:24b", "Mistral Small 3.2, 24B: strong general model"),
    ("granite3.3:8b", "IBM Granite 3.3, 8B: business writing and tools"),
)


DELETING = "being deleted from the model server; choose “Keep it” on its page first"


def _being_deleted(name: str) -> bool:
    """Whether the catalog waits to delete *name* (a download would be deleted right after it finishes)."""
    name = pulls_db.canonical_ollama_name(name)
    alias = name.removesuffix(":latest") if name.endswith(":latest") else name
    return bool(db.scalar("SELECT 1 FROM ai_models WHERE backend='ollama' AND delete_requested_at IS NOT NULL AND "
                          "(ollama_name IN (?,?) OR backend_model_name IN (?,?))", (name, alias, name, alias)))


def enqueue_ollama(name: str, user_id: str | None, config=None, *, source: str = "") -> int:
    name = validate_ollama_name(name)
    space = disk_space(config)
    if space["checked"] and not space["ok"]:
        raise ValueError(space["message"])
    with db.transaction():
        if _being_deleted(name):
            raise ValueError(f"{name} is {DELETING}.")
        return pulls_db.enqueue(name, user_id, backend="ollama", source=source)


def enqueue_many(names, user_id: str | None, config=None) -> dict:
    """Queue several Ollama models at once (idempotent: installed or already queued models are skipped).

    Returns ``{"queued": [(job id, name)], "skipped": [(name, reason)], "errors": [(name, reason)]}``.
    """
    config = _config(config)
    result: dict = {"queued": [], "skipped": [], "errors": []}
    wanted, seen = [], set()
    for raw in names:
        raw = (raw or "").strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            name = validate_ollama_name(raw)
        except ValueError as error:
            result["errors"].append((raw[:120], str(error)))
            continue
        if name not in seen:
            seen.add(name)
            wanted.append(name)
    if len(wanted) > MAX_BULK:
        raise ValueError(f"Queue at most {MAX_BULK} models at once.")
    if not wanted:
        if result["errors"]:
            return result
        raise ValueError("Enter or select at least one model to download.")
    space = disk_space(config)
    if space["checked"] and not space["ok"]:
        raise ValueError(space["message"])
    installed = _installed_set(config)
    for name in wanted:
        try:
            with db.transaction():
                if _being_deleted(name):
                    result["skipped"].append((name, DELETING))
                    continue
                if installed is not None and _is_installed(name, installed):
                    result["skipped"].append((name, "already installed"))
                    continue
                job_id = pulls_db.enqueue(name, user_id, backend="ollama", source="bulk")
        except pulls_db.AlreadyActive as error:
            result["skipped"].append((name, str(error).removeprefix(f"A download of {name} is ")
                                      .rstrip(".")))
            continue
        result["queued"].append((job_id, name))
    return result


def enqueue_checkpoint(values: dict, user_id: str | None, config=None) -> int:
    config = _config(config)
    if not config.images_enabled:
        raise ValueError("Image generation is disabled (BC_IMAGE_BACKEND), so checkpoints cannot be downloaded.")
    problem = checkpoint_agent.configuration_error(config)
    if problem:
        raise ValueError(problem)
    recipe = validate_checkpoint(**values)
    return pulls_db.enqueue(recipe["target_name"], user_id, backend="comfyui", **recipe)


def retry(job_id: int, user_id: str | None, config=None) -> int:
    job = pulls_db.get(job_id)
    if job is None or job["status"] not in ("failed", "cancelled"):
        raise ValueError("Only failed or cancelled downloads can be retried.")
    if job["backend"] == "comfyui":
        return enqueue_checkpoint({key: job[key] for key in pulls_db.RECIPE_FIELDS}, user_id, config)
    return enqueue_ollama(job["ollama_name"], user_id, config)


def _installed_set(config) -> set[str] | None:
    try:
        return ollama.installed_names(config, primary=True)
    except (UpstreamError, OSError):
        return None


def _is_installed(name: str, names: set[str]) -> bool:
    return name in names or (":" not in name.rsplit("/", 1)[-1] and f"{name}:latest" in names)


def suggestions(config=None) -> list[dict]:
    """The curated list minus installed, queued and ignored models."""
    from bananachat.services import model_lifecycle

    rules = model_lifecycle.patterns()
    active = {job["ollama_name"] for job in pulls_db.active_jobs()}
    known = {row["ollama_name"] for row in catalog.list_models(backend="ollama") if row["backend_available"]}
    return [{"name": name, "description": description} for name, description in SUGGESTED
            if name not in known and name not in active and not model_lifecycle.matches(name, rules)]


def missing_catalog_models() -> list[dict]:
    """Catalog models the model server no longer has (not ignored, retired or already queued)."""
    from bananachat.services import model_lifecycle

    rules = model_lifecycle.patterns()
    active = {job["ollama_name"] for job in pulls_db.active_jobs()}
    result = []
    for row in catalog.list_models(backend="ollama"):
        name = row["backend_model_name"] or row["ollama_name"]
        if row["backend_available"] or row["enrollment"] == "ignored" or row["retired_at"] or name in active \
                or model_lifecycle.matches(name, rules):
            continue
        result.append({"name": name, "display_name": row["display_name"], "published": bool(row["is_rolled_out"])})
    return result


# ----- queue control ------------------------------------------------------------

PAUSE = "pause"
STALL = "stall"
GONE = "gone"

_lock = threading.Lock()
_running: dict[int, CancelToken] = {}
_state = {"recovered": False}
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 1800
DISK_CHECK_SECONDS = 15
# Network trouble on the compute server is worth another try; these say the model itself is wrong.
_TRANSIENT_MARKERS = ("dial tcp", "i/o timeout", "connection reset", "connection refused", "timeout", "eof",
                      "tls handshake", "temporary failure", "no such host", "try again", "too many requests",
                      "service unavailable", "bad gateway", "stopped responding", "could not be reached",
                      "ended before it was complete", "does not list")
_PERMANENT_MARKERS = ("file does not exist", "manifest unknown", "not found", "does not exist", "unauthorized",
                      "invalid model", "invalid reference", "denied", "forbidden")
_DISK_MARKERS = ("no space left", "disk full", "not enough space", "insufficient space", "disk quota")


class DownloadProblem(Exception):
    """A download attempt failed; ``transient`` ones are retried with back-off."""

    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


def classify(error) -> DownloadProblem:
    """Turn an error from the model server (directly or through the compute gateway) into a problem."""
    if isinstance(error, DownloadProblem):
        return error
    text = str(error) or type(error).__name__
    lowered = text.lower()
    if any(marker in lowered for marker in _DISK_MARKERS):
        return DownloadProblem(f"The model server ran out of disk space: {text}", transient=False)
    status = getattr(error, "status", None)
    if status in (400, 401, 403, 404, 413):
        return DownloadProblem(text, transient=False)
    if any(marker in lowered for marker in _TRANSIENT_MARKERS) or getattr(error, "kind", None):
        return DownloadProblem(text, transient=True)
    if any(marker in lowered for marker in _PERMANENT_MARKERS):
        return DownloadProblem(text, transient=False)
    return DownloadProblem(text, transient=True)


def current_job_id() -> int | None:
    with _lock:
        return min(_running) if _running else None


def running_job_ids() -> list[int]:
    with _lock:
        return sorted(_running)


def _local_token(job_id: int) -> CancelToken | None:
    with _lock:
        return _running.get(job_id)


def interrupt_current(reason: str = SHUTDOWN) -> bool:
    """Stop the downloads running in this process. A shutdown re-queues them."""
    with _lock:
        tokens = list(_running.values())
    for token in tokens:
        token.cancel(reason)
    return bool(tokens)


def cancel(job_id: int, *, cleanup: bool = True) -> bool:
    """An administrator's cancel: ends the job for good (the process running it notices within a second).

    With *cleanup*, the partial download of a model that was not installed
    before is removed; without it, a later download resumes where this stopped.
    """
    with db.transaction():
        before = pulls_db.get(job_id)
        if before is None or before["status"] not in pulls_db.ACTIVE:
            return False
        pulls_db.set_cleanup(job_id, cleanup)
        cancelled = pulls_db.cancel(job_id, "Cancelled by an administrator.")
    token = _local_token(job_id)
    if cancelled and token is not None:
        token.cancel(USER)
    elif cancelled and cleanup and before is not None and before["status"] == "queued":
        _cancel_on_agent(before)
    return cancelled


def _cancel_on_agent(job) -> None:
    """A waiting checkpoint job (paused or interrupted) may still download on the agent: stop it there too."""
    if job["backend"] != "comfyui" or not job["remote_job_id"]:
        return
    try:
        checkpoint_agent.client(_config()).cancel_download(job["remote_job_id"])
    except Exception as error:  # noqa: BLE001 - best effort, like the cleanup of a running download
        log.info("Cancelling %s on the checkpoint agent failed: %s", job["ollama_name"], error)


def cancel_all(*, cleanup: bool = True) -> list:
    """Cancel every queued and running download; returns the cancelled jobs."""
    cancelled = []
    for job in pulls_db.active_jobs():
        if cancel(job["id"], cleanup=cleanup):
            cancelled.append(job)
    return cancelled


def pause(job_id: int) -> bool:
    """Hold one download; a running one stops and resumes from where it was when resumed."""
    paused = pulls_db.set_paused(job_id, True)
    token = _local_token(job_id)
    if paused and token is not None:
        token.cancel(PAUSE)
    return paused


def resume(job_id: int) -> bool:
    return pulls_db.set_paused(job_id, False)


def set_queue_paused(paused: bool, actor=None) -> None:
    """Pause or resume the whole queue (running downloads stop and wait, nothing is lost)."""
    from bananachat.services import model_lifecycle

    model_lifecycle.save_policy({"downloads_paused": paused}, actor)
    if paused:
        interrupt_current(PAUSE)


def move(job_id: int, direction: str) -> bool:
    if direction not in ("up", "down", "top", "bottom"):
        raise ValueError("Unknown direction.")
    return pulls_db.move(job_id, direction)


def queue_status() -> dict:
    from bananachat.services import model_lifecycle

    current = model_lifecycle.policy()
    summary = pulls_db.summary()
    summary.update(queue_paused=current["downloads_paused"], concurrency=current["download_concurrency"],
                   retries=current["download_retries"], stall_minutes=current["stall_minutes"])
    return summary


def _policy() -> dict:
    from bananachat.services import model_lifecycle

    return model_lifecycle.policy()


def _stall_seconds(current: dict) -> float:
    return current["stall_minutes"] * 60


def _owner(job) -> str | None:
    """The claim token of this run of *job* (what it writes names it; see ``db.pulls``)."""
    return job["claim_token"] if "claim_token" in job.keys() else None


def _watch(job, token: CancelToken, finished: threading.Event, stopping, progress: dict) -> None:
    """Turn a shutdown, a pause, a stall, a cancel made by another process or a job claimed by another run
    (a new leader) into a cancelled token."""
    job_id = job["id"]
    checks = 0
    current = _policy()
    try:
        while not finished.wait(1.0):
            if stopping():
                token.cancel(SHUTDOWN)
                return
            checks += 1
            try:
                if checks % 5 == 0:
                    current = _policy()
                row = pulls_db.state(job_id)
            except Exception:  # noqa: BLE001 - database briefly busy: check again
                db.close_thread_connection()
                continue
            if row is None or row["status"] == "cancelled":
                token.cancel(USER if row is not None else GONE)
                return
            if row["status"] != "pulling" or (_owner(job) and row["claim_token"] != _owner(job)):
                token.cancel(GONE)
                return
            if row["paused"] or current["downloads_paused"]:
                token.cancel(PAUSE)
                return
            if time.monotonic() - progress["at"] > _stall_seconds(current):
                token.cancel(STALL)
                return
    finally:
        db.close_thread_connection()


def _skipped_backend(config) -> str | None:
    """Ollama downloads wait while the health probe sees the remote model server as down (no attempt is used)."""
    return "ollama" if not config.ollama_is_local and health.inference_down() else None


def process_next(config=None, *, stopping=None) -> bool:
    """Claim the next runnable download and run it to its end. Returns False when nothing was started."""
    config = _config(config)
    current = _policy()
    if current["downloads_paused"]:
        return False
    job = pulls_db.claim_next(current["download_concurrency"], skip_backend=_skipped_backend(config))
    if job is None:
        return False
    _execute(job, config, stopping or background.stopping)
    return True


def _execute(job, config, stopping) -> None:
    from bananachat.services import model_lifecycle

    token, finished = CancelToken(), threading.Event()
    progress = {"at": time.monotonic()}
    with _lock:
        _running[job["id"]] = token
    watcher = threading.Thread(target=_watch, args=(job, token, finished, stopping, progress),
                               name=f"bananachat-pull-watch-{job['id']}", daemon=True)
    watcher.start()
    operation = None
    try:
        operation = model_lifecycle.operation_lock(job["ollama_name"], config, backend=job["backend"])
        while True:
            _check_claim(job, token)
            try:
                operation.acquire(timeout=0)
                break
            except Timeout:
                progress["at"] = time.monotonic()  # waiting for an older operation is not a stalled download
                token.wait(0.25)
        _check_claim(job, token)
        if job["backend"] == "comfyui":
            _run_checkpoint(job, token, config, progress)
        else:
            _run_ollama(job, token, config, progress)
    except Cancelled:
        _interrupted(job, token)
    except Exception as error:  # noqa: BLE001 - never leave a job running on an unexpected error
        log.exception("Download job %s failed unexpectedly", job["id"])
        pulls_db.finish(job["id"], "failed", f"Unexpected error: {type(error).__name__}", owner=_owner(job))
    finally:
        if operation is not None:
            operation.release()
        finished.set()
        with _lock:
            _running.pop(job["id"], None)
        watcher.join(timeout=5)


def _retry_or_fail(job, problem: DownloadProblem) -> None:
    current = _policy()
    if problem.transient and job["backend"] == "ollama" and _skipped_backend(_config()):
        # The model server is down (an outage, a reboot): wait for it without using up an attempt.
        pulls_db.schedule_retry(job["id"], job["attempts"] or 0, db.now(timedelta(seconds=RETRY_BASE_SECONDS)),
                                str(problem), f"Waiting for the model server to be reachable again: {problem}",
                                owner=_owner(job))
        return
    attempts = (job["attempts"] or 0) + 1
    if problem.transient and attempts <= current["download_retries"]:
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 4 ** (attempts - 1))
        wait = f"{delay // 60} min" if delay >= 60 else f"{delay} s"
        detail = f"Retrying in {wait} (attempt {attempts + 1} of {current['download_retries'] + 1}): {problem}"
        if pulls_db.schedule_retry(job["id"], attempts, db.now(timedelta(seconds=delay)), str(problem), detail,
                                   owner=_owner(job)):
            log.info("Download of %s failed (%s); retrying in %s", job["ollama_name"], problem, wait)
        return
    message = str(problem)
    if problem.transient and attempts > 1:
        message = f"{message} (gave up after {attempts} attempts)"
    pulls_db.finish(job["id"], "failed", message, owner=_owner(job))
    log.warning("Download of %s failed: %s", job["ollama_name"], message)


def _check_claim(job, token: CancelToken) -> None:
    """Check ownership at backend boundaries, where the watcher may not have run yet."""
    row = pulls_db.state(job["id"])
    if row is None or (_owner(job) and row["claim_token"] != _owner(job)):
        token.cancel(GONE)
    elif row["status"] == "cancelled":
        token.cancel(USER)
    elif row["status"] != "pulling":
        token.cancel(GONE)
    elif row["paused"]:
        token.cancel(PAUSE)
    token.check()


def _interrupted(job, token: CancelToken, *, cleanup=None) -> None:
    """Handle a cancelled token: re-queue after a shutdown or pause, clean up after a user cancel, retry a stall."""
    if token.reason == USER:
        pulls_db.cancel(job["id"], "Cancelled by an administrator.", owner=_owner(job))
        log.info("Download of %s cancelled by an administrator", job["ollama_name"])
        row = pulls_db.state(job["id"])
        active = pulls_db.active_for(job["ollama_name"], job["backend"])
        owns_cancel = row is not None and row["status"] == "cancelled" and (
            not _owner(job) or row["claim_token"] == _owner(job))
        if cleanup is not None and owns_cancel and row["cleanup_partial"] and active is None:
            try:
                cleanup()
            except Exception as error:  # noqa: BLE001 - best effort
                log.info("Cleanup after cancelling %s failed: %s", job["ollama_name"], error)
    elif token.reason == SHUTDOWN:
        pulls_db.requeue(job["id"], owner=_owner(job))
        log.info("Download of %s interrupted by shutdown; it will resume", job["ollama_name"])
    elif token.reason == PAUSE:
        pulls_db.requeue(job["id"], "Paused; it resumes from where it stopped.", owner=_owner(job))
        log.info("Download of %s paused", job["ollama_name"])
    elif token.reason == STALL:
        minutes = _policy()["stall_minutes"]
        _retry_or_fail(job, DownloadProblem(f"No progress for {minutes} minute{'s' if minutes != 1 else ''}.",
                                            transient=True))


def _installed(name: str, config) -> bool | None:
    """Whether the primary Ollama (where downloads go) already has *name*; None when that cannot be determined."""
    names = _installed_set(config)
    return None if names is None else _is_installed(name, names)


def _verify(name: str, config) -> str:
    """The digest the model server lists for *name* after a download (raises DownloadProblem when it does not)."""
    try:
        tags = ollama.list_tags(config, primary=True)
    except (UpstreamError, OSError) as error:
        raise DownloadProblem(f"The download finished but could not be verified: {error}", transient=True) from None
    for tag in tags:
        if tag["name"] == name or (":" not in name.rsplit("/", 1)[-1] and tag["name"] == f"{name}:latest"):
            digest = tag.get("digest")
            if isinstance(digest, str) and digest:
                return digest[:128]
            raise DownloadProblem(f"The model server lists {name} without a digest.", transient=True)
    raise DownloadProblem(f"The download finished but the model server does not list {name}.", transient=True)


def _run_ollama(job, token: CancelToken, config, progress: dict | None = None) -> None:
    name = job["ollama_name"]
    progress = progress if progress is not None else {"at": time.monotonic()}
    space = disk_space(config)
    if space["checked"] and not space["ok"]:
        pulls_db.finish(job["id"], "failed", space["message"], owner=_owner(job))
        return
    installed_before = _installed(name, config)
    log.info("Downloading %s (job %s, attempt %s)", name, job["id"], (job["attempts"] or 0) + 1)

    def cleanup():
        # Only a model that did not exist before may be removed; an update of an
        # installed model keeps the installed version.
        if installed_before is False:
            ollama.delete(name, config)

    success, last_update, last_percent = False, 0.0, -1
    last_seen: tuple = ("", -1)
    disk_checked = time.monotonic()
    try:
        _check_claim(job, token)
        for record in ollama.pull(name, cancel=token, config=config):
            token.check()
            status = str(record.get("status") or "")[:120]
            if status == "success":
                success = True
                break
            total, completed = record.get("total"), record.get("completed") or 0
            now = time.monotonic()
            if (status, completed) != last_seen and (status != last_seen[0] or completed > last_seen[1]):
                progress["at"] = now  # the stall timer restarts on any real progress
                last_seen = (status, completed)
            if isinstance(total, (int, float)) and total > 0 and isinstance(completed, (int, float)):
                percent = min(99, int(completed * 100 / total))
                detail = f"{status}: {completed / 1024 ** 2:,.0f} / {total / 1024 ** 2:,.0f} MB"
                sizes = (int(completed), int(total))
            else:
                percent, detail, sizes = max(0, last_percent), status or "Working…", (None, None)
            if percent != last_percent or now - last_update >= 1.0:
                pulls_db.update_progress(job["id"], percent, detail, completed=sizes[0], total=sizes[1],
                                         owner=_owner(job))
                last_update, last_percent = now, percent
            if config.ollama_is_local and now - disk_checked >= DISK_CHECK_SECONDS:
                disk_checked = now
                space = disk_space(config)
                if space["checked"] and not space["ok"]:
                    raise DownloadProblem(f"Stopped: {space['message']}", transient=False)
        token.check()
        if not success:
            raise DownloadProblem("The download ended before it was complete.", transient=True)
        digest = _verify(name, config)
        _check_claim(job, token)
    except Cancelled:
        _interrupted(job, token, cleanup=cleanup)
        return
    except (UpstreamError, OSError, DownloadProblem) as error:
        if token.cancelled:
            _interrupted(job, token, cleanup=cleanup)
            return
        _retry_or_fail(job, classify(error))
        return
    if not pulls_db.finish(job["id"], "done", digest=digest, owner=_owner(job)):
        try:
            _check_claim(job, token)
        except Cancelled:
            _interrupted(job, token, cleanup=cleanup)
        return
    log.info("Downloaded %s (%s)", name, digest[:19])
    try:
        ollama.sync_catalog(config, source="download")
    except (UpstreamError, OSError) as error:
        log.info("Catalog sync after downloading %s failed: %s", name, error)


def _sync_comfyui() -> str | None:
    """Refresh ComfyUI checkpoints in the catalog; returns a problem or None."""
    try:
        from bananachat.services import comfyui
    except ImportError:
        return "ComfyUI support is not installed."
    try:
        comfyui.sync_models()
    except Exception as error:  # noqa: BLE001 - reported to the administrator
        return f"ComfyUI could not be refreshed: {error}"
    return None


def _run_checkpoint(job, token: CancelToken, config, progress: dict | None = None) -> None:
    from bananachat.services.checkpoint_agent import CheckpointAgentError

    target = job["target_name"]
    remote_id = job["remote_job_id"]
    agent = None

    def cancel_remote():
        if agent is not None and remote_id:
            agent.cancel_download(remote_id)

    try:
        _check_claim(job, token)
        if not config.images_enabled:
            raise CheckpointAgentError("Image generation is disabled (BC_IMAGE_BACKEND).")
        agent = checkpoint_agent.client(config)
        if not remote_id:
            remote = agent.submit_download(
                repo_id=job["repo_id"], source_filename=job["source_filename"], revision=job["revision"],
                target_name=target, expected_sha256=job["expected_sha256"], expected_size=job["expected_size"],
                idempotency_key=job["idempotency_key"])
            remote_id = remote["id"]
            pulls_db.set_remote_id(job["id"], remote_id, owner=_owner(job))
            _check_claim(job, token)
        while True:
            if token.cancelled:
                _interrupted(job, token, cleanup=cancel_remote)
                return
            remote = agent.get_download(remote_id)
            _check_claim(job, token)
            status = remote.get("status")
            if status not in checkpoint_agent.REMOTE_STATUSES:
                raise CheckpointAgentError("The checkpoint agent returned an unknown status.")
            received = remote.get("bytes_received") or 0
            total = remote.get("expected_size") or job["expected_size"]
            if isinstance(received, bool) or not isinstance(received, int) or received < 0 or (
                    total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 1)):
                raise CheckpointAgentError("The checkpoint agent returned invalid progress.")
            percent = min(99, received * 100 // total) if total else 0
            detail = (f"{status}: {received / 1024 ** 2:,.0f} / {total / 1024 ** 2:,.0f} MB" if total
                      else f"{status}: {received / 1024 ** 2:,.0f} MB")
            if progress is not None and (status == "queued" or (status, received) != progress.get("seen")):
                # Waiting in the agent's own queue is not a stall.
                progress.update(at=time.monotonic(), seen=(status, received))
            pulls_db.update_progress(job["id"], percent, detail, completed=received, total=total, owner=_owner(job))
            if status == "completed":
                if not pulls_db.finish(job["id"], "done", owner=_owner(job)):
                    _check_claim(job, token)
                    return
                problem = _sync_comfyui()
                model = catalog.get_by_name(f"comfyui:{target}")
                if problem or model is None or not model["backend_available"]:
                    pulls_db.finished_detail(job["id"], "Downloaded and verified. ComfyUI has not listed it "
                                                       "yet; use Sync now once ComfyUI sees the file.",
                                             owner=_owner(job))
                log.info("Checkpoint %s downloaded", target)
                return
            if status == "failed":
                raise CheckpointAgentError(checkpoint_agent.job_error_message(remote))
            if status == "canceled":
                pulls_db.finish(job["id"], "cancelled", "Cancelled on the compute server.", owner=_owner(job))
                return
            token.wait(2.0)
    except Cancelled:
        _interrupted(job, token, cleanup=cancel_remote)
    except (CheckpointAgentError, UpstreamError, OSError) as error:
        if token.cancelled:
            _interrupted(job, token, cleanup=cancel_remote)
            return
        pulls_db.finish(job["id"], "failed", str(error), owner=_owner(job))
        log.warning("Checkpoint download of %s failed: %s", target, error)


# ----- background job -----------------------------------------------------------

def _job_thread(app, job) -> None:
    with app.app_context():
        try:
            _execute(job, app.config["BC"], background.stopping)
        finally:
            db.release_thread_connection()


@background.job("model-pulls", every=2, long_running=True, initial_delay=3)
def run_downloads(app) -> None:
    """Leader-only loop: resume interrupted jobs once, then run queued ones (``download_concurrency`` at a time)."""
    if not _state["recovered"]:
        resumed = pulls_db.reset_stuck()
        _state["recovered"] = True
        if resumed:
            log.info("Resuming %d interrupted model download(s)", resumed)
    workers: dict[int, threading.Thread] = {}
    while not background.stopping():
        for job_id, thread in list(workers.items()):
            if not thread.is_alive():
                workers.pop(job_id)
        current = _policy()
        job = None
        if not current["downloads_paused"] and len(workers) < current["download_concurrency"]:
            job = pulls_db.claim_next(current["download_concurrency"], skip_backend=_skipped_backend(app.config["BC"]))
        if job is not None:
            thread = threading.Thread(target=_job_thread, args=(app, job), name=f"bananachat-pull-{job['id']}",
                                      daemon=True)
            workers[job["id"]] = thread
            thread.start()
            continue
        if not workers:
            return
        time.sleep(0.5)
    for thread in workers.values():
        thread.join(timeout=10)
