"""The usage ledger, image reservations and quota requests.

Every amount is in **tokens** (prompt + completion). Usage is recorded per
request type and counted per *pool*:

* ``api`` - API, playground and image generation;
* ``chat`` - the chat interface (normal and no-history chats);
* ``agent`` - agents.

The limits of each pool and model (request rates, 5-hour and weekly tokens,
tiers, dynamic adjustment, grants) are decided by
:mod:`bananachat.services.limits`; :func:`budget` is the short answer
admission needs. Each pool has one 5-hour token allowance and an optional
weekly allowance. Music-program participants receive a bonus (multiplier or fixed
amount). Administrators are never limited.

A ledger row keeps the request's raw ``tokens_in``/``tokens_out`` and a
``consumes_limits`` flag (unmetered chat keeps raw usage for reporting). In
``credits_used`` it keeps the tokens counted against the pool
divided by 1,000 - tokens × the model's weight, or 0 for a model that does not
count toward the pool - which is the credit unit of earlier releases. Metered
usage opens the account's 5-hour and weekly windows (of the pool and of the
model) when none is open; unmetered chat opens neither.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bananachat import db

TOKENS_PER_CREDIT = 1000  # the unit of credits_used (and of earlier releases)
UNLIMITED = 10**12
POOL_TYPES = {"api": ("api", "playground"), "chat": ("chat", "chat_incognito"), "agent": ("agent",)}
REQUEST_TYPES = tuple(kind for kinds in POOL_TYPES.values() for kind in kinds)


class InsufficientCredits(ValueError):
    """The request needs more tokens than remain in the 5-hour window (or this week, when ``weekly``)."""

    def __init__(self, message: str, *, weekly: bool = False):
        super().__init__(message)
        self.weekly = weekly


class ReservationLost(RuntimeError):
    """An image reservation expired before the charge could be recorded."""


def tokens_to_credits(tokens: float) -> float:
    return tokens / TOKENS_PER_CREDIT


@dataclass(frozen=True)
class Budget:
    """What remains of the 5-hour tokens, capped by the optional weekly allowance.

    ``resets_at`` is when the open 5-hour window ends (None while no window is
    open: the next counted request opens one).
    """
    pool: str
    unlimited: bool
    regular_limit: float
    slow_limit: float
    regular_used: float
    slow_used: float
    weekly_limit: float | None = None
    weekly_used: float = 0.0
    resets_at: datetime | None = None
    weekly_resets_at: datetime | None = None

    @property
    def weekly_left(self) -> float:
        if self.unlimited or self.weekly_limit is None:
            return UNLIMITED
        return max(0.0, self.weekly_limit - self.weekly_used)

    @property
    def regular_left(self) -> float:
        if self.unlimited:
            return UNLIMITED
        return min(max(0.0, self.regular_limit - self.regular_used), self.weekly_left)

    @property
    def slow_left(self) -> float:
        """Deprecated compatibility value; there is no second token allowance."""
        return 0.0

    @property
    def available(self) -> bool:
        return self.unlimited or self.regular_left > 0

    @property
    def next_is_slow(self) -> bool:
        return False

    @property
    def weekly_exhausted(self) -> bool:
        return not self.unlimited and self.weekly_limit is not None and self.weekly_left <= 0

    @property
    def blocked_until(self) -> datetime | None:
        """When tokens come back: the end of the week when the weekly tokens ran out, else of the 5-hour window."""
        return self.weekly_resets_at if self.weekly_exhausted else self.resets_at

    def seconds_until_reset(self, now: datetime | None = None, *, weekly: bool | None = None) -> int:
        """Whole seconds until :attr:`blocked_until` (or the given window's end; at least 60, for ``Retry-After``)."""
        until = self.blocked_until if weekly is None else (self.weekly_resets_at if weekly else self.resets_at)
        if until is None:
            return 3600
        now = now or datetime.now(timezone.utc)
        return max(60, math.ceil((until - now).total_seconds()))


def pool_of(request_type: str) -> str:
    for pool, kinds in POOL_TYPES.items():
        if request_type in kinds:
            return pool
    raise ValueError("Unknown request type.")


# ----- per-account API quota (compatibility) -----------------------------------

def site_defaults(settings: dict | None = None) -> tuple[int, int]:
    """The API pool's default 5-hour tokens, followed by an inert legacy zero."""
    from bananachat.db import limits

    window = limits.get_policy("api")["window"]
    return int(window["tokens"]), 0


def _settings() -> dict:
    row = db.one("SELECT * FROM site_settings WHERE id=1")
    return row.to_dict() if row else {}


def get_quota(user_id: str) -> tuple[float, float]:
    """The account's own API-pool 5-hour tokens, followed by an inert legacy zero."""
    from bananachat.services import limits

    base = limits.base_limits(user_id, "api")
    return _whole(base["window_tokens"]), 0.0


def _whole(value):
    return int(value) if float(value).is_integer() else value


def set_quota(user_id: str, tokens: int, slow: int, updated_by: str | None) -> None:
    """Set the account's API 5-hour tokens; the deprecated *slow* argument is ignored."""
    from bananachat.db import limits

    if not 0 <= tokens <= limits.TOKENS_MAX:
        raise ValueError(f"Quotas must be between 0 and {limits.TOKENS_MAX:,} tokens.")
    limits.set_override(user_id, "api", updated_by, window_tokens=tokens)


# ----- music program bonus --------------------------------------------------

def music_bonus(user_id: str, settings: dict | None = None) -> tuple[str, float, float]:
    """``(mode, amount, 0)``: no bonus, a multiplier, or fixed extra tokens per 5-hour window.

    The final tuple slot is retained for older callers and never grants tokens.
    """
    settings = settings if settings is not None else _settings()
    if not settings.get("music_enabled"):
        return "none", 1.0, 0.0
    if not db.scalar("SELECT music_opted_in FROM users WHERE id=?", (user_id,), 0):
        return "none", 1.0, 0.0
    if (settings.get("music_bonus_mode") or "multiplier") == "fixed":
        tokens, _legacy_slow = music_fixed(settings)
        return "fixed", tokens, 0.0
    multiplier = settings.get("music_credit_multiplier")
    multiplier = 2.0 if multiplier is None else max(0.0, float(multiplier))
    return "multiplier", multiplier, 0.0


def music_fixed(settings: dict) -> tuple[float, float]:
    """The fixed token bonus followed by an inert zero (legacy credit units are a fallback)."""
    tokens = settings.get("music_bonus_fixed_tokens")
    if tokens is None:
        credits = settings.get("music_bonus_fixed_credits")
        tokens = (30 if credits is None else credits) * TOKENS_PER_CREDIT
    return float(tokens), 0.0


def music_weekly_fixed(settings: dict) -> float:
    """The weekly music bonus, preserved separately when old 5-hour allowances were combined."""
    tokens = settings.get("music_bonus_fixed_weekly_tokens")
    return float(tokens) if tokens is not None else music_fixed(settings)[0] * 7


# ----- usage ----------------------------------------------------------------

def usage_today(user_id: str, pool: str) -> tuple[float, float]:
    """All counted tokens today (UTC), followed by an inert legacy zero.

    Historical slow charges and active image reservations consume the same allowance.
    """
    start, end = db.day_bounds()
    types = POOL_TYPES[pool]
    placeholders = ",".join("?" for _ in types)
    row = db.one(
        "SELECT SUM(credits_used) "
        f"FROM credit_ledger WHERE user_id=? AND created_at>=? AND created_at<? AND request_type IN ({placeholders})",
        (user_id, start, end, *types))
    used = (row[0] or 0.0) if row else 0.0
    if pool == "api":
        reserved = db.one("SELECT SUM(credits_reserved) "
                          "FROM image_credit_reservations WHERE user_id=?", (user_id,))
        used += reserved[0] or 0.0
    return float(used) * TOKENS_PER_CREDIT, 0.0


def budget(user, pool: str) -> Budget:
    """The remaining allowance of *user* in *pool* (see ``services.limits.effective``)."""
    if pool not in POOL_TYPES:
        raise ValueError("Unknown credit pool.")
    from bananachat.services import limits

    return limits.effective(user, pool).budget()


def _open_windows(user_id: str, pool: str | None, model_id, at: str) -> None:
    """Open the windows at *at*, the time stamped on the ledger row, so the row that opens a window counts in it."""
    from bananachat.db import limits

    scopes = ([limits.pool_scope(pool)] if pool else []) + ([limits.model_scope(model_id)] if model_id else [])
    if scopes:
        limits.open_windows(user_id, scopes, db.parse_timestamp(at))


def charge(user_id: str, tokens_in: int, tokens_out: int, *, request_type: str, model_id=None, token_id=None,
           usage_estimated: bool = False) -> tuple[float, bool]:
    """Record usage in the ledger and open the account's windows. Join an open transaction when one exists.

    Returns ``(tokens counted against the pool, False)``; the legacy slow flag is inert.
    """
    from bananachat.services import limits

    if request_type not in REQUEST_TYPES:
        raise ValueError("Unknown request type.")
    tokens_in, tokens_out = max(0, int(tokens_in)), max(0, int(tokens_out))
    with db.transaction():
        user = db.one("SELECT id, role FROM users WHERE id=?", (user_id,))
        if user is None:
            return 0.0, False
        model = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,)) if model_id is not None else None
        if model is None:
            model_id = None
        pool = pool_of(request_type)
        with limits.snapshot(fresh=True):  # the latest windows and usage, never what admission read
            weight, counted = limits.charge_terms(model_id)
            consumes_limits = limits.consumes_token_limits(model, pool)
            counted = counted and consumes_limits
            counted_tokens = (tokens_in + tokens_out) * weight if counted else 0.0
        stamp = db.now()
        db.execute(
            "INSERT INTO credit_ledger (user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out, "
            "request_type, created_at, usage_estimated, consumes_limits) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, token_id, model_id, tokens_to_credits(counted_tokens), 0, tokens_in, tokens_out,
             request_type, stamp, int(usage_estimated), int(consumes_limits)))
        if consumes_limits:
            _open_windows(user_id, pool if counted else None, model_id, stamp)
    return counted_tokens, False


