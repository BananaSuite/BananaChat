"""Usage and compute metrics with a CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Response, current_app, render_template, request, stream_with_context

from bananachat import db
from bananachat.db import metrics as metrics_db
from bananachat.security import admin_required
from bananachat.services import metrics as metrics_service  # noqa: F401  (registers the background jobs)

from . import bp
from ._helpers import audit

RANGES = {"24h": (timedelta(hours=24), "hour", "Last 24 hours"),
          "7d": (timedelta(days=7), "day", "Last 7 days"),
          "30d": (timedelta(days=30), "day", "Last 30 days")}
CHART_WIDTH, CHART_HEIGHT = 720, 180


def _range():
    key = request.args.get("range", "24h")
    if key not in RANGES:
        key = "24h"
    span, bucket, label = RANGES[key]
    return key, span, bucket, label


def _buckets(span: timedelta, bucket: str) -> list[str]:
    now = datetime.now(timezone.utc)
    if bucket == "hour":
        start = (now - span).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step, fmt = timedelta(hours=1), "%Y-%m-%d %H"
    else:
        start = (now - span).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        step, fmt = timedelta(days=1), "%Y-%m-%d"
    keys = []
    while start <= now:
        keys.append(start.strftime(fmt))
        start += step
    return keys


def _chart(rows, span, bucket) -> dict:
    by_key = {row["bucket"]: row for row in rows}
    keys = _buckets(span, bucket)
    values = [(key, by_key[key]["requests"] if key in by_key else 0, by_key[key]["errors"] if key in by_key else 0)
              for key in keys]
    peak = max([count for _key, count, _errors in values] + [1])
    slot = CHART_WIDTH / max(1, len(values))
    gap = 2 if slot > 6 else 1
    bars = []
    for index, (key, count, errors) in enumerate(values):
        height = 0 if count == 0 else max(2.0, count / peak * (CHART_HEIGHT - 4))
        label = f"{key}:00 UTC" if bucket == "hour" else key
        bars.append({"x": round(index * slot + gap / 2, 2), "width": round(max(1.0, slot - gap), 2),
                     "y": round(CHART_HEIGHT - height, 2), "height": round(height, 2), "label": label,
                     "count": count, "errors": errors})
    ticks = [bars[0], bars[len(bars) // 2], bars[-1]] if bars else []
    return {"bars": bars, "peak": peak, "width": CHART_WIDTH, "height": CHART_HEIGHT, "ticks": ticks,
            "total": sum(bar["count"] for bar in bars)}


@bp.get("/metrics", endpoint="metrics")
@admin_required
def show():
    key, span, bucket, label = _range()
    since = db.now(-span)
    snapshots = metrics_db.snapshots(since, limit=48)
    latest = metrics_db.latest_snapshot()
    config = current_app.config["BC"]
    stale_after = max(90, config.compute_snapshot_interval * 3)
    stale = latest is None or (datetime.now(timezone.utc) - db.parse_timestamp(latest["recorded_at"])) \
        > timedelta(seconds=stale_after)
    return render_template(
        "admin/metrics.html", section="metrics", range_key=key, range_label=label, ranges=RANGES,
        summary=metrics_db.summary(since), by_type=metrics_db.by_type(since), by_model=metrics_db.by_model(since),
        by_status=metrics_db.by_status(since), chart=_chart(metrics_db.series(since, bucket), span, bucket),
        snapshots=list(reversed(snapshots)), latest=latest, stale=stale, peaks=metrics_db.snapshot_peaks(since),
        source_labels=metrics_service.SOURCE_LABELS, retention_days=config.metrics_retention_days,
        interval=config.compute_snapshot_interval)


def _cell(value):
    """Neutralise spreadsheet formulas in exported text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@bp.get("/metrics/export.csv", endpoint="metrics_export")
@admin_required
def export():
    key, span, _bucket, _label = _range()
    since = db.now(-span)
    audit("metrics_export", key)
    columns = ["id", "created_at", "request_type", "status", "model_id", "model_name", "user_id", "username",
               "tokens_in", "tokens_out", "duration_ms", "queue_wait_ms", "usage_estimated"]

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for index, row in enumerate(metrics_db.iter_requests(since), 1):
            writer.writerow([_cell(row[column]) for column in columns])
            if index % 500 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    response = Response(stream_with_context(generate()), mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="request-metrics-{key}-{stamp}.csv"'
    response.headers["Cache-Control"] = "private, no-store"
    return response
