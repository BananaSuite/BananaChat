"""Serving behind Cloudflare's proxy: the rendered Caddyfile, the detection and `proxy`."""

import json
import os
import shutil
import socket
import subprocess

import pytest

from banana_ops import cli, cloudflare, profile
from banana_ops.files import read_json, write_json
from banana_ops.manager import Manager

SINGLE = {"schema": 1, "product": "BananaChat", "mode": "single", "service": "bananachat", "domain": "chat.example.org",
          "port": 5000}
COMPUTE = {**SINGLE, "mode": "compute", "domain": "compute.example.org", "port": 11435}
HOSTING = {**SINGLE, "product": "BananaWiki", "mode": "hosting", "portal_domain": "portal.example.org",
           "domain": "example.org", "port": 5099, "root": "/opt/bananachat"}
RANGES = " ".join(cloudflare.RANGES)


def test_cloudflare_addresses():
    assert cloudflare.is_cloudflare_address("104.16.1.1") and cloudflare.is_cloudflare_address("2606:4700::1")
    assert cloudflare.is_cloudflare_address("::ffff:172.64.0.1") and cloudflare.is_cloudflare_address("[2606:4700::1]")
    assert not cloudflare.is_cloudflare_address("203.0.113.5") and not cloudflare.is_cloudflare_address("nonsense")
    assert cloudflare.all_cloudflare(["104.16.1.1", "172.67.2.2"])
    assert not cloudflare.all_cloudflare([]) and not cloudflare.all_cloudflare(["104.16.1.1", "198.51.100.1"])
    assert len(cloudflare.IPV4) == 15 and len(cloudflare.IPV6) == 7 and cloudflare.UPDATED


def answer(monkeypatch, *addresses):
    """Make every lookup return *addresses*."""
    def lookup(host, port, **options):
        return [(socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (value, 0, 0, 0) if ":" in value else (value, 0)) for value in addresses]
    monkeypatch.setattr(socket, "getaddrinfo", lookup)


def test_a_domain_is_proxied_only_when_every_address_is_cloudflare(monkeypatch):
    answer(monkeypatch, "104.16.1.1", "2606:4700::1")
    assert cloudflare.proxied("chat.example.org")
    answer(monkeypatch, "104.16.1.1", "203.0.113.5")
    assert not cloudflare.proxied("chat.example.org")
    answer(monkeypatch, "203.0.113.5")
    assert not cloudflare.proxied("chat.example.org")


def test_a_failed_lookup_is_not_cloudflare(monkeypatch):
    def refuse(*arguments, **options):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert not cloudflare.proxied("chat.example.org")


def test_caddy_versions():
    assert profile.parse_version("v2.10.2 h1:g/gT=") == (2, 10, 2)
    assert profile.parse_version("") is None
    assert profile.supports_client_ip(None) and profile.supports_client_ip((2, 7, 0))
    assert not profile.supports_client_ip((2, 6, 2))


def test_the_site_answers_plain_http_without_a_cloudflare_redirect_loop():
    text = profile.caddyfile(SINGLE)
    assert "\nchat.example.org, http://chat.example.org {\n" in text
    assert ("\t@needs_https {\n\t\tprotocol http\n\t\tnot {\n\t\t\tremote_ip " + RANGES + "\n"
            '\t\t\theader CF-Visitor *"scheme":"https"*\n\t\t}\n\t}\n'
            "\tredir @needs_https https://{host}{uri} 308\n") in text


def test_the_application_gets_the_visitors_address_from_one_trusted_hop():
    text = profile.caddyfile(SINGLE)
    assert "\t\ttrusted_proxies static " + RANGES + "\n\t\tclient_ip_headers CF-Connecting-IP\n" in text
    assert "\t\theader_up X-Forwarded-For {client_ip}\n" in text
    # BC_PROXY_HOPS=1 (ProxyFix) reads the last entry; the application sets HSTS itself.
    assert "Strict-Transport-Security" not in text
    old = profile.caddyfile(SINGLE, client_ip=False)
    assert "trusted_proxies" not in old and "client_ip" not in old and "X-Forwarded-For" not in old


def test_streaming_and_timeouts_suit_cloudflare():
    text = profile.caddyfile(SINGLE)
    assert "\t\tflush_interval -1\n" in text
    # Cloudflare reuses idle origin connections for 900 s.
    assert "idle 16m" in text and "idle 2m" not in text
    assert "read_header 10s" in text and "read_body 5m" in text and "write" not in text.replace("read_body", "")


