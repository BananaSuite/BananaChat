"""API capacity errors stay distinct from failed provider streams, without live credentials."""

from __future__ import annotations

import time

import pytest

from bananachat import db
from bananachat.db import catalog, claude_pool as accounts, limits as limits_db
from bananachat.services import claude_pool as pool, inference, ollama
from bananachat.services.upstream import UpstreamError
from tests.app.test_api import assert_error, chat, events, ledger, roll_out, token_for

MODEL = "capacity-test-sonnet"
LOCAL_MODEL = "llama3.2:3b"


@pytest.fixture
def provider(app):
    calls = []

    def answer(account, model, messages, options, *, cancel):
        cancel.check()
        calls.append(account["id"])
        yield {"text": "Provider answer", "tokens_in": 3, "tokens_out": 4, "done": True}

    pool.reset_transport()
    pool.register_site_chat(answer)
    pool.register_site_discovery(lambda: [{"name": MODEL, "family": "sonnet", "reasoning": [],
                                          "capabilities": ["completion"]}])
    with app.app_context():
        pool.sync_catalog(selected=[MODEL], source="admin")
        model = catalog.get_by_name(MODEL)
        catalog.set_rollout(model["id"], True)
        account_id = accounts.add_account("capacity-primary", priority=10, window_limit=1_000_000)
    yield {"model": model, "account_id": account_id, "calls": calls}
    pool.reset_transport()


@pytest.fixture
def user(app, make_user, provider):
    return make_user("capacity-api-user")


@pytest.fixture
def token(app, user):
    return token_for(app, user)


def subscription_observation(**changes):
    return {"source": "claude_code", "available": True, "observed_at": time.time(),
            "window_left": 0.8, "window_resets_at": time.time() + 10_000,
            "weekly_left": 0.7, "weekly_resets_at": time.time() + 100_000,
            "subscription_type": "max", "model_limits": {}, **changes}


def change_capacity_at_open(monkeypatch, provider, reason):
    """Lose provider capacity after the API's normal selection and fresh admission checks."""
    account_id = provider["account_id"]
    observation = subscription_observation()
    if reason != "busy":
        pool.register_site_reporter(lambda: {"account_reports": {str(account_id): dict(observation)}})
    original = inference._open_stream
    opened = []

    def open_stream(request, model, via_worker, cancel, config):
        opened.append(model["ollama_name"])
        if model["backend"] == "claude":
            if reason == "quota0":
                observation["window_left"] = 0
            elif reason == "stale":
                observation["observed_at"] = time.time() - 901
            else:
                assert accounts.claim(account_id, "another-request")
            pool.refresh_quota()
        return original(request, model, via_worker, cancel, config)

    monkeypatch.setattr(inference, "_open_stream", open_stream)
    return opened


def configured_local_fallback(app, monkeypatch):
    """Supply a fallback while keeping the real API's permission and quota checks."""
    roll_out(app)
    with app.app_context():
        local = catalog.get_by_name(LOCAL_MODEL)
        catalog.update(local["id"], system_prompt="Fallback instructions", temperature=0.2)
        local = catalog.get(local["id"])
    original = inference.select_model

    def select_model(context, requested, **kwargs):
        selection = original(context, requested, **kwargs)
        if requested == MODEL:
            selection.fallbacks = [local]
        return selection

    monkeypatch.setattr(inference, "select_model", select_model)
    return local


def assert_idle(app):
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["quota0", "stale"])
def test_initial_provider_capacity_unavailable_is_retryable_instead_of_a_user_quota_error(
        app, provider, user, token, stream, reason):
    observation = subscription_observation(**({"window_left": 0} if reason == "quota0" else
                                              {"observed_at": time.time() - 901}))
    pool.register_site_reporter(lambda: {"account_reports": {str(provider["account_id"]): observation}})
    with app.app_context():
        pool.refresh_quota()
        assert pool.capacity_report(catalog.get(provider["model"]["id"])).window_left == 0
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 503, "server_error", "provider_capacity_unavailable")
    assert response.headers["Retry-After"] == "30"
    assert response.mimetype == "application/json"
    assert provider["calls"] == []
    response.close()
    assert ledger(app, user) == []
    with app.app_context():
        assert catalog.get(provider["model"]["id"])["recent_failures"] == 0
        assert accounts.get(provider["account_id"])["window_used"] == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
