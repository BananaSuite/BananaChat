"""The sandbox runner against a real Docker engine (skipped when none is available).

These tests start the runner exactly as production does (``create_server``
with its start-up checks) and prove the isolation from inside real containers.
They need ``docker`` and the image ``mirror.gcr.io/library/python:3.12-slim``
(pulled beforehand: the runner never pulls). Set ``BC_SANDBOX_REQUIRE_DOCKER=1``
to make a missing engine a failure instead of a skip (the CI job does).
Every container is labelled with a per-run instance name and removed afterwards.
"""

import gzip
import http.client
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import zipfile

import pytest

from compute import sandbox_runner as sr

IMAGE = sr.DEFAULT_IMAGE
DOCKER = shutil.which("docker")
REQUIRED = os.environ.get("BC_SANDBOX_REQUIRE_DOCKER") == "1"
INSTANCE = "pytest-" + secrets.token_hex(4)


def _docker_ready() -> str:
    if not DOCKER:
        return "docker is not installed"
    try:
        info = subprocess.run([DOCKER, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=30)
        if info.returncode != 0:
            return "the Docker daemon is not reachable"
        image = subprocess.run([DOCKER, "image", "inspect", IMAGE], capture_output=True, timeout=30)
        if image.returncode != 0:
            return f"{IMAGE} is not pulled"
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"docker failed: {error}"
    return ""


REASON = _docker_ready()
if REASON and not REQUIRED:
    pytest.skip(f"Docker integration tests skipped: {REASON}", allow_module_level=True)


def _cleanup():
    if not DOCKER:
        return
    listed = subprocess.run([DOCKER, "ps", "-aq", "--filter", f"label={sr.LABEL_RUNNER}={INSTANCE}"],
                            capture_output=True, text=True, timeout=60)
    ids = listed.stdout.split()
    if ids:
        subprocess.run([DOCKER, "rm", "-f", *ids], capture_output=True, timeout=120)


def _containers() -> list:
    listed = subprocess.run([DOCKER, "ps", "-a", "--filter", f"label={sr.LABEL_RUNNER}={INSTANCE}", "--format",
                             "{{.Names}}"], capture_output=True, text=True, timeout=60)
    return listed.stdout.split()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    if REASON:
        pytest.fail(f"BC_SANDBOX_REQUIRE_DOCKER=1 but {REASON}")
    token_file = tmp_path_factory.mktemp("runner") / "token"
    environ = {
        "BC_SANDBOX_TOKEN_FILE": str(token_file),
        "BC_SANDBOX_PORT": str(_free_port()),
        "BC_SANDBOX_ENGINE": "docker",
        "BC_SANDBOX_IMAGES": IMAGE,
        # The daemon here is usually rootful; the test opts in explicitly, as an operator would have to.
        "BC_SANDBOX_ALLOW_ROOTFUL": "1",
        "BC_SANDBOX_INSTANCE": INSTANCE,
        "BC_SANDBOX_MAX": "3",
        "BC_SANDBOX_MEMORY_MB": "256",
        "BC_SANDBOX_PIDS": "64",
        "BC_SANDBOX_WORKSPACE_MB": "64",
        "BC_SANDBOX_EXEC_TIMEOUT": "30",
        "BC_SANDBOX_OUTPUT_KB": "16",
    }
    # A labelled leftover from a "previous run" is removed at start-up.
    subprocess.run([DOCKER, "run", "-d", "--rm", "--label", f"{sr.LABEL}=1", "--label", f"{sr.LABEL_RUNNER}={INSTANCE}",
                    "--network", "none", "--entrypoint", "sleep", IMAGE, "300"], capture_output=True, timeout=60,
                   check=True)
    assert len(_containers()) == 1
    server = sr.create_server(environ)
    assert _containers() == []
    server.token_value = token_file.read_text().strip()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.manager.shutdown()
        _cleanup()
        assert _containers() == []


def call(server, method, path, body=None, raw=None, token=None, timeout=90):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    headers = {"Authorization": f"Bearer {token or server.token_value}"} if token != "" else {}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    if response.getheader("Content-Type") == "application/json" and payload:
        payload = json.loads(payload)
    return response.status, payload


@pytest.fixture
def sandbox(runner):
    status, payload = call(runner, "POST", "/v1/sandboxes", {"session": "pytest"})
    assert status == 201, payload
    yield payload["id"]
    call(runner, "DELETE", f"/v1/sandboxes/{payload['id']}")


def run(runner, sandbox, command, timeout=20, **extra):
    status, payload = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec",
                           dict(command=command, timeout=timeout, **extra))
    assert status == 200, payload
    return payload


