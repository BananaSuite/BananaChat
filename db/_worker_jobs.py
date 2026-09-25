"""Bounded, ordered remote inference with transactional worker ownership."""

import json
import time
import uuid

import config
from ._connection import get_db_context

JOB_ACTIVITY_LEASE = 60
MAX_CHUNK_BYTES = 16 * 1024
MAX_CHUNKS = 32768
CHUNK_BATCH_SIZE = 128
_ACTIVE = ("claimed", "streaming")


def create_worker_job(model_name, messages, options=None, priority=2):
    """Create a job after its caller has reserved inference capacity."""
    job_id, now = uuid.uuid4().hex, time.time()
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO worker_jobs(id,model_name,messages,options,priority,created_at,heartbeat_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, model_name, json.dumps(messages), json.dumps(options) if options else None, priority, now, now),
        )
        conn.commit()
    return job_id


def _fail(conn, job_id, message):
    conn.execute(
        "UPDATE worker_jobs SET status='failed',stop_requested=1,done_at=?,error_message=? "
        "WHERE id=? AND status IN ('pending','claimed','streaming')",
        (time.time(), message, job_id),
    )


def claim_next_worker_job(worker_id, available_models=None):
    """Claim one eligible job per worker; idle polls do not acquire a write lock."""
    models = list(available_models or [])
    if len(models) > 512 or any(not isinstance(m, str) or len(m) > 512 for m in models):
        raise ValueError("Invalid worker model inventory")
    query = "SELECT * FROM worker_jobs WHERE status='pending' AND stop_requested=0 AND created_at>=?"
    params = [time.time() - config.GENERATION_TIMEOUT]
    if models:
        query += " AND model_name IN (" + ",".join("?" for _ in models) + ")"
        params += models
    query += " ORDER BY priority,created_at,id LIMIT 1"
    with get_db_context() as conn:
        if conn.execute(query, params).fetchone() is None:
            return None
        conn.execute("BEGIN IMMEDIATE")
        worker = conn.execute("SELECT status FROM worker_nodes WHERE id=?", (worker_id,)).fetchone()
        if not worker or worker["status"] == "disabled":
            return None
        now = time.time()
        conn.execute(
            "UPDATE worker_jobs SET status='timeout',stop_requested=1,done_at=? "
            "WHERE status IN ('claimed','streaming') AND heartbeat_at<?", (now, now - JOB_ACTIVITY_LEASE),
        )
        if conn.execute("SELECT 1 FROM worker_jobs WHERE worker_id=? AND status IN ('claimed','streaming')", (worker_id,)).fetchone():
            conn.commit()
            return None
        row = conn.execute(query, params).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("UPDATE worker_jobs SET status='claimed',worker_id=?,claimed_at=?,heartbeat_at=? WHERE id=?",
                     (worker_id, now, now, row["id"]))
        conn.commit()
        return {**dict(row), "status": "claimed", "worker_id": worker_id, "claimed_at": now, "heartbeat_at": now}


def get_worker_job(job_id):
    with get_db_context() as conn:
        row = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def _owned(conn, job, worker_id):
    if not job or job["status"] not in _ACTIVE or job["stop_requested"]:
        return False
    if worker_id is not None and job["worker_id"] != worker_id:
        return False
    if job["heartbeat_at"] < time.time() - JOB_ACTIVITY_LEASE:
        _fail(conn, job["id"], "Worker contact expired")
        return False
    worker = conn.execute("SELECT status FROM worker_nodes WHERE id=?", (job["worker_id"],)).fetchone()
    return worker is not None and worker["status"] != "disabled"


