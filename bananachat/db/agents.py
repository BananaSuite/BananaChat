"""Agent tasks, their steps, sub-agent lanes, follow-up messages and settings (SQL only).

A task being worked on holds a lease: ``owner_token`` plus ``heartbeat_at``
(epoch seconds, renewed by ``services.supervisor``). Every write made by the
agent loop names its token, so a process that lost the lease (because another
process recovered the task after a crash) can no longer change it.
Decisions (budgets, access, what to run) live in ``services.agents``.
"""

from __future__ import annotations

import json
import secrets
import time

from bananachat import db

LEASE_SECONDS = 60
ACTIVE_STATES = ("queued", "running", "paused")
TERMINAL_STATES = ("finished", "failed", "stopped", "out_of_budget", "interrupted")
STATES = ACTIVE_STATES + TERMINAL_STATES
STEP_KINDS = ("user", "assistant", "tool", "notice", "error", "summary")
LANE_STATES = ("queued", "running", "finished", "failed", "stopped", "out_of_budget")
MAX_TEXT = 200_000


class Busy(RuntimeError):
    """Too many active tasks: ``scope`` is ``user`` or ``site``."""

    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


def _cutoff() -> float:
    return time.time() - LEASE_SECONDS


def _clip(value, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit]


_ACTIVE_SQL = "status IN ('queued', 'running', 'paused') AND owner_token IS NOT NULL AND heartbeat_at>=?"


# ----- tasks ----------------------------------------------------------------------

def get(task_id: str):
    if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
        return None
    return db.one("SELECT t.*, u.username FROM agent_tasks t LEFT JOIN users u ON u.id=t.user_id WHERE t.id=?",
                  (task_id,))


def active_count(user_id: str | None = None) -> int:
    if user_id is None:
        return db.scalar(f"SELECT COUNT(*) FROM agent_tasks WHERE {_ACTIVE_SQL}", (_cutoff(),), 0)
    return db.scalar(f"SELECT COUNT(*) FROM agent_tasks WHERE user_id=? AND {_ACTIVE_SQL}", (user_id, _cutoff()), 0)


def _check_capacity(user_id: str, *, max_user: int | None, max_site: int) -> None:
    if max_user is not None and active_count(user_id) >= max_user:
        raise Busy("user")
    if active_count() >= max_site:
        raise Busy("site")


def create(task_id: str, *, user_id: str, title: str, prompt: str, model_id, model_name: str, swarm: bool,
           owner_token: str, max_user: int | None, max_site: int) -> None:
    """Insert a task holding its lease, if the user and the site have room. Raises :class:`Busy`."""
    now = db.now()
    with db.transaction():
        _check_capacity(user_id, max_user=max_user, max_site=max_site)
        db.execute(
            "INSERT INTO agent_tasks (id, user_id, title, prompt, status, model_id, model_name, swarm, owner_token, "
            "heartbeat_at, runs, created_at, updated_at) VALUES (?,?,?,?,'queued',?,?,?,?,?,1,?,?)",
            (task_id, user_id, _clip(title, 200), _clip(prompt), model_id, model_name, 1 if swarm else 0,
             owner_token, time.time(), now, now))
        db.execute("INSERT INTO agent_steps (task_id, agent, kind, content, created_at) VALUES (?,0,'user',?,?)",
                   (task_id, _clip(prompt), now))


def resume(task_id: str, *, owner_token: str, max_user: int | None, max_site: int, model_id=None,
           model_name: str | None = None) -> bool:
    """Take the lease of a task that is not running (a follow-up). False when it is already active."""
    with db.transaction():
        row = db.one("SELECT user_id, status, owner_token, heartbeat_at FROM agent_tasks WHERE id=?", (task_id,))
        if row is None:
            raise LookupError(task_id)
        if row["owner_token"] and (row["heartbeat_at"] or 0) >= _cutoff():
            return False
        _check_capacity(row["user_id"], max_user=max_user, max_site=max_site)
        db.execute("UPDATE agent_tasks SET status='queued', owner_token=?, heartbeat_at=?, stop_requested=0, "
                   "runs=runs+1, error='', notice='', finished_at=NULL, updated_at=?, "
                   "model_id=COALESCE(?, model_id), model_name=COALESCE(?, model_name) WHERE id=?",
                   (owner_token, time.time(), db.now(), model_id, model_name, task_id))
    return True


def owns(task_id: str, token: str) -> bool:
    return db.one("SELECT 1 FROM agent_tasks WHERE id=? AND owner_token=?", (task_id, token)) is not None


