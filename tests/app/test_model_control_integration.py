"""Admission boundaries shared by model lifecycle and reasoning controls."""

import sys
from types import ModuleType, SimpleNamespace

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


def test_configured_adapter_routes_api_extra_and_shares_account_rate_between_keys(make_app, monkeypatch):
    from bananachat import security
    from bananachat.db import claude_pool as accounts, limits as store, users
    from bananachat.services import claude_pool, limits
    from tests.app.test_api import assert_error, chat, token_for

    calls = []

    def provider_chat(account, model, messages, options, *, cancel):
        cancel.check()
        calls.append((account["id"], model, options["effort"]))
        yield {"text": "Hello", "tokens_in": 4, "tokens_out": 6, "done": True}

    module = ModuleType("bc_api_provider_fixture")
    module.create_adapter = lambda config: SimpleNamespace(
        chat=provider_chat,
        discover=lambda: [{"name": "claude-test-sonnet", "reasoning": ["low", "medium", "high", "xhigh"]}],
        quota=lambda: {"window_left": 1.0, "weekly_left": None},
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = make_app(CLAUDE_EXTENSION=module.__name__)
    with app.app_context():
        users.create("adapter-api", security.hash_password("adapter-api-password"), role="user")
        user = users.get_by_username("adapter-api")
        account_id = accounts.add_account("provider-fixture", window_limit=100_000)
        claude_pool.sync_catalog(selected=["claude-test-sonnet"], source="test")
        model = catalog.get_by_name("claude-test-sonnet")
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
    assert calls == [(account_id, model["ollama_name"], "extra")]
    with app.app_context():
        assert accounts.get(account_id)["window_used"] == 10
        assert db.scalar("SELECT COUNT(*) FROM claude_account_leases") == 0
        assert limits.effective(user, "api").window.used == 0
        assert limits.model_limits(user, catalog.get(model["id"])).window.used == 10
    assert_error(chat(app, second, model=model["ollama_name"]), 429, "rate_limit_error")
    assert len(calls) == 1
