"""Production regressions for request validation and inference admission."""

from __future__ import annotations

import pytest

from tests.app.test_api import assert_error, chat, roll_out, token_for


@pytest.fixture
def account(app, make_user):
    roll_out(app)
    return make_user("streaming")


@pytest.fixture
def token(app, account):
    return token_for(app, account)


@pytest.mark.parametrize("effort", [[], {}, 1, True])
def test_invalid_reasoning_effort_is_a_client_error(app, token, effort):
    response = chat(app, token, reasoning_effort=effort)
    assert_error(response, 400, "invalid_request_error", "invalid_value")
    assert response.json["error"]["param"] == "reasoning_effort"


def test_json_nested_beyond_the_parser_limit_is_a_client_error(app, token):
    # CPython 3.12's C decoder can exceed the Python recursion limit.
    depth = 100_000
    response = app.test_client().post("/v1/chat/completions",
        data='{"messages":' + "[" * depth + "0" + "]" * depth + "}",
        content_type="application/json", headers={"Authorization": f"Bearer {token}"})
    assert_error(response, 400, "invalid_request_error")
    assert response.json["error"]["code"] == "invalid_json", response.json


def test_json_numeric_overflow_is_rejected_even_in_an_ignored_api_field(app, token):
    response = app.test_client().post("/v1/chat/completions",
        data='{"messages":[{"role":"user","content":"Hi"}],"unused":1e999}',
        content_type="application/json", headers={"Authorization": f"Bearer {token}"})
    assert_error(response, 400, "invalid_request_error", "invalid_json")


@pytest.mark.parametrize("change", ["policy", "rollout", "effort"])
def test_model_permission_is_rechecked_after_waiting_for_admission(app, account, token, fake_ollama, change):
    from bananachat import db
    from bananachat.db import access, catalog
    from bananachat.db import limits as limit_store
    from bananachat.services.completions import CompletionRun, parse_chat_request

    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        if change == "effort":
            db.execute("UPDATE ai_models SET is_reasoning=1, reasoning_levels=? WHERE id=?",
                       ('["low", "medium", "high"]', model["id"]))
            limit_store.set_effort_level(account["id"], model["id"], "high", source="admin")
        params = parse_chat_request({"model": model["ollama_name"], "messages": [{"role": "user", "content": "Hi"}],
                                    **({"reasoning_effort": "high"} if change == "effort" else {})})
        run = CompletionRun(account, params, request_type="api")
        if change == "policy":
            access.set_policy("model", model["id"], "deny_except_allowlist", False, None)
        elif change == "rollout":
            catalog.set_rollout(model["id"], False)
        else:
            limit_store.set_effort_level(account["id"], model["id"], "low", source="admin")
        from bananachat.services.completions import CompletionError

        with pytest.raises(CompletionError) as caught:
            list(run.events())
        assert caught.value.status == 403
        assert caught.value.code == ("reasoning_effort_locked" if change == "effort" else "model_not_allowed")
        run.close()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (account["id"],)) == 0
    assert not fake_ollama.chat_bodies()


