"""Automatic official-CLI discovery respects operator restrictions and caches."""
import json
from types import SimpleNamespace

import pytest

from bananachat.services.claude_code import Adapter, ConnectorError

OPUS = {"value": "opus", "resolvedModel": "claude-opus-test", "displayName": "Opus", "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]}
SONNET = {"value": "sonnet", "resolvedModel": "claude-sonnet-test", "displayName": "Sonnet", "supportsEffort": True,
          "supportedEffortLevels": ["low", "medium", "high"]}
FAKE = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
home=Path(os.environ['HOME'])
scenario=json.loads((home/'scenario.json').read_text())
if sys.argv[1:3]==['auth','status']:
 print(json.dumps({'loggedIn':scenario.get('authenticated',True),'authMethod':scenario.get('auth','claude.ai')}))
 sys.exit(0)
data=[json.loads(line) for line in sys.stdin.read().splitlines()]
with (home/'requests.jsonl').open('a') as log:
 for record in data: log.write(json.dumps(record)+'\\n')
for record in data:
 response={'subtype':'success','request_id':record['request_id'],'response':{'models':scenario.get('models',[])}}
 if scenario.get('fail'): response={'subtype':'error','request_id':record['request_id'],'error':'private-secret'}
 print(json.dumps({'type':'control_response','response':response}),flush=True)
'''


@pytest.fixture
def automatic(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    manifest = {"binary": str(binary), "profiles": {}}
    homes = {}
    for name in ("primary", "secondary"):
        home = tmp_path / name
        config = home / "config"
        config.mkdir(parents=True)
        home.chmod(0o700)
        config.chmod(0o700)
        manifest["profiles"][name] = {"home": str(home), "config_dir": str(config)}
        (home / "scenario.json").write_text(json.dumps({"models": [OPUS, SONNET]}))
        homes[name] = home
    path = tmp_path / "manifest.json"

    def build():
        path.write_text(json.dumps(manifest))
        path.chmod(0o600)
        adapter = Adapter(SimpleNamespace(claude_code_config=str(path)))
        adapter.bindings = lambda: {"1": "primary", "2": "secondary"}
        return adapter

    return build, manifest, homes


def _change(home, **values):
    (home / "scenario.json").write_text(json.dumps({"models": [OPUS, SONNET], **values}))


def test_models_omitted_default_to_automatic_and_cache_successful_snapshot(automatic):
    build, _, homes = automatic
    adapter = build()
    profile = adapter.profiles["primary"]
    assert profile["discovery"] == "automatic"
    models = adapter._profile_models("primary", profile)
    assert [model["name"] for model in models] == ["claude-opus-test", "claude-sonnet-test"]
    _change(homes["primary"], models=[])
    assert adapter._profile_models("primary", profile) == models
    adapter.invalidate_models()
    assert adapter._profile_models("primary", profile) == []


def test_automatic_allowlist_preserves_alias_and_intersects_actual_effort(automatic):
    build, manifest, _ = automatic
    manifest["profiles"]["primary"].update(discovery="automatic", models=[
        {"name": "sonnet", "family": "sonnet", "display": "Our Sonnet",
         "reasoning": ["medium", "high", "extra", "max"]},
        {"name": "claude-haiku-missing", "family": "haiku", "reasoning": []},
    ])
    adapter = build()
    models = adapter._profile_models("primary", adapter.profiles["primary"])
    assert len(models) == 1
    assert models[0]["name"] == "sonnet" and models[0]["display"] == "Our Sonnet"
    assert models[0]["reasoning"] == ["medium", "high"]


def test_legacy_explicit_manifest_keeps_manual_discovery(automatic):
    build, manifest, homes = automatic
    manifest["profiles"]["primary"]["models"] = [{"name": "sonnet", "family": "sonnet", "reasoning": ["low"]}]
    adapter = build()
    models = adapter._profile_models("primary", adapter.profiles["primary"])
    assert models[0]["name"] == "sonnet"
    assert adapter.profiles["primary"]["discovery"] == "manual"
    assert not (homes["primary"] / "requests.jsonl").exists()


@pytest.mark.parametrize("auth", [{"authenticated": False}, {"auth": "api_key"}])
def test_automatic_discovery_requires_native_authenticated_subscription(automatic, auth):
    build, _, homes = automatic
    _change(homes["primary"], **auth)
    adapter = build()
    with pytest.raises(ConnectorError, match="subscription"):
        adapter._profile_models("primary", adapter.profiles["primary"])
    assert not (homes["primary"] / "requests.jsonl").exists()


def test_failed_refresh_does_not_keep_stale_automatic_models(automatic):
    build, _, homes = automatic
    adapter = build()
    profile = adapter.profiles["primary"]
    assert adapter._profile_models("primary", profile)
    adapter.invalidate_models()
    _change(homes["primary"], fail=True)
    with pytest.raises(RuntimeError):
        adapter._profile_models("primary", profile)
    assert "primary" not in adapter._models
    _change(homes["primary"], models=[SONNET])
    assert [model["name"] for model in adapter._profile_models("primary", profile)] == ["claude-sonnet-test"]


def test_discovery_intersects_capabilities_over_real_account_profiles(automatic, monkeypatch):
    from bananachat.db import claude_pool as accounts

    build, _, homes = automatic
    _change(homes["secondary"], models=[{**SONNET, "supportedEffortLevels": ["low", "medium"]}])
    adapter = build()
    monkeypatch.setattr(accounts, "list_accounts", lambda **_: [
        {"id": 1, "label": "primary"}, {"id": 2, "label": "secondary"},
    ])
    models = {item["name"]: item for item in adapter.discover()}
    assert models["claude-opus-test"]["account_ids"] == [1]
    assert models["claude-sonnet-test"]["account_ids"] == [1, 2]
    assert models["claude-sonnet-test"]["reasoning"] == ["low", "medium"]
