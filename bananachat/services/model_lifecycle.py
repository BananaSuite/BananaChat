"""The life of a model in the catalog: detection, enrollment, and retirement when it goes missing, breaks or is deprecated.

States are independent columns of ``ai_models`` (see migration v10), so a
model that comes back returns to exactly what it was:

* **Enrollment** (``enrollment``): ``new`` (detected, hidden, waiting for an
  administrator), ``auto`` (enabled automatically by the ``automatic`` policy,
  to be reviewed), ``reviewed`` or ``ignored`` (matches the ignore list or was
  ignored by hand; never offered). Publishing a ``new`` model reviews it.
* **Missing** (``missing_at``): absent from the model server for
  ``missing_syncs`` consecutive successful syncs or ``missing_minutes``. It is
  hidden (``backend_available=0``) and removed from the catalog after
  ``retention_days``; chat history keeps its display name.
* **Failing** (``failing_at``): ``failure_threshold`` load or generation
  failures within ``failure_window_minutes`` and no success in between (outages
  seen by the health probe, busy gateways, worker PCs, cancellations and
  timeouts under load do not count). Hidden from selection and fallback; the ``model-lifecycle`` job sends
  a small test prompt when the queue has room (back-off from
  ``recheck_minutes`` up to six hours) and restores it when it answers.
* **Deprecated** (``deprecated_at``, ``replacement_id``, ``retire_at``): the
  chat picker hides it and chats that select it move to the replacement with a
  notice; the API keeps serving it until it is **retired** (``retired_at``),
  after which it is refused with the replacement's name.
* **Being deleted** (``delete_requested_at``): hidden while running requests
  finish, then deleted from the model server.

Every change is written to ``model_lifecycle_events`` with its reason (and to
the audit log when nobody made it by hand; the administrator views audit their
own actions).

Limits: enabling a model applies a strictness preset (``light``, ``standard``,
``heavy``) chosen from its parameter size. The preset is stored in
``ai_models.limit_preset`` and passed to ``apply_model_preset(model_id, preset)``
in ``services.limits`` (or ``db.limits``) when that function exists.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib
import inspect
import json
import logging
import re
import time
from datetime import timedelta

from flask import current_app
from filelock import FileLock, Timeout

from bananachat import db
from bananachat.db import catalog, users
from bananachat.db import model_lifecycle as lifecycle_db
from bananachat.db import pulls as pulls_db
from bananachat.db import settings as site_settings
from bananachat.services import background, health, ollama
from bananachat.services.upstream import UpstreamError

log = logging.getLogger("bananachat.models")

ENROLLMENT_POLICIES = ("manual", "automatic")
PRESETS = ("light", "standard", "heavy")
PRESET_LABELS = {"light": "Light", "standard": "Standard", "heavy": "Heavy"}
# name -> (default, minimum, maximum)
NUMBERS = {
    "missing_syncs": (3, 1, 100),
    "missing_minutes": (30, 0, 7 * 24 * 60),
    "retention_days": (30, 1, 3650),
    "failure_threshold": (5, 2, 100),
    "failure_window_minutes": (30, 1, 24 * 60),
    "recheck_minutes": (10, 1, 24 * 60),
    "download_concurrency": (1, 1, 4),
    "download_retries": (3, 0, 10),
    "stall_minutes": (10, 1, 240),
}
FLAGS = {"downloads_paused": False}
MAX_RECHECK_SECONDS = 6 * 3600

REASONING_LEVELS = {"none": [], "boolean": ["off", "on"], "levels": ["off", "low", "medium", "high"]}
# Model families whose ``think`` accepts effort levels instead of true/false.
LEVELED_FAMILIES = ("gptoss", "gpt-oss")
EMBEDDING_FAMILIES = ("bert", "nomic-bert", "xlm-roberta", "jina-bert")

DETAILS_PER_SYNC = 20
DETAILS_TIMEOUT = 10
DETAILS_BUDGET = 60
SYNC_STATE_KEY = "model_sync_last"
EMPTY_LISTINGS_KEY = "model_sync_empty_listings"
EVENT_RETENTION_DAYS = 400
PROBE_PROMPT = "Reply with the single word OK."

_PATTERN_RE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9:._/\-*?]{0,199}$")
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMBT])?\s*$", re.IGNORECASE)
_state = {"events_pruned": 0.0}


def _config(config=None):
    return config or current_app.config["BC"]


def _loads(value, default):
    try:
        result = json.loads(value) if isinstance(value, str) else default
    except ValueError:
        return default
    return result if isinstance(result, type(default)) else default


def _name(model) -> str:
    return model["ollama_name"] if model["backend"] == "external" else model["backend_model_name"] or model["ollama_name"]


def operation_lock(name: str, config=None, *, backend: str = "ollama") -> FileLock:
    """Serialize downloads, cancellation cleanup and deletion of the same model across workers."""
    if backend == "ollama":
        name = pulls_db.canonical_ollama_name(name)
    key = hashlib.sha256(f"{backend}:{name}".encode()).hexdigest()
    directory = _config(config).instance_dir / ".model-operations"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(str(directory / f"{key}.lock"), timeout=0, mode=0o600)


def _record(model, event: str, reason: str, actor=None) -> None:
    """One state change: an event for the model's page, and the audit log when nobody made it by hand."""
    lifecycle_db.add_event(model["id"], model["ollama_name"], event, reason, actor)
    if actor is None:
        users.audit(None, f"models.{event}", model["ollama_name"], {"reason": reason[:500]})
    log.info("Model %s: %s. %s", model["ollama_name"], event.replace("_", " "), reason)


# ----- policy ---------------------------------------------------------------------------------

