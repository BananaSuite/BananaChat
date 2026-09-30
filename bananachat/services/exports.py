"""Personal data exports (the account page and the administrator use the same functions).

* :func:`gdpr_export` - everything stored about one account: profile (never
  the password hash), preferences, quota and limits (custom limits, tier,
  speed, the account's own grants), API token metadata (never the token
  hash), the credit ledger, access-list memberships and requests, quota
  requests, personalities, sign-in sessions (never the session hash), the
  account's own audit-log entries, every chat that has not been deleted
  with its messages and attachments, and the account's agent tasks with
  their steps, sub-agents and follow-up messages (never the task's lease or
  sandbox identifiers).
* :func:`chats_export` - chats and messages only.

Attachments are exported as metadata plus their extracted text. Image bytes
are included as base64 only while each image is at most
``INLINE_IMAGE_MAX_BYTES`` and the export's inline images stay within
``INLINE_IMAGES_TOTAL_BYTES``; other images carry ``"image_included": false``
(their SHA-256 still identifies them). Both functions return plain,
JSON-serialisable dicts.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from bananachat import __version__, db
from bananachat.db import users

EXPORT_FORMAT_VERSION = 2
INLINE_IMAGE_MAX_BYTES = 512 * 1024
INLINE_IMAGES_TOTAL_BYTES = 20 * 1024 * 1024


def _rows(sql: str, params=()) -> list[dict]:
    return [row.to_dict() for row in db.query(sql, params)]


def _header(kind: str, user) -> dict:
    return {
        "export_type": kind,
        "format_version": EXPORT_FORMAT_VERSION,
        "application_version": __version__,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_id": user["id"] if user else None,
        "username": user["username"] if user else None,
    }


def _columns(table: str) -> set[str]:
    return {row["name"] for row in db.query(f'PRAGMA table_info("{table}")')}


def _chats(user_id: str, *, include_images: bool) -> list[dict]:
    deleted_clause = " AND s.deleted_at IS NULL" if "deleted_at" in _columns("chat_sessions") else ""
    sessions = _rows(
        "SELECT s.id, s.title, s.is_incognito, s.created_at, s.updated_at, p.name AS personality "
        "FROM chat_sessions s LEFT JOIN personalities p ON p.id=s.personality_id "
        f"WHERE s.user_id=?{deleted_clause} ORDER BY s.created_at, s.id",
        (user_id,))
    budget = {"left": INLINE_IMAGES_TOTAL_BYTES}
    for chat in sessions:
        chat["is_incognito"] = bool(chat["is_incognito"])
        messages = _rows(
            "SELECT m.id, m.role, m.content, m.created_at, m.tokens_in, m.tokens_out, "
            "a.display_name AS model, a.ollama_name AS model_id FROM chat_messages m "
            "LEFT JOIN ai_models a ON a.id=m.model_id WHERE m.session_id=? ORDER BY m.id", (chat["id"],))
        attachments: dict[int, list] = {}
        if messages:
            for row in db.query(
                    "SELECT t.id, t.message_id, t.kind, t.filename, t.media_type, t.size_bytes, t.sha256, "
                    "t.extracted_text, t.created_at, "
                    "CASE WHEN t.image_data IS NOT NULL AND length(t.image_data)<=? THEN t.image_data END AS small_image "
                    "FROM chat_attachments t JOIN chat_messages m ON m.id=t.message_id "
                    "WHERE m.session_id=? ORDER BY t.created_at, t.id",
                    (INLINE_IMAGE_MAX_BYTES if include_images else -1, chat["id"])):
                item = row.to_dict()
                image = item.pop("small_image")
                if item["kind"] == "image":
                    item.pop("extracted_text", None)
                    included = image is not None and len(image) <= budget["left"]
                    item["image_included"] = included
                    if included:
                        budget["left"] -= len(image)
                        item["image_base64"] = base64.b64encode(image).decode("ascii")
                attachments.setdefault(item.pop("message_id"), []).append(item)
        for message in messages:
            message["attachments"] = attachments.get(message.pop("id"), [])
        chat["messages"] = messages
    return sessions


def _agent_tasks(user_id: str) -> list[dict]:
    if not _columns("agent_tasks"):
        return []
    tasks = _rows(
        "SELECT id, title, prompt, status, model_name, swarm, runs, steps_used, tool_calls, tokens_in, tokens_out, "
        "summary, error, notice, created_at, started_at, updated_at, finished_at FROM agent_tasks WHERE user_id=? "
        "ORDER BY created_at, id", (user_id,))
    for task in tasks:
        task["swarm"] = bool(task["swarm"])
        task["steps"] = _rows(
            "SELECT agent, kind, content, thinking, tool_name, tool_args, tool_result, tool_status, tokens_in, "
            "tokens_out, duration_ms, created_at FROM agent_steps WHERE task_id=? ORDER BY id", (task["id"],))
        task["sub_agents"] = _rows(
            "SELECT agent, title, instructions, status, steps_used, summary, created_at, finished_at "
            "FROM agent_lanes WHERE task_id=? ORDER BY agent", (task["id"],))
        task["messages"] = _rows(
            "SELECT content, created_at, consumed_at FROM agent_messages WHERE task_id=? ORDER BY id", (task["id"],))
    return tasks


def chats_export(user_id: str) -> dict:
    """The account's chats (not deleted) with their messages and attachment metadata."""
    user = users.get(user_id)
    payload = _header("chats_export", user)
    payload["chats"] = _chats(user_id, include_images=False) if user else []
    return payload


