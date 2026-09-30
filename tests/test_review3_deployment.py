"""Third review of the two-server deployment: pairing, token rotation, existing Ollama and model inventory."""

import json
import os
import socket

import pytest

from banana_ops import backend, profile
from banana_ops.files import read_environment, write_environment
from banana_ops.manager import Manager
from banana_ops.models import inventory
from test_deployment_roles import run_cli
from test_managed_lifecycle import Services, checkout, installed  # noqa: F401
from tests.app.fake_ollama import FakeOllama


def closed_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def test_rotate_token_checks_the_address_before_replacing_the_token(tmp_path, checkout, monkeypatch, capsys):
    manager, services = installed(tmp_path, checkout, "BananaChat", "compute")
    token = manager.root / "data/.compute-api-token"
    old = token.read_text()
    restarts = []
    monkeypatch.setattr(services, "stop", lambda names: restarts.extend(names))
    assert run_cli(monkeypatch, manager, "compute", "rotate-token", "--url", "http://compute.example.org") == 1
    assert "HTTPS" in capsys.readouterr().err
    # The old token still works and nothing was restarted: the web server stays connected.
    assert token.read_text() == old and restarts == []


@pytest.mark.skipif(os.geteuid() != 0, reason="changing file owners needs root")
def test_rotate_token_keeps_the_owner_of_an_operator_chosen_token_file(tmp_path, checkout, monkeypatch, capsys):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "compute")
    custom = tmp_path / "etc-bananachat" / "gateway.token"
    custom.parent.mkdir()
    custom.write_text("d" * 64 + "\n")
    custom.chmod(0o600)
    os.chown(custom, 4321, 4321)  # readable by the gateway's account only
    environment = read_environment(manager.config_dir / "app.env")
    environment["BC_COMPUTE_TOKEN_FILE"] = str(custom)
    write_environment(manager.config_dir / "app.env", environment)
    assert run_cli(monkeypatch, manager, "compute", "rotate-token") == 0
    rotated = json.loads(capsys.readouterr().out)
    assert backend.decode_pairing(rotated["pairing_code"])[1] == custom.read_text().strip() != "d" * 64
    info = custom.stat()
    assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (4321, 4321, 0o600)


def test_status_survives_a_damaged_model_recovery_file(tmp_path, checkout, monkeypatch, capsys):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "compute")
    (manager.root / "data/.model-recovery.json").write_text("{not json")
    assert run_cli(monkeypatch, manager, "status") == 0
    assert json.loads(capsys.readouterr().out)["inference"]["model_recovery"] == "invalid"


def test_restoring_into_an_installation_keeps_its_existing_ollama(tmp_path, checkout):
    # A package from a server that ran its own Ollama (or from before a switch).
    (tmp_path / "old").mkdir()
    old, _ = installed(tmp_path / "old", checkout, "BananaChat", "compute")
    package = old.backup(exclude_model_weights=True)
    # This machine keeps an Ollama that BananaChat must not manage or replace.
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    manager.install(checkout, mode="compute", ollama_url="http://127.0.0.1:11500",
                    source_options={"url": str(checkout), "allow_local": True})
    manager.restore(package)
    settings = manager.settings()
    environment = read_environment(manager.config_dir / "app.env")
    assert settings["ollama_url"] == "http://127.0.0.1:11500" and not settings["ollama_binary"]
    assert list(profile.service_commands(settings)) == ["bananachat"]
    assert environment["BC_COMPUTE_UPSTREAM"] == "http://127.0.0.1:11500"
    assert "OLLAMA_HOST" not in environment and "OLLAMA_MODELS" not in environment


def test_restoring_its_own_package_keeps_the_operators_upstream_edit(tmp_path, checkout, ollama_server):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    manager.install(checkout, mode="compute", ollama_url=ollama_server.url,
                    source_options={"url": str(checkout), "allow_local": True})
    environment = read_environment(manager.config_dir / "app.env")
    environment["BC_COMPUTE_UPSTREAM"] = "http://localhost:" + ollama_server.url.rsplit(":", 1)[1]
    write_environment(manager.config_dir / "app.env", environment)
    manager.restore(manager.backup(exclude_model_weights=True))
    assert read_environment(manager.config_dir / "app.env")["BC_COMPUTE_UPSTREAM"] == environment["BC_COMPUTE_UPSTREAM"]


