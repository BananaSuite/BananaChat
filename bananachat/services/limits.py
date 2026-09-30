"""The limit system: effective limits, request rates, per-model limits, 5-hour windows, dynamic adjustment,
tiers and reasoning-effort tiers.

Every amount is in **tokens** (prompt + completion). Each pool (``api``,
``chat``, ``agent``) has a policy (:mod:`bananachat.db.limits`) with three
limits:

* **rate** - one or more rules, ``N requests per second | minute | hour |
  day`` with a burst; each is a token bucket per account (every API key, the
  playground, chat and agents of one account share their pool's buckets) and
  all must pass. Never raised automatically; ``dynamic`` may only lower it
  while demand is high.
* **window** - tokens per **5-hour window**, then slow tokens (slow requests
  queue behind others). A window opens at the first counted request when none
  is open for that account and pool, lasts 5 hours, and resets when it ends.
* **weekly** - tokens per rolling week (7 days from the first counted request
  after the previous weekly window ended); off by default. When used up,
  requests wait for the week to end (there is no slow lane).

Every model has a policy too: a **weight** (usage counts ``tokens × weight``
against pool limits), whether it **counts toward the pool** limits (on by
default for local models, off by default for other providers), and optional
model-specific limits (rate rules, 5-hour and weekly tokens, counted in raw
tokens across every service) with the same windows. A request passes the
pool limits (unless the model does not count toward them) **and** the model's
limits; :func:`admit` checks both.

:func:`effective` (pool) and :func:`model_limits` compute what applies to one
account, in this order:

1. base - the account's custom limit, else the policy, times the tier
   multiplier when the limit's ``auto_tiers`` option is on;
2. dynamic multiplier (``dynamic`` on, account not exempt) - site demand
   (queue use and the number of people using the site) times the account's
   usage pattern, clamped to 0.5-2.0 in 5 % steps; model limits scale the
   deviation by the model's ``sensitivity`` (heavy models react more); custom
   limits are a floor and are never reduced;
3. model limits only: the provider's remaining capacity (:func:`register_capacity_provider`);
4. pool limits only: the music-program bonus;
5. grants (for the pool, or for the model) - any ``unlimited`` grant wins,
   then multipliers (multiplied together), then extras (added);
6. administrators are never limited.

Reasoning effort (``off < low < medium < high < max``): a model's supported
levels come from the catalog (:func:`supported_efforts`); each account may use
levels up to its ceiling (:func:`effort_ceiling`): its own level for the model
or for all models, else the model's default, else the site default (medium).
Levels above are unlocked by an administrator, an approved request, or
automatically after sustained use (:func:`auto_unlock_effort`).

Background jobs (run by one process): ``limits-demand`` samples the
inference queue and the number of active people every minute, ``limits-peak-hours``
finds the site's busiest hours, ``limits-tiers`` promotes accounts (up only)
when any policy has ``auto_tiers`` on and unlocks reasoning effort, and
``limits-buckets`` forgets idle request buckets.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from bananachat import db
from bananachat.db import credits
from bananachat.db import limits as store
from bananachat.db import settings as site_settings
from bananachat.db import users
from bananachat.i18n import translate
from bananachat.services import background

DYNAMIC_MIN, DYNAMIC_MAX, DYNAMIC_STEP = 0.5, 2.0, 0.05
DEMAND_KEY = "limits.demand"
PEAK_KEY = "limits.peak_hours"
DEMAND_TIME_CONSTANT = 15 * 60      # seconds; the average forgets older samples over ~15 minutes
DEMAND_MAX_AGE = 15 * 60            # older samples mean "unknown demand" (neutral factor)
DEMAND_RESTART_GAP = 5 * 60         # a longer gap between samples restarts the average
ACTIVE_PEOPLE_MINUTES = 15          # people with a request in this many minutes are "active"
PEAK_HOURS = 6                      # the busiest hours of the day count as peak hours
PEAK_MIN_SITE_TOKENS = 50_000       # below this the site histogram says nothing
PERSONAL_TTL = 600                  # seconds a per-account factor is cached in each process
PERSONAL_MIN_TOKENS = 5_000         # accounts with less usage get a neutral factor
PERSONAL_CACHE_MAX = 10_000
HISTORY_DAYS = 30
BUCKET_IDLE_SECONDS = 86_400
UNLIMITED = credits.UNLIMITED
WINDOW = timedelta(seconds=store.WINDOW_SECONDS)
WEEK = timedelta(seconds=store.WEEK_SECONDS)
LOCAL_PROVIDERS = (None, "", "ollama", "comfyui", "local")
EFFORT_DEFAULT = "medium"


def _get(row, key, default=None):
    """A column of a catalog row or dict; absent columns (added by later versions) read as *default*."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


# ----- results ---------------------------------------------------------------------

@dataclass(frozen=True)
class Reason:
    """Why a number is what it is; ``text()`` renders it (``account.reason_<code>``)."""
    code: str
    params: tuple = ()

    def text(self, lang: str = "en") -> str:
        params = dict(self.params)
        if "tokens" in params:
            from bananachat.formatting import tokens_text
            params["tokens"] = tokens_text(float(params["tokens"]), lang)
        return translate(lang, f"account.reason_{self.code}", **params)


def _reason(code: str, **params) -> Reason:
    return Reason(code, tuple(sorted(params.items())))


def _percent(factor: float) -> str:
    return str(round(abs(factor - 1) * 100))


def _until(value) -> str:
    return (value[:16] + " UTC") if value else ""


@dataclass(frozen=True)
class RateRule:
    requests: float
    per: str
    burst: int
    # The rule as configured ("60/minute/10"): its bucket's name, kept when dynamic limits or grants scale it,
    # so a new or raised rule starts with a full bucket.
    base: str = ""

    @property
    def seconds(self) -> int:
        return store.UNITS[self.per]

    @property
    def per_second(self) -> float:
        return self.requests / self.seconds

    def as_dict(self) -> dict:
        return {"requests": self.requests, "per": self.per, "burst": self.burst}


def _rules(items) -> tuple[RateRule, ...]:
    return tuple(RateRule(float(item["requests"]), item["per"], int(item["burst"]),
                          f"{item['requests']}/{item['per']}/{item['burst']}") for item in items or ())


@dataclass(frozen=True)
class RateLimit:
    limited: bool
    rules: tuple = ()
    custom: bool = False
    reasons: tuple = ()


@dataclass(frozen=True)
class Window:
    """A 5-hour or weekly limit with its usage. ``used`` is regular tokens (5-hour) or all tokens (weekly).

    ``open`` tells whether the window is running; a closed window opens with
    the next counted request (``starts_at``/``resets_at`` are then None).
    """
    period: str
    limited: bool
    tokens: float
    slow_tokens: float
    used: float
    slow_used: float
    open: bool = False
    starts_at: datetime | None = None
    resets_at: datetime | None = None
    base: float = 0.0
    custom: bool = False
    reasons: tuple = ()

    @property
    def left(self) -> float:
        return UNLIMITED if not self.limited else max(0.0, self.tokens - self.used)

    @property
    def slow_left(self) -> float:
        return 0.0 if not self.limited else max(0.0, self.slow_tokens - self.slow_used)

    @property
    def exhausted(self) -> bool:
        return self.limited and self.left <= 0 and self.slow_left <= 0


@dataclass(frozen=True)
class Dynamic:
    multiplier: float
    demand: float
    personal: float
    reasons: tuple = ()
    people: int | None = None


@dataclass(frozen=True)
class Effective:
    pool: str
    admin: bool
    rate: RateLimit
    window: Window
    weekly: Window
    speed: str = "normal"
    tier: dict | None = None
    tiers_apply: bool = False
    dynamic: Dynamic | None = None
    dynamic_applies: bool = False
    grants: tuple = ()
    bonus: tuple = ("none", 1.0, 0.0)

    @property
    def unlimited(self) -> bool:
        return self.admin or not (self.window.limited or self.weekly.limited)

    def budget(self) -> credits.Budget:
        window, weekly = self.window, self.weekly
        if self.unlimited:
            return credits.Budget(self.pool, True, UNLIMITED, 0, 0, 0, resets_at=window.resets_at,
                                  weekly_resets_at=weekly.resets_at)
        return credits.Budget(
            self.pool, False, window.tokens if window.limited else UNLIMITED,
            window.slow_tokens if window.limited else 0.0, window.used, window.slow_used,
            weekly_limit=weekly.tokens if weekly.limited else None, weekly_used=weekly.used,
            resets_at=window.resets_at, weekly_resets_at=weekly.resets_at)


@dataclass(frozen=True)
class ModelLimits:
    """What applies to one account and one model."""
    model_id: int
    name: str
    admin: bool
    weight: float
    counts_toward_pool: bool
    locked: bool
    rate: RateLimit
    window: Window
    weekly: Window
    policy: dict = field(default_factory=dict)
    custom: bool = False
    dynamic: float = 1.0
    capacity: float = 1.0

    @property
    def limited(self) -> bool:
        return not self.admin and (self.rate.limited or self.window.limited or self.weekly.limited)

    @property
    def exhausted(self) -> bool:
        return not self.admin and (self.window.exhausted or self.weekly.exhausted)

    @property
    def weekly_exhausted(self) -> bool:
        return not self.admin and self.weekly.exhausted

    @property
    def blocked_until(self) -> datetime | None:
        return self.weekly.resets_at if self.weekly.exhausted else self.window.resets_at


# ----- small helpers --------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> dict:
    try:
        from flask import g
        cached = getattr(g, "settings", None)
    except RuntimeError:
        cached = None
    return cached if cached is not None else site_settings.get()


# ----- reading settings once per request ---------------------------------------------------

