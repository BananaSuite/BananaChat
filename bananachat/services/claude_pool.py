"""Claude subscription pool: shared 5-hour/weekly quota over several site accounts.

The site holds Claude *subscriptions* (three accounts), not an API key. Users
pick Claude models in BananaChat (``backend='claude'``, ``provider='claude'``);
on the backend the pool picks a healthy subscription account and talks to the
Claude site with it.

Token model (same units as the rest of the limits):

* Claude models **do not count toward the pool (service) limits by default**
  (``counts_toward_pool`` defaults to off for non-local providers, see
  ``services.limits``); an administrator can switch counting on per model.
  Instead each Claude model has its own **strict token limits**: a 5-hour
  window (tokens) and an optional weekly window (off by default), plus request
  rates. Opus is strict (heavy), Sonnet moderate, Haiku light.
* The pool reports its remaining 5-hour/weekly quota to the limit system via
  ``limits.register_capacity_provider("claude", ...)``. While less than half
  of the pooled allowance is left, every Claude model's limits shrink in
  proportion (to zero when nothing is left), on top of dynamic adjustment for
  site demand (queue use, active people) and the account's usage pattern.
* Reasoning effort: Claude models offer ``off/low/medium/high/max``
  (``max`` is the "extra" tier). Site default is medium; gating can be
  switched on per model or globally. Users request higher tiers through the
  normal quota-request flow (``kind="effort"``); approving ``max`` also allows
  ``high`` and below (the ceiling covers every level at or below it), and the
  automatic unlock (sustained use) promotes one level at a time. Quotas can be
  disabled globally (effort gating off, model limits off) or per account
  (``effort_gating_off``, per-model locks/overrides).
* Enrollment: curated Claude models sync into the catalog like Ollama models
  (``new`` waiting for review, or ``auto`` with reasonable strict limits when
  the enrollment policy is ``automatic``; the ignore list skips models).
  Administrators select which Claude models to offer and which to ignore.
  Deprecated or long-missing models retire/remove through the standard
  lifecycle (``retire_due``/``purge_missing``).

The actual HTTP conversation with the Claude site is site-specific (session
cookies per subscription account) and lives behind :func:`chat_stream`, which
currently routes through the configured site client when present and otherwise
raises an informative error. Quota scraping (:func:`refresh_quota`) likewise
calls the configured reporter when present, else keeps the last reported
values. This keeps the pool, limits and enrollment testable without
credentials while leaving one integration point to implement.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from bananachat import db
from bananachat.db import catalog
from bananachat.db import claude_pool as store

log = logging.getLogger("bananachat.claude")

PROVIDER = "claude"
BACKEND = "claude"

# Curated Claude models offered for enrollment. ``family`` drives strictness:
# opus (heavy/strict), sonnet (standard), haiku (light). ``reasoning`` lists
# the effort levels the model takes in the chat composer.
CURATED = (
    # Current generation. Thinking cannot be switched off on Fable 5.1 and Opus 5.5 (effort is the only control),
    # so they offer no "off" level; "max" is the extra tier.
    {"name": "claude-fable-5-1", "display": "Claude Fable 5.1", "family": "fable",
     "description": "Anthropic's most capable model; the strictest limits.", "reasoning": ["low", "medium", "high", "max"]},
    {"name": "claude-opus-5-5", "display": "Claude Opus 5.5", "family": "opus",
     "description": "The current Opus for demanding work; strict limits.", "reasoning": ["low", "medium", "high", "max"]},
    {"name": "claude-sonnet-5-5", "display": "Claude Sonnet 5.5", "family": "sonnet",
     "description": "The current Sonnet for everyday work.", "reasoning": ["off", "low", "medium", "high", "max"]},
    # Earlier generation, kept so sites that enrolled them keep them.
    {"name": "claude-opus-4-1", "display": "Claude Opus 4.1", "family": "opus",
     "description": "Most powerful Claude; strictest limits.", "reasoning": ["off", "low", "medium", "high", "max"]},
    {"name": "claude-sonnet-4-5", "display": "Claude Sonnet 4.5", "family": "sonnet",
     "description": "Balanced Claude for everyday work.", "reasoning": ["off", "low", "medium", "high", "max"]},
    {"name": "claude-haiku-4-5", "display": "Claude Haiku 4.5", "family": "haiku",
     "description": "Fast, light Claude for quick tasks.", "reasoning": ["off", "low", "medium", "high"]},
)

# Strict per-model defaults (tokens per 5-hour window; weekly off by default).
# Opus is strict, Sonnet moderate, Haiku generous — all token-based.
STRICTNESS = {
    "fable": {"preset": "heavy", "weight": 5.0, "counts_toward_pool": False,
              "rate_rules": [{"requests": 4, "per": "minute", "burst": 2},
                             {"requests": 100, "per": "day", "burst": 100}],
              "window_tokens": 30_000, "weekly_tokens": None, "dynamic": True,
              "sensitivity": 2.5, "effort_default": "low"},
    "opus": {"preset": "heavy", "weight": 3.0, "counts_toward_pool": False,
             "rate_rules": [{"requests": 6, "per": "minute", "burst": 3},
                            {"requests": 200, "per": "day", "burst": 200}],
             "window_tokens": 50_000, "weekly_tokens": None, "dynamic": True,
             "sensitivity": 2.0, "effort_default": "low"},
    "sonnet": {"preset": "standard", "weight": 1.5, "counts_toward_pool": False,
               "rate_rules": [{"requests": 30, "per": "minute", "burst": 10},
                              {"requests": 1000, "per": "day", "burst": 1000}],
               "window_tokens": 150_000, "weekly_tokens": None, "dynamic": True,
               "sensitivity": 1.0, "effort_default": "medium"},
    "haiku": {"preset": "light", "weight": 0.5, "counts_toward_pool": False,
              "rate_rules": [{"requests": 60, "per": "minute", "burst": 20}],
              "window_tokens": 300_000, "weekly_tokens": None, "dynamic": False,
              "sensitivity": 0.5, "effort_default": "medium"},
}

POOL_STATE_KEY = "claude_pool_quota"
POOL_CACHE_SECONDS = 30

COOLDOWN_ERROR = timedelta(minutes=5)  # an account whose request failed rests this long before it is tried again


class QuotaExhausted(Exception):
    """Raised by the site handler when the Claude site says an account is out of quota.

    ``resets_at`` (an aware datetime, optional) is when it has quota again; the
    pool rests the account until then (or until its 5-hour window ends) and
    moves on to the next one.
    """

    def __init__(self, message: str = "", resets_at: datetime | None = None):
        super().__init__(message or "The Claude subscription account is out of quota.")
        self.resets_at = resets_at


_site_reporter = None   # () -> {"window_left": 0-1|None, "weekly_left": 0-1|None}
_site_chat = None       # (account, model_name, messages, options) -> stream generator
_pool_cache: dict = {"at": 0.0, "value": None}


def register_site_reporter(report) -> None:
    """Register how to read the pooled Claude quota: ``report()`` returns
    ``{"window_left": 0-1|None, "weekly_left": 0-1|None}`` (fractions left)."""
    global _site_reporter
    _site_reporter = report


def register_site_chat(handler) -> None:
    """Register the site-specific Claude conversation handler."""
    global _site_chat
    _site_chat = handler


def family_of(model_name: str) -> str:
    lowered = (model_name or "").lower()
    if "fable" in lowered or "mythos" in lowered:
        return "fable"
    if "opus" in lowered:
        return "opus"
    if "haiku" in lowered:
        return "haiku"
    return "sonnet"


def strict_policy(model_name: str) -> dict:
    """The strict default model policy for a Claude model (see STRICTNESS)."""
    base = dict(STRICTNESS[family_of(model_name)])
    return {"preset": base["preset"], "enabled": True, "weight": base["weight"],
            "counts_toward_pool": base["counts_toward_pool"], "rate_rules": list(base["rate_rules"]),
            "window_tokens": base["window_tokens"], "weekly_tokens": base["weekly_tokens"],
            "dynamic": base["dynamic"], "sensitivity": base["sensitivity"], "auto_tiers": False,
            "effort_default": base["effort_default"]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _current(row, now: datetime | None = None) -> tuple[int, int]:
    """``(5-hour used, weekly used)`` of an account now: a window whose reset time has passed counts as empty."""
    now = now or _now()
    window_reset = db.parse_timestamp(row["window_resets_at"])
    weekly_reset = db.parse_timestamp(row["weekly_resets_at"])
    window = 0 if window_reset is not None and window_reset <= now else int(row["window_used"] or 0)
    weekly = 0 if weekly_reset is not None and weekly_reset <= now else int(row["weekly_used"] or 0)
    return window, weekly


def _resting(row, now: datetime | None = None) -> bool:
    """Whether the account is resting after the site reported it out of quota (or a failed request)."""
    try:
        until = row["cooldown_until"]
    except (IndexError, KeyError):
        return False
    moment = db.parse_timestamp(until)
    return moment is not None and moment > (now or _now())


def usable(row, now: datetime | None = None) -> bool:
    """Active, not resting, and with 5-hour and weekly quota left (as far as the pool knows)."""
    if row["status"] != "active" or _resting(row, now):
        return False
    window, weekly = _current(row, now)
    if row["window_limit"] and window >= int(row["window_limit"]):
        return False
    return not (row["weekly_limit"] and weekly >= int(row["weekly_limit"]))


def accounts_in_order(exclude: set[int] | None = None, now: datetime | None = None) -> list:
    """Usable accounts, best first: highest priority, then the most 5-hour quota left."""
    exclude = exclude or set()
    now = now or _now()
    rows = [row for row in store.list_accounts(include_disabled=False)
            if row["id"] not in exclude and usable(row, now)]

    def left(row) -> float:
        if not row["window_limit"]:
            return 1.0
        return 1 - _current(row, now)[0] / float(row["window_limit"])

    return sorted(rows, key=lambda row: (-int(row["priority"] or 0), -left(row), row["id"]))


def pick_account(exclude: set[int] | None = None):
    """The best usable subscription account (highest priority, then most quota left), or None."""
    ordered = accounts_in_order(exclude)
    return ordered[0] if ordered else None


def _fractions_from_accounts(rows, now: datetime | None = None) -> tuple[float | None, float | None]:
    """Pooled ``(window_left, weekly_left)`` fractions 0-1 from reported usage.

    The pool's quota is the sum over its active accounts (a resting account
    counts as empty): one exhausted subscription only removes its own share,
    the others keep serving. With no active account at all nothing is left (0).
    Accounts without a known limit say nothing (None when none has one).
    """
    now = now or _now()
    active = [row for row in rows if row["status"] == "active"]
    if not active:
        return 0.0, 0.0
    fractions = []
    for limit_key, index in (("window_limit", 0), ("weekly_limit", 1)):
        total = left = 0.0
        for row in active:
            limit = float(row[limit_key] or 0)
            if limit <= 0:
                continue
            total += limit
            if not _resting(row, now):
                left += max(0.0, limit - _current(row, now)[index])
        fractions.append(max(0.0, min(1.0, left / total)) if total > 0 else None)
    if all(_resting(row, now) for row in active):
        return 0.0, 0.0
    return fractions[0], fractions[1]


def pooled_quota(force: bool = False) -> dict:
    """Cached pooled quota ``{"window_left", "weekly_left", "accounts"}``."""
    now = time.monotonic()
    if not force and _pool_cache["value"] is not None and now - _pool_cache["at"] < POOL_CACHE_SECONDS \
            and _pool_cache.get("path") == db.path():
        return _pool_cache["value"]
    rows = store.list_accounts(include_disabled=False)
    window_left, weekly_left = _fractions_from_accounts(rows)
    if _site_reporter is not None:
        try:
            reported = _site_reporter() or {}
            if reported.get("window_left") is not None:
                value = max(0.0, min(1.0, float(reported["window_left"])))
                window_left = value if window_left is None else min(window_left, value)
            if reported.get("weekly_left") is not None:
                value = max(0.0, min(1.0, float(reported["weekly_left"])))
                weekly_left = value if weekly_left is None else min(weekly_left, value)
        except Exception:  # noqa: BLE001 - a broken reporter never blocks admission
            log.warning("Claude quota reporter failed", exc_info=True)
    value = {"window_left": window_left, "weekly_left": weekly_left,
             "accounts": len([r for r in rows if r["status"] == "active"]),
             "usable": len([r for r in rows if usable(r)])}
    _pool_cache.update(at=now, value=value, path=db.path())
    try:
        from bananachat.db import settings as site_settings
        site_settings.state_set(POOL_STATE_KEY, {**value, "at": time.time()})
    except Exception:  # noqa: BLE001 - informational only
        pass
    return value


def capacity_report(model):
    """``limits.Capacity`` for a Claude model from the pooled quota."""
    from bananachat.services import limits as limits_service

    quota = pooled_quota()
    return limits_service.Capacity(window_left=quota["window_left"], weekly_left=quota["weekly_left"])


def ensure_registered() -> None:
    """Hook the pooled quota into dynamic model limits (idempotent)."""
    from bananachat.services import limits as limits_service

    limits_service.register_capacity_provider(PROVIDER, capacity_report)


def refresh_quota() -> dict:
    """Re-read the pooled quota now (reporter + stored account usage)."""
    _pool_cache.update(at=0.0, value=None)
    return pooled_quota(force=True)


def record_usage(account_id: int, tokens_in: int, tokens_out: int) -> None:
    """Add one Claude answer to an account's 5-hour/weekly counters.

    ``window_resets_at``/``weekly_resets_at`` hold when the current window
    ends (what the Claude site reports too): an answer after that starts a new
    window of 5 hours (a week) from now.
    """
    from bananachat.db import limits as limits_db

    now = _now()
    total = max(0, int(tokens_in or 0)) + max(0, int(tokens_out or 0))
    with db.transaction():
        row = store.get(account_id)
        if row is None:
            return
        window_reset = db.parse_timestamp(row["window_resets_at"])
        if window_reset is None or window_reset <= now:
            window_used, window_reset = 0, now + timedelta(seconds=limits_db.WINDOW_SECONDS)
        else:
            window_used = int(row["window_used"] or 0)
        weekly_reset = db.parse_timestamp(row["weekly_resets_at"])
        if weekly_reset is None or weekly_reset <= now:
            weekly_used, weekly_reset = 0, now + timedelta(seconds=limits_db.WEEK_SECONDS)
        else:
            weekly_used = int(row["weekly_used"] or 0)
        db.execute("UPDATE claude_accounts SET window_used=?, window_resets_at=?, weekly_used=?, "
                   "weekly_resets_at=?, updated_at=? WHERE id=?",
                   (window_used + total, db.timestamp(window_reset), weekly_used + total, db.timestamp(weekly_reset),
                    db.now(), account_id))
    _pool_cache.update(at=0.0, value=None)


def rest(account_id: int, until: datetime, error: str = "") -> None:
    """Skip an account until *until* (the site said it is out of quota, or a request failed)."""
    db.execute("UPDATE claude_accounts SET cooldown_until=?, last_error=?, updated_at=? WHERE id=?",
               (db.timestamp(until), (error or "")[:500], db.now(), account_id))
    _pool_cache.update(at=0.0, value=None)


# ----- catalog sync (curated models, selectable, auto/manual enroll) -------------

def _loads(value, default):
    import json as _json
    try:
        result = _json.loads(value) if isinstance(value, str) else default
    except ValueError:
        return default
    return result if isinstance(result, type(default)) else default


def sync_catalog(*, selected: list[str] | None = None, source: str = "background") -> dict:
    """Reconcile curated Claude models with the catalog.

    *selected* (None: all curated) chooses which Claude models exist; models
    not selected stay absent (administrators select which to use and which to
    ignore via the ignore list / enrollment). New models follow the
    model-lifecycle enrollment policy: ``manual`` waits for review (``new``),
    ``automatic`` enables at once with strict per-family limits. Returns
    ``{"count", "new", "enabled", "missing"}``.
    """
    from bananachat.db import limits as limits_db
    from bananachat.services import model_lifecycle

    rules = model_lifecycle.patterns()
    policy = model_lifecycle.policy()
    if selected is None and source == "background" and policy.get("enrollment") != "automatic":
        # Background sync with manual enrollment never enrolls new curated models
        # on its own; administrators add them explicitly from the Claude page.
        # It only refreshes models already in the catalog.
        existing_names = {row["ollama_name"] for row in db.query("SELECT ollama_name FROM ai_models "
                                                                 "WHERE backend='claude'")}
        wanted = [item for item in CURATED if item["name"] in existing_names]
    else:
        wanted = [item for item in CURATED if selected is None or item["name"] in set(selected)]
    state: dict = {"at": db.now(), "source": source, "ok": True, "count": len(wanted), "new": [],
                   "enabled": [], "missing": [], "note": ""}
    with db.transaction():
        for item in wanted:
            if model_lifecycle.matches(item["name"], rules):
                continue
            existing = catalog.get_by_name(item["name"])
            if existing is not None:
                if existing["backend"] != BACKEND:
                    continue
                db.execute("UPDATE ai_models SET backend_available=1, backend_last_seen_at=?, "
                           "display_name=CASE WHEN display_name='' THEN ? ELSE display_name END WHERE id=?",
                           (db.now(), item["display"], existing["id"]))
                if existing["missing_at"]:
                    db.execute("UPDATE ai_models SET missing_at=NULL, missing_reason=NULL WHERE id=?",
                               (existing["id"],))
                continue
            enrollment = "ignored" if model_lifecycle.is_ignored(item["name"]) else "new"
            cursor = db.execute(
                "INSERT INTO ai_models (ollama_name, backend, provider, backend_model_name, backend_available, "
                "backend_last_seen_at, display_name, description, is_rolled_out, is_image_generation, sort_order, "
                "created_at, updated_at, enrollment, family, capabilities, reasoning_levels, state_reason, "
                "state_changed_at) VALUES (?, 'claude', 'claude', ?, 1, ?, ?, ?, 0, 0, "
                "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM ai_models), ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["name"], item["name"], db.now(), item["display"], item["description"], db.now(), db.now(),
                 enrollment, item["family"], json.dumps(["completion"]), json.dumps(item["reasoning"]),
                 "Matches the ignore list." if enrollment == "ignored" else "Claude model available; waiting for review.",
                 db.now()))
            model_id = cursor.lastrowid
            state["new"].append(item["name"])
            from bananachat.db import model_lifecycle as lifecycle_db
            lifecycle_db.add_event(model_id, item["name"], "detected",
                                   "Claude model available; waiting for review.")
        # Models no longer selected/curated become missing (standard lifecycle removes them after retention).
        names = {item["name"] for item in wanted}
        for row in db.query("SELECT * FROM ai_models WHERE backend='claude'"):
            if row["ollama_name"] not in names and not row["missing_at"]:
                reason = "No longer offered by the Claude pool; missing until removed after retention."
                db.execute("UPDATE ai_models SET backend_available=0, missing_at=?, missing_reason=?, "
                           "state_reason=?, state_changed_at=? WHERE id=?",
                           (db.now(), reason, reason, db.now(), row["id"]))
                state["missing"].append(row["ollama_name"])
    # Automatic enrollment with strict per-family limits (review later).
    if policy.get("enrollment") == "automatic":
        for row in catalog.with_enrollment("new"):
            if row["backend"] != BACKEND or row["ollama_name"] not in names:
                continue
            if model_lifecycle.matches(row["ollama_name"], rules):
                continue
            with db.transaction():
                fresh = catalog.get(row["id"])
                if fresh is None or fresh["enrollment"] != "new":
                    continue
                strict = strict_policy(row["ollama_name"])
                reason = (f"Enabled automatically with strict Claude limits "
                          f"({family_of(row['ollama_name'])} family); review it.")
                catalog.set_lifecycle(row["id"], enrollment="auto", enrolled_at=db.now(), is_rolled_out=1,
                                      limit_preset=strict["preset"], state_reason=reason)
                from bananachat.db import model_lifecycle as lifecycle_db
                lifecycle_db.add_event(row["id"], row["ollama_name"], "auto_enabled", reason)
            try:
                limits_db.set_model_policy(row["id"], strict, None)
            except ValueError:
                log.warning("Could not store strict Claude limits for %s", row["ollama_name"])
            state["enabled"].append(row["ollama_name"])
    return state


def chat_stream(model_name: str, messages: list[dict], *, options: dict | None = None, account_id: int | None = None):
    """Stream a Claude answer through the pooled subscription accounts.

    Requires :func:`register_site_chat` (the site-specific transport). The best
    usable account answers (:func:`accounts_in_order`); when the handler
    raises :class:`QuotaExhausted` or fails before producing anything, that
    account rests (until its quota comes back, or a few minutes) and the next
    one is tried. Usage is recorded against the account that answered. Raises
    ``RuntimeError`` when no account can answer.
    """
    if _site_chat is None:
        raise RuntimeError("The Claude subscription pool has no site client: register one with "
                           "claude_pool.register_site_chat(handler) (session handling for the pooled "
                           "Claude accounts), then sync the Claude catalog.")
    candidates = [store.get(account_id)] if account_id else accounts_in_order()
    candidates = [row for row in candidates if row is not None]
    if not candidates:
        raise RuntimeError("No Claude subscription account has quota left right now.")
    last_error = "No Claude subscription account could answer."
    for account in candidates:
        tokens_in = tokens_out = 0
        produced = False
        try:
            for chunk in _site_chat(account, model_name, messages, options or {}):
                # Chunks are dicts: {"text", "thinking", "tokens_in", "tokens_out", "done", "finish_reason"}.
                if isinstance(chunk, dict):
                    tokens_in += int(chunk.get("tokens_in") or 0)
                    tokens_out += int(chunk.get("tokens_out") or 0)
                    produced = produced or bool(chunk.get("text") or chunk.get("thinking"))
                yield chunk
        except QuotaExhausted as error:
            window_end = db.parse_timestamp(account["window_resets_at"])
            until = error.resets_at or (window_end if window_end and window_end > _now() else
                                        _now() + timedelta(hours=1))
            rest(account["id"], until, str(error))
            log.info("Claude account %s is out of quota until %s", account["label"], until)
            last_error = str(error)
            if produced:
                raise RuntimeError(last_error) from None
            continue
        except Exception as error:  # noqa: BLE001 - one account failing moves on to the next
            rest(account["id"], _now() + COOLDOWN_ERROR, str(error) or type(error).__name__)
            log.warning("Claude account %s failed: %s", account["label"], error)
            last_error = str(error) or "The Claude site did not answer."
            if produced:
                raise RuntimeError(last_error) from None
            continue
        finally:
            if tokens_in or tokens_out:
                try:
                    record_usage(account["id"], tokens_in, tokens_out)
                except Exception:  # noqa: BLE001 - bookkeeping must not break a completed answer
                    log.warning("Could not record Claude usage", exc_info=True)
        return
    raise RuntimeError(last_error)


# Effort levels as the chat composer names them; ``on`` (thinking models without named levels) is medium.
_EFFORTS = {"off", "low", "medium", "high", "max"}


def effort_for(think, effort: str | None = None) -> str | None:
    """The effort level to ask the Claude site for: the resolved level when known, else from Ollama's ``think``."""
    if effort in _EFFORTS:
        return effort
    if effort == "on" or think is True:
        return "medium"
    if think is False:
        return "off"
    return think if think in _EFFORTS else None


