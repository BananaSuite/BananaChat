"""Kept workspaces taken by a full runner, races around them (AUDIT.md X2-1, X2-2, Y2-1, Y2-2, Z-1)."""

import threading
import time
import types
from datetime import timedelta

from tests.app.test_agents import (add_user, agents_app, call, env, fast_loop,  # noqa: F401
                                   runner, start_ok, token_file, wait_status)


def test_x2_1_a_failed_runner_delete_still_frees_at_most_one_workspace(env, runner, fake_ollama, monkeypatch):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import loop
    from bananachat.services.agents import runner as runner_mod

    bob = add_user(env.app, "bob", allowed=True)
    kept = []
    with env.app.app_context():
        for n in range(3):
            task_id = f"{n:x}" * 32
            box = runner_mod.client().create(task_id)
            agents_db.create(task_id, user_id=bob["id"], title="t", prompt="p", model_id=None, model_name="m",
                             swarm=False, owner_token="tok", max_user=9, max_site=9)
            agents_db.finish(task_id, "tok", "finished")
            db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=?",
                       (box["id"], db.now(timedelta(minutes=30)), task_id))
            runner.sandboxes[box["id"]].write("/workspace/result.txt", b"bob's results")
            kept.append(box["id"])

    real_delete = runner_mod.Runner.delete
    failed = []

    def flaky_delete(self, sandbox_id):
        if sandbox_id in kept:  # e.g. the runner's DELETE times out behind its cleanup lock, or a proxy 502/504
            failed.append(sandbox_id)
            raise runner_mod.RunnerError("The sandbox runner did not answer in time.", None, transient=True)
        return real_delete(self, sandbox_id)

    monkeypatch.setattr(runner_mod.Runner, "delete", flaky_delete)
    runner.fail_create = (429, "capacity")  # genuinely full
    monkeypatch.setattr(loop, "SANDBOX_WAIT_SECONDS", 7)
    fake_ollama.tool_script = [call("bash", command="ls")]
    wait_status(env.app, start_ok(env.browser), "failed", timeout=40)

    with env.app.app_context():
        rows = db.query("SELECT id, sandbox_id FROM agent_tasks WHERE user_id=? ORDER BY id", (bob["id"],))
    print("delete attempted on:", failed)
    print("bob's rows:", [(r["id"][:4], r["sandbox_id"]) for r in rows])
    still_on_runner = [s for s in kept if s in runner.sandboxes]
    print("still on runner but no longer named by any task:", still_on_runner)
    # Documented / commit claim: at most one kept workspace per start.
    lost = [r for r in rows if r["sandbox_id"] is None]
    assert len(lost) <= 1, f"{len(lost)} of bob's kept workspaces were taken off their tasks by one start"





def test_x2_2_recovery_never_writes_back_a_sandbox_the_task_no_longer_has(env, runner, monkeypatch):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod
    from bananachat.services.agents import service

    bob = add_user(env.app, "bob", allowed=True)
    task_id = "b" * 32
    with env.app.app_context():
        s1 = runner_mod.client().create(task_id)["id"]
        agents_db.create(task_id, user_id=bob["id"], title="t", prompt="p", model_id=None, model_name="m",
                         swarm=False, owner_token="dead", max_user=9, max_site=9)
        agents_db.set_sandbox(task_id, "dead", s1, None)
        # The process running it died: the lease is stale, recovery marks it interrupted and keeps S1.
        db.execute("UPDATE agent_tasks SET heartbeat_at=0 WHERE id=?", (task_id,))
        ids = agents_db.recover_stale(keep_until=db.now(timedelta(minutes=30)), error="x")
        assert ids == [task_id]

    entered, go = threading.Event(), threading.Event()
    real_interrupt = runner_mod.Runner.interrupt

    def slow_interrupt(self, sandbox_id):  # the leftover command takes a moment to kill
        entered.set()
        go.wait(10)
        return real_interrupt(self, sandbox_id)

    monkeypatch.setattr(runner_mod.Runner, "interrupt", slow_interrupt)

    def recovery():
        with env.app.app_context():
            service._stop_leftovers([task_id])

    worker = threading.Thread(target=recovery)
    worker.start()
    assert entered.wait(10)
    with env.app.app_context():
        # Another person's start meets a full runner: the reclaim takes S1 and deletes it ...
        row = agents_db.take_kept_sandbox()
        assert row["sandbox_id"] == s1
        runner_mod.client().delete(s1)
        # ... bob sends a follow-up: the task resumes and its run creates S2 (ensure_sandbox).
        assert agents_db.resume(task_id, owner_token="new", max_user=9, max_site=9)
        s2 = runner_mod.client().create(task_id)["id"]
        assert agents_db.set_sandbox(task_id, "new", s2, None)
    go.set()
    worker.join(10)
    with env.app.app_context():
        named = agents_db.get(task_id)["sandbox_id"]
        known = agents_db.known_sandboxes()
    print("S1 (deleted) =", s1, " S2 (live) =", s2, " row names:", named, " S2 known:", s2 in known)
    assert named == s2, "the running task's row now names its deleted sandbox; its live one is unknown"


