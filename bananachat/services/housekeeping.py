"""Core background jobs: model catalog sync, inference health, expiry of old rows.

The model lifecycle (``services.model_lifecycle``) and downloads
(``services.pulls``) register their own jobs; importing them here makes sure
they exist in every process.
"""

from __future__ import annotations

import logging

from bananachat.db import users
from bananachat.services import background, health, model_lifecycle, ollama, pulls  # noqa: F401  (register jobs)
from bananachat.services.upstream import UpstreamError

log = logging.getLogger("bananachat.housekeeping")


@background.job("ollama-sync", every=lambda app: app.config["BC"].ollama_sync_interval, initial_delay=3)
def sync_ollama(app) -> None:
    config = app.config["BC"]
    if not config.ollama_is_local and health.inference_down():
        # The primary server is unreachable: nothing is marked missing meanwhile.
        return
    try:
        ollama.sync_catalog(config, source="background")
    except (UpstreamError, OSError) as error:
        log.info("Ollama model sync skipped: %s", error)


@background.job("inference-health", every=lambda app: app.config["BC"].inference_health_interval, initial_delay=2)
def probe_inference(app) -> None:
    config = app.config["BC"]
    if config.ollama_is_local:
        return
    try:
        ollama.version(config, timeout=5, primary=True)
        ok, error = True, ""
    except (UpstreamError, OSError) as failure:
        ok, error = False, str(failure)
    before = health.status().get("down")
    state = health.record_probe(ok, config.inference_health_failures, error)
    if state["down"] and not before:
        log.error("The inference server is unreachable: %s", error)
    elif before and not state["down"]:
        log.warning("The inference server is reachable again")


@background.job("model-recovery-check", every=60, initial_delay=20)
def check_model_recovery(app) -> None:
    """After a restore, or when the compute server lost published models, see what is missing."""
    config = app.config["BC"]
    if not config.ollama_is_local and health.inference_down():
        return
    from bananachat.services import model_recovery
    model_recovery.check(config)


@background.job("expire-sessions", every=3600, initial_delay=30)
def expire(app) -> None:
    users.purge_expired_sessions()
    users.purge_rate_limits()
