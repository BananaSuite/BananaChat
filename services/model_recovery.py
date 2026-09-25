"""Administrator-controlled model downloads after restoring application data."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from filelock import FileLock

import config
import db
from services import checkpoint_agent, comfyui, ollama

FILENAME = ".model-recovery.json"


def path():
    return Path(config.INSTANCE_DIR) / FILENAME


def read():
    target = path()
    if not target.exists():
        return None
    if target.is_symlink() or not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Invalid model recovery file.")
    value = json.loads(target.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid model recovery file.")
    inventory = value.get("inventory", {})
    if (value.get("schema") != 1 or not isinstance(inventory, dict)
            or not all(isinstance(inventory.get(key), list) and len(inventory[key]) <= 10000
                       for key in ("ollama", "huggingface", "manual"))):
        raise ValueError("Invalid model recovery inventory.")
    return value


def write(value):
    target = path()
    if target.is_symlink():
        raise ValueError("The model recovery file cannot be a symbolic link.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".model-recovery-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def cancel_restored_pulls(connection):
    """Call on the staged database before it can become visible to a worker."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(model_pull_jobs)")}
    if {"status", "error_message", "finished_at"} <= columns:
        connection.execute("UPDATE model_pull_jobs SET status='cancelled', error_message=?, finished_at=? "
                           "WHERE status IN ('queued', 'pulling')",
                           ("Restore requires administrator approval before downloading models.",
                            datetime.now(timezone.utc).isoformat()))
        connection.commit()


def stage_from_database():
    from banana_ops.models import inventory
    value = inventory(Path(config.INSTANCE_DIR), {"BC_DATABASE_PATH": str(Path(config.DATABASE_PATH).absolute())})
    write({"schema": 1, "state": "pending", "inventory": value})


def choices(record):
    result = []
    for number, name in enumerate(record["inventory"]["ollama"]):
        if isinstance(name, str):
            result.append({"id": f"ollama:{number}", "label": name, "source": "Ollama / Hugging Face GGUF"})
    for number, recipe in enumerate(record["inventory"]["huggingface"]):
        if isinstance(recipe, dict):
            result.append({"id": f"hf:{number}", "label": recipe.get("target_name", "Unknown checkpoint"),
                           "source": "Hugging Face: " + str(recipe.get("repo_id", "")) + " @ " + str(recipe.get("revision", ""))})
    return result


def decide(action, selected, user_id):
    with FileLock(str(path()) + ".lock", timeout=10):
        record = read()
        if not record:
            raise ValueError("There is no restored model inventory to review.")
        if action == "defer":
            for item in record.get("jobs", []):
                db.cancel_pull_job(item["id"])
            record["state"] = "deferred"
            write(record)
            return {"queued": [], "errors": [], "skipped": [], "deferred": True}
        available_choices = {item["id"] for item in choices(record)}
        if action != "download" or not selected or not set(selected) <= available_choices or len(selected) > 256:
            raise ValueError("Select up to 256 models to download, or choose 'Not now'.")
        selected = sorted(set(selected))
        names, checkpoints = set(), set()
        if any(item.startswith("ollama:") for item in selected):
            names = {item.get("name", "") for item in ollama.list_available_models()}
        if any(item.startswith("hf:") for item in selected):
            if checkpoint_agent.configuration_error():
                raise ValueError("Configure the checkpoint download agent before restoring Hugging Face checkpoints.")
            checkpoints = set(comfyui.discover_checkpoints())
        queued, skipped, errors = [], [], []
        for item in selected:
            kind, index = item.split(":")
            entry = record["inventory"]["ollama" if kind == "ollama" else "huggingface"][int(index)]
            label = entry if kind == "ollama" else entry.get("target_name", "checkpoint")
            try:
                if kind == "ollama":
                    if entry in names or (":" not in entry and entry + ":latest" in names):
                        skipped.append(label)
                        continue
                    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,299}", entry) or ".." in entry
                            or ("/" in entry and "." in entry.split("/", 1)[0] and not entry.startswith(("hf.co/", "huggingface.co/")))):
                        raise ValueError("Review this custom registry or model name manually.")
                    job = db.enqueue_pull_job(entry, user_id)
                else:
                    if label in checkpoints:
                        skipped.append(label)
                        continue
                    job = db.enqueue_pull_job(label, user_id, backend="comfyui", **{key: entry.get(key) for key in (
                        "repo_id", "source_filename", "revision", "target_name", "expected_sha256", "expected_size")})
                queued.append({"id": job, "model": label})
            except ValueError as error:
                errors.append({"model": label, "reason": str(error)})
        jobs = {item["id"]: item for item in [*record.get("jobs", []), *queued]}
        record.update(state="requested" if not errors else "deferred", jobs=list(jobs.values()), errors=errors)
        write(record)
        return {"queued": queued, "skipped": skipped, "errors": errors, "deferred": False}
