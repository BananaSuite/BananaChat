"""Third security review of the agents feature (web side): regressions for the findings."""

from __future__ import annotations

import time
import types

import pytest

from tests.app.conftest import Browser
from tests.app.test_agents import (  # noqa: F401 - fixtures (fast_loop is autouse)
    add_user, agents_app, call, env, error_key, fast_loop, runner, set_settings, start_ok, steps, task_row,
    token_file, wait_for, wait_status)

# ----- nothing the agent started keeps running once its run ended ---------------------------------------


def test_a_finished_run_kills_what_the_agent_left_running(env, runner, fake_ollama):
    """`nohup miner &` returns at once; the runner only kills leftovers when asked (interrupt)."""
    fake_ollama.tool_script = [call("bash", command="nohup sh -c 'while :; do :; done' >/dev/null 2>&1 &"),
                               call("finish", summary="done")]
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "finished")
    box = runner.only()
    assert runner.interrupted == [box.id]
    assert row["sandbox_id"] == box.id and box.id in runner.sandboxes  # still kept for downloads


def test_the_kill_switch_stops_background_processes_between_commands(agents_app, runner, fake_ollama):
    app = agents_app()
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    fake_ollama.tool_script = [call("bash", command="nohup ./attack.sh >/dev/null 2>&1 &")]
    fake_ollama.tool_delay = 2  # so the next model call is in progress, and no command runs, when Stop arrives
    task_id = start_ok(browser)
    wait_for(lambda: runner.execs, timeout=10, message="the command")
    time.sleep(0.3)
    admin = Browser(app)
    admin.login("admin", "admin-password")
    assert admin.post("/admin/agents/stop-all").status_code == 302
    row = wait_status(app, task_id, "stopped", timeout=8)
    assert error_key(app, row["error"]) == "agents.stopped_admin"
    assert runner.interrupted == [runner.execs[0][0]]


def test_stop_during_a_command_interrupts_once(env, runner, fake_ollama):
    runner.hang_exec = True
    fake_ollama.tool_script = [call("bash", command="sleep 1000")]
    task_id = start_ok(env.browser)
    wait_for(lambda: runner.execs, message="the command to start")
    assert env.browser.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    wait_status(env.app, task_id, "stopped", timeout=5)
    assert runner.interrupted == [runner.execs[0][0]]  # the stop already cleaned the sandbox


def test_crash_recovery_kills_what_the_dead_process_left_running(env, runner):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod
    from bananachat.services.agents import service

    with env.app.app_context():
        sandbox = runner_mod.client().create("c" * 32)
        agents_db.create("c" * 32, user_id=env.user["id"], title="Crashed", prompt="p", model_id=None,
                         model_name="m", swarm=False, owner_token="dead", max_user=5, max_site=5)
        agents_db.set_sandbox("c" * 32, "dead", sandbox["id"])
        db.execute("UPDATE agent_tasks SET heartbeat_at=? WHERE id=?", (time.time() - 300, "c" * 32))
        service.recover_tasks(env.app)
        row = agents_db.get("c" * 32)
        assert row["status"] == "interrupted"
        assert runner.interrupted == [sandbox["id"]]
        assert row["sandbox_id"] == sandbox["id"]  # kept (keep_workspace_minutes) for downloads


# ----- budgets -----------------------------------------------------------------------------------------


def test_follow_ups_that_restart_a_finished_run_obey_the_start_rate(agents_app, fake_ollama):
    """A message queued while a run works restarts the task when it finishes: that is a start like any other."""
    app = agents_app({"starts_per_hour": 1})
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    fake_ollama.tool_delay = 1.0  # the message arrives during the last model call, after the run read messages
    fake_ollama.tool_script = [call("finish", summary="one")]
    task_id = start_ok(browser)
    wait_for(lambda: task_row(app, task_id)["status"] == "running", message="running")
    time.sleep(0.2)
    response = browser.post_json(f"/agents/{task_id}/messages", {"content": "and again"})
    assert response.status_code == 202 and response.get_json()["queued"] is True
    fake_ollama.tool_delay = 0
    row = wait_status(app, task_id, "finished")
    time.sleep(1.0)  # a restart would have happened by now
    row = task_row(app, task_id)
    assert row["runs"] == 1 and row["status"] == "finished"
    from bananachat.db import agents as agents_db
    with app.app_context():
        assert agents_db.pending_messages(task_id) == 1  # kept for when the person may start again


