"""A release from 1.7 on that lacks its hash-pinned dependency locks is refused, unless the operator allows it (OPS2-09).

Without the locks the installer resolves every dependency anew from PyPI,
newest in range, with no hash check: how releases from before 1.7 (a
rollback) were always installed, never how a later release missing a lock by
mistake should be. It is refused before anything is installed; only
``--allow-unpinned`` on ``update`` (never an automatic update), ``restore``,
``rollback`` or ``backups restore`` deploys it.
"""

import json
import os
import shutil
from types import SimpleNamespace

import pytest

from banana_backup.store import Store
from banana_ops import cli
from banana_ops.files import write_json
from banana_ops.manager import Manager
from test_managed_lifecycle import Services, bundled, checkout, git, installed  # noqa: F401


def commit(source, message, files, *, remove=()):
    for name in remove:
        git("-C", source, "rm", "-q", "--ignore-unmatch", name)
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-q", "-m", message)
    return git("-C", source, "rev-parse", "HEAD")


def version(value):
    return {"bananachat/__init__.py": f'__version__ = "{value}"\n'}


LOCKS = {"requirements.lock": "# locked\n", "requirements-build.lock": "# locked\n"}


def test_a_release_from_1_7_on_without_its_locks_is_refused(tmp_path, checkout, monkeypatch, capsys):
    manager, services = installed(tmp_path, checkout, "BananaChat", "single")
    installed_revision = commit(checkout, "1.7.0", {**version("1.7.0"), **LOCKS})
    assert manager.update()["revision"] == installed_revision
    broken = commit(checkout, "1.7.1, its build lock forgotten", version("1.7.1"),
                    remove=("requirements-build.lock",))
    with pytest.raises(ValueError, match="requirements-build.lock.*--allow-unpinned"):
        manager.update()
    assert broken not in services.prepared and manager.settings()["revision"] == installed_revision
    write_json(manager.config_dir / "updates.json", {"enabled": True, "interval_minutes": 60, "keep_backups": 3,
                                                     "unsigned": True})
    with pytest.raises(ValueError, match="--allow-unpinned"):
        manager.update(automatic=True, retry_failed=True, allow_unpinned=True)
    write_json(manager.config_dir / "updates.json", {"enabled": False, "interval_minutes": 60, "keep_backups": 3})
    monkeypatch.setattr(cli, "Manager", lambda *_args, **_options: manager)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    capsys.readouterr()
    assert not cli.main(["--root", str(manager.root), "update", "--allow-unpinned", "--retry-failed"])
    assert manager.settings()["revision"] == broken
    output = capsys.readouterr()
    assert json.loads(output.out)["dependencies_unpinned"] is True
    assert "requirements.txt" in output.err


def test_a_release_with_one_lock_only_is_refused_whatever_its_version(tmp_path, checkout):
    manager, services = installed(tmp_path, checkout, "BananaChat", "single")
    broken = commit(checkout, "half its locks", {**version("1.6.9"), "requirements.lock": "# locked\n"})
    with pytest.raises(ValueError, match="requirements-build.lock"):
        manager.update()
    assert broken not in services.prepared


def test_a_release_from_before_1_7_installs_from_its_ranges(tmp_path, checkout):
    manager, _services = installed(tmp_path, checkout, "BananaChat", "single")
    older = commit(checkout, "1.6.0 again", version("1.6.0"))
    result = manager.update()
    assert result["revision"] == older and result["dependencies_unpinned"] is True


def test_a_remote_snapshot_of_such_a_release_is_restored_only_with_allow_unpinned(tmp_path, checkout, monkeypatch,
                                                                                    capsys):
    """``backups restore`` takes the flag too: a snapshot of a 1.7 release deployed with --allow-unpinned can be
    restored on a new server, by the same explicit choice, instead of only after downloading it by hand."""
    manager, _services = installed(tmp_path, checkout, "BananaChat", "single")
    commit(checkout, "1.7.0", {**version("1.7.0"), **LOCKS})
    manager.update()
    broken = commit(checkout, "1.7.1, its build lock forgotten", version("1.7.1"), remove=("requirements-build.lock",))
    assert manager.update(allow_unpinned=True)["revision"] == broken
    snapshot = manager.backup()
    # The remote series hands back that package (encryption and Git: test_git_backups.py).
    monkeypatch.setattr(Store, "download", lambda _store, _identifier, output, **_options: (
        shutil.copyfile(snapshot, output), {"verified": True, "authenticated": True})[1])
    monkeypatch.setattr(Store, "set_schedule", lambda _store, enabled, interval=None: {"enabled": bool(enabled),
                                                                                       "interval_minutes": 1440})
    services = Services()
    monkeypatch.setattr(cli, "Manager", lambda root, **_options: Manager(root, product="BananaChat", system=services))
    # Root for the command only: the backup directory must still belong to whoever runs the test.
    monkeypatch.setattr(cli, "os", SimpleNamespace(**{**vars(os), "geteuid": lambda: 0}))
    new_server = tmp_path / "new-server"
    command = ["--root", str(new_server), "backups", "restore", "20261010T000000.000000Z-fixture",
               "--trust-source", bundled(snapshot)]
    capsys.readouterr()
    assert cli.main(command) == 1
    assert "requirements-build.lock" in capsys.readouterr().err and services.prepared == []
    assert not (new_server / "config/installation.json").exists()
    assert cli.main([*command, "--allow-unpinned"]) == 0
    restored = Manager(new_server, product="BananaChat", system=services)
    assert restored.settings()["revision"] == broken and services.prepared == [broken]
    assert json.loads((new_server / "releases" / broken / ".release.json").read_text())["unpinned"] is True
    assert "requirements.txt" in capsys.readouterr().err


@pytest.mark.parametrize("line", ['__version__ = "1.7.1"\n', '__version__: str = "1.7.1"\n',
                                  'from typing import Final\n__version__: Final = "1.7.1"\n'])
def test_an_annotated_version_counts_like_a_plain_one(tmp_path, line):
    """``__version__: str = "1.7.1"`` is a 1.7 release too: read as no version, a release missing its locks
    would install from its ranges like one from before 1.7 (the verifier's OPS2-09 note)."""
    from banana_ops.manager import check_dependency_locks, declared_version

    (tmp_path / "bananachat").mkdir()
    (tmp_path / "bananachat" / "__init__.py").write_text(line, encoding="utf-8")
    assert declared_version(tmp_path, "bananachat") == (1, 7, 1)
    with pytest.raises(ValueError, match="--allow-unpinned"):
        check_dependency_locks(tmp_path, "bananachat")
    (tmp_path / "bananachat" / "__init__.py").write_text("__version__: str\n", encoding="utf-8")
    assert declared_version(tmp_path, "bananachat") is None