def test_internal_claude_token_allowance_still_uses_the_429_quota_error(
        app, provider, user, token, stream):
    with app.app_context():
        model = catalog.get(provider["model"]["id"])
        policy = limits_db.get_model_policy(model)
        limits_db.set_model_policy(model["id"], {**policy, "window_tokens": 0, "dynamic": False}, None)
        assert pool.capacity_report(model).window_left == 1
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 429, "rate_limit_error", "insufficient_quota")
    assert provider["calls"] == []
    response.close()
    assert ledger(app, user) == []
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["quota0", "stale", "busy"])
def test_capacity_lost_before_output_is_retryable_without_charging_or_model_failure(
        app, provider, user, token, monkeypatch, stream, reason):
    opened = change_capacity_at_open(monkeypatch, provider, reason)
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 503, "server_error", "provider_capacity_unavailable")
    assert response.headers["Retry-After"] == "30"
    assert response.mimetype == "application/json"
    assert "choices" not in response.json
    assert opened == [MODEL] and provider["calls"] == []
    response.close()
    response.close()
    assert ledger(app, user) == []
    with app.app_context():
        model = catalog.get(provider["model"]["id"])
        assert model["recent_failures"] == 0 and model["failing_at"] is None
        assert accounts.get(provider["account_id"])["window_used"] == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0
        metrics = db.query("SELECT status FROM request_metrics WHERE user_id=?", (user["id"],))
        assert len(metrics) == 1 and metrics[0]["status"] == "error"
        if reason == "busy":
            # Failed admission must preserve the other request's lease.
            assert accounts.owns(provider["account_id"], "another-request")
            accounts.release(provider["account_id"], "another-request")
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("condition", ["quota", "admission"])
def test_provider_quota_or_admission_changes_before_output_are_retryable(
        app, provider, user, token, stream, condition):
    seen = []

    def unavailable(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        if condition == "quota":
            raise pool.QuotaExhausted("private-provider-secret")
        raise pool.AdmissionChanged("private-provider-secret")

    pool.register_site_chat(unavailable)
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 503, "server_error", "provider_capacity_unavailable")
    assert response.headers["Retry-After"] == "30"
    assert "private-provider-secret" not in response.get_data(as_text=True)
    assert seen == [provider["account_id"]]
    response.close()
    assert ledger(app, user) == []
    with app.app_context():
        assert catalog.get(provider["model"]["id"])["recent_failures"] == 0
        assert accounts.get(provider["account_id"])["window_used"] == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
def test_capacity_failure_can_use_an_authorized_fallback_before_output(
        app, provider, user, token, monkeypatch, stream):
    local = configured_local_fallback(app, monkeypatch)
    opened = change_capacity_at_open(monkeypatch, provider, "quota0")
    calls = []

    def local_answer(model, messages, *, options, **kwargs):
        calls.append((model, messages, options))
        yield ollama.Chunk(content="Local fallback answer", done=True, prompt_tokens=3, completion_tokens=4)

    monkeypatch.setattr(ollama, "chat_stream", local_answer)
    response = chat(app, token, model=MODEL, stream=stream, stream_options={"include_usage": True})
    assert response.status_code == 200
    if stream:
        items = events(response)
        assert items[-1] == "[DONE]"
        assert not any("error" in item for item in items if isinstance(item, dict))
        assert "".join(item["choices"][0]["delta"].get("content", "")
                       for item in items if isinstance(item, dict) and item.get("choices")) == "Local fallback answer"
        usage = items[-2]["usage"]
    else:
        assert response.json["choices"][0]["message"]["content"] == "Local fallback answer"
        assert response.json["model"] == LOCAL_MODEL
        usage = response.json["usage"]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert opened == [MODEL, LOCAL_MODEL] and provider["calls"] == []
    assert len(calls) == 1 and calls[0][0] == LOCAL_MODEL
    assert calls[0][1][0] == {"role": "system", "content": "Fallback instructions"}
    assert calls[0][2]["temperature"] == 0.2
    response.close()
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]["model_id"] == local["id"]
    assert (rows[0]["tokens_in"], rows[0]["tokens_out"], rows[0]["usage_estimated"]) == (3, 4, 0)
    with app.app_context():
        assert catalog.get(provider["model"]["id"])["recent_failures"] == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
