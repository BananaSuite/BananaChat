"""Service installer checks for all three platforms.

These exercise the artefacts the installer generates and the commands it
issues, with the platform forced and the tools mocked. None of them claims a
native trial on Windows or macOS; see the deployment notes for what still has
to be confirmed on the machine itself.
"""
from pathlib import Path
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

from worker import service


def test_windows_task_uses_current_user_and_consistent_controls(monkeypatch):
    calls = []
    temporary = []
    namespace = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'sc':
            return subprocess.CompletedProcess(command, 1060)
        if command[0] == 'whoami':
            return subprocess.CompletedProcess(command, 0, stdout='HOST\\operator\n')
        if '/XML' in command:
            path = Path(command[command.index('/XML') + 1])
            temporary.append(path)
            tree = ET.fromstring(path.read_text(encoding='utf-16'))
            assert tree.findtext('.//t:Principal/t:UserId', namespaces=namespace) == 'HOST\\operator'
            assert tree.findtext('.//t:RunLevel', namespaces=namespace) == 'LeastPrivilege'
            assert tree.findtext('.//t:LogonType', namespaces=namespace) == 'InteractiveToken'
            assert tree.findtext('.//t:Command', namespaces=namespace) == service.sys.executable
            arguments = tree.findtext('.//t:Arguments', namespaces=namespace)
            assert 'bananachat_worker.py' in arguments and arguments.endswith(' run')
        return subprocess.CompletedProcess(command, 0, stdout='Ready')

    monkeypatch.setattr(service, '_OS', 'Windows')
    monkeypatch.setattr(service.subprocess, 'run', run)
    service.install()
    assert temporary and not temporary[0].exists()
    service.start()
    service.stop()
    service.status()
    service.uninstall()
    for action in ('/Create', '/Run', '/End', '/Query', '/Delete'):
        assert any(command[:2] == ['schtasks', action] and 'BananaChatWorker' in command for command in calls)
    assert not any(command[:2] in (['sc', 'start'], ['sc', 'create']) for command in calls)