def test_a_fallback_cannot_use_access_revoked_since_selection(app, account, token, fake_ollama, monkeypatch):
    from bananachat.db import access, catalog
    from bananachat.services import ollama

    original = ollama.chat_stream
    calls = []

    def stream(model, *args, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            fallback = catalog.get_by_name("qwen3:4b")
            access.set_policy("model", fallback["id"], "deny_except_allowlist", False, None)
        yield from original(model, *args, **kwargs)

    monkeypatch.setattr(ollama, "chat_stream", stream)
    fake_ollama.fail_models = {"llama3.2:3b"}
    response = chat(app, token, model="auto")
    assert_error(response, 403, "permission_error", "model_not_allowed")
    assert calls == ["llama3.2:3b"]


def test_terminal_backend_record_keeps_its_text_and_reasoning(app, account, token, monkeypatch):
    from bananachat.services import ollama

    def stream(*args, **kwargs):
        yield ollama.Chunk(content="Final answer", thinking="Final reasoning", done=True,
                           prompt_tokens=3, completion_tokens=4)

    monkeypatch.setattr(ollama, "chat_stream", stream)
    response = chat(app, token)
    assert response.status_code == 200
    message = response.json["choices"][0]["message"]
    assert message == {"role": "assistant", "content": "Final answer", "reasoning_content": "Final reasoning"}
    assert response.json["usage"]["total_tokens"] == 7


@pytest.mark.parametrize("change", ["policy", "rollout"])
def test_chat_rechecks_model_access_after_preparing_the_message(app, account, fake_ollama, change):
    from bananachat import db
    from bananachat.db import access, catalog, chats
    from bananachat.services import chat as chat_service
    from tests.app.test_chat import parse_sse

    with app.app_context():
        session_id = chats.create(account["id"])
        prepared = chat_service.prepare(account, chats.get(session_id),
            {"content": "Hi", "model": "llama3.2:3b"}, [], lang="en")
        model = prepared.selection.model
        if change == "policy":
            access.set_policy("model", model["id"], "deny_except_allowlist", False, None)
        else:
            catalog.set_rollout(model["id"], False)
        run = chat_service.start(prepared)
        items = parse_sse("".join(run.channel.stream()))
        assert items[-1]["type"] == "error" and items[-1]["state"] == "failed"
        assert "access" in items[-1]["message"].lower()
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0
        assert db.scalar("SELECT COUNT(*) FROM active_streams") == 0
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (account["id"],)) == 0
    assert not fake_ollama.chat_bodies()


