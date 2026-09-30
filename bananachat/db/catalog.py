"""The model catalog (``ai_models``) and model categories.

``ai_models.ollama_name`` is the public model identifier for every backend:
the Ollama tag for text models, ``comfyui:<checkpoint>`` for image models.
"""

from __future__ import annotations

import time

from bananachat import db

CATEGORY_SCOPES = ("chat", "api", "both")
EDITABLE_FIELDS = frozenset({
    "display_name", "description", "system_prompt", "temperature", "top_p", "top_k", "num_ctx",
    "repeat_penalty", "is_reasoning", "supports_vision", "is_uncensored", "sort_order",
})
OPTION_FIELDS = ("temperature", "top_p", "top_k", "num_ctx", "repeat_penalty")


def get(model_id):
    return db.one("SELECT * FROM ai_models WHERE id=?", (model_id,))


def get_by_name(name: str):
    return db.one("SELECT * FROM ai_models WHERE ollama_name=?", (name,)) if name else None


def list_models(*, rolled_out_only: bool = False, backend: str | None = None):
    clauses, params = [], []
    if rolled_out_only:
        clauses.append("is_rolled_out=1")
    if backend:
        clauses.append("backend=?")
        params.append(backend)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(f"SELECT * FROM ai_models {where} ORDER BY sort_order, display_name COLLATE NOCASE", params)


def categories_by_model() -> dict[int, list]:
    """Map model id -> its categories, in one query."""
    result: dict[int, list] = {}
    for row in db.query("SELECT a.model_id, c.* FROM model_category_assignments a "
                        "JOIN model_categories c ON c.id=a.category_id ORDER BY c.sort_order, c.name COLLATE NOCASE"):
        result.setdefault(row["model_id"], []).append(row)
    return result


def model_categories(model_id: int):
    return db.query("SELECT c.* FROM model_categories c JOIN model_category_assignments a ON a.category_id=c.id "
                    "WHERE a.model_id=? ORDER BY c.sort_order, c.name COLLATE NOCASE", (model_id,))


def options(model) -> dict:
    """Administrator-defined sampling options for a model."""
    return {key: model[key] for key in OPTION_FIELDS if model[key] is not None}


def _display_name_from_tag(name: str) -> str:
    stem = name.split("/")[-1].split(":")[0]
    return stem.replace("-", " ").replace("_", " ").strip().title() or name


