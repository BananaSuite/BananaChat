"""Validated subscription telemetry from Claude Code's unbilled /usage RPC.

The native CLI owns OAuth authentication, refresh, networking and trust. This
module never opens the credential store. Its separate, private configuration
cache supplies the observation timestamp: the CLI can otherwise return an old
snapshot without identifying it as cached in its experimental RPC response.
Only the allowlisted plan and quota fields below can leave this module.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from bananachat.services import claude_control
from bananachat.services.upstream import Cancelled

CACHE_SECONDS = 60
STALE_CACHE_SECONDS = 300
MAX_AGE_SECONDS = 900
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_MODEL_WINDOWS = 200
_CACHE_LIMIT = 128
_FLIGHT_LIMIT = 128
_WAIT_SECONDS = 40
_cache = OrderedDict()
_attempts = OrderedDict()
_flights = {}
_lock = threading.RLock()
_PLANS = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise"}
_PLAN_LABELS = {"Max 5x", "Max 20x", "Max (5x)", "Max (20x)", "Max 5×", "Max 20×"}


class UsageError(RuntimeError):
    """A quota failure without credentials or provider diagnostics."""


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.report = None
        self.failed = False
        self.cancelled = False
        self.invalidated = False


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise UsageError("Claude returned an invalid subscription reset time.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        result = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise UsageError("Claude returned an invalid subscription reset time.") from None
    if not math.isfinite(result) or result < 0:
        raise UsageError("Claude returned an invalid subscription reset time.")
    return result


def _window(value):
    if value is None:
        return {"left": None, "resets_at": None}
    if not isinstance(value, dict):
        raise UsageError("Claude returned an invalid subscription quota window.")
    used = value.get("utilization")
    if used is not None and (not _finite(used) or not 0 <= used <= 100):
        raise UsageError("Claude returned an invalid subscription usage percentage.")
    return {"left": None if used is None else max(0.0, min(1.0, (100 - used) / 100)),
            "resets_at": _timestamp(value.get("resets_at"))}


def _label(value):
    # A model bucket is a provider label, never an arbitrary error/body string.
    if not isinstance(value, str) or not value.strip() or len(value) > 100 \
            or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise UsageError("Claude returned an invalid model quota label.")
    return value.strip()


def parse(value, *, observed_at, now=None, plan_label=None, allow_stale=False):
    """Normalize the experimental native response into a safe, stable report.

    A missing quota window remains unknown. Extra spend capacity is never used
    to raise subscription allowance or converted into an invented token budget.
    Explicit native display mode preserves a validated stale snapshot for the
    administrator, but marks it unavailable: it never authorizes model usage.
    """
    now = time.time() if now is None else now
    if not isinstance(allow_stale, bool):
        raise UsageError("Choose whether to display stale Claude subscription observations.")
    if not isinstance(value, dict) or not _finite(observed_at) or not _finite(now) \
            or observed_at < 0 or now - observed_at < 0 or (not allow_stale and now - observed_at > MAX_AGE_SECONDS):
        raise UsageError("The Claude subscription usage observation is stale.")
    subscription = value.get("subscription_type")
    if subscription is not None and (not isinstance(subscription, str) or subscription not in _PLANS):
        raise UsageError("Claude returned an unsupported subscription plan.")
    if not isinstance(value.get("rate_limits_available"), bool):
        raise UsageError("Claude returned invalid subscription usage availability.")
    report = {"source": "claude_code", "observed_at": float(observed_at),
              "subscription_type": subscription, "plan_label": _PLANS.get(subscription),
              "available": False, "window_left": None, "window_resets_at": None,
              "weekly_left": None, "weekly_resets_at": None, "model_limits": {}, "model_scoped": []}
    if subscription == "max" and isinstance(plan_label, str) and plan_label in _PLAN_LABELS:
        report["plan_label"] = plan_label
    raw = value.get("rate_limits")
    if not value["rate_limits_available"] or raw is None:
        return report
    if subscription is None or not isinstance(raw, dict):
        raise UsageError("Claude returned invalid subscription quota data.")
    window, weekly = _window(raw.get("five_hour")), _window(raw.get("seven_day"))
    report.update(window_left=window["left"], window_resets_at=window["resets_at"],
                  weekly_left=weekly["left"], weekly_resets_at=weekly["resets_at"])
    for family in ("opus", "sonnet", "oauth_apps"):
        item = raw.get("seven_day_" + family)
        if item is not None:
            parsed = _window(item)
            if parsed["left"] is not None:
                report["model_limits"][family] = parsed
    scoped = raw.get("model_scoped", [])
    if not isinstance(scoped, list) or len(scoped) > MAX_MODEL_WINDOWS:
        raise UsageError("Claude returned an invalid model quota list.")
    for item in scoped:
        if not isinstance(item, dict):
            raise UsageError("Claude returned an invalid model quota window.")
        label = _label(item.get("display_name"))
        parsed = _window(item)
        report["model_scoped"].append({"display_name": label, **parsed})
        known = re.fullmatch(r"(?:claude[- ]+)?(opus|sonnet|haiku|fable|mythos)(?:[- ]+[0-9][a-z0-9 .-]*)?",
                             label, flags=re.IGNORECASE)
        if known and parsed["left"] is not None:
            family = known.group(1).lower()
            if family == "mythos":
                family = "fable"
            previous = report["model_limits"].get(family)
            if previous is not None:
                parsed["left"] = min(previous["left"], parsed["left"])
                resets = [v for v in (previous["resets_at"], parsed["resets_at"]) if v is not None]
                parsed["resets_at"] = min(resets) if resets else None
            report["model_limits"][family] = parsed
    # The subscription's OAuth-app allowance is conservative when present.
    oauth = report["model_limits"].get("oauth_apps", {}).get("left")
    if oauth is not None:
        report["weekly_left"] = min(weekly["left"], oauth) if weekly["left"] is not None else oauth
        resets = [v for v in (report["weekly_resets_at"], report["model_limits"]["oauth_apps"]["resets_at"])
                  if v is not None]
        report["weekly_resets_at"] = min(resets) if resets else None
    report["available"] = window["left"] is not None
    expired = now - observed_at > MAX_AGE_SECONDS or any(
        report[key] is not None and report[key] <= now for key in ("window_resets_at", "weekly_resets_at"))
    if expired:
        if allow_stale:
            report.update(available=False, status="stale")
        elif report["available"]:
            raise UsageError("The Claude subscription usage observation needs refreshing after its reset.")
    return report


def fresh(report, *, now=None):
    """An observation cannot gain a new timestamp when a cached RPC is reread."""
    now = time.time() if now is None else now
    if not isinstance(report, dict) or report.get("available") is not True:
        return False
    stamp = report.get("observed_at")
    if not _finite(stamp) or not _finite(now) or not 0 <= now - stamp <= MAX_AGE_SECONDS:
        return False
    windows = [(report.get("window_left"), report.get("window_resets_at")),
               (report.get("weekly_left"), report.get("weekly_resets_at"))]
    for left, reset in windows:
        if left is not None and (not _finite(left) or not 0 <= left <= 1):
            return False
        if reset is not None and (not _finite(reset) or reset <= now):
            return False
    return report.get("window_left") is not None


def for_model(report, model_name, *, family=None, now=None):
    """Return subscription fractions, including any matching model weekly cap."""
    if not fresh(report, now=now):
        return {"window_left": 0.0, "weekly_left": 0.0}
    week = report["weekly_left"]
    tokens = set(re.findall(r"[a-z]+|[0-9]+", str(model_name).lower()))
    if isinstance(family, str):
        tokens.add(family.lower())
    if "mythos" in tokens:
        tokens.add("fable")
    selected = [item for key, item in report["model_limits"].items() if key in tokens]
    selected.extend(item for item in report["model_scoped"]
                    if set(re.findall(r"[a-z]+|[0-9]+", item["display_name"].lower())) <= tokens)
    for item in selected:
        reset = item.get("resets_at")
        if reset is not None and reset <= (time.time() if now is None else now):
            return {"window_left": report["window_left"], "weekly_left": 0.0}
        if item["left"] is not None:
            week = min(week, item["left"]) if week is not None else item["left"]
    return {"window_left": report["window_left"], "weekly_left": week}


def _metadata(profile):
    """Select freshness/plan metadata from the CLI configuration, not its auth store."""
    path = Path(profile["config_dir"]) / ".claude.json"
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                or info.st_size > MAX_CONFIG_BYTES:
            raise UsageError("Claude usage freshness needs a private service-owned configuration cache.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            data = source.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise UsageError("The Claude usage configuration cache is too large.")
        value = json.loads(data)
        cached = value.get("cachedUsageUtilization") if isinstance(value, dict) else None
        stamp = cached.get("fetchedAtMs") if isinstance(cached, dict) else None
        if not _finite(stamp) or stamp < 0:
            raise UsageError("Claude subscription usage freshness could not be verified.")
        account = value.get("oauthAccount")
        plan = account.get("planDisplayName") if isinstance(account, dict) else None
        fetched = account.get("profileFetchedAt") if isinstance(account, dict) else None
        changed = profile.get("identity_changed_at")
        verified_plan = plan if isinstance(plan, str) and plan in _PLAN_LABELS and _finite(fetched) \
            and 0 <= time.time() - fetched / 1000 <= 86400 \
            and (changed is None or (_finite(changed) and fetched / 1000 >= changed)) else None
        return stamp / 1000, verified_plan
    except (OSError, ValueError, UnicodeError):
        raise UsageError("Claude subscription usage freshness could not be verified.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_fresh(adapter, profile, cancel):
    value = claude_control.request(adapter, profile, "get_usage", cancel=cancel)
    if cancel is not None:
        cancel.check()
    if value.get("rate_limits_available") is False or value.get("rate_limits") is None:
        # Preserve only verified plan information, with unknown quota capacity.
        return parse(value, observed_at=time.time())
    stamp, plan = _metadata(profile)
    changed = profile.get("identity_changed_at")
    if changed is not None and stamp < changed:
        raise UsageError("Refresh Claude usage after changing this profile's subscription login.")
    return parse(value, observed_at=stamp, plan_label=plan, allow_stale=True)


def read(adapter, profile, *, force=False, cancel=None):
    """Read one profile, sharing concurrent refreshes without serializing accounts.

    Validated stale snapshots remain visible with their original timestamp and
    unavailable status. They back off regular polling for five minutes; manual
    refreshes never repeat a native request sooner than one minute. A failed
    refresh removes the previous result. Callers must still check ``fresh``.
    Waiters can cancel independently and never receive stale cached capacity
    after the shared refresh failed. Native I/O and waiter time are bounded.
    """
    key = (adapter.binary, profile["home"], profile["config_dir"])
    while True:
        if cancel is not None:
            cancel.check()
        with _lock:
            cached = _cache.get(key)
            now = time.time()
            changed = profile.get("identity_changed_at")
            if cached and (changed is None or cached[1]["observed_at"] >= changed):
                report = copy.deepcopy(cached[1])
                if report["available"] and not fresh(report, now=now):
                    report.update(available=False, status="stale")
                stale = report.get("status") == "stale" and report["available"] is False
                duration = STALE_CACHE_SECONDS if stale and not force else CACHE_SECONDS
                if 0 <= now - cached[0] < duration:
                    _cache.move_to_end(key)
                    return report
            flight = _flights.get(key)
            owner = flight is None
            if owner:
                _cache.pop(key, None)
                attempted = _attempts.get(key)
                if changed is not None and attempted is not None and attempted < changed:
                    _attempts.pop(key, None)
                    attempted = None
                if attempted is not None and 0 <= now - attempted < CACHE_SECONDS:
                    raise UsageError("Claude subscription usage was checked recently. Retry after one minute.")
                if len(_flights) >= _FLIGHT_LIMIT:
                    raise UsageError("Too many Claude subscription usage refreshes are already running.")
                flight = _Flight()
                _flights[key] = flight
                _attempts[key] = now
                _attempts.move_to_end(key)
                while len(_attempts) > _CACHE_LIMIT:
                    _attempts.popitem(last=False)
        if owner:
            break
        deadline = time.monotonic() + _WAIT_SECONDS
        while not flight.done.wait(0.1):
            if cancel is not None:
                cancel.check()
            if time.monotonic() >= deadline:
                raise UsageError("Waiting for Claude subscription usage exceeded the timeout.")
        if cancel is not None:
            cancel.check()
        if flight.cancelled:
            # Another caller's cancellation must not cancel an independent read.
            continue
        if flight.failed or flight.invalidated or flight.report is None:
            raise UsageError("Claude subscription usage refresh failed. Try refreshing this account again.")
        report = copy.deepcopy(flight.report)
        if report["available"] and not fresh(report):
            report.update(available=False, status="stale")
        changed = profile.get("identity_changed_at")
        if changed is not None and report["observed_at"] < changed:
            raise UsageError("Refresh Claude usage after changing this profile's subscription login.")
        return report
    try:
        report = _read_fresh(adapter, profile, cancel)
        with _lock:
            if flight.invalidated:
                raise UsageError("Refresh Claude usage after changing this profile's subscription login.")
            _cache[key] = (time.time(), copy.deepcopy(report))
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
            flight.report = copy.deepcopy(report)
        return report
    except Cancelled:
        flight.cancelled = True
        with _lock:
            _attempts.pop(key, None)
        raise
    except BaseException:
        # Store no exception object/traceback: provider records remain private.
        flight.failed = True
        raise
    finally:
        with _lock:
            _flights.pop(key, None)
            flight.done.set()


def invalidate(adapter, profile):
    """A profile login changed: never reuse its previous account's observation."""
    key = (adapter.binary, profile["home"], profile["config_dir"])
    with _lock:
        _cache.pop(key, None)
        _attempts.pop(key, None)
        flight = _flights.get(key)
        if flight is not None:
            flight.invalidated = True