@pytest.mark.parametrize("constraint", ["quota", "rate"])
def test_a_fallback_rechecks_and_consumes_its_own_limits(app, account, token, fake_ollama, monkeypatch, constraint):
    from bananachat.db import catalog, credits
    from bananachat.services import limits, ollama
    from tests.app.test_limits_tokens import _model_policy

    with app.app_context():
        fallback = catalog.get_by_name("qwen3:4b")
        _model_policy(fallback, enabled=True,
                      **({"window_tokens": 10} if constraint == "quota" else
                         {"rate_rules": [{"requests": 1, "per": "hour", "burst": 1}]}))
    original = ollama.chat_stream
    calls = []

    def stream(model, *args, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            if constraint == "quota":
                credits.charge(account["id"], 10, 0, request_type="api", model_id=fallback["id"])
            else:
                assert limits.admit(account, "api", fallback).allowed
        yield from original(model, *args, **kwargs)

    monkeypatch.setattr(ollama, "chat_stream", stream)
    fake_ollama.fail_models = {"llama3.2:3b"}
    response = chat(app, token, model="auto")
    assert_error(response, 429, "rate_limit_error",
                 "insufficient_quota" if constraint == "quota" else "rate_limit_exceeded")
    assert calls == ["llama3.2:3b"]


def test_finished_chat_survives_backend_cleanup_failure(app, account, monkeypatch):
    from bananachat.db import chats
    from bananachat.services import chat as chat_service
    from bananachat.services import ollama
    from tests.app.test_chat import messages, parse_sse

    def stream(*args, **kwargs):
        try:
            yield ollama.Chunk(content="The whole answer")
            yield ollama.Chunk(done=True, prompt_tokens=3, completion_tokens=4)
        finally:
            raise RuntimeError("The backend connection could not close")

    monkeypatch.setattr(ollama, "chat_stream", stream)
    with app.app_context():
        session_id = chats.create(account["id"])
        prepared = chat_service.prepare(account, chats.get(session_id),
                                        {"content": "Hi", "model": "llama3.2:3b"}, [], lang="en")
        run = chat_service.start(prepared)
        items = parse_sse("".join(run.channel.stream()))
    assert [item["type"] for item in items if item["type"] in ("done", "error")] == ["done"]
    assert messages(app, session_id)[-1]["content"] == "The whole answer"


def test_aborting_an_expired_lease_cannot_reset_a_new_run(app, account):
    import time

    from bananachat import db
    from bananachat.db import chats, runs

    with app.app_context():
        session_id = chats.create(account["id"])
        session = chats.get(session_id)
        old = runs.begin(session, account, content="First question", attachments=[], title=None, one_per_user=True)
        db.execute("UPDATE active_streams SET heartbeat_at=? WHERE session_id=?",
                   (time.time() - runs.LEASE_SECONDS - 10, session_id))
        assert runs.recover_stale(session_id) == 1
        new = runs.begin(session, account, content="Next question", attachments=[], title=None, one_per_user=True)
        runs.abort(session_id, old)
        assert runs.status(session_id)["state"] == "queued"
        assert db.one("SELECT owner_token FROM active_streams WHERE session_id=?", (session_id,))[0] == new.token
        assert db.scalar("SELECT COUNT(*) FROM chat_messages WHERE id=?", (old.message_id,)) == 1
        runs.abort(session_id, new)
        assert not runs.status(session_id)["active"]


def test_an_old_chat_run_does_not_contact_the_backend_after_losing_its_lease(app, account, fake_ollama):
    import time

    from bananachat import db
    from bananachat.db import chats, runs
    from bananachat.services import chat as chat_service

    with app.app_context():
        session_id = chats.create(account["id"])
        session = chats.get(session_id)
        prepared = chat_service.prepare(account, session, {"content": "Hi", "model": "llama3.2:3b"}, [], lang="en")
        old = runs.begin(session, account, content="Hi", attachments=[], title=None, one_per_user=True)
        run = chat_service.Run(app, prepared, old)
        db.execute("UPDATE active_streams SET heartbeat_at=? WHERE session_id=?",
                   (time.time() - runs.LEASE_SECONDS - 10, session_id))
        assert runs.recover_stale(session_id) == 1
        new = runs.begin(session, account, content="New", attachments=[], title=None, one_per_user=True)
        run._execute()
        assert run.cancel.reason == "lease lost"
        assert runs.status(session_id)["state"] == "queued"
        runs.abort(session_id, new)
    assert not fake_ollama.chat_bodies()


def test_admitted_model_uses_its_current_prompt_and_sampling_defaults(app, account, fake_ollama):
    from bananachat.db import catalog
    from bananachat.services.completions import CompletionRun, parse_chat_request

    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        catalog.update(model["id"], system_prompt="Old instructions", temperature=0.9)
        run = CompletionRun(account, parse_chat_request({"model": model["ollama_name"],
            "messages": [{"role": "user", "content": "Hi"}]}), request_type="api")
        catalog.update(model["id"], system_prompt="Current instructions", temperature=0.2)
        list(run.events())
        run.close()
    body = fake_ollama.chat_bodies()[-1]
    assert body["messages"][0] == {"role": "system", "content": "Current instructions"}
    assert body["options"]["temperature"] == 0.2


def test_interrupted_fallback_accounts_for_the_prompt_actually_sent(app, account, monkeypatch):
    from bananachat.db import catalog, credits
    from bananachat.services import inference, ollama
    from bananachat.services.completions import CompletionRun, parse_chat_request
    from bananachat.services.upstream import UpstreamError
    from tests.app.test_api import ledger

    def stream(model, *args, **kwargs):
        if model == "llama3.2:3b":
            raise UpstreamError("The primary model failed")
        yield ollama.Chunk(content="Partial answer")

    monkeypatch.setattr(ollama, "chat_stream", stream)
    prompt = "Fallback instructions " * 100
    with app.app_context():
        catalog.update(catalog.get_by_name("llama3.2:3b")["id"], system_prompt="Primary")
        catalog.update(catalog.get_by_name("qwen3:4b")["id"], system_prompt=prompt)
        run = CompletionRun(account, parse_chat_request({"model": "auto",
            "messages": [{"role": "user", "content": "Hi"}]}), request_type="api")
        run.prime(until=(inference.Delta,))
        run.close()
    rows = ledger(app, account)
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == credits.estimate_tokens(prompt) + credits.estimate_tokens("Hi")
    assert rows[0]["tokens_out"] == credits.estimate_tokens("Partial answer")
    assert rows[0]["usage_estimated"]
