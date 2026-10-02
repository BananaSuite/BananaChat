"""Storage for the Claude subscription pool (``claude_accounts``).

Each row is one configured account whose 5-hour and
weekly usage the pool tracks. Decisions (which account serves a request, how
remaining quota shrinks model limits) live in
:mod:`bananachat.services.claude_pool`; this module only validates and stores.
Every amount is in **tokens** (prompt + completion).
"""

from __future__ import annotations

import time

from bananachat import db

STATUSES = ("active", "disabled", "error")
TOKENS_MAX = 1_000_000_000
LEASE_SECONDS = 90
_UNSET = object()


def _invalidate_pool_cache() -> None:
    try:
        from bananachat.services import claude_pool as _pool

        _pool._pool_cache.update(at=0.0, value=None)
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass


def list_accounts(*, include_disabled: bool = True):
    where = "" if include_disabled else "WHERE status='active'"
    return db.query(f"SELECT * FROM claude_accounts {where} ORDER BY priority, id")


def get(account_id: int):
    return db.one("SELECT * FROM claude_accounts WHERE id=?", (account_id,))


def get_by_label(label: str):
    return db.one("SELECT * FROM claude_accounts WHERE label=?", ((label or "").strip(),))


def _clean_label(label: str) -> str:
    label = " ".join((label or "").split())[:80]
    if not label:
        raise ValueError("Name the subscription account (e.g. claude-1).")
    return label


def _tokens(value, label: str):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a number.") from None
    import math
    if not math.isfinite(number) or not 0 <= number <= TOKENS_MAX:
        raise ValueError(f"{label} must be between 0 and {TOKENS_MAX:,}.")
    if number != int(number):
        raise ValueError(f"{label} must be a whole number.")
    return int(number)


def add_account(label: str, *, priority: int = 0, note: str = "", window_limit=None,
                weekly_limit=None, created_by: str | None = None) -> int:
    label = _clean_label(label)
    try:
        priority = int(priority or 0)
    except (TypeError, ValueError):
        raise ValueError("Priority must be a whole number.") from None
    if get_by_label(label) is not None:
        raise ValueError(f"There is already a subscription account called {label}.")
    cursor = db.execute(
        "INSERT INTO claude_accounts (label, status, priority, note, window_limit, weekly_limit, "
        "created_at, updated_at) VALUES (?, 'active', ?, ?, ?, ?, ?, ?)",
        (label, max(-100, min(100, priority)), (note or "")[:500],
         _tokens(window_limit, "5-hour tokens"), _tokens(weekly_limit, "Weekly tokens"),
         db.now(), db.now()))
    _invalidate_pool_cache()
    return cursor.lastrowid


def update_account(account_id: int, *, label=None, status=None, priority=None, note=None,
                   window_limit=_UNSET, weekly_limit=_UNSET) -> None:
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
    if not values:
        return
    values["updated_at"] = db.now()
    assignments = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE claude_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache()


def remove_account(account_id: int) -> bool:
    with db.transaction():
        if leased(account_id):
            raise ValueError("This account is answering a request. Disable it and wait before removing it.")
        removed = db.execute("DELETE FROM claude_accounts WHERE id=?", (account_id,)).rowcount == 1
    if removed:
        _invalidate_pool_cache()
    return removed


def report_quota(account_id: int, *, window_used=None, window_limit=None, window_resets_at=None,
                 weekly_used=None, weekly_limit=None, weekly_resets_at=None, error: str = "") -> None:
    """Store a validated quota observation (None leaves a column alone).

    Failed observations do not renew the last successful snapshot or reactivate
    disabled accounts. Token amounts are estimates supplied by the adapter;
    they are not a claim about Anthropic's subscription billing units.
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
        db.execute(f"UPDATE claude_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache()


def leased(account_id: int, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.one("SELECT 1 FROM claude_account_leases WHERE account_id=? AND expires_at>?",
                  (account_id, now)) is not None


def claim(account_id: int, lease_id: str, *, now: float | None = None) -> bool:
    """Exclusively claim an active account. Caller checks quota in the same transaction."""
    now = time.time() if now is None else now
    with db.transaction():
        db.execute("DELETE FROM claude_account_leases WHERE account_id=? AND expires_at<=?", (account_id, now))
        changed = db.execute("INSERT INTO claude_account_leases (account_id, lease_id, heartbeat_at, expires_at) "
                             "SELECT id, ?, ?, ? FROM claude_accounts WHERE id=? AND status='active' "
                             "AND NOT EXISTS (SELECT 1 FROM claude_account_leases WHERE account_id=?)",
                             (lease_id, now, now + LEASE_SECONDS, account_id, account_id)).rowcount
    return changed == 1


def renew(account_id: int, lease_id: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.execute("UPDATE claude_account_leases SET heartbeat_at=?, expires_at=? "
                      "WHERE account_id=? AND lease_id=? AND expires_at>? "
                      "AND EXISTS (SELECT 1 FROM claude_accounts WHERE id=? AND status='active')",
                      (now, now + LEASE_SECONDS, account_id, lease_id, now, account_id)).rowcount == 1


def owns(account_id: int, lease_id: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return db.one("SELECT 1 FROM claude_account_leases l JOIN claude_accounts a ON a.id=l.account_id "
                  "WHERE l.account_id=? AND l.lease_id=? AND l.expires_at>? AND a.status='active'",
                  (account_id, lease_id, now)) is not None


def release(account_id: int, lease_id: str) -> None:
    """Release only this request's lease, never a newer owner's replacement."""
    db.execute("DELETE FROM claude_account_leases WHERE account_id=? AND lease_id=?", (account_id, lease_id))


def quarantine(account_id: int, lease_id: str) -> bool:
    """Disable an uncertain transport only while this request owns the account."""
    changed = db.execute("UPDATE claude_accounts SET status='disabled', last_error=?, updated_at=? "
                         "WHERE id=? AND EXISTS (SELECT 1 FROM claude_account_leases "
                         "WHERE account_id=? AND lease_id=? AND expires_at>?)",
                         ("Transport shutdown failed; verify the account before enabling it.", db.now(),
                          account_id, account_id, lease_id, time.time())).rowcount == 1
    if changed:
        _invalidate_pool_cache()
    return changed