def test_health_and_auth(runner):
    status, payload = call(runner, "GET", "/healthz", token="")
    assert status == 200 and payload == {"ok": True, "engine": "docker"}
    status, payload = call(runner, "GET", "/healthz")
    assert payload["network"] == "none" and payload["images"] == [IMAGE] and payload["max"] == 3
    assert call(runner, "GET", "/v1/sandboxes", token="")[0] == 401
    assert call(runner, "GET", "/v1/sandboxes", token="w" * 64)[0] == 401
    assert call(runner, "POST", "/v1/sandboxes", {"session": "x"}, token="w" * 64)[0] == 401


def test_non_root_without_capabilities_and_read_only_root(runner, sandbox):
    result = run(runner, sandbox, "id -u; id -g; grep -E '^(CapEff|CapPrm|CapBnd|NoNewPrivs|Seccomp):' "
                                  "/proc/self/status; awk '$2==\"/\"{print $4}' /proc/mounts | cut -d, -f1; "
                                  "stat -c '%u:%g:%a' /workspace; echo $HOME; pwd")
    lines = result["stdout"].split("\n")
    assert lines[0:2] == ["1000", "1000"]
    status = dict(line.split(":\t") for line in lines[2:7])
    assert status["CapEff"] == status["CapPrm"] == status["CapBnd"] == "0000000000000000"
    assert status["NoNewPrivs"] == "1" and status["Seccomp"] == "2"
    assert lines[7:11] == ["ro", "1000:1000:700", "/workspace", "/workspace"]
    result = run(runner, sandbox, "for p in / /etc /usr/local/bin /root /var; do touch $p/x 2>/dev/null "
                                  "&& echo WROTE $p; done; touch /workspace/ok /tmp/ok && echo writable")
    assert result["stdout"].strip() == "writable"
    result = run(runner, sandbox, "cp /bin/true /workspace/t && /workspace/t && echo exec-ok; "
                                  "cp /bin/true /tmp/t && /tmp/t || echo tmp-noexec")
    assert result["stdout"] == "exec-ok\ntmp-noexec\n"
    # No host mounts or engine sockets leak in.
    result = run(runner, sandbox, "ls /var/run/docker.sock /run/podman 2>&1; grep -cE ' (/home|/root|/srv) ' "
                                  "/proc/mounts")
    assert "No such file" in result["stdout"] and result["stdout"].strip().endswith("0")


def test_there_is_no_network(runner, sandbox):
    result = run(runner, sandbox, "ls /sys/class/net; python3 - <<'EOF'\n"
                                  "import socket\n"
                                  "for target in [('1.1.1.1', 53), ('8.8.8.8', 443), ('172.17.0.1', 22)]:\n"
                                  "    try:\n"
                                  "        socket.create_connection(target, timeout=2); print('CONNECTED', target)\n"
                                  "    except OSError as error: print('refused', error.errno)\n"
                                  "try:\n"
                                  "    socket.getaddrinfo('example.com', 80); print('RESOLVED')\n"
                                  "except OSError: print('no dns')\n"
                                  "EOF")
    assert result["stdout"].split("\n")[0] == "lo"
    assert "CONNECTED" not in result["stdout"] and "RESOLVED" not in result["stdout"]
    assert result["stdout"].count("refused") == 3


def test_memory_limit_kills_the_hog_but_not_the_sandbox(runner, sandbox):
    result = run(runner, sandbox, "python3 -c 'b = bytearray(600 * 1024 * 1024); print(len(b))'", timeout=30)
    assert result["exit_code"] == 137 and "629145600" not in result["stdout"]
    assert run(runner, sandbox, "echo alive")["stdout"] == "alive\n"


