"""Models missing after a restore (or on a rebuilt compute server), offered for download.

A weight-free restore writes ``<instance>/.model-recovery.json``
(``banana_ops/models.py``)::

    {"schema": 1, "state": "pending",
     "inventory": {"ollama": ["llama3.2:3b", ...],
                   "huggingface": [{"repo_id", "source_filename", "revision", "target_name",
                                    "expected_sha256", "expected_size"}, ...],
                   "manual": [{"backend", "name", "reason"}, ...],
                   "excluded_paths": [...], "sizes": {"llama3.2:3b": 2019393189, ...}}}

The background job :func:`check` compares the list with the models the
Ollama server (the compute server in a two-server setup) and ComfyUI have.
When nothing is missing the record completes silently; otherwise the
administrator sees one card: download the missing models, choose some, or
dismiss the list. Without an open record the same check watches the
published catalog, so a compute server rebuilt from scratch or from its own
backup is noticed here too (``"origin": "catalog"``); models dismissed there
are remembered in ``ignored`` until they are installed again.

``state`` is ``pending`` (nothing decided), ``deferred`` ("Not now"),
``requested`` (downloads queued), ``complete`` or ``dismissed``; the first
three are open. The command-line tool (``bananachat models``) reads the same
format, so unknown keys are preserved.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from filelock import FileLock
from flask import current_app

from bananachat.db import catalog
from bananachat.db import pulls as pulls_db
from bananachat.services import checkpoint_agent, health, ollama, pulls
from bananachat.services.upstream import UpstreamError

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SELECTION = 256
STATES = ("pending", "deferred", "requested", "complete", "dismissed")
OPEN_STATES = ("pending", "deferred", "requested")
# An administrator opening a page re-checks at most this often (seconds);
# after a failed check, wait longer before trying again from a page.
PAGE_CHECK_AGE = 5
PAGE_RETRY_AGE = 60


def path(config=None) -> Path:
    return (config or current_app.config["BC"]).model_recovery_file


def read(config=None) -> dict | None:
    """The recovery record, or None when there is none. Raises ValueError when it is malformed."""
    target = path(config)
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("The model recovery file is not a regular file of a reasonable size.")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("The model recovery file is not valid JSON.") from None
    inventory = value.get("inventory") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(inventory, dict)
            or not all(isinstance(inventory.get(key), list) and len(inventory[key]) <= 10000
                       for key in ("ollama", "huggingface", "manual"))):
        raise ValueError("The model recovery file has an unknown format.")
    return value


def write(value: dict, config=None) -> None:
    """Replace the record atomically (same JSON layout as the command-line tool)."""
    target = path(config)
    if target.is_symlink():
        raise ValueError("The model recovery file cannot be a symbolic link.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".model-recovery-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _lock(config=None) -> FileLock:
    return FileLock(str(path(config)) + ".lock", timeout=10, mode=0o600)


def _state(record: dict) -> str:
    return record.get("state") if record.get("state") in STATES else "pending"


def choices(record: dict) -> list[dict]:
    """Downloadable items: ``{"id": "ollama:0" | "hf:0", "label", "source", "kind"}``."""
    result = []
    for index, name in enumerate(record["inventory"]["ollama"]):
        if isinstance(name, str):
            result.append({"id": f"ollama:{index}", "label": name, "kind": "ollama",
                           "source": "Hugging Face GGUF" if name.startswith(("hf.co/", "huggingface.co/"))
                           else "Ollama library"})
    for index, recipe in enumerate(record["inventory"]["huggingface"]):
        if isinstance(recipe, dict):
            result.append({"id": f"hf:{index}", "label": str(recipe.get("target_name") or "Unknown checkpoint"),
                           "kind": "huggingface",
                           "source": f"Hugging Face {recipe.get('repo_id', '?')} @ {recipe.get('revision', '?')}"})
    return result


def manual_items(record: dict) -> list[dict]:
    return [item for item in record["inventory"]["manual"] if isinstance(item, dict)]


# ----- what is missing ---------------------------------------------------------------

def _installed(name: str, installed: set[str]) -> bool:
    return (name in installed or f"{name}:latest" in installed
            or (name.endswith(":latest") and name.removesuffix(":latest") in installed))


def _checkpoint_installed(name: str) -> bool:
    row = catalog.get_by_name(f"comfyui:{name}")
    return row is not None and bool(row["backend_available"])


def _missing(record: dict, installed: set[str]) -> tuple[list[str], list[int]]:
    """Ids of missing downloadable items and indexes of missing manual items."""
    missing = [item["id"] for item in choices(record)
               if not (_installed(item["label"], installed) if item["kind"] == "ollama"
                       else _checkpoint_installed(item["label"]))]
    manual = []
    for index, item in enumerate(record["inventory"]["manual"]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        present = (_installed(name, installed) if item.get("backend") == "ollama"
                   else item.get("backend") == "comfyui" and _checkpoint_installed(name))
        if not present:
            manual.append(index)
    return missing, manual


def _downloading(record: dict) -> bool:
    """Whether a download queued from this list is still waiting or running."""
    for item in record.get("jobs") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            job = pulls_db.get(item["id"])
            if job is not None and job["status"] in pulls_db.ACTIVE:
                return True
    return False


def check(config=None, *, timeout: float = 10, watch_catalog: bool = True) -> None:
    """Compare the saved list with the installed models; complete it silently when nothing is missing.

    Without an open record, published catalog models that the Ollama server
    no longer has start a new record (``watch_catalog``).
    """
    config = config or current_app.config["BC"]
    try:
        record = read(config)
    except ValueError:
        return
    is_open = record is not None and _state(record) in OPEN_STATES
    if not is_open and not watch_catalog:
        return
    listed_at = time.time()
    try:
        installed = ollama.installed_names(config, timeout=timeout, primary=True)
    except (UpstreamError, OSError, ValueError) as error:
        if is_open:
            with _lock(config):
                current = read(config)
                if current is not None and _state(current) in OPEN_STATES:
                    current.update(check_error=str(error)[:300], checked_at=time.time())
                    write(current, config)
        return
    with _lock(config):
        try:
            record = read(config)
        except ValueError:
            return
        if record is not None and _state(record) in OPEN_STATES:
            missing, manual = _missing(record, installed)
            record.update(checked_at=time.time(), check_error="", missing=missing, manual_missing=manual)
            if not missing and not manual:
                record["state"] = "complete"
            elif _state(record) == "requested" and not _downloading(record):
                # Every queued download ended and something is still missing
                # (a failed or cancelled download): offer it again.
                record["state"] = "deferred"
            write(record, config)
        elif watch_catalog:
            _watch_catalog(record, installed, config, listed_at)


def _watch_catalog(record: dict | None, installed: set[str], config, listed_at: float = 0.0) -> None:
    previous = [name for name in (record or {}).get("ignored") or [] if isinstance(name, str)]
    if float((record or {}).get("ignored_at") or 0) >= listed_at:
        # Listed before an administrator deleted a model on purpose: the list
        # may still show it, which must not make it watched again.
        ignored = sorted(set(previous))
    else:
        # A dismissed model that is installed again is watched again.
        ignored = sorted({name for name in previous if not _installed(name, installed)})
    gone = []
    for row in catalog.list_models(rolled_out_only=True, backend="ollama"):
        if row["retired_at"] or row["enrollment"] == "ignored":
            continue  # withdrawn on purpose: not worth downloading again
        name = row["backend_model_name"] or row["ollama_name"]
        if not _installed(name, installed) and name not in ignored and name not in gone:
            gone.append(name)
    if gone:
        write({"schema": 1, "state": "pending", "origin": "catalog", "checked_at": time.time(), "check_error": "",
               "inventory": {"ollama": gone[:10000], "huggingface": [], "manual": []},
               "missing": [f"ollama:{index}" for index in range(len(gone[:10000]))], "manual_missing": [],
               "ignored": ignored}, config)
    elif record is not None and ignored != sorted(previous):
        record["ignored"] = ignored
        write(record, config)


def refresh(config=None) -> None:
    """Re-check an open record when an administrator opens a page (bounded, never slow twice in a row)."""
    config = config or current_app.config["BC"]
    try:
        record = read(config)
    except ValueError:
        return
    if record is None or _state(record) not in OPEN_STATES:
        return
    age = time.time() - float(record.get("checked_at") or 0)
    if age < (PAGE_RETRY_AGE if record.get("check_error") else PAGE_CHECK_AGE):
        return
    if not config.ollama_is_local and health.inference_down():
        return
    try:
        check(config, timeout=4, watch_catalog=False)
    except OSError:
        return  # another process is changing the list (filelock.Timeout); the page shows the saved state


# ----- display ----------------------------------------------------------------------

def _sizes(record: dict) -> dict[str, int]:
    saved = record["inventory"].get("sizes")
    sizes = {name: size for name, size in saved.items()
             if isinstance(name, str) and type(size) is int and size > 0} if isinstance(saved, dict) else {}
    sizes.update(ollama.known_sizes())
    return sizes


def status(config=None) -> dict | None:
    """The record decorated for display."""
    try:
        record = read(config)
    except ValueError as error:
        return {"state": "invalid", "error": str(error), "open": True, "choices": [], "missing_choices": [],
                "manual": [], "jobs": [], "errors": []}
    if record is None:
        return None
    jobs = []
    for item in record.get("jobs") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            job = pulls_db.get(item["id"])
            jobs.append({"id": item["id"], "model": str(item.get("model") or ""),
                         "status": job["status"] if job else "removed"})
    state = _state(record)
    checked = bool(record.get("checked_at")) and isinstance(record.get("missing"), list)
    every = choices(record)
    missing_ids = set(record["missing"]) if checked else {item["id"] for item in every}
    missing_choices = [item for item in every if item["id"] in missing_ids]
    manual = manual_items(record)
    if checked and isinstance(record.get("manual_missing"), list):
        indexes = set(record["manual_missing"])
        manual = [item for index, item in enumerate(record["inventory"]["manual"])
                  if index in indexes and isinstance(item, dict)]
    sizes, total, known = _sizes(record), 0, 0
    recipes = record["inventory"]["huggingface"]
    for item in missing_choices:
        if item["kind"] == "ollama":
            size = sizes.get(item["label"])
        else:
            recipe = recipes[int(item["id"].split(":", 1)[1])]
            size = recipe.get("expected_size") if type(recipe.get("expected_size")) is int else None
        if size:
            total, known = total + size, known + 1
    return {"state": state, "open": state in OPEN_STATES, "origin": record.get("origin") or "restore",
            "checked": checked, "check_error": str(record.get("check_error") or ""),
            "choices": every, "missing_choices": missing_choices, "installed_count": len(every) - len(missing_choices),
            "manual": manual, "missing_count": len(missing_choices) + len(manual),
            "size_bytes": total or None, "size_complete": bool(missing_choices) and known == len(missing_choices),
            "jobs": jobs, "active_jobs": sum(job["status"] in pulls_db.ACTIVE for job in jobs),
            "errors": [item for item in record.get("errors") or [] if isinstance(item, dict)]}


# ----- decisions --------------------------------------------------------------------

def defer(user_id: str | None, config=None) -> None:
    """"Not now": cancel downloads queued from this list and keep it for later."""
    with _lock(config):
        record = read(config)
        if record is None:
            raise ValueError("There is no restored model list to review.")
        for item in record.get("jobs") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                pulls.cancel(item["id"])
        record["state"] = "deferred"
        write(record, config)


def dismiss(user_id: str | None, config=None) -> None:
    """Close the list; its missing models are not offered again until they are installed once more."""
    with _lock(config):
        record = read(config)
        if record is None:
            raise ValueError("There is no restored model list to review.")
        every = {item["id"]: item for item in choices(record)}
        missing = record.get("missing") if isinstance(record.get("missing"), list) else list(every)
        names = {every[item]["label"] for item in missing if item in every and every[item]["kind"] == "ollama"}
        names |= {str(item.get("name")) for item in manual_items(record) if item.get("backend") == "ollama"}
        ignored = {name for name in record.get("ignored") or [] if isinstance(name, str)}
        record.update(state="dismissed", ignored=sorted(ignored | names))
        write(record, config)


def ignore(names: list[str], config=None) -> None:
    """Do not offer these models as missing (an administrator removed them on purpose)."""
    with _lock(config):
        try:
            record = read(config)
        except ValueError:
            return
        if record is None:
            record = {"schema": 1, "state": "dismissed", "origin": "catalog",
                      "inventory": {"ollama": [], "huggingface": [], "manual": []}}
        ignored = {name for name in record.get("ignored") or [] if isinstance(name, str)}
        record.update(ignored=sorted(ignored | set(names)), ignored_at=time.time())
        write(record, config)


def download(selected: list[str], user_id: str | None, config=None) -> dict:
    """Queue the selected items; models that are already installed are skipped."""
    config = config or current_app.config["BC"]
    selected = sorted(set(selected or []))
    installed: set[str] = set()
    if any(item.startswith("ollama:") for item in selected):
        # Asked before taking the lock: a slow compute server must not keep
        # the other process (pages, the background check) waiting.
        try:
            installed = ollama.installed_names(config, primary=True)
        except (UpstreamError, OSError):
            raise ValueError("The Ollama server cannot be reached; try again when it is running.") from None
    with _lock(config):
        record = read(config)
        if record is None:
            raise ValueError("There is no restored model list to review.")
        available = {item["id"] for item in choices(record)}
        if not selected or len(selected) > MAX_SELECTION or not set(selected) <= available:
            raise ValueError(f"Select between 1 and {MAX_SELECTION} models to download, or choose Not now.")
        if any(item.startswith("hf:") for item in selected):
            problem = checkpoint_agent.configuration_error(config)
            if problem or not config.images_enabled:
                raise ValueError("Enable image generation and configure the checkpoint download agent before "
                                 "restoring Hugging Face checkpoints.")
        queued, skipped, errors = [], [], []
        for item in selected:
            kind, index = item.split(":", 1)
            label = item
            try:
                if kind == "ollama":
                    name = record["inventory"]["ollama"][int(index)]
                    label = name
                    if _installed(name, installed):
                        skipped.append(label)
                        continue
                    job_id = pulls.enqueue_ollama(name, user_id, config)
                else:
                    recipe = record["inventory"]["huggingface"][int(index)]
                    label = str(recipe.get("target_name") or "checkpoint")
                    if _checkpoint_installed(label):
                        skipped.append(label)
                        continue
                    job_id = pulls.enqueue_checkpoint({key: recipe.get(key) for key in pulls_db.RECIPE_FIELDS},
                                                      user_id, config)
                queued.append({"id": job_id, "model": label})
            except ValueError as error:
                errors.append({"model": label, "reason": str(error)})
        jobs = {item["id"]: item for item in [*(record.get("jobs") or []), *queued] if isinstance(item, dict)}
        record.update(state="deferred" if errors else "requested", jobs=list(jobs.values()), errors=errors)
        if not queued and not errors and installed:
            # Everything selected was already there; the list may be complete.
            missing, manual = _missing(record, installed)
            record.update(checked_at=time.time(), check_error="", missing=missing, manual_missing=manual)
            if not missing and not manual:
                record["state"] = "complete"
        write(record, config)
    return {"queued": queued, "skipped": skipped, "errors": errors}


def download_missing(user_id: str | None, config=None) -> dict:
    """"Download them": queue every missing model that can be downloaded from here."""
    config = config or current_app.config["BC"]
    item = status(config)
    if item is None or item["state"] == "invalid":
        raise ValueError("There is no restored model list to review.")
    ollama_ids = [choice["id"] for choice in item["missing_choices"] if choice["kind"] == "ollama"]
    checkpoint_ids = [choice["id"] for choice in item["missing_choices"] if choice["kind"] == "huggingface"]
    ready = not checkpoint_agent.configuration_error(config) and config.images_enabled
    selected = ollama_ids + (checkpoint_ids if ready else [])
    if not selected:
        raise ValueError("Nothing can be downloaded from here: the missing checkpoints need image generation and "
                         "the checkpoint download agent, and the other items need manual action.")
    result = download(selected[:MAX_SELECTION], user_id, config)
    if checkpoint_ids and not ready:
        result["errors"].append({"model": f"{len(checkpoint_ids)} checkpoint(s)",
                                 "reason": "Configure image generation and the checkpoint download agent first."})
    return result
