"""Agents and Git: strict repository addresses, the SSRF-safe archive download, importing into the sandbox,
patch export, the administrator's settings and the pages.

Nothing here reaches the internet: the fetcher's resolver and connector are replaced so that "public"
addresses lead to a local HTTPS server whose certificate is signed by a throw-away test CA (made with
``openssl``), and the sandbox runner is the in-process fake (the real scripts run in Docker in
``test_agents_git_docker.py``).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.app.conftest import Browser
from tests.app.test_agents import (  # noqa: F401 - fixtures (fast_loop is autouse)
    add_user, agents_app, call, env, error_key, fast_loop, runner, set_settings, start, start_ok, steps, token_file,
    wait_status)

PUBLIC = {"codeload.github.com": ["140.82.112.10"], "github.com": ["140.82.112.3"], "gitlab.com": ["172.65.251.78"],
          "git.example.org": ["93.184.215.14"], "codeberg.org": ["217.197.91.145"]}
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ----- helpers --------------------------------------------------------------------------------

def tarball(files: dict, top: str = "demo-main") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


DEMO = tarball({"README.md": b"# Demo\n", "src/app.py": b"print('hi')\n"})


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    """A test CA and a server certificate for some hosts (not codeberg.org), made with openssl."""
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    folder = tmp_path_factory.mktemp("tls")
    (folder / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
        "subjectAltName=DNS:codeload.github.com,DNS:github.com,DNS:gitlab.com,DNS:git.example.org\n")

    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=folder, check=True, capture_output=True, timeout=60)

    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout",
            "ca-key", "-out", "ca-cert", "-days", "30", "-subj", "/CN=BananaChat test CA", "-addext",
            "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext",
            "subjectKeyIdentifier=hash")
    openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "leaf-key",
            "-out", "leaf-csr", "-subj", "/CN=codeload.github.com")
    openssl("x509", "-req", "-in", "leaf-csr", "-CA", "ca-cert", "-CAkey", "ca-key", "-CAcreateserial", "-out",
            "leaf-cert", "-days", "30", "-extfile", "leaf.ext")
    return types.SimpleNamespace(ca=folder / "ca-cert", cert=folder / "leaf-cert", key=folder / "leaf-key")


class ArchiveServer:
    """A local HTTPS server. ``routes[path]`` is ``(status, headers, body)`` or ``callable(handler)``."""

    def __init__(self, tls_files):
        self.routes: dict = {}
        self.requests: list[tuple[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                server.requests.append((self.headers.get("Host"), self.path))
                route = server.routes.get(self.path, (404, {"Content-Type": "text/plain"}, b"not found"))
                if callable(route):
                    return route(self)
                status, headers, body = route
                self.send_response(status)
                for name, value in {"Content-Length": str(len(body)), **headers}.items():
                    self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_files.cert, tls_files.key)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def archive(self, path: str, data: bytes, **headers) -> None:
        self.routes[path] = (200, {"Content-Type": "application/x-gzip", **headers}, data)

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        self.routes[path] = (status, {"Location": location}, b"")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def net(monkeypatch, tls_files):
    """The fetcher's resolver, connector and TLS trust pointed at a local HTTPS server."""
    from bananachat.services.agents import gitfetch

    server = ArchiveServer(tls_files)
    state = types.SimpleNamespace(server=server, dns={host: list(values) for host, values in PUBLIC.items()},
                                  lookups=[], connects=[], target=("127.0.0.1", server.port))

    def resolve(host):
        state.lookups.append(host)
        if host not in state.dns:
            raise gitfetch.FetchError("agents.import_network")
        return list(state.dns[host])

    def connect(address, port, timeout):
        state.connects.append((address, port))
        return socket.create_connection(state.target, timeout=timeout)

    monkeypatch.setattr(gitfetch, "_resolve", resolve)
    monkeypatch.setattr(gitfetch, "_connect", connect)
    monkeypatch.setattr(gitfetch, "_ssl_context", lambda: ssl.create_default_context(cafile=str(tls_files.ca)))
    monkeypatch.delenv("BC_AGENTS_GIT_PROXY", raising=False)
    yield state
    server.close()


def fetch(tmp_path, url="https://github.com/octo/demo", ref="main", *, max_bytes=1024 * 1024, hosts=None,
          cancel=None):
    from bananachat.services.agents import gitfetch

    hosts = hosts or gitfetch.DEFAULT_HOSTS
    source = gitfetch.parse(url, ref, hosts)
    tmp_path.mkdir(exist_ok=True)
    destination = tmp_path / f"archive-{time.monotonic_ns()}.tar.gz"
    size = gitfetch.download(source, destination, max_bytes=max_bytes, hosts=hosts, cancel=cancel)
    return destination, size


def fetch_error(tmp_path, *args, **kwargs) -> str:
    from bananachat.services.agents import gitfetch

    folder = tmp_path / "failed"
    with pytest.raises(gitfetch.FetchError) as caught:
        fetch(folder, *args, **kwargs)
    assert not list(folder.glob("archive-*")), "a failed download left a file behind"
    return caught.value.key


# ----- parsing (no network) ----------------------------------------------------------------------

