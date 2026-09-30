"""Accounting for API, playground and image requests.

Every finished request writes one ``credit_ledger`` row (through
``db.credits``) and one ``request_metrics`` row, in a single transaction, so
the usage history and the administrator metrics always agree.
"""

from __future__ import annotations

from bananachat import db
from bananachat.db import credits

METRIC_STATUSES = ("ok", "stopped", "error")


def record_metric(*, request_type: str, user_id: str | None, model_id=None, tokens_in: int = 0,
                  tokens_out: int = 0, duration_ms: int = 0, queue_wait_ms: int = 0, status: str = "ok",
                  usage_estimated: bool = False) -> None:
    """Insert one ``request_metrics`` row (joins the caller's transaction)."""
    if status not in METRIC_STATUSES:
        status = "error"
    db.execute(
        "INSERT INTO request_metrics (request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, "
        "queue_wait_ms, status, created_at, usage_estimated) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_type, model_id, user_id, max(0, int(tokens_in)), max(0, int(tokens_out)),
         max(0, int(duration_ms)), max(0, int(queue_wait_ms)), status, db.now(), int(bool(usage_estimated))))


def charge_text(*, user_id: str, request_type: str, model_id, token_id, prompt_tokens: int,
                completion_tokens: int, usage_estimated: bool, duration_ms: int, queue_wait_ms: int,
                status: str) -> float:
    """Charge a text request and record its metrics atomically. Returns the tokens counted against the pool."""
    with db.transaction():
        used, _slow = credits.charge(user_id, prompt_tokens, completion_tokens, request_type=request_type,
                                     model_id=model_id, token_id=token_id, usage_estimated=usage_estimated)
        record_metric(request_type=request_type, user_id=user_id, model_id=model_id, tokens_in=prompt_tokens,
                      tokens_out=completion_tokens, duration_ms=duration_ms, queue_wait_ms=queue_wait_ms,
                      status=status, usage_estimated=usage_estimated)
    return used


def charge_image(*, reservation_id, user_id: str, tokens_due: float, model_id, token_id, duration_ms: int,
                 queue_wait_ms: int, counted: bool = True) -> None:
    """Turn an image reservation into a charge (*tokens_due*: the image's token cost) and record the metrics
    atomically."""
    with db.transaction():
        credits.finalize_reservation(reservation_id, user_id=user_id, tokens=tokens_due, model_id=model_id,
                                     token_id=token_id, counted=counted)
        record_metric(request_type="image", user_id=user_id, model_id=model_id, tokens_in=0,
                      tokens_out=int(tokens_due), duration_ms=duration_ms, queue_wait_ms=queue_wait_ms, status="ok")
