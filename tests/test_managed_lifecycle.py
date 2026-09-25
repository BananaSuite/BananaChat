"""Exercise real Git updates and portable data restore without modifying host services."""

from dataclasses import dataclass
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

from banana_ops.files import (atomic_write, digest_file, extract_archive, read_environment, read_json, read_package,
                              regular_files, write_environment, write_json, write_package)
from banana_ops.gate import MaintenanceGate
from banana_ops.manager import Manager
from banana_ops import profile
from banana_ops.source import GitSource, valid_url


def git(*args):
    return subprocess.run(['git', *map(str, args)], capture_output=True, text=True, check=True).stdout.strip()


class Services:
    def __init__(self):
        self.running = set()
        self.timers = {}
        self.fail_revision = None
        self.on_health = None
        self.prepared = []
        self.observed_maintenance = False
        self.tenant_records = []

    def preflight(self, settings):
        pass

    def account(self, settings):
        pass

    def data_permissions(self, settings):
        pass

    def prepare_release(self, settings, release):
        self.prepared.append(settings['revision'])

    def install_units(self, settings):
        pass

    def install_timer(self, settings, policy):
        self.timers[settings['service']] = policy.copy()

    def install_backup_timer(self, settings, policy):
        self.timers[settings['service'] + '-backup'] = policy.copy()

    def active(self, name):
        return name in self.running

    def start(self, names):
        self.running.update(names)

    def stop(self, names):
        self.running.difference_update(names)

    def containers(self, settings):
        return self.tenant_records

    def stop_containers(self, records):
        pass

    def resume_containers(self, records):
        pass

    def remove_containers(self, records):
        pass

    def healthy(self, settings, containers=(), timeout=180):
        self.observed_maintenance = (Path(settings['root']) / 'data/.banana-maintenance').exists()
        if self.on_health:
            self.on_health(settings)
        return settings['revision'] != self.fail_revision

    def uninstall(self, settings):
        self.running.clear()


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git('init', '-b', 'main', source)
    git('-C', source, 'config', 'user.name', 'Fixture')
    git('-C', source, 'config', 'user.email', 'fixture@example.invalid')
    (source / 'banana').write_text('# managed entry point\n')
    (source / 'LICENSE').write_text('Fixture source\n')
    (source / 'version.txt').write_text('one\n')
    git('-C', source, 'add', '.')
    git('-C', source, 'commit', '-m', 'Initial fixture')
    return source


def revision(source, value='two'):
    (source / 'version.txt').write_text(value + '\n')
    git('-C', source, 'add', '.')
    git('-C', source, 'commit', '-m', 'Update fixture')
    return git('-C', source, 'rev-parse', 'HEAD')


def installed(tmp_path, checkout, product='BananaWiki', mode='wiki'):
    services = Services()
    manager = Manager(tmp_path / 'managed', product=product, system=services)
    model_binary = tmp_path / 'ollama'
    model_binary.write_text('fixture')
    manager.install(checkout, mode=mode, backend_url='https://compute.example.org' if mode == 'web' else '',
                    ollama_binary=str(model_binary), source_options={'url': str(checkout), 'allow_local': True})
    return manager, services


def populate(manager):
    database = manager.root / 'data' / ('hosting.db' if manager.settings()['mode'] == 'hosting' else 'example.db')
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE records (value TEXT)')
        connection.execute("INSERT INTO records VALUES ('customer content')")
    (manager.root / 'data/private.key').write_text('private fixture key')
    return database


@pytest.mark.parametrize('product,mode', [('BananaWiki', 'wiki'), ('BananaWiki', 'hosting'), ('BananaChat', 'single'), ('BananaChat', 'web'), ('BananaChat', 'compute')])
def test_every_mode_is_remembered_and_updates_are_off(tmp_path, checkout, product, mode):
    manager, services = installed(tmp_path, checkout, product, mode)
    assert manager.settings()['mode'] == mode
    assert manager.policy()['enabled'] is False
    assert services.timers[product.lower()]['enabled'] is False
    assert services.observed_maintenance
    assert not (manager.root / 'data/.banana-maintenance').exists()
    assert manager.update(automatic=True)['outcome'] == 'disabled'


@pytest.mark.parametrize('product,mode', [('BananaWiki', 'wiki'), ('BananaWiki', 'hosting'),
                                         ('BananaChat', 'single'), ('BananaChat', 'web'), ('BananaChat', 'compute')])
