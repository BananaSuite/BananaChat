"""Version 16: one token allowance per five-hour window, and optional weekly limits.

Active regular and slow allowances are combined within a unified two-billion
token maximum. Tier-adjusted account overrides can retain larger derived totals.
When the old global slow allowance was disabled, only the
regular amount survives. Policy format 3 records that conversion. Existing
schema columns remain, with deprecated allowance values zeroed; usage ledger
and image-reservation history, including ``is_slow``, are never rewritten.

An inherited override component uses the account's current tier. The combined
override is an automatic floor only when all its explicit active components
were automatic; an explicit administrator limit stays exact. Both inherited
components remain inherited. Requests retain their state, metadata and votes.

This migration is frozen and safe to run again. Do not import live defaults or
validators: later product changes must not alter an existing upgrade.
"""

import copy
import json
import math

from sqlite_migrations import add_columns

OLD_TOKENS_MAX = 1_000_000_000
TOKENS_MAX = 2_000_000_000
OVERRIDE_WINDOW_MAX = 200_000_000_000
TOKENS_PER_CREDIT = 1000
_WINDOW_DEFAULTS = {"api": (30_000, 15_000), "chat": (100_000, 0), "agent": (100_000, 0)}
_AUTOMATIC_FIELDS = frozenset({"rate_rules", "window_tokens", "weekly_tokens"})


def _amount(value, fallback=0, *, factor=1, maximum=TOKENS_MAX):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(fallback)
    if not math.isfinite(number):
        number = float(fallback)
    return math.ceil(max(0, min(maximum, number * factor)))


def _total(*amounts, maximum=TOKENS_MAX):
    return math.ceil(min(maximum, sum(amounts)))


def _rows(conn, sql):
    cursor = conn.execute(sql)
    names = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(names, row, strict=True))


def _from_tokens_or_credits(row, tokens, credits, default=0):
    value = row.get(tokens)
    return _amount(value) if value is not None else _amount(row.get(credits), default, factor=TOKENS_PER_CREDIT)


def _policies(conn, slow_enabled):
    old_windows = {}
    for row in _rows(conn, "SELECT pool, config FROM limit_policy"):
        pool = row["pool"]
        try:
            config = json.loads(row["config"])
        except (TypeError, ValueError, RecursionError):
            config = None
        if not isinstance(config, dict) or config.get("version") not in (2, 3):
            # Version 9's released converter contains its own frozen defaults.
            from .v9_limits_tokens import convert_policy
            config = convert_policy(pool, config)
        config = copy.deepcopy(config)
        regular_default, slow_default = _WINDOW_DEFAULTS.get(pool, _WINDOW_DEFAULTS["chat"])
        stored_window = config.get("window")
        window = stored_window if isinstance(stored_window, dict) else {}
        maximum = TOKENS_MAX if config.get("version") == 3 else OLD_TOKENS_MAX
        regular = _amount(window.get("tokens"), regular_default, maximum=maximum)
        slow = (_amount(window.get("slow_tokens"), slow_default, maximum=OLD_TOKENS_MAX)
                if slow_enabled and config.get("version") != 3 else 0)
        old_windows[pool] = (regular, slow, bool(window.get("auto_tiers")))
        window = {**window, "tokens": _total(regular, slow)}
        window.pop("slow_tokens", None)
        config.update(version=3, window=window)
        conn.execute("UPDATE limit_policy SET config=? WHERE pool=?", (json.dumps(config, sort_keys=True), pool))
    return old_windows


def _tier_factors(conn):
    tiers = list(_rows(conn, "SELECT id, multiplier FROM limit_tiers ORDER BY position, id"))
    factors = {row["id"]: _factor(row["multiplier"]) for row in tiers}
    default = factors[tiers[0]["id"]] if tiers else 1.0
    return {row["user_id"]: factors.get(row["tier_id"], default)
            for row in _rows(conn, "SELECT user_id, tier_id FROM user_limits")}, default