@pytest.fixture
def ollama_server():
    server = FakeOllama().start()
    yield server
    server.stop()


def test_a_weight_free_backup_does_not_silently_lose_the_existing_ollamas_model_list(tmp_path):
    with pytest.raises(ValueError, match="did not answer"):
        inventory(tmp_path, {}, upstream=closed_url())


def test_a_backup_with_an_unreachable_existing_ollama_fails_and_keeps_running(tmp_path, checkout):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    manager.install(checkout, mode="compute", ollama_url=closed_url(),
                    source_options={"url": str(checkout), "allow_local": True})
    with pytest.raises(ValueError, match="did not answer"):
        manager.backup(exclude_model_weights=True)
    assert manager.system.active("bananachat") and not (manager.root / "data/.banana-maintenance").exists()
    assert not list((manager.root / "backups").glob("manual-*"))


# ----- follow-up: the installation's Ollama choice wins in both directions --------------------------

def test_restoring_an_existing_ollama_package_into_a_managed_installation_keeps_it_managed(tmp_path, checkout,
                                                                                          ollama_server):
    old = Manager(tmp_path / "old", product="BananaChat", system=Services())
    old.install(checkout, mode="compute", ollama_url=ollama_server.url,
                source_options={"url": str(checkout), "allow_local": True})
    package = old.backup()
    manager, _ = installed(tmp_path, checkout, "BananaChat", "compute")
    before = read_environment(manager.config_dir / "app.env")
    binary = manager.settings()["ollama_binary"]
    manager.restore(package)
    settings = manager.settings()
    environment = read_environment(manager.config_dir / "app.env")
    assert not settings.get("ollama_url") and settings["ollama_binary"] == binary
    assert list(profile.service_commands(settings)) == ["bananachat-ollama", "bananachat"]
    for key in ("BC_COMPUTE_UPSTREAM", "OLLAMA_HOST", "OLLAMA_MODELS"):
        assert environment[key] == before[key]


def test_a_stale_managed_ollama_unit_is_disabled_when_an_existing_ollama_is_used(tmp_path, monkeypatch):
    from banana_ops.system import System
    system = System()
    system.unit_dir, system.bin_dir = tmp_path / "units", tmp_path / "bin"
    system.unit_dir.mkdir()
    commands = []
    monkeypatch.setattr(system, "run", lambda command, **options: commands.append([str(part) for part in command]))
    root = tmp_path / "root"
    stale = system.unit_dir / "bananachat-ollama.service"
    stale.write_text(f"# Managed by BananaSuite\n[Service]\nWorkingDirectory={root / 'current'}\n")
    unrelated = system.unit_dir / "other-ollama.service"
    unrelated.write_text("[Service]\nExecStart=/usr/bin/ollama serve\n")
    settings = {"root": str(root), "mode": "compute", "product": "BananaChat", "service": "bananachat",
                "port": 11435, "ollama_binary": "", "ollama_url": "http://127.0.0.1:11434"}
    system.install_units(settings)
    assert not stale.exists() and unrelated.exists()
    assert ["systemctl", "disable", "--now", "bananachat-ollama"] in commands
    # A managed Ollama keeps its unit.
    commands.clear()
    system.install_units({**settings, "ollama_url": "", "ollama_binary": "/usr/bin/ollama"})
    assert stale.exists() and not [command for command in commands if "--now" in command]


# ----- follow-up: install --restore keeps the package's role and connection --------------------------

@pytest.mark.parametrize("option", [["--pair"], ["--pair", "bcpair1.x"], ["--pair-file", "/root/code"],
                                    ["--backend-url", "https://compute.example.org"],
                                    ["--backend-token-file", "/root/token"], ["--mode", "web"],
                                    ["--skip-connection-check"]])
def test_install_restore_refuses_connection_options(tmp_path, monkeypatch, capsys, option):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    monkeypatch.setattr(manager, "restore", lambda *args, **kwargs: pytest.fail("nothing may be restored"))
    assert run_cli(monkeypatch, manager, "install", "--restore", str(tmp_path / "package.tar.gz"), *option) == 1
    error = capsys.readouterr().err
    assert option[0] in error and "backend connect" in error