def set_status(task_id: str, token: str, status: str, *, notice: str | None = None, started: bool = False) -> bool:
    if status not in ACTIVE_STATES:
        raise ValueError("Use finish() for terminal states.")
    extra = ", started_at=COALESCE(started_at, ?)" if started else ""
    params: list = [status, db.now()]
    if notice is not None:
        extra += ", notice=?"
    if started:
        params.append(db.now())
    if notice is not None:
        params.append(_clip(notice, 500))
    return db.execute(f"UPDATE agent_tasks SET status=?, updated_at=?{extra} WHERE id=? AND owner_token=?",
                      (*params, task_id, token)).rowcount == 1


def set_sandbox(task_id: str, token: str | None, sandbox_id: str | None, expires_at: str | None = None) -> bool:
    if token is None:
        return db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=?",
                          (sandbox_id, expires_at, task_id)).rowcount == 1
    return db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=? AND owner_token=?",
                      (sandbox_id, expires_at, task_id, token)).rowcount == 1


def clear_sandbox(task_id: str, sandbox_id: str) -> None:
    db.execute("UPDATE agent_tasks SET sandbox_id=NULL, sandbox_expires_at=NULL WHERE id=? AND sandbox_id=?",
               (task_id, sandbox_id))


def add_usage(task_id: str, token: str, *, steps: int = 0, tool_calls: int = 0, tokens_in: int = 0,
              tokens_out: int = 0) -> bool:
    return db.execute("UPDATE agent_tasks SET steps_used=steps_used+?, tool_calls=tool_calls+?, "
                      "tokens_in=tokens_in+?, tokens_out=tokens_out+?, updated_at=? WHERE id=? AND owner_token=?",
                      (steps, tool_calls, max(0, tokens_in), max(0, tokens_out), db.now(), task_id,
                       token)).rowcount == 1


def finish(task_id: str, token: str, status: str, *, summary: str = "", error: str = "",
           sandbox_expires_at: str | None = None) -> bool:
    """End the current run and release the lease. False when the lease was lost."""
    if status not in TERMINAL_STATES:
        raise ValueError("Unknown terminal state.")
    now = db.now()
    return db.execute(
        "UPDATE agent_tasks SET status=?, summary=CASE WHEN ?<>'' THEN ? ELSE summary END, error=?, notice='', "
        "owner_token=NULL, heartbeat_at=NULL, stop_requested=0, finished_at=?, updated_at=?, "
        "sandbox_expires_at=CASE WHEN sandbox_id IS NOT NULL THEN ? ELSE NULL END WHERE id=? AND owner_token=?",
        (status, summary, _clip(summary, 50_000), _clip(error, 1000), now, now, sandbox_expires_at, task_id,
         token)).rowcount == 1


STOP_BY_OWNER = 1
STOP_BY_ADMIN = 2


def request_stop(task_id: str, *, by_admin: bool = False) -> bool:
    """Ask the process running the task to stop it (1 = the owner, 2 = an administrator)."""
    return db.execute("UPDATE agent_tasks SET stop_requested=? WHERE id=? AND status IN ('queued', 'running', "
                      "'paused') AND owner_token IS NOT NULL",
                      (STOP_BY_ADMIN if by_admin else STOP_BY_OWNER, task_id)).rowcount == 1


def request_stop_all() -> list[str]:
    """The administrator's kill switch: every active task is asked to stop."""
    with db.transaction():
        rows = db.query("SELECT id FROM agent_tasks WHERE status IN ('queued', 'running', 'paused') "
                        "AND owner_token IS NOT NULL")
        db.execute("UPDATE agent_tasks SET stop_requested=? WHERE status IN ('queued', 'running', 'paused') "
                   "AND owner_token IS NOT NULL", (STOP_BY_ADMIN,))
    return [row["id"] for row in rows]


def lease_check(task_id: str, token: str, renew: bool) -> str | None:
    """For the supervisor: ``"lease lost"``, ``"stopped"``, ``"stopped by admin"`` or None.

    Renews the heartbeat when *renew* is true and the lease is still held.
    """
    row = db.one("SELECT owner_token, stop_requested FROM agent_tasks WHERE id=?", (task_id,))
    if row is None or row["owner_token"] != token:
        return "lease lost"
    if row["stop_requested"]:
        return "stopped by admin" if row["stop_requested"] == STOP_BY_ADMIN else "stopped"
    if renew:
        db.execute("UPDATE agent_tasks SET heartbeat_at=? WHERE id=? AND owner_token=?", (time.time(), task_id, token))
    return None


