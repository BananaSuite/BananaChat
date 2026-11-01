"""Personal data exports (the account page and the administrator use the same functions).

* :func:`gdpr_export` - everything stored about one account: profile (never
  the password hash), preferences, quota and limits (custom limits, tier,
  speed, the account's own grants), API token metadata with scope and expiry
  (never the token hash), whether two-step verification is on (since when,
  how many recovery codes are left; never the authenticator secret or the
  recovery-code digests), the credit ledger, access-list memberships and
  requests, quota requests, personalities, sign-in sessions (never the
  session hash), the account's own audit-log entries, every chat that has
  not been deleted with its messages and attachments, and the account's
  agent tasks with their steps, sub-agents and follow-up messages (never the
  task's lease or sandbox identifiers).
* :func:`chats_export` - chats and messages only.

Attachments are exported as metadata plus their extracted text. Image bytes
are included as base64 only while each image is at most
``INLINE_IMAGE_MAX_BYTES`` and the export's inline images stay within
``INLINE_IMAGES_TOTAL_BYTES``; other images carry ``"image_included": false``
(their SHA-256 still identifies them). Both functions return plain,
JSON-serialisable dicts.

:func:`stream` writes the same JSON document piece by piece for downloads:
chats, messages, attachment text and the longer lists are read in batches, so
memory stays bounded whatever the account stores.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

from bananachat import __version__, db
from bananachat.db import users

EXPORT_FORMAT_VERSION = 2
INLINE_IMAGE_MAX_BYTES = 512 * 1024
INLINE_IMAGES_TOTAL_BYTES = 20 * 1024 * 1024
# Chats whose message counts are read together.
SESSION_BATCH = 200
# Messages read together (each at most BC_CHAT_MAX_MESSAGE_KB), and rows of the longer lists.
MESSAGE_BATCH = 100
ROW_BATCH = 500
# Attachment text and inline images read together (one larger item is read on its own).
CONTENT_BATCH_BYTES = 4 * 1024 * 1024
# Output is handed to the server in pieces of about this size.
CHUNK_BYTES = 64 * 1024


class _Lazy:
    """A list in the export that is read while it is written (see :func:`stream`)."""

    def __init__(self, items: Iterable):
        self.items = items

    def __iter__(self):
        return iter(self.items)


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


def _session_batches(user_id: str) -> Iterator[list[dict]]:
    deleted_clause = " AND s.deleted_at IS NULL" if "deleted_at" in _columns("chat_sessions") else ""
    after = ("", "")
    while True:
        batch = _rows(
            "SELECT s.id, s.title, s.is_incognito, s.created_at, s.updated_at, p.name AS personality "
            "FROM chat_sessions s LEFT JOIN personalities p ON p.id=s.personality_id "
            f"WHERE s.user_id=?{deleted_clause} AND (s.created_at>? OR (s.created_at=? AND s.id>?)) "
            "ORDER BY s.created_at, s.id LIMIT ?",
            (user_id, after[0], after[0], after[1], SESSION_BATCH))
        if not batch:
            return
        yield batch
        after = (batch[-1]["created_at"], batch[-1]["id"])


_MESSAGE_SELECT = ("SELECT m.session_id, m.id, m.role, m.content, m.created_at, m.tokens_in, m.tokens_out, "
                   "a.display_name AS model, a.ollama_name AS model_id FROM chat_messages m "
                   "LEFT JOIN ai_models a ON a.id=m.model_id ")


def _message_pages(batch: list[dict]) -> Iterator[list[dict]]:
    """The batch's messages in export order (chat by chat, oldest first), at most MESSAGE_BATCH at a time.

    Small chats are read together; a chat with more messages is read in pages of its own.
    """
    placeholders = ",".join("?" for _ in batch)
    counts = {row["session_id"]: row["n"] for row in db.query(
        f"SELECT session_id, COUNT(*) AS n FROM chat_messages WHERE session_id IN ({placeholders}) "
        "GROUP BY session_id", [chat["id"] for chat in batch])}
    group: list[str] = []
    size = 0

    def read_group():
        marks = ",".join("?" for _ in group)
        rows = _rows(_MESSAGE_SELECT + f"WHERE m.session_id IN ({marks}) ORDER BY m.session_id, m.id", group)
        position = {chat_id: index for index, chat_id in enumerate(group)}
        rows.sort(key=lambda row: (position[row["session_id"]], row["id"]))
        return rows

    for chat in batch:
        count = counts.get(chat["id"], 0)
        if count > MESSAGE_BATCH:
            if group:
                yield read_group()
                group, size = [], 0
            # Messages added meanwhile are not included.
            last = db.scalar("SELECT MAX(id) FROM chat_messages WHERE session_id=?", (chat["id"],), 0)
            cursor = 0
            while True:
                rows = _rows(_MESSAGE_SELECT + "WHERE m.session_id=? AND m.id>? AND m.id<=? ORDER BY m.id LIMIT ?",
                             (chat["id"], cursor, last, MESSAGE_BATCH))
                if not rows:
                    break
                yield rows
                cursor = rows[-1]["id"]
            continue
        if group and size + count > MESSAGE_BATCH:
            yield read_group()
            group, size = [], 0
        group.append(chat["id"])
        size += count
    if group:
        yield read_group()


def _attachment_contents(wanted: list[tuple[str, int]]) -> Iterator[tuple[str, dict | None]]:
    """(id, row with extracted_text and image_data) for each wanted (id, length), in order, read in batches."""
    chunk: list[str] = []
    size = 0

    def read():
        marks = ",".join("?" for _ in chunk)
        found = {row["id"]: row for row in db.query(
            f"SELECT id, extracted_text, image_data FROM chat_attachments WHERE id IN ({marks})", chunk)}
        return [(attachment_id, found.get(attachment_id)) for attachment_id in chunk]

    for attachment_id, length in wanted:
        if chunk and size + length > CONTENT_BATCH_BYTES:
            yield from read()
            chunk, size = [], 0
        chunk.append(attachment_id)
        size += length
    if chunk:
        yield from read()


def _with_attachments(messages: list[dict], budget: dict, include_images: bool) -> Iterator[dict]:
    """The messages with their attachments, in order; attachment text and images are read in batches."""
    if not messages:
        return
    placeholders = ",".join("?" for _ in messages)
    by_message: dict[int, list] = {}
    for row in db.query(
            "SELECT id, message_id, kind, filename, media_type, size_bytes, sha256, created_at, "
            "length(CAST(extracted_text AS BLOB)) AS text_length, "
            "CASE WHEN image_data IS NOT NULL AND length(image_data)<=? THEN length(image_data) END AS image_length "
            f"FROM chat_attachments WHERE message_id IN ({placeholders}) ORDER BY created_at, id",
            (INLINE_IMAGE_MAX_BYTES if include_images else -1, *(message["id"] for message in messages))):
        by_message.setdefault(row["message_id"], []).append(row.to_dict())
    # The inline-image budget is spent in export order, oldest attachment of a message first.
    wanted = []
    for message in messages:
        for item in by_message.get(message["id"], []):
            if item["kind"] == "image":
                image_length = item["image_length"]
                item["image_included"] = image_length is not None and image_length <= budget["left"]
                if item["image_included"]:
                    budget["left"] -= image_length
                    wanted.append((item["id"], image_length))
            elif item["text_length"] is not None:
                wanted.append((item["id"], item["text_length"]))
    contents = _attachment_contents(wanted)
    for message in messages:
        # A new dict, so the page does not keep the attachment text of the messages already written.
        exported_message = {key: value for key, value in message.items() if key != "id"}
        attachments = []
        for item in by_message.pop(message["id"], []):
            text_length, image_length = item.pop("text_length"), item.pop("image_length")
            item.pop("message_id")
            if item["kind"] == "image":
                included = item.pop("image_included")
                content = next(contents)[1] if included else None
                image = content["image_data"] if content is not None else None
                included = image is not None
                exported = {key: item[key] for key in ("id", "kind", "filename", "media_type", "size_bytes",
                                                        "sha256", "created_at")}
                exported["image_included"] = included
                if included:
                    exported["image_base64"] = base64.b64encode(image).decode("ascii")
            else:
                content = next(contents)[1] if text_length is not None else None
                exported = {key: item[key] for key in ("id", "kind", "filename", "media_type", "size_bytes",
                                                        "sha256")}
                exported["extracted_text"] = content["extracted_text"] if content is not None else None
                exported["created_at"] = item["created_at"]
            attachments.append(exported)
        exported_message["attachments"] = attachments
        yield exported_message


class _ByChat:
    """Splits the batch's messages (in export order) into one lazy list per chat."""

    def __init__(self, messages: Iterator[dict]):
        self.messages = messages
        self.pending: dict | None = None

    def of(self, chat_id: str) -> Iterator[dict]:
        while True:
            if self.pending is None:
                self.pending = next(self.messages, None)
                if self.pending is None:
                    return
            if self.pending["session_id"] != chat_id:
                return
            message, self.pending = self.pending, None
            del message["session_id"]
            yield message


