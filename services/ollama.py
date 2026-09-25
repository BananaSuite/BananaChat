"""Ollama HTTP client and model sync."""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from http_transport import open_http
import db
from filelock import FileLock, Timeout
from services import model_access

_logger = logging.getLogger("bananachat.ollama")


class OllamaConnectionError(RuntimeError):
    pass

def _auth_headers() -> dict:
    """Return auth headers to include in every Ollama request.

    When BC_OLLAMA_API_KEY is set, Ollama requires an Authorization header
    (introduced in Ollama ≥ 0.1.24).  For local deployments leave the env
    var unset and no header is added.
    """
    if config.OLLAMA_API_KEY:
        return {"Authorization": f"Bearer {config.OLLAMA_API_KEY}"}
    return {}


def _resolve_url(path, *, use_primary=False):
    """Build the full URL for an Ollama API path.

    Uses the effective URL (primary or fallback) unless *use_primary* is
    True, which forces the configured primary URL (used by the health
    monitor to ping the real server even during fallback).
    """
    base = config.OLLAMA_BASE_URL if use_primary else get_effective_ollama_url()
    return base.rstrip("/") + path


def _get(path, timeout=10):
    url = _resolve_url(path)
    headers = {"Accept": "application/json", **_auth_headers()}
    req = urllib.request.Request(url, headers=headers)
    with open_http(req, timeout=timeout, public_only=False) as resp:
        return json.loads(resp.read())


