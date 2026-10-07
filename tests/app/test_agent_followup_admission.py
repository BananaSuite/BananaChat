"""Concurrent follow-ups share the pending bound and the actual SQLite lease."""

from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from bananachat import db
from bananachat.db import agents as agents_db, catalog, users
from bananachat.services.agents import service, settings
from tests.app.test_agents import (  # noqa: F401 - application and runner fixtures.
    add_user, agents_app, fast_loop, runner, token_file,
)


def fixture_task(app, *, active, pending=4):
    user = add_user(app, "followup-owner", allowed=True)
    task_id = "f" * 32
    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        agents_db.create(task_id, user_id=user["id"], title="Follow-up fixture", prompt="p",
                         model_id=model["id"], model_name=model["ollama_name"], swarm=False,
                         owner_token="fixture-owner", max_user=1, max_site=4)
        if not active:
            agents_db.finish(task_id, "fixture-owner", "finished")
        for index in range(pending):
            agents_db.add_message(task_id, user["id"], f"existing {index}")
        task = agents_db.get(task_id)
    return user, task


@pytest.mark.parametrize("active", [True, False])
def test_concurrent_owner_followups_keep_pending_bound_without_extra_start(agents_app, monkeypatch, active):
    app = agents_app()
    user, task = fixture_task(app, active=active)
    barrier = threading.Barrier(2)
    original_count = agents_db.pending_messages
    launched = []

    def count(task_id):
        result = original_count(task_id)
        # The old implementation observed both counts outside the write lock.
        # Correct admission performs this observation under BEGIN IMMEDIATE.
        if not db.conn().in_transaction:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(agents_db, "pending_messages", count)
    monkeypatch.setattr(service, "_launch", lambda *args: launched.append(args))
    if not active:
        def capacity(_settings):
            barrier.wait(timeout=10)
            return 4
        monkeypatch.setattr(service, "site_limit", capacity)

    def send(index):
        with app.app_context():
            try:
                service.follow_up(user, task, f"concurrent {index}")
                return 202
            except service.AgentError as error:
                assert error.key == "agents.error_too_many_messages"
                assert error.retry_after == 30
                return error.status
            finally:
                db.close_thread_connection()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(responses) == [202, 429]
    with app.app_context():
        assert original_count(task["id"]) == settings.MAX_PENDING_FOLLOW_UPS
        current = agents_db.get(task["id"])
        assert current["runs"] == (1 if active else 2)
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == (0 if active else 1)
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 0
    assert len(launched) == (0 if active else 1)


@pytest.mark.parametrize("active", [True, False])
def test_full_pending_queue_refuses_without_rate_or_lease_mutation(agents_app, monkeypatch, active):
    app = agents_app()
    user, task = fixture_task(app, active=active, pending=settings.MAX_PENDING_FOLLOW_UPS)
    monkeypatch.setattr(service, "_check_start", lambda *_: pytest.fail("refused follow-up debited start rates"))
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("refused follow-up launched a run"))
    monkeypatch.setattr(service, "site_limit", lambda *_: pytest.fail("full queue queried runner health"))
    with app.app_context():
        with pytest.raises(service.AgentError, match="agents.error_too_many_messages") as refusal:
            service.follow_up(user, task, "one too many")
        assert refusal.value.status == 429
        current = agents_db.get(task["id"])
        assert current["runs"] == task["runs"]
        assert current["owner_token"] == task["owner_token"]
        assert agents_db.pending_messages(task["id"]) == settings.MAX_PENDING_FOLLOW_UPS


