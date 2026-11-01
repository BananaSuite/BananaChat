"""Subscription pool: shared 5-hour/weekly quota over several site accounts.

The administrator configures authorized Claude and Codex account transports.
Users pick subscription models in BananaChat (``backend='claude'`` or
``backend='codex'``, with the matching ``provider``); on the backend the pool
picks a healthy account of that provider and talks to its configured transport.
This module does not scrape websites, store credentials, or establish provider
authorization.

Each provider is independent: a request for a Claude model is only ever served
by a Claude account and a Codex request only by a Codex account. The pool
reports its remaining 5-hour/weekly quota per provider through
``limits.register_capacity_provider`` so each provider's model limits shrink as
its shared allowance runs down. Account leases prevent concurrent requests from
choosing the same account; provider quota snapshots expire and failed refreshes
cannot raise capacity. Local counters are administrator-set token estimates,
not the provider's subscription billing units. Tests exercise these controls
without live credentials.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bananachat import db
from bananachat.db import catalog
from bananachat.db import subscription_pool as store

log = logging.getLogger("bananachat.subscription")

PROVIDERS = ("claude", "codex")
LABELS = {"claude": "Claude", "codex": "Codex"}

# Reference names verified against each provider's model overview. These are
# not proof of availability: only an adapter's discovery can enroll a model or
# declare its actual reasoning capabilities.
CURATED = {
    "claude": (
        {"name": "claude-fable-5-1", "display": "Claude Fable 5.1", "family": "fable",
         "description": "Claude Fable; the strictest account limits."},
        {"name": "claude-opus-5-5", "display": "Claude Opus 5.5", "family": "opus",
         "description": "Claude Opus for demanding work; strict limits."},
        {"name": "claude-sonnet-5-5", "display": "Claude Sonnet 5.5", "family": "sonnet",
         "description": "Claude Sonnet for everyday work."},
        {"name": "claude-opus-4-1", "display": "Claude Opus 4.1", "family": "opus",
         "description": "Claude Opus; strict account limits."},
        {"name": "claude-sonnet-4-5", "display": "Claude Sonnet 4.5", "family": "sonnet",
         "description": "Claude Sonnet for everyday work."},
        {"name": "claude-haiku-4-5", "display": "Claude Haiku 4.5", "family": "haiku",
         "description": "Claude Haiku for quick tasks."},
    ),
    "codex": (
        {"name": "gpt-6-astra", "display": "GPT-6-Astra", "family": "astra",
         "description": "GPT Astra for the most demanding work; the strictest account limits."},
        {"name": "gpt-6.1-sol", "display": "GPT-6.1-Sol", "family": "sol",
         "description": "GPT Sol for coding and everyday work."},
        {"name": "gpt-6-sol", "display": "GPT-6-Sol", "family": "sol",
         "description": "GPT Sol, previous generation."},
        {"name": "gpt-6-luna", "display": "GPT-6-Luna", "family": "luna",
         "description": "GPT Luna for quick tasks."},
        {"name": "gpt-5.6-terra", "display": "GPT-5.6-Terra", "family": "terra",
         "description": "GPT Terra for straightforward work."},
    ),
}

# Strict per-model defaults (tokens per 5-hour window; weekly off by default).
STRICTNESS = {
    "claude": {
        "fable": {"preset": "heavy", "weight": 5.0, "counts_toward_pool": False,
                  "rate_rules": [{"requests": 4, "per": "minute", "burst": 2},
                                 {"requests": 100, "per": "day", "burst": 100}],
                  "window_tokens": 30_000, "weekly_tokens": None, "dynamic": True,
                  "sensitivity": 2.5, "effort_default": "medium"},
        "opus": {"preset": "heavy", "weight": 3.0, "counts_toward_pool": False,
                 "rate_rules": [{"requests": 6, "per": "minute", "burst": 3},
                                {"requests": 200, "per": "day", "burst": 200}],
                 "window_tokens": 50_000, "weekly_tokens": None, "dynamic": True,
                 "sensitivity": 2.0, "effort_default": "medium"},
        "sonnet": {"preset": "standard", "weight": 1.5, "counts_toward_pool": False,
                   "rate_rules": [{"requests": 30, "per": "minute", "burst": 10},
                                  {"requests": 1000, "per": "day", "burst": 1000}],
                   "window_tokens": 150_000, "weekly_tokens": None, "dynamic": True,
                   "sensitivity": 1.0, "effort_default": "medium"},
        "haiku": {"preset": "light", "weight": 0.5, "counts_toward_pool": False,
                  "rate_rules": [{"requests": 60, "per": "minute", "burst": 20}],
                  "window_tokens": 300_000, "weekly_tokens": None, "dynamic": True,
                  "sensitivity": 0.5, "effort_default": "medium"},
    },
    "codex": {
        "astra": {"preset": "heavy", "weight": 5.0, "counts_toward_pool": False,
                  "rate_rules": [{"requests": 4, "per": "minute", "burst": 2},
                                 {"requests": 100, "per": "day", "burst": 100}],
                  "window_tokens": 30_000, "weekly_tokens": None, "dynamic": True,
                  "sensitivity": 2.5, "effort_default": "medium"},
        "sol": {"preset": "standard", "weight": 1.5, "counts_toward_pool": False,
                "rate_rules": [{"requests": 30, "per": "minute", "burst": 10},
                               {"requests": 1000, "per": "day", "burst": 1000}],
                "window_tokens": 150_000, "weekly_tokens": None, "dynamic": True,
                "sensitivity": 1.0, "effort_default": "medium"},
        "terra": {"preset": "standard", "weight": 1.0, "counts_toward_pool": False,
                  "rate_rules": [{"requests": 30, "per": "minute", "burst": 10},
                                 {"requests": 1000, "per": "day", "burst": 1000}],
                  "window_tokens": 200_000, "weekly_tokens": None, "dynamic": True,
                  "sensitivity": 1.0, "effort_default": "medium"},
        "luna": {"preset": "light", "weight": 0.5, "counts_toward_pool": False,
                 "rate_rules": [{"requests": 60, "per": "minute", "burst": 20}],
                 "window_tokens": 300_000, "weekly_tokens": None, "dynamic": True,
                 "sensitivity": 0.5, "effort_default": "medium"},
        # Older or newly released models without a tier name.
        "gpt": {"preset": "standard", "weight": 1.5, "counts_toward_pool": False,
                "rate_rules": [{"requests": 30, "per": "minute", "burst": 10},
                               {"requests": 1000, "per": "day", "burst": 1000}],
                "window_tokens": 150_000, "weekly_tokens": None, "dynamic": True,
                "sensitivity": 1.0, "effort_default": "medium"},
    },
}

# The family whose strict defaults an administrator's limit preset applies.
PRESET_FAMILIES = {
    "claude": {"light": "haiku", "standard": "sonnet", "heavy": "opus"},
    "codex": {"light": "luna", "standard": "sol", "heavy": "astra"},
}

POOL_CACHE_SECONDS = 30
QUOTA_MAX_AGE = timedelta(minutes=15)
DISCOVERY_CACHE_SECONDS = 300
DISCOVERY_RETRY_SECONDS = 15
BUSY_WAIT_SECONDS = 15
BUSY_POLL_SECONDS = 0.5
MAX_DISCOVERED_MODELS = 200
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_STREAM_RECORDS = 200_000
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")

COOLDOWN_ERROR = timedelta(minutes=5)  # an account whose request failed rests this long before it is tried again
_WITHDRAWN = "No adapter is connected for this provider; unavailable until one is configured."

# Each provider's transport reports under its own name; another provider's report is not this account's.
_NATIVE_SOURCES = {"claude": "claude_code", "codex": "codex"}
_PLANS = {
    "claude": {"pro": "Claude Pro", "max": "Claude Max", "team": "Claude Team", "enterprise": "Claude Enterprise"},
    "codex": {"free": "ChatGPT Free", "go": "ChatGPT Go", "plus": "ChatGPT Plus", "pro": "ChatGPT Pro",
              "team": "ChatGPT Team", "business": "ChatGPT Business", "enterprise": "ChatGPT Enterprise",
              "edu": "ChatGPT Edu"},
}
# A finer plan name is shown only when it is one the transport can verify.
_PLAN_DETAILS = {
    ("claude", "max"): {"Claude Max 5x", "Claude Max 20x", "Claude Max (5x)", "Claude Max (20x)", "Claude Max 5×",
                        "Claude Max 20×", "Max 5x", "Max 20x", "Max (5x)", "Max (20x)", "Max 5×", "Max 20×"},
    ("codex", "pro"): {"ChatGPT Pro (More)", "ChatGPT Pro (Max)"},
}


class QuotaExhausted(Exception):
    """Raised by a transport when the provider says an account is out of quota.

    ``resets_at`` (an aware datetime, optional) is when it has quota again; the
    pool rests the account until then (or until its 5-hour window ends) and
    moves on to the next one.
    """

    def __init__(self, message: str = "", resets_at: datetime | None = None):
        super().__init__(message or "The subscription account is out of quota.")
        if resets_at is not None and not isinstance(resets_at, datetime):
            raise ValueError("The provider quota reset must be a date and time.")
        self.resets_at = resets_at


class RequestRejected(RuntimeError):
    """The request itself cannot be answered (too long, not text, an unsupported effort): no account is at fault.

    ``spent`` marks a rejection after the provider read the prompt and worked on it (Codex tried a tool).
    """

    def __init__(self, message: str, *, spent: bool = False):
        super().__init__(message)
        self.spent = spent


class RejectedAfterSending(RuntimeError):
    """A request that failed once the provider's process had the prompt and worked on it (Codex tried a tool, or
    the process ran out of time before any output): the prompt used the account's allowance, so it is recorded on
    the account and charged as an interrupted request."""


_attempts = threading.local()


_TOO_LONG = re.compile(r"prompt is too long|context window|context_length_exceeded|maximum context length", re.I)


def request_too_long(message) -> bool:
    """Whether a provider's error message says the request itself is too long for the model."""
    return isinstance(message, str) and _TOO_LONG.search(message) is not None


