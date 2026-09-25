"""Credit ledger and quota management.

1 credit = 1,000 tokens (input + output combined).
Users get 30 regular credits + 15 slow credits per day (API/playground).
Chat credits are optional and tracked separately when enabled by an admin.
Admins have unlimited credits on all pools.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import math
import sqlite3

import config

from ._connection import get_db_context, retry_on_busy

TOKENS_PER_CREDIT = 1000
DEFAULT_DAILY_CREDITS = 30
DEFAULT_DAILY_SLOW_CREDITS = 15


class InsufficientImageCreditsError(ValueError):
    pass


class ImageCreditReservationError(RuntimeError):
    pass


def tokens_to_credits(tokens):
    """Convert token count to credit units (float)."""
    return tokens / TOKENS_PER_CREDIT


def _site_default_quota(conn):
    """Read the admin-configured quota defaults, falling back to module constants."""
    row = conn.execute(
        "SELECT default_daily_credits, default_slow_credits FROM site_settings WHERE id=1"
    ).fetchone()
    if row and row["default_daily_credits"] is not None:
        return int(row["default_daily_credits"]), int(row["default_slow_credits"] or 0)
    return DEFAULT_DAILY_CREDITS, DEFAULT_DAILY_SLOW_CREDITS


def _get_quota_settings(conn):
    """Read the quota-related site settings."""
    row = conn.execute(
        "SELECT slow_credits_enabled, chat_daily_limit_enabled, "
        "chat_daily_credits, chat_daily_slow_credits "
        "FROM site_settings WHERE id=1"
    ).fetchone()
    if not row:
        return {"slow_credits_enabled": 1, "chat_daily_limit_enabled": 0,
                "chat_daily_credits": None, "chat_daily_slow_credits": None}
    return dict(row)


@retry_on_busy
def get_user_quota(user_id, *, connection=None):
    """Return the user's quota row, creating default if missing."""
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        row = conn.execute("SELECT * FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            credits, slow = _site_default_quota(conn)
            conn.execute(
                "INSERT OR IGNORE INTO user_quota (user_id, daily_credits, daily_slow_credits) VALUES (?,?,?)",
                (user_id, credits, slow),
            )
            if connection is None:
                conn.commit()
            row = conn.execute("SELECT * FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
        return row


@retry_on_busy
def set_user_quota(user_id, daily_credits, daily_slow_credits, updated_by=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "INSERT INTO user_quota (user_id, daily_credits, daily_slow_credits, updated_at, updated_by) "
            "VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "daily_credits=excluded.daily_credits, daily_slow_credits=excluded.daily_slow_credits, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (user_id, daily_credits, daily_slow_credits, now, updated_by),
        )
        conn.commit()


@retry_on_busy
def get_today_usage(user_id, pool="api", *, connection=None):
    """Return (credits_used, slow_credits_used) for today (UTC).

    pool='api'  counts request_type IN ('api', 'playground')
    pool='chat' counts request_type IN ('chat', 'chat_incognito')
    pool='all'  counts everything (original behaviour)
    """
    today = datetime.now(timezone.utc).date().isoformat()
    if pool == "chat":
        type_filter = "AND request_type IN ('chat', 'chat_incognito')"
    elif pool == "api":
        type_filter = "AND request_type IN ('api', 'playground')"
    else:
        type_filter = ""
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        row = conn.execute(
            "SELECT SUM(CASE WHEN is_slow=0 THEN credits_used ELSE 0 END) AS reg, "
            "SUM(CASE WHEN is_slow=1 THEN credits_used ELSE 0 END) AS slow "
            f"FROM credit_ledger WHERE user_id=? AND date(created_at)=? {type_filter}",
            (user_id, today),
        ).fetchone()
        return (row["reg"] or 0.0, row["slow"] or 0.0) if row else (0.0, 0.0)


def deduct_credits(user_id, tokens_in, tokens_out, model_id=None, token_id=None,
                   request_type="api", *, connection=None, usage_estimated=False):
    """Record usage and select its credit pool in the same write transaction."""
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (tokens_in, tokens_out)):
            raise ValueError("Token usage must contain non-negative integers.")
        credits = tokens_to_credits(tokens_in + tokens_out)
        is_chat = request_type in ("chat", "chat_incognito")
        pool = "chat" if is_chat else "api"
        reg_used, _ = get_today_usage(user_id, pool=pool, connection=conn)
        if is_chat:
            settings = _get_quota_settings(conn)
            reg_limit = int(settings["chat_daily_credits"]) if (
                settings.get("chat_daily_limit_enabled") and settings.get("chat_daily_credits") is not None
            ) else 999999
        else:
            reg_limit = get_user_quota(user_id, connection=conn)["daily_credits"]
        reg_limit, _ = _apply_music_bonus(user_id, reg_limit, 0, connection=conn)
        is_slow = reg_used >= reg_limit
        conn.execute(
            "INSERT INTO credit_ledger "
            "(user_id, token_id, model_id, credits_used, is_slow, tokens_in, tokens_out, request_type, created_at, usage_estimated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, token_id, model_id, credits, int(is_slow), tokens_in, tokens_out,
             request_type, datetime.now(timezone.utc).isoformat(), int(usage_estimated)),
        )
        if connection is None:
            conn.commit()
        return credits, is_slow