_snapshot: ContextVar[dict | None] = ContextVar("bananachat_limits_snapshot", default=None)


@contextmanager
def snapshot(*, fresh: bool = False):
    """Inside the block, each limit setting and usage figure is read once (policies, custom limits, grants,
    account settings, windows, usage, demand); admission, fallbacks and ``auto`` share them.

    Blocks nest. Use it only around code that writes none of them (request buckets are never cached). With
    *fresh* the block reads everything again (after a queue wait, for a charge) and an enclosing snapshot is
    emptied when it ends, since what it read may have changed.
    """
    outer = _snapshot.get()
    if outer is not None and not fresh:
        yield
        return
    token = _snapshot.set({})
    try:
        yield
    finally:
        _snapshot.reset(token)
        if outer is not None:
            outer.clear()


def _once(key, load):
    """``load()``, once per :func:`snapshot` (every time outside one)."""
    cache = _snapshot.get()
    if cache is None:
        return load()
    if key not in cache:
        cache[key] = load()
    return cache[key]


def _policy(pool: str) -> dict:
    return _once("policies", store.all_policies)[pool] if _snapshot.get() is not None else store.get_policy(pool)


def _model_policy(model) -> dict:
    return _once(("model_policy", model["id"]), lambda: store.get_model_policy(model))


def _prefs(user_id: str):
    return _once(("prefs", user_id), lambda: store.user_settings(user_id))


def _override(user_id: str, pool: str):
    if _snapshot.get() is None:
        return store.get_override(user_id, pool) or store.Override(pool)
    return _once(("overrides", user_id), lambda: store.overrides_for(user_id)).get(pool) or store.Override(pool)


def _model_override(user_id: str, model_id: int):
    if _snapshot.get() is None:
        return store.get_model_override(user_id, model_id) or store.ModelOverride(model_id)
    return _once(("model_overrides", user_id), lambda: store.model_overrides_for(user_id)).get(model_id) or \
        store.ModelOverride(model_id)


def _grants(user_id: str, at: str):
    return _once(("grants", user_id), lambda: store.active_grants(user_id, at))


def _renounced(user_id: str, at: str) -> dict:
    """Tokens the account renounced for others, in force now: ``{(pool, model_id, scope): (tokens, until)}``."""
    def load():
        return {(row["pool"], row["model_id"], row["scope"]): (float(row["tokens"] or 0), row["ends_at"])
                for row in store.active_pledges(user_id, at)}
    return _once(("renounced", user_id), load)


def _effort_levels(user_id: str):
    return _once(("effort", user_id), lambda: store.effort_levels(user_id))


def _stored_window(user_id: str, scope: str) -> tuple:
    if _snapshot.get() is None:
        return store.windows(user_id, [scope]).get(scope, (None, None))
    return _once(("windows", user_id), lambda: store.all_windows(user_id)).get(scope, (None, None))


def _latest(*values):
    present = [value for value in values if value]
    return max(present) if present else None


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def tokens_label(value: float, lang: str = "en") -> str:
    """``45.2k tokens`` in *lang*."""
    from bananachat.formatting import tokens_text

    return tokens_text(value, lang)


def _whole(value: float) -> float:
    """Token amounts are whole numbers."""
    return float(math.floor(value + 1e-6))


def tiers() -> list[dict]:
    return _once("tiers", lambda: [row.to_dict() for row in store.list_tiers()])


def counted_types(policies: dict | None = None) -> tuple[str, ...]:
    """Request types whose usage earns tiers and dynamic bonuses: those of services with 5-hour or weekly
    token limits (free, unlimited use would otherwise earn higher limits elsewhere)."""
    policies = policies or store.all_policies()
    return tuple(kind for pool, policy in policies.items()
                 if policy["window"]["enabled"] or policy["weekly"]["enabled"] for kind in credits.POOL_TYPES[pool])


def resolve_tier(tier_id, all_tiers: list[dict] | None = None) -> dict | None:
    """The account's tier; unknown or unset means the entry (first) tier."""
    all_tiers = tiers() if all_tiers is None else all_tiers
    for tier in all_tiers:
        if tier["id"] == tier_id:
            return tier
    return all_tiers[0] if all_tiers else None


def _grant_parts(grants, scope: str):
    relevant = [grant for grant in grants if grant["scope"] in (None, scope)]
    unlimited = [grant for grant in relevant if grant["kind"] == "unlimited"]
    multipliers = [grant for grant in relevant if grant["kind"] == "multiplier"]
    extras = [grant for grant in relevant if grant["kind"] == "extra" and grant["scope"] == scope]
    return unlimited, multipliers, extras


def _grant_reason(grant) -> Reason:
    until = _until(grant["ends_at"])
    if grant["kind"] == "unlimited":
        return _reason("grant_unlimited" if until else "grant_unlimited_forever", until=until)
    if grant["kind"] == "multiplier":
        return _reason("grant_multiplier" if until else "grant_multiplier_forever", factor=f"{grant['amount']:g}",
                       until=until)
    return _reason("grant_extra" if until else "grant_extra_forever", tokens=grant["amount"], until=until)


def _open_since(started_at: str | None, length: timedelta, now: datetime) -> datetime | None:
    """When a stored window started, if it is still open at *now*."""
    start = db.parse_timestamp(started_at)
    if start is None or start + length <= now:
        return None
    return start


# ----- dynamic adjustment -------------------------------------------------------------

def _curve(value: float, points) -> float:
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if value <= x1:
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return points[-1][1]


def demand_factor(utilisation: float | None) -> float:
    """Queue use (queued + running per inference slot, averaged) to a factor.

    Quiet (up to 0.2) gives +25 %, normal load (0.5-0.9) nothing, and an
    overloaded server (2 or more) -30 %, linear in between.
    """
    if utilisation is None:
        return 1.0
    return _curve(utilisation, ((0.2, 1.25), (0.5, 1.0), (0.9, 1.0), (2.0, 0.7)))


def people_factor(people: int | None, slots: int | None) -> float:
    """People actively using the site (requests in the last 15 minutes) per inference slot to a factor.

    Up to one person per slot gives +10 %, 3-6 nothing, 12 or more -20 %,
    linear in between.
    """
    if people is None:
        return 1.0
    return _curve(people / max(1, int(slots or 1)), ((1, 1.1), (3, 1.0), (6, 1.0), (12, 0.8)))


def personal_factor(peak_share: float | None, active_days: int, total_tokens: float) -> float:
    """An account's usage pattern over 30 days to a factor between 0.85 and 1.25.

    Off-peak use earns up to +15 % (mostly peak-hour use costs up to -15 %),
    regular use (active on up to 20 of the last 30 days) up to +10 %.
    Accounts with little history are neutral.
    """
    if total_tokens < PERSONAL_MIN_TOKENS:
        return 1.0
    offpeak = 0.0
    if peak_share is not None:
        baseline = PEAK_HOURS / 24
        offpeak = max(-1.0, min(1.0, (baseline - peak_share) / baseline)) * 0.15
    regular = 0.10 * min(max(active_days, 0), 20) / 20
    return 1.0 + offpeak + regular


def quantise(factor: float) -> float:
    """Clamp to 0.5-2.0 and round to 5 % steps so limits do not jitter."""
    factor = max(DYNAMIC_MIN, min(DYNAMIC_MAX, factor))
    return round(round(factor / DYNAMIC_STEP) * DYNAMIC_STEP, 2)


def scaled(multiplier: float, sensitivity: float) -> float:
    """A dynamic multiplier whose deviation from 1 is scaled by a model's sensitivity (heavy models react more)."""
    return quantise(1 + (multiplier - 1) * sensitivity)


_demand_cache: dict[str, tuple[float, dict]] = {}
DEMAND_CACHE_SECONDS = 2  # the sample changes once a minute; one read serves a request's several checks


def _demand_state() -> dict:
    key = db.path()
    cached = _demand_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    state = site_settings.state_get(DEMAND_KEY, max_age=DEMAND_MAX_AGE)
    state = state if isinstance(state, dict) else {}
    _demand_cache[key] = (time.monotonic() + DEMAND_CACHE_SECONDS, state)
    return state


def current_demand(state: dict | None = None) -> float | None:
    try:
        return float((_demand_state() if state is None else state)["ewma"])
    except (KeyError, TypeError, ValueError):
        return None


def current_people(state: dict | None = None) -> tuple[int | None, int]:
    """``(people active in the last 15 minutes, inference slots)`` from the last sample (None: unknown)."""
    state = _demand_state() if state is None else state
    try:
        people = int(state["people"])
    except (KeyError, TypeError, ValueError):
        people = None
    try:
        slots = max(1, int(state.get("slots") or 1))
    except (TypeError, ValueError):
        slots = 1
    return people, slots


def record_demand(running: int, waiting: int, max_concurrent: int, *, people: int | None = None,
                  at: float | None = None) -> float:
    """Add one queue sample (and the number of active people) to the shared state; returns the new average."""
    at = time.time() if at is None else at
    utilisation = (running + waiting) / max(1, max_concurrent)
    previous = site_settings.state_get(DEMAND_KEY)
    try:
        gap = at - float(previous["at"])
        average = float(previous["ewma"])
    except (KeyError, TypeError, ValueError):
        gap, average = None, None
    if gap is None or not 0 <= gap <= DEMAND_RESTART_GAP:
        average = utilisation
    else:
        alpha = 1 - math.exp(-gap / DEMAND_TIME_CONSTANT)
        average += alpha * (utilisation - average)
    state = {"ewma": round(average, 4), "last": round(utilisation, 4), "at": at, "slots": max(1, max_concurrent)}
    if people is not None:
        state["people"] = int(people)
    site_settings.state_set(DEMAND_KEY, state)
    _demand_cache.pop(db.path(), None)
    return average


