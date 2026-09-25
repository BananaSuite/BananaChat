"""HTTP inference and the remote-worker protocol under failure and concurrency."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
import json
import sqlite3
import threading
import time

import pytest

from test_chat_runtime import runtime_app, _client  # noqa: F401
import config
import db
from db import _worker_jobs as jobs
from services import dispatcher, inference, ollama, queue as q


def _api_client(runtime_app):
    application, user, _, _, _ = runtime_app
    _, raw = db.create_token(user["id"])
    client = application.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + raw
    return client


def _body(**extra):
    return {"model": "fixture:latest", "messages": [{"role": "user", "content": "A request"}], **extra}


def test_api_rate_limit_follows_token_owner_across_tokens_and_shared_ips(runtime_app, monkeypatch):
    application, first_user, _, _, _ = runtime_app
    db.update_site_settings(api_rpm=1)
    second_user = db.create_user("second-api-owner", "unused")
    _, first_token = db.create_token(first_user["id"])
    _, same_owner_token = db.create_token(first_user["id"])
    _, second_token = db.create_token(second_user)
    monkeypatch.setattr(ollama, "generate_chat_stream", lambda *_a, **_k: iter([
        ("Reply", True, {"prompt_tokens": 1, "completion_tokens": 1})
    ]))
    client = application.test_client()
    with client.session_transaction() as session:
        session["user_id"] = second_user
    for token, status in (("invalid", 401), (first_token, 200), (same_owner_token, 429), (second_token, 200)):
        response = client.post("/v1/chat/completions", json=_body(), headers={"Authorization": "Bearer " + token})
        assert response.status_code == status, response.get_json()


@pytest.mark.parametrize("body", [None, [], {"model": "fixture:latest", "messages": [None]},
    _body(stream="yes"), _body(max_tokens=True), _body(max_tokens=-1),
    _body(messages=[{"role": "tool", "content": "unsupported"}])])
def test_api_rejects_invalid_input_before_admission(runtime_app, body):
    client = _api_client(runtime_app)
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 400
    assert q.get_stats()["total"] == 0


@pytest.mark.parametrize("body", [[], {"prompt": {}}, {"prompt": "image", "n": True}, {"prompt": "image", "size": []}])
def test_image_api_rejects_invalid_field_types(runtime_app, monkeypatch, body):
    from routes import api_v1
    monkeypatch.setattr(api_v1.comfyui, "is_enabled", lambda: True)
    response = _api_client(runtime_app).post("/v1/images/generations", json=body)
    assert response.status_code == 400
    assert q.get_stats()["total"] == 0


def test_image_request_rechecks_suspension_after_waiting(runtime_app, monkeypatch):
    from routes import api_v1
    _, user, _, model, _ = runtime_app
    monkeypatch.setattr(api_v1.comfyui, "is_enabled", lambda: True)
    image_model = dict(model, backend="comfyui", backend_model_name="fixture")
    monkeypatch.setattr(api_v1.image_generation, "resolve_model", lambda *_: image_model)
    monkeypatch.setattr(api_v1.comfyui, "generate_image", lambda *_a, **_k: pytest.fail("Suspended work reached the image backend"))
    monkeypatch.setattr(q, "MAX_CONCURRENT", 1)
    client = _api_client(runtime_app)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with q.acquire():
            future = pool.submit(client.post, "/v1/images/generations", json={"prompt": "Image", "model": "fixture"})
            deadline = time.monotonic() + 3
            while q.get_stats()["total"] < 2 and not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert q.get_stats()["total"] == 2
            db.suspend_user(user["id"])
        assert future.result(timeout=3).status_code == 403
    with db.get_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM image_credit_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0] == 0


def test_image_charge_rolls_back_if_its_usage_record_fails(runtime_app):
    _, user, _, model, _ = runtime_app
    reservation, _ = db.reserve_image_credits(user["id"], 1, model_id=model["id"])
    with db.get_db_context() as conn:
        conn.execute("CREATE TRIGGER reject_image_metric BEFORE INSERT ON request_metrics BEGIN SELECT RAISE(ABORT,'fixture metric failure'); END")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with db.get_db_context() as conn:
            conn.execute("BEGIN IMMEDIATE")
            db.finalize_image_credit_reservation(reservation, user["id"], connection=conn)
            db.record_request_metric("image", user_id=user["id"], model_id=model["id"], connection=conn)
            conn.commit()
    with db.get_db_context() as conn:
        assert conn.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM image_credit_reservations").fetchone()[0] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_api_meters_before_success_and_caps_output_tokens(runtime_app, monkeypatch, stream):
    client = _api_client(runtime_app)
    captured = []
    def backend(_name, _messages, options):
        captured.append(options)
        yield "Result", False, {}
        yield "", True, {"prompt_tokens": 12, "completion_tokens": 17, "finish_reason": "length"}
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    response = client.post("/v1/chat/completions", json=_body(stream=stream, max_tokens=19))
    assert response.status_code == 200
    data = response.get_data(as_text=True)
    assert '"length"' in data and "Result" in data
    assert captured[0]["num_predict"] == 19
    assert captured[0].get("num_ctx") != 19
    with db.get_db_context() as conn:
        ledger = conn.execute("SELECT * FROM credit_ledger").fetchall()
        metrics = conn.execute("SELECT * FROM request_metrics WHERE request_type='api'").fetchall()
        assert len(ledger) == len(metrics) == 1
        assert ledger[0]["tokens_out"] == metrics[0]["tokens_out"] == 17
        assert not ledger[0]["usage_estimated"]
    assert q.get_stats()["total"] == 0


@pytest.mark.parametrize("remote", [False, True])
def test_overload_returns_http_error_before_sse_headers(runtime_app, monkeypatch, remote):
    client = _api_client(runtime_app)
    monkeypatch.setattr(q, "MAX_QUEUE_DEPTH", 1)
    monkeypatch.setattr(dispatcher, "should_use_worker", lambda *_: remote)
    monkeypatch.setattr(ollama, "generate_chat_stream", lambda *_a, **_k: pytest.fail("Overload reached inference"))
    with q.acquire():
        response = client.post("/v1/chat/completions", json=_body(stream=True))
        assert response.status_code == 503 and response.is_json
        assert response.headers["Retry-After"] == "5"
        with db.get_db_context() as conn:
            assert conn.execute("SELECT COUNT(*) FROM worker_jobs").fetchone()[0] == 0


def test_failed_usage_commit_never_announces_api_success(runtime_app, monkeypatch):
    client = _api_client(runtime_app)
    monkeypatch.setattr(ollama, "generate_chat_stream", lambda *_a, **_k: iter([
        ("A partial response", False, {}), ("", True, {"prompt_tokens": 1, "completion_tokens": 2})]))
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("private database diagnostic")
    monkeypatch.setattr(inference, "record_usage", fail)
    response = client.post("/v1/chat/completions", json=_body(stream=True))
    text = response.get_data(as_text=True)
    assert '"server_error"' in text and "private database diagnostic" not in text
    assert '"finish_reason": "stop"' not in text
    assert q.get_stats()["total"] == 0


def test_disconnect_releases_capacity_and_records_estimated_partial_usage(runtime_app, monkeypatch):
    _, user, _, model, _ = runtime_app
    closed = threading.Event()
    def backend(*_args, **_kwargs):
        try:
            yield "Some generated text", False, {}
            pytest.fail("A closed request must not keep consuming inference")
        finally:
            closed.set()
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    work = inference.TextInference(user, model["id"], "fixture:latest", _body()["messages"], {}, q.PRIORITY_API)
    with closing(work.stream()) as stream:
        assert next(stream)[0] == "Some generated text"
    assert closed.is_set() and q.get_stats()["total"] == 0
    with db.get_db_context() as conn:
        metric = conn.execute("SELECT * FROM request_metrics").fetchone()
        ledger = conn.execute("SELECT * FROM credit_ledger").fetchone()
        assert metric["status"] == "interrupted" and metric["usage_estimated"]
        assert ledger["usage_estimated"] and ledger["tokens_out"] > 0


def test_one_users_waiter_does_not_block_another_users_request(runtime_app, monkeypatch):
    monkeypatch.setattr(q, "MAX_CONCURRENT", 2)
    with q.acquire(owner_key="one") as running:
        waiting = q.acquire(owner_key="one", timeout=2)
        try:
            assert not q._try_acquire(waiting._entry.req_id)
            with q.acquire(owner_key="two", timeout=2):
                assert q.get_stats()["running"] == 2
            assert q.get_stats()["running"] == 1
            running.close()
            with waiting:
                assert q.get_stats()["running"] == 1
        finally:
            waiting.close()


def _worker_job():
    worker, raw = db.create_worker("Fixture worker")
    job = db.create_worker_job("fixture:latest", _body()["messages"])
    assert db.claim_next_worker_job(worker)["id"] == job
    return worker, raw, job


def test_worker_terminal_chunk_waits_for_usage_commit(runtime_app):
    worker, _, job = _worker_job()
    assert db.add_worker_chunk(job, 0, "Result", False, worker_id=worker)
    assert db.add_worker_chunk(job, 1, "", True, worker_id=worker)
    with closing(dispatcher.stream_from_worker(job)) as stream:
        assert next(stream) == ("Result", False, {})
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(next, stream)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.05)
            assert db.finish_worker_job(job, 33, 44, worker_id=worker)
            assert pending.result(timeout=2) == ("", True, {"prompt_tokens": 33, "completion_tokens": 44, "finish_reason": "stop"})
    assert db.get_worker_chunks_after(job, -1) == []
    assert db.finish_worker_job(job, 33, 44, worker_id=worker)


def test_worker_chunk_retries_are_idempotent_and_order_is_enforced(runtime_app):
    worker, _, job = _worker_job()
    assert db.add_worker_chunk(job, 0, "é", False, worker_id=worker)
    assert db.add_worker_chunk(job, 0, "é", False, worker_id=worker)
    assert db.get_worker_job(job)["stream_bytes"] == 2
    assert len(db.get_worker_chunks_after(job, -1)) == 1
    assert not db.add_worker_chunk(job, 2, "missing sequence", False, worker_id=worker)
    assert db.get_worker_job(job)["status"] == "failed"
    assert not db.finish_worker_job(job, 33, 44, worker_id=worker)


def test_worker_byte_limit_stops_output_without_replacing_prior_chunks(runtime_app, monkeypatch):
    worker, _, job = _worker_job()
    monkeypatch.setattr(config, "CHAT_MAX_RESPONSE_BYTES", 3)
    assert db.add_worker_chunk(job, 0, "é", False, worker_id=worker)
    assert not db.add_worker_chunk(job, 1, "é", False, worker_id=worker)
    assert db.get_worker_job(job)["stream_bytes"] == 2
    assert len(db.get_worker_chunks_after(job, -1)) == 1


def test_worker_api_fences_disabled_owners_and_late_completion(runtime_app):
    application, *_ = runtime_app
    worker, raw, job = _worker_job()
    client = application.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer " + raw
    assert client.post(f"/worker/v1/jobs/{job}/chunk", json={"seq": 0, "content": "before", "done": True}).json["ok"]
    db.set_worker_disabled(worker, True)
    # A concurrent heartbeat cannot re-enable a worker the administrator disabled.
    db.update_worker_heartbeat(worker, "online")
    assert db.get_worker_by_id(worker)["status"] == "disabled"
    result = client.post(f"/worker/v1/jobs/{job}/complete", json={"tokens_in": 1, "tokens_out": 2})
    assert result.json["stop"] and not result.json["ok"]
    assert db.get_worker_job(job)["status"] == "failed"


def test_cancelled_pending_job_is_never_claimed_and_incomplete_worker_cannot_succeed(runtime_app):
    worker, _ = db.create_worker("Fixture")
    cancelled = db.create_worker_job("fixture:latest", [])
    db.request_stop_worker_job(cancelled)
    assert db.claim_next_worker_job(worker) is None
    job = db.create_worker_job("fixture:latest", [])
    assert db.claim_next_worker_job(worker)["id"] == job
    assert not db.finish_worker_job(job, worker_id=worker)
    assert db.get_worker_job(job)["status"] == "failed"


def test_abandoned_relay_stops_worker_and_purges_buffer(runtime_app):
    worker, _, job = _worker_job()
    db.add_worker_chunk(job, 0, "partial", False, worker_id=worker)
    with closing(dispatcher.stream_from_worker(job)) as stream:
        assert next(stream)[0] == "partial"
    assert db.get_worker_job(job)["status"] == "failed"
    assert db.get_worker_chunks_after(job, -1) == []
    assert not db.add_worker_chunk(job, 1, "late", True, worker_id=worker)


def test_inference_http_limit_leaves_status_and_stop_accessible(runtime_app, monkeypatch):
    from services import http_capacity
    from test_chat_runtime import _send
    application, user, session, _, tasks = runtime_app
    monkeypatch.setattr(http_capacity, "_limit", 1)
    entered, release = threading.Event(), threading.Event()
    def backend(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        yield "Result", True, {"prompt_tokens": 1, "completion_tokens": 1}
    monkeypatch.setattr(ollama, "generate_chat_stream", backend)
    response = _send(_client(application, user), session)
    try:
        assert entered.wait(5)
        rejected = _send(_client(application, user), session)
        assert rejected.status_code == 503 and rejected.headers["Retry-After"] == "5"
        assert _client(application, user).post(f"/chat/{session}/stop").status_code == 200
        assert application.test_client().get("/health").status_code == 200
    finally:
        response.close()
        release.set()
        for task in tasks:
            task.thread.join(timeout=5)
    assert http_capacity._active == 0
