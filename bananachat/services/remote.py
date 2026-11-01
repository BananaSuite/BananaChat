"""Routing text generation to volunteer worker PCs (``BC_WORKERS_ENABLED``).

``services.inference`` calls:

* ``should_route(user, model, *, request_type=None, think=None)`` - whether this request goes to a worker
  instead of the local Ollama server.
* ``stream(model_name, messages, options, *, cancel, priority, first_token_timeout, total_timeout)`` - run
  the job on a worker and yield chunks like ``ollama.chat_stream``. When no worker answers before producing
  text, the local server answers instead. Raises ``upstream.Cancelled`` when *cancel* fires and
  ``upstream.UpstreamError`` when the job fails after text was relayed or the local server also fails.

``web.worker_api`` calls the worker side: ``heartbeat``, ``poll``, ``submit_chunk``, ``complete`` and ``fail``.

Routing policy, pausing of failing workers, privacy and the protocol: docs/workers.md.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
import uuid

from flask import current_app

from bananachat import db
from bananachat.db import workers as store
from bananachat.services import background, ollama
from bananachat.services.ollama import Chunk
from bananachat.services.upstream import Cancelled, UpstreamError

log = logging.getLogger("bananachat.remote")

HEARTBEAT_FRESH_SECONDS = 45      # a worker silent for longer is offline
LONG_POLL_SECONDS = 25            # below common 30 s reverse-proxy timeouts
MAX_LONG_POLLS = 4                # per process; further polls are answered at once
MAX_CHUNK_BYTES = 16 * 1024
MAX_CHUNK_SEQ = 1_000_000
MAX_JOB_BYTES = 16 * 1024 * 1024
MAX_MODELS = 512
MAX_NAME_LENGTH = 80
PURGE_AFTER_SECONDS = 3600
COOLDOWN_SECONDS = 60
PAUSE_SECONDS = 60
PAUSE_MAX_SECONDS = 3600
# After a failure of one of its jobs was held against a worker, an account is answered locally for this long (after
# one held against the request, it is not sent to that worker for this long), and further failures of its jobs in
# this time are not held against a worker. Kept below PURGE_AFTER_SECONDS, which keeps the record (worker_jobs.fault).
USER_FAULT_SECONDS = 1800
# A worker on which the jobs of this many accounts failed for their requests' sake within USER_FAULT_SECONDS, with no
# answered job in between, is paused all the same: fewer accounts' requests may really be at fault.
REQUEST_FAULT_ACCOUNTS = 3
MAX_REQUEUES = 3
RELAY_MIN_INTERVAL = 0.05
RELAY_MAX_INTERVAL = 0.25
ACTIVITY_STATES = ("idle", "light", "active", "gaming")
WORKER_STATUSES = ("online", "busy", "offline")
NO_REMOTE_REQUEST_TYPES = frozenset({"chat_incognito", "incognito", "no_history", "agent"})
# When this server started (imported by Gunicorn's master before it starts workers): a worker could not reach it
# before, so a lease that ran out while it was down (an update, a restart) is not held against the worker.
_SERVER_STARTED = time.time()
TOKEN_PREFIX = "bcw_"  # noqa: S105 - the public prefix of worker tokens, not a token
# Users are charged by the usage a worker reports, so a report is capped at what
# the job can have used: the prompt at PROMPT_ESTIMATE_FACTOR times the server's
# own estimate (as for interrupted answers: its text, and IMAGE_TOKEN_ESTIMATE per
# image, never the bytes of the encoded images), the answer at one token per byte
# relayed, each plus room for the chat template around it.
PROMPT_ESTIMATE_FACTOR = 2
PROMPT_TOKEN_ALLOWANCE = 4096
ANSWER_TOKEN_ALLOWANCE = 16


class InvalidRequest(ValueError):
    """A worker sent a malformed request (HTTP 400)."""


class JobNotFound(LookupError):
    """The job does not exist or belongs to another worker (HTTP 404)."""


class WorkerDisabled(RuntimeError):
    """An administrator disabled the worker (HTTP 403)."""


class _NotStarted(Exception):
    """The pool did not produce any text; the local server may answer instead."""


def _config(config=None):
    return config or current_app.config["BC"]


def lease_seconds(config=None) -> float:
    return float(max(60, _config(config).inference_read_timeout * 2))


def _field(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


# ----- model names ------------------------------------------------------------------

def model_aliases(name: str) -> set[str]:
    """Names Ollama treats as the same model (``llama3`` and ``llama3:latest``)."""
    name = (name or "").strip()
    if not name:
        return set()
    if ":" not in name.rsplit("/", 1)[-1]:
        return {name, f"{name}:latest"}
    if name.endswith(":latest"):
        return {name, name[: -len(":latest")]}
    return {name}


def model_name(model) -> str:
    if isinstance(model, str):
        return model
    return _field(model, "backend_model_name") or _field(model, "ollama_name") or ""


def advertised_models(worker) -> list[str]:
    raw = _field(worker, "capabilities")
    try:
        capabilities = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return []
    models = capabilities.get("models") if isinstance(capabilities, dict) else None
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, str) and model][:MAX_MODELS]


def _alias_set(models) -> set[str]:
    names: set[str] = set()
    for model in models:
        names |= model_aliases(model)
    return names


# ----- routing ------------------------------------------------------------------------

_cooldown: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _cool_down(name: str) -> None:
    with _cooldown_lock:
        for alias in model_aliases(name):
            _cooldown[alias] = time.monotonic() + COOLDOWN_SECONDS


def _cooling(name: str) -> bool:
    with _cooldown_lock:
        until = _cooldown.get(name)
        if until is None:
            return False
        if until < time.monotonic():
            _cooldown.pop(name, None)
            return False
        return True


def worker_is_available(worker, now: float) -> bool:
    return (_field(worker, "status") == "online"
            and float(_field(worker, "last_heartbeat", 0)) >= now - HEARTBEAT_FRESH_SECONDS
            and _field(worker, "activity_state") != "gaming"
            and not _paused(worker, now))


def _paused(worker, now: float) -> bool:
    return float(_field(worker, "cooldown_until", 0)) > now


def _penalize(worker_id, now: float, reason: str) -> None:
    """Pause a worker that failed or stalled a job (see the module docstring). Joins the caller's transaction."""
    if not worker_id:
        return
    with db.transaction():
        worker = store.get(worker_id)
        if worker is None:
            return
        strikes = int(_field(worker, "strikes", 0)) + 1
        pause = min(PAUSE_MAX_SECONDS, PAUSE_SECONDS * 2 ** min(strikes - 1, 16))
        store.pause(worker_id, strikes, now + pause)
    log.warning("Worker %s is paused for %d s (%s; failure %d in a row)", worker["name"], pause, reason, strikes)


