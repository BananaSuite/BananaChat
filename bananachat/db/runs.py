"""Generation runs: chat runs (one lease per session in ``active_streams``) and API answers
(``completion_leases``).

A run starts with :func:`begin` (the user's message, its attachments and the
lease are stored together), saves checkpoints of the partial answer while it
streams, and ends with :func:`finish`, which stores the answer, charges
credits, records metrics and releases the lease in one transaction.

Leases are renewed by ``services.supervisor`` (``heartbeat_at``, epoch
seconds). A lease not renewed for :data:`LEASE_SECONDS` belongs to a process
that died; :func:`recover_stale` turns its last checkpoint into a saved
answer in the ``interrupted`` state.

A lease names its account (``user_id``, ``is_incognito``) and outlives its
chat: erasing a chat while it answers only asks the run to stop
(``bananachat.db.chats``), so the run, or recovery, still charges what was
generated. Nothing is stored for an erased chat.

API and playground answers (``services.completions``) hold a lease in
``completion_leases`` from their first output on, which records what they would
be charged if they ended now (renewed every few seconds by the supervisor).
The answer itself charges and drops its lease in one transaction
(:func:`close_completion`); a lease not renewed for :data:`LEASE_SECONDS`
belongs to a process that died, and :func:`recover_completions` charges its last
record instead. Either happens once.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from bananachat import db
from bananachat.db import chats, credits, metrics
from bananachat.db.credits import estimate_tokens

LEASE_SECONDS = 60
TERMINAL_STATES = ("completed", "stopped", "failed", "interrupted")


class Busy(RuntimeError):
    """A run is already active for this chat (``scope='session'``) or this user (``scope='user'``)."""

    def __init__(self, scope: str):
        super().__init__(scope)
        self.scope = scope


@dataclass
class Begun:
    token: str
    message_id: int
    title: str | None          # the automatic title set by this run, if any
    previous_title: str


def _cutoff() -> float:
    return time.time() - LEASE_SECONDS


def user_busy(user_id: str) -> bool:
    """True when the user has a live run, also one whose chat was erased while it answers (its lease names the
    account until the run has charged what it generated)."""
    return db.one("SELECT 1 FROM active_streams a LEFT JOIN chat_sessions s ON s.id=a.session_id "
                  "WHERE COALESCE(a.user_id, s.user_id)=? AND a.heartbeat_at>=? LIMIT 1",
                  (user_id, _cutoff())) is not None


def session_busy(session_id: str) -> bool:
    return db.one("SELECT 1 FROM active_streams WHERE session_id=? AND heartbeat_at>=?",
                  (session_id, _cutoff())) is not None


def begin(session, user, *, content: str, attachments: list[dict], title: str | None,
          one_per_user: bool) -> Begun:
    """Store the user's message and take the session's lease atomically. Raises :class:`Busy`."""
    session_id = session["id"]
    with db.transaction():
        _recover(db.query("SELECT * FROM active_streams WHERE heartbeat_at<?", (_cutoff(),)))
        if db.one("SELECT 1 FROM active_streams WHERE session_id=?", (session_id,)):
            raise Busy("session")
        if one_per_user and user_busy(user["id"]):
            raise Busy("user")
        current = db.one("SELECT title, is_incognito FROM chat_sessions WHERE id=? AND deleted_at IS NULL",
                         (session_id,))
        if current is None:
            raise LookupError("The chat no longer exists.")
        message_id = chats.add_message(session_id, "user", content, user_id=user["id"],
                                       incognito=bool(current["is_incognito"]))
        chats.add_attachments(message_id, attachments)
        applied = None
        if title and chats.set_title_if_untitled(session_id, title):
            applied = title
        token = uuid.uuid4().hex
        now = time.time()
        db.execute("INSERT INTO active_streams (session_id, owner_token, stop_requested, heartbeat_at, started_at, "
                   "user_message_id, user_id, is_incognito) VALUES (?,?,0,?,?,?,?,?)",
                   (session_id, token, now, now, message_id, user["id"], int(bool(current["is_incognito"]))))
        db.execute("UPDATE chat_sessions SET generation_state='queued', generation_error='', "
                   "generation_message_id=NULL WHERE id=?", (session_id,))
    return Begun(token, message_id, applied, current["title"])


def abort(session_id: str, begun: Begun) -> None:
    """Undo :func:`begin` for a request refused before it ran (e.g. the queue is full)."""
    with db.transaction():
        if not db.execute("DELETE FROM active_streams WHERE session_id=? AND owner_token=?",
                          (session_id, begun.token)).rowcount:
            # Recovery or a newer run already owns this chat. Its state and
            # the recovered message must survive a late refusal of the old run.
            return
        # A no-history chat's message was also copied to the audit log by begin().
        db.execute("DELETE FROM incognito_audit WHERE id=(SELECT a.id FROM incognito_audit a JOIN chat_messages m "
                   "ON a.session_id=m.session_id AND a.role=m.role AND a.content=m.content AND a.created_at=m.created_at "
                   "WHERE m.id=? ORDER BY a.id DESC LIMIT 1)", (begun.message_id,))
        chats.delete_message(begun.message_id)
        if begun.title:
            db.execute("UPDATE chat_sessions SET title=? WHERE id=? AND title=?",
                       (begun.previous_title, session_id, begun.title))
        db.execute("UPDATE chat_sessions SET generation_state='idle', generation_error='' WHERE id=?", (session_id,))


def _existing_model(model_id):
    """*model_id*, or None when the model's row is gone (removed by an administrator meanwhile)."""
    if model_id is not None and not db.one("SELECT 1 FROM ai_models WHERE id=?", (model_id,)):
        return None
    return model_id


