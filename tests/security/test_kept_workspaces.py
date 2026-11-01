"""Workspaces kept for downloads never keep anyone else's task from starting (AUDIT.md U2-2)."""

from __future__ import annotations

from tests.app.conftest import Browser
from tests.app.test_agents import (add_user, agents_app, call, env, fast_loop, runner, start_ok,  # noqa: F401
                                   token_file, wait_status)


def test_u2_2_kept_workspaces_of_one_person_give_way_to_another_persons_task(env, runner, fake_ollama,
                                                                          monkeypatch):
    from bananachat.services.agents import loop

    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 2)
    for index in range(runner.max_sandboxes):  # one person's short tasks, one after the other
        fake_ollama.tool_script = [call("list_files"), call("finish", summary="ok")]
        wait_status(env.app, start_ok(env.browser, f"tiny {index}"), "finished")
    assert len(runner.sandboxes) == runner.max_sandboxes

    add_user(env.app, "bob", allowed=True)
    bob = Browser(env.app)
    bob.login("bob")
    fake_ollama.tool_script = [call("list_files"), call("finish", summary="ok")]
    row = wait_status(env.app, start_ok(bob, "real work"), "failed", "finished", timeout=30)

    assert row["status"] == "finished", row["error"]


def _bobs_kept_workspaces(env, runner, count):
    from datetime import timedelta

    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod

    bob = add_user(env.app, "bob", allowed=True)
    kept = []
    with env.app.app_context():
        for n in range(count):
            task_id = f"{n:x}" * 32
            box = runner_mod.client().create(task_id)
            agents_db.create(task_id, user_id=bob["id"], title="t", prompt="p", model_id=None, model_name="m",
                             swarm=False, owner_token="tok", max_user=9, max_site=9)
            agents_db.finish(task_id, "tok", "finished")
            db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=?",
                       (box["id"], db.now(timedelta(minutes=30)), task_id))
            kept.append(box["id"])
    return kept


def test_v2_1_a_busy_runner_never_costs_anyone_their_kept_workspace(env, runner, fake_ollama, monkeypatch):
    from bananachat.services.agents import loop

    kept = _bobs_kept_workspaces(env, runner, 3)
    runner.max_sandboxes = 64
    runner.fail_create = (503, "busy")  # engine slots taken, or the runner stopping: not a lack of places
    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 0.5)
    fake_ollama.tool_script = [call("bash", command="ls")]
    wait_status(env.app, start_ok(env.browser), "failed")

    assert not set(kept) & set(runner.deleted)


def test_v2_1_one_start_frees_at_most_one_kept_workspace(env, runner, fake_ollama, monkeypatch):
    from bananachat.services.agents import loop

    kept = _bobs_kept_workspaces(env, runner, 3)
    runner.fail_create = (429, "capacity")  # a full runner whose removals do not free a place
    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 0.5)
    fake_ollama.tool_script = [call("bash", command="ls")]
    wait_status(env.app, start_ok(env.browser), "failed")

    assert len(set(kept) & set(runner.deleted)) == 1