def reserve_image_credits(user_id, credits, model_id=None, token_id=None):
    """Atomically reserve a fixed image charge before expensive GPU work.

    Returns (reservation_id, is_slow). Raises ValueError when neither API credit
    pool has enough remaining capacity for the complete reservation.
    """
    credits = float(credits)
    if credits <= 0:
        return None, False

    quota = get_user_quota(user_id)
    reg_limit, slow_limit = _apply_music_bonus(
        user_id, quota["daily_credits"], quota["daily_slow_credits"]
    )
    with get_db_context() as settings_conn:
        quota_settings = _get_quota_settings(settings_conn)
    if not quota_settings.get("slow_credits_enabled", 1):
        slow_limit = 0

    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    stale_before = (
        datetime.now(timezone.utc)
        - timedelta(seconds=config.IMAGE_CREDIT_RESERVATION_TTL)
    ).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM image_credit_reservations "
            "WHERE COALESCE(updated_at, created_at)<?",
            (stale_before,),
        )
        usage = conn.execute(
            "SELECT SUM(CASE WHEN is_slow=0 THEN credits_used ELSE 0 END) AS reg, "
            "SUM(CASE WHEN is_slow=1 THEN credits_used ELSE 0 END) AS slow "
            "FROM credit_ledger WHERE user_id=? AND date(created_at)=? "
            "AND request_type IN ('api', 'playground')",
            (user_id, today),
        ).fetchone()
        reg_used = float(usage["reg"] or 0)
        slow_used = float(usage["slow"] or 0)
        reserved = conn.execute(
            "SELECT SUM(CASE WHEN is_slow=0 THEN credits_reserved ELSE 0 END) AS reg, "
            "SUM(CASE WHEN is_slow=1 THEN credits_reserved ELSE 0 END) AS slow "
            "FROM image_credit_reservations WHERE user_id=?",
            (user_id,),
        ).fetchone()
        reg_used += float(reserved["reg"] or 0)
        slow_used += float(reserved["slow"] or 0)
        if reg_used + credits <= reg_limit:
            is_slow = False
        elif slow_used + credits <= slow_limit:
            is_slow = True
        else:
            conn.rollback()
            raise InsufficientImageCreditsError(
                "Not enough credits remaining for image generation"
            )
        cur = conn.execute(
            "INSERT INTO image_credit_reservations "
            "(user_id, token_id, model_id, credits_reserved, is_slow, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, token_id, model_id, credits, int(is_slow), now, now),
        )
        conn.commit()
        return cur.lastrowid, is_slow


def touch_image_credit_reservation(reservation_id, user_id):
    """Refresh an active reservation while its request is queued or running."""
    if reservation_id is None:
        return False
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE image_credit_reservations SET updated_at=? WHERE id=? AND user_id=?",
            (datetime.now(timezone.utc).isoformat(), reservation_id, user_id),
        )
        conn.commit()
        return cur.rowcount == 1