def test_pids_limit_and_fork_bombs(runner, sandbox):
    result = run(runner, sandbox, "for i in $(seq 1 100); do sleep 30 & done 2>&1 | tail -1; echo done",
                 timeout=5)
    assert result["timed_out"] or "done" in result["stdout"]
    text = result["stdout"] + result["stderr"]
    assert "fork" in text.lower() or "resource temporarily unavailable" in text.lower() or result["timed_out"]
    started = time.monotonic()
    status, bomb = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec",
                        {"command": "bomb() { bomb | bomb & }; bomb; sleep 60", "timeout": 3})
    assert time.monotonic() - started < 40
    if status == 200:
        assert bomb["timed_out"] or bomb["truncated"]  # stopped by the timeout or the output flood guard
    else:
        assert status == 410
    # The runner stays healthy; the sandbox is either clean again or was removed.
    assert call(runner, "GET", "/healthz")[1]["ok"]
    status, payload = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec",
                           {"command": "ls -d /proc/[0-9]* | wc -l", "timeout": 10})
    if status == 200:
        assert int(payload["stdout"]) <= 6, payload
    else:
        assert status in (404, 410)


def test_timeouts_kill_the_command_and_everything_it_started(runner, sandbox):
    started = time.monotonic()
    result = run(runner, sandbox, "setsid sh -c 'sleep 1000' & nohup sleep 1001 >/dev/null 2>&1 & "
                                  "(sleep 1002 &) ; sleep 1003", timeout=2)
    elapsed = time.monotonic() - started
    assert result["timed_out"] and result["exit_code"] == 124 and 2 <= elapsed < 20
    assert "timed out" in result["stderr"] and not result["sandbox_removed"]
    listing = run(runner, sandbox, "for d in /proc/[0-9]*; do cat $d/cmdline 2>/dev/null | tr '\\0' ' '; echo; "
                                   "done")["stdout"]
    assert "1000" not in listing and "1001" not in listing and "1002" not in listing and "1003" not in listing
    assert "sleep infinity" in listing  # the idle main process survives
    assert run(runner, sandbox, "echo still usable")["stdout"] == "still usable\n"


def test_stdin_is_closed_and_cwd_is_honoured(runner, sandbox):
    result = run(runner, sandbox, "cat; mkdir -p sub/dir && cd sub && pwd", timeout=10)
    assert result["exit_code"] == 0 and result["stdout"] == "/workspace/sub\n"
    assert run(runner, sandbox, "pwd", cwd="/workspace/sub/dir")["stdout"] == "/workspace/sub/dir\n"
    result = run(runner, sandbox, "pwd", cwd="/workspace/missing")
    assert result["exit_code"] == 126 and "cannot enter" in result["stderr"]


def test_output_is_truncated_and_floods_are_stopped(runner, sandbox):
    result = run(runner, sandbox, "head -c 100000 /dev/zero | tr '\\0' a; echo err >&2")
    assert result["truncated"] and result["stdout"].startswith("a" * 16384)
    assert "[output truncated: 83616 more bytes]" in result["stdout"] and result["stderr"] == "err\n"
    started = time.monotonic()
    result = run(runner, sandbox, "yes", timeout=30)
    assert result["truncated"] and "too much output" in result["stderr"] and not result["timed_out"]
    assert time.monotonic() - started < 25
    assert run(runner, sandbox, "echo ok")["stdout"] == "ok\n"


