"""Malformed model-server replies cannot withdraw models or report false success."""

from __future__ import annotations

import pytest

from bananachat import db
from bananachat.db import catalog
from bananachat.services import model_lifecycle, ollama
from bananachat.services.upstream import UpstreamError
from tests.app.test_api import assert_error, chat, events, ledger, roll_out, token_for


@pytest.mark.parametrize("body", [None, [], {}, {"models": None}, {"models": {}}, {"models": 3},
                                  {"models": [None]}, {"models": [{"name": []}]},
                                  {"models": [{"name": "bad name"}]},
                                  {"models": [{"name": "valid:1b"}] * 5001}])
def test_invalid_inventory_never_changes_existing_catalog(app, fake_ollama, body):
    roll_out(app)
    with app.app_context():
        before = [dict(model) for model in catalog.list_models()]
    fake_ollama.json_overrides["/api/tags"] = body
    with app.app_context():
        # Repeated invalid replies must not accumulate evidence of absence.
        for _ in range(model_lifecycle.policy()["missing_syncs"] + 1):
            with pytest.raises(UpstreamError):
                model_lifecycle.sync()
        assert [dict(model) for model in catalog.list_models()] == before
        assert not model_lifecycle.sync_status()["ok"]


@pytest.mark.parametrize("path,reader", [("/api/version", ollama.version), ("/api/ps", ollama.list_running)])
@pytest.mark.parametrize("body", [None, [], {}, {"version": 2, "models": 2}])
def test_invalid_health_and_running_model_replies_have_controlled_errors(app, fake_ollama, path, reader, body):
    fake_ollama.json_overrides[path] = body
    with app.app_context(), pytest.raises(UpstreamError):
        reader()


def test_optional_malformed_metadata_does_not_fail_an_otherwise_valid_inventory(app, fake_ollama):
    fake_ollama.json_overrides["/api/tags"] = {"models": [
        {"name": "llama3.2:3b", "details": {"family": [], "quantization_level": {}, "parameter_size": []},
         "size": [], "digest": {}},
        {"name": "qwen3:4b", "details": [], "size": float("inf")},
    ]}
    with app.app_context():
        assert model_lifecycle.sync()["count"] == 2
        assert {model["ollama_name"] for model in catalog.list_models()} == set(fake_ollama.models)
        assert db.query("PRAGMA foreign_key_check") == []


FINAL = {"message": {"content": ""}, "done": True, "prompt_eval_count": 11, "eval_count": 5}
BAD_RECORDS = ([], {"message": []}, {"message": {"content": []}}, {"message": {"thinking": False}},
               {"message": {"content": "Hi"}, "done": "false"},
               {**FINAL, "eval_count": -1}, {**FINAL, "eval_count": True},
               {**FINAL, "prompt_eval_count": 1_000_000_001}, b"not valid JSON",
               b"[" * 5000 + b"0" + b"]" * 5000)


@pytest.mark.parametrize("record", BAD_RECORDS)
def test_invalid_stream_records_are_http_errors_before_any_output(app, make_user, fake_ollama, record):
    roll_out(app)
    user = make_user("malformed-stream")
    token = token_for(app, user)
    fake_ollama.chat_records = [record, FINAL]
    response = chat(app, token, model="llama3.2:3b")
    assert_error(response, 502, "server_error", "upstream_error")
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_invalid_record_after_partial_output_never_emits_terminal_success(app, make_user, fake_ollama):
    roll_out(app)
    user = make_user("partial-protocol")
    token = token_for(app, user)
    fake_ollama.chat_records = [{"message": {"content": "Partial answer"}, "done": False}, b"invalid", FINAL]
    response = chat(app, token, model="llama3.2:3b", stream=True, stream_options={"include_usage": True})
    assert response.status_code == 200
    items = events(response)
    assert items[-1] == "[DONE]" and items[-2]["error"]["code"] == "upstream_error"
    assert not any(item.get("choices") and item["choices"][0]["finish_reason"]
                   for item in items if isinstance(item, dict))
    rows = ledger(app, user)
    assert len(rows) == 1 and rows[0]["tokens_out"] > 0
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0


def test_an_empty_valid_inventory_is_still_supported(app, fake_ollama):
    fake_ollama.models = []
    with app.app_context():
        assert ollama.list_tags() == [] and ollama.list_running() == []
