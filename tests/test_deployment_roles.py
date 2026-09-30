"""BananaChat server roles: pairing the web server with its compute server, and an existing Ollama.

The two-server layout is the default: ``install --mode compute`` prints a
pairing code, ``install --mode web --pair`` (or ``backend connect``) uses it
and tests the connection first. ``--ollama-url`` lets single and compute
servers use an Ollama that already runs on the machine without managing it.
"""

import json
import socket
import sqlite3
import threading

import pytest

from banana_ops import backend, cli, profile
from banana_ops.files import read_environment, read_json, write_environment, write_json
from banana_ops.manager import Manager
from banana_ops.models import compute_recovery, inventory
from compute.inference_proxy import ComputeServer
from test_managed_lifecycle import Services, checkout, installed, revision  # noqa: F401
from tests.app.fake_ollama import FakeOllama

TOKEN = "c" * 64


@pytest.fixture
def ollama():
    server = FakeOllama().start()
    yield server
    server.stop()


@pytest.fixture
def gateway(ollama, tmp_path):
    """The real compute gateway in front of an imitation Ollama."""
    server = ComputeServer(("127.0.0.1", 0), upstream=ollama.url, token=TOKEN,
                           maintenance=str(tmp_path / "gateway-maintenance"))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}"
    yield server
    server.shutdown()
    server.server_close()


def run_cli(monkeypatch, manager, *arguments):
    monkeypatch.setattr(cli, "Manager", lambda _: manager)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    return cli.main(["--root", str(manager.root), *arguments])


def cli_installer(monkeypatch, tmp_path, checkout):
    """A manager whose CLI installs from the fixture checkout instead of this repository."""
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    install = manager.install
    monkeypatch.setattr(manager, "install", lambda _checkout, **options: install(
        checkout, **{**options, "source_options": {"url": str(checkout), "allow_local": True}}))
    return manager


