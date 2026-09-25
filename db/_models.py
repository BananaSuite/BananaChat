"""AI model catalog CRUD."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


@retry_on_busy
def list_models(rolled_out_only=False):
    with get_db_context() as conn:
        if rolled_out_only:
            return conn.execute(
                "SELECT * FROM ai_models WHERE is_rolled_out=1 ORDER BY sort_order ASC, display_name ASC"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM ai_models ORDER BY sort_order ASC, display_name ASC"
        ).fetchall()


@retry_on_busy
def get_model_by_id(model_id):
    with get_db_context() as conn:
        return conn.execute("SELECT * FROM ai_models WHERE id=?", (model_id,)).fetchone()


@retry_on_busy
def get_model_by_ollama_name(name):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM ai_models WHERE ollama_name=?", (name,)
        ).fetchone()


def upsert_model(ollama_name, display_name, description=""):
    """Insert or update a model from Ollama discovery."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        existing = conn.execute(
            "SELECT id, backend FROM ai_models WHERE ollama_name=?", (ollama_name,)
        ).fetchone()
        if existing:
            if existing["backend"] != "ollama":
                raise ValueError(f"Model identifier '{ollama_name}' belongs to another backend")
            conn.execute(
                "UPDATE ai_models SET display_name=?, description=?, backend_model_name=?, "
                "backend_available=1, backend_last_seen_at=?, is_image_generation=0, "
                "updated_at=? WHERE ollama_name=?",
                (display_name, description, ollama_name, now, now, ollama_name),
            )
        else:
            conn.execute(
                "INSERT INTO ai_models "
                "(ollama_name, backend, backend_model_name, backend_available, "
                "backend_last_seen_at, display_name, description, updated_at) "
                "VALUES (?, 'ollama', ?, 1, ?, ?, ?, ?)",
                (ollama_name, ollama_name, now, display_name, description, now),
            )
        conn.commit()


