"""Admission boundaries shared by model lifecycle and reasoning controls."""

import pytest

from bananachat import db
from bananachat.db import catalog
from bananachat.services import inference
from bananachat.services.upstream import CancelToken, UpstreamError
from tests.app.test_api import roll_out


def test_fallback_is_registered_before_its_deletion_check(app, make_user, monkeypatch):
    user = make_user("fallback-delete")
    roll_out(app)
    opened = []
    prepared = []

    with app.app_context():
        first = catalog.get_by_name("llama3.2:3b")
        fallback = catalog.get_by_name("qwen3:4b")

        class Slot:
            wait_ms = 0
            lane = "local"
            running = True

            def set_model(self, name):
                assert name == fallback["ollama_name"]
                catalog.set_lifecycle(fallback["id"], delete_requested_at=db.now())

        def prepare(model):
            prepared.append(model["id"])
            return [{"role": "user", "content": "Hello"}], {}

        def open_stream(request, model, *args):
            opened.append(model["id"])
            raise UpstreamError("Temporary failure")

        monkeypatch.setattr(inference, "_open_stream", open_stream)
        request = inference.TextRequest(user, first, [], fallbacks=[fallback], prepare=prepare)
        events = list(inference._run_admitted(request, CancelToken(), Slot(), app.config["BC"]))

    assert opened == prepared == [first["id"]]
    assert isinstance(events[-1], inference.Finished) and events[-1].state == "failed"


@pytest.mark.parametrize("level", ["extra", "xhigh"])
def test_openai_effort_aliases_parse_to_extra(level):
    from bananachat.config import Config
    from bananachat.services.completions import parse_chat_request

    params = parse_chat_request({"model": "model", "messages": [{"role": "user", "content": "Hello"}],
                                 "reasoning_effort": level}, config=Config())
    assert params.effort == "extra"


def test_extra_keeps_ollama_within_its_supported_think_levels():
    model = {"is_reasoning": True, "reasoning_levels": '["low","medium","high","extra","max"]'}
    assert inference.think_for(model, "extra") == "high"


def test_hosted_model_routes_api_extra_and_shares_account_rate_between_keys(app, make_user, monkeypatch):
    from bananachat.db import external_providers as provider_store, limits as store
    from bananachat.services import external_providers, limits
    from bananachat.services.ollama import Chunk
    from tests.app.test_api import assert_error, chat, token_for

    calls = []

    def provider_stream(model, messages, *, options=None, effort=None, tools=None, cancel=None):
        cancel.check()
        calls.append((model["backend_model_name"], effort))
        yield Chunk(content="Hello", done=True, finish_reason="stop", prompt_tokens=4, completion_tokens=6)

    monkeypatch.setattr(external_providers, "stream", provider_stream)
    user = make_user("hosted-api")
    with app.app_context():
        provider_id = provider_store.save(name="Effort fixture", protocol="openai",
                                          base_url="https://provider.invalid/v1")
        model = external_providers.enroll(provider_store.get(provider_id), "effort-test-model", manual=True,
                                          reasoning=["low", "medium", "high", "extra"])
        catalog.set_rollout(model["id"], True)
        catalog.set_lifecycle(model["id"], enrollment="reviewed")
        store.set_effort_level(user["id"], model["id"], "extra", source="admin")
        store.set_override(user["id"], "api", None,
                           rate_rules=[{"requests": 1, "per": "hour", "burst": 1}])
    first = token_for(app, user, "first")
    second = token_for(app, user, "second")
    response = chat(app, first, model=model["ollama_name"], reasoning_effort="xhigh")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json["choices"][0]["message"]["content"] == "Hello"
    assert response.json["usage"]["total_tokens"] == 10
    assert calls == [("effort-test-model", "extra")]
    with app.app_context():
        assert limits.model_limits(user, catalog.get(model["id"])).window.used == 10
        assert db.scalar("SELECT COUNT(*) FROM inference_queue") == 0
    assert_error(chat(app, second, model=model["ollama_name"]), 429, "rate_limit_error")
    assert len(calls) == 1