def _may_rest_for(provider: str, user_id: str | None) -> bool:
    """Whether a failure of *user_id*'s request may rest an account: once per person and provider per
    ``COOLDOWN_ERROR``, across processes, so nobody can rest the pool one account at a time."""
    if not user_id:
        return True
    key, now = f"subscription:rested-by:{provider}:{user_id}", time.time()
    with db.transaction():
        row = db.one("SELECT updated_at FROM runtime_state WHERE key=?", (key,))
        if row is not None and now - float(row["updated_at"] or 0) < COOLDOWN_ERROR.total_seconds():
            return False
        db.execute("INSERT INTO runtime_state (key, value, updated_at) VALUES (?, '1', ?) ON CONFLICT(key) "
                   "DO UPDATE SET updated_at=excluded.updated_at", (key, now))
    return True


def cli_started() -> None:
    """Adapters call this just before they start the provider's process for a chat attempt.

    Until then nothing has reached the account: a failure (input it cannot encode, a temporary file, a sandbox
    that cannot be set up) says nothing about the account, so :func:`chat_stream` does not rest it.
    """
    attempt = getattr(_attempts, "current", None)
    if attempt is not None:
        attempt["started"] = True


class AdmissionChanged(Exception):
    """Refuse stale admission after refreshing account/model observations.

    A changed identity or model-specific allowance is not proof that every
    model on the subscription is exhausted. The adapter must publish its fresh
    observations before raising this; the pool can try another account before
    output, without placing a subscription-wide cooldown on the first account.
    """


class CapacityUnavailable(RuntimeError):
    """No account can admit a request before output; this is temporary capacity, not model failure."""

    code = "provider_capacity_unavailable"
    retry_after = 30


_transports: dict[str, dict] = {}
_pool_cache: dict[str, dict] = {}
_discovery_cache: dict[str, dict] = {}


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("Unknown subscription provider.")
    return provider


def _slot(provider: str) -> dict:
    return _transports.setdefault(_check_provider(provider),
                                  {"reporter": None, "chat": None, "discovery": None, "accepts_cancel": False,
                                   "reports_start": False})


def reset_transport(provider: str | None = None) -> None:
    """Discard registered adapters and process caches before reconfiguration."""
    for name in (PROVIDERS if provider is None else (provider,)):
        _transports.pop(name, None)
        _pool_cache.pop(name, None)
        _discovery_cache.pop(name, None)


def connected(provider: str) -> bool:
    """Whether an adapter is installed for this provider in this process."""
    return _slot(provider)["chat"] is not None


def discovers(provider: str) -> bool:
    """Whether an adapter's model discovery is installed for this provider in this process."""
    return _slot(provider)["discovery"] is not None


def invalidate(provider: str) -> None:
    """Forget a provider's cached discovery and pooled quota after its identity or models change."""
    _check_provider(provider)
    _discovery_cache.pop(provider, None)
    _pool_cache.pop(provider, None)


def forget_report(provider: str, account_id: int) -> None:
    """Drop one account's stored observation after its profile changes."""
    from bananachat.db import settings

    value = settings.state_get(f"{_check_provider(provider)}.account_reports")
    if isinstance(value, dict) and isinstance(value.get("reports"), dict):
        value["reports"].pop(str(account_id), None)
        settings.state_set(f"{provider}.account_reports", value)
    _pool_cache.pop(provider, None)


def register_site_reporter(provider: str, report) -> None:
    """Register how to read a provider's pooled quota: ``report()`` returns
    ``{"window_left": 0-1|None, "weekly_left": 0-1|None, "account_reports": {...}}``."""
    if report is not None and not callable(report):
        raise ValueError("The quota reporter must be callable.")
    _slot(provider)["reporter"] = report
    _pool_cache.pop(provider, None)


