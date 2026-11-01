"""Subscription pools: a request that fails on its own merits must not take every account offline."""

from __future__ import annotations

import json

from tests.app.test_api import token_for
from tests.app.test_claude_code import _delta, _terminal
from tests.security.test_cinfer import _claude_chat, _claude_pool, _rested

# An error result the CLI prints for a failure it does not explain.
FAILED = {"type": "result", "subtype": "success", "is_error": True, "result": "Something went wrong",
          "usage": {"input_tokens": 0, "output_tokens": 0}}
# What `claude -p --output-format stream-json` prints for a prompt above the model's context window.
TOO_LONG = {**FAILED, "result": "Prompt is too long"}


def _answering(tmp_path):
    for name in ("acct-a", "acct-b", "acct-c"):
        (tmp_path / name / "scenario.json").write_text(json.dumps({"records": [_delta("Hi"), _terminal()]}))


def test_r5_1_one_failing_request_rests_at_most_one_account(tmp_path, make_app, monkeypatch):
    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch, scenario={"records": [FAILED]})
    token = token_for(app, user)

    assert _claude_chat(app, token, "hello").status_code >= 500
    assert len(_rested(app, ids)) == 1

    _answering(tmp_path)
    assert _claude_chat(app, token, "hello").status_code == 200


def test_r4_2_one_person_rests_at_most_one_account_per_cooldown(tmp_path, make_app, monkeypatch):
    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch, scenario={"records": [FAILED]})
    token = token_for(app, user)

    for _ in range(3):
        assert _claude_chat(app, token, "hello").status_code >= 500
    assert len(_rested(app, ids)) == 1

    _answering(tmp_path)
    assert _claude_chat(app, token, "hello").status_code == 200


def test_r4_2_a_prompt_too_long_for_the_model_rests_no_account(tmp_path, make_app, monkeypatch):
    app, user, ids = _claude_pool(tmp_path, make_app, monkeypatch, scenario={"records": [TOO_LONG]})
    token = token_for(app, user)

    assert _claude_chat(app, token, "far too long").status_code >= 400
    assert _rested(app, ids) == []
