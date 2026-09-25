"""BananaChat Worker daemon main loop.

Architecture
------------
Three concurrent threads cooperate via simple shared state:

  heartbeat_thread: POSTs /worker/v1/heartbeat every HEARTBEAT_INTERVAL
                     seconds to keep the worker visible in the admin panel.

  activity_thread: Refreshes activity state (idle/light/active/gaming)
                     and adjusts process priority every ACTIVITY_CHECK_INTERVAL
                     seconds.

  main loop: Polls the server for jobs.  Before each poll it checks
                     the activity state.  If gaming, it sleeps and skips the
                     poll.  Otherwise it claims and runs the job.

Job execution flow
------------------
  1. poll_for_job(): long-polls server (blocks up to 28 s)
  2. ensure_ollama_running: start Ollama if not already up
  3. generate_stream(): run inference on local Ollama
  4. submit_chunk(): POST each chunk to server
  5. complete_job(): POST final token counts
  6. Update last_job_time: used to auto-stop Ollama after idle timeout
"""

import logging
import sys
import threading
import time

import config
import activity
import ollama_mgr
import resources
import server_client

_logger = logging.getLogger("bananachat.worker.daemon")

# Shared state: written by activity_thread, read by main loop and heartbeat.
_state_lock            = threading.Lock()
_activity_state: str   = "idle"    # idle / light / active / gaming
_gpu_name: str | None  = None
_ollama_version: str | None = None
_available_models: list[str] = []

_last_job_time: float  = 0.0       # monotonic timestamp of last completed job
_stop_event            = threading.Event()


# Heartbeat thread

def _heartbeat_loop():
    """Send periodic heartbeats to keep this worker visible on the server."""
    while not _stop_event.wait(config.HEARTBEAT_INTERVAL):
        with _state_lock:
            state  = _activity_state
            gpu    = _gpu_name
            ver    = _ollama_version
            models = list(_available_models)

        gpu_util = activity.get_gpu_utilisation()
        ok = server_client.send_heartbeat(
            status="online" if state != "gaming" else "busy",
            gpu_name=gpu,
            ollama_version=ver,
            capabilities={"models": models},
            activity_state=state,
            gpu_util=gpu_util,
        )
        if not ok:
            _logger.debug("Heartbeat not acknowledged (server may be unreachable)")


# Activity monitoring thread

def _activity_loop():
    """Refresh activity state and adjust process priority on schedule."""
    global _activity_state, _gpu_name, _ollama_version, _available_models

    while not _stop_event.wait(config.ACTIVITY_CHECK_INTERVAL):
        state     = activity.get_activity_state()
        gpu_name  = activity.get_gpu_name()
        managed_pid = ollama_mgr.get_managed_pid()

        # Refresh model list while Ollama is running
        models = []
        if ollama_mgr.is_alive():
            models = ollama_mgr.list_models()
            ver    = ollama_mgr.get_version()
        else:
            ver = None

        with _state_lock:
            _activity_state  = state
            if gpu_name:
                _gpu_name = gpu_name
            if ver:
                _ollama_version = ver
            if models:
                _available_models = models

        resources.apply_for_state(state, ollama_pid=managed_pid)

        _logger.debug(
            "Activity: %s | GPU util: %s%%",
            state,
            f"{activity.get_gpu_utilisation():.0f}" if activity.get_gpu_utilisation() is not None else "n/a",
        )


# Job execution

