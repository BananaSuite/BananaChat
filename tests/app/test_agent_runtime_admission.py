"""Queued and retried agent inference must honor current permissions and quotas."""

from __future__ import annotations

import pytest

from bananachat import db
from bananachat.db import access, catalog, credits, users
from bananachat.services import ollama, queue
from tests.app.conftest import Browser
from tests.app.test_agents import (  # noqa: F401 - agent integration fixtures.
    add_user, agents_app, call, error_key, fast_loop, runner, set_settings,
    start_ok, token_file, wait_for, wait_status)
from tests.app.test_limits_tokens import _model_policy, _policy


def waiting_request(app, task_id):
    with app.app_context():
        return db.one("SELECT req_id FROM inference_queue WHERE owner_key=? AND status='waiting'",
                      (f'agent:{task_id}:0',))


def revoke(app, user_id, model, change):
    with app.app_context():
        if change == 'suspension':
            users.suspend(user_id)
        elif change == 'agent_access':
            access.remove_membership('agents', 0, user_id, 'allowlist')
        elif change == 'disabled':
            set_settings(app, enabled=False)
        elif change == 'admins_only':
            set_settings(app, access_mode='admins')
        elif change == 'rollout':
            catalog.set_rollout(model['id'], False)
        elif change == 'model_access':
            access.set_policy('model', model['id'], 'deny_except_allowlist', False, None)
        elif change == 'tool_support':
            set_settings(app, model_overrides={str(model['id']): 'deny'})
        elif change == 'agent_tokens':
            credits.charge(user_id, 10, 0, request_type='agent', model_id=model['id'])
        elif change == 'model_tokens':
            credits.charge(user_id, 10, 0, request_type='api', model_id=model['id'])
        else:
            raise AssertionError(change)


@pytest.mark.parametrize('change,state,key', [
    ('suspension', 'stopped', 'agents.stop_account'),
    ('agent_access', 'stopped', 'agents.stop_access'),
    ('disabled', 'stopped', 'agents.stop_disabled'),
    ('admins_only', 'stopped', 'agents.stop_access'),
    ('rollout', 'failed', 'agents.stop_model'),
    ('model_access', 'failed', 'agents.stop_model'),
    ('tool_support', 'failed', 'agents.stop_model'),
    ('agent_tokens', 'out_of_budget', 'agents.stop_credits'),
    ('model_tokens', 'out_of_budget', 'agents.stop_model_limit'),
])
def test_queued_agent_rechecks_current_admission(agents_app, runner, fake_ollama, change, state, key):
    app = agents_app({'access_mode': 'custom'}, MAX_CONCURRENT=1)
    user = add_user(app, 'alice', allowed=True)
    browser = Browser(app)
    browser.login('alice')
    with app.app_context():
        model = catalog.get_by_name('llama3.2:3b')
        if change == 'agent_tokens':
            _policy('agent', window={'enabled': True, 'tokens': 10, 'dynamic': False, 'auto_tiers': False})
        elif change == 'model_tokens':
            _model_policy(model, enabled=True, window_tokens=10)
        # Occupy actual shared inference capacity, without calling a provider.
        with queue.Slot(queue.PRIORITY_ADMIN, owner_key='test:blocker') as blocker:
            blocker.wait(timeout=2)
            task_id = start_ok(browser, model=model['ollama_name'])
            wait_for(lambda: waiting_request(app, task_id), message='agent waiting in inference queue')
            revoke(app, user['id'], model, change)
    row = wait_status(app, task_id, state)
    assert error_key(app, row['error']) == key
    assert not fake_ollama.chat_bodies()
    assert not runner.execs
    with app.app_context():
        assert db.scalar('SELECT COUNT(*) FROM inference_queue') == 0
        assert db.scalar('SELECT COUNT(*) FROM credit_ledger WHERE user_id=?', (user['id'],)) == (
            1 if change in ('agent_tokens', 'model_tokens') else 0)
        assert row['owner_token'] is None


@pytest.mark.parametrize('change,state,key', [
    ('suspension', 'stopped', 'agents.stop_account'),
    ('agent_access', 'stopped', 'agents.stop_access'),
    ('disabled', 'stopped', 'agents.stop_disabled'),
    ('rollout', 'failed', 'agents.stop_model'),
    ('model_access', 'failed', 'agents.stop_model'),
    ('agent_tokens', 'out_of_budget', 'agents.stop_credits'),
])
def test_retry_rechecks_admission_after_first_provider_failure(agents_app, runner, fake_ollama,
                                                             monkeypatch, change, state, key):
    app = agents_app({'access_mode': 'custom'})
    user = add_user(app, 'alice', allowed=True)
    browser = Browser(app)
    browser.login('alice')
    with app.app_context():
        model = catalog.get_by_name('llama3.2:3b')
        if change == 'agent_tokens':
            _policy('agent', window={'enabled': True, 'tokens': 10, 'dynamic': False, 'auto_tiers': False})
    original = ollama.chat_stream
    calls = []

    def fail_and_revoke(name, *args, **kwargs):
        calls.append(name)
        revoke(app, user['id'], model, change)
        yield from original(name, *args, **kwargs)

    monkeypatch.setattr(ollama, 'chat_stream', fail_and_revoke)
    fake_ollama.fail_models = {model['ollama_name']}
    task_id = start_ok(browser, model=model['ollama_name'])
    row = wait_status(app, task_id, state)
    assert error_key(app, row['error']) == key
    assert calls == [model['ollama_name']]
    assert len(fake_ollama.chat_bodies()) == 1
    assert not runner.execs
    with app.app_context():
        assert db.scalar('SELECT COUNT(*) FROM inference_queue') == 0
        assert db.scalar('SELECT COUNT(*) FROM credit_ledger WHERE user_id=?', (user['id'],)) == (
            1 if change == 'agent_tokens' else 0)
        assert row['owner_token'] is None