def refund_image_credit_reservation(reservation_id, user_id):
    """Remove a failed image generation's pending credit reservation."""
    if reservation_id is None:
        return False
    with get_db_context() as conn:
        cur = conn.execute(
            "DELETE FROM image_credit_reservations WHERE id=? AND user_id=?",
            (reservation_id, user_id),
        )
        conn.commit()
        return cur.rowcount == 1


def finalize_image_credit_reservation(reservation_id, user_id, *, connection=None):
    """Convert a pending image reservation into a completed ledger charge."""
    if reservation_id is None:
        return False
    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        if connection is None:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM image_credit_reservations WHERE id=? AND user_id=?",
            (reservation_id, user_id),
        ).fetchone()
        if not row:
            if connection is None:
                conn.rollback()
            raise ImageCreditReservationError(
                "Image credit reservation expired or was not found"
            )
        conn.execute(
            "INSERT INTO credit_ledger "
            "(user_id, token_id, model_id, credits_used, is_slow, tokens_in, "
            "tokens_out, request_type, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["user_id"], row["token_id"], row["model_id"],
                row["credits_reserved"], row["is_slow"], 0,
                int(row["credits_reserved"] * TOKENS_PER_CREDIT), "api",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            "DELETE FROM image_credit_reservations WHERE id=?", (reservation_id,)
        )
        if connection is None:
            conn.commit()
        return True


def _apply_music_bonus(user_id, reg_limit, slow_limit, *, connection=None):
    """Apply the music program bonus (multiplier or fixed) to limits."""
    from ._music import get_effective_quota_bonus
    mode, value_reg, value_slow = get_effective_quota_bonus(user_id, connection=connection)
    if mode == "multiplier":
        return int(reg_limit * value_reg), int(slow_limit * value_reg)
    elif mode == "fixed":
        return reg_limit + int(value_reg), slow_limit + int(value_slow)
    return reg_limit, slow_limit


def check_credits_available(user_id, role="user"):
    """Return (ok, is_slow, reg_used, slow_used, reg_limit, slow_limit).

    Checks the API/playground credit pool.
    ok=True means request may proceed (may be slow).
    ok=False means both regular and slow credits exhausted.
    """
    if role in ("admin",):
        return True, False, 0, 0, 999999, 999999

    quota = get_user_quota(user_id)
    reg_used, slow_used = get_today_usage(user_id, pool="api")

    reg_limit = quota["daily_credits"]
    slow_limit = quota["daily_slow_credits"]

    reg_limit, slow_limit = _apply_music_bonus(user_id, reg_limit, slow_limit)

    # Check if slow credits are globally disabled
    with get_db_context() as conn:
        qs = _get_quota_settings(conn)
    if not qs.get("slow_credits_enabled", 1):
        slow_limit = 0

    if reg_used < reg_limit:
        return True, False, reg_used, slow_used, reg_limit, slow_limit
    if slow_limit > 0 and slow_used < slow_limit:
        return True, True, reg_used, slow_used, reg_limit, slow_limit
    return False, True, reg_used, slow_used, reg_limit, slow_limit


def check_chat_credits_available(user_id, role="user", *, connection=None):
    """Return (ok, is_slow, reg_used, slow_used, reg_limit, slow_limit) for chat pool.

    Returns ok=True unconditionally if chat daily limits are not enabled.
    """
    if role in ("admin",):
        return True, False, 0, 0, 999999, 999999

    with (get_db_context() if connection is None else nullcontext(connection)) as conn:
        qs = _get_quota_settings(conn)

    if not qs.get("chat_daily_limit_enabled"):
        return True, False, 0, 0, 999999, 999999

    chat_reg = qs.get("chat_daily_credits")
    chat_slow = qs.get("chat_daily_slow_credits")
    if chat_reg is None:
        return True, False, 0, 0, 999999, 999999

    reg_limit = int(chat_reg)
    slow_limit = int(chat_slow or 0)

    reg_limit, slow_limit = _apply_music_bonus(user_id, reg_limit, slow_limit, connection=connection)

    if not qs.get("slow_credits_enabled", 1):
        slow_limit = 0

    reg_used, slow_used = get_today_usage(user_id, pool="chat", connection=connection)

    if reg_used < reg_limit:
        return True, False, reg_used, slow_used, reg_limit, slow_limit
    if slow_limit > 0 and slow_used < slow_limit:
        return True, True, reg_used, slow_used, reg_limit, slow_limit
    return False, True, reg_used, slow_used, reg_limit, slow_limit


