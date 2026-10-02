"""Administrator settings of the agents feature and which models may drive agents.

Settings are one JSON document (``agent_settings``), validated on write and
again on read: unknown keys are dropped and out-of-range values are clamped,
so a damaged row can never loosen a limit beyond its hard maximum. The feature
is **off** until an administrator enables it, and swarms are off by default,
as is starting tasks from a Git repository (``git_enabled``; ``git_hosts`` lists
the allowed hosts, validated by :mod:`gitfetch`).

A model may drive agents when it is an available text model whose Ollama
capabilities include ``tools`` (checked with ``/api/show``), unless an
administrator overrides that for the model (``allow`` or ``deny``).
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass

from bananachat import db
from bananachat.db import agents as agents_db
from bananachat.db import users
from bananachat.services.access import AccessContext, is_text_model, usable_models
from bananachat.services.agents import gitfetch

# name: (default, minimum, maximum) for integers; booleans have no range.
INTEGER_FIELDS = {
    "max_steps": (40, 1, 200),               # model calls per run (all agents together)
    "max_minutes": (20, 1, 240),             # wall time per run (paused time excluded)
    "max_tokens": (300_000, 1_000, 10_000_000),  # prompt + answer tokens per run
    "max_tasks_per_user": (1, 1, 10),        # active tasks per user (administrators: site limit only)
    "max_tasks_total": (4, 1, 64),           # active tasks on the site (also capped by the runner)
    "max_subagents": (4, 1, 8),              # sub-agents per swarm run
    "max_concurrent_subagents": (2, 1, 4),   # sub-agents working at the same time
    "command_timeout": (120, 5, 3600),       # longest command (the runner applies its own cap too)
    "keep_workspace_minutes": (30, 0, 1440),  # keep the sandbox after a run for downloads
    "retention_days": (30, 1, 3650),         # delete ended tasks after this many days
    "starts_per_hour": (10, 1, 1000),        # task starts and follow-ups per user per hour
    "git_max_mb": (50, 1, 50),               # repository archive size (the runner accepts at most 50 MB)
    "git_imports_per_hour": (10, 1, 100),    # repository imports per user per hour
}
BOOLEAN_FIELDS = {"enabled": False, "swarms_enabled": False, "git_enabled": False}
ACCESS_MODES = ("admins", "everyone", "custom")
OVERRIDES = ("allow", "deny")
# Hard caps that do not depend on settings.
MAX_TOOL_CALLS_PER_STEP = 8
MAX_PROMPT_CHARS = 20_000
MAX_FOLLOW_UP_CHARS = 10_000
MAX_PENDING_FOLLOW_UPS = 5


@dataclass(frozen=True)
class Settings:
    enabled: bool
    access_mode: str
    swarms_enabled: bool
    max_steps: int
    max_minutes: int
    max_tokens: int
    max_tasks_per_user: int
    max_tasks_total: int
    max_subagents: int
    max_concurrent_subagents: int
    command_timeout: int
    keep_workspace_minutes: int
    retention_days: int
    starts_per_hour: int
    git_enabled: bool
    git_max_mb: int
    git_imports_per_hour: int
    git_hosts: tuple
    model_overrides: dict

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in
                (*BOOLEAN_FIELDS, *INTEGER_FIELDS, "access_mode", "model_overrides")}
        data["git_hosts"] = list(self.git_hosts)
        return data


def normalise(data: dict) -> Settings:
    """Settings from stored or submitted values: defaults for missing ones, clamped into range."""
    data = data if isinstance(data, dict) else {}
    values: dict = {}
    for name, default in BOOLEAN_FIELDS.items():
        value = data.get(name, default)
        values[name] = value if isinstance(value, bool) else default
    # Existing saved configurations use capability policies. Preserve that
    # audience when upgrading, including a disabled site's later reactivation.
    # A fresh document defaults to administrators only and remains disabled.
    if "access_mode" not in data:
        values["access_mode"] = "custom" if isinstance(data.get("enabled"), bool) else "admins"
    elif isinstance(data["access_mode"], str) and data["access_mode"] in ACCESS_MODES:
        values["access_mode"] = data["access_mode"]
    else:
        values["access_mode"] = "admins"
        values["enabled"] = False
    for name, (default, low, high) in INTEGER_FIELDS.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            value = default
        values[name] = max(low, min(high, value))
    values["max_concurrent_subagents"] = min(values["max_concurrent_subagents"], values["max_subagents"])
    overrides = {}
    raw = data.get("model_overrides")
    if isinstance(raw, dict):
        for key, value in list(raw.items())[:1000]:
            if isinstance(key, str) and key.isdigit() and value in OVERRIDES:
                overrides[key] = value
    values["model_overrides"] = overrides
    values["git_hosts"] = gitfetch.normalise_hosts(data.get("git_hosts", list(gitfetch.DEFAULT_HOSTS)))
    return Settings(**values)


_cache = {"at": 0.0, "value": None, "database": None}


def current(*, fresh: bool = False) -> Settings:
    """The stored settings (cached for two seconds per process)."""
    now = time.monotonic()
    database = str(db.path())
    if (not fresh and _cache["value"] is not None and _cache["database"] == database
            and now - _cache["at"] < 2):
        return _cache["value"]
    value = normalise(agents_db.load_settings())
    _cache.update(at=now, value=value, database=database)
    return value


def save(values: dict, updated_by: str | None) -> Settings:
    settings = normalise(values)
    agents_db.save_settings(settings.to_dict(), updated_by)
    _cache.update(at=0.0, value=None)
    return settings


def enabled() -> bool:
    return current().enabled


# ----- models ----------------------------------------------------------------------

def supports_tools(model, caps: dict, overrides: dict) -> bool:
    # Claude Code chat disables tools; Ollama and external API transports support them.
    if model["backend"] not in ("ollama", "external"):
        return False
    override = overrides.get(str(model["id"]))
    if override == "deny":
        return False
    if override == "allow":
        return True
    if model["backend"] == "external":
        try:
            return "tools" in json.loads(model["capabilities"])
        except (ValueError, TypeError):
            return False
    info = caps.get(model["id"])
    return bool(info and info["supports_tools"])


def agent_models(context: AccessContext, settings: Settings | None = None) -> list:
    """Models *context*'s user may run agents with (chat access + tool calling)."""
    settings = settings or current()
    caps = agents_db.model_caps()
    return [model for model in usable_models(context, "chat", kind="text")
            if supports_tools(model, caps, settings.model_overrides)]


def model_allowed(model, context: AccessContext, settings: Settings | None = None) -> bool:
    settings = settings or current()
    return bool(is_text_model(model) and context.can_use(model, "chat")
                and supports_tools(model, agents_db.model_caps(), settings.model_overrides))


def user_allowed(user, settings: Settings | None = None, *, context: AccessContext | None = None) -> bool:
    """An active signed-in user belongs to the configured session audience.

    Everyone means every active account; model policies and quotas still apply
    independently. Custom preserves the existing ``agents`` capability policy.
    """
    if user is None or users.is_suspended(user):
        return False
    settings = settings or current()
    if not settings.enabled:
        return False
    if settings.access_mode == "admins":
        return user["role"] == "admin"
    if settings.access_mode == "everyone":
        return True
    if settings.access_mode == "custom":
        return (context or AccessContext.load(user)).allows("agents")
    return False


def access_state(user, settings: Settings | None = None) -> str:
    """The UI state: ``ok``, ``disabled`` or ``denied``."""
    settings = settings or current()
    if not settings.enabled:
        return "disabled"
    return "ok" if user_allowed(user, settings) else "denied"
