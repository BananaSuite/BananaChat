"""Route admitted inference to a remote worker and relay bounded results."""

import logging
import time

import config
import db

_logger = logging.getLogger("bananachat.dispatcher")

# How often to poll the DB for new chunks while waiting for a worker
_CHUNK_POLL_INTERVAL = 0.1    # 100 ms
# How long to wait for a worker to complete before giving up
_JOB_TIMEOUT = config.GENERATION_TIMEOUT             # 5 minutes
# How often to check for job-level stop / failure while no chunks arrive
_STATUS_CHECK_INTERVAL = 0.5   # 500 ms


# Routing decision

def should_use_worker(is_admin: bool, model_name: str = None) -> bool:
    """Return True if this request should be dispatched to a remote worker.

    Decision criteria:
    - BC_WORKERS_ENABLED must be true (default false).
    - Admin requests bypass the worker pool and always run locally so admins
      are not affected by worker availability.
    - At least one healthy worker must be online.
    """
    if not config.WORKERS_ENABLED:
        return False
    if is_admin:
        return False
    return db.get_available_worker(model_name) is not None


# Job creation

def create_worker_job(model_name: str, messages: list, options=None,
                      priority: int = 2) -> str:
    """Create a pending worker job and return its job_id."""
    job_id = db.create_worker_job(model_name, messages, options, priority)
    _logger.debug("Worker job created: %s (model=%s)", job_id[:8], model_name)
    return job_id


# Chunk streaming relay

def stream_from_worker(job_id: str, stop_ev=None):
    """Announce success only after terminal text and usage are committed.

    Closing or abandoning the generator terminates the job and removes relay
    data. A delayed worker response cannot resurrect a cancelled request.
    """
    last_seq = -1
    terminal_seq = None
    deadline = time.monotonic() + _JOB_TIMEOUT
    completed = False
    try:
        while time.monotonic() < deadline:
            if stop_ev is not None and stop_ev.is_set():
                return
            chunks = db.get_worker_chunks_after(job_id, after_seq=last_seq)
            for chunk in chunks:
                if chunk["seq"] != last_seq + 1 or terminal_seq is not None:
                    raise RuntimeError("Remote inference returned an incomplete response.")
                last_seq = chunk["seq"]
                if chunk["done"]:
                    terminal_seq = last_seq
                if chunk["content"]:
                    yield chunk["content"], False, {}
            job = db.get_worker_job(job_id)
            if not job or job["status"] in {"failed", "timeout"} or job["stop_requested"]:
                raise RuntimeError("Remote inference stopped before completing. Please retry or check worker status.")
            if job["status"] in {"claimed", "streaming"} and job["heartbeat_at"] < time.time() - 60:
                raise RuntimeError("The inference worker lost contact. Please retry or check worker status.")
            if job["status"] == "done" and last_seq == job["next_chunk_seq"] - 1:
                if terminal_seq != last_seq:
                    raise RuntimeError("Remote inference ended without a completion marker.")
                completed = True
                yield "", True, {"prompt_tokens": job["tokens_in"], "completion_tokens": job["tokens_out"],
                                 "finish_reason": job["finish_reason"]}
                return
            if not chunks or terminal_seq is not None:
                if stop_ev is not None:
                    stop_ev.wait(_CHUNK_POLL_INTERVAL) if hasattr(stop_ev, "wait") else time.sleep(_CHUNK_POLL_INTERVAL)
                else:
                    time.sleep(_CHUNK_POLL_INTERVAL)
        raise TimeoutError("The inference worker reached its time limit. Please retry or check worker status.")
    finally:
        try:
            if not completed:
                db.request_stop_worker_job(job_id)
            db.purge_worker_job_chunks(job_id)
        except Exception:
            # Expiry and periodic retention cleanup also cover storage outages.
            _logger.exception("Could not clean remote inference job %s", job_id[:8])