def _misbehaved(job, worker_id, now: float, reason: str) -> None:
    """The worker broke the protocol or abandoned a job it was answering: always its own fault."""
    store.set_fault(job["id"], "worker")
    _penalize(worker_id, now, reason)


def _blame(job, now: float, reason: str) -> None:
    """A job failed or stalled on its worker in a way its request may have caused (a reported failure, no first
    output in time, silence): pause the worker, unless the job's account already held a worker responsible within
    :data:`USER_FAULT_SECONDS`. Joins the caller's transaction."""
    account = _field(job, "requested_by")
    if account and store.account_fault_since(account, now - USER_FAULT_SECONDS, fault="worker", exclude=job["id"]):
        store.set_fault(job["id"], "request")
        log.info("Worker job %s failed (%s); not held against its worker: the account already paused one",
                 job["id"][:8], reason)
        return
    store.set_fault(job["id"], "worker")
    _penalize(_field(job, "worker_id"), now, reason)


# Memory the request needs that the worker's hardware does not have (Ollama and llama.cpp wording): a context the
# request made too large, not a broken worker.
_RESOURCE_FAULTS = ("out of memory", "more system memory", "not enough memory", "insufficient memory",
                    "unable to allocate", "failed to allocate", "cudamalloc failed")
# How the worker daemon reports a refusal by its Ollama (the local server treats any 4xx as the request's fault).
_REFUSED = re.compile(r"\bHTTP 4\d\d\b")


def _request_fault(message: str, job) -> bool:
    """A failure that follows from what the request asked (as the local server's would) is not the worker's fault."""
    from bananachat.services.inference import input_fault

    lowered = message.lower()
    return (input_fault(message, images='"images"' in (_field(job, "messages") or ""))
            or any(marker in lowered for marker in _RESOURCE_FAULTS) or _REFUSED.search(message) is not None)


def _request_faults_add_up(job, worker, now: float) -> None:
    """A failure was held against the request on the worker's word: pause the worker once the jobs of
    :data:`REQUEST_FAULT_ACCOUNTS` accounts failed on it that way (see the module docstring). Joins the caller's
    transaction."""
    accounts = store.request_fault_accounts(worker["id"], now - USER_FAULT_SECONDS)
    if _field(job, "requested_by") and accounts >= REQUEST_FAULT_ACCOUNTS:
        _penalize(worker["id"], now, f"the jobs of {accounts} accounts failed on it, reportedly for their requests' sake")


def account_benched(user, now: float | None = None) -> bool:
    """Whether a failure of one of *user*'s jobs was held against a worker lately: the account is answered locally
    for a while (see the module docstring)."""
    account = _field(user, "id")
    if not account:
        return False
    now = time.time() if now is None else now
    return store.account_fault_since(account, now - USER_FAULT_SECONDS, fault="worker")


def _workers_avoided_by(user, now: float) -> set[str]:
    """The workers on which a job of *user* failed lately: its jobs are not sent there for a while."""
    account = _field(user, "id")
    return store.workers_that_failed(account, now - USER_FAULT_SECONDS) if account else set()


