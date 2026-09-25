"""Worker activity sensing and configuration loading.

The idle readings are parsed from captured tool output rather than from a
live desktop session, so these cover the parsing and the fallbacks, not the
platform APIs themselves.
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

from worker import activity


# Captured from ioreg -c IOHIDSystem -d 4 -r on macOS.
IOREG_SAMPLE = """
  +-o IOHIDSystem  <class IOHIDSystem, id 0x100000282, registered, matched>
      {
        "HIDIdleTime" = 4500000000
        "HIDPointerAcceleration" = 49152
      }
"""


def _must_not_run():
    raise AssertionError('Linux idle probes must not run on macOS')


def _fixed_run(stdout='', returncode=0):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout=stdout)
    return run


def test_macos_idle_time_is_read_in_nanoseconds(monkeypatch):
    monkeypatch.setattr(activity.subprocess, 'run', _fixed_run(IOREG_SAMPLE))
    assert activity._idle_seconds_macos() == 4.5


def test_macos_idle_time_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setattr(activity.subprocess, 'run', _fixed_run('', returncode=1))
    assert activity._idle_seconds_macos() is None

    monkeypatch.setattr(activity.subprocess, 'run', _fixed_run('nothing useful here'))
    assert activity._idle_seconds_macos() is None

    def missing(command, **kwargs):
        raise FileNotFoundError('ioreg')

    monkeypatch.setattr(activity.subprocess, 'run', missing)
    assert activity._idle_seconds_macos() is None


def test_macos_uses_the_mac_idle_source(monkeypatch):
    """Darwin must not fall through to the Linux probes: xprintidle and
    dbus-send do not exist there, so the worker would read every Mac as idle
    and never step aside for the person using it."""
    monkeypatch.setattr(activity, '_OS', 'Darwin')
    monkeypatch.setattr(activity, '_last_idle_check', 0.0)
    monkeypatch.setattr(activity, '_idle_seconds_linux', _must_not_run)
    monkeypatch.setattr(activity, '_idle_seconds_macos', lambda: 12.0)
    assert activity.get_user_idle_seconds() == 12.0


def test_a_mac_with_a_person_at_the_keyboard_is_not_reported_idle(monkeypatch):
    """No NVIDIA telemetry exists on a Mac, so the idle reading alone has to
    keep the worker from claiming work while someone is typing."""
    # activity.py does a plain `import config`, which resolves to the worker's
    # own config only because the entrypoint puts worker/ first on sys.path.
    # Imported as a package here it picks up the application's config instead,
    # so pin the thresholds this test depends on.
    thresholds = SimpleNamespace(
        IDLE_THRESHOLD_SECONDS=300,
        LIGHT_THRESHOLD_SECONDS=30,
        GPU_GAMING_THRESHOLD=70,
        GPU_ACTIVE_THRESHOLD=50,
    )
    monkeypatch.setattr(activity, 'config', thresholds)
    monkeypatch.setattr(activity, '_OS', 'Darwin')
    monkeypatch.setattr(activity, 'get_gpu_utilisation', lambda: None)
    monkeypatch.setattr(activity, 'get_user_idle_seconds', lambda: 2.0)
    assert activity.get_activity_state() == 'active'

    monkeypatch.setattr(activity, 'get_user_idle_seconds', lambda: 10_000.0)
    assert activity.get_activity_state() == 'idle'


def test_apple_silicon_reports_its_chip_name(monkeypatch):
    monkeypatch.setattr(activity, '_OS', 'Darwin')
    monkeypatch.setattr(activity.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(activity._gpu_name_macos, '_cached', activity._UNSET, raising=False)
    monkeypatch.setattr(activity.subprocess, 'run', _fixed_run('Apple M2 Pro\n'))
    assert activity.get_gpu_name() == 'Apple M2 Pro'


def test_intel_mac_reports_its_discrete_gpu(monkeypatch):
    listing = """Graphics/Displays:

    Radeon Pro 5500M:

      Chipset Model: Radeon Pro 5500M
      Type: GPU
