"""Community consent for quota requests: other people can back someone's request with part of their own quota.

A quota request (any kind, for a service or one model: 5-hour or weekly
tokens, a request rate, a temporary boost such as unlimited use for 24 hours,
or a reasoning-effort level) can be **offered to the community** when it is
sent. Then, while its voting window is open (``community_hours``, 72 by
default):

* administrators can still approve or deny it at once;
* other people can **support** it - renouncing some tokens of their own limit
  in the same scope (the same service's 5-hour or weekly tokens, or the same
  model's) - or **object** to it; a vote can be withdrawn while voting is open;
* when the consent is good enough (:func:`consent`), the request is approved
  automatically: the increase is applied as a time-limited grant (a temporary
  request lasts its own hours, other kinds ``community_boost_hours``; an
  effort level is unlocked), and every supporter's renounced tokens are taken
  off their own limit for the same time.

Consent is good when at least ``community_min_supporters`` people support it,
supporters are at least ``community_approval_percent`` % of everyone who
voted, and (for token increases) the renounced tokens cover
``community_coverage_percent`` % of the increase (0: votes alone decide).
Supporters must not be the requester or an administrator, must have had their
account for ``community_min_account_days`` and must not be suspended; each
person can hold at most ``community_max_pledge_percent`` % of their own limit
renounced at a time (across open and approved requests).

When voting closes without consent, the request keeps waiting for an
administrator, as long as it takes, until an administrator decides or the
requester cancels it. Pledges of a request that is cancelled, denied,
approved by an administrator (the site grants it) or whose voting closed are
released at once.

Administrators switch the whole feature, and each kind of request, on or off
(on by default). Everything is in **tokens**.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bananachat import db
from bananachat.services import background

KINDS = ("window", "weekly", "rate", "temporary", "effort")
DEFAULTS = {"enabled": True, "kinds": KINDS, "min_supporters": 3, "approval_percent": 66, "coverage_percent": 100,
            "hours": 72, "boost_hours": 168, "min_account_days": 7, "max_pledge_percent": 50}
BOUNDS = {"min_supporters": (1, 1000), "approval_percent": (1, 100), "coverage_percent": (0, 100),
          "hours": (1, 720), "boost_hours": (1, 720), "min_account_days": (0, 3650), "max_pledge_percent": (0, 100)}
STANCES = ("support", "object")


class VoteError(ValueError):
    """A vote that cannot be cast. ``key`` is its message in the ``community`` catalog."""

    def __init__(self, message: str, key: str, **params):
        super().__init__(message)
        self.key = key
        self.params = params


# ----- settings ---------------------------------------------------------------------------

def settings(site: dict | None = None) -> dict:
    """The community options from the site settings (defaults for missing or unreadable values)."""
    if site is None:
        from bananachat.db import settings as site_settings
        site = site_settings.get()
    result = dict(DEFAULTS)
    if site.get("community_quota_enabled") is not None:
        result["enabled"] = bool(site.get("community_quota_enabled"))
    raw = site.get("community_kinds")
    if isinstance(raw, str):
        result["kinds"] = tuple(kind for kind in KINDS if kind in {part.strip() for part in raw.split(",")})
    for name, (low, high) in BOUNDS.items():
        try:
            value = int(site.get(f"community_{name}"))
        except (TypeError, ValueError):
            continue
        result[name] = max(low, min(high, value))
    return result


def offered(kind: str, site: dict | None = None) -> bool:
    """Whether requests of *kind* can be offered to the community now."""
    options = settings(site)
    return options["enabled"] and kind in options["kinds"]


# ----- what a request asks of the community ---------------------------------------------------

def target(row) -> tuple[str | None, int | None]:
    """``(pool, model_id)`` whose limits a request changes: one model's, else a service's."""
    from bananachat.db import credits

    if credits.request_kind(row) != "effort" and row["model_id"] is not None:
        return None, row["model_id"]
    return row["pool"] or "api", None


def scope(row) -> str | None:
    """The token limit supporters renounce from (``window``/``weekly``), None when the request has no tokens."""
    from bananachat.db import credits

    kind = credits.request_kind(row)
    if kind == "window" or (kind == "temporary" and not row["grant_unlimited"]):
        return "window"
    if kind == "weekly":
        return "weekly"
    return None


def base_for(user_id: str, row) -> dict:
    """The account's own limits in the request's target (a service, or one model)."""
    from bananachat.services import limits

    pool, model_id = target(row)
    if model_id is not None:
        model = db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))
        if model is None:
            return {"window_tokens": 0.0, "weekly_tokens": 0.0, "window_slow_tokens": 0.0, "rate_rules": [],
                    "window_enabled": False, "weekly_enabled": False, "rate_enabled": False}
        return limits.model_base(user_id, model)
    return limits.base_limits(user_id, pool)


def increase(row) -> int:
    """Tokens the request adds to the requester's limit (0 for requests without tokens)."""
    from bananachat.db import credits

    kind = credits.request_kind(row)
    if kind == "temporary":
        return 0 if row["grant_unlimited"] else int(row["new_tokens"] or 0)
    if kind not in ("window", "weekly"):
        return 0
    base = base_for(row["user_id"], row)
    if kind == "window":
        return max(0, int(row["new_tokens"] or 0) - int(base["window_tokens"]))
    return max(0, int(row["new_weekly_tokens"] or 0) - int(base["weekly_tokens"]))


