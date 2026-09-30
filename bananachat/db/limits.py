"""Storage for the limit system: pool and model policies, per-account settings, custom limits and
per-model limits, reasoning-effort unlocks, tiers, grants, request-rate buckets and usage windows.

Decisions (effective limits, dynamic adjustment, tier promotion, effort
unlocks) live in :mod:`bananachat.services.limits`; this module validates
what it stores and answers queries. Every amount is in **tokens** (prompt +
completion).

Pool policies are JSON documents (``limit_policy.config``, format 2), one
per pool::

    {"version": 2,
     "rate":   {"enabled": true, "rules": [{"requests": 60, "per": "minute", "burst": 10}], "dynamic": false},
     "window": {"enabled": true, "tokens": 30000, "slow_tokens": 15000, "dynamic": false, "auto_tiers": false},
     "weekly": {"enabled": false, "tokens": 150000, "dynamic": false, "auto_tiers": false}}

``window`` is the 5-hour window. A rate rule allows ``requests`` per
``second``, ``minute``, ``hour`` or ``day`` with bursts of up to ``burst``
(default ``requests``); every rule of a policy must pass.

Model policies (``model_limit_policy``) hold the model's weight, whether it
counts toward the pool limits, its own limits (rate rules, 5-hour and weekly
tokens; unset means none) and reasoning-effort default; see
:func:`validate_model_policy`.

Custom limits (``user_limit_overrides``, ``user_model_limits``) hold only the
values an administrator (or an approved request) set; NULL columns follow the
policy.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bananachat import db

POOLS = ("api", "chat", "agent")
SCOPES = ("rate", "window", "weekly")
PERIODS = ("window", "weekly")
SPEEDS = ("slow", "normal", "fast")
GRANT_KINDS = ("unlimited", "multiplier", "extra")
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}
EFFORT_LEVELS = ("off", "low", "medium", "high", "max")
EFFORT_SOURCES = ("admin", "request", "automatic")
PRESETS = ("light", "standard", "heavy")

WINDOW_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86_400
TOKENS_MAX = 1_000_000_000
REQUESTS_MAX = 1_000_000
BURST_MAX = 1_000_000
RULES_MAX = 4
WEIGHT_MIN, WEIGHT_MAX = 0.01, 100.0
SENSITIVITY_MAX = 5.0
MULTIPLIER_MAX = 100.0
TIER_MULTIPLIER_MAX = 100.0
TIERS_MAX = 20
TIER_NAME_MAX = 40
REASON_MAX = 1000

DEFAULT_POLICIES = {
    "api": {"version": 2,
            "rate": {"enabled": True, "rules": [{"requests": 1, "per": "second", "burst": 10}], "dynamic": False},
            "window": {"enabled": True, "tokens": 30_000, "slow_tokens": 15_000, "dynamic": False,
                       "auto_tiers": False},
            "weekly": {"enabled": False, "tokens": 150_000, "dynamic": False, "auto_tiers": False}},
    "chat": {"version": 2,
             "rate": {"enabled": True, "rules": [{"requests": 1, "per": "second", "burst": 5}], "dynamic": False},
             "window": {"enabled": False, "tokens": 100_000, "slow_tokens": 0, "dynamic": False, "auto_tiers": False},
             "weekly": {"enabled": False, "tokens": 500_000, "dynamic": False, "auto_tiers": False}},
    "agent": {"version": 2,
              "rate": {"enabled": True, "rules": [{"requests": 1, "per": "second", "burst": 10}], "dynamic": False},
              "window": {"enabled": False, "tokens": 100_000, "slow_tokens": 0, "dynamic": False,
                         "auto_tiers": False},
              "weekly": {"enabled": False, "tokens": 500_000, "dynamic": False, "auto_tiers": False}},
}

# What the strictness presets fill in (other settings of the model are kept). Heavy is strict.
PRESET_VALUES = {
    "light": {"weight": 0.5, "sensitivity": 0.5, "enabled": False, "rate_rules": [], "window_tokens": None,
              "weekly_tokens": None, "dynamic": False, "effort_default": None},
    "standard": {"weight": 1.0, "sensitivity": 1.0, "enabled": False, "rate_rules": [], "window_tokens": None,
                 "weekly_tokens": None, "dynamic": False, "effort_default": None},
    "heavy": {"weight": 3.0, "sensitivity": 2.0, "enabled": True,
              "rate_rules": [{"requests": 6, "per": "minute", "burst": 3}, {"requests": 200, "per": "day",
                                                                             "burst": 200}],
              "window_tokens": 200_000, "weekly_tokens": None, "dynamic": True, "effort_default": "low"},
}
DEFAULT_MODEL_POLICY = {"preset": "standard", "enabled": False, "weight": 1.0, "counts_toward_pool": None,
                        "rate_rules": [], "window_tokens": None, "weekly_tokens": None, "dynamic": False,
                        "sensitivity": 1.0, "auto_tiers": False, "effort_default": None}


def _check_pool(pool: str) -> None:
    if pool not in POOLS:
        raise ValueError("Unknown credit pool.")


def effort_rank(level) -> int:
    """Position of a reasoning effort level (``on`` counts as ``medium``); -1 for unknown ones."""
    if level == "on":
        level = "medium"
    return EFFORT_LEVELS.index(level) if level in EFFORT_LEVELS else -1


# ----- validation ----------------------------------------------------------------------------

def _number(value, label: str, minimum: float, maximum: float, *, whole: bool = False):
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{label} is required.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum:,g} and {maximum:,g}.")
    if whole:
        if number != int(number):
            raise ValueError(f"{label} must be a whole number.")
        return int(number)
    return number


def _tokens(value, label: str, *, optional: bool = False):
    if optional and value is None:
        return None
    return _number(value, label, 0, TOKENS_MAX, whole=True)


def validate_rule(rule) -> dict:
    """``{"requests", "per", "burst"}``; the burst defaults to the number of requests."""
    if not isinstance(rule, dict):
        raise ValueError("A request-rate rule must be an object.")
    per = rule.get("per")
    if per not in UNITS:
        raise ValueError("A request-rate rule counts requests per second, minute, hour or day.")
    requests = _number(rule.get("requests"), f"Requests per {per}", 1, REQUESTS_MAX, whole=True)
    burst = rule.get("burst")
    burst = requests if burst in (None, "") else _number(burst, "Burst", 1, BURST_MAX, whole=True)
    return {"requests": requests, "per": per, "burst": burst}


def validate_rules(rules) -> list[dict]:
    if rules is None:
        return []
    if not isinstance(rules, (list, tuple)):
        raise ValueError("Request-rate rules must be a list.")
    clean = [validate_rule(rule) for rule in rules]
    if len(clean) > RULES_MAX:
        raise ValueError(f"At most {RULES_MAX} request-rate rules.")
    units = [rule["per"] for rule in clean]
    if len(set(units)) != len(units):
        raise ValueError("Use one request-rate rule per time unit.")
    return sorted(clean, key=lambda rule: UNITS[rule["per"]])


def rule_per_second(rule: dict) -> float:
    return float(rule["requests"]) / UNITS[rule["per"]]


def validate_policy(config: dict) -> dict:
    """A complete, normalised copy of *config*. Raises ValueError with a readable message."""
    if not isinstance(config, dict):
        raise ValueError("A policy must be an object.")
    rate, window, weekly = (config.get(key) or {} for key in SCOPES)
    if not all(isinstance(part, dict) for part in (rate, window, weekly)):
        raise ValueError("A policy has rate, window and weekly sections.")
    rules = validate_rules(rate.get("rules"))
    enabled = bool(rate.get("enabled", True))
    if enabled and not rules:
        raise ValueError("Add at least one request-rate rule, or switch the request rate off.")
    return {
        "version": 2,
        "rate": {"enabled": enabled, "rules": rules, "dynamic": bool(rate.get("dynamic", False))},
        "window": {"enabled": bool(window.get("enabled", False)),
                   "tokens": _tokens(window.get("tokens"), "Tokens per 5 hours"),
                   "slow_tokens": _tokens(window.get("slow_tokens", 0), "Slow tokens per 5 hours"),
                   "dynamic": bool(window.get("dynamic", False)),
                   "auto_tiers": bool(window.get("auto_tiers", False))},
        "weekly": {"enabled": bool(weekly.get("enabled", False)),
                   "tokens": _tokens(weekly.get("tokens"), "Tokens per week"),
                   "dynamic": bool(weekly.get("dynamic", False)),
                   "auto_tiers": bool(weekly.get("auto_tiers", False))},
    }


def upgrade_policy(pool: str, stored) -> dict | None:
    """A stored policy in format 2 (older documents are converted like migration 9 did)."""
    if isinstance(stored, dict) and stored.get("version") == 2:
        return stored
    if not isinstance(stored, dict):
        return None
    from bananachat.db.migrations.v9_limits_tokens import convert_policy

    return convert_policy(pool, stored)


def _merged(pool: str, stored: dict | None) -> dict:
    """The stored policy over the defaults; an unreadable policy falls back to the defaults."""
    config = copy.deepcopy(DEFAULT_POLICIES[pool])
    stored = upgrade_policy(pool, stored)
    if isinstance(stored, dict):
        for scope in SCOPES:
            if isinstance(stored.get(scope), dict):
                config[scope].update(stored[scope])
    try:
        return validate_policy(config)
    except ValueError:
        return copy.deepcopy(DEFAULT_POLICIES[pool])


def _load(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ----- pool policies ------------------------------------------------------------------------

def get_policy(pool: str) -> dict:
    _check_pool(pool)
    return _merged(pool, _load(db.scalar("SELECT config FROM limit_policy WHERE pool=?", (pool,))))


def all_policies() -> dict[str, dict]:
    rows = {row["pool"]: row["config"] for row in db.query("SELECT pool, config FROM limit_policy")}
    return {pool: _merged(pool, _load(rows.get(pool))) for pool in POOLS}


def set_policy(pool: str, config: dict, updated_by: str | None) -> dict:
    _check_pool(pool)
    clean = validate_policy(config)
    db.execute("INSERT INTO limit_policy (pool, config, updated_at, updated_by) VALUES (?,?,?,?) "
               "ON CONFLICT(pool) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at, "
               "updated_by=excluded.updated_by", (pool, json.dumps(clean, sort_keys=True), db.now(), updated_by))
    return clean


# ----- model policies -----------------------------------------------------------------------

def validate_model_policy(config: dict) -> dict:
    """A complete model policy. Unset token amounts (None) mean no model-specific limit."""
    if not isinstance(config, dict):
        raise ValueError("A model policy must be an object.")
    merged = {**DEFAULT_MODEL_POLICY, **config}
    preset = merged.get("preset")
    counts = merged.get("counts_toward_pool")
    effort = merged.get("effort_default")
    if effort in ("", None):
        effort = None
    elif effort not in EFFORT_LEVELS:
        raise ValueError("Choose a valid reasoning effort level.")
    return {
        "preset": preset if preset in (*PRESETS, "custom") else "custom",
        "enabled": bool(merged.get("enabled")),
        "weight": round(_number(merged.get("weight"), "Weight", WEIGHT_MIN, WEIGHT_MAX), 2),
        "counts_toward_pool": None if counts is None else bool(counts),
        "rate_rules": validate_rules(merged.get("rate_rules")),
        "window_tokens": _tokens(merged.get("window_tokens"), "Tokens per 5 hours", optional=True),
        "weekly_tokens": _tokens(merged.get("weekly_tokens"), "Tokens per week", optional=True),
        "dynamic": bool(merged.get("dynamic")),
        "sensitivity": round(_number(merged.get("sensitivity"), "Sensitivity", 0, SENSITIVITY_MAX), 2),
        "auto_tiers": bool(merged.get("auto_tiers")),
        "effort_default": effort,
    }


def preset_policy(preset: str, base: dict | None = None) -> dict:
    """*base* (a model policy) with the values of a strictness preset."""
    if preset not in PRESETS:
        raise ValueError("Choose light, standard or heavy.")
    return validate_model_policy({**(base or DEFAULT_MODEL_POLICY), **copy.deepcopy(PRESET_VALUES[preset]),
                                  "preset": preset})


def _model_default(model) -> dict:
    """The policy of a model nobody configured: its catalog preset (``ai_models.limit_preset``) or standard."""
    preset = None
    if model is not None:
        try:
            preset = model["limit_preset"]
        except (IndexError, KeyError):
            preset = None
    return preset_policy(preset if preset in PRESETS else "standard")


def _model_policy(model, raw) -> dict:
    stored = _load(raw)
    if isinstance(stored, dict):
        try:
            return validate_model_policy(stored)
        except ValueError:
            pass
    return _model_default(model)


def get_model_policy(model) -> dict:
    """The policy of *model* (a catalog row)."""
    raw = db.scalar("SELECT config FROM model_limit_policy WHERE model_id=?", (model["id"],))
    return _model_policy(model, raw)


def model_policies(models) -> dict[int, dict]:
    """Policies of several catalog rows: ``{model_id: policy}`` in one query."""
    stored = {row["model_id"]: row["config"] for row in db.query("SELECT model_id, config FROM model_limit_policy")}
    return {model["id"]: _model_policy(model, stored.get(model["id"])) for model in models}


def has_model_policy(model_id: int) -> bool:
    return db.one("SELECT 1 FROM model_limit_policy WHERE model_id=?", (model_id,)) is not None


def set_model_policy(model_id: int, config: dict, updated_by: str | None) -> dict:
    clean = validate_model_policy(config)
    if not db.one("SELECT 1 FROM ai_models WHERE id=?", (model_id,)):
        raise ValueError("That model does not exist.")
    db.execute("INSERT INTO model_limit_policy (model_id, config, updated_at, updated_by) VALUES (?,?,?,?) "
               "ON CONFLICT(model_id) DO UPDATE SET config=excluded.config, updated_at=excluded.updated_at, "
               "updated_by=excluded.updated_by", (model_id, json.dumps(clean, sort_keys=True), db.now(), updated_by))
    return clean


def clear_model_policy(model_id: int) -> bool:
    return db.execute("DELETE FROM model_limit_policy WHERE model_id=?", (model_id,)).rowcount == 1


# ----- per-user settings ----------------------------------------------------------

@dataclass(frozen=True)
class UserSettings:
    tier_id: int | None = None
    tier_locked: bool = False
    tier_changed_at: str | None = None
    dynamic_exempt: bool = False
    speed: str = "normal"
    usage_reset_at: str | None = None
    weekly_reset_at: str | None = None
    effort_gating_off: bool = False


_USER_FIELDS = ("tier_id", "tier_locked", "tier_changed_at", "dynamic_exempt", "speed", "usage_reset_at",
                "weekly_reset_at", "effort_gating_off")


def user_settings(user_id: str) -> UserSettings:
    row = db.one(f"SELECT {', '.join(_USER_FIELDS)} FROM user_limits WHERE user_id=?", (user_id,))
    if row is None:
        return UserSettings()
    return UserSettings(row["tier_id"], bool(row["tier_locked"]), row["tier_changed_at"], bool(row["dynamic_exempt"]),
                        row["speed"] if row["speed"] in SPEEDS else "normal", row["usage_reset_at"],
                        row["weekly_reset_at"], bool(row["effort_gating_off"]))


def update_user_settings(user_id: str, updated_by: str | None, **values) -> None:
    unknown = set(values) - set(_USER_FIELDS)
    if unknown:
        raise ValueError(f"Unknown limit settings: {', '.join(sorted(unknown))}")
    if "speed" in values and values["speed"] not in SPEEDS:
        raise ValueError("Choose a valid speed.")
    if "tier_id" in values and values["tier_id"] is not None and get_tier(values["tier_id"]) is None:
        raise ValueError("That tier does not exist.")
    for key in ("tier_locked", "dynamic_exempt", "effort_gating_off"):
        if key in values:
            values[key] = int(bool(values[key]))
    columns = ["user_id", *values, "updated_at", "updated_by"]
    params = [user_id, *values.values(), db.now(), updated_by]
    updates = ", ".join(f"{name}=excluded.{name}" for name in columns[1:])
    db.execute(f"INSERT INTO user_limits ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
               f"ON CONFLICT(user_id) DO UPDATE SET {updates}", params)


def speed_of(user_id: str) -> str:
    return db.scalar("SELECT speed FROM user_limits WHERE user_id=?", (user_id,), "normal")


# ----- custom limits of one account (per pool) ----------------------------------------------------

@dataclass(frozen=True)
class Override:
    pool: str
    rate_rules: tuple | None = None
    window_tokens: int | None = None
    window_slow_tokens: int | None = None
    weekly_tokens: int | None = None
    updated_at: str | None = None
    updated_by: str | None = None
    # Amounts set by an automatically approved request: floors under the tier's amount (see set_override).
    automatic: frozenset = frozenset()

    @property
    def has_rate(self) -> bool:
        return self.rate_rules is not None

    @property
    def has_window(self) -> bool:
        return self.window_tokens is not None or self.window_slow_tokens is not None

    @property
    def has_weekly(self) -> bool:
        return self.weekly_tokens is not None

    @property
    def empty(self) -> bool:
        return not (self.has_rate or self.has_window or self.has_weekly)


_OVERRIDE_FIELDS = ("rate_rules", "window_tokens", "window_slow_tokens", "weekly_tokens")


def _rules_column(raw) -> tuple | None:
    rules = _load(raw)
    if not isinstance(rules, list):
        return None
    try:
        return tuple(validate_rules(rules)) or None
    except ValueError:
        return None


def _automatic(raw) -> frozenset:
    return frozenset(name for name in (raw or "").split(",") if name in _OVERRIDE_FIELDS)


def _override(row) -> Override:
    return Override(row["pool"], _rules_column(row["rate_rules"]), row["window_tokens"], row["window_slow_tokens"],
                    row["weekly_tokens"], row["updated_at"], row["updated_by"], _automatic(row["automatic"]))


_OVERRIDE_SELECT = ("SELECT user_id, pool, rate_rules, window_tokens, window_slow_tokens, weekly_tokens, updated_at, "
                    "updated_by, automatic FROM user_limit_overrides")


def get_override(user_id: str, pool: str) -> Override | None:
    row = db.one(f"{_OVERRIDE_SELECT} WHERE user_id=? AND pool=?", (user_id, pool))
    return _override(row) if row else None


def overrides_for(user_id: str) -> dict[str, Override]:
    return {row["pool"]: _override(row) for row in db.query(f"{_OVERRIDE_SELECT} WHERE user_id=?", (user_id,))}


def overrides_of(user_ids) -> dict[tuple[str, str], Override]:
    """Custom limits of several accounts: ``{(user_id, pool): Override}``."""
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    return {(row["user_id"], row["pool"]): _override(row) for row in db.query(
        f"{_OVERRIDE_SELECT} WHERE user_id IN ({','.join('?' for _ in user_ids)})", user_ids)}


def tier_ids_of(user_ids) -> dict[str, int | None]:
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    return {row["user_id"]: row["tier_id"] for row in db.query(
        f"SELECT user_id, tier_id FROM user_limits WHERE user_id IN ({','.join('?' for _ in user_ids)})", user_ids)}


def validate_override(values: dict) -> dict:
    """Check custom limit values (None clears one; an empty rule list clears the rate)."""
    clean = {}
    labels = {"window_tokens": "Tokens per 5 hours", "window_slow_tokens": "Slow tokens per 5 hours",
              "weekly_tokens": "Tokens per week"}
    for name, value in values.items():
        if name not in _OVERRIDE_FIELDS:
            raise ValueError(f"Unknown limit: {name}")
        if name == "rate_rules":
            clean[name] = tuple(validate_rules(value)) or None if value is not None else None
        else:
            clean[name] = _tokens(value, labels[name], optional=True)
    return clean


def set_override(user_id: str, pool: str, updated_by: str | None, *, automatic: bool = False,
                 **values) -> Override | None:
    """Set (or with None, clear) custom limits of *user_id* in *pool*; other values are kept.

    With *automatic* (an automatically approved request) the amounts set are
    floors under the tier's amount; a value set otherwise is exact, unless it
    is the value already there (saving an account's form unchanged keeps it
    automatic).
    """
    _check_pool(pool)
    clean = validate_override(values)
    with db.transaction():
        current = get_override(user_id, pool) or Override(pool)
        merged = {name: clean.get(name, getattr(current, name)) for name in _OVERRIDE_FIELDS}
        if all(value is None for value in merged.values()):
            db.execute("DELETE FROM user_limit_overrides WHERE user_id=? AND pool=?", (user_id, pool))
            return None
        floors = set(current.automatic)
        for name, value in clean.items():
            if value is None or (not automatic and value != getattr(current, name)):
                floors.discard(name)
            elif automatic:
                floors.add(name)
        rules = json.dumps(list(merged["rate_rules"])) if merged["rate_rules"] else None
        db.execute(
            "INSERT INTO user_limit_overrides (user_id, pool, rate_rules, window_tokens, window_slow_tokens, "
            "weekly_tokens, updated_at, updated_by, automatic) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, pool) DO UPDATE SET rate_rules=excluded.rate_rules, "
            "window_tokens=excluded.window_tokens, window_slow_tokens=excluded.window_slow_tokens, "
            "weekly_tokens=excluded.weekly_tokens, updated_at=excluded.updated_at, updated_by=excluded.updated_by, "
            "automatic=excluded.automatic",
            (user_id, pool, rules, merged["window_tokens"], merged["window_slow_tokens"], merged["weekly_tokens"],
             db.now(), updated_by, ",".join(sorted(floors)) or None))
    return get_override(user_id, pool)


def clear_overrides(user_id: str) -> int:
    return db.execute("DELETE FROM user_limit_overrides WHERE user_id=?", (user_id,)).rowcount


def restore_defaults(user_id: str | None, updated_by: str | None) -> int:
    """Remove custom limits (per pool and per model, locks included) and speed settings of one account
    (or everyone when None).

    Tiers, tier locks, dynamic exemptions and reasoning-effort unlocks are
    kept: they are earned or deliberate, not custom limits. Returns the number
    of accounts changed.
    """
    with db.transaction():
        if user_id is None:
            users = {row[0] for row in db.query(
                "SELECT user_id FROM user_limit_overrides UNION SELECT user_id FROM user_model_limits "
                "UNION SELECT user_id FROM user_limits WHERE speed!='normal'")}
            db.execute("DELETE FROM user_limit_overrides")
            db.execute("DELETE FROM user_model_limits")
            db.execute("UPDATE user_limits SET speed='normal', updated_at=?, updated_by=? WHERE speed!='normal'",
                       (db.now(), updated_by))
            return len(users)
        changed = clear_overrides(user_id)
        changed += db.execute("DELETE FROM user_model_limits WHERE user_id=?", (user_id,)).rowcount
        changed += db.execute("UPDATE user_limits SET speed='normal', updated_at=?, updated_by=? "
                              "WHERE user_id=? AND speed!='normal'", (db.now(), updated_by, user_id)).rowcount
        return 1 if changed else 0


def count_custom() -> int:
    return db.scalar("SELECT COUNT(*) FROM (SELECT user_id FROM user_limit_overrides "
                     "UNION SELECT user_id FROM user_model_limits)", default=0)


# ----- custom limits of one account (per model) ---------------------------------------------------

@dataclass(frozen=True)
class ModelOverride:
    model_id: int
    rate_rules: tuple | None = None
    window_tokens: int | None = None
    weekly_tokens: int | None = None
    locked: bool = False
    updated_at: str | None = None

    @property
    def empty(self) -> bool:
        return self.rate_rules is None and self.window_tokens is None and self.weekly_tokens is None and \
            not self.locked


def _model_override(row) -> ModelOverride:
    return ModelOverride(row["model_id"], _rules_column(row["rate_rules"]), row["window_tokens"],
                         row["weekly_tokens"], bool(row["locked"]), row["updated_at"])


def get_model_override(user_id: str, model_id: int) -> ModelOverride | None:
    row = db.one("SELECT * FROM user_model_limits WHERE user_id=? AND model_id=?", (user_id, model_id))
    return _model_override(row) if row else None


def model_overrides_for(user_id: str) -> dict[int, ModelOverride]:
    return {row["model_id"]: _model_override(row)
            for row in db.query("SELECT * FROM user_model_limits WHERE user_id=?", (user_id,))}


def locked_model_ids(user_id: str) -> set[int]:
    return {row[0] for row in db.query("SELECT model_id FROM user_model_limits WHERE user_id=? AND locked=1",
                                       (user_id,))}


def set_model_override(user_id: str, model_id: int, updated_by: str | None, **values) -> ModelOverride | None:
    """Set (None clears) a per-model limit of one account: ``rate_rules``, ``window_tokens``, ``weekly_tokens``,
    ``locked``. Other values are kept."""
    allowed = {"rate_rules", "window_tokens", "weekly_tokens", "locked"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unknown limit: {', '.join(sorted(unknown))}")
    clean = {}
    for name, value in values.items():
        if name == "rate_rules":
            clean[name] = (tuple(validate_rules(value)) or None) if value is not None else None
        elif name == "locked":
            clean[name] = bool(value)
        else:
            clean[name] = _tokens(value, "Tokens per 5 hours" if name == "window_tokens" else "Tokens per week",
                                  optional=True)
    with db.transaction():
        if not db.one("SELECT 1 FROM ai_models WHERE id=?", (model_id,)):
            raise ValueError("That model does not exist.")
        current = get_model_override(user_id, model_id) or ModelOverride(model_id)
        merged = {name: clean.get(name, getattr(current, name)) for name in ("rate_rules", "window_tokens",
                                                                               "weekly_tokens", "locked")}
        if merged["rate_rules"] is None and merged["window_tokens"] is None and merged["weekly_tokens"] is None \
                and not merged["locked"]:
            db.execute("DELETE FROM user_model_limits WHERE user_id=? AND model_id=?", (user_id, model_id))
            return None
        db.execute(
            "INSERT INTO user_model_limits (user_id, model_id, rate_rules, window_tokens, weekly_tokens, locked, "
            "updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id, model_id) DO UPDATE SET "
            "rate_rules=excluded.rate_rules, window_tokens=excluded.window_tokens, "
            "weekly_tokens=excluded.weekly_tokens, locked=excluded.locked, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            (user_id, model_id, json.dumps(list(merged["rate_rules"])) if merged["rate_rules"] else None,
             merged["window_tokens"], merged["weekly_tokens"], int(merged["locked"]), db.now(), updated_by))
    return get_model_override(user_id, model_id)


# ----- reasoning effort -----------------------------------------------------------------------

@dataclass(frozen=True)
class EffortLevel:
    model_id: int | None
    level: str
    pinned: bool = False
    source: str = "admin"
    updated_at: str | None = None


def effort_levels(user_id: str) -> dict[int | None, EffortLevel]:
    """The account's own effort levels: ``{model_id or None (all models): EffortLevel}``."""
    return {row["model_id"]: EffortLevel(row["model_id"], row["level"], bool(row["pinned"]), row["source"],
                                         row["updated_at"])
            for row in db.query("SELECT model_id, level, pinned, source, updated_at FROM user_effort_levels "
                                "WHERE user_id=? AND level IN ('off','low','medium','high','max')", (user_id,))}


