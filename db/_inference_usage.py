"""Record API/playground usage and its outcome as one transaction."""

from ._connection import get_db_context
from ._credits import deduct_credits


def record_usage(user_id, model_id, token_id, request_type, tokens_in, tokens_out,
                 duration_ms, queue_wait_ms, status, usage_estimated=False):
    with get_db_context() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if model_id is not None and not conn.execute("SELECT 1 FROM ai_models WHERE id=?", (model_id,)).fetchone():
            model_id = None
        if token_id is not None and not conn.execute("SELECT 1 FROM api_tokens WHERE id=?", (token_id,)).fetchone():
            token_id = None
        if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            deduct_credits(user_id, tokens_in, tokens_out, model_id, token_id, request_type,
                           connection=conn, usage_estimated=usage_estimated)
        else:
            user_id = None
        conn.execute(
            "INSERT INTO request_metrics(request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, queue_wait_ms, status, usage_estimated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (request_type, model_id, user_id, tokens_in, tokens_out, duration_ms, queue_wait_ms, status, int(usage_estimated)),
        )
        conn.commit()
