"""Read-only counts for the administrator pages (no other module depends on this)."""

from __future__ import annotations

from datetime import timedelta

from bananachat import db


def user_counts() -> dict:
    row = db.one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(role='admin'), 0) AS admins, "
        "COALESCE(SUM(suspended=1), 0) AS suspended, COALESCE(SUM(created_at>=?), 0) AS new_week, "
        "COALESCE(SUM(last_login_at>=?), 0) AS active_day FROM users",
        (db.now(-timedelta(days=7)), db.now(-timedelta(days=1))))
    return row.to_dict()


def model_counts() -> dict:
    row = db.one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(backend_available=1), 0) AS available, "
        "COALESCE(SUM(is_rolled_out=1), 0) AS rolled_out, "
        "COALESCE(SUM(is_rolled_out=1 AND backend_available=1 AND failing_at IS NULL AND retired_at IS NULL "
        "AND delete_requested_at IS NULL AND enrollment<>'ignored'), 0) AS live, "
        "COALESCE(SUM(backend='comfyui'), 0) AS image FROM ai_models")
    return row.to_dict()


def pending_counts() -> dict:
    return {
        "quota": db.scalar("SELECT COUNT(*) FROM quota_requests WHERE status='pending'", default=0),
        "access": db.scalar("SELECT COUNT(*) FROM model_access_requests WHERE status='pending'", default=0),
        "downloads": db.scalar("SELECT COUNT(*) FROM model_pull_jobs WHERE status IN ('queued', 'pulling')",
                               default=0),
        "new_models": db.scalar("SELECT COUNT(*) FROM ai_models WHERE enrollment='new'", default=0),
        "failing_models": db.scalar("SELECT COUNT(*) FROM ai_models WHERE failing_at IS NOT NULL", default=0),
    }


def audit_actions() -> list[str]:
    return [row["action"] for row in db.query("SELECT DISTINCT action FROM audit_log ORDER BY action LIMIT 500")]


def invite_usage(invite_ids: list[int]) -> dict[int, list]:
    if not invite_ids:
        return {}
    marks = ",".join("?" for _ in invite_ids)
    result: dict[int, list] = {}
    for row in db.query(f"SELECT x.invite_code_id, x.used_at, u.username FROM invite_code_usage x "
                        f"JOIN users u ON u.id=x.user_id WHERE x.invite_code_id IN ({marks}) "
                        "ORDER BY x.used_at DESC", invite_ids):
        result.setdefault(row["invite_code_id"], []).append(row)
    return result