def test_files_stay_inside_the_workspace(runner, sandbox):
    base = f"/v1/sandboxes/{sandbox}/files"
    status, payload = call(runner, "PUT", base + "?path=deep/er/file.txt", raw="héllo\n".encode())
    assert status == 200 and payload == {"path": "/workspace/deep/er/file.txt", "size": 7}
    status, payload = call(runner, "GET", base + "?path=/workspace/deep/er/file.txt")
    assert payload["content"] == "héllo\n" and payload["encoding"] == "utf-8" and not payload["truncated"]
    call(runner, "PUT", base + "?path=bin.dat", raw=bytes(range(256)))
    status, payload = call(runner, "GET", base + "?path=bin.dat")
    assert payload["encoding"] == "base64" and payload["size"] == 256
    status, payload = call(runner, "GET", base + "?path=/workspace")
    assert {entry["name"]: entry["type"] for entry in payload["entries"]} == {"deep": "dir", "bin.dat": "file"}
    for path in ("../etc/passwd", "/etc/passwd", "/workspace/../etc/shadow", "%2e%2e/etc/passwd", "a%00b"):
        assert call(runner, "GET", f"{base}?path={path}")[0] == 400, path
    # Symlinks made inside the container cannot lead file operations out of /workspace.
    run(runner, sandbox, "ln -s /etc etc-link; ln -s /tmp/outside out-file; ln -s deep inside-link")
    status, payload = call(runner, "GET", base + "?path=etc-link/passwd")
    assert status == 403 and payload["error"]["code"] == "outside_workspace"
    assert call(runner, "PUT", base + "?path=etc-link/evil", raw=b"x")[0] == 403
    assert call(runner, "PUT", base + "?path=out-file", raw=b"x")[0] == 403
    assert run(runner, sandbox, "ls /tmp/outside 2>&1")["exit_code"] != 0
    assert call(runner, "GET", base + "?path=inside-link/er/file.txt")[1]["content"] == "héllo\n"
    # Special files are refused rather than read (a FIFO would block).
    run(runner, sandbox, "mkfifo pipe")
    assert call(runner, "GET", base + "?path=pipe")[0] == 400
    assert call(runner, "PUT", base + "?path=pipe", raw=b"x")[0] == 400
    assert call(runner, "GET", base + "?path=nope")[0] == 404
    # Huge (sparse) files are read partially.
    run(runner, sandbox, "truncate -s 5G sparse")
    status, payload = call(runner, "GET", base + "?path=sparse")
    assert status == 200 and payload["truncated"] and payload["size"] == 5 * 1024 ** 3
    assert len(payload["content"]) == sr.MAX_READ_BYTES


def test_the_workspace_size_is_capped(runner, sandbox):
    result = run(runner, sandbox, "head -c 100M /dev/zero > big; echo $?; df -m /workspace | tail -1")
    assert "No space left" in result["stderr"] and result["stdout"].startswith("1\n")
    status, payload = call(runner, "PUT", f"/v1/sandboxes/{sandbox}/files?path=more.bin", raw=b"x" * 1_000_000)
    assert status == 507 and payload["error"]["code"] == "write_failed"


