"""Storage for the Claude subscription pool (``claude_accounts``).

Each row is one subscription account (the site has three) whose 5-hour and
weekly usage the pool tracks. Decisions (which account serves a request, how
remaining quota shrinks model limits) live in
:mod:`bananachat.services.claude_pool`; this module only validates and stores.
Every amount is in **tokens** (prompt + completion).
"""

from __future__ import annotations

from bananachat import db

STATUSES = ("active", "disabled", "error")
TOKENS_MAX = 1_000_000_000


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
    except (TypeError, ValueError):
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
                   window_limit=None, weekly_limit=None) -> None:
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
    if window_limit is not None:
        values["window_limit"] = _tokens(window_limit, "5-hour tokens") \
            if window_limit not in ("", None) else None
    if weekly_limit is not None:
        values["weekly_limit"] = _tokens(weekly_limit, "Weekly tokens") \
            if weekly_limit not in ("", None) else None
    if not values:
        return
    values["updated_at"] = db.now()
    assignments = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE claude_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache()


def remove_account(account_id: int) -> bool:
    removed = db.execute("DELETE FROM claude_accounts WHERE id=?", (account_id,)).rowcount == 1
    if removed:
        _invalidate_pool_cache()
    return removed


def report_quota(account_id: int, *, window_used=None, window_limit=None, window_resets_at=None,
                 weekly_used=None, weekly_limit=None, weekly_resets_at=None, error: str = "") -> None:
    """Store what the Claude site reports is left on one account (None leaves a column alone)."""
    row = get(account_id)
    if row is None:
        raise ValueError("That subscription account does not exist.")
    values: dict = {"last_checked_at": db.now(), "last_error": (error or "")[:500],
                    "updated_at": db.now()}
    for name, value in (("window_used", window_used), ("window_limit", window_limit),
                        ("weekly_used", weekly_used), ("weekly_limit", weekly_limit)):
        if value is not None:
            values[name] = _tokens(value, name.replace("_", " ").capitalize())
    for name, value in (("window_resets_at", window_resets_at), ("weekly_resets_at", weekly_resets_at)):
        if value is not None:
            values[name] = value
    if error:
        values["status"] = "error"
    elif row["status"] == "error":
        values["status"] = "active"
    assignments = ", ".join(f"{name}=?" for name in values)
    db.execute(f"UPDATE claude_accounts SET {assignments} WHERE id=?", (*values.values(), account_id))
    _invalidate_pool_cache()