def policy() -> dict:
    """The administrator's settings merged over the defaults (stored values are re-validated)."""
    stored = lifecycle_db.load_policy()
    result: dict = {"enrollment": stored.get("enrollment") if stored.get("enrollment") in ENROLLMENT_POLICIES
                    else "manual"}
    for key, (default, low, high) in NUMBERS.items():
        value = stored.get(key)
        result[key] = value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high \
            else default
    for key, default in FLAGS.items():
        result[key] = stored[key] if isinstance(stored.get(key), bool) else default
    return result


def save_policy(changes: dict, actor=None) -> dict:
    """Validate and store *changes* (other settings keep their values). Raises ValueError."""
    current = policy()
    for key, value in changes.items():
        if key == "enrollment":
            if value not in ENROLLMENT_POLICIES:
                raise ValueError("Choose manual or automatic enrollment.")
        elif key in NUMBERS:
            _default, low, high = NUMBERS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
        elif key in FLAGS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
        else:
            raise ValueError(f"Unknown setting: {key}")
        current[key] = value
    lifecycle_db.save_policy(current, actor["id"] if actor else None)
    return current


# ----- ignore list ------------------------------------------------------------------------------

def validate_pattern(value: str) -> str:
    value = (value or "").strip()
    if not _PATTERN_RE.fullmatch(value) or ".." in value:
        raise ValueError("Enter a model name or a pattern such as *:latest or *-embed* (letters, digits, . : _ - / "
                         "and the wildcards * and ?).")
    if not re.search(r"[A-Za-z0-9]", value):
        raise ValueError("A pattern needs at least one letter or digit; to review every new model, use manual "
                         "enrollment instead.")
    return value


def patterns() -> list[str]:
    return [row["pattern"] for row in lifecycle_db.ignore_rules()]


def matches(name: str, rules) -> bool:
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, rule.casefold()) for rule in rules)


def is_ignored(name: str) -> bool:
    return matches(name, patterns())


def add_ignore_rule(pattern: str, actor=None, note: str = "") -> tuple[int, list[str]]:
    """Add a rule and ignore the models waiting for review that it matches; returns ``(rule id, names)``."""
    pattern = validate_pattern(pattern)
    affected = []
    with db.transaction():
        rule_id, _created = lifecycle_db.add_rule(pattern, note, actor["id"] if actor else None)
        for model in catalog.with_enrollment("new"):
            if matches(model["ollama_name"], [pattern]):
                reason = f"Matches the ignore rule {pattern}."
                catalog.set_lifecycle(model["id"], enrollment="ignored", is_rolled_out=0, state_reason=reason)
                _record(model, "ignored", reason, actor)
                affected.append(model["ollama_name"])
    return rule_id, affected


def remove_ignore_rule(rule_id: int) -> str | None:
    """Delete a rule (models it ignored stay ignored until restored). Returns the pattern."""
    rule = lifecycle_db.get_rule(rule_id)
    if rule is None:
        return None
    lifecycle_db.delete_rule(rule_id)
    return rule["pattern"]


def ignore_model(model, actor=None) -> None:
    """Never offer this model: hide it and remember its exact name."""
    with db.transaction():
        lifecycle_db.add_rule(model["ollama_name"], "Ignored from the catalog.", actor["id"] if actor else None)
        reason = "Ignored by an administrator."
        catalog.set_lifecycle(model["id"], enrollment="ignored", is_rolled_out=0, state_reason=reason)
        _record(model, "ignored", reason, actor)


def restore_model(model, actor=None) -> None:
    """Take a model off the ignore list: it waits for review again (never enabled automatically)."""
    with db.transaction():
        lifecycle_db.delete_rule_pattern(model["ollama_name"])
        reason = "Restored from the ignore list; waiting for review."
        catalog.set_lifecycle(model["id"], enrollment="new", enrolled_at=db.now(), state_reason=reason)
        _record(model, "restored", reason, actor)


# ----- detection ----------------------------------------------------------------------------------