def test_repository_addresses_are_parsed_strictly():
    from bananachat.services.agents import gitfetch

    hosts = (*gitfetch.DEFAULT_HOSTS, "gitea:git.example.org", "gitlab:gitlab.example.org")
    good = {
        ("https://github.com/octo/demo", ""): ("codeload.github.com", "/octo/demo/tar.gz/HEAD", "demo"),
        ("https://GitHub.com/Octo-Cat/my.repo_2.git/", "v1.2.3"): ("codeload.github.com",
                                                                   "/Octo-Cat/my.repo_2/tar.gz/v1.2.3", "my.repo_2"),
        ("github.com/octo/demo", "feature/login-form"): ("codeload.github.com",
                                                         "/octo/demo/tar.gz/feature/login-form", "demo"),
        ("https://github.com:443/octo/demo", COMMIT): ("codeload.github.com", f"/octo/demo/tar.gz/{COMMIT}", "demo"),
        ("https://gitlab.com/group/sub/project", "main"): (
            "gitlab.com", "/group/sub/project/-/archive/main/project-main.tar.gz", "project"),
        ("https://gitlab.example.org/team/app", "release/2"): (
            "gitlab.example.org", "/team/app/-/archive/release/2/app-release-2.tar.gz", "app"),
        ("https://codeberg.org/forgejo/forgejo", ""): ("codeberg.org", "/forgejo/forgejo/archive/HEAD.tar.gz",
                                                      "forgejo"),
        ("https://git.example.org/me/tool.git", "v2"): ("git.example.org", "/me/tool/archive/v2.tar.gz", "tool"),
    }
    for (url, ref), (host, path, directory) in good.items():
        source = gitfetch.parse(url, ref, hosts)
        assert source.archive() == (host, path), url
        assert source.directory == directory
        assert gitfetch.Source.from_dict(json.loads(json.dumps(source.to_dict()))) == source

    refused = {
        "http://github.com/octo/demo": "agents.import_https_only",
        "git@github.com:octo/demo.git": "agents.import_https_only",
        "ssh://git@github.com/octo/demo": "agents.import_https_only",
        "file:///etc/passwd": "agents.import_https_only",
        "github.com:octo/demo": "agents.import_https_only",
        "https://user:pass@github.com/octo/demo": "agents.import_bad_url",
        "https://github.com/octo/demo?ref=main": "agents.import_bad_url",
        "https://github.com/octo/demo#readme": "agents.import_bad_url",
        "https://github.com/octo/%2e%2e/demo": "agents.import_bad_url",
        "https://github.com/octo/../demo": "agents.import_bad_url",
        "https://github.com/octo/demo/tree/main": "agents.import_bad_url",
        "https://github.com/octo": "agents.import_bad_url",
        "https://github.com//demo": "agents.import_bad_url",
        "https://github.com/octo_cat/demo": "agents.import_bad_url",
        "https://github.com/-octo/demo": "agents.import_bad_url",
        "https://github.com/octo/-demo": "agents.import_bad_url",
        "https://github.com/octo/.bananachat-export-0": "agents.import_bad_url",
        "https://github.com/octo/de mo": "agents.import_bad_url",
        "https://github.com/octo/démo": "agents.import_bad_url",
        "https://github.com/octo/de\nmo": "agents.import_bad_url",
        "https://github.com/octo/de\tmo": "agents.import_bad_url",
        "https://github.com/octo/demo\\x": "agents.import_bad_url",
        "https://github.com/octo/$(reboot)": "agents.import_bad_url",
        "https://github.com:8443/octo/demo": "agents.import_bad_url",
        "https://github.com/" + "a" * 300: "agents.import_bad_url",
        "https://gitlab.com/a/b/c/d/e/f": "agents.import_bad_url",
        "https://evil.example/octo/demo": "agents.import_host_not_allowed",
        "https://github.com.evil.example/octo/demo": "agents.import_host_not_allowed",
        "https://127.0.0.1/octo/demo": "agents.import_bad_url",
        "https://[::1]/octo/demo": "agents.import_bad_url",
        "https://169.254.169.254/latest/meta-data": "agents.import_bad_url",
        "https://localhost/octo/demo": "agents.import_bad_url",
        "": "agents.import_bad_url",
    }
    for url, key in refused.items():
        with pytest.raises(gitfetch.SourceError) as caught:
            gitfetch.parse(url, "", hosts)
        assert caught.value.key == key, url
    for ref in ("../main", "-x", "a..b", "a//b", "x.lock", "x.lock/y", "main@{1}", "a b", "$(reboot)", ";rm -rf",
                "feature/", ".hidden", "a/.b", "x" * 201, "re\nf", "é", "a~1", "a^", "a:b", "*", "a?"):
        with pytest.raises(gitfetch.SourceError) as caught:
            gitfetch.parse("https://github.com/octo/demo", ref, hosts)
        assert caught.value.key == "agents.import_bad_ref", ref
    with pytest.raises(gitfetch.SourceError):  # a stored request is validated again
        gitfetch.Source.from_dict({"host": "github.com", "provider": "github", "owner": "o/../x", "repo": "r",
                                   "ref": "main"})


