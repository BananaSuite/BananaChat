"""Authenticated stdlib client for the checkpoint download agent."""

import ipaddress
import json
import logging
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request

import config


_MAX_JSON_BYTES = 256 * 1024
_TOKEN_MAX_BYTES = 4096
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ERROR_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
_URL_IN_ERROR_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BEARER_IN_ERROR_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_logger = logging.getLogger("bananachat.checkpoint_agent")


class CheckpointAgentError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback(hostname):
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_tailscale(hostname):
    if hostname.lower().endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address in _TAILSCALE_V4 or address in _TAILSCALE_V6


def _validate_base_url(value, allow_insecure_tailscale=None):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Checkpoint agent URL is not configured correctly.")
    if _SAFE_ERROR_RE.search(value) or any(char.isspace() for char in value):
        raise ValueError("Checkpoint agent URL is not configured correctly.")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Checkpoint agent URL is not configured correctly.") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or port == 0
        or "%" in parsed.netloc
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("Checkpoint agent URL is not configured correctly.")
    hostname = parsed.hostname
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ipaddress.AddressValueError as exc:
            raise ValueError("Checkpoint agent URL is not configured correctly.") from exc
    elif len(hostname) > 253 or any(
        not _HOST_LABEL_RE.fullmatch(label) for label in hostname.split(".")
    ):
        raise ValueError("Checkpoint agent URL is not configured correctly.")
    if parsed.scheme == "http" and not _is_loopback(hostname):
        if allow_insecure_tailscale is None:
            allow_insecure_tailscale = getattr(
                config, "CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE", False
            )
        if not (allow_insecure_tailscale and _is_tailscale(hostname)):
            raise ValueError(
                "Checkpoint agent URL must use HTTPS unless it is loopback."
            )
        _logger.warning(
            "INSECURE CHECKPOINT AGENT TRANSPORT ENABLED for Tailscale host %s; "
            "bearer credentials and checkpoint metadata are not protected by TLS",
            hostname,
        )
    return value.rstrip("/")


def _read_token(path):
    if not isinstance(path, str) or not path:
        raise ValueError("Checkpoint agent token file is not configured.")
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ValueError("Checkpoint agent token file must be a regular mode-0600 file.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("Checkpoint agent token file is unavailable.") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("Checkpoint agent token file must be a regular mode-0600 file.")
        raw = os.read(fd, _TOKEN_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not raw or len(raw) > _TOKEN_MAX_BYTES:
        raise ValueError("Checkpoint agent token file has an invalid size.")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Checkpoint agent token file is invalid.") from exc
    if not token or "\x00" in token or "\r" in token or "\n" in token:
        raise ValueError("Checkpoint agent token file is invalid.")
    return token


def configuration_error():
    url = getattr(config, "CHECKPOINT_AGENT_URL", "")
    token_file = getattr(config, "CHECKPOINT_AGENT_TOKEN_FILE", "")
    if not url and not token_file:
        return "Checkpoint agent is not configured."
    if not url or not token_file:
        return "Both checkpoint agent URL and token file must be configured."
    try:
        _validate_base_url(url)
        _read_token(token_file)
    except ValueError as exc:
        return str(exc)
    return None


def is_configured():
    return configuration_error() is None


def _sanitize_agent_message(value):
    if not isinstance(value, str):
        return None
    value = _SAFE_ERROR_RE.sub("", value)
    value = _URL_IN_ERROR_RE.sub("[redacted URL]", value)
    value = _BEARER_IN_ERROR_RE.sub("Bearer [redacted]", value).strip()
    return value[:300] or None


def job_error_message(job):
    """Return a bounded, display-safe error from an agent job payload."""
    if not isinstance(job, dict):
        return "Checkpoint agent returned an invalid job status."
    error = job.get("error")
    if not isinstance(error, dict):
        return "Checkpoint download failed."
    message = _sanitize_agent_message(error.get("message"))
    code = _sanitize_agent_message(error.get("code"))
    if message and code:
        return f"{message} ({code})"
    return message or code or "Checkpoint download failed."


class CheckpointAgentClient:
    def __init__(self, base_url, token_file, timeout=10):
        self.base_url = _validate_base_url(base_url)
        self._token = _read_token(token_file)
        self.timeout = max(1, min(float(timeout), 60))
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @staticmethod
    def _validate_job_id(job_id):
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise CheckpointAgentError("Checkpoint agent returned an invalid job ID.")
        return job_id

    def _request(self, method, path, body=None):
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self._token,
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(_MAX_JSON_BYTES + 1)
            except Exception:
                raw = b""
            message = None
            if len(raw) <= _MAX_JSON_BYTES:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    message = _sanitize_agent_message(payload.get("error", {}).get("message"))
                except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
            message = message or f"Checkpoint agent returned HTTP {exc.code}."
            raise CheckpointAgentError(message, status=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CheckpointAgentError("Checkpoint agent is unavailable.") from None
        if len(raw) > _MAX_JSON_BYTES:
            raise CheckpointAgentError("Checkpoint agent response exceeded the size limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointAgentError("Checkpoint agent returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise CheckpointAgentError("Checkpoint agent returned an invalid response.")
        return payload

    def submit_download(
        self, *, repo_id, source_filename, revision, target_name,
        expected_sha256, expected_size, idempotency_key,
    ):
        body = {
            "source": {
                "type": "huggingface",
                "repo_id": repo_id,
                "filename": source_filename,
                "revision": revision,
            },
            "target_name": target_name,
            "expected_sha256": expected_sha256,
            "expected_size": expected_size,
            "idempotency_key": idempotency_key,
        }
        job = self._request("POST", "/v1/downloads", body)
        self._validate_job_id(job.get("id"))
        return job

    def get_download(self, job_id):
        job_id = self._validate_job_id(job_id)
        job = self._request("GET", "/v1/downloads/" + job_id)
        if self._validate_job_id(job.get("id")) != job_id:
            raise CheckpointAgentError("Checkpoint agent returned a mismatched job ID.")
        return job

    def cancel_download(self, job_id):
        job_id = self._validate_job_id(job_id)
        try:
            return self._request("DELETE", "/v1/downloads/" + job_id)
        except CheckpointAgentError as exc:
            if exc.status in (404, 409):
                return None
            raise

    def status(self):
        return self._request("GET", "/v1/status")

    def list_checkpoints(self):
        return self._request("GET", "/v1/checkpoints")


def get_client():
    error = configuration_error()
    if error:
        raise CheckpointAgentError(error)
    return CheckpointAgentClient(
        config.CHECKPOINT_AGENT_URL, config.CHECKPOINT_AGENT_TOKEN_FILE
    )