@pytest.mark.parametrize("change", ["suspension", "audience", "deleted_task", "ownership"])
def test_resume_rechecks_eligibility_after_runner_health(agents_app, monkeypatch, change):
    app = agents_app()
    user, task = fixture_task(app, active=False, pending=0)
    other = add_user(app, "another-owner", allowed=True)

    def capacity(_settings):
        assert not db.conn().in_transaction  # HTTP health must not hold the write lock.
        if change == "suspension":
            users.suspend(user["id"])
        elif change == "audience":
            settings.save({**settings.current(fresh=True).to_dict(), "access_mode": "admins"}, None)
        elif change == "deleted_task":
            agents_db.delete(task["id"])
        else:
            db.execute("UPDATE agent_tasks SET user_id=? WHERE id=?", (other["id"], task["id"]))
        return 4

    monkeypatch.setattr(service, "site_limit", capacity)
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("revoked follow-up launched a run"))
    with app.app_context():
        with pytest.raises(service.AgentError) as refusal:
            service.follow_up(user, task, "must not run")
        assert refusal.value.status == (404 if change in ("deleted_task", "ownership") else 403)
        assert agents_db.pending_messages(task["id"]) == 0
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == 0
        current = agents_db.get(task["id"])
        assert current is None or current["owner_token"] is None


def test_followup_uses_current_task_state_instead_of_request_snapshot(agents_app, monkeypatch):
    app = agents_app()
    user, stale_task = fixture_task(app, active=True, pending=0)
    launched = []
    monkeypatch.setattr(service, "site_limit", lambda *_: 4)
    monkeypatch.setattr(service, "_launch", lambda *args: launched.append(args))
    with app.app_context():
        agents_db.finish(stale_task["id"], "fixture-owner", "finished")
        result = service.follow_up(user, stale_task, "continue the finished task")
        assert not result.queued_message
        assert len(launched) == 1
        assert agents_db.get(stale_task["id"])["runs"] == 2
        assert agents_db.pending_messages(stale_task["id"]) == 1


def test_resume_storage_failure_rolls_back_message_lease_and_rate_debits(agents_app, monkeypatch):
    app = agents_app()
    user, task = fixture_task(app, active=False, pending=0)
    original_resume = agents_db.resume

    def fail_after_lease(*args, **kwargs):
        assert original_resume(*args, **kwargs)
        raise OSError("fixture failure after resuming")

    monkeypatch.setattr(service, "site_limit", lambda *_: 4)
    monkeypatch.setattr(agents_db, "resume", fail_after_lease)
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("uncommitted lease launched a run"))
    with app.app_context():
        with pytest.raises(OSError, match="fixture failure after resuming"):
            service.follow_up(user, task, "must roll back")
        current = agents_db.get(task["id"])
        assert current["status"] == "finished" and current["owner_token"] is None
        assert current["runs"] == 1
        assert agents_db.pending_messages(task["id"]) == 0
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == 0
        assert db.scalar("SELECT COUNT(*) FROM rate_buckets WHERE key LIKE ?", (f"%:{user['id']}:%",)) == 0


@pytest.mark.parametrize("winner", ["manual_resume", "manual_stop", "deleted_task", "suspension"])
def test_automatic_continuation_rechecks_health_races_without_losing_rate_debits(agents_app, monkeypatch, winner):
    app = agents_app()
    user, task = fixture_task(app, active=False, pending=1)
    health_calls = 0
    launches = []

    def capacity(_settings):
        nonlocal health_calls
        assert not db.conn().in_transaction
        health_calls += 1
        if health_calls == 1:
            if winner in ("manual_resume", "manual_stop"):
                service.follow_up(user, task, "winning manual follow-up")
                if winner == "manual_stop":
                    current = agents_db.get(task["id"])
                    service.stop(current)
                    agents_db.finish(task["id"], current["owner_token"], "stopped")
            elif winner == "deleted_task":
                agents_db.delete(task["id"])
            else:
                users.suspend(user["id"])
        return 4

    monkeypatch.setattr(service, "site_limit", capacity)
    monkeypatch.setattr(service, "_launch", lambda *args: launches.append(args))
    service._after_run(SimpleNamespace(app=app, task_id=task["id"], outcome="finished"))
    with app.app_context():
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == (
            1 if winner in ("manual_resume", "manual_stop") else 0)
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 0
        current = agents_db.get(task["id"])
        if winner == "deleted_task":
            assert current is None
        else:
            assert current["runs"] == (2 if winner in ("manual_resume", "manual_stop") else 1)
            assert (current["owner_token"] is not None) == (winner == "manual_resume")
            if winner == "manual_stop":
                assert current["status"] == "stopped"
    assert len(launches) == (1 if winner in ("manual_resume", "manual_stop") else 0)
