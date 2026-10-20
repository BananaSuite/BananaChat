"""The managed web service may write exactly the enabled subscription connectors' profile directories."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from banana_ops import system
from banana_ops.files import write_environment
from bananachat.services import claude_sandbox

CLAUDE = {"BC_CLAUDE_EXTENSION": "bananachat.services.claude_code", "BC_CLAUDE_CODE_CONFIG": "/srv/bc/claude.json"}
CODEX = {"BC_CODEX_EXTENSION": "bananachat.services.codex_cli", "BC_CODEX_CLI_CONFIG": "/srv/bc/codex.json"}
ROOT = Path("/opt/bananachat")


def _manifests(monkeypatch, found):
    calls = []

    def managed_paths(manifest, *, uid, root, keys, forbidden):
        calls.append((manifest, keys, list(forbidden)))
        return [Path(path) for path in found[manifest]]

    monkeypatch.setattr(claude_sandbox, "managed_paths", managed_paths)
    return calls


def test_each_enabled_connector_is_read_with_its_own_profile_keys(monkeypatch):
    calls = _manifests(monkeypatch, {"/srv/bc/claude.json": ["/srv/claude/one"],
                                     "/srv/bc/codex.json": ["/srv/codex/one"]})
    assert system.subscription_paths({}, uid=1001, root=ROOT) == []
    assert system.subscription_paths({**CLAUDE, **CODEX}, uid=1001, root=ROOT) == \
        [Path("/srv/claude/one"), Path("/srv/codex/one")]
    # Neither provider's profiles may expose the other provider's manifest.
    assert calls == [("/srv/bc/claude.json", ("home", "config_dir"), ["/srv/bc/codex.json"]),
                     ("/srv/bc/codex.json", ("codex_home",), ["/srv/bc/claude.json"])]


def test_a_third_party_extension_gets_no_profile_access(monkeypatch):
    calls = _manifests(monkeypatch, {})
    environment = {**CLAUDE, "BC_CLAUDE_EXTENSION": "operator_extensions.claude"}
    assert system.subscription_paths(environment, uid=1001, root=ROOT) == []
    assert calls == []


@pytest.mark.parametrize("codex_home", ["/srv/claude/one", "/srv/claude/one/codex", "/srv/claude"])
def test_claude_and_codex_profiles_must_not_overlap(monkeypatch, codex_home):
    _manifests(monkeypatch, {"/srv/bc/claude.json": ["/srv/claude/one"], "/srv/bc/codex.json": [codex_home]})
    with pytest.raises(ValueError, match="separate private directories"):
        system.subscription_paths({**CLAUDE, **CODEX}, uid=1001, root=ROOT)


def test_an_unreadable_manifest_names_its_provider(monkeypatch):
    def managed_paths(manifest, **_):
        raise FileNotFoundError(manifest)

    monkeypatch.setattr(claude_sandbox, "managed_paths", managed_paths)
    with pytest.raises(ValueError, match="native Codex manifest"):
        system.subscription_paths(CODEX, uid=1001, root=ROOT)


def test_codex_profiles_are_validated_under_their_own_key(monkeypatch):
    monkeypatch.setattr(claude_sandbox, "private_path", lambda value, **_: Path(value))
    home = Path("/srv/codex-test/one")
    with pytest.raises(claude_sandbox.SandboxError, match="outside application"):
        claude_sandbox.profile_paths({"one": {"codex_home": str(home)}}, uid=1001, keys=("codex_home",),
                                     forbidden=(home / "claude.json",))


@pytest.mark.parametrize("mode,granted", [("web", True), ("compute", False)])
def test_only_the_web_service_unit_gets_the_profile_directories(tmp_path, monkeypatch, mode, granted):
    managed = system.System()
    managed.unit_dir, managed.bin_dir = tmp_path / "units", tmp_path / "bin"
    managed.unit_dir.mkdir()
    managed.bin_dir.mkdir()
    monkeypatch.setattr(managed, "run", lambda command, **options: None)
    monkeypatch.setattr(system.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001))
    seen = []
    monkeypatch.setattr(system, "subscription_paths",
                        lambda environment, **_: seen.append(environment) or [Path("/srv/claude/one")])
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    write_environment(root / "config/app.env", CLAUDE)
    managed.install_units({"root": str(root), "mode": mode, "product": "BananaChat", "service": "bananachat",
                           "port": 8000, "ollama_binary": "", "ollama_url": "http://127.0.0.1:11434"})
    unit = (managed.unit_dir / "bananachat.service").read_text()
    assert ("ReadWritePaths=/srv/claude/one\n" in unit) is granted
    assert seen == ([CLAUDE] if granted else [])