# ----- votes ------------------------------------------------------------------------------------

def votes(request_id: int):
    return db.query("SELECT v.*, u.username FROM quota_request_votes v JOIN users u ON u.id=v.user_id "
                    "WHERE v.request_id=? ORDER BY v.id", (request_id,))


def my_votes(user_id: str) -> dict[int, dict]:
    return {row["request_id"]: row.to_dict() for row in db.query(
        "SELECT * FROM quota_request_votes WHERE user_id=?", (user_id,))}


@dataclass(frozen=True)
class Consent:
    supporters: int
    objectors: int
    pledged: int
    needed: int
    min_supporters: int
    approval_percent: int

    @property
    def percent(self) -> int:
        total = self.supporters + self.objectors
        return round(100 * self.supporters / total) if total else 0

    @property
    def reached(self) -> bool:
        total = self.supporters + self.objectors
        return (self.supporters >= self.min_supporters and total > 0
                and self.supporters * 100 >= self.approval_percent * total and self.pledged >= self.needed)


def consent(row, options: dict | None = None, rows=None) -> Consent:
    """How far the community backs a request (*rows*: its votes, read when None)."""
    options = options or settings()
    rows = votes(row["id"]) if rows is None else rows
    supporters = [vote for vote in rows if vote["stance"] == "support"]
    objectors = [vote for vote in rows if vote["stance"] == "object"]
    # Released pledges (voting closed, the request was decided otherwise) no longer count.
    pledged = sum(int(vote["tokens"] or 0) for vote in supporters if not vote["released_at"])
    needed = math.ceil(increase(row) * options["coverage_percent"] / 100) if scope(row) else 0
    return Consent(len(supporters), len(objectors), pledged, needed, options["min_supporters"],
                   options["approval_percent"])


def voting_open(row, options: dict | None = None, now: str | None = None) -> bool:
    from bananachat.db import credits

    options = options or settings()
    now = now or db.now()
    return bool(row["status"] == "pending" and row["community"] and not row["community_closed_at"]
                and row["community_until"] and row["community_until"] > now and options["enabled"]
                and credits.request_kind(row) in options["kinds"])


