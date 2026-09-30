"""Personalities: validation, response style, templates, import/export and who may use what.

A personality adds instructions below the model's own system prompt (see
:func:`bananachat.services.chat.system_prompt`) and presents itself in the
chat with an emoji avatar, an accent colour, a description, an optional
greeting and conversation starters. Its response style is applied in two
ways only: the length as a short instruction added to the personality text,
the creativity as a temperature derived from the model's administrator
setting (:func:`style_temperature`). Users never pass raw options.

Everything a user may type or import goes through :func:`clean`, so the form,
the import and administrator forms share the same limits. Problems raise
:class:`Invalid` with a catalog key (``personality.*``) and its parameters;
administrator pages translate them to English.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping

from bananachat import db
from bananachat.db import personalities as store
from bananachat.i18n import translate
from bananachat.services.access import AccessContext, usable_models

MAX_NAME = store.MAX_NAME
MAX_INSTRUCTIONS = store.MAX_INSTRUCTIONS
MAX_DESCRIPTION = 160
MAX_GREETING = 600
MAX_STARTERS = 4
MAX_STARTER = 200
MAX_IMPORT_BYTES = 64 * 1024

LENGTHS = ("concise", "balanced", "detailed")
CREATIVITY = ("precise", "balanced", "creative")
# Accent colours are keys of a fixed palette (personalities.css); never free CSS.
COLORS = ("amber", "orange", "red", "pink", "violet", "blue", "teal", "green", "slate")
# Quick picks offered next to the avatar field; any single emoji is accepted.
AVATAR_CHOICES = ("✨", "🤖", "🧠", "🎯", "🦉", "📚", "🧑‍🏫", "🔍", "💻", "🛠️", "✍️", "🎨",
                  "🌍", "🇮🇹", "🧪", "📈", "🩺", "⚖️", "🍳", "🎵", "🧘", "🐙", "🦊", "🚀")

EXPORT_FORMAT = "bananachat.personality"
EXPORT_VERSION = 1
FIELDS = ("name", "description", "avatar", "color", "instructions", "greeting", "starters", "preferred_model",
          "response_length", "creativity")

# Ollama's own default, used when the administrator left the model's temperature empty.
DEFAULT_TEMPERATURE = 0.8
TEMPERATURE_BOUNDS = (0.0, 2.0)  # the bounds of the administrator's model form

LENGTH_INSTRUCTIONS = {
    "concise": "Response length: keep answers short and focused (a few sentences or a brief list) unless the user "
               "asks for more detail.",
    "detailed": "Response length: give thorough, well-structured answers with explanations and examples where "
                "they help.",
}

# Built-in starting points, shown as suggestions in the editor (never stored until saved).
# Texts are catalog entries personality.tpl_<key>_<field>.
TEMPLATES = {
    "concise_expert": {"avatar": "🎯", "color": "blue", "response_length": "concise", "creativity": "precise",
                       "starters": 3},
    "friendly_tutor": {"avatar": "🦉", "color": "green", "response_length": "detailed", "creativity": "balanced",
                       "starters": 3},
    "code_reviewer": {"avatar": "🔍", "color": "violet", "response_length": "balanced", "creativity": "precise",
                      "starters": 3},
    "italian_translator": {"avatar": "🇮🇹", "color": "red", "response_length": "concise", "creativity": "precise",
                           "starters": 2},
}


class Invalid(ValueError):
    """A value was refused; *key* is a catalog entry and *params* its placeholders."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def message(self, lang: str) -> str:
        return translate(lang, self.key, **self.params)


# ----- emoji ----------------------------------------------------------------------

_ZWJ, _VS16, _KEYCAP, _CANCEL_TAG = 0x200D, 0xFE0F, 0x20E3, 0xE007F
_SINGLE_SYMBOLS = {0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2, 0x3030, 0x303D, 0x3297, 0x3299}


def _regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _skin_tone(cp: int) -> bool:
    return 0x1F3FB <= cp <= 0x1F3FF


def _pictographic(cp: int) -> bool:
    if _regional(cp) or _skin_tone(cp):
        return False
    if 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or cp in _SINGLE_SYMBOLS or 0x25AA <= cp <= 0x25FE:
        return True
    if 0x2190 <= cp <= 0x21FF or 0x2300 <= cp <= 0x23FF or 0x2900 <= cp <= 0x297F or 0x2B00 <= cp <= 0x2BFF:
        return unicodedata.category(chr(cp)) == "So"
    return False