def estimate_tokens(text: str) -> int:
    """Rough token estimate (four characters per token) when a backend reports none."""
    return math.ceil(len(text or "") / 4)


def history(user_id: str, *, pool: str = "api", limit: int = 200):
    types = POOL_TYPES[pool]
    placeholders = ",".join("?" for _ in types)
    return db.query(
        "SELECT l.*, l.credits_used * 1000 AS counted_tokens, t.name AS token_name, t.token_prefix, "
        "m.display_name AS model_name FROM credit_ledger l "
        "LEFT JOIN api_tokens t ON t.id=l.token_id LEFT JOIN ai_models m ON m.id=l.model_id "
        f"WHERE l.user_id=? AND l.request_type IN ({placeholders}) ORDER BY l.id DESC LIMIT ?",
        (user_id, *types, limit))


# ----- image reservations ---------------------------------------------------

def reserve_image(user, tokens: float, *, ttl_seconds: float, model_id=None, token_id=None, counted: bool = True):
    """Reserve a fixed image charge (in counted tokens) before generation. Returns the reservation id or None.

    A model that does not count toward the pool reserves nothing (its own
    limits are checked at admission).
    """
    from bananachat.services import limits

    if tokens <= 0 or not counted:
        return None
    with db.transaction():
        purge_expired_reservations(ttl_seconds)
        if user["role"] == "admin":
            return None
        with limits.snapshot(fresh=True):  # checked and reserved in one transaction: never what admission read
            current = budget(user, "api")
        if current.regular_left < tokens:
            if current.weekly_exhausted or current.weekly_left < tokens:
                raise InsufficientCredits("Not enough tokens remain this week for an image.", weekly=True)
            raise InsufficientCredits("Not enough tokens remain in this 5-hour window for an image.")
        stamp = db.now()
        cursor = db.execute(
            "INSERT INTO image_credit_reservations (user_id, token_id, model_id, credits_reserved, is_slow, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (user["id"], token_id, model_id, tokens_to_credits(tokens), 0, stamp, stamp))
        _open_windows(user["id"], "api", None, stamp)
        return cursor.lastrowid


def purge_expired_reservations(ttl_seconds: float) -> int:
    """Delete reservations not refreshed within *ttl_seconds* (their request died without a refund)."""
    return db.execute("DELETE FROM image_credit_reservations WHERE COALESCE(updated_at, created_at)<?",
                      (db.now(-timedelta(seconds=ttl_seconds)),)).rowcount


def touch_reservation(reservation_id) -> bool:
    if reservation_id is None:
        return True
    return db.execute("UPDATE image_credit_reservations SET updated_at=? WHERE id=?",
                      (db.now(), reservation_id)).rowcount == 1


def refund_reservation(reservation_id) -> None:
    if reservation_id is not None:
        db.execute("DELETE FROM image_credit_reservations WHERE id=?", (reservation_id,))


def finalize_reservation(reservation_id, *, user_id: str, tokens: float, model_id=None, token_id=None,
                         counted: bool = True) -> None:
    """Turn a reservation into a ledger charge (join the caller's transaction).

    *tokens* is the image's raw token cost; the pool counts it × the model's
    weight (the reserved amount). If the reservation vanished (it outlived its
    lifetime), the image was still produced, so the charge is recorded anyway
    rather than given away.
    """
    from bananachat.services import limits

    with db.transaction():
        row = None
        if reservation_id is not None:
            row = db.one("SELECT * FROM image_credit_reservations WHERE id=?", (reservation_id,))
        weight, _counts = limits.charge_terms(model_id)
        counted_credits = tokens_to_credits(tokens * weight) if counted else 0.0
        if row is not None:
            db.execute("DELETE FROM image_credit_reservations WHERE id=?", (reservation_id,))
            counted_credits = row["credits_reserved"]
        stamp = db.now()
        db.execute(
            "INSERT INTO credit_ledger (user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out, "
            "request_type, created_at, usage_estimated) VALUES (?,?,?,?,?,0,?,'api',?,0)",
            (user_id, token_id, model_id, counted_credits, 0, int(tokens), stamp))
        _open_windows(user_id, "api" if counted else None, model_id, stamp)


# ----- quota requests -------------------------------------------------------

REQUEST_KINDS = ("window", "weekly", "rate", "temporary", "effort")
REQUEST_TOKENS_MAX = 2_000_000_000
REQUEST_WEEKLY_MAX = 700_000_000
REQUEST_HOURS_MAX = 720
REQUEST_REASON_MIN, REQUEST_REASON_MAX = 5, 1000


class RequestError(ValueError):
    """A quota request that cannot be sent. ``key`` is its message in the ``account`` catalog."""

    def __init__(self, message: str, key: str, **params):
        super().__init__(message)
        self.key = key
        self.params = params


def _in_range(value, low, high) -> bool:
    return value is not None and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high


def _ceil(value) -> int:
    return int(math.ceil(float(value) - 1e-9))


def request_kind(row) -> str:
    """The kind of a stored request (``daily`` from earlier builds is the 5-hour ``window``)."""
    kind = row["kind"] or "window"
    return "window" if kind == "daily" else kind


def request_rules(row) -> list[dict]:
    try:
        rules = json.loads(row["new_rate_rules"] or "[]")
    except (ValueError, TypeError):
        rules = []
    return [rule for rule in rules if isinstance(rule, dict) and rule.get("per") and rule.get("requests")]


def submit_request(user_id: str, tokens: int | None = None, slow: int | None = None, reason: str = "", *,
                   kind: str = "window", pool: str = "api", weekly: int | None = None, per: str | None = None,
                   requests: int | None = None, hours: int | None = None, unlimited: bool = False,
                   model_id: int | None = None, level: str | None = None, community: bool = False) -> dict:
    """Ask for more: 5-hour (``window``) or ``weekly`` tokens, a higher request ``rate`` (one rule of the
    account, by its unit), a ``temporary`` grant, or a higher reasoning ``effort`` level (one model or all).

    With *model_id*, the 5-hour, weekly, rate and temporary kinds ask for more of that one model's own limits
    (every service shares them) instead of a service's; for ``effort`` it names the model (None: all models).

    Service 5-hour (and, when configured, weekly) requests within the automatic-approval
    amounts are approved at once; other requests wait for an administrator, and with *community* (when the
    site allows it for the kind) for other people's consent too (``services.community``). One request can be
    pending per account. The deprecated *slow* argument is ignored.
    """
    from bananachat.db import limits as limits_db
    from bananachat.services import community as community_service
    from bananachat.services import limits

    reason = (reason or "").strip()
    kind = "window" if kind == "daily" else kind
    if kind not in REQUEST_KINDS or pool not in limits_db.POOLS:
        raise RequestError("Unknown kind of request.", "quota_failed")
    if not REQUEST_REASON_MIN <= len(reason) <= REQUEST_REASON_MAX:
        raise RequestError("Explain the request in 5-1000 characters.", "quota_reason_length",
                           min=REQUEST_REASON_MIN, max=REQUEST_REASON_MAX)
    with db.transaction():
        target = None
        if model_id is not None and kind != "effort":
            target = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))
            if target is None:
                raise RequestError("That model does not exist.", "quota_failed")
        base = limits.model_base(user_id, target) if target is not None else limits.base_limits(user_id, pool)
        if db.one("SELECT 1 FROM quota_requests WHERE user_id=? AND status='pending'", (user_id,)):
            raise RequestError("You already have a pending request.", "quota_already_pending")
        settings = _settings()
        auto = bool(settings.get("quota_auto_approve_enabled"))
        values = {"new_tokens": None, "new_slow_tokens": 0, "new_weekly_tokens": None, "new_rate_rules": None,
                  "grant_hours": None, "grant_unlimited": 0, "model_id": None, "effort_level": None,
                  "effort_all_models": 0}
        automatic = False
        if kind == "window":
            if not base["window_enabled"]:
                raise RequestError("This service has no 5-hour limit.", "quota_not_limited")
            if not _in_range(tokens, 1, REQUEST_TOKENS_MAX):
                raise RequestError(f"Request between 1 and {REQUEST_TOKENS_MAX:,} tokens.", "quota_range",
                                   max=REQUEST_TOKENS_MAX)
            if tokens <= base["window_tokens"]:
                raise RequestError("A request must raise your quota.", "quota_must_raise")
            values.update(new_tokens=int(tokens))
            automatic = auto and target is None and \
                tokens <= int(settings.get("quota_auto_approve_max_tokens") or 0)
        elif kind == "weekly":
            if not base["weekly_enabled"]:
                raise RequestError("This service has no weekly limit.", "quota_not_limited")
            if not _in_range(weekly, 1, REQUEST_WEEKLY_MAX):
                raise RequestError("Request between 1 and 700,000,000 tokens.", "quota_range",
                                   max=REQUEST_WEEKLY_MAX)
            if weekly <= base["weekly_tokens"]:
                raise RequestError("A request must raise your quota.", "quota_must_raise")
            values.update(new_weekly_tokens=int(weekly))
            weekly_max = int(settings.get("quota_auto_approve_max_weekly_tokens") or 0)
            automatic = auto and target is None and weekly_max > 0 and weekly <= weekly_max
        elif kind == "rate":
            if not base["rate_enabled"]:
                raise RequestError("This service has no request-rate limit.", "quota_not_limited")
            current = next((rule for rule in base["rate_rules"] if rule["per"] == per), None)
            if current is None or not _in_range(requests, 1, limits_db.REQUESTS_MAX):
                raise RequestError("Ask for a valid request rate.", "quota_rate_range", max=limits_db.REQUESTS_MAX)
            if requests <= current["requests"]:
                raise RequestError("A request must raise your rate.", "quota_must_raise")
            values.update(new_rate_rules=json.dumps([{"requests": int(requests), "per": per}]))
        elif kind == "effort":
            level = level if level in limits_db.EFFORT_LEVELS[1:] else None
            model = None
            if model_id is not None:
                model = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))
                if model is None or not limits.supported_efforts(model):
                    raise RequestError("That model has no reasoning effort levels.", "quota_effort_model")
            if level is None:
                raise RequestError("Choose a reasoning effort level.", "quota_effort_level")
            user = db.one("SELECT id, role FROM users WHERE id=?", (user_id,))
            if model is not None:
                ceiling = limits.effort_ceiling(user, model) or "off"
            else:
                own = limits_db.effort_levels(user_id).get(None)
                ceiling = own.level if own else limits.effort_settings(settings)["default"]
            if not limits.effort_gated(user) or limits_db.effort_rank(level) <= limits_db.effort_rank(ceiling):
                raise RequestError("You can already use that level.", "quota_effort_already")
            values.update(model_id=model_id, effort_level=level, effort_all_models=int(model_id is None))
            pool = "chat"
        else:
            if not _in_range(hours, 1, REQUEST_HOURS_MAX):
                raise RequestError("Ask for 1 to 720 hours.", "quota_hours_range", max=REQUEST_HOURS_MAX)
            if not unlimited and not base["window_enabled"]:
                raise RequestError("This service has no 5-hour limit: only unlimited use can be requested.",
                                   "quota_temporary_no_window")
            if not unlimited and not _in_range(tokens, 1, REQUEST_TOKENS_MAX):
                raise RequestError(f"Request between 1 and {REQUEST_TOKENS_MAX:,} tokens.", "quota_range",
                                   max=REQUEST_TOKENS_MAX)
            values.update(new_tokens=0 if unlimited else int(tokens), grant_hours=int(hours),
                          grant_unlimited=int(bool(unlimited)))
        if target is not None:
            values["model_id"] = target["id"]
        status = "approved" if automatic else "pending"
        community = bool(community) and not automatic and community_service.offered(kind, settings)
        community_until = db.now(timedelta(hours=community_service.settings(settings)["hours"])) \
            if community else None
        # The credit columns are what earlier releases read.
        credits_value = (values["new_tokens"] or 0) / TOKENS_PER_CREDIT
        cursor = db.execute(
            "INSERT INTO quota_requests (user_id, kind, pool, new_credits, new_slow_credits, new_weekly_credits, "
            "new_tokens, new_slow_tokens, new_weekly_tokens, new_rate_rules, grant_hours, grant_unlimited, model_id, "
            "effort_level, effort_all_models, reason, duration_type, status, resolution_source, created_at, "
            "resolved_at, community, community_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'permanent', ?, ?, ?, ?, "
            "?, ?)",
            (user_id, kind, pool, _ceil(credits_value), _ceil((values["new_slow_tokens"] or 0) / TOKENS_PER_CREDIT),
             _ceil(values["new_weekly_tokens"] / TOKENS_PER_CREDIT) if values["new_weekly_tokens"] else None,
             values["new_tokens"], values["new_slow_tokens"], values["new_weekly_tokens"], values["new_rate_rules"],
             values["grant_hours"], values["grant_unlimited"], values["model_id"], values["effort_level"],
             values["effort_all_models"], reason,
             status, "automatic" if automatic else "manual", db.now(), db.now() if automatic else None,
             int(community), community_until))
        if automatic:
            # A granted raise is a custom limit, so restoring defaults for everyone is a deliberate choice.
            _apply(db.one("SELECT * FROM quota_requests WHERE id=?", (cursor.lastrowid,)), user_id)
    return {"id": cursor.lastrowid, "status": status}