def held(user_id: str, pool: str | None, model_id: int | None, scope_name: str, *, exclude: int | None = None,
         now: str | None = None) -> int:
    """Tokens *user_id* has renounced (or promised on open requests) in one scope, now."""
    now = now or db.now()
    return int(db.scalar(
        "SELECT SUM(tokens) FROM quota_request_votes WHERE user_id=? AND stance='support' AND tokens>0 "
        "AND released_at IS NULL AND (ends_at IS NULL OR ends_at>?) AND IFNULL(pool, '')=? "
        "AND IFNULL(model_id, 0)=? AND scope=? AND request_id!=?",
        (user_id, now, pool or "", model_id or 0, scope_name, exclude or 0), 0) or 0)


def pledge_room(user, row, options: dict | None = None) -> int:
    """The most tokens *user* can renounce for this request now (0 when its scope does not limit them)."""
    options = options or settings()
    scope_name = scope(row)
    if scope_name is None or user["role"] == "admin":
        return 0
    base = base_for(user["id"], row)
    if not base[f"{scope_name}_enabled"]:
        return 0
    pool, model_id = target(row)
    cap = math.floor(float(base[f"{scope_name}_tokens"]) * options["max_pledge_percent"] / 100)
    return max(0, cap - held(user["id"], pool, model_id, scope_name, exclude=row["id"]))


def eligible(user, row, options: dict | None = None, now: datetime | None = None) -> str | None:
    """Why *user* cannot vote on the request (a ``community`` message key), None when they can."""
    from bananachat.db import users

    options = options or settings()
    now = now or datetime.now(timezone.utc)
    if not voting_open(row, options, db.timestamp(now)):
        return "closed"
    if user["id"] == row["user_id"]:
        return "own"
    if user["role"] == "admin":
        return "admin"
    fresh = users.get(user["id"])
    if fresh is None or users.is_suspended(fresh):
        return "suspended"
    created = db.parse_timestamp(fresh["created_at"])
    if created is not None and created > now - timedelta(days=options["min_account_days"]):
        return "too_new"
    return None


def vote(user, request_id: int, stance: str, tokens: int = 0) -> dict:
    """Support (renouncing *tokens*) or object to a request, replacing the person's earlier vote.

    Returns ``{"status": "pending"|"approved", "consent": Consent}``; the request is approved at once when the
    consent becomes good enough.
    """
    if stance not in STANCES:
        raise VoteError("Choose support or object.", "vote_failed")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise VoteError("Renounce a whole number of tokens.", "vote_tokens")
    options = settings()
    with db.transaction():
        row = request_row(request_id)
        if row is None:
            raise VoteError("That request does not exist.", "vote_missing")
        problem = eligible(user, row, options)
        if problem:
            raise VoteError("You cannot vote on this request.", f"vote_{problem}")
        scope_name = scope(row)
        if stance == "object" or scope_name is None:
            tokens = 0
        elif tokens:
            room = pledge_room(user, row, options)
            if tokens > room:
                raise VoteError("That is more than you can renounce.", "vote_too_many", max=room)
        pool, model_id = target(row)
        db.execute(
            "INSERT INTO quota_request_votes (request_id, user_id, stance, tokens, pool, model_id, scope, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id, user_id) DO UPDATE SET "
            "stance=excluded.stance, tokens=excluded.tokens, pool=excluded.pool, model_id=excluded.model_id, "
            "scope=excluded.scope, updated_at=excluded.updated_at, released_at=NULL",
            (request_id, user["id"], stance, tokens, pool, model_id, scope_name if tokens else None, db.now(),
             db.now()))
        status = _settle(row, options)
    return {"status": status, "consent": consent(request_row(request_id), options)}


def withdraw(user, request_id: int) -> bool:
    """Take back a vote while voting is open."""
    with db.transaction():
        row = request_row(request_id)
        if row is None or not voting_open(row):
            return False
        return db.execute("DELETE FROM quota_request_votes WHERE request_id=? AND user_id=?",
                          (request_id, user["id"])).rowcount == 1


