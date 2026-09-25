"""A real Git HTTPS fetch using the stored access token, with no token in its URL."""

import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading

import pytest

from banana_ops.files import atomic_write
from banana_ops.source import GitSource


class PrivateGit(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        credential = 'Basic ' + base64.b64encode(b'bot:fixture-private-token').decode()
        if self.headers.get('Authorization') != credential:
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="private repository"')
            self.end_headers()
            return
        self.server.authorized_requests += 1
        super().do_GET()


@pytest.mark.skipif(not shutil.which('openssl'), reason='OpenSSL is needed to create the local TLS fixture')
def test_git_authenticates_to_private_https_remote_without_persisting_token_in_source(tmp_path):
    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
    checkout = tmp_path / 'project'
    run('git', 'init', '-b', 'main', str(checkout))
    (checkout / 'README').write_text('private source')
    run('git', '-C', str(checkout), 'add', '.')
    run('git', '-C', str(checkout), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Initial')
    expected = run('git', '-C', str(checkout), 'rev-parse', 'HEAD')
    remote = tmp_path / 'private.git'
    run('git', 'clone', '--bare', str(checkout), str(remote))
    run('git', '--git-dir', str(remote), 'update-server-info')
    key, certificate = tmp_path / 'tls.key', tmp_path / 'tls.crt'
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(key), '-out', str(certificate))
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(PrivateGit, directory=str(tmp_path)))
    server.authorized_requests = 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / 'managed'
    atomic_write(root / 'config/repo.token', 'fixture-private-token\n')
    source = {'url': f'https://127.0.0.1:{server.server_port}/private.git', 'branch': 'main', 'auth': 'token', 'username': 'bot'}
    class LocalCA(GitSource):
        def environment(self):
            return {**super().environment(), 'GIT_SSL_CAINFO': str(certificate)}
    try:
        git_source = LocalCA(root, source)
        sha, branch, forward = git_source.resolve(expected)
        assert (sha, branch, forward) == (expected, 'main', True)
        assert server.authorized_requests > 0
        assert 'fixture-private-token' not in source['url']
        assert 'fixture-private-token' not in (root / 'repository.git/config').read_text()
        assert 'fixture-private-token' not in (root / 'config/git-askpass').read_text()
        assert (root / 'config/repo.token').stat().st_mode & 0o777 == 0o600
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
