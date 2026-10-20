"""Storage for the Claude and Codex subscription pools.

Each ``subscription_accounts`` row is one configured account whose 5-hour and
weekly usage the pool tracks. Decisions (which account serves a request, how
remaining quota shrinks model limits) live in
:mod:`bananachat.services.subscription_pool`; this module only validates and
stores. Budgets are in **tokens** (prompt + completion); caps are whole
percentages of the provider's own allowance.
"""

from __future__ import annotations

import math
import time

from bananachat import db

PROVIDERS = ("claude", "codex")
STATUSES = ("active", "disabled", "error")
QUOTA_SOURCES = ("local", "reported")
TOKENS_MAX = 1_000_000_000
LEASE_SECONDS = 90
_UNSET = object()


def _invalidate_pool_cache(provider: str | None = None) -> None:
    try:
        from bananachat.services import subscription_pool as _pool
    except ImportError:  # the cache is best-effort; a stale entry expires on its own
        return
    if provider is None:
        _pool._pool_cache.clear()
    else:
        _pool._pool_cache.pop(provider, None)


def _clean_label(label: str) -> str:
    label = " ".join((label or "").split())[:80]
    if not label:
        raise ValueError("Name the subscription account (e.g. claude-1).")
    return label


def _clean_family(family: str) -> str:
    family = (family or "").strip().lower()
    if not family or len(family) > 40 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in family):
        raise ValueError("A model family is a short name such as fable or opus.")
    return family


def _tokens(value, label: str):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(number) or not 0 <= number <= TOKENS_MAX:
        raise ValueError(f"{label} must be between 0 and {TOKENS_MAX:,}.")
    if number != int(number):
        raise ValueError(f"{label} must be a whole number.")
    return int(number)


def _percent(value, label: str):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number of percent.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a whole number of percent.") from None
    if not math.isfinite(number) or number != int(number) or not 1 <= number <= 100:
        raise ValueError(f"{label} must be a whole number between 1 and 100.")
    return int(number)


def list_accounts(*, provider: str | None = None, include_disabled: bool = True):
    clauses = []
    params: list = []
    if not include_disabled:
        clauses.append("status='active'")
    if provider is not None:
        clauses.append("provider=?")
        params.append(provider)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return db.query(f"SELECT * FROM subscription_accounts {where} ORDER BY priority, id", tuple(params))


def get(account_id: int):
    return db.one("SELECT * FROM subscription_accounts WHERE id=?", (account_id,))


def get_by_label(label: str):
    return db.one("SELECT * FROM subscription_accounts WHERE label=?", ((label or "").strip(),))


def add_account(label: str, *, provider: str = "claude", priority: int = 0, note: str = "",
                window_limit=None, weekly_limit=None, window_cap_pct=None, weekly_cap_pct=None) -> int:
    if provider not in PROVIDERS:
        raise ValueError("Choose a subscription provider.")
    label = _clean_label(label)
    try:
        priority = int(priority or 0)
    except (TypeError, ValueError):
        raise ValueError("Priority must be a whole number.") from None
    if get_by_label(label) is not None:
        raise ValueError(f"There is already a subscription account called {label}.")
    cursor = db.execute(
        "INSERT INTO subscription_accounts (provider, label, status, priority, note, window_limit, "
        "weekly_limit, window_cap_pct, weekly_cap_pct, created_at, updated_at) "
        "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, label, max(-100, min(100, priority)), (note or "")[:500],
         _tokens(window_limit, "5-hour tokens"), _tokens(weekly_limit, "Weekly tokens"),
         _percent(window_cap_pct, "The 5-hour cap"), _percent(weekly_cap_pct, "The weekly cap"),
         db.now(), db.now()))
    _invalidate_pool_cache(provider)
    return cursor.lastrowid


