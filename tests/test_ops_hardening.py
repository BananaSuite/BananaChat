"""Regressions for lifecycle housekeeping, privileged file handling, and CLI errors."""

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess

import pytest

from banana_ops import cli
from banana_ops.files import atomic_write, read_environment, read_json, write_environment, write_json
from banana_ops.manager import HISTORY_LIMIT, Manager
from banana_ops.source import GitSource, redact, valid_url
from test_managed_lifecycle import Services, checkout, installed, populate, revision  # noqa: F401


def run_cli(monkeypatch, manager, *arguments):
    monkeypatch.setattr(cli, 'Manager', lambda _: manager)
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    return cli.main(['--root', str(manager.root), *arguments])


def packages(manager, prefix, count):
    names = []
    for day in range(1, count + 1):
        path = manager.root / 'backups' / f'{prefix}-202601{day:02d}T000000Z-abcdef.tar.gz'
        path.write_bytes(b'fixture package')
        names.append(path.name)
    return names


def test_every_automatic_backup_kind_is_pruned_but_manual_and_rollback_packages_stay(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    manager.set_updates(False, keep=2)
    created = {prefix: packages(manager, prefix, 4)
               for prefix in ('auto', 'before-update', 'before-restore', 'before-legacy-import', 'remote', 'manual')}
    rollback = manager.root / 'backups' / created['before-update'][0]
    write_json(manager.config_dir / 'last-update.json', {'backup': str(rollback)})
    custom = manager.root / 'backups/auto-operator-copy.tar.gz'
    custom.write_bytes(b'operator named')
    caddy = manager.root / 'backups/Caddyfile-20260101T000000Z'
    caddy.write_text('saved proxy')
    manager.prune_backups()
    remaining = {path.name for path in (manager.root / 'backups').iterdir()}
    for prefix in ('auto', 'before-restore', 'before-legacy-import', 'remote'):
        assert {name for name in remaining if name.startswith(prefix + '-2026')} == set(created[prefix][-2:])
    assert {name for name in remaining if name.startswith('before-update-')} == {*created['before-update'][-2:], rollback.name}
    assert set(created['manual']) <= remaining
    assert custom.exists() and caddy.exists()


def test_a_completed_update_is_recorded_even_if_pruning_fails(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    new = revision(checkout)
    observed = []

    def failing_prune():
        observed.append(read_json(manager.config_dir / 'last-update.json')['revision'])
        raise OSError('fixture pruning failure')

    def failing_releases(_preserve):
        raise OSError('fixture release pruning failure')

    manager.prune_backups = failing_prune
    manager.prune_releases = failing_releases
    result = manager.update()
    assert result['outcome'] == 'complete' and manager.settings()['revision'] == new
    assert observed == [new]
    assert any('fixture pruning failure' in warning for warning in result['pruning_warnings'])
    assert any('release pruning failure' in warning for warning in result['pruning_warnings'])
    assert read_json(manager.config_dir / 'status.json')['outcome'] == 'complete'


@pytest.mark.parametrize('product,mode,key', [('BananaWiki', 'wiki', 'BW_SETUP_TOKEN'),
                                              ('BananaChat', 'single', 'BC_SETUP_TOKEN'),
                                              ('BananaChat', 'web', 'BC_SETUP_TOKEN')])
def test_a_removed_setup_token_is_not_recreated_by_update_or_restore(tmp_path, checkout, product, mode, key):
    manager, _ = installed(tmp_path, checkout, product, mode)
    path = manager.config_dir / 'app.env'
    assert len(read_environment(path)[key]) == 64
    environment = read_environment(path)
    del environment[key]
    write_environment(path, environment)
    revision(checkout)
    assert manager.update()['outcome'] == 'complete'
    assert key not in read_environment(path)
    archive = manager.backup()
    manager.restore(archive)
    assert key not in read_environment(path)
    restored = Manager(tmp_path / 'restored-root', product=product, system=Services())
    restored.restore(archive, new=True)
    assert key not in read_environment(restored.config_dir / 'app.env')


def test_an_existing_setup_token_survives_updates(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout, 'BananaChat', 'single')
    before = read_environment(manager.config_dir / 'app.env')['BC_SETUP_TOKEN']
    revision(checkout)
    manager.update()
    assert read_environment(manager.config_dir / 'app.env')['BC_SETUP_TOKEN'] == before


@pytest.fixture
def real_system(tmp_path, checkout, monkeypatch):
    import pwd
    from banana_ops.system import System
    manager, _ = installed(tmp_path, checkout)
    identity = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(pwd, 'getpwnam', lambda _: identity)
    system = System()
    system.log_dir = manager.config_dir
    return manager, system


def test_data_permissions_never_follow_links_or_change_hard_linked_files(tmp_path, real_system):
    manager, system = real_system
    data = manager.root / 'data'
    outside, shared = tmp_path / 'outside-secret', tmp_path / 'hard-linked-secret'
    for path in (outside, shared):
        path.write_text('keep this')
        path.chmod(0o604)
    (data / 'nested').mkdir()
    ordinary = data / 'nested/ordinary.txt'
    ordinary.write_text('service data')
    ordinary.chmod(0o644)
    (data / 'link').symlink_to(outside)
    os.link(shared, data / 'nested/hard')
    os.mkfifo(data / 'pipe')
    skipped = system.data_permissions(manager.settings())
    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / 'nested').stat().st_mode) == 0o700
    assert stat.S_IMODE(outside.stat().st_mode) == 0o604
    assert stat.S_IMODE(shared.stat().st_mode) == 0o604
    assert (data / 'link').is_symlink()
    assert {(path.name, reason) for path, reason in skipped} == {
        ('link', 'symbolic link'), ('hard', 'hard link'), ('pipe', 'special file')}
    record = read_json(manager.config_dir / 'data-permissions-skipped.json')
    assert {item['path'] for item in record['skipped']} == {'link', 'nested/hard', 'pipe'}
    (data / 'link').unlink()
    (data / 'nested/hard').unlink()
    (data / 'pipe').unlink()
    assert system.data_permissions(manager.settings()) == []
    assert not (manager.config_dir / 'data-permissions-skipped.json').exists()