def set_effort_level(user_id: str, model_id: int | None, level: str | None, *, pinned: bool = False,
                     source: str = "admin", updated_by: str | None = None) -> None:
    """Set the highest effort level of one account for one model (None: all models); ``level=None`` removes it."""
    if level is not None and level not in EFFORT_LEVELS:
        raise ValueError("Choose a valid reasoning effort level.")
    if source not in EFFORT_SOURCES:
        raise ValueError("Unknown source.")
    with db.transaction():
        if model_id is not None and not db.one("SELECT 1 FROM ai_models WHERE id=?", (model_id,)):
            raise ValueError("That model does not exist.")
        db.execute("DELETE FROM user_effort_levels WHERE user_id=? AND IFNULL(model_id, 0)=?",
                   (user_id, model_id or 0))
        if level is not None:
            db.execute("INSERT INTO user_effort_levels (user_id, model_id, level, pinned, source, updated_at, "
                       "updated_by) VALUES (?,?,?,?,?,?,?)",
                       (user_id, model_id, level, int(bool(pinned)), source, db.now(), updated_by))


def clear_effort_levels(model_id: int | None = None, *, everyone: bool = False) -> int:
    """Remove the effort levels of every account for one model (or for all models with *everyone*)."""
    if everyone:
        return db.execute("DELETE FROM user_effort_levels").rowcount
    return db.execute("DELETE FROM user_effort_levels WHERE IFNULL(model_id, 0)=?", (model_id or 0,)).rowcount