def _post_stream(path, body, timeout=None):
    url = _resolve_url(path)
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_auth_headers(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    deadline = time.monotonic() + config.GENERATION_TIMEOUT
    with open_http(req, timeout=timeout or config.INFERENCE_READ_TIMEOUT, total_timeout=config.GENERATION_TIMEOUT, public_only=False) as resp:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Inference exceeded the configured generation deadline.")
            line = resp.readline(128 * 1024 + 1)
            if not line:
                break
            if len(line) > 128 * 1024:
                raise RuntimeError("Inference server returned an oversized stream record.")
            line = line.strip()
            if line:
                yield line.decode("utf-8", errors="replace")


def _post(path, body, timeout=300):
    url = _resolve_url(path)
    data = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_auth_headers(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with open_http(req, timeout=timeout, public_only=False) as resp:
        return json.loads(resp.read())


def list_running_models():
    """Return list of currently loaded models from Ollama /api/ps."""
    try:
        data = _get("/api/ps", timeout=5)
        return data.get("models", [])
    except Exception as exc:
        _logger.debug("Could not fetch running models: %s", exc)
        return []


def list_available_models():
    """Return list of all installed models from Ollama /api/tags."""
    try:
        data = _get("/api/tags", timeout=10)
    except Exception as exc:
        raise OllamaConnectionError("Could not fetch models from Ollama") from exc
    if (
        not isinstance(data, dict)
        or "models" not in data
        or not isinstance(data["models"], list)
    ):
        raise OllamaConnectionError("Ollama returned an invalid model list")
    return data["models"]


def sync_models():
    """Sync Ollama model list into the internal DB catalog."""
    models = list_available_models()
    discovered = []
    for m in models:
        if not isinstance(m, dict):
            raise OllamaConnectionError("Ollama returned an invalid model entry")
        ollama_name = m.get("name")
        size = m.get("size", 0)
        details = m.get("details", {})
        if (
            not isinstance(ollama_name, str)
            or not ollama_name.strip()
            or isinstance(size, bool)
            or not isinstance(size, (int, float))
            or size < 0
            or not isinstance(details, dict)
        ):
            raise OllamaConnectionError("Ollama returned an invalid model entry")
        ollama_name = ollama_name.strip()
        display_name = ollama_name.split(":")[0].replace("-", " ").replace("_", " ").title()
        size_gb = size / (1024 ** 3)
        description = f"{details.get('parameter_size', '')} | {size_gb:.1f} GB".strip(" |")
        discovered.append({
            "name": ollama_name,
            "display_name": display_name,
            "description": description,
        })
    db.sync_ollama_models(discovered)
    _logger.debug("Model sync complete: %d models from Ollama", len(models))
    return models


def _nvidia_metrics():
    """Return aggregate NVIDIA telemetry, or None when nvidia-smi is absent."""
    binary = shutil.which("nvidia-smi")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 4:
                continue
            rows.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
        if not rows:
            return None
        total_mb = sum(row[2] for row in rows)
        weighted_util = (
            sum(row[3] * row[2] for row in rows) / total_mb if total_mb else None
        )
        return {
            "name": " + ".join(row[0] for row in rows),
            "used_mb": sum(row[1] for row in rows),
            "total_mb": total_mb,
            "utilization": weighted_util,
            "source": "nvidia-smi",
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ollama_is_local():
    try:
        host = (urllib.parse.urlparse(config.OLLAMA_BASE_URL).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def collect_compute_snapshot(queue_depth=0):
    running = list_running_models()
    active_names = [m.get("name", "") for m in running if m.get("name")]

    # Local hardware tools describe the inference GPU only when Ollama runs on
    # this host. With a remote Ollama server, local nvidia-smi would report the
    # wrong machine, so use Ollama's allocation data instead.
    gpu = _nvidia_metrics() if _ollama_is_local() else None
    if not gpu:
        # Ollama exposes model VRAM allocation even when hardware tooling is
        # unavailable. It is not total board usage, so leave total unknown and
        # label the source explicitly in the admin UI.
        allocated = sum(float(m.get("size_vram") or 0) for m in running) / (1024 ** 2)
        gpu = {
            "name": None,
            "used_mb": allocated if allocated > 0 else None,
            "total_mb": None,
            "utilization": None,
            "source": "ollama-allocation" if allocated > 0 else "unavailable",
        }

    cpu_percent = None
    system_mem_used = None
    system_mem_total = None
    try:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        system_mem_used = vm.used / (1024 ** 2)
        system_mem_total = vm.total / (1024 ** 2)
    except ImportError:
        pass

    db.record_compute_snapshot(
        gpu_name=gpu["name"],
        gpu_memory_used_mb=gpu["used_mb"],
        gpu_memory_total_mb=gpu["total_mb"],
        gpu_utilization_percent=gpu["utilization"],
        system_memory_used_mb=system_mem_used,
        system_memory_total_mb=system_mem_total,
        metrics_source=gpu["source"],
        cpu_percent=cpu_percent,
        active_models=active_names,
        queue_depth=queue_depth,
    )


def generate_chat_stream(model, messages, options=None):
    """Stream chat completions from Ollama. Yields (content_delta, done, usage).

    Raises RuntimeError if Ollama returns an error in the response.
    """
    body = {"model": model, "messages": messages, "stream": True, "keep_alive": config.KEEP_ALIVE}
    if options:
        body["options"] = options

    for raw_line in _post_stream("/api/chat", body):
        try:
            chunk = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Ollama surfaces errors as {"error": "..."} even in streaming mode
        if "error" in chunk:
            raise RuntimeError(chunk["error"])

        done = chunk.get("done", False)
        content = chunk.get("message", {}).get("content", "")
        usage = {}
        if done:
            usage = {
                "prompt_tokens": chunk.get("prompt_eval_count", 0),
                "completion_tokens": chunk.get("eval_count", 0),
                "finish_reason": chunk.get("done_reason", "stop"),
            }
        yield content, done, usage


def generate_chat(model, messages, options=None):
    """Non-streaming chat completion. Returns (full_content, prompt_tokens, completion_tokens)."""
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": config.KEEP_ALIVE}
    if options:
        body["options"] = options

    data = _post("/api/chat", body)
    if "error" in data:
        raise RuntimeError(data["error"])
    content = data.get("message", {}).get("content", "")
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    return content, prompt_tokens, completion_tokens


def pull_model(model_name, on_progress=None, is_cancelled=None):
    """Stream-pull a model from the Ollama registry.

    on_progress(pct, detail) is called with each progress update.
    is_cancelled() is polled between each response line; return True to abort.
    Returns True on success, False if cancelled. Raises RuntimeError on Ollama error.
    """
    url = _resolve_url("/api/pull")
    data = json.dumps({"name": model_name, "stream": True}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", **_auth_headers()},
        method="POST",
    )
    try:
        resp = open_http(req, timeout=60, total_timeout=43200, public_only=False)
    except Exception as exc:
        raise RuntimeError(f"Could not connect to Ollama: {exc}") from exc

    try:
        for raw_line in resp:
            if is_cancelled and is_cancelled():
                return False
            line = raw_line.strip().decode("utf-8", errors="replace")
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            status = chunk.get("status", "")
            if status == "success":
                if on_progress:
                    on_progress(100, "Done")
                return True
            total_bytes = chunk.get("total")
            completed_bytes = chunk.get("completed", 0)
            if total_bytes and total_bytes > 0:
                pct = min(99, int(completed_bytes / total_bytes * 100))
                detail = f"{status}: {completed_bytes/(1024*1024):.0f} / {total_bytes/(1024*1024):.0f} MB"
            else:
                pct = 0
                detail = status or "…"
            if on_progress:
                on_progress(pct, detail)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    raise RuntimeError("The model download ended before completion")


def delete_ollama_model(model_name):
    """Delete a model from Ollama storage. Returns True on success, False if not found."""
    url = _resolve_url("/api/delete")
    data = json.dumps({"name": model_name}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **_auth_headers()},
        method="DELETE",
    )
    try:
        with open_http(req, timeout=30, public_only=False):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"Ollama delete failed (HTTP {exc.code})") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not connect to Ollama: {exc}") from exc


def check_disk_space():
    """Check free disk space on the Ollama model directory.

    Returns (ok: bool, message: str | None).
    If the check is disabled (empty path or min_free=0) returns (True, None).
    """
    model_dir = config.OLLAMA_MODEL_DIR
    min_free_gb = config.MIN_FREE_DISK_GB
    if not model_dir or min_free_gb <= 0:
        return True, None
    try:
        usage = shutil.disk_usage(model_dir)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        if free_gb < min_free_gb:
            return False, f"Only {free_gb:.1f} GB free (minimum {min_free_gb:.0f} GB required)"
        return True, f"{free_gb:.1f} GB free of {total_gb:.1f} GB"
    except Exception as exc:
        _logger.warning("Disk space check failed: %s", exc)
        return True, None


def unload_model(model_name):
    """Unload a specific model from Ollama memory by sending keep_alive=0."""
    try:
        body = {"model": model_name, "keep_alive": 0, "stream": False}
        _post("/api/generate", body, timeout=10)
        return True
    except Exception as exc:
        _logger.debug("Could not unload model %s: %s", model_name, exc)
        return False


def unload_all_models():
    """Unload all currently running models from Ollama memory."""
    running = list_running_models()
    names = [m.get("name", "") for m in running if m.get("name")]
    for name in names:
        unload_model(name)
    return names


def is_model_available(model_name):
    """Return True if the model exists in Ollama (checks both with and without :latest tag)."""
    models = list_available_models()
    names = {m.get("name", "") for m in models}
    if model_name in names:
        return True
    if ":" not in model_name and (model_name + ":latest") in names:
        return True
    return False


def select_auto_model(user, surface="chat", image_only=False, vision_only=False, *, exclude=()):
    """Select the best model for 'auto' routing.

    1. Fetches models actually installed in Ollama.
    2. Filters to models authorized for the requesting user and surface.
    3. Prefers currently-loaded (running) models, then smaller models.
    Returns (model_dict, None) on success, or (None, error_message) on failure.
    """
    if image_only:
        return None, "Ollama is a text-only backend; use ComfyUI for images."

    try:
        available = list_available_models()
        available_names = {m.get("name", "") for m in available}
    except Exception as exc:
        _logger.warning("auto model: cannot reach Ollama: %s", exc)
        return None, "Ollama is not reachable: make sure it is running."

    if not available_names:
        _logger.warning("auto model: Ollama returned no models")
        return None, "Ollama is running but has no models installed."

    try:
        running = list_running_models()
        running_names = {m.get("name", "") for m in running}
    except Exception:
        running_names = set()

    candidates = model_access.list_accessible_models(
        user, surface, image_only=image_only, available_only=False
    )
    if not candidates:
        if user and user.get("role") == "admin":
            _logger.warning("auto model: no models in catalog at all")
            return None, "No models found in catalog. Go to Admin → Models → Sync."
        _logger.warning("auto model: no authorized models for user")
        return None, "No models are currently available to your account."

    def _resolve_ollama_name(db_name):
        """Return the exact Ollama name to use, or None if unavailable."""
        if db_name in available_names:
            return db_name
        if ":" not in db_name and (db_name + ":latest") in available_names:
            return db_name + ":latest"
        return None

    valid = [
        m for m in candidates
        if m.get("backend") == "ollama" and not m.get("is_image_generation")
        and m["id"] not in exclude and m["ollama_name"] not in exclude
        and _resolve_ollama_name(m.get("backend_model_name") or m["ollama_name"]) not in exclude
        and (not vision_only or m.get("supports_vision"))
        and _resolve_ollama_name(m.get("backend_model_name") or m["ollama_name"]) is not None
    ]
    if not valid:
        _logger.warning(
            "auto model: rolled-out models %s not found in Ollama (%s): sync needed",
            [m["ollama_name"] for m in candidates],
            sorted(available_names),
        )
        return (
            None,
            "Rolled-out models are no longer available in Ollama. "
            "Go to Admin → Models → Sync, then re-roll out your models.",
        )

    def _score(m):
        name = m["ollama_name"].lower()
        resolved = _resolve_ollama_name(m.get("backend_model_name") or m["ollama_name"])
        running_bonus = 0 if resolved in running_names else 1
        small_bonus = 0 if any(
            kw in name for kw in ("small", "mini", "nano", "tiny", "1b", "2b", "3b")
        ) else 1
        return (running_bonus, small_bonus, name)

    best = min(valid, key=_score)
    resolved_name = _resolve_ollama_name(
        best.get("backend_model_name") or best["ollama_name"]
    )
    result = dict(best)
    result["backend_model_name"] = resolved_name
    result["backend_available"] = 1
    _logger.debug(
        "auto model selected: %s (resolved from DB name %s)", resolved_name, best["ollama_name"]
    )
    return result, None


# Thread handles and stop events for the background workers. Under Gunicorn a
# file lock elects one worker to own them, so in every other process these stay
# at their initial values.

_sync_thread = None      # model catalog sync
_snapshot_thread = None  # compute snapshots + retention cleanup
_sync_stop = threading.Event()
_snapshot_stop = threading.Event()
_leader_lock = None

_pull_thread = None
_pull_stop = threading.Event()
_current_pull_cancel = threading.Event()

_health_thread = None
_health_stop = threading.Event()
_inference_server_down = False
_inference_failure_count = 0
_inference_health_lock = threading.Lock()


def is_inference_server_down():
    """Return True when the remote inference server is unreachable."""
    return _inference_server_down


def _is_remote_ollama():
    """Return True when the configured Ollama URL points to a remote host."""
    from urllib.parse import urlparse
    parsed = urlparse(config.OLLAMA_BASE_URL)
    host = (parsed.hostname or "").lower()
    return host not in ("", "localhost", "127.0.0.1", "::1")


def _ping_inference_server():
    """Try to reach the primary Ollama API.  Returns True on success."""
    try:
        url = _resolve_url("/api/tags", use_primary=True)
        headers = {"Accept": "application/json", **_auth_headers()}
        req = urllib.request.Request(url, headers=headers)
        with open_http(req, timeout=5, public_only=False) as resp:
            resp.read()
        return True
    except Exception:
        return False


def get_effective_ollama_url():
    """Return the Ollama URL to use for inference right now.

    In fallback mode, returns the local URL when the primary is down.
    Otherwise always returns the configured primary URL.
    """
    if _inference_server_down and config.INFERENCE_OUTAGE_MODE == "fallback":
        return config.INFERENCE_FALLBACK_URL
    return config.OLLAMA_BASE_URL


def cancel_current_pull():
    """Signal the in-progress Ollama pull to cancel."""
    _current_pull_cancel.set()


def _process_checkpoint_pull(job):
    """Submit and monitor one ComfyUI checkpoint agent job."""
    from services import checkpoint_agent, comfyui

    job_id = job["id"]
    target_name = job["target_name"]
    remote_job_id = job.get("remote_job_id")
    client = None

    def is_cancelled():
        return db.is_pull_job_cancelled(job_id)

    def cancel_remote():
        if client is not None and remote_job_id:
            try:
                client.cancel_download(remote_job_id)
            except checkpoint_agent.CheckpointAgentError as exc:
                _logger.warning(
                    "Checkpoint cancel request failed for job %d: %s", job_id, exc
                )

    _logger.info("Checkpoint pull started: %s (job %d)", target_name, job_id)
    try:
        if not comfyui.is_enabled():
            raise checkpoint_agent.CheckpointAgentError(
                "ComfyUI image generation is disabled."
            )
        client = checkpoint_agent.get_client()
        if is_cancelled():
            db.finish_pull_job(job_id, success=False, error=None)
            return

        if not remote_job_id:
            remote = client.submit_download(
                repo_id=job["repo_id"],
                source_filename=job["source_filename"],
                revision=job["revision"],
                target_name=target_name,
                expected_sha256=job["expected_sha256"],
                expected_size=job["expected_size"],
                idempotency_key=job["idempotency_key"],
            )
            remote_job_id = remote["id"]
            if is_cancelled():
                cancel_remote()
                db.finish_pull_job(job_id, success=False, error=None)
                return
            db.set_pull_job_remote_id(job_id, remote_job_id)

        while not _pull_stop.is_set():
            if is_cancelled():
                cancel_remote()
                db.finish_pull_job(job_id, success=False, error=None)
                _logger.info("Checkpoint pull cancelled: %s (job %d)", target_name, job_id)
                return

            remote = client.get_download(remote_job_id)
            status = remote.get("status")
            if status not in ("queued", "downloading", "completed", "failed", "canceled"):
                raise checkpoint_agent.CheckpointAgentError(
                    "Checkpoint agent returned an invalid job status."
                )
            received = remote.get("bytes_received", 0)
            total = remote.get("expected_size") or job.get("expected_size")
            if isinstance(received, bool) or not isinstance(received, int) or received < 0:
                raise checkpoint_agent.CheckpointAgentError(
                    "Checkpoint agent returned invalid progress."
                )
            if total is not None and (
                isinstance(total, bool) or not isinstance(total, int) or total < 1
            ):
                raise checkpoint_agent.CheckpointAgentError(
                    "Checkpoint agent returned invalid progress."
                )
            pct = min(99, int(received * 100 / total)) if total else 0
            if total:
                detail = (
                    f"{status}: {received / (1024 ** 2):.0f} / "
                    f"{total / (1024 ** 2):.0f} MB"
                )
            else:
                detail = f"{status}: {received / (1024 ** 2):.0f} MB"
            db.update_pull_job_progress(job_id, pct, detail)

            if status == "completed":
                if is_cancelled():
                    db.finish_pull_job(job_id, success=False, error=None)
                    return
                synced = comfyui.sync_models()
                installed = any(
                    model.get("backend_model_name") == target_name
                    and bool(model.get("backend_available"))
                    for model in synced
                )
                if not installed:
                    raise checkpoint_agent.CheckpointAgentError(
                        "Checkpoint download completed, but ComfyUI did not report "
                        "the target model as available."
                    )
                if is_cancelled():
                    db.finish_pull_job(job_id, success=False, error=None)
                    return
                db.finish_pull_job(job_id, success=True)
                _logger.info("Checkpoint pull done: %s (job %d)", target_name, job_id)
                return
            if status == "failed":
                raise checkpoint_agent.CheckpointAgentError(
                    checkpoint_agent.job_error_message(remote)
                )
            if status == "canceled":
                db.finish_pull_job(job_id, success=False, error=None)
                return
            _pull_stop.wait(1)
    except Exception as exc:
        if is_cancelled():
            cancel_remote()
            db.finish_pull_job(job_id, success=False, error=None)
            return
        error = str(exc)[:500] or "Checkpoint pull failed."
        _logger.warning("Checkpoint pull failed for job %d: %s", job_id, error)
        db.finish_pull_job(job_id, success=False, error=error)


def _process_single_pull(job):
    """Execute one pull job, updating the DB throughout."""
    if job.get("backend", "ollama") == "comfyui":
        _process_checkpoint_pull(job)
        return

    job_id = job["id"]
    model_name = job["ollama_name"]
    _logger.info("Pull started: %s (job %d)", model_name, job_id)
    _current_pull_cancel.clear()

    def on_progress(pct, detail):
        db.update_pull_job_progress(job_id, pct, detail)

    def is_cancelled():
        return _current_pull_cancel.is_set() or db.is_pull_job_cancelled(job_id)

    success = False
    error = None
    was_cancelled = False

    try:
        result = pull_model(model_name, on_progress=on_progress, is_cancelled=is_cancelled)
        if result is False or is_cancelled():
            was_cancelled = True
        else:
            success = True
    except Exception as exc:
        if is_cancelled():
            was_cancelled = True
        else:
            error = str(exc)[:500]
        _logger.warning("Pull %s (job %d) error: %s", model_name, job_id, exc)

    if was_cancelled:
        _logger.info("Pull cancelled: %s (job %d)", model_name, job_id)
        db.finish_pull_job(job_id, success=False, error=None)
        try:
            delete_ollama_model(model_name)
        except Exception as exc:
            _logger.debug("Could not clean partial download %s: %s", model_name, exc)
    elif success:
        _logger.info("Pull done: %s (job %d)", model_name, job_id)
        db.finish_pull_job(job_id, success=True)
        try:
            sync_models()
        except Exception as exc:
            _logger.warning("Post-pull sync error: %s", exc)
    else:
        _logger.warning("Pull failed: %s (job %d): %s", model_name, job_id, error)
        db.finish_pull_job(job_id, success=False, error=error)


def _pull_worker_loop():
    """Background thread: process one pull job at a time."""
    while not _pull_stop.is_set():
        try:
            job = db.claim_next_pull_job()
            if job:
                _process_single_pull(job)
            else:
                _pull_stop.wait(3)
        except Exception as exc:
            _logger.warning("Pull worker error: %s", exc)
            _pull_stop.wait(5)


def start_background_sync(use_leader_lock=False):
    """Start periodic background threads, optionally only in the elected worker process.

    Two separate threads are started to prevent a slow or hung Ollama request
    in the model-sync path from delaying compute snapshot collection:

    * ollama-sync: polls Ollama for model catalog changes (OLLAMA_SYNC_INTERVAL)
    * snapshot-loop: records compute snapshots and runs retention cleanup
                      (COMPUTE_SNAPSHOT_INTERVAL / hourly)
    """
    global _sync_thread, _snapshot_thread, _pull_thread, _leader_lock
    if _sync_thread and _sync_thread.is_alive():
        return True
    if use_leader_lock:
        candidate = FileLock(os.path.join(config.INSTANCE_DIR, ".background-services.lock"))
        try:
            candidate.acquire(timeout=0)
        except Timeout:
            _logger.info("Background services are owned by another worker")
            return False
        _leader_lock = candidate
    _sync_stop.clear()
    _snapshot_stop.clear()
    _pull_stop.clear()
    _current_pull_cancel.clear()

    # Recover any pull jobs interrupted by a previous unclean shutdown
    try:
        db.reset_stuck_pulls()
    except Exception as exc:
        _logger.warning("Could not reset stuck pull jobs: %s", exc)

    def _model_sync_loop():
        """Thread 1: Ollama model catalog sync."""
        last_sync = 0
        while not _sync_stop.is_set():
            now = time.time()
            if now - last_sync >= config.OLLAMA_SYNC_INTERVAL:
                try:
                    sync_models()
                except Exception as exc:
                    _logger.warning("Model sync error: %s", exc)
                last_sync = time.time()
            _sync_stop.wait(5)

    def _snapshot_loop():
        """Thread 2: compute snapshots + incognito retention cleanup.

        Intentionally decoupled from the model-sync thread so a slow or
        hung Ollama HTTP request in sync_models() cannot starve metric
        collection.
        """
        last_snap = 0
        last_retention = 0
        while not _snapshot_stop.is_set():
            now = time.time()
            if now - last_snap >= config.COMPUTE_SNAPSHOT_INTERVAL:
                try:
                    from services.queue import get_queue_depth
                    depth = get_queue_depth()
                except Exception:
                    depth = 0
                try:
                    collect_compute_snapshot(queue_depth=depth)
                except Exception as exc:
                    _logger.debug("Compute snapshot error: %s", exc)
                last_snap = time.time()

            if now - last_retention >= 3600:
                try:
                    db.purge_expired_incognito_sessions(config.NO_HISTORY_TTL_HOURS)
                except Exception as exc:
                    _logger.debug("No-history retention cleanup error: %s", exc)
                last_retention = time.time()

            _snapshot_stop.wait(5)

    _sync_thread = threading.Thread(target=_model_sync_loop, name="ollama-sync", daemon=True)
    _sync_thread.start()
    _snapshot_thread = threading.Thread(target=_snapshot_loop, name="snapshot-loop", daemon=True)
    _snapshot_thread.start()
    _pull_thread = threading.Thread(target=_pull_worker_loop, name="pull-worker", daemon=True)
    _pull_thread.start()

    # Start the inference health monitor only when using a remote Ollama server
    global _health_thread
    _health_stop.clear()
    if _is_remote_ollama():
        def _health_monitor_loop():
            global _inference_server_down, _inference_failure_count
            threshold = max(1, config.INFERENCE_HEALTH_FAILURES)
            while not _health_stop.is_set():
                alive = _ping_inference_server()
                with _inference_health_lock:
                    if alive:
                        if _inference_server_down:
                            _logger.info("Inference server is back online")
                        _inference_server_down = False
                        _inference_failure_count = 0
                    else:
                        _inference_failure_count += 1
                        if _inference_failure_count >= threshold and not _inference_server_down:
                            _inference_server_down = True
                            _logger.warning(
                                "Inference server declared DOWN after %d consecutive failures "
                                "(mode=%s)",
                                _inference_failure_count,
                                config.INFERENCE_OUTAGE_MODE,
                            )
                _health_stop.wait(config.INFERENCE_HEALTH_INTERVAL)

        _health_thread = threading.Thread(target=_health_monitor_loop, name="inference-health", daemon=True)
        _health_thread.start()
        _logger.info(
            "Inference health monitor started (remote=%s, mode=%s, interval=%ds)",
            config.OLLAMA_BASE_URL, config.INFERENCE_OUTAGE_MODE, config.INFERENCE_HEALTH_INTERVAL,
        )

    _logger.info("Model background sync, snapshot loop, and pull worker started")
    return True


def stop_background_sync():
    global _sync_thread, _snapshot_thread, _pull_thread, _health_thread, _leader_lock
    _sync_stop.set()
    _snapshot_stop.set()
    _pull_stop.set()
    _current_pull_cancel.set()
    _health_stop.set()
    if _sync_thread and _sync_thread.is_alive():
        _sync_thread.join(timeout=10)
    _sync_thread = None
    if _snapshot_thread and _snapshot_thread.is_alive():
        _snapshot_thread.join(timeout=10)
    _snapshot_thread = None
    if _pull_thread and _pull_thread.is_alive():
        _pull_thread.join(timeout=10)
    _pull_thread = None
    if _health_thread and _health_thread.is_alive():
        _health_thread.join(timeout=10)
    _health_thread = None
    if _leader_lock:
        try:
            _leader_lock.release()
        finally:
            _leader_lock = None
