"""Model download jobs (``model_pull_jobs``).

Statuses: ``queued`` -> ``pulling`` -> ``done`` | ``failed`` | ``cancelled``.
Queued jobs run in ``queue_position`` order, skipping ``paused`` ones and
those waiting for a retry (``next_attempt_at``); at most the configured number
run at once (see ``services.pulls``). A partial unique index allows one
active job per model across processes. A job interrupted by a shutdown or a
pause goes back to ``queued``; only an administrator's cancel ends it as
``cancelled``. Claiming a job gives it a new ``claim_token``; what the run
writes afterwards (progress, retry, re-queue, the outcome) names that token, so
a thread left over from a previous leader cannot change a job that was claimed
again. Validation of names and checkpoint recipes lives in ``services.pulls``.
"""

from __future__ import annotations

import secrets
import sqlite3

from bananachat import db

ACTIVE = ("queued", "pulling")
FINISHED = ("done", "failed", "cancelled")
RECIPE_FIELDS = ("repo_id", "source_filename", "revision", "target_name", "expected_sha256", "expected_size")


def get(job_id):
    return db.one("SELECT * FROM model_pull_jobs WHERE id=?", (job_id,))


def list_jobs(limit: int = 50):
    """Active jobs in queue order (running first), then finished ones, newest first."""
    return db.query("SELECT j.*, u.username AS requested_by_name FROM model_pull_jobs j "
                    "LEFT JOIN users u ON u.id=j.requested_by ORDER BY j.status IN ('queued', 'pulling') DESC, "
                    "j.status='pulling' DESC, CASE WHEN j.status IN ('queued', 'pulling') THEN j.queue_position END, "
                    "j.id DESC LIMIT ?", (limit,))


def active_jobs():
    return db.query("SELECT * FROM model_pull_jobs WHERE status IN ('queued', 'pulling') "
                    "ORDER BY status='pulling' DESC, queue_position, id")


def active_for(name: str, backend: str = "ollama"):
    return db.one("SELECT * FROM model_pull_jobs WHERE backend=? AND ollama_name=? AND status IN ('queued', 'pulling')",
                  (backend, name))


def active_count() -> int:
    return db.scalar("SELECT COUNT(*) FROM model_pull_jobs WHERE status IN ('queued', 'pulling')", default=0)


class AlreadyActive(ValueError):
    """A download of this model is already queued or running (``job_id``)."""

    def __init__(self, name: str, status: str, job_id: int):
        super().__init__(f"A download of {name} is already {status}.")
        self.job_id = job_id


