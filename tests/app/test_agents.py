"""Agents: tool validation, the agent loop with a fake runner and fake tool-calling model, budgets, stop,
crash recovery, swarms, quotas, capability gating, maintenance, retention, pages and administration."""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
import time
import types
import zipfile
from datetime import timedelta

import pytest

from tests.app.conftest import Browser
from tests.app.fake_runner import TOKEN, FakeRunner

FAST_SETTINGS = {"enabled": True, "max_steps": 40, "max_minutes": 20, "keep_workspace_minutes": 30,
                 "max_tasks_per_user": 1, "max_tasks_total": 4, "starts_per_hour": 100}


# ----- fixtures and helpers ----------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_loop(monkeypatch):
    from bananachat.services.agents import loop
    monkeypatch.setattr(loop, "MODEL_BACKOFF", (0.01, 0.01, 0.01))
    monkeypatch.setattr(loop, "RUNNER_BACKOFF", (0.01, 0.01, 0.01))
    monkeypatch.setattr(loop, "PAUSE_POLL_SECONDS", 0.1)
    yield
    for run in loop.local_runs():
        run.stop()
    for run in loop.local_runs():
        run.finished.wait(10)


@pytest.fixture
def runner():
    server = FakeRunner().start()
    yield server
    server.stop()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "runner.token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    return path


def configure(app, **settings):
    from bananachat.db import catalog
    from bananachat.services import ollama
    from bananachat.services.agents import service
    from bananachat.services.agents import settings as agent_settings

    with app.app_context():
        ollama.sync_catalog()
        for model in catalog.list_models():
            catalog.set_rollout(model["id"], True)
        service.refresh_model_capabilities(force=True)
        agent_settings.save({**FAST_SETTINGS, **settings}, None)


@pytest.fixture
def agents_app(make_app, runner, token_file):
    def factory(settings=None, **env):
        app = make_app(AGENTS_RUNNER_URL=runner.url, AGENTS_RUNNER_TOKEN_FILE=str(token_file), **env)
        configure(app, **(settings or {}))
        return app
    return factory


@pytest.fixture
def env(agents_app):
    """An app with agents enabled and ``alice`` allowed to use them."""
    app = agents_app()
    alice = add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    return types.SimpleNamespace(app=app, user=alice, browser=browser)


def add_user(app, username, *, allowed=False, role="user"):
    from bananachat import security
    from bananachat.db import access, users

    with app.test_request_context():
        user_id = users.create(username, security.hash_password(f"{username}-password"), role=role)
        if allowed:
            access.add_membership("agents", 0, user_id, "allowlist", added_by=None)
        return users.get(user_id)


def set_settings(app, **values):
    from bananachat.services.agents import settings as agent_settings
    with app.app_context():
        agent_settings.save({**agent_settings.current(fresh=True).to_dict(), **values}, None)


