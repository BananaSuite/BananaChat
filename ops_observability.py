"""Lightweight in-process observability helpers for reliability triage.

The counters in this module are intentionally process-local and dependency-free.
They provide low-overhead visibility into recurring failure patterns (5xx and
SQLite contention/latency buckets) without requiring an external metrics stack.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List

_LOCK = threading.Lock()

_COUNTERS = Counter()
_SLOW_SQL_BUCKETS = Counter()
_SQLITE_RETRY_BUCKETS = Counter()
_RECENT_EVENTS: List[dict] = []
_MAX_RECENT_EVENTS = 200

_SLOW_SQL_THRESHOLDS_MS = (10, 50, 100, 250, 500, 1000, 5000)
_SQLITE_RETRY_THRESHOLDS_MS = (50, 100, 250, 500, 1000, 2000, 5000)
_SQL_SAMPLE_MAX_LENGTH = 140

_WS_RE = re.compile(r"\s+")

# Per-(surface, status_code) cap on how many distinct route and upstream
# strings get their own counter key.  Without a cap a misbehaving bot
# (or a pathological scan) could 500 across thousands of unique URLs
# and grow ``_COUNTERS`` indefinitely: the in-process Counter is the
# only counter store we have, so leaks here surface as slow worker
# memory growth and eventual OOM.  When the cap is reached additional
# distinct routes/upstreams are folded into an ``<overflow>`` key and a
# dedicated overflow counter is incremented so operators still see
# *that* high-cardinality 5xx traffic is happening.
_MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET = 64
_route_key_pools: Dict[tuple, set] = {}
_upstream_key_pools: Dict[tuple, set] = {}


def _bucket_label(value_ms: float, thresholds_ms) -> str:
    """Return a human-readable bucket label for *value_ms*."""
    for upper in thresholds_ms:
        if value_ms <= upper:
            return f"<= {upper}ms"
    return f"> {thresholds_ms[-1]}ms"


def _trim_events() -> None:
    """Keep recent events bounded in memory."""
    if len(_RECENT_EVENTS) <= _MAX_RECENT_EVENTS:
        return
    overflow = len(_RECENT_EVENTS) - _MAX_RECENT_EVENTS
    del _RECENT_EVENTS[0:overflow]


def _record_event(kind: str, payload: dict) -> None:
    """Store a bounded recent event payload."""
    event = {
        "kind": kind,
        "at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    with _LOCK:
        _RECENT_EVENTS.append(event)
        _trim_events()


def _bounded_key(pool_map, bucket_key, value, max_keys):
    """Return *value* if recording it stays under the cap, else ``"<overflow>"``.

    Per-(surface, status) pools cap how many distinct ``value`` strings
    are admitted into ``_COUNTERS``.  ``value`` is admitted unconditionally
    if it's already in the pool, otherwise it's admitted only if the pool
    still has headroom.  Once the cap is reached additional unique
    values collapse onto the literal ``"<overflow>"`` token so the
    counter still reflects that high-cardinality traffic is happening,
    just without leaking memory.

    Must be called while holding ``_LOCK``.
    """
    pool = pool_map.get(bucket_key)
    if pool is None:
        pool = set()
        pool_map[bucket_key] = pool
    if value in pool:
        return value
    if len(pool) < max_keys:
        pool.add(value)
        return value
    return "<overflow>"


def record_http_error(surface: str, status_code: int, *, route: str = "", upstream: str = "") -> None:
    """Increment structured HTTP error counters.

    ``surface`` should identify where the error originated (e.g. ``wiki``,
    ``hosting_portal``, ``hosting_proxy``).

    High-cardinality keys (``route`` and ``upstream``) are bounded per
    ``(surface, status_code)`` bucket via :func:`_bounded_key` so a 5xx
    flood across many unique URLs cannot grow ``_COUNTERS`` indefinitely.
    """
    route = (route or "").strip() or "<unknown>"
    upstream = (upstream or "").strip() or "<none>"
    with _LOCK:
        bucket_key = (surface, int(status_code))
        capped_route = _bounded_key(
            _route_key_pools,
            bucket_key,
            route,
            _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET,
        )
        _COUNTERS["http_error_total"] += 1
        _COUNTERS[f"http_error_{status_code}_total"] += 1
        _COUNTERS[f"http_error_surface:{surface}"] += 1
        _COUNTERS[f"http_error_surface:{surface}:status:{status_code}"] += 1
        _COUNTERS[f"http_error_surface:{surface}:status:{status_code}:route:{capped_route}"] += 1
        if capped_route == "<overflow>":
            _COUNTERS[f"http_error_surface:{surface}:status:{status_code}:route_overflow_total"] += 1
        if upstream != "<none>":
            capped_upstream = _bounded_key(
                _upstream_key_pools,
                bucket_key,
                upstream,
                _MAX_HIGH_CARDINALITY_KEYS_PER_BUCKET,
            )
            _COUNTERS[f"http_error_surface:{surface}:status:{status_code}:upstream:{capped_upstream}"] += 1
            if capped_upstream == "<overflow>":
                _COUNTERS[f"http_error_surface:{surface}:status:{status_code}:upstream_overflow_total"] += 1
    _record_event(
        "http_error",
        {
            "surface": surface,
            "status_code": int(status_code),
            "route": route,
            "upstream": upstream,
        },
    )


def record_db_recovery(
    *,
    db_path: str,
    corrupt_path: str,
    mode: str,
    success: bool,
) -> None:
    """Record a malformed-DB recovery attempt.

    ``mode`` is ``"iterdump"`` when salvageable rows were recovered or
    ``"empty"`` when the corrupt file could not be salvaged and the
    slot was left for schema re-init.  Emitted as a discrete event so
    operators can immediately spot corruption in their dashboards.
    """
    db_path = (db_path or "").strip() or "<unknown>"
    corrupt_path = (corrupt_path or "").strip() or "<unknown>"
    mode = (mode or "").strip() or "<unknown>"
    with _LOCK:
        _COUNTERS["sqlite_recovery_attempt_total"] += 1
        if success:
            _COUNTERS["sqlite_recovery_success_total"] += 1
        else:
            _COUNTERS["sqlite_recovery_failure_total"] += 1
        _COUNTERS[f"sqlite_recovery_mode:{mode}"] += 1
    _record_event(
        "sqlite_recovery",
        {
            "db_path": db_path,
            "corrupt_path": corrupt_path,
            "mode": mode,
            "success": bool(success),
        },
    )


def record_sqlite_retry(operation: str, attempts: int, elapsed_ms: float, *, exhausted: bool = False) -> None:
    """Record SQLite retry/backoff observability data."""
    operation = (operation or "").strip() or "<unknown>"
    attempts = max(0, int(attempts))
    elapsed_ms = max(0.0, float(elapsed_ms))
    bucket = _bucket_label(elapsed_ms, _SQLITE_RETRY_THRESHOLDS_MS)
    with _LOCK:
        _COUNTERS["sqlite_retry_events_total"] += 1
        _COUNTERS[f"sqlite_retry_operation:{operation}"] += 1
        _COUNTERS[f"sqlite_retry_attempts:{attempts}"] += 1
        if exhausted:
            _COUNTERS["sqlite_retry_exhausted_total"] += 1
            _COUNTERS[f"sqlite_retry_exhausted_operation:{operation}"] += 1
        _SQLITE_RETRY_BUCKETS[bucket] += 1
    _record_event(
        "sqlite_retry",
        {
            "operation": operation,
            "attempts": attempts,
            "elapsed_ms": round(elapsed_ms, 3),
            "bucket": bucket,
            "exhausted": bool(exhausted),
        },
    )


def _normalize_sql(sql: str, max_len: int = _SQL_SAMPLE_MAX_LENGTH) -> str:
    """Return a compact SQL sample safe for logs and in-memory events."""
    # Values never belong in diagnostic samples, even for literal legacy SQL.
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    text = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b[0-9]+(?:\.[0-9]+)?\b", "?", text)
    text = _WS_RE.sub(" ", text.strip())
    if not text:
        return "<empty>"
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def record_sql_timing(sql: str, elapsed_ms: float) -> None:
    """Record SQL query latency into buckets."""
    elapsed_ms = max(0.0, float(elapsed_ms))
    bucket = _bucket_label(elapsed_ms, _SLOW_SQL_THRESHOLDS_MS)
    sample = _normalize_sql(sql)
    with _LOCK:
        _COUNTERS["sql_query_total"] += 1
        _SLOW_SQL_BUCKETS[bucket] += 1
        if elapsed_ms >= 250:
            _COUNTERS["sql_slow_query_total"] += 1
            _COUNTERS[f"sql_slow_query_bucket:{bucket}"] += 1
    if elapsed_ms >= 250:
        _record_event(
            "sql_slow",
            {
                "elapsed_ms": round(elapsed_ms, 3),
                "bucket": bucket,
                "sql": sample,
            },
        )


def get_observability_snapshot() -> Dict[str, object]:
    """Return a point-in-time snapshot of all counters and recent events."""
    with _LOCK:
        counters = dict(_COUNTERS)
        slow_sql_buckets = dict(_SLOW_SQL_BUCKETS)
        sqlite_retry_buckets = dict(_SQLITE_RETRY_BUCKETS)
        recent_events = list(_RECENT_EVENTS)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "counters": counters,
        "sql_timing_buckets": slow_sql_buckets,
        "sqlite_retry_timing_buckets": sqlite_retry_buckets,
        "recent_events": recent_events,
    }


def reset_observability_snapshot() -> None:
    """Reset in-memory counters (used by tests)."""
    with _LOCK:
        _COUNTERS.clear()
        _SLOW_SQL_BUCKETS.clear()
        _SQLITE_RETRY_BUCKETS.clear()
        _RECENT_EVENTS.clear()
        _route_key_pools.clear()
        _upstream_key_pools.clear()