def test_data_permissions_do_not_follow_an_entry_replaced_after_inspection(tmp_path, real_system, monkeypatch):
    manager, system = real_system
    entry = manager.root / 'data/switchme'
    entry.write_text('service data')
    outside = tmp_path / 'outside'
    outside.write_text('keep this')
    outside.chmod(0o604)
    original_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if path == 'switchme':
            entry.unlink()
            entry.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', replaced_open)
    skipped = system.data_permissions(manager.settings())
    assert entry.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o604
    assert [(path.name, reason) for path, reason in skipped] == [('switchme', 'changed during inspection')]


@pytest.mark.parametrize('url', ['-oProxyCommand=touch /tmp/x', '--upload-pack=touch x', 'git@-oProxy:team/repo.git',
                                 'git@forge.example:-repo.git', 'ssh://-oProxyCommand=x/repo.git', '-'])
def test_git_urls_that_look_like_options_are_rejected(url):
    with pytest.raises(ValueError):
        valid_url(url)
    with pytest.raises(ValueError):
        valid_url(url, allow_local=True)


def test_git_errors_include_a_redacted_diagnosis(tmp_path):
    root = tmp_path / 'managed'
    (root / 'staging').mkdir(parents=True)
    missing = tmp_path / 'missing-repository'
    source = GitSource(root, {'url': str(missing), 'branch': 'main', 'auth': 'none'})
    with pytest.raises(RuntimeError, match='Git reported: .*missing-repository'):
        source.resolve('a' * 40)
    text = ('fatal: unable to access https://bot:hunter2secret@forge.example/x.git: 403\n'
            'Authorization: Bearer abcdefghijklmnop\ntoken=ghp_abcdefghijklmnopqrstuvwx access denied for fixture-token-value')
    cleaned = redact(text, ('fixture-token-value',))
    for secret in ('hunter2secret', 'abcdefghijklmnop', 'ghp_', 'fixture-token-value'):
        assert secret not in cleaned
    assert 'access denied' in cleaned and '\n' not in cleaned
    assert len(redact('x' * 5000)) <= 600


def test_git_token_is_redacted_from_reported_errors(tmp_path, monkeypatch):
    root = tmp_path / 'managed'
    atomic_write(root / 'config/repo.token', 'fixture-private-token\n')
    source = GitSource(root, {'url': 'https://forge.example/team/repo.git', 'branch': 'main', 'auth': 'token'})

    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, '', 'remote: invalid credential fixture-private-token\n')

    monkeypatch.setattr('banana_ops.source.subprocess.run', failed)
    with pytest.raises(RuntimeError) as error:
        source.git('ls-remote', '--heads', '--', 'https://forge.example/team/repo.git')
    assert 'invalid credential' in str(error.value) and 'fixture-private-token' not in str(error.value)


