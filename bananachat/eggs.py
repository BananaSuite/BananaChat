"""BananaSuite easter eggs, format ``banana-eggs/v1``: the one place that decides what a stored egg list may hold.

Shared by BananaWiki (wiki and hosting portal) and BananaChat; BananaGit has the same rules in Go
(``modules/setting/bananagit_eggs.go``). ``eggs-vectors.json`` holds input/output pairs every copy must agree on.

An egg::

    {"id": "konami", "enabled": true,
     "keys": ["ArrowUp", "ArrowUp", ..., "b", "a"],          # KeyboardEvent.key, single characters lowercased
     "sprite": {"type": "emoji", "value": "🍌"},              # or {"type": "mascot"}
     "count": 30, "speed_min": 1.5, "speed_max": 3.5, "delay": 80,
     "unlock": {"action": "page", "delay": 2500},             # page | toast | none
     "record": true, "badge": null, "message": ""}

``clean(raw, strict=True)`` is for saving an admin's list: anything wrong raises :class:`EggError` with one
``(egg index, field, code)`` per problem. ``clean(raw)`` is for reading what is stored: invalid eggs are
dropped and numbers clamped, so a bad row never breaks a page. ``from_legacy`` turns the 1.4 BananaWiki egg
settings (key codes) into this format.
"""

from __future__ import annotations

import json
import re
from typing import Any

VERSION = 1
MAX_EGGS = 10
MAX_BYTES = 8192
MIN_KEYS = 2
MAX_KEYS = 32
MAX_KEY_LENGTH = 16
MAX_EMOJI_UNITS = 16  # UTF-16 code units, as the browser counts them
MAX_MESSAGE = 200
UNLOCK_ACTIONS = ("page", "toast", "none")
SPRITES = ("emoji", "mascot")
NUMBERS = {  # field: (minimum, maximum, default, integer)
    "count": (1, 200, 30, True),
    "speed_min": (0.3, 20.0, 1.5, False),
    "speed_max": (0.3, 20.0, 3.5, False),
    "delay": (10, 2000, 80, True),
}
UNLOCK_DELAY = (100, 30000, 2500)
BANANA = "\U0001F34C"
KONAMI = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight",
          "b", "a"]
EMOJIS = ["🍌", "🍎", "🍕", "🧀", "🍩", "🍪", "🎉", "🎈", "⭐", "✨", "💎", "🔥", "💡", "🧩", "🎯", "🎨", "🎸", "🚀",
          "🌈", "🌻", "🦄", "🐱", "🐶", "🐸", "🐧", "🦆", "👻", "🎃", "💀", "❤️", "📚", "✏️", "⚙️", "🔧", "📦", "🪄"]

_ID = re.compile(r"[a-z0-9-]{1,32}\Z")
_BADGE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_NAMED_KEY = re.compile(r"[A-Z][A-Za-z0-9]{1,15}\Z")  # ArrowUp, Enter, Escape, F5, ...
_LEGACY_CODES = {8: "Backspace", 9: "Tab", 13: "Enter", 27: "Escape", 32: " ",
                 37: "ArrowLeft", 38: "ArrowUp", 39: "ArrowRight", 40: "ArrowDown"}


class EggError(ValueError):
    """The list cannot be saved: ``errors`` is a list of ``(egg index or None, field, code)``."""

    def __init__(self, errors: list[tuple[int | None, str, str]]):
        super().__init__(", ".join(f"{'list' if i is None else i}.{field}: {code}" for i, field, code in errors))
        self.errors = errors


def default_egg() -> dict[str, Any]:
    """The egg every app starts with: the Konami code, a short shower of the mascot (a banana without one)."""
    return {"id": "konami", "enabled": True, "keys": list(KONAMI), "sprite": {"type": "mascot"},
            "count": 10, "speed_min": 1.6, "speed_max": 2.1, "delay": 80,
            "unlock": {"action": "none", "delay": 2500}, "record": True, "badge": None, "message": ""}


def default_config() -> dict[str, Any]:
    return {"version": VERSION, "eggs": [default_egg()]}