def _clip(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _whole(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _listing(models: list[dict]) -> list[dict]:
    seen, clean = set(), []
    for model in models:
        name = str(model.get("name") or "").strip()
        if not name or "\x00" in name or len(name) > 300 or name in seen:
            continue
        seen.add(name)
        clean.append({"name": name, "description": str(model.get("description") or "")[:500],
                      "digest": _clip(model.get("digest"), 128) or None, "size": _whole(model.get("size")),
                      "family": _clip(model.get("family"), 80), "parameter_size": _clip(model.get("parameter_size"), 40),
                      "quantization": _clip(model.get("quantization"), 40)})
    return clean


# Two syncs closer together than this (the background job and "Sync now" at
# the same moment, in two processes) count as one when models are absent.
ABSENT_COUNT_SPACING = 20


def sync_ollama(models: list[dict], *, missing_syncs: int = 3, missing_minutes: float = 30.0,
                is_ignored=None) -> dict:
    """Reconcile the Ollama model list: mark availability, add new models, count absences.

    Each item has ``name`` and optionally ``description``, ``digest``,
    ``size``, ``family``, ``parameter_size`` and ``quantization`` (from
    ``/api/tags``). The listing is taken as complete: callers skip this when
    the server could not be listed. Administrator-edited metadata (name,
    description, rollout, parameters) is never overwritten; new models get a
    generated display name, are not rolled out and wait for review
    (``enrollment='new'``), or are ``ignored`` when *is_ignored(name)* says so.

    A model absent from *missing_syncs* consecutive listings, or for
    *missing_minutes*, gets ``missing_at``; one that is listed again loses it
    and keeps everything else. Returns the changes: ``inserted``,
    ``returned``, ``missing`` and ``changed`` (new digest), as lists of
    ``(id, name)`` (``missing`` adds the reason).
    """
    clean = _listing(models)
    now = db.now()
    moment = db.parse_timestamp(now)
    summary: dict = {"inserted": [], "returned": [], "missing": [], "changed": []}
    with db.transaction():
        rows = {row["ollama_name"]: row for row in db.query(
            "SELECT id, ollama_name, backend, backend_available, backend_digest, absent_syncs, absent_since, "
            "absent_counted_at, missing_at FROM ai_models WHERE backend='ollama'")}
        for item in clean:
            name, row = item["name"], rows.get(item["name"])
            if row is None:
                if db.one("SELECT 1 FROM ai_models WHERE ollama_name=?", (name,)):
                    continue  # the name belongs to another backend
                enrollment = "ignored" if is_ignored is not None and is_ignored(name) else "new"
                cursor = db.execute(
                    "INSERT INTO ai_models (ollama_name, backend, provider, backend_model_name, backend_available, "
                    "backend_last_seen_at, display_name, description, is_rolled_out, is_image_generation, "
                    "sort_order, created_at, updated_at, enrollment, backend_digest, size_bytes, family, "
                    "parameter_size, quantization, state_reason, state_changed_at) VALUES (?, 'ollama', 'ollama', "
                    "?, 1, ?, ?, ?, 0, 0, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM ai_models), ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?)",
                    (name, name, now, _display_name_from_tag(name), item["description"], now, now, enrollment,
                     item["digest"], item["size"], item["family"], item["parameter_size"], item["quantization"],
                     "Matches the ignore list." if enrollment == "ignored" else "Detected on the model server.",
                     now))
                summary["inserted"].append((cursor.lastrowid, name))
                continue
            db.execute(
                "UPDATE ai_models SET backend_model_name=?, backend_available=1, backend_last_seen_at=?, "
                "is_image_generation=0, absent_syncs=0, absent_since=NULL, absent_counted_at=NULL, "
                "backend_digest=COALESCE(?, backend_digest), size_bytes=COALESCE(?, size_bytes), "
                "family=CASE WHEN ?<>'' THEN ? ELSE family END, "
                "parameter_size=CASE WHEN ?<>'' THEN ? ELSE parameter_size END, "
                "quantization=CASE WHEN ?<>'' THEN ? ELSE quantization END WHERE id=?",
                (name, now, item["digest"], item["size"], item["family"], item["family"], item["parameter_size"],
                 item["parameter_size"], item["quantization"], item["quantization"], row["id"]))
            db.execute("UPDATE ai_models SET description=? WHERE id=? AND description=''",
                       (item["description"], row["id"]))
            if row["missing_at"]:
                db.execute("UPDATE ai_models SET missing_at=NULL, missing_reason=NULL, state_reason=?, "
                           "state_changed_at=? WHERE id=?", ("Listed by the model server again.", now, row["id"]))
                summary["returned"].append((row["id"], name))
            if item["digest"] and row["backend_digest"] and item["digest"] != row["backend_digest"]:
                summary["changed"].append((row["id"], name))
        listed = {item["name"] for item in clean}
        for name, row in rows.items():
            if name in listed:
                continue
            if row["absent_since"] is None:
                count, since, counted = 1, now, now
            else:
                count, since, counted = row["absent_syncs"], row["absent_since"], row["absent_counted_at"]
                last = db.parse_timestamp(counted)
                if last is None or (moment - last).total_seconds() >= ABSENT_COUNT_SPACING:
                    count, counted = count + 1, now
            db.execute("UPDATE ai_models SET backend_available=0, absent_syncs=?, absent_since=?, absent_counted_at=? "
                       "WHERE id=?", (count, since, counted, row["id"]))
            if row["missing_at"]:
                continue
            started = db.parse_timestamp(since)
            minutes = (moment - started).total_seconds() / 60 if started else 0
            if count >= max(1, missing_syncs) or (missing_minutes > 0 and minutes >= missing_minutes):
                reason = (f"Not listed by the model server in {count} consecutive sync{'s' if count != 1 else ''} "
                          f"since {since} UTC.")
                db.execute("UPDATE ai_models SET missing_at=?, missing_reason=?, state_reason=?, state_changed_at=? "
                           "WHERE id=?", (now, reason, reason, now, row["id"]))
                summary["missing"].append((row["id"], name, reason))
    return summary


def sync_comfyui(checkpoints: list[str]) -> None:
    """Reconcile discovered ComfyUI checkpoints (same rules as :func:`sync_ollama`)."""
    seen, clean = set(), []
    for checkpoint in checkpoints:
        checkpoint = str(checkpoint or "").strip()
        if checkpoint and len(checkpoint) <= 512 and "\x00" not in checkpoint and checkpoint not in seen:
            seen.add(checkpoint)
            clean.append(checkpoint)
    now = db.now()
    with db.transaction():
        db.execute("UPDATE ai_models SET backend_available=0 WHERE backend='comfyui'")
        for checkpoint in clean:
            public_id = f"comfyui:{checkpoint}"
            existing = db.one("SELECT id, backend FROM ai_models WHERE ollama_name=?", (public_id,))
            if existing is not None:
                if existing["backend"] == "comfyui":
                    db.execute("UPDATE ai_models SET backend_model_name=?, backend_available=1, "
                               "backend_last_seen_at=?, is_image_generation=1 WHERE id=?",
                               (checkpoint, now, existing["id"]))
                continue
            stem = checkpoint.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            display = stem.replace("-", " ").replace("_", " ").strip().title() or checkpoint
            db.execute(
                "INSERT INTO ai_models (ollama_name, backend, provider, backend_model_name, backend_available, "
                "backend_last_seen_at, display_name, description, is_rolled_out, is_image_generation, "
                "sort_order, created_at, updated_at) VALUES (?, 'comfyui', 'comfyui', ?, 1, ?, ?, 'ComfyUI checkpoint', "
                "0, 1, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM ai_models), ?, ?)",
                (public_id, checkpoint, now, display, now, now))


def set_rollout(model_id: int, rolled_out: bool) -> None:
    """Publish or hide a model. Publishing a model that waits for review enrolls it (an ignored model is restored
    first, see ``services.model_lifecycle.restore_model``)."""
    db.execute("UPDATE ai_models SET is_rolled_out=?, updated_at=?, enrollment=CASE WHEN ?=1 AND enrollment='new' "
               "THEN 'reviewed' ELSE enrollment END, enrolled_at=CASE WHEN ?=1 AND enrollment='new' "
               "THEN ? ELSE enrolled_at END WHERE id=?",
               (1 if rolled_out else 0, db.now(), 1 if rolled_out else 0, 1 if rolled_out else 0, db.now(), model_id))


def update(model_id: int, **fields) -> None:
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown model fields: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name}=?" for name in fields)
    db.execute(f"UPDATE ai_models SET {assignments}, updated_at=? WHERE id=?",
               (*fields.values(), db.now(), model_id))


def delete(model_id: int) -> None:
    db.execute("DELETE FROM ai_models WHERE id=?", (model_id,))


# ----- lifecycle ------------------------------------------------------------------------
# Decisions live in ``services.model_lifecycle``; these only read and write the columns.

ENROLLMENTS = ("new", "auto", "reviewed", "ignored")
LIFECYCLE_FIELDS = frozenset({
    "capabilities", "reasoning_levels", "reasoning_levels_locked", "embedding_only", "context_length", "family",
    "parameter_size", "quantization", "details_digest", "details_at", "details_error", "limit_preset",
    "enrollment", "enrolled_at", "missing_at", "missing_reason", "backend_available", "recent_failures",
    "first_failure_at", "last_failure", "failing_at", "recheck_at", "recheck_attempts", "deprecated_at",
    "deprecation_note", "replacement_id", "retire_at", "retired_at", "delete_requested_at", "state_reason",
    "is_rolled_out", "is_reasoning", "supports_vision", "display_name", "description", "is_uncensored",
})


def set_lifecycle(model_id: int, **fields) -> None:
    """Write lifecycle columns; a ``state_reason`` also stamps ``state_changed_at``."""
    unknown = set(fields) - LIFECYCLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown model fields: {', '.join(sorted(unknown))}")
    if not fields:
        return
    if "enrollment" in fields and fields["enrollment"] not in ENROLLMENTS:
        raise ValueError("Unknown enrollment state.")
    now = db.now()
    if "state_reason" in fields:
        fields["state_changed_at"] = now
    assignments = ", ".join(f"{name}=?" for name in fields)
    db.execute(f"UPDATE ai_models SET {assignments}, updated_at=? WHERE id=?", (*fields.values(), now, model_id))


def needing_details(*, retry_before: str, stale_before: str, limit: int):
    """Available Ollama models whose details were never read, are for another digest, or failed a while ago."""
    return db.query(
        "SELECT * FROM ai_models WHERE backend='ollama' AND backend_available=1 AND ("
        "details_at IS NULL OR (backend_digest IS NOT NULL AND (details_digest IS NULL OR details_digest<>backend_digest)"
        " AND (details_error IS NULL OR details_at<?)) OR (details_error IS NOT NULL AND details_at<?) "
        "OR (backend_digest IS NULL AND details_at<?)) ORDER BY details_at IS NOT NULL, id LIMIT ?",
        (retry_before, retry_before, stale_before, limit))


def with_enrollment(*enrollments: str):
    marks = ",".join("?" for _ in enrollments)
    return db.query(f"SELECT * FROM ai_models WHERE enrollment IN ({marks}) "
                    "ORDER BY sort_order, display_name COLLATE NOCASE", enrollments)


def needing_attention():
    """Models an administrator should look at: enabled automatically, missing, failing, deprecated, being deleted."""
    return db.query("SELECT * FROM ai_models WHERE enrollment='auto' OR missing_at IS NOT NULL OR failing_at IS NOT NULL "
                    "OR deprecated_at IS NOT NULL OR delete_requested_at IS NOT NULL "
                    "ORDER BY sort_order, display_name COLLATE NOCASE")


def record_failure(model_id: int, error: str, *, window_start: str, now: str):
    """Count one failure (restarting the count when the last run began before *window_start*); returns the row."""
    db.execute("UPDATE ai_models SET recent_failures=CASE WHEN first_failure_at IS NULL OR first_failure_at<? "
               "THEN 1 ELSE recent_failures+1 END, first_failure_at=CASE WHEN first_failure_at IS NULL OR "
               "first_failure_at<? THEN ? ELSE first_failure_at END, last_failure=? WHERE id=?",
               (window_start, window_start, now, str(error)[:500], model_id))
    return get(model_id)


def reset_failures(model_id: int) -> bool:
    """A success: forget the failures (only written when there were some)."""
    if not db.scalar("SELECT recent_failures FROM ai_models WHERE id=?", (model_id,), 0):
        return False
    return db.execute("UPDATE ai_models SET recent_failures=0, first_failure_at=NULL WHERE id=? AND recent_failures>0",
                      (model_id,)).rowcount == 1


def failing_due(now: str, limit: int = 3):
    return db.query("SELECT * FROM ai_models WHERE failing_at IS NOT NULL AND (recheck_at IS NULL OR recheck_at<=?) "
                    "ORDER BY recheck_at LIMIT ?", (now, limit))


def retirement_due(now: str):
    return db.query("SELECT * FROM ai_models WHERE deprecated_at IS NOT NULL AND retired_at IS NULL "
                    "AND retire_at IS NOT NULL AND retire_at<=?", (now,))


def missing_before(cutoff: str):
    return db.query("SELECT * FROM ai_models WHERE missing_at IS NOT NULL AND missing_at<? AND backend_available=0",
                    (cutoff,))


def pending_deletes():
    return db.query("SELECT * FROM ai_models WHERE delete_requested_at IS NOT NULL ORDER BY delete_requested_at")


def replaced_by(model_id: int):
    return db.query("SELECT * FROM ai_models WHERE replacement_id=?", (model_id,))


def in_use(name: str, lease_seconds: float) -> int:
    """Requests waiting for or running on *name* in any process (``inference_queue`` leases)."""
    return db.scalar("SELECT COUNT(*) FROM inference_queue WHERE model_name=? AND heartbeat_at>=?",
                     (name, time.time() - lease_seconds), 0)


def retarget_personalities(old_name: str, new_name: str) -> int:
    """Point personalities that prefer *old_name* at *new_name* ('' lets them use the default model)."""
    return db.execute("UPDATE personalities SET preferred_model=? WHERE preferred_model=?",
                      (new_name, old_name)).rowcount


def reorder(model_ids: list[int]) -> None:
    with db.transaction():
        for position, model_id in enumerate(model_ids):
            db.execute("UPDATE ai_models SET sort_order=? WHERE id=?", (position, model_id))


# ----- categories -----------------------------------------------------------

def list_categories():
    return db.query("SELECT c.*, (SELECT COUNT(*) FROM model_category_assignments a WHERE a.category_id=c.id) "
                    "AS model_count FROM model_categories c ORDER BY c.sort_order, c.name COLLATE NOCASE")


def get_category(category_id: int):
    return db.one("SELECT * FROM model_categories WHERE id=?", (category_id,))


def _check_category(name: str, scope: str):
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Category names need 1-80 characters.")
    if scope not in CATEGORY_SCOPES:
        raise ValueError("Unknown category scope.")
    return name


def create_category(name: str, description: str = "", scope: str = "both") -> int:
    name = _check_category(name, scope)
    cursor = db.execute(
        "INSERT INTO model_categories (name, description, scope, sort_order, created_at) VALUES "
        "(?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM model_categories), ?)",
        (name, (description or "")[:500], scope, db.now()))
    return cursor.lastrowid


def update_category(category_id: int, name: str, description: str, scope: str) -> None:
    name = _check_category(name, scope)
    db.execute("UPDATE model_categories SET name=?, description=?, scope=? WHERE id=?",
               (name, (description or "")[:500], scope, category_id))


def delete_category(category_id: int) -> None:
    db.execute("DELETE FROM model_categories WHERE id=?", (category_id,))


def reorder_categories(category_ids: list[int]) -> None:
    with db.transaction():
        for position, category_id in enumerate(category_ids):
            db.execute("UPDATE model_categories SET sort_order=? WHERE id=?", (position, category_id))


def set_model_categories(model_id: int, category_ids: list[int]) -> None:
    with db.transaction():
        db.execute("DELETE FROM model_category_assignments WHERE model_id=?", (model_id,))
        for category_id in category_ids:
            if get_category(category_id):
                db.execute("INSERT OR IGNORE INTO model_category_assignments (model_id, category_id) VALUES (?, ?)",
                           (model_id, category_id))
