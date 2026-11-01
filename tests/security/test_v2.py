"""Codex subscription pool (full re-audit passes 16 and 17): a request ended by a tool attempt (VC2-1) or by the
CLI timeout before any output (XC2-1) is charged its prompt, as an interrupted one is."""
from __future__ import annotations

import json
import time

import pytest

from bananachat import db, security
from bananachat.db import catalog, users
from bananachat.db import subscription_pool as store
from bananachat.services import api_usage, subscription_pool
from bananachat.services.codex_cli import MAX_LINE
from tests.app.test_api import call, token_for
from tests.app.test_codex_cli import MODEL, _answer, _item, _reload, _scenario, connector  # noqa: F401


@pytest.mark.parametrize("answer", ["tool", "oversized"], ids=["tool_attempt", "oversized_record_ac2_1"])
def test_vc2_1_a_codex_tool_attempt_is_charged_its_prompt(make_app, connector, monkeypatch, answer):  # noqa: F811
    adapter, home, manifest = connector
    _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    limits = {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 10, "windowDurationMins": 300,
                                                             "resetsAt": int(time.time()) + 3600}}}
    app = make_app(CODEX_EXTENSION="bananachat.services.codex_cli", CODEX_CLI_CONFIG=str(manifest))
    charges, recorded = [], []
    charge, record = api_usage.charge_text, subscription_pool.record_usage
    monkeypatch.setattr(api_usage, "charge_text",
                        lambda **kw: charges.append(kw["prompt_tokens"]) or charge(**kw))
    monkeypatch.setattr(subscription_pool, "record_usage",
                        lambda account_id, prompt, *a, **kw: recorded.append(prompt) or record(account_id, prompt, *a, **kw))
    try:
        with app.app_context():
            store.add_account("primary", provider="codex", window_limit=10_000_000)
            subscription_pool.refresh_quota("codex")
            subscription_pool.sync_catalog("codex", selected=[MODEL["name"]], source="test")
            model = catalog.get_by_name(MODEL["name"])
            catalog.set_rollout(model["id"], True)
            catalog.set_lifecycle(model["id"], enrollment="reviewed")
            user = users.get(users.create("mallory", security.hash_password("mallory-password")))
        token = token_for(app, user)
        body = {"model": MODEL["name"], "messages": [{"role": "user", "content": "word " * 4000 + "apply_patch"}]}
        item = _item("file_change", "") if answer == "tool" else _item("agent_message", "x" * (MAX_LINE + 10))
        _scenario(home, rate_limits=limits, **_answer(item))
        assert call(app, "POST", "/v1/chat/completions", token, body).status_code == 502
        assert len(charges) == 1 and charges[0] > 1000
        assert len(recorded) == 1 and recorded[0] > 1000
        with app.app_context():  # the request was at fault, not the account
            assert all(not row["cooldown_until"] for row in db.query("SELECT cooldown_until FROM subscription_accounts"))
    finally:
        subscription_pool.reset_transport()


@pytest.mark.parametrize("recently_rested", [False, True], ids=["first", "after_a_rest_yc2_1"])
def test_xc2_1_a_codex_request_that_times_out_before_any_output_is_charged_its_prompt(
        make_app, connector, monkeypatch, recently_rested):  # noqa: F811
    adapter, home, manifest = connector
    _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    data = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**data, "timeout_seconds": 10}))
    limits = {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 10, "windowDurationMins": 300,
                                                             "resetsAt": int(time.time()) + 3600}}}
    app = make_app(CODEX_EXTENSION="bananachat.services.codex_cli", CODEX_CLI_CONFIG=str(manifest))
    charges, recorded = [], []
    charge, record = api_usage.charge_text, subscription_pool.record_usage
    monkeypatch.setattr(api_usage, "charge_text",
                        lambda **kw: charges.append(kw["prompt_tokens"]) or charge(**kw))
    monkeypatch.setattr(subscription_pool, "record_usage",
                        lambda account_id, prompt, *a, **kw: recorded.append(prompt) or record(account_id, prompt, *a, **kw))
    try:
        with app.app_context():
            store.add_account("primary", provider="codex", window_limit=10_000_000)
            subscription_pool.refresh_quota("codex")
            subscription_pool.sync_catalog("codex", selected=[MODEL["name"]], source="test")
            model = catalog.get_by_name(MODEL["name"])
            catalog.set_rollout(model["id"], True)
            catalog.set_lifecycle(model["id"], enrollment="reviewed")
            user = users.get(users.create("mallory", security.hash_password("mallory-password")))
            if recently_rested:  # YC2-1: an earlier failure of this person rested an account a moment ago
                db.execute("INSERT INTO runtime_state (key, value, updated_at) VALUES (?, '1', ?)",
                           (f"subscription:rested-by:codex:{user['id']}", time.time()))
        token = token_for(app, user)
        body = {"model": MODEL["name"], "messages": [{"role": "user", "content": "word " * 4000 + "write a book"}]}
        _scenario(home, rate_limits=limits, hang=True,
                  events=[{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}])
        assert call(app, "POST", "/v1/chat/completions", token, body).status_code == 502
        assert len(charges) == 1 and charges[0] > 1000
        assert len(recorded) == 1 and recorded[0] > 1000
    finally:
        subscription_pool.reset_transport()