def wait_for(check, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def task_row(app, task_id):
    from bananachat.db import agents as agents_db
    with app.app_context():
        return agents_db.get(task_id)


def wait_status(app, task_id, *states, timeout=15.0):
    """Wait until the task is in one of *states* (for ended states: and its run released the lease)."""
    def check():
        row = task_row(app, task_id)
        if row is None or row["status"] not in states:
            return None
        if row["status"] not in ("queued", "running", "paused") and row["owner_token"]:
            return None
        return row
    return wait_for(check, timeout, f"task in {states}")


def steps(app, task_id, **kwargs):
    from bananachat.db import agents as agents_db
    with app.app_context():
        return [row.to_dict() for row in agents_db.steps(task_id, **kwargs)]


def start(browser, prompt="Build it", **fields):
    response = browser.fetch("/agents", method="POST", data={"prompt": prompt, **fields})
    return response


def start_ok(browser, prompt="Build it", **fields):
    response = start(browser, prompt, **fields)
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["id"]


def error_key(app, value):
    return json.loads(value)["key"] if value and value.startswith("{") else value


def call(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


# ----- tool validation (no app needed) --------------------------------------------------------

def test_tool_arguments_are_validated_strictly():
    from bananachat.services.agents import tools

    allowed = tools.BASE_TOOLS
    ok = tools.validate({"name": "bash", "arguments": {"command": "ls", "timeout": 10}}, allowed)
    assert ok.arguments == {"command": "ls", "timeout": 10}
    assert tools.validate({"name": "bash", "arguments": '{"command": "pwd"}'}, allowed).arguments == {"command": "pwd"}
    assert tools.validate({"name": "read_file", "arguments": {"path": "src/../app.py"}}, allowed).arguments == \
        {"path": "/workspace/app.py"}
    bad = [
        {"name": "rm_rf", "arguments": {}},
        {"name": "delegate", "arguments": {"tasks": [{"title": "a", "instructions": "b"}]}},  # not offered
        {"name": "bash", "arguments": {"command": "ls", "shell": "bash"}},        # unknown key
        {"name": "bash", "arguments": {}},                                        # missing
        {"name": "bash", "arguments": {"command": 5}},                            # wrong type
        {"name": "bash", "arguments": {"command": "ls", "timeout": True}},        # bool is not an int
        {"name": "bash", "arguments": {"command": "ls", "timeout": "10"}},        # no coercion
        {"name": "bash", "arguments": {"command": "ls", "timeout": 0}},           # range
        {"name": "bash", "arguments": {"command": "x" * (tools.MAX_COMMAND + 1)}},
        {"name": "bash", "arguments": {"command": "a\x00b"}},
        {"name": "bash", "arguments": "not json"},
        {"name": "bash", "arguments": ["ls"]},
        {"name": "read_file", "arguments": {"path": "../etc/passwd"}},
        {"name": "read_file", "arguments": {"path": "/etc/passwd"}},
        {"name": "read_file", "arguments": {"path": "/workspace/../../root"}},
        {"name": "read_file", "arguments": {"path": "/workspace/a\nb"}},
        {"name": "read_file", "arguments": {"path": "/workspacex/file"}},
        {"name": "read_file", "arguments": {"path": "a" * 2000}},
        {"name": "read_file", "arguments": {"path": "x", "limit": 5000}},
        {"name": "edit_file", "arguments": {"path": "x", "old": "", "new": "y"}},
        {"name": "write_file", "arguments": {"path": "x", "content": "y" * (tools.MAX_WRITE_BYTES + 1)}},
        {"name": "list_files", "arguments": {"depth": 9}},
        {"name": "finish", "arguments": {"summary": ""}},
    ]
    for raw in bad:
        with pytest.raises(tools.ToolError):
            tools.validate(raw, allowed)

    swarm = tools.ORCHESTRATOR_TOOLS
    good = tools.validate({"name": "delegate", "arguments": {"tasks": [{"title": "A", "instructions": "Do A"}]}},
                          swarm, max_subagents=2)
    assert good.arguments["tasks"] == [{"title": "A", "instructions": "Do A"}]
    for tasks in ([], [{"title": "A"}], [{"title": "A", "instructions": "x", "model": "y"}], "A",
                  [{"title": "A", "instructions": "x"}] * 3):
        with pytest.raises(tools.ToolError):
            tools.validate({"name": "delegate", "arguments": {"tasks": tasks}}, swarm, max_subagents=2)


def test_results_and_context_are_bounded():
    from bananachat.services.agents import loop, tools

    text = "a" * 50_000
    short = tools.truncate(text)
    assert len(short) <= tools.RESULT_LIMIT + 100 and "omitted" in short
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "the task"}]
    for index in range(60):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "bash"}}]})
        messages.append({"role": "tool", "content": f"{index}" + "x" * 5000, "tool_name": "bash"})
    fitted = loop.fit_context(messages, limit=40_000)
    assert fitted[0]["content"] == "rules" and fitted[1]["content"] == "the task"
    assert sum(len(item.get("content") or "") for item in fitted) <= 45_000
    assert fitted[-1]["content"].startswith("59")
    budget = loop.Budget(10, 1000, time.monotonic() - 1, 5)
    with pytest.raises(loop.StopRun) as stopped:
        budget.check()
    assert stopped.value.state == "out_of_budget" and stopped.value.key == "agents.stop_time"


def test_default_settings_are_safe_and_clamped():
    from bananachat.services.agents import settings as agent_settings

    defaults = agent_settings.normalise({})
    assert defaults.enabled is False and defaults.swarms_enabled is False
    assert defaults.max_tasks_per_user == 1
    wild = agent_settings.normalise({"enabled": "yes", "max_steps": 10**9, "max_minutes": -5, "max_subagents": 2,
                                     "max_concurrent_subagents": 4, "model_overrides": {"1": "allow", "x": "deny",
                                                                                        "2": "maybe"}})
    assert wild.enabled is False
    assert wild.max_steps == 200 and wild.max_minutes == 1
    assert wild.max_concurrent_subagents == 2
    assert wild.model_overrides == {"1": "allow"}


# ----- inference and runner client ---------------------------------------------------------------

def test_chat_stream_surfaces_tool_calls_and_is_unchanged_without_tools(app, fake_ollama):
    from bananachat.services import ollama

    fake_ollama.tool_script = [{"content": "Let me look.", "tool_calls": [{"name": "bash", "arguments": {"command": "ls"}}]}]
    with app.app_context():
        chunks = list(ollama.chat_stream("llama3.2:3b", [{"role": "user", "content": "hi"}],
                                         tools=[{"type": "function", "function": {"name": "bash"}}]))
        plain = list(ollama.chat_stream("llama3.2:3b", [{"role": "user", "content": "hi"}]))
    calls = [call for chunk in chunks for call in chunk.tool_calls]
    assert calls == [{"name": "bash", "arguments": {"command": "ls"}}]
    assert "".join(chunk.content for chunk in chunks) == "Let me look."
    assert all(not chunk.tool_calls for chunk in plain)
    bodies = fake_ollama.chat_bodies()
    assert "tools" in bodies[0] and "tools" not in bodies[1]