def register_site_chat(provider: str, handler) -> None:
    """Register ``chat(account, model, messages, options, *, cancel)`` for a provider.

    Older four-argument callbacks remain accepted. A production adapter must
    honor cancellation, bound its I/O and emit explicit terminal chunks. Token
    fields are nonnegative incremental deltas, never repeated totals.
    """
    slot = _slot(provider)
    if handler is not None:
        if not callable(handler):
            raise ValueError("The chat handler must be callable.")
        try:
            signature = inspect.signature(handler)
            accepts_cancel = "cancel" in signature.parameters or any(
                param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
            if accepts_cancel:
                signature.bind(None, "model", [], {}, cancel=None)
            else:
                signature.bind(None, "model", [], {})
        except (TypeError, ValueError) as error:
            raise ValueError("The chat handler must accept account, model, messages and options.") from error
        slot["accepts_cancel"] = accepts_cancel
    else:
        slot["accepts_cancel"] = False
    # Adapters that call cli_started() (reports_cli_start = True) let the pool tell the account's failures from
    # the request's or this server's own; for the others every failure counts as the account's, as before.
    slot["reports_start"] = getattr(getattr(handler, "__self__", None), "reports_cli_start", False) is True
    slot["chat"] = handler
    _pool_cache.pop(provider, None)


def register_site_discovery(provider: str, discover) -> None:
    """Register ``discover() -> [{name, family, reasoning, ...}]`` from an authorized adapter."""
    if discover is not None and not callable(discover):
        raise ValueError("The model discovery handler must be callable.")
    _slot(provider)["discovery"] = discover
    _discovery_cache.pop(provider, None)


def family_of(provider: str, model_name: str) -> str:
    _check_provider(provider)
    lowered = (model_name or "").lower()
    if provider == "claude":
        if "fable" in lowered or "mythos" in lowered:
            return "fable"
        if "opus" in lowered:
            return "opus"
        if "haiku" in lowered:
            return "haiku"
        if "sonnet" in lowered:
            return "sonnet"
        raise ValueError("Declare the Claude model's family: fable, opus, sonnet or haiku.")
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
    for family in ("astra", "luna", "terra", "sol"):
        if family in tokens:
            return family
    if lowered.startswith("gpt-"):
        return "gpt"
    raise ValueError("Declare the Codex model's family: astra, sol, terra, luna or gpt.")


def strict_policy(provider: str, model_name: str, *, family: str | None = None) -> dict:
    """The strict default model policy for a subscription model (see STRICTNESS)."""
    families = STRICTNESS[_check_provider(provider)]
    family = family or family_of(provider, model_name)
    if family not in families:
        raise ValueError("Unknown subscription model family.")
    base = dict(families[family])
    return {"preset": base["preset"], "enabled": True, "weight": base["weight"],
            "counts_toward_pool": base["counts_toward_pool"], "rate_rules": list(base["rate_rules"]),
            "window_tokens": base["window_tokens"], "weekly_tokens": base["weekly_tokens"],
            "dynamic": base["dynamic"], "sensitivity": base["sensitivity"], "auto_tiers": False,
            "effort_default": base["effort_default"]}


def _model_descriptor(provider: str, item) -> dict:
    from bananachat.db import limits as limits_db

    families = STRICTNESS[provider]
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _MODEL_NAME.fullmatch(item["name"]):
        raise ValueError("Discovery must return valid model ids.")
    name = item["name"]
    family = item.get("family") or family_of(provider, name)
    if family not in families:
        raise ValueError("Discovery returned an unsupported model family.")
    levels = item.get("reasoning", [])
    if not isinstance(levels, (list, tuple)) or len(levels) > 10 or not all(isinstance(level, str) for level in levels):
        raise ValueError("Declare a model's supported reasoning levels as a list.")
    levels = {"extra" if level == "xhigh" else level for level in levels}
    if not levels <= set(limits_db.EFFORT_LEVELS) | {"on"}:
        raise ValueError("Discovery returned an unsupported reasoning level.")
    capabilities = item.get("capabilities", ["completion"])
    if not isinstance(capabilities, (list, tuple)) or not set(capabilities) <= {"completion", "vision"}:
        raise ValueError("Discovery returned unsupported model capabilities.")
    account_ids = item.get("account_ids")
    if account_ids is not None and (not isinstance(account_ids, (list, tuple)) or len(account_ids) > 200 or
                                    any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                                        for value in account_ids)):
        raise ValueError("Discovery returned invalid account ids.")
    return {"name": name, "display": str(item.get("display") or name)[:120], "family": family,
            "description": str(item.get("description") or "Model available through the configured adapter.")[:500],
            "reasoning": sorted(levels, key=limits_db.effort_rank),
            "capabilities": sorted(set(capabilities) | {"completion"}),
            "account_ids": sorted(set(account_ids)) if account_ids is not None else None}


def discovered_models(provider: str, *, force: bool = False) -> dict:
    """A bounded adapter snapshot. Unconfigured or failed discovery proves no model available."""
    _check_provider(provider)
    cache = _discovery_cache.get(provider)
    now = time.monotonic()
    if not force and cache is not None and cache.get("path") == db.path() \
            and now - cache["at"] < DISCOVERY_CACHE_SECONDS:
        return cache["value"]
    state = {"at": db.now(), "ok": False, "models": [], "note": "No model discovery adapter is configured."}
    discover = _slot(provider)["discovery"]
    if discover is not None:
        try:
            items = discover()
            if not isinstance(items, (list, tuple)) or len(items) > MAX_DISCOVERED_MODELS:
                raise ValueError(f"Discovery must return at most {MAX_DISCOVERED_MODELS} model descriptors.")
            models = [_model_descriptor(provider, item) for item in items]
            if len({item["name"] for item in models}) != len(models):
                raise ValueError("Discovery returned duplicate model ids.")
            state.update(ok=True, models=models, note="" if models else "The adapter reports no available models.")
        except Exception as error:  # noqa: BLE001 - unavailable discovery never fabricates a catalog
            state["note"] = "Model discovery failed. Check the configured adapter."
            log.warning("%s model discovery failed (%s)", LABELS[provider], type(error).__name__)
    # A failed read is tried again soon: one busy moment must not keep a provider's models unavailable for minutes.
    _discovery_cache[provider] = {"at": now if state["ok"] else now - DISCOVERY_CACHE_SECONDS + DISCOVERY_RETRY_SECONDS,
                                  "value": state, "path": db.path()}
    return state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _current(row, now: datetime | None = None) -> tuple[int, int]:
    """Current counters; only local budget periods may roll over without a fresh report."""
    now = now or _now()
    window_reset = db.parse_timestamp(row["window_resets_at"])
    weekly_reset = db.parse_timestamp(row["weekly_resets_at"])
    local = row["quota_source"] == "local"
    window = 0 if local and window_reset is not None and window_reset <= now else int(row["window_used"] or 0)
    weekly = 0 if local and weekly_reset is not None and weekly_reset <= now else int(row["weekly_used"] or 0)
    return window, weekly


def _resting(row, now: datetime | None = None) -> bool:
    """Whether the account is resting after the provider reported it out of quota (or a failed request)."""
    try:
        until = row["cooldown_until"]
    except (IndexError, KeyError):
        return False
    moment = db.parse_timestamp(until)
    return moment is not None and moment > (now or _now())


def quota_fresh(row, now: datetime | None = None) -> bool:
    """Provider snapshots must be recent and within their reported reset periods."""
    if row["quota_source"] == "local":
        return True
    now = now or _now()
    for limit, reset, updated in (("window_limit", "window_resets_at", "quota_window_updated_at"),
                                  ("weekly_limit", "weekly_resets_at", "quota_weekly_updated_at")):
        if row[limit] is None:
            continue
        checked = db.parse_timestamp(row[updated])
        if checked is None or checked > now + timedelta(minutes=1) or now - checked > QUOTA_MAX_AGE:
            return False
        moment = db.parse_timestamp(row[reset])
        if moment is not None and moment <= now:
            return False
    return True


def account_reports(provider: str) -> dict | None:
    """Sanitized, shared provider observations; ``None`` retains legacy local-only routing."""
    if _slot(provider)["reporter"] is None:
        return None
    from bananachat.db import settings

    value = settings.state_get(f"{provider}.account_reports")
    return value.get("reports") if isinstance(value, dict) and isinstance(value.get("reports"), dict) else None


def _valid_fraction(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def _validated_report(provider: str, report) -> dict:
    """Persist only quota/plan metadata, never an adapter's arbitrary account or error details."""
    if not isinstance(report, dict) or not isinstance(report.get("available"), bool):
        raise ValueError("Invalid account quota observation.")
    source = report.get("source")
    if source not in ("local", "file", _NATIVE_SOURCES[provider]):
        raise ValueError("Invalid account quota source.")
    status = "available" if report["available"] else "unavailable"
    if report.get("status") == "stale":
        if report["available"]:
            raise ValueError("Stale account quota cannot be available.")
        if source == _NATIVE_SOURCES[provider]:
            status = "stale"
    result = {"available": report["available"], "source": source, "status": status}
    for key in ("observed_at", "window_resets_at", "weekly_resets_at"):
        value = report.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                  not math.isfinite(value) or not 0 <= value <= 253402300799):
            raise ValueError("Invalid account quota date.")
        result[key] = value
    for key in ("window_left", "weekly_left"):
        value = report.get(key)
        if value is not None and not _valid_fraction(value):
            raise ValueError("Invalid account quota fraction.")
        result[key] = value
    plans = _PLANS[provider]
    plan = report.get("subscription_type")
    if plan not in plans:
        plan = None
    result["subscription_type"] = plan
    result["plan_label"] = plans.get(plan)
    verified_label = report.get("plan_label")
    if isinstance(verified_label, str) and verified_label in _PLAN_DETAILS.get((provider, plan), ()):
        result["plan_label"] = verified_label
    result["model_limits"] = {}
    scopes = report.get("model_limits", {})
    if not isinstance(scopes, dict):
        raise ValueError("Invalid account model quota.")
    for family in STRICTNESS[provider]:
        scope = scopes.get(family)
        if scope is None:
            continue
        if not isinstance(scope, dict) or not _valid_fraction(scope.get("left")):
            raise ValueError("Invalid account model quota.")
        reset = scope.get("resets_at")
        if reset is not None and (isinstance(reset, bool) or not isinstance(reset, (int, float)) or
                                  not math.isfinite(reset) or not 0 <= reset <= 253402300799):
            raise ValueError("Invalid account model quota date.")
        result["model_limits"][family] = {"left": scope["left"], "resets_at": reset}
    return result


