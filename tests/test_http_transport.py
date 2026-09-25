"""Exercise real HTTP responses while controlling DNS and network boundaries."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

import http_transport as transport


@contextmanager
def server(handler):
    class QuietHandler(handler):
        def log_message(self, *_):
            pass
    instance = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "169.254.169.254", "10.0.0.1", "100.64.0.1", "::ffff:127.0.0.1"])
def test_public_request_rejects_internal_addresses(host):
    host = "[" + host + "]" if ":" in host else host
    with pytest.raises(ValueError, match="disallowed"):
        transport.open_http("http://" + host)


def test_dns_is_not_repeated_between_validation_and_connect(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.headers["Host"].encode())
    with server(Handler) as instance:
        calls = []
        def resolve(host, port, **_kwargs):
            calls.append(host)
            address = "8.8.8.8" if len(calls) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
        original_dial = transport._dial
        def dial(addresses, timeout, source_address=None):
            assert addresses[0][-1][0] == "8.8.8.8"
            return original_dial([(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", instance.server_port))], timeout)
        monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
        monkeypatch.setattr(transport, "_dial", dial)
        with transport.open_http("http://fixture.example/resource") as response:
            assert response.read() == b"fixture.example"
        assert calls == ["fixture.example"]


def test_redirects_never_forward_credentials_and_reads_stay_bounded(monkeypatch):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 4096)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with server(Handler) as instance:
        base = "http://127.0.0.1:" + str(instance.server_port)
        req = urllib.request.Request(base + "/redirect", headers={"Authorization": "Bearer fixture-secret"})
        with pytest.raises(urllib.error.HTTPError) as error:
            transport.open_http(req, public_only=False)
        error.value.close()
        assert received == [("/redirect", "Bearer fixture-secret")]
        with transport.open_http(base + "/large", max_bytes=2048, public_only=False) as response:
            assert len(response.read(1024)) == 1024
            with pytest.raises(urllib.error.URLError, match="size limit"):
                response.read()


def test_trusted_camera_policy_allows_lan_but_blocks_metadata(monkeypatch):
    def resolve(_host, port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port))]
    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="disallowed"):
        transport.resolve_target("http://camera.example", public_only=False, address_policy=lambda address: not address.is_link_local)


def test_https_keeps_original_hostname_for_sni_and_certificate_validation(tmp_path, monkeypatch):
    """Pinning a TCP address must not disable TLS identity checks."""
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("TLS certificate fixture requires the OpenSSL command")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "1",
        "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=fixture.example",
        "-addext", "subjectAltName=DNS:fixture.example",
    ], check=True, capture_output=True, timeout=20)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    names = []
    tls.set_servername_callback(lambda _socket, name, _context: names.append(name))
    trusted = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: trusted)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.headers["Host"].encode())

    with server(Handler) as instance:
        instance.socket = tls.wrap_socket(instance.socket, server_side=True)
        monkeypatch.setattr(transport.socket, "getaddrinfo", lambda _host, _port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", instance.server_port)),
        ])
        with transport.open_http("https://fixture.example/", public_only=False) as response:
            assert response.read() == b"fixture.example"
        with pytest.raises(urllib.error.URLError) as failure:
            transport.open_http("https://wrong.example/", public_only=False)
        assert isinstance(failure.value.__cause__, ssl.SSLCertVerificationError)
    assert names == ["fixture.example", "wrong.example"]


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_trickling_headers_and_body_cannot_extend_the_total_deadline(phase):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if phase == "body":
                    self.send_response(200)
                    self.end_headers()
                for byte in (b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n" if phase == "headers" else b"x" * 80):
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.015)
            except (BrokenPipeError, ConnectionResetError):
                pass
    with server(Handler) as instance:
        started = time.monotonic()
        with pytest.raises(urllib.error.URLError):
            with transport.open_http("http://127.0.0.1:" + str(instance.server_port), timeout=1, total_timeout=0.12, public_only=False) as response:
                response.readline()
        assert time.monotonic() - started < 0.6
