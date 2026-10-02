"""Normalize the official Claude Code SDK model catalog without inventing models."""
from __future__ import annotations

from bananachat.services import claude_control, claude_pool

MAX_MODELS = 200
LEVELS = ("low", "medium", "high", "extra", "max")


class DiscoveryError(RuntimeError):
    """A safe model-metadata validation error."""


def _text(item, key, default, maximum):
    value = item.get(key, default)
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise DiscoveryError("Claude returned invalid model metadata.")
    return value


def parse_models(info):
    """Canonical models plus aliases; capabilities reflect this text-only transport.

    The CLI may return both 'default' and 'opus' pointing at the same model.
    Merge them and intersect reported effort levels so a duplicate cannot raise
    a model's capability. Preserve aliases for existing manifest model names.
    """
    if not isinstance(info, dict) or not isinstance(info.get("models"), list) or len(info["models"]) > MAX_MODELS:
        raise DiscoveryError("Claude did not return a supported model catalog.")
    found, aliases = {}, {}
    for item in info["models"]:
        if not isinstance(item, dict):
            raise DiscoveryError("Claude returned an invalid model entry.")
        alias = _text(item, "value", "", 200)
        name = _text(item, "resolvedModel", alias, 200)
        if not alias or not name or not claude_pool._MODEL_NAME.fullmatch(name):
            raise DiscoveryError("Claude returned an invalid model identifier.")
        if alias in aliases and aliases[alias] != name:
            raise DiscoveryError("Claude returned conflicting model aliases.")
        aliases[alias] = name
        try:
            family = claude_pool.family_of(name)
        except ValueError:
            raise DiscoveryError("The Claude model catalog contains an unsupported model family.") from None
        supports_effort = item.get("supportsEffort", False)
        levels = item.get("supportedEffortLevels", [])
        if not isinstance(supports_effort, bool) or not isinstance(levels, list) or len(levels) > 10 \
                or any(not isinstance(level, str) or level not in (*LEVELS, "xhigh") for level in levels):
            raise DiscoveryError("Claude returned invalid reasoning capabilities.")
        levels = {"extra" if level == "xhigh" else level for level in levels} if supports_effort else set()
        descriptor = {"name": name, "display": _text(item, "displayName", name, 500)[:120], "family": family,
                      "description": _text(item, "description", "Claude model reported by the official CLI.", 2000)[:500],
                      "reasoning": sorted(levels, key=LEVELS.index), "capabilities": ["completion"], "aliases": [alias]}
        if name in found:
            known = found[name]
            known["reasoning"] = [level for level in known["reasoning"] if level in levels]
            known["aliases"] = sorted(set(known["aliases"]) | {alias})
            # Prefer a named selector over the generic default label.
            if alias != "default":
                known.update(display=descriptor["display"], description=descriptor["description"])
        else:
            found[name] = descriptor
    return list(found.values())


def discover(adapter, profile, *, cancel=None, timeout=30):
    """Ask the CLI for supported models without generating a model response."""
    return parse_models(claude_control.request(adapter, profile, "initialize", cancel=cancel, timeout=timeout))