def _store_account_reports(provider: str, reported) -> None:
    from bananachat.db import settings

    reports = reported.get("account_reports")
    if reports is None:
        settings.state_set(f"{provider}.account_reports", None)
        return
    if not isinstance(reports, dict) or len(reports) > 200:
        raise ValueError("Invalid account quota observations.")
    fallback = _NATIVE_SOURCES[provider]
    kept = settings.state_get(f"{provider}.account_reports")
    kept = kept.get("reports") if isinstance(kept, dict) and isinstance(kept.get("reports"), dict) else {}
    now = _now()
    valid = {}
    for row in store.list_accounts(provider=provider, include_disabled=False):
        key = str(row["id"])
        try:
            valid[key] = _validated_report(provider, reports.get(key, reports.get(row["id"])))
        except ValueError:
            # A failed account observation must not withdraw every healthy account.
            valid[key] = {"available": False, "source": fallback, "status": "unavailable"}
        # Workers read usage separately; one worker's failed read must not erase what another just observed.
        if not valid[key]["available"] and valid[key].get("observed_at") is None and _report_fresh(kept.get(key), now):
            valid[key] = kept[key]
    settings.state_set(f"{provider}.account_reports", {"at": time.time(), "reports": valid})


def _report_fresh(report, now: datetime, family: str | None = None) -> bool:
    if not isinstance(report, dict) or not report.get("available"):
        return False
    if report.get("source") == "local":
        return True
    observed = report.get("observed_at")
    moment = now.timestamp()
    if not isinstance(observed, (int, float)) or not 0 <= moment - observed <= QUOTA_MAX_AGE.total_seconds():
        return False
    # Automatic routing requires an observed five-hour allowance, not just a login/plan.
    if report.get("window_left") is None and report.get("source") in _NATIVE_SOURCES.values():
        return False
    for key in ("window_resets_at", "weekly_resets_at"):
        reset = report.get(key)
        if reset is not None and reset <= moment:
            return False
    scope = report.get("model_limits", {}).get(family) if family else None
    if scope:
        reset = scope.get("resets_at")
        if reset is not None and reset <= moment:
            return False
    return True


def subscription_fresh(row, now: datetime | None = None) -> bool:
    reports = account_reports(row["provider"])
    return reports is None or _report_fresh(reports.get(str(row["id"])), now or _now())


def _capped(left: float | None, percent: int | None) -> float | None:
    """The share of an allowance still usable when an administrator caps use at *percent* of it."""
    if percent is None:
        return left
    if left is None:
        return 0.0  # A cap on an allowance the provider does not report cannot be honoured.
    # Whole percentages: subtracting 1 - percent / 100 in floats leaves a residue at the cap (0.9 is not exact).
    used = round((1 - left) * 100, 6)
    return 0.0 if used >= percent else min(1.0, (percent - used) / percent)


def _upstream_capacity(row, now: datetime, family: str | None = None) -> tuple[float | None, float | None]:
    try:
        family_pct = store.scoped_caps(row["id"]).get(family, {}).get("cap_pct") if family else None
    except Exception:  # noqa: BLE001 - an unreadable cap must not grant capacity
        return 0.0, 0.0
    capped = row["window_cap_pct"] is not None or row["weekly_cap_pct"] is not None or family_pct is not None
    reports = account_reports(row["provider"])
    if reports is None:
        return (0.0, 0.0) if capped else (None, None)
    report = reports.get(str(row["id"]))
    if not _report_fresh(report, now, family):
        return 0.0, 0.0
    if report.get("source") == "local":
        return (0.0, 0.0) if capped else (None, None)
    window = _capped(report.get("window_left"), row["window_cap_pct"])
    weekly = _capped(report.get("weekly_left"), row["weekly_cap_pct"])
    if family:
        scope = report.get("model_limits", {}).get(family)
        left = _capped(scope["left"] if scope else None, family_pct)
        if left is not None:
            weekly = min(weekly, left) if weekly is not None else left
    return window, weekly


def _scoped_ok(row, family: str | None, now: datetime | None = None) -> bool:
    """Honour an administrator's per-account, per-family ceiling (fail closed on error)."""
    if not family:
        return True
    from bananachat.db import limits as limits_db

    try:
        cap = store.scoped_caps(row["id"]).get(family)
    except Exception:  # noqa: BLE001 - an unreadable cap must not grant capacity
        return False
    if not cap:
        return True
    moment = (now or _now()).timestamp()
    for key, span in (("window_cap", limits_db.WINDOW_SECONDS), ("weekly_cap", limits_db.WEEK_SECONDS)):
        limit = cap.get(key)
        if limit is None:
            continue
        try:
            used = store.scoped_sum(row["id"], family, since_epoch=moment - span)
        except Exception:  # noqa: BLE001 - unknown usage denies the scoped cap
            return False
        if used >= int(limit):
            return False
    return True


def usable(row, now: datetime | None = None, *, family: str | None = None) -> bool:
    """Active, not resting, and with 5-hour and weekly quota left (as far as the pool knows)."""
    if row["status"] != "active" or row["window_limit"] is None or _resting(row, now) or not quota_fresh(row, now):
        return False
    now = now or _now()
    upstream = _upstream_capacity(row, now, family)
    if any(value is not None and value <= 0 for value in upstream):
        return False
    window, weekly = _current(row, now)
    if row["window_limit"] is not None and window >= int(row["window_limit"]):
        return False
    if row["weekly_limit"] is not None and weekly >= int(row["weekly_limit"]):
        return False
    return _scoped_ok(row, family, now)


def accounts_in_order(provider: str, exclude: set[int] | None = None, now: datetime | None = None,
                      *, family: str | None = None) -> list:
    """Usable accounts, best first: highest priority, then the most 5-hour quota left."""
    exclude = exclude or set()
    now = now or _now()
    rows = [row for row in store.list_accounts(provider=provider, include_disabled=False)
            if row["id"] not in exclude and usable(row, now, family=family) and not store.leased(row["id"])]

    def left(row) -> float:
        local = 1 - _current(row, now)[0] / float(row["window_limit"])
        observed = [value for value in _upstream_capacity(row, now, family) if value is not None]
        return min([local, *observed])

    return sorted(rows, key=lambda row: (-int(row["priority"] or 0), -left(row), row["id"]))


def pick_account(provider: str, exclude: set[int] | None = None):
    """The best usable subscription account (highest priority, then most quota left), or None."""
    ordered = accounts_in_order(provider, exclude)
    return ordered[0] if ordered else None