def release(request_id: int) -> int:
    """Free the pledges of a request that did not get community approval (joins a transaction)."""
    return db.execute("UPDATE quota_request_votes SET released_at=?, updated_at=? WHERE request_id=? "
                      "AND released_at IS NULL AND starts_at IS NULL", (db.now(), db.now(), request_id)).rowcount


def request_row(request_id: int):
    return db.one("SELECT r.*, m.display_name AS model_name, u.username FROM quota_requests r "
                  "JOIN users u ON u.id=r.user_id LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.id=?",
                  (request_id,))


# ----- approval ---------------------------------------------------------------------------------

def _settle(row, options: dict) -> str:
    """Approve *row* when its consent is good enough (joins the caller's transaction). Returns its status."""
    if row["status"] != "pending" or not voting_open(row, options):
        return row["status"]
    if not consent(row, options).reached:
        return "pending"
    try:
        with db.transaction():  # a savepoint: nothing stays behind when the request cannot be carried out
            apply(row, options)
    except ValueError:
        # The request can no longer be carried out as asked (a limit was switched off, a model removed):
        # it keeps waiting for an administrator, who can deny it.
        return "pending"
    return "approved"


def apply(row, options: dict | None = None, now: datetime | None = None) -> None:
    """Carry out a community-approved request as a time-limited grant (joins the caller's transaction)."""
    from bananachat.db import credits
    from bananachat.db import limits as limits_db
    from bananachat.services import limits

    options = options or settings()
    now = now or datetime.now(timezone.utc)
    kind = credits.request_kind(row)
    pool, model_id = target(row)
    grant_pool = None if model_id is not None else pool
    hours = int(row["grant_hours"] or 0) if kind == "temporary" else options["boost_hours"]
    ends = now + timedelta(hours=max(1, hours))
    reason = f"Community approval of request #{row['id']}: {row['reason']}"[:limits_db.REASON_MAX]
    grant_id = None
    common = {"created_by": None, "user_id": row["user_id"], "pool": grant_pool, "model_id": model_id,
              "starts_at": db.timestamp(now), "ends_at": db.timestamp(ends), "reason": reason}
    if kind == "effort":
        if not row["effort_level"] or (row["model_id"] is None and not row["effort_all_models"]):
            raise ValueError("This request names no model or level.")
        limits.unlock_effort(row["user_id"], row["model_id"], row["effort_level"], source="request",
                             updated_by=None)
        ends = None
    elif kind == "temporary":
        if row["grant_unlimited"]:
            grant_id = limits_db.create_grant(scope=None, kind="unlimited", amount=0, **common)
        else:
            if not base_for(row["user_id"], row)["window_enabled"]:
                raise ValueError("That limit is no longer on.")
            grant_id = limits_db.create_grant(scope="window", kind="extra", amount=int(row["new_tokens"] or 0),
                                              **common)
    elif kind in ("window", "weekly"):
        base = base_for(row["user_id"], row)
        if not base[f"{kind}_enabled"]:
            raise ValueError("That limit is no longer on.")
        amount = increase(row)
        if amount > 0:
            grant_id = limits_db.create_grant(scope=kind, kind="extra", amount=amount, **common)
    elif kind == "rate":
        base = base_for(row["user_id"], row)
        asked = {rule["per"]: int(rule["requests"]) for rule in credits.request_rules(row)}
        factors = [asked[rule["per"]] / float(rule["requests"]) for rule in base["rate_rules"]
                   if rule["per"] in asked and float(rule["requests"]) > 0]
        if not base["rate_enabled"] or not factors:
            raise ValueError("That limit is no longer on.")
        factor = min(limits_db.MULTIPLIER_MAX, max(factors))
        if factor >= 1.01:
            grant_id = limits_db.create_grant(scope="rate", kind="multiplier", amount=round(factor, 2), **common)
    else:
        raise ValueError("Unknown kind of request.")
    stamp = db.now()
    db.execute("UPDATE quota_requests SET status='approved', resolution_source='community', resolved_at=?, "
               "grant_id=COALESCE(?, grant_id), boost_ends_at=?, admin_message=COALESCE(admin_message, ?) WHERE id=?",
               (stamp, grant_id, db.timestamp(ends) if ends else None, "Approved by the community.", row["id"]))
    # Supporters' renounced tokens are taken off their own limits for as long as the increase lasts
    # (an effort unlock has no tokens, so there is nothing to take).
    if ends is not None:
        db.execute("UPDATE quota_request_votes SET starts_at=?, ends_at=?, updated_at=? WHERE request_id=? "
                   "AND stance='support' AND tokens>0 AND released_at IS NULL",
                   (db.timestamp(now), db.timestamp(ends), stamp, row["id"]))
    db.execute("UPDATE quota_request_votes SET released_at=? WHERE request_id=? AND starts_at IS NULL "
               "AND released_at IS NULL", (stamp, row["id"]))
    from bananachat.db import users
    users.audit(None, "quota.community_approve", str(row["id"]),
                {"user": row["user_id"], "kind": kind, "pool": grant_pool, "model": model_id, "grant": grant_id,
                 "until": db.timestamp(ends) if ends else None})


