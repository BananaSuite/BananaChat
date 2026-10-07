"""Production transport rejects unsafe setup before any provider process starts."""
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from bananachat.services.claude_code import Adapter, ConnectorError
from bananachat.services.claude_sandbox import SandboxError, private_path, private_roots, profile_paths, trusted_executable
from bananachat.services import claude_sandbox
from tests.app.test_claude_code import connector  # noqa: F401


def test_missing_environment_cannot_silently_disable_production_sandbox(connector):
    _, _, manifest = connector
    with pytest.raises(ConnectorError, match="production Claude sandbox"):
        Adapter(SimpleNamespace(claude_code_config=str(manifest)))


def test_explicit_production_cannot_use_service_writable_executable(connector):
    _, _, manifest = connector
    with pytest.raises(ConnectorError, match="production Claude sandbox"):
        Adapter(SimpleNamespace(environment="production", claude_code_config=str(manifest)))


def test_profile_path_rejects_symlinked_parent(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SandboxError, match="symlinks"):
        private_path(link / "actual", uid=actual.stat().st_uid, directory=True)


def test_profile_path_rejects_systemd_expansion(tmp_path):
    path = tmp_path / "%h"
    path.mkdir(mode=0o700)
    with pytest.raises(SandboxError, match="specifiers"):
        private_path(path, uid=path.stat().st_uid, directory=True)


@pytest.mark.parametrize("directory,mode", [(False, 0o000), (True, 0o500), (True, 0o000)])
def test_private_paths_require_service_owner_access(tmp_path, directory, mode):
    path = tmp_path / "private"
    if directory:
        path.mkdir()
    else:
        path.write_text("synthetic")
    path.chmod(mode)
    with pytest.raises(SandboxError, match="service user needs"):
        private_path(path, uid=path.stat().st_uid, directory=directory)


def test_private_path_requires_searchable_parent_for_service_uid(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    path.chmod(0o600)
    original = Path.stat

    def metadata(value, **kwargs):
        info = original(value, **kwargs)
        if value == path:
            return SimpleNamespace(st_uid=1001, st_mode=stat.S_IFREG | 0o600)
        if value == tmp_path:
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        return info

    monkeypatch.setattr(Path, "stat", metadata)
    with pytest.raises(SandboxError, match="traverse"):
        private_path(path, uid=1001)


@pytest.mark.parametrize("private_binary,private_parent", [(True, False), (False, True)])
def test_root_preflight_refuses_executable_inaccessible_to_service(tmp_path, monkeypatch, private_binary, private_parent):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    original = Path.stat

    def metadata(value, **kwargs):
        info = original(value, **kwargs)
        mode = (0o700 if private_binary else 0o755) if value == binary else (0o700 if value == tmp_path and private_parent else 0o755)
        kind = stat.S_IFREG if value == binary else stat.S_IFDIR
        return SimpleNamespace(st_uid=0, st_mode=kind | mode, st_size=info.st_size)

    monkeypatch.setattr(Path, "stat", metadata)
    with pytest.raises(SandboxError, match="publicly"):
        trusted_executable(binary)


def test_profile_cannot_mount_application_or_system_storage(tmp_path):
    home = tmp_path / "home"
    config = home / "config"
    config.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    with pytest.raises(SandboxError, match="outside application"):
        profile_paths({"one": {"home": str(home), "config_dir": str(config)}},
                      uid=home.stat().st_uid, forbidden=(tmp_path,))


def test_executable_trust_does_not_accept_relative_path():
    with pytest.raises(SandboxError, match="absolute"):
        trusted_executable(Path("claude"))


def test_global_manifest_cannot_be_exposed_inside_active_profile(monkeypatch):
    """Separate private profiles cannot rewrite the shared binding manifest.

    Validate mount selection with already-checked logical paths: this runs on
    unprivileged CI without needing to create root-owned /srv directories.
    """
    home = Path("/srv/claude-test/one")
    monkeypatch.setattr(claude_sandbox, "private_path", lambda value, **_: Path(value))
    with pytest.raises(SandboxError, match="outside application"):
        profile_paths({"one": {"home": str(home), "config_dir": str(home / "config")}},
                      uid=1001, forbidden=(home / "connector.json",))


@pytest.mark.parametrize("path", ["/usr/local/lib/private-app", "/usr/local/bin/source", "/etc/ssl/certs/private-storage"])
def test_private_application_paths_cannot_use_exposed_runtime_trees(path):
    with pytest.raises(SandboxError, match="outside public runtime"):
        private_roots((path,))