def _fractions_from_accounts(rows, now: datetime | None = None, *, family: str | None = None) \
        -> tuple[float | None, float | None]:
    """Pooled ``(window_left, weekly_left)`` fractions 0-1 from reported usage.

    The pool's quota is the sum over its active accounts (a resting account
    counts as empty): one exhausted subscription only removes its own share,
    the others keep serving. With no active account at all nothing is left (0).
    """
    now = now or _now()
    active = [row for row in rows if row["status"] == "active"]
    if not active:
        return 0.0, 0.0
    fractions = []
    for limit_key, index in (("window_limit", 0), ("weekly_limit", 1)):
        # An account without a weekly budget can still serve when the budgeted accounts are exhausted; its share
        # is what the provider reports, and an unusable account counts as empty.
        if index == 1 and any(row[limit_key] is None and usable(row, now, family=family) for row in active):
            rows = [row for row in active if row[limit_key] is None]
            upstream = [_upstream_capacity(row, now, family)[1] if usable(row, now, family=family) else 0.0
                        for row in rows]
            fractions.append(None if any(value is None for value in upstream) else sum(upstream) / len(rows))
            continue
        total = left = 0.0
        for row in active:
            limit = float(row[limit_key] or 0)
            if limit <= 0:
                continue
            total += limit
            if usable(row, now, family=family):
                remaining = max(0.0, limit - _current(row, now)[index])
                upstream = _upstream_capacity(row, now, family)[index]
                left += min(remaining, limit * upstream) if upstream is not None else remaining
        fractions.append(max(0.0, min(1.0, left / total)) if total > 0 else None)
    if not any(usable(row, now, family=family) for row in active):
        return 0.0, 0.0
    return fractions[0], fractions[1]


def pooled_quota(provider: str, force: bool = False) -> dict:
    """Cached pooled quota for one provider ``{"window_left", "weekly_left", ...}``."""
    _check_provider(provider)
    cache = _pool_cache.get(provider)
    now = time.monotonic()
    if not force and cache is not None and now - cache["at"] < POOL_CACHE_SECONDS and cache.get("path") == db.path():
        return cache["value"]
    reporter = _slot(provider)["reporter"]
    reporter_error = False
    reported = None
    if reporter is not None:
        try:
            reported = reporter()
            if not isinstance(reported, dict):
                raise ValueError("Invalid quota report.")
            _store_account_reports(provider, reported)
        except Exception as error:  # noqa: BLE001 - failed reports never unlock capacity
            reporter_error = True
            log.warning("Reading the %s pool quota failed (%s)", LABELS[provider], type(error).__name__)
    rows = store.list_accounts(provider=provider, include_disabled=False)
    window_left, weekly_left = _fractions_from_accounts(rows)
    if reported is not None and not reporter_error:
        try:
            fractions = []
            for key, stored in (("window_left", window_left), ("weekly_left", weekly_left)):
                value = reported.get(key)
                if value is None:
                    fractions.append(stored)
                    continue
                if not _valid_fraction(value):
                    raise ValueError("Invalid quota fraction.")
                fractions.append(min(stored, float(value)) if stored is not None else float(value))
            window_left, weekly_left = fractions
        except Exception as error:  # noqa: BLE001 - failed reports never unlock capacity
            reporter_error = True
            log.warning("Reading the %s pool quota failed (%s)", LABELS[provider], type(error).__name__)
    if _slot(provider)["chat"] is None or reporter_error:
        window_left = weekly_left = 0.0
    value = {"window_left": window_left, "weekly_left": weekly_left,
             "accounts": len([r for r in rows if r["status"] == "active"]),
             "usable": len([r for r in rows if usable(r)]),
             "busy": len([r for r in rows if store.leased(r["id"])]),
             "stale": len([r for r in rows if not quota_fresh(r) or not subscription_fresh(r)]),
             "reporter_error": reporter_error}
    _pool_cache[provider] = {"at": now, "value": value, "path": db.path()}
    try:
        from bananachat.db import settings as site_settings
        site_settings.state_set(f"{provider}_pool_quota", {**value, "at": time.time()})
    except Exception as error:  # noqa: BLE001 - the stored copy is informational
        log.warning("Could not save the %s pool quota (%s)", LABELS[provider], type(error).__name__)
    return value


def capacity_report(model):
    """``limits.Capacity`` for a subscription model from its provider's pooled quota."""
    from bananachat.services import limits as limits_service

    provider = model["provider"]
    if provider not in PROVIDERS:
        return limits_service.Capacity(window_left=0.0, weekly_left=0.0)
    discovery = discovered_models(provider)
    name = model["backend_model_name"] or model["ollama_name"]
    descriptor = next((item for item in discovery["models"] if item["name"] == name), None)
    if not discovery["ok"] or descriptor is None or _slot(provider)["chat"] is None:
        return limits_service.Capacity(window_left=0.0, weekly_left=0.0)
    quota = pooled_quota(provider)
    window, weekly = quota["window_left"], quota["weekly_left"]
    rows = [row for row in store.list_accounts(provider=provider, include_disabled=False)
            if descriptor["account_ids"] is None or row["id"] in descriptor["account_ids"]]
    own_window, own_weekly = _fractions_from_accounts(rows, family=descriptor["family"])
    window = min(window, own_window) if window is not None and own_window is not None else own_window
    weekly = min(weekly, own_weekly) if weekly is not None and own_weekly is not None else own_weekly
    return limits_service.Capacity(window_left=window, weekly_left=weekly)


def ensure_registered() -> None:
    """Hook each provider's pooled quota into dynamic model limits (idempotent)."""
    from bananachat.services import limits as limits_service

    for provider in PROVIDERS:
        limits_service.register_capacity_provider(provider, capacity_report)


def refresh_quota(provider: str | None = None) -> dict:
    """Re-read the pooled quota now (reporter + stored account usage)."""
    result = {}
    for name in (PROVIDERS if provider is None else (_check_provider(provider),)):
        _pool_cache.pop(name, None)
        result[name] = pooled_quota(name, force=True)
    return result


def record_usage(account_id: int, tokens_in: int, tokens_out: int, *, provider: str, family: str = "",
                 lease_id: str | None = None) -> None:
    """Add one answer to an account's 5-hour/weekly counters and the usage ledger.

    Local budgets start another window after their reset. Provider snapshots
    retain their reported counters and resets until the adapter refreshes them.
    """
    from bananachat.db import limits as limits_db

    now = _now()
    tokens_in = max(0, int(tokens_in or 0))
    tokens_out = max(0, int(tokens_out or 0))
    total = tokens_in + tokens_out
    with db.transaction():
        row = store.get(account_id)
        if row is None:
            raise RuntimeError("The subscription account was removed before usage could be recorded.")
        if row["provider"] != provider:
            raise RuntimeError("The usage belongs to another subscription provider.")
        if lease_id is not None and not store.insert_usage(lease_id, account_id, provider, family, tokens_in,
                                                           tokens_out, now_epoch=now.timestamp()):
            return
        local = row["quota_source"] == "local"
        window_reset = db.parse_timestamp(row["window_resets_at"])
        window_used = int(row["window_used"] or 0)
        if local and window_reset is not None and window_reset <= now:
            window_used = 0
        if local and (window_reset is None or window_reset <= now):
            window_reset = now + timedelta(seconds=limits_db.WINDOW_SECONDS)
        weekly_reset = db.parse_timestamp(row["weekly_resets_at"])
        weekly_used = int(row["weekly_used"] or 0)
        if local and weekly_reset is not None and weekly_reset <= now:
            weekly_used = 0
        if local and (weekly_reset is None or weekly_reset <= now):
            weekly_reset = now + timedelta(seconds=limits_db.WEEK_SECONDS)
        store.bump_counters(account_id, window_used + total,
                            db.timestamp(window_reset) if window_reset else None,
                            weekly_used + total, db.timestamp(weekly_reset) if weekly_reset else None)
    _pool_cache.pop(provider, None)


def rest(account_id: int, until: datetime, error: str = "", *, lease_id: str | None = None) -> bool:
    """Skip an account until *until* (the provider said it is out of quota, or a request failed)."""
    changed = store.rest(account_id, db.timestamp(until), error, lease_id=lease_id)
    if changed:
        _pool_cache.clear()
    return changed