def _iter_chats(user_id: str, *, include_images: bool) -> Iterator[dict]:
    """Each chat that has not been deleted, its ``messages`` a :class:`_Lazy` list read while it is written.

    Consume each chat's messages before asking for the next chat.
    """
    budget = {"left": INLINE_IMAGES_TOTAL_BYTES}
    for batch in _session_batches(user_id):
        by_chat = _ByChat(message for page in _message_pages(batch)
                          for message in _with_attachments(page, budget, include_images))
        for chat in batch:
            chat["is_incognito"] = bool(chat["is_incognito"])
            chat["messages"] = _Lazy(by_chat.of(chat["id"]))
            yield chat


def _keyset(sql: str, params: tuple, key: str) -> Iterator[dict]:
    """Rows of *sql* read ROW_BATCH at a time; *sql* ends with ``AND <key>>?`` and ``ORDER BY <key> LIMIT ?``.

    *key* is the name of the selected integer column the batches continue from; it is not exported.
    """
    cursor = -1
    while True:
        rows = _rows(sql, (*params, cursor, ROW_BATCH))
        for row in rows:
            cursor = row.pop(key)
            yield row
        if len(rows) < ROW_BATCH:
            return


def _agent_tasks(user_id: str) -> Iterator[dict]:
    if not _columns("agent_tasks"):
        return
    after = ("", "")
    while True:
        tasks = _rows(
            "SELECT id, title, prompt, status, model_name, swarm, runs, steps_used, tool_calls, tokens_in, "
            "tokens_out, summary, error, notice, created_at, started_at, updated_at, finished_at FROM agent_tasks "
            "WHERE user_id=? AND (created_at>? OR (created_at=? AND id>?)) ORDER BY created_at, id LIMIT ?",
            (user_id, after[0], after[0], after[1], ROW_BATCH))
        for task in tasks:
            task["swarm"] = bool(task["swarm"])
            task["steps"] = _Lazy(_keyset(
                "SELECT id AS key, agent, kind, content, thinking, tool_name, tool_args, tool_result, tool_status, "
                "tokens_in, tokens_out, duration_ms, created_at FROM agent_steps WHERE task_id=? AND id>? "
                "ORDER BY id LIMIT ?", (task["id"],), "key"))
            task["sub_agents"] = _rows(
                "SELECT agent, title, instructions, status, steps_used, summary, created_at, finished_at "
                "FROM agent_lanes WHERE task_id=? ORDER BY agent", (task["id"],))
            task["messages"] = _Lazy(_keyset(
                "SELECT id AS key, content, created_at, consumed_at FROM agent_messages WHERE task_id=? AND id>? "
                "ORDER BY id LIMIT ?", (task["id"],), "key"))
            yield task
        if len(tasks) < ROW_BATCH:
            return
        after = (tasks[-1]["created_at"], tasks[-1]["id"])