def test_runner_configuration_is_validated(make_app, runner, token_file, tmp_path):
    from bananachat.services.agents import runner as runner_mod

    app = make_app(AGENTS_RUNNER_URL="http://203.0.113.5:11436", AGENTS_RUNNER_TOKEN_FILE=str(token_file))
    with app.app_context(), pytest.raises(runner_mod.RunnerUnavailable):
        runner_mod.client()
    loose = tmp_path / "loose.token"
    loose.write_text(TOKEN)
    os.chmod(loose, 0o644)
    app = make_app(AGENTS_RUNNER_URL=runner.url, AGENTS_RUNNER_TOKEN_FILE=str(loose))
    with app.app_context(), pytest.raises(runner_mod.RunnerUnavailable):
        runner_mod.client()
    app = make_app(AGENTS_RUNNER_URL=runner.url, AGENTS_RUNNER_TOKEN="wrong-token")
    with app.app_context():
        with pytest.raises(runner_mod.RunnerError) as denied:
            runner_mod.client().list()
        assert denied.value.status == 401
    app = make_app(AGENTS_RUNNER_URL=runner.url, AGENTS_RUNNER_TOKEN_FILE=str(token_file))
    with app.app_context():
        assert runner_mod.health(fresh=True)["ok"] is True
        assert runner_mod.client().list() == []


def test_tool_capability_detection_chooses_models(agents_app, fake_ollama):
    from bananachat.db import catalog
    from bananachat.services.access import AccessContext
    from bananachat.services.agents import service
    from bananachat.services.agents import settings as agent_settings

    fake_ollama.capabilities = {"qwen3:4b": ["completion"]}
    app = agents_app()
    with app.app_context():
        service.refresh_model_capabilities(force=True)
        alice = add_user(app, "alice", allowed=True)
        names = [model["ollama_name"] for model in agent_settings.agent_models(AccessContext.load(alice))]
        assert names == ["llama3.2:3b"]
        qwen = catalog.get_by_name("qwen3:4b")
        settings = agent_settings.current(fresh=True).to_dict()
        agent_settings.save({**settings, "model_overrides": {str(qwen["id"]): "allow"}}, None)
        names = [model["ollama_name"] for model in agent_settings.agent_models(AccessContext.load(alice))]
        assert sorted(names) == ["llama3.2:3b", "qwen3:4b"]
    assert any(path == "/api/show" for path, _ in fake_ollama.requests)


# ----- the loop --------------------------------------------------------------------------------------

def test_task_runs_tools_in_the_sandbox_and_finishes(env, runner, fake_ollama):
    runner.exec_handler = lambda box, command, timeout: {"stdout": "x" * 20_000 if "big" in command else "ok\n"}
    fake_ollama.tool_script = [
        {"content": "I'll start.", "tool_calls": [{"name": "bash", "arguments": {"command": "echo big"}}]},
        call("write_file", path="app.py", content="print('hi')\n"),
        call("edit_file", path="app.py", old="hi", new="hello"),
        call("read_file", path="/workspace/app.py"),
        call("list_files"),
        call("search", pattern="hello|$(reboot)"),
        call("finish", summary="Created **app.py**."),
    ]
    task_id = start_ok(env.browser, "Write a hello app")
    row = wait_status(env.app, task_id, "finished")
    assert row["summary"] == "Created **app.py**."
    assert row["steps_used"] == 7 and row["tool_calls"] == 7
    box = runner.only()
    assert box.session == task_id
    assert box.files["/workspace/app.py"] == b"print('hello')\n"
    assert [command for _, command in runner.execs][0] == "echo big"
    assert "grep -rnIE" in runner.execs[1][1] and "-e 'hello|$(reboot)' --" in runner.execs[1][1]
    log = steps(env.app, task_id)
    tool_steps = [step for step in log if step["kind"] == "tool"]
    assert [step["tool_name"] for step in tool_steps] == ["bash", "write_file", "edit_file", "read_file",
                                                            "list_files", "search", "finish"]
    assert all(step["tool_status"] == "ok" for step in tool_steps)
    assert len(tool_steps[0]["tool_result"]) <= 8200 and "omitted" in tool_steps[0]["tool_result"]
    assert "1\tprint('hello')" in tool_steps[3]["tool_result"]
    assert log[-1]["kind"] == "summary"
    # The model got the tool definitions and every request stayed off worker PCs.
    bodies = [body for body in fake_ollama.chat_bodies() if body.get("tools")]
    assert len(bodies) == 7
    assert {tool["function"]["name"] for tool in bodies[0]["tools"]} == {
        "bash", "read_file", "write_file", "edit_file", "list_files", "search", "finish"}
    assert bodies[1]["messages"][-1]["role"] == "tool"
    # Credits and metrics went to the agent pool; the sandbox is kept for downloads.
    from bananachat import db
    with env.app.app_context():
        ledger = db.query("SELECT request_type, tokens_in, tokens_out FROM credit_ledger WHERE user_id=?",
                          (env.user["id"],))
        assert {row["request_type"] for row in ledger} == {"agent"} and len(ledger) == 7
        assert db.scalar("SELECT COUNT(*) FROM request_metrics WHERE request_type='agent'") == 7
    assert row["sandbox_id"] == box.id and row["sandbox_expires_at"]