def private_file(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return path


# ----- pairing codes ---------------------------------------------------------------

def test_a_pairing_code_carries_the_address_and_token_and_survives_line_wrapping():
    code = backend.encode_pairing("https://compute.example.org/", TOKEN)
    assert code.startswith("bcpair1.") and "=" not in code
    wrapped = "  " + code[:30] + "\n" + code[30:] + "\n"
    assert backend.decode_pairing(wrapped) == ("https://compute.example.org", TOKEN)
    assert backend.decode_pairing(backend.encode_pairing("http://127.0.0.1:11435", TOKEN))[0] == "http://127.0.0.1:11435"


@pytest.mark.parametrize("code,message", [
    ("not-a-code", "not a BananaChat pairing code"),
    ("bcpair1.@@@", "incomplete or damaged"),
    ("bcpair1." + "e30", "incomplete or damaged"),
])
def test_a_wrong_pairing_code_is_explained(code, message):
    with pytest.raises(ValueError, match=message):
        backend.decode_pairing(code)


@pytest.mark.parametrize("url", ["http://compute.example.org", "https://user:pw@compute.example.org",
                                 "https://compute.example.org/?a=1", "ftp://compute.example.org"])
def test_pairing_refuses_unencrypted_or_credential_urls(url):
    import base64
    body = base64.urlsafe_b64encode(json.dumps({"v": 1, "url": url, "token": TOKEN}).encode()).decode()
    with pytest.raises(ValueError, match="HTTPS"):
        backend.decode_pairing("bcpair1." + body)


def test_pairing_refuses_a_short_token():
    with pytest.raises(ValueError, match="compute token"):
        backend.encode_pairing("https://compute.example.org", "short")


# ----- connection test ---------------------------------------------------------------

def test_the_connection_test_uses_the_gateway_and_token_like_the_web_server(gateway):
    assert backend.check_backend(gateway.url, TOKEN) == "0.9.0"


def test_connection_problems_are_explained(gateway, tmp_path):
    with pytest.raises(ValueError, match="rejected the token"):
        backend.check_backend(gateway.url, "w" * 64)
    (tmp_path / "gateway-maintenance").write_text("maintenance")
    with pytest.raises(ValueError, match="not ready or maintenance"):
        backend.check_backend(gateway.url, TOKEN)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    with pytest.raises(ValueError, match="cannot be reached"):
        backend.check_backend(closed, TOKEN)


def test_a_server_that_is_not_a_gateway_is_recognised(ollama):
    with pytest.raises(ValueError, match="not a BananaChat compute gateway"):
        backend.check_backend(ollama.url + "/elsewhere", TOKEN)


# ----- web installation ----------------------------------------------------------------

def test_pairing_implies_web_mode_and_stores_the_tested_connection(tmp_path, checkout, monkeypatch, gateway, capsys):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    code = private_file(tmp_path / "pairing", backend.encode_pairing(gateway.url, TOKEN) + "\n")
    assert run_cli(monkeypatch, manager, "install", "--pair-file", str(code), "--yes") == 0
    settings = manager.settings()
    environment = read_environment(manager.config_dir / "app.env")
    assert settings["mode"] == "web" and settings["backend_url"] == gateway.url
    assert environment["BC_OLLAMA_URL"] == gateway.url and environment["BC_OLLAMA_API_KEY"] == TOKEN
    assert not any(key.startswith("OLLAMA_") for key in environment)
    output = capsys.readouterr()
    assert "Connected to the compute server" in output.err and TOKEN not in output.out
    assert "BC_SETUP_TOKEN" in output.out and "updates enable" in output.out


def test_the_pairing_code_can_be_typed_at_a_hidden_prompt(tmp_path, checkout, monkeypatch, gateway):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    monkeypatch.setattr(backend.getpass, "getpass", lambda prompt: backend.encode_pairing(gateway.url, TOKEN))
    assert run_cli(monkeypatch, manager, "install", "--mode", "web", "--pair", "--yes") == 0
    assert read_environment(manager.config_dir / "app.env")["BC_OLLAMA_API_KEY"] == TOKEN


def test_a_failed_connection_test_installs_nothing(tmp_path, checkout, monkeypatch, gateway, capsys):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    code = private_file(tmp_path / "pairing", backend.encode_pairing(gateway.url, "w" * 64))
    assert run_cli(monkeypatch, manager, "install", "--pair-file", str(code), "--yes") == 1
    assert "rejected the token" in capsys.readouterr().err
    assert not (manager.config_dir / "installation.json").exists()


def test_the_old_backend_options_still_work_and_are_tested(tmp_path, checkout, monkeypatch, gateway):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    token = private_file(tmp_path / "token", TOKEN + "\n")
    assert run_cli(monkeypatch, manager, "install", "--mode", "web", "--backend-url", gateway.url,
                   "--backend-token-file", str(token)) == 0
    assert read_environment(manager.config_dir / "app.env")["BC_OLLAMA_API_KEY"] == TOKEN


@pytest.mark.parametrize("arguments,message", [
    (["--mode", "web"], "needs its compute server"),
    (["--mode", "web", "--backend-url", "https://compute.example.org", "--ollama-binary", "/usr/bin/ollama"],
     "never runs Ollama"),
    (["--mode", "web", "--backend-url", "https://compute.example.org", "--ollama-url", "http://127.0.0.1:11434"],
     "never runs Ollama"),
    (["--mode", "compute", "--backend-url", "https://compute.example.org"], "for the web server"),
    (["--mode", "single", "--ollama-url", "http://10.0.0.5:11434", "--skip-connection-check"], "loopback"),
    (["--mode", "compute", "--ollama-url", "http://127.0.0.1:11434", "--ollama-binary", "/usr/bin/ollama"],
     "either --ollama-url"),
    ([], "two servers"),
])
def test_contradictory_install_options_are_refused_before_installing(tmp_path, checkout, monkeypatch, capsys,
                                                                     arguments, message):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    assert run_cli(monkeypatch, manager, "install", *arguments) == 1
    assert message in capsys.readouterr().err
    assert not (manager.config_dir / "installation.json").exists()


def test_a_web_server_gets_no_local_ollama_address_by_default():
    settings = {"root": "/opt/bananachat", "mode": "web", "product": "BananaChat", "port": 8000,
                "source_url": "https://github.com/BananaSuite/BananaChat.git", "backend_url": ""}
    assert "BC_OLLAMA_URL" not in profile.environment(settings)
    settings["backend_url"] = "https://compute.example.org"
    assert profile.environment(settings)["BC_OLLAMA_URL"] == "https://compute.example.org"


# ----- compute server --------------------------------------------------------------------

def test_compute_install_prints_a_pairing_code_for_the_web_server(tmp_path, checkout, monkeypatch, capsys):
    binary = private_file(tmp_path / "ollama", "fixture")
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    assert run_cli(monkeypatch, manager, "install", "--mode", "compute", "--domain", "compute.example.org",
                   "--ollama-binary", str(binary)) == 0
    output = capsys.readouterr().out
    code = next(word for word in output.split() if word.startswith("bcpair1."))
    token = (manager.root / "data/.compute-api-token").read_text().strip()
    assert backend.decode_pairing(code) == ("https://compute.example.org", token)
    assert "--mode web" in output and "--pair" in output and "proxy --install" in output


def test_pairing_code_and_token_rotation(tmp_path, checkout, monkeypatch, capsys):
    manager, services = installed(tmp_path, checkout, "BananaChat", "compute")
    old = (manager.root / "data/.compute-api-token").read_text().strip()
    assert run_cli(monkeypatch, manager, "compute", "pairing-code") == 0
    shown = json.loads(capsys.readouterr().out)
    assert backend.decode_pairing(shown["pairing_code"]) == ("http://127.0.0.1:11435", old)
    assert shown["token_fingerprint"] == backend.fingerprint(old)
    assert run_cli(monkeypatch, manager, "compute", "pairing-code", "--url", "https://gpu.example.org") == 0
    assert backend.decode_pairing(json.loads(capsys.readouterr().out)["pairing_code"])[0] == "https://gpu.example.org"
    restarts = []
    monkeypatch.setattr(services, "start", lambda names: (restarts.extend(names), services.running.update(names)))
    assert run_cli(monkeypatch, manager, "compute", "rotate-token") == 0
    new = (manager.root / "data/.compute-api-token").read_text().strip()
    rotated = json.loads(capsys.readouterr().out)
    assert new != old and backend.decode_pairing(rotated["pairing_code"])[1] == new
    assert restarts == ["bananachat"]  # only the gateway; the managed Ollama keeps running


def test_pairing_commands_belong_to_their_role(tmp_path, checkout, monkeypatch, capsys):
    web, _ = installed(tmp_path, checkout, "BananaChat", "web")
    assert run_cli(monkeypatch, web, "compute", "pairing-code") == 1
    assert "compute server" in capsys.readouterr().err
    (tmp_path / "second").mkdir()
    compute, _ = installed(tmp_path / "second", checkout, "BananaChat", "compute")
    assert run_cli(monkeypatch, compute, "backend", "status") == 1
    assert "Only a web server" in capsys.readouterr().err


# ----- web server connection later ------------------------------------------------------------

def test_backend_connect_status_and_test(tmp_path, checkout, monkeypatch, gateway, capsys):
    manager, services = installed(tmp_path, checkout, "BananaChat", "web")
    restarted = []
    monkeypatch.setattr(services, "stop", lambda names: restarted.extend(names))
    assert run_cli(monkeypatch, manager, "backend", "connect", backend.encode_pairing(gateway.url, TOKEN), "--yes") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "connected" and result["ollama_version"] == "0.9.0" and result["restarted"]
    assert restarted == ["bananachat"]
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["BC_OLLAMA_URL"] == gateway.url and environment["BC_OLLAMA_API_KEY"] == TOKEN
    assert manager.settings()["backend_url"] == gateway.url

    assert run_cli(monkeypatch, manager, "backend", "status") == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["compute_server"] == gateway.url and status["token_fingerprint"] == backend.fingerprint(TOKEN)
    assert TOKEN not in output

    assert run_cli(monkeypatch, manager, "backend", "test") == 0
    assert json.loads(capsys.readouterr().out)["ollama_version"] == "0.9.0"
    assert run_cli(monkeypatch, manager, "status") == 0
    output = capsys.readouterr().out
    assert json.loads(output)["inference"]["compute_server"] == gateway.url and TOKEN not in output

    environment["BC_OLLAMA_API_KEY"] = "w" * 64
    write_environment(manager.config_dir / "app.env", environment)
    assert run_cli(monkeypatch, manager, "backend", "test") == 1
    assert "rejected the token" in capsys.readouterr().err


def test_a_rejected_connection_leaves_the_configuration_alone(tmp_path, checkout, monkeypatch, gateway, capsys):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "web")
    before = (manager.config_dir / "app.env").read_text()
    assert run_cli(monkeypatch, manager, "backend", "connect", backend.encode_pairing(gateway.url, "w" * 64), "--yes") == 1
    assert "rejected the token" in capsys.readouterr().err
    assert (manager.config_dir / "app.env").read_text() == before