def test_remote_backup_restores_each_deployment_into_a_new_root(tmp_path, checkout, monkeypatch, product, mode):
    """Exercise the CLI hooks through real encryption, Git, and portable restore."""
    import shutil
    if not shutil.which('age') or not shutil.which('age-keygen'):
        pytest.fail('Install age to run the encrypted backup tests.')
    from banana_backup import cli, crypto, store as storage
    from banana_backup.files import atomic_write as private_write
    from banana_backup.git import Git
    from banana_backup.store import Store

    remote = tmp_path / 'remote.git'
    git('init', '--bare', remote)
    monkeypatch.setattr(cli, 'check_private', lambda *_: True)
    monkeypatch.setattr(storage, 'check_private', lambda *_: True)

    @contextmanager
    def local_transport(self, config):
        with tempfile.TemporaryDirectory(dir=self.root) as name:
            yield Git(Path(name), {**config, 'url': str(remote)}, self.token, allow_local=True)

    monkeypatch.setattr(Store, 'transport', local_transport)
    key, token = tmp_path / 'recovery.agekey', tmp_path / 'repository.token'
    crypto.keygen(key)
    private_write(token, 'fixture-repository-token')
    manager, services = installed(tmp_path, checkout, product, mode)
    database = populate(manager)
    weights = manager.root / 'data/models/blobs/fixture'
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b'weights')
    store = Store(manager.config_dir / 'remote-backup', product)
    settings = dict(repo='https://forge.example/team/backups.git', forge='forgejo', name='production',
                    token_file=token, key_file=key, max_mib=2)
    store.configure(**settings)
    running = services.running.copy()
    package = manager.backup_name('remote')
    result = cli.handle(SimpleNamespace(backup_action='run', automatic=False), store,
                        create_package=lambda: manager.backup(package, exclude_model_weights=True),
                        restore_package=None)
    assert result['verified'] and not package.exists()
    assert services.running == running

    restored = Manager(tmp_path / 'new-server', product=product, system=Services())
    destination = Store(restored.config_dir / 'remote-backup', product)
    destination.configure(**settings)
    destination.set_schedule(True)
    cli.handle(SimpleNamespace(backup_action='restore', snapshot=result['snapshot']), destination,
               create_package=None, restore_package=lambda path, _: restored.restore(path))
    assert restored.settings()['mode'] == mode
    assert not restored.policy()['enabled'] and not destination.schedule()['enabled']
    with sqlite3.connect(restored.root / 'data' / database.name) as connection:
        assert connection.execute('SELECT value FROM records').fetchone()[0] == 'customer content'
    assert (restored.root / 'data/private.key').read_text() == 'private fixture key'
    if product == 'BananaChat':
        assert not (restored.root / 'data/models/blobs/fixture').exists()
        assert json.loads((restored.root / 'data/.model-recovery.json').read_text())['state'] == 'pending'
    else:
        assert (restored.root / 'data/models/blobs/fixture').read_bytes() == b'weights'


@pytest.mark.parametrize('mode', ['single', 'web', 'compute'])
def test_lightweight_ai_restore_preserves_data_and_waits_for_model_consent(tmp_path, checkout, mode):
    manager, _ = installed(tmp_path, checkout, product='BananaChat', mode=mode)
    data = manager.root / 'data'
    weights = data / 'models/blobs/sha256-fixture'
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b'model weights are deliberately excluded')
    manifest = data / 'models/manifests/registry.ollama.ai/library/tiny/latest'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{}')
    attachment = data / 'uploads/customer.bin'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'customer attachment stays in the backup')
    if mode != 'compute':
        with sqlite3.connect(data / 'bananachat.db') as connection:
            connection.executescript('''
                CREATE TABLE ai_models (ollama_name TEXT, backend TEXT, backend_model_name TEXT);
                INSERT INTO ai_models VALUES ('tiny:latest', 'ollama', 'tiny:latest');
                CREATE TABLE chat_messages (content TEXT);
                INSERT INTO chat_messages VALUES ('conversation before migration');
                CREATE TABLE model_pull_jobs (status TEXT, error_message TEXT, finished_at TEXT);
                INSERT INTO model_pull_jobs VALUES ('queued', NULL, NULL);
            ''')
    remote_credentials = manager.config_dir / 'remote-backup'
    remote_credentials.mkdir()
    (remote_credentials / 'recovery.agekey').write_text('fixture recovery key must stay out of its own backup')
    archive = manager.backup(exclude_model_weights=True)
    with read_package(archive, 'BananaChat', manager.root / 'staging') as (extracted, metadata):
        assert metadata['model_weights_excluded'] is True
        assert not (extracted / 'data/models').exists()
        assert (extracted / 'data/uploads/customer.bin').read_bytes() == attachment.read_bytes()
        assert read_json(extracted / 'model-inventory.json')['ollama'] == ['tiny:latest']
        assert not (extracted / 'config/remote-backup').exists()
    restored = Manager(tmp_path / 'restored-ai', product='BananaChat', system=Services())
    restored.restore(archive, new=True)
    pending = read_json(restored.root / 'data/.model-recovery.json')
    assert pending['state'] == 'pending'
    assert pending['inventory']['ollama'] == ['tiny:latest']
    assert not (restored.root / 'data/models/blobs/sha256-fixture').exists()
    if mode != 'compute':
        with sqlite3.connect(restored.root / 'data/bananachat.db') as connection:
            assert connection.execute('SELECT content FROM chat_messages').fetchone()[0] == 'conversation before migration'
            assert connection.execute('SELECT status FROM model_pull_jobs').fetchone()[0] == 'cancelled'