def normalise_key(key: Any) -> str | None:
    """A ``KeyboardEvent.key`` as eggs store it (single characters lowercased), or None when it cannot be one."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH:
        return None
    if len(key) == 1:
        return None if (ord(key) < 32 or ord(key) == 127) else key.lower()
    return key if _NAMED_KEY.match(key) else None


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _number(raw: dict, name: str, limits: tuple, errors: list, index: int, prefix: str = ""):
    low, high, default, integer = limits
    field = prefix + name
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # bool, text, NaN
        errors.append((index, field, "type"))
        value = default
    elif not low <= value <= high:
        errors.append((index, field, "range"))
        value = min(high, max(low, value))
    return int(round(value)) if integer else round(float(value), 2)


def _egg(raw: Any, index: int) -> tuple[dict[str, Any] | None, list]:
    errors: list[tuple[int | None, str, str]] = []
    if not isinstance(raw, dict):
        return None, [(index, "egg", "type")]

    egg_id = raw.get("id")
    if not isinstance(egg_id, str) or not _ID.match(egg_id):
        errors.append((index, "id", "invalid"))
        egg_id = None

    keys = raw.get("keys")
    if not isinstance(keys, list):
        errors.append((index, "keys", "type"))
        keys = []
    keys = [normalise_key(k) for k in keys]
    if None in keys:
        errors.append((index, "keys", "invalid"))
    elif not MIN_KEYS <= len(keys) <= MAX_KEYS:
        errors.append((index, "keys", "length"))

    sprite = raw.get("sprite", {"type": "emoji", "value": BANANA})
    if not isinstance(sprite, dict) or sprite.get("type") not in SPRITES:
        errors.append((index, "sprite", "invalid"))
        sprite = {"type": "emoji", "value": BANANA}
    if sprite["type"] == "emoji":
        value = sprite.get("value")
        if not isinstance(value, str) or not value.strip() or _utf16(value.strip()) > MAX_EMOJI_UNITS \
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or "<" in value or ">" in value:
            errors.append((index, "sprite.value", "invalid"))
            value = BANANA
        sprite = {"type": "emoji", "value": value.strip()}
    else:
        sprite = {"type": "mascot"}

    numbers = {name: _number(raw, name, limits, errors, index) for name, limits in NUMBERS.items()}
    if numbers["speed_min"] > numbers["speed_max"]:
        numbers["speed_min"], numbers["speed_max"] = numbers["speed_max"], numbers["speed_min"]

    unlock = raw.get("unlock", {"action": "page", "delay": UNLOCK_DELAY[2]})
    if not isinstance(unlock, dict):
        errors.append((index, "unlock", "type"))
        unlock = {}
    action = unlock.get("action", "page")
    if action not in UNLOCK_ACTIONS:
        errors.append((index, "unlock.action", "invalid"))
        action = "page"
    delay = _number(unlock, "delay", (*UNLOCK_DELAY, True), errors, index, "unlock.")

    enabled, record = raw.get("enabled", True), raw.get("record", True)
    for name, flag in (("enabled", enabled), ("record", record)):
        if not isinstance(flag, bool):
            errors.append((index, name, "type"))
    badge = raw.get("badge")
    if badge in ("", None):
        badge = None
    elif isinstance(badge, int) and not isinstance(badge, bool) and badge > 0:
        badge = str(badge)
    elif not isinstance(badge, str) or not _BADGE.match(badge):
        errors.append((index, "badge", "invalid"))
        badge = None
    message = raw.get("message", "")
    if not isinstance(message, str) or len(message) > MAX_MESSAGE \
            or any(ord(ch) < 32 and ch not in "\t" for ch in message):
        errors.append((index, "message", "invalid"))
        message = message[:MAX_MESSAGE] if isinstance(message, str) else ""
    message = " ".join(message.split())

    egg = {"id": egg_id, "enabled": enabled if isinstance(enabled, bool) else True,
           "keys": keys, "sprite": sprite, **numbers, "unlock": {"action": action, "delay": delay},
           "record": record if isinstance(record, bool) else True, "badge": badge, "message": message}
    fatal = any(field in ("egg", "id", "keys") for _, field, _ in errors)
    return (None if fatal else egg), errors


def clean(raw: Any, *, strict: bool = False) -> dict[str, Any]:
    """``{"version": 1, "eggs": [...]}`` from JSON text, a dict or a bare list. See the module docstring."""
    errors: list[tuple[int | None, str, str]] = []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_BYTES:
            errors.append((None, "list", "size"))
        try:
            raw = json.loads(raw) if raw.strip() else {"version": VERSION, "eggs": []}
        except ValueError:
            errors.append((None, "list", "json"))
            raw = {}
    if isinstance(raw, list):
        raw = {"version": VERSION, "eggs": raw}
    if not isinstance(raw, dict):
        errors.append((None, "list", "type"))
        raw = {}
    if raw.get("version", VERSION) != VERSION:
        errors.append((None, "version", "invalid"))
    items = raw.get("eggs", [])
    if not isinstance(items, list):
        errors.append((None, "eggs", "type"))
        items = []
    if len(items) > MAX_EGGS:
        errors.append((None, "eggs", "too_many"))

    eggs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        egg, problems = _egg(item, index)
        errors.extend(problems)
        if egg is None:
            continue
        if egg["id"] in seen:
            errors.append((index, "id", "duplicate"))
            continue
        seen.add(egg["id"])
        if len(eggs) < MAX_EGGS:
            eggs.append(egg)
    if strict and errors:
        raise EggError(errors)
    return {"version": VERSION, "eggs": eggs}


def dumps(config: dict[str, Any]) -> str:
    """Compact JSON for storage; ``clean`` first."""
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


def _legacy_key(code: Any) -> str | None:
    try:
        code = int(str(code).strip())
    except ValueError:
        return None
    if 65 <= code <= 90:
        return chr(code).lower()
    if 48 <= code <= 57:
        return chr(code)
    if 96 <= code <= 105:
        return chr(code - 48)
    return _LEGACY_CODES.get(code)


def _legacy_number(item: dict, name: str, default: float) -> float:
    try:
        return float(item.get(name)) if item.get(name) not in (None, "") else default
    except (TypeError, ValueError):
        return default


def from_legacy(value: Any = None, columns: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The 1.4 BananaWiki eggs (a JSON list in ``easter_eggs`` or the old ``easter_egg_*`` columns), or None.

    Each egg becomes ``legacy-1``, ``legacy-2``, ... and opens the easter egg page as it used to. An egg whose
    key codes cannot all be translated is left out.
    """
    items: list = []
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            items = json.loads(value)
        except ValueError:
            items = []
    elif isinstance(value, list):
        items = value
    if not items and columns and any(columns.get(k) not in (None, "") for k in
                                     ("easter_egg_keys", "easter_egg_emoji", "easter_egg_count")):
        items = [{"keys": columns.get("easter_egg_keys"), "emoji": columns.get("easter_egg_emoji"),
                  "count": columns.get("easter_egg_count"), "speed_min": columns.get("easter_egg_speed_min"),
                  "speed_max": columns.get("easter_egg_speed_max"), "delay": columns.get("easter_egg_delay"),
                  "unlock_delay": columns.get("easter_egg_unlock_delay")}]
    eggs = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        codes = str(item.get("keys") or "38,38,40,40,37,39,37,39,66,65").split(",")
        keys = [_legacy_key(code) for code in codes if code.strip()]
        if not keys or None in keys:
            continue

        eggs.append({"id": f"legacy-{len(eggs) + 1}", "enabled": True, "keys": keys,
                     "sprite": {"type": "emoji", "value": str(item.get("emoji") or BANANA)},
                     "count": _legacy_number(item, "count", 30), "speed_min": _legacy_number(item, "speed_min", 1.5),
                     "speed_max": _legacy_number(item, "speed_max", 3.5), "delay": _legacy_number(item, "delay", 80),
                     "unlock": {"action": "page", "delay": _legacy_number(item, "unlock_delay", 2500)},
                     "record": True, "badge": None, "message": ""})
    return clean({"version": VERSION, "eggs": eggs}) if eggs else None


def client_config(config: dict[str, Any], *, found_url: str | None, page_url: str | None, signed_in: bool,
                  csrf_header: str | None = None, csrf_token: str = "", reduce_motion: bool = False,
                  strings: dict[str, str] | None = None) -> dict[str, Any]:
    """What the page puts in ``<script type="application/json" id="banana-eggs">`` (enabled eggs only).

    ``found_url`` keeps a literal ``{id}`` the script fills in. Badges stay on the server.
    """
    eggs = [{k: v for k, v in egg.items() if k != "badge"} for egg in config.get("eggs", []) if egg.get("enabled")]
    return {"version": VERSION, "eggs": eggs, "signedIn": bool(signed_in), "foundUrl": found_url,
            "pageUrl": page_url, "csrf": {"header": csrf_header, "token": csrf_token} if csrf_header else None,
            "reduceMotion": bool(reduce_motion), "strings": strings or {}}


def script_json(data: dict[str, Any]) -> str:
    """JSON that is safe inside a ``<script type="application/json">`` element (no ``</script>`` break-out)."""
    return (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