@retry_on_busy
def get_usage_history(user_id, limit=100):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT cl.*, at.name AS token_name, at.token_prefix, am.display_name AS model_name "
            "FROM credit_ledger cl "
            "LEFT JOIN api_tokens at ON cl.token_id=at.id "
            "LEFT JOIN ai_models am ON cl.model_id=am.id "
            "WHERE cl.user_id=? AND cl.request_type IN ('api', 'playground') "
            "ORDER BY cl.created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def _quota_request_value(value):
    if isinstance(value, bool):
        raise ValueError("Invalid credit values")
    if isinstance(value, int):
        return value
    if not isinstance(value, float) or not math.isfinite(value) or not value.is_integer():
        raise ValueError("Invalid credit values")
    return int(value)


def submit_quota_request(user_id, new_credits, new_slow_credits, reason,
                         duration_type="permanent"):
    """Validate and atomically submit, and optionally approve, a quota request."""
    new_credits = _quota_request_value(new_credits)
    new_slow_credits = _quota_request_value(new_slow_credits)
    reason = (reason or "").strip()[:1000]
    if duration_type not in ("one_day", "permanent"):
        raise ValueError("Invalid quota request duration")
    if duration_type == "one_day":
        raise ValueError(
            "One-day quota requests are unavailable because temporary quota expiry is not supported"
        )
    if new_credits < 1 or new_credits > 10000 or new_slow_credits < 0 or new_slow_credits > 10000:
        raise ValueError("Invalid credit values")
    if not reason:
        raise ValueError("A reason is required")

    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        default_credits, default_slow = _site_default_quota(conn)
        conn.execute(
            "INSERT OR IGNORE INTO user_quota "
            "(user_id, daily_credits, daily_slow_credits) VALUES (?,?,?)",
            (user_id, default_credits, default_slow),
        )
        current = conn.execute(
            "SELECT daily_credits, daily_slow_credits FROM user_quota WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if (new_credits < current["daily_credits"] or
                new_slow_credits < current["daily_slow_credits"] or
                (new_credits == current["daily_credits"] and
                 new_slow_credits == current["daily_slow_credits"])):
            conn.rollback()
            raise ValueError(
                "Requested quotas must not reduce either allocation and must increase at least one"
            )

        settings = conn.execute(
            "SELECT quota_auto_approve_enabled, quota_auto_approve_max_credits, "
            "quota_auto_approve_max_slow_credits FROM site_settings WHERE id=1"
        ).fetchone()
        auto_approved = bool(
            settings and settings["quota_auto_approve_enabled"] and
            duration_type == "permanent" and
            new_credits <= settings["quota_auto_approve_max_credits"] and
            new_slow_credits <= settings["quota_auto_approve_max_slow_credits"]
        )
        status = "approved" if auto_approved else "pending"
        message = None
        resolved_at = None
        if auto_approved:
            resolved_at = now
            message = (
                "Automatically approved: this permanent increase is within the configured "
                f"limits of {settings['quota_auto_approve_max_credits']} regular and "
                f"{settings['quota_auto_approve_max_slow_credits']} slow credits per day."
            )
        try:
            cur = conn.execute(
                "INSERT INTO quota_requests "
                "(user_id, new_credits, new_slow_credits, reason, duration_type, status, "
                "admin_message, resolution_source, created_at, resolved_at, resolved_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (user_id, new_credits, new_slow_credits, reason, duration_type,
                 status, message, "automatic" if auto_approved else "manual", now, resolved_at),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if "unique" in str(exc).lower():
                raise ValueError("You already have a pending quota request") from exc
            raise

        if auto_approved:
            conn.execute(
                "UPDATE user_quota SET daily_credits=?, daily_slow_credits=?, "
                "updated_at=?, updated_by=NULL WHERE user_id=?",
                (new_credits, new_slow_credits, now, user_id),
            )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM quota_requests WHERE id=?", (cur.lastrowid,)
        ).fetchone())