def test_model_weights_remain_in_an_explicit_full_local_package(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout, product='BananaChat', mode='single')
    model = manager.root / 'data/models/custom-weights'
    model.parent.mkdir()
    model.write_bytes(b'irreplaceable local custom weights')
    archive = manager.backup()
    with read_package(archive, 'BananaChat', manager.root / 'staging') as (extracted, metadata):
        assert metadata['model_weights_excluded'] is False
        assert (extracted / 'data/models/custom-weights').read_bytes() == model.read_bytes()


def test_restored_download_queue_uses_the_configured_database_path(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout, product='BananaChat', mode='web')
    database = manager.root / 'data/custom-chat.db'
    with sqlite3.connect(database) as connection:
        connection.executescript('''
            CREATE TABLE model_pull_jobs (status TEXT, error_message TEXT, finished_at TEXT);
            INSERT INTO model_pull_jobs VALUES ('queued', NULL, NULL);
        ''')
    environment = read_environment(manager.config_dir / 'app.env')
    environment['BC_DATABASE_PATH'] = str(database)
    write_environment(manager.config_dir / 'app.env', environment)
    archive = manager.backup(exclude_model_weights=True)
    restored = Manager(tmp_path / 'custom-restored', product='BananaChat', system=Services())
    restored.restore(archive, new=True)
    with sqlite3.connect(restored.root / 'data/custom-chat.db') as connection:
        assert connection.execute('SELECT status FROM model_pull_jobs').fetchone()[0] == 'cancelled'


def test_backup_schedule_is_separate_and_uninstall_removes_its_units(tmp_path, checkout, monkeypatch):
    from banana_ops.system import System
    manager, _ = installed(tmp_path, checkout)
    system = System()
    system.unit_dir = tmp_path / 'units'
    system.bin_dir = tmp_path / 'bin'
    calls = []
    monkeypatch.setattr(system, 'run', lambda command, **kwargs: calls.append(command))
    settings = manager.settings()
    system.install_backup_timer(settings, {'enabled': False, 'interval_minutes': 1440})
    service = system.unit_dir / 'bananawiki-backup.service'
    timer = system.unit_dir / 'bananawiki-backup.timer'
    assert 'backups run --automatic' in service.read_text()
    assert 'OnUnitActiveSec=1440min' in timer.read_text()
    assert ['systemctl', 'disable', '--now', 'bananawiki-backup.timer'] in calls
    system.uninstall(settings)
    assert not service.exists() and not timer.exists()