def gdpr_export(user_id: str) -> dict:
    """Everything stored about an account, without password, token or session hashes."""
    user = users.get(user_id)
    payload = _header("gdpr_data_export", user)
    if user is None:
        payload["account"] = None
        return payload

    account_fields = ("id", "username", "role", "suspended", "suspended_until", "invite_code", "created_at",
                      "last_login_at", "music_opted_in", "music_forced")
    account = {field: user.get(field) for field in account_fields}
    for flag in ("suspended", "music_opted_in", "music_forced"):
        account[flag] = bool(account[flag])
    payload["account"] = account
    payload["preferences"] = users.get_preferences(user_id)
    quota = db.one("SELECT daily_credits, daily_slow_credits, updated_at FROM user_quota WHERE user_id=?", (user_id,))
    payload["quota"] = quota.to_dict() if quota else {}
    payload["api_tokens"] = [
        {**row, "revoked": bool(row["revoked"])} for row in _rows(
            "SELECT id, name, token_prefix, created_at, last_used_at, revoked FROM api_tokens WHERE user_id=? "
            "ORDER BY id", (user_id,))]
    payload["credit_ledger"] = _rows(
        "SELECT l.created_at, l.request_type, l.credits_used * 1000 AS counted_tokens, l.is_slow, l.tokens_in, "
        "l.tokens_out, "
        "m.display_name AS model, t.name AS token_name FROM credit_ledger l "
        "LEFT JOIN ai_models m ON m.id=l.model_id LEFT JOIN api_tokens t ON t.id=l.token_id "
        "WHERE l.user_id=? ORDER BY l.id", (user_id,))
    payload["image_credit_reservations"] = _rows(
        "SELECT credits_reserved * 1000 AS tokens_reserved, is_slow, created_at FROM image_credit_reservations "
        "WHERE user_id=? ORDER BY id",
        (user_id,))
    payload["quota_requests"] = _rows(
        "SELECT r.id, r.kind, r.pool, r.new_tokens, r.new_slow_tokens, r.new_weekly_tokens, r.new_rate_rules, "
        "r.new_credits, r.new_slow_credits, r.new_weekly_credits, r.new_rate_per_second, r.new_rate_burst, "
        "r.grant_hours, r.grant_unlimited, m.display_name AS model, r.effort_level, r.effort_all_models, r.reason, "
        "r.duration_type, r.status, r.resolution_source, r.admin_message, r.created_at, r.resolved_at "
        "FROM quota_requests r LEFT JOIN ai_models m ON m.id=r.model_id WHERE r.user_id=? ORDER BY r.id",
        (user_id,))
    limits = db.one("SELECT l.speed, l.tier_locked, l.dynamic_exempt, l.effort_gating_off, l.usage_reset_at, "
                    "l.weekly_reset_at, "
                    "l.tier_changed_at, t.name AS tier FROM user_limits l LEFT JOIN limit_tiers t ON t.id=l.tier_id "
                    "WHERE l.user_id=?", (user_id,))
    payload["limits"] = {
        "settings": limits.to_dict() if limits else {},
        "custom_limits": _rows(
            "SELECT pool, rate_rules, window_tokens, window_slow_tokens, weekly_tokens, updated_at "
            "FROM user_limit_overrides WHERE user_id=? ORDER BY pool", (user_id,)),
        "model_limits": _rows(
            "SELECT m.display_name AS model, o.rate_rules, o.window_tokens, o.weekly_tokens, o.locked, o.updated_at "
            "FROM user_model_limits o LEFT JOIN ai_models m ON m.id=o.model_id WHERE o.user_id=? ORDER BY o.model_id",
            (user_id,)),
        "reasoning_effort": _rows(
            "SELECT m.display_name AS model, e.model_id IS NULL AS all_models, e.level, e.pinned, e.source, "
            "e.updated_at FROM user_effort_levels e LEFT JOIN ai_models m ON m.id=e.model_id WHERE e.user_id=? "
            "ORDER BY e.id", (user_id,)),
        "windows": _rows(
            "SELECT scope, window_started_at, week_started_at FROM limit_windows WHERE user_id=? ORDER BY scope",
            (user_id,)),
        "grants": _rows(
            "SELECT g.pool, m.display_name AS model, g.scope, g.kind, g.amount, g.starts_at, g.ends_at, g.reason, "
            "g.created_at, g.revoked_at FROM limit_grants g LEFT JOIN ai_models m ON m.id=g.model_id "
            "WHERE g.user_id=? ORDER BY g.id", (user_id,)),
    }
    payload["access_memberships"] = _rows(
        "SELECT scope, resource_id, list_type, reason, expires_at, created_at, updated_at "
        "FROM model_access_memberships WHERE user_id=? ORDER BY created_at", (user_id,))
    payload["access_requests"] = _rows(
        "SELECT id, scope, resource_id, use_case, confirmed_safe, confirmed_logging, status, admin_message, "
        "created_at, resolved_at FROM model_access_requests WHERE user_id=? ORDER BY id", (user_id,))
    payload["personalities"] = _rows(
        "SELECT id, kind, name, description, avatar, color, instructions, greeting, starters, preferred_model, "
        "response_length, creativity, is_enabled, admin_disabled, disabled_until, disabled_reason, "
        "share_token IS NOT NULL AS shared, created_at, updated_at FROM personalities WHERE user_id=? ORDER BY id",
        (user_id,))
    payload["default_personality_id"] = db.scalar(
        "SELECT personality_id FROM personality_defaults WHERE user_id=?", (user_id,))
    payload["sign_in_sessions"] = _rows(
        "SELECT created_at, last_seen_at, expires_at, user_agent, ip_address FROM auth_sessions WHERE user_id=? "
        "ORDER BY created_at", (user_id,))
    payload["audit_log"] = _rows(
        "SELECT action, target, details, ip_address, created_at FROM audit_log WHERE actor_id=? ORDER BY id",
        (user_id,))
    payload["incognito_audit"] = _rows(
        "SELECT i.session_id, i.role, i.content, i.created_at, m.display_name AS model FROM incognito_audit i "
        "LEFT JOIN ai_models m ON m.id=i.model_id WHERE i.user_id=? ORDER BY i.id", (user_id,))
    if "deleted_at" in _columns("chat_sessions"):
        payload["deleted_chats_awaiting_erasure"] = db.scalar(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND deleted_at IS NOT NULL", (user_id,), 0)
    payload["chats"] = _chats(user_id, include_images=True)
    payload["agent_tasks"] = _agent_tasks(user_id)
    payload["notes"] = {
        "images": f"Images up to {INLINE_IMAGE_MAX_BYTES // 1024} KB are included as base64 "
                  f"(at most {INLINE_IMAGES_TOTAL_BYTES // (1024 * 1024)} MB in total); larger ones are listed "
                  "with their SHA-256 only.",
        "secrets": "Password, API token and session hashes are never exported.",
    }
    return payload