def test_backend_connect_accepts_a_url_and_token_file_or_a_tunnel_override(tmp_path, checkout, monkeypatch, gateway):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "web")
    token = private_file(tmp_path / "token", TOKEN)
    assert run_cli(monkeypatch, manager, "backend", "connect", "--url", gateway.url, "--token-file", str(token)) == 0
    code = backend.encode_pairing("https://compute.example.org", TOKEN)
    assert run_cli(monkeypatch, manager, "backend", "connect", code, "--url", gateway.url) == 0
    assert read_environment(manager.config_dir / "app.env")["BC_OLLAMA_URL"] == gateway.url


# ----- an Ollama that already runs on the machine -----------------------------------------------

def test_compute_with_an_existing_ollama_never_manages_it(tmp_path, checkout, monkeypatch, ollama, capsys):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    assert run_cli(monkeypatch, manager, "install", "--mode", "compute", "--ollama-url", ollama.url) == 0
    assert "existing Ollama 0.9.0" in capsys.readouterr().err
    settings = manager.settings()
    environment = read_environment(manager.config_dir / "app.env")
    assert settings["ollama_url"] == ollama.url and not settings["ollama_binary"]
    assert list(profile.service_commands(settings)) == ["bananachat"]
    assert environment["BC_COMPUTE_UPSTREAM"] == ollama.url
    assert "OLLAMA_HOST" not in environment and "OLLAMA_MODELS" not in environment
    assert profile.health_urls(settings)[1] == ollama.url + "/api/version"
    # Updates keep it that way.
    revision(checkout)
    assert manager.update()["outcome"] == "complete"
    assert list(profile.service_commands(manager.settings())) == ["bananachat"]
    assert "OLLAMA_HOST" not in read_environment(manager.config_dir / "app.env")