def is_emoji(value) -> bool:
    """Exactly one emoji: a pictograph (with optional presentation selector, skin tone and tag sequence),
    a keycap, a flag, or several of those joined by zero-width joiners (👩‍💻, 🏳️‍🌈)."""
    if not isinstance(value, str) or not value:
        return False
    points = [ord(ch) for ch in value]
    count = len(points)
    if count > 20 or len(value.encode("utf-8")) > 64:
        return False
    if count == 2 and all(_regional(cp) for cp in points):
        return True
    index = 0
    while True:
        if index >= count:
            return False
        cp = points[index]
        if chr(cp) in "0123456789#*":
            index += 1
            if index < count and points[index] == _VS16:
                index += 1
            if index >= count or points[index] != _KEYCAP:
                return False
            index += 1
        elif _pictographic(cp):
            index += 1
            if index < count and points[index] == _VS16:
                index += 1
            if index < count and _skin_tone(points[index]):
                index += 1
            if index < count and 0xE0020 <= points[index] <= 0xE007E:
                while index < count and 0xE0020 <= points[index] <= 0xE007E:
                    index += 1
                if index >= count or points[index] != _CANCEL_TAG:
                    return False
                index += 1
        else:
            return False
        if index == count:
            return True
        if points[index] != _ZWJ:
            return False
        index += 1


# ----- validation -----------------------------------------------------------------

