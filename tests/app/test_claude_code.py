"""Real subprocess contract tests without subscription credentials or billing."""
import json
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from bananachat.db import claude_pool as store
from bananachat.db import settings
from bananachat.services import claude_pool
from bananachat.services.claude_code import Adapter, BINDINGS_KEY, ConnectorError
from bananachat.services.upstream import Cancelled, CancelToken

MODEL = {"name": "claude-test-sonnet", "family": "sonnet", "reasoning": ["low", "medium", "high", "xhigh"]}
FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['HOME'])
scenario = json.loads((home/'scenario.json').read_text()) if (home/'scenario.json').exists() else {}
if sys.argv[1:3] == ['auth', 'status']:
 print(json.dumps({'loggedIn': True, 'authMethod': scenario.get('auth', 'claude.ai')}, indent=2))
 sys.exit(0)
data = sys.stdin.read()
(home/'invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ), 'input':data, 'cwd':os.getcwd()}))
if scenario.get('child'):
 child = os.fork()
 if child == 0:
  time.sleep(60)
  sys.exit(0)
 (home/'child-pid').write_text(str(child))
if scenario.get('hang'):
 (home/'pid').write_text(str(os.getpid()))
 time.sleep(60)
if scenario.get('stderr'):
 sys.stderr.write('credential-must-not-leak')
for record in scenario.get('records', []):
 print(json.dumps(record), flush=True)
 if scenario.get('delay'): time.sleep(scenario['delay'])
sys.exit(scenario.get('exit', 0))
'''


def _terminal(**overrides):
    return {"type": "result", "subtype": "success", "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 3, "cache_creation_input_tokens": 2,
                      "cache_read_input_tokens": 5}, **overrides}


def _delta(text, kind="text"):
    return {"type": "stream_event", "event": {"type": "content_block_delta",
            "delta": {"type": kind + "_delta", kind: text}}}


@pytest.fixture
def connector(tmp_path):
    home = tmp_path / "home"
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    home.chmod(0o700)
    config_dir.chmod(0o700)
    binary = tmp_path / "claude"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    manifest = tmp_path / "connector.json"
    manifest.write_text(json.dumps({"binary": str(binary), "profiles": {
        "primary": {"home": str(home), "config_dir": str(config_dir), "usage_source": "local", "models": [MODEL]}}}))
    manifest.chmod(0o600)
    adapter = Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))
    # Unit subprocess tests avoid a database, while integration tests use the real bindings.
    adapter.bindings = lambda: {"1": "primary"}
    return adapter, home, manifest


def _run(connector, scenario, **kwargs):
    adapter, home, _ = connector
    (home / "scenario.json").write_text(json.dumps(scenario))
    return list(adapter.chat({"id": 1, "label": "primary"}, MODEL["name"],
                             [{"role": "system", "content": "Be concise."},
                              {"role": "user", "content": "hello $(touch hacked)"}],
                             {"effort": "extra"}, **kwargs))


def test_stream_tokens_auth_isolation_and_no_duplicate_text(connector, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://approved-proxy.test:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-override")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    records = [_delta("thinking", "thinking"), _delta("Hello"),
               {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}, _terminal()]
    chunks = _run(connector, {"records": records, "stderr": True})
    assert chunks == [{"thinking": "thinking"}, {"text": "Hello"},
                      {"tokens_in": 17, "tokens_out": 3, "done": True}]
    invocation = json.loads((connector[1] / "invocation.json").read_text())
    assert all(key not in invocation["env"] for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"))
    assert invocation["env"]["HTTPS_PROXY"] == "http://approved-proxy.test:8080"
    assert invocation["env"]["NO_PROXY"] == "localhost,127.0.0.1"
    args = invocation["args"]
    assert args[args.index("--effort") + 1] == "xhigh"
    assert args[args.index("--tools") + 1] == ""
    assert "--safe-mode" in args and "--no-session-persistence" in args and "--strict-mcp-config" in args
    assert "$(touch hacked)" in invocation["input"]
    assert not os.path.exists(invocation["cwd"])
    assert not (connector[1] / "hacked").exists()


@pytest.mark.parametrize("scenario", [
    {"records": [_delta("Hello")]},
    {"records": [_terminal()], "exit": 1},
    {"records": [_terminal(is_error=True)]},
    {"records": [_terminal(usage={"input_tokens": -1, "output_tokens": 3})]},
    {"records": [_terminal(usage={})]},
    {"records": [_terminal(), _delta("too late")]},
    {"auth": "api_key", "records": [_terminal()]},
])
def test_failures_never_emit_done(connector, scenario):
    adapter, home, _ = connector
    (home / "scenario.json").write_text(json.dumps(scenario))
    observed = []
    with pytest.raises(ConnectorError):
        for chunk in adapter.chat({"id": 1, "label": "primary"}, MODEL["name"], [], {}, cancel=CancelToken()):
            observed.append(chunk)
    assert not any(chunk.get("done") for chunk in observed)


def test_rate_rejection_preserves_provider_reset(connector):
    reset = time.time() + 300
    with pytest.raises(claude_pool.QuotaExhausted) as caught:
        _run(connector, {"records": [{"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": reset}}]})
    assert abs(caught.value.resets_at.timestamp() - reset) < 1


def test_cancel_stops_real_process_before_returning(connector):
    cancel = CancelToken()
    timer = threading.Timer(0.4, cancel.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            _run(connector, {"hang": True}, cancel=cancel)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    pid = int((connector[1] / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_stops_process(connector):
    connector[0].timeout = 0.3
    with pytest.raises(ConnectorError, match="timeout"):
        _run(connector, {"hang": True})
    pid = int((connector[1] / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_private_config_and_duplicate_auth_profiles_required(connector):
    _, _, manifest = connector
    manifest.chmod(0o644)
    with pytest.raises(ConnectorError, match="private"):
        Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))
    manifest.chmod(0o600)
    data = json.loads(manifest.read_text())
    data["profiles"]["duplicate"] = data["profiles"]["primary"]
    manifest.write_text(json.dumps(data))
    with pytest.raises(ConnectorError, match="separate"):
        Adapter(SimpleNamespace(environment="testing", claude_code_config=str(manifest)))


def test_text_only_rejects_unsupported_content(connector):
    with pytest.raises(ConnectorError, match="text messages"):
        list(connector[0].chat({"id": 1, "label": "primary"}, MODEL["name"],
                              [{"role": "user", "content": [{"type": "image"}]}], {}))


def test_built_in_extension_admin_bindings_and_duplicate_rejection(make_app, connector):
    from tests.app.fixtures import Browser
    app = make_app(CLAUDE_EXTENSION="bananachat.services.claude_code", CLAUDE_CODE_CONFIG=str(connector[2]))
    browser = Browser(app)
    browser.login("admin")
    assert app.extensions["claude_transport"]["configured"]
    response = browser.post("/admin/models/claude/accounts", {
        "label": "Subscription one", "profile": "primary", "window_limit": "1000", "priority": "0"})
    assert response.status_code == 302
    with app.app_context():
        row = store.get_by_label("Subscription one")
        assert settings.state_get(BINDINGS_KEY)[str(row["id"])] == "primary"
        models = app.extensions["claude_adapter"].discover()
        assert models[0]["account_ids"] == [row["id"]]
    browser.post("/admin/models/claude/accounts", {
        "label": "Duplicate", "profile": "primary", "window_limit": "1000", "priority": "0"})
    with app.app_context():
        assert store.get_by_label("Duplicate") is None
    page = browser.get("/admin/models/claude")
    assert page.status_code == 200
    assert b"Check login" in page.data and b"Automatic profiles discover models" in page.data
    assert browser.post(f'/admin/models/claude/accounts/{row["id"]}/check').status_code == 302


def test_stale_telemetry_fails_closed(connector, app):
    adapter, home, _ = connector
    telemetry = home / "usage.json"
    telemetry.write_text(json.dumps({"observed_at": time.time() - 1000, "window_left": 0.8}))
    telemetry.chmod(0o600)
    adapter.profiles["primary"]["telemetry"] = str(telemetry)
    with app.app_context():
        store.add_account("primary", window_limit=1000)
        assert adapter.quota()["account_reports"]["1"]["available"] is False


def test_pool_records_real_connector_usage_and_releases_lease(make_app, connector):
    from bananachat import db
    adapter, home, manifest = connector
    (home / "scenario.json").write_text(json.dumps({"records": [_delta("Hello"), _terminal()]}))
    app = make_app(CLAUDE_EXTENSION="bananachat.services.claude_code", CLAUDE_CODE_CONFIG=str(manifest))
    with app.app_context():
        account_id = store.add_account("primary", window_limit=1000)
        chunks = list(claude_pool.chat_stream(MODEL["name"], [{"role": "user", "content": "Hello"}],
                                             options={"effort": "medium"}))
        assert chunks[-1]["done"] and chunks[-1]["tokens_in"] == 17
        assert store.get(account_id)["window_used"] == 20
        assert not store.leased(account_id)
        assert db.scalar("SELECT COUNT(*) FROM claude_account_usage") == 1


def test_admin_cannot_rebind_leased_profile_but_can_disable(make_app, connector):
    from tests.app.fixtures import Browser
    _, home, manifest = connector
    other_home = home.parent / "other"
    other_config = other_home / "config"
    other_config.mkdir(parents=True)
    other_home.chmod(0o700)
    other_config.chmod(0o700)
    data = json.loads(manifest.read_text())
    data["profiles"]["other"] = {"home": str(other_home), "config_dir": str(other_config), "models": [MODEL]}
    manifest.write_text(json.dumps(data))
    app = make_app(CLAUDE_EXTENSION="bananachat.services.claude_code", CLAUDE_CODE_CONFIG=str(manifest))
    browser = Browser(app)
    browser.login("admin")
    with app.app_context():
        account_id = store.add_account("primary", window_limit=1000)
        assert store.claim(account_id, "test-lease")
    url = f"/admin/models/claude/accounts/{account_id}"
    form = {"profile": "other", "status": "active", "priority": "0", "window_limit": "1000"}
    browser.post(url, form)
    with app.app_context():
        assert settings.state_get(BINDINGS_KEY, {}) == {}
    form.update(profile="primary", status="disabled")
    browser.post(url, form)
    with app.app_context():
        assert store.get(account_id)["status"] == "disabled"
        store.release(account_id, "test-lease")


def test_pre_cancelled_request_does_not_start_client(connector):
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        _run(connector, {"hang": True}, cancel=cancel)
    assert not (connector[1] / "pid").exists()


def test_telemetry_reduces_capacity_and_reset_invalidates_it(connector, app):
    adapter, home, _ = connector
    telemetry = home / "usage.json"
    report = {"observed_at": time.time(), "window_left": 0.25, "window_resets_at": time.time() + 300}
    telemetry.write_text(json.dumps(report))
    telemetry.chmod(0o600)
    adapter.profiles["primary"]["telemetry"] = str(telemetry)
    with app.app_context():
        store.add_account("primary", window_limit=1000)
        report = adapter.quota()["account_reports"]["1"]
        assert report["window_left"] == 0.25 and report["weekly_left"] is None
        report["window_resets_at"] = time.time() - 1
        telemetry.write_text(json.dumps(report))
        assert adapter.quota()["account_reports"]["1"]["available"] is False


def test_cancellation_stops_child_process_group(connector):
    cancel = CancelToken()
    timer = threading.Timer(0.5, cancel.cancel)
    timer.start()
    try:
        with pytest.raises(Cancelled):
            _run(connector, {"child": True, "hang": True}, cancel=cancel)
    finally:
        timer.cancel()
    child = int((connector[1] / "child-pid").read_text())
    from pathlib import Path
    status = Path(f"/proc/{child}/stat")
    # An orphan may briefly await init's reaper; it must not remain running.
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_system_prompt_shares_input_size_bound(connector):
    with pytest.raises(ConnectorError, match="input limit"):
        list(connector[0].chat({"id": 1, "label": "primary"}, MODEL["name"],
                              [{"role": "system", "content": "x" * (1024 * 1024)},
                               {"role": "user", "content": "hello"}], {}))
    assert not (connector[1] / "invocation.json").exists()