def enqueue(name: str, requested_by: str | None, *, backend: str = "ollama", source: str = "", **recipe) -> int:
    """Queue a download at the end of the queue; raises :class:`AlreadyActive` for a second active job."""
    unknown = set(recipe) - set(RECIPE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown download fields: {', '.join(sorted(unknown))}")
    values = {key: recipe.get(key) for key in RECIPE_FIELDS}
    try:
        with db.transaction():
            existing = active_for(name, backend)
            if existing is not None:
                raise AlreadyActive(name, existing["status"], existing["id"])
            cursor = db.execute(
                "INSERT INTO model_pull_jobs (ollama_name, backend, repo_id, source_filename, revision, target_name, "
                "expected_sha256, expected_size, idempotency_key, status, progress_pct, progress_detail, requested_by, "
                "created_at, source, queue_position) VALUES (?,?,?,?,?,?,?,?,?, 'queued', 0, '', ?, ?, ?, "
                "(SELECT COALESCE(MAX(queue_position), 0) + 1 FROM model_pull_jobs))",
                (name, backend, *values.values(), secrets.token_hex(16), requested_by, db.now(), source[:20]))
    except sqlite3.IntegrityError:
        existing = active_for(name, backend)
        if existing is None:
            raise
        raise AlreadyActive(name, existing["status"], existing["id"]) from None
    return cursor.lastrowid


def claim_next(limit: int = 1, *, skip_backend: str | None = None):
    """Atomically start the first runnable queued job while fewer than *limit* run (not of *skip_backend*)."""
    with db.transaction():
        if db.scalar("SELECT COUNT(*) FROM model_pull_jobs WHERE status='pulling'", default=0) >= max(1, limit):
            return None
        job = db.one("SELECT id FROM model_pull_jobs WHERE status='queued' AND paused=0 AND "
                     "(next_attempt_at IS NULL OR next_attempt_at<=?) AND backend IS NOT ? "
                     "ORDER BY queue_position, id LIMIT 1", (db.now(), skip_backend))
        if job is None:
            return None
        db.execute("UPDATE model_pull_jobs SET status='pulling', started_at=?, error_message=NULL, progress_at=?, "
                   "claim_token=? WHERE id=?", (db.now(), db.now(), secrets.token_hex(16), job["id"]))
    return get(job["id"])


_OWNED = " AND (? IS NULL OR claim_token=?)"


def update_progress(job_id: int, percent: int, detail: str, *, completed: int | None = None,
                    total: int | None = None, owner: str | None = None) -> None:
    db.execute("UPDATE model_pull_jobs SET progress_pct=?, progress_detail=?, bytes_completed=COALESCE(?, "
               "bytes_completed), bytes_total=COALESCE(?, bytes_total), progress_at=? WHERE id=? AND status='pulling'"
               + _OWNED, (max(0, min(100, int(percent))), str(detail)[:500], completed, total, db.now(), job_id,
                          owner, owner))


def schedule_retry(job_id: int, attempts: int, next_attempt_at: str, error: str, detail: str, *,
                   owner: str | None = None) -> bool:
    """Put a running job back in the queue to try again later."""
    return db.execute("UPDATE model_pull_jobs SET status='queued', attempts=?, next_attempt_at=?, last_error=?, "
                      "progress_detail=? WHERE id=? AND status='pulling'" + _OWNED,
                      (attempts, next_attempt_at, str(error)[:500], str(detail)[:500], job_id, owner,
                       owner)).rowcount == 1


def state(job_id: int):
    return db.one("SELECT status, paused, cleanup_partial, claim_token FROM model_pull_jobs WHERE id=?", (job_id,))


def set_paused(job_id: int, paused: bool) -> bool:
    return db.execute("UPDATE model_pull_jobs SET paused=? WHERE id=? AND status IN ('queued', 'pulling')",
                      (1 if paused else 0, job_id)).rowcount == 1


def set_cleanup(job_id: int, cleanup: bool) -> None:
    db.execute("UPDATE model_pull_jobs SET cleanup_partial=? WHERE id=?", (1 if cleanup else 0, job_id))


def move(job_id: int, direction: str) -> bool:
    """Move a queued job ``up``, ``down``, to the ``top`` or the ``bottom`` of the queue."""
    with db.transaction():
        ids = [row["id"] for row in db.query("SELECT id FROM model_pull_jobs WHERE status='queued' "
                                             "ORDER BY queue_position, id")]
        if job_id not in ids:
            return False
        index = ids.index(job_id)
        ids.pop(index)
        target = {"up": max(0, index - 1), "down": min(len(ids), index + 1), "top": 0, "bottom": len(ids)}[direction]
        ids.insert(target, job_id)
        base = db.scalar("SELECT COALESCE(MIN(queue_position), 1) FROM model_pull_jobs WHERE status='queued'",
                         default=1)
        for offset, item in enumerate(ids):
            db.execute("UPDATE model_pull_jobs SET queue_position=? WHERE id=?", (base + offset, item))
    return True


def set_remote_id(job_id: int, remote_job_id: str, *, owner: str | None = None) -> None:
    db.execute("UPDATE model_pull_jobs SET remote_job_id=? WHERE id=? AND status='pulling'" + _OWNED,
               (remote_job_id, job_id, owner, owner))


def status(job_id: int) -> str | None:
    return db.scalar("SELECT status FROM model_pull_jobs WHERE id=?", (job_id,))


def finish(job_id: int, outcome: str, error: str | None = None, *, digest: str | None = None,
           owner: str | None = None) -> bool:
    """End a running job as done/failed/cancelled. False when it was no longer running (or claimed by another run
    than *owner*)."""
    if outcome not in FINISHED:
        raise ValueError("Unknown outcome.")
    progress = ", progress_pct=100" if outcome == "done" else ""
    return db.execute(
        f"UPDATE model_pull_jobs SET status=?, error_message=?, finished_at=?, next_attempt_at=NULL, "
        f"digest=COALESCE(?, digest){progress} WHERE id=? AND status IN ('queued', 'pulling')" + _OWNED,
        (outcome, (error or None) and str(error)[:500], db.now(), digest, job_id, owner, owner)).rowcount == 1


def requeue(job_id: int, note: str = "Interrupted by a server restart; it will resume.", *,
            owner: str | None = None) -> bool:
    """Put a running job back in the queue (a shutdown or a pause is not a cancellation)."""
    return db.execute("UPDATE model_pull_jobs SET status='queued', progress_detail=?, finished_at=NULL "
                      "WHERE id=? AND status='pulling'" + _OWNED, (note[:500], job_id, owner, owner)).rowcount == 1


def cancel(job_id: int, error: str | None = None) -> bool:
    return db.execute("UPDATE model_pull_jobs SET status='cancelled', error_message=?, finished_at=?, "
                      "next_attempt_at=NULL WHERE id=? AND status IN ('queued', 'pulling')",
                      (error, db.now(), job_id)).rowcount == 1


def summary() -> dict:
    """Counts and overall progress of the active jobs."""
    row = db.one("SELECT COUNT(*) AS active, COALESCE(SUM(status='pulling'), 0) AS pulling, "
                 "COALESCE(SUM(status='queued' AND paused=1), 0) AS paused, "
                 "COALESCE(SUM(status='queued' AND next_attempt_at IS NOT NULL), 0) AS retrying, "
                 "COALESCE(SUM(progress_pct), 0) AS percent_sum, "
                 "COALESCE(SUM(CASE WHEN bytes_total>0 THEN bytes_total END), 0) AS bytes_total, "
                 "COALESCE(SUM(CASE WHEN bytes_total>0 THEN MIN(bytes_completed, bytes_total) END), 0) AS bytes_done "
                 "FROM model_pull_jobs WHERE status IN ('queued', 'pulling')")
    active = row["active"]
    return {"active": active, "pulling": row["pulling"], "queued": active - row["pulling"], "paused": row["paused"],
            "retrying": row["retrying"], "percent": round(row["percent_sum"] / active) if active else 0,
            "bytes_total": row["bytes_total"], "bytes_done": row["bytes_done"]}


def reset_stuck() -> int:
    """After a restart, jobs left ``pulling`` by a stopped process resume from the queue."""
    return db.execute("UPDATE model_pull_jobs SET status='queued', progress_detail=?, claim_token=NULL "
                      "WHERE status='pulling'", ("Interrupted by a server restart; it will resume.",)).rowcount


def delete_finished(job_id: int) -> bool:
    return db.execute("DELETE FROM model_pull_jobs WHERE id=? AND status IN ('done', 'failed', 'cancelled')",
                      (job_id,)).rowcount == 1


def clear_finished() -> int:
    """Remove finished jobs, keeping completed checkpoint downloads.

    Completed checkpoint jobs are the download recipes (repository, revision,
    SHA-256) that backups save for weight-free disaster recovery.
    """
    return db.execute("DELETE FROM model_pull_jobs WHERE status IN ('done', 'failed', 'cancelled') "
                      "AND NOT (backend='comfyui' AND status='done')").rowcount