def test_invalid_tool_calls_are_rejected_without_running_anything(env, runner, fake_ollama):
    fake_ollama.tool_script = [
        {"tool_calls": [{"name": "bash", "arguments": {"command": 5}},
                        {"name": "read_file", "arguments": {"path": "../../etc/shadow"}},
                        {"name": "launch_missiles", "arguments": {}}]},
        call("finish", summary="Gave up."),
    ]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    tool_steps = [step for step in steps(env.app, task_id) if step["kind"] == "tool"]
    assert [step["tool_status"] for step in tool_steps[:3]] == ["invalid"] * 3
    assert "inside /workspace" in tool_steps[1]["tool_result"]
    assert "Unknown tool" in tool_steps[2]["tool_result"]
    assert runner.execs == [] and runner.sandboxes == {}
    feedback = fake_ollama.chat_bodies()[-1]["messages"]
    assert [item["role"] for item in feedback[-3:]] == ["tool", "tool", "tool"]


def test_step_budget_is_a_hard_limit(env, fake_ollama):
    set_settings(env.app, max_steps=3)
    fake_ollama.tool_responder = lambda body: call("bash", command="make")
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "out_of_budget")
    assert error_key(env.app, row["error"]) == "agents.stop_steps"
    assert len([body for body in fake_ollama.chat_bodies() if body.get("tools")]) == 3
    assert row["steps_used"] == 3


def test_token_budget_and_bounded_model_retries(env, fake_ollama):
    set_settings(env.app, max_tokens=1000)
    fake_ollama.tool_responder = lambda body: {**call("bash", command="ls"), "prompt_tokens": 900,
                                               "completion_tokens": 200}
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "out_of_budget")
    assert error_key(env.app, row["error"]) == "agents.stop_tokens"

    fake_ollama.fail_models.update(fake_ollama.models)
    before = len(fake_ollama.chat_bodies())
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "failed")
    assert error_key(env.app, row["error"]) == "agents.stop_model_failed"
    from bananachat.services.agents import loop
    assert len(fake_ollama.chat_bodies()) - before == loop.MODEL_RETRIES + 1


def test_stop_kills_the_running_command_promptly(env, runner, fake_ollama):
    runner.hang_exec = True
    fake_ollama.tool_script = [call("bash", command="sleep 1000")]
    task_id = start_ok(env.browser)
    wait_for(lambda: runner.execs, message="the command to start")
    started = time.monotonic()
    assert env.browser.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    row = wait_status(env.app, task_id, "stopped", timeout=5)
    assert time.monotonic() - started < 5
    assert error_key(env.app, row["error"]) == "agents.stopped"
    box_id = runner.execs[0][0]
    assert runner.interrupted == [box_id]
    assert box_id in runner.sandboxes  # interrupted, so the workspace is kept for downloads


def test_stop_request_from_another_process_and_app_instance(agents_app, runner, fake_ollama):
    from bananachat.db import agents as agents_db

    app = agents_app()
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    fake_ollama.tool_delay = 3
    task_id = start_ok(browser)
    wait_for(lambda: fake_ollama.active_tool_chats, message="the model call")
    # Another process only writes the stop flag; this process's supervisor applies it.
    with app.app_context():
        assert agents_db.request_stop(task_id)
    row = wait_status(app, task_id, "stopped", timeout=6)
    assert error_key(app, row["error"]) == "agents.stopped"

    # A second app instance on the same database stops a task started by the first one.
    fake_ollama.tool_delay = 3
    task_id = start_ok(browser)
    second = Browser(_second_app(app))
    second.login("alice")
    wait_for(lambda: task_row(app, task_id)["status"] == "running", message="running")
    assert second.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    wait_status(app, task_id, "stopped", timeout=6)


def _second_app(app):
    from bananachat import create_app
    from bananachat.config import load_config

    config = app.config["BC"]
    environ = {"BC_INSTANCE_DIR": str(config.instance_dir), "BC_OLLAMA_URL": config.ollama_url, "BC_ENV": "testing",
               "BC_LOGGING_LEVEL": "minimal", "BC_SECRET_KEY": config.secret_key, "BC_MIN_FREE_MEMORY_MB": "0",
               "BC_AGENTS_RUNNER_URL": config.agents_runner_url,
               "BC_AGENTS_RUNNER_TOKEN_FILE": config.agents_runner_token_file}
    return create_app(load_config(environ), testing=True)


