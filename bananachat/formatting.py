"""Template filters for readable colors, dates, numbers and sizes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from flask import g
from markupsafe import Markup

from bananachat import db
from bananachat.i18n import DECIMAL_COMMA, translate


def primary_foreground(color: str) -> str:
    """Choose the higher-contrast text color for a validated #rrggbb primary.

    Compare WCAG contrast with pure black and white; either choice maintains
    at least 4.5:1 for any opaque sRGB background. Keep the live Customize
    preview's primaryForeground calculation in sync.
    """
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
              for channel in channels]
    luminance = sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))
    return "#000000" if (luminance + 0.05) / 0.05 >= 1.05 / (luminance + 0.05) else "#ffffff"


def _language() -> str:
    return getattr(g, "lang", "en")


def _is_admin_page() -> bool:
    from flask import has_request_context, request

    return has_request_context() and request.blueprint == "admin"


def _display_language() -> str:
    """For the token and time filters: English on administrator pages (that interface is English-only)."""
    return "en" if _is_admin_page() else _language()


def timeago(value) -> str:
    language = _display_language()
    moment = db.parse_timestamp(value)
    if moment is None:
        return translate(language, "time.never")
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 0:
        return datetime_text(value)
    if seconds < 60:
        return translate(language, "time.just_now")
    for unit, size in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if seconds >= size:
            count = seconds // size
            if unit == "days" and count > 30:
                return datetime_text(value, date_only=True)
            return translate(language, f"time.{unit}_ago", count=count)
    return translate(language, "time.just_now")


def datetime_text(value, date_only: bool = False) -> str:
    moment = db.parse_timestamp(value)
    if moment is None:
        return "—"
    return moment.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M UTC")


def datetime_html(value) -> Markup:
    """:func:`datetime_text` for page text: the date and the time never break inside (``2026-`` / ``10-09``)."""
    text = datetime_text(value)
    date, _, clock = text.partition(" ")
    if not clock:
        return Markup('<span class="nowrap">{}</span>').format(text)
    return Markup('<span class="nowrap">{}</span> <span class="nowrap">{}</span>').format(date, clock)


def time_html(value) -> Markup:
    """:func:`time_text` for page text: ``21:01 UTC`` and ``2026-10-16`` never break inside."""
    text = time_text(value)
    parts = text.split(" ", 1)
    if len(parts) == 2 and "-" in parts[0]:
        return Markup('<span class="nowrap">{}</span> <span class="nowrap">{}</span>').format(*parts)
    return Markup('<span class="nowrap">{}</span>').format(text)


def local_time(value, style: str = "datetime") -> Markup:
    """A moment on a user page, shown like the chat's message times: core.js writes it in the reader's
    time zone and language. The UTC text stays as the fallback, and on administrator pages (always UTC).

    ``style``: ``datetime``; ``date`` (the day only); ``auto`` (the time only when it is today)."""
    moment = db.parse_timestamp(value)
    if moment is None:
        return Markup("—")
    if style == "date":
        text = Markup('<span class="nowrap">{}</span>').format(datetime_text(moment, date_only=True))
    elif style == "auto":
        text = time_html(moment)
    else:
        text = datetime_html(moment)
    if _is_admin_page():
        return text
    iso = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Markup('<time datetime="{}" data-local="{}">{}</time>').format(iso, style, text)


def break_points(value) -> Markup:
    """An identifier for page text (``account.two_step_enable``) that may wrap only after its dots and
    underscores (``<wbr>``), never in the middle of a word; the text is escaped."""
    return Markup("<wbr>").join(re.split(r"(?<=[._])", str(value or "")))


def pretty_json(value) -> str:
    """JSON for reading in a <pre> (autoescaped by the template): real characters, not the HTML-safe
    \\u0026-style escapes of Flask's ``tojson``, which is meant for <script> blocks.

    Indented while that stays within about twice the one-line form, else one line: indentation is repeated
    on every line, so a deeply nested value (a model's tool arguments) would otherwise grow many times over."""
    flat = json.dumps(value, ensure_ascii=False, default=str)
    budget, size, parts = 2 * len(flat) + 256, 0, []
    for chunk in json.JSONEncoder(indent=2, ensure_ascii=False, default=str).iterencode(value):
        size += len(chunk)
        if size > budget:
            return flat
        parts.append(chunk)
    return "".join(parts)


def number(value, digits: int = 0) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.{digits}f}"
    if _display_language() in DECIMAL_COMMA:
        text = text.replace(",", " ").replace(".", ",")
    return text


def credits(value) -> str:
    value = float(value or 0)
    return number(value, 0 if value == int(value) else 2)


