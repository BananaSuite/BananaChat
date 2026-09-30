"""The BananaChat worker API (``/worker/v1``), as seen from the worker."""

from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import urlencode

from .transport import TransportError, request_json

log = logging.getLogger("bananachat.worker.client")

POLL_READ_TIMEOUT = 45          # the server holds a poll for at most 25 s
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_REPLY_BYTES = 64 * 1024


class ServerError(Exception):
    """The server refused or could not be reached. ``status`` is the HTTP status, if any."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, settings):
        self.url = settings.server_url
        self.headers = {"Authorization": f"Bearer {settings.token}"}

    def _call(self, method: str, path: str, body=None, *, read_timeout: float = 20, max_bytes: int = MAX_REPLY_BYTES):
        try:
            status, data = request_json(method, self.url, "/worker/v1" + path, body=body, headers=self.headers,
                                        read_timeout=read_timeout, max_bytes=max_bytes)
        except TransportError as error:
            raise ServerError(str(error), error.status) from None
        if status != 204 and not isinstance(data, dict):
            raise ServerError("The server sent an unexpected answer.")
        return status, data

    def heartbeat(self, payload: dict) -> dict:
        return self._call("POST", "/heartbeat", payload)[1]

    def poll(self, models: Optional[List[str]]) -> Optional[dict]:
        query = "?" + urlencode({"models": ",".join(models)}) if models else ""
        status, data = self._call("GET", "/jobs/poll" + query, read_timeout=POLL_READ_TIMEOUT, max_bytes=MAX_JOB_BYTES)
        if status == 204:
            return None
        if not isinstance(data.get("job_id"), str) or not isinstance(data.get("model"), str) \
                or not isinstance(data.get("messages"), list):
            raise ServerError("The server sent an invalid job.")
        return data

    def chunk(self, job_id: str, seq: int, content: str, done: bool) -> bool:
        """Send a piece of the answer. Returns False when the server wants the job stopped."""
        _status, reply = self._call("POST", f"/jobs/{job_id}/chunk", {"seq": seq, "content": content, "done": done})
        return reply.get("ok") is True and not reply.get("stop")

    def complete(self, job_id: str, tokens_in: int, tokens_out: int, finish_reason: str) -> bool:
        _status, reply = self._call("POST", f"/jobs/{job_id}/complete", {
            "tokens_in": tokens_in, "tokens_out": tokens_out, "finish_reason": finish_reason})
        return reply.get("ok") is True

    def fail(self, job_id: str, error: str, *, requeue: bool = False) -> None:
        body = {"error": "deferred" if requeue else (error or "The worker failed.")[:500]}
        if requeue:
            body["requeue"] = True
        try:
            self._call("POST", f"/jobs/{job_id}/fail", body)
        except ServerError as failure:
            log.warning("Could not report job %s to the server: %s", job_id[:8], failure)