# Y2-1, Y2-2: giving a taken workspace back



def _kept(env, runner, owner, task_id, minutes=30):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import runner as runner_mod
    with env.app.app_context():
        box = runner_mod.client().create(task_id)
        agents_db.create(task_id, user_id=owner["id"], title="t", prompt="p", model_id=None, model_name="m",
                         swarm=False, owner_token="tok", max_user=9, max_site=9)
        agents_db.finish(task_id, "tok", "finished")
        db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=?",
                   (box["id"], db.now(timedelta(minutes=minutes)), task_id))
    runner.sandboxes[box["id"]].created_at = time.time() - 3600  # made by a run an hour ago
    return box["id"]


def test_y2_1_a_workspace_the_reaper_deleted_meanwhile_is_not_given_back(env, runner, monkeypatch):
    """Reclaim takes S1 off bob's task; its runner DELETE fails (timeout). Meanwhile the reaper's unknown-sandbox
    sweep (S1 is in no row now) deletes S1. give_back then writes S1 back: the row names a sandbox that is gone."""
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import loop, service
    from bananachat.services.agents import runner as runner_mod

    bob = add_user(env.app, "bob", allowed=True)
    task_id = "b" * 32
    s1 = _kept(env, runner, bob, task_id)

    real_delete = runner_mod.Runner.delete
    state = {"reclaim_call": True}

    def delete(self, sandbox_id):
        if state["reclaim_call"] and sandbox_id == s1:
            state["reclaim_call"] = False
            # While the reclaim's DELETE hangs (up to 60 s), the reaper job (every 60 s) runs:
            removed = service.reap_sandboxes()
            print("reaper removed", removed, "; S1 still on runner:", s1 in runner.sandboxes)
            raise runner_mod.RunnerError("The sandbox runner did not answer in time.", None, transient=True)
        return real_delete(self, sandbox_id)

    monkeypatch.setattr(runner_mod.Runner, "delete", delete)
    with env.app.app_context():
        fake_run = types.SimpleNamespace(runner=runner_mod.client(), task_id="a" * 32)
        freed = loop.TaskRun._reclaim_kept_sandbox(fake_run)
        row = agents_db.get(task_id)
    print("reclaim freed:", freed, " row names:", row["sandbox_id"], " on runner:", s1 in runner.sandboxes)
    assert not (row["sandbox_id"] == s1 and s1 not in runner.sandboxes), \
        "bob's task row names a sandbox the runner deleted (workspace shown, downloads 404, follow-up loses a reset)"


def test_y2_2_a_give_back_never_lengthens_a_keep_time(env, runner, monkeypatch):
    """Recovery (service._stop_leftovers) could not stop the leftover processes of a crashed run, so it marks the
    sandbox for removal now (expire_sandbox_now). A reclaim that took S1 meanwhile and could not delete it gives
    it back with its old keep time: the sandbox, its processes still running, is kept for 30 more minutes."""
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import loop, service
    from bananachat.services.agents import runner as runner_mod

    bob = add_user(env.app, "bob", allowed=True)
    task_id = "c" * 32
    with env.app.app_context():
        s1 = runner_mod.client().create(task_id)["id"]
        agents_db.create(task_id, user_id=bob["id"], title="t", prompt="p", model_id=None, model_name="m",
                         swarm=False, owner_token="dead", max_user=9, max_site=9)
        agents_db.set_sandbox(task_id, "dead", s1, None)
        db.execute("UPDATE agent_tasks SET heartbeat_at=0 WHERE id=?", (task_id,))
        assert agents_db.recover_stale(keep_until=db.now(timedelta(minutes=30)), error="x") == [task_id]

    taken, delete_may_fail = threading.Event(), threading.Event()
    real_delete = runner_mod.Runner.delete

    def interrupt(self, sandbox_id):  # the runner is overloaded: interrupt times out (30 s) ...
        assert taken.wait(10)
        raise runner_mod.RunnerError("The sandbox runner did not answer in time.", None, transient=True)

    def delete(self, sandbox_id):  # ... and so does the reclaim's DELETE (60 s), which started later
        if sandbox_id == s1:
            taken.set()
            assert delete_may_fail.wait(10)
            raise runner_mod.RunnerError("The sandbox runner did not answer in time.", None, transient=True)
        return real_delete(self, sandbox_id)

    monkeypatch.setattr(runner_mod.Runner, "interrupt", interrupt)
    monkeypatch.setattr(runner_mod.Runner, "delete", delete)

    def reclaim():  # another person's start meets a full runner
        with env.app.app_context():
            loop.TaskRun._reclaim_kept_sandbox(types.SimpleNamespace(runner=runner_mod.client(), task_id="a" * 32))

    def recovery():
        with env.app.app_context():
            service._stop_leftovers([task_id])

    rec = threading.Thread(target=recovery)
    rec.start()
    time.sleep(0.3)  # recovery has read the row (S1) and is in interrupt
    worker = threading.Thread(target=reclaim)
    worker.start()
    rec.join(15)
    delete_may_fail.set()
    worker.join(15)
    with env.app.app_context():
        row = agents_db.get(task_id)
        now = db.now()
    print("row names:", row["sandbox_id"], " expires:", row["sandbox_expires_at"], " now:", now)
    assert not (row["sandbox_id"] == s1 and row["sandbox_expires_at"] > now), \
        "recovery's 'remove now' (processes could not be stopped) was undone: kept for the full keep time"


