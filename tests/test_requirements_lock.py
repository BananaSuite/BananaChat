"""The hash-pinned locks production installs use: complete, in step with requirements.txt, and used.

A requirement added to requirements.txt without regenerating the locks
(python scripts/lock_requirements.py) would be missing from every managed
server: this fails first. The managed updater installs the locks with
--require-hashes and wheels only.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements.lock", "requirements-build.lock")


def lock_tool():
    spec = importlib.util.spec_from_file_location("lock_requirements", ROOT / "scripts/lock_requirements.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy(tmp_path):
    for name in FILES:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    return tmp_path


def test_the_locks_pin_every_requirement_by_hash():
    assert lock_tool().check(ROOT) == []


def test_the_locks_are_hashed_and_complete():
    entries = lock_tool().pins(ROOT / "requirements.lock")
    assert {"flask", "werkzeug", "jinja2", "gunicorn", "pillow", "psutil"} <= {name for name, *_ in entries}
    assert all(version and hashes for _name, version, _marker, hashes in entries)
    assert {name for name, *_ in lock_tool().pins(ROOT / "requirements-build.lock")} == {"pip", "setuptools"}


def test_a_requirement_added_without_relocking_is_reported(tmp_path):
    tool, root = lock_tool(), copy(tmp_path)
    with (root / "requirements.txt").open("a", encoding="utf-8") as requirements:
        requirements.write("totally-new-package>=1.0\n")
    assert any("totally-new-package" in problem for problem in tool.check(root))


def run_preparation(tmp_path, monkeypatch, mode="web"):
    from banana_ops.system import System

    calls = []
    monkeypatch.setattr("banana_ops.system.pwd.getpwnam", lambda _: SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setattr("banana_ops.system.set_release_owner", lambda *args, **kwargs: None)
    system = System()
    monkeypatch.setattr(system, "run", lambda command, **kwargs: calls.append(command))
    system.prepare_release({"product": "BananaChat", "mode": mode, "service": "fixture", "revision": "a" * 40},
                           tmp_path)
    return [command for command in calls if "pip" in command and "install" in command]


def test_a_release_with_locks_installs_only_pinned_hashed_wheels(tmp_path, monkeypatch):
    for name in FILES:
        (tmp_path / name).write_text("flask==3.1.3 --hash=sha256:" + "0" * 64 + "\n")
    bootstrap, install = run_preparation(tmp_path, monkeypatch)
    assert bootstrap[bootstrap.index("-r") + 1] == str(tmp_path / "requirements-build.lock")
    assert install[install.index("-r") + 1] == str(tmp_path / "requirements.lock")
    for command in (bootstrap, install):
        assert {"--require-hashes", "--only-binary=:all:"} <= set(command)
    assert not any("pip>=26.2.1" in command for command in (bootstrap, install))


def test_a_release_without_locks_still_installs_wheels_only(tmp_path, monkeypatch):
    """An older release (a rollback) resolves its ranges, but never builds a package from source."""
    (tmp_path / "requirements.txt").write_text("Flask==3.1.3\n")
    bootstrap, install = run_preparation(tmp_path, monkeypatch)
    assert bootstrap[-1] == "pip>=26.2.1"
    assert install[install.index("-r") + 1] == str(tmp_path / "requirements.txt")
    assert "--only-binary=:all:" in install