def sync_ollama_models(models):
    """Atomically reconcile a successfully fetched Ollama model list."""
    clean = []
    seen = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("Ollama returned an invalid model entry")
        name = model.get("name")
        if not isinstance(name, str) or not name.strip() or "\x00" in name:
            raise ValueError("Ollama returned an invalid model name")
        name = name.strip()
        if name in seen:
            continue
        seen.add(name)
        clean.append((name, str(model.get("display_name") or name), str(model.get("description") or "")))

    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, _display_name, _description in clean:
                existing = conn.execute(
                    "SELECT backend FROM ai_models WHERE ollama_name=?", (name,)
                ).fetchone()
                if existing and existing["backend"] != "ollama":
                    raise ValueError(
                        f"Model identifier '{name}' belongs to another backend"
                    )
            conn.execute(
                "UPDATE ai_models SET backend_available=0, updated_at=? "
                "WHERE backend='ollama'",
                (now,),
            )
            for name, display_name, description in clean:
                existing = conn.execute(
                    "SELECT id FROM ai_models WHERE ollama_name=?", (name,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE ai_models SET display_name=?, description=?, "
                        "backend_model_name=?, backend_available=1, "
                        "backend_last_seen_at=?, is_image_generation=0, updated_at=? "
                        "WHERE id=?",
                        (display_name, description, name, now, now, existing["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO ai_models "
                        "(ollama_name, backend, backend_model_name, backend_available, "
                        "backend_last_seen_at, display_name, description, "
                        "is_image_generation, updated_at) "
                        "VALUES (?, 'ollama', ?, 1, ?, ?, ?, 0, ?)",
                        (name, name, now, display_name, description, now),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return conn.execute(
            "SELECT * FROM ai_models WHERE backend='ollama' "
            "ORDER BY sort_order ASC, display_name ASC"
        ).fetchall()


def sync_comfyui_models(checkpoints):
    """Atomically reconcile a successfully discovered ComfyUI checkpoint list.

    Existing display, rollout, access, and capability metadata is deliberately
    untouched. A failed or partial discovery must never call this helper.
    """
    clean = []
    seen = set()
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, str):
            raise ValueError("ComfyUI checkpoint names must be strings")
        checkpoint = checkpoint.strip()
        if not checkpoint or len(checkpoint) > 512 or "\x00" in checkpoint:
            raise ValueError("ComfyUI returned an invalid checkpoint name")
        if checkpoint not in seen:
            clean.append(checkpoint)
            seen.add(checkpoint)

    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for checkpoint in clean:
                public_id = f"comfyui:{checkpoint}"
                existing = conn.execute(
                    "SELECT id, backend FROM ai_models WHERE ollama_name=?",
                    (public_id,),
                ).fetchone()
                if existing and existing["backend"] != "comfyui":
                    raise ValueError(
                        f"Model identifier '{public_id}' belongs to another backend"
                    )

            conn.execute(
                "UPDATE ai_models SET backend_available=0, updated_at=? "
                "WHERE backend='comfyui'",
                (now,),
            )
            for checkpoint in clean:
                public_id = f"comfyui:{checkpoint}"
                existing = conn.execute(
                    "SELECT id FROM ai_models WHERE ollama_name=?", (public_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE ai_models SET backend_model_name=?, backend_available=1, "
                        "backend_last_seen_at=?, is_image_generation=1, updated_at=? WHERE id=?",
                        (checkpoint, now, now, existing["id"]),
                    )
                else:
                    filename = checkpoint.replace("\\", "/").rsplit("/", 1)[-1]
                    stem = filename.rsplit(".", 1)[0]
                    display_name = stem.replace("-", " ").replace("_", " ").strip()
                    display_name = display_name.title() or checkpoint
                    conn.execute(
                        "INSERT INTO ai_models "
                        "(ollama_name, backend, backend_model_name, backend_available, "
                        "backend_last_seen_at, display_name, description, "
                        "is_rolled_out, is_image_generation, updated_at) "
                        "VALUES (?, 'comfyui', ?, 1, ?, ?, 'ComfyUI checkpoint', 0, 1, ?)",
                        (public_id, checkpoint, now, display_name, now),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return conn.execute(
            "SELECT * FROM ai_models WHERE backend='comfyui' "
            "ORDER BY sort_order ASC, display_name ASC"
        ).fetchall()


def set_model_rollout(model_id, rolled_out):
    now = datetime.now(timezone.utc).isoformat()
    with get_db_context() as conn:
        conn.execute(
            "UPDATE ai_models SET is_rolled_out=?, updated_at=? WHERE id=?",
            (1 if rolled_out else 0, now, model_id),
        )
        conn.commit()


def update_model(model_id, **kwargs):
    allowed = {
        "display_name", "description", "sort_order", "is_rolled_out",
        "system_prompt", "temperature", "top_p", "top_k", "num_ctx", "repeat_penalty",
        "is_reasoning", "is_uncensored", "is_image_generation",
        "supports_vision",
    }
    safe = {k: v for k, v in kwargs.items() if k in allowed}
    if not safe:
        return
    with get_db_context() as conn:
        if "is_image_generation" in safe:
            row = conn.execute(
                "SELECT backend FROM ai_models WHERE id=?", (model_id,)
            ).fetchone()
            if not row:
                return
            safe["is_image_generation"] = 1 if row["backend"] == "comfyui" else 0
        safe["updated_at"] = datetime.now(timezone.utc).isoformat()
        cols = ", ".join(f"{k}=?" for k in safe)
        conn.execute(f"UPDATE ai_models SET {cols} WHERE id=?", [*safe.values(), model_id])  # noqa: S608
        conn.commit()


def build_model_options(model_row):
    """Build an Ollama options dict from stored inference parameters. Returns None if nothing is set."""
    if not model_row:
        return None
    d = dict(model_row)
    opts = {}
    for key in ("temperature", "top_p", "top_k", "num_ctx", "repeat_penalty"):
        val = d.get(key)
        if val is not None:
            opts[key] = val
    return opts or None


def get_model_system_prompt(model_row):
    """Return the model's system_prompt string, or None."""
    if not model_row:
        return None
    return dict(model_row).get("system_prompt") or None


def delete_model(model_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM ai_models WHERE id=?", (model_id,))
        conn.commit()


@retry_on_busy
def list_categories():
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM model_categories ORDER BY sort_order ASC, name ASC"
        ).fetchall()


def create_category(name, description="", scope="both", sort_order=0):
    with get_db_context() as conn:
        cur = conn.execute(
            "INSERT INTO model_categories (name, description, scope, sort_order) VALUES (?,?,?,?)",
            (name, description, scope, sort_order),
        )
        conn.commit()
        return cur.lastrowid


def update_category(cat_id, **kwargs):
    allowed = {"name", "description", "scope", "sort_order"}
    safe = {k: v for k, v in kwargs.items() if k in allowed}
    if not safe:
        return
    cols = ", ".join(f"{k}=?" for k in safe)
    with get_db_context() as conn:
        conn.execute(f"UPDATE model_categories SET {cols} WHERE id=?", [*safe.values(), cat_id])  # noqa: S608
        conn.commit()


def delete_category(cat_id):
    with get_db_context() as conn:
        conn.execute("DELETE FROM model_categories WHERE id=?", (cat_id,))
        conn.commit()


def assign_model_category(model_id, category_id):
    with get_db_context() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO model_category_assignments (model_id, category_id) VALUES (?,?)",
            (model_id, category_id),
        )
        conn.commit()


def remove_model_category(model_id, category_id):
    with get_db_context() as conn:
        conn.execute(
            "DELETE FROM model_category_assignments WHERE model_id=? AND category_id=?",
            (model_id, category_id),
        )
        conn.commit()


def get_model_categories(model_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT mc.* FROM model_categories mc "
            "JOIN model_category_assignments mca ON mc.id=mca.category_id "
            "WHERE mca.model_id=? ORDER BY mc.sort_order ASC",
            (model_id,),
        ).fetchall()


@retry_on_busy
def list_models_with_categories(rolled_out_only=False):
    """Return models with their categories as a list of dicts."""
    models = list_models(rolled_out_only=rolled_out_only)
    result = []
    for m in models:
        cats = get_model_categories(m["id"])
        d = dict(m)
        d["categories"] = [dict(c) for c in cats]
        result.append(d)
    return result


@retry_on_busy
def reorder_models(ordered_ids):
    """Set sort_order for models based on position in ordered_ids list."""
    with get_db_context() as conn:
        for idx, model_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE ai_models SET sort_order=? WHERE id=?",
                (idx, int(model_id)),
            )
        conn.commit()


@retry_on_busy
def reorder_categories(ordered_ids):
    """Set sort_order for categories based on position in ordered_ids list."""
    with get_db_context() as conn:
        for idx, cat_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE model_categories SET sort_order=? WHERE id=?",
                (idx, int(cat_id)),
            )
        conn.commit()