def effort_candidates(since: str, clean_since: str):
    """Per account and model with ledger usage since *since*: active days and tokens, for automatic unlocks.
    Suspended accounts, administrators and accounts suspended after *clean_since* are left out."""
    return db.query(
        "SELECT l.user_id, l.model_id, COUNT(DISTINCT substr(l.created_at, 1, 10)) AS active_days, "
        "SUM(l.tokens_in + l.tokens_out) AS tokens FROM credit_ledger l JOIN users u ON u.id=l.user_id "
        "WHERE l.created_at>=? AND l.model_id IS NOT NULL AND u.role!='admin' AND u.suspended=0 "
        "AND (u.last_suspension_at IS NULL OR u.last_suspension_at<?) GROUP BY l.user_id, l.model_id",
        (since, clean_since))


# ----- tiers --------------------------------------------------------------------------

_TIER_FIELDS = ("name", "multiplier", "min_account_days", "min_active_days", "min_tokens_30d", "clean_days")


def list_tiers():
    return db.query("SELECT * FROM limit_tiers ORDER BY position, id")


def get_tier(tier_id):
    return db.one("SELECT * FROM limit_tiers WHERE id=?", (tier_id,))


def validate_tier(values: dict) -> dict:
    name = " ".join(str(values.get("name") or "").split())
    if not 1 <= len(name) <= TIER_NAME_MAX:
        raise ValueError(f"A tier name has 1-{TIER_NAME_MAX} characters.")
    return {
        "name": name,
        "multiplier": _number(values.get("multiplier"), "Multiplier", 0, TIER_MULTIPLIER_MAX),
        "min_account_days": _number(values.get("min_account_days", 0), "Account age", 0, 3650, whole=True),
        "min_active_days": _number(values.get("min_active_days", 0), "Active days", 0, 30, whole=True),
        "min_tokens_30d": float(_tokens(values.get("min_tokens_30d", 0), "Tokens used")),
        "clean_days": _number(values.get("clean_days", 0), "Days without suspension", 0, 3650, whole=True),
    }