"""
    monkeypatch.setattr(activity, '_OS', 'Darwin')
    monkeypatch.setattr(activity.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(activity._gpu_name_macos, '_cached', activity._UNSET, raising=False)
    monkeypatch.setattr(activity.subprocess, 'run', _fixed_run(listing))
    assert activity.get_gpu_name() == 'Radeon Pro 5500M'


def test_environment_file_fills_gaps_without_overriding_exports(tmp_path, monkeypatch):
    """launchd has no EnvironmentFile, so the worker reads one itself. A unit
    or a shell that already exported a value must still win."""
    from worker import config

    env_file = tmp_path / 'worker.env'
    env_file.write_text(
        '# a comment\n'
        '\n'
        'BC_SERVER_URL=https://from-file.example.com\n'
        'BC_WORKER_TOKEN="quoted-token"\n'
        "BC_WORKER_NAME='single-quoted'\n"
        'this line is malformed\n'
    )
    monkeypatch.delenv('BC_WORKER_TOKEN', raising=False)
    monkeypatch.delenv('BC_WORKER_NAME', raising=False)
    monkeypatch.setenv('BC_SERVER_URL', 'https://exported.example.com')

    assert config.load_env_file(str(env_file)) == str(env_file)
    import os
    assert os.environ['BC_SERVER_URL'] == 'https://exported.example.com'
    assert os.environ['BC_WORKER_TOKEN'] == 'quoted-token'
    assert os.environ['BC_WORKER_NAME'] == 'single-quoted'


def test_a_missing_environment_file_is_not_an_error(tmp_path):
    from worker import config
    assert config.load_env_file(str(tmp_path / 'absent.env')) is None


def test_a_log_file_is_rotated_and_a_bad_path_does_not_stop_the_worker(tmp_path):
    """Windows tasks have nowhere to send stdout, so the worker writes its own
    file. An unattended worker must neither fill the disk nor refuse to run.

    The entrypoint is loaded under its own name with a stand-in config, rather
    than by putting worker/ on sys.path, because 'config' is also the name of
    the application's own module and swapping it out globally would leak into
    every other test.
    """
    import importlib.util
    import logging
    import sys
    from types import SimpleNamespace

    worker_dir = Path(__file__).resolve().parents[1] / 'worker'
    spec = importlib.util.spec_from_file_location(
        'bananachat_worker_entrypoint', worker_dir / 'bananachat_worker.py')
    entry = importlib.util.module_from_spec(spec)

    settings = SimpleNamespace(
        LOG_LEVEL='INFO', LOG_FILE=str(tmp_path / 'logs' / 'worker.log'),
        LOG_MAX_BYTES=65536, LOG_BACKUPS=2,
    )
    saved_config = sys.modules.get('config')
    saved_path = sys.path[:]
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    try:
        sys.modules['config'] = settings
        spec.loader.exec_module(entry)
        entry.config = settings

        root.handlers = []
        entry._setup_logging()
        handler = root.handlers[0]
        assert handler.maxBytes == 65536 and handler.backupCount == 2
        logging.getLogger('bananachat.worker').info('recorded')
        handler.flush()
        assert 'recorded' in Path(settings.LOG_FILE).read_text()

        # An unwritable destination must fall back to the console, not raise.
        for open_handler in root.handlers:
            open_handler.close()
        root.handlers = []
        blocker = tmp_path / 'blocked'
        blocker.write_text('not a directory')
        settings.LOG_FILE = str(blocker / 'nested.log')
        entry._setup_logging()
    finally:
        for open_handler in root.handlers:
            open_handler.close()
        root.handlers = saved_handlers
        sys.path[:] = saved_path
        if saved_config is None:
            sys.modules.pop('config', None)
        else:
            sys.modules['config'] = saved_config
        sys.modules.pop('bananachat_worker_entrypoint', None)