def mark_running(session_id: str, token: str, model_id) -> bool:
    with db.transaction():
        updated = db.execute("UPDATE active_streams SET model_id=? WHERE session_id=? AND owner_token=?",
                             (_existing_model(model_id), session_id, token)).rowcount
        if updated:
            db.execute("UPDATE chat_sessions SET generation_state='running' WHERE id=?", (session_id,))
    return updated == 1


def checkpoint(session_id: str, token: str, content: str, model_id, *, tokens_in: int = 0,
               tokens_out: int = 0) -> bool:
    """Save the partial answer and the usage so far (charged if the run is recovered).

    False when the lease was lost (stop generating).
    """
    with db.transaction():
        return db.execute("UPDATE active_streams SET partial_content=?, model_id=?, tokens_in=?, tokens_out=? "
                          "WHERE session_id=? AND owner_token=?",
                          (content, _existing_model(model_id), max(0, int(tokens_in)), max(0, int(tokens_out)),
                           session_id, token)).rowcount == 1


def expire(session_id: str, token: str) -> bool:
    """Give up a run's lease now (its process is exiting): recovery saves its last checkpoint at once."""
    return db.execute("UPDATE active_streams SET heartbeat_at=0 WHERE session_id=? AND owner_token=?",
                      (session_id, token)).rowcount == 1


def request_stop(session_id: str) -> bool:
    return db.execute("UPDATE active_streams SET stop_requested=1 WHERE session_id=?", (session_id,)).rowcount == 1


@dataclass
class Outcome:
    message_id: int | None
    title: str | None


def finish(session_id: str, token: str, *, user_id: str, content: str, state: str, error: str = "",
           model_id=None, tokens_in: int = 0, tokens_out: int = 0, usage_estimated: bool = False,
           duration_ms: int = 0, queue_wait_ms: int = 0, charge: bool = True) -> Outcome | None:
    """Commit a run's outcome. Returns None when the lease was lost (another process recovered it)."""
    if state not in TERMINAL_STATES:
        raise ValueError("Unknown generation state.")
    with db.transaction():
        lease = db.one("SELECT is_incognito FROM active_streams WHERE session_id=? AND owner_token=?",
                       (session_id, token))
        if lease is None:
            return None
        return _commit(session_id, user_id=user_id, content=content, state=state, error=error, model_id=model_id,
                       tokens_in=tokens_in, tokens_out=tokens_out, usage_estimated=usage_estimated,
                       duration_ms=duration_ms, queue_wait_ms=queue_wait_ms, charge=charge,
                       incognito=bool(lease["is_incognito"]))


