"""Interface translations (Italian, English and German).

Catalogs live in ``bananachat/i18n/*.json``; each file covers one area of the
interface and has the shape ``{"en": {key: text}, "it": {key: text}, "de": {key: text}}``. Keys
must be unique across files. Texts use ``{name}`` placeholders; a text may be
an object ``{"one": ..., "other": ...}`` chosen by the ``count`` argument.
Keys starting with ``js.`` are also sent to the browser.

The administrator interface is intentionally English-only: it is aimed at the
operator, not at end users.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..config import SUPPORTED_LANGUAGES

LANGUAGE_NAMES = {"it": "Italiano", "en": "English", "de": "Deutsch"}
# Languages that write 1,5 rather than 1.5.
DECIMAL_COMMA = frozenset({"it", "de"})
_DIRECTORY = Path(__file__).parent


@lru_cache(maxsize=1)
def catalogs() -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {language: {} for language in SUPPORTED_LANGUAGES}
    owner: dict[str, str] = {}
    for file in sorted(_DIRECTORY.glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        for language, entries in data.items():
            if language not in merged:
                raise ValueError(f"{file.name}: unsupported language {language!r}")
            for key, text in entries.items():
                previous = owner.setdefault(f"{language}:{key}", file.name)
                if previous != file.name:
                    raise ValueError(f"Translation key {key!r} is defined in both {previous} and {file.name}")
                merged[language][key] = text
    return merged


def translate(language: str, key: str, /, **params) -> str:
    table = catalogs()
    text = table.get(language, {}).get(key)
    if text is None:
        text = table["en"].get(key, key)
    if isinstance(text, dict):
        count = params.get("count", 0)
        text = text.get("one" if count == 1 else "other") or text.get("other", key)
    if params:
        try:
            return str(text).format(**params)
        except (KeyError, IndexError, ValueError):
            return str(text)
    return str(text)


def translate_html(language: str, key: str, /, **params):
    """:func:`translate` for page text with markup parameters (dates that must not break inside):
    the translated text is escaped, parameters that are already ``Markup`` are kept as they are."""
    from markupsafe import Markup, escape

    holders = {name: f"\x00{index}\x00" for index, (name, value) in enumerate(params.items())
               if isinstance(value, Markup)}
    result = escape(translate(language, key, **{**params, **holders}))
    for name, holder in holders.items():
        result = result.replace(holder, params[name])
    return result


@lru_cache(maxsize=len(SUPPORTED_LANGUAGES))
def browser_catalog(language: str) -> dict[str, object]:
    """The ``js.*`` subset for the browser, with English fallbacks."""
    table = catalogs()
    keys = {key for key in table["en"] if key.startswith("js.")}
    return {key[3:]: table.get(language, {}).get(key, table["en"][key]) for key in sorted(keys)}


def negotiate(accept_language: str | None, default: str) -> str:
    """Pick the best supported language from an ``Accept-Language`` header."""
    if not accept_language:
        return default
    best, best_quality = default, -1.0
    for part in accept_language.split(","):
        pieces = part.strip().split(";")
        tag = pieces[0].strip().lower()
        quality = 1.0
        for parameter in pieces[1:]:
            name, _, value = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        primary = tag.split("-")[0]
        if primary in SUPPORTED_LANGUAGES and quality > best_quality:
            best, best_quality = primary, quality
    return best