def test_crash_recovery_marks_tasks_interrupted_and_cleans_up(env, runner):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod
    from bananachat.services.agents import service

    set_settings(env.app, keep_workspace_minutes=0)
    with env.app.app_context():
        sandbox = runner_mod.client().create("f" * 32)
        agents_db.create("a" * 32, user_id=env.user["id"], title="Crashed", prompt="p", model_id=None,
                         model_name="m", swarm=False, owner_token="dead-token", max_user=5, max_site=5)
        agents_db.set_sandbox("a" * 32, "dead-token", sandbox["id"])
        agents_db.create("b" * 32, user_id=env.user["id"], title="Alive", prompt="p", model_id=None,
                         model_name="m", swarm=False, owner_token="live-token", max_user=5, max_site=5)
        db.execute("UPDATE agent_tasks SET heartbeat_at=? WHERE id=?", (time.time() - 300, "a" * 32))
        service.recover_tasks(env.app)
        crashed, alive = agents_db.get("a" * 32), agents_db.get("b" * 32)
        assert crashed["status"] == "interrupted" and crashed["owner_token"] is None
        assert error_key(env.app, crashed["error"]) == "agents.interrupted"
        assert alive["status"] == "queued" and alive["owner_token"] == "live-token"
        # The dead process can no longer write anything.
        assert agents_db.add_step("a" * 32, "dead-token", kind="notice", content="late") is None
        assert agents_db.finish("a" * 32, "dead-token", "finished") is False
        assert service.reap_sandboxes() >= 1
        assert sandbox["id"] in runner.deleted
        assert agents_db.get("a" * 32)["sandbox_id"] is None
        db.execute("UPDATE agent_tasks SET owner_token=NULL WHERE id=?", ("b" * 32,))


def test_swarm_runs_bounded_sub_agents_that_cannot_delegate(env, fake_ollama):
    set_settings(env.app, swarms_enabled=True, max_subagents=4, max_concurrent_subagents=2)
    concurrent_lanes = threading.Barrier(2)

    def responder(body):
        messages = body["messages"]
        names = {tool["function"]["name"] for tool in body["tools"]}
        if "You are a sub-agent" in messages[0]["content"]:
            assert "delegate" not in names
            if messages[-1]["role"] == "tool":
                return call("finish", summary="lane done")
            # Hold each pair's actual HTTP requests until both lanes arrive.
            # A fixed sleep only observed overlap when the scheduler was fast.
            concurrent_lanes.wait(timeout=10)
            return call("delegate", tasks=[{"title": "deeper", "instructions": "recurse"}])
        assert "delegate" in names
        if messages[-1]["role"] == "tool":
            return call("finish", summary="all lanes done")
        return call("delegate", tasks=[{"title": f"Part {n}", "instructions": f"Do part {n}"} for n in range(4)])

    fake_ollama.tool_responder = responder
    task_id = start_ok(env.browser, swarm="1")
    row = wait_status(env.app, task_id, "finished", timeout=20)
    assert row["summary"] == "all lanes done"
    assert fake_ollama.max_tool_chats == 2
    from bananachat.db import agents as agents_db
    with env.app.app_context():
        lanes = agents_db.lanes(task_id)
    assert [lane["status"] for lane in lanes] == ["finished"] * 4
    assert [lane["summary"] for lane in lanes] == ["lane done"] * 4
    log = steps(env.app, task_id)
    refused = [step for step in log if step["agent"] > 0 and step["tool_name"] == "delegate"]
    assert len(refused) == 4 and all(step["tool_status"] == "invalid" for step in refused)
    delegate = next(step for step in log if step["agent"] == 0 and step["tool_name"] == "delegate")
    assert "lane done" in delegate["tool_result"]


def test_swarm_limits_are_enforced(env, fake_ollama):
    fake_ollama.tool_script = []
    assert start(env.browser, swarm="1").status_code == 400  # swarms are off
    set_settings(env.app, swarms_enabled=True, max_subagents=2, max_concurrent_subagents=2)
    fake_ollama.tool_script = [call("delegate", tasks=[{"title": str(n), "instructions": "x"} for n in range(3)]),
                               call("finish", summary="ok")]
    task_id = start_ok(env.browser, swarm="1")
    wait_status(env.app, task_id, "finished")
    tool_steps = [step for step in steps(env.app, task_id) if step["kind"] == "tool"]
    assert tool_steps[0]["tool_status"] == "invalid" and "at most 2" in tool_steps[0]["tool_result"]


def test_credits_are_checked_before_starting_and_before_each_step(env, fake_ollama, monkeypatch):
    from bananachat.db import credits

    original = credits.budget
    state = {"exhausted": True}

    def budget(user, pool):
        if pool == "agent" and state["exhausted"]:
            return types.SimpleNamespace(available=False, next_is_slow=False, slow_limit=0, weekly_exhausted=False,
                                         resets_at=None, weekly_resets_at=None, unlimited=False,
                                         seconds_until_reset=lambda weekly=False: 120)
        return original(user, pool)

    monkeypatch.setattr(credits, "budget", budget)
    response = start(env.browser)
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "quota_exhausted"

    state["exhausted"] = False

    def responder(body):
        state["exhausted"] = True  # used up by this step
        return call("bash", command="ls")

    fake_ollama.tool_responder = responder
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "out_of_budget")
    assert error_key(env.app, row["error"]) == "agents.stop_credits"