def test_repository_cache_is_created_with_the_hardened_git_configuration(tmp_path, monkeypatch):
    import banana_ops.source as module
    calls = []
    original = module.subprocess.run

    def record(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(module.subprocess, 'run', record)
    GitSource(tmp_path / 'managed', {'url': 'https://forge.example/x.git', 'branch': 'main'}).initialize()
    init = next(command for command in calls if 'init' in command)
    assert 'core.hooksPath=/dev/null' in init and 'protocol.ext.allow=never' in init
    assert init[init.index('init'):] == ['init', '--bare', '--', str(tmp_path / 'managed/repository.git')]


def test_askpass_helper_is_written_only_when_missing_or_changed(tmp_path):
    root = tmp_path / 'managed'
    atomic_write(root / 'config/repo.token', 'fixture-private-token\n')
    source = GitSource(root, {'url': 'https://forge.example/x.git', 'branch': 'main', 'auth': 'token'})
    helper = root / 'config/git-askpass'
    source.environment()
    first = helper.stat()
    source.environment()
    assert helper.stat().st_ino == first.st_ino and helper.stat().st_mtime_ns == first.st_mtime_ns
    expected = helper.read_text()
    helper.write_text('#!/bin/sh\necho tampered\n')
    source.environment()
    assert helper.read_text() == expected and stat.S_IMODE(helper.stat().st_mode) == 0o700


def test_purge_refuses_directories_that_are_not_a_managed_installation(tmp_path, checkout, monkeypatch):
    import shutil
    manager, services = installed(tmp_path, checkout)
    name = manager.settings()['service']
    shutil.rmtree(manager.root / 'releases')
    with pytest.raises(ValueError, match='managed BananaWiki installation'):
        manager.uninstall(purge=True, confirm=name)
    (manager.root / 'releases').mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: manager.root))
    with pytest.raises(ValueError, match='home directory'):
        manager.uninstall(purge=True, confirm=name)
    # Refusal happens before services, timers, or metadata are touched.
    assert manager.settings()['installed'] and name in services.running
    monkeypatch.undo()
    assert manager.uninstall(purge=True, confirm=name) == {'outcome': 'uninstalled', 'data_preserved': False}
    assert not manager.root.exists()


def test_history_keeps_only_the_newest_events(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    history = manager.config_dir / 'history.jsonl'
    atomic_write(history, ''.join(json.dumps({'operation': 'old', 'number': n}) + '\n' for n in range(HISTORY_LIMIT + 50)))
    manager.event('fixture', 'complete', marker='newest')
    lines = history.read_text().splitlines()
    assert len(lines) == HISTORY_LIMIT
    assert json.loads(lines[-1])['marker'] == 'newest'
    assert json.loads(lines[0])['number'] == 51
    assert stat.S_IMODE(history.stat().st_mode) == 0o600


def test_a_bad_backend_token_file_is_rejected_before_any_installation_state(tmp_path, checkout):
    manager = Manager(tmp_path / 'managed', product='BananaChat', system=Services())
    with pytest.raises(ValueError, match='credential'):
        manager.install(checkout, mode='web', backend_url='https://compute.example.org',
                        backend_token_file=tmp_path / 'missing-token', source_options={'url': str(checkout), 'allow_local': True})
    for name in ('config/installation.json', 'config/source.json', 'config/app.env', 'current'):
        assert not (manager.root / name).exists() and not (manager.root / name).is_symlink()
    assert not any((manager.root / 'releases').iterdir())
    token = tmp_path / 'backend-token'
    token.write_text('fixture-backend-token\n')
    manager.install(checkout, mode='web', backend_url='https://compute.example.org', backend_token_file=token,
                    source_options={'url': str(checkout), 'allow_local': True})
    assert read_environment(manager.config_dir / 'app.env')['BC_OLLAMA_API_KEY'] == 'fixture-backend-token'


def queued_download(manager):
    with sqlite3.connect(manager.root / 'data/bananachat.db') as connection:
        connection.executescript('''
            CREATE TABLE ai_models (ollama_name TEXT, backend TEXT, backend_model_name TEXT);
            INSERT INTO ai_models VALUES ('tiny:latest', 'ollama', 'tiny:latest');
            CREATE TABLE model_pull_jobs (status TEXT, error_message TEXT, finished_at TEXT);
            INSERT INTO model_pull_jobs VALUES ('queued', NULL, NULL);
        ''')


def test_a_full_package_restore_does_not_ask_to_download_models(tmp_path, checkout, monkeypatch, capsys):
    manager, _ = installed(tmp_path, checkout, 'BananaChat', 'single')
    queued_download(manager)
    weights = manager.root / 'data/models/blobs/fixture'
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b'weights')
    archive = manager.backup()
    restored = Manager(tmp_path / 'restored-full', product='BananaChat', system=Services())
    restored.restore(archive, new=True)
    assert not (restored.root / 'data/.model-recovery.json').exists()
    assert (restored.root / 'data/models/blobs/fixture').read_bytes() == b'weights'
    with sqlite3.connect(restored.root / 'data/bananachat.db') as connection:
        assert connection.execute('SELECT status FROM model_pull_jobs').fetchone()[0] == 'cancelled'
    capsys.readouterr()
    assert run_cli(monkeypatch, manager, 'restore', str(archive)) == 0
    assert 'Model weights were not in this backup' not in capsys.readouterr().out


