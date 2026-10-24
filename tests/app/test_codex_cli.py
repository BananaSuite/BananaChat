"""The Codex transport serves only ChatGPT subscriptions, through a CLI that may not use tools.

Every test drives a fake ``codex`` executable (tests/app/fake_codex.py) as a real
subprocess; nothing reaches OpenAI and no credential exists.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bananachat.db import settings
from bananachat.db import subscription_pool as store
from bananachat.services import codex_usage, subscription_pool
from bananachat.services.codex_cli import (BINDINGS_KEY, DISABLED_FEATURES, Adapter, ConnectorError,
                                           NativeAuthenticationError)
from bananachat.services.upstream import Cancelled, CancelToken

FAKE = Path(__file__).with_name("fake_codex.py")
MODEL = {"name": "gpt-6.1-sol", "reasoning": ["low", "medium", "high", "xhigh"]}
USAGE = {"input_tokens": 120, "cached_input_tokens": 100, "cache_write_input_tokens": 0, "output_tokens": 30,
         "reasoning_output_tokens": 12}
LIMIT = "You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits " \
        "or try again at 4:05 PM."


def _item(kind, text="", event="item.completed"):
    return {"type": event, "item": {"id": "item_0", "type": kind, "text": text}}


def _efforts(*levels):
    return [{"reasoningEffort": level, "description": ""} for level in levels]


def _model(slug, *levels, hidden=False):
    return {"id": slug, "model": slug, "displayName": slug.upper(), "description": "A test model.",
            "hidden": hidden, "supportedReasoningEfforts": _efforts(*levels), "defaultReasoningEffort": "medium",
            "inputModalities": ["text", "image"], "isDefault": False}


def _profile(tmp_path, name):
    home = tmp_path / name
    home.mkdir()
    home.chmod(0o700)
    return home


def _scenario(home, **scenario):
    (home / "scenario.json").write_text(json.dumps(scenario))


def _calls(home, kind=None):
    path = home / "calls.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [entry for entry in entries if kind is None or entry["kind"] == kind]


@pytest.fixture
def connector(tmp_path):
    home = _profile(tmp_path, "home")
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable} -I\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
    binary.chmod(0o700)
    manifest = tmp_path / "connector.json"
    manifest.write_text(json.dumps({"binary": str(binary), "profiles": {
        "primary": {"codex_home": str(home), "usage_source": "local", "models": [MODEL]}}}))
    manifest.chmod(0o600)
    adapter = Adapter(SimpleNamespace(environment="testing", codex_cli_config=str(manifest)))
    # Unit subprocess tests avoid a database, while integration tests use the real bindings.
    adapter.bindings = lambda: {"1": "primary"}
    return adapter, home, manifest


def _reload(manifest, **profiles):
    data = json.loads(manifest.read_text())
    data["profiles"].update(profiles)
    manifest.write_text(json.dumps(data))
    return Adapter(SimpleNamespace(environment="testing", codex_cli_config=str(manifest)))


def _run(connector, scenario, *, effort="extra", **kwargs):
    adapter, home, _ = connector
    _scenario(home, **scenario)
    return list(adapter.chat({"id": 1, "label": "primary"}, MODEL["name"],
                             [{"role": "system", "content": "Be concise."},
                              {"role": "user", "content": "hello $(touch hacked)"}],
                             {"effort": effort} if effort else {}, **kwargs))


def _answer(*events):
    return {"events": [{"type": "thread.started", "thread_id": "thread-1"}, {"type": "turn.started"},
                       *events, {"type": "turn.completed", "usage": USAGE}]}


def test_chatgpt_login_is_accepted_and_api_key_login_refused(connector):
    adapter, home, _ = connector
    assert adapter.check_profile("primary") == {"authenticated": True, "plan": "plus"}
    for account in ({"type": "apiKey"}, {"type": "amazonBedrock", "usesCodexManagedCredentials": False}, None):
        _scenario(home, account=account)
        with pytest.raises(NativeAuthenticationError, match="ChatGPT subscription"):
            adapter.check_profile("primary")
    _scenario(home, account_error=True)
    with pytest.raises(ConnectorError) as caught:
        adapter.check_profile("primary")
    # An unreadable status is not proof of a logout, and provider text never reaches the message.
    assert not isinstance(caught.value, NativeAuthenticationError)
    assert "sk-must-not-leak" not in str(caught.value)


def test_app_server_messages_are_plain_json_lines_that_skip_notifications(connector):
    adapter, home, _ = connector
    _scenario(home, notify=True)
    adapter.check_profile("primary")
    start = _calls(home, "start")[-1]
    assert start["args"] == ["app-server", "--listen", "stdio://"]
    messages = [entry["message"] for entry in _calls(home, "message")]
    assert [message.get("method") for message in messages] == ["initialize", "initialized", "account/read"]
    assert all("jsonrpc" not in message for message in messages)
    assert messages[0]["params"]["clientInfo"]["name"] == "bananachat"
    assert "id" not in messages[1] and "params" not in messages[1]
    assert messages[2]["id"] == 2 and messages[2]["params"] == {}


@pytest.mark.parametrize("scenario", [{"server_request": True}, {"wrong_id": True}])
def test_unexpected_app_server_messages_are_not_taken_as_a_login(connector, scenario):
    adapter, home, _ = connector
    _scenario(home, **scenario)
    with pytest.raises(ConnectorError) as caught:
        adapter.check_profile("primary")
    assert not isinstance(caught.value, NativeAuthenticationError)


def test_answer_yields_thinking_text_and_exactly_one_terminal_record(connector):
    chunks = _run(connector, _answer(_item("reasoning", "Weighing it."), _item("agent_message", "Hello there.")))
    assert chunks == [{"thinking": "Weighing it."}, {"text": "Hello there."},
                      {"tokens_in": 120, "tokens_out": 30, "done": True, "finish_reason": "stop"}]
    invocation = _calls(connector[1], "exec")[-1]
    assert "$(touch hacked)" in invocation["input"] and "Reply only to the final user message" in invocation["input"]
    assert invocation["system"] == "Be concise."
    args = _calls(connector[1], "start")[-1]["args"]
    assert args[0] == "exec" and args[-1] == "-"
    assert not any("hello" in value or "Be concise" in value for value in args)
    assert args[args.index("--model") + 1] == MODEL["name"]
    assert 'model_reasoning_effort="xhigh"' in args
    assert not os.path.exists(_calls(connector[1], "start")[-1]["cwd"])
    assert not (connector[1] / "hacked").exists()


def test_answer_runs_with_every_configurable_tool_switched_off(connector):
    _run(connector, _answer(_item("agent_message", "Hi")), effort=None)
    args = _calls(connector[1], "start")[-1]["args"]
    disabled = {args[index + 1] for index, value in enumerate(args) if value == "--disable"}
    assert disabled == set(DISABLED_FEATURES)
    assert {"shell_tool", "unified_exec", "view_image", "image_generation", "apps", "plugins",
            "multi_agent"} <= disabled
    settings_given = {args[index + 1] for index, value in enumerate(args) if value == "--config"}
    assert {'approval_policy="never"', 'web_search="disabled"', "agents.enabled=false",
            'forced_login_method="chatgpt"', 'model_provider="openai"', "cloud.skills.enabled=false",
            "tools.experimental_request_user_input.enabled=false"} <= settings_given
    assert not any(value.startswith("model_reasoning_effort") for value in settings_given)
    assert args[args.index("--sandbox") + 1] == "read-only"
    # Strict configuration makes a renamed override key stop the run rather than be ignored.
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config", "--json",
            "--skip-git-repo-check"} <= set(args)


def test_an_unsupported_effort_is_refused_before_codex_starts(connector):
    with pytest.raises(subscription_pool.RequestRejected, match="effort"):
        _run(connector, _answer(_item("agent_message", "Hi")), effort="max")
    assert not _calls(connector[1])


def test_pre_cancelled_request_does_not_start_codex(connector):
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        _run(connector, {"hang": True}, cancel=cancel)
    assert not _calls(connector[1])


def test_system_prompt_shares_the_input_size_bound(connector):
    with pytest.raises(subscription_pool.RequestRejected, match="input limit"):
        list(connector[0].chat({"id": 1, "label": "primary"}, MODEL["name"],
                               [{"role": "system", "content": "x" * (1024 * 1024)},
                                {"role": "user", "content": "hello"}], {}))
    assert not _calls(connector[1])


@pytest.mark.parametrize("model", [
    {"name": "gpt-6.1-sol", "reasoning": ["minimal", "low"]},
    {"name": "gpt-6.1-sol", "capabilities": ["completion", "vision"]},
    {"name": "codex-auto-review"},
])
def test_manual_models_must_be_text_models_bananachat_can_express(connector, model):
    _, home, manifest = connector
    with pytest.raises(ConnectorError, match="model configuration"):
        _reload(manifest, primary={"codex_home": str(home), "models": [model]})


def test_child_environment_carries_no_api_keys_or_codex_overrides(connector, monkeypatch, warnings_log):
    for key, value in {"OPENAI_API_KEY": "sk-secret", "CODEX_API_KEY": "sk-secret", "CODEX_ACCESS_TOKEN": "token",
                       "OPENAI_BASE_URL": "https://untrusted.invalid", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "x",
                       "CODEX_CA_CERTIFICATE": "/tmp/ca.pem", "RUST_LOG": "trace",
                       "HTTPS_PROXY": "http://approved-proxy.test:8080", "NO_PROXY": "localhost"}.items():
        monkeypatch.setenv(key, value)
    chunks = _run(connector, {**_answer(_item("agent_message", "Hi")), "stderr": True})
    # Provider stderr is drained but never forwarded or logged.
    assert "sk-credential-must-not-leak" not in json.dumps(chunks) + warnings_log.text
    starts = _calls(connector[1], "start")
    assert [start["args"][0] for start in starts] == ["app-server", "exec"]
    for start in starts:
        env = start["env"]
        assert not [key for key in env if key.startswith(("OPENAI_", "CODEX_")) and key != "CODEX_HOME"]
        assert "RUST_LOG" not in env
        assert env["CODEX_HOME"] == env["HOME"] == str(connector[1])
        assert env["HTTPS_PROXY"] == "http://approved-proxy.test:8080" and env["NO_PROXY"] == "localhost"


@pytest.mark.parametrize("record", [
    {"type": "turn.failed", "error": {"message": LIMIT}},
    {"type": "turn.failed", "error": {"message": "You’ve hit your usage limit for GPT-6-Astra. Switch to "
                                                 "another model now, or try again later."}},
    {"type": "error", "message": "Your workspace is out of credits. Add credits to continue."},
    {"type": "error", "message": "You hit your spend cap set in your workspace. Increase your spend cap "
                                 "to continue."},
])
def test_usage_limit_failures_raise_quota_exhausted(connector, record):
    with pytest.raises(subscription_pool.QuotaExhausted):
        _run(connector, {"events": [{"type": "turn.started"}, record]})


def test_transient_stream_errors_do_not_end_the_turn(connector):
    chunks = _run(connector, _answer({"type": "error", "message": "Reconnecting... 1/5"},
                                     _item("agent_message", "Recovered.")))
    assert chunks[0] == {"text": "Recovered."} and chunks[-1]["done"]


@pytest.mark.parametrize("event", ["item.started", "item.completed"])
@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search",
                                  "collab_tool_call", "todo_list", "image_view"])
def test_a_tool_item_stops_the_answer_and_its_process(connector, kind, event):
    adapter, home, _ = connector
    _scenario(home, hang=True, events=[_item("agent_message", "Let me look."), _item(kind, event=event)])
    started = time.monotonic()
    observed = []
    with pytest.raises(ConnectorError, match="tool"):
        for chunk in adapter.chat({"id": 1, "label": "primary"}, MODEL["name"],
                                  [{"role": "user", "content": "cat ~/.codex/auth.json"}], {}):
            observed.append(chunk)
    # The fake keeps running for a minute; only killing its process group returns this quickly.
    assert time.monotonic() - started < 10
    assert not any(chunk.get("done") for chunk in observed)
    with pytest.raises(ProcessLookupError):
        os.kill(int((home / "pid").read_text()), 0)


@pytest.mark.parametrize("scenario", [
    {"events": [_item("agent_message", "Hello")]},
    {**_answer(_item("agent_message", "Hello")), "exit": 1},
    {"events": [{"type": "turn.failed", "error": {"message": "stream disconnected"}}]},
    {"events": [{"type": "turn.completed", "usage": {**USAGE, "input_tokens": -1}}]},
    {"events": [{"type": "turn.completed", "usage": {"output_tokens": 3}}]},
    {"events": [{"type": "turn.completed", "usage": USAGE}, _item("agent_message", "too late")]},
    {"events": [{"type": "session.configured"}]},
    {"account": {"type": "apiKey"}, **_answer(_item("agent_message", "Hello"))},
])
def test_failures_never_emit_done(connector, scenario):
    adapter, home, _ = connector
    _scenario(home, **scenario)
    observed = []
    with pytest.raises(ConnectorError):
        for chunk in adapter.chat({"id": 1, "label": "primary"}, MODEL["name"], [], {}, cancel=CancelToken()):
            observed.append(chunk)
    assert not any(chunk.get("done") for chunk in observed)


def _cancel_once(path, cancel):
    def watch():
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.cancel()

    threading.Thread(target=watch, daemon=True).start()


def test_cancel_stops_the_real_process_before_returning(connector):
    cancel = CancelToken()
    _cancel_once(connector[1] / "pid", cancel)
    started = time.monotonic()
    with pytest.raises(Cancelled):
        _run(connector, {"hang": True}, cancel=cancel)
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int((connector[1] / "pid").read_text()), 0)


@pytest.mark.parametrize("child", [True, "own_group"])
def test_cancellation_stops_every_descendant_in_the_session(connector, child):
    cancel = CancelToken()
    _cancel_once(connector[1] / "child-pid", cancel)
    with pytest.raises(Cancelled):
        _run(connector, {"child": child, "hang": True}, cancel=cancel)
    status = Path(f"/proc/{int((connector[1] / 'child-pid').read_text())}/stat")
    # An orphan may briefly await init's reaper; it must not remain running.
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_timeout_stops_the_process(connector):
    connector[0].timeout = 1
    with pytest.raises(ConnectorError, match="timeout"):
        _run(connector, {"hang": True})
    with pytest.raises(ProcessLookupError):
        os.kill(int((connector[1] / "pid").read_text()), 0)


def test_private_config_and_separate_profiles_required(connector):
    _, home, manifest = connector
    manifest.chmod(0o644)
    with pytest.raises(ConnectorError, match="private"):
        Adapter(SimpleNamespace(environment="testing", codex_cli_config=str(manifest)))
    manifest.chmod(0o600)
    with pytest.raises(ConnectorError, match="separate"):
        _reload(manifest, duplicate={"codex_home": str(home), "models": [MODEL]})


def test_production_refuses_an_unsandboxed_executable(connector):
    with pytest.raises(ConnectorError, match="sandbox"):
        Adapter(SimpleNamespace(environment="production", codex_cli_config=str(connector[2])))


def test_a_changed_login_is_admitted_again_before_it_answers(connector, app):
    adapter, home, _ = connector
    adapter.bindings = lambda: {}
    messages = [{"role": "user", "content": "Hello"}]
    with app.app_context():
        account = {"id": store.add_account("primary", provider="codex", window_limit=1000), "label": "primary"}
        for email in ("first@example.com", "second@example.com"):
            _scenario(home, account={"type": "chatgpt", "email": email, "planType": "plus"},
                      **_answer(_item("agent_message", "Hi")))
            if email.startswith("second"):
                with pytest.raises(subscription_pool.AdmissionChanged):
                    list(adapter.chat(account, MODEL["name"], messages, {}))
            assert list(adapter.chat(account, MODEL["name"], messages, {}))[-1]["done"]
    # The replacement login answered only after the pool admitted it again.
    assert len(_calls(home, "exec")) == 2


def test_text_only_rejects_unsupported_content(connector):
    with pytest.raises(subscription_pool.RequestRejected, match="text messages"):
        list(connector[0].chat({"id": 1, "label": "primary"}, MODEL["name"],
                               [{"role": "user", "content": [{"type": "image"}]}], {}))
    assert not _calls(connector[1])


def test_discovery_skips_hidden_models_maps_efforts_and_follows_pages(connector, app):
    _, home, manifest = connector
    adapter = _reload(manifest, primary={"codex_home": str(home), "usage_source": "local"})
    _scenario(home, page_size=2, models=[
        _model("gpt-6-astra", "low", "medium", "high", "xhigh", "max", "ultra"),
        _model("codex-auto-review", "low", "medium", hidden=True),
        _model("gpt-6-luna", "minimal", "low", "medium", "xhigh"),
        _model("gpt-daybreak-red-latest", "low", hidden=True),
        _model("gpt-5.5", "none", "low", "medium", "high", "xhigh"),
    ])
    with app.app_context():
        account_id = store.add_account("Codex one", provider="codex", window_limit=1000)
        settings.state_set(BINDINGS_KEY, {str(account_id): "primary"})
        models = adapter.discover()
    assert [(m["name"], m["family"], m["reasoning"], m["capabilities"], m["account_ids"]) for m in models] == [
        ("gpt-6-astra", "astra", ["low", "medium", "high", "extra", "max"], ["completion"], [account_id]),
        ("gpt-6-luna", "luna", ["low", "medium", "extra"], ["completion"], [account_id]),
        ("gpt-5.5", "gpt", ["low", "medium", "high", "extra"], ["completion"], [account_id]),
    ]
    pages = [entry["message"]["params"] for entry in _calls(home, "message")
             if entry["message"].get("method") == "model/list"]
    assert pages == [{"limit": 100}, {"limit": 100, "cursor": "2"}, {"limit": 100, "cursor": "4"}]
    assert all(subscription_pool._model_descriptor("codex", model) for model in models)


def test_invalidated_models_are_discovered_again(connector, app):
    _, home, manifest = connector
    adapter = _reload(manifest, primary={"codex_home": str(home), "usage_source": "local"})
    _scenario(home, models=[_model("gpt-6-luna", "low", "medium")])

    def listed():
        return sum(entry["message"].get("method") == "model/list" for entry in _calls(home, "message"))

    with app.app_context():
        account_id = store.add_account("Codex one", provider="codex", window_limit=1000)
        settings.state_set(BINDINGS_KEY, {str(account_id): "primary"})
        adapter.discover()
        adapter.discover()
        assert listed() == 1
        adapter.invalidate_models()
        assert adapter.discover()[0]["name"] == "gpt-6-luna"
    assert listed() == 2


def test_rate_limits_are_read_in_camel_case_by_window_length():
    now = time.time()
    value = {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "planType": "promax",
            # The weekly window comes first here: position must not decide its meaning.
            "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": now + 86400,
                        "reset_at": now + 60},
            "secondary": {"usedPercent": 25, "windowDurationMins": 299, "resetsAt": now + 3600,
                          "resets_at": now + 30},
        },
        "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 99, "windowDurationMins": 300, "resetsAt": now + 60}},
            "codex_bengalfox": {"limitName": "GPT-6-Astra", "normalModelSlug": "gpt-6-astra",
                                "primary": {"usedPercent": 90, "windowDurationMins": 300, "resetsAt": now + 1800}},
            "codex_other": {"limitName": "codex_other",
                            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": now + 1800}},
        },
    }
    report = codex_usage.parse(value, observed_at=now, now=now)
    assert report["available"] is True and report["source"] == "codex"
    assert report["window_left"] == 0.75 and report["window_resets_at"] == now + 3600
    assert report["weekly_left"] == 0.6 and report["weekly_resets_at"] == now + 86400
    assert report["model_limits"] == {"astra": {"left": pytest.approx(0.1), "resets_at": now + 1800}}
    assert report["subscription_type"] == "pro" and report["plan_label"] == "ChatGPT Pro (Max)"
    stored = subscription_pool._validated_report("codex", report)
    assert stored["plan_label"] == "ChatGPT Pro (Max)" and stored["model_limits"]["astra"]["left"] == pytest.approx(0.1)


@pytest.mark.parametrize("change", [
    {"primary": {"usedPercent": 10, "windowDurationMins": 1440, "resetsAt": None}},
    {"primary": {"usedPercent": 10, "windowDurationMins": None, "resetsAt": None}},
    {"spendControlReached": True},
    {"rateLimitReachedType": "workspace_member_credits_depleted"},
])
def test_rate_limits_never_establish_a_five_hour_allowance_they_did_not_report(change):
    now = time.time()
    snapshot = {"planType": "plus", "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": now + 60},
                **change}
    report = codex_usage.parse({"rateLimits": snapshot}, observed_at=now, now=now)
    assert report["available"] is False
    assert codex_usage.parse({"rateLimits": snapshot, "ordinaryUsageAllowed": False}, observed_at=now,
                             now=now)["available"] is False


@pytest.mark.parametrize("plan, expected, label", [
    ("plus", "plus", None), ("prolite", "pro", None), ("pro", "pro", "ChatGPT Pro (More)"),
    ("self_serve_business_usage_based", "business", None), ("enterprise_cbp_automation", "enterprise", None),
    ("edu_plus", "edu", None), ("something_new", None, None),
])
def test_plans_are_folded_into_the_names_bananachat_shows(plan, expected, label):
    now = time.time()
    report = codex_usage.parse({"rateLimits": {"planType": plan, "primary": {
        "usedPercent": 0, "windowDurationMins": 300, "resetsAt": None}}}, observed_at=now, now=now)
    assert (report["subscription_type"], report["plan_label"]) == (expected, label)


def test_live_rate_limits_reach_the_account_report(connector, app):
    _, home, manifest = connector
    adapter = _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    reset = int(time.time()) + 3600
    _scenario(home, rate_limits={"rateLimits": {"planType": "plus", "primary": {
        "usedPercent": 30, "windowDurationMins": 300, "resetsAt": reset}}})
    with app.app_context():
        account_id = store.add_account("Codex one", provider="codex", window_limit=1000)
        settings.state_set(BINDINGS_KEY, {str(account_id): "primary"})
        report = adapter.quota()["account_reports"][str(account_id)]
    assert report["available"] is True and report["window_left"] == 0.7 and report["window_resets_at"] == reset
    request = next(entry["message"] for entry in _calls(home, "message")
                   if entry["message"].get("method") == "account/rateLimits/read")
    assert request["params"] == {"excludeResetCreditDetails": True}


@pytest.mark.parametrize("scenario", [{"rate_limits_error": True}, {"rate_limits": {"accountId": "workspace-1"}},
                                      {"account": {"type": "apiKey"}}])
def test_unreadable_rate_limits_fail_closed(connector, app, scenario):
    _, home, manifest = connector
    adapter = _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    _scenario(home, **scenario)
    with app.app_context():
        account_id = store.add_account("Codex one", provider="codex", window_limit=1000)
        settings.state_set(BINDINGS_KEY, {str(account_id): "primary"})
        report = adapter.quota()["account_reports"][str(account_id)]
    assert report == {"source": "codex", "available": False}


def test_only_codex_accounts_are_probed_or_offered(connector, app):
    adapter, home, manifest = connector
    adapter = _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    with app.app_context():
        claude_id = store.add_account("primary", provider="claude", window_limit=1000)
        codex_id = store.add_account("Codex one", provider="codex", window_limit=1000)
        # Even a stray binding of a Claude account to this profile must not make it a Codex account.
        settings.state_set(BINDINGS_KEY, {str(claude_id): "primary", str(codex_id): "primary"})
        reports = adapter.quota()["account_reports"]
        models = adapter.discover()
        with pytest.raises(ConnectorError, match="another subscription provider"):
            list(adapter.chat(store.get(claude_id), MODEL["name"], [{"role": "user", "content": "Hello"}], {}))
    assert set(reports) == {str(codex_id)}
    assert models[0]["account_ids"] == [codex_id]
    assert not _calls(home, "exec")


def test_two_profiles_on_one_subscription_are_withheld(connector, app, tmp_path):
    adapter, home, manifest = connector
    other = _profile(tmp_path, "other")
    adapter = _reload(manifest, primary={"codex_home": str(home), "usage_source": "local", "models": [MODEL]},
                      other={"codex_home": str(other), "usage_source": "local", "models": [MODEL]})
    for profile in (home, other):
        _scenario(profile, account={"type": "chatgpt", "email": "Same@Example.com ", "planType": "plus"})
    with app.app_context():
        first = store.add_account("Codex one", provider="codex", window_limit=1000)
        second = store.add_account("Codex two", provider="codex", window_limit=1000)
        settings.state_set(BINDINGS_KEY, {str(first): "primary", str(second): "other"})
        reports = adapter.usage_reports()
        assert reports == {str(first): {"source": "codex", "available": False},
                           str(second): {"source": "codex", "available": False}}
        with pytest.raises(ConnectorError, match="discovery"):
            adapter.discover()
        with pytest.raises(ConnectorError, match="same subscription"):
            list(adapter.chat({"id": first, "label": "Codex one"}, MODEL["name"],
                              [{"role": "user", "content": "Hello"}], {}))
    assert not _calls(home, "exec")


def test_built_in_extension_serves_the_codex_pool(make_app, connector):
    adapter, home, manifest = connector
    _reload(manifest, primary={"codex_home": str(home), "models": [MODEL]})
    reset = int(time.time()) + 3600
    _scenario(home, rate_limits={"rateLimits": {"planType": "plus", "primary": {
        "usedPercent": 10, "windowDurationMins": 300, "resetsAt": reset}}}, **_answer(_item("agent_message", "Hi")))
    app = make_app(CODEX_EXTENSION="bananachat.services.codex_cli", CODEX_CLI_CONFIG=str(manifest))
    try:
        assert app.extensions["codex_transport"]["configured"]
        adapter = app.extensions["codex_adapter"]
        assert (adapter.provider, adapter.bindings_key, sorted(adapter.profiles)) == \
            ("codex", "codex_cli_profiles", ["primary"])
        with app.app_context():
            account_id = store.add_account("primary", provider="codex", window_limit=1000)
            subscription_pool.refresh_quota("codex")
            chunks = list(subscription_pool.chat_stream("codex", MODEL["name"], [{"role": "user", "content": "Hi"}],
                                                        options={"effort": "extra"}))
            assert chunks[-1]["done"] and chunks[-1]["tokens_in"] == 120
            assert store.get(account_id)["window_used"] == 150
            assert not store.leased(account_id)
    finally:
        subscription_pool.reset_transport()
