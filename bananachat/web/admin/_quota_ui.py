"""Shared quota form parsing and display context for administrator views.

This module registers no routes and never changes a policy. It adapts request
fields and template context; the limits store validates the policy itself.
"""

from __future__ import annotations

from flask import request

from bananachat import db
from bananachat.db import limits as limits_db
from bananachat.formatting import compact
from bananachat.services import limits

from ._helpers import FormError, integer

POOL_LABELS = {"api": "API, playground and images", "chat": "Chat", "agent": "Agents"}
UNIT_LABELS = {"second": "second", "minute": "minute", "hour": "hour", "day": "day"}
RULE_ROWS = limits_db.RULES_MAX


def rate_form_context() -> dict:
    """Fields shared by rate forms, without querying the request queue."""
    return {"units": UNIT_LABELS, "rule_rows": RULE_ROWS}


def quota_tabs_context() -> dict:
    """Navigation and rate fields for the site-wide quota pages."""
    return {"section": "quotas", "pool_labels": POOL_LABELS, **rate_form_context(),
            "pending_count": db.scalar("SELECT COUNT(*) FROM quota_requests WHERE status='pending'", default=0)}


def tokens_en(value) -> str:
    return f"{compact(value, 'en')} tokens"


def rules_en(rules) -> str:
    return limits.rate_text("en", rules) if rules else "no request-rate limit"


def rules_from_form(prefix: str = "rule") -> list[dict]:
    """Parse indexed request-rate rows; empty rows are ignored.

    Field names are ``<prefix>_requests_N``, ``<prefix>_per_N`` and
    ``<prefix>_burst_N``. The store validates and normalizes the resulting rules.
    """
    rules = []
    for index in range(RULE_ROWS):
        requests_raw = (request.form.get(f"{prefix}_requests_{index}") or "").strip()
        if not requests_raw:
            continue
        per = request.form.get(f"{prefix}_per_{index}")
        if per not in limits_db.UNITS:
            raise FormError("Choose second, minute, hour or day for every request-rate rule.")
        label = f"Requests per {per}"
        count = integer(f"{prefix}_requests_{index}", minimum=1, maximum=limits_db.REQUESTS_MAX, label=label)
        burst = integer(f"{prefix}_burst_{index}", minimum=1, maximum=limits_db.BURST_MAX,
                        label=f"Burst of the {per} rule", optional=True)
        rules.append({"requests": count, "per": per, "burst": burst})
    try:
        return limits_db.validate_rules(rules)
    except ValueError as error:
        raise FormError(str(error)) from None


def typed_rules(prefix: str = "rule") -> list[dict]:
    """Bounded, unvalidated rows for redisplaying a rejected form."""
    rows = []
    for index in range(RULE_ROWS):
        count = (request.form.get(f"{prefix}_requests_{index}") or "").strip()[:20]
        if count:
            rows.append({"requests": count, "per": request.form.get(f"{prefix}_per_{index}") or "minute",
                         "burst": (request.form.get(f"{prefix}_burst_{index}") or "").strip()[:20]})
    return rows


def typed(name: str) -> str:
    """A bounded field for redisplaying a rejected form, without validation."""
    return (request.form.get(name) or "").strip()[:100]


def policy_summary(policy: dict) -> str:
    """A single line describing a service's request and token limits."""
    parts = [rules_en(policy["rate"]["rules"]) if policy["rate"]["enabled"] else "no request-rate limit"]
    window, weekly = policy["window"], policy["weekly"]
    parts.append(f"{tokens_en(window['tokens'])} per 5 hours"
                 if window["enabled"] else "no 5-hour limit")
    if weekly["enabled"]:
        parts.append(f"{tokens_en(weekly['tokens'])} per week")
    return " · ".join(parts)