def stop_requested(task_id: str) -> bool:
    return bool(db.scalar("SELECT stop_requested FROM agent_tasks WHERE id=?", (task_id,), 0))


def is_stale(row) -> bool:
    return bool(row["owner_token"]) and (row["heartbeat_at"] or 0) < _cutoff()


def recover_stale(*, keep_until: str, error: str, status: str = "interrupted", task_id: str | None = None) -> list:
    """Mark tasks whose process died (lease not renewed) as ended. Returns their ids."""
    with db.transaction():
        clause, params = (" AND id=?", [task_id]) if task_id else ("", [])
        rows = db.query("SELECT id FROM agent_tasks WHERE owner_token IS NOT NULL AND heartbeat_at<?" + clause +
                        " LIMIT 200", (_cutoff(), *params))
        now = db.now()
        for row in rows:
            db.execute("UPDATE agent_tasks SET status=?, error=?, notice='', owner_token=NULL, heartbeat_at=NULL, "
                       "stop_requested=0, finished_at=?, updated_at=?, sandbox_expires_at=CASE WHEN sandbox_id "
                       "IS NOT NULL THEN ? ELSE NULL END WHERE id=?", (status, error, now, now, keep_until, row["id"]))
            db.execute("UPDATE agent_lanes SET status='stopped', finished_at=? WHERE task_id=? AND status IN "
                       "('queued', 'running')", (now, row["id"]))
            db.execute("INSERT INTO agent_steps (task_id, agent, kind, content, created_at) VALUES (?,0,'error',?,?)",
                       (row["id"], error, now))
    return [row["id"] for row in rows]


