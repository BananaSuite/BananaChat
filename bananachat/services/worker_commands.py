"""Model downloads and removals on worker PCs, asked for by administrators.

An administrator queues a command for a worker (``pull`` a model from the
Ollama library or Hugging Face, or ``delete`` one it advertises). The worker
receives it in its next heartbeat reply (only daemons that report the
``model_commands`` feature, which their owner can switch off), runs it against
its own Ollama and reports progress to ``/worker/v1/commands/<id>`` every few
seconds; the reply tells it when the administrator cancelled.

Nothing waits for ever: a command for a worker that is offline says so and
fails after :data:`QUEUE_SECONDS`; a running command that stops reporting for
:data:`STALL_SECONDS` fails (the worker restarted or lost its connection), and
the administrator can retry it.
"""

from __future__ import annotations

import logging
import math
import time
import uuid

from bananachat import db
from bananachat.db import worker_remote as store
from bananachat.db import workers as worker_store
from bananachat.services import background

log = logging.getLogger("bananachat.worker_commands")

FEATURE = "model_commands"
QUEUE_SECONDS = 24 * 3600
STALL_SECONDS = 15 * 60
LOST_SECONDS = 60           # a handed-out command the worker does not list after this long was lost
HANDOUT = 2                 # commands given to a worker per heartbeat
KEEP_SECONDS = 7 * 86400
STATUSES = ("running", "done", "failed")


class CommandError(ValueError):
    """Shown to the administrator as it is."""


def supports(worker) -> bool:
    from bananachat.services.worker_remote import system_info

    return FEATURE in (system_info(worker).get("features") or [])


def _row(worker_id: str):
    return db.one("SELECT id, name, status, capabilities, system_info, last_heartbeat FROM worker_nodes WHERE id=?",
                  (worker_id,))


def queue(worker_id: str, kind: str, model: str, requested_by: str | None) -> str:
    """Queue a command; returns its id. Raises :class:`CommandError`."""
    from bananachat.services import pulls, remote

    if kind not in ("pull", "delete"):
        raise CommandError("Unknown command.")
    worker = _row(worker_id)
    if worker is None:
        raise CommandError("No such worker.")
    if worker["status"] == "disabled":
        raise CommandError("This worker is disabled.")
    if not supports(worker):
        raise CommandError(f"{worker['name']} cannot manage its models from here: update its worker daemon, or its "
                           "owner switched it off (BC_WORKER_MODEL_MANAGEMENT=0).")
    if kind == "pull":
        try:
            model = pulls.validate_ollama_name(model)
        except ValueError as error:
            raise CommandError(str(error)) from None
    else:
        model = (model or "").strip()
        if not model or model not in remote.advertised_models(worker) and \
                not (remote.model_aliases(model) & set(remote.advertised_models(worker))):
            raise CommandError(f"{worker['name']} does not have {model or 'that model'}.")
    command_id = uuid.uuid4().hex
    with db.transaction():
        existing = store.live_command_for(worker_id, model)
        if existing is not None:
            raise CommandError(f"{model} already has a {existing['kind']} {existing['status']} on {worker['name']}.")
        store.insert_command(command_id, worker_id, kind, model, requested_by, time.time())
    return command_id


def queue_many(worker_ids, kind: str, model: str, requested_by: str | None) -> dict:
    result = {"queued": [], "errors": []}
    for worker_id in worker_ids:
        try:
            result["queued"].append(queue(worker_id, kind, model, requested_by))
        except CommandError as error:
            result["errors"].append(str(error))
    return result


def cancel(command_id: str) -> bool:
    return bool(store.finish_command(command_id, "cancelled", time.time(), detail="Cancelled by an administrator."))


def retry(command_id: str, requested_by: str | None) -> str:
    row = store.command(command_id)
    if row is None or row["status"] not in ("failed", "cancelled"):
        raise CommandError("Only failed or cancelled commands can be retried.")
    return queue(row["worker_id"], row["kind"], row["model_name"], requested_by)


def heartbeat(worker, system, running=None) -> list | None:
    """Commands to hand to a worker in its heartbeat reply (they become ``running``).

    *running* lists the commands the worker says it runs (daemons from 1.7.3 on): a command handed to it a while ago
    that is not there was lost (the worker restarted) and fails at once instead of after :data:`STALL_SECONDS`.
    """
    features = (system or {}).get("features") if system is not None else None
    if features is None:
        current = _row(worker["id"])
        features = (system_info_of(current) or {}).get("features") or []
    if FEATURE not in features:
        return None
    now = time.time()
    handed = []
    with db.transaction():
        active = store.running_commands(worker["id"])
        if running is not None:
            for row in active:
                if row["id"] not in running and now - float(row["started_at"] or now) > LOST_SECONDS:
                    store.finish_command(row["id"], "failed", now, only_from=("running",),
                                         error="The worker restarted before it finished. Retry it.")
            active = store.running_commands(worker["id"])
        if len(active) >= HANDOUT:
            return []
        for row in store.queued_commands(worker["id"], HANDOUT):
            if store.start_command(row["id"], worker["id"], now):
                handed.append({"id": row["id"], "kind": row["kind"], "model": row["model_name"]})
    return handed