def update_account(account_id: int, *, label=None, status=None, priority=None, note=None,
                   window_limit=_UNSET, weekly_limit=_UNSET, window_cap_pct=_UNSET, weekly_cap_pct=_UNSET) -> None:
    row = get(account_id)
    if row is None:
        raise ValueError("That subscription account does not exist.")
    values: dict = {}
    if label is not None:
        label = _clean_label(label)
        if label != row["label"] and get_by_label(label) is not None:
            raise ValueError(f"There is already a subscription account called {label}.")
        values["label"] = label
    if status is not None:
        if status not in STATUSES:
            raise ValueError("Unknown status.")
        values["status"] = status
    if priority is not None:
        try:
            values["priority"] = max(-100, min(100, int(priority)))
        except (TypeError, ValueError):
            raise ValueError("Priority must be a whole number.") from None
    if note is not None:
        values["note"] = (note or "")[:500]
    if window_limit is not _UNSET:
        values["window_limit"] = _tokens(window_limit, "5-hour tokens")
    if weekly_limit is not _UNSET:
        values["weekly_limit"] = _tokens(weekly_limit, "Weekly tokens")
    if window_cap_pct is not _UNSET:
        values["window_cap_pct"] = _percent(window_cap_pct, "The 5-hour cap")
    if weekly_cap_pct is not _UNSET:
        values["weekly_cap_pct"] = _percent(weekly_cap_pct, "The weekly cap")
    if not values:
        return
    values["updated_at"] = db.now()
    assignments = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE subscription_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache(row["provider"])


def set_scoped_cap(account_id: int, family: str, *, cap_pct=_UNSET, window_cap=_UNSET, weekly_cap=_UNSET) -> None:
    """Set or clear an account's per-family ceilings: a share of the provider's allowance, or token budgets."""
    family = _clean_family(family)
    row = get(account_id)
    if row is None:
        raise ValueError("That subscription account does not exist.")
    current = db.one("SELECT cap_pct, window_cap, weekly_cap FROM subscription_account_caps "
                     "WHERE account_id=? AND family=?", (account_id, family))
    values = {name: current[name] if current else None for name in ("cap_pct", "window_cap", "weekly_cap")}
    if cap_pct is not _UNSET:
        values["cap_pct"] = _percent(cap_pct, "The family cap")
    if window_cap is not _UNSET:
        values["window_cap"] = _tokens(window_cap, "5-hour cap")
    if weekly_cap is not _UNSET:
        values["weekly_cap"] = _tokens(weekly_cap, "Weekly cap")
    with db.transaction():
        if all(value is None for value in values.values()):
            db.execute("DELETE FROM subscription_account_caps WHERE account_id=? AND family=?",
                       (account_id, family))
        elif current is None:
            db.execute("INSERT INTO subscription_account_caps (account_id, family, cap_pct, window_cap, weekly_cap) "
                       "VALUES (?, ?, ?, ?, ?)", (account_id, family, *values.values()))
        else:
            db.execute("UPDATE subscription_account_caps SET cap_pct=?, window_cap=?, weekly_cap=? "
                       "WHERE account_id=? AND family=?", (*values.values(), account_id, family))
    _invalidate_pool_cache(row["provider"])


def scoped_caps(account_id: int) -> dict:
    """Return the account's per-family caps as ``{family: {cap_pct, window_cap, weekly_cap}}``."""
    rows = db.query("SELECT family, cap_pct, window_cap, weekly_cap FROM subscription_account_caps "
                    "WHERE account_id=? ORDER BY family", (account_id,))
    return {row["family"]: {"cap_pct": row["cap_pct"], "window_cap": row["window_cap"],
                            "weekly_cap": row["weekly_cap"]} for row in rows}


def remove_account(account_id: int) -> bool:
    with db.transaction():
        if leased(account_id):
            raise ValueError("This account is answering a request. Disable it and wait before removing it.")
        removed = db.execute("DELETE FROM subscription_accounts WHERE id=?", (account_id,)).rowcount == 1
    if removed:
        _invalidate_pool_cache()
    return removed


