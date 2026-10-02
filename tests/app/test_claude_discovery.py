"""SDK metadata discovery uses no user prompt and no subscription credentials."""
import json
import os
import threading
import time

import pytest

from bananachat.services import claude_control, claude_discovery
from bananachat.services.claude_code import Adapter, ConnectorError
from bananachat.services.upstream import Cancelled, CancelToken

CATALOG = [
    {"value": "default", "resolvedModel": "claude-opus-test", "displayName": "Default", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "opus", "resolvedModel": "claude-opus-test", "displayName": "Opus", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh"]},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-test", "displayName": "Sonnet", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high"]},
    {"value": "haiku", "resolvedModel": "claude-haiku-test", "displayName": "Haiku"},
]
FAKE = '''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
home=Path(os.environ['HOME'])
scenario=json.loads((home/'scenario.json').read_text())
data=sys.stdin.read()
records=[json.loads(line) for line in data.splitlines()]
(home/'invocation.json').write_text(json.dumps({'args':sys.argv[1:],'requests':records,'cwd':os.getcwd()}))
if scenario.get('hang'):
 (home/'pid').write_text(str(os.getpid()))
 time.sleep(60)
if scenario.get('stderr'): sys.stderr.write('private-credential-must-not-leak')
for record in records:
 info={'models':scenario.get('models',[]),'account':{'email':'private@account.test'},'commands':['private-command']}
 if record['request']['subtype']=='get_usage':
  info={'subscription_type':'max','rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':10}},'identity':'private'}
 reply={'type':'control_response','response':{'subtype':'success','request_id':record['request_id'],'response':info}}
 if scenario.get('wrong_id'): reply['response']['request_id']='unmatched'
 if scenario.get('error'): reply['response']={'subtype':'error','request_id':record['request_id'],'error':'credential-secret'}
 if scenario.get('kind'): reply={'type':scenario['kind'],'message':'credential-secret'}
 if not scenario.get('missing'): print(json.dumps(reply),flush=True)
 if scenario.get('duplicate'): print(json.dumps(reply),flush=True)
sys.exit(scenario.get('exit',0))
'''


@pytest.fixture
def metadata(tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    binary = tmp_path / "claude"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    adapter = Adapter.__new__(Adapter)
    adapter.binary, adapter.timeout = str(binary), 30
    profile = {"home": str(home), "config_dir": str(home)}
    (home / "scenario.json").write_text(json.dumps({"models": CATALOG}))
    return adapter, profile, home


def _scenario(metadata, **values):
    (metadata[2] / "scenario.json").write_text(json.dumps({"models": CATALOG, **values}))


def test_live_process_discovery_deduplicates_and_never_sends_prompt(metadata):
    adapter, profile, home = metadata
    _scenario(metadata, stderr=True)
    models = claude_discovery.discover(adapter, profile)
    assert [model["name"] for model in models] == ["claude-opus-test", "claude-sonnet-test", "claude-haiku-test"]
    assert models[0]["aliases"] == ["default", "opus"]
    assert models[0]["display"] == "Opus"
    assert models[0]["reasoning"] == ["low", "medium", "high", "extra"]
    assert models[2]["reasoning"] == []
    assert all(model["capabilities"] == ["completion"] for model in models)
    invocation = json.loads((home / "invocation.json").read_text())
    assert [record["type"] for record in invocation["requests"]] == ["control_request"]
    assert invocation["requests"][0]["request"] == {"subtype": "initialize", "hooks": None}
    arguments = invocation["args"]
    assert arguments[arguments.index("--tools") + 1] == ""
    assert "--safe-mode" in arguments and "--strict-mcp-config" in arguments
    assert "--no-session-persistence" in arguments
    assert not os.path.exists(invocation["cwd"])
    assert "private@account.test" not in json.dumps(models)


def test_usage_is_initialized_and_only_allowed_fields_returned(metadata):
    adapter, profile, home = metadata
    usage = claude_control.request(adapter, profile, "get_usage", fields={"skip_behaviors": True})
    assert usage == {"subscription_type": "max", "rate_limits_available": True,
                     "rate_limits": {"five_hour": {"utilization": 10}}}
    invocation = json.loads((home / "invocation.json").read_text())
    assert [value["request"]["subtype"] for value in invocation["requests"]] == ["initialize", "get_usage"]
    assert invocation["requests"][1]["request"]["skip_behaviors"] is True
    assert set(claude_control.request(adapter, profile, "initialize")) == {"models"}


@pytest.mark.parametrize("scenario", [
    {"wrong_id": True}, {"duplicate": True}, {"missing": True}, {"error": True}, {"exit": 1},
    {"kind": "assistant"}, {"kind": "control_request"}, {"kind": "result"},
])
def test_bad_protocol_never_leaks_diagnostics(metadata, scenario):
    _scenario(metadata, **scenario)
    with pytest.raises((claude_control.ControlError, ConnectorError)) as caught:
        claude_control.request(metadata[0], metadata[1], "initialize")
    assert "credential" not in str(caught.value)


@pytest.mark.parametrize("subtype,fields,timeout", [
    ("set_permission_mode", None, 30), ("initialize", {"hooks": {}}, 30),
    ("get_usage", {"skip_behaviors": False}, 30), ("get_usage", {"secret": "x"}, 30),
    ("initialize", None, float("inf")), ("initialize", None, True), ("initialize", None, 0),
])
def test_invalid_control_arguments_do_not_start_process(metadata, subtype, fields, timeout):
    with pytest.raises(claude_control.ControlError):
        claude_control.request(metadata[0], metadata[1], subtype, fields=fields, timeout=timeout)
    assert not (metadata[2] / "invocation.json").exists()


def test_timeout_and_cancellation_reap_metadata_process(metadata):
    _scenario(metadata, hang=True)
    cancel = CancelToken()
    timer = threading.Timer(0.3, cancel.cancel)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            claude_control.request(metadata[0], metadata[1], "initialize", cancel=cancel)
    finally:
        timer.cancel()
    assert time.monotonic() - start < 5
    pid = int((metadata[2] / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ConnectorError, match="timeout"):
        claude_control.request(metadata[0], metadata[1], "initialize", timeout=0.15)


@pytest.mark.parametrize("models", [
    None, [None], [{"value": "sonnet", "resolvedModel": "bad id"}],
    [{"value": "sonnet", "resolvedModel": "claude-sonnet-test", "supportsEffort": "true"}],
    [{"value": "sonnet", "resolvedModel": "claude-sonnet-test", "supportedEffortLevels": ["extreme"]}],
    [{"value": "sonnet", "resolvedModel": "claude-sonnet-test", "displayName": "Bad\nlabel"}],
    [{"value": "unknown", "resolvedModel": "claude-newfamily-test"}],
    [{"value": "sonnet", "resolvedModel": "claude-sonnet-test"},
     {"value": "sonnet", "resolvedModel": "claude-opus-test"}],
])
def test_invalid_catalog_fails_without_fabricating_capabilities(models):
    with pytest.raises(claude_discovery.DiscoveryError):
        claude_discovery.parse_models({"models": models})


def test_empty_catalog_is_valid_and_old_cli_aliases_remain_valid():
    assert claude_discovery.parse_models({"models": []}) == []
    models = claude_discovery.parse_models({"models": [{"value": "sonnet", "displayName": "Sonnet"}]})
    assert models[0]["name"] == "sonnet"
    assert models[0]["reasoning"] == []