def compact(value, lang: str | None = None) -> str:
    """A token amount in a few characters: ``850``, ``45.2k``, ``150k``, ``1.2M`` (decimal comma in Italian and German)."""
    lang = lang or _display_language()
    value = float(value or 0)
    size = abs(value)
    if size < 1000:
        return str(round(value))
    for unit, scale in (("k", 1e3), ("M", 1e6), ("B", 1e9)):
        number = value / scale
        if abs(round(number, 1)) < 1000 or unit == "B":
            text = f"{number:.1f}" if abs(number) < 100 else f"{number:.0f}"
            if text.endswith(".0"):
                text = text[:-2]
            if abs(float(text)) >= 1000 and unit != "B":
                continue
            return (text.replace(".", ",") if lang in DECIMAL_COMMA else text) + unit
    return f"{value:g}"


_SUFFIXES = {"k": 1e3, "m": 1e6, "mln": 1e6, "b": 1e9, "mld": 1e9}


def parse_amount(text) -> float | None:
    """A typed amount: ``30000``, ``30,000``, ``30 000``, ``30.000``, ``30k``, ``1.5M``, ``1,5M`` (None if unreadable).

    A separator followed by exactly three digits groups thousands unless a
    suffix (k, M, B) follows; otherwise it is the decimal separator.
    """
    if text is None:
        return None
    raw = "".join(str(text).split()).replace("_", "").replace("\u202f", "").replace("'", "").lower()
    match = re.fullmatch(r"([0-9][0-9.,]*)(k|m|mln|b|mld)?", raw)
    if not match:
        return None
    number, suffix = match.group(1), match.group(2)
    if "." in number and "," in number:
        decimal = "." if number.rfind(".") > number.rfind(",") else ","
        number = number.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif number.count(",") + number.count(".") > 0:
        separator = "," if "," in number else "."
        parts = number.split(separator)
        grouped = len(parts) > 1 and all(len(part) == 3 for part in parts[1:]) and 1 <= len(parts[0]) <= 3
        if grouped and (suffix is None or len(parts) > 2):
            number = "".join(parts)
        elif len(parts) == 2:
            number = f"{parts[0]}.{parts[1]}"
        else:
            return None
    try:
        value = float(number) * (_SUFFIXES[suffix] if suffix else 1)
    except ValueError:
        return None
    return value if value == value and value != float("inf") else None


def token_input(value) -> str:
    """A token amount for a form field: ``30k``, ``1.5M``, else the exact number (empty for None)."""
    if value is None or value == "":
        return ""
    value = float(value)
    for suffix, scale in (("M", 1e6), ("k", 1e3)):
        scaled = value / scale
        if abs(value) >= scale and abs(scaled * 10 - round(scaled * 10)) < 1e-9:
            text = f"{scaled:.1f}".removesuffix(".0")
            return text + suffix
    return f"{value:.0f}" if value == int(value) else f"{value:g}"


def tokens_text(value, lang: str | None = None) -> str:
    """``45.2k tokens`` / ``45,2k token``."""
    lang = lang or _display_language()
    value = float(value or 0)
    return translate(lang, "common.tokens", count=1 if value == 1 else 2, amount=compact(value, lang))


def tokens(value) -> str:
    return tokens_text(value)


def time_text(value) -> str:
    """``14:05 UTC`` for a moment today, else the date and time."""
    moment = value if isinstance(value, datetime) else db.parse_timestamp(value)
    if moment is None:
        return "—"
    moment = moment.astimezone(timezone.utc)
    if moment.date() == datetime.now(timezone.utc).date():
        return moment.strftime("%H:%M UTC")
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def time_left(value, lang: str | None = None) -> str:
    """How long until a moment: ``2 h 5 min``, ``35 min``, ``3 d 4 h`` (``less than a minute`` when close)."""
    lang = lang or _display_language()
    moment = value if isinstance(value, datetime) else db.parse_timestamp(value)
    if moment is None:
        return "—"
    seconds = int((moment - datetime.now(timezone.utc)).total_seconds())
    if seconds < 60:
        return translate(lang, "time.duration_soon")
    minutes = (seconds + 59) // 60
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        if not hours:
            return translate(lang, "time.duration_days", days=days)
        return translate(lang, "time.duration_days_hours", days=days, hours=hours)
    if hours:
        if not minutes:
            return translate(lang, "time.duration_hours", hours=hours)
        return translate(lang, "time.duration_hours_minutes", hours=hours, minutes=minutes)
    return translate(lang, "time.duration_minutes", minutes=minutes)


def time_left_html(value, lang: str | None = None) -> Markup:
    """:func:`time_left` for page text: ``6 d 23 h`` never breaks inside."""
    return Markup('<span class="nowrap">{}</span>').format(time_left(value, lang))


def filesize(value) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{number(size, 0 if unit in ('B', 'KB') else 1)} {unit}"
        size /= 1024
    return f"{size} B"