def _commit(session_id, *, user_id, content, state, error, model_id, tokens_in, tokens_out, usage_estimated,
            duration_ms, queue_wait_ms, charge, incognito=False) -> Outcome:
    """Store the outcome and charge it. A chat erased meanwhile stores nothing; *incognito* (from the lease)
    then names the request type."""
    model_id = _existing_model(model_id)
    session = db.one("SELECT user_id, title, is_incognito, deleted_at FROM chat_sessions WHERE id=?", (session_id,))
    incognito = bool(session["is_incognito"]) if session is not None else bool(incognito)
    message_id = None
    if session is not None and session["deleted_at"] is None:
        if content:
            message_id = chats.add_message(session_id, "assistant", content, user_id=session["user_id"],
                                           incognito=incognito, model_id=model_id, tokens_in=tokens_in,
                                           tokens_out=tokens_out, generation_state=state,
                                           usage_estimated=usage_estimated)
        db.execute("UPDATE chat_sessions SET generation_state=?, generation_error=?, generation_message_id=?, "
                   "updated_at=? WHERE id=?", (state, (error or "")[:500], message_id, db.now(), session_id))
    request_type = "chat_incognito" if incognito else "chat"
    user_exists = db.one("SELECT 1 FROM users WHERE id=?", (user_id,)) is not None
    if not charge:
        tokens_in = tokens_out = 0
    if user_exists and (tokens_in or tokens_out):
        credits.charge(user_id, tokens_in, tokens_out, request_type=request_type, model_id=model_id,
                       usage_estimated=usage_estimated)
    metrics.record_request(request_type, model_id=model_id, user_id=user_id if user_exists else None,
                           tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=duration_ms,
                           queue_wait_ms=queue_wait_ms, status="ok" if state == "completed" else state,
                           usage_estimated=usage_estimated)
    db.execute("DELETE FROM active_streams WHERE session_id=?", (session_id,))
    return Outcome(message_id, session["title"] if session else None)


def status(session_id: str) -> dict:
    """Read-only view of a chat's generation (never writes, so polling stays cheap)."""
    row = db.one(
        "SELECT s.generation_state, s.generation_error, s.generation_message_id, s.title, a.owner_token, "
        "a.partial_content, a.stop_requested, a.heartbeat_at FROM chat_sessions s "
        "LEFT JOIN active_streams a ON a.session_id=s.id WHERE s.id=?", (session_id,))
    if row is None:
        return {"state": "idle", "error": "", "active": False, "stale": False, "stopping": False, "partial": "",
                "message_id": None, "title": None}
    has_lease = row["owner_token"] is not None
    stale = has_lease and row["heartbeat_at"] < _cutoff()
    return {
        "state": "interrupted" if stale else row["generation_state"],
        "error": row["generation_error"] or "",
        "active": has_lease and not stale,
        "stale": stale,
        "stopping": bool(has_lease and row["stop_requested"]),
        "partial": (row["partial_content"] or "") if has_lease else "",
        "message_id": row["generation_message_id"],
        "title": row["title"],
    }


def has_stale(session_id: str | None = None) -> bool:
    if session_id is None:
        return db.one("SELECT 1 FROM active_streams WHERE heartbeat_at<? LIMIT 1", (_cutoff(),)) is not None
    return db.one("SELECT 1 FROM active_streams WHERE session_id=? AND heartbeat_at<?",
                  (session_id, _cutoff())) is not None


def recover_stale(session_id: str | None = None) -> int:
    """Save the last checkpoint of runs whose process died as ``interrupted`` answers."""
    if not has_stale(session_id):
        return 0
    with db.transaction():
        # Repeat the test inside the transaction: a heartbeat may have just renewed the lease.
        if session_id is None:
            rows = db.query("SELECT * FROM active_streams WHERE heartbeat_at<? LIMIT 200", (_cutoff(),))
        else:
            rows = db.query("SELECT * FROM active_streams WHERE session_id=? AND heartbeat_at<?",
                            (session_id, _cutoff()))
        return _recover(rows)