def test_concurrency_and_start_rate_limits(agents_app, fake_ollama):
    app = agents_app({"max_tasks_total": 1})
    alice = Browser(app)
    add_user(app, "alice", allowed=True)
    add_user(app, "bob", allowed=True)
    alice.login("alice")
    bob = Browser(app)
    bob.login("bob")
    fake_ollama.tool_delay = 2
    first = start_ok(alice)
    assert start(alice).status_code == 409                     # one task per user
    response = start(bob)
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "busy"  # site limit
    wait_status(app, first, "finished")
    fake_ollama.tool_delay = 0
    set_settings(app, starts_per_hour=2, max_tasks_total=4)
    add_user(app, "carol", allowed=True)
    carol = Browser(app)
    carol.login("carol")
    for _ in range(2):
        wait_status(app, start_ok(carol), "finished")
    response = start(carol)
    assert response.status_code == 429 and response.get_json()["error"]["code"] == "rate_limited"


def test_capability_gating_and_navigation(agents_app, fake_ollama):
    app = agents_app({"enabled": False})
    add_user(app, "alice", allowed=True)
    add_user(app, "bob")
    alice, bob, admin = Browser(app), Browser(app), Browser(app)
    alice.login("alice")
    bob.login("bob")
    admin.login("admin", "admin-password")

    page = alice.get("/agents").get_data(as_text=True)
    assert "Cloud sessions are off" in page or "sessioni cloud sono disattivate" in page
    assert start(alice).status_code == 403
    assert 'href="/agents"' not in alice.get("/account").get_data(as_text=True)

    set_settings(app, enabled=True)
    assert 'href="/agents"' in alice.get("/account").get_data(as_text=True)
    assert 'href="/agents"' not in bob.get("/account").get_data(as_text=True)
    response = start(bob)
    assert response.status_code == 403 and response.get_json()["error"]["code"] == "forbidden"
    assert start(admin).status_code == 201  # administrators are always allowed
    task_id = start_ok(alice)
    for path in (f"/agents/{task_id}", f"/agents/{task_id}/events", f"/agents/{task_id}/workspace"):
        assert bob.get(path).status_code == 404
    assert bob.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 404
    assert bob.post_json(f"/agents/{task_id}/messages", {"content": "hi"}).status_code == 404
    wait_status(app, task_id, "finished")


def test_maintenance_pauses_running_tasks_and_refuses_new_ones(env, runner, fake_ollama):
    import threading

    from bananachat.db import settings as site_settings

    entered, proceed = threading.Event(), threading.Event()

    def handler(box, command, timeout):
        entered.set()
        proceed.wait(10)
        return {"stdout": "built"}

    runner.exec_handler = handler
    fake_ollama.tool_script = [call("bash", command="make"), call("finish", summary="done")]
    task_id = start_ok(env.browser)
    assert entered.wait(10)
    with env.app.app_context():
        site_settings.update(maintenance_mode=1)
    proceed.set()
    wait_status(env.app, task_id, "paused")
    response = start(env.browser)
    assert response.status_code == 503
    assert env.browser.post_json(f"/agents/{task_id}/messages", {"content": "more"}).status_code == 503
    with env.app.app_context():
        site_settings.update(maintenance_mode=0)
    row = wait_status(env.app, task_id, "finished")
    assert row["summary"] == "done"
    notices = [error_key(env.app, step["content"]) for step in steps(env.app, task_id) if step["kind"] == "notice"]
    assert "agents.paused_maintenance" in notices and "agents.notice_resumed" in notices


