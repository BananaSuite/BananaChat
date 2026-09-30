"""Inference-server health shared by every worker process.

The background leader probes a remote Ollama server and records the result
in ``runtime_state``; each process reads it with a short cache, so outage
handling behaves the same whichever process serves a request.
"""

from __future__ import annotations

import threading
import time

from bananachat.db import settings as site_settings

STATE_KEY = "inference_health"
_lock = threading.Lock()
_cache = {"at": 0.0, "down": False}


def inference_down(max_age: float = 5.0) -> bool:
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < max_age:
            return _cache["down"]
    state = site_settings.state_get(STATE_KEY, {}) or {}
    down = bool(state.get("down"))
    with _lock:
        _cache.update(at=now, down=down)
    return down


def status() -> dict:
    return site_settings.state_get(STATE_KEY, {}) or {}


def record_probe(ok: bool, failures_to_trip: int, error: str = "") -> dict:
    """Update the shared health state after one probe."""
    state = status()
    failures = 0 if ok else int(state.get("failures", 0)) + 1
    down = failures >= failures_to_trip
    new_state = {"down": down, "failures": failures, "checked_at": time.time(),
                 "since": state.get("since") if (down and state.get("down")) else (time.time() if down else None),
                 "error": "" if ok else error[:300]}
    site_settings.state_set(STATE_KEY, new_state)
    with _lock:
        _cache.update(at=time.monotonic(), down=down)
    return new_state


def reset() -> None:
    site_settings.state_delete(STATE_KEY)
    with _lock:
        _cache.update(at=0.0, down=False)


def using_fallback(config) -> bool:
    """Whether requests go to ``BC_INFERENCE_FALLBACK_URL`` right now."""
    return config.fallback_enabled and not config.ollama_is_local and inference_down()


def ollama_url(config) -> str:
    """The Ollama base URL to use now (the fallback while the primary is down)."""
    return config.inference_fallback_url if using_fallback(config) else config.ollama_url