def test_windows_task_refuses_to_leave_an_older_system_service_running(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(service.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='older.*system service'):
        service.install_windows()
    assert calls == [['sc', 'query', 'BananaChatWorker']]


def _launchctl_recorder(results=None):
    """Record every command and answer launchctl with a chosen return code."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        code = 0
        if results and command[0] == 'launchctl':
            code = results.get(command[1], 0)
        return subprocess.CompletedProcess(command, code, stdout='ok', stderr='launchctl said no')

    return calls, run


def test_macos_agent_keeps_the_token_out_of_the_plist(monkeypatch, tmp_path):
    """The plist is world-readable in ~/Library/LaunchAgents, so it may carry
    the path to the environment file but never the token itself."""
    import plistlib

    monkeypatch.setenv('HOME', str(tmp_path))
    calls, run = _launchctl_recorder()
    monkeypatch.setattr(service, '_OS', 'Darwin')
    monkeypatch.setattr(service.subprocess, 'run', run)

    service.install(system=False)

    plist_path = tmp_path / 'Library' / 'LaunchAgents' / 'com.bananasuite.bananachat.worker.plist'
    assert plist_path.exists()
    document = plistlib.loads(plist_path.read_bytes())
    assert document['Label'] == 'com.bananasuite.bananachat.worker'
    assert document['ProgramArguments'][0] == service.sys.executable
    assert document['ProgramArguments'][-1] == 'run'
    assert document['RunAtLoad'] is True
    # Restart after a crash, stay down after a clean stop, same as systemd.
    assert document['KeepAlive'] == {'SuccessfulExit': False}
    # Tell the macOS scheduler this is background work.
    assert document['ProcessType'] == 'Background'

    env_file = tmp_path / '.config' / 'bananachat-worker.env'
    assert document['EnvironmentVariables'] == {'BC_WORKER_ENV_FILE': str(env_file)}
    assert 'BC_WORKER_TOKEN' not in plist_path.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == '0o600'

    assert ['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist_path)] in calls


def test_macos_falls_back_to_load_on_systems_without_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    calls, run = _launchctl_recorder({'bootstrap': 1})
    monkeypatch.setattr(service, '_OS', 'Darwin')
    monkeypatch.setattr(service.subprocess, 'run', run)

    service.install(system=False)

    assert any(command[:3] == ['launchctl', 'load', '-w'] for command in calls)


def test_macos_control_verbs_and_a_failed_start_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    calls, run = _launchctl_recorder()
    monkeypatch.setattr(service, '_OS', 'Darwin')
    monkeypatch.setattr(service.subprocess, 'run', run)

    service.start()
    service.stop()
    service.status()
    service.uninstall()

    label = f'gui/{os.getuid()}/com.bananasuite.bananachat.worker'
    assert ['launchctl', 'kickstart', '-k', label] in calls
    assert ['launchctl', 'bootout', label] in calls
    assert ['launchctl', 'print', label] in calls

    _calls, failing = _launchctl_recorder({'kickstart': 1, 'bootout': 1})
    monkeypatch.setattr(service.subprocess, 'run', failing)
    with pytest.raises(RuntimeError, match='Could not start'):
        service.start()
    with pytest.raises(RuntimeError, match='Could not stop'):
        service.stop()


def test_macos_system_daemon_refuses_root_and_runs_as_the_account(monkeypatch, tmp_path):
    """A LaunchDaemon runs as root unless told otherwise. Inference must not."""
    import plistlib
    import pwd

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(service, '_OS', 'Darwin')
    monkeypatch.setattr(service.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError, match='non-root'):
        service.install(system=True)

    # The --system path resolves the worker account through pwd rather than
    # HOME, so pin it at the temporary directory. Without this the test
    # writes into the real home of whoever runs the suite.
    account = pwd.struct_passwd(('worker', 'x', 1000, 1000, 'Worker', str(tmp_path), '/bin/sh'))
    monkeypatch.setattr(service.os, 'geteuid', lambda: 1000)
    monkeypatch.delenv('SUDO_USER', raising=False)
    monkeypatch.setattr(pwd, 'getpwuid', lambda _uid: account)
    written = {}

    def run(command, **kwargs):
        if 'install' in command:
            written['plist'] = Path(command[-2]).read_bytes()
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(service.subprocess, 'run', run)
    service.install(system=True)
    document = plistlib.loads(written['plist'])
    assert document['UserName'] == 'worker'
    assert document['EnvironmentVariables']['BC_WORKER_ENV_FILE'].startswith(str(tmp_path))


def test_service_commands_refuse_an_unknown_platform_instead_of_doing_nothing(monkeypatch):
    """Silently succeeding would leave the worker looking installed and dead."""
    monkeypatch.setattr(service, '_OS', 'Plan9')
    for action in (service.install, service.uninstall, service.start, service.stop, service.status):
        with pytest.raises(SystemExit) as exit_info:
            action()
        assert exit_info.value.code == 1


def test_windows_rejects_the_system_flag(monkeypatch):
    monkeypatch.setattr(service, '_OS', 'Windows')
    with pytest.raises(ValueError, match='--system'):
        service.install(system=True)


def test_linux_unit_points_at_this_checkout_and_rejects_injection(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(service, '_OS', 'Linux')
    monkeypatch.setattr(service.subprocess, 'run',
                        lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    service.install(system=False)
    unit = (tmp_path / '.config' / 'systemd' / 'user' / 'bananachat-worker.service').read_text()
    assert 'bananachat_worker.py" run' in unit
    assert 'NoNewPrivileges=true' in unit
    assert 'WantedBy=default.target' in unit

    monkeypatch.setattr(service.sys, 'executable', '/usr/bin/py"thon')
    with pytest.raises(ValueError, match='control characters'):
        service.install(system=False)


def test_the_settings_file_path_agrees_between_installer_and_worker(monkeypatch, tmp_path):
    """service.py writes the file and config.py reads it. They build the path
    separately, so a change to one has to be matched in the other."""
    from worker import config

    for system, appdata in (('Linux', None), ('Darwin', None), ('Windows', str(tmp_path / 'AppData'))):
        monkeypatch.setattr(service, '_OS', system)
        monkeypatch.setattr(config.platform, 'system', lambda value=system: value)
        if appdata:
            monkeypatch.setenv('APPDATA', appdata)
        else:
            monkeypatch.delenv('APPDATA', raising=False)
        assert service._env_file_path() == config.default_env_file(), system


def test_windows_install_creates_settings_and_a_log_file(monkeypatch, tmp_path):
    """A Task Scheduler job has nowhere to send stdout, so the installer has
    to give the worker a log file of its own."""
    monkeypatch.setattr(service, '_OS', 'Windows')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'AppData'))

    def run(command, **kwargs):
        if command[0] == 'sc':
            return subprocess.CompletedProcess(command, 1060)
        if command[0] == 'whoami':
            return subprocess.CompletedProcess(command, 0, stdout='HOST\\operator\n')
        return subprocess.CompletedProcess(command, 0, stdout='')

    monkeypatch.setattr(service.subprocess, 'run', run)
    service.install()

    env_file = tmp_path / 'AppData' / 'BananaChat' / 'bananachat-worker.env'
    assert env_file.exists()
    assert oct(env_file.stat().st_mode & 0o777) == '0o600'
    body = env_file.read_text()
    assert 'BC_SERVER_URL=' in body
    assert 'BC_WORKER_LOG_FILE=' in body
    assert body.rstrip().endswith('bananachat-worker.log')


def test_every_supported_platform_writes_its_settings_file_once(monkeypatch, tmp_path):
    """Installing twice must not overwrite a file the operator has edited."""
    monkeypatch.setattr(service, '_OS', 'Linux')
    env_file = tmp_path / 'settings.env'
    assert service._write_env_template(str(env_file)) is True
    env_file.write_text('BC_SERVER_URL=https://edited.example.com\n')
    assert service._write_env_template(str(env_file)) is False
    assert 'edited.example.com' in env_file.read_text()
