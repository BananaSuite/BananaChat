"""Model pull job queue: DB CRUD."""

import re
import secrets
from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/\-]{0,299}$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REMOTE_JOB_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_checkpoint_path(value, field):
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ValueError(f"{field} is invalid.")
    parts = value.split("/")
    if any(
        not part
        or part in (".", "..")
        or part.startswith(".")
        or not _PATH_PART_RE.fullmatch(part)
        for part in parts
    ):
        raise ValueError(f"{field} is invalid.")
    if not parts[-1].endswith(".safetensors"):
        raise ValueError(f"{field} must end in .safetensors.")
    return "/".join(parts)


def _validated_pull_metadata(
    ollama_name, backend, repo_id, source_filename, revision, target_name,
    expected_sha256, expected_size,
):
    if backend not in ("ollama", "comfyui"):
        raise ValueError("Invalid pull backend.")
    if backend == "ollama":
        if not isinstance(ollama_name, str) or not _MODEL_NAME_RE.fullmatch(ollama_name):
            raise ValueError("Invalid Ollama model name.")
        if any(
            value is not None
            for value in (
                repo_id, source_filename, revision, target_name,
                expected_sha256, expected_size,
            )
        ):
            raise ValueError("Checkpoint metadata is not valid for an Ollama pull.")
        return {
            "ollama_name": ollama_name,
            "backend": backend,
            "repo_id": None,
            "source_filename": None,
            "revision": None,
            "target_name": None,
            "expected_sha256": None,
            "expected_size": None,
        }

    if not isinstance(repo_id, str) or len(repo_id) > 192:
        raise ValueError("Hugging Face repository must use owner/repo format.")
    repo_parts = repo_id.split("/")
    if (
        len(repo_parts) != 2
        or any(".." in part or not _REPO_PART_RE.fullmatch(part) for part in repo_parts)
    ):
        raise ValueError("Hugging Face repository must use owner/repo format.")
    if (
        not isinstance(revision, str)
        or ".." in revision
        or not _REVISION_RE.fullmatch(revision)
    ):
        raise ValueError("Revision is invalid.")
    source_filename = _validate_checkpoint_path(source_filename, "Source filename")
    target_name = _validate_checkpoint_path(target_name, "Target name")
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("Expected SHA-256 must contain exactly 64 hexadecimal characters.")
    if expected_size is not None and (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
        or expected_size > 1024 ** 5
    ):
        raise ValueError("Expected size is invalid.")
    if ollama_name not in (None, target_name):
        raise ValueError("Checkpoint pull target does not match its artifact name.")
    return {
        "ollama_name": target_name,
        "backend": backend,
        "repo_id": repo_id,
        "source_filename": source_filename,
        "revision": revision,
        "target_name": target_name,
        "expected_sha256": expected_sha256.lower(),
        "expected_size": expected_size,
    }


def enqueue_pull_job(
    ollama_name, user_id, *, backend="ollama", repo_id=None,
    source_filename=None, revision=None, target_name=None,
    expected_sha256=None, expected_size=None,
):
    """Validate and enqueue one backend-specific pull job."""
    values = _validated_pull_metadata(
        ollama_name, backend, repo_id, source_filename, revision, target_name,
        expected_sha256, expected_size,
    )
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, status FROM model_pull_jobs "
            "WHERE backend=? AND ollama_name=? AND status IN ('queued', 'pulling')",
            (values["backend"], values["ollama_name"]),
        ).fetchone()
        if existing:
            conn.commit()
            raise ValueError(
                f"A {backend} pull job for '{values['ollama_name']}' is already {existing['status']}."
            )
        cur = conn.execute(
            "INSERT INTO model_pull_jobs "
            "(ollama_name, backend, repo_id, source_filename, revision, target_name, "
            "expected_sha256, expected_size, requested_by, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["ollama_name"], values["backend"], values["repo_id"],
                values["source_filename"], values["revision"], values["target_name"],
                values["expected_sha256"], values["expected_size"], user_id,
                secrets.token_hex(16),
            ),
        )
        conn.commit()
        return cur.lastrowid


@retry_on_busy
def list_pull_jobs(limit=50):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT mpj.*, u.username AS requested_by_name "
            "FROM model_pull_jobs mpj "
            "LEFT JOIN users u ON mpj.requested_by=u.id "
            "ORDER BY mpj.id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_pull_job(job_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM model_pull_jobs WHERE id=?", (job_id,)
        ).fetchone()