def test_allowed_host_entries_are_validated_and_normalised():
    from bananachat.services.agents import gitfetch
    from bananachat.services.agents import settings as agent_settings

    assert gitfetch.parse_host_entry(" GitHub.com ") == ("github.com", "github")
    assert gitfetch.parse_host_entry("gitlab:Git.Example.org") == ("git.example.org", "gitlab")
    for entry in ("example.org", "http://github.com", "github:example.org", "gitea:github.com", "gitlab:127.0.0.1",
                  "gitlab:localhost", "gitlab:exa mple.org", "gitlab:", "svn:example.org", "gitlab:*.example.org"):
        with pytest.raises(ValueError):
            gitfetch.parse_host_entry(entry)
    assert gitfetch.normalise_hosts(["github.com", "bogus", "GITHUB.COM", "gitea:git.example.org"]) == \
        ("github.com", "gitea:git.example.org")
    assert gitfetch.download_hosts(["github.com", "codeberg.org"]) == {"github.com", "codeload.github.com",
                                                                       "codeberg.org"}
    defaults = agent_settings.normalise({})
    assert defaults.git_enabled is False and defaults.git_hosts == gitfetch.DEFAULT_HOSTS
    assert defaults.git_max_mb == 50 and defaults.git_imports_per_hour == 10
    wild = agent_settings.normalise({"git_enabled": "yes", "git_max_mb": 10**6, "git_imports_per_hour": 0,
                                     "git_hosts": ["evil", 5, "gitlab:git.example.org"]})
    assert wild.git_enabled is False and wild.git_max_mb == 50 and wild.git_imports_per_hour == 1
    assert wild.git_hosts == ("gitlab:git.example.org",)
    assert agent_settings.normalise({"git_hosts": "github.com"}).git_hosts == gitfetch.DEFAULT_HOSTS


def test_only_public_addresses_are_accepted():
    from bananachat.services.agents import gitfetch

    blocked = ["10.0.0.5", "172.16.3.4", "192.168.1.1", "127.0.0.1", "127.8.9.1", "0.0.0.0", "169.254.169.254",
               "100.64.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255", "192.0.2.10", "198.18.0.1", "::",
               "::1", "fe80::1", "fc00::1", "fd12::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
               "2002:0a00:0001::1", "2002:7f00:0001::1", "64:ff9b::a00:1", "64:ff9b::7f00:1", "2001:db8::1",
               "fe80::1%eth0", "::ffff:140.82.112.3", "64:ff9b::808:808", "not-an-address", ""]
    for address in blocked:
        assert gitfetch.is_public_address(address) is False, address
    for address in ("140.82.112.3", "8.8.8.8", "2606:4700:4700::1111", "2a01:4f8:c0c:1234::1"):
        assert gitfetch.is_public_address(address) is True, address


def test_scripts_and_their_answers_are_strict():
    from bananachat.services.agents import gitrepo

    staging, out = gitrepo.STAGING_PREFIX + "0" * 16, gitrepo.EXPORT_PREFIX + "a" * 16
    command = gitrepo.import_command(staging, "/workspace/demo", "Imported o/demo@it's $(reboot)")
    assert "message='Imported o/demo@it'\"'\"'s $(reboot)'" in command  # quoted, never interpreted
    for bad in (("/workspace/x", "/workspace/demo"), (staging, "/workspace/../etc"), (staging, "/etc"),
                (staging, "/workspace/a b"), (staging, "/workspace/.hidden")):
        with pytest.raises(ValueError):
            gitrepo.import_command(*bad, "m")
    assert "GIT_CONFIG_GLOBAL=/dev/null" in gitrepo.export_command("/workspace/demo", COMMIT, out, 10)
    for bad in (("/workspace/demo", "HEAD", out), ("/workspace/demo", COMMIT, "/tmp/x"),
                ("/workspace/../demo", COMMIT, out), ("/workspace/demo", COMMIT + "; reboot", out)):
        with pytest.raises(ValueError):
            gitrepo.export_command(*bad, 10)
    with pytest.raises(ValueError):
        gitrepo.cleanup_command("/workspace")

    assert gitrepo.parse_import({"stdout": f"BCIMPORT ok git {COMMIT}\n"}) == gitrepo.ImportOutcome(True, True,
                                                                                                    COMMIT)
    assert gitrepo.parse_import({"stdout": "BCIMPORT ok nogit\n"}).git is False
    assert gitrepo.parse_import({"stdout": "BCIMPORT error exists\n"}).error == "exists"
    for stdout in ("", "BCIMPORT ok git HEAD", f"BCIMPORT ok git {COMMIT}; rm", "noise", None, 5):
        assert gitrepo.parse_import({"stdout": stdout}).ok is False
    assert gitrepo.parse_import({"stdout": "", "timed_out": True}).error == "timeout"
    ok = gitrepo.parse_export({"stdout": f"BCEXPORT ok 2097153 {'f' * 64}\n"})
    assert ok.ok and ok.size == 2097153 and ok.parts == 3
    assert gitrepo.parse_export({"stdout": "BCEXPORT error toolarge 99999999"}).error == "toolarge"
    for stdout in ("BCEXPORT ok -1 " + "f" * 64, "BCEXPORT ok 5 xyz", "BCEXPORT ok 1234567890123 " + "f" * 64):
        assert gitrepo.parse_export({"stdout": stdout}).ok is False

    record = json.dumps({"key": "agents.notice_imported", "params": {
        "repo": "o/demo@main", "host": "github.com", "name": "demo", "dir": "/workspace/demo", "git": True,
        "commit": COMMIT, "files": 2, "size": 10}})
    assert gitrepo.parse_record(record)["commit"] == COMMIT
    for change in ({"dir": "/workspace/../etc"}, {"dir": "/etc"}, {"name": "a/b"}, {"repo": 5}):
        data = json.loads(record)
        data["params"].update(change)
        assert gitrepo.parse_record(json.dumps(data)) is None, change
    data = json.loads(record)
    data["params"]["commit"] = "HEAD"
    assert gitrepo.parse_record(json.dumps(data))["git"] is False  # no usable base: no export
    assert gitrepo.parse_record('{"key": "agents.stopped", "params": {}}') is None
    assert gitrepo.parse_record("not json") is None and gitrepo.parse_record(None) is None