def test_reinstalling_switches_between_an_existing_and_a_managed_ollama(tmp_path, checkout):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    options = {"source_options": {"url": str(checkout), "allow_local": True}}
    manager.install(checkout, mode="compute", ollama_url="http://127.0.0.1:11500", **options)
    manager.uninstall()
    binary = private_file(tmp_path / "ollama", "fixture")
    manager.install(checkout, mode="compute", ollama_binary=str(binary), reuse_data=True, **options)
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["BC_COMPUTE_UPSTREAM"] == "http://127.0.0.1:11434" == "http://" + environment["OLLAMA_HOST"]
    assert "bananachat-ollama" in profile.service_commands(manager.settings())
    manager.uninstall()
    manager.install(checkout, mode="compute", ollama_url="http://127.0.0.1:11500", reuse_data=True, **options)
    assert read_environment(manager.config_dir / "app.env")["BC_COMPUTE_UPSTREAM"] == "http://127.0.0.1:11500"
    assert list(profile.service_commands(manager.settings())) == ["bananachat"]


def test_an_existing_ollama_must_answer_before_installing(tmp_path, checkout, monkeypatch, capsys):
    manager = cli_installer(monkeypatch, tmp_path, checkout)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    assert run_cli(monkeypatch, manager, "install", "--mode", "single", "--ollama-url", closed) == 1
    assert "No Ollama answers" in capsys.readouterr().err
    assert not (manager.config_dir / "installation.json").exists()


