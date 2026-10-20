"""Account binding and authentication changes cannot reuse another subscription's capacity."""
import json
import sys
import time
from types import SimpleNamespace

import pytest

from bananachat.db import subscription_pool as accounts
from bananachat.services import claude_usage, subscription_pool
from bananachat.services.claude_code import Adapter, ConnectorError


@pytest.fixture
def identity_adapter(tmp_path, monkeypatch):
    manifest = {"binary": sys.executable, "profiles": {}}
    names = ("primary", "secondary")
    addresses = {"primary": "first@review.invalid", "secondary": "second@review.invalid"}
    launched, refreshed = [], []
    for name in names:
        home = tmp_path / name
        config = home / "config"
        config.mkdir(parents=True)
        home.chmod(0o700)
        config.chmod(0o700)
        manifest["profiles"][name] = {"home": str(home), "config_dir": str(config), "discovery": "manual",
                                     "usage_source": "automatic", "models": [{"name": "identity-sonnet",
                                     "family": "sonnet", "reasoning": ["low", "medium"]}]}
    path = tmp_path / "connector.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    adapter = Adapter(SimpleNamespace(environment="testing", claude_code_config=str(path)))
    bindings = {"1": "primary", "2": "secondary"}
    adapter.bindings = lambda: dict(bindings)
    monkeypatch.setattr(accounts, "list_accounts", lambda **_: [
        {"id": 1, "label": "First"}, {"id": 2, "label": "Second"},
    ])

    def records(profile, arguments, *args, **kwargs):
        name = next(name for name in names if profile["home"] == adapter.profiles[name]["home"])
        if arguments[:2] == ["auth", "status"]:
            yield {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max",
                   "email": addresses[name]}
            return
        launched.append(name)
        yield {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "apiKeySource": "none"}
        yield {"type": "result", "subtype": "success", "is_error": False,
               "usage": {"input_tokens": 1, "output_tokens": 1}}

    adapter._records = records
    # These adapter contract tests isolate storage; pooled re-admission is
    # covered separately with real account rows and subscription observations.
    monkeypatch.setattr(subscription_pool, "refresh_quota", lambda provider=None: refreshed.append(provider) or {})
    monkeypatch.setattr(subscription_pool, "invalidate", lambda provider: refreshed.append(("invalidate", provider)))
    return adapter, bindings, addresses, launched, refreshed


def test_missing_account_profile_does_not_withdraw_healthy_discovery(identity_adapter):
    adapter, bindings, _, launched, _ = identity_adapter
    bindings["2"] = "removed-profile"
    models = adapter.discover()
    assert len(models) == 1 and models[0]["name"] == "identity-sonnet"
    assert models[0]["account_ids"] == [1]
    assert launched == []


def test_request_rechecks_duplicate_identity_before_generation(identity_adapter):
    adapter, _, addresses, launched, _ = identity_adapter
    adapter.check_profile("primary")
    adapter.check_profile("secondary")
    # A profile may be reauthenticated while an earlier quota observation is cached.
    addresses["primary"] = addresses["secondary"]
    with pytest.raises(ConnectorError):
        list(adapter.chat({"id": 1, "label": "First"}, "identity-sonnet",
                          [{"role": "user", "content": "synthetic"}], {"effort": "low"}))
    assert launched == []


def test_authentication_change_invalidates_model_and_usage_cache(identity_adapter, monkeypatch):
    adapter, _, addresses, launched, refreshed = identity_adapter
    adapter.check_profile("primary")
    adapter._models["primary"] = (time.monotonic(), [{"name": "cached-sonnet"}])
    invalidated = []
    monkeypatch.setattr(claude_usage, "invalidate", lambda owner, profile: invalidated.append((owner, profile)))
    addresses["primary"] = "replacement@review.invalid"
    adapter.check_profile("primary")
    assert "primary" not in adapter._models
    assert invalidated == [(adapter, adapter.profiles["primary"])]
    assert refreshed == [("invalidate", "claude")]
    assert launched == []


def test_observation_from_previous_authenticated_account_cannot_authorize_replacement(identity_adapter, monkeypatch):
    adapter, _, addresses, launched, _ = identity_adapter
    adapter.check_profile("primary")
    observed = time.time() - 1
    response = {"subscription_type": "max", "rate_limits_available": True,
                "rate_limits": {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}}}
    monkeypatch.setattr(claude_usage.claude_control, "request", lambda *args, **kwargs: response)
    monkeypatch.setattr(claude_usage, "_metadata", lambda profile: (observed, None))
    addresses["primary"] = "replacement@review.invalid"
    adapter.check_profile("primary")
    reports = adapter.usage_reports()
    assert reports["1"]["available"] is False
    assert reports["2"]["available"] is True
    assert launched == []


@pytest.mark.parametrize("exhausted", ["five_hour", "seven_day", "seven_day_sonnet"])
def test_request_after_auth_change_refuses_fresh_exhausted_allowance(identity_adapter, monkeypatch, exhausted):
    adapter, _, addresses, launched, refreshed = identity_adapter
    adapter.check_profile("primary")
    adapter.check_profile("secondary")
    response = {"subscription_type": "max", "rate_limits_available": True,
                "rate_limits": {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20},
                                "seven_day_sonnet": {"utilization": 30}}}
    response["rate_limits"][exhausted]["utilization"] = 100
    monkeypatch.setattr(claude_usage.claude_control, "request", lambda *args, **kwargs: response)
    monkeypatch.setattr(claude_usage, "_metadata", lambda profile: (time.time(), None))
    addresses["primary"] = "replacement@review.invalid"
    with pytest.raises(subscription_pool.AdmissionChanged):
        list(adapter.chat({"id": 1, "label": "First"}, "identity-sonnet",
                          [{"role": "user", "content": "synthetic"}], {"effort": "low"}))
    # Only the Claude pool re-reads its observations; Codex admission is untouched.
    assert refreshed == [("invalidate", "claude"), "claude"]
    assert launched == []
