"""Model download recipes for weight-free BananaChat disaster recovery."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import urllib.request

from .files import read_json, write_json

RECOVERY_FILE = ".model-recovery.json"


def weight_paths(data, environment):
    paths = {"models", "huggingface", ".cache/huggingface", "checkpoints"}
    for key in ("OLLAMA_MODELS", "BC_OLLAMA_MODEL_DIR", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "BC_CHECKPOINT_ROOT"):
        value = environment.get(key)
        if value:
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(data) and path != data:
                paths.add(path.relative_to(data).as_posix())
    return sorted(paths)


def inventory(data, environment):
    """Read only local catalog/manifests; do not contact model registries."""
    result = {"schema": 1, "ollama": [], "huggingface": [], "manual": [],
              "excluded_paths": weight_paths(data, environment)}
    names = set()
    database = Path(environment.get("BC_DATABASE_PATH", data / "bananachat.db")).absolute()
    if database.is_file() and not database.is_symlink():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "ai_models" in tables:
                models = [dict(row) for row in connection.execute("SELECT * FROM ai_models LIMIT 10001")]
                if len(models) > 10000:
                    raise ValueError("The model inventory exceeds 10,000 entries.")
                jobs = []
                if "model_pull_jobs" in tables:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(model_pull_jobs)")}
                    if {"backend", "expected_sha256", "repo_id"} <= columns:
                        jobs = [dict(row) for row in connection.execute(
                            "SELECT * FROM model_pull_jobs WHERE backend='comfyui' AND status='done' ORDER BY id DESC LIMIT 10000")]
                recipes = {}
                for job in jobs:
                    recipes.setdefault(job.get("target_name"), job)
                for model in models:
                    name = model.get("backend_model_name") or model["ollama_name"]
                    if model.get("backend", "ollama") == "ollama":
                        names.add(name)
                    elif model.get("backend") == "comfyui":
                        if name in recipes:
                            result["huggingface"].append({key: recipes[name].get(key) for key in (
                                "repo_id", "source_filename", "revision", "target_name", "expected_sha256", "expected_size")})
                        else:
                            result["manual"].append({"backend": "comfyui", "name": name,
                                                     "reason": "No retained Hugging Face download recipe; choose its source in Admin → Models."})
    manifests = Path(environment.get("OLLAMA_MODELS", data / "models")) / "manifests"
    if manifests.is_dir() and not manifests.is_symlink():
        for path in manifests.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            parts = path.relative_to(manifests).parts
            if len(parts) < 4:
                continue
            registry, *model_path, tag = parts
            model = "/".join(model_path)
            if registry == "registry.ollama.ai":
                model = model.removeprefix("library/")
            elif registry in {"hf.co", "huggingface.co"}:
                model = registry + "/" + model
            else:
                result["manual"].append({"backend": "ollama", "name": registry + "/" + model + ":" + tag,
                                         "reason": "Custom registry; review its access and source before downloading."})
                continue
            names.add(model + ":" + tag)
    if len(names) > 10000:
        raise ValueError("The model inventory exceeds 10,000 entries.")
    result["ollama"] = sorted(names)
    return result


def prepare_recovery(data, value, *, database=None):
    if (not isinstance(value, dict) or value.get("schema") != 1
            or not all(isinstance(value.get(key), list) and len(value[key]) <= 10000
                       for key in ("ollama", "huggingface", "manual"))):
        raise ValueError("Invalid model recovery inventory.")
    database = Path(database) if database is not None else data / "bananachat.db"
    if not database.is_relative_to(data):
        raise ValueError("A restored model queue must use a database inside the managed data directory.")
    write_json(data / RECOVERY_FILE, {"schema": 1, "state": "pending", "inventory": value})
    if database.is_file():
        with closing(sqlite3.connect(database)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "active_streams" in tables:
                connection.execute("UPDATE active_streams SET heartbeat_at=0")
            if "inference_queue" in tables:
                connection.execute("DELETE FROM inference_queue")
            if "worker_jobs" in tables:
                connection.execute("UPDATE worker_jobs SET status='failed',stop_requested=1,done_at=strftime('%s','now'),error_message='Restored jobs require a new request' WHERE status IN ('pending','claimed','streaming')")
            if "worker_job_chunks" in tables:
                connection.execute("DELETE FROM worker_job_chunks")
            if "model_pull_jobs" in tables:
                # Restoring a queued download must not implicitly authorize it
                # on a new machine. An admin chooses what to download again.
                connection.execute("UPDATE model_pull_jobs SET status='cancelled', error_message=?, finished_at=? "
                                   "WHERE status IN ('queued', 'pulling')",
                                   ("Restore requires administrator approval before downloading models.",
                                    datetime.now(timezone.utc).isoformat()))
            connection.commit()


def compute_recovery(data, action, *, assume_yes=False):
    path = data / RECOVERY_FILE
    record = read_json(path)
    if not record:
        return {"state": "none", "message": "No restored model inventory is waiting."}
    if action == "status":
        return record
    if action == "skip":
        record["state"] = "deferred"
        write_json(path, record)
        return {"state": "deferred", "message": "No models downloaded. Use models restore when ready."}
    if not assume_yes:
        raise ValueError("Review models status, then use models restore --yes to download these models, or models skip.")
    failed, completed = [], []
    for name in record["inventory"]["ollama"]:
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,299}", name)
                or ".." in name or ("/" in name and name.split("/", 1)[0].count(".") and not name.startswith(("hf.co/", "huggingface.co/")))):
            failed.append({"model": str(name)[:300], "reason": "Review this model's registry manually."})
            continue
        request = urllib.request.Request("http://127.0.0.1:11434/api/pull",
                                         data=json.dumps({"name": name, "stream": True}).encode(),
                                         headers={"Content-Type": "application/json"})
        success = False
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=120) as response:
                while line := response.readline(65537):
                    if len(line) > 65536:
                        raise ValueError("Invalid download response.")
                    item = json.loads(line)
                    if item.get("error"):
                        raise ValueError("The registry or Ollama rejected the download.")
                    success = item.get("status") == "success"
            if not success:
                raise ValueError("The model download ended before completion.")
            completed.append(name)
        except (OSError, ValueError):
            failed.append({"model": name, "reason": "Download failed. Check Ollama, registry access, and disk space; retry when ready."})
    record.update(state="deferred" if failed or record["inventory"]["manual"] or record["inventory"]["huggingface"] else "complete",
                  completed=completed, failed=failed)
    write_json(path, record)
    return record