def _tar_gz(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data, mode, kind in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.type = kind
                info.linkname = data
                tar.addfile(info)
    return buffer.getvalue()


def test_archive_round_trip(runner, sandbox, monkeypatch):
    upload = _tar_gz([("project/run.sh", b"#!/bin/sh\necho ran $(cat data.txt)\n", 0o4755, "file"),
                      ("project/data.txt", b"payload", 0o644, "file"),
                      ("project/escape", "/etc/passwd", 0o777, tarfile.SYMTYPE)])
    status, payload = call(runner, "PUT", f"/v1/sandboxes/{sandbox}/archive", raw=upload)
    assert status == 200 and payload == {"path": "/workspace", "files": 2, "directories": 0, "skipped": 1,
                                         "size": 42}
    result = run(runner, sandbox, "cd project && ./run.sh && stat -c '%a %u %n' run.sh data.txt && ls")
    assert result["stdout"] == "ran payload\n755 1000 run.sh\n644 1000 data.txt\ndata.txt\nrun.sh\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("notes/readme.md", "# zipped\n")
    status, payload = call(runner, "PUT", f"/v1/sandboxes/{sandbox}/archive?path=/workspace/unzipped",
                           raw=buffer.getvalue())
    assert status == 200 and payload["files"] == 1
    assert call(runner, "GET", f"/v1/sandboxes/{sandbox}/files?path=unzipped/notes/readme.md")[1]["content"] == \
        "# zipped\n"
    # Malicious archives change nothing.
    for bad in (_tar_gz([("../../tmp/evil", b"x", 0o644, "file")]),
                _tar_gz([("ok-first", b"x", 0o644, "file"), ("/etc/evil", b"x", 0o644, "file")]),
                _tar_gz([("bomb", b"\0" * (80 << 20), 0o644, "file")]),  # 80 MB > the 64 MB workspace
                gzip.compress(b"not a tar file" * 1000)):
        status, payload = call(runner, "PUT", f"/v1/sandboxes/{sandbox}/archive", raw=bad)
        assert status in (400, 415), payload
    assert run(runner, sandbox, "ls /tmp; ls ok-first 2>&1")["stdout"].strip().endswith("No such file or directory")
    # Download and compare.
    status, data = call(runner, "GET", f"/v1/sandboxes/{sandbox}/archive")
    assert status == 200
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
        assert {"./project/run.sh", "./project/data.txt", "./unzipped/notes/readme.md"} <= names
        assert tar.extractfile("./project/data.txt").read() == b"payload"
    monkeypatch.setattr(sr, "MAX_ARCHIVE_DOWNLOAD", 1_000_000)
    run(runner, sandbox, "head -c 3000000 /dev/urandom > random.bin")
    status, payload = call(runner, "GET", f"/v1/sandboxes/{sandbox}/archive")
    assert status == 413 and payload["error"]["code"] == "too_large"


def test_interrupt_stops_a_running_command(runner, sandbox):
    results = []
    worker = threading.Thread(target=lambda: results.append(run(runner, sandbox, "sleep 25", timeout=30)))
    started = time.monotonic()
    worker.start()
    time.sleep(1.5)
    status, busy = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec", {"command": "true"})
    assert status == 409 and busy["error"]["code"] == "busy"
    assert call(runner, "POST", f"/v1/sandboxes/{sandbox}/interrupt", {})[1]["ok"]
    worker.join(20)
    assert results and results[0]["interrupted"] and time.monotonic() - started < 15
    assert run(runner, sandbox, "echo after")["stdout"] == "after\n"


def test_killing_everything_from_inside_ends_the_sandbox(runner):
    status, payload = call(runner, "POST", "/v1/sandboxes", {"session": "suicide"})
    sandbox = payload["id"]
    try:
        # `kill -1` spares the shell that sends it, so the command itself may still succeed; the
        # sandbox is gone either way and the next operation says so.
        status, payload = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec",
                               {"command": "kill -9 -1", "timeout": 10})
        assert status in (200, 410)
        if status == 200:
            status, payload = call(runner, "POST", f"/v1/sandboxes/{sandbox}/exec", {"command": "true"})
        assert status == 410 and payload["error"]["code"] == "sandbox_gone"
        assert call(runner, "GET", f"/v1/sandboxes/{sandbox}")[0] == 410  # removed ids answer 410, not 404
        assert sr.NAME_PREFIX + sandbox not in _containers()
    finally:
        call(runner, "DELETE", f"/v1/sandboxes/{sandbox}")


def test_capacity_and_the_reaper(runner):
    created = []
    for index in range(3):
        status, payload = call(runner, "POST", "/v1/sandboxes", {"session": f"cap-{index}", "memory_mb": 128})
        assert status == 201, payload
        created.append(payload["id"])
    status, payload = call(runner, "POST", "/v1/sandboxes", {"session": "one-too-many"})
    assert status == 429 and payload["error"]["code"] == "capacity"
    manager = runner.manager
    old_ttl = manager.config.idle_ttl
    try:
        run(runner, created[0], "true")
        manager.config.idle_ttl = 4
        time.sleep(2.5)
        run(runner, created[0], "true")  # recently used: kept
        time.sleep(2.5)
        manager.reap()  # (the background reaper may have run too; the outcome is the same)
        assert [item["id"] for item in manager.list()] == [created[0]]
        assert sorted(_containers()) == [sr.NAME_PREFIX + created[0]]
    finally:
        manager.config.idle_ttl = old_ttl
    # A labelled container the runner does not know is removed by the reaper.
    subprocess.run([DOCKER, "run", "-d", "--label", f"{sr.LABEL}=1", "--label", f"{sr.LABEL_RUNNER}={INSTANCE}",
                    "--network", "none", "--entrypoint", "sleep", IMAGE, "300"], capture_output=True, timeout=60,
                   check=True)
    assert len(_containers()) == 2
    manager.reap()
    assert _containers() == [sr.NAME_PREFIX + created[0]]
    assert call(runner, "DELETE", f"/v1/sandboxes/{created[0]}")[0] == 204
    assert _containers() == []
