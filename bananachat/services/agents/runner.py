"""Client for the sandbox runner on the compute host (``compute/sandbox_runner.py``).

The web server never runs agent code: every tool call becomes one of these
HTTP calls, and the runner (the only component that talks to the container
engine) enforces the sandbox limits on its side.

Configuration: ``BC_AGENTS_RUNNER_URL`` plus ``BC_AGENTS_RUNNER_TOKEN_FILE``
(a regular mode-0600 file) or ``BC_AGENTS_RUNNER_TOKEN``. HTTPS is required
unless the runner is on this host (for example through an SSH tunnel); plain
HTTP over Tailscale needs ``BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE=1``.

Errors are :class:`RunnerError` with the HTTP status (None when the runner
could not be reached) and two hints for retrying: ``transient`` (worth trying
again after a pause) and ``unsent`` (the request never reached the runner, so
repeating it cannot run a command twice).
"""

from __future__ import annotations

import base64
import re
import threading
import time
from urllib.parse import quote

from flask import current_app

from bananachat.services import checkpoint_agent
from bananachat.services.upstream import Cancelled, CancelToken, UpstreamError, open_request

SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_JSON_BYTES = 8 * 1024 * 1024
# An exec answer holds two streams of up to BC_SANDBOX_OUTPUT_KB (at most 1 MB each) that JSON may
# escape to six bytes per byte (\u0001, \ufffd): about 12 MB at worst.
MAX_EXEC_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
HEALTH_TTL = 30.0


class RunnerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, *, code: str | None = None,
                 transient: bool = False, unsent: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient
        self.unsent = unsent

    @property
    def gone(self) -> bool:
        """The sandbox itself no longer exists (410; a 404 may also mean a missing file)."""
        return self.status == 410 or self.code == "sandbox_gone"

    @property
    def not_found(self) -> bool:
        return self.status in (404, 410)

    @property
    def capacity(self) -> bool:
        return self.status in (429, 503)


class RunnerUnavailable(RunnerError):
    """The runner is not configured (or its configuration is invalid)."""


def _config(config=None):
    return config or current_app.config["BC"]


def configured(config=None) -> bool:
    config = _config(config)
    return bool(config.agents_runner_url and (config.agents_runner_token_file or config.agents_runner_token))


def _clean(message: str) -> str:
    """Runner messages without control characters, URLs or tokens (they are shown to people)."""
    return checkpoint_agent.sanitize(str(message)) or ""