def stream_chunks(model_name: str, messages: list[dict], *, options: dict | None = None, think=None,
                  effort: str | None = None, cancel=None):
    """:func:`chat_stream` as the chunks inference reads (``ollama.Chunk``), ending with a ``done`` chunk.

    Failures become ``UpstreamError`` so inference falls back to the next
    model (a local one, when the site allows it).
    """
    from bananachat.services.ollama import Chunk
    from bananachat.services.upstream import UpstreamError

    options = dict(options or {})
    level = effort_for(think, effort)
    if level is not None:
        options["effort"] = level
    prompt = completion = None
    finish = ""
    try:
        for item in chat_stream(model_name, messages, options=options):
            if cancel is not None and getattr(cancel, "cancelled", False):
                return
            if not isinstance(item, dict):
                continue
            if item.get("tokens_in"):
                prompt = (prompt or 0) + int(item["tokens_in"])
            if item.get("tokens_out"):
                completion = (completion or 0) + int(item["tokens_out"])
            finish = item.get("finish_reason") or finish
            text, thinking = item.get("text") or "", item.get("thinking") or ""
            if text or thinking:
                yield Chunk(content=text, thinking=thinking)
            if item.get("done"):
                break
    except RuntimeError as error:
        raise UpstreamError(str(error)) from None
    yield Chunk(done=True, finish_reason=finish or "stop", prompt_tokens=prompt, completion_tokens=completion)