def peak_hours() -> list[int]:
    state = site_settings.state_get(PEAK_KEY, max_age=2 * 86_400)
    hours = state.get("hours") if isinstance(state, dict) else None
    return [int(hour) for hour in hours] if isinstance(hours, list) else []


def refresh_peak_hours(now: datetime | None = None) -> list[int]:
    """The site's busiest UTC hours over 30 days (none while the site is barely used)."""
    now = now or _now()
    histogram = store.site_hourly(db.timestamp(now - timedelta(days=HISTORY_DAYS)))
    if sum(histogram.values()) < PEAK_MIN_SITE_TOKENS:
        hours = []
    else:
        ranked = sorted(histogram.items(), key=lambda item: (-item[1], item[0]))
        hours = sorted(hour for hour, amount in ranked[:PEAK_HOURS] if amount > 0)
    site_settings.state_set(PEAK_KEY, {"hours": hours, "at": time.time()})
    return hours


_personal_cache: dict[str, tuple[float, tuple]] = {}
_personal_lock = threading.Lock()


def _personal(user_id: str, now: datetime) -> tuple[float, float | None, int, float]:
    """``(factor, peak_share, active_days, tokens)`` from the account's last 30 days (cached)."""
    key = f"{db.path()}:{user_id}"
    # Inside a write transaction (a charge) the 30-day query would hold the database's write lock: use what this
    # process knows, even when it is older than PERSONAL_TTL (admission computed it moments ago), else neutral.
    locked = db.conn().in_transaction
    with _personal_lock:
        cached = _personal_cache.get(key)
        if cached is not None and (locked or cached[0] > time.monotonic()):
            return cached[1]
    if locked:
        return 1.0, None, 0, 0.0
    rows = store.user_activity(user_id, db.timestamp(now - timedelta(days=HISTORY_DAYS)), counted_types())
    peaks = set(peak_hours())
    total = sum(float(row["tokens"] or 0) for row in rows)
    days = len({row["day"] for row in rows})
    share = None
    if peaks and total > 0:
        share = sum(float(row["tokens"] or 0) for row in rows if row["hour"] in peaks) / total
    value = (personal_factor(share, days, total), share, days, total)
    with _personal_lock:
        if len(_personal_cache) >= PERSONAL_CACHE_MAX:
            _personal_cache.clear()
        _personal_cache[key] = (time.monotonic() + PERSONAL_TTL, value)
    return value


def forget_personal(user_id: str | None = None) -> None:
    with _personal_lock:
        if user_id is None:
            _personal_cache.clear()
        else:
            for key in [key for key in _personal_cache if key.endswith(f":{user_id}")]:
                _personal_cache.pop(key, None)


def dynamic_for(user_id: str, now: datetime | None = None) -> Dynamic:
    now = now or _now()
    state = _demand_state()
    queue_part = demand_factor(current_demand(state))
    people, slots = current_people(state)
    crowd = people_factor(people, slots)
    demand = queue_part * crowd
    personal = _personal(user_id, now)[0]
    multiplier = quantise(demand * personal)
    reasons = []
    if round(queue_part, 2) != 1.0:
        reasons.append(_reason("offpeak" if queue_part > 1 else "high_demand", percent=_percent(queue_part)))
    if round(crowd, 2) != 1.0:
        reasons.append(_reason("few_people" if crowd > 1 else "many_people", percent=_percent(crowd)))
    if round(personal, 2) != 1.0:
        reasons.append(_reason("pattern_bonus" if personal > 1 else "pattern_peak", percent=_percent(personal)))
    return Dynamic(multiplier, round(demand, 3), round(personal, 3), tuple(reasons), people)


# ----- provider capacity (hook) ----------------------------------------------------------

@dataclass(frozen=True)
class Capacity:
    """What a provider reports it has left, as fractions of its own allowance (0-1; None: unknown)."""
    window_left: float | None = None
    weekly_left: float | None = None


CAPACITY_COMFORT = 0.5  # model limits start shrinking once less than half of the provider's allowance is left
_capacity_providers: dict[str, Callable] = {}


def register_capacity_provider(provider: str, report: Callable) -> None:
    """Let a provider (``ai_models.provider``) report its remaining capacity: ``report(model) -> Capacity | None``.

    While a provider reports less than half of its 5-hour or weekly allowance
    left, the limits of its models shrink in proportion (to zero when nothing
    is left), so a shared upstream allowance is spread over everyone. The
    default is no provider: nothing changes. Reports are asked for on each
    admission, so they should be cheap (cached by the provider).
    """
    _capacity_providers[provider] = report


def unregister_capacity_provider(provider: str) -> None:
    _capacity_providers.pop(provider, None)


def capacity_factor(model) -> float:
    """1.0, or less while the model's provider is running out of capacity (5 % steps, 0-1)."""
    report = _capacity_providers.get(_get(model, "provider", ""))
    if report is None:
        return 1.0
    try:
        capacity = report(model)
    except Exception:  # noqa: BLE001 - a broken report never blocks admission
        return 1.0
    if capacity is None:
        return 1.0
    left = [value for value in (capacity.window_left, capacity.weekly_left) if value is not None]
    if not left:
        return 1.0
    fraction = max(0.0, min(1.0, min(left)))
    return round(round(min(1.0, fraction / CAPACITY_COMFORT) / DYNAMIC_STEP) * DYNAMIC_STEP, 2)


# ----- effective limits of a pool ---------------------------------------------------------

def _admin_rate() -> RateLimit:
    return RateLimit(False, (), reasons=(_reason("admin"),))


def _admin_window(period: str) -> Window:
    return Window(period, False, UNLIMITED, 0.0, 0.0, 0.0, reasons=(_reason("admin"),))


def _admin_result(pool: str) -> Effective:
    return Effective(pool, True, _admin_rate(), _admin_window("window"), _admin_window("weekly"))


def _scaled_rules(rules, factor: float, *, grow_burst: bool) -> tuple[RateRule, ...]:
    result = []
    for rule in rules:
        burst = max(rule.burst, math.ceil(rule.burst * factor)) if grow_burst else max(1, math.floor(rule.burst * factor))
        result.append(RateRule(round(rule.requests * factor, 4), rule.per, int(burst), rule.base))
    return tuple(result)


def _rate(enabled: bool, base_rules, custom_rules, *, dynamic: float | None, dynamic_on: bool, grants,
          capacity: float = 1.0) -> RateLimit:
    custom = custom_rules is not None
    limited = (enabled or custom) and bool(custom_rules if custom else base_rules)
    rules = _rules(custom_rules if custom else base_rules)
    reasons = [_reason("custom" if custom else "default")]
    if not limited:
        return RateLimit(False, rules, custom, (_reason("not_limited"),))
    # Never raised automatically, and custom rates are never reduced.
    if dynamic_on and dynamic is not None and not custom and dynamic < 1:
        rules = _scaled_rules(rules, dynamic, grow_burst=False)
        reasons.append(_reason("dynamic_down", percent=_percent(dynamic)))
    if capacity < 1:
        rules = _scaled_rules(rules, max(capacity, 0.0001), grow_burst=False)
        reasons.append(_reason("capacity", percent=_percent(capacity)))
    unlimited, multipliers, _extras = _grant_parts(grants, "rate")
    if unlimited:
        return RateLimit(False, rules, custom, tuple(reasons + [_grant_reason(unlimited[0])]))
    for grant in multipliers:
        rules = _scaled_rules(rules, float(grant["amount"]), grow_burst=True)
        reasons.append(_grant_reason(grant))
    return RateLimit(True, rules, custom, tuple(reasons))


def _window(period: str, policy: dict, custom_tokens, custom_slow, *, tier, dynamic, dynamic_on, bonus,
            slow_enabled, grants, started: datetime | None, used: tuple[float, float], length: timedelta,
            limited_override: bool | None = None, capacity: float = 1.0,
            renounced: tuple[float, str | None] = (0.0, None)) -> Window:
    custom = custom_tokens is not None or custom_slow is not None
    limited = (policy["enabled"] or custom) if limited_override is None else limited_override
    start, end = (started, started + length) if started else (None, None)
    reasons = [_reason("custom" if custom else "default")]
    tier_factor = float(tier["multiplier"]) if policy.get("auto_tiers") and tier is not None else 1.0
    uses_tier = custom_tokens is None or (period == "window" and custom_slow is None)
    if tier_factor != 1.0 and uses_tier:
        reasons.append(_reason("tier", name=tier["name"], factor=f"{tier_factor:g}"))
    regular = float(custom_tokens) if custom_tokens is not None else float(policy.get("tokens") or 0) * tier_factor
    slow = float(custom_slow) if custom_slow is not None else float(policy.get("slow_tokens") or 0) * tier_factor
    base = regular
    if not limited:
        return Window(period, False, UNLIMITED, 0.0, used[0], used[1], started is not None, start, end, base, custom,
                      (_reason("not_limited"),))
    if dynamic_on and dynamic is not None and dynamic != 1.0:
        if custom and dynamic < 1:
            reasons.append(_reason("custom_floor"))
        else:
            regular *= dynamic
            slow *= dynamic
            reasons.append(_reason("dynamic_up" if dynamic > 1 else "dynamic_down", percent=_percent(dynamic)))
    if capacity < 1:
        regular *= capacity
        slow *= capacity
        reasons.append(_reason("capacity", percent=_percent(capacity)))
    mode, bonus_regular, bonus_slow = bonus
    if mode == "multiplier" and bonus_regular != 1.0:
        regular *= bonus_regular
        slow *= bonus_slow
        reasons.append(_reason("music_multiplier", factor=f"{bonus_regular:g}"))
    elif mode == "fixed":
        windows = 7 if period == "weekly" else 1
        regular += bonus_regular * windows
        slow += bonus_slow if period == "window" else 0.0
        reasons.append(_reason("music_fixed", tokens=bonus_regular * windows))
    if not slow_enabled or period == "weekly":
        slow = 0.0
    unlimited, multipliers, extras = _grant_parts(grants, period)
    if unlimited:
        return Window(period, False, UNLIMITED, 0.0, used[0], used[1], started is not None, start, end, base, custom,
                      tuple(reasons + [_grant_reason(unlimited[0])]))
    for grant in multipliers:
        regular *= grant["amount"]
        slow *= grant["amount"]
        reasons.append(_grant_reason(grant))
    for grant in extras:
        regular += grant["amount"]
        reasons.append(_grant_reason(grant))
    if renounced[0] > 0:
        # Tokens the account renounced to support someone else's request (community consent).
        regular = max(0.0, regular - renounced[0])
        reasons.append(_reason("renounced", tokens=renounced[0], until=_until(renounced[1])))
    return Window(period, True, _whole(regular), _whole(slow), used[0], used[1], started is not None, start, end,
                  _whole(base), custom, tuple(reasons))


