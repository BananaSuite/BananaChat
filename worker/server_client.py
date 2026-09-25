"""HTTP client for the BananaChat worker API.

Uses only the Python standard library (urllib) to avoid adding dependencies
that differ between Windows and Linux packaging.
"""

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlencode

import config

_logger = logging.getLogger("bananachat.worker.client")


class ServerError(Exception):
    """Non-200 response from the BananaChat server."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ServerError("The worker server redirected a credentialed request; configure its final URL")


def _read_json(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ServerError("Worker server response exceeds its size limit")
    try:
        result = json.loads(data)
    except (UnicodeError, ValueError) as error:
        raise ServerError("Worker server returned invalid JSON") from error
    if not isinstance(result, dict):
        raise ServerError("Worker server returned an invalid response")
    return result


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.WORKER_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _post(path: str, body: dict, timeout: int = 15) -> dict:
    url  = config.SERVER_URL + path
    data = json.dumps(body).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=timeout) as resp:
            return _read_json(resp, 64 * 1024)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        raise ServerError(f"HTTP {exc.code} from {path}: {raw[:200]}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ServerError(f"Cannot reach server at {config.SERVER_URL}: {type(exc).__name__}") from exc


def _get(path: str, params: dict = None, timeout: int = 35) -> tuple:
    """Returns (status_code, body_dict | None)."""
    url = config.SERVER_URL + path
    if params:
        qs = urlencode(params)
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=timeout) as resp:
            if resp.status == 204:
                return 204, None
            return resp.status, _read_json(resp, 64 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raw = exc.read(4096).decode("utf-8", errors="replace")
            raise ServerError(f"Worker forbidden (disabled?): {raw[:200]}") from exc
        raise ServerError(f"HTTP {exc.code} from {path}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ServerError(f"Cannot reach server: {type(exc).__name__}") from exc


# API helpers

def send_heartbeat(status: str, gpu_name: str = None, ollama_version: str = None,
                   capabilities: dict = None, activity_state: str = None,
                   gpu_util: float = None) -> bool:
    """POST heartbeat to keep this worker's status alive. Returns True on success."""
    try:
        _post("/worker/v1/heartbeat", {
            "status":         status,
            "gpu_name":       gpu_name,
            "ollama_version": ollama_version,
            "capabilities":   capabilities,
            "activity_state": activity_state,
            "gpu_util":       gpu_util,
        })
        return True
    except ServerError as exc:
        _logger.warning("Heartbeat failed: %s", exc)
        return False


def poll_for_job(available_models=None) -> dict | None:
    """Long-poll for the next job. Returns a job dict or None on timeout / error.

    Blocks for up to ~28 seconds on the server side.
    """
    params = {}
    if available_models:
        params["models"] = ",".join(available_models)
    try:
        code, body = _get("/worker/v1/jobs/poll", params=params, timeout=35)
        if code == 204 or body is None:
            return None
        return body
    except ServerError as exc:
        _logger.warning("Job poll error: %s", exc)
        return None


def submit_chunk(job_id: str, seq: int, content: str, done: bool) -> bool:
    """POST one streaming chunk. Returns True if the server says keep going,
    False if the server wants us to stop (stop=True in response)."""
    try:
        resp = _post(f"/worker/v1/jobs/{job_id}/chunk", {
            "seq":     seq,
            "content": content,
            "done":    done,
        })
        return resp.get("ok") is True and not resp.get("stop", False)
    except ServerError as exc:
        _logger.warning("Chunk submit error for job %s: %s", job_id[:8], exc)
        return False   # Treat server errors as a stop signal to avoid infinite loops


def complete_job(job_id: str, tokens_in: int, tokens_out: int, finish_reason="stop") -> bool:
    try:
        result = _post(f"/worker/v1/jobs/{job_id}/complete", {
            "tokens_in":  tokens_in,
            "tokens_out": tokens_out,
            "finish_reason": finish_reason,
        })
        return result.get("ok") is True
    except ServerError as exc:
        _logger.warning("Complete job %s error: %s", job_id[:8], exc)
        return False


def fail_job(job_id: str, error: str) -> None:
    try:
        _post(f"/worker/v1/jobs/{job_id}/fail", {"error": str(error)[:500]})
    except ServerError as exc:
        _logger.warning("Fail job %s error: %s", job_id[:8], exc)
