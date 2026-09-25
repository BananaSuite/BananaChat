"""Rate limiting helpers."""

import functools
import ipaddress
import logging
import threading
import time
from collections import defaultdict

from flask import request, flash, jsonify, abort

import db
from logger import log_action

# In-process fallback, used only when the SQLite rate-limit table is
# unreachable (disk full, DB corruption). It counts per worker rather than
# across workers, so up to max_requests × num_workers requests can slip
# through before limits kick in. That is vastly safer than failing fully open
# with no limiting at all.
_fallback_lock = threading.Lock()
_fallback_store: dict = defaultdict(list)   # key -> list of monotonic timestamps
_FALLBACK_MAX_KEYS = 5000                   # cap memory usage


def _check_rate_limit_memory(key: str, max_requests: int, window: int) -> bool:
    """Sliding-window rate check using per-process memory."""
    now = time.monotonic()
    cutoff = now - window
    with _fallback_lock:
        hits = _fallback_store[key]
        # Prune expired entries in-place
        i = 0
        while i < len(hits) and hits[i] < cutoff:
            i += 1
        if i:
            del hits[:i]
        if len(hits) >= max_requests:
            return False
        hits.append(now)
        # Evict oldest entry if store is growing unbounded
        if len(_fallback_store) > _FALLBACK_MAX_KEYS:
            try:
                oldest = next(iter(_fallback_store))
                del _fallback_store[oldest]
            except (StopIteration, KeyError):
                pass
        return True


def _client_key(remote_addr):
    if not remote_addr:
        return "unknown"
    try:
        addr = ipaddress.ip_address(remote_addr)
    except ValueError:
        cleaned = remote_addr.strip().lstrip("[").split("]", 1)[0]
        cleaned = cleaned.rsplit(":", 1)[0] if cleaned.count(":") == 1 else cleaned
        try:
            addr = ipaddress.ip_address(cleaned)
        except ValueError:
            return remote_addr
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _current_client_key():
    return _client_key(request.remote_addr)


def rate_limit(max_requests=60, window=60):
    """Route decorator enforcing a per-client sliding-window rate limit.

    Uses the SQLite rate_limit_hits table so the limit is enforced across
    all Gunicorn workers, not just within a single process.
    """
    def decorator(f):
        bucket = f.__name__

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            if max_requests == 0:
                log_action("rate_limited", request, endpoint=bucket)
                if request.path.startswith("/v1/"):
                    return jsonify({"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}), 429
                if request.is_json:
                    return jsonify({"error": "Rate limit exceeded"}), 429
                flash("Too many requests. Please slow down.", "error")
                abort(429)

            if max_requests > 0:
                ip = _current_client_key()
                key = f"{ip}:{bucket}"
                try:
                    allowed = db.check_rate_limit(key, max_requests, window)
                except Exception:
                    logging.getLogger("bananachat").warning(
                        "Rate-limit DB check failed for %s: falling back to "
                        "in-process limiter (per-worker, not cross-worker)",
                        bucket,
                    )
                    allowed = _check_rate_limit_memory(key, max_requests, window)

                if not allowed:
                    log_action("rate_limited", request, endpoint=bucket)
                    if request.path.startswith("/v1/"):
                        return jsonify({"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}}), 429
                    if request.is_json:
                        return jsonify({"error": "Rate limit exceeded"}), 429
                    flash("Too many requests. Please slow down.", "error")
                    abort(429)

            return f(*args, **kwargs)

        return wrapper
    return decorator
