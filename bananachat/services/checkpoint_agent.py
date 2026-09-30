"""Client for the checkpoint download agent on the compute server.

The agent (``compute/checkpoint_agent.py``) downloads Hugging Face
safetensors checkpoints into ComfyUI's model directory and verifies their
SHA-256. It is optional: configure ``BC_CHECKPOINT_AGENT_URL`` and
``BC_CHECKPOINT_AGENT_TOKEN_FILE`` (a mode-0600 file holding the bearer
token). HTTPS is required unless the agent is on this host; plain HTTP over
Tailscale needs ``BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE=1``.

Agent API: ``POST /v1/downloads``, ``GET``/``DELETE /v1/downloads/<id>``,
``GET /v1/status``, ``GET /v1/checkpoints``. Messages coming back from the
agent are sanitised (no URLs, tokens or control characters) before display.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import stat
from urllib.parse import urlsplit

from flask import current_app

from bananachat.services.upstream import UpstreamError, open_request

log = logging.getLogger("bananachat.checkpoint_agent")

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REMOTE_STATUSES = ("queued", "downloading", "completed", "failed", "canceled")
_MAX_JSON_BYTES = 256 * 1024
_TOKEN_MAX_BYTES = 4096
_CONTROL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class CheckpointAgentError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _config(config=None):
    return config or current_app.config["BC"]


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_tailscale(hostname: str) -> bool:
    if hostname.lower().endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address in _TAILSCALE_V4 or address in _TAILSCALE_V6


def validate_base_url(value: str, *, allow_insecure_tailscale: bool = False) -> str:
    problem = ValueError("The checkpoint agent URL is not configured correctly.")
    if not isinstance(value, str) or not value or value != value.strip() or _CONTROL_RE.search(value) \
            or any(char.isspace() for char in value):
        raise problem
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise problem from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or parts.path not in ("", "/")
            or port == 0 or "%" in parts.netloc or "\\" in parts.netloc or parts.netloc.endswith(":")):
        raise problem
    hostname = parts.hostname
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ipaddress.AddressValueError:
            raise problem from None
    elif len(hostname) > 253 or not all(_HOST_LABEL_RE.fullmatch(label) for label in hostname.split(".")):
        raise problem
    if parts.scheme == "http" and not _is_loopback(hostname):
        if not (allow_insecure_tailscale and _is_tailscale(hostname)):
            raise ValueError("The checkpoint agent URL must use HTTPS unless the agent runs on this host.")
    return value.rstrip("/")


def read_token(path: str) -> str:
    """Read the bearer token from a regular, owner-only (0600) file without following links."""
    if not path:
        raise ValueError("The checkpoint agent token file is not configured.")
    invalid = ValueError("The checkpoint agent token file must be a regular file with mode 0600.")
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise invalid
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ValueError("The checkpoint agent token file is unavailable.") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise invalid
        raw = os.read(descriptor, _TOKEN_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("The checkpoint agent token file is invalid.") from None
    if not token or len(raw) > _TOKEN_MAX_BYTES or any(char in token for char in "\x00\r\n"):
        raise ValueError("The checkpoint agent token file is invalid.")
    return token


def configuration_error(config=None) -> str | None:
    """Why the agent cannot be used, or None when it is configured correctly."""
    config = _config(config)
    url, token_file = config.checkpoint_agent_url, config.checkpoint_agent_token_file
    if not url and not token_file:
        return "The checkpoint download agent is not configured."
    if not url or not token_file:
        return "Set both BC_CHECKPOINT_AGENT_URL and BC_CHECKPOINT_AGENT_TOKEN_FILE."
    try:
        validate_base_url(url, allow_insecure_tailscale=config.checkpoint_agent_allow_insecure_tailscale)
        read_token(token_file)
    except ValueError as error:
        return str(error)
    return None


def is_configured(config=None) -> bool:
    config = _config(config)
    if not (config.checkpoint_agent_url or config.checkpoint_agent_token_file):
        return False
    return configuration_error(config) is None


def sanitize(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = _CONTROL_RE.sub("", value)
    value = _URL_RE.sub("[URL]", value)
    value = _BEARER_RE.sub("Bearer [redacted]", value).strip()
    return value[:300] or None


def job_error_message(job) -> str:
    if not isinstance(job, dict) or not isinstance(job.get("error"), dict):
        return "The checkpoint download failed."
    message, code = sanitize(job["error"].get("message")), sanitize(job["error"].get("code"))
    if message and code:
        return f"{message} ({code})"
    return message or code or "The checkpoint download failed."


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 10):
        self.base_url = base_url
        self._token = token
        self.timeout = max(1.0, min(float(timeout), 60.0))

    def _request(self, method: str, path: str, body=None) -> dict:
        headers = {"Authorization": "Bearer " + self._token}
        try:
            with open_request(method, self.base_url, path, body=body, headers=headers,
                              connect_timeout=self.timeout, first_byte_timeout=self.timeout,
                              read_timeout=self.timeout, total_timeout=self.timeout * 3,
                              max_bytes=_MAX_JSON_BYTES + 1) as response:
                payload = response.json(_MAX_JSON_BYTES)
        except UpstreamError as error:
            if error.status is not None:
                message = sanitize(str(error)) or f"The checkpoint agent returned HTTP {error.status}."
                raise CheckpointAgentError(message, error.status) from None
            raise CheckpointAgentError("The checkpoint agent is unavailable.") from None
        if not isinstance(payload, dict):
            raise CheckpointAgentError("The checkpoint agent returned an invalid response.")
        return payload

    @staticmethod
    def _job_id(value) -> str:
        if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
            raise CheckpointAgentError("The checkpoint agent returned an invalid job id.")
        return value

    def submit_download(self, *, repo_id, source_filename, revision, target_name, expected_sha256, expected_size,
                        idempotency_key) -> dict:
        job = self._request("POST", "/v1/downloads", {
            "source": {"type": "huggingface", "repo_id": repo_id, "filename": source_filename, "revision": revision},
            "target_name": target_name, "expected_sha256": expected_sha256, "expected_size": expected_size,
            "idempotency_key": idempotency_key,
        })
        self._job_id(job.get("id"))
        return job

    def get_download(self, job_id: str) -> dict:
        job = self._request("GET", "/v1/downloads/" + self._job_id(job_id))
        if job.get("id") != job_id:
            raise CheckpointAgentError("The checkpoint agent returned a different job.")
        return job

    def cancel_download(self, job_id: str) -> dict | None:
        try:
            return self._request("DELETE", "/v1/downloads/" + self._job_id(job_id))
        except CheckpointAgentError as error:
            if error.status in (404, 409):
                return None
            raise

    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    def list_checkpoints(self) -> dict:
        return self._request("GET", "/v1/checkpoints")


def client(config=None) -> Client:
    config = _config(config)
    error = configuration_error(config)
    if error:
        raise CheckpointAgentError(error)
    url = validate_base_url(config.checkpoint_agent_url,
                            allow_insecure_tailscale=config.checkpoint_agent_allow_insecure_tailscale)
    if url.startswith("http://") and not _is_loopback(urlsplit(url).hostname or ""):
        log.warning("The checkpoint agent is reached over plain HTTP (Tailscale); its token is not protected by TLS")
    return Client(url, read_token(config.checkpoint_agent_token_file))
