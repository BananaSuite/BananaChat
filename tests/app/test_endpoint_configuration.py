"""Invalid backend settings must keep the site usable without rerouting secrets."""

from __future__ import annotations

import pytest

from bananachat.config import DEFAULT_SOURCE_URL, load_config
from bananachat.services.upstream import UpstreamError, open_request


BAD_URLS = ("http://[", "https://host:bad", "https://host:65536", "https://host:0",
            "https://host／other", "ftp://127.0.0.1", "https://host/path\nother",
            "https://user:synthetic-secret@host", "https://host?key=synthetic-secret",
            "https://host/#fragment", "https://host/é", "https://host:", "https://host\\other")


@pytest.mark.parametrize("name,attribute", [("BC_OLLAMA_URL", "ollama_url"),
                                           ("BC_COMFYUI_URL", "comfyui_url"),
                                           ("BC_INFERENCE_FALLBACK_URL", "inference_fallback_url")])
@pytest.mark.parametrize("url", BAD_URLS)
def test_invalid_backend_urls_disable_the_endpoint_without_crashing(name, attribute, url):
    config = load_config({name: url, "BC_OLLAMA_API_KEY": "synthetic-secret",
                          "BC_INFERENCE_FALLBACK_API_KEY": "synthetic-secret"}, load_secret=False)
    assert getattr(config, attribute) == ""
    assert any(name in warning for warning in config.warnings)
    assert "synthetic-secret" not in " ".join(config.warnings)
    if name == "BC_OLLAMA_URL":
        assert config.ollama_api_key == "" and not config.ollama_is_local
    elif name == "BC_INFERENCE_FALLBACK_URL":
        assert config.inference_fallback_api_key == ""


@pytest.mark.parametrize("url", ["http://[", "https://host:bad", "https://host:65536",
                                 "https://host:0", "https://host／other"])
def test_invalid_source_urls_use_the_default(url):
    config = load_config({"BC_SOURCE_URL": url}, load_secret=False)
    assert config.source_url == DEFAULT_SOURCE_URL
    assert any("BC_SOURCE_URL" in warning for warning in config.warnings)


@pytest.mark.parametrize("token", ["synthetic-secret\nheader", "synthetic secret", "synthetic-é", "x" * 4097])
@pytest.mark.parametrize("name,attribute", [("BC_OLLAMA_API_KEY", "ollama_api_key"),
                                           ("BC_INFERENCE_FALLBACK_API_KEY", "inference_fallback_api_key")])
def test_invalid_backend_tokens_are_never_sent_or_logged(name, attribute, token):
    config = load_config({name: token, "BC_OLLAMA_URL": "https://compute.example.org",
                          "BC_INFERENCE_FALLBACK_URL": "https://backup.example.org"}, load_secret=False)
    assert getattr(config, attribute) == ""
    assert any(name in warning for warning in config.warnings)
    assert token not in " ".join(config.warnings)


def test_valid_endpoint_paths_ipv6_and_source_parameters_are_preserved():
    config = load_config({"BC_OLLAMA_URL": "http://[::1]:11434/proxy/",
                          "BC_OLLAMA_API_KEY": "synthetic-token",
                          "BC_SOURCE_URL": "https://example.org/source?ref=release#license",
                          "BC_COMFYUI_URL": "https://images.example.org/proxy/%C3%A9/"}, load_secret=False)
    assert not config.warnings
    assert config.ollama_url == "http://[::1]:11434/proxy"
    assert config.ollama_api_key == "synthetic-token"
    assert config.source_url == "https://example.org/source?ref=release#license"
    assert config.comfyui_url == "https://images.example.org/proxy/%C3%A9"


def test_invalid_fallback_cannot_be_enabled():
    config = load_config({"BC_INFERENCE_OUTAGE_MODE": "fallback",
                          "BC_INFERENCE_FALLBACK_URL": "https://host:bad"}, load_secret=False)
    assert config.inference_outage_mode == "shutdown" and not config.fallback_enabled


@pytest.mark.parametrize("url", ["http://[", "https://host:bad", "https://host:65536", "http://host:0"])
def test_direct_transport_reports_malformed_urls_without_opening_a_socket(monkeypatch, url):
    import socket

    def unexpected_connection(*args, **kwargs):
        pytest.fail("An invalid URL must never create a socket")

    monkeypatch.setattr(socket, "create_connection", unexpected_connection)
    with pytest.raises(UpstreamError, match="URL"):
        open_request("GET", url, "/api/version")


def test_a_bad_primary_keeps_authentication_and_pages_available(make_app, monkeypatch):
    import socket

    from bananachat.services import housekeeping, ollama
    from tests.app.fixtures import Browser

    app = make_app(OLLAMA_URL="https://[", OLLAMA_API_KEY="synthetic-token")
    browser = Browser(app)
    browser.login("admin")
    assert browser.get("/account").status_code == 200
    assert browser.get("/admin/").status_code == 200
    assert browser.get("/status").status_code == 200

    def unexpected_connection(*args, **kwargs):
        pytest.fail("An invalid primary must not contact a default local service")

    monkeypatch.setattr(socket, "create_connection", unexpected_connection)
    with app.app_context():
        assert ollama.endpoint() == ("", {})
        with pytest.raises(UpstreamError):
            ollama.version()
        housekeeping.probe_inference(app)