def _lease_ran_out(job, now: float, reason: str) -> None:
    """A claimed job timed out (no first output in time, or silence): blame its worker unless it last spoke before
    this server started, when it could not reach the server for part of its lease."""
    last_seen = float(_field(job, "heartbeat_at") or _field(job, "claimed_at") or 0)
    if last_seen < _SERVER_STARTED:
        log.info("Worker job %s timed out while this server was not running; not held against its worker",
                 job["id"][:8])
        return
    _blame(job, now, reason)


def _requeue(job, now: float) -> bool:
    """Hand an unstarted job back to the pool, or fail it once it went back too often. True when re-queued."""
    if int(_field(job, "requeues", 0)) >= MAX_REQUEUES:
        store.finish(job["id"], "failed", now, error="No worker kept the job.", only_from=store.ACTIVE)
        return False
    return bool(store.requeue(job["id"], now))


def worker_features(worker) -> set[str]:
    """What a worker daemon reported it supports (``think``, ``model_commands``, ``remote_access``)."""
    from bananachat.services.worker_remote import system_info

    features = system_info(worker).get("features") if "system_info" in worker.keys() else None
    return {item for item in features or [] if isinstance(item, str)}


def idle_workers_for(name: str, now: float | None = None, *, feature: str | None = None) -> list:
    """Available workers without a running job that advertise *name* (and support *feature*)."""
    now = time.time() if now is None else now
    wanted = model_aliases(name)
    busy = store.workers_with_active_jobs()
    return [worker for worker in store.online(now - HEARTBEAT_FRESH_SECONDS)
            if worker["id"] not in busy and worker_is_available(worker, now)
            and wanted & _alias_set(advertised_models(worker))
            and (feature is None or feature in worker_features(worker))]


def should_route(user, model, *, request_type: str | None = None, think=None, config=None) -> bool:
    config = _config(config)
    if not config.workers_enabled or model is None:
        return False
    if _field(model, "backend") not in (None, "ollama"):
        return False  # worker PCs run Ollama models only (never external API providers or subscription pools)
    # Reasoning goes only to daemons that relay it (they report the "think" feature); older ones answer without.
    feature = "think" if think else None
    if _field(user, "role") == "admin":
        return False
    if request_type in NO_REMOTE_REQUEST_TYPES:
        return False
    name = model_name(model)
    if not name or _cooling(name):
        return False
    try:
        now = time.time()
        if account_benched(user, now):
            return False
        avoided = _workers_avoided_by(user, now)
        idle = [worker for worker in idle_workers_for(name, now, feature=feature) if worker["id"] not in avoided]
        return len(idle) > store.count_pending(model_aliases(name))
    except sqlite3.Error:
        log.warning("Worker routing check failed; using the local server", exc_info=True)
        return False


# ----- relay (web server side) ------------------------------------------------------------

_new_job = threading.Condition()


def _announce() -> None:
    with _new_job:
        _new_job.notify_all()


def lease_expired(job, now: float, lease: float, first_token_timeout: float) -> bool:
    last_seen = float(_field(job, "heartbeat_at") or _field(job, "claimed_at") or 0)
    if now - last_seen > lease:
        return True
    claimed = _field(job, "claimed_at")
    return (_field(job, "next_chunk_seq", 0) == 0 and claimed is not None
            and now - float(claimed) > first_token_timeout)


def stream(model_name, messages, options, *, cancel, priority, first_token_timeout, total_timeout, think=None,
           config=None, user_id: str | None = None):
    """See the module docstring; *user_id* is the account the job serves (see "Misbehaving workers")."""
    config = _config(config)
    deadline = time.monotonic() + float(total_timeout)
    messages_json = json.dumps(messages, ensure_ascii=False)
    options_json = json.dumps(options, ensure_ascii=False) if options else None
    if len(messages_json.encode("utf-8")) + len((options_json or "").encode("utf-8")) > MAX_JOB_BYTES:
        log.info("A %s request is too large for the worker pool; answering locally", model_name)
    else:
        job_id = uuid.uuid4().hex
        store.insert_job(job_id, model_name, messages_json, options_json, int(priority), time.time(),
                         requested_by=user_id, think=json.dumps(think) if think is not None else None)
        _announce()
        try:
            yield from _relay(job_id, cancel, config, float(first_token_timeout), deadline)
            return
        except _NotStarted as reason:
            log.info("Worker job %s for %s did not start (%s); answering locally", job_id[:8], model_name, reason)
        finally:
            _release(job_id)
    cancel.check()
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise UpstreamError("The answer took too long.")
    yield from ollama.chat_stream(model_name, messages, options=options, think=think, cancel=cancel, config=config,
                                  first_token_timeout=min(float(first_token_timeout), remaining),
                                  total_timeout=remaining)


