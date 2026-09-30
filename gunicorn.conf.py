"""Gunicorn settings. Run: ``gunicorn -c gunicorn.conf.py wsgi:app``.

Existing managed installations start the service with exactly this file
name, so keep it at the repository root.
"""

import os
import re

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
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None

# gthread workers heartbeat independently of request threads, so long
# streaming responses are not killed by this timeout.
timeout = 120
# On reload or shutdown, running generations get this long to finish; any
# still running are saved as interrupted answers by the next process.
graceful_timeout = 60
keepalive = 5
max_requests = 5000
max_requests_jitter = 500


def post_fork(server, worker):
    """Drop any database connection inherited from the master process."""
    from bananachat import db

    db.close_thread_connection()


def post_worker_init(worker):
    from bananachat.app import start_background_services

    start_background_services(worker.wsgi)


def worker_exit(server, worker):
    from bananachat.services import background

    background.stop()