def report_quota(account_id: int, *, window_used=None, window_limit=None, window_resets_at=None,
                 weekly_used=None, weekly_limit=None, weekly_resets_at=None, error: str = "") -> None:
    """Store a validated quota observation (None leaves a column alone).

    Failed observations do not renew the last successful snapshot or reactivate
    disabled accounts. Token amounts are estimates supplied by the adapter;
    they are not a claim about the provider's subscription billing units.
    """
    with db.transaction():
        row = get(account_id)
        if row is None:
            raise ValueError("That subscription account does not exist.")
        values: dict = {"last_checked_at": db.now(), "last_error": (error or "")[:500],
                        "updated_at": db.now()}
        for name, value in (("window_used", window_used), ("window_limit", window_limit),
                            ("weekly_used", weekly_used), ("weekly_limit", weekly_limit)):
            if value is not None:
                values[name] = _tokens(value, name.replace("_", " ").capitalize())
        reported = any(value is not None for value in (window_used, window_limit, window_resets_at,
                                                       weekly_used, weekly_limit, weekly_resets_at))
        for name, value in (("window_resets_at", window_resets_at), ("weekly_resets_at", weekly_resets_at)):
            if value is not None:
                try:
                    moment = db.parse_timestamp(value)
                except (ValueError, OverflowError, OSError):
                    moment = None
                if moment is None:
                    raise ValueError(f"{name.replace('_', ' ').capitalize()} must be a valid date and time.")
                values[name] = db.timestamp(moment)
        if error and row["status"] != "disabled":
            values["status"] = "error"
        elif not error and reported and row["status"] == "error":
            values["status"] = "active"
        if reported and not error:
            values.update(quota_source="reported", quota_updated_at=db.now())
            # A reset or a report from another scope cannot renew stale usage.
            if window_used is not None:
                values["quota_window_updated_at"] = db.now()
            if weekly_used is not None:
                values["quota_weekly_updated_at"] = db.now()
        if error:
            values = {name: value for name, value in values.items()
                      if name in ("last_checked_at", "last_error", "updated_at", "status")}
        assignments = ", ".join(f"{name}=?" for name in values)
        db.execute(f"UPDATE subscription_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache(row["provider"])


def leased(account_id: int, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.one("SELECT 1 FROM subscription_account_leases WHERE account_id=? AND expires_at>?",
                  (account_id, now)) is not None


def claim(account_id: int, lease_id: str, *, provider: str, now: float | None = None) -> bool:
    """Exclusively claim an active account of this provider in one transaction."""
    now = time.time() if now is None else now
    with db.transaction():
        db.execute("DELETE FROM subscription_account_leases WHERE account_id=? AND expires_at<=?",
                   (account_id, now))
        changed = db.execute(
            "INSERT INTO subscription_account_leases (account_id, lease_id, provider, heartbeat_at, expires_at) "
            "SELECT id, ?, provider, ?, ? FROM subscription_accounts "
            "WHERE id=? AND provider=? AND status='active' "
            "AND NOT EXISTS (SELECT 1 FROM subscription_account_leases WHERE account_id=?)",
            (lease_id, now, now + LEASE_SECONDS, account_id, provider, account_id)).rowcount
    return changed == 1


def renew(account_id: int, lease_id: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.execute("UPDATE subscription_account_leases SET heartbeat_at=?, expires_at=? "
                      "WHERE account_id=? AND lease_id=? AND expires_at>? "
                      "AND EXISTS (SELECT 1 FROM subscription_accounts WHERE id=? AND status='active')",
                      (now, now + LEASE_SECONDS, account_id, lease_id, now, account_id)).rowcount == 1


def owns(account_id: int, lease_id: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.one("SELECT 1 FROM subscription_account_leases l JOIN subscription_accounts a ON a.id=l.account_id "
                  "WHERE l.account_id=? AND l.lease_id=? AND l.expires_at>? AND a.status='active'",
                  (account_id, lease_id, now)) is not None


def release(account_id: int, lease_id: str) -> None:
    """Release only this request's lease, never a newer owner's replacement."""
    db.execute("DELETE FROM subscription_account_leases WHERE account_id=? AND lease_id=?",
               (account_id, lease_id))


def quarantine(account_id: int, lease_id: str) -> bool:
    """Disable an uncertain transport only while this request owns the account."""
    changed = db.execute("UPDATE subscription_accounts SET status='disabled', last_error=?, updated_at=? "
                         "WHERE id=? AND EXISTS (SELECT 1 FROM subscription_account_leases "
                         "WHERE account_id=? AND lease_id=? AND expires_at>?)",
                         ("Transport shutdown failed; verify the account before enabling it.", db.now(),
                          account_id, account_id, lease_id, time.time())).rowcount == 1
    if changed:
        _invalidate_pool_cache()
    return changed


def rest(account_id: int, until_text, error: str, *, lease_id: str | None = None,
         now: float | None = None) -> bool:
    """Put an account on cooldown, lease-guarded so a stale owner cannot rest it."""
    now = time.time() if now is None else now
    if lease_id is None:
        changed = db.execute("UPDATE subscription_accounts SET cooldown_until=?, last_error=?, updated_at=? "
                             "WHERE id=?", (until_text, (error or "")[:500], db.now(), account_id)).rowcount
    else:
        changed = db.execute("UPDATE subscription_accounts SET cooldown_until=?, last_error=?, updated_at=? "
                             "WHERE id=? AND EXISTS (SELECT 1 FROM subscription_account_leases "
                             "WHERE account_id=? AND lease_id=? AND expires_at>?)",
                             (until_text, (error or "")[:500], db.now(), account_id, account_id, lease_id,
                              now)).rowcount
    if changed:
        _invalidate_pool_cache()
    return changed == 1


def clear_cooldown(account_id: int) -> None:
    db.execute("UPDATE subscription_accounts SET cooldown_until=NULL, updated_at=? WHERE id=?",
               (db.now(), account_id))
    _invalidate_pool_cache()


def insert_usage(lease_id: str, account_id: int, provider: str, family: str, tokens_in: int,
                 tokens_out: int, *, now_epoch: float) -> bool:
    """Append one request's tokens to the rolling ledger, idempotent on lease."""
    return db.execute("INSERT OR IGNORE INTO subscription_usage (lease_id, account_id, provider, family, "
                      "tokens_in, tokens_out, created_epoch) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (lease_id, account_id, provider, family or "", int(tokens_in), int(tokens_out),
                       now_epoch)).rowcount == 1


def bump_counters(account_id: int, window_used, window_resets_at, weekly_used, weekly_resets_at) -> None:
    """Refresh the denormalised reported-snapshot counters shown to administrators."""
    db.execute("UPDATE subscription_accounts SET window_used=?, window_resets_at=?, weekly_used=?, "
               "weekly_resets_at=?, updated_at=? WHERE id=?",
               (int(window_used), window_resets_at, int(weekly_used), weekly_resets_at, db.now(), account_id))


def scoped_sum(account_id: int, family: str, *, since_epoch: float) -> int:
    return db.scalar("SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM subscription_usage "
                     "WHERE account_id=? AND family=? AND created_epoch>?",
                     (account_id, family or "", since_epoch), 0)


def accounts_resting_expired(*, provider: str | None = None):
    """Accounts whose rest period has ended."""
    sql = "SELECT * FROM subscription_accounts WHERE cooldown_until IS NOT NULL AND cooldown_until<=?"
    params: list = [db.now()]
    if provider is not None:
        sql += " AND provider=?"
        params.append(provider)
    return db.query(sql + " ORDER BY priority, id", tuple(params))