def _release(job_id: str) -> None:
    """End the job if the relay leaves early and delete what is left of it."""
    try:
        with db.transaction():
            store.finish(job_id, "failed", time.time(), error="Generation stopped")
            store.delete_chunks(job_id)
    except sqlite3.Error:
        log.warning("Could not clean up worker job %s; the worker-jobs task will", job_id[:8], exc_info=True)


def _fail_relay(job_id: str, status: str, message: str, produced: bool, *, blame: str | None = None):
    """End the job. *blame* ``worker``: the worker running it broke the protocol and is paused; ``stall``: it
    stalled, which may be the request's doing too (:func:`_blame`)."""
    now = time.time()
    with db.transaction():
        job = store.get_job(job_id) if blame else None
        if store.finish(job_id, status, now, error=message) and job is not None and job["worker_id"]:
            if blame == "worker":
                _misbehaved(job, job["worker_id"], now, message)
            else:
                _blame(job, now, message)
    if not produced:
        raise _NotStarted(message)
    raise UpstreamError(message)


def _relay(job_id: str, cancel, config, first_token_timeout: float, deadline: float):
    claim_timeout = float(config.worker_claim_timeout)
    lease = lease_seconds(config)
    pending_since = time.monotonic()
    # However often the job changes hands, the pool has this long to start answering.
    pool_deadline = pending_since + claim_timeout + first_token_timeout
    previous_status = "pending"
    last_seq = -1
    terminal = False
    produced = False
    missing = False
    interval = RELAY_MIN_INTERVAL
    while True:
        if cancel.cancelled:
            store.finish(job_id, "failed", time.time(), error="Generation stopped")
            raise Cancelled(cancel.reason or "cancelled")
        if not produced and time.monotonic() >= min(deadline, pool_deadline):
            # The local server answers instead (with whatever time is left). The worker holding the job is held
            # responsible only if it had the whole first-token time itself, not when it took the job over late.
            current = store.get_job(job_id)
            held = current is not None and current["status"] in store.ACTIVE and \
                current["claimed_at"] is not None and time.time() - float(current["claimed_at"]) >= first_token_timeout
            _fail_relay(job_id, "timeout", "No worker started answering in time.", produced,
                        blame="stall" if held else None)
        if time.monotonic() >= deadline:
            store.finish(job_id, "timeout", time.time(), error="The answer took too long.")
            raise UpstreamError("The worker took too long to answer.")

        rows = store.chunks_after(job_id, last_seq)
        for row in rows:
            if row["seq"] != last_seq + 1 or terminal:
                _fail_relay(job_id, "failed", "The worker sent an incomplete answer.", produced, blame="worker")
            last_seq = row["seq"]
            terminal = bool(row["done"])
            thinking = row["thinking"] if "thinking" in row.keys() else ""
            if row["content"] or thinking:
                produced = True
                yield Chunk(content=row["content"], thinking=thinking or "")
        if rows and last_seq > 0:
            store.delete_chunks_before(job_id, last_seq)

        job = store.get_job(job_id)
        if job is None:
            if not produced:
                raise _NotStarted("the job was removed")
            raise UpstreamError("The worker job was removed.")
        status = job["status"]
        now = time.time()
        if status in store.TERMINAL and last_seq < job["next_chunk_seq"] - 1 and (rows or not missing):
            # Chunks written after the read above: read again (once without progress).
            missing = not rows
            continue
        if status == "done":
            if not terminal or last_seq != job["next_chunk_seq"] - 1:
                _fail_relay(job_id, "failed", "The worker ended without a completion marker.", produced,
                            blame="worker")
            yield Chunk(done=True, finish_reason=job["finish_reason"] or "stop",
                        prompt_tokens=job["tokens_in"] or None, completion_tokens=job["tokens_out"] or None)
            return
        if terminal and (status in ("failed", "timeout") or (
                status in store.ACTIVE and lease_expired(job, now, lease, first_token_timeout))):
            # The final chunk arrived, so the answer is whole: only the worker's
            # usage report was lost (it vanished or lost contact before completing).
            store.finish(job_id, "timeout", now, error="The worker did not report its usage.",
                         only_from=store.ACTIVE)
            yield Chunk(done=True, finish_reason="stop")
            return
        if status in ("failed", "timeout"):
            message = job["error_message"] or "The worker failed."
            if not produced:
                raise _NotStarted(message)
            raise UpstreamError(message)
        if status == "pending":
            if previous_status != "pending":
                pending_since = time.monotonic()  # re-queued by its worker: a fresh claim window
            if time.monotonic() - pending_since >= claim_timeout:
                withdrawn = store.finish(job_id, "failed", now, error="No worker took the job in time.",
                                         only_from=("pending",))
                if withdrawn:
                    _cool_down(job["model_name"])
                    raise _NotStarted("no worker took the job in time")
                continue
        elif lease_expired(job, now, lease, first_token_timeout):
            _fail_relay(job_id, "timeout", "The worker stopped responding.", produced, blame="stall")
        previous_status = status

        if rows:
            interval = RELAY_MIN_INTERVAL
            continue
        cancel.wait(interval)
        interval = min(RELAY_MAX_INTERVAL, interval * 1.5)