def _starts(user_id: str, scope: str, prefs, settings, now: datetime) -> tuple[datetime | None, datetime | None,
                                                                              str | None, str | None]:
    """``(window start, week start, 5-hour count since, weekly count since)``; None where a window is closed."""
    stored = _stored_window(user_id, scope)
    window_start = _open_since(stored[0], WINDOW, now)
    week_start = _open_since(stored[1], WEEK, now)
    global_window, global_week = settings.get("limits_usage_reset_at"), settings.get("limits_weekly_reset_at")
    window_since = _latest(db.timestamp(window_start), prefs.usage_reset_at, prefs.weekly_reset_at, global_window,
                           global_week) if window_start else None
    week_since = _latest(db.timestamp(week_start), prefs.weekly_reset_at, global_week) if week_start else None
    return window_start, week_start, window_since, week_since


def effective(user, pool: str, *, usage: bool = True, now: datetime | None = None) -> Effective:
    """Every limit of *user* in *pool*, with usage and the reasons behind each number."""
    if pool not in store.POOLS:
        raise ValueError("Unknown credit pool.")
    now = now or _now()
    if user["role"] == "admin":
        return _admin_result(pool)
    user_id = user["id"]
    policy = _policy(pool)
    prefs = _prefs(user_id)
    override = _override(user_id, pool)
    settings = _settings()
    tiers_apply = policy["window"]["auto_tiers"] or policy["weekly"]["auto_tiers"]
    tier = resolve_tier(prefs.tier_id) if tiers_apply else None
    dynamic_applies = not prefs.dynamic_exempt and any(policy[scope]["dynamic"] for scope in store.SCOPES)
    dynamic = dynamic_for(user_id, now) if dynamic_applies else None
    grants = tuple(grant.to_dict() for grant in _grants(user_id, db.timestamp(now))
                   if grant["model_id"] is None and grant["pool"] in (None, pool))
    bonus = _once(("bonus", user_id), lambda: credits.music_bonus(user_id, settings))
    slow_enabled = bool(settings.get("slow_credits_enabled", 1))

    window_start = week_start = None
    regular_used = slow_used = weekly_used = 0.0
    if usage:
        window_start, week_start, window_since, week_since = _starts(user_id, store.pool_scope(pool), prefs,
                                                                     settings, now)
        regular_used, slow_used, weekly_used = _once(("usage", user_id, pool, window_since, week_since),
                                                     lambda: store.usage(user_id, pool, window_since, week_since))

    multiplier = dynamic.multiplier if dynamic else None
    common = {"tier": tier, "dynamic": multiplier, "bonus": bonus, "slow_enabled": slow_enabled, "grants": grants}
    renounced = _renounced(user_id, db.timestamp(now))
    window = _window("window", policy["window"], _custom(override, "window_tokens", policy["window"], "tokens", tier),
                     _custom(override, "window_slow_tokens", policy["window"], "slow_tokens", tier), **common,
                     dynamic_on=policy["window"]["dynamic"], started=window_start,
                     used=(round(regular_used, 2), round(slow_used, 2)), length=WINDOW,
                     renounced=renounced.get((pool, None, "window"), (0.0, None)))
    weekly = _window("weekly", policy["weekly"], _custom(override, "weekly_tokens", policy["weekly"], "tokens", tier),
                     None, **common,
                     dynamic_on=policy["weekly"]["dynamic"], started=week_start, used=(round(weekly_used, 2), 0.0),
                     length=WEEK, renounced=renounced.get((pool, None, "weekly"), (0.0, None)))
    rate = _rate(policy["rate"]["enabled"], policy["rate"]["rules"], override.rate_rules, dynamic=multiplier,
                 dynamic_on=policy["rate"]["dynamic"], grants=grants)
    return Effective(pool, False, rate, window, weekly, prefs.speed, tier, tiers_apply, dynamic, dynamic_applies,
                     grants, bonus)


def _custom(override, name: str, section: dict, key: str, tier) -> float | None:
    """A custom amount; one set by an automatically approved request is a floor under the policy × tier."""
    value = getattr(override, name)
    if value is None or name not in override.automatic:
        return value
    factor = float(tier["multiplier"]) if section.get("auto_tiers") and tier is not None else 1.0
    return max(float(value), _whole(float(section.get(key) or 0) * factor))


def rate_limit(user, pool: str) -> RateLimit:
    """Only the request rate of a pool (no usage queries), for admission."""
    if user["role"] == "admin":
        return _admin_rate()
    policy = _policy(pool)["rate"]
    override = _override(user["id"], pool)
    dynamic = None
    if policy["enabled"] and policy["dynamic"] and not override.has_rate and not _prefs(user["id"]).dynamic_exempt:
        dynamic = dynamic_for(user["id"]).multiplier
    grants = [grant for grant in _grants(user["id"], db.now())
              if grant["model_id"] is None and grant["pool"] in (None, pool)]
    return _rate(policy["enabled"], policy["rules"], override.rate_rules, dynamic=dynamic,
                 dynamic_on=policy["dynamic"], grants=grants)


# ----- model limits -------------------------------------------------------------------

def is_local(model) -> bool:
    """Whether a model is served by this site's own backends (Ollama, ComfyUI): no ``provider``, or a local one."""
    return _get(model, "provider", "") in LOCAL_PROVIDERS


def counts_toward_pool(model, policy: dict | None = None) -> bool:
    """Whether a model's usage counts against the pool limits: the policy's choice, else yes for local models
    (Ollama, ComfyUI) and no for other providers."""
    policy = policy if policy is not None else _model_policy(model)
    if policy.get("counts_toward_pool") is not None:
        return bool(policy["counts_toward_pool"])
    return is_local(model)


def outside_pool(name: str | None) -> bool:
    """Whether the model called *name* exists and does not count toward the pool limits."""
    if not name or name == "auto":
        return False
    model = db.one("SELECT * FROM ai_models WHERE ollama_name=?", (name,))
    return model is not None and not counts_toward_pool(model)


def any_outside_pool() -> bool:
    """Whether a published model does not count toward the pool limits (``auto`` may then still find one)."""
    models = db.query("SELECT * FROM ai_models WHERE is_rolled_out=1")
    policies = store.model_policies(models)
    return any(not counts_toward_pool(model, policies[model["id"]]) for model in models)


def model_limits(user, model, *, usage: bool = True, now: datetime | None = None,
                 policy: dict | None = None) -> ModelLimits:
    """The model-specific limits of *user* for *model* (a catalog row), with usage and reasons."""
    now = now or _now()
    policy = policy if policy is not None else _model_policy(model)
    counts = counts_toward_pool(model, policy)
    name = _get(model, "display_name") or _get(model, "ollama_name", "")
    if user["role"] == "admin":
        return ModelLimits(model["id"], name, True, policy["weight"], counts, False, _admin_rate(),
                           _admin_window("window"), _admin_window("weekly"), policy)
    user_id = user["id"]
    override = _model_override(user_id, model["id"])
    prefs = _prefs(user_id)
    settings = _settings()
    enabled = policy["enabled"]
    tier = resolve_tier(prefs.tier_id) if policy["auto_tiers"] and enabled else None
    dynamic = None
    if enabled and policy["dynamic"] and not prefs.dynamic_exempt:
        dynamic = scaled(dynamic_for(user_id, now).multiplier, policy["sensitivity"])
    capacity = capacity_factor(model)
    grants = tuple(grant.to_dict() for grant in _grants(user_id, db.timestamp(now))
                   if _for_model(grant, model["id"]))

    window_start = week_start = None
    window_used = weekly_used = 0.0
    if usage:
        window_start, week_start, window_since, week_since = _starts(user_id, store.model_scope(model["id"]), prefs,
                                                                     settings, now)
        window_used, weekly_used = _once(("model_usage", user_id, model["id"], window_since, week_since),
                                         lambda: store.model_usage(user_id, model["id"], window_since, week_since))

    common = {"tier": tier, "dynamic": dynamic, "dynamic_on": True, "bonus": ("none", 1.0, 0.0),
              "slow_enabled": False, "grants": grants, "capacity": capacity}
    window_policy = {"enabled": enabled and policy["window_tokens"] is not None,
                     "tokens": policy["window_tokens"] or 0, "slow_tokens": 0, "auto_tiers": policy["auto_tiers"]}
    weekly_policy = {"enabled": enabled and policy["weekly_tokens"] is not None,
                     "tokens": policy["weekly_tokens"] or 0, "auto_tiers": policy["auto_tiers"]}
    renounced = _renounced(user_id, db.timestamp(now))
    window = _window("window", window_policy, override.window_tokens, None, **common, started=window_start,
                     used=(round(window_used, 2), 0.0), length=WINDOW,
                     renounced=renounced.get((None, model["id"], "window"), (0.0, None)))
    weekly = _window("weekly", weekly_policy, override.weekly_tokens, None, **common, started=week_start,
                     used=(round(weekly_used, 2), 0.0), length=WEEK,
                     renounced=renounced.get((None, model["id"], "weekly"), (0.0, None)))
    rate = _rate(enabled, policy["rate_rules"], override.rate_rules, dynamic=dynamic, dynamic_on=True,
                 grants=grants, capacity=capacity)
    return ModelLimits(model["id"], name, False, policy["weight"], counts, override.locked, rate, window, weekly,
                       policy, not override.empty, dynamic or 1.0, capacity)