def _factor(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return max(0.0, min(100.0, number)) if math.isfinite(number) else 1.0


def _overrides(conn, old_windows, slow_enabled):
    factors, entry_factor = _tier_factors(conn)
    for row in _rows(conn, "SELECT * FROM user_limit_overrides"):
        regular, slow, tiered = old_windows.get(row["pool"], (100_000, 0, False))
        factor = factors.get(row["user_id"], entry_factor) if tiered else 1.0
        # The old service floors each tier-derived allowance to whole tokens.
        defaults = tuple(math.floor(min(OVERRIDE_WINDOW_MAX, amount * factor) + 1e-6)
                         for amount in (regular, slow))
        components = [("window_tokens", row["window_tokens"], defaults[0])]
        if slow_enabled:
            components.append(("window_slow_tokens", row["window_slow_tokens"], defaults[1]))
        automatic = set((row["automatic"] or "").split(","))
        explicit = [(name, value) for name, value, _default in components if value is not None]
        total = None
        if explicit:
            amounts = []
            for name, value, default in components:
                amount = _amount(value, maximum=OVERRIDE_WINDOW_MAX)
                amounts.append(default if value is None else max(amount, default) if name in automatic else amount)
            total = _total(*amounts, maximum=OVERRIDE_WINDOW_MAX)
        floors = automatic & (_AUTOMATIC_FIELDS - {"window_tokens"})
        if explicit and all(name in automatic for name, _value in explicit):
            floors.add("window_tokens")
        conn.execute("UPDATE user_limit_overrides SET window_tokens=?, window_slow_tokens=0, "
                     "daily_slow_credits=0, automatic=? WHERE user_id=? AND pool=?",
                     (total, ",".join(sorted(floors)) or None, row["user_id"], row["pool"]))
    conn.execute("DELETE FROM user_limit_overrides WHERE window_tokens IS NULL AND weekly_tokens IS NULL "
                 "AND (rate_rules IS NULL OR rate_rules='' OR rate_rules='[]')")
    conn.execute("UPDATE user_quota SET daily_slow_credits=0")


def _requests(conn, slow_enabled):
    for row in _rows(conn, "SELECT * FROM quota_requests"):
        if row["kind"] not in ("daily", "window"):
            continue
        regular = _from_tokens_or_credits(row, "new_tokens", "new_credits")
        slow = _from_tokens_or_credits(row, "new_slow_tokens", "new_slow_credits") if slow_enabled else 0
        total = _total(regular, slow)
        conn.execute("UPDATE quota_requests SET new_tokens=?, new_credits=? WHERE id=?",
                     (total, math.ceil(total / TOKENS_PER_CREDIT), row["id"]))
    conn.execute("UPDATE quota_requests SET new_slow_tokens=0, new_slow_credits=0")


def _settings(conn, row, slow_enabled):
    automatic = _from_tokens_or_credits(row, "quota_auto_approve_max_tokens", "quota_auto_approve_max_credits")
    fixed = _from_tokens_or_credits(row, "music_bonus_fixed_tokens", "music_bonus_fixed_credits", 30)
    weekly = _amount(row.get("music_bonus_fixed_weekly_tokens"), fixed * 7, maximum=7 * TOKENS_MAX)
    if slow_enabled:
        automatic = _total(automatic, _from_tokens_or_credits(
            row, "quota_auto_approve_max_slow_tokens", "quota_auto_approve_max_slow_credits"))
        fixed = _total(fixed, _from_tokens_or_credits(
            row, "music_bonus_fixed_slow_tokens", "music_bonus_fixed_slow", 15))
    conn.execute("UPDATE site_settings SET quota_auto_approve_max_tokens=?, music_bonus_fixed_tokens=?, "
                 "music_bonus_fixed_weekly_tokens=?, "
                 "slow_credits_enabled=0, default_slow_credits=0, chat_daily_slow_credits=0, "
                 "quota_auto_approve_max_slow_tokens=0, quota_auto_approve_max_slow_credits=0, "
                 "music_bonus_fixed_slow_tokens=0, music_bonus_fixed_slow=0 WHERE id=1", (automatic, fixed, weekly))


def upgrade(conn):
    add_columns(conn, "site_settings", {"music_bonus_fixed_weekly_tokens": "INTEGER"})
    settings = next(_rows(conn, "SELECT * FROM site_settings WHERE id=1"), {})
    slow_enabled = bool(settings.get("slow_credits_enabled", 1))
    old_windows = _policies(conn, slow_enabled)
    _overrides(conn, old_windows, slow_enabled)
    _requests(conn, slow_enabled)
    _settings(conn, settings, slow_enabled)