# ----- worker side -------------------------------------------------------------------------

def authenticate(header: str | None):
    """The worker row for an ``Authorization: Bearer <token>`` header, or None."""
    scheme, _, token = (header or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return None
    return store.by_token_hash(store.hash_token(token))


def _text(body: dict, name: str, limit: int):
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise InvalidRequest(f"Invalid {name}.")
    return value.strip() or None


def parse_models(values) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_MODELS:
        raise InvalidRequest("Invalid worker model inventory.")
    models = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise InvalidRequest("Invalid worker model inventory.")
        models.append(value.strip())
    return models


def heartbeat(worker, body, config=None) -> dict:
    """Record a heartbeat. Returns the reply (``job_stop`` answers for ``body["job_id"]``)."""
    if not isinstance(body, dict):
        raise InvalidRequest("Expected a JSON object.")
    status = body.get("status") or "online"
    if status not in WORKER_STATUSES:
        raise InvalidRequest("Invalid worker status.")
    activity = body.get("activity_state")
    if activity is not None and activity not in ACTIVITY_STATES:
        raise InvalidRequest("Invalid activity state.")
    gpu_util = body.get("gpu_util")
    if gpu_util is not None and (isinstance(gpu_util, bool) or not isinstance(gpu_util, (int, float))
                                 or not math.isfinite(gpu_util) or not 0 <= gpu_util <= 100):
        raise InvalidRequest("Invalid GPU utilization.")
    capabilities = body.get("capabilities")
    capabilities_json = None
    if capabilities is not None:
        if not isinstance(capabilities, dict):
            raise InvalidRequest("Invalid worker model inventory.")
        capabilities_json = json.dumps({"models": parse_models(capabilities.get("models", []))})
    gpu_name = _text(body, "gpu_name", 512)
    ollama_version = _text(body, "ollama_version", 512)
    platform = _text(body, "platform", 128)
    job_id = body.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        raise InvalidRequest("Invalid job_id.")
    from bananachat.services import worker_commands, worker_remote

    try:
        remote_access = worker_remote.parse_remote(body.get("remote"))
    except ValueError as error:
        raise InvalidRequest(str(error)) from None
    system = worker_remote.parse_system(body.get("system"))
    running = body.get("commands_running")
    if running is not None and (not isinstance(running, list) or len(running) > 64 or not all(
            isinstance(item, str) and len(item) == 32 for item in running)):
        raise InvalidRequest("Invalid commands_running.")

    now = time.time()
    job_stop = False
    with db.transaction():
        store.record_heartbeat(worker["id"], now=now, status=status, gpu_name=gpu_name,
                               gpu_util=float(gpu_util) if gpu_util is not None else None,
                               ollama_version=ollama_version, capabilities_json=capabilities_json,
                               activity_state=activity, platform=platform)
        if remote_access is not None or system is not None:
            worker_remote.record(worker["id"], remote_access, system)
        if status != "online" or activity == "gaming":
            # The owner is gaming or the worker is going away: jobs it has not
            # started answering go back to the pool (older daemons silently
            # sat on them until the lease expired).
            for job in store.active_jobs_for_worker(worker["id"]):
                if job["next_chunk_seq"] == 0 and _requeue(job, now):
                    log.info("Worker %s is %s; job %s returned to the pool", worker["name"],
                             activity if activity == "gaming" else status, job["id"][:8])
                    _announce()
        store.renew_lease(worker["id"], now)
        if job_id:
            job = store.get_job(job_id)
            job_stop = (job is None or job["worker_id"] != worker["id"] or job["status"] not in store.ACTIVE
                        or bool(job["stop_requested"]))
    reply = {"ok": True, "job_stop": job_stop}
    # Optional parts that older daemons ignore: remote access and model commands.
    remote_reply = worker_remote.heartbeat_reply(worker["id"], config)
    if remote_reply is not None:
        reply["remote"] = remote_reply
    command_reply = worker_commands.heartbeat(worker, system, running=running)
    if command_reply is not None:
        reply["commands"] = command_reply
    return reply


def _job_payload(job, config) -> dict:
    payload = {
        "job_id": job["id"],
        "model": job["model_name"],
        "messages": json.loads(job["messages"] or "[]"),
        "options": json.loads(job["options"]) if job["options"] else None,
        "priority": job["priority"],
        "first_token_timeout": config.first_token_timeout,
        "lease_seconds": lease_seconds(config),
        "generation_timeout": config.generation_timeout,
    }
    think = job["think"] if "think" in job.keys() else None
    if think:
        try:
            value = json.loads(think)
        except ValueError:
            value = None
        if isinstance(value, (bool, str)):
            payload["think"] = value  # Ollama's think: false, true or a level (newer daemons only read it)
    return payload


def _nothing() -> None:
    pass


def payload_bound(job) -> int:
    """At least the size of the job's reply: JSON-escaping its stored text at most triples it."""
    return 3 * (len((job["messages"] or "").encode("utf-8")) + len((job["options"] or "").encode("utf-8"))) + 4096


def claim(worker, models: list[str] | None, config=None, *, reserve=None):
    """Claim the next job this worker can run, or None. Idle checks take no write lock.

    *reserve(size)*, called before a job is claimed with a bound on its reply's size, returns the function
    that gives back the place the reply is sent from, or None to leave the job pending (the web view has no
    place to send it from, see ``security.take_reply_place``). A place for a job that was not claimed after
    all is given back here.
    """
    config = _config(config)
    names = _alias_set(models) if models is not None else None
    now = time.time()
    failed_since = now - USER_FAULT_SECONDS
    active = store.active_jobs_for_worker(worker["id"])
    reasoning = "think" in worker_features(worker)  # older daemons would drop the reasoning
    if not active and (_paused(worker, now)
                       or store.next_pending(names, worker_id=worker["id"], failed_since=failed_since,
                                             reasoning=reasoning) is None):
        return None
    with db.transaction():
        current = store.get(worker["id"])
        if current is None or current["status"] == "disabled":
            raise WorkerDisabled()
        for job in store.active_jobs_for_worker(worker["id"]):
            # Daemons run one job at a time: a worker asking for work has
            # abandoned whatever it still holds (a restart, a lost reply).
            if job["next_chunk_seq"] == 0:
                _requeue(job, now)
            elif store.finish(job["id"], "failed", now, error="The worker abandoned the job."):
                _misbehaved(job, worker["id"], now, "abandoned a job it was answering")
        if _paused(store.get(worker["id"]), now):
            return None
        job = store.next_pending(names, worker_id=worker["id"], failed_since=failed_since, reasoning=reasoning)
        if job is None:
            return None
        release = reserve(payload_bound(job)) if reserve is not None else _nothing
        if release is None:
            return None
        if not store.set_claimed(job["id"], worker["id"], now):
            release()
            return None
    log.info("Worker job %s (%s) claimed by %s", job["id"][:8], job["model_name"], worker["name"])
    return _job_payload(job, config)


_poll_lock = threading.Lock()
_polls = {"active": 0}


def poll(worker, models: list[str] | None, *, wait: float | None = None, config=None, reserve=None):
    """Long-poll for a job (at most ``MAX_LONG_POLLS`` waiting per process); *reserve* as for :func:`claim`."""
    config = _config(config)
    store.touch(worker["id"], time.time())
    if models is None:
        # Only what the worker advertised: an empty (or unreported) inventory
        # must not mean "any model", or its prompt goes to a PC that cannot run it.
        models = advertised_models(worker)
    wait = LONG_POLL_SECONDS if wait is None else wait
    job = claim(worker, models, config, reserve=reserve)
    if job is not None or wait <= 0:
        return job
    with _poll_lock:
        if _polls["active"] >= MAX_LONG_POLLS:
            return None
        _polls["active"] += 1
    try:
        deadline = time.monotonic() + min(wait, LONG_POLL_SECONDS)
        while not background.stopping():
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            with _new_job:
                _new_job.wait(min(1.0, left))
            db.release_thread_connection()
            job = claim(worker, models, config, reserve=reserve)
            if job is not None:
                return job
        return None
    finally:
        with _poll_lock:
            _polls["active"] -= 1


def _owned(job, worker, config) -> bool:
    """Whether *worker* may still write to *job* (expiring its lease if needed)."""
    if job["status"] not in store.ACTIVE or job["stop_requested"] or job["worker_id"] != worker["id"]:
        return False
    now = time.time()
    if lease_expired(job, now, lease_seconds(config), config.first_token_timeout):
        if store.finish(job["id"], "timeout", now, error="The worker stopped responding."):
            _lease_ran_out(job, now, "stopped responding")
        return False
    return True


def _load_owned(worker, job_id):
    job = store.get_job(job_id)
    if job is None or job["worker_id"] != worker["id"]:
        raise JobNotFound()
    return job


def _truncate_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[: max(0, limit)].decode("utf-8", "ignore")


def submit_chunk(worker, job_id: str, body, config=None) -> dict:
    config = _config(config)
    if not isinstance(body, dict):
        raise InvalidRequest("Expected a JSON object.")
    seq, content, done = body.get("seq"), body.get("content", ""), body.get("done", False)
    thinking = body.get("thinking") or ""
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < MAX_CHUNK_SEQ:
        raise InvalidRequest("Invalid chunk sequence.")
    if content is None:
        content = ""
    if not isinstance(content, str) or not isinstance(done, bool) or not isinstance(thinking, str):
        raise InvalidRequest("Invalid chunk.")
    size = len(content.encode("utf-8")) + len(thinking.encode("utf-8"))
    if size > MAX_CHUNK_BYTES:
        raise InvalidRequest("The chunk is too large.")
    now = time.time()
    with db.transaction():
        job = _load_owned(worker, job_id)
        if not _owned(job, worker, config):
            return {"ok": False, "stop": True}
        expected = job["next_chunk_seq"]
        if seq < expected:
            # A retry of something already accepted (the reply was lost).
            stored = store.chunk(job_id, seq)
            if stored is None or (stored["content"] == content and bool(stored["done"]) == done
                                  and (stored["thinking"] or "") == thinking):
                store.renew_lease(worker["id"], now)
                return {"ok": True, "stop": False}
            store.finish(job_id, "failed", now, error="The worker sent conflicting chunks.")
            _misbehaved(job, worker["id"], now, "sent conflicting chunks")
            return {"ok": False, "stop": True}
        previous = store.chunk(job_id, expected - 1) if expected else None
        if seq > expected or (previous is not None and previous["done"]):
            store.finish(job_id, "failed", now, error="The worker sent chunks out of order.")
            _misbehaved(job, worker["id"], now, "sent chunks out of order")
            return {"ok": False, "stop": True}
        budget = config.chat_max_response_bytes - job["stream_bytes"]
        if size > budget:
            # Keep what fits and end the answer here, as the local path does.
            thinking = _truncate_utf8(thinking, budget)
            content = _truncate_utf8(content, budget - len(thinking.encode("utf-8")))
            store.insert_chunk(job_id, seq, content, True, now, thinking=thinking)
            store.advance(job_id, len(content.encode("utf-8")) + len(thinking.encode("utf-8")), now)
            store.finish(job_id, "done", now, finish_reason="length", only_from=store.ACTIVE)
            return {"ok": True, "stop": True}
        store.insert_chunk(job_id, seq, content, done, now, thinking=thinking)
        store.advance(job_id, size, now)
    return {"ok": True, "stop": False}


def _count(value, name) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**31 - 1:
        raise InvalidRequest(f"Invalid {name}.")
    return value


def prompt_token_cap(messages_json: str | None) -> int:
    """The most prompt tokens a worker may report for a job (see PROMPT_ESTIMATE_FACTOR)."""
    from bananachat.db.credits import IMAGE_TOKEN_ESTIMATE, estimate_tokens

    try:
        messages = json.loads(messages_json or "[]")
    except ValueError:
        messages = []
    estimate = 0
    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, dict):
            estimate += estimate_tokens(str(message.get("content") or ""))
            estimate += IMAGE_TOKEN_ESTIMATE * len(message.get("images") or ())
    return PROMPT_ESTIMATE_FACTOR * estimate + PROMPT_TOKEN_ALLOWANCE


