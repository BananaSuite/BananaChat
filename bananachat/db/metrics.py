"""Request metrics and compute snapshots.

Recording a finished request (chat, playground, API, image)::

    from bananachat.db import metrics

    metrics.record_request(
        "chat",                    # request type: chat, chat_incognito, playground, api, image, ...
        model_id=model["id"],      # ai_models.id or None
        user_id=user["id"],        # users.id or None
        tokens_in=12, tokens_out=340,
        duration_ms=5120,          # total time including the queue wait
        queue_wait_ms=80,          # time spent waiting for a free inference slot
        status="ok",               # ok, error, cancelled, timeout, ...
        usage_estimated=False,     # True when token counts were estimated
    )

The call joins the caller's transaction when one is open and never raises for
out-of-range numbers (they are clamped). Callers may also insert into
``request_metrics`` directly; timestamps must use ``db.now()``.

Compute snapshots (GPU, memory, running models) are recorded by
``services.metrics`` in the background. Both tables are pruned after
``BC_METRICS_RETENTION_DAYS``.
"""

from __future__ import annotations

import json
import math

from bananachat import db

MAX_INT = 2**53


def _count(value) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number < 0:
        return 0
    return int(min(number, MAX_INT))


def _label(value, default: str) -> str:
    text = str(value or "").strip()[:32]
    return text or default


# ----- requests -------------------------------------------------------------

def record_request(request_type: str, *, model_id=None, user_id=None, tokens_in=0, tokens_out=0,
                   duration_ms=0, queue_wait_ms=0, status="ok", usage_estimated=False) -> None:
    """Record one finished request (see the module docstring)."""
    db.execute(
        "INSERT INTO request_metrics (request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, "
        "queue_wait_ms, status, created_at, usage_estimated) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_label(request_type, "unknown"), model_id, user_id, _count(tokens_in), _count(tokens_out),
         _count(duration_ms), _count(queue_wait_ms), _label(status, "ok"), db.now(), 1 if usage_estimated else 0),
    )


def summary(since: str) -> dict:
    """Totals for requests recorded at or after *since*."""
    row = db.one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(tokens_in), 0) AS tokens_in, "
        "COALESCE(SUM(tokens_out), 0) AS tokens_out, AVG(duration_ms) AS avg_duration_ms, "
        "AVG(queue_wait_ms) AS avg_queue_wait_ms, "
        "COALESCE(SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END), 0) AS errors, "
        "COALESCE(SUM(usage_estimated), 0) AS estimated "
        "FROM request_metrics WHERE created_at>=?", (since,))
    return row.to_dict() if row else {"requests": 0, "tokens_in": 0, "tokens_out": 0, "avg_duration_ms": None,
                                      "avg_queue_wait_ms": None, "errors": 0, "estimated": 0}


def _grouped(column_sql: str, since: str, *, join: str = "", limit: int = 50):
    return db.query(
        f"SELECT {column_sql} AS label, COUNT(*) AS requests, COALESCE(SUM(r.tokens_in), 0) AS tokens_in, "
        "COALESCE(SUM(r.tokens_out), 0) AS tokens_out, AVG(r.duration_ms) AS avg_duration_ms, "
        "AVG(r.queue_wait_ms) AS avg_queue_wait_ms, "
        "COALESCE(SUM(CASE WHEN r.status<>'ok' THEN 1 ELSE 0 END), 0) AS errors "
        f"FROM request_metrics r {join} WHERE r.created_at>=? GROUP BY label ORDER BY requests DESC LIMIT ?",
        (since, limit))


def by_type(since: str):
    return _grouped("r.request_type", since)


def by_status(since: str):
    return _grouped("r.status", since)


def by_model(since: str, limit: int = 50):
    return _grouped("COALESCE(m.display_name, m.ollama_name, '(deleted model)')", since,
                    join="LEFT JOIN ai_models m ON m.id=r.model_id", limit=limit)


def series(since: str, bucket: str):
    """Requests and tokens per ``hour`` or ``day`` bucket (UTC)."""
    length = 13 if bucket == "hour" else 10
    return db.query(
        f"SELECT substr(created_at, 1, {length}) AS bucket, COUNT(*) AS requests, "
        "COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens, "
        "COALESCE(SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END), 0) AS errors "
        "FROM request_metrics WHERE created_at>=? GROUP BY bucket ORDER BY bucket", (since,))