def test_wall_time_bounds_commands_and_tool_calls_within_a_step(env, runner, fake_ollama, monkeypatch):
    """Only the start of a step checked the time: 8 commands per step each got the full command timeout."""
    from bananachat.services.agents import loop

    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(loop, "time", types.SimpleNamespace(monotonic=lambda: real() + offset[0]))
    set_settings(env.app, max_minutes=1, command_timeout=120)
    timeouts = []

    def handler(box, command, timeout):
        timeouts.append(timeout)
        offset[0] += 55 if len(timeouts) == 1 else 10  # the commands take (fake) time
        return {"stdout": "ok"}
    runner.exec_handler = handler
    fake_ollama.tool_script = [{"tool_calls": [{"name": "bash", "arguments": {"command": f"sleep {n}",
                                                                               "timeout": 120}}
                                               for n in range(3)]}]
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "out_of_budget")
    assert error_key(env.app, row["error"]) == "agents.stop_time"
    assert len(timeouts) == 2, timeouts           # the third command never started
    assert timeouts[0] <= 60 and timeouts[1] <= 6  # each capped to the time left


# ----- the web server's own resources -------------------------------------------------------------------


def test_large_command_answers_from_the_runner_are_accepted(env, runner, fake_ollama):
    """The runner may send ~12 MB of JSON for one command (2 x 1 MB streams escaped as \\u0001)."""
    runner.exec_handler = lambda box, command, timeout: {"stdout": "\x01" * (1 << 20), "stderr": "\x02" * (1 << 20),
                                                         "truncated": True}
    fake_ollama.tool_script = [call("bash", command="head -c 1M /dev/urandom"), call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    tool = [step for step in steps(env.app, task_id) if step["kind"] == "tool"][0]
    assert tool["tool_status"] == "error" or tool["tool_status"] == "ok"
    assert "could not do this" not in tool["tool_result"], tool["tool_result"][:300]
    assert "omitted" in tool["tool_result"]


def test_one_archive_download_per_person_at_a_time(env, runner, fake_ollama):
    """A slowly read download holds one of the runner's two archive slots for minutes; one person gets one."""
    fake_ollama.tool_script = [call("write_file", path="a.txt", content="hi"), call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    first = env.browser.get(f"/agents/{task_id}/workspace/archive", buffered=False)
    assert first.status_code == 200
    second = env.browser.fetch(f"/agents/{task_id}/workspace/archive")
    assert second.status_code == 429 and second.headers.get("Retry-After")
    # Another person is not affected.
    from bananachat.db import agents as agents_db
    bob = add_user(env.app, "bob", allowed=True)
    other = Browser(env.app)
    other.login("bob")
    with env.app.app_context():
        from bananachat import db
        db.execute("UPDATE agent_tasks SET user_id=? WHERE id=?", (bob["id"], task_id))
    response = other.get(f"/agents/{task_id}/workspace/archive")
    assert response.status_code == 200
    response.close()
    with env.app.app_context():
        db.execute("UPDATE agent_tasks SET user_id=? WHERE id=?", (env.user["id"], task_id))
        assert agents_db.get(task_id)["user_id"] == env.user["id"]
    first.close()
    third = env.browser.get(f"/agents/{task_id}/workspace/archive")
    assert third.status_code == 200 and third.data[:2] == b"\x1f\x8b"


@pytest.mark.parametrize("path", ["/workspace/../etc/passwd", "/etc/passwd", "a\nb"])
def test_workspace_paths_stay_inside_for_other_people_too(env, runner, fake_ollama, path):
    """Cross-user: a task id of someone else is a 404 for every task route (no existence oracle)."""
    fake_ollama.tool_script = [call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    add_user(env.app, "mallory", allowed=True)
    mallory = Browser(env.app)
    mallory.login("mallory")
    for url in (f"/agents/{task_id}", f"/agents/{task_id}/events", f"/agents/{task_id}/workspace?path={path}",
                f"/agents/{task_id}/workspace/file?path={path}", f"/agents/{task_id}/workspace/archive"):
        assert mallory.get(url).status_code == 404, url
    for url in (f"/agents/{task_id}/stop", f"/agents/{task_id}/delete", f"/agents/{task_id}/workspace/upload"):
        assert mallory.fetch(url, method="POST").status_code == 404, url
    assert mallory.post_json(f"/agents/{task_id}/messages", {"content": "hi"}).status_code == 404
    # And CSRF: a cookie-authenticated POST without the token is refused.
    assert env.browser.client.post(f"/agents/{task_id}/delete").status_code in (400, 403)
    assert task_row(env.app, task_id) is not None


def test_exec_requests_wait_as_long_as_the_runner_may_take(env, monkeypatch):
    """After the command's timeout the runner may spend ~50 s killing it (two rounds) and removing the sandbox;
    the documented contract is timeout + 60 s, else the answer (sandbox_removed) is lost as a runner failure."""
    from bananachat.services.agents import runner as runner_mod

    seen = {}
    with env.app.app_context():
        client = runner_mod.client()

        def fake_json(method, path, **kwargs):
            seen.update(kwargs)
            return {"exit_code": 0}
        monkeypatch.setattr(client, "_json", fake_json)
        client.exec("a" * 32, "ls", timeout=100)
    assert seen["timeout"] >= 160


# ----- archive downloads: one per person across processes, fetched before being streamed ------------------


def _finished_task(env, fake_ollama):
    fake_ollama.tool_script = [call("write_file", path="a.txt", content="hi"), call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    return task_id


def test_the_download_limit_holds_across_processes(env, runner, fake_ollama):
    from bananachat import db
    from bananachat.db import agents as agents_db

    task_id = _finished_task(env, fake_ollama)
    with env.app.app_context():
        token = agents_db.claim_download(env.user["id"])  # another web process is serving a download
        assert token and agents_db.claim_download(env.user["id"]) is None
    response = env.browser.fetch(f"/agents/{task_id}/workspace/archive")
    assert response.status_code == 429
    with env.app.app_context():
        agents_db.release_download(env.user["id"], token)
    response = env.browser.get(f"/agents/{task_id}/workspace/archive")
    assert response.status_code == 200
    response.close()  # a WSGI server closes every response once sent
    with env.app.app_context():
        assert agents_db.claim_download(env.user["id"])  # released after the download
        # A lease left by a process that died expires.
        db.execute("UPDATE runtime_state SET updated_at=? WHERE key=?",
                   (time.time() - agents_db.DOWNLOAD_LEASE_SECONDS - 1, f"agents:download:{env.user['id']}"))
    assert env.browser.get(f"/agents/{task_id}/workspace/archive").status_code == 200


def test_archives_are_fetched_from_the_runner_before_the_browser_reads_them(env, runner, fake_ollama, monkeypatch):
    """The browser's speed must not decide how long a runner archive slot stays taken."""
    from bananachat.services.agents import service

    task_id = _finished_task(env, fake_ollama)
    opened = []
    real_open = service.open_archive

    def tracking(task):
        upstream = real_open(task)
        opened.append(upstream)
        real_close = upstream.close
        upstream.closed_flag = False

        def close():
            upstream.closed_flag = True
            real_close()
        upstream.close = close
        return upstream
    monkeypatch.setattr(service, "open_archive", tracking)
    response = env.browser.get(f"/agents/{task_id}/workspace/archive", buffered=False)
    assert response.status_code == 200
    assert opened and opened[0].closed_flag  # read completely and closed before streaming began
    body = b"".join(response.response)
    response.close()
    assert body[:2] == b"\x1f\x8b"


# ----- malformed tool calls: refused cleanly, never a failed task ---------------------------------------


def test_deeply_nested_tool_call_arguments_are_refused_cleanly(env, runner, fake_ollama, monkeypatch):
    import json as real_json

    from tests.app import fake_ollama as fake_module

    deep = "[" * 5000 + "]" * 5000

    def dumps(payload, *args, **kwargs):
        return real_json.dumps(payload, *args, **kwargs).replace('"__DEEP__"', deep)
    monkeypatch.setattr(fake_module, "json", types.SimpleNamespace(dumps=dumps, loads=real_json.loads))
    nested: dict = {}
    for _ in range(200):
        nested = {"a": nested}
    fake_ollama.tool_script = [
        call("bash", command="__DEEP__"),                                       # too deep for any JSON parser
        {"tool_calls": [{"name": "bash", "arguments": {"command": "ls", "x": nested}}]},   # parseable but deep
        {"tool_calls": [{"name": "bash", "arguments": deep}]},                  # as a JSON string
        call("finish", summary="survived"),
    ]
    task_id = start_ok(env.browser)
    row = wait_status(env.app, task_id, "finished")
    assert row["summary"] == "survived"
    tools_log = [step for step in steps(env.app, task_id) if step["kind"] == "tool"]
    assert [step["tool_status"] for step in tools_log] == ["invalid", "invalid", "invalid", "ok"]
    assert all("nested too deeply" in step["tool_result"] for step in tools_log[:3]), \
        [step["tool_result"] for step in tools_log[:3]]
    assert not runner.execs


# ----- swarms: sub-agents reserve steps, so the step limit is exact -------------------------------------


def test_sub_agents_cannot_overshoot_the_step_limit(env, fake_ollama):
    set_settings(env.app, swarms_enabled=True, max_subagents=4, max_concurrent_subagents=4, max_steps=3)
    fake_ollama.tool_delay = 0.5

    def responder(body):
        if "You are a sub-agent" in body["messages"][0]["content"]:
            return call("bash", command="ls")  # never finishes by itself: only the budget stops it
        if body["messages"][-1]["role"] == "tool":
            return call("finish", summary="done")
        return call("delegate", tasks=[{"title": f"P{n}", "instructions": "work"} for n in range(4)])
    fake_ollama.tool_responder = responder
    task_id = start_ok(env.browser, swarm="1")
    row = wait_status(env.app, task_id, "out_of_budget", "finished", timeout=20)
    assert row["steps_used"] <= 3, row["steps_used"]
    assert sum(1 for step in steps(env.app, task_id) if step["kind"] == "assistant") <= 3