def _text(value, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid("personality.error_type", field=field)
    return value.replace("\r\n", "\n").replace("\x00", "").strip()


def _line(value, *, field: str) -> str:
    return " ".join(_text(value, field=field).split())


def _choice(value, options: tuple, *, field: str, error: str) -> str:
    value = _text(value, field=field) or "balanced"
    if value not in options:
        raise Invalid(error)
    return value


def clean(raw: Mapping, *, allowed_models: Iterable[str] | None, keep_model: str = "") -> dict:
    """Validated fields of a personality from a form, an import or a template.

    *allowed_models* are the Ollama names the owner may chat with (None skips the
    check); *keep_model* is the stored preferred model, kept even when it is no
    longer allowed so editing other fields still works (it is then ignored in chats).
    """
    name = _line(raw.get("name"), field="name")
    if not 1 <= len(name) <= MAX_NAME:
        raise Invalid("personality.name_length", max=MAX_NAME)
    instructions = _text(raw.get("instructions"), field="instructions")
    if not 1 <= len(instructions) <= MAX_INSTRUCTIONS:
        raise Invalid("personality.instructions_length", max=MAX_INSTRUCTIONS)
    description = _line(raw.get("description"), field="description")
    if len(description) > MAX_DESCRIPTION:
        raise Invalid("personality.description_length", max=MAX_DESCRIPTION)
    avatar = "".join(_text(raw.get("avatar"), field="avatar").split())
    if avatar and not is_emoji(avatar):
        raise Invalid("personality.error_avatar")
    color = _text(raw.get("color"), field="color")
    if color and color not in COLORS:
        raise Invalid("personality.error_color")
    greeting = _text(raw.get("greeting"), field="greeting")
    if len(greeting) > MAX_GREETING:
        raise Invalid("personality.greeting_length", max=MAX_GREETING)

    items = raw.get("starters")
    if items is None:
        items = []
    if not isinstance(items, (list, tuple)):
        raise Invalid("personality.error_type", field="starters")
    starters = [line for line in (_line(item, field="starters") for item in items) if line]
    if len(starters) > MAX_STARTERS:
        raise Invalid("personality.starters_count", max=MAX_STARTERS)
    if any(len(item) > MAX_STARTER for item in starters):
        raise Invalid("personality.starter_length", max=MAX_STARTER)

    preferred = _text(raw.get("preferred_model"), field="preferred_model")
    if len(preferred) > 300:
        raise Invalid("personality.error_model")
    if preferred and allowed_models is not None and preferred not in set(allowed_models) and preferred != keep_model:
        raise Invalid("personality.error_model")
    return {
        "name": name, "instructions": instructions, "description": description, "avatar": avatar, "color": color,
        "greeting": greeting, "starters": starters, "preferred_model": preferred,
        "response_length": _choice(raw.get("response_length"), LENGTHS, field="response_length",
                                   error="personality.error_style"),
        "creativity": _choice(raw.get("creativity"), CREATIVITY, field="creativity", error="personality.error_style"),
    }


def from_form(form) -> dict:
    """The raw fields of a submitted editor form (starters come as ``starter_1`` … ``starter_4``)."""
    raw = {name: form.get(name) or "" for name in FIELDS if name != "starters"}
    raw["starters"] = [form.get(f"starter_{number}") or "" for number in range(1, MAX_STARTERS + 1)]
    return raw


def fields_of(row) -> dict:
    """The editable fields of a stored personality, in the shape :func:`clean` returns."""
    return {name: (store.starters(row) if name == "starters" else (row[name] or "")) for name in FIELDS}


def allowed_model_names(context: AccessContext) -> list[str]:
    return [model["ollama_name"] for model in usable_models(context, "chat", kind="text")]


# ----- import and export ------------------------------------------------------------

def _reject_constant(value):
    raise ValueError(f"unsupported constant {value}")


def parse_import(data: bytes) -> dict:
    """Raw fields from an exported file. Raises :class:`Invalid` for anything unexpected."""
    if len(data) > MAX_IMPORT_BYTES:
        raise Invalid("personality.import_too_large", size=MAX_IMPORT_BYTES // 1024)
    try:
        document = json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Invalid("personality.import_invalid") from None
    if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
        raise Invalid("personality.import_invalid")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise Invalid("personality.import_invalid")
    if version > EXPORT_VERSION:
        raise Invalid("personality.import_newer")
    body = document.get("personality")
    if not isinstance(body, dict):
        raise Invalid("personality.import_invalid")
    return {name: body.get(name) for name in FIELDS}


def export(row) -> dict:
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "exported_at": db.now()[:19].replace(" ", "T") + "Z",
            "personality": fields_of(row)}


def export_filename(row) -> str:
    return f"{row['name']}.personality.json"


# ----- templates ----------------------------------------------------------------------

def template(key: str, lang: str) -> dict | None:
    """A built-in starting point as raw editor fields, or None for an unknown key."""
    spec = TEMPLATES.get(key)
    if spec is None:
        return None
    text = lambda field: translate(lang, f"personality.tpl_{key}_{field}")  # noqa: E731
    return {
        "name": text("name"), "description": text("description"), "instructions": text("instructions"),
        "greeting": text("greeting"), "avatar": spec["avatar"], "color": spec["color"],
        "starters": [text(f"starter_{number}") for number in range(1, spec["starters"] + 1)],
        "preferred_model": "", "response_length": spec["response_length"], "creativity": spec["creativity"],
    }


def templates(lang: str) -> list[dict]:
    return [{"key": key, **template(key, lang)} for key in TEMPLATES]


# ----- applying a personality to a chat -------------------------------------------------

def prompt_text(row) -> str:
    """The personality's part of the system prompt: its instructions and the length instruction."""
    parts = [(row["instructions"] or "").strip()]
    length = LENGTH_INSTRUCTIONS.get(row["response_length"] or "")
    if length:
        parts.append(length)
    return "\n\n".join(part for part in parts if part)


def style_temperature(model, creativity: str | None) -> float | None:
    """The temperature for a creativity setting, relative to the model's administrator setting.

    ``balanced`` keeps the model's own setting (None). ``precise`` lowers it to 40 %;
    ``creative`` raises it by 40 % (at least 0.3). The result stays within the bounds the
    administrator's model form allows.
    """
    if creativity not in ("precise", "creative"):
        return None
    base = model["temperature"] if model["temperature"] is not None else DEFAULT_TEMPERATURE
    low, high = TEMPERATURE_BOUNDS
    base = min(max(float(base), low), high)
    value = base * 0.4 if creativity == "precise" else base + max(0.3, base * 0.4)
    return round(min(max(value, low), high), 2)


def usable(user, personality_id, context: AccessContext | None = None):
    """The personality if *user* may apply it to a chat now (their own or a featured one), else None."""
    if not personality_id:
        return None
    try:
        personality_id = int(personality_id)
    except (TypeError, ValueError):
        return None
    context = context or AccessContext.load(user)
    if not context.allows("custom_personality"):
        return None
    row = store.get(personality_id)
    if row is None or not store.is_active(row):
        return None
    if row["kind"] == "featured" or row["user_id"] == user["id"]:
        return row
    return None


def choices(user, context: AccessContext) -> list:
    """Personalities *user* can pick in a chat: their active ones, then published featured ones."""
    if not context.allows("custom_personality"):
        return []
    own = [row for row in store.list_for(user["id"], enabled_only=True) if store.is_active(row)]
    featured = [row for row in store.list_featured() if store.is_active(row)]
    return own + featured


def default_for(user, context: AccessContext | None = None):
    """The personality new chats of *user* start with, if it is still usable."""
    personality_id = store.default_id(user["id"])
    return usable(user, personality_id, context) if personality_id else None


def chat_json(row, model_names: set[str]) -> dict:
    """What the chat page needs to show and switch to a personality (all text is escaped by the page)."""
    preferred = row["preferred_model"] or ""
    return {
        "id": row["id"], "name": row["name"], "avatar": row["avatar"] or "", "color": row["color"] or "",
        "description": row["description"] or "", "greeting": row["greeting"] or "", "starters": store.starters(row),
        "featured": row["kind"] == "featured", "preferred_model": preferred,
        "preferred_available": bool(preferred) and preferred in model_names,
    }