def list_for_user(user_id: str, limit: int = 100):
    return db.query("SELECT id, title, status, model_name, swarm, steps_used, tokens_in, tokens_out, created_at, "
                    "updated_at, finished_at, owner_token, heartbeat_at FROM agent_tasks WHERE user_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit))


def list_all(*, status: str | None = None, user_id: str | None = None, limit: int = 50, offset: int = 0):
    clauses, params = [], []
    if status:
        clauses.append("t.status=?")
        params.append(status)
    if user_id:
        clauses.append("t.user_id=?")
        params.append(user_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.query(f"SELECT t.id, t.title, t.status, t.model_name, t.swarm, t.steps_used, t.tool_calls, "
                    f"t.tokens_in, t.tokens_out, t.created_at, t.updated_at, t.finished_at, t.owner_token, "
                    f"t.heartbeat_at, t.sandbox_id, t.user_id, u.username FROM agent_tasks t "
                    f"LEFT JOIN users u ON u.id=t.user_id {where} ORDER BY t.created_at DESC, t.rowid DESC "
                    f"LIMIT ? OFFSET ?", (*params, limit, offset))


def count_all(*, status: str | None = None, user_id: str | None = None) -> int:
    clauses, params = [], []
    if status:
        clauses.append("status=?")
        params.append(status)
    if user_id:
        clauses.append("user_id=?")
        params.append(user_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return db.scalar(f"SELECT COUNT(*) FROM agent_tasks {where}", params, 0)


def delete(task_id: str) -> None:
    db.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))


# ----- steps ----------------------------------------------------------------------

def add_step(task_id: str, token: str | None, *, agent: int = 0, kind: str, content: str = "", thinking: str = "",
             tool_name: str | None = None, tool_args=None, tool_calls=None, tool_result: str | None = None,
             tool_status: str | None = None, tokens_in: int = 0, tokens_out: int = 0,
             duration_ms: int = 0) -> int | None:
    """Store a step; with *token*, only while the lease is held (None when it was lost)."""
    if kind not in STEP_KINDS:
        raise ValueError("Unknown step kind.")
    values = (task_id, int(agent), kind, _clip(content), _clip(thinking, 50_000), tool_name,
              json.dumps(tool_args, ensure_ascii=False)[:MAX_TEXT] if tool_args is not None else None,
              json.dumps(tool_calls, ensure_ascii=False)[:MAX_TEXT] if tool_calls is not None else None,
              _clip(tool_result, 64_000) if tool_result is not None else None, tool_status,
              max(0, int(tokens_in)), max(0, int(tokens_out)), max(0, int(duration_ms)), db.now())
    columns = ("task_id, agent, kind, content, thinking, tool_name, tool_args, tool_calls, tool_result, "
               "tool_status, tokens_in, tokens_out, duration_ms, created_at")
    placeholders = ",".join("?" for _ in values)
    if token is None:
        cursor = db.execute(f"INSERT INTO agent_steps ({columns}) VALUES ({placeholders})", values)
        return cursor.lastrowid
    cursor = db.execute(f"INSERT INTO agent_steps ({columns}) SELECT {placeholders} WHERE EXISTS "
                        "(SELECT 1 FROM agent_tasks WHERE id=? AND owner_token=?)", (*values, task_id, token))
    return cursor.lastrowid if cursor.rowcount == 1 else None


def steps(task_id: str, *, after: int = 0, limit: int = 500, agent: int | None = None):
    if agent is None:
        return db.query("SELECT * FROM agent_steps WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                        (task_id, after, limit))
    return db.query("SELECT * FROM agent_steps WHERE task_id=? AND agent=? AND id>? ORDER BY id LIMIT ?",
                    (task_id, agent, after, limit))


def recent_steps(task_id: str, agent: int, limit: int):
    """The last *limit* steps of one agent, oldest first (for rebuilding its conversation)."""
    rows = db.query("SELECT * FROM agent_steps WHERE task_id=? AND agent=? ORDER BY id DESC LIMIT ?",
                    (task_id, agent, limit))
    return list(reversed(rows))


def step_count(task_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM agent_steps WHERE task_id=?", (task_id,), 0)


# ----- sub-agent lanes -------------------------------------------------------------

def next_lane_index(task_id: str) -> int:
    return db.scalar("SELECT COALESCE(MAX(agent), 0) + 1 FROM agent_lanes WHERE task_id=?", (task_id,), 1)


def add_lane(task_id: str, agent: int, title: str, instructions: str) -> None:
    db.execute("INSERT INTO agent_lanes (task_id, agent, title, instructions, status, created_at) "
               "VALUES (?,?,?,?,'queued',?)", (task_id, agent, _clip(title, 200), _clip(instructions, 20_000), db.now()))


def update_lane(task_id: str, agent: int, *, status: str | None = None, steps_used: int | None = None,
                summary: str | None = None) -> None:
    if status is not None and status not in LANE_STATES:
        raise ValueError("Unknown lane state.")
    finished = status in ("finished", "failed", "stopped", "out_of_budget")
    db.execute("UPDATE agent_lanes SET status=COALESCE(?, status), steps_used=COALESCE(?, steps_used), "
               "summary=COALESCE(?, summary), finished_at=CASE WHEN ? THEN ? ELSE finished_at END "
               "WHERE task_id=? AND agent=?",
               (status, steps_used, _clip(summary, 20_000) if summary is not None else None, 1 if finished else 0,
                db.now(), task_id, agent))


def lanes(task_id: str):
    return db.query("SELECT * FROM agent_lanes WHERE task_id=? ORDER BY agent", (task_id,))


def lane_count(task_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM agent_lanes WHERE task_id=?", (task_id,), 0)


# ----- follow-up messages -------------------------------------------------------------

def add_message(task_id: str, user_id: str, content: str, *, max_pending: int | None = None) -> int:
    """Queue a follow-up, checking an optional pending bound atomically."""
    with db.transaction():
        if max_pending is not None and pending_messages(task_id) >= max_pending:
            raise Busy("messages")
        return db.execute("INSERT INTO agent_messages (task_id, user_id, content, created_at) VALUES (?,?,?,?)",
                          (task_id, user_id, _clip(content, 50_000), db.now())).lastrowid


def pending_messages(task_id: str) -> int:
    return db.scalar("SELECT COUNT(*) FROM agent_messages WHERE task_id=? AND consumed_at IS NULL", (task_id,), 0)


def take_messages(task_id: str, token: str, limit: int = 10) -> list[str]:
    """Consume pending follow-ups (oldest first) and store them as user steps, while the lease is held."""
    with db.transaction():
        if not owns(task_id, token):
            return []
        rows = db.query("SELECT id, content FROM agent_messages WHERE task_id=? AND consumed_at IS NULL "
                        "ORDER BY id LIMIT ?", (task_id, limit))
        now = db.now()
        for row in rows:
            db.execute("UPDATE agent_messages SET consumed_at=? WHERE id=?", (now, row["id"]))
            db.execute("INSERT INTO agent_steps (task_id, agent, kind, content, created_at) VALUES (?,0,'user',?,?)",
                       (task_id, row["content"], now))
    return [row["content"] for row in rows]


# ----- sandboxes and retention ------------------------------------------------------

def expired_sandboxes(now: str, limit: int = 50):
    return db.query("SELECT id, sandbox_id FROM agent_tasks WHERE sandbox_id IS NOT NULL AND owner_token IS NULL "
                    "AND sandbox_expires_at IS NOT NULL AND sandbox_expires_at<=? LIMIT ?", (now, limit))


def known_sandboxes() -> dict[str, str]:
    """Sandbox id -> task id for every task that still has one."""
    return {row["sandbox_id"]: row["id"] for row in
            db.query("SELECT id, sandbox_id FROM agent_tasks WHERE sandbox_id IS NOT NULL")}


def old_tasks(cutoff: str, limit: int = 200):
    return db.query("SELECT id, sandbox_id FROM agent_tasks WHERE owner_token IS NULL AND "
                    "COALESCE(finished_at, updated_at)<? LIMIT ?", (cutoff, limit))


# ----- workspace archive downloads (one per person, across processes) -----------------------
# A lease in runtime_state, renewed while the download runs; a process that dies lets it expire.

DOWNLOAD_LEASE_SECONDS = 120


def _download_key(user_id: str) -> str:
    return f"agents:download:{user_id}"


def claim_download(user_id: str) -> str | None:
    """Start a download for *user_id*: a token, or None while another one (any process) is running."""
    token = secrets.token_hex(16)
    key, now = _download_key(user_id), time.time()
    with db.transaction():
        row = db.one("SELECT updated_at FROM runtime_state WHERE key=?", (key,))
        if row is not None and now - float(row["updated_at"] or 0) < DOWNLOAD_LEASE_SECONDS:
            return None
        db.execute("INSERT INTO runtime_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                   "SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(token), now))
    return token


def renew_download(user_id: str, token: str) -> None:
    db.execute("UPDATE runtime_state SET updated_at=? WHERE key=? AND value=?",
               (time.time(), _download_key(user_id), json.dumps(token)))


def release_download(user_id: str, token: str) -> None:
    db.execute("DELETE FROM runtime_state WHERE key=? AND value=?", (_download_key(user_id), json.dumps(token)))


# ----- tool-calling capability of models ------------------------------------------------

def set_model_caps(model_id: int, supports_tools: bool, capabilities: list[str]) -> None:
    db.execute("INSERT INTO agent_model_caps (model_id, supports_tools, capabilities, checked_at) VALUES (?,?,?,?) "
               "ON CONFLICT(model_id) DO UPDATE SET supports_tools=excluded.supports_tools, "
               "capabilities=excluded.capabilities, checked_at=excluded.checked_at",
               (model_id, 1 if supports_tools else 0, ",".join(capabilities)[:500], db.now()))


def model_caps() -> dict[int, dict]:
    return {row["model_id"]: row.to_dict() for row in db.query("SELECT * FROM agent_model_caps")}


# ----- settings ------------------------------------------------------------------------

def load_settings() -> dict:
    row = db.one("SELECT config FROM agent_settings WHERE id=1")
    if row is None:
        return {}
    try:
        data = json.loads(row["config"] or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(config: dict, updated_by: str | None) -> None:
    db.execute("INSERT INTO agent_settings (id, config, updated_by, updated_at) VALUES (1,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET config=excluded.config, updated_by=excluded.updated_by, "
               "updated_at=excluded.updated_at", (json.dumps(config, sort_keys=True), updated_by, db.now()))


# ----- Git imports ----------------------------------------------------------------------
# An import is recorded as a notice step ({"key": "agents.notice_imported...", "params": {...}});
# services.agents.gitrepo validates the content whenever it is read.

def import_record(task_id: str) -> str | None:
    """The content of the task's latest import step, or None."""
    row = db.one("SELECT content FROM agent_steps WHERE task_id=? AND agent=0 AND kind='notice' AND "
                 "content LIKE ? ORDER BY id DESC LIMIT 1", (task_id, '{"key": "agents.notice_imported%'))
    return row["content"] if row else None


IMAGE_STATE_KEY = "agents:image-git"


def image_state() -> dict | None:
    """What was last learnt about the sandbox image: ``{"image", "git", "version", "checked_at"}``."""
    row = db.one("SELECT value FROM runtime_state WHERE key=?", (IMAGE_STATE_KEY,))
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def set_image_state(*, image: str, git: bool, version: str = "") -> None:
    value = json.dumps({"image": str(image)[:300], "git": bool(git), "version": str(version)[:100],
                        "checked_at": db.now()})
    db.execute("INSERT INTO runtime_state (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
               "SET value=excluded.value, updated_at=excluded.updated_at", (IMAGE_STATE_KEY, value, time.time()))