def parameters_billions(size: str | None, count=None) -> float | None:
    """``8.0B`` -> 8.0, ``567M`` -> 0.567 (or a raw parameter count)."""
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count / 1e9
    match = _SIZE_RE.match(size or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    return value * {"K": 1e-6, "M": 1e-3, "B": 1.0, "T": 1e3}[unit]


def preset_for(model) -> str:
    """Heavy from 30B parameters, standard from 7B, light below; standard when unknown.

    Claude subscription models use per-family strictness instead: opus is
    heavy/strict, sonnet standard, haiku light.
    """
    try:
        backend = model["backend"]
    except (IndexError, KeyError, TypeError):
        backend = None
    if backend == "external":
        from bananachat.services import external_providers

        return external_providers.preset_for_name(model["backend_model_name"] or model["ollama_name"])
    if backend == "claude":
        from bananachat.services import claude_pool

        return claude_pool.strict_policy(model["ollama_name"], family=model["family"] or None)["preset"]
    billions = parameters_billions(model["parameter_size"])
    if billions is None:
        return "standard"
    return "heavy" if billions >= 30 else "standard" if billions >= 7 else "light"


def reasoning_levels_for(name: str, capabilities: list[str], family: str = "", architecture: str = "") -> list[str]:
    if "thinking" not in capabilities:
        return []
    base = name.rsplit("/", 1)[-1].split(":", 1)[0].lower()
    if (family or "").lower() in LEVELED_FAMILIES or (architecture or "").lower() in LEVELED_FAMILIES \
            or base.startswith("gpt-oss"):
        return list(REASONING_LEVELS["levels"])
    return list(REASONING_LEVELS["boolean"])


def _format_parameters(count) -> str:
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return ""
    return f"{count / 1e9:.1f}B" if count >= 1e9 else f"{count / 1e6:.0f}M"


def parse_show(name: str, data: dict) -> dict:
    """The details BananaChat keeps from ``/api/show``."""
    if not isinstance(data, dict):
        raise ValueError("The model server returned no details.")
    raw = data.get("capabilities")
    capabilities = [value for value in raw if isinstance(value, str) and 0 < len(value) <= 40][:20] \
        if isinstance(raw, list) else []
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    info = data.get("model_info") if isinstance(data.get("model_info"), dict) else {}
    architecture = info.get("general.architecture") if isinstance(info.get("general.architecture"), str) else ""
    context = info.get(f"{architecture}.context_length") if architecture else None
    if not isinstance(context, int) or isinstance(context, bool):
        context = next((value for key, value in info.items() if isinstance(key, str)
                        and key.endswith(".context_length") and isinstance(value, int)
                        and not isinstance(value, bool)), None)
    family = str(details.get("family") or architecture or "")[:80]
    if not capabilities:
        # Servers older than capability reporting: guess from the family.
        capabilities = ["embedding"] if family.lower() in EMBEDDING_FAMILIES else ["completion"]
        if data.get("projector_info"):
            capabilities.append("vision")
    return {
        "capabilities": capabilities,
        "context_length": context if isinstance(context, int) and 0 < context < 100_000_000 else None,
        "family": family,
        "parameter_size": str(details.get("parameter_size") or _format_parameters(info.get("general.parameter_count"))
                              )[:40],
        "quantization": str(details.get("quantization_level") or "")[:40],
        "reasoning_levels": reasoning_levels_for(name, capabilities, family, architecture),
        "embedding_only": "embedding" in capabilities and "completion" not in capabilities,
    }


def _store_details(row, details: dict) -> None:
    fields = {
        "capabilities": json.dumps(details["capabilities"]), "embedding_only": int(details["embedding_only"]),
        "context_length": details["context_length"], "details_digest": row["backend_digest"],
        "details_at": db.now(), "details_error": None,
    }
    for key in ("family", "parameter_size", "quantization"):
        if details[key]:
            fields[key] = details[key]
    if not row["reasoning_levels_locked"]:
        fields["reasoning_levels"] = json.dumps(details["reasoning_levels"])
    if row["enrollment"] in ("new", "ignored") and not row["enrolled_at"]:
        # Nobody reviewed it yet: its features follow what the server reports.
        fields["supports_vision"] = int("vision" in details["capabilities"])
        fields["is_reasoning"] = int("thinking" in details["capabilities"])
    updated = bool(row["details_digest"]) and row["details_digest"] != row["backend_digest"]
    if updated and row["failing_at"]:
        fields["recheck_at"] = db.now()  # a new version may work: check it soon
    catalog.set_lifecycle(row["id"], **fields)
    if updated:
        _record(row, "updated", f"The model server has a new version (digest {str(row['backend_digest'])[:19]}…).")


def _refresh_details(config, deadline: float) -> list[dict]:
    errors = []
    rows = catalog.needing_details(retry_before=db.now(-timedelta(hours=1)), stale_before=db.now(-timedelta(days=1)),
                                   limit=DETAILS_PER_SYNC)
    for row in rows:
        if time.monotonic() > deadline:
            break
        name = _name(row)
        try:
            details = parse_show(name, ollama.show(name, config, timeout=DETAILS_TIMEOUT, primary=True))
        except (UpstreamError, OSError, ValueError) as error:
            message = str(error)[:300] or type(error).__name__
            catalog.set_lifecycle(row["id"], details_at=db.now(), details_error=message)
            errors.append({"model": name, "error": message})
            log.info("Could not read the details of %s: %s", name, message)
            continue
        try:
            _store_details(row, details)
        except Exception as error:  # noqa: BLE001 - one bad model must not stop the sync
            log.warning("Could not store the details of %s", name, exc_info=True)
            errors.append({"model": name, "error": f"Could not be stored: {type(error).__name__}"})
    return errors


def _listing_item(tag: dict) -> dict:
    details = tag.get("details") if isinstance(tag.get("details"), dict) else {}
    return {"name": tag["name"], "description": ollama.describe(tag), "digest": tag.get("digest"),
            "size": tag.get("size"), "family": details.get("family"), "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level")}