def _tier_values(clean: dict) -> tuple:
    # min_credits_30d is what earlier builds read; it follows the tokens.
    return (*clean.values(), clean["min_tokens_30d"] / 1000)


def create_tier(values: dict) -> int:
    clean = validate_tier(values)
    with db.transaction():
        if db.scalar("SELECT COUNT(*) FROM limit_tiers", default=0) >= TIERS_MAX:
            raise ValueError(f"At most {TIERS_MAX} tiers.")
        position = db.scalar("SELECT MAX(position) FROM limit_tiers", default=-1) + 1
        cursor = db.execute(
            f"INSERT INTO limit_tiers (position, {', '.join(_TIER_FIELDS)}, min_credits_30d, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (position, *_tier_values(clean), db.now(), db.now()))
    return cursor.lastrowid


def update_tier(tier_id: int, values: dict) -> None:
    clean = validate_tier(values)
    assignments = ", ".join(f"{name}=?" for name in _TIER_FIELDS)
    if db.execute(f"UPDATE limit_tiers SET {assignments}, min_credits_30d=?, updated_at=? WHERE id=?",
                  (*_tier_values(clean), db.now(), tier_id)).rowcount != 1:
        raise ValueError("That tier does not exist.")


def move_tier(tier_id: int, direction: int) -> None:
    """Swap a tier with its neighbour (``-1`` up, ``+1`` down) and renumber positions."""
    with db.transaction():
        ids = [row["id"] for row in list_tiers()]
        if tier_id not in ids:
            raise ValueError("That tier does not exist.")
        index = ids.index(tier_id)
        other = index + (1 if direction > 0 else -1)
        if 0 <= other < len(ids):
            ids[index], ids[other] = ids[other], ids[index]
        db.executemany("UPDATE limit_tiers SET position=? WHERE id=?", list(enumerate(ids)))


def delete_tier(tier_id: int) -> None:
    """Delete a tier; its accounts return to the entry tier. The last tier cannot be deleted."""
    with db.transaction():
        if db.scalar("SELECT COUNT(*) FROM limit_tiers", default=0) <= 1:
            raise ValueError("Keep at least one tier.")
        if db.execute("DELETE FROM limit_tiers WHERE id=?", (tier_id,)).rowcount != 1:
            raise ValueError("That tier does not exist.")
        db.execute("UPDATE user_limits SET tier_id=NULL WHERE tier_id=?", (tier_id,))


def tier_counts() -> dict:
    """Accounts per tier id (None: the entry tier by default)."""
    return {row[0]: row[1] for row in db.query(
        "SELECT l.tier_id, COUNT(*) FROM users u LEFT JOIN user_limits l ON l.user_id=u.id "
        "WHERE u.role!='admin' GROUP BY l.tier_id")}


# ----- grants ---------------------------------------------------------------------------

def validate_grant(*, user_id, pool, scope, kind, amount, starts_at: str, ends_at: str | None, reason: str,
                   model_id=None) -> dict:
    if pool is not None:
        _check_pool(pool)
    if model_id is not None:
        if pool is not None:
            raise ValueError("A grant is for one service or for one model, not both.")
        if not db.one("SELECT 1 FROM ai_models WHERE id=?", (model_id,)):
            raise ValueError("That model does not exist.")
    if scope is not None and scope not in SCOPES:
        raise ValueError("Choose a valid scope.")
    if kind not in GRANT_KINDS:
        raise ValueError("Choose a valid kind of grant.")
    if kind == "unlimited":
        amount = 0.0
    elif kind == "multiplier":
        amount = _number(amount, "Multiplier", 1.01, MULTIPLIER_MAX)
    else:
        if scope not in ("window", "weekly"):
            raise ValueError("Extra tokens need one limit (5-hour or weekly tokens); for the request rate use a "
                             "multiplier.")
        amount = _number(amount, "Extra tokens", 1, TOKENS_MAX, whole=True)
    reason = (reason or "").strip()
    if len(reason) > REASON_MAX:
        raise ValueError(f"The reason can be at most {REASON_MAX:,} characters.")
    if ends_at is not None and ends_at <= starts_at:
        raise ValueError("A grant must end after it starts.")
    return {"user_id": user_id, "pool": pool, "model_id": model_id, "scope": scope, "kind": kind,
            "amount": float(amount), "starts_at": starts_at, "ends_at": ends_at, "reason": reason}


def create_grant(*, created_by: str | None, **values) -> int:
    clean = validate_grant(**values)
    cursor = db.execute(
        "INSERT INTO limit_grants (user_id, pool, model_id, scope, kind, amount, starts_at, ends_at, reason, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*clean.values(), created_by, db.now()))
    return cursor.lastrowid


_GRANT_SELECT = ("SELECT g.*, u.username, c.username AS created_by_name, r.username AS revoked_by_name, "
                 "m.display_name AS model_name FROM limit_grants g LEFT JOIN users u ON u.id=g.user_id "
                 "LEFT JOIN users c ON c.id=g.created_by LEFT JOIN users r ON r.id=g.revoked_by "
                 "LEFT JOIN ai_models m ON m.id=g.model_id")


def get_grant(grant_id: int):
    return db.one(f"{_GRANT_SELECT} WHERE g.id=?", (grant_id,))


def revoke_grant(grant_id: int, revoked_by: str | None) -> bool:
    return db.execute("UPDATE limit_grants SET revoked_at=?, revoked_by=? WHERE id=? AND revoked_at IS NULL",
                      (db.now(), revoked_by, grant_id)).rowcount == 1


def active_grants(user_id: str, at: str | None = None):
    """Grants in force now for *user_id* (their own and those for everyone), oldest first."""
    at = at or db.now()
    return db.query(
        "SELECT * FROM limit_grants WHERE (user_id=? OR user_id IS NULL) AND revoked_at IS NULL "
        "AND starts_at<=? AND (ends_at IS NULL OR ends_at>?) ORDER BY id", (user_id, at, at))


def user_grants(user_id: str, at: str | None = None):
    """Active and upcoming grants that affect *user_id*."""
    at = at or db.now()
    return db.query(
        "SELECT g.*, m.display_name AS model_name FROM limit_grants g LEFT JOIN ai_models m ON m.id=g.model_id "
        "WHERE (g.user_id=? OR g.user_id IS NULL) AND g.revoked_at IS NULL "
        "AND (g.ends_at IS NULL OR g.ends_at>?) ORDER BY g.starts_at, g.id", (user_id, at))


GRANT_STATES = ("active", "upcoming", "ended")


def list_grants(state: str = "active", *, user_id: str | None = None, limit: int = 200):
    at = db.now()
    if state == "active":
        where, params = "g.revoked_at IS NULL AND g.starts_at<=? AND (g.ends_at IS NULL OR g.ends_at>?)", [at, at]
    elif state == "upcoming":
        where, params = "g.revoked_at IS NULL AND g.starts_at>?", [at]
    elif state == "ended":
        where, params = "(g.revoked_at IS NOT NULL OR (g.ends_at IS NOT NULL AND g.ends_at<=?))", [at]
    else:
        raise ValueError("Unknown grant state.")
    if user_id is not None:
        where += " AND (g.user_id=? OR g.user_id IS NULL)"
        params.append(user_id)
    order = "g.id DESC" if state == "ended" else "g.starts_at, g.id"
    return db.query(f"{_GRANT_SELECT} WHERE {where} ORDER BY {order} LIMIT ?", (*params, limit))


def count_active_grants() -> int:
    at = db.now()
    return db.scalar("SELECT COUNT(*) FROM limit_grants WHERE revoked_at IS NULL AND starts_at<=? "
                     "AND (ends_at IS NULL OR ends_at>?)", (at, at), 0)


# ----- request-rate buckets ---------------------------------------------------------

def bucket_key(scope: str, user_id: str, rule: str) -> str:
    """One bucket per account, scope (a pool, or ``model<id>``) and configured rule (``60/minute/10``): every
    surface of the account (API keys, playground, chat, agents) shares them. A changed rule starts full."""
    return f"{scope}:{user_id}:{rule}"


def take_tokens(buckets, now: float) -> tuple[bool, list[tuple[float, float]]]:
    """Take one request from several token buckets at once, shared by every process.

    *buckets* holds ``(key, per_second, burst)``. Each bucket starts full and
    refills at *per_second* up to *burst*. The request is allowed only when
    every bucket has a whole token; then one is taken from each (otherwise
    none). Returns ``(allowed, [(tokens_left, retry_after_seconds), ...])``.
    """
    buckets = list(buckets)
    with db.transaction():
        levels, stamps = [], []
        for key, per_second, burst in buckets:
            row = db.one("SELECT tokens, updated_at FROM rate_buckets WHERE key=?", (key,))
            if row is None:
                tokens, stamp = float(burst), now
            else:
                # Another process may have written a later time while this one waited for the lock: never move
                # the bucket back, or the next request would refill the same interval twice.
                stamp = max(now, float(row["updated_at"]))
                tokens = min(float(burst), float(row["tokens"]) + (stamp - float(row["updated_at"])) * per_second)
            levels.append(tokens)
            stamps.append(stamp)
        allowed = all(tokens >= 1.0 for tokens in levels)
        if allowed:
            levels = [tokens - 1.0 for tokens in levels]
        db.executemany("INSERT INTO rate_buckets (key, tokens, updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET tokens=excluded.tokens, updated_at=excluded.updated_at",
                       [(key, tokens, stamp) for (key, _rate, _burst), tokens, stamp
                        in zip(buckets, levels, stamps, strict=True)])
    result = [(tokens, 0.0 if tokens >= 1.0 or allowed else (stamp - now) + (1.0 - tokens) / per_second)
              for (_key, per_second, _burst), tokens, stamp in zip(buckets, levels, stamps, strict=True)]
    return allowed, result


def take_token(key: str, per_second: float, burst: int, now: float) -> tuple[bool, float, float]:
    """One bucket: ``(allowed, tokens_left, retry_after_seconds)``."""
    allowed, [(tokens, retry)] = take_tokens([(key, per_second, burst)], now)
    return allowed, tokens, retry


def clear_buckets(user_id: str | None = None) -> None:
    if user_id is None:
        db.execute("DELETE FROM rate_buckets")
    else:
        pattern = user_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        db.execute("DELETE FROM rate_buckets WHERE key LIKE ? ESCAPE '\\'", (f"%:{pattern}:%",))


def custom_rule_sets() -> list[list[dict]]:
    """Every custom rate-rule list (per pool and per model), to know how long buckets take to refill."""
    rows = db.query("SELECT rate_rules FROM user_limit_overrides WHERE rate_rules IS NOT NULL UNION ALL "
                    "SELECT rate_rules FROM user_model_limits WHERE rate_rules IS NOT NULL")
    return [list(rules) for rules in (_rules_column(row[0]) for row in rows) if rules]


def purge_buckets(idle_seconds: float, now: float) -> int:
    """Forget buckets idle long enough to be full again (a missing bucket counts as full)."""
    return db.execute("DELETE FROM rate_buckets WHERE updated_at<?", (now - idle_seconds,)).rowcount


# ----- usage windows ---------------------------------------------------------------------

_FORMAT = "%Y-%m-%d %H:%M:%S"


def pool_scope(pool: str) -> str:
    return f"pool:{pool}"


def model_scope(model_id: int) -> str:
    return f"model:{model_id}"


def open_windows(user_id: str, scopes, now: datetime | None = None) -> None:
    """A counted request: open the 5-hour and weekly windows of *scopes* that are not open (joins a transaction).

    A window opens at the first counted request when none is open and lasts
    5 hours (7 days for the week); usage inside counts until it ends.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = timestamp(now)
    window_cutoff = timestamp(now - timedelta(seconds=WINDOW_SECONDS))
    week_cutoff = timestamp(now - timedelta(seconds=WEEK_SECONDS))
    db.executemany(
        "INSERT INTO limit_windows (user_id, scope, window_started_at, week_started_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, scope) DO UPDATE SET "
        "window_started_at=CASE WHEN window_started_at IS NULL OR window_started_at<=? "
        "THEN excluded.window_started_at ELSE window_started_at END, "
        "week_started_at=CASE WHEN week_started_at IS NULL OR week_started_at<=? "
        "THEN excluded.week_started_at ELSE week_started_at END",
        [(user_id, scope, stamp, stamp, window_cutoff, week_cutoff) for scope in scopes])


def windows(user_id: str, scopes) -> dict[str, tuple[str | None, str | None]]:
    """``{scope: (window_started_at, week_started_at)}`` as stored (open or not)."""
    scopes = list(scopes)
    if not scopes:
        return {}
    return {row["scope"]: (row["window_started_at"], row["week_started_at"]) for row in db.query(
        f"SELECT scope, window_started_at, week_started_at FROM limit_windows WHERE user_id=? "
        f"AND scope IN ({','.join('?' for _ in scopes)})", (user_id, *scopes))}


def all_windows(user_id: str) -> dict[str, tuple[str | None, str | None]]:
    """Every stored window of one account (its pools and the models it used), in one query."""
    return {row["scope"]: (row["window_started_at"], row["week_started_at"]) for row in db.query(
        "SELECT scope, window_started_at, week_started_at FROM limit_windows WHERE user_id=?", (user_id,))}


def close_windows(user_id: str | None, *, weekly: bool) -> None:
    """End the open 5-hour windows (and weekly ones) of one account or everyone; the next request opens new ones."""
    assignment = "window_started_at=NULL" + (", week_started_at=NULL" if weekly else "")
    if user_id is None:
        db.execute(f"UPDATE limit_windows SET {assignment}")
    else:
        db.execute(f"UPDATE limit_windows SET {assignment} WHERE user_id=?", (user_id,))


def global_resets() -> tuple[str | None, str | None]:
    row = db.one("SELECT limits_usage_reset_at, limits_weekly_reset_at FROM site_settings WHERE id=1")
    return (row[0], row[1]) if row else (None, None)


def reset_usage(user_id: str | None, *, weekly: bool, updated_by: str | None) -> None:
    """Start counting afresh from now (the ledger is kept): the 5-hour windows close, and with *weekly* the
    weekly ones too. The next request opens new windows."""
    now = db.now()
    with db.transaction():
        if user_id is None:
            column = "limits_weekly_reset_at" if weekly else "limits_usage_reset_at"
            db.execute(f"UPDATE site_settings SET {column}=? WHERE id=1", (now,))
        else:
            update_user_settings(user_id, updated_by, **({"weekly_reset_at": now} if weekly else
                                                          {"usage_reset_at": now}))
        close_windows(user_id, weekly=weekly)
        clear_buckets(user_id)


def _types(pool: str) -> tuple[str, ...]:
    from bananachat.db.credits import POOL_TYPES
    return POOL_TYPES[pool]


def usage(user_id: str, pool: str, window_since: str | None, weekly_since: str | None) -> tuple[float, float, float]:
    """``(5-hour regular, 5-hour slow, weekly total)`` tokens counted against *pool* since the given times
    (None: the window is not open, nothing counts).

    The ledger's ``credits_used`` holds counted tokens / 1,000 (tokens × the
    model's weight). Pending image reservations count as used (API pool).
    """
    types = _types(pool)
    placeholders = ",".join("?" for _ in types)
    starts = [value for value in (window_since, weekly_since) if value]
    if not starts and pool != "api":
        return 0.0, 0.0, 0.0
    never = "9999-12-31 23:59:59"
    # One query: the ledger since the windows opened, and (API pool) the pending image reservations.
    reserved = ("(SELECT SUM(CASE WHEN is_slow=0 THEN credits_reserved ELSE 0 END), "
                "SUM(CASE WHEN is_slow=1 THEN credits_reserved ELSE 0 END) FROM image_credit_reservations "
                "WHERE user_id=?)" if pool == "api" else "(SELECT 0, 0)")
    row = db.one(
        "SELECT l.*, r.* FROM (SELECT SUM(CASE WHEN created_at>=? AND is_slow=0 THEN credits_used ELSE 0 END), "
        "SUM(CASE WHEN created_at>=? AND is_slow=1 THEN credits_used ELSE 0 END), "
        "SUM(CASE WHEN created_at>=? THEN credits_used ELSE 0 END) "
        f"FROM credit_ledger WHERE user_id=? AND created_at>=? AND request_type IN ({placeholders})) l, {reserved} r",
        (window_since or never, window_since or never, weekly_since or never, user_id, min(starts, default=never),
         *types, *((user_id,) if pool == "api" else ())))
    regular, slow, weekly, reserved_regular, reserved_slow = (float(value or 0) * 1000 for value in row)
    return regular + reserved_regular, slow + reserved_slow, weekly + reserved_regular + reserved_slow


def model_usage(user_id: str, model_id: int, window_since: str | None,
                weekly_since: str | None) -> tuple[float, float]:
    """``(5-hour, weekly)`` tokens (prompt + completion, not weighted) of one account with one model, in every
    service, since the given times (None: that window is not open)."""
    starts = [value for value in (window_since, weekly_since) if value]
    never = "9999-12-31 23:59:59"
    # One query: the ledger since the windows opened, and the model's pending image reservations.
    row = db.one(
        "SELECT SUM(CASE WHEN created_at>=? THEN tokens_in + tokens_out ELSE 0 END), "
        "SUM(CASE WHEN created_at>=? THEN tokens_in + tokens_out ELSE 0 END), "
        "(SELECT SUM(credits_reserved) FROM image_credit_reservations WHERE user_id=? AND model_id=?) "
        "FROM credit_ledger WHERE user_id=? AND model_id=? AND created_at>=?",
        (window_since or never, weekly_since or never, user_id, model_id, user_id, model_id,
         min(starts, default=never)))
    window, weekly, reserved = (float(value or 0) for value in row)
    return window + reserved * 1000, weekly + reserved * 1000


# ----- activity statistics (tiers and dynamic limits) --------------------------------------
# *types* are the request types that count (``services.limits.counted_types``); none means no activity.
# Amounts are counted tokens.

def _in(types) -> str:
    return ",".join("?" for _ in types) or "NULL"


def user_activity(user_id: str, since: str, types=()):
    """Counted tokens per (UTC day, hour) of one account since *since*, in the given request types."""
    return db.query(
        "SELECT substr(created_at, 1, 10) AS day, CAST(substr(created_at, 12, 2) AS INTEGER) AS hour, "
        "SUM(credits_used) * 1000 AS tokens FROM credit_ledger WHERE user_id=? AND created_at>=? "
        f"AND request_type IN ({_in(types)}) GROUP BY day, hour", (user_id, since, *types))


def site_hourly(since: str) -> dict[int, float]:
    """Site-wide counted tokens per UTC hour of day since *since*."""
    return {int(row[0]): float(row[1] or 0) * 1000 for row in db.query(
        "SELECT CAST(substr(created_at, 12, 2) AS INTEGER) AS hour, SUM(credits_used) FROM credit_ledger "
        "WHERE created_at>=? GROUP BY hour", (since,))}


def active_people(since: str) -> int:
    """Distinct accounts with requests since *since* (people using the site right now)."""
    return int(db.scalar("SELECT COUNT(DISTINCT user_id) FROM credit_ledger WHERE created_at>=?", (since,), 0) or 0)


def promotion_candidates(since: str, types=()):
    """Non-administrator accounts with what tier promotion looks at, in one query."""
    return db.query(
        "SELECT u.id, u.username, u.created_at, u.suspended, u.suspended_until, u.last_suspension_at, "
        "l.tier_id, COALESCE(l.tier_locked, 0) AS tier_locked, "
        "COALESCE(a.active_days, 0) AS active_days, COALESCE(a.tokens, 0) AS tokens "
        "FROM users u LEFT JOIN user_limits l ON l.user_id=u.id "
        "LEFT JOIN (SELECT user_id, COUNT(DISTINCT substr(created_at, 1, 10)) AS active_days, "
        "SUM(credits_used) * 1000 AS tokens FROM credit_ledger WHERE created_at>=? "
        f"AND request_type IN ({_in(types)}) GROUP BY user_id) a ON a.user_id=u.id "
        "WHERE u.role!='admin'", (since, *types))


def activity_summary(user_id: str, since: str, types=()) -> tuple[int, float]:
    """``(active days, counted tokens)`` of one account since *since*."""
    row = db.one("SELECT COUNT(DISTINCT substr(created_at, 1, 10)), SUM(credits_used) FROM credit_ledger "
                 f"WHERE user_id=? AND created_at>=? AND request_type IN ({_in(types)})",
                 (user_id, since, *types))
    return (int(row[0] or 0), float(row[1] or 0) * 1000) if row else (0, 0.0)


def timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_FORMAT)
