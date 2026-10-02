"""Unbilled, bounded metadata calls through Claude Code's SDK control protocol.

The CLI owns subscription authentication and refresh. No user message is ever
sent, no credential file is read here, and provider diagnostics stay private.
"""
from __future__ import annotations

import json
import math
import tempfile
import uuid


class ControlError(RuntimeError):
    """A credential-free control-protocol failure."""


def request(adapter, profile, subtype, *, fields=None, cancel=None, timeout=30):
    """Return a validated initialization or subscription-usage response.

    Reuse the transport's bounded I/O and process-group cleanup. Consume the
    iterator completely before returning, so the CLI is reaped even on success.
    ``get_usage`` excludes transcript-dependent behavior advice by default.
    """
    if subtype not in ("initialize", "get_usage"):
        raise ControlError("Unsupported Claude metadata request.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ControlError("Claude metadata timeout must be between zero and 60 seconds.")
    if fields is not None and (not isinstance(fields, dict) or subtype != "get_usage" or
                               set(fields) - {"skip_behaviors"} or fields.get("skip_behaviors", True) is not True):
        raise ControlError("Unsupported Claude metadata options.")
    initialize_id = "bc_init_" + uuid.uuid4().hex
    requests = [{"type": "control_request", "request_id": initialize_id,
                 "request": {"subtype": "initialize", "hooks": None}}]
    target_id = initialize_id
    if subtype == "get_usage":
        target_id = "bc_usage_" + uuid.uuid4().hex
        requests.append({"type": "control_request", "request_id": target_id,
                         "request": {"subtype": "get_usage", "skip_behaviors": True}})
    arguments = ["--print", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--safe-mode",
                 "--disable-slash-commands", "--no-session-persistence", "--max-turns", "1"]
    data = b"".join(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n" for value in requests)
    expected = {value["request_id"] for value in requests}
    replies = {}
    with tempfile.TemporaryDirectory(prefix="bc-claude-metadata-", dir=profile["home"]) as work:
        for record in adapter._records(profile, arguments, data, cancel=cancel, timeout=timeout, cwd=work,
                                       usage_metadata=subtype == "get_usage"):
            kind = record.get("type")
            if kind == "system":
                continue  # UI notices can carry account information; never forward them.
            if kind != "control_response":
                raise ControlError("Claude returned an unexpected metadata record.")
            response = record.get("response")
            if not isinstance(response, dict):
                raise ControlError("Claude returned an invalid metadata response.")
            identity = response.get("request_id")
            if not isinstance(identity, str) or identity not in expected or identity in replies:
                raise ControlError("Claude returned an unmatched metadata response.")
            if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                raise ControlError("Claude metadata is unavailable. Check the CLI version and subscription login.")
            replies[identity] = response["response"]
    if set(replies) != expected:
        raise ControlError("Claude did not complete the metadata request.")
    response = replies[target_id]
    if subtype == "initialize":
        # The SDK also returns identity, local paths, commands and environment
        # metadata. Discovery needs only models; do not hand those fields out.
        return {"models": response.get("models")}
    return {key: response[key] for key in ("subscription_type", "rate_limits_available", "rate_limits")
            if key in response}
