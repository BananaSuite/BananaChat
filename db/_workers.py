"""Worker node registry and remote job management.

Supports the 'personal PC worker' deployment: a daemon running on a
user's home or gaming PC polls for inference jobs, runs them locally via
Ollama, and streams the chunks back to this server.

All coordination is through the shared SQLite database so any Gunicorn
worker process can dispatch to or query remote workers transparently.
"""

import hashlib
import json
import logging
import time
import uuid

from ._connection import get_db_context

_logger = logging.getLogger("bananachat.workers")

# A worker that has not sent a heartbeat within this window is treated as
# offline for dispatch purposes.  Its status column is not updated here;
# callers should read the effective status via list_workers() which applies
# the staleness check in Python.
WORKER_HEARTBEAT_LEASE = 30   # seconds


def hash_worker_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_worker(name: str, platform: str = None) -> tuple:
    """Register a new worker. Returns (worker_id, raw_token).

    The raw_token is shown to the admin exactly once; only its hash is
    stored in the database.
    """
    worker_id = uuid.uuid4().hex
    raw_token = uuid.uuid4().hex + uuid.uuid4().hex  # 64-char random token
    token_hash = hash_worker_token(raw_token)
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO worker_nodes(id, name, token_hash, platform) "
            "VALUES(?,?,?,?)",
            (worker_id, name, token_hash, platform),
        )
        conn.commit()
    _logger.info("Worker registered: %s (%s)", name, worker_id[:8])
    return worker_id, raw_token


def get_worker_by_token(raw_token: str):
    """Look up a worker by its bearer token. Returns row or None."""
    token_hash = hash_worker_token(raw_token)
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM worker_nodes WHERE token_hash=?", (token_hash,)
        ).fetchone()


def get_worker_by_id(worker_id: str):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM worker_nodes WHERE id=?", (worker_id,)
        ).fetchone()


def update_worker_heartbeat(worker_id: str, status: str, gpu_name: str = None,
                            ollama_version: str = None, capabilities=None,
                            activity_state: str = None, gpu_util: float = None):
    """Called by the worker on each heartbeat to update its presence and state."""
    with get_db_context() as conn:
        conn.execute(
            "UPDATE worker_nodes "
            "SET status=?, last_heartbeat=?, gpu_name=?, ollama_version=?, "
            "    capabilities=COALESCE(?, capabilities), activity_state=COALESCE(?, activity_state), gpu_util=COALESCE(?, gpu_util) "
            "WHERE id=? AND status<>'disabled'",
            (
                status,
                time.time(),
                gpu_name,
                ollama_version,
                json.dumps(capabilities) if capabilities is not None else None,
                activity_state,
                gpu_util,
                worker_id,
            ),
        )
        conn.commit()


def set_worker_disabled(worker_id: str, disabled: bool):
    status = "disabled" if disabled else "offline"
    with get_db_context() as conn:
        conn.execute(
            "UPDATE worker_nodes SET status=? WHERE id=?", (status, worker_id)
        )
        if disabled:
            conn.execute("UPDATE worker_jobs SET status='failed',stop_requested=1,done_at=?,error_message='Worker disabled' WHERE worker_id=? AND status IN ('claimed','streaming')", (time.time(), worker_id))
        conn.commit()


def delete_worker(worker_id: str):
    with get_db_context() as conn:
        conn.execute("UPDATE worker_jobs SET status='failed',stop_requested=1,done_at=?,error_message='Worker removed' WHERE worker_id=? AND status IN ('claimed','streaming')", (time.time(), worker_id))
        conn.execute("DELETE FROM worker_nodes WHERE id=?", (worker_id,))
        conn.commit()


def list_workers():
    """Return all workers with effective (staleness-adjusted) status."""
    cutoff = time.time() - WORKER_HEARTBEAT_LEASE
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_nodes ORDER BY registered_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        # A worker that has stopped heartbeating should appear offline even if
        # its last status write was "online" or "busy".
        if d["status"] not in ("disabled",):
            if not d.get("last_heartbeat") or d["last_heartbeat"] < cutoff:
                d["status"] = "offline"
        if d.get("capabilities"):
            try:
                d["capabilities"] = json.loads(d["capabilities"])
            except (TypeError, ValueError):
                d["capabilities"] = {}
        result.append(d)
    return result


def get_available_worker(model_name: str = None):
    """Return the best available (online, not busy) worker for a job.

    When model_name is provided, prefer workers that have advertised that
    model in their capabilities.  Falls back to any online worker since the
    model may just not have been reported yet.
    """
    cutoff = time.time() - WORKER_HEARTBEAT_LEASE
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_nodes "
            "WHERE status='online' AND last_heartbeat>? "
            "ORDER BY last_heartbeat DESC",
            (cutoff,),
        ).fetchall()
    if not rows:
        return None

    if model_name:
        # Prefer workers that have explicitly advertised the model.
        for row in rows:
            caps = {}
            if row["capabilities"]:
                try:
                    caps = json.loads(row["capabilities"])
                except (TypeError, ValueError):
                    pass
            if model_name in caps.get("models", []):
                return row
        # Fallback: any online worker (it may have the model even if not listed)

    return rows[0]
