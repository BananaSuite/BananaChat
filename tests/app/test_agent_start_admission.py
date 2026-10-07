"""New task admission shares current access, import/start rates and SQLite capacity."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from bananachat import db
from bananachat.db import agents as agents_db, catalog, users
from bananachat.services.agents import service, settings
from tests.app.test_agents import (  # noqa: F401 - application and runner fixtures.
    add_user, agents_app, fast_loop, runner, token_file,
)
from tests.app.test_limits_tokens import _model_policy, _policy


def configure_rates(app):
    with app.app_context():
        rules = [{"requests": 100, "per": "hour", "burst": 100}]
        _policy("agent", rate={"enabled": True, "rules": rules})
        _model_policy(catalog.get_by_name("llama3.2:3b"), enabled=True, rate_rules=rules)


def start_import(user):
    return service.start_task(user, prompt="Import fixture", model_name="llama3.2:3b", swarm=False,
                              repo_url="https://github.com/octo/demo", repo_ref="main")


def assert_no_admission(user):
    assert agents_db.count_all(user_id=user["id"]) == 0
    assert users.count_hits(service.import_rate_key(user), 3600) == 0
    assert users.count_hits(f"agents:start:{user['id']}", 3600) == 0
    assert db.scalar("SELECT COUNT(*) FROM rate_buckets") == 0
    assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 0


@pytest.mark.parametrize("role", ["user", "admin"])
def test_concurrent_import_starts_obey_one_import_limit_for_all_roles(agents_app, monkeypatch, role):
    app = agents_app({"git_enabled": True, "git_imports_per_hour": 1,
                      "max_tasks_per_user": 3, "max_tasks_total": 4})
    configure_rates(app)
    user = add_user(app, "import-owner", allowed=True, role=role)
    observed = threading.Barrier(3)
    launches = []

    def capacity(_settings):
        assert not db.conn().in_transaction
        observed.wait(timeout=10)
        return 4

    monkeypatch.setattr(service, "site_limit", capacity)
    monkeypatch.setattr(service, "_launch", lambda *args: launches.append(args))

    def start(_index):
        with app.app_context():
            try:
                start_import(user)
                return 202
            except service.AgentError as error:
                assert error.key == "agents.error_import_rate"
                assert error.code == "rate_limited" and error.retry_after == 600
                return error.status
            finally:
                db.close_thread_connection()

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(start, range(3)))
    with app.app_context():
        print({"responses": responses, "tasks": agents_db.count_all(user_id=user["id"]),
               "import_hits": users.count_hits(service.import_rate_key(user), 3600),
               "launches": len(launches), "role": role})
    assert sorted(responses) == [202, 429, 429]
    with app.app_context():
        assert agents_db.count_all(user_id=user["id"]) == agents_db.active_count(user["id"]) == 1
        assert users.count_hits(service.import_rate_key(user), 3600) == 1
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == (1 if role == "user" else 0)
        assert db.scalar("SELECT COUNT(*) FROM rate_buckets") == (2 if role == "user" else 0)
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (user["id"],)) == 0
    assert len(launches) == 1


@pytest.mark.parametrize("scope", ["user", "site"])
def test_busy_import_start_rolls_back_import_and_start_admission(agents_app, monkeypatch, scope):
    app = agents_app({"git_enabled": True, "git_imports_per_hour": 1,
                      "max_tasks_per_user": 1, "max_tasks_total": 1 if scope == "site" else 4})
    configure_rates(app)
    user = add_user(app, "import-owner", allowed=True)
    owner = user if scope == "user" else add_user(app, "other-owner", allowed=True)
    monkeypatch.setattr(service, "site_limit", lambda *_: 4)
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("refused import launched a run"))
    with app.app_context():
        model = catalog.get_by_name("llama3.2:3b")
        agents_db.create("b" * 32, user_id=owner["id"], title="Existing task", prompt="p",
                         model_id=model["id"], model_name=model["ollama_name"], swarm=False,
                         owner_token="existing-owner", max_user=None, max_site=4)
        with pytest.raises(service.AgentError) as refusal:
            start_import(user)
        assert refusal.value.code == "busy"
        assert refusal.value.status == (409 if scope == "user" else 429)
        assert agents_db.count_all() == 1
        assert users.count_hits(service.import_rate_key(user), 3600) == 0
        assert users.count_hits(f"agents:start:{user['id']}", 3600) == 0
        assert db.scalar("SELECT COUNT(*) FROM rate_buckets") == 0


def test_failure_after_task_insert_rolls_back_task_and_all_admission(agents_app, monkeypatch):
    app = agents_app({"git_enabled": True, "git_imports_per_hour": 1})
    configure_rates(app)
    user = add_user(app, "import-owner", allowed=True)
    original_create = agents_db.create

    def fail_after_insert(*args, **kwargs):
        original_create(*args, **kwargs)
        raise OSError("fixture failure after creating task")

    monkeypatch.setattr(service, "site_limit", lambda *_: 4)
    monkeypatch.setattr(agents_db, "create", fail_after_insert)
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("uncommitted task launched a run"))
    with app.app_context():
        with pytest.raises(OSError, match="fixture failure after creating task"):
            start_import(user)
        assert_no_admission(user)


@pytest.mark.parametrize("change", ["suspension", "audience", "git_disabled", "hosts"])
def test_import_start_rechecks_access_and_repository_policy_after_health(agents_app, monkeypatch, change):
    app = agents_app({"git_enabled": True, "git_imports_per_hour": 1})
    configure_rates(app)
    user = add_user(app, "import-owner", allowed=True)

    def capacity(_settings):
        assert not db.conn().in_transaction
        if change == "suspension":
            users.suspend(user["id"])
        else:
            update = {"audience": {"access_mode": "admins"}, "git_disabled": {"git_enabled": False},
                      "hosts": {"git_hosts": ["gitlab.com"]}}[change]
            settings.save({**settings.current(fresh=True).to_dict(), **update}, None)
        return 4

    monkeypatch.setattr(service, "site_limit", capacity)
    monkeypatch.setattr(service, "_launch", lambda *_: pytest.fail("revoked start launched a run"))
    with app.app_context():
        with pytest.raises(service.AgentError) as refusal:
            start_import(user)
        assert refusal.value.status == (400 if change == "hosts" else 403)
        assert_no_admission(user)