def test_model_download_hint_is_shown_only_after_a_weight_free_restore(tmp_path, checkout, monkeypatch, capsys):
    manager, _ = installed(tmp_path, checkout, 'BananaChat', 'single')
    queued_download(manager)
    archive = manager.backup(exclude_model_weights=True)
    capsys.readouterr()
    assert run_cli(monkeypatch, manager, 'restore', str(archive)) == 0
    assert 'Model weights were not in this backup' in capsys.readouterr().out
    assert read_json(manager.root / 'data/.model-recovery.json')['state'] == 'pending'
    assert run_cli(monkeypatch, manager, 'backups', 'status') == 0
    assert 'Model weights were not in this backup' not in capsys.readouterr().out
    record = read_json(manager.root / 'data/.model-recovery.json')
    write_json(manager.root / 'data/.model-recovery.json', {**record, 'state': 'deferred'})
    assert run_cli(monkeypatch, manager, 'restart') == 0
    assert 'Model weights were not in this backup' not in capsys.readouterr().out


def test_manual_retry_of_a_failed_revision_requires_retry_failed(tmp_path, checkout, monkeypatch, capsys):
    manager, services = installed(tmp_path, checkout)
    old = manager.settings()['revision']
    new = revision(checkout)
    services.fail_revision = new
    with pytest.raises(RuntimeError, match='readiness'):
        manager.update()
    services.fail_revision = None
    paused = manager.update()
    assert paused['outcome'] == 'paused' and '--retry-failed' in paused['reason']
    assert manager.settings()['revision'] == old
    capsys.readouterr()
    assert run_cli(monkeypatch, manager, 'update') == 2
    assert '--retry-failed' in capsys.readouterr().err
    manager.set_updates(True)
    assert manager.update(automatic=True)['outcome'] == 'paused'
    assert manager.update(retry_failed=True)['outcome'] == 'complete'
    assert manager.settings()['revision'] == new
    assert not (manager.config_dir / 'failed-revision.json').exists()


@pytest.mark.parametrize('error', [KeyError('revision'), sqlite3.OperationalError('database is locked')])
def test_cli_reports_the_original_error_even_if_recording_it_fails(tmp_path, checkout, monkeypatch, capsys, error):
    manager, _ = installed(tmp_path, checkout)

    def fail(**_):
        raise error

    def broken_event(*_args, **_kwargs):
        raise OSError('fixture disk full')

    monkeypatch.setattr(manager, 'update', fail)
    monkeypatch.setattr(manager, 'event', broken_event)
    capsys.readouterr()
    assert run_cli(monkeypatch, manager, 'update') == 1
    output = capsys.readouterr().err
    assert ('revision' if isinstance(error, KeyError) else 'database is locked') in output
    assert 'fixture disk full' in output


def test_legacy_import_module_is_neutral_and_the_old_name_still_works(tmp_path):
    from banana_ops import legacy_ai, legacy_import
    assert legacy_ai.restore_database is legacy_import.restore_database
    assert legacy_ai.stage_database is legacy_import.stage_database
    database = tmp_path / 'old.db'
    with sqlite3.connect(database) as connection:
        connection.executescript('''
            CREATE TABLE users (id INTEGER); CREATE TABLE chat_sessions (id INTEGER);
            CREATE TABLE chat_messages (id INTEGER); CREATE TABLE ai_models (id INTEGER);
        ''')
        connection.execute(f'PRAGMA application_id={legacy_import.DATABASE_APPLICATION_ID}')
    staging = tmp_path / 'staging'
    staging.mkdir()
    with legacy_import.stage_database(database, staging) as staged:
        assert staged.parent.name.startswith('legacy-import-')
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA application_id=1234')
    with pytest.raises(ValueError, match='not a BananaChat database'):
        with legacy_import.stage_database(database, staging):
            pass