# ----- adapter discovery and catalog enrollment ----------------------------------

def automatic_enrollment(provider: str) -> bool:
    """A provider can follow the catalog default or have an explicit enrollment policy."""
    from bananachat.db import settings
    from bananachat.services import model_lifecycle

    value = settings.state_get(f"{provider}.auto_enroll")
    return value if isinstance(value, bool) else model_lifecycle.policy().get("enrollment") == "automatic"


def sync_catalog(provider: str, *, selected: list[str] | None = None, source: str = "background",
                 auto_enroll: bool | None = None) -> dict:
    """Enroll only adapter-discovered models, respecting selection and the ignore list.

    A failed discovery leaves the catalog intact. Explicit exclusions are saved so
    a later automatic refresh can enroll new models without re-enabling rejected ones.
    Policies are stored before publication in the same transaction.
    """
    from bananachat.db import limits as limits_db
    from bananachat.db import model_lifecycle as lifecycle_db
    from bananachat.db import settings as site_settings
    from bananachat.services import model_lifecycle

    _check_provider(provider)
    discovery = discovered_models(provider, force=True)
    if not discovery["ok"]:
        raise ValueError(discovery["note"])
    available = {item["name"]: item for item in discovery["models"]}
    if auto_enroll is not None and not isinstance(auto_enroll, bool):
        raise ValueError("Choose manual or automatic enrollment.")
    automatic = automatic_enrollment(provider) if auto_enroll is None else auto_enroll
    excluded = site_settings.state_get(f"{provider}.excluded_models")
    if not isinstance(excluded, list) or not all(isinstance(name, str) for name in excluded):
        excluded = None
    if selected is not None:
        if not isinstance(selected, (list, tuple)) or len(selected) > MAX_DISCOVERED_MODELS or \
                not all(isinstance(name, str) and name in available for name in selected):
            raise ValueError("Select only models the configured adapter reports as available.")
        selection = sorted(set(selected))
        excluded = sorted((set(excluded or []) - available.keys()) | (available.keys() - set(selection)))
    else:
        selection = site_settings.state_get(f"{provider}.selected_models")
        if not isinstance(selection, list) or not all(isinstance(name, str) for name in selection):
            selection = None
    rules = model_lifecycle.patterns()
    existing_names = {row["ollama_name"]
                      for row in db.query("SELECT ollama_name FROM ai_models WHERE backend=?", (provider,))}
    if automatic:
        # Older selections remain exclusions on the first refresh. Later genuinely new models can
        # enroll without bringing back an explicitly unchecked model.
        if excluded is None and selection is not None:
            excluded = sorted(available.keys() - set(selection))
        selection = sorted(available.keys() - set(excluded or []))
    elif selection is None and source == "background":
        selection = list(existing_names)
    wanted = [item for name, item in available.items() if selection is None or name in selection]
    names = {item["name"] for item in wanted}
    state = {"at": db.now(), "source": source, "ok": True, "count": len(wanted), "new": [],
             "enabled": [], "missing": [], "note": ""}
    with db.transaction():
        if selected is not None:
            site_settings.state_set(f"{provider}.selected_models", sorted(set(selected)))
        if excluded is not None:
            site_settings.state_set(f"{provider}.excluded_models", excluded)
        if auto_enroll is not None:
            site_settings.state_set(f"{provider}.auto_enroll", auto_enroll)
        for item in wanted:
            ignored = model_lifecycle.matches(item["name"], rules)
            existing = catalog.get_by_name(item["name"])
            if existing is not None and existing["backend"] != provider:
                raise ValueError("A discovered id conflicts with a different model backend.")
            if existing is None:
                enrollment = "ignored" if ignored else "new"
                reason = "Matches the ignore list." if ignored else "Discovered by the adapter; waiting for review."
                cursor = db.execute(
                    "INSERT INTO ai_models (ollama_name, backend, provider, backend_model_name, backend_available, "
                    "backend_last_seen_at, display_name, description, is_rolled_out, is_image_generation, sort_order, "
                    "created_at, updated_at, enrollment, family, capabilities, reasoning_levels, is_reasoning, "
                    "supports_vision, state_reason, state_changed_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0, 0, "
                    "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM ai_models), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["name"], provider, provider, item["name"], db.now(), item["display"], item["description"],
                     db.now(), db.now(), enrollment, item["family"], json.dumps(item["capabilities"]),
                     json.dumps(item["reasoning"]), int(any(level != "off" for level in item["reasoning"])),
                     int("vision" in item["capabilities"]), reason, db.now()))
                existing = catalog.get(cursor.lastrowid)
                state["new"].append(item["name"])
                lifecycle_db.add_event(existing["id"], item["name"], "detected", reason)
            else:
                db.execute("UPDATE ai_models SET backend_available=1, backend_last_seen_at=?, "
                           "missing_at=NULL, missing_reason=NULL, family=?, capabilities=?, reasoning_levels=?, "
                           "is_reasoning=?, supports_vision=?, "
                           "state_reason=CASE WHEN state_reason=? THEN ? ELSE state_reason END WHERE id=?",
                           (db.now(), item["family"], json.dumps(item["capabilities"]),
                            json.dumps(item["reasoning"]),
                            int(any(level != "off" for level in item["reasoning"])),
                            int("vision" in item["capabilities"]), _WITHDRAWN,
                            "Reported by the adapter again.", existing["id"]))
            strict = strict_policy(provider, item["name"], family=item["family"])
            if not limits_db.has_model_policy(existing["id"]):
                limits_db.set_model_policy(existing["id"], strict, None)
            if ignored:
                catalog.set_lifecycle(existing["id"], enrollment="ignored", is_rolled_out=0,
                                      state_reason="Matches the ignore list.")
            elif automatic and existing["enrollment"] == "new":
                reason = "Enabled automatically with strict model limits; review it."
                catalog.set_lifecycle(existing["id"], enrollment="auto", enrolled_at=db.now(), is_rolled_out=1,
                                      limit_preset=strict["preset"], state_reason=reason)
                lifecycle_db.add_event(existing["id"], item["name"], "auto_enabled", reason)
                state["enabled"].append(item["name"])
        for row in db.query("SELECT * FROM ai_models WHERE backend=?", (provider,)):
            if row["ollama_name"] not in names and not row["missing_at"]:
                reason = "Not selected or not reported by the adapter; unavailable until rediscovered."
                db.execute("UPDATE ai_models SET backend_available=0, missing_at=?, missing_reason=?, "
                           "state_reason=?, state_changed_at=? WHERE id=?",
                           (db.now(), reason, reason, db.now(), row["id"]))
                state["missing"].append(row["ollama_name"])
    return state


def withdraw(provider: str) -> list[str]:
    """Stop offering a provider's models while no adapter is connected for it.

    Only availability changes: the rows keep their settings and are not marked
    missing, so a temporary disconnection never lets the lifecycle retire them.
    """
    names = []
    with db.transaction():
        for row in db.query("SELECT id, ollama_name FROM ai_models WHERE backend=? AND backend_available=1",
                            (_check_provider(provider),)):
            db.execute("UPDATE ai_models SET backend_available=0, state_reason=?, state_changed_at=? WHERE id=?",
                       (_WITHDRAWN, db.now(), row["id"]))
            names.append(row["ollama_name"])
    return names