def complete(worker, job_id: str, body, config=None) -> dict:
    config = _config(config)
    if not isinstance(body, dict):
        raise InvalidRequest("Expected a JSON object.")
    tokens_in, tokens_out = _count(body.get("tokens_in"), "tokens_in"), _count(body.get("tokens_out"), "tokens_out")
    reason = body.get("finish_reason") or "stop"
    if not isinstance(reason, str) or len(reason) > 32:
        raise InvalidRequest("Invalid finish_reason.")
    reason = reason if reason in ("stop", "length") else "stop"
    now = time.time()
    with db.transaction():
        job = _load_owned(worker, job_id)
        if job["status"] == "done":
            return {"ok": True, "stop": False}
        if not _owned(job, worker, config):
            return {"ok": False, "stop": True}
        last = store.chunk(job_id, job["next_chunk_seq"] - 1) if job["next_chunk_seq"] else None
        if last is None or not last["done"]:
            store.finish(job_id, "failed", now, error="The worker ended without a completion marker.")
            _misbehaved(job, worker["id"], now, "ended a job without a completion marker")
            return {"ok": False, "stop": True}
        tokens_in = min(tokens_in, prompt_token_cap(job["messages"]))
        tokens_out = min(tokens_out, int(job["stream_bytes"] or 0) + ANSWER_TOKEN_ALLOWANCE)
        store.finish(job_id, "done", now, tokens_in=tokens_in, tokens_out=tokens_out, finish_reason=reason,
                     only_from=store.ACTIVE)
        store.clear_pause(worker["id"])
    return {"ok": True, "stop": False}


