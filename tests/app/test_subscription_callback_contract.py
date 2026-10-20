"""Synchronous adapter cleanup and reasoning contracts for each provider, using no provider connection.

A transport whose close fails cannot prove the provider request stopped, so the
account stays quarantined until an administrator reactivates it; a required
``cancel`` keyword receives BananaChat's ``extra`` level for the CLI's xhigh.
"""

from datetime import timedelta

import pytest

from bananachat import db
from bananachat.db import subscription_pool as accounts
from bananachat.services import subscription_pool
from bananachat.services.upstream import Cancelled, CancelToken

MODEL = "review-provider-model"
STANDARD = {"claude": "sonnet", "codex": "sol"}


@pytest.fixture(autouse=True, params=subscription_pool.PROVIDERS)
def provider(request):
    yield request.param
    subscription_pool.reset_transport()


def configure_adapter(provider, chat, *, reasoning=()):
    account_id = accounts.add_account("review-account", provider=provider, window_limit=1000)
    subscription_pool.register_site_chat(provider, chat)
    subscription_pool.register_site_discovery(provider, lambda: [{"name": MODEL, "family": STANDARD[provider],
                                                                  "reasoning": list(reasoning),
                                                                  "account_ids": [account_id]}])
    return account_id


class UnclosedTransport:
    """The upstream operation survives a failed close attempt."""

    def __init__(self):
        self.records = iter([{"text": "partial", "tokens_in": 4, "tokens_out": 2}, {"done": True}])
        self.close_attempted = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.records)

    def close(self):
        self.close_attempted = True
        raise RuntimeError("private-provider-material")


@pytest.mark.parametrize("exit_path", ["cancel_then_close", "cancel_then_read", "consumer_close"])
def test_failed_transport_close_quarantines_account_until_admin_reactivation(app, warnings_log, provider, exit_path):
    transport = UnclosedTransport()

    def chat(account, model, messages, options, *, cancel):
        return transport

    with app.app_context():
        account_id = configure_adapter(provider, chat)
        cancel = CancelToken()
        stream = subscription_pool.stream_chunks(provider, MODEL, [], cancel=cancel)
        assert next(stream).content == "partial"
        assert accounts.leased(account_id)
        if exit_path.startswith("cancel"):
            cancel.cancel("stopped")
        if exit_path == "cancel_then_read":
            with pytest.raises(Cancelled):
                next(stream)
        else:
            stream.close()
        row = accounts.get(account_id)
        assert transport.close_attempted
        assert row["status"] == "disabled"
        assert row["window_used"] == 6
        assert not accounts.leased(account_id)
        assert subscription_pool.accounts_in_order(provider) == []
        assert "account stream failed (RuntimeError)" in warnings_log.text
        assert "private-provider-material" not in row["last_error"] + warnings_log.text
        # A successful telemetry refresh does not establish that the old operation stopped.
        accounts.report_quota(account_id, window_used=6, window_limit=1000,
                              window_resets_at=db.now(timedelta(hours=5)))
        assert accounts.get(account_id)["status"] == "disabled"


@pytest.mark.parametrize("effort", ["extra", "xhigh"])
def test_required_cancel_callback_receives_extra_and_releases_account_after_close(app, provider, effort):
    seen, closed = [], []

    def chat(account, model, messages, options, *, cancel):
        seen.append((account["id"], model, options["effort"], cancel))
        try:
            yield {"text": "ok", "tokens_in": 4, "tokens_out": 1}
            yield {"done": True}
        finally:
            closed.append(account["id"])

    with app.app_context():
        account_id = configure_adapter(provider, chat, reasoning=("low", "medium", "high", "xhigh"))
        cancel = CancelToken()
        result = list(subscription_pool.stream_chunks(provider, MODEL, [], effort=effort, cancel=cancel))
        assert seen == [(account_id, MODEL, "extra", cancel)]
        assert result[-1].done and result[-1].prompt_tokens == 4 and result[-1].completion_tokens == 1
        assert closed == [account_id]
        assert not accounts.leased(account_id)
        row = accounts.get(account_id)
        assert row["window_used"] == 5 and row["status"] == "active"
        assert subscription_pool.usable(row)