def create_quota_request(user_id, new_credits, new_slow_credits, reason,
                         duration_type="permanent"):
    """Create a request and return its id (legacy DB API)."""
    return submit_quota_request(
        user_id, new_credits, new_slow_credits, reason, duration_type
    )["id"]


@retry_on_busy
def get_pending_quota_request(user_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM quota_requests WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()


@retry_on_busy
def list_quota_requests(status=None, limit=100):
    with get_db_context() as conn:
        if status:
            return conn.execute(
                "SELECT qr.*, u.username, ra.username AS resolved_by_username "
                "FROM quota_requests qr "
                "JOIN users u ON qr.user_id=u.id "
                "LEFT JOIN users ra ON qr.resolved_by=ra.id "
                "WHERE qr.status=? ORDER BY qr.created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return conn.execute(
            "SELECT qr.*, u.username, ra.username AS resolved_by_username "
            "FROM quota_requests qr "
            "JOIN users u ON qr.user_id=u.id "
            "LEFT JOIN users ra ON qr.resolved_by=ra.id "
            "ORDER BY qr.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


@retry_on_busy
def list_user_quota_requests(user_id):
    """Return a user's complete quota decision history, newest first."""
    with get_db_context() as conn:
        return conn.execute(
            "SELECT qr.*, resolver.username AS resolved_by_username "
            "FROM quota_requests qr "
            "LEFT JOIN users resolver ON qr.resolved_by=resolver.id "
            "WHERE qr.user_id=? ORDER BY qr.created_at DESC, qr.id DESC",
            (user_id,),
        ).fetchall()


@retry_on_busy
def resolve_quota_request(request_id, admin_id, approved, admin_message=None):
    now = datetime.now(timezone.utc).isoformat()
    status = "approved" if approved else "denied"
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        req = conn.execute("SELECT * FROM quota_requests WHERE id=?", (request_id,)).fetchone()
        if not req or req["status"] != "pending":
            conn.rollback()
            raise ValueError("Request not found or already resolved")
        if approved and req["duration_type"] == "one_day":
            conn.rollback()
            raise ValueError(
                "One-day quota requests cannot be approved because temporary quota expiry is not supported"
            )
        rows = conn.execute(
            "UPDATE quota_requests SET status=?, admin_message=?, resolution_source='manual', "
            "resolved_at=?, resolved_by=? "
            "WHERE id=? AND status='pending'",
            (status, admin_message, now, admin_id, request_id),
        ).rowcount
        if rows == 0:
            conn.rollback()
            raise ValueError("Request was already resolved by another admin")
        if approved:
            default_credits, default_slow = _site_default_quota(conn)
            conn.execute(
                "INSERT OR IGNORE INTO user_quota "
                "(user_id, daily_credits, daily_slow_credits) VALUES (?,?,?)",
                (req["user_id"], default_credits, default_slow),
            )
            current = conn.execute(
                "SELECT daily_credits, daily_slow_credits FROM user_quota WHERE user_id=?",
                (req["user_id"],),
            ).fetchone()
            if (req["new_credits"] < current["daily_credits"] or
                    req["new_slow_credits"] < current["daily_slow_credits"]):
                conn.rollback()
                raise ValueError("Approval would reduce the user's current quota")
            conn.execute(
                "INSERT INTO user_quota (user_id, daily_credits, daily_slow_credits, updated_at, updated_by) "
                "VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "daily_credits=excluded.daily_credits, daily_slow_credits=excluded.daily_slow_credits, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (req["user_id"], req["new_credits"], req["new_slow_credits"], now, admin_id),
            )
        conn.commit()
    return req