def _for_model(grant, model_id) -> bool:
    """Whether a grant changes a model's own limits: a grant for that model, or unlimited use for every service
    (what an administrator granting "Unlimited, every service" expects; multipliers, extras and grants for one
    service leave model limits alone, since those count across services)."""
    return grant["model_id"] == model_id or (grant["model_id"] is None and grant["pool"] is None
                                             and grant["kind"] == "unlimited")


def charge_terms(model_id) -> tuple[float, bool]:
    """``(weight, counts toward the pool)`` of a model for charging (a missing model counts ×1)."""
    if model_id is None:
        return 1.0, True
    model = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))
    if model is None:
        return 1.0, True
    policy = _model_policy(model)
    return float(policy["weight"]), counts_toward_pool(model, policy)


# ----- request rate ----------------------------------------------------------------------

@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limited: bool
    limit: int = 0            # the binding rule's bucket size (burst)
    remaining: int = 0        # whole requests left in that bucket
    reset_seconds: float = 0  # until that bucket is full again
    retry_after: int = 0      # whole seconds to wait when refused
    rule: RateRule | None = None
    rules: tuple = ()

    @property
    def per_second(self) -> float:
        return self.rule.per_second if self.rule else 0.0

    def headers(self) -> dict[str, str]:
        """OpenAI-style ``x-ratelimit-*-requests`` headers for the tightest rule (none when not limited)."""
        if not self.limited:
            return {}
        return {"x-ratelimit-limit-requests": str(self.limit),
                "x-ratelimit-remaining-requests": str(self.remaining),
                "x-ratelimit-reset-requests": duration(self.reset_seconds)}


UNIT_ORDER = tuple(store.UNITS)


def _short(value: float, lang: str = "en") -> str:
    """At most three significant digits, never in exponent form (decimal comma in Italian)."""
    text = f"{value:.0f}" if value >= 100 else f"{value:.3g}"
    return text.replace(".", ",") if lang == "it" else text


def rule_text(lang: str, rule) -> str:
    """"60 requests per minute (bursts of up to 10)"."""
    if isinstance(rule, dict):
        rule = RateRule(float(rule["requests"]), rule["per"], int(rule.get("burst") or rule["requests"]))
    text = translate(lang, f"account.rate_rule_{rule.per}", count=round(rule.requests, 2),
                     rate=_short(rule.requests, lang))
    if rule.burst != round(rule.requests) and rule.burst > 1:
        text = translate(lang, "account.rate_burst", rule=text, burst=rule.burst)
    return text


def rate_text(lang: str, rules) -> str:
    """Every rule of a request rate, joined ("1 request per second and 300 per hour")."""
    texts = [rule_text(lang, rule) for rule in rules]
    if not texts:
        return translate(lang, "account.rate_none")
    return translate(lang, "account.rate_and").join(texts)


def duration(seconds: float) -> str:
    """``4h59m30s``, ``1m30s``, ``2s``, ``0.25s`` (the format OpenAI uses in rate-limit headers)."""
    seconds = max(0.0, seconds)
    if seconds >= 3600:
        total = int(math.ceil(seconds))
        hours, rest = divmod(total, 3600)
        minutes, rest = divmod(rest, 60)
        return f"{hours}h{minutes}m{rest}s"
    if seconds >= 60:
        minutes, rest = divmod(int(math.ceil(seconds)), 60)
        return f"{minutes}m{rest}s"
    return f"{round(seconds, 3):g}s"


def token_headers(user, pool: str = "api") -> dict[str, str]:
    """OpenAI-style ``x-ratelimit-*-tokens`` headers for the pool's 5-hour window (none when not limited).
    Before a window opens the reset is ``0s``: nothing to wait for."""
    if user["role"] == "admin":
        return {}
    with snapshot(fresh=True):
        window = effective(user, pool).window
    if not window.limited:
        return {}
    reset = (window.resets_at - _now()).total_seconds() if window.resets_at else 0.0
    return {"x-ratelimit-limit-tokens": str(int(window.tokens)),
            "x-ratelimit-remaining-tokens": str(int(window.left)),
            "x-ratelimit-reset-tokens": duration(reset)}


def _take(scope: str, user_id: str, rate: RateLimit, now: float | None) -> RateDecision:
    if not rate.limited or not rate.rules:
        return RateDecision(True, False)
    now = time.time() if now is None else now
    rules = rate.rules
    allowed, states = store.take_tokens(
        [(store.bucket_key(scope, user_id, rule.base or rule.per), rule.per_second, rule.burst) for rule in rules], now)
    if allowed:
        # The tightest rule: the fewest whole requests left (the longer window on ties).
        index = min(range(len(rules)), key=lambda i: (math.floor(states[i][0] + 1e-9), -rules[i].seconds))
        retry = 0
    else:
        index = max(range(len(rules)), key=lambda i: states[i][1])
        retry = max(1, math.ceil(states[index][1] - 1e-9))
    rule, (tokens, _retry) = rules[index], states[index]
    return RateDecision(allowed, True, rule.burst, int(math.floor(tokens + 1e-9)),
                        (rule.burst - tokens) / rule.per_second, retry, rule, rules)


def check_rate(user, pool: str, *, now: float | None = None) -> RateDecision:
    """Take one request from each of the account's buckets for *pool* (all rules must pass)."""
    with snapshot():
        return _take(pool, user["id"], rate_limit(user, pool), now)


# ----- admission ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Refusal:
    """Why a request cannot run: ``code`` (API error code), HTTP ``status``, ``Retry-After`` and a message."""
    code: str
    status: int
    retry_after: int | None
    key: str
    params: tuple = ()

    def message(self, lang: str = "en") -> str:
        params = dict(self.params)
        if params.get("rule") is not None:
            params["rate"] = rule_text(lang, params["rule"])  # the English ``rate`` is for callers without a language
        return translate(lang, f"account.refusal_{self.key}", **params)


@dataclass(frozen=True)
class Admission:
    refusal: Refusal | None
    budget: credits.Budget | None = None
    model: ModelLimits | None = None
    rate: RateDecision | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None

    @property
    def slow(self) -> bool:
        """Whether the request should wait in the slow lane (the pool's regular tokens are used up)."""
        return self.budget is not None and self.budget.next_is_slow


