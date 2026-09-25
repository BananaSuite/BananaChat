"""A short maintenance window prevents writes until an update passes health checks."""

import os
from pathlib import Path


class MaintenanceGate:
    def __init__(self, app, path=None):
        self.app = app
        self.path = path or os.environ.get("BANANA_MAINTENANCE_FILE", "") or os.environ.get("BW_MAINTENANCE_FILE", "")

    def __call__(self, environ, start_response):
        if self.path and Path(self.path).exists() and environ.get("PATH_INFO") not in {"/health", "/healthz"}:
            body = b"Maintenance is in progress. Please retry shortly.\n"
            start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8"),
                           ("Content-Length", str(len(body))), ("Retry-After", "30"), ("Cache-Control", "no-store")])
            return [body]
        return self.app(environ, start_response)
