import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from compute.inference_proxy import ComputeServer


class Ollama(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        self.server.calls.append((self.path, self.headers.get('Authorization')))
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = b'{"message":{"content":"hello"},"done":false}\n{"done":true}\n'
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def compute(tmp_path):
    backend = ThreadingHTTPServer(('127.0.0.1', 0), Ollama)
    backend.calls = []
    proxy = ComputeServer(('127.0.0.1', 0), upstream=f'http://127.0.0.1:{backend.server_port}', token='x' * 64,
                          maintenance=str(tmp_path / 'maintenance'))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, proxy)]
    for thread in threads:
        thread.start()
    yield proxy, backend, tmp_path / 'maintenance'
    for server in (proxy, backend):
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join()


def call(proxy, path, token='', method='GET', body=None):
    connection = http.client.HTTPConnection('127.0.0.1', proxy.server_port, timeout=3)
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def test_compute_requires_real_auth_and_streams_without_forwarding_credential(compute):
    proxy, backend, _ = compute
    assert call(proxy, '/api/chat', method='POST', body={})[0] == 401
    assert not backend.calls
    status, body = call(proxy, '/api/chat', token='x' * 64, method='POST', body={'stream': True})
    assert status == 200 and len(body.splitlines()) == 2
    assert backend.calls == [('/api/chat', None)]


def test_compute_maintenance_blocks_inference_but_health_remains_available(compute):
    proxy, backend, marker = compute
    marker.write_text('updating')
    assert call(proxy, '/api/chat', token='x' * 64, method='POST', body={})[0] == 503
    assert call(proxy, '/healthz')[0] == 200
    assert call(proxy, '/source')[0] == 200
    marker.unlink()
    assert call(proxy, '/api/tags', token='x' * 64)[0] == 200


@pytest.mark.parametrize('path', ['/api/../private', '//external.example/api/chat', '/not-supported', '/api/chat?redirect=elsewhere'])
def test_compute_rejects_arbitrary_paths(compute, path):
    proxy, backend, _ = compute
    assert call(proxy, path, token='x' * 64)[0] == 400
    assert not backend.calls
