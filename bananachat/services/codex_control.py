"""Unbilled, bounded metadata calls through the Codex CLI's stdio app server.

The CLI owns ChatGPT authentication and refresh. No thread or turn is ever
started, no credential file is read here, and provider diagnostics stay
private. Messages are newline-delimited JSON objects without a "jsonrpc" member.
"""
from __future__ import annotations

import itertools
import json
import math
import tempfile
from contextlib import contextmanager

from bananachat import __version__
from bananachat.services import subscription_pool

METHODS = ("initialize", "account/read", "account/rateLimits/read", "model/list")
MAX_MODELS = 200
MAX_PAGES = 20
LEVELS = ("low", "medium", "high", "extra", "max")


class ControlError(RuntimeError):
    """A credential-free app-server failure."""


def _line(value):
    return json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n"


@contextmanager
def session(adapter, profile, *, cancel=None, timeout=30):
    """Start one app server and yield ``call(method, params)`` returning each validated result.

    Requests go one at a time through the transport's bounded I/O. Standard
    input stays open until the last reply: at end of input the server drops
    requests it has not answered. Leaving the block reaps the process group.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ControlError("Codex metadata timeout must be between zero and 60 seconds.")
    data = bytearray()
    numbers = itertools.count(1)
    with tempfile.TemporaryDirectory(prefix="bc-codex-metadata-", dir=profile["home"]) as work:
        records = adapter._records(profile, ["app-server", "--listen", "stdio://"], data, cancel=cancel,
                                   timeout=timeout, cwd=work, conversation=True)

        def call(method, params):
            if method not in METHODS or not isinstance(params, dict):
                raise ControlError("Unsupported Codex metadata request.")
            identity = next(numbers)
            data.extend(_line({"id": identity, "method": method, "params": params}))
            for record in records:
                if "method" in record:
                    if "id" in record:
                        raise ControlError("Codex sent an unexpected request.")
                    continue  # Notifications can carry account details; never forward them.
                if record.get("id") != identity:
                    raise ControlError("Codex returned an unmatched metadata response.")
                if "error" in record or not isinstance(record.get("result"), dict):
                    raise ControlError("Codex metadata is unavailable. Check the CLI version and subscription login.")
                return record["result"]
            raise ControlError("Codex did not complete the metadata request.")

        try:
            call("initialize", {"clientInfo": {"name": "bananachat", "title": "BananaChat", "version": __version__}})
            data.extend(_line({"method": "initialized"}))
            yield call
        finally:
            records.close()


def account(adapter, profile, *, cancel=None, timeout=15):
    """The login type, email and plan of a profile, or ``None`` when it is logged out."""
    with session(adapter, profile, cancel=cancel, timeout=timeout) as call:
        value = call("account/read", {}).get("account")
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ControlError("Codex returned an invalid account status.")
    # The reply may grow workspace routing details; hand out only what the connector checks.
    return {key: value.get(key) for key in ("type", "email", "planType")}


def rate_limits(adapter, profile, *, cancel=None, timeout=30):
    """The account's allowance snapshot, without the reset-credit detail lookup."""
    with session(adapter, profile, cancel=cancel, timeout=timeout) as call:
        value = call("account/rateLimits/read", {"excludeResetCreditDetails": True})
    return {key: value[key] for key in ("rateLimits", "rateLimitsByLimitId", "ordinaryUsageAllowed") if key in value}


def _text(item, key, default, maximum):
    value = item.get(key, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise ControlError("Codex returned invalid model metadata.")
    return " ".join(value.split())


def parse_models(items):
    """Visible models with the effort levels BananaChat can express; capabilities reflect this text-only transport.

    Hidden models (reviewers and previews) are never offered. ``xhigh`` becomes
    ``extra``; levels without a BananaChat equivalent, such as ``minimal`` or
    ``ultra``, are dropped rather than approximated. A model outside the Codex
    families is skipped, because it would have no strict default limits.
    """
    if not isinstance(items, list) or len(items) > MAX_MODELS:
        raise ControlError("Codex did not return a supported model catalogue.")
    found = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("hidden"), bool):
            raise ControlError("Codex returned an invalid model entry.")
        if item["hidden"]:
            continue
        name = _text(item, "model", "", 200)
        if not subscription_pool._MODEL_NAME.fullmatch(name):
            raise ControlError("Codex returned an invalid model identifier.")
        try:
            family = subscription_pool.family_of("codex", name)
        except ValueError:
            continue
        efforts = item.get("supportedReasoningEfforts", [])
        if not isinstance(efforts, list) or len(efforts) > 20 or any(
                not isinstance(option, dict) or not isinstance(option.get("reasoningEffort"), str) for option in efforts):
            raise ControlError("Codex returned invalid reasoning capabilities.")
        levels = {"extra" if option["reasoningEffort"] == "xhigh" else option["reasoningEffort"] for option in efforts}
        descriptor = {"name": name, "display": _text(item, "displayName", name, 500)[:120] or name, "family": family,
                      "description": _text(item, "description", "Codex model reported by the official CLI.", 2000)[:500],
                      "reasoning": [level for level in LEVELS if level in levels], "capabilities": ["completion"]}
        if name in found:
            # A duplicate entry cannot raise a model's capability.
            found[name]["reasoning"] = [level for level in found[name]["reasoning"] if level in levels]
        else:
            found[name] = descriptor
    return list(found.values())


def models(adapter, profile, *, cancel=None, timeout=30):
    """Ask the CLI for its model picker, page by page, without generating a model response."""
    items, cursor = [], None
    with session(adapter, profile, cancel=cancel, timeout=timeout) as call:
        for _ in range(MAX_PAGES):
            page = call("model/list", {"limit": 100} if cursor is None else {"limit": 100, "cursor": cursor})
            if not isinstance(page.get("data"), list):
                raise ControlError("Codex did not return a supported model catalogue.")
            items.extend(page["data"])
            if len(items) > MAX_MODELS:
                raise ControlError("Codex did not return a supported model catalogue.")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 200:
                raise ControlError("Codex returned an invalid model catalogue page.")
        else:
            raise ControlError("Codex returned too many model catalogue pages.")
    return parse_models(items)
