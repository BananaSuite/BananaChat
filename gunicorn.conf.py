"""Run with ``gunicorn wsgi:app -c gunicorn.conf.py``."""

import codecs
import os

import config as _bc_config  # underscore prefix avoids Gunicorn setting clash


def _resolve_worker_tmp_dir():
    """Keep worker heartbeats off disk when writable tmpfs is available."""
    candidate = "/dev/shm"
    if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
        return candidate
    import sys
    print(
        "WARNING: /dev/shm not writable: Gunicorn heartbeats will use /tmp. "
        "Worker kills under disk I/O pressure are possible.",
        file=sys.stderr,
    )
    return None

def _format_host_for_bind(host: str) -> str:
    """Bracket bare IPv6 addresses for Gunicorn's host:port syntax."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


# Override with Gunicorn's --bind option.
bind = f"{_format_host_for_bind(_bc_config.HOST)}:{_bc_config.PORT}"

# SQLite serialises all writes, so more workers just increases lock
# contention without improving throughput. Thread count accommodates streaming
# clients, with four threads per process reserved from inference admission.
workers = 2

worker_class = "gthread"
threads = _bc_config.HTTP_THREADS

# Initialize the persistent session key before workers fork.
preload_app = True

# Application proxy handling also requires PROXY_MODE.
forwarded_allow_ips = "127.0.0.1,::1"

accesslog = "-"        # stdout
errorlog = "-"         # stderr
loglevel = "info"

# The managed systemd service has a read-only application directory.
control_socket_disable = True

timeout = max(
    300,
    _bc_config.GENERATION_TIMEOUT + 15,
    int(_bc_config.IMAGE_REQUEST_TIMEOUT + 15)
    if _bc_config.IMAGE_BACKEND == "comfyui" else 300,
)  # Keep image requests alive through queueing, polling, and output download.
# Worker recycling must allow a bounded active generation to finish. The
# systemd stop deadline still bounds a requested installation shutdown.
graceful_timeout = timeout
keepalive = 2          # keep-alive connections (seconds)

# Restart workers after handling this many requests to limit memory growth.
max_requests = 5000
max_requests_jitter = 500

_worker_tmp = _resolve_worker_tmp_dir()
if _worker_tmp:
    worker_tmp_dir = _worker_tmp


def pre_request(worker, request):
    """Bound stalled socket reads/writes without limiting a healthy SSE lifetime."""
    request.unreader.sock.settimeout(30)


def post_worker_init(worker):
    """Verify request support before starting the worker's model sync."""
    from services.http_capacity import configure
    configure(worker.cfg.threads)
    try:
        codecs.lookup("idna")
    except LookupError as exc:
        worker.log.critical(
            "Worker startup self-check failed: Python's idna codec is unavailable"
        )
        raise RuntimeError("required Python codec 'idna' is unavailable") from exc

    from services.ollama import start_background_sync
    start_background_sync(use_leader_lock=True)


def worker_exit(server, worker):
    from services.ollama import stop_background_sync
    stop_background_sync()