def fail(worker, job_id: str, body, config=None) -> dict:
    """Record a failure; ``{"requeue": true}`` or ``{"error": "deferred"}`` before any chunk re-queues."""
    config = _config(config)
    if not isinstance(body, dict):
        raise InvalidRequest("Expected a JSON object.")
    error = body.get("error", "")
    if error is None:
        error = ""
    if not isinstance(error, str) or not isinstance(body.get("requeue", False), bool):
        raise InvalidRequest("Expected an error message.")
    requeue = body.get("requeue", False) or error.strip().lower() == "deferred"
    now = time.time()
    with db.transaction():
        job = _load_owned(worker, job_id)
        if job["status"] not in store.ACTIVE:
            return {"ok": False, "stop": True}
        if requeue and job["next_chunk_seq"] == 0 and not job["stop_requested"]:
            requeued = _requeue(job, now)
            if requeued:
                log.info("Worker %s deferred job %s; it is back in the pool", worker["name"], job_id[:8])
        else:
            # One printable line: the text reaches the server log and API clients.
            message = " ".join("".join(char if char.isprintable() else " " for char in error).split())
            if store.finish(job_id, "failed", now, error=message[:500] or "The worker failed.") \
                    and not job["stop_requested"]:
                if _request_fault(message, job):
                    store.set_fault(job_id, "request")
                    _request_faults_add_up(job, worker, now)
                else:
                    _blame(job, now, "reported a failure")
            requeued = False
    if requeued:
        _announce()
    return {"ok": True, "stop": True}