def test_capacity_error_survives_a_selected_fallback_being_withdrawn_before_its_attempt(
        app, provider, user, token, monkeypatch, stream):
    local = configured_local_fallback(app, monkeypatch)
    opened = change_capacity_at_open(monkeypatch, provider, "quota0")
    original = inference._open_stream

    def open_stream(request, model, via_worker, cancel, config):
        if model["backend"] == "claude":
            catalog.set_rollout(local["id"], False)
        return original(request, model, via_worker, cancel, config)

    monkeypatch.setattr(inference, "_open_stream", open_stream)
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 503, "server_error", "provider_capacity_unavailable")
    assert response.headers["Retry-After"] == "30"
    assert opened == [MODEL] and provider["calls"] == []
    response.close()
    assert ledger(app, user) == []
    with app.app_context():
        assert catalog.get(provider["model"]["id"])["recent_failures"] == 0
        assert catalog.get(local["id"])["recent_failures"] == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fault", ["malformed", "provider_error", "missing_terminal"])
def test_failed_provider_streams_keep_the_502_upstream_error_envelope(
        app, provider, user, token, stream, fault):
    records = {"malformed": [{"text": 4}], "provider_error": [{"error": "private-provider-secret"}],
               "missing_terminal": []}
    pool.register_site_chat(lambda *args, **kwargs: iter(records[fault]))
    response = chat(app, token, model=MODEL, stream=stream)
    assert_error(response, 502, "server_error", "upstream_error")
    assert "Retry-After" not in response.headers
    assert "private-provider-secret" not in response.get_data(as_text=True)
    response.close()
    assert ledger(app, user) == []
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["text", "thinking"])
def test_quota_failure_after_output_keeps_partial_accounting_without_account_or_model_replay(
        app, provider, user, token, monkeypatch, stream, field):
    from bananachat.db.credits import estimate_tokens

    configured_local_fallback(app, monkeypatch)
    with app.app_context():
        accounts.add_account("capacity-spare", window_limit=1_000_000)
    seen = []

    def partial_answer(account, model, messages, options, *, cancel):
        seen.append(account["id"])
        yield {field: "Partial provider answer", "tokens_in": 5}
        raise pool.QuotaExhausted("private-provider-secret")

    def unexpected_fallback(*args, **kwargs):
        raise AssertionError("A model fallback must not replay a prompt after observable provider output")

    pool.register_site_chat(partial_answer)
    monkeypatch.setattr(ollama, "chat_stream", unexpected_fallback)
    response = chat(app, token, model=MODEL, stream=stream, stream_options={"include_usage": True})
    if stream:
        assert response.status_code == 200
        items = events(response)
        assert items[-1] == "[DONE]"
        assert items[-2]["error"]["code"] == "upstream_error"
        delta_field = "content" if field == "text" else "reasoning_content"
        assert "".join(item["choices"][0]["delta"].get(delta_field, "")
                       for item in items if isinstance(item, dict) and item.get("choices")) == "Partial provider answer"
        assert not any("usage" in item or (item.get("choices") and item["choices"][0]["finish_reason"])
                       for item in items if isinstance(item, dict))
    else:
        assert_error(response, 502, "server_error", "upstream_error")
    assert "Retry-After" not in response.headers
    assert "private-provider-secret" not in response.get_data(as_text=True)
    assert seen == [provider["account_id"]]
    response.close()
    response.close()
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]["model_id"] == provider["model"]["id"]
    assert (rows[0]["tokens_in"], rows[0]["tokens_out"], rows[0]["usage_estimated"]) == (
        estimate_tokens("Hi"), estimate_tokens("Partial provider answer"), 1)
    with app.app_context():
        usage = db.query("SELECT * FROM claude_account_usage")
        assert len(usage) == 1
        assert (usage[0]["tokens_in"], usage[0]["tokens_out"]) == (5, estimate_tokens("Partial provider answer"))
        assert accounts.get(provider["account_id"])["window_used"] == 5 + estimate_tokens("Partial provider answer")
        assert db.scalar("SELECT status FROM request_metrics WHERE user_id=?", (user["id"],)) == "stopped"
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
    assert_idle(app)


@pytest.mark.parametrize("stream", [False, True])
def test_ordinary_local_upstream_error_still_counts_as_a_model_failure(
        app, provider, user, token, monkeypatch, stream):
    roll_out(app)

    def failed_answer(*args, **kwargs):
        raise UpstreamError("The model could not generate an answer.")

    monkeypatch.setattr(ollama, "chat_stream", failed_answer)
    response = chat(app, token, model=LOCAL_MODEL, stream=stream)
    assert_error(response, 502, "server_error", "upstream_error")
    response.close()
    with app.app_context():
        assert catalog.get_by_name(LOCAL_MODEL)["recent_failures"] == 1
    assert ledger(app, user) == []
    assert_idle(app)