# Z-1: the reaper's stale list and a give-back

def test_z_1_a_workspace_the_reaper_deletes_after_its_give_back_is_forgotten(env, runner, monkeypatch):
    from bananachat import db
    from bananachat.db import agents as agents_db
    from bananachat.services.agents import loop, service
    from bananachat.services.agents import runner as runner_mod

    bob = add_user(env.app, "bob", allowed=True)
    with env.app.app_context():
        # U: an old leftover sandbox of a deleted task, listed before S1 (the reaper deletes it first).
        u = runner_mod.client().create("e" * 32)["id"]
        task_id = "b" * 32
        s1 = runner_mod.client().create(task_id)["id"]
        agents_db.create(task_id, user_id=bob["id"], title="t", prompt="p", model_id=None, model_name="m",
                         swarm=False, owner_token="tok", max_user=9, max_site=9)
        agents_db.finish(task_id, "tok", "finished")
        db.execute("UPDATE agent_tasks SET sandbox_id=?, sandbox_expires_at=? WHERE id=?",
                   (s1, db.now(timedelta(minutes=30)), task_id))
    for box in (u, s1):
        runner.sandboxes[box].created_at = time.time() - 3600

    # Timeline (overloaded runner, as in Y2-1):
    #  t0 a start meets a full runner: reclaim takes S1 off bob's task; its DELETE hangs (60 s timeout).
    #  t1 the reaper job runs: lists the runner, reads `known` (S1 named by no row), starts deleting unknown
    #     old sandboxes one by one; U (first) is slow.
    #  t2 the reclaim's DELETE times out; exists(S1) is True (the reaper has not reached it); S1 given back.
    #  t3 the reaper, working from its t1 snapshot, deletes S1. Bob's row names a deleted sandbox.
    taken = {}
    real_take, real_known = agents_db.take_kept_sandbox, agents_db.known_sandboxes
    real_delete = runner_mod.Runner.delete

    def known():  # t0 happened just before the reaper reads `known`
        taken["row"] = real_take()
        assert taken["row"]["sandbox_id"] == s1
        return real_known()

    def delete(self, sandbox_id):
        if sandbox_id == s1 and taken.get("in_reclaim"):
            raise runner_mod.RunnerError("The sandbox runner did not answer in time.", None, transient=True)
        if sandbox_id == u and "row" in taken and not taken.get("done"):
            taken["done"] = taken["in_reclaim"] = True  # t2, while the reaper is on U
            monkeypatch.setattr(agents_db, "take_kept_sandbox", lambda: taken["row"])
            with env.app.app_context():
                fake_run = types.SimpleNamespace(runner=runner_mod.client(), task_id="a" * 32)
                freed = loop.TaskRun._reclaim_kept_sandbox(fake_run)
                print("reclaim freed:", freed, " row after give-back:", agents_db.get(task_id)["sandbox_id"])
            taken["in_reclaim"] = False
        return real_delete(self, sandbox_id)

    monkeypatch.setattr(agents_db, "known_sandboxes", known)
    monkeypatch.setattr(runner_mod.Runner, "delete", delete)
    with env.app.app_context():
        removed = service.reap_sandboxes()
        row = agents_db.get(task_id)
    print("reaper removed", removed, " row names:", row["sandbox_id"], row["sandbox_expires_at"],
          " S1 on runner:", s1 in runner.sandboxes)
    assert not (row["sandbox_id"] == s1 and s1 not in runner.sandboxes), \
        "bob's task names a sandbox the reaper deleted after the give-back's exists() check"
