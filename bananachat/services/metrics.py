"""Compute snapshots and metrics retention (background jobs).

Every ``BC_COMPUTE_SNAPSHOT_INTERVAL`` seconds the background leader records
what the inference server is doing: running models and their memory (Ollama
``/api/ps``), GPU telemetry from ``nvidia-smi`` (only when Ollama runs on this
host, otherwise it would describe the wrong machine), host CPU and RAM via
``psutil`` when installed, and the queue depth. Once a day rows older than
``BC_METRICS_RETENTION_DAYS`` are deleted. Collection never raises.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import timedelta

from bananachat import db
from bananachat.db import metrics as metrics_db
from bananachat.services import background, ollama
from bananachat.services.upstream import UpstreamError

log = logging.getLogger("bananachat.metrics")

SOURCE_LABELS = {
    "nvidia-smi": "NVIDIA telemetry",
    "ollama-allocation": "Ollama model allocation",
    "unavailable": "GPU telemetry unavailable",
    "legacy-system-ram": "Older sample (host memory)",
}


def nvidia_metrics() -> dict | None:
    """Aggregate telemetry of the local NVIDIA GPUs, or None."""
    binary = shutil.which("nvidia-smi")
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            rows.append((parts[0][:80], float(parts[1]), float(parts[2]), float(parts[3])))
        except ValueError:
            continue
    if not rows:
        return None
    total = sum(row[2] for row in rows)
    return {"gpu_name": " + ".join(row[0] for row in rows)[:200], "gpu_memory_used_mb": sum(row[1] for row in rows),
            "gpu_memory_total_mb": total,
            "gpu_utilization_percent": (sum(row[3] * row[2] for row in rows) / total) if total else None,
            "metrics_source": "nvidia-smi"}


def host_metrics() -> dict:
    try:
        import psutil
    except ImportError:
        return {}
    try:
        memory = psutil.virtual_memory()
        return {"cpu_percent": psutil.cpu_percent(interval=0.1), "system_memory_used_mb": memory.used / 1024 ** 2,
                "system_memory_total_mb": memory.total / 1024 ** 2}
    except (OSError, RuntimeError, AttributeError):
        return {}


def collect_snapshot(config) -> dict:
    """Gather one snapshot (never raises)."""
    try:
        running = ollama.list_running(config)
    except (UpstreamError, OSError, ValueError):
        running = []
    names = [str(item.get("name") or item.get("model")) for item in running if item.get("name") or item.get("model")]
    values = nvidia_metrics() if config.ollama_is_local else None
    if values is None:
        allocated = sum(float(item.get("size_vram") or 0) for item in running
                        if isinstance(item.get("size_vram"), (int, float))) / 1024 ** 2
        values = {"gpu_name": None, "gpu_memory_used_mb": allocated or None, "gpu_memory_total_mb": None,
                  "gpu_utilization_percent": None,
                  "metrics_source": "ollama-allocation" if allocated else "unavailable"}
    values.update(host_metrics())
    try:
        values["queue_depth"] = db.scalar("SELECT COUNT(*) FROM inference_queue", default=0)
    except Exception:  # noqa: BLE001
        values["queue_depth"] = 0
    values["active_models"] = names
    return values


def record_snapshot(config) -> None:
    values = collect_snapshot(config)
    metrics_db.record_snapshot(**values)


def purge(config) -> tuple[int, int]:
    cutoff = db.now(-timedelta(days=config.metrics_retention_days))
    return metrics_db.purge_before(cutoff)


@background.job("compute-snapshots", every=lambda app: app.config["BC"].compute_snapshot_interval, initial_delay=15)
def snapshot_job(app) -> None:
    try:
        record_snapshot(app.config["BC"])
    except Exception as error:  # noqa: BLE001 - metrics must never disturb the server
        log.debug("Compute snapshot skipped: %s", error)


@background.job("metrics-retention", every=86400, initial_delay=300)
def retention_job(app) -> None:
    requests, snapshots = purge(app.config["BC"])
    if requests or snapshots:
        log.info("Removed %d request metrics and %d compute snapshots past retention", requests, snapshots)