def _run_job(job: dict):
    """Execute one inference job and stream results back to the server."""
    global _last_job_time

    job_id     = job["job_id"]
    model      = job["model"]
    messages   = job["messages"]
    options    = job.get("options")

    _logger.info("Starting job %s  model=%s  msgs=%d", job_id[:8], model, len(messages))

    seq, tokens_in, tokens_out = 0, 0, 0
    completed = False
    pending = ""
    last_send = time.monotonic()
    stream = None
    try:
        if not ollama_mgr.ensure_running():
            raise RuntimeError("Ollama could not be started")
        stream = ollama_mgr.generate_stream(model, messages, options)
        for content, done, usage in stream:
            if not isinstance(content, str):
                raise RuntimeError("Ollama returned invalid text")
            pending += content
            while pending and (len(pending) >= 4096 or done or time.monotonic() - last_send >= 0.1):
                piece, pending = pending[:4096], pending[4096:]
                if not server_client.submit_chunk(job_id, seq, piece, False):
                    raise RuntimeError("Server stopped accepting inference output")
                seq += 1
                last_send = time.monotonic()
            if done:
                if not server_client.submit_chunk(job_id, seq, "", True):
                    raise RuntimeError("Server stopped accepting inference output")
                tokens_in, tokens_out = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                completed = server_client.complete_job(job_id, tokens_in, tokens_out, usage.get("finish_reason", "stop"))
                break
        if not completed:
            raise RuntimeError("Inference ended before its result was accepted")
        _logger.info("Job %s done (%d input / %d output tokens)", job_id[:8], tokens_in, tokens_out)
    except Exception as exc:
        _logger.error("Job %s failed: %s", job_id[:8], exc)
        server_client.fail_job(job_id, str(exc))
    finally:
        if stream is not None:
            stream.close()

    _last_job_time = time.monotonic()


# Ollama idle shutdown

def _maybe_stop_ollama_idle():
    """Stop Ollama if it has been idle longer than OLLAMA_IDLE_TIMEOUT."""
    if config.OLLAMA_IDLE_TIMEOUT <= 0:
        return
    if _last_job_time == 0.0:
        return   # Never ran a job yet: Ollama wasn't started by us
    idle_secs = time.monotonic() - _last_job_time
    if idle_secs >= config.OLLAMA_IDLE_TIMEOUT:
        if ollama_mgr.get_managed_pid() is not None:
            _logger.info(
                "Ollama idle for %.0f s (threshold %d s): stopping to free VRAM",
                idle_secs, config.OLLAMA_IDLE_TIMEOUT,
            )
            ollama_mgr.stop_managed()


# Main daemon entry point

def run():
    """Start all threads and enter the job-polling loop."""
    try:
        config.validate()
    except ValueError as exc:
        _logger.error("Configuration error: %s", exc)
        sys.exit(1)

    _logger.info(
        "BananaChat Worker starting  name=%s  server=%s",
        config.WORKER_NAME, config.SERVER_URL,
    )

    server_client.send_heartbeat(status="online")

    heartbeat_t = threading.Thread(
        target=_heartbeat_loop, name="heartbeat", daemon=True
    )
    activity_t  = threading.Thread(
        target=_activity_loop, name="activity",  daemon=True
    )
    heartbeat_t.start()
    activity_t.start()

    _logger.info("Worker daemon running: polling for jobs")

    try:
        while not _stop_event.is_set():
            # Check activity state before attempting to claim a job
            with _state_lock:
                state  = _activity_state
                models = list(_available_models)

            if state == "gaming":
                _logger.debug("GPU busy (gaming/rendering): skipping job poll")
                time.sleep(5)
                continue

            _maybe_stop_ollama_idle()

            # Long-poll for a job (blocks up to 28 s on the server)
            job = server_client.poll_for_job(available_models=models or None)

            if job is None:
                # No job during this poll window: small gap then poll again
                if not _stop_event.wait(config.POLL_GAP_SECONDS):
                    continue
                break

            # Re-check state in case user started gaming while we were polling
            with _state_lock:
                state = _activity_state
            if state == "gaming":
                _logger.info(
                    "GPU became busy while polling: deferring job %s back to server",
                    job.get("job_id", "?")[:8],
                )
                # The job will time out and be retried when GPU is free again.
                # We do NOT call fail_job here; server-side timeout handles it.
                time.sleep(5)
                continue

            _run_job(job)
            time.sleep(config.POLL_GAP_SECONDS)

    except KeyboardInterrupt:
        _logger.info("Interrupted: shutting down")
    finally:
        _stop_event.set()
        server_client.send_heartbeat(status="offline")
        ollama_mgr.stop_managed()
        _logger.info("Worker daemon stopped")


def stop():
    """Signal the daemon to stop cleanly (for use by the service manager)."""
    _stop_event.set()