def test_tls_alpn_is_disabled_only_behind_cloudflare():
    assert "\tcert_issuer acme {\n\t\tdisable_tlsalpn_challenge\n\t}\n" in profile.caddyfile(SINGLE, cloudflare_proxy=True)
    assert "cert_issuer" not in profile.caddyfile(SINGLE)


def test_the_email_is_a_global_option():
    assert profile.caddyfile(COMPUTE, email="ops@example.org").startswith(
        "# Managed by BananaSuite\n{\n\temail ops@example.org\n")
    assert "email" not in profile.caddyfile(COMPUTE)
    for value in ("ops", "a@b.org\n}", "a b@example.org", "{a}@example.org"):
        with pytest.raises(ValueError, match="e-mail"):
            profile.caddyfile(COMPUTE, email=value)


def test_a_domain_is_required():
    with pytest.raises(ValueError, match="domain"):
        profile.caddyfile({**SINGLE, "domain": ""})


def test_hosting_keeps_its_configuration():
    text = profile.caddyfile(HOSTING)
    assert "idle 2m" in text and ":443 {\n\ttls {\n\t\ton_demand\n" in text
    assert "trusted_proxies" not in text and "cert_issuer" not in text


# Real Caddy (set CADDY_BINARY, or have caddy on PATH)


def caddy_binary():
    return os.environ.get("CADDY_BINARY") or shutil.which("caddy")


@pytest.mark.parametrize("settings", [SINGLE, COMPUTE, HOSTING], ids=["single", "compute", "hosting"])
@pytest.mark.parametrize("options", [{}, {"cloudflare_proxy": True}, {"client_ip": False},
                                     {"cloudflare_proxy": True, "client_ip": False, "email": "ops@example.org"}],
                         ids=["plain", "cloudflare", "legacy", "everything"])