def claim_next_pull_job():
    """Atomically claim the next queued job. Returns dict or None."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        already = conn.execute(
            "SELECT id FROM model_pull_jobs WHERE status='pulling'"
        ).fetchone()
        if already:
            conn.commit()
            return None
        job = conn.execute(
            "SELECT * FROM model_pull_jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not job:
            conn.commit()
            return None
        conn.execute(
            "UPDATE model_pull_jobs SET status='pulling', started_at=? "
            "WHERE id=? AND status='queued'",
            (now, job["id"]),
        )
        conn.commit()
        # Return with the updated status so the caller sees 'pulling'
        claimed = dict(job)
        claimed["status"] = "pulling"
        claimed["started_at"] = now
        return claimed


def update_pull_job_progress(job_id, progress_pct, detail):
    with get_db_context() as conn:
        conn.execute(
            "UPDATE model_pull_jobs SET progress_pct=?, progress_detail=? WHERE id=?",
            (progress_pct, str(detail)[:500], job_id),
        )
        conn.commit()


def set_pull_job_remote_id(job_id, remote_job_id):
    """Persist the compute agent job ID once, tolerating idempotent retries."""
    if not isinstance(remote_job_id, str) or not _REMOTE_JOB_RE.fullmatch(remote_job_id):
        raise ValueError("Invalid remote pull job ID.")
    with get_db_context() as conn:
        result = conn.execute(
            "UPDATE model_pull_jobs SET remote_job_id=? "
            "WHERE id=? AND backend='comfyui' AND status='pulling' "
            "AND (remote_job_id IS NULL OR remote_job_id=?)",
            (remote_job_id, job_id, remote_job_id),
        )
        conn.commit()
    if result.rowcount == 0:
        row = get_pull_job(job_id)
        if not row or row["remote_job_id"] != remote_job_id:
            raise ValueError("Could not associate the remote pull job.")


def is_pull_job_cancelled(job_id):
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT status FROM model_pull_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return row is None or row["status"] == "cancelled"


def finish_pull_job(job_id, success, error=None):
    now = datetime.now(timezone.utc).isoformat()
    if success:
        status, pct = "done", 100
    elif error:
        status, pct = "failed", None
    else:
        status, pct = "cancelled", None
    with get_db_context() as conn:
        if pct is not None:
            conn.execute(
                "UPDATE model_pull_jobs SET status=?, error_message=?, finished_at=?, progress_pct=? WHERE id=?",
                (status, error, now, pct, job_id),
            )
        else:
            conn.execute(
                "UPDATE model_pull_jobs SET status=?, error_message=?, finished_at=? WHERE id=?",
                (status, error, now, job_id),
            )
        conn.commit()


def cancel_pull_job(job_id):
    """Cancel a queued or pulling job. Returns True if the row was updated."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        result = conn.execute(
            "UPDATE model_pull_jobs SET status='cancelled', finished_at=? "
            "WHERE id=? AND status IN ('queued', 'pulling')",
            (now, job_id),
        )
        conn.commit()
    return result.rowcount > 0


def reset_stuck_pulls():
    """Fail Ollama pulls and requeue resumable ComfyUI pulls after restart."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE model_pull_jobs SET status='failed', "
            "error_message='Interrupted by server restart', finished_at=? "
            "WHERE status='pulling' AND backend='ollama'",
            (now,),
        )
        conn.execute(
            "UPDATE model_pull_jobs SET status='queued', error_message=NULL, "
            "finished_at=NULL WHERE status='pulling' AND backend='comfyui'"
        )
        conn.commit()


def delete_pull_job(job_id):
    """Delete a single finished pull job (done/failed/cancelled). Returns True if deleted."""
    with get_db_context() as conn:
        result = conn.execute(
            "DELETE FROM model_pull_jobs WHERE id=? AND status IN ('done', 'failed', 'cancelled')",
            (job_id,),
        )
        conn.commit()
    return result.rowcount > 0


def clear_pull_history():
    """Delete all finished pull jobs (done/failed/cancelled). Returns count deleted."""
    with get_db_context() as conn:
        result = conn.execute(
            "DELETE FROM model_pull_jobs WHERE status IN ('done', 'failed', 'cancelled')"
        )
        conn.commit()
    return result.rowcount
