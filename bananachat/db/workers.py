"""Remote worker registry and job relay storage (SQL only; policy lives in ``services.remote``).

Tables (fixed by earlier releases):

* ``worker_nodes`` - registered worker PCs. ``token_hash`` is the SHA-256 hex
  digest of the bearer token; ``last_heartbeat`` is epoch seconds;
  ``capabilities`` is JSON ``{"models": [...]}``.
* ``worker_jobs`` - one row per request sent to the worker pool. Times are
  epoch seconds (REAL). ``heartbeat_at`` is the last sign of life of the
  worker running the job. ``messages``/``options`` hold the prompt only while
  the job is live; every terminal transition clears them.
* ``worker_job_chunks`` - streamed pieces waiting to be relayed.
"""

from __future__ import annotations

import hashlib

from bananachat import db

ACTIVE = ("claimed", "streaming")
LIVE = ("pending", "claimed", "streaming")
TERMINAL = ("done", "failed", "timeout")

_WORKER_COLUMNS = ("id, name, token_hash, platform, gpu_name, gpu_util, ollama_version, capabilities, "
                   "activity_state, status, last_heartbeat, registered_at")
_JOB_COLUMNS = ("id, worker_id, model_name, messages, options, status, priority, stop_requested, created_at, "
                "claimed_at, done_at, heartbeat_at, tokens_in, tokens_out, error_message, stream_bytes, "
                "next_chunk_seq, finish_reason")


def _marks(values) -> str:
    return ",".join("?" for _ in values)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ----- workers ------------------------------------------------------------------

def insert_worker(worker_id: str, name: str, token_hash: str) -> None:
    db.execute("INSERT INTO worker_nodes (id, name, token_hash, status, registered_at) VALUES (?,?,?,?,?)",
               (worker_id, name, token_hash, "offline", db.now()))


def get(worker_id: str):
    return db.one(f"SELECT {_WORKER_COLUMNS} FROM worker_nodes WHERE id=?", (worker_id,))


def by_token_hash(token_hash: str):
    return db.one(f"SELECT {_WORKER_COLUMNS} FROM worker_nodes WHERE token_hash=?", (token_hash,))


def list_all():
    return db.query(f"SELECT {_WORKER_COLUMNS} FROM worker_nodes ORDER BY registered_at DESC, name")


def online(since: float):
    """Workers not disabled that reported ``online`` after *since*."""
    return db.query(f"SELECT {_WORKER_COLUMNS} FROM worker_nodes WHERE status='online' AND last_heartbeat>=?",
                    (since,))


def set_status(worker_id: str, status: str) -> int:
    return db.execute("UPDATE worker_nodes SET status=? WHERE id=?", (status, worker_id)).rowcount


def delete(worker_id: str) -> int:
    return db.execute("DELETE FROM worker_nodes WHERE id=?", (worker_id,)).rowcount


def record_heartbeat(worker_id: str, *, now: float, status: str, gpu_name, gpu_util, ollama_version,
                     capabilities_json, activity_state, platform) -> None:
    db.execute(
        "UPDATE worker_nodes SET status=?, last_heartbeat=?, gpu_name=COALESCE(?, gpu_name), "
        "gpu_util=?, ollama_version=COALESCE(?, ollama_version), capabilities=COALESCE(?, capabilities), "
        "activity_state=COALESCE(?, activity_state), platform=COALESCE(?, platform) "
        "WHERE id=? AND status<>'disabled'",
        (status, now, gpu_name, gpu_util, ollama_version, capabilities_json, activity_state, platform, worker_id),
    )


def touch(worker_id: str, now: float) -> None:
    """A poll proves the worker is alive and willing to work."""
    db.execute("UPDATE worker_nodes SET status='online', last_heartbeat=? WHERE id=? AND status<>'disabled'",
               (now, worker_id))


def workers_with_active_jobs() -> set[str]:
    rows = db.query(f"SELECT DISTINCT worker_id FROM worker_jobs WHERE status IN ({_marks(ACTIVE)}) "
                    "AND worker_id IS NOT NULL", ACTIVE)
    return {row[0] for row in rows}


# ----- jobs ---------------------------------------------------------------------

def insert_job(job_id: str, model_name: str, messages_json: str, options_json, priority: int, now: float) -> None:
    db.execute("INSERT INTO worker_jobs (id, model_name, messages, options, status, priority, created_at, heartbeat_at) "
               "VALUES (?,?,?,?,'pending',?,?,?)", (job_id, model_name, messages_json, options_json, priority, now, now))


def get_job(job_id: str):
    return db.one(f"SELECT {_JOB_COLUMNS} FROM worker_jobs WHERE id=?", (job_id,))


def next_pending(model_names=None):
    """The next job waiting for a worker (optionally only for these model names)."""
    sql = f"SELECT {_JOB_COLUMNS} FROM worker_jobs WHERE status='pending' AND stop_requested=0"
    params: list = []
    if model_names is not None:
        names = sorted(model_names)
        if not names:
            return None
        sql += f" AND model_name IN ({_marks(names)})"
        params += names
    return db.one(sql + " ORDER BY priority, created_at, id LIMIT 1", params)


def count_pending(model_names) -> int:
    names = sorted(model_names)
    if not names:
        return 0
    return int(db.scalar(f"SELECT COUNT(*) FROM worker_jobs WHERE status='pending' AND stop_requested=0 "
                         f"AND model_name IN ({_marks(names)})", names, 0))


def active_jobs_for_worker(worker_id: str):
    return db.query(f"SELECT {_JOB_COLUMNS} FROM worker_jobs WHERE worker_id=? AND status IN ({_marks(ACTIVE)})",
                    (worker_id, *ACTIVE))