def test_a_single_server_can_use_an_existing_ollama(tmp_path, checkout, ollama):
    manager = Manager(tmp_path / "managed", product="BananaChat", system=Services())
    manager.install(checkout, mode="single", ollama_url=ollama.url + "/",
                    source_options={"url": str(checkout), "allow_local": True})
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["BC_OLLAMA_URL"] == ollama.url and "OLLAMA_MODELS" not in environment
    assert list(profile.service_commands(manager.settings())) == ["bananachat"]


def test_operator_edits_of_the_managed_ollama_survive_updates_and_readiness_follows_them(tmp_path, checkout):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "compute")
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert environment["OLLAMA_MODELS"] == str(manager.root / "data/models")
    environment.update(OLLAMA_HOST="127.0.0.1:11500", OLLAMA_MODELS="/srv/models",
                       BC_COMPUTE_UPSTREAM="http://127.0.0.1:11500")
    write_environment(manager.config_dir / "app.env", environment)
    revision(checkout)
    assert manager.update()["outcome"] == "complete"
    environment = read_environment(manager.config_dir / "app.env")
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11500" and environment["OLLAMA_MODELS"] == "/srv/models"
    assert profile.health_urls(manager.settings())[1] == "http://127.0.0.1:11500/api/version"
    assert "bananachat-ollama" in profile.service_commands(manager.settings())


def test_readiness_ignores_a_non_loopback_address_and_old_installations_stay_managed():
    settings = {"root": "/nonexistent", "mode": "single", "product": "BananaChat", "port": 8000}
    environment = {"BC_OLLAMA_URL": "https://compute.example.org"}
    assert profile.ollama_upstream(settings, environment) == "http://127.0.0.1:11434"
    assert profile.managed_ollama(settings)  # installation.json from the previous release has no ollama_url
    assert profile.ollama_upstream({**settings, "ollama_url": "http://127.0.0.1:11500"}, {}) == "http://127.0.0.1:11500"


def test_the_ollama_port_is_checked_only_for_a_managed_ollama(tmp_path, monkeypatch):
    from banana_ops.system import System
    system = System()
    system.unit_dir, system.bin_dir = tmp_path / "units", tmp_path / "bin"
    monkeypatch.setattr(system, "active", lambda name: False)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        root = tmp_path / "root"
        (root / "config").mkdir(parents=True)
        write_environment(root / "config/app.env", {"BC_COMPUTE_UPSTREAM": f"http://127.0.0.1:{port}"})
        settings = {"root": str(root), "mode": "compute", "product": "BananaChat", "service": "bananachat",
                    "port": 1, "ollama_binary": "/usr/bin/ollama"}
        with pytest.raises(ValueError, match="--ollama-url http://127.0.0.1:%d" % port):
            system.preflight(settings)
        system.preflight({**settings, "ollama_url": f"http://127.0.0.1:{port}"})


def test_a_managed_package_can_be_restored_onto_an_existing_ollama(tmp_path, checkout, ollama):
    manager, _ = installed(tmp_path, checkout, "BananaChat", "compute")
    archive = manager.backup(exclude_model_weights=True)
    restored = Manager(tmp_path / "new-server", product="BananaChat", system=Services())
    restored.restore(archive, new=True, ollama_url=ollama.url)
    environment = read_environment(restored.config_dir / "app.env")
    assert environment["BC_COMPUTE_UPSTREAM"] == ollama.url and "OLLAMA_HOST" not in environment
    assert list(profile.service_commands(restored.settings())) == ["bananachat"]


# ----- model restore on a compute server -------------------------------------------------------------