def sync(config=None, *, source: str = "background") -> dict:
    """Read the model server's list and reconcile the catalog. Raises when the server cannot be listed.

    A failed listing changes nothing (no model is counted as absent). An empty
    listing while the catalog has available models is only believed after it
    repeats ``missing_syncs`` times. Details come from ``/api/show`` for new or
    changed models (bounded per sync; one failing model never stops the others).
    """
    config = _config(config)
    started = time.monotonic()
    state = {"at": db.now(), "source": source, "ok": False, "count": 0, "new": [], "missing": [], "returned": [],
             "enabled": [], "errors": [], "error": "", "note": ""}
    try:
        tags = ollama.list_tags(config, timeout=15, primary=True)
    except (UpstreamError, OSError) as error:
        state["error"] = f"The model server could not be listed: {error}"[:500]
        _save_sync_state(state)
        raise
    current = policy()
    if not tags and db.scalar("SELECT COUNT(*) FROM ai_models WHERE backend='ollama' AND backend_available=1",
                              default=0):
        empties = int(site_settings.state_get(EMPTY_LISTINGS_KEY, 0) or 0) + 1
        if empties < current["missing_syncs"]:
            site_settings.state_set(EMPTY_LISTINGS_KEY, empties)
            state.update(ok=True, note="The model server listed no models; nothing was changed. It is checked "
                                       "again at the next sync.")
            _save_sync_state(state)
            return state
    site_settings.state_delete(EMPTY_LISTINGS_KEY)
    rules = patterns()
    with db.transaction():
        blocked = pulls_db.blocked_discovery_names()
        known = {pulls_db.canonical_ollama_name(row["ollama_name"])
                 for row in catalog.list_models(backend="ollama")}
        visible_tags = [tag for tag in tags if pulls_db.canonical_ollama_name(tag["name"]) not in blocked
                        or pulls_db.canonical_ollama_name(tag["name"]) in known]
        summary = catalog.sync_ollama([_listing_item(tag) for tag in visible_tags], missing_syncs=current["missing_syncs"],
                                      missing_minutes=current["missing_minutes"],
                                      is_ignored=lambda name: matches(name, rules))
        for model_id, _inserted in summary["inserted"]:
            row = catalog.get(model_id)
            if row["enrollment"] == "ignored":
                _record(row, "ignored", "Detected on the model server; matches the ignore list.")
            else:
                _record(row, "detected", "Detected on the model server; waiting for review.")
        for model_id, _returned in summary["returned"]:
            _record(catalog.get(model_id), "returned", "Listed by the model server again.")
        for model_id, _missing, reason in summary["missing"]:
            _record(catalog.get(model_id), "missing", reason)
    state["errors"] = _refresh_details(config, started + DETAILS_BUDGET)
    state["enabled"] = enroll_pending(current)
    ollama.remember_sizes(tags)
    state.update(ok=True, count=len(tags), new=[name for _id, name in summary["inserted"]][:50],
                 missing=[name for _id, name, _reason in summary["missing"]][:50],
                 returned=[name for _id, name in summary["returned"]][:50])
    try:
        from bananachat.services import claude_pool

        # Claude models enroll only once the pool is in use (accounts exist)
        # or already enrolled; otherwise every Ollama sync would add curated
        # Claude rows to sites that never asked for them.
        from bananachat.db import claude_pool as _pool_db

        has_claude = bool(db.scalar("SELECT 1 FROM ai_models WHERE backend='claude' LIMIT 1", default=None))
        has_accounts = bool(_pool_db.list_accounts())
        if has_claude or has_accounts:
            claude_state = claude_pool.sync_catalog(source=source)
            state["new"] = (state["new"] + claude_state.get("new", []))[:50]
            state["enabled"] = (state["enabled"] + claude_state.get("enabled", []))[:50]
            state["missing"] = (state["missing"] + claude_state.get("missing", []))[:50]
    except Exception:  # noqa: BLE001 - Claude enrollment must never break the Ollama sync
        log.warning("Claude catalog sync failed", exc_info=True)
        db.close_thread_connection()
    _save_sync_state(state)
    if state["errors"]:
        log.warning("Model sync: %d model(s) could not be inspected", len(state["errors"]))
    return state


def _save_sync_state(state: dict) -> None:
    previous = site_settings.state_get(SYNC_STATE_KEY, {}) or {}
    state = dict(state)
    state["errors"] = state.get("errors", [])[:20]
    state["last_success_at"] = state["at"] if state.get("ok") else previous.get("last_success_at")
    try:
        site_settings.state_set(SYNC_STATE_KEY, state)
    except Exception:  # noqa: BLE001 - the status line is informational
        log.warning("Could not save the model sync status", exc_info=True)


def sync_status() -> dict | None:
    value = site_settings.state_get(SYNC_STATE_KEY)
    return value if isinstance(value, dict) else None


# ----- enrollment ---------------------------------------------------------------------------------