def test_update_is_backed_up_and_preserves_data(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    populate(manager)
    old = manager.settings()['revision']
    new = revision(checkout)
    manager.set_updates(True)
    result = manager.update(automatic=True)
    assert result['outcome'] == 'complete'
    assert manager.settings()['revision'] == new
    assert (manager.root / 'current/version.txt').read_text() == 'two\n'
    assert (manager.root / 'data/private.key').read_text() == 'private fixture key'
    assert services.observed_maintenance
    with read_package(result['backup'], 'BananaWiki', manager.root / 'staging') as (root, manifest):
        assert manifest['revision'] == old
        assert (root / 'data/private.key').read_text() == 'private fixture key'
        assert not (root / 'data/.banana-maintenance').exists()


def test_failed_health_rolls_back_both_code_and_database(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    database = populate(manager)
    old = manager.settings()['revision']
    new = revision(checkout)
    services.fail_revision = new
    def migration(settings):
        if settings['revision'] == new:
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE records SET value='bad migrated content'")
    services.on_health = migration
    with pytest.raises(RuntimeError, match='readiness'):
        manager.update()
    assert manager.settings()['revision'] == old
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT value FROM records').fetchone()[0] == 'customer content'
    assert not (manager.root / 'data/.banana-maintenance').exists()
    manager.set_updates(True)
    assert manager.update(automatic=True)['outcome'] == 'paused'


def test_branch_deletion_requires_explicit_fallback(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    git('-C', checkout, 'checkout', '-b', 'stable')
    sha = revision(checkout)
    git('-C', checkout, 'branch', '-D', 'main')
    with pytest.raises(RuntimeError, match='deleted'):
        manager.update()
    manager.configure_source(fallback='stable')
    result = manager.update()
    assert result['used_fallback'] and result['revision'] == sha


def test_unrelated_fallback_never_silently_replaces_source(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    git('-C', checkout, 'checkout', '--orphan', 'different')
    revision(checkout)
    git('-C', checkout, 'branch', '-D', 'main')
    manager.configure_source(fallback='different')
    manager.set_updates(True)
    assert manager.update(automatic=True, allow_divergent=True)['outcome'] == 'paused'


def test_migration_package_restores_code_data_credentials_and_disables_sync(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    populate(manager)
    token = tmp_path / 'token'
    token.write_text('fixture-private-repository-token')
    manager.configure_source(url='https://forge.example/private/BananaWiki.git', token_file=token, username='bot')
    manager.set_updates(True)
    archive = manager.backup()
    assert archive.stat().st_mode & 0o777 == 0o600
    restored = Manager(tmp_path / 'other-server', product='BananaWiki', system=Services())
    restored.restore(archive, new=True)
    assert restored.settings()['mode'] == 'wiki'
    assert restored.settings()['revision'] == manager.settings()['revision']
    assert (restored.root / 'data/private.key').read_text() == 'private fixture key'
    assert (restored.config_dir / 'repo.token').read_text().strip() == token.read_text()
    assert restored.policy()['enabled'] is False
    environment = read_environment(restored.config_dir / 'app.env')
    assert environment['BW_INSTANCE_DIR'] == str(restored.root / 'data')
    assert 'fixture-private-repository-token' not in json.dumps(restored.status())


def test_restore_after_install_creates_a_backup_of_existing_state(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    populate(manager)
    archive = manager.backup()
    (manager.root / 'data/private.key').write_text('new data before restore')
    manager.restore(archive)
    assert (manager.root / 'data/private.key').read_text() == 'private fixture key'
    previous = list((manager.root / 'backups').glob('before-restore-*.tar.gz'))
    assert len(previous) == 1
    with read_package(previous[0], 'BananaWiki', manager.root / 'staging') as (root, _):
        assert (root / 'data/private.key').read_text() == 'new data before restore'


def test_hosting_storage_aliases_survive_update_and_portable_restore(tmp_path, checkout, monkeypatch):
    import pwd
    from banana_ops.system import System

    manager, _ = installed(tmp_path, checkout, mode='hosting')
    populate(manager)
    tenant = manager.root / 'data/instances/customer'
    tenant.mkdir()
    profile.prepare_hosting_storage(manager.root / 'data')
    (tenant / 'attachments/customer.txt').write_text('portable customer attachment')
    identity = pwd.getpwuid(os.getuid())
    with monkeypatch.context() as context:
        context.setattr(pwd, 'getpwnam', lambda _: identity)
        System().data_permissions(manager.settings())

    archive = manager.backup()
    with tarfile.open(archive) as package:
        assert all(member.isfile() for member in package.getmembers())
        assert 'data/instances/customer/storage/attachments/customer.txt' in package.getnames()
        assert 'data/instances/customer/attachments/customer.txt' not in package.getnames()
    revision(checkout)
    manager.update()
    assert (tenant / 'attachments/customer.txt').read_text() == 'portable customer attachment'
    restored = Manager(tmp_path / 'restored-hosting', product='BananaWiki', system=Services())
    restored.restore(archive, new=True)
    restored_tenant = restored.root / 'data/instances/customer'
    assert (restored_tenant / 'attachments').is_symlink()
    assert (restored_tenant / 'attachments').resolve() == restored_tenant / 'storage/attachments'
    assert (restored_tenant / 'attachments/customer.txt').read_text() == 'portable customer attachment'
    assert restored.policy()['enabled'] is False
    with monkeypatch.context() as context:
        context.setattr(pwd, 'getpwnam', lambda _: identity)
        System().data_permissions(restored.settings())


def test_restart_waits_for_tenants_that_were_running_before_service_shutdown(tmp_path, checkout, monkeypatch):
    from banana_ops import cli

    manager, services = installed(tmp_path, checkout, mode='hosting')
    running = True
    checked = []
    def containers(_):
        return [{'id': 'tenant', 'running': running, 'ports': [{'HostPort': '6001'}]}]
    def stop(_):
        nonlocal running
        running = False
    def healthy(settings, tenants=(), timeout=180):
        checked.extend(tenants)
        return True
    services.containers = containers
    services.stop = stop
    services.healthy = healthy
    monkeypatch.setattr(cli, 'Manager', lambda _: manager)
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    assert cli.main(['--root', str(manager.root), 'restart']) == 0
    assert checked == [{'id': 'tenant', 'running': True, 'ports': [{'HostPort': '6001'}]}]


def test_real_readiness_checks_recreated_and_restored_internal_tenants(tmp_path, checkout, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from banana_ops.system import System

    manager, _ = installed(tmp_path, checkout, mode='hosting')
    connection = sqlite3.connect(manager.root / 'data/hosting.db')
    try:
        connection.execute('CREATE TABLE instances(subdomain TEXT, domain_mode TEXT, status TEXT)')
        connection.execute("INSERT INTO instances VALUES('customer','hosting','running')")
        connection.commit()
    finally:
        connection.close()
    directory = str(manager.root / 'data/instances/customer')
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = dict(manager.settings(), port=server.server_port)
    system = System()
    system.log_dir = manager.root / 'config'
    monkeypatch.setattr(system, 'active', lambda _: True)
    records = []
    monkeypatch.setattr(system, 'containers', lambda _: records)
    monkeypatch.setattr('banana_ops.system.time.sleep', lambda _: None)
    try:
        # An apparently healthy portal cannot hide a missing restored tenant.
        assert not system.healthy(settings, timeout=0.05)
        report = system.log_dir / 'last-readiness-failure.json'
        assert report.stat().st_mode & 0o077 == 0
        assert any('Tenant has no running container' in issue
                   for issue in json.loads(report.read_text())['issues'])
        records.append({'id': 'recreated-container', 'data_dir': directory,
                        'running': True, 'ports': [], 'addresses': ['127.0.0.1'],
                        'internal_port': server.server_port})
        previous = [{'id': 'removed-container', 'data_dir': directory, 'running': True,
                     'ports': [{'HostPort': '1'}]}]
        calls.clear()
        assert system.healthy(settings, previous, timeout=1)
        assert calls == ['/health', '/health']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_hosting_readiness_allows_configured_recovery_waves(tmp_path, checkout):
    from banana_ops.system import System

    manager, _ = installed(tmp_path, checkout, mode='hosting')
    with sqlite3.connect(manager.root / 'data/hosting.db') as connection:
        connection.execute('CREATE TABLE instances(subdomain TEXT, domain_mode TEXT, status TEXT)')
        connection.executemany('INSERT INTO instances VALUES(?,?,?)',
                               [(f'customer-{number}', 'hosting', 'running') for number in range(5)])
    path = manager.root / 'config/app.env'
    environment = read_environment(path)
    environment.update(INSTANCE_STARTUP_TIMEOUT_SECONDS='300', BW_HOSTING_RECOVERY_MAX_WORKERS='2')
    write_environment(path, environment)
    deadline = System().readiness_timeout(manager.settings())
    # Three waves must each have time for a cold start and its bounded retry.
    assert 3 * 2 * 300 <= deadline <= 7200


@pytest.mark.parametrize('unsafe', ['absolute', 'relative', 'storage-parent', 'storage-target', 'unknown-alias'])
def test_hosting_packages_reject_links_other_than_internal_storage_aliases(tmp_path, checkout, unsafe):
    manager, _ = installed(tmp_path, checkout, mode='hosting')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private.txt').write_text('must not enter the package')
    tenant = manager.root / 'data/instances/customer'
    tenant.mkdir()
    profile.prepare_hosting_storage(manager.root / 'data')
    if unsafe in {'absolute', 'relative'}:
        (tenant / 'uploads').unlink()
        (tenant / 'uploads').symlink_to(outside if unsafe == 'absolute' else '../../../../outside')
    elif unsafe == 'storage-parent':
        import shutil
        shutil.rmtree(tenant / 'storage')
        (tenant / 'storage').symlink_to(outside)
    elif unsafe == 'storage-target':
        (tenant / 'storage/uploads').rmdir()
        (tenant / 'storage/uploads').symlink_to(outside)
    else:
        (tenant / 'unexpected').symlink_to('storage/uploads')
    with pytest.raises(ValueError, match='regular files'):
        manager.backup()
    assert not list((manager.root / 'backups').glob('*.tar.gz'))
    assert (outside / 'private.txt').read_text() == 'must not enter the package'


def test_restore_can_change_hostname_port_and_service_identity(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    populate(manager)
    archive = manager.backup()
    restored = Manager(tmp_path / 'new-host', product='BananaWiki', system=Services())
    restored.restore(archive, new=True, name='bananawiki-new', domain='wiki.example.org', port=5002)
    values = read_environment(restored.config_dir / 'app.env')
    assert values['BW_PORT'] == '5002'
    assert values['BW_PROXY_MODE'] == '1' and values['BW_PREFERRED_URL_SCHEME'] == 'https'
    assert values['BW_SYSTEMD_SERVICE'] == 'bananawiki-new.service'
    assert values['BW_DATABASE_PATH'].startswith(str(restored.root / 'data'))


def test_backup_failure_during_restore_restores_running_services(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    populate(manager)
    archive = manager.backup()
    def cannot_backup(*_):
        raise OSError('fixture disk failure')
    manager.package = cannot_backup
    with pytest.raises(OSError, match='disk failure'):
        manager.restore(archive)
    assert manager.settings()['service'] in services.running
    assert not manager.status()['maintenance'] and not manager.status()['recovery_pending']


def test_hosted_wiki_maintenance_is_retained_until_all_checks_pass(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout, mode='hosting')
    tenant = manager.root / 'data/instances/fixture'
    tenant.mkdir()
    seen = []
    def check(settings):
        seen.append((tenant / '.banana-maintenance').exists())
    services.on_health = check
    revision(checkout)
    manager.update()
    assert seen == [True]
    assert not (tenant / '.banana-maintenance').exists()


def test_private_credentials_are_not_reused_on_another_host(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    token = tmp_path / 'private-token'
    token.write_text('fixture-token')
    manager.configure_source(url='https://one.example/team/wiki.git', token_file=token)
    manager.configure_source(url='https://two.example/team/wiki.git')
    assert manager.source()['auth'] == 'none'
    assert 'GIT_ASKPASS' not in GitSource(manager.root, manager.source()).environment()
    assert (manager.root / 'repository.git').stat().st_mode & 0o777 == 0o700


def test_interrupted_update_recovers_from_durable_journal(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    populate(manager)
    settings = manager.settings()
    journal = manager.snapshot_state(settings)
    manager.quiesce(journal)
    archive = manager.package(settings, manager.backup_name('crash'))
    journal.update(backup=str(archive), phase='backed_up')
    write_json(manager.config_dir / 'transaction.json', journal)
    (manager.root / 'data/private.key').write_text('partially modified')
    assert manager.recover()
    assert (manager.root / 'data/private.key').read_text() == 'private fixture key'
    assert not manager.status()['recovery_pending']


def test_opt_out_during_preparation_survives_inflight_update(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    manager.set_updates(True)
    revision(checkout)
    previous = services.prepare_release
    def prepare(settings, release):
        previous(settings, release)
        manager.set_updates(False)
    services.prepare_release = prepare
    assert manager.update(automatic=True)['outcome'] == 'cancelled'
    assert not manager.policy()['enabled']
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'


def test_uninstall_retains_data_and_does_not_allow_accidental_purge(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout)
    populate(manager)
    with pytest.raises(ValueError, match='confirm'):
        manager.uninstall(purge=True)
    assert manager.uninstall()['data_preserved']
    assert (manager.root / 'data/private.key').exists()
    assert not services.running and not manager.policy()['enabled']


@pytest.mark.parametrize('name,kind', [('../outside', 'file'), ('/absolute', 'file'), ('link', 'symlink'), ('device', 'device')])
def test_package_extraction_rejects_unsafe_members(tmp_path, name, kind):
    archive = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(archive, 'w:gz') as target:
        entry = tarfile.TarInfo(name)
        if kind == 'symlink':
            entry.type, entry.linkname = tarfile.SYMTYPE, '/etc/passwd'
        elif kind == 'device':
            entry.type = tarfile.CHRTYPE
        else:
            entry.size = 1
        target.addfile(entry, io.BytesIO(b'x') if kind == 'file' else None)
    with pytest.raises(ValueError):
        extract_archive(archive, tmp_path / 'restore')


def test_maintenance_gate_blocks_writes_but_allows_readiness(tmp_path):
    marker = tmp_path / 'maintenance'
    marker.write_text('working')
    called = []
    app = MaintenanceGate(lambda env, start: called.append(env) or [b'ok'], marker)
    status = []
    assert b'Maintenance' in b''.join(app({'PATH_INFO': '/edit', 'REQUEST_METHOD': 'POST'}, lambda code, headers: status.append(code)))
    assert not called and status == ['503 Service Unavailable']
    assert app({'PATH_INFO': '/health'}, lambda *_: None) == [b'ok']
    marker.unlink()
    assert app({'PATH_INFO': '/edit'}, lambda *_: None) == [b'ok']


def test_secrets_are_quoted_as_data_and_not_executed(tmp_path):
    path = tmp_path / 'app.env'
    values = {'A': 'literal $(touch /never) `command` $value # value', 'B': 'a"b\\c'}
    write_environment(path, values)
    assert read_environment(path) == values
    for url in ('https://token@github.com/owner/repo.git', 'ext::sh command', 'http://forge.example/repo.git'):
        with pytest.raises(ValueError):
            valid_url(url)


def test_wal_database_validation_does_not_modify_the_restoration_payload(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    database = data / 'example.db'
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE notes (body TEXT)')
        connection.execute("INSERT INTO notes VALUES ('saved in the WAL')")
        connection.commit()
        assert Path(str(database) + '-wal').is_file()
        archive = write_package(tmp_path / 'wal.tar.gz', {'product': 'BananaWiki'},
                                [(path, 'data/' + name) for path, name in regular_files(data)])
        with read_package(archive, 'BananaWiki', tmp_path) as (extracted, manifest):
            for name, record in manifest['files'].items():
                assert digest_file(extracted / name) == record['sha256']
            with sqlite3.connect(extracted / 'data/example.db') as restored:
                assert restored.execute('SELECT body FROM notes').fetchone()[0] == 'saved in the WAL'


def test_source_offer_supports_both_ssh_url_forms():
    assert profile.source_link('git@forge.example.org:team/wiki.git') == 'https://forge.example.org/team/wiki'
    assert profile.source_link('ssh://git@forge.example.org:2222/team/wiki.git') == 'https://forge.example.org/team/wiki'


def sign_revision(source, key, value='signed'):
    """Commit with an SSH signature from ``key``, the way a signing maintainer would."""
    (source / 'version.txt').write_text(value + '\n')
    git('-C', source, 'add', '.')
    git('-C', source, '-c', 'gpg.format=ssh', '-c', 'user.signingkey=' + str(key),
        'commit', '-S', '-m', 'Signed fixture')
    return git('-C', source, 'rev-parse', 'HEAD')


def signing_key(directory, name):
    """Return a fresh ed25519 key pair and its allowed-signers line."""
    private = directory / name
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', name, '-f', str(private)],
                   check=True, capture_output=True)
    public = private.with_suffix('.pub').read_text().strip()
    return private, f'fixture@example.invalid namespaces="git" {public}\n'


def test_a_revision_that_empties_the_repository_is_rolled_back(tmp_path, checkout):
    """A commit that deletes the application must not take the deployment with it."""
    manager, _ = installed(tmp_path, checkout)
    database = populate(manager)
    old = manager.settings()['revision']
    for name in ('banana', 'LICENSE', 'version.txt'):
        (checkout / name).unlink()
    git('-C', checkout, 'add', '-A')
    git('-C', checkout, 'commit', '-m', 'Delete everything')
    with pytest.raises(ValueError, match='revision'):
        manager.update()
    assert manager.settings()['revision'] == old
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'
    assert (manager.root / 'data/private.key').read_text() == 'private fixture key'
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT value FROM records').fetchone()[0] == 'customer content'
    manager.set_updates(True)
    assert manager.update(automatic=True)['outcome'] == 'paused'


def test_signed_sources_refuse_an_unsigned_revision(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    key, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    assert manager.source()['signing'] == 'ssh'
    old = manager.settings()['revision']
    revision(checkout)
    with pytest.raises(RuntimeError, match='not signed'):
        manager.update()
    assert manager.settings()['revision'] == old
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'


def test_signed_sources_refuse_a_key_that_is_not_listed(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    _, allowed = signing_key(tmp_path, 'maintainer')
    stranger, _ = signing_key(tmp_path, 'stranger')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    old = manager.settings()['revision']
    sign_revision(checkout, stranger)
    with pytest.raises(RuntimeError, match='not signed'):
        manager.update()
    assert manager.settings()['revision'] == old


def test_a_revision_signed_by_a_listed_key_is_deployed(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    key, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    expected = sign_revision(checkout, key)
    assert manager.update()['revision'] == expected
    assert (manager.root / 'current/version.txt').read_text() == 'signed\n'
    manager.configure_source(clear_signatures=True)
    assert manager.source()['signing'] == 'none'
    assert not (manager.root / 'config/repo.allowed_signers').exists()


def test_automatic_updates_also_require_a_signature(tmp_path, checkout):
    """The unattended path is the one that matters: it must not relax the check."""
    manager, _ = installed(tmp_path, checkout)
    _, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    manager.set_updates(True)
    old = manager.settings()['revision']
    revision(checkout)
    with pytest.raises(RuntimeError, match='not signed'):
        manager.update(automatic=True)
    assert manager.settings()['revision'] == old
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'


def test_a_missing_allowed_signers_file_stops_the_update(tmp_path, checkout):
    """Losing the file must fail closed, never silently fall back to unsigned."""
    manager, _ = installed(tmp_path, checkout)
    key, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    (manager.root / 'config/repo.allowed_signers').unlink()
    old = manager.settings()['revision']
    sign_revision(checkout, key)
    with pytest.raises(ValueError, match='0600|owned'):
        manager.update()
    assert manager.settings()['revision'] == old


def test_a_world_readable_allowed_signers_file_is_refused(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout)
    key, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    (manager.root / 'config/repo.allowed_signers').chmod(0o644)
    sign_revision(checkout, key)
    with pytest.raises(ValueError, match='0600|owned'):
        manager.update()


@pytest.mark.skipif(not shutil.which('gpg'), reason='OpenPGP signing needs gpg')
def test_an_openpgp_signature_does_not_satisfy_the_allowed_signers_file(tmp_path, checkout, request):
    """Git chooses its verifier from the signature, not from gpg.format.

    An OpenPGP-signed commit would otherwise be checked against whatever the
    machine's GnuPG keyring trusts, which is not the file the operator
    configured. Only SSH signatures count here.
    """
    home = tmp_path / 'gnupg'
    home.mkdir(mode=0o700)
    environment = {**os.environ, 'GNUPGHOME': str(home)}
    # Generating a key starts a gpg-agent daemon that outlives the test and
    # holds a socket under tmp_path. Stop it however this test ends.
    request.addfinalizer(lambda: subprocess.run(
        ['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
        env=environment, capture_output=True, timeout=60, check=False))
    subprocess.run(['gpg', '--batch', '--passphrase', '', '--quick-gen-key',
                    'Fixture <fixture@example.invalid>', 'ed25519', 'sign', '0'],
                   env=environment, check=True, capture_output=True, timeout=120)
    fingerprint = subprocess.run(['gpg', '--list-secret-keys', '--with-colons'], env=environment,
                                 check=True, capture_output=True, text=True, timeout=60).stdout
    key = next(line.split(':')[9] for line in fingerprint.splitlines() if line.startswith('fpr'))

    manager, _ = installed(tmp_path, checkout)
    _, allowed = signing_key(tmp_path, 'maintainer')
    signers = tmp_path / 'allowed_signers'
    signers.write_text(allowed)
    manager.configure_source(signers_file=signers)
    old = manager.settings()['revision']

    (checkout / 'version.txt').write_text('openpgp\n')
    subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.signingkey=' + key,
                    'commit', '-S', '-m', 'OpenPGP signed'],
                   env=environment, check=True, capture_output=True, timeout=120)

    with pytest.raises(RuntimeError, match='OpenPGP signature'):
        manager.update()
    assert manager.settings()['revision'] == old
    assert (manager.root / 'current/version.txt').read_text() == 'one\n'