# ----- follow-up: the compute token only travels over HTTPS or loopback ----------------------------------

def test_backend_test_refuses_a_plain_http_remote_address(tmp_path, checkout, monkeypatch, capsys):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "web")
    environment = read_environment(manager.config_dir / "app.env")
    environment.update(BC_OLLAMA_URL="http://compute.example.org", BC_OLLAMA_API_KEY="c" * 64)
    write_environment(manager.config_dir / "app.env", environment)
    monkeypatch.setattr(backend, "_get", lambda *args, **kwargs: pytest.fail("the token must not be sent"))
    assert run_cli(monkeypatch, manager, "backend", "test") == 1
    assert "HTTPS" in capsys.readouterr().err


# ----- follow-up: confirm the address in a pairing code before sending the token ------------------------

@pytest.fixture
def pairing_target(ollama_server):
    """A server that records every request: nothing may reach it before confirmation."""
    return ollama_server, backend.encode_pairing(ollama_server.url, "c" * 64)


def cli_installer(monkeypatch, tmp_path, checkout):
    from test_deployment_roles import cli_installer as installer
    return installer(monkeypatch, tmp_path, checkout)


def test_pairing_without_a_terminal_needs_yes(tmp_path, checkout, monkeypatch, capsys, pairing_target):
    server, code = pairing_target
    monkeypatch.setattr(backend.sys.stdin, "isatty", lambda: False, raising=False)
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    assert run_cli(monkeypatch, manager, "install", "--pair", code) == 1
    error = capsys.readouterr().err
    assert server.url in error and "--yes" in error
    assert server.authorizations == [] and not (manager.config_dir / "installation.json").exists()
    (tmp_path / "web").mkdir()
    web, _ = installed(tmp_path / "web", checkout, "BananaChat", "web")
    before = (web.config_dir / "app.env").read_text()
    assert run_cli(monkeypatch, web, "backend", "connect", code) == 1
    assert "--yes" in capsys.readouterr().err
    assert server.authorizations == [] and (web.config_dir / "app.env").read_text() == before


@pytest.mark.parametrize("answer,result", [("n", 1), ("", 1), ("yes", 0)])
def test_pairing_at_a_terminal_asks_for_the_address(tmp_path, checkout, monkeypatch, capsys, pairing_target,
                                                     answer, result):
    server, code = pairing_target
    asked = []
    monkeypatch.setattr(backend.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: (asked.append(prompt), answer)[1])
    web, _ = installed(tmp_path, checkout, "BananaChat", "web")
    assert run_cli(monkeypatch, web, "backend", "connect", code) == result
    assert len(asked) == 1 and server.url in asked[0]
    if result:
        assert server.authorizations == [] and "cancelled" in capsys.readouterr().err
    else:
        assert read_environment(web.config_dir / "app.env")["BC_OLLAMA_URL"] == server.url


def test_yes_skips_the_question(tmp_path, checkout, monkeypatch, pairing_target):
    server, code = pairing_target
    monkeypatch.setattr(backend.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("--yes must not ask"))
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    assert run_cli(monkeypatch, manager, "install", "--pair", code, "--yes") == 0
    assert read_environment(manager.config_dir / "app.env")["BC_OLLAMA_URL"] == server.url


# ----- follow-up: tokens and a failed connect -------------------------------------------------------------

def test_a_token_must_be_ascii():
    with pytest.raises(ValueError, match="printable"):
        backend.check_token("é" * 40)


def test_a_failed_connect_puts_the_previous_connection_back(tmp_path, checkout, monkeypatch, capsys, pairing_target):
    server, code = pairing_target
    web, services = installed(tmp_path, checkout, "BananaChat", "web")
    environment_before = (web.config_dir / "app.env").read_text()
    settings_before = web.settings()
    services.fail_revision = settings_before["revision"]  # readiness fails after the restart
    assert run_cli(monkeypatch, web, "backend", "connect", code, "--yes") == 1
    assert "readiness" in capsys.readouterr().err
    assert (web.config_dir / "app.env").read_text() == environment_before
    assert web.settings()["backend_url"] == settings_before["backend_url"]