def _preset_function():
    for module_name in ("bananachat.services.limits", "bananachat.db.limits"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        function = getattr(module, "apply_model_preset", None)
        if callable(function):
            return function
    return None


def apply_limit_preset(model_id: int, preset: str, actor=None) -> bool:
    """Store the preset and let the limits apply it. False when the limits could not (the preset stays stored)."""
    if preset not in PRESETS:
        raise ValueError("Unknown limit preset.")
    catalog.set_lifecycle(model_id, limit_preset=preset)
    function = _preset_function()
    if function is None:
        return False
    try:
        if actor is not None and "updated_by" in inspect.signature(function).parameters:
            function(model_id, preset, updated_by=actor["id"])
        else:
            function(model_id, preset)
    except Exception:  # noqa: BLE001 - the preset is stored; limits can be set by hand
        log.warning("Applying the %s limit preset to model %s failed", preset, model_id, exc_info=True)
        return False
    return True


def _auto_candidate(row) -> bool:
    if row["backend"] != "ollama":
        return False
    if row["enrolled_at"] or not row["backend_available"] or not row["details_at"] or row["details_error"]:
        return False
    if any(row[key] for key in ("missing_at", "failing_at", "deprecated_at", "retired_at", "delete_requested_at")):
        return False
    if row["embedding_only"] or (row["backend_digest"] and row["details_digest"] != row["backend_digest"]):
        return False
    capabilities = _loads(row["capabilities"], [])
    return "completion" in capabilities


def enroll_pending(current: dict | None = None) -> list[str]:
    """With the automatic policy, enable new models whose details are known. Returns their names."""
    current = current or policy()
    if current["enrollment"] != "automatic":
        return []
    enabled = []
    for row in catalog.with_enrollment("new"):
        if not _auto_candidate(row):
            continue
        with db.transaction():
            fresh = catalog.get(row["id"])
            if fresh is None or fresh["enrollment"] != "new" or not _auto_candidate(fresh):
                continue  # another process decided first
            preset = preset_for(fresh)
            capabilities = _loads(fresh["capabilities"], [])
            try:
                with db.transaction():
                    if not apply_limit_preset(fresh["id"], preset):
                        raise ValueError("The limit preset could not be applied.")
            except ValueError:
                catalog.set_lifecycle(fresh["id"], limit_preset=preset,
                                      state_reason="Waiting for review: its limit preset could not be applied.")
                continue
            reason = (f"Enabled automatically with the {preset} limit preset "
                      f"({fresh['parameter_size'] or 'unknown size'}); review it.")
            catalog.set_lifecycle(row["id"], enrollment="auto", enrolled_at=db.now(), is_rolled_out=1,
                                  limit_preset=preset, supports_vision=int("vision" in capabilities),
                                  is_reasoning=int("thinking" in capabilities), state_reason=reason)
            _record(fresh, "auto_enabled", reason)
        enabled.append(row["ollama_name"])
    return enabled


def enable(model, actor=None, *, preset: str | None = None) -> None:
    """An administrator enables a model after reviewing it: published and reviewed. Raises ValueError for an
    ignored model (restore it first)."""
    with db.transaction():
        fresh = catalog.get(model["id"])
        if fresh is None:
            raise ValueError("The model no longer exists; refresh the catalog.")
        if fresh["enrollment"] == "ignored":
            raise ValueError(f"{fresh['ollama_name']} is on the ignore list; restore it first.")
        if fresh["retired_at"] or fresh["delete_requested_at"]:
            raise ValueError("This model is retired or being deleted; restore it before publishing it.")
        current_preset = fresh["limit_preset"] or preset_for(fresh)
        preset = preset or current_preset
        from bananachat.db import limits as limits_db

        preserve_policy = fresh["backend"] in ("claude", "external") and \
            limits_db.has_model_policy(fresh["id"]) and preset == current_preset
        if preserve_policy:
            # Enrollment installs hosted-model limits before publication. A
            # review/re-publication must not reset those limits or custom edits.
            catalog.set_lifecycle(fresh["id"], limit_preset=preset)
        elif not apply_limit_preset(fresh["id"], preset, actor):
            raise ValueError("The limit preset could not be applied. The model stays unpublished; try again or "
                             "check its limit settings.")
        reason = f"Enabled by an administrator with the {preset} limit preset."
        catalog.set_lifecycle(model["id"], enrollment="reviewed", enrolled_at=db.now(), is_rolled_out=1,
                              state_reason=reason)
        _record(fresh, "enabled", reason, actor)


def mark_reviewed(model, actor=None) -> None:
    with db.transaction():
        catalog.set_lifecycle(model["id"], enrollment="reviewed", state_reason="Reviewed by an administrator.")
        _record(model, "reviewed", "Reviewed by an administrator.", actor)


def set_preset(model, preset: str, actor=None) -> bool:
    applied = apply_limit_preset(model["id"], preset, actor)
    _record(model, "limit_preset", f"Limit preset set to {preset}." if applied else
            f"Limit preset set to {preset} (stored; the limits did not apply it).", actor)
    return applied


def set_reasoning_levels(model, choice: str, actor=None) -> None:
    """``auto`` follows detection again; ``none``, ``boolean`` or ``levels`` fixes the list."""
    if choice == "auto":
        details = _loads(model["capabilities"], [])
        levels = reasoning_levels_for(_name(model), details, model["family"] or "")
        catalog.set_lifecycle(model["id"], reasoning_levels=json.dumps(levels), reasoning_levels_locked=0)
    elif choice in REASONING_LEVELS:
        catalog.set_lifecycle(model["id"], reasoning_levels=json.dumps(REASONING_LEVELS[choice]),
                              reasoning_levels_locked=1)
    else:
        raise ValueError("Unknown reasoning levels.")


def reasoning_levels(model) -> list[str]:
    """The reasoning levels a model accepts (``[]`` when it cannot reason)."""
    return [level for level in _loads(model["reasoning_levels"], []) if isinstance(level, str)]


# ----- failures -----------------------------------------------------------------------------------

def counts_as_failure(error) -> bool:
    """Whether an error from the model server says something about the model (not the network or a busy server)."""
    if not isinstance(error, UpstreamError):
        return False
    # 404: the server does not have the model (removed by hand): the sync marks it missing, not failing.
    if error.status in (401, 403, 404, 408, 429, 502, 503, 504):
        return False
    return getattr(error, "kind", None) != "connect"


def _outage(config) -> bool:
    if config.ollama_is_local:
        return False
    if health.inference_down() or health.using_fallback(config):
        return True
    return bool(health.status().get("failures"))


TIMEOUTS_KEY = "model_timeouts"
TIMEOUTS_TOGETHER_SECONDS = 300


def _timeout_under_load(model, error) -> bool:
    """A timeout (no first token, a stalled stream) counts only when the system is not overloaded.

    Overloaded: requests are waiting for a slot in the inference queue, or another model timed out within
    ``TIMEOUTS_TOGETHER_SECONDS`` (several models slow at once point at the server). Every timeout is
    remembered for that comparison. (An outage seen by the health probe is excluded before.)
    """
    if getattr(error, "kind", None) != "timeout":
        return False
    from bananachat.services import queue

    now, name = time.time(), model["ollama_name"]
    with db.transaction():
        seen = site_settings.state_get(TIMEOUTS_KEY, {}) or {}
        recent = {key: value for key, value in seen.items() if isinstance(key, str)
                  and isinstance(value, (int, float)) and now - value < TIMEOUTS_TOGETHER_SECONDS} \
            if isinstance(seen, dict) else {}
        together = any(key != name for key in recent)
        recent[name] = now
        site_settings.state_set(TIMEOUTS_KEY, dict(list(recent.items())[-100:]))
    return together or queue.stats()["waiting"] > 0


def record_failure(model, error, config=None) -> bool:
    """Count a failed load or generation; marks the model failing at the threshold. True when it did."""
    try:
        config = _config(config)
        if model is None or model["backend"] != "ollama" or not counts_as_failure(error) or _outage(config):
            return False
        if _timeout_under_load(model, error):
            log.info("Model %s timed out while the server was busy; not counted as a failure", model["ollama_name"])
            return False
        current = policy()
        now = db.now()
        with db.transaction():
            row = catalog.record_failure(
                model["id"], str(error) or type(error).__name__, now=now,
                window_start=db.now(-timedelta(minutes=current["failure_window_minutes"])))
            if row is None or row["failing_at"] or row["recent_failures"] < current["failure_threshold"]:
                return False
            reason = (f"{row['recent_failures']} failures within {current['failure_window_minutes']} minutes without "
                      f"a successful answer. Last error: {str(error)[:300]}")
            catalog.set_lifecycle(model["id"], failing_at=now, recheck_attempts=0, state_reason=reason,
                                  recheck_at=db.now(timedelta(minutes=current["recheck_minutes"])))
            _record(row, "failing", reason)
        return True
    except Exception:  # noqa: BLE001 - bookkeeping must never break a request
        log.warning("Could not record a failure of model %s", model["ollama_name"] if model else "?", exc_info=True)
        db.close_thread_connection()
        return False


def record_success(model) -> None:
    try:
        if model is not None and model["backend"] == "ollama":
            catalog.reset_failures(model["id"])
    except Exception:  # noqa: BLE001 - bookkeeping must never break a request
        log.warning("Could not record a success of model %s", model["ollama_name"], exc_info=True)
        db.close_thread_connection()


def gone(model, error: str = "") -> bool:
    """Whether a request failed because the model server no longer has *model*: it is being deleted, was
    deleted or went missing, or the server answered that it does not know it (removed by hand, not synced yet)."""
    if model is None or model["backend"] != "ollama":
        return False
    fresh = catalog.get(model["id"])
    if fresh is None or fresh["delete_requested_at"] or fresh["missing_at"] or not fresh["backend_available"]:
        return True
    lowered = (error or "").lower()
    return "model" in lowered and "not found" in lowered


def retry_check(model, actor=None) -> None:
    with db.transaction():
        catalog.set_lifecycle(model["id"], recheck_at=db.now(), recheck_attempts=0)
        _record(model, "recheck_requested", "A test prompt is sent within a minute.", actor)


def force_enable(model, actor=None) -> None:
    with db.transaction():
        reason = "Enabled again by an administrator despite failures."
        catalog.set_lifecycle(model["id"], failing_at=None, recheck_at=None, recheck_attempts=0, recent_failures=0,
                              first_failure_at=None, state_reason=reason)
        _record(model, "force_enabled", reason, actor)


def _probe(row, config):
    """Send the test prompt when the queue has room. ``(ok, error)``, or None when busy."""
    from bananachat.services import queue

    name = _name(row)
    try:
        with queue.Slot(queue.PRIORITY_SLOW, owner_key="system:model-check", model=name) as slot:
            slot.wait(timeout=5)
            ollama.probe(name, config, prompt=PROBE_PROMPT)
    except (queue.QueueFull, queue.QueueTimeout):
        return None
    except (UpstreamError, OSError) as error:
        return False, str(error)[:300] or type(error).__name__
    return True, ""


def recheck_failing(config=None, current: dict | None = None) -> list[tuple[str, bool]]:
    """Test failing models that are due (off-peak: only when the inference queue has room right away)."""
    config = _config(config)
    current = current or policy()
    results = []
    for row in catalog.failing_due(db.now(), limit=2):
        if not row["backend_available"]:
            catalog.set_lifecycle(row["id"], recheck_at=db.now(timedelta(minutes=current["recheck_minutes"])))
            continue
        outcome = _probe(row, config)
        if outcome is None:
            catalog.set_lifecycle(row["id"], recheck_at=db.now(timedelta(minutes=5)))
            continue
        ok, error = outcome
        with db.transaction():
            fresh = catalog.get(row["id"])
            if fresh is None or not fresh["failing_at"]:
                continue
            if ok:
                reason = "Answered a test prompt again."
                catalog.set_lifecycle(row["id"], failing_at=None, recheck_at=None, recheck_attempts=0,
                                      recent_failures=0, first_failure_at=None, state_reason=reason)
                _record(fresh, "recovered", reason)
            else:
                attempts = fresh["recheck_attempts"] + 1
                delay = min(MAX_RECHECK_SECONDS, current["recheck_minutes"] * 60 * 2 ** attempts)
                reason = f"Still failing a test prompt: {error}"
                catalog.set_lifecycle(row["id"], recheck_attempts=attempts, last_failure=error, state_reason=reason,
                                      recheck_at=db.now(timedelta(seconds=delay)))
                if attempts <= 3:
                    _record(fresh, "recheck_failed", reason)
        results.append((row["ollama_name"], ok))
    return results


# ----- deprecation and retirement ----------------------------------------------------------------------

def _check_replacement(model, replacement_id):
    if replacement_id in (None, "", 0):
        return None
    replacement = catalog.get(int(replacement_id))
    if replacement is None:
        raise ValueError("The replacement model no longer exists.")
    if replacement["id"] == model["id"]:
        raise ValueError("A model cannot replace itself.")
    if replacement["backend"] != model["backend"]:
        raise ValueError("The replacement must be the same kind of model.")
    if replacement["retired_at"]:
        raise ValueError("The replacement is retired; choose another model.")
    seen, current = {model["id"]}, replacement
    for _ in range(20):
        if current is None or not current["replacement_id"]:
            break
        if current["replacement_id"] in seen:
            raise ValueError("That replacement would replace this model in a loop.")
        seen.add(current["id"])
        current = catalog.get(current["replacement_id"])
    return replacement


def deprecate(model, actor=None, *, replacement_id=None, retire_at: str | None = None, note: str = "") -> None:
    replacement = _check_replacement(model, replacement_id)
    reason = "Deprecated" + (f"; replaced by {replacement['display_name']}" if replacement else "") + \
        (f"; retired on {retire_at} UTC" if retire_at else "") + "." + (f" {note}" if note else "")
    with db.transaction():
        catalog.set_lifecycle(model["id"], deprecated_at=model["deprecated_at"] or db.now(),
                              replacement_id=replacement["id"] if replacement else None, retire_at=retire_at,
                              deprecation_note=note[:500] or None, retired_at=None, state_reason=reason)
        _record(model, "deprecated", reason, actor)


def undeprecate(model, actor=None) -> None:
    with db.transaction():
        reason = "No longer deprecated."
        catalog.set_lifecycle(model["id"], deprecated_at=None, replacement_id=None, retire_at=None, retired_at=None,
                              deprecation_note=None, state_reason=reason)
        _record(model, "undeprecated", reason, actor)


def retire(model, actor=None, reason: str = "Retired by an administrator.") -> None:
    replacement = catalog.get(model["replacement_id"]) if model["replacement_id"] else None
    with db.transaction():
        catalog.set_lifecycle(model["id"], retired_at=db.now(), deprecated_at=model["deprecated_at"] or db.now(),
                              state_reason=reason)
        moved = catalog.retarget_personalities(model["ollama_name"], replacement["ollama_name"] if replacement else "")
        _record(model, "retired", reason + (f" {moved} personalit{'y' if moved == 1 else 'ies'} now "
                                            f"{'prefer ' + replacement['display_name'] if replacement else 'use the default model'}."
                                            if moved else ""), actor)


def retire_due() -> int:
    count = 0
    now = db.now()
    for row in catalog.retirement_due(now):
        with db.transaction():
            fresh = catalog.get(row["id"])
            if (fresh is not None and fresh["deprecated_at"] and not fresh["retired_at"] and
                    fresh["retire_at"] and fresh["retire_at"] <= now):
                retire(fresh, None, "Its retirement date passed.")
                count += 1
    return count


def replacement_for(model, context, surface: str = "chat"):
    """The first usable model down the replacement chain (preferring one that is not deprecated itself)."""
    from bananachat.services.access import is_text_model

    seen, current, fallback = {model["id"]}, model, None
    for _ in range(20):
        replacement_id = current["replacement_id"]
        if not replacement_id or replacement_id in seen:
            break
        seen.add(replacement_id)
        current = catalog.get(replacement_id)
        if current is None:
            break
        if current["retired_at"] or not is_text_model(current) or not context.can_use(current, surface):
            continue
        if not current["deprecated_at"]:
            return current
        fallback = fallback or current
    return fallback


# ----- deletion -------------------------------------------------------------------------------------------

class ModelInUse(ValueError):
    """Requests are using the model; ``count`` of them."""

    def __init__(self, count: int):
        super().__init__(f"{count} request{'s are' if count != 1 else ' is'} using this model right now.")
        self.count = count


def in_use(model) -> int:
    from bananachat.services import queue

    return catalog.in_use(_name(model), queue.LEASE_SECONDS)


def delete_from_server(model, actor=None, *, when_idle: bool = False, config=None) -> str:
    """Delete an Ollama model from the model server: ``deleted``, ``absent`` (was not installed) or ``scheduled``.

    The model is hidden first, so no new request picks it; running requests
    make this raise :class:`ModelInUse`, or with *when_idle* the deletion waits
    for them (the ``model-lifecycle`` job finishes it).
    """
    config = _config(config)
    if model["backend"] == "claude":
        raise ValueError("Claude subscription models are removed from the catalog, not from a model server: "
                         "retire or delete the catalog entry instead.")
    if model["backend"] != "ollama":
        raise ValueError("Only Ollama models can be deleted from here; remove checkpoints on the ComfyUI server.")
    name = _name(model)
    who = actor["username"] if actor else "the system"
    with db.transaction():
        model = catalog.get(model["id"])
        if model is None:
            raise ValueError("The model no longer exists; refresh the catalog.")
        if pulls_db.active_for(name, "ollama"):
            raise ValueError(f"A download of {name} is queued or running; cancel it first.")
        already = model["delete_requested_at"]
        catalog.set_lifecycle(model["id"], delete_requested_at=already or db.now(),
                              state_reason=f"Being deleted from the model server ({who}).")
        count = in_use(model)
        if count:
            if when_idle:
                _record(model, "delete_scheduled", f"Deleted when its {count} running request"
                                                   f"{'s finish' if count != 1 else ' finishes'}.", actor)
                return "scheduled"
            raise ModelInUse(count)  # the transaction also restores the previous visibility
    try:
        with operation_lock(name, config):
            fresh = catalog.get(model["id"])
            if fresh is None or not fresh["delete_requested_at"]:
                raise ValueError("The deletion was cancelled; refresh the catalog.")
            return _delete_now(fresh, actor, config)
    except Timeout:
        if when_idle:
            _record(model, "delete_scheduled", "Deleted after its current model operation finishes.", actor)
            return "scheduled"
        if not already:
            catalog.set_lifecycle(model["id"], delete_requested_at=None, state_reason=model["state_reason"])
        raise ValueError("An operation on this model is still finishing. Try again shortly or delete it when idle.") \
            from None
    except (UpstreamError, OSError):
        if not already:
            catalog.set_lifecycle(model["id"], delete_requested_at=None, state_reason=model["state_reason"])
        raise


def cancel_delete(model, actor=None) -> None:
    try:
        with operation_lock(_name(model)):
            with db.transaction():
                catalog.set_lifecycle(model["id"], delete_requested_at=None, state_reason="Deletion cancelled.")
                _record(model, "delete_cancelled", "Deletion cancelled.", actor)
    except Timeout:
        raise ValueError("Deletion has already started on the model server; it can no longer be cancelled.") from None


def _delete_now(model, actor, config) -> str:
    from bananachat.services import model_recovery

    name = _name(model)
    existed = ollama.delete(name, config)
    # The pending-deletion marker excludes recovery checks while the backend
    # call runs. A failed call must not suppress future recovery of the model.
    model_recovery.ignore([name], config)
    replacement = catalog.get(model["replacement_id"]) if model["replacement_id"] else None
    reason = (f"Deleted from the model server by {actor['username']}." if actor else
              "Deleted from the model server after its requests finished.")
    with db.transaction():
        catalog.set_lifecycle(model["id"], delete_requested_at=None, backend_available=0, missing_at=db.now(),
                              missing_reason=reason, state_reason=reason)
        moved = catalog.retarget_personalities(model["ollama_name"], replacement["ollama_name"] if replacement else "")
        _record(model, "deleted", reason + (f" {moved} personalit{'y' if moved == 1 else 'ies'} updated."
                                            if moved else ""), actor)
    # Only this row changes here (a full sync with /api/show reads could hold a web request for a minute);
    # the background sync reconciles the rest.
    return "deleted" if existed else "absent"


def process_pending_deletes(config=None) -> int:
    config = _config(config)
    done = 0
    for row in catalog.pending_deletes():
        if in_use(row) or pulls_db.active_for(_name(row), "ollama"):
            continue  # a download queued before the deletion was asked for: it waits (or is cancelled) first
        try:
            with operation_lock(_name(row), config):
                fresh = catalog.get(row["id"])
                if fresh is None or not fresh["delete_requested_at"]:
                    continue  # "Keep it" was chosen meanwhile
                _delete_now(fresh, None, config)
                done += 1
        except Timeout:
            continue
        except (UpstreamError, OSError) as error:
            catalog.set_lifecycle(row["id"], state_reason=f"Deleting from the model server failed: {error}; "
                                                          "trying again shortly.")
            log.warning("Deleting %s from the model server failed: %s", row["ollama_name"], error)
    return done


def purge_missing(current: dict | None = None) -> int:
    """Remove models missing for longer than the retention period (history keeps their names)."""
    current = current or policy()
    status = sync_status()
    if _outage(_config()) or (status is not None and not status.get("ok")):
        return 0  # a failed listing never establishes that a model is still missing
    cutoff = db.now(-timedelta(days=current["retention_days"]))
    removed = 0
    for row in catalog.missing_before(cutoff):
        try:
            with operation_lock(_name(row), backend=row["backend"]):
                with db.transaction():
                    fresh = catalog.get(row["id"])
                    if (fresh is None or not fresh["missing_at"] or fresh["backend_available"] or
                            fresh["missing_at"] >= cutoff or fresh["delete_requested_at"] or in_use(fresh) or
                            pulls_db.active_for(_name(fresh), fresh["backend"])):
                        continue
                    _record(fresh, "removed", f"Missing for more than {current['retention_days']} days; removed from the "
                                              "catalog. Chats keep its name.")
                    catalog.delete(fresh["id"])
                    removed += 1
        except Timeout:
            continue
    return removed


# ----- display ---------------------------------------------------------------------------------------------

def badges(model) -> list[dict]:
    """State badges for the administrator: ``{"label", "tone", "title"}``."""
    result = []
    enrollment = model["enrollment"]
    if enrollment == "new":
        result.append({"label": "New — waiting for review", "tone": "info", "title": model["state_reason"] or ""})
    elif enrollment == "auto":
        result.append({"label": "Enabled automatically — review", "tone": "warning", "title": ""})
    elif enrollment == "ignored":
        result.append({"label": "Ignored", "tone": "", "title": model["state_reason"] or ""})
    if model["delete_requested_at"]:
        result.append({"label": "Deleting after current requests", "tone": "warning", "title": ""})
    if model["missing_at"]:
        result.append({"label": "Missing", "tone": "danger", "title": model["missing_reason"] or ""})
    if model["failing_at"]:
        result.append({"label": "Failing", "tone": "danger", "title": model["state_reason"] or ""})
    if model["retired_at"]:
        result.append({"label": "Retired", "tone": "", "title": model["state_reason"] or ""})
    elif model["deprecated_at"]:
        result.append({"label": "Deprecated", "tone": "warning", "title": model["state_reason"] or ""})
    if model["embedding_only"]:
        result.append({"label": "Embeddings only", "tone": "", "title": "Cannot be used for chat."})
    return result


def capabilities(model) -> list[str]:
    return [value for value in _loads(model["capabilities"], []) if isinstance(value, str)]


# ----- background ----------------------------------------------------------------------------------------

def _step(name: str, function, *args) -> None:
    try:
        function(*args)
    except Exception:  # noqa: BLE001 - one step must not stop the others
        log.warning("Model lifecycle step %s failed", name, exc_info=True)
        db.close_thread_connection()


# Its own thread: a test prompt may wait minutes for a slow model, which must not hold up the health probe
# and the catalog sync on the shared loop of periodic jobs.
@background.job("model-lifecycle", every=30, initial_delay=25, long_running=True)
def lifecycle_job(app) -> None:
    """Retire due models, remove long-missing ones, finish waiting deletions and re-test failing models."""
    config = app.config["BC"]
    current = policy()
    _step("retire", retire_due)
    _step("purge-missing", purge_missing, current)
    if time.monotonic() - _state["events_pruned"] > 3600:
        _state["events_pruned"] = time.monotonic()
        _step("prune-events", lifecycle_db.purge_events, db.now(-timedelta(days=EVENT_RETENTION_DAYS)))
    if _outage(config):
        return
    _step("pending-deletes", process_pending_deletes, config)
    _step("recheck", recheck_failing, config, current)
