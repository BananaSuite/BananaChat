"""Validated subscription allowance from the Codex app server's rate-limit read.

The native CLI owns ChatGPT authentication, refresh and networking; this module
never opens auth.json. The app server answers in camelCase, so a window resets
at ``resetsAt`` (``resets_at`` belongs to core turn events, ``reset_at`` to
neither). Windows are told apart by their length, never by their position.
Only the allowlisted plan and quota fields below can leave this module.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from collections import OrderedDict

from bananachat.services import codex_control, subscription_pool
from bananachat.services.upstream import Cancelled

CACHE_SECONDS = 60
STALE_CACHE_SECONDS = 300
MAX_AGE_SECONDS = 900
MAX_LIMITS = 50
FIVE_HOURS = 5 * 60
_CACHE_LIMIT = 128
_FLIGHT_LIMIT = 128
_WAIT_SECONDS = 40
_cache = OrderedDict()
_attempts = OrderedDict()
_flights = {}
_lock = threading.RLock()
# Codex's plan identifiers, folded into the plans BananaChat names.
_PLANS = {"free": "free", "go": "go", "plus": "plus", "pro": "pro", "prolite": "pro", "promax": "pro",
          "team": "team", "self_serve_business_prolite": "business", "self_serve_business_usage_based": "business",
          "business": "business", "ent26": "enterprise", "enterprise_cbp_automation": "enterprise",
          "enterprise_cbp_usage_based": "enterprise", "enterprise": "enterprise", "hc": "enterprise",
          "edu": "edu", "education": "edu", "edu_plus": "edu", "edu_pro": "edu"}
# Codex itself calls "pro" Pro (More) and "promax" Pro (Max); "prolite" is plain Pro.
_PLAN_LABELS = {"pro": "ChatGPT Pro (More)", "promax": "ChatGPT Pro (Max)"}


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


def _reset(value):
    if value is None:
        return None
    if not _finite(value) or not 0 <= value <= 253402300799:
        raise UsageError("Codex returned an invalid subscription reset time.")
    return float(value)


def _window(value):
    """``(minutes, left, resets_at)`` of one window; ``usedPercent`` runs from 0 to 100."""
    if not isinstance(value, dict):
        raise UsageError("Codex returned an invalid subscription quota window.")
    used = value.get("usedPercent")
    if not _finite(used) or not 0 <= used <= 100:
        raise UsageError("Codex returned an invalid subscription usage percentage.")
    minutes = value.get("windowDurationMins")
    if minutes is not None and (not _finite(minutes) or minutes <= 0):
        raise UsageError("Codex returned an invalid subscription quota window.")
    return minutes, max(0.0, min(1.0, (100 - used) / 100)), _reset(value.get("resetsAt"))


def _tighter(current, left, reset):
    return (left, reset) if current is None or left < current[0] else current


def _snapshot(value):
    """``(window, weekly)`` as ``(left, resets_at)`` pairs, or ``None`` when not reported.

    Only a window of about five hours, within the five per cent Codex allows
    itself when naming one, is the five-hour allowance. A weekly, daily or
    unlabelled window and a workspace spend limit restrict the weekly figure:
    an unrecognised allowance can lower capacity but never establish it.
    """
    if not isinstance(value, dict):
        raise UsageError("Codex returned an invalid subscription usage snapshot.")
    window = weekly = None
    for key in ("primary", "secondary"):
        if value.get(key) is None:
            continue
        minutes, left, reset = _window(value[key])
        if minutes is not None and FIVE_HOURS * 0.95 <= minutes <= FIVE_HOURS * 1.05:
            window = _tighter(window, left, reset)
        else:
            weekly = _tighter(weekly, left, reset)
    spend = value.get("individualLimit")
    if spend is not None:
        remaining = spend.get("remainingPercent") if isinstance(spend, dict) else None
        if not _finite(remaining) or not 0 <= remaining <= 100:
            raise UsageError("Codex returned an invalid workspace spend limit.")
        weekly = _tighter(weekly, remaining / 100, _reset(spend.get("resetsAt")))
    return window, weekly


def _family(limit_id, value):
    for name in (value.get("normalModelSlug"), value.get("limitName"), limit_id):
        if isinstance(name, str) and 0 < len(name) <= 200:
            try:
                return subscription_pool.family_of("codex", name)
            except ValueError:
                continue
    return None


def parse(value, *, observed_at, now=None):
    """Normalize the app server's reply into a safe, stable report.

    A missing five-hour window remains unknown, which never routes. Purchased
    credits are not subscription allowance and never raise capacity. A spend
    cap, a reached limit or a refusal of ordinary usage makes the report
    unavailable; an elapsed reset marks it stale until the next refresh.
    """
    now = time.time() if now is None else now
    if not isinstance(value, dict) or not _finite(observed_at) or not _finite(now) \
            or observed_at < 0 or not 0 <= now - observed_at <= MAX_AGE_SECONDS:
        raise UsageError("The Codex subscription usage observation is stale.")
    main = value.get("rateLimits")
    if not isinstance(main, dict):
        raise UsageError("Codex returned no subscription usage snapshot.")
    raw = main.get("planType")
    raw = raw if isinstance(raw, str) else None
    report = {"source": "codex", "observed_at": float(observed_at), "subscription_type": _PLANS.get(raw),
              "plan_label": _PLAN_LABELS.get(raw), "available": False, "window_left": None,
              "window_resets_at": None, "weekly_left": None, "weekly_resets_at": None, "model_limits": {}}
    window, weekly = _snapshot(main)
    if window is not None:
        report.update(window_left=window[0], window_resets_at=window[1])
    if weekly is not None:
        report.update(weekly_left=weekly[0], weekly_resets_at=weekly[1])
    scoped = value.get("rateLimitsByLimitId")
    if scoped is not None and (not isinstance(scoped, dict) or len(scoped) > MAX_LIMITS):
        raise UsageError("Codex returned an invalid model quota list.")
    for limit_id, item in (scoped or {}).items():
        if limit_id == "codex":
            continue  # The account-wide allowance, already read above.
        if not isinstance(item, dict):
            raise UsageError("Codex returned an invalid model quota.")
        family = _family(limit_id, item)
        observed = [pair for pair in _snapshot(item) if pair is not None]
        if family is None or not observed:
            continue  # An allowance no model family names cannot be attributed.
        tightest = min(observed, key=lambda pair: pair[0])
        previous = report["model_limits"].get(family)
        if previous is None or tightest[0] < previous["left"]:
            report["model_limits"][family] = {"left": tightest[0], "resets_at": tightest[1]}
    blocked = main.get("spendControlReached") is True or main.get("rateLimitReachedType") is not None \
        or value.get("ordinaryUsageAllowed") is False
    report["available"] = window is not None and not blocked
    if report["available"] and any(report[key] is not None and report[key] <= now
                                   for key in ("window_resets_at", "weekly_resets_at")):
        report.update(available=False, status="stale")
    return report


def fresh(report, *, now=None):
    """An observation cannot gain a new timestamp when a cached reply is reread."""
    now = time.time() if now is None else now
    if not isinstance(report, dict) or report.get("available") is not True:
        return False
    stamp = report.get("observed_at")
    if not _finite(stamp) or not _finite(now) or not 0 <= now - stamp <= MAX_AGE_SECONDS:
        return False
    for left, reset in ((report.get("window_left"), report.get("window_resets_at")),
                        (report.get("weekly_left"), report.get("weekly_resets_at"))):
        if left is not None and (not _finite(left) or not 0 <= left <= 1):
            return False
        if reset is not None and (not _finite(reset) or reset <= now):
            return False
    return report.get("window_left") is not None


def _read_fresh(adapter, profile, cancel):
    started = time.time()
    value = codex_control.rate_limits(adapter, profile, cancel=cancel)
    if cancel is not None:
        cancel.check()
    changed = profile.get("identity_changed_at")
    if changed is not None and started < changed:
        raise UsageError("Refresh Codex usage after changing this profile's subscription login.")
    return parse(value, observed_at=started)


def read(adapter, profile, *, force=False, cancel=None):
    """Read one profile, sharing concurrent refreshes without serializing accounts.

    Stale snapshots remain visible with their original timestamp and
    unavailable status. They back off regular polling for five minutes; manual
    refreshes never repeat a native request sooner than one minute. A failed
    refresh removes the previous result. Callers must still check ``fresh``.
    Waiters can cancel independently and never receive stale cached capacity
    after the shared refresh failed. Native I/O and waiter time are bounded.
    """
    key = (adapter.binary, profile["home"])
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
                    raise UsageError("Codex subscription usage was checked recently. Retry after one minute.")
                if len(_flights) >= _FLIGHT_LIMIT:
                    raise UsageError("Too many Codex subscription usage refreshes are already running.")
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
                raise UsageError("Waiting for Codex subscription usage exceeded the timeout.")
        if cancel is not None:
            cancel.check()
        if flight.cancelled:
            # Another caller's cancellation must not cancel an independent read.
            continue
        if flight.failed or flight.invalidated or flight.report is None:
            raise UsageError("Codex subscription usage refresh failed. Try refreshing this account again.")
        report = copy.deepcopy(flight.report)
        if report["available"] and not fresh(report):
            report.update(available=False, status="stale")
        changed = profile.get("identity_changed_at")
        if changed is not None and report["observed_at"] < changed:
            raise UsageError("Refresh Codex usage after changing this profile's subscription login.")
        return report
    try:
        report = _read_fresh(adapter, profile, cancel)
        with _lock:
            if flight.invalidated:
                raise UsageError("Refresh Codex usage after changing this profile's subscription login.")
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
    key = (adapter.binary, profile["home"])
    with _lock:
        _cache.pop(key, None)
        _attempts.pop(key, None)
        flight = _flights.get(key)
        if flight is not None:
            flight.invalidated = True