def test_follow_ups_continue_in_the_same_workspace(env, runner, fake_ollama):
    fake_ollama.tool_script = [call("write_file", path="a.txt", content="one"), call("finish", summary="first")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    fake_ollama.tool_script = [call("read_file", path="a.txt"), call("finish", summary="second")]
    response = env.browser.post_json(f"/agents/{task_id}/messages", {"content": "Now read it back"})
    assert response.status_code == 202 and response.get_json()["queued"] is False
    row = wait_status(env.app, task_id, "finished")
    assert row["summary"] == "second" and row["runs"] == 2
    assert len(runner.sandboxes) == 1 and not runner.deleted  # reused
    history = [body for body in fake_ollama.chat_bodies() if body.get("tools")][2]["messages"]
    assert history[1]["content"] == "Build it" and history[-1]["content"] == "Now read it back"
    read = [step for step in steps(env.app, task_id) if step["tool_name"] == "read_file"][0]
    assert "one" in read["tool_result"]

    # While running, a follow-up is queued and read at the next step.
    fake_ollama.tool_delay = 0.4
    fake_ollama.tool_script = [call("bash", command="ls"), call("finish", summary="third")]
    assert env.browser.post_json(f"/agents/{task_id}/messages", {"content": "go"}).status_code == 202
    wait_for(lambda: task_row(env.app, task_id)["status"] == "running", message="running")
    response = env.browser.post_json(f"/agents/{task_id}/messages", {"content": "also this"})
    assert response.get_json()["queued"] is True
    wait_status(env.app, task_id, "finished")
    users = [step["content"] for step in steps(env.app, task_id) if step["kind"] == "user"]
    assert users[-2:] == ["go", "also this"]


def test_uploads_workspace_browser_and_downloads(env, runner, fake_ollama):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("src/main.py", "print(1)\n")
    fake_ollama.tool_script = [call("list_files"), call("finish", summary="Looked.")]
    response = env.browser.fetch("/agents", method="POST", data={
        "prompt": "Look", "files": [(io.BytesIO(b"hello notes"), "../../notes.txt"),
                                    (io.BytesIO(archive.getvalue()), "code.zip")]},
        content_type="multipart/form-data")
    assert response.status_code == 201, response.get_data(as_text=True)
    task_id = response.get_json()["id"]
    wait_status(env.app, task_id, "finished")
    box = runner.only()
    assert box.files["/workspace/notes.txt"] == b"hello notes"
    assert box.files["/workspace/src/main.py"] == b"print(1)\n"
    listing = [step for step in steps(env.app, task_id) if step["tool_name"] == "list_files"][0]["tool_result"]
    assert "src/" in listing and "notes.txt" in listing

    data = env.browser.get(f"/agents/{task_id}/workspace").get_json()
    assert data["type"] == "dir" and {"name": "src", "type": "dir", "size": 0} in data["entries"]
    data = env.browser.get(f"/agents/{task_id}/workspace?path=/workspace/src/main.py").get_json()
    assert data["text"] == "print(1)\n"
    response = env.browser.get(f"/agents/{task_id}/workspace/file?path=/workspace/notes.txt")
    assert response.data == b"hello notes"
    assert response.headers["Content-Disposition"] == 'attachment; filename="notes.txt"'
    assert response.mimetype == "application/octet-stream"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert env.browser.get(f"/agents/{task_id}/workspace?path=/etc/passwd").status_code == 400
    assert env.browser.get(f"/agents/{task_id}/workspace?path=../../etc").status_code == 400
    response = env.browser.get(f"/agents/{task_id}/workspace/archive")
    assert response.status_code == 200 and response.mimetype == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(response.data)) as bundle:
        assert "./notes.txt" in bundle.getnames()
    response = env.browser.fetch(f"/agents/{task_id}/workspace/upload", method="POST",
                                 data={"files": [(io.BytesIO(b"x"), "late.txt")]}, content_type="multipart/form-data")
    assert response.status_code == 200 and box.files["/workspace/late.txt"] == b"x"


def test_lost_sandbox_is_replaced_and_capacity_waits_are_bounded(env, runner, fake_ollama, monkeypatch):
    def responder(body):
        if len([m for m in body["messages"] if m["role"] == "tool"]) == 1:
            for box_id in list(runner.sandboxes):  # the runner's reaper removed it meanwhile
                del runner.sandboxes[box_id]
        count = len([m for m in body["messages"] if m["role"] == "tool"])
        return call("bash", command="ls") if count < 3 else call("finish", summary="ok")

    fake_ollama.tool_responder = responder
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    log = steps(env.app, task_id)
    results = [step["tool_result"] for step in log if step["kind"] == "tool"]
    assert "sandbox was lost" in results[1]
    assert "agents.notice_sandbox_reset" in [error_key(env.app, step["content"]) for step in log
                                             if step["kind"] == "notice"]
    assert len(runner.sandboxes) == 1

    from bananachat.services.agents import loop
    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 0.3)
    runner.fail_create = (429, "capacity")
    fake_ollama.tool_responder = None
    fake_ollama.tool_script = [call("bash", command="ls")]
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "failed")
    assert error_key(env.app, row["error"]) == "agents.stop_no_sandbox"


def test_retention_deletes_old_tasks_and_their_sandboxes(env, runner):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod
    from bananachat.services.agents import service

    with env.app.app_context():
        sandbox = runner_mod.client().create("c" * 32)
        for task_id, age in (("c" * 32, 40), ("d" * 32, 2)):
            agents_db.create(task_id, user_id=env.user["id"], title="t", prompt="p", model_id=None, model_name="m",
                             swarm=False, owner_token="token", max_user=9, max_site=9)
            agents_db.finish(task_id, "token", "finished")
            db.execute("UPDATE agent_tasks SET finished_at=? WHERE id=?", (db.now(-timedelta(days=age)), task_id))
        db.execute("UPDATE agent_tasks SET sandbox_id=? WHERE id=?", (sandbox["id"], "c" * 32))
        assert service.purge_old_tasks() == 1
        assert agents_db.get("c" * 32) is None and agents_db.get("d" * 32) is not None
        assert db.scalar("SELECT COUNT(*) FROM agent_steps WHERE task_id=?", ("c" * 32,)) == 0
    assert sandbox["id"] in runner.deleted