def test_an_existing_ollama_is_inventoried_through_its_api(tmp_path, ollama):
    result = inventory(tmp_path, {}, upstream=ollama.url)
    assert result["ollama"] == sorted(ollama.models)
    assert result["sizes"] == {name: 2 * 1024 ** 3 for name in ollama.models}


def test_manifest_sizes_are_recorded(tmp_path):
    manifest = tmp_path / "models/manifests/registry.ollama.ai/library/tiny/latest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"config": {"size": 10}, "layers": [{"size": 1000}, {"size": 24}]}))
    assert inventory(tmp_path, {})["sizes"] == {"tiny:latest": 1034}


def test_compute_restore_downloads_only_missing_models_from_the_configured_upstream(tmp_path, ollama):
    write_json(tmp_path / ".model-recovery.json", {"schema": 1, "state": "pending", "inventory": {
        "ollama": ["llama3.2:3b", "missing:1b"], "huggingface": [], "manual": [], "sizes": {"missing:1b": 5}}})
    status = compute_recovery(tmp_path, "status", upstream=ollama.url)
    assert status["missing"] == ["missing:1b"] and status["missing_bytes"] == 5
    with pytest.raises(ValueError, match="1 model"):
        compute_recovery(tmp_path, "restore", upstream=ollama.url)
    result = compute_recovery(tmp_path, "restore", assume_yes=True, upstream=ollama.url)
    assert result["state"] == "complete" and result["completed"] == ["missing:1b"]
    assert [body["name"] for path, body in ollama.requests if path == "/api/pull"] == ["missing:1b"]


def test_compute_restore_completes_by_itself_when_everything_is_installed(tmp_path, ollama):
    write_json(tmp_path / ".model-recovery.json", {"schema": 1, "state": "pending", "inventory": {
        "ollama": ["qwen3:4b"], "huggingface": [], "manual": []}})
    assert compute_recovery(tmp_path, "status", upstream=ollama.url)["message"] == "Every saved model is installed."
    assert read_json(tmp_path / ".model-recovery.json")["state"] == "complete"
    assert "nothing to download" in compute_recovery(tmp_path, "restore", upstream=ollama.url)["message"]
    assert not [path for path, _ in ollama.requests if path == "/api/pull"]


def test_restore_messages_say_what_happens_next(tmp_path, checkout, monkeypatch, capsys):
    web, _ = installed(tmp_path, checkout, "BananaChat", "web")
    with sqlite3.connect(web.root / "data/bananachat.db") as connection:
        connection.executescript("CREATE TABLE ai_models (ollama_name TEXT, backend TEXT, backend_model_name TEXT);"
                                 "INSERT INTO ai_models VALUES ('tiny:latest', 'ollama', 'tiny:latest');")
    assert run_cli(monkeypatch, web, "restore", str(web.backup(exclude_model_weights=True))) == 0
    assert "Admin → Overview offers to download them" in capsys.readouterr().out
    assert run_cli(monkeypatch, web, "models", "status") == 1
    assert "Admin → Overview" in capsys.readouterr().err
    (tmp_path / "compute").mkdir()
    compute, _ = installed(tmp_path / "compute", checkout, "BananaChat", "compute")
    assert run_cli(monkeypatch, compute, "restore", str(compute.backup(exclude_model_weights=True))) == 0
    output = capsys.readouterr().out
    assert "web server connected to this compute server" in output and "models restore --yes" in output


def test_legacy_import_prepares_the_model_list_once_in_the_data_directory(tmp_path, checkout, monkeypatch):
    from banana_ops import legacy_import
    from test_legacy_database_restore import _database, _installed_chat
    manager, _, destination = _installed_chat(tmp_path, checkout)
    calls = []
    prepare = legacy_import.prepare_recovery
    monkeypatch.setattr(legacy_import, "prepare_recovery",
                        lambda data, value, database: (calls.append((data, database)), prepare(data, value, database=database)))
    legacy_import.restore_database(manager, _database(tmp_path / "export.db"))
    assert calls == [(manager.root / "data", destination)]
    assert read_json(manager.root / "data/.model-recovery.json")["state"] == "pending"
