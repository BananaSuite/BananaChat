"""Codex subscription pool (full re-audit pass 15, u2): a prompt that makes Codex try a tool is refused without
resting the account it ran on (UC2-1)."""
from __future__ import annotations

import time

import pytest

from bananachat.db import subscription_pool as store
from bananachat.services import subscription_pool
from tests.app.test_codex_cli import MODEL, _answer, _item, _reload, _scenario, connector  # noqa: F401


def test_uc2_1_tool_attempt_does_not_rest_the_account(make_app, connector):  # noqa: F811
    adapter, home, manifest = connector
    _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    limits = {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 10, "windowDurationMins": 300,
                                                             "resetsAt": int(time.time()) + 3600}}}
    _scenario(home, rate_limits=limits, **_answer(_item("file_change", "")))
    app = make_app(CODEX_EXTENSION="bananachat.services.codex_cli", CODEX_CLI_CONFIG=str(manifest))
    try:
        with app.app_context():
            account_id = store.add_account("primary", provider="codex", window_limit=100000)
            subscription_pool.refresh_quota("codex")
            with pytest.raises(RuntimeError, match="tried to use a tool"):
                list(subscription_pool.chat_stream("codex", MODEL["name"],
                                                   [{"role": "user", "content": "Create hello.txt with apply_patch"}],
                                                   user_id="mallory"))
            row = store.get(account_id)
            assert not row["cooldown_until"] and row["status"] == "active"
            # Another person's ordinary request is still answered by the only account.
            _scenario(home, rate_limits=limits, **_answer(_item("agent_message", "Hi")))
            chunks = list(subscription_pool.chat_stream("codex", MODEL["name"], [{"role": "user", "content": "Hi"}],
                                                        user_id="alice"))
            assert any(chunk.get("text") == "Hi" for chunk in chunks)
    finally:
        subscription_pool.reset_transport()