def _time_text(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def _seconds_until(moment: datetime | None, default: int = 3600) -> int:
    if moment is None:
        return default
    return max(60, math.ceil((moment - _now()).total_seconds()))


def pool_refusal(budget: credits.Budget) -> Refusal:
    if budget.weekly_exhausted:
        return Refusal("insufficient_quota", 429, budget.seconds_until_reset(), "weekly",
                       (("date", _time_text(budget.weekly_resets_at)),))
    if budget.resets_at is None:
        return Refusal("insufficient_quota", 429, 3600, "window_none")
    return Refusal("insufficient_quota", 429, budget.seconds_until_reset(), "window",
                   (("date", _time_text(budget.resets_at)),))


def model_refusal(limits: ModelLimits) -> Refusal | None:
    if limits.admin:
        return None
    if limits.locked:
        return Refusal("model_locked", 403, None, "model_locked", (("model", limits.name),))
    if limits.weekly.exhausted:
        return Refusal("insufficient_quota", 429, _seconds_until(limits.weekly.resets_at), "model_weekly",
                       (("date", _time_text(limits.weekly.resets_at)), ("model", limits.name)))
    if limits.window.exhausted:
        if limits.window.resets_at is None:
            return Refusal("insufficient_quota", 429, 3600, "model_window_none", (("model", limits.name),))
        return Refusal("insufficient_quota", 429, _seconds_until(limits.window.resets_at), "model_window",
                       (("date", _time_text(limits.window.resets_at)), ("model", limits.name)))
    return None


def admit(user, pool: str, model, *, take_rate: bool = True, now: float | None = None) -> Admission:
    """Can *user* run a request in *pool* with *model* now? Checks the pool's 5-hour and weekly tokens (unless
    the model does not count toward them), then the model's lock and limits, and takes one request from the
    model's rate buckets (*take_rate*). The pool's request rate is checked separately (:func:`check_rate`),
    where requests arrive."""
    if user["role"] == "admin":
        return Admission(None, _admin_result(pool).budget())
    with snapshot():
        return _admit(user, pool, model, take_rate=take_rate, now=now)


def _admit(user, pool: str, model, *, take_rate: bool, now: float | None) -> Admission:
    policy = _model_policy(model) if model is not None else None
    counted = model is None or counts_toward_pool(model, policy)
    budget = credits.budget(user, pool)
    if counted and not budget.available:
        return Admission(pool_refusal(budget), budget)
    if model is None:
        return Admission(None, budget)
    limits = model_limits(user, model, policy=policy)
    refusal = model_refusal(limits)
    if refusal is not None:
        return Admission(refusal, budget, limits)
    decision = None
    if take_rate and limits.rate.limited:
        decision = _take(f"model{model['id']}", user["id"], limits.rate, now)
        if not decision.allowed:
            return Admission(Refusal("rate_limit_exceeded", 429, decision.retry_after, "model_rate",
                                     (("model", limits.name), ("rate", rule_text("en", decision.rule)),
                                      ("rule", decision.rule), ("seconds", decision.retry_after))), budget, limits,
                                     decision)
    if not counted:
        budget = replace(budget, slow_limit=0.0) if not budget.unlimited else budget
    return Admission(None, budget if counted else _admin_result(pool).budget(), limits, decision)


def model_blocked(user, model, pool: str | None = None, *, budget: credits.Budget | None = None) -> bool:
    """Whether *model* cannot be used by *user* now: locked or its own tokens used up, or (with *pool*) it counts
    toward the pool's tokens and those are used up. Takes nothing."""
    if user["role"] == "admin":
        return False
    policy = _model_policy(model)
    if pool is not None and counts_toward_pool(model, policy):
        if not (budget or credits.budget(user, pool)).available:
            return True
    return model_refusal(model_limits(user, model, policy=policy)) is not None


def prefer_usable(user, selection, requested: str | None, *, pool: str | None = None, candidates=None):
    """For ``auto``: when the first candidate is locked or used up for the account (with *pool*: also when it
    counts toward the pool's used-up tokens), move to the next usable one - among the fallbacks, then among
    ``candidates()`` (every model the request could use), so a model outside a used-up pool still answers."""
    if user["role"] == "admin" or (requested or "auto") != "auto":
        return selection
    budget = credits.budget(user, pool) if pool is not None else None
    if not model_blocked(user, selection.model, pool, budget=budget):
        return selection
    options = list(selection.fallbacks)
    if candidates is not None:
        seen = {model["id"] for model in (selection.model, *options)}
        options += [model for model in candidates() if model["id"] not in seen]
    usable = [model for model in options if not model_blocked(user, model, pool, budget=budget)]
    if usable:
        selection.model, selection.fallbacks = usable[0], usable[1:3]
    return selection


def fallback_directions(settings: dict | None = None) -> tuple[bool, bool]:
    """``(cloud → local, local → cloud)``: which way a chosen model may be replaced when its quota runs out
    (both on by default)."""
    settings = settings if settings is not None else _settings()

    def on(name):
        value = settings.get(name)
        return True if value is None else bool(value)

    return on("quota_fallback_to_local"), on("quota_fallback_to_cloud")


def crosses_allowed(model, other, directions: tuple[bool, bool]) -> bool:
    """Whether *other* may stand in for *model*: the same kind (local or cloud) always, the other kind when the
    administrator allows that direction."""
    local, other_local = is_local(model), is_local(other)
    if local == other_local:
        return True
    to_local, to_cloud = directions
    return to_local if other_local else to_cloud


def quota_fallback(user, selection, requested: str | None, *, pool: str, candidates, settings: dict | None = None):
    """For a model chosen by name: when its quota is used up for the account (locked, its own limits or its
    provider's pooled quota spent, or it counts toward the pool's spent tokens), switch to a usable model -
    the same kind first (local or cloud), then the other kind when the administrator allows that direction.
    The switch is recorded as ``selection.reason = "quota"`` with the original in ``selection.requested``.

    A model that is not blocked keeps its place and gets fallbacks of the other kind (when allowed), so a cloud
    model whose upstream quota runs out mid-request can still be answered locally, and vice versa.
    ``candidates()`` lists every model the request could use. ``auto`` is left to :func:`prefer_usable`.
    """
    if user["role"] == "admin" or (requested or "auto") == "auto" or selection.reason:
        return selection
    directions = fallback_directions(settings)
    model = selection.model
    budget = credits.budget(user, pool)
    options = [other for other in candidates() if other["id"] != model["id"] and crosses_allowed(model, other,
                                                                                                    directions)]
    if not model_blocked(user, model, pool, budget=budget):
        if not selection.fallbacks:
            others = [other for other in options if is_local(other) != is_local(model)]
            selection.fallbacks = [other for other in others if not model_blocked(user, other, pool,
                                                                                  budget=budget)][:2]
        return selection
    same = [other for other in options if is_local(other) == is_local(model)]
    usable = [other for other in same + [o for o in options if o not in same]
              if not model_blocked(user, other, pool, budget=budget)]
    if usable:
        selection.requested, selection.reason = model, "quota"
        selection.model, selection.fallbacks = usable[0], usable[1:3]
    return selection


def usable_fallbacks(user, pool: str, fallbacks, *, think=None, effort: str | None = None) -> list:
    """Fallback models a request may switch to: not blocked for the account, within the pool limits when they
    count toward them, and able to take the same reasoning setting (the effort must be unlocked for them)."""
    from bananachat.services.inference import compatible_fallbacks

    candidates = compatible_fallbacks(list(fallbacks), think)
    if user["role"] == "admin" or not candidates:
        return candidates
    budget = None
    result = []
    for model in candidates:
        policy = _model_policy(model)
        if counts_toward_pool(model, policy):
            budget = budget or credits.budget(user, pool)
            if not budget.available:
                continue
        if model_refusal(model_limits(user, model, policy=policy)) is not None:
            continue
        if effort is None:
            # The first model does not reason, so no ``think`` is sent: a reasoning fallback thinks at its own
            # default (medium), which the account must be allowed to use with it.
            if supported_efforts(model):
                try:
                    resolve_effort(user, model, EFFORT_DEFAULT, policy=policy)
                except EffortLocked:
                    continue
        elif effort != "off" and store.effort_rank(effort) > \
                store.effort_rank(effort_ceiling(user, model, policy=policy) or "off"):
            continue
        result.append(model)
    return result


# ----- limits of one account, for forms ------------------------------------------------------

def base_limits(user_id: str, pool: str) -> dict:
    """The account's own limits before dynamic adjustment, bonus and grants (request forms compare to these)."""
    policy = store.get_policy(pool)
    override = store.get_override(user_id, pool) or store.Override(pool)
    return _base(policy, override, resolve_tier(store.user_settings(user_id).tier_id))


def base_limits_many(pairs) -> dict:
    """:func:`base_limits` for many ``(user_id, pool)`` pairs with a fixed number of queries."""
    pairs = set(pairs)
    user_ids = sorted({user_id for user_id, _pool in pairs})
    policies, all_tiers = store.all_policies(), tiers()
    overrides, tier_ids = store.overrides_of(user_ids), store.tier_ids_of(user_ids)
    return {(user_id, pool): _base(policies[pool], overrides.get((user_id, pool)) or store.Override(pool),
                                   resolve_tier(tier_ids.get(user_id), all_tiers))
            for user_id, pool in pairs}


def _base(policy: dict, override, tier) -> dict:
    def tiered(period, value):
        return _whole(value * float(tier["multiplier"])) if policy[period]["auto_tiers"] and tier else value

    def amount(name, period, key):
        value, default = getattr(override, name), tiered(period, policy[period][key])
        if value is None:
            return default
        return max(value, default) if name in override.automatic else value

    rules = list(override.rate_rules) if override.has_rate else policy["rate"]["rules"]
    return {
        "window_tokens": amount("window_tokens", "window", "tokens"),
        "window_slow_tokens": amount("window_slow_tokens", "window", "slow_tokens"),
        "weekly_tokens": amount("weekly_tokens", "weekly", "tokens"),
        "rate_rules": [dict(rule) for rule in rules],
        "window_enabled": policy["window"]["enabled"] or override.has_window,
        "weekly_enabled": policy["weekly"]["enabled"] or override.has_weekly,
        "rate_enabled": (policy["rate"]["enabled"] or override.has_rate) and bool(rules),
    }


def model_base(user_id: str, model) -> dict:
    """The account's own limits for one model before dynamic adjustment, capacity and grants, in the shape of
    :func:`base_limits` (model-specific requests and pledges compare to these)."""
    policy = store.get_model_policy(model)
    override = store.get_model_override(user_id, model["id"]) or store.ModelOverride(model["id"])
    enabled = policy["enabled"]
    tier = resolve_tier(store.user_settings(user_id).tier_id) if policy["auto_tiers"] and enabled else None

    def amount(name):
        own = getattr(override, name)
        if own is not None:
            return float(own)
        value = policy[name] if enabled else None
        if value is None:
            return None
        return _whole(float(value) * float(tier["multiplier"])) if tier is not None else float(value)

    window, weekly = amount("window_tokens"), amount("weekly_tokens")
    rules = list(override.rate_rules) if override.rate_rules is not None else \
        (list(policy["rate_rules"]) if enabled else [])
    return {"window_tokens": window or 0.0, "window_slow_tokens": 0.0, "weekly_tokens": weekly or 0.0,
            "rate_rules": [dict(rule) for rule in rules], "window_enabled": window is not None,
            "weekly_enabled": weekly is not None, "rate_enabled": bool(rules)}


def limit_on(user_id: str | None, pool: str | None, scope: str, model=None) -> bool:
    """Whether the *scope* limit is on in *pool* (any pool when None) - or for *model* - for the account (the
    policies when None), so that an ``extra`` grant for it changes something."""
    if model is not None:
        policy = store.get_model_policy(model)
        override = (store.get_model_override(user_id, model["id"]) if user_id else None) or \
            store.ModelOverride(model["id"])
        key = f"{scope}_tokens"
        return getattr(override, key, None) is not None or (policy["enabled"] and policy.get(key) is not None)
    for name in [pool] if pool else store.POOLS:
        if user_id is not None:
            if base_limits(user_id, name)[f"{scope}_enabled"]:
                return True
        elif store.get_policy(name)[scope]["enabled"]:
            return True
    return False


def visible_pools(user) -> list[str]:
    """Pools shown to the account: API and chat always, agents once they have token limits."""
    pools = ["api", "chat"]
    agent = store.get_policy("agent")
    if agent["window"]["enabled"] or agent["weekly"]["enabled"] or \
            (user["role"] != "admin" and not (store.get_override(user["id"], "agent") or store.Override("agent")).empty):
        pools.append("agent")
    return pools


# ----- speed ---------------------------------------------------------------------------

SPEED_RATE_FACTORS = {"slow": 0.5, "fast": 2.0}


def set_speed(user_id: str, speed: str, *, adjust_rate: bool, updated_by: str | None) -> None:
    """Slow down or speed up an account: queue priority, and optionally its request rate.

    Slowing down halves every request-rate rule of every pool, speeding up
    doubles it (as custom limits); a rate the previous speed set is replaced,
    not compounded. Back to normal removes custom rates.
    """
    if speed not in store.SPEEDS:
        raise ValueError("Choose a valid speed.")
    with db.transaction():
        previous = store.user_settings(user_id).speed
        store.update_user_settings(user_id, updated_by, speed=speed)
        if not adjust_rate:
            return
        for pool in store.POOLS:
            if speed == "normal":
                store.set_override(user_id, pool, updated_by, rate_rules=None)
                continue
            policy_rules = store.get_policy(pool)["rate"]["rules"]
            current = base_limits(user_id, pool)["rate_rules"]
            if previous in SPEED_RATE_FACTORS and _speed_rules(policy_rules, previous) == current:
                current = policy_rules  # the custom rate is the previous adjustment: start again from the normal rate
            rules = _speed_rules(current, speed)
            store.set_override(user_id, pool, updated_by, rate_rules=rules or None)


def _speed_rules(rules, speed: str) -> list[dict]:
    """*rules* slowed down or sped up (whole numbers, at least one request)."""
    factor = SPEED_RATE_FACTORS[speed]
    return [{"requests": min(store.REQUESTS_MAX, max(1, math.floor(rule["requests"] * factor))), "per": rule["per"],
             "burst": min(store.BURST_MAX, max(1, math.floor(rule["burst"] * factor)))} for rule in rules]


# ----- tiers -----------------------------------------------------------------------------

def _suspension_clean(row, now: datetime, days: int) -> bool:
    if users.is_suspended(row):
        return False
    last = db.parse_timestamp(row["last_suspension_at"])
    return last is None or last <= now - timedelta(days=days)


def requirements(tier: dict, stats: dict, now: datetime) -> list[dict]:
    """Each promotion requirement of *tier* with what the account has."""
    created = db.parse_timestamp(stats["created_at"]) or now
    age = max(0, (now - created).days)
    items = [
        {"key": "account_days", "need": int(tier["min_account_days"]), "have": age},
        {"key": "active_days", "need": int(tier["min_active_days"]), "have": int(stats["active_days"])},
        {"key": "tokens", "need": float(tier["min_tokens_30d"]), "have": round(float(stats["tokens"]))},
    ]
    for item in items:
        item["met"] = item["have"] >= item["need"]
    clean = _suspension_clean(stats, now, int(tier["clean_days"]))
    items.append({"key": "clean_days", "need": int(tier["clean_days"]), "have": None, "met": clean})
    return items


def eligible_index(all_tiers: list[dict], stats: dict, now: datetime) -> int:
    """The highest tier whose requirements the account meets (0 when none)."""
    best = 0
    for index, tier in enumerate(all_tiers):
        if index and all(item["met"] for item in requirements(tier, stats, now)):
            best = index
    return best


def auto_tiers_enabled(policies: dict | None = None) -> bool:
    policies = policies or store.all_policies()
    return any(policy[period]["auto_tiers"] for policy in policies.values() for period in store.PERIODS)


def promote(now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Move accounts up to the highest tier they qualify for (never down, never locked ones).

    Returns ``(username, old tier, new tier)`` for every promotion.
    """
    now = now or _now()
    if not auto_tiers_enabled():
        return []
    all_tiers = tiers()
    if len(all_tiers) < 2:
        return []
    positions = {tier["id"]: index for index, tier in enumerate(all_tiers)}
    promoted = []
    for row in store.promotion_candidates(db.timestamp(now - timedelta(days=HISTORY_DAYS)), counted_types()):
        if row["tier_locked"]:
            continue
        current = positions.get(row["tier_id"], 0)
        target = eligible_index(all_tiers, row, now)
        if target <= current:
            continue
        with db.transaction():
            store.update_user_settings(row["id"], None, tier_id=all_tiers[target]["id"],
                                       tier_changed_at=db.timestamp(now))
            users.audit(None, "limits.tier_promote", row["username"],
                        {"from": all_tiers[current]["name"], "to": all_tiers[target]["name"]})
        promoted.append((row["username"], all_tiers[current]["name"], all_tiers[target]["name"]))
    return promoted


def tier_progress(user, now: datetime | None = None) -> dict | None:
    """The account's tier and what the next one asks for (None for administrators or without tiers)."""
    if user["role"] == "admin":
        return None
    now = now or _now()
    all_tiers = tiers()
    if not all_tiers:
        return None
    prefs = store.user_settings(user["id"])
    current = resolve_tier(prefs.tier_id, all_tiers)
    index = next(i for i, tier in enumerate(all_tiers) if tier["id"] == current["id"])
    result = {"tier": current, "index": index, "count": len(all_tiers), "locked": prefs.tier_locked,
              "next": None, "requirements": [], "auto": auto_tiers_enabled()}
    if index + 1 < len(all_tiers):
        active_days, used = store.activity_summary(user["id"], db.timestamp(now - timedelta(days=HISTORY_DAYS)),
                                                   counted_types())
        row = users.get(user["id"])
        stats = {"created_at": row["created_at"], "active_days": active_days, "tokens": used,
                 "suspended": row["suspended"], "suspended_until": row["suspended_until"],
                 "last_suspension_at": row["last_suspension_at"]}
        result["next"] = all_tiers[index + 1]
        result["requirements"] = requirements(all_tiers[index + 1], stats, now)
    return result


# ----- reasoning effort ----------------------------------------------------------------------

EFFORT_CHOICES = (*store.EFFORT_LEVELS, "on")


class EffortLocked(Exception):
    """The requested reasoning effort is above what the account has unlocked for the model."""

    def __init__(self, requested: str, allowed: str, model_name: str = ""):
        super().__init__(f"Reasoning effort '{requested}' is locked; the highest allowed is '{allowed}'.")
        self.requested = requested
        self.allowed = allowed
        self.model_name = model_name


def supported_efforts(model) -> tuple[str, ...]:
    """The effort levels a model takes, lowest first: the catalog's ``reasoning_levels`` (a JSON list), else
    ``("off", "on")`` for thinking models (``on`` counts as medium), else none."""
    raw = _get(model, "reasoning_levels")
    levels: list = []
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            parsed = None
        if isinstance(parsed, (list, tuple)):
            levels = [str(level).strip().lower() for level in parsed]
    known = {level for level in levels if level in store.EFFORT_LEVELS or level == "on"}
    if "on" in known and known & {"low", "medium", "high", "max"}:
        known.discard("on")  # a model with named levels needs no generic "on"
    if known:
        return tuple(sorted(known, key=lambda level: (store.effort_rank(level), level == "on")))
    if _get(model, "is_reasoning"):
        return ("off", "on")
    return ()


def named_levels(model) -> bool:
    """Whether the model takes named levels (low, medium, high) rather than thinking on or off."""
    return bool(set(supported_efforts(model)) & {"low", "medium", "high", "max"})


def effort_settings(settings: dict | None = None) -> dict:
    settings = settings if settings is not None else _settings()

    def level(name, default):
        value = settings.get(name)
        return value if value in store.EFFORT_LEVELS else default

    def whole(name, default, low, high):
        try:
            value = int(settings.get(name))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    return {"gating": bool(settings.get("effort_gating_enabled", 1)),
            "default": level("effort_default_level", EFFORT_DEFAULT),
            "auto": bool(settings.get("effort_auto_unlock", 1)),
            "active_days": whole("effort_auto_active_days", 30, 1, 365),
            "tokens": whole("effort_auto_tokens", 2_000_000, 0, store.TOKENS_MAX),
            "period_days": whole("effort_auto_period_days", 60, 1, 365),
            "clean_days": whole("effort_auto_clean_days", 90, 0, 3650),
            "ceiling": level("effort_auto_ceiling", "high")}


def effort_gated(user, prefs=None, settings: dict | None = None) -> bool:
    """Whether effort levels are limited for the account (never for administrators)."""
    if user["role"] == "admin" or not effort_settings(settings)["gating"]:
        return False
    prefs = prefs or _prefs(user["id"])
    return not prefs.effort_gating_off


def effort_ceiling(user, model, *, policy: dict | None = None, own: dict | None = None, prefs=None,
                   settings: dict | None = None) -> str | None:
    """The highest effort level *user* may use with *model* (None when the model does not reason).

    Without gating, every level the model takes. Otherwise the account's own
    level for the model, else for all models, else the model's default, else
    the site default. A level an administrator set is exact (it may lock below
    the default); one from a request or an automatic unlock only ever raises,
    so it never caps a model below the level for all models or its default.
    """
    levels = supported_efforts(model)
    if not levels:
        return None
    if not effort_gated(user, prefs, settings):
        return "medium" if levels[-1] == "on" else levels[-1]
    own = own if own is not None else _effort_levels(user["id"])
    policy = policy if policy is not None else _model_policy(model)
    level = policy.get("effort_default") or effort_settings(settings)["default"]
    for mine in (own.get(None), own.get(model["id"])):
        if mine is not None and (mine.source == "admin" or store.effort_rank(mine.level) > store.effort_rank(level)):
            level = mine.level
    return level


def _model_level(levels: tuple, requested: str) -> str:
    """The level of *levels* that serves *requested*: the highest at or below it (``on`` for any level of an
    on/off model), else the lowest one that thinks."""
    if requested == "off":
        return "off" if "off" in levels else next(level for level in levels if level != "off")
    thinking = [level for level in levels if level != "off"]
    if not thinking:
        return "off"
    fitting = [level for level in thinking if store.effort_rank(level) <= store.effort_rank(requested)]
    return fitting[-1] if fitting else thinking[0]


def allowed_efforts(user, model, **kwargs) -> tuple[str, ...]:
    """The levels of *model* the account may use now, lowest first."""
    levels = supported_efforts(model)
    ceiling = effort_ceiling(user, model, **kwargs)
    if not levels or ceiling is None:
        return ()
    return tuple(level for level in levels if level == "off" or store.effort_rank(level) <= store.effort_rank(ceiling))


def resolve_effort(user, model, requested: str | None, **kwargs) -> str | None:
    """The effort level to run *model* with (None: the model does not reason). Raises :class:`EffortLocked`
    when *requested* is above the account's ceiling. Without a request: medium, or the ceiling if lower."""
    levels = supported_efforts(model)
    if not levels:
        return None
    allowed = allowed_efforts(user, model, **kwargs)
    if requested is None:
        target = _model_level(levels, EFFORT_DEFAULT)
        if target in allowed:
            return target
        # Below the default: the highest allowed level (or the lowest the model has, when it cannot stop thinking).
        thinking = [level for level in allowed if level != "off"]
        return thinking[-1] if thinking else (allowed[0] if allowed else levels[0])
    level = _model_level(levels, requested)
    if level not in allowed:
        best = [level for level in allowed if level != "off"]
        name = _get(model, "display_name") or _get(model, "ollama_name", "")
        raise EffortLocked(requested, "medium" if best and best[-1] == "on" else (best[-1] if best else "off"), name)
    return level


def effort_label(level: str, lang: str = "en") -> str:
    return translate(lang, f"account.effort_{level}")


def effort_summary(user, model, *, lang: str = "en", **kwargs) -> dict | None:
    """What the composer and account pages show for one model: levels, which are allowed, the default."""
    levels = supported_efforts(model)
    if not levels:
        return None
    allowed = allowed_efforts(user, model, **kwargs)
    default = resolve_effort(user, model, None, **kwargs)
    return {"levels": [{"value": level, "label": effort_label(level, lang), "allowed": level in allowed}
                       for level in levels],
            "default": default, "locked": [level for level in levels if level not in allowed]}


def composer_choices(user, choices: list[dict], *, lang: str, request_url) -> list[dict]:
    """The chat's model choices without models locked for the account, each with its effort levels
    (``effort``: see :func:`effort_summary`; locked levels carry ``request_url(model_name, level)``)."""
    if not choices:
        return choices
    rows = {row["ollama_name"]: row for row in db.query("SELECT * FROM ai_models")}
    locked = set() if user["role"] == "admin" else store.locked_model_ids(user["id"])
    policies = store.model_policies([row for row in rows.values()])
    own = store.effort_levels(user["id"]) if user["role"] != "admin" else {}
    prefs = store.user_settings(user["id"])
    result = []
    for choice in choices:
        model = rows.get(choice["name"])
        if model is not None and model["id"] in locked:
            continue
        effort = effort_summary(user, model, lang=lang, policy=policies[model["id"]], own=own, prefs=prefs) \
            if model is not None else None
        if effort:
            for level in effort["levels"]:
                if not level["allowed"]:
                    level["request_url"] = request_url(model["ollama_name"], level["value"])
        weight = policies[model["id"]]["weight"] if model is not None else 1.0
        counted = counts_toward_pool(model, policies[model["id"]]) if model is not None else True
        result.append({**choice, "effort": effort, "weight": weight if counted else None})
    return result


def unlock_effort(user_id: str, model_id: int | None, level: str, *, source: str, updated_by: str | None) -> str:
    """Raise an account's level for one model (or all models) to *level* - never lower it. Returns the level."""
    if level not in store.EFFORT_LEVELS:
        raise ValueError("Choose a valid reasoning effort level.")
    with db.transaction():
        current = store.effort_levels(user_id).get(model_id)
        if current is not None and store.effort_rank(current.level) >= store.effort_rank(level):
            return current.level
        store.set_effort_level(user_id, model_id, level, pinned=bool(current and current.pinned), source=source,
                               updated_by=updated_by)
    return level


def _next_level(levels: tuple, current: str) -> str | None:
    """The next level of *levels* above *current*, as stored (``on`` is stored as medium)."""
    for level in levels:
        if level != "off" and store.effort_rank(level) > store.effort_rank(current):
            return "medium" if level == "on" else level
    return None


def auto_unlock_effort(now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Unlock the next effort level for accounts with sustained use of a model (never above the ceiling).

    Needs, with that model within the period (60 days by default): enough
    active days (30) and tokens (2M), and no suspension in the clean period
    (90 days). After an unlock the account starts counting again. Levels an
    administrator kept (``pinned``) are left alone. Returns ``(username,
    model, level)`` for every unlock.
    """
    now = now or _now()
    settings = effort_settings(site_settings.get())
    if not settings["gating"] or not settings["auto"]:
        return []
    since = now - timedelta(days=settings["period_days"])
    clean_since = db.timestamp(now - timedelta(days=settings["clean_days"]))
    candidates = [row for row in store.effort_candidates(db.timestamp(since), clean_since)
                  if row["active_days"] >= settings["active_days"] and (row["tokens"] or 0) >= settings["tokens"]]
    unlocked = []
    ceiling_rank = store.effort_rank(settings["ceiling"])
    for row in candidates:
        user = users.get(row["user_id"])
        model = db.one("SELECT * FROM ai_models WHERE id=?", (row["model_id"],))
        if user is None or model is None or users.is_suspended(user) or not effort_gated(user):
            continue
        levels = supported_efforts(model)
        own = store.effort_levels(user["id"])
        mine = own.get(model["id"])
        if mine is not None and mine.pinned:
            continue
        if mine is not None and mine.updated_at and mine.updated_at > db.timestamp(since):
            # Counting starts again after the last change of this level.
            active_days, tokens = _model_activity(user["id"], model["id"], mine.updated_at)
            if active_days < settings["active_days"] or tokens < settings["tokens"]:
                continue
        current = effort_ceiling(user, model, own=own) or "off"
        target = _next_level(levels, current)
        if target is None or store.effort_rank(target) > ceiling_rank:
            continue
        with db.transaction():
            store.set_effort_level(user["id"], model["id"], target, source="automatic")
            users.audit(None, "limits.effort_unlock", user["username"],
                        {"model": model["ollama_name"], "from": current, "to": target, "source": "automatic"})
        unlocked.append((user["username"], model["ollama_name"], target))
    return unlocked


def _model_activity(user_id: str, model_id: int, since: str) -> tuple[int, float]:
    row = db.one("SELECT COUNT(DISTINCT substr(created_at, 1, 10)), SUM(tokens_in + tokens_out) FROM credit_ledger "
                 "WHERE user_id=? AND model_id=? AND created_at>=?", (user_id, model_id, since))
    return (int(row[0] or 0), float(row[1] or 0)) if row else (0, 0.0)


# ----- model presets --------------------------------------------------------------------------

def apply_model_preset(model_id: int, preset: str, *, updated_by: str | None = None) -> dict:
    """Fill a model's weight, limits, sensitivity and effort default from a strictness preset
    (``light``, ``standard`` or ``heavy``; heavy is strict). Its other settings (counting toward the pool,
    tiers) are kept. Returns the saved policy. Joins the caller's transaction."""
    model = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))
    if model is None:
        raise ValueError("That model does not exist.")
    current = store.get_model_policy(model)
    return store.set_model_policy(model_id, store.preset_policy(preset, current), updated_by)


# ----- background jobs -------------------------------------------------------------------

@background.job("limits-demand", every=60, initial_delay=15)
def sample_demand(app) -> None:
    from bananachat.services import queue

    stats = queue.stats()
    people = store.active_people(db.now(-timedelta(minutes=ACTIVE_PEOPLE_MINUTES)))
    record_demand(stats["running"], stats["waiting"], stats["max_concurrent"], people=people)


@background.job("limits-peak-hours", every=600, initial_delay=30)
def peak_hours_job(app) -> None:
    refresh_peak_hours()


@background.job("limits-tiers", every=3600, initial_delay=120)
def tiers_job(app) -> None:
    promote()
    auto_unlock_effort()


def forget_idle_buckets(now: float | None = None) -> int:
    """Forget request buckets idle for a day, or for as long as the slowest rule needs to refill its burst
    (a forgotten bucket counts as full, so forgetting one earlier would hand out requests)."""
    rule_sets = [policy["rate"]["rules"] for policy in store.all_policies().values()]
    rule_sets += [policy["rate_rules"] for policy in store.model_policies(
        db.query("SELECT * FROM ai_models")).values()]
    rule_sets += store.custom_rule_sets()
    refills = [rule["burst"] / store.rule_per_second(rule) for rules in rule_sets for rule in rules]
    idle = max(BUCKET_IDLE_SECONDS, *refills) if refills else BUCKET_IDLE_SECONDS
    return store.purge_buckets(idle, time.time() if now is None else now)


@background.job("limits-buckets", every=3600, initial_delay=300)
def buckets_job(app) -> None:
    forget_idle_buckets()