def active_jobs():
    return db.query(f"SELECT {_JOB_COLUMNS} FROM worker_jobs WHERE status IN ({_marks(ACTIVE)})", ACTIVE)


def set_claimed(job_id: str, worker_id: str, now: float) -> int:
    return db.execute("UPDATE worker_jobs SET status='claimed', worker_id=?, claimed_at=?, heartbeat_at=? "
                      "WHERE id=? AND status='pending' AND stop_requested=0",
                      (worker_id, now, now, job_id)).rowcount


def requeue(job_id: str, now: float) -> int:
    """Give a claimed job that produced nothing back to the pool."""
    return db.execute(
        "UPDATE worker_jobs SET status='pending', worker_id=NULL, claimed_at=NULL, heartbeat_at=? "
        f"WHERE id=? AND status IN ({_marks(ACTIVE)}) AND stop_requested=0 AND next_chunk_seq=0",
        (now, job_id, *ACTIVE)).rowcount


def renew_lease(worker_id: str, now: float) -> int:
    return db.execute(f"UPDATE worker_jobs SET heartbeat_at=? WHERE worker_id=? AND status IN ({_marks(ACTIVE)}) "
                      "AND stop_requested=0", (now, worker_id, *ACTIVE)).rowcount


def finish(job_id: str, status: str, now: float, *, error: str | None = None, tokens_in: int = 0,
           tokens_out: int = 0, finish_reason: str = "stop", only_from=LIVE) -> int:
    """Move a job to a terminal state and forget its prompt. Returns 1 when it changed."""
    return db.execute(
        "UPDATE worker_jobs SET status=?, stop_requested=?, done_at=?, error_message=?, tokens_in=?, tokens_out=?, "
        f"finish_reason=?, messages='[]', options=NULL WHERE id=? AND status IN ({_marks(only_from)})",
        (status, 0 if status == "done" else 1, now, error, tokens_in, tokens_out, finish_reason, job_id, *only_from),
    ).rowcount


def finish_worker_jobs(worker_id: str, error: str, now: float) -> int:
    return db.execute(
        "UPDATE worker_jobs SET status='failed', stop_requested=1, done_at=?, error_message=?, messages='[]', "
        f"options=NULL WHERE worker_id=? AND status IN ({_marks(ACTIVE)})", (now, error, worker_id, *ACTIVE)).rowcount


def advance(job_id: str, size: int, now: float) -> None:
    db.execute("UPDATE worker_jobs SET status='streaming', heartbeat_at=?, stream_bytes=stream_bytes+?, "
               "next_chunk_seq=next_chunk_seq+1 WHERE id=?", (now, size, job_id))


def expire_pending(before: float, now: float) -> int:
    return db.execute(
        "UPDATE worker_jobs SET status='timeout', stop_requested=1, done_at=?, error_message=?, messages='[]', "
        "options=NULL WHERE status='pending' AND heartbeat_at<?",
        (now, "No worker took the job in time.", before)).rowcount


def expire_created_before(before: float, now: float) -> int:
    return db.execute(
        "UPDATE worker_jobs SET status='timeout', stop_requested=1, done_at=?, error_message=?, messages='[]', "
        f"options=NULL WHERE status IN ({_marks(LIVE)}) AND created_at<?",
        (now, "The job exceeded the generation time limit.", *LIVE, before)).rowcount


def scrub_terminal() -> int:
    """Belt and braces: no prompt survives a terminal state."""
    return db.execute(f"UPDATE worker_jobs SET messages='[]', options=NULL WHERE status IN ({_marks(TERMINAL)}) "
                      "AND (messages<>'[]' OR options IS NOT NULL)", TERMINAL).rowcount


def delete_terminal_before(before: float) -> int:
    return db.execute(f"DELETE FROM worker_jobs WHERE status IN ({_marks(TERMINAL)}) AND COALESCE(done_at, created_at)<?",
                      (*TERMINAL, before)).rowcount


# ----- chunks -------------------------------------------------------------------

def chunk(job_id: str, seq: int):
    return db.one("SELECT seq, content, done FROM worker_job_chunks WHERE job_id=? AND seq=?", (job_id, seq))


def insert_chunk(job_id: str, seq: int, content: str, done: bool, now: float) -> None:
    db.execute("INSERT INTO worker_job_chunks (job_id, seq, content, done, created_at) VALUES (?,?,?,?,?)",
               (job_id, seq, content, int(done), now))


def chunks_after(job_id: str, seq: int, limit: int = 256):
    return db.query("SELECT seq, content, done FROM worker_job_chunks WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (job_id, seq, limit))


def delete_chunks_before(job_id: str, seq: int) -> None:
    db.execute("DELETE FROM worker_job_chunks WHERE job_id=? AND seq<?", (job_id, seq))


def delete_chunks(job_id: str) -> None:
    db.execute("DELETE FROM worker_job_chunks WHERE job_id=?", (job_id,))


def delete_orphan_chunks(before: float) -> int:
    """Chunks of jobs that ended before *before* (the relay normally removes its own)."""
    return db.execute(f"DELETE FROM worker_job_chunks WHERE job_id IN (SELECT id FROM worker_jobs WHERE status IN "
                      f"({_marks(TERMINAL)}) AND COALESCE(done_at, created_at)<?) "
                      "OR job_id NOT IN (SELECT id FROM worker_jobs)", (*TERMINAL, before)).rowcount


def job_counts() -> dict:
    rows = db.query("SELECT status, COUNT(*) FROM worker_jobs GROUP BY status")
    return {row[0]: row[1] for row in rows}