class _AccountLease:
    """A request-local account claim, renewed by the existing process supervisor."""

    def __init__(self, account, lease_id, cancel):
        self.account, self.lease_id, self.cancel = account, lease_id, cancel
        self.path = db.path()
        self.key = f"{account['provider']}:{lease_id}"

    def check(self, renew=False):
        if db.path() != self.path:
            return "Subscription account lease lost"
        try:
            active = store.renew(self.account["id"], self.lease_id) if renew else \
                store.owns(self.account["id"], self.lease_id)
        except Exception:  # noqa: BLE001 - stop using an account when ownership cannot be established
            return "Subscription account storage unavailable"
        return None if active else "Subscription account lease lost"

    def __enter__(self):
        from bananachat.services import supervisor

        supervisor.register_watch(self.key, self.cancel, self.check)
        return self

    def require_active(self):
        self.cancel.check()
        reason = self.check()
        if reason:
            self.cancel.cancel(reason)
            self.cancel.check()

    def rest(self, until, error):
        if not rest(self.account["id"], until, error, lease_id=self.lease_id):
            self.cancel.cancel("Subscription account lease lost")
            self.cancel.check()

    def __exit__(self, *_):
        from bananachat.services import supervisor

        supervisor.unregister_watch(self.key, self.cancel)
        store.release(self.account["id"], self.lease_id)


class _StreamError(RuntimeError):
    """A controlled contract/storage error that is safe to show to the requester."""


def _claim_account(provider, descriptor, exclude, account_id, cancel):
    cancel.check()
    allowed = descriptor["account_ids"]
    with db.transaction():
        rows = accounts_in_order(provider, exclude, family=descriptor["family"])
        for account in rows:
            if account_id is not None and account["id"] != account_id:
                continue
            if allowed is not None and account["id"] not in allowed:
                continue
            lease_id = uuid.uuid4().hex
            if store.claim(account["id"], lease_id, provider=provider):
                return _AccountLease(account, lease_id, cancel)
    return None


def _reported_reset(account) -> datetime | None:
    """When the provider says this account has allowance again, from its last observation."""
    report = (account_reports(account["provider"]) or {}).get(str(account["id"]))
    if not isinstance(report, dict):
        return None
    reset = report.get("weekly_resets_at" if report.get("weekly_left") == 0 else "window_resets_at")
    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or reset <= time.time():
        return None
    return datetime.fromtimestamp(reset, timezone.utc)


def _busy(provider, descriptor, exclude, account_id) -> bool:
    """Whether an account that could serve this request is only answering another one right now."""
    allowed = descriptor["account_ids"]
    return any(store.leased(row["id"]) and usable(row, family=descriptor["family"])
               for row in store.list_accounts(provider=provider, include_disabled=False)
               if row["id"] not in exclude and (account_id is None or row["id"] == account_id)
               and (allowed is None or row["id"] in allowed))


def _claim_waiting(provider, descriptor, exclude, account_id, cancel):
    """Claim an account, waiting a little while every usable one is busy with another request."""
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    while True:
        lease = _claim_account(provider, descriptor, exclude, account_id, cancel)
        if lease is not None or time.monotonic() >= deadline or not _busy(provider, descriptor, exclude, account_id):
            return lease
        time.sleep(BUSY_POLL_SECONDS)
        cancel.check()


def _chunk(item):
    """Validate the adapter contract before output or token deltas become observable."""
    if not isinstance(item, dict):
        raise _StreamError("The adapter returned an invalid stream record.")
    result = {}
    for key in ("text", "thinking"):
        value = item.get(key, "")
        if not isinstance(value, str):
            raise _StreamError("The adapter returned invalid text.")
        result[key] = value
    for key in ("tokens_in", "tokens_out"):
        value = item.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or
                                  not 0 <= value <= store.TOKENS_MAX):
            raise _StreamError("The adapter returned invalid token usage.")
        if value is not None:
            result[key] = value
    done = item.get("done", False)
    if not isinstance(done, bool):
        raise _StreamError("The adapter returned an invalid terminal marker.")
    result["done"] = done
    finish = item.get("finish_reason", "")
    if finish not in ("", "stop", "length", "content_filter"):
        raise _StreamError("The adapter returned an invalid finish reason.")
    result["finish_reason"] = finish
    if item.get("error"):
        raise _StreamError("The adapter reported a failed response.")
    return result


@dataclass
class _AccountUsage:
    lease: _AccountLease
    messages: list
    provider: str
    family: str
    tokens_in: int = 0
    tokens_out: int = 0
    chars_out: int = 0
    bytes_out: int = 0
    records: int = 0
    prompt_known: bool = False
    completion_known: bool = False
    produced: bool = False
    interrupted: bool = False
    settled: bool = False

    def observe(self, item):
        self.records += 1
        self.tokens_in += item.get("tokens_in", 0)
        self.tokens_out += item.get("tokens_out", 0)
        self.prompt_known = self.prompt_known or "tokens_in" in item
        self.completion_known = self.completion_known or "tokens_out" in item
        if self.tokens_in > store.TOKENS_MAX or self.tokens_out > store.TOKENS_MAX:
            raise _StreamError("The adapter exceeded its token accounting bound.")
        text = item["text"] + item["thinking"]
        self.bytes_out += len(text.encode("utf-8"))
        self.chars_out += len(text)
        if self.bytes_out > MAX_STREAM_BYTES or self.records > MAX_STREAM_RECORDS:
            raise _StreamError("The response exceeded the output limit.")
        self.produced = self.produced or bool(text)

    def settle(self):
        from bananachat.db.credits import estimate_tokens

        if self.settled:
            return
        if self.produced or self.prompt_known or self.completion_known or self.interrupted:
            prompt = self.tokens_in if self.prompt_known else sum(estimate_tokens(str(message.get("content", "")))
                                                                  for message in self.messages)
            completion = self.tokens_out if self.completion_known else math.ceil(self.chars_out / 4)
            try:
                record_usage(self.lease.account["id"], prompt, completion, provider=self.provider,
                             family=self.family, lease_id=self.lease.lease_id)
            except Exception as error:
                raise _StreamError("Usage could not be recorded. Try again later.") from error
        self.settled = True