def test_lowered_step_limit_stops_an_already_queued_next_step(agents_app, runner, fake_ollama):
    app = agents_app({'access_mode': 'custom'}, MAX_CONCURRENT=1)
    add_user(app, 'alice', allowed=True)
    browser = Browser(app)
    browser.login('alice')
    blockers = []

    def hold_next_model_call(box, command, timeout):
        # The first answer has been charged before its tool runs. Hold real
        # inference capacity so the next step queues under the original limit.
        with app.app_context():
            blocker = queue.Slot(queue.PRIORITY_ADMIN, owner_key='test:blocker')
            blocker.__enter__()
            blockers.append(blocker)
            blocker.wait(timeout=2)
        return {'stdout': 'done', 'exit_code': 0}

    runner.exec_handler = hold_next_model_call
    fake_ollama.tool_script = [call('bash', command='true')]
    task_id = start_ok(browser, model='llama3.2:3b')
    try:
        wait_for(lambda: waiting_request(app, task_id), message='second agent step waiting for inference')
        with app.app_context():
            assert db.scalar('SELECT steps_used FROM agent_tasks WHERE id=?', (task_id,)) == 1
        set_settings(app, max_steps=1)
    finally:
        with app.app_context():
            for blocker in blockers:
                blocker.release()
    row = wait_status(app, task_id, 'out_of_budget')
    assert error_key(app, row['error']) == 'agents.stop_steps'
    assert row['steps_used'] == 1
    assert len(fake_ollama.chat_bodies()) == 1
    assert len(runner.execs) == 1
    with app.app_context():
        assert db.scalar('SELECT COUNT(*) FROM inference_queue') == 0
        assert row['owner_token'] is None


def test_lowered_swarm_step_limit_counts_both_queued_reservations(agents_app, runner, fake_ollama, monkeypatch):
    from bananachat.services.agents import loop

    app = agents_app({'access_mode': 'custom', 'swarms_enabled': True,
                      'max_steps': 40, 'max_subagents': 2, 'max_concurrent_subagents': 2}, MAX_CONCURRENT=2)
    add_user(app, 'alice', allowed=True)
    browser = Browser(app)
    browser.login('alice')
    blockers = []
    original = loop.TaskRun._delegate

    def block_parallel_steps(run, agent, arguments):
        # The orchestrator's first answer is complete. Both subagents must
        # reserve real steps before the administrator tightens their shared cap.
        with app.app_context():
            for number in range(2):
                blocker = queue.Slot(queue.PRIORITY_ADMIN, owner_key=f'test:blocker:{number}')
                blocker.__enter__()
                blockers.append(blocker)
                blocker.wait(timeout=2)
        return original(run, agent, arguments)

    monkeypatch.setattr(loop.TaskRun, '_delegate', block_parallel_steps)
    fake_ollama.tool_script = [call('delegate', tasks=[
        {'title': 'First part', 'instructions': 'Report the first part'},
        {'title': 'Second part', 'instructions': 'Report the second part'}])]
    # Without the fresh reservation gate both calls begin before either has
    # completed; their completed-step counter alone would incorrectly pass.
    fake_ollama.tool_delay = 0.4
    task_id = start_ok(browser, model='llama3.2:3b', swarm='1')

    def both_lanes_waiting():
        with app.app_context():
            return db.scalar("SELECT COUNT(*) FROM inference_queue WHERE status='waiting' AND "
                             'owner_key IN (?,?)', (f'agent:{task_id}:1', f'agent:{task_id}:2')) == 2

    try:
        wait_for(both_lanes_waiting, message='both reserved swarm steps waiting for inference')
        run = loop.local_run(task_id)
        assert run is not None
        with run.budget.lock:
            assert run.budget.steps == 1 and run.budget.reserved == 2
        set_settings(app, max_steps=2)
    finally:
        with app.app_context():
            for blocker in blockers:
                blocker.release()
    row = wait_status(app, task_id, 'out_of_budget')
    assert error_key(app, row['error']) == 'agents.stop_steps'
    assert row['steps_used'] <= 2
    assert len(fake_ollama.chat_bodies()) <= 2
    assert not runner.execs
    with run.budget.lock:
        assert run.budget.reserved == 0
    with app.app_context():
        assert db.scalar('SELECT COUNT(*) FROM inference_queue') == 0
        assert row['owner_token'] is None