def _apply(row, resolved_by: str) -> None:
    """Carry out an approved request: a custom limit (never lower than now), a grant or an effort unlock."""
    from bananachat.db import limits as limits_db
    from bananachat.services import limits

    user_id, pool, kind = row["user_id"], row["pool"] or "api", request_kind(row)
    if kind == "effort":
        if not row["effort_level"]:
            raise ValueError("This request names no reasoning effort level; deny it instead.")
        if row["model_id"] is None and not row["effort_all_models"]:
            raise ValueError("The model of this request was removed; deny it instead.")
        limits.unlock_effort(user_id, row["model_id"], row["effort_level"], source="request", updated_by=resolved_by)
        return
    if row["model_id"] is not None:
        _apply_model(row, resolved_by)
        return
    base = limits.base_limits(user_id, pool)
    # A custom value switches its limit on, so a request sent before the limit was switched off
    # would give this one account a limit nobody else has.
    if kind in ("window", "weekly", "rate") and not base[f"{kind}_enabled"]:
        what = {"rate": "request rate", "window": "5-hour", "weekly": "weekly"}[kind]
        raise ValueError(f"This service no longer has a {what} limit; deny the request instead.")
    if kind == "temporary" and not row["grant_unlimited"] and not base["window_enabled"]:
        raise ValueError("This service no longer has a 5-hour limit, so extra tokens would do nothing; "
                         "deny the request instead.")
    new_tokens = row["new_tokens"] if row["new_tokens"] is not None else (row["new_credits"] or 0) * TOKENS_PER_CREDIT
    # An automatically approved amount is a floor under the tier's amount (an administrator's approval is exact).
    automatic = row["resolution_source"] == "automatic"
    if kind == "window":
        limits_db.set_override(user_id, pool, resolved_by, automatic=automatic,
                               window_tokens=_ceil(max(base["window_tokens"], new_tokens)))
    elif kind == "weekly":
        new_weekly = row["new_weekly_tokens"] if row["new_weekly_tokens"] is not None else \
            (row["new_weekly_credits"] or 0) * TOKENS_PER_CREDIT
        limits_db.set_override(user_id, pool, resolved_by, automatic=automatic,
                               weekly_tokens=_ceil(max(base["weekly_tokens"], new_weekly)))
    elif kind == "rate":
        asked = {rule["per"]: int(rule["requests"]) for rule in request_rules(row)}
        rules = []
        for rule in base["rate_rules"]:
            wanted = asked.get(rule["per"], 0)
            if wanted > rule["requests"]:
                burst = max(int(rule["burst"]), math.ceil(rule["burst"] * wanted / rule["requests"]))
                rule = {"requests": wanted, "per": rule["per"], "burst": min(limits_db.BURST_MAX, burst)}
            rules.append(rule)
        limits_db.set_override(user_id, pool, resolved_by, rate_rules=rules)
    elif kind == "temporary":
        now = datetime.now(timezone.utc)
        unlimited = bool(row["grant_unlimited"])
        grant_id = limits_db.create_grant(
            created_by=resolved_by, user_id=user_id, pool=pool, scope=None if unlimited else "window",
            kind="unlimited" if unlimited else "extra", amount=0 if unlimited else new_tokens,
            starts_at=db.timestamp(now), ends_at=db.timestamp(now + timedelta(hours=int(row["grant_hours"]))),
            reason=f"Request #{row['id']}: {row['reason']}"[:limits_db.REASON_MAX])
        db.execute("UPDATE quota_requests SET grant_id=? WHERE id=?", (grant_id, row["id"]))
    else:
        raise ValueError("Unknown kind of request.")