def _recover(rows) -> int:
    for row in rows:
        session = db.one("SELECT user_id FROM chat_sessions WHERE id=?", (row["session_id"],))
        user_id = session["user_id"] if session is not None else row["user_id"]
        if user_id is None:  # a lease from before leases named their account, of an erased chat
            db.execute("DELETE FROM active_streams WHERE session_id=?", (row["session_id"],))
            continue
        content = row["partial_content"] or ""
        # The last checkpoint's usage (the whole prompt, provider counts), at least an estimate of what was saved.
        prompt = db.scalar("SELECT length(content) FROM chat_messages WHERE id=?", (row["user_message_id"],), 0)
        tokens_in = max(row["tokens_in"] or 0, (prompt + 3) // 4)
        tokens_out = max(row["tokens_out"] or 0, estimate_tokens(content))
        charge = bool(content) or bool(row["tokens_out"])
        _commit(row["session_id"], user_id=user_id, content=content, state="interrupted", error="",
                model_id=row["model_id"], tokens_in=tokens_in if charge else 0,
                tokens_out=tokens_out, usage_estimated=True,
                duration_ms=int(max(0.0, row["heartbeat_at"] - (row["started_at"] or row["heartbeat_at"])) * 1000),
                queue_wait_ms=0, charge=charge, incognito=bool(row["is_incognito"]))
    return len(rows)


# ----- API and playground answers ------------------------------------------------------------------------------

def open_completion(user_id: str, *, request_type: str, token_id=None, model_id=None, queue_wait_ms: int = 0) -> str:
    """Record an API answer that started; returns its lease id (known only to its process)."""
    lease_id = uuid.uuid4().hex
    now = time.time()
    db.execute("INSERT INTO completion_leases (id, user_id, token_id, request_type, model_id, queue_wait_ms, "
               "started_at, heartbeat_at) VALUES (?,?,?,?,?,?,?,?)",
               (lease_id, user_id, token_id, request_type, _existing_model(model_id), max(0, int(queue_wait_ms)),
                now, now))
    return lease_id


def checkpoint_completion(lease_id: str, *, model_id, tokens_in: int, tokens_out: int) -> bool:
    """Renew the lease with the usage to charge if the answer ended now. False when it was recovered elsewhere."""
    return db.execute("UPDATE completion_leases SET heartbeat_at=?, model_id=?, tokens_in=?, tokens_out=? WHERE id=?",
                      (time.time(), _existing_model(model_id), max(0, int(tokens_in)), max(0, int(tokens_out)),
                       lease_id)).rowcount == 1


def close_completion(lease_id: str) -> bool:
    """Drop the lease of an answer that is being charged (join the charge's transaction). False when recovery
    already charged it."""
    return db.execute("DELETE FROM completion_leases WHERE id=?", (lease_id,)).rowcount == 1


def expire_completion(lease_id: str) -> bool:
    """Give the lease up now (its process is exiting): recovery charges its last record at once."""
    return db.execute("UPDATE completion_leases SET heartbeat_at=0 WHERE id=?", (lease_id,)).rowcount == 1


def recover_completions() -> int:
    """Charge the last record of API answers whose process died, and record their metrics."""
    if db.one("SELECT 1 FROM completion_leases WHERE heartbeat_at<? LIMIT 1", (_cutoff(),)) is None:
        return 0
    with db.transaction():
        rows = db.query("SELECT * FROM completion_leases WHERE heartbeat_at<? LIMIT 200", (_cutoff(),))
        for row in rows:
            db.execute("DELETE FROM completion_leases WHERE id=?", (row["id"],))
            user_exists = db.one("SELECT 1 FROM users WHERE id=?", (row["user_id"],)) is not None
            if not user_exists:
                continue
            model_id = _existing_model(row["model_id"])
            token_id = row["token_id"]
            if token_id is not None and not db.one("SELECT 1 FROM api_tokens WHERE id=?", (token_id,)):
                token_id = None
            tokens_in, tokens_out = row["tokens_in"] or 0, row["tokens_out"] or 0
            if tokens_in or tokens_out:
                credits.charge(row["user_id"], tokens_in, tokens_out, request_type=row["request_type"],
                               model_id=model_id, token_id=token_id, usage_estimated=True)
            ended = max(row["heartbeat_at"], row["started_at"] or 0)
            metrics.record_request(row["request_type"], model_id=model_id, user_id=row["user_id"],
                                   tokens_in=tokens_in, tokens_out=tokens_out,
                                   duration_ms=int(max(0.0, ended - (row["started_at"] or ended)) * 1000),
                                   queue_wait_ms=row["queue_wait_ms"] or 0,
                                   status="stopped" if tokens_out else "error", usage_estimated=True)
    return len(rows)
