"""Small helpers shared by the administrator views: form parsing, paging, audit."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import abort, flash, redirect, request, url_for

from bananachat import security
from bananachat.db import limits as limits_db
from bananachat.db import users


class FormError(ValueError):
    """A submitted value is invalid; the message is shown to the administrator."""


def text(name: str, *, max_length: int, required: bool = False, label: str | None = None, strip: bool = True) -> str:
    value = request.form.get(name) or ""
    if strip:
        value = value.strip()
    label = label or name.replace("_", " ").capitalize()
    if required and not value:
        raise FormError(f"{label} is required.")
    if len(value) > max_length:
        raise FormError(f"{label} can be at most {max_length:,} characters.")
    return value


def integer(name: str, *, minimum: int, maximum: int, label: str, default=None, optional: bool = False):
    raw = (request.form.get(name) or "").strip()
    if raw == "":
        if optional:
            return None
        if default is not None:
            return default
        raise FormError(f"{label} is required.")
    try:
        value = int(raw)
    except ValueError:
        raise FormError(f"{label} must be a whole number.") from None
    if not minimum <= value <= maximum:
        raise FormError(f"{label} must be between {minimum:,} and {maximum:,}.")
    return value


def number(name: str, *, minimum: float, maximum: float, label: str, optional: bool = False):
    raw = (request.form.get(name) or "").strip().replace(",", ".")
    if raw == "":
        if optional:
            return None
        raise FormError(f"{label} is required.")
    try:
        value = float(raw)
    except ValueError:
        raise FormError(f"{label} must be a number.") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise FormError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return value


def tokens(name: str, *, label: str, maximum: int = limits_db.TOKENS_MAX, optional: bool = False, default=None):
    """A token amount typed as ``30000``, ``30,000``, ``30k`` or ``1.5M`` (a whole number of tokens)."""
    from bananachat.formatting import parse_amount

    raw = (request.form.get(name) or "").strip()
    if raw == "":
        if optional:
            return None
        if default is not None:
            return default
        raise FormError(f"{label} is required.")
    value = parse_amount(raw)
    if value is None:
        raise FormError(f"{label}: write a number of tokens such as 50000, 50k or 1.5M.")
    if not 0 <= value <= maximum:
        raise FormError(f"{label} must be between 0 and {maximum:,} tokens.")
    return int(round(value))


def flag(name: str) -> bool:
    return request.form.get(name) in ("1", "on", "true", "yes")


def choice(name: str, options, *, label: str, default=None) -> str:
    value = request.form.get(name, default)
    if value not in options:
        raise FormError(f"Choose a valid {label.lower()}.")
    return value


def utc_datetime(name: str, *, label: str, future: bool = True) -> datetime | None:
    """Parse a ``datetime-local`` field. The form labels it as UTC, so it is read as UTC."""
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise FormError(f"{label} is not a valid date and time.") from None
    moment = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    if future and moment <= datetime.now(timezone.utc):
        raise FormError(f"{label} must be in the future (times are UTC).")
    if moment.year > 9999 or moment.year < 2000:
        raise FormError(f"{label} is out of range.")
    return moment


def utc_input_min() -> str:
    """Value for ``min`` on datetime-local inputs (now, UTC, minute precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")


@dataclass
class Page:
    number: int
    size: int
    total: int

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size))

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


def page(total: int, size: int = 50) -> Page:
    number = request.args.get("page", 1, type=int) or 1
    result = Page(1, size, total)
    result.number = max(1, min(number, result.pages))
    return result


def audit(action: str, target="", details=None) -> None:
    users.audit(security.current_user(), f"admin.{action}", target, details, security.client_ip())


def back(endpoint: str, **values):
    return redirect(url_for(endpoint, **values))


def fail(message: str, endpoint: str, **values):
    flash(message, "error")
    return back(endpoint, **values)


def user_or_404(user_id: str):
    user = users.get(user_id)
    if user is None:
        abort(404)
    return user


def me():
    return security.current_user()
