"""Real threaded worker recycling must drain requests already accepted."""

from concurrent.futures import ThreadPoolExecutor
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn is a POSIX server")
def test_worker_recycling_preserves_concurrent_requests(tmp_path):
    pytest.importorskip("gunicorn")
    (tmp_path / "probe.py").write_text(
        "import os, time\n"
        "def application(environ, start_response):\n"
        "    time.sleep(0.01)\n"
        "    body = (str(os.getpid()) + '|' + environ['PATH_INFO']).encode()\n"
        "    start_response('200 OK', [('Content-Length', str(len(body)))])\n"
        "    return [body]\n"
    )
    configuration = tmp_path / "gunicorn.conf.py"
    configuration.write_text("control_socket_disable = True\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "GUNICORN_CMD_ARGS": ""}
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "--config", str(configuration),
             "--bind", f"127.0.0.1:{port}", "--workers", "2", "--threads", "4",
             "--worker-class", "gthread", "--max-requests", "24", "--max-requests-jitter", "3",
             "--graceful-timeout", "15", "probe:application"],
            cwd=tmp_path, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            def read(index):
                # Deliberately do not retry: a dropped request is a failure.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(base + f"/{index}", timeout=10) as response:
                    body = response.read().decode()
                    assert response.status == 200 and body.endswith(f"|/{index}")
                    return body.split("|", 1)[0]

            deadline = time.monotonic() + 15
            while True:
                try:
                    read("health")
                    break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail("Gunicorn did not become ready; inspect the fixture server.log")
                    time.sleep(0.05)
            with ThreadPoolExecutor(max_workers=32) as pool:
                workers = set(pool.map(read, range(400)))
            assert len(workers) > 4, "The workload must exercise multiple replacement workers"
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
