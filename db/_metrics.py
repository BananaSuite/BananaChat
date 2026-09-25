"""Compute metrics storage."""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from ._connection import get_db_context, retry_on_busy


@retry_on_busy
def record_compute_snapshot(gpu_name=None, gpu_memory_used_mb=None, gpu_memory_total_mb=None,
                            gpu_utilization_percent=None, system_memory_used_mb=None,
                            system_memory_total_mb=None, metrics_source=None,
                            cpu_percent=None, active_models=None, queue_depth=0):
    now = datetime.now(timezone.utc).isoformat()
    active_json = json.dumps(active_models) if active_models else None
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO compute_snapshots "
            "(gpu_name, gpu_memory_used_mb, gpu_memory_total_mb, gpu_utilization_percent, "
            "system_memory_used_mb, system_memory_total_mb, metrics_source, cpu_percent, "
            "active_models, queue_depth, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (gpu_name, gpu_memory_used_mb, gpu_memory_total_mb, gpu_utilization_percent,
             system_memory_used_mb, system_memory_total_mb, metrics_source, cpu_percent,
             active_json, queue_depth, now),
        )
        conn.commit()


@retry_on_busy
def record_request_metric(request_type, model_id=None, user_id=None, tokens_in=0, tokens_out=0,
                          duration_ms=0, queue_wait_ms=0, status="ok", *, connection=None):
    now = datetime.now(timezone.utc).isoformat()
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        conn.execute(
            "INSERT INTO request_metrics "
            "(request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, queue_wait_ms, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (request_type, model_id, user_id, tokens_in, tokens_out,
             duration_ms, queue_wait_ms, status, now),
        )
        if connection is None:
            conn.commit()


@retry_on_busy
def get_compute_history(hours=24, limit=500):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_db_context() as conn:
        rows = conn.execute(
            "SELECT * FROM compute_snapshots WHERE recorded_at>=? ORDER BY recorded_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("active_models"):
            try:
                d["active_models"] = json.loads(d["active_models"])
            except Exception:
                d["active_models"] = []
        result.append(d)
    return result


@retry_on_busy
def get_request_stats(hours=24):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_db_context() as conn:
        return conn.execute(
            "SELECT rm.request_type, am.display_name AS model_name, "
            "COUNT(*) AS request_count, "
            "SUM(rm.tokens_in) AS total_tokens_in, "
            "SUM(rm.tokens_out) AS total_tokens_out, "
            "AVG(rm.duration_ms) AS avg_duration_ms, "
            "AVG(rm.queue_wait_ms) AS avg_queue_wait_ms, "
            "SUM(CASE WHEN rm.status!='ok' THEN 1 ELSE 0 END) AS error_count "
            "FROM request_metrics rm "
            "LEFT JOIN ai_models am ON rm.model_id=am.id "
            "WHERE rm.created_at>=? "
            "GROUP BY rm.request_type, rm.model_id "
            "ORDER BY request_count DESC",
            (cutoff,),
        ).fetchall()


@retry_on_busy
def get_latest_snapshot():
    with get_db_context() as conn:
        row = conn.execute(
            "SELECT * FROM compute_snapshots ORDER BY recorded_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["active_models"] = json.loads(result.get("active_models") or "[]")
    except (TypeError, ValueError):
        result["active_models"] = []
    return result


@retry_on_busy
def purge_old_metrics(days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db_context() as conn:
        conn.execute("DELETE FROM compute_snapshots WHERE recorded_at<?", (cutoff,))
        conn.execute("DELETE FROM request_metrics WHERE created_at<?", (cutoff,))
        conn.commit()
