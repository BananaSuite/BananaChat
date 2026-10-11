"""Plain HTTP to the checkpoint agent and the sandbox runner only inside networks the operator lists.

Both send a bearer token, so HTTPS is required unless the service is on this
host or its address is an IP literal inside ``BC_CHECKPOINT_AGENT_INSECURE_NETWORKS``
or ``BC_AGENTS_RUNNER_INSECURE_NETWORKS`` (a LAN, a VPN, a container network).
Those lists may only name private ranges; the previous Tailscale-only flags
keep working as aliases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bananachat.config import Config, load_config
from bananachat.services import checkpoint_agent
from bananachat.services.agents import runner as runner_mod

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = {"checkpoint": ("BC_CHECKPOINT_AGENT_INSECURE_NETWORKS", "checkpoint_agent_insecure_networks",
                           "BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE"),
            "runner": ("BC_AGENTS_RUNNER_INSECURE_NETWORKS", "agents_runner_insecure_networks",
                       "BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE")}
TAILSCALE = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")


def _load(**environ):
    return load_config(environ, load_secret=False)


# ----- configuration ------------------------------------------------------------------------

@pytest.mark.parametrize("which", SETTINGS)
def test_private_networks_are_accepted_and_normalised(which):
    name, attribute, _ = SETTINGS[which]
    config = _load(**{name: " 10.0.0.0/8, 192.168.1.0/24,,172.17.0.0/16, 100.64.0.0/10, fd00::/8,"
                            "10.20.0.5, 127.0.0.0/8, ::1, 10.0.0.0/8"})
    assert getattr(config, attribute) == ("10.0.0.0/8", "192.168.1.0/24", "172.17.0.0/16", "100.64.0.0/10",
                                          "fd00::/8", "10.20.0.5/32", "127.0.0.0/8", "::1/128")
    assert config.warnings == ()


@pytest.mark.parametrize("which", SETTINGS)
def test_unset_means_no_networks(which):
    name, attribute, _ = SETTINGS[which]
    assert getattr(_load(), attribute) == ()
    assert getattr(_load(**{name: "  "}), attribute) == ()
    assert getattr(Config(), attribute) == ()


@pytest.mark.parametrize("which", SETTINGS)
@pytest.mark.parametrize("value,reason", [
    ("0.0.0.0/0", "every address"), ("::/0", "every address"),
    ("8.8.8.0/24", "not a private network"), ("10.0.0.0/7", "not a private network"),
    ("172.0.0.0/8", "not a private network"), ("2001:db8::/32", "not a private network"),
    ("169.254.0.0/16", "not a private network"), ("fe80::/10", "not a private network"),
    ("192.0.2.1", "not a private network"), ("::ffff:10.0.0.0/104", "not a private network"),
    ("not-a-network", "not an IP address or network"), ("gpu.example.org", "not an IP address or network"),
    ("10.0.0.1/8", "not an IP address or network"), ("10.0.0.0/33", "not an IP address or network"),
    ("fd00::%eth0/64", "not an IP address or network"), ("10.0.0.0/8 192.168.0.0/16", "not an IP address"),
])
def test_invalid_or_public_networks_are_refused_with_a_warning(which, value, reason):
    name, attribute, _ = SETTINGS[which]
    config = _load(**{name: f"{value},192.168.10.0/24"})
    # The entry is left out; the valid one next to it still applies.
    assert getattr(config, attribute) == ("192.168.10.0/24",)
    [warning] = config.warnings
    assert name in warning and repr(value) in warning and reason in warning


@pytest.mark.parametrize("which", SETTINGS)
def test_the_tailscale_flag_is_an_alias_that_adds_the_tailscale_ranges(which):
    name, attribute, alias = SETTINGS[which]
    flag = attribute.replace("insecure_networks", "allow_insecure_tailscale")
    config = _load(**{alias: "1"})
    assert getattr(config, attribute) == TAILSCALE and getattr(config, flag) is True
    combined = _load(**{alias: "yes", name: "10.1.0.0/16,100.64.0.0/10"})
    assert getattr(combined, attribute) == ("10.1.0.0/16", "100.64.0.0/10", "fd7a:115c:a1e0::/48")
    assert getattr(_load(**{alias: "0"}), attribute) == ()
    assert config.warnings == combined.warnings == ()


def test_each_service_has_its_own_list():
    config = _load(BC_CHECKPOINT_AGENT_INSECURE_NETWORKS="10.0.0.0/8",
                   BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE="1")
    assert config.checkpoint_agent_insecure_networks == ("10.0.0.0/8",)
    assert config.agents_runner_insecure_networks == TAILSCALE
    assert not config.checkpoint_agent_allow_insecure_tailscale


# ----- the URL check ---------------------------------------------------------------------------

LAN = ("10.0.0.0/8", "fd00::/8")


@pytest.mark.parametrize("url", ["http://10.1.2.3:8190", "http://10.1.2.3:8190/", "http://[fd00::5]:8765",
                                 "http://127.0.0.1:8190", "http://localhost:8190", "http://[::1]:8190"])
def test_plain_http_is_accepted_inside_the_listed_networks_and_on_this_host(url):
    assert checkpoint_agent.validate_base_url(url, insecure_networks=LAN) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://192.168.1.5:8190", "http://100.100.1.2:8190", "http://[fc00::1]:8190",
                                 "http://8.8.8.8", "http://[2001:db8::1]:8190", "http://169.254.169.254",
                                 # Names are never resolved for this check, so DNS cannot move the token.
                                 "http://gpu.lan:8190", "http://10.1.2.3.nip.io", "http://gpu.tailnet.ts.net",
                                 "http://0x0a010203", "http://167838211", "http://[::ffff:10.1.2.3]"])
def test_plain_http_is_refused_elsewhere(url):
    with pytest.raises(ValueError, match="HTTPS"):
        checkpoint_agent.validate_base_url(url, insecure_networks=LAN)


def test_plain_http_to_another_machine_is_refused_by_default():
    with pytest.raises(ValueError, match="HTTPS"):
        checkpoint_agent.validate_base_url("http://10.1.2.3:8190")


@pytest.mark.parametrize("networks", [(), LAN, ("0.0.0.0/0",)])
@pytest.mark.parametrize("url", ["https://gpu.example.org", "https://8.8.8.8:8443", "https://10.1.2.3",
                                 "https://[2001:db8::1]"])
def test_https_is_always_accepted(url, networks):
    assert checkpoint_agent.validate_base_url(url, insecure_networks=networks) == url


@pytest.mark.parametrize("networks", [("0.0.0.0/0",), ("::/0",), ("8.0.0.0/8",), ("not a network",),
                                      ("169.254.0.0/16",)])
def test_a_public_network_given_directly_is_still_ignored(networks):
    # load_config refuses these; a Config built in code must not get further.
    for url in ("http://8.8.8.8", "http://[2001:db8::1]", "http://169.254.169.254"):
        with pytest.raises(ValueError, match="HTTPS"):
            checkpoint_agent.validate_base_url(url, insecure_networks=networks)


def test_the_deprecated_flag_keeps_its_tailscale_semantics():
    for url in ("http://100.100.1.2:8190", "http://[fd7a:115c:a1e0::1]:8190", "http://gpu.tailnet.ts.net:8190"):
        assert checkpoint_agent.validate_base_url(url, allow_insecure_tailscale=True) == url
    # The ranges alone admit addresses, not MagicDNS names.
    assert checkpoint_agent.validate_base_url("http://100.100.1.2", insecure_networks=TAILSCALE)
    with pytest.raises(ValueError, match="HTTPS"):
        checkpoint_agent.validate_base_url("http://gpu.tailnet.ts.net", insecure_networks=TAILSCALE)
    with pytest.raises(ValueError, match="HTTPS"):
        checkpoint_agent.validate_base_url("http://10.1.2.3", allow_insecure_tailscale=True)


def test_the_refusal_names_the_setting():
    with pytest.raises(ValueError, match="BC_CHECKPOINT_AGENT_INSECURE_NETWORKS"):
        checkpoint_agent.validate_base_url("http://10.1.2.3")


# ----- the two clients ------------------------------------------------------------------------

def _token(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    token.chmod(0o600)
    return str(token)


def test_the_checkpoint_agent_uses_its_list(tmp_path):
    environ = {"BC_CHECKPOINT_AGENT_URL": "http://192.168.50.7:8765", "BC_CHECKPOINT_AGENT_TOKEN_FILE": _token(tmp_path)}
    refused = checkpoint_agent.configuration_error(_load(**environ))
    assert "HTTPS" in refused and "BC_CHECKPOINT_AGENT_INSECURE_NETWORKS" in refused
    config = _load(**environ, BC_CHECKPOINT_AGENT_INSECURE_NETWORKS="192.168.50.0/24")
    assert checkpoint_agent.configuration_error(config) is None
    assert checkpoint_agent.client(config).base_url == "http://192.168.50.7:8765"
    # The runner's list does not open the agent.
    other = _load(**environ, BC_AGENTS_RUNNER_INSECURE_NETWORKS="192.168.50.0/24")
    assert checkpoint_agent.configuration_error(other) is not None


def test_the_sandbox_runner_uses_its_list(tmp_path):
    environ = {"BC_AGENTS_RUNNER_URL": "http://10.8.0.2:11436", "BC_AGENTS_RUNNER_TOKEN_FILE": _token(tmp_path)}
    with pytest.raises(runner_mod.RunnerUnavailable, match="BC_AGENTS_RUNNER_INSECURE_NETWORKS"):
        runner_mod.Runner(_load(**environ))
    with pytest.raises(runner_mod.RunnerUnavailable):
        runner_mod.Runner(_load(**environ, BC_CHECKPOINT_AGENT_INSECURE_NETWORKS="10.8.0.0/24"))
    assert runner_mod.Runner(_load(**environ, BC_AGENTS_RUNNER_INSECURE_NETWORKS="10.8.0.0/24")).base_url \
        == "http://10.8.0.2:11436"
    tailscale = {"BC_AGENTS_RUNNER_URL": "http://100.101.102.103:11436", "BC_AGENTS_RUNNER_TOKEN": "tok",
                 "BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE": "1"}
    assert runner_mod.Runner(_load(**tailscale)).base_url == "http://100.101.102.103:11436"


PLAIN = "token is not protected by TLS"


@pytest.mark.parametrize("scheme", ["http", "HTTP", "Http"])
def test_plain_http_to_another_machine_is_logged_whatever_the_scheme_s_case(tmp_path, caplog, monkeypatch, scheme):
    monkeypatch.setattr(checkpoint_agent, "_PLAIN_HTTP_WARNED", set())
    config = _load(BC_CHECKPOINT_AGENT_URL=f"{scheme}://10.0.0.5:8765", BC_CHECKPOINT_AGENT_TOKEN_FILE=_token(tmp_path),
                   BC_CHECKPOINT_AGENT_INSECURE_NETWORKS="10.0.0.0/8")
    with caplog.at_level("WARNING", logger="bananachat.checkpoint_agent"):
        checkpoint_agent.client(config)
        checkpoint_agent.client(config)
    assert [record.getMessage() for record in caplog.records if PLAIN in record.getMessage()] == [
        "The checkpoint agent is reached over plain HTTP on a private network; its token is not protected by TLS"]


def test_the_sandbox_runner_logs_plain_http_to_another_machine_too(caplog, monkeypatch):
    monkeypatch.setattr(checkpoint_agent, "_PLAIN_HTTP_WARNED", set())
    lan = {"BC_AGENTS_RUNNER_TOKEN": "tok", "BC_AGENTS_RUNNER_INSECURE_NETWORKS": "10.8.0.0/24"}
    with caplog.at_level("WARNING", logger="bananachat.checkpoint_agent"):
        runner_mod.Runner(_load(BC_AGENTS_RUNNER_URL="http://10.8.0.2:11436", **lan))
        runner_mod.Runner(_load(BC_AGENTS_RUNNER_URL="HTTP://10.8.0.2:11436", **lan))
        runner_mod.Runner(_load(BC_AGENTS_RUNNER_URL="http://127.0.0.1:11436", BC_AGENTS_RUNNER_TOKEN="tok"))
        runner_mod.Runner(_load(BC_AGENTS_RUNNER_URL="https://10.8.0.2:11436", BC_AGENTS_RUNNER_TOKEN="tok"))
    warned = [record.getMessage() for record in caplog.records if PLAIN in record.getMessage()]
    assert warned == ["The sandbox runner is reached over plain HTTP on a private network; its token is not "
                      "protected by TLS"] * 2


def test_runner_health_is_not_reused_across_network_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod.Runner, "health", lambda self: {"ok": True})
    environ = {"BC_AGENTS_RUNNER_URL": "http://10.8.0.2:11436", "BC_AGENTS_RUNNER_TOKEN_FILE": _token(tmp_path)}
    refused = runner_mod.health(_load(**environ), fresh=True)
    assert not refused["ok"] and "HTTPS" in refused["error"]
    assert runner_mod.health(_load(**environ, BC_AGENTS_RUNNER_INSECURE_NETWORKS="10.8.0.0/24"))["ok"]


# ----- documentation ----------------------------------------------------------------------------

def test_the_settings_are_documented():
    reference = (ROOT / "docs" / "configuration.md").read_text()
    sample = (ROOT / ".env.example").read_text()
    for name, _, alias in SETTINGS.values():
        assert f"`{name}`" in reference and f"# {name}=" in sample
        line = next(line for line in reference.splitlines() if f"`{alias}`" in line)
        assert "deprecated" in line.lower() and name in line
