"""Dependency builds must not redirect privileged ownership or mode changes."""

import os
from pathlib import Path
import stat

import pytest

from banana_ops.system import set_release_owner


@pytest.mark.parametrize('mode', ['wiki', 'hosting', 'single', 'web', 'compute'])
def test_preparation_updates_the_isolated_installer_before_dependencies(tmp_path, monkeypatch, mode):
    from banana_ops.system import System
    from types import SimpleNamespace
    calls = []
    identity = SimpleNamespace(pw_uid=1001, pw_gid=1001)
    monkeypatch.setattr('banana_ops.system.pwd.getpwnam', lambda _: identity)
    monkeypatch.setattr('banana_ops.system.set_release_owner', lambda *args, **kwargs: None)
    system = System()
    monkeypatch.setattr(system, 'run', lambda command, **kwargs: calls.append(command))
    settings = {'product': 'BananaWiki' if mode in {'wiki', 'hosting'} else 'BananaChat',
                'mode': mode, 'service': 'fixture-service', 'revision': 'a' * 40}
    system.prepare_release(settings, tmp_path)
    installations = [command for command in calls if 'pip' in command and 'install' in command]
    assert installations and installations[0][-1] == 'pip>=26.2.1'
    assert '--only-binary=:all:' in installations[0] and '--no-deps' in installations[0]
    assert all(command[:4] == ['runuser', '-u', 'fixture-service', '--'] for command in installations)
    if mode == 'compute':
        assert len(installations) == 1
    else:
        assert '-r' in installations[1]


def test_installer_upgrade_failure_stops_dependency_and_image_preparation(tmp_path, monkeypatch):
    from banana_ops.system import System
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr('banana_ops.system.pwd.getpwnam', lambda _: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setattr('banana_ops.system.set_release_owner', lambda *args, **kwargs: None)
    def failed_upgrade(command, **kwargs):
        calls.append(command)
        if 'pip>=26.2.1' in command:
            raise RuntimeError('The secure installer could not be prepared')
    system = System()
    monkeypatch.setattr(system, 'run', failed_upgrade)
    with pytest.raises(RuntimeError, match='secure installer'):
        system.prepare_release({'product': 'BananaWiki', 'mode': 'hosting', 'service': 'fixture', 'revision': 'a' * 40}, tmp_path)
    assert not any('-r' in command or command[0] == 'docker' for command in calls)


def test_release_sealing_preserves_python_links_and_external_files(tmp_path):
    release = tmp_path / "release"
    binary = release / ".venv/bin"
    binary.mkdir(parents=True)
    ordinary = release / "app.py"
    ordinary.write_text("example")
    executable = binary / "tool"
    executable.write_text("example")
    executable.chmod(0o755)
    outside = tmp_path / "outside"
    outside.write_text("keep this")
    outside.chmod(0o604)
    (binary / "python").symlink_to(outside)

    set_release_owner(release, os.geteuid(), os.getegid(), readonly=True)

    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o640
    assert stat.S_IMODE(executable.stat().st_mode) == 0o750
    assert stat.S_IMODE(binary.stat().st_mode) == 0o750
    assert (binary / "python").is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o604
    assert outside.read_text() == "keep this"


def test_release_sealing_does_not_follow_a_replaced_entry(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    entry = release / "switchme"
    entry.write_text("build file")
    outside = tmp_path / "outside"
    outside.write_text("keep this")
    outside.chmod(0o604)
    original_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if path == "switchme":
            entry.unlink()
            entry.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced_open)
    set_release_owner(release, os.geteuid(), os.getegid(), readonly=True)
    assert entry.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o604
    assert outside.read_text() == "keep this"


def test_release_sealing_rejects_hard_links_and_special_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("keep this")
    outside.chmod(0o604)
    for kind in ("hardlink", "fifo"):
        release = tmp_path / kind
        release.mkdir()
        entry = release / "entry"
        if kind == "hardlink":
            os.link(outside, entry)
        else:
            os.mkfifo(entry)
        with pytest.raises(ValueError, match="special files or hard links"):
            set_release_owner(release, os.geteuid(), os.getegid(), readonly=True)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o604


def test_build_access_is_granted_after_visiting_children(tmp_path, monkeypatch):
    release = tmp_path / "release"
    child = release / "bin"
    child.mkdir(parents=True)
    (child / "python.py").write_text("example")
    visited = []
    monkeypatch.setattr(os, "fchown", lambda descriptor, *_: visited.append(os.fstat(descriptor).st_ino))
    set_release_owner(release, os.geteuid(), os.getegid())
    assert visited == [(child / "python.py").stat().st_ino, child.stat().st_ino, release.stat().st_ino]
