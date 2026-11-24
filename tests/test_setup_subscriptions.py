"""scripts/setup_subscriptions.py --allow-bwrap-userns on Ubuntu releases that ship bwrap-userns-restrict."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("setup_subscriptions", ROOT / "scripts/setup_subscriptions.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def _isolate(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(setup, "APPARMOR_PROFILE", tmp_path / "bananachat-bwrap")
    monkeypatch.setattr(setup, "atomic_write", lambda path, text, mode: Path(path).write_text(text))
    monkeypatch.setattr(setup.subprocess, "run", lambda command, **kwargs: calls.append(command))
    return calls


def test_the_packaged_ubuntu_profile_is_disabled_and_unloaded(monkeypatch, tmp_path):
    calls = _isolate(monkeypatch, tmp_path)
    packaged = tmp_path / "bwrap-userns-restrict"
    packaged.write_text("profile bwrap /usr/bin/bwrap {}\n")
    setup.allow_bwrap_userns(False, packaged)
    link = tmp_path / "disable" / "bwrap-userns-restrict"
    assert link.is_symlink() and link.resolve() == packaged
    assert calls == [["apparmor_parser", "-R", str(packaged)],
                     ["apparmor_parser", "-r", str(tmp_path / "bananachat-bwrap")]]
    assert "userns," in (tmp_path / "bananachat-bwrap").read_text()
    setup.allow_bwrap_userns(False, packaged)  # running it again changes nothing
    assert link.is_symlink()


def test_without_the_packaged_profile_only_ours_is_installed(monkeypatch, tmp_path):
    calls = _isolate(monkeypatch, tmp_path)
    setup.allow_bwrap_userns(False, tmp_path / "bwrap-userns-restrict")
    assert not (tmp_path / "disable").exists()
    assert calls == [["apparmor_parser", "-r", str(tmp_path / "bananachat-bwrap")]]


def test_a_dry_run_changes_nothing(monkeypatch, tmp_path, capsys):
    calls = _isolate(monkeypatch, tmp_path)
    packaged = tmp_path / "bwrap-userns-restrict"
    packaged.write_text("")
    setup.allow_bwrap_userns(True, packaged)
    assert calls == [] and not (tmp_path / "disable").exists() and not (tmp_path / "bananachat-bwrap").exists()
    assert "would disable" in capsys.readouterr().out