def chat_stream(provider: str, model_name: str, messages: list[dict], *, options: dict | None = None,
                account_id: int | None = None, cancel=None, user_id: str | None = None):
    """Stream through one exclusively leased account at a time.

    Only adapter-discovered models and fresh, positive configured/provider
    capacity may run. Another account is tried only before any text or
    reasoning is emitted. Missing terminal markers and bookkeeping failures
    are errors; transports close and leases release on every exit path.
    """
    from bananachat.services.upstream import Cancelled, CancelToken

    _check_provider(provider)
    cancel = cancel if cancel is not None else CancelToken()
    cancel.check()
    slot = _slot(provider)
    if slot["chat"] is None:
        raise RuntimeError(f"{LABELS[provider]} is not connected. Configure an authorized adapter before using it.")
    discovery = discovered_models(provider)
    descriptor = next((item for item in discovery["models"] if item["name"] == model_name), None)
    if not discovery["ok"] or descriptor is None:
        raise RuntimeError("The configured adapter has not reported this model as available.")
    options = dict(options or {})
    level = options.get("effort")
    if level == "xhigh":
        level = options["effort"] = "extra"
    if level is not None and level not in descriptor["reasoning"]:
        if level == "off" and not descriptor["reasoning"]:
            options.pop("effort")
        else:
            raise RuntimeError("The configured model does not support this reasoning level.")
    quota = pooled_quota(provider)
    if quota["reporter_error"] or (quota["window_left"] is not None and quota["window_left"] <= 0) or \
            (quota["weekly_left"] is not None and quota["weekly_left"] <= 0):
        if quota["reporter_error"] or quota["stale"]:
            raise CapacityUnavailable(f"{LABELS[provider]} usage is unavailable. Try again shortly.")
        raise CapacityUnavailable(f"{LABELS[provider]} has no available quota right now. Try again later.")
    tried = set()
    last_error = f"{LABELS[provider]} is currently at capacity. Try again shortly."
    capacity_unavailable = True
    rested_for_failure = False
    while True:
        lease = _claim_waiting(provider, descriptor, tried, account_id, cancel)
        if lease is None:
            if capacity_unavailable:
                raise CapacityUnavailable(last_error)
            raise RuntimeError(last_error)
        account = lease.account
        tried.add(account["id"])
        usage = _AccountUsage(lease, messages, provider, descriptor["family"])
        stream = None
        attempt = {"started": False}
        outer, _attempts.current = getattr(_attempts, "current", None), attempt

        with lease:
            try:
                cancel.check()
                if slot["accepts_cancel"]:
                    stream = slot["chat"](account, model_name, messages, options, cancel=cancel)
                else:
                    stream = slot["chat"](account, model_name, messages, options)
                for raw in stream:
                    lease.require_active()
                    item = _chunk(raw)
                    usage.observe(item)
                    if item["done"]:
                        # Persist before the terminal record can turn into a completed API/chat answer.
                        usage.settle()
                        yield item
                        return
                    yield item
                raise _StreamError("The model stopped before finishing its answer.")
            except Cancelled:
                # Once the request was sent the prompt has used allowance, answer or not.
                usage.interrupted = stream is not None
                raise
            except RequestRejected as error:
                # The same request fails on every account; resting them all would only take the pool offline.
                lease.require_active()
                if error.spent and attempt["started"]:
                    usage.interrupted = True  # settled below, with the estimated prompt
                    raise RejectedAfterSending(str(error)) from None
                raise RuntimeError(str(error)) from None
            except AdmissionChanged:
                lease.require_active()
                last_error = "The account's availability changed. Try again."
                if usage.produced or usage.settled:
                    raise _StreamError(last_error) from None
            except QuotaExhausted as error:
                lease.require_active()
                window_end = db.parse_timestamp(account["window_resets_at"])
                until = error.resets_at or _reported_reset(account) or (
                    window_end if window_end and window_end > _now() else _now() + timedelta(hours=1))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                lease.rest(max(until, _now() + timedelta(minutes=1)), "Provider quota is exhausted.")
                last_error = f"{LABELS[provider]} has no available quota right now. Try again later."
                if usage.produced:
                    raise RuntimeError(last_error) from None
            except Exception as error:  # noqa: BLE001 - retry only when nothing observable was emitted
                lease.require_active()
                # Preserve a failed/malformed transport as a provider failure,
                # even when all other accounts are subsequently unavailable.
                capacity_unavailable = False
                started = attempt["started"] or not slot["reports_start"]
                # A request that runs out of time would run out of time on every account: one rest at most.
                timed_out = getattr(error, "timed_out", False) is True
                # The provider read the prompt and worked on it for the whole time allowed: charged as an
                # interrupted request, like a tool attempt, whether or not the account is rested below.
                spent = timed_out and attempt["started"] and not usage.produced
                if getattr(error, "requires_quarantine", False) is True:
                    # An adapter that cannot prove process/socket shutdown must never release this account
                    # back into the serving pool.
                    if not store.quarantine(account["id"], lease.lease_id):
                        cancel.cancel("Subscription account lease lost")
                        cancel.check()
                elif not started:
                    pass  # it failed before the provider's process ran: nothing says the account is at fault
                elif timed_out and usage.produced:
                    pass  # the account answered; the answer was longer than the time allowed
                elif rested_for_failure or not _may_rest_for(provider, user_id):
                    # A second account failing the same request points at the request (too long for the
                    # model, refused content), as does a person whose requests already rested one lately:
                    # resting every account would take the pool offline.
                    log.warning("%s account %s also failed (%s): not trying more accounts for this request",
                                LABELS[provider], account["id"], type(error).__name__)
                    if spent:
                        usage.interrupted = True
                        raise RejectedAfterSending("The adapter could not answer.") from None
                    raise RuntimeError("The adapter could not answer.") from None
                else:
                    lease.rest(_now() + COOLDOWN_ERROR, "The adapter request failed.")
                    rested_for_failure = True
                log.warning("%s account %s failed (%s)", LABELS[provider], account["id"], type(error).__name__)
                last_error = str(error) if isinstance(error, _StreamError) else "The adapter could not answer."
                if spent:
                    usage.interrupted = True
                    raise RejectedAfterSending(last_error) from None
                if usage.produced or usage.settled or timed_out:
                    raise RuntimeError(last_error) from None
            finally:
                _attempts.current = outer
                try:
                    if stream is not None and hasattr(stream, "close"):
                        stream.close()
                except Exception as error:  # noqa: BLE001 - cleanup cannot replace the outcome
                    log.warning("Closing the %s account stream failed (%s)", LABELS[provider], type(error).__name__)
                    # Failed shutdown cannot prove the provider request stopped. Require explicit
                    # administrator recovery before routing again.
                    store.quarantine(account["id"], lease.lease_id)
                usage.settle()
        if account_id is not None:
            if capacity_unavailable:
                raise CapacityUnavailable(last_error)
            raise RuntimeError(last_error)


def effort_for(think, effort: str | None = None) -> str | None:
    """Preserve actual resolved capabilities, including extra/xhigh and on/off models."""
    from bananachat.db import limits as limits_db

    if effort == "xhigh":
        return "extra"
    if effort in set(limits_db.EFFORT_LEVELS) | {"on"}:
        return effort
    if think is True:
        return "on"
    if think is False:
        return "off"
    return think if isinstance(think, str) and think in set(limits_db.EFFORT_LEVELS) | {"on"} else None


def stream_chunks(provider: str, model_name: str, messages: list[dict], *, options: dict | None = None,
                  think=None, effort: str | None = None, cancel=None, user_id: str | None = None):
    """Validated adapter records as inference chunks, preserving explicit completion and usage."""
    from bananachat.services.ollama import Chunk
    from bananachat.services.upstream import Cancelled, UpstreamError

    options = dict(options or {})
    level = effort_for(think, effort)
    if think is True and effort is None:
        descriptor = next((item for item in discovered_models(provider)["models"] if item["name"] == model_name), None)
        supported = descriptor["reasoning"] if descriptor else []
        if "on" not in supported:
            level = "medium" if "medium" in supported else "low" if "low" in supported else level
    if level is not None:
        options["effort"] = level
    prompt = completion = None
    stream = chat_stream(provider, model_name, messages, options=options, cancel=cancel, user_id=user_id)
    try:
        for item in stream:
            if "tokens_in" in item:
                prompt = (prompt or 0) + item["tokens_in"]
            if "tokens_out" in item:
                completion = (completion or 0) + item["tokens_out"]
            yield Chunk(content=item["text"], thinking=item["thinking"], done=item["done"],
                        finish_reason=item["finish_reason"] or ("stop" if item["done"] else ""),
                        prompt_tokens=prompt if item["done"] else None,
                        completion_tokens=completion if item["done"] else None)
            if item["done"]:
                return
    except Cancelled:
        raise
    except CapacityUnavailable as error:
        raise UpstreamError(str(error), status=503, code=error.code, kind="capacity") from None
    except RejectedAfterSending as error:
        raise UpstreamError(str(error), sent=True) from None
    except RuntimeError as error:
        raise UpstreamError(str(error)) from None
    finally:
        stream.close()


def reprobe(provider: str | None = None) -> dict:
    """Re-read each connected provider's quota and end expired rest periods."""
    result = {}
    for name in (PROVIDERS if provider is None else (_check_provider(provider),)):
        if _slot(name)["reporter"] is None and _slot(name)["chat"] is None:
            continue
        quota = refresh_quota(name)[name]
        cleared = 0
        for row in store.accounts_resting_expired(provider=name):
            store.clear_cooldown(row["id"])
            cleared += 1
        result[name] = {"window_left": quota["window_left"], "weekly_left": quota["weekly_left"], "cleared": cleared}
    return result


from bananachat.services.background import job  # noqa: E402  (registers the pool re-probe job)


@job("subscription-pool-probe", every=lambda app: app.config["BC"].subscription_probe_interval, initial_delay=12)
def _probe(app) -> None:
    reprobe()
