"""Gunicorn settings. Run: ``gunicorn -c gunicorn.conf.py wsgi:app``.

Existing managed installations start the service with exactly this file
name, so keep it at the repository root.
"""

import os
import re
import signal
import threading
import time

from gunicorn.glogging import Logger

from bananachat.config import load_config

_config = load_config(load_secret=False)


def _bind_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


bind = f"{_bind_host(_config.host)}:{_config.port}"

# SQLite serialises writes, so a few processes with many threads suit the
# workload: most requests wait on the model, not on Python.
workers = int(os.environ.get("BC_WORKERS", "2"))
worker_class = "gthread"
threads = _config.http_threads

# Create the database and secret key once, before forking.
preload_app = True

# Proxy headers are only honoured from the local reverse proxy.
forwarded_allow_ips = "127.0.0.1,::1"

accesslog = "-"
errorlog = "-"
loglevel = "info"
# Never log query strings (they may carry invitation codes) or share-link tokens.
access_log_format = '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms "%(a)s"'
_SHARE_TOKEN = re.compile(r"^(/share/)[^/]+")


class AccessLogger(Logger):
    """Replaces the token of ``/share/<token>`` paths in the access log: it grants access to a chat."""

    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        atoms["U"] = _SHARE_TOKEN.sub(r"\1[token]", atoms.get("U") or "")
        return atoms


logger_class = AccessLogger

# The managed service has a read-only application directory.
control_socket_disable = True
# Gunicorn keeps only its worker heartbeat files here (created with mkstemp).
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None  # noqa: S108

# gthread workers heartbeat independently of request threads, so long
# streaming responses are not killed by this timeout.
timeout = 120
# On reload or shutdown, open requests (including chat and API streams) get
# about this long to finish; answers still generating are then stopped, saved
# and charged for what they produced (see _hand_over).
graceful_timeout = 60
keepalive = 5
# Workers are replaced after this many requests, but never sooner than
# _MIN_WORKER_SECONDS after they started, so cheap requests that anyone can
# send cannot force replacements (which cut long answers short). Answers keep
# their usage records while they stream (services.completions, db.runs): one
# that is cut anyway is charged from its last record by another process.
max_requests = 5000
max_requests_jitter = 500
_MIN_WORKER_SECONDS = 3600
# Time to stop running answers and charge them (services.chat and services.completions save within 10 seconds).
_SAVE_SECONDS = 15


def _hand_over_grace(cfg) -> float:
    """How long a replaced worker lets running answers finish (at most 45 s). The master kills a worker `timeout`
    seconds after its last heartbeat, and open connections may already have used `graceful_timeout` of that: the
    effective settings count (a --timeout override included)."""
    return float(max(0, min(45, cfg.timeout - cfg.graceful_timeout - _SAVE_SECONDS)))


def _hand_over(grace: float) -> None:
    """Let this process's chat runs and API answers finish for *grace* seconds, then stop and charge the rest. Its
    inference queue places are freed once both are handed over: answers still finishing keep counting."""
    from bananachat.services import chat, completions

    api = threading.Thread(target=completions.shutdown, kwargs={"grace": grace}, name="hand-over-api", daemon=True)
    api.start()
    try:
        chat.shutdown(grace=grace, release_places=False)
    finally:
        api.join(grace + completions.SAVE_TIMEOUT + 1)
        chat.release_queue_places()


def post_fork(server, worker):
    """Drop any database connection inherited from the master process."""
    from bananachat import db

    worker.bc_started = time.monotonic()
    db.close_thread_connection()


def post_worker_init(worker):
    from bananachat.app import start_background_services

    start_background_services(worker.wsgi)
    _hand_over_before_the_master_kills(worker)


def _hand_over_before_the_master_kills(worker):
    """On SIGTERM (a stop, restart or reload) the master kills this process `graceful_timeout` seconds later, often
    before worker_exit runs: open streams can hold the process that long. Stop and charge what is still running in
    time."""
    previous = signal.getsignal(signal.SIGTERM)

    def stopping(signum, frame):
        if callable(previous):
            previous(signum, frame)  # gunicorn's own handler: stop accepting requests
        if not getattr(worker, "bc_stopping", False):
            worker.bc_stopping = True
            timer = threading.Timer(max(0.0, worker.cfg.graceful_timeout - _SAVE_SECONDS), _hand_over, args=(0.0,))
            timer.daemon = True
            timer.start()

    signal.signal(signal.SIGTERM, stopping)
    signal.siginterrupt(signal.SIGTERM, False)  # as gunicorn: let the signal not disturb requests


def pre_request(worker, req):
    """Postpone replacing a worker that started less than _MIN_WORKER_SECONDS ago (see max_requests)."""
    worker.log.debug("%s %s", req.method, req.path)
    started = getattr(worker, "bc_started", None)
    if started is not None and worker.nr + 1 >= worker.max_requests and \
            time.monotonic() - started < _MIN_WORKER_SECONDS:
        worker.max_requests = worker.nr + 1 + max(1, worker.cfg.max_requests // 10)  # look again later


def worker_exit(server, worker):
    """Hand over this worker's chat runs, API answers and inference queue places before it exits."""
    from bananachat.services import background

    recycled = worker.nr >= worker.max_requests
    try:
        _hand_over(_hand_over_grace(worker.cfg) if recycled else 0.0)
    finally:
        background.stop()


def child_exit(server, worker):
    """In the master: the queue places of a worker that exited (also one that crashed or was killed) stop
    counting within seconds instead of after their lease."""
    from bananachat import db
    from bananachat.services import queue

    try:
        queue.expire_process(worker.pid)
    except Exception:  # noqa: BLE001 - the places expire on their own
        server.log.warning("Could not release the inference queue places of worker %s", worker.pid, exc_info=True)
    finally:
        db.close_thread_connection()  # the master must not carry a database connection into fork()