def _apply_model(row, resolved_by: str) -> None:
    """An approved request for one model's own limits: a custom model limit (never lower than now) or a grant."""
    from bananachat.db import limits as limits_db
    from bananachat.services import limits

    kind, user_id = request_kind(row), row["user_id"]
    model = db.one("SELECT * FROM ai_models WHERE id=?", (row["model_id"],))
    if model is None:
        raise ValueError("The model of this request was removed; deny it instead.")
    base = limits.model_base(user_id, model)
    if kind in ("window", "weekly", "rate") and not base[f"{kind}_enabled"]:
        what = {"rate": "request rate", "window": "5-hour", "weekly": "weekly"}[kind]
        raise ValueError(f"This model no longer has a {what} limit; deny the request instead.")
    if kind == "window":
        limits_db.set_model_override(user_id, model["id"], resolved_by,
                                     window_tokens=_ceil(max(base["window_tokens"], row["new_tokens"] or 0)))
    elif kind == "weekly":
        limits_db.set_model_override(user_id, model["id"], resolved_by,
                                     weekly_tokens=_ceil(max(base["weekly_tokens"], row["new_weekly_tokens"] or 0)))
    elif kind == "rate":
        asked = {rule["per"]: int(rule["requests"]) for rule in request_rules(row)}
        rules = []
        for rule in base["rate_rules"]:
            wanted = asked.get(rule["per"], 0)
            if wanted > rule["requests"]:
                burst = max(int(rule["burst"]), math.ceil(rule["burst"] * wanted / rule["requests"]))
                rule = {"requests": wanted, "per": rule["per"], "burst": min(limits_db.BURST_MAX, burst)}
            rules.append(rule)
        limits_db.set_model_override(user_id, model["id"], resolved_by, rate_rules=rules)
    elif kind == "temporary":
        unlimited = bool(row["grant_unlimited"])
        if not unlimited and not base["window_enabled"]:
            raise ValueError("This model no longer has a 5-hour limit, so extra tokens would do nothing; "
                             "deny the request instead.")
        now = datetime.now(timezone.utc)
        grant_id = limits_db.create_grant(
            created_by=resolved_by, user_id=user_id, pool=None, model_id=model["id"],
            scope=None if unlimited else "window", kind="unlimited" if unlimited else "extra",
            amount=0 if unlimited else row["new_tokens"], starts_at=db.timestamp(now),
            ends_at=db.timestamp(now + timedelta(hours=int(row["grant_hours"]))),
            reason=f"Request #{row['id']}: {row['reason']}"[:limits_db.REASON_MAX])
        db.execute("UPDATE quota_requests SET grant_id=? WHERE id=?", (grant_id, row["id"]))
    else:
        raise ValueError("Unknown kind of request.")