def iter_requests(since: str, batch: int = 1000):
    """Yield request rows (oldest first) in batches so exports use bounded memory."""
    last_id = 0
    while True:
        rows = db.query(
            "SELECT r.id, r.created_at, r.request_type, r.status, r.model_id, m.ollama_name AS model_name, "
            "r.user_id, u.username, r.tokens_in, r.tokens_out, r.duration_ms, r.queue_wait_ms, r.usage_estimated "
            "FROM request_metrics r LEFT JOIN ai_models m ON m.id=r.model_id LEFT JOIN users u ON u.id=r.user_id "
            "WHERE r.created_at>=? AND r.id>? ORDER BY r.id LIMIT ?", (since, last_id, batch))
        if not rows:
            return
        yield from rows
        last_id = rows[-1]["id"]


# ----- compute snapshots ----------------------------------------------------

SNAPSHOT_FIELDS = ("gpu_name", "gpu_memory_used_mb", "gpu_memory_total_mb", "gpu_utilization_percent",
                   "system_memory_used_mb", "system_memory_total_mb", "metrics_source", "cpu_percent")


def record_snapshot(*, active_models: list[str] | None = None, queue_depth: int = 0, **values) -> None:
    unknown = set(values) - set(SNAPSHOT_FIELDS)
    if unknown:
        raise ValueError(f"Unknown snapshot fields: {', '.join(sorted(unknown))}")
    row = {name: values.get(name) for name in SNAPSHOT_FIELDS}
    db.execute(
        "INSERT INTO compute_snapshots (gpu_name, gpu_memory_used_mb, gpu_memory_total_mb, gpu_utilization_percent, "
        "system_memory_used_mb, system_memory_total_mb, metrics_source, cpu_percent, active_models, queue_depth, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (*row.values(), json.dumps(list(active_models or [])[:100]), _count(queue_depth), db.now()))


def _decode(row) -> dict:
    item = row.to_dict()
    try:
        models = json.loads(item.get("active_models") or "[]")
    except ValueError:
        models = []
    item["active_models"] = [str(name) for name in models if isinstance(name, str)] if isinstance(models, list) else []
    return item


def latest_snapshot() -> dict | None:
    row = db.one("SELECT * FROM compute_snapshots ORDER BY recorded_at DESC, id DESC LIMIT 1")
    return _decode(row) if row else None


def snapshots(since: str, limit: int = 2000) -> list[dict]:
    """Snapshots since *since*, oldest first, thinned evenly to at most *limit* rows."""
    total = db.scalar("SELECT COUNT(*) FROM compute_snapshots WHERE recorded_at>=?", (since,), 0)
    step = max(1, math.ceil(total / limit)) if limit else 1
    rows = db.query(
        "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY recorded_at, id) AS n FROM compute_snapshots "
        "WHERE recorded_at>=?) WHERE (n - 1) % ? = 0 ORDER BY recorded_at, id", (since, step))
    return [_decode(row) for row in rows]


def snapshot_peaks(since: str) -> dict:
    row = db.one(
        "SELECT MAX(gpu_memory_used_mb) AS gpu_used, MAX(gpu_memory_total_mb) AS gpu_total, "
        "MAX(gpu_utilization_percent) AS gpu_util, MAX(system_memory_used_mb) AS ram_used, "
        "MAX(system_memory_total_mb) AS ram_total, MAX(cpu_percent) AS cpu, MAX(queue_depth) AS queue, "
        "COUNT(*) AS samples FROM compute_snapshots WHERE recorded_at>=?", (since,))
    return row.to_dict() if row else {}


# ----- retention ------------------------------------------------------------

def purge_before(cutoff: str, batch: int = 5000) -> tuple[int, int]:
    """Delete metrics older than *cutoff* in small batches (short write locks)."""
    removed = []
    for table, column in (("request_metrics", "created_at"), ("compute_snapshots", "recorded_at")):
        total = 0
        while True:
            count = db.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {column}<? LIMIT ?)",
                (cutoff, batch)).rowcount
            total += count
            if count < batch:
                break
        removed.append(total)
    return removed[0], removed[1]