# ----- administration ------------------------------------------------------------------------

def valid_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME_LENGTH or any(ord(char) < 32 for char in name):
        raise InvalidRequest(f"Enter a worker name of up to {MAX_NAME_LENGTH} characters.")
    return name


def register_worker(name: str) -> tuple[str, str]:
    """Create a worker; returns ``(worker_id, token)``. Only the token's hash is stored."""
    name = valid_name(name)
    worker_id = uuid.uuid4().hex
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    store.insert_worker(worker_id, name, store.hash_token(token))
    return worker_id, token


def set_enabled(worker_id: str, enabled: bool) -> bool:
    now = time.time()
    with db.transaction():
        if store.get(worker_id) is None:
            return False
        store.set_status(worker_id, "offline" if enabled else "disabled")
        if enabled:
            store.clear_pause(worker_id)  # an administrator vouches for it again
        else:
            store.finish_worker_jobs(worker_id, "The worker was disabled.", now)
            from bananachat.db import worker_remote as remote_store

            remote_store.end_worker_tunnels(worker_id, "The worker was disabled.", now)
    return True


def delete_worker(worker_id: str) -> bool:
    now = time.time()
    with db.transaction():
        if store.get(worker_id) is None:
            return False
        store.finish_worker_jobs(worker_id, "The worker was removed.", now)
        store.delete(worker_id)
    return True


def describe(worker, now: float | None = None, busy: set | None = None) -> dict:
    """What the administrator page shows about a worker."""
    now = time.time() if now is None else now
    last = _field(worker, "last_heartbeat")
    status = worker["status"]
    if status != "disabled" and (last is None or float(last) < now - HEARTBEAT_FRESH_SECONDS):
        status = "offline"
    return {
        "id": worker["id"],
        "name": worker["name"],
        "status": status,
        "activity": _field(worker, "activity_state") if status != "offline" else None,
        "working": bool(busy and worker["id"] in busy),
        # Paused after failed or stalled jobs (see the module docstring).
        "paused": status != "disabled" and _paused(worker, now),
        "gpu_name": _field(worker, "gpu_name"),
        "gpu_util": round(float(worker["gpu_util"])) if _field(worker, "gpu_util") is not None else None,
        "ollama_version": _field(worker, "ollama_version"),
        "platform": _field(worker, "platform"),
        "models": advertised_models(worker),
        "last_heartbeat": db.timestamp(_epoch(last)) if last else None,
        "seconds_since_heartbeat": int(now - float(last)) if last else None,
        "registered_at": _field(worker, "registered_at"),
    }


def _epoch(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(value), timezone.utc)


def overview() -> list[dict]:
    now = time.time()
    busy = store.workers_with_active_jobs()
    return [describe(worker, now, busy) for worker in store.list_all()]


# ----- housekeeping ---------------------------------------------------------------------------

def maintain(config=None) -> dict:
    """Time out stale jobs and forget finished ones (run by the ``worker-jobs`` background job)."""
    config = _config(config)
    now = time.time()
    lease = lease_seconds(config)
    timed_out = 0
    with db.transaction():
        timed_out += store.expire_pending(now - max(120.0, config.worker_claim_timeout * 3.0), now)
        for job in store.active_jobs():
            if lease_expired(job, now, lease, config.first_token_timeout) and store.finish(
                    job["id"], "timeout", now, error="The worker stopped responding.", only_from=store.ACTIVE):
                timed_out += 1
                _lease_ran_out(job, now, "stopped responding")
        timed_out += store.expire_created_before(now - (config.generation_timeout + 120.0), now)
    with db.transaction():
        store.scrub_terminal()
        chunks = store.delete_orphan_chunks(now - 600)
        purged = store.delete_terminal_before(now - PURGE_AFTER_SECONDS)
    return {"timed_out": timed_out, "purged": purged, "chunks": chunks}


@background.job("worker-jobs", every=60, initial_delay=20)
def _maintain_jobs(app) -> None:
    result = maintain(app.config["BC"])
    if result["timed_out"] or result["purged"]:
        log.info("Worker jobs: %(timed_out)d timed out, %(purged)d finished jobs removed", result)