def cancel_request(request_id: int, user_id: str) -> bool:
    """The requester withdraws a pending request; its pledges are released. False when it is not pending."""
    from bananachat.services import community

    with db.transaction():
        changed = db.execute("UPDATE quota_requests SET status='cancelled', cancelled_at=?, resolved_at=? "
                             "WHERE id=? AND user_id=? AND status='pending'",
                             (db.now(), db.now(), request_id, user_id)).rowcount == 1
        if changed:
            community.release(request_id)
    return changed


def pending_request(user_id: str):
    return db.one("SELECT r.*, m.display_name AS model_name FROM quota_requests r "
                  "LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.user_id=? AND r.status='pending'", (user_id,))


def user_requests(user_id: str, limit: int = 20):
    return db.query("SELECT r.*, m.display_name AS model_name FROM quota_requests r "
                    "LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.user_id=? ORDER BY r.id DESC LIMIT ?",
                    (user_id, limit))


def list_requests(status: str | None = "pending", limit: int = 200):
    where = "WHERE r.status=?" if status else ""
    params = (status, limit) if status else (limit,)
    return db.query(
        "SELECT r.*, u.username, a.username AS resolved_by_name, m.display_name AS model_name "
        "FROM quota_requests r JOIN users u ON u.id=r.user_id LEFT JOIN users a ON a.id=r.resolved_by "
        "LEFT JOIN ai_models m ON m.id=r.model_id "
        f"{where} ORDER BY r.id DESC LIMIT ?", params)


def resolve_request(request_id: int, admin_id: str, approved: bool, message: str = "") -> None:
    with db.transaction():
        row = db.one("SELECT * FROM quota_requests WHERE id=?", (request_id,))
        if row is None or row["status"] != "pending":
            raise ValueError("This request was already resolved.")
        if approved and row["duration_type"] != "permanent":
            raise ValueError("Temporary requests are no longer supported; deny it instead.")
        if approved:
            _apply(row, admin_id)
        db.execute("UPDATE quota_requests SET status=?, admin_message=?, resolved_at=?, resolved_by=? WHERE id=?",
                   ("approved" if approved else "denied", (message or "")[:1000] or None, db.now(), admin_id,
                    request_id))
        # An administrator's decision needs nobody's tokens: pledges made for it are released.
        from bananachat.services import community
        community.release(request_id)