def _two_step(user_id: str) -> dict:
    """Whether two-step verification is on, without its secret or recovery-code digests."""
    row = db.one("SELECT enabled_at, recovery_hashes FROM user_two_step WHERE user_id=?", (user_id,)) \
        if _columns("user_two_step") else None
    if row is None:
        return {"enabled": False, "enabled_at": None, "recovery_codes_left": 0}
    try:
        digests = json.loads(row["recovery_hashes"] or "[]")
    except ValueError:
        digests = []
    return {"enabled": True, "enabled_at": row["enabled_at"],
            "recovery_codes_left": len(digests) if isinstance(digests, list) else 0}


def _chats_payload(user_id: str) -> dict:
    user = users.get(user_id)
    payload = _header("chats_export", user)
    payload["chats"] = _Lazy(_iter_chats(user_id, include_images=False) if user else ())
    return payload


def _gdpr_payload(user_id: str) -> dict:
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
    now = db.now()
    payload["api_tokens"] = [
        {**row, "revoked": bool(row["revoked"]), "expired": bool(row["expires_at"]) and row["expires_at"] <= now}
        for row in _rows(
            "SELECT id, name, token_prefix, scope, created_at, expires_at, last_used_at, revoked FROM api_tokens "
            "WHERE user_id=? ORDER BY id", (user_id,))]
    payload["two_step"] = _two_step(user_id)
    payload["credit_ledger"] = _Lazy(_keyset(
        "SELECT l.id AS key, l.created_at, l.request_type, l.credits_used * 1000 AS counted_tokens, l.is_slow, l.tokens_in, "
        "l.tokens_out, "
        "m.display_name AS model, t.name AS token_name FROM credit_ledger l "
        "LEFT JOIN ai_models m ON m.id=l.model_id LEFT JOIN api_tokens t ON t.id=l.token_id "
        "WHERE l.user_id=? AND l.id>? ORDER BY l.id LIMIT ?", (user_id,), "key"))
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
    payload["audit_log"] = _Lazy(_keyset(
        "SELECT id AS key, action, target, details, ip_address, created_at FROM audit_log WHERE actor_id=? "
        "AND id>? ORDER BY id LIMIT ?", (user_id,), "key"))
    payload["incognito_audit"] = _Lazy(_keyset(
        "SELECT i.id AS key, i.session_id, i.role, i.content, i.created_at, m.display_name AS model "
        "FROM incognito_audit i LEFT JOIN ai_models m ON m.id=i.model_id WHERE i.user_id=? AND i.id>? "
        "ORDER BY i.id LIMIT ?", (user_id,), "key"))
    if "deleted_at" in _columns("chat_sessions"):
        payload["deleted_chats_awaiting_erasure"] = db.scalar(
            "SELECT COUNT(*) FROM chat_sessions WHERE user_id=? AND deleted_at IS NOT NULL", (user_id,), 0)
    payload["chats"] = _Lazy(_iter_chats(user_id, include_images=True))
    payload["agent_tasks"] = _Lazy(_agent_tasks(user_id))
    payload["notes"] = {
        "images": f"Images up to {INLINE_IMAGE_MAX_BYTES // 1024} KB are included as base64 "
                  f"(at most {INLINE_IMAGES_TOTAL_BYTES // (1024 * 1024)} MB in total); larger ones are listed "
                  "with their SHA-256 only.",
        "secrets": "Password, API token and session hashes, the two-step verification secret and its recovery "
                   "codes are never exported.",
    }
    return payload