# ----- pages ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("language", ["en", "it"])
def test_pages_render_in_both_languages(env, fake_ollama, language):
    with env.browser.client.session_transaction() as session:
        session["language"] = language
    page = env.browser.get("/agents").get_data(as_text=True)
    assert ("New session" if language == "en" else "Nuova sessione") in page
    assert ("Cloud sessions" if language == "en" else "Sessioni cloud") in page
    assert 'id="new-task-form"' in page and "<script>" not in page and "style=" not in page
    fake_ollama.tool_script = [{"content": "Thinking <b>aloud</b>", "tool_calls": [
        {"name": "bash", "arguments": {"command": "echo '<img src=x onerror=alert(1)>'"}}]},
        call("finish", summary="Done <script>")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    response = env.browser.get(f"/agents/{task_id}")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert ("Workspace" if language == "en" else "Area di lavoro") in page
    assert "<img src=x" not in page and "<script>\"" not in page  # JSON is escaped for the page
    data = json.loads(page.split('id="page-data">')[1].split("</script>")[0])
    assert data["task"]["status"] == "finished"
    events = env.browser.get(f"/agents/{task_id}/events?after=0").get_json()
    assert [step["kind"] for step in events["steps"]][0] == "user"
    last = events["steps"][-1]["id"]
    assert env.browser.get(f"/agents/{task_id}/events?after={last}").get_json()["steps"] == []
    listing = env.browser.get("/agents").get_data(as_text=True)
    assert ("Finished" if language == "en" else "Completato") in listing


def test_i18n_catalog_has_identical_keys():
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parents[2] / "bananachat/i18n/agents.json").read_text())
    assert set(data["en"]) == set(data["it"])
    assert all(key.startswith(("agents.", "js.agents.")) for key in data["en"])


def test_deleting_a_task_removes_its_sandbox(env, runner, fake_ollama):
    fake_ollama.tool_script = [call("write_file", path="x", content="y"), call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    box = runner.only()
    response = env.browser.fetch(f"/agents/{task_id}/delete", method="POST")
    assert response.status_code == 200
    assert box.id in runner.deleted and task_row(env.app, task_id) is None


# ----- administration ------------------------------------------------------------------------------

def test_admin_settings_models_and_audit(env, fake_ollama):
    admin = Browser(env.app)
    admin.login("admin", "admin-password")
    page = admin.get("/admin/agents").get_data(as_text=True)
    assert "Sandbox runner" in page and "podman" in page and "Stop all sessions" in page
    response = admin.post("/admin/agents/settings", {"enabled": "1", "max_steps": "100000", "max_minutes": "20"})
    assert response.status_code == 302
    set_values = {name: str(value) for name, value in FAST_SETTINGS.items() if name != "enabled"}
    from bananachat.services.agents import settings as agent_settings
    with env.app.app_context():
        assert agent_settings.current(fresh=True).max_steps == 40  # refused: out of range
    form = {**{name: str(default) for name, (default, _, _) in agent_settings.INTEGER_FIELDS.items()},
            **set_values, "max_steps": "12", "enabled": "1", "swarms_enabled": "1"}
    assert admin.post("/admin/agents/settings", form).status_code == 302
    from bananachat import db
    from bananachat.db import catalog
    with env.app.app_context():
        saved = agent_settings.current(fresh=True)
        assert saved.max_steps == 12 and saved.swarms_enabled
        model = catalog.get_by_name("qwen3:4b")
    assert admin.post("/admin/agents/models", {f"override_{model['id']}": "deny"}).status_code == 302
    with env.app.app_context():
        assert agent_settings.current(fresh=True).model_overrides == {str(model["id"]): "deny"}
        actions = [row["action"] for row in db.query("SELECT action FROM audit_log")]
    assert "admin.agents.settings" in actions and "admin.agents.models" in actions
    assert Browser(env.app).get("/admin/agents").status_code == 302  # signed out
    assert env.browser.get("/admin/agents").status_code == 403       # not an administrator


def test_admin_kill_switch_and_task_log(agents_app, runner, fake_ollama):
    app = agents_app()
    browsers = []
    for name in ("alice", "bob"):
        add_user(app, name, allowed=True)
        browser = Browser(app)
        browser.login(name)
        browsers.append(browser)
    fake_ollama.tool_delay = 5
    ids = [start_ok(browser) for browser in browsers]
    for task_id in ids:
        wait_for(lambda task_id=task_id: task_row(app, task_id)["status"] == "running", message="running")
    admin = Browser(app)
    admin.login("admin", "admin-password")
    assert "Active sessions" in admin.get("/admin/agents").get_data(as_text=True)
    started = time.monotonic()
    assert admin.post("/admin/agents/stop-all").status_code == 302
    for task_id in ids:
        row = wait_status(app, task_id, "stopped", timeout=6)
        assert error_key(app, row["error"]) == "agents.stopped_admin"
    assert time.monotonic() - started < 6
    page = admin.get(f"/admin/agents/tasks/{ids[0]}").get_data(as_text=True)
    assert "Build it" in page and "An administrator stopped the session." in page
    from bananachat import db
    with app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='admin.agents.stop_all'") == 1
    # The owner sees it too, in their language.
    events = browsers[0].get(f"/agents/{ids[0]}/events").get_json()
    assert events["task"]["status"] == "stopped"


def test_disabling_the_feature_stops_new_work_but_keeps_history(env, fake_ollama):
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    set_settings(env.app, enabled=False)
    assert start(env.browser).status_code == 403
    assert env.browser.post_json(f"/agents/{task_id}/messages", {"content": "more"}).status_code == 403
    assert env.browser.get(f"/agents/{task_id}").status_code == 200
    assert task_id in env.browser.get("/agents").get_data(as_text=True)