def finish_worker_job(job_id, tokens_in=0, tokens_out=0, error=None, *, worker_id=None, finish_reason="stop"):
    """Commit completion only for its live owner and a received terminal chunk."""
    for count in (tokens_in, tokens_out):
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 2**31 - 1:
            raise ValueError("Token counts must be nonnegative integers")
    if finish_reason not in {"stop", "length"}:
        raise ValueError("Invalid finish reason")
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or (worker_id is not None and job["worker_id"] != worker_id):
            return False
        if error:
            _fail(conn, job_id, str(error)[:500])
            conn.commit()
            return job["status"] in ("pending", *_ACTIVE)
        # A retried completion is harmless, including after the relay is purged.
        if job["status"] == "done":
            return job["tokens_in"] == tokens_in and job["tokens_out"] == tokens_out and job["finish_reason"] == finish_reason
        if not _owned(conn, job, worker_id):
            conn.commit()
            return False
        terminal = conn.execute("SELECT done FROM worker_job_chunks WHERE job_id=? AND seq=?",
                                (job_id, job["next_chunk_seq"] - 1)).fetchone()
        if not terminal or not terminal["done"]:
            _fail(conn, job_id, "Worker ended without a completion marker")
            conn.commit()
            return False
        conn.execute(
            "UPDATE worker_jobs SET status='done',done_at=?,tokens_in=?,tokens_out=?,finish_reason=?,error_message=NULL WHERE id=?",
            (time.time(), tokens_in, tokens_out, finish_reason, job_id),
        )
        conn.commit()
        return True


def request_stop_worker_job(job_id):
    """Cancellation is terminal, so late chunks/completions cannot revive work."""
    with get_db_context() as conn:
        _fail(conn, job_id, "Generation stopped")
        conn.commit()


def should_stop_worker_job(job_id):
    with get_db_context() as conn:
        job = conn.execute("SELECT status,stop_requested,heartbeat_at FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        return not job or job["status"] not in _ACTIVE or bool(job["stop_requested"]) or job["heartbeat_at"] < time.time() - JOB_ACTIVITY_LEASE


def add_worker_chunk(job_id, seq, content, done, *, worker_id=None):
    """Accept ordered chunks once, with UTF-8 byte and row budgets per job."""
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < MAX_CHUNKS:
        raise ValueError("Invalid chunk sequence")
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CHUNK_BYTES or not isinstance(done, bool):
        raise ValueError("Invalid or oversized chunk")
    size = len(content.encode("utf-8"))
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM worker_jobs WHERE id=?", (job_id,)).fetchone()
        if not _owned(conn, job, worker_id):
            conn.commit()
            return False
        old = conn.execute("SELECT content,done FROM worker_job_chunks WHERE job_id=? AND seq=?", (job_id, seq)).fetchone()
        if old and old["content"] == content and bool(old["done"]) == done:
            return True
        terminal = conn.execute("SELECT 1 FROM worker_job_chunks WHERE job_id=? AND seq=? AND done=1",
                                (job_id, seq - 1)).fetchone()
        if old or seq != job["next_chunk_seq"] or terminal or job["stream_bytes"] + size > config.CHAT_MAX_RESPONSE_BYTES:
            _fail(conn, job_id, "Worker response exceeded its limits or arrived out of order")
            conn.commit()
            return False
        now = time.time()
        conn.execute("INSERT INTO worker_job_chunks(job_id,seq,content,done,created_at) VALUES(?,?,?,?,?)",
                     (job_id, seq, content, int(done), now))
        conn.execute("UPDATE worker_jobs SET status='streaming',heartbeat_at=?,stream_bytes=stream_bytes+?,next_chunk_seq=next_chunk_seq+1 WHERE id=?",
                     (now, size, job_id))
        conn.commit()
        return True


def get_worker_chunks_after(job_id, after_seq):
    with get_db_context() as conn:
        return conn.execute("SELECT seq,content,done FROM worker_job_chunks WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                            (job_id, after_seq, CHUNK_BATCH_SIZE)).fetchall()


def purge_worker_job_chunks(job_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM worker_job_chunks WHERE job_id=?", (job_id,))
        conn.commit()


def purge_old_worker_jobs(max_age_seconds=3600):
    """Expire orphaned requests and remove prompt/relay data after retention."""
    now = time.time()
    with get_db_context() as conn:
        conn.execute("UPDATE worker_jobs SET status='timeout',stop_requested=1,done_at=? WHERE status IN ('pending','claimed','streaming') AND created_at<?",
                     (now, now - config.GENERATION_TIMEOUT))
        conn.execute("DELETE FROM worker_jobs WHERE status IN ('done','failed','timeout') AND done_at<?", (now - max_age_seconds,))
        conn.commit()