def _resolved(value):
    """*value* with every lazy list read into a plain list, in document order."""
    if isinstance(value, _Lazy):
        return [_resolved(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolved(item) for key, item in value.items()}
    return value


def _holds_lazy(value) -> bool:
    return isinstance(value, _Lazy) or (isinstance(value, dict) and any(map(_holds_lazy, value.values())))


def _pieces(value, level: int = 0) -> Iterator[str]:
    """``json.dumps(value, ensure_ascii=False, indent=2, default=str)`` in pieces, reading lazy lists as it goes."""
    if not _holds_lazy(value):
        yield json.dumps(value, ensure_ascii=False, indent=2, default=str).replace("\n", "\n" + "  " * level)
        return
    is_dict = isinstance(value, dict)
    opening, closing = ("{", "}") if is_dict else ("[", "]")
    separator = "\n" + "  " * (level + 1)
    empty = True
    for item in (value.items() if is_dict else value):
        yield (opening if empty else ",") + separator
        empty = False
        if is_dict:
            key, item = item
            yield json.dumps(key, ensure_ascii=False) + ": "
        yield from _pieces(item, level + 1)
    yield opening + closing if empty else "\n" + "  " * level + closing


def chats_export(user_id: str) -> dict:
    """The account's chats (not deleted) with their messages and attachment metadata."""
    return _resolved(_chats_payload(user_id))


def gdpr_export(user_id: str) -> dict:
    """Everything stored about an account, without password, token or session hashes."""
    return _resolved(_gdpr_payload(user_id))


def stream(kind: str, user_id: str) -> Iterator[str]:
    """The ``"data"`` (:func:`gdpr_export`) or ``"chats"`` export as JSON text, written in pieces.

    Reads the database while it is iterated, so iterate it inside an application context.
    """
    payload = _gdpr_payload(user_id) if kind == "data" else _chats_payload(user_id)
    pieces, size = [], 0
    for piece in _pieces(payload):
        pieces.append(piece)
        size += len(piece)
        if size >= CHUNK_BYTES:
            yield "".join(pieces)
            pieces, size = [], 0
    if pieces:
        yield "".join(pieces)