# ----- the download (local HTTPS server; no internet) ---------------------------------------------------

def test_download_connects_to_the_checked_address_over_verified_tls(net, tmp_path):
    net.server.archive("/octo/demo/tar.gz/main", DEMO)
    destination, size = fetch(tmp_path)
    assert size == len(DEMO) and destination.read_bytes() == DEMO
    assert destination.stat().st_mode & 0o777 == 0o600
    assert net.lookups == ["codeload.github.com"]           # resolved once ...
    assert net.connects == [("140.82.112.10", 443)]           # ... and connected to that address
    assert net.server.requests == [("codeload.github.com", "/octo/demo/tar.gz/main")]


def test_private_addresses_from_dns_are_refused_before_connecting(net, tmp_path):
    net.dns["codeload.github.com"] = ["10.0.0.7"]
    assert fetch_error(tmp_path) == "agents.import_blocked_address"
    net.dns["codeload.github.com"] = ["140.82.112.10", "127.0.0.1"]   # one bad answer is enough
    assert fetch_error(tmp_path) == "agents.import_blocked_address"
    net.dns["codeload.github.com"] = ["::ffff:169.254.169.254"]
    assert fetch_error(tmp_path) == "agents.import_blocked_address"
    net.dns["codeload.github.com"] = []
    assert fetch_error(tmp_path) == "agents.import_network"
    assert net.connects == [] and net.server.requests == []


def test_redirects_only_to_allowed_hosts_and_each_hop_is_checked(net, tmp_path):
    server = net.server
    server.redirect("/octo/demo/tar.gz/main", "https://codeload.github.com/octo/renamed/tar.gz/main")
    server.archive("/octo/renamed/tar.gz/main", DEMO)
    assert fetch(tmp_path)[1] == len(DEMO)
    assert net.lookups == ["codeload.github.com", "codeload.github.com"]  # resolved and checked again

    cases = {
        "https://evil.example/archive.tar.gz": "agents.import_redirect_blocked",           # another host
        "http://codeload.github.com/octo/x/tar.gz/main": "agents.import_redirect_blocked",  # not HTTPS
        "https://codeload.github.com:8443/x": "agents.import_redirect_blocked",             # another port
        "https://user@codeload.github.com/x": "agents.import_redirect_blocked",             # credentials
        "https://10.0.0.1/x": "agents.import_redirect_blocked",
        "//evil.example/x": "agents.import_redirect_blocked",                               # scheme-relative
        "": "agents.import_redirect_blocked",
        "https://gitlab.com/users/sign_in": "agents.import_private",                        # private repository
    }
    for location, key in cases.items():
        server.requests.clear()
        server.redirect("/octo/demo/tar.gz/main", location)
        assert fetch_error(tmp_path) == key, location
        assert len(server.requests) == 1
    # An allowed host whose name now resolves to a private address.
    net.dns["gitlab.com"] = ["192.168.0.10"]
    server.redirect("/octo/demo/tar.gz/main", "https://gitlab.com/o/demo/-/archive/main/demo-main.tar.gz")
    net.connects.clear()
    assert fetch_error(tmp_path) == "agents.import_blocked_address"
    assert net.connects == [("140.82.112.10", 443)]
    # At most three redirects.
    for index in range(5):
        server.redirect(f"/loop/{index}", f"/loop/{index + 1}")
    server.redirect("/octo/demo/tar.gz/main", "/loop/0")
    assert fetch_error(tmp_path) == "agents.import_redirects"
    assert sum(1 for _, path in server.requests if path.startswith("/loop/")) == 3