class Runner:
    def __init__(self, config=None):
        config = _config(config)
        if not configured(config):
            raise RunnerUnavailable("The sandbox runner is not configured.")
        try:
            self.base_url = checkpoint_agent.validate_base_url(
                config.agents_runner_url, allow_insecure_tailscale=config.agents_runner_allow_insecure_tailscale)
        except ValueError:
            raise RunnerUnavailable("The sandbox runner URL is invalid (HTTPS is required unless it runs on this "
                                    "host).") from None
        if config.agents_runner_token_file:
            try:
                self.token = checkpoint_agent.read_token(config.agents_runner_token_file)
            except ValueError:
                raise RunnerUnavailable("The sandbox runner token file is unreadable or not mode 0600.") from None
        else:
            self.token = config.agents_runner_token
        if not self.token or len(self.token) > 4096 or any(ord(char) < 33 for char in self.token):
            raise RunnerUnavailable("The sandbox runner token is invalid.")

    # ----- plumbing --------------------------------------------------------------
    def _headers(self, extra=None) -> dict:
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def _open(self, method: str, path: str, *, body=None, headers=None, timeout: float = 30,
              max_bytes: int = MAX_JSON_BYTES, cancel: CancelToken | None = None):
        try:
            return open_request(method, self.base_url, path, body=body, headers=self._headers(headers),
                                connect_timeout=min(10, timeout), first_byte_timeout=timeout, read_timeout=timeout,
                                total_timeout=timeout + 30, max_bytes=max_bytes, cancel=cancel)
        except Cancelled:
            raise
        except UpstreamError as error:
            raise self._error(error) from None

    @staticmethod
    def _error(error: UpstreamError) -> RunnerError:
        message = _clean(str(error)) or "The sandbox runner failed."
        status = error.status
        unsent = status is None and "could not be reached" in str(error)
        transient = status is None or status in (429, 502, 503, 504)
        return RunnerError(message, status, code=getattr(error, "code", None), transient=transient, unsent=unsent)

    def _json(self, method: str, path: str, *, body=None, timeout: float = 30, cancel=None,
              max_bytes: int = MAX_JSON_BYTES):
        with self._open(method, path, body=body, timeout=timeout, cancel=cancel, max_bytes=max_bytes) as response:
            if response.status == 204:
                return {}
            try:
                data = response.json(max_bytes)
            except UpstreamError as error:
                raise self._error(error) from None
        if not isinstance(data, (dict, list)):
            raise RunnerError("The sandbox runner sent an invalid answer.")
        return data

    @staticmethod
    def _sandbox(sandbox_id: str) -> str:
        if not isinstance(sandbox_id, str) or not SANDBOX_ID_RE.fullmatch(sandbox_id):
            raise RunnerError("Invalid sandbox id.", 400)
        return quote(sandbox_id, safe="")

    # ----- API -----------------------------------------------------------------------
    def health(self, timeout: float = 8) -> dict:
        data = self._json("GET", "/healthz", timeout=timeout)
        return data if isinstance(data, dict) else {}

    def create(self, session: str, *, limits: dict | None = None, image: str | None = None) -> dict:
        if not SESSION_RE.fullmatch(session or ""):
            raise RunnerError("Invalid session id.", 400)
        body = {"session": session, **{key: value for key, value in (limits or {}).items() if value}}
        if image:
            body["image"] = image
        data = self._json("POST", "/v1/sandboxes", body=body, timeout=120)
        if not isinstance(data, dict) or not SANDBOX_ID_RE.fullmatch(str(data.get("id") or "")):
            raise RunnerError("The sandbox runner returned an invalid sandbox.")
        return data

    def delete(self, sandbox_id: str) -> None:
        try:
            self._json("DELETE", f"/v1/sandboxes/{self._sandbox(sandbox_id)}", timeout=60)
        except RunnerError as error:
            if error.status != 404:
                raise

    def exists(self, sandbox_id: str) -> bool:
        """False when the runner no longer knows the sandbox (other errors are raised)."""
        try:
            self._json("GET", f"/v1/sandboxes/{self._sandbox(sandbox_id)}", timeout=20)
        except RunnerError as error:
            if error.not_found:
                return False
            raise
        return True

    def interrupt(self, sandbox_id: str) -> dict:
        """Kill the command running in the sandbox (the workspace is kept unless it cannot be killed)."""
        data = self._json("POST", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/interrupt", body={}, timeout=30)
        return data if isinstance(data, dict) else {}

    def list(self) -> list[dict]:
        data = self._json("GET", "/v1/sandboxes", timeout=20)
        items = data.get("sandboxes") if isinstance(data, dict) else data
        return [item for item in items or [] if isinstance(item, dict)]

    def exec(self, sandbox_id: str, command: str, *, timeout: int, cwd: str = "/workspace",
             cancel: CancelToken | None = None) -> dict:
        data = self._json("POST", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/exec",
                          body={"command": command, "timeout": int(timeout), "cwd": cwd},
                          timeout=int(timeout) + 60, cancel=cancel, max_bytes=MAX_EXEC_BYTES)
        if not isinstance(data, dict):
            raise RunnerError("The sandbox runner sent an invalid answer.")
        return data

    def read(self, sandbox_id: str, path: str, *, cancel: CancelToken | None = None) -> dict:
        """A file (``content``, ``encoding``, ``size``) or a directory (``entries``)."""
        data = self._json("GET", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/files?path={quote(path, safe='/')}",
                          timeout=60, cancel=cancel, max_bytes=4 * MAX_FILE_BYTES)
        if not isinstance(data, dict):
            raise RunnerError("The sandbox runner sent an invalid answer.")
        return data

    @staticmethod
    def file_bytes(data: dict) -> bytes | None:
        content = data.get("content")
        if not isinstance(content, str):
            return None
        if data.get("encoding") == "base64":
            try:
                return base64.b64decode(content, validate=True)
            except ValueError:
                return None
        return content.encode("utf-8")

    def write(self, sandbox_id: str, path: str, data: bytes, *, cancel: CancelToken | None = None) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise RunnerError("The file is too large.", 413)
        with self._open("PUT", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/files?path={quote(path, safe='/')}",
                        body=bytes(data), headers={"Content-Type": "application/octet-stream"}, timeout=120,
                        cancel=cancel) as response:
            if response.status == 204:
                return {}
            try:
                result = response.json(MAX_JSON_BYTES)
            except UpstreamError as error:
                raise self._error(error) from None
        return result if isinstance(result, dict) else {}

    def open_archive(self, sandbox_id: str):
        """The workspace as a gzip tar stream (an open response: always close it)."""
        return self._open("GET", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/archive", timeout=300,
                          max_bytes=MAX_ARCHIVE_BYTES + 1024, headers={"Accept": "application/gzip"})

    def put_archive(self, sandbox_id: str, data: bytes, *, kind: str, path: str | None = None,
                    cancel: CancelToken | None = None) -> dict:
        """Extract an archive inside the sandbox, into *path* (a folder under /workspace, created if needed)."""
        if len(data) > MAX_ARCHIVE_BYTES:
            raise RunnerError("The archive is too large.", 413)
        content_type = "application/zip" if kind == "zip" else "application/gzip"
        target = f"?path={quote(path, safe='/')}" if path else ""
        with self._open("PUT", f"/v1/sandboxes/{self._sandbox(sandbox_id)}/archive{target}", body=bytes(data),
                        headers={"Content-Type": content_type}, timeout=300, cancel=cancel) as response:
            if response.status == 204:
                return {}
            try:
                result = response.json(MAX_JSON_BYTES)
            except UpstreamError as error:
                raise self._error(error) from None
        return result if isinstance(result, dict) else {}


def client(config=None) -> Runner:
    return Runner(config)


# ----- cached health (admin page, capacity) ---------------------------------------------

_health_lock = threading.Lock()
_health: dict = {"at": 0.0, "value": None, "key": None}


def health(config=None, *, fresh: bool = False) -> dict:
    """``{"ok": bool, "error": str, ...runner fields}``, cached for HEALTH_TTL seconds."""
    config = _config(config)
    key = (config.agents_runner_url, config.agents_runner_token_file, bool(config.agents_runner_token))
    now = time.monotonic()
    with _health_lock:
        if not fresh and _health["value"] is not None and _health["key"] == key and now - _health["at"] < HEALTH_TTL:
            return dict(_health["value"])
    if not configured(config):
        value = {"ok": False, "configured": False, "error": "The sandbox runner is not configured."}
    else:
        try:
            data = Runner(config).health()
            value = {**data, "ok": bool(data.get("ok", True)), "configured": True, "error": ""}
        except RunnerError as error:
            value = {"ok": False, "configured": True, "error": str(error)}
    with _health_lock:
        _health.update(at=now, value=value, key=key)
    return dict(value)


def site_capacity(config=None, fallback: int = 4) -> int:
    """The runner's sandbox limit when it reports one, else *fallback*."""
    value = health(config).get("max")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback
