"""Custom personality storage and administrator moderation."""

from datetime import datetime, timezone

from ._connection import get_db_context, retry_on_busy


MAX_PERSONALITIES_PER_USER = 20
MAX_PERSONALITY_NAME = 80
MAX_PERSONALITY_INSTRUCTIONS = 8000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate(name, instructions):
    name = (name or "").strip()
    instructions = (instructions or "").strip()
    if not name:
        raise ValueError("Personality name is required.")
    if len(name) > MAX_PERSONALITY_NAME:
        raise ValueError(f"Personality name must be {MAX_PERSONALITY_NAME} characters or fewer.")
    if not instructions:
        raise ValueError("Personality instructions are required.")
    if len(instructions) > MAX_PERSONALITY_INSTRUCTIONS:
        raise ValueError(
            f"Personality instructions must be {MAX_PERSONALITY_INSTRUCTIONS} characters or fewer."
        )
    return name, instructions


def create_personality(user_id, name, instructions, created_by=None, enabled=True):
    name, instructions = _validate(name, instructions)
    now = _now()
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = conn.execute(
            "SELECT COUNT(*) FROM personalities WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        if count >= MAX_PERSONALITIES_PER_USER:
            conn.rollback()
            raise ValueError(f"A user may have at most {MAX_PERSONALITIES_PER_USER} personalities.")
        try:
            cur = conn.execute(
                "INSERT INTO personalities "
                "(user_id, name, instructions, is_enabled, created_by, updated_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, name, instructions, int(bool(enabled)), created_by, created_by, now, now),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if "unique" in str(exc).lower():
                raise ValueError("A personality with that name already exists.") from exc
            raise
        return cur.lastrowid


@retry_on_busy
def get_personality(personality_id):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT p.*, u.username, creator.username AS created_by_username, "
            "editor.username AS updated_by_username "
            "FROM personalities p JOIN users u ON u.id=p.user_id "
            "LEFT JOIN users creator ON creator.id=p.created_by "
            "LEFT JOIN users editor ON editor.id=p.updated_by WHERE p.id=?",
            (personality_id,),
        ).fetchone()


@retry_on_busy
def list_user_personalities(user_id, include_disabled=True):
    clause = "" if include_disabled else " AND is_enabled=1"
    with get_db_context() as conn:
        return conn.execute(
            "SELECT * FROM personalities WHERE user_id=?" + clause +
            " ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()


@retry_on_busy
def list_all_personalities(limit=500, offset=0):
    with get_db_context() as conn:
        return conn.execute(
            "SELECT p.*, u.username, creator.username AS created_by_username "
            "FROM personalities p JOIN users u ON u.id=p.user_id "
            "LEFT JOIN users creator ON creator.id=p.created_by "
            "ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()


def update_personality(personality_id, name, instructions, updated_by, enabled=None):
    name, instructions = _validate(name, instructions)
    values = [name, instructions]
    enabled_sql = ""
    if enabled is not None:
        enabled_sql = ", is_enabled=?"
        values.append(int(bool(enabled)))
    values.extend((updated_by, _now(), personality_id))
    with get_db_context() as conn:
        try:
            cur = conn.execute(
                "UPDATE personalities SET name=?, instructions=?" + enabled_sql +
                ", updated_by=?, updated_at=? WHERE id=?",
                values,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if "unique" in str(exc).lower():
                raise ValueError("A personality with that name already exists.") from exc
            raise
        return cur.rowcount > 0


def set_personality_moderation(personality_id, disabled, updated_by,
                               disabled_until=None, reason=""):
    with get_db_context() as conn:
        cur = conn.execute(
            "UPDATE personalities SET admin_disabled=?, disabled_until=?, "
            "disabled_reason=?, updated_by=?, updated_at=? WHERE id=?",
            (
                int(bool(disabled)), disabled_until if disabled else None,
                (reason or "").strip() if disabled else "", updated_by, _now(), personality_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_personality(personality_id):
    with get_db_context() as conn:
        cur = conn.execute("DELETE FROM personalities WHERE id=?", (personality_id,))
        conn.commit()
        return cur.rowcount > 0


def personality_is_active(personality):
    if not personality or not personality.get("is_enabled"):
        return False
    if not personality.get("admin_disabled"):
        return True
    until = personality.get("disabled_until")
    if not until:
        return False
    try:
        parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False