def test_download_errors_are_clear(net, tmp_path):
    server = net.server
    path = "/octo/demo/tar.gz/main"
    for status, key in ((404, "agents.import_not_found"), (410, "agents.import_not_found"),
                        (401, "agents.import_private"), (403, "agents.import_private"),
                        (429, "agents.import_rate_limited"), (500, "agents.import_upstream"),
                        (304, "agents.import_upstream")):
        server.routes[path] = (status, {"Content-Type": "application/octet-stream"}, b"x")
        assert fetch_error(tmp_path) == key, status
    server.routes[path] = (200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>sign in</html>")
    assert fetch_error(tmp_path) == "agents.import_not_archive"
    server.routes[path] = (200, {"Content-Type": "application/octet-stream"}, b"PK\x03\x04 not gzip")
    assert fetch_error(tmp_path) == "agents.import_not_archive"
    server.routes[path] = (200, {"Content-Type": "application/octet-stream"}, b"")
    assert fetch_error(tmp_path) == "agents.import_not_archive"
    # TLS is verified for the host name: this server has no certificate for codeberg.org.
    server.archive("/o/demo/archive/HEAD.tar.gz", DEMO)
    assert fetch_error(tmp_path, "https://codeberg.org/o/demo", "") == "agents.import_tls"
    # A server that does not speak TLS at all.
    plain = socket.socket()
    plain.bind(("127.0.0.1", 0))
    plain.listen(5)

    def answer_plain_http():
        connection, _ = plain.accept()
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        connection.close()

    threading.Thread(target=answer_plain_http, daemon=True).start()
    net.target = plain.getsockname()
    assert fetch_error(tmp_path) in ("agents.import_tls", "agents.import_network")
    plain.close()


def test_the_size_cap_is_enforced_while_streaming(net, tmp_path):
    from bananachat.services.agents import gitfetch

    server = net.server
    cap = 256 * 1024
    big = b"\x1f\x8b" + b"x" * (cap * 4)
    server.archive("/octo/demo/tar.gz/main", big)
    assert fetch_error(tmp_path, max_bytes=cap) == "agents.import_too_large"  # Content-Length says so

    sent = {"bytes": 0}

    def endless(handler):  # chunked, no length: only counting while reading can stop it
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        piece = b"\x1f\x8b" + b"y" * 65534
        try:
            for _ in range(10_000):
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                sent["bytes"] += len(piece)
        except OSError:
            pass

    server.routes["/octo/demo/tar.gz/main"] = endless
    started = time.monotonic()
    assert fetch_error(tmp_path, max_bytes=cap) == "agents.import_too_large"
    assert time.monotonic() - started < 20
    assert sent["bytes"] < 200 * cap  # the download stopped early instead of reading everything
    assert gitfetch.CHUNK <= cap


def test_timeouts_and_stop_end_the_download(net, tmp_path, monkeypatch):
    from bananachat.services.agents import gitfetch
    from bananachat.services.upstream import Cancelled, CancelToken

    release = threading.Event()

    def slow(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", "1000000")
        handler.end_headers()
        handler.wfile.write(b"\x1f\x8b" + b"z" * 1000)
        handler.wfile.flush()
        release.wait(20)

    net.server.routes["/octo/demo/tar.gz/main"] = slow
    monkeypatch.setattr(gitfetch, "TOTAL_TIMEOUT", 1.0)
    started = time.monotonic()
    assert fetch_error(tmp_path) == "agents.import_timeout"
    assert time.monotonic() - started < 5

    monkeypatch.setattr(gitfetch, "TOTAL_TIMEOUT", 60.0)
    cancel = CancelToken()
    threading.Timer(0.5, cancel.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        fetch(tmp_path / "stopped", cancel=cancel)
    assert time.monotonic() - started < 5 and not list((tmp_path / "stopped").glob("archive-*"))
    release.set()


def test_the_proxy_is_only_used_when_the_operator_opts_in(net, tmp_path, monkeypatch):
    from bananachat.services.agents import gitfetch

    net.server.archive("/octo/demo/tar.gz/main", DEMO)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    proxy_port = listener.getsockname()[1]
    tunnels: list[bytes] = []

    def proxy():
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            request = b""
            while b"\r\n\r\n" not in request:
                request += client.recv(4096)
            tunnels.append(request.split(b"\r\n\r\n")[0])
            upstream = socket.create_connection(("127.0.0.1", net.server.port))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

            def pipe(source, target):
                try:
                    while data := source.recv(65536):
                        target.sendall(data)
                except OSError:
                    pass
                finally:
                    for sock in (source, target):
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    threading.Thread(target=proxy, daemon=True).start()
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:secret@127.0.0.1:{proxy_port}")
    fetch(tmp_path)  # not opted in: HTTPS_PROXY is ignored
    assert tunnels == [] and net.connects == [("140.82.112.10", 443)]

    monkeypatch.setenv("BC_AGENTS_GIT_PROXY", "1")
    net.connects.clear()
    monkeypatch.setattr(gitfetch, "_connect", lambda address, port, timeout: socket.create_connection(
        (address, port), timeout=timeout))
    _, size = fetch(tmp_path)
    assert size == len(DEMO)
    # The proxy is asked for the address that was checked here, never for a name it would resolve itself.
    assert tunnels[0].startswith(b"CONNECT 140.82.112.10:443 HTTP/1.1")
    assert b"Proxy-Authorization: Basic dXNlcjpzZWNyZXQ=" in tunnels[0]
    net.dns["codeload.github.com"] = ["10.1.1.1"]
    assert fetch_error(tmp_path) == "agents.import_blocked_address" and len(tunnels) == 1
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    net.dns["codeload.github.com"] = ["140.82.112.10"]
    assert fetch_error(tmp_path) == "agents.import_network"
    listener.close()


# ----- import into the sandbox and patch export (fake runner) --------------------------------------------

def git_runner(runner, *, git=True, patch=b"", export_answer=None):
    """Make the fake runner answer the import, export and clean-up scripts like the real image would."""
    state = types.SimpleNamespace(imports=[], exports=[], cleanups=[], patch=patch, export_answer=export_answer)

    def handler(box, command, timeout):
        if "BCIMPORT" in command:
            state.imports.append(command)
            staging = re.search(r"staging='?(/workspace/\.bananachat-import-[0-9a-f]{16})", command).group(1)
            target = re.search(r"target='?(/workspace/[A-Za-z0-9_.-]+)", command).group(1)
            for path in [path for path in box.files if path.startswith(staging + "/")]:
                rest = path[len(staging) + 1:].split("/", 1)[1]
                box.write(f"{target}/{rest}", box.files.pop(path))
            box.dirs = {folder for folder in box.dirs if not folder.startswith(staging)}
            return {"stdout": f"BCIMPORT ok git {COMMIT}\n" if git else "BCIMPORT ok nogit\n"}
        if "BCEXPORT" in command:
            state.exports.append(command)
            out = re.search(r"out='?(/workspace/\.bananachat-export-[0-9a-f]{16})", command).group(1)
            if state.export_answer is not None:
                return {"stdout": state.export_answer}
            data = state.patch
            for index in range(0, len(data), 1024 * 1024):
                box.write(f"{out}/part-{index // (1024 * 1024):03d}", data[index:index + 1024 * 1024])
            return {"stdout": f"BCEXPORT ok {len(data)} {hashlib.sha256(data).hexdigest()}\n"}
        if command.startswith("rm -rf -- /workspace/.bananachat-export-"):
            state.cleanups.append(command)
            folder = command.split()[-1]
            for path in [path for path in box.files if path.startswith(folder + "/")]:
                del box.files[path]
            return {"stdout": ""}
        return {"stdout": "ok\n"}

    runner.exec_handler = handler
    return state


@pytest.fixture
def git_env(env, net):
    with env.browser.client.session_transaction() as session:
        session["language"] = "en"
    set_settings(env.app, git_enabled=True)
    net.server.archive("/octo/demo/tar.gz/main", DEMO)
    return env


def test_a_task_starts_from_a_repository_and_exports_a_patch(git_env, runner, fake_ollama):
    from bananachat import db

    env = git_env
    patch = (b"diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n # Demo\n+More\n"
             + b"x" * (2 * 1024 * 1024) + b"\xff\xfe binary-ish tail\n")
    state = git_runner(runner, patch=patch)
    fake_ollama.tool_script = [call("list_files"), call("finish", summary="Looked at the repository.")]
    task_id = start_ok(env.browser, "Improve the README", repo_url="https://github.com/octo/demo", repo_ref="main")
    row = wait_status(env.app, task_id, "finished")
    assert row["summary"] == "Looked at the repository."
    box = runner.only()
    assert box.files["/workspace/demo/README.md"] == b"# Demo\n"
    assert box.files["/workspace/demo/src/app.py"] == b"print('hi')\n"
    archive_puts = [path for method, path, _ in runner.requests if method == "PUT" and "/archive" in path]
    assert len(archive_puts) == 1 and re.search(r"\?path=/workspace/\.bananachat-import-[0-9a-f]{16}$",
                                                archive_puts[0])
    assert len(state.imports) == 1 and "Imported octo/demo@main" in state.imports[0]
    log = steps(env.app, task_id)
    notices = [json.loads(step["content"]) for step in log if step["kind"] == "notice"]
    assert [notice["key"] for notice in notices][:2] == ["agents.notice_importing", "agents.notice_imported"]
    assert notices[1]["params"]["commit"] == COMMIT and notices[1]["params"]["dir"] == "/workspace/demo"
    # The agent is told where the repository is; importing cost no model call and no credit.
    bodies = [body for body in fake_ollama.chat_bodies() if body.get("tools")]
    assert "/workspace/demo" in bodies[0]["messages"][0]["content"]
    with env.app.app_context():
        assert db.scalar("SELECT COUNT(*) FROM credit_ledger WHERE user_id=?", (env.user["id"],)) == len(bodies)
        assert db.scalar("SELECT COUNT(*) FROM rate_limit_hits WHERE key=?", (f"agents:import:{env.user['id']}",)) == 1
    # The pending import and its download are gone from the web server.
    instance = env.app.config["BC"].instance_dir
    assert not (instance / "agent-uploads" / task_id).exists()

    events = env.browser.get(f"/agents/{task_id}/events").get_json()
    assert events["task"]["repository"] == {"label": "octo/demo@main", "dir": "/workspace/demo", "git": True}
    page = env.browser.get(f"/agents/{task_id}").get_data(as_text=True)
    assert "octo/demo@main" in page and 'id="patch-link"' in page

    response = env.browser.get(f"/agents/{task_id}/workspace/patch")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.data == patch
    assert response.headers["Content-Type"] == "text/x-diff"
    assert response.headers["Content-Disposition"] == 'attachment; filename="demo-changes.patch"'
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response.headers["Content-Security-Policy"]
    command = state.exports[0]
    assert "/workspace/demo" in command and COMMIT in command and "GIT_CONFIG_GLOBAL=/dev/null" in command
    assert len(state.cleanups) == 1 and not any(".bananachat-export-" in path for path in box.files)
    # Follow-ups do not import again, and the agent still knows about the repository.
    fake_ollama.tool_script = [call("finish", summary="again")]
    assert env.browser.post_json(f"/agents/{task_id}/messages", {"content": "more"}).status_code == 202
    wait_status(env.app, task_id, "finished")
    assert len(state.imports) == 1
    assert "/workspace/demo" in [body for body in fake_ollama.chat_bodies() if body.get("tools")][-1][
        "messages"][0]["content"]


def test_patch_export_failures_are_reported(git_env, runner, fake_ollama):
    env = git_env
    state = git_runner(runner, patch=b"")
    fake_ollama.tool_script = [call("finish", summary="ok")]
    task_id = start_ok(env.browser, repo_url="github.com/octo/demo", repo_ref="main")
    wait_status(env.app, task_id, "finished")
    url = f"/agents/{task_id}/workspace/patch"

    def failure(expected_status):
        response = env.browser.fetch(url, headers={"X-Requested-With": "fetch"})
        assert response.status_code == expected_status, response.get_data(as_text=True)
        return response.get_json()["error"]

    assert failure(409)["code"] == "no_changes"
    state.export_answer = "BCEXPORT error toolarge 99999999\n"
    assert "20 MB" in failure(413)["message"]
    state.export_answer = "BCEXPORT error nobase\n"
    assert failure(409)["message"].startswith("The imported commit")
    state.export_answer = "garbage\n"
    assert failure(409)["code"] == "export_failed"
    state.export_answer = f"BCEXPORT ok 5 {'0' * 64}\n"  # the parts do not match what the script reported
    assert failure(502)["code"] == "export_failed"
    # Someone else's task, a task without a repository, and a person whose access was withdrawn.
    add_user(env.app, "mallory", allowed=True)
    mallory = Browser(env.app)
    mallory.login("mallory")
    assert mallory.get(url).status_code == 404
    fake_ollama.tool_script = [call("finish", summary="plain")]
    plain = start_ok(env.browser, "No repository")
    wait_status(env.app, plain, "finished")
    assert env.browser.fetch(f"/agents/{plain}/workspace/patch",
                             headers={"X-Requested-With": "fetch"}).get_json()["error"]["code"] == "no_repository"


def test_images_without_git_import_but_do_not_export(git_env, runner, fake_ollama):
    env = git_env
    git_runner(runner, git=False)
    fake_ollama.tool_script = [call("finish", summary="ok")]
    task_id = start_ok(env.browser, repo_url="https://github.com/octo/demo", repo_ref="main")
    wait_status(env.app, task_id, "finished")
    notices = [json.loads(step["content"])["key"] for step in steps(env.app, task_id) if step["kind"] == "notice"]
    assert "agents.notice_imported_nogit" in notices
    assert env.browser.get(f"/agents/{task_id}/events").get_json()["task"]["repository"]["git"] is False
    response = env.browser.fetch(f"/agents/{task_id}/workspace/patch", headers={"X-Requested-With": "fetch"})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "no_git"
    admin = Browser(env.app)
    admin.login("admin", "admin-password")
    assert "The sandbox image has no Git" in admin.get("/admin/agents").get_data(as_text=True)


def test_failed_imports_end_the_task_before_any_model_call(git_env, runner, net, fake_ollama):
    env = git_env
    git_runner(runner)
    cases = [("/octo/missing/tar.gz/HEAD", (404, {}, b"no"), "https://github.com/octo/missing", "",
              "agents.import_not_found"),
             ("/octo/big/tar.gz/HEAD", (200, {"Content-Type": "application/gzip"}, b"\x1f\x8b" + b"0" * 2_000_000),
              "https://github.com/octo/big", "", "agents.import_too_large")]
    set_settings(env.app, git_max_mb=1)
    for path, route, url, ref, key in cases:
        net.server.routes[path] = route
        task_id = start_ok(env.browser, repo_url=url, repo_ref=ref)
        row = wait_status(env.app, task_id, "failed")
        assert error_key(env.app, row["error"]) == key
    net.dns["codeload.github.com"] = ["127.0.0.1"]
    task_id = start_ok(env.browser, repo_url="https://github.com/octo/demo", repo_ref="main")
    row = wait_status(env.app, task_id, "failed")
    assert error_key(env.app, row["error"]) == "agents.import_blocked_address"
    page = env.browser.get(f"/agents/{task_id}/events").get_json()
    assert "private or reserved network address" in page["task"]["error"]
    assert [body for body in fake_ollama.chat_bodies() if body.get("tools")] == []


def test_the_new_task_form_validates_repositories(git_env, runner, fake_ollama):
    env = git_env
    git_runner(runner)

    def refused(status, code, **fields):
        response = start(env.browser, **fields)
        assert response.status_code == status, response.get_data(as_text=True)
        assert response.get_json()["error"]["code"] == code
        return response.get_json()["error"]["message"]

    assert "https://" in refused(400, "bad_repository", repo_url="http://github.com/octo/demo")
    assert "evil.example" in refused(400, "bad_repository", repo_url="https://evil.example/octo/demo")
    refused(400, "bad_repository", repo_url="https://github.com/octo/demo", repo_ref="../../etc")
    refused(400, "bad_request", repo_ref="main")
    set_settings(env.app, git_imports_per_hour=1)
    fake_ollama.tool_script = [call("finish", summary="ok")]
    task_id = start_ok(env.browser, repo_url="https://github.com/octo/demo", repo_ref="main")
    wait_status(env.app, task_id, "finished")
    refused(429, "rate_limited", repo_url="https://github.com/octo/demo", repo_ref="main")
    fake_ollama.tool_script = [call("finish", summary="ok")]
    wait_status(env.app, start_ok(env.browser, "Without a repository"), "finished")  # other tasks still start
    set_settings(env.app, git_enabled=False)
    refused(403, "import_disabled", repo_url="https://github.com/octo/demo")


# ----- administration and pages -----------------------------------------------------------------------------

def test_admin_git_settings_image_check_and_audit(env, runner):
    from bananachat import db
    from bananachat.services.agents import settings as agent_settings

    admin = Browser(env.app)
    admin.login("admin", "admin-password")
    page = admin.get("/admin/agents").get_data(as_text=True)
    assert "Git repositories" in page and "not checked" in page and 'name="git_hosts"' in page
    form = {"git_enabled": "1", "git_hosts": "github.com\ngitlab:git.example.org\n", "git_max_mb": "20",
            "git_imports_per_hour": "3"}
    assert admin.post("/admin/agents/git", form).status_code == 302
    with env.app.app_context():
        saved = agent_settings.current(fresh=True)
    assert saved.git_enabled and saved.git_hosts == ("github.com", "gitlab:git.example.org")
    assert saved.git_max_mb == 20 and saved.git_imports_per_hour == 3
    assert saved.max_steps == 40  # other settings untouched
    for bad in ({"git_hosts": "example.org"}, {"git_hosts": "http://github.com"}, {"git_max_mb": "51"},
                {"git_imports_per_hour": "0"}, {"git_hosts": "\n".join(f"gitlab:h{n}.example.org" for n in range(21))}):
        assert admin.post("/admin/agents/git", {**form, **bad, "git_enabled": ""}).status_code == 302
        with env.app.app_context():
            assert agent_settings.current(fresh=True).git_enabled is True, bad  # refused, unchanged

    runner.exec_handler = lambda box, command, timeout: {"stdout": "git version 2.39.5\n"}
    assert admin.post("/admin/agents/image-check").status_code == 302
    assert runner.sandboxes == {} and len(runner.deleted) == 1  # the probe sandbox is removed at once
    page = admin.get("/admin/agents").get_data(as_text=True)
    assert "git version 2.39.5" in page and "The sandbox image has no Git" not in page
    runner.exec_handler = lambda box, command, timeout: {"stdout": "BCPROBE nogit\n"}
    admin.post("/admin/agents/image-check")
    page = admin.get("/admin/agents").get_data(as_text=True)
    assert "The sandbox image has no Git" in page
    with env.app.app_context():
        actions = [row["action"] for row in db.query("SELECT action FROM audit_log")]
    assert "admin.agents.git" in actions and actions.count("admin.agents.image_check") == 2
    assert env.browser.post("/admin/agents/git", form).status_code == 403


@pytest.mark.parametrize("language", ["en", "it"])
def test_pages_offer_repositories_in_both_languages(env, runner, fake_ollama, language):
    with env.browser.client.session_transaction() as session:
        session["language"] = language
    page = env.browser.get("/agents").get_data(as_text=True)
    assert 'name="repo_url"' not in page  # off by default
    set_settings(env.app, git_enabled=True)
    page = env.browser.get("/agents").get_data(as_text=True)
    assert 'name="repo_url"' in page and 'name="repo_ref"' in page
    assert ("Start from a Git repository" if language == "en" else "Parti da un repository Git") in page
    assert "github.com, gitlab.com, codeberg.org" in page and "style=" not in page and "<script>" not in page
    fake_ollama.tool_script = [call("finish", summary="ok")]
    task_id = start_ok(env.browser)
    wait_status(env.app, task_id, "finished")
    page = env.browser.get(f"/agents/{task_id}").get_data(as_text=True)
    assert ("Download changes (.patch)" if language == "en" else "Scarica le modifiche (.patch)") in page
    response = start(env.browser, repo_url="ftp://github.com/a/b")
    message = response.get_json()["error"]["message"]
    assert message == ("Use the repository's https:// address; other protocols are not supported." if language == "en"
                       else "Usa l'indirizzo https:// del repository; altri protocolli non sono supportati.")