def test_rendered_caddyfiles_validate(tmp_path, settings, options):
    binary = caddy_binary()
    if not binary:
        pytest.skip("needs a caddy binary (CADDY_BINARY, or caddy on PATH)")
    config = tmp_path / "Caddyfile"
    config.write_text(profile.caddyfile(settings, **options))
    result = subprocess.run([binary, "validate", "--config", str(config), "--adapter", "caddyfile"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    if settings["mode"] != "hosting":
        formatted = subprocess.run([binary, "fmt", str(config)], capture_output=True, text=True)
        assert formatted.stdout == config.read_text(), "the rendered file is already `caddy fmt` clean"


def test_the_adapted_configuration_keeps_the_policy(tmp_path):
    binary = caddy_binary()
    if not binary:
        pytest.skip("needs a caddy binary (CADDY_BINARY, or caddy on PATH)")
    config = tmp_path / "Caddyfile"
    config.write_text(profile.caddyfile(SINGLE, cloudflare_proxy=True))
    adapted = subprocess.run([binary, "adapt", "--config", str(config), "--adapter", "caddyfile"],
                             capture_output=True, text=True)
    assert adapted.returncode == 0, adapted.stderr[-1500:]
    loaded = json.loads(adapted.stdout)
    servers = loaded["apps"]["http"]["servers"].values()
    # Both listeners (443 and the plain HTTP one for ACME HTTP-01) share the timeouts and the trusted ranges.
    assert len(servers) == 2
    for server in servers:
        assert server["idle_timeout"] == 16 * 60 * 10**9 and server["read_header_timeout"] == 10 * 10**9
        assert server["trusted_proxies"]["ranges"] == list(cloudflare.RANGES)
        assert server["client_ip_headers"] == ["CF-Connecting-IP"]
        # One matcher set: plain HTTP and not (a Cloudflare peer whose visitor used HTTPS).
        matcher = server["routes"][0]["handle"][0]["routes"][0]["match"][0]
        assert matcher["protocol"] == "http" and len(matcher["not"]) == 1 and matcher["not"][0].keys() == {"remote_ip", "header"}
    issuer = loaded["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]
    assert issuer == {"module": "acme", "challenges": {"tls-alpn": {"disabled": True}}}
    config.write_text(profile.caddyfile(SINGLE, cloudflare_proxy=True, email="ops@example.org"))
    adapted = subprocess.run([binary, "adapt", "--config", str(config), "--adapter", "caddyfile"], capture_output=True, text=True)
    assert json.loads(adapted.stdout)["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]["email"] == "ops@example.org"


# `bananachat proxy`


class Host:
    """A fake host whose Caddy reports a version."""

    def __init__(self, tmp, version="v2.10.2 h1:x"):
        self.proxy_file = tmp / "Caddyfile"
        self.version = version
        self.commands = []

    def run(self, command, *, check=True, **options):
        self.commands.append(command)
        if command[:2] == ["caddy", "version"]:
            if self.version is None:
                raise FileNotFoundError("caddy")
            return subprocess.CompletedProcess(command, 0, self.version, "")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "opt"
    write_json(root / "config/installation.json", {**SINGLE, "root": str(root)})
    host = Host(tmp_path)
    return Manager(root, product="BananaChat", system=host), host


@pytest.fixture(autouse=True)
def unproxied(monkeypatch):
    answer(monkeypatch, "203.0.113.5")


def proxy(manager, *arguments):
    return cli.configure_proxy(manager, cli.parser().parse_args(["--root", str(manager.root), "proxy", *arguments]))


def test_proxy_detects_cloudflare_from_dns_and_says_so(installed, monkeypatch, capsys):
    manager, _ = installed
    assert proxy(manager) is None
    captured = capsys.readouterr()
    assert "does not resolve to Cloudflare" in captured.err and "ACME HTTP-01" not in captured.err
    assert "cert_issuer" not in captured.out and captured.out.startswith("# Managed by BananaSuite\n")
    answer(monkeypatch, "104.16.1.1", "2606:4700::1")
    assert proxy(manager) is None
    captured = capsys.readouterr()
    assert "treated as proxied" in captured.err and "Always Use HTTPS" in captured.err
    assert "/.well-known/acme-challenge/" in captured.err and "not supported here" in captured.err
    assert "disable_tlsalpn_challenge" in captured.out and "Cloudflare" not in captured.out


def test_proxy_arguments_override_the_detection(installed, monkeypatch, capsys):
    manager, _ = installed
    assert proxy(manager, "--cloudflare") is None
    captured = capsys.readouterr()
    assert "treated as proxied (--cloudflare)" in captured.err and "disable_tlsalpn_challenge" in captured.out
    answer(monkeypatch, "104.16.1.1")
    assert proxy(manager, "--no-cloudflare") is None
    captured = capsys.readouterr()
    assert "not proxied (--no-cloudflare)" in captured.err and "cert_issuer" not in captured.out


def test_proxy_does_not_look_the_domain_up_when_told(installed, monkeypatch):
    manager, _ = installed

    def refuse(*arguments, **options):
        raise AssertionError("no lookup")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    proxy(manager, "--no-cloudflare")
    proxy(manager, "--cloudflare")


def test_proxy_install_reports_the_decision(installed, capsys):
    manager, host = installed
    result = proxy(manager, "--install", "--cloudflare", "--email", "ops@example.org")
    assert result == {"outcome": "proxy configured", "domain": "chat.example.org", "cloudflare": True}
    text = host.proxy_file.read_text()
    assert "email ops@example.org" in text and "disable_tlsalpn_challenge" in text
    assert ["caddy", "validate", "--config", str(manager.config_dir / "Caddyfile.next"), "--adapter", "caddyfile"] in host.commands
    assert read_json(manager.config_dir / "proxy.json").keys() == {"installed_sha256", "previous"}
    assert proxy(manager, "--install", "--no-cloudflare")["cloudflare"] is False
    assert "cert_issuer" not in host.proxy_file.read_text()


def test_an_old_caddy_cannot_pass_the_real_address(installed, capsys):
    manager, host = installed
    host.version = "v2.6.2 h1:x"
    proxy(manager, "--cloudflare")
    captured = capsys.readouterr()
    assert "2.7 or newer" in captured.err
    assert "trusted_proxies" not in captured.out and "{client_ip}" not in captured.out
    proxy(manager, "--no-cloudflare")
    assert "2.7 or newer" not in capsys.readouterr().err


def test_an_unknown_caddy_is_assumed_current(installed, capsys):
    manager, host = installed
    host.version = None
    proxy(manager, "--cloudflare")
    captured = capsys.readouterr()
    assert "2.7 or newer" not in captured.err and "{client_ip}" in captured.out


def test_proxy_needs_a_domain(installed):
    manager, _ = installed
    write_json(manager.config_dir / "installation.json", {**SINGLE, "root": str(manager.root), "domain": ""})
    with pytest.raises(ValueError, match="domain"):
        proxy(manager)