# ----- the board --------------------------------------------------------------------------------

def open_requests(limit: int = 100):
    """Requests offered to the community whose voting is open, newest first."""
    options = settings()
    if not options["enabled"]:
        return []
    rows = db.query(
        "SELECT r.*, m.display_name AS model_name, u.username FROM quota_requests r JOIN users u ON u.id=r.user_id "
        "LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.status='pending' AND r.community=1 "
        "AND r.community_closed_at IS NULL AND r.community_until>? ORDER BY r.id DESC LIMIT ?", (db.now(), limit))
    return [row for row in rows if voting_open(row, options)]


def count_open() -> int:
    options = settings()
    if not options["enabled"]:
        return 0
    return int(db.scalar("SELECT COUNT(*) FROM quota_requests WHERE status='pending' AND community=1 "
                         "AND community_closed_at IS NULL AND community_until>?", (db.now(),), 0) or 0)


def my_pledges(user_id: str):
    """Tokens the account renounced (in force or promised), with the request they back."""
    now = db.now()
    return db.query(
        "SELECT v.*, r.kind, r.pool AS request_pool, r.status AS request_status, u.username, "
        "m.display_name AS model_name FROM quota_request_votes v JOIN quota_requests r ON r.id=v.request_id "
        "JOIN users u ON u.id=r.user_id LEFT JOIN ai_models m ON m.id=v.model_id "
        "WHERE v.user_id=? AND v.stance='support' AND v.tokens>0 AND v.released_at IS NULL "
        "AND (v.ends_at IS NULL OR v.ends_at>?) ORDER BY v.id DESC", (user_id, now))


# ----- background ---------------------------------------------------------------------------------

def close_expired(now: str | None = None) -> list[int]:
    """Close voting that ran out (the request keeps waiting for an administrator); settle the rest."""
    now = now or db.now()
    options = settings()
    closed = []
    for row in db.query("SELECT * FROM quota_requests WHERE status='pending' AND community=1 "
                        "AND community_closed_at IS NULL"):
        with db.transaction():
            fresh = db.one("SELECT * FROM quota_requests WHERE id=?", (row["id"],))
            if fresh is None or fresh["status"] != "pending" or fresh["community_closed_at"]:
                continue
            if fresh["community_until"] and fresh["community_until"] <= now:
                db.execute("UPDATE quota_requests SET community_closed_at=? WHERE id=?", (now, row["id"]))
                release(row["id"])
                closed.append(row["id"])
            else:
                _settle(fresh, options)
    return closed


@background.job("community-quota", every=300, initial_delay=90)
def community_job(app) -> None:
    close_expired()