def system_info_of(row) -> dict:
    from bananachat.services.worker_remote import system_info

    return system_info(row) if row is not None else {}


def _count(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or \
            not 0 <= value <= 2 ** 53:
        raise ValueError("Invalid byte count.")
    return int(value)


def report(worker, command_id: str, body) -> dict:
    """A worker's progress or outcome for one of its commands. Returns ``{"ok", "cancel"}``."""
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    status = body.get("status")
    if status not in STATUSES:
        raise ValueError("Invalid command status.")
    percent = body.get("percent", 0)
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
        raise ValueError("Invalid progress.")
    detail = body.get("detail") or ""
    error = body.get("error") or ""
    if not isinstance(detail, str) or not isinstance(error, str):
        raise ValueError("Invalid progress text.")
    detail = " ".join("".join(c if c.isprintable() else " " for c in detail).split())[:300]
    error = " ".join("".join(c if c.isprintable() else " " for c in error).split())[:500]
    completed, total = _count(body.get("completed")), _count(body.get("total"))
    now = time.time()
    with db.transaction():
        row = store.command(command_id)
        if row is None or row["worker_id"] != worker["id"]:
            raise LookupError("No such command.")
        if row["status"] == "cancelled":
            return {"ok": False, "cancel": True}
        if row["status"] != "running":
            return {"ok": False, "cancel": True}
        if status == "running":
            store.update_command(command_id, worker["id"], now, percent=int(percent), detail=detail or "Working…",
                                 bytes_done=completed, bytes_total=total)
        elif status == "done":
            store.finish_command(command_id, "done", now, detail=detail or "Done.", only_from=("running",))
        else:
            store.finish_command(command_id, "failed", now, error=error or "The worker could not run it.",
                                 detail=detail or None, only_from=("running",))
    return {"ok": True, "cancel": False}


def describe(row, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    keys = ("id", "worker_id", "kind", "model_name", "status", "progress_pct", "detail", "bytes_done", "bytes_total",
            "created_at", "started_at", "updated_at", "finished_at", "error")
    result = {key: row[key] for key in keys}
    if "worker_name" in row.keys():
        result["worker_name"] = row["worker_name"]
    return result


def overview(worker_id: str | None = None, limit: int = 100) -> list[dict]:
    now = time.time()
    return [describe(row, now) for row in store.commands(worker_id, limit)]


def maintain() -> dict:
    """Fail commands that wait for an absent worker too long or stopped reporting; note why queued ones wait."""
    from bananachat.services.remote import HEARTBEAT_FRESH_SECONDS

    now = time.time()
    failed = 0
    with db.transaction():
        for row in store.live_commands():
            if row["status"] == "running" and now - float(row["updated_at"]) > STALL_SECONDS:
                failed += store.finish_command(row["id"], "failed", now, only_from=("running",),
                                               error="The worker stopped reporting progress (it restarted or lost its "
                                                     "connection). Retry it.")
                continue
            if row["status"] != "queued":
                continue
            worker = worker_store.get(row["worker_id"])
            if worker is None:
                continue
            if now - float(row["created_at"]) > QUEUE_SECONDS:
                failed += store.finish_command(row["id"], "failed", now, only_from=("queued",),
                                               error="The worker did not come online in time.")
            elif worker["status"] == "disabled":
                db.execute("UPDATE worker_commands SET detail=? WHERE id=? AND status='queued'",
                           ("Waiting: the worker is disabled.", row["id"]))
            elif worker["last_heartbeat"] is None or float(worker["last_heartbeat"]) < now - HEARTBEAT_FRESH_SECONDS:
                db.execute("UPDATE worker_commands SET detail=? WHERE id=? AND status='queued'",
                           ("Waiting for the worker to come online.", row["id"]))
        purged = store.delete_finished_commands(now - KEEP_SECONDS)
    return {"failed": failed, "purged": purged}


@background.job("worker-commands", every=60, initial_delay=35)
def _maintain(app) -> None:
    result = maintain()
    if result["failed"]:
        log.info("Worker commands: %(failed)d failed (stalled or never picked up)", result)
