"""The update contract with already-installed servers.

An installed server updates itself with the lifecycle code of the *previous*
release. That code starts ``<release>/.venv/bin/gunicorn -c gunicorn.conf.py
wsgi:app`` from the release directory with ``config/app.env`` and waits for
``GET http://127.0.0.1:<port>/health`` to answer 200 while the maintenance file
still exists. These tests run exactly that against a database written by the
previous release.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "legacy_v4.sql"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _managed_environment(tmp_path: Path, port: int, ollama_url: str) -> dict:
    """What the previous release's ``profile.environment`` writes for a single/web install."""
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "logs").mkdir(mode=0o700)
    database = data / "bananachat.db"
    connection = sqlite3.connect(database)
    connection.executescript(FIXTURE.read_text())
    connection.close()
    Path(str(database) + ".initialized").write_text("BananaSuite SQLite initialized\n")
    key = data / ".secret_key"
    key.write_text("0" * 64)
    key.chmod(0o600)
    maintenance = data / ".banana-maintenance"
    maintenance.write_text("Maintenance in progress\n")
    environment = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "HOME")}
    environment.update({
        "BANANA_MAINTENANCE_FILE": str(maintenance), "PYTHONDONTWRITEBYTECODE": "1",
        "BC_HOST": "127.0.0.1", "BC_PORT": str(port), "BC_INSTANCE_DIR": str(data),
        "BC_DATABASE_PATH": str(database), "BC_LOG_FILE": str(data / "logs" / "bananachat.log"),
        "BC_OLLAMA_URL": ollama_url, "BC_PROXY_MODE": "1", "BC_PROXY_HOPS": "1", "BC_SECURE_COOKIES": "1",
        "BC_ENV": "production", "BC_SETUP_TOKEN": "a" * 64, "BC_SOURCE_URL": "https://example.org/source",
    })
    return environment


@pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn is a POSIX server")
def test_updated_release_starts_under_gunicorn_and_passes_the_health_check(tmp_path, fake_ollama):
    pytest.importorskip("gunicorn")
    port = _free_port()
    environment = _managed_environment(tmp_path, port, fake_ollama.url)
    instance_existed = (ROOT / "instance").exists()
    log = (tmp_path / "gunicorn.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"],
                               cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                status, _ = _get(base + "/health")
                if status == 200:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                log.flush()
                pytest.fail("The server did not become healthy:\n" + (tmp_path / "gunicorn.log").read_text()[-4000:])
            time.sleep(0.2)
        # The lifecycle manager checks readiness while maintenance is still on.
        assert _get(base + "/healthz")[0] == 200
        assert _get(base + "/login")[0] == 503
        status, body = _get(base + "/status")
        assert status == 200 and b"updating" in body
        Path(environment["BANANA_MAINTENANCE_FILE"]).unlink()
        status, body = _get(base + "/login")
        assert status == 200 and b"BananaChat" in body and b"BananaAI" not in body
        database = sqlite3.connect(environment["BC_DATABASE_PATH"])
        from bananachat.db import SCHEMA_VERSION
        assert database.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        database.close()
        # Nothing may be written into the (read-only) release directory.
        assert (ROOT / "instance").exists() == instance_existed
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


def test_entry_points_used_by_installed_servers_exist():
    assert (ROOT / "banana").is_file() and (ROOT / "LICENSE").is_file()
    assert (ROOT / "requirements.txt").is_file() and "gunicorn" in (ROOT / "requirements.txt").read_text()
    assert (ROOT / "gunicorn.conf.py").is_file() and (ROOT / "wsgi.py").is_file()
    assert (ROOT / "compute" / "inference_proxy.py").is_file()


def test_compute_proxy_imports_with_the_standard_library_only():
    """Compute installs get no pip packages, so the proxy must not need any."""
    code = ("import sys; sys.modules['flask'] = None; sys.modules['werkzeug'] = None; "
            "import compute.inference_proxy")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
