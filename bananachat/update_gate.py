"""The page shown while the lifecycle tool updates or backs up the server.

During those few minutes the lifecycle tool creates ``BANANA_MAINTENANCE_FILE``
and nothing may write to the database. This WSGI wrapper answers every request
without touching the application, except:

* ``/health`` and ``/healthz`` reach the application (the updater checks them);
* ``/status`` answers ``200 {"status": "updating"}`` so monitors can tell a
  planned update from an outage.

Pages get a small bilingual "back in a moment" page (HTTP 503 with
``Retry-After``, which is correct for a planned interruption and keeps search
engines and proxies from caching it); API clients get JSON.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<meta name="robots" content="noindex">
<title>Updating · BananaChat</title>
<style>
:root { color-scheme: light dark; --bg: #141414; --card: #1d1d1d; --text: #ededed; --muted: #a9a9a9; --accent: #e6be32; }
@media (prefers-color-scheme: light) { :root { --bg: #faf9f5; --card: #ffffff; --text: #202124; --muted: #5f6368; --accent: #8a6500; } }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 1.5rem; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { width: min(460px, 100%); background: var(--card); border-radius: 16px; padding: 2rem 1.75rem; text-align: center;
  box-shadow: 0 18px 48px rgb(0 0 0 / .18); border: 1px solid color-mix(in srgb, var(--text) 12%, transparent); }
.spinner { width: 44px; height: 44px; margin: 0 auto 1.25rem; border-radius: 50%; border: 4px solid color-mix(in srgb, var(--accent) 25%, transparent);
  border-top-color: var(--accent); animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
h1 { font-size: 1.3rem; margin: 0 0 .4rem; }
p { margin: 0 0 .6rem; color: var(--muted); }
hr { border: 0; border-top: 1px solid color-mix(in srgb, var(--text) 12%, transparent); margin: 1.25rem 0; }
small { color: var(--muted); }
</style>
</head>
<body>
<main>
  <div class="spinner" aria-hidden="true"></div>
  <h1>We&rsquo;ll be right back</h1>
  <p>The server is being updated. This usually takes a few minutes; the page reloads by itself.</p>
  <hr>
  <h1 lang="it">Torniamo subito</h1>
  <p lang="it">Il server &egrave; in aggiornamento. Di solito richiede pochi minuti; la pagina si ricarica da sola.</p>
  <small>Your chats and settings are safe. &middot; Le tue chat e impostazioni sono al sicuro.</small>
</main>
</body>
</html>
""".encode()


class UpdateGate:
    """WSGI middleware active while the lifecycle maintenance file exists."""

    def __init__(self, app, path: str | None = None):
        self.app = app
        self.path = path or os.environ.get("BANANA_MAINTENANCE_FILE", "") or os.environ.get("BW_MAINTENANCE_FILE", "")

    def active(self) -> bool:
        return bool(self.path) and Path(self.path).exists()

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        if path in ("/health", "/healthz") or not self.active():
            return self.app(environ, start_response)
        if path == "/status":
            body = json.dumps({"status": "updating", "accepting_requests": False}).encode()
            return self._respond(start_response, "200 OK", "application/json", body)
        accept = environ.get("HTTP_ACCEPT", "")
        wants_json = (path.startswith(("/v1/", "/worker/")) or environ.get("HTTP_X_REQUESTED_WITH")
                      or ("application/json" in accept and "text/html" not in accept))
        if wants_json:
            body = json.dumps({"error": {"code": "updating", "type": "server_error",
                                         "message": "The server is being updated. Please retry in a minute."}}).encode()
            return self._respond(start_response, "503 Service Unavailable", "application/json", body)
        return self._respond(start_response, "503 Service Unavailable", "text/html; charset=utf-8", _PAGE)

    @staticmethod
    def _respond(start_response, status, content_type, body):
        start_response(status, [
            ("Content-Type", content_type), ("Content-Length", str(len(body))), ("Retry-After", "30"),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"),
        ])
        return [body]
