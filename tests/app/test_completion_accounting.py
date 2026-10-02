"""Completion success requires committed usage; cleanup keeps the original outcome."""

from __future__ import annotations

import sqlite3

import pytest

from tests.app.fixtures import Browser
from tests.app.test_api import assert_error, chat, events, ledger, roll_out, token_for


@pytest.fixture
def account(app, make_user):
    roll_out(app)
    return make_user("accounting")


@pytest.fixture
def api_token(app, account):
    return token_for(app, account)


@pytest.fixture
def failed_metrics(monkeypatch):
    """Fail after the ledger insert, so the real accounting transaction must roll back."""
    from bananachat.services import api_usage

    attempts = []

    def unavailable(**fields):
        attempts.append(fields)
        raise sqlite3.OperationalError("database or disk is full: /private/instance/chat.db")

    monkeypatch.setattr(api_usage, "record_metric", unavailable)
    return attempts


def assert_no_usage_and_no_queue(app, account):
    from bananachat import db

    assert ledger(app, account) == []
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM request_metrics WHERE user_id=?", (account["id"],)) == 0
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


@pytest.mark.parametrize("stream", [False, True])
def test_failed_usage_commit_never_reports_completion_success(app, account, api_token, failed_metrics, stream):
    response = chat(app, api_token, stream=stream, stream_options={"include_usage": True})
    if stream:
        # Headers have already been sent with the first delta: the failure is an SSE error.
        assert response.status_code == 200
        items = events(response)
        assert items[-1] == "[DONE]"
        assert items[-2]["error"]["code"] == "storage_unavailable"
        assert items[-2]["error"]["type"] == "server_error"
        assert any(item.get("choices") and item["choices"][0]["delta"].get("content")
                   for item in items if isinstance(item, dict))
        assert not any("usage" in item or (item.get("choices") and item["choices"][0]["finish_reason"])
                       for item in items if isinstance(item, dict))
    else:
        assert_error(response, 503, "server_error", "storage_unavailable")
        assert response.headers["Retry-After"] == "5"
        assert "choices" not in response.json
    assert "/private/" not in response.get_data(as_text=True)
    response.close()
    response.close()
    assert len(failed_metrics) == 1  # no second charge with estimated usage during cleanup
    assert_no_usage_and_no_queue(app, account)


def test_failed_usage_for_an_empty_answer_is_http_error_before_streaming(
        app, account, api_token, fake_ollama, failed_metrics):
    fake_ollama.reply = ""
    response = chat(app, api_token, stream=True)
    assert_error(response, 503, "server_error", "storage_unavailable")
    assert len(failed_metrics) == 1
    assert_no_usage_and_no_queue(app, account)


@pytest.mark.parametrize("language", ["en", "it"])
def test_playground_reports_translated_usage_failure_without_done(app, account, failed_metrics, language):
    from bananachat.db import users
    from bananachat.i18n import translate

    with app.app_context():
        users.save_preferences(account["id"], {"interface_language": language})
    browser = Browser(app)
    browser.login("accounting")
    response = browser.post_json("/developer/playground/send", {
        "messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 200
    items = events(response)
    assert any(item["type"] == "delta" for item in items)
    assert items[-1] == {"type": "error", "code": "storage_unavailable",
                         "message": translate(language, "errors.storage_unavailable")}
    assert not any(item["type"] == "done" for item in items)
    assert len(failed_metrics) == 1
    assert_no_usage_and_no_queue(app, account)


def test_backend_error_survives_failure_to_record_its_metric(
        app, account, api_token, fake_ollama, failed_metrics):
    fake_ollama.fail_models = set(fake_ollama.models)
    response = chat(app, api_token)
    assert_error(response, 502, "server_error", "upstream_error")
    assert len(failed_metrics) == 1
    assert_no_usage_and_no_queue(app, account)


def test_disconnect_with_failed_partial_accounting_closes_without_retry(
        app, account, api_token, fake_ollama, failed_metrics):
    fake_ollama.reply = " ".join(["word"] * 200)
    fake_ollama.chunk_delay = 0.01
    response = app.test_client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_token}"},
                                      json={"stream": True, "messages": [{"role": "user", "content": "Count"}]},
                                      buffered=False)
    stream = iter(response.response)
    received = [next(stream) for _ in range(5)]
    assert any("word" in (part.decode() if isinstance(part, bytes) else part) for part in received)
    response.close()
    response.close()
    assert len(failed_metrics) == 1
    assert failed_metrics[0]["status"] == "stopped" and failed_metrics[0]["usage_estimated"]
    assert_no_usage_and_no_queue(app, account)


def test_cleanup_and_partial_accounting_failures_keep_the_primary_error(
        app, account, api_token, failed_metrics, monkeypatch):
    from bananachat.services import inference
    from bananachat.services.completions import CompletionError

    class BrokenSource:
        def __init__(self, request):
            self.model = request.model

        def __iter__(self):
            yield inference.Started(self.model, False, 0)
            yield inference.Delta("Partial answer")
            raise CompletionError("The inference request failed.", 502, "upstream_error")

        def close(self):
            raise RuntimeError("Inference cleanup failed")

    monkeypatch.setattr(inference, "generate", lambda request, cancel: BrokenSource(request))
    response = chat(app, api_token)
    assert_error(response, 502, "server_error", "upstream_error")
    assert response.json["error"]["message"] == "The inference request failed."
    assert len(failed_metrics) == 1
    assert_no_usage_and_no_queue(app, account)


@pytest.mark.parametrize("stream", [False, True])
def test_backend_cleanup_after_finished_does_not_replace_committed_success(
        app, account, api_token, monkeypatch, stream):
    from bananachat.services import inference

    closed = []

    def generate(request, cancel):
        try:
            yield inference.Started(request.model, False, 0)
            yield inference.Delta("The whole answer")
            yield inference.Finished("completed", request.model, prompt_tokens=3, completion_tokens=4)
        finally:
            closed.append(True)
            raise RuntimeError("The backend connection could not close")

    monkeypatch.setattr(inference, "generate", generate)
    response = chat(app, api_token, stream=stream, stream_options={"include_usage": True})
    assert response.status_code == 200
    if stream:
        items = events(response)
        assert items[-1] == "[DONE]"
        assert not any("error" in item for item in items if isinstance(item, dict))
        finished = [item for item in items[:-1] if item.get("choices") and item["choices"][0]["finish_reason"]]
        assert len(finished) == 1 and finished[0]["choices"][0]["finish_reason"] == "stop"
        usage = items[-2]["usage"]
    else:
        assert response.json["choices"][0]["message"]["content"] == "The whole answer"
        usage = response.json["usage"]
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    response.close()
    response.close()
    assert closed == [True]
    rows = ledger(app, account)
    assert len(rows) == 1 and rows[0]["tokens_in"] == 3 and rows[0]["tokens_out"] == 4
