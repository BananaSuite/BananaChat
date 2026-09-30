"""Third security review of the sandbox runner: regressions for the findings (fake engine, plus Docker when present)."""

import http.client
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from compute import sandbox_runner as sr
from tests.test_sandbox_runner import TOKEN, FakeEngine, create, make_config, start

# ----- a removed sandbox answers 410, not 404 ---------------------------------------------------------------


@pytest.fixture
def manager():
    manager = sr.SandboxManager(make_config(max_sandboxes=3), FakeEngine())
    manager.preflight()
    return manager


def test_a_sandbox_removed_by_the_runner_keeps_answering_410(manager):
    """CI flake: a command that could not be killed removed its sandbox, and the next exec answered 404.

    Callers must be able to tell "this sandbox died" (410) from "no such id" (404) deterministically.
    """
    engine = manager.engine
    box = manager.create({"session": "s"})
    engine.exec_handler = lambda argv, stdin: sr.RunResult(None, timed_out=True)
    engine.kill_ok = False
    assert manager.execute(box.id, {"command": "bomb", "timeout": 1})["sandbox_removed"]
    for operation in (lambda: manager.execute(box.id, {"command": "ls"}), lambda: manager.get(box.id),
                      lambda: manager.interrupt(box.id), lambda: manager.read_path(box.id, "/workspace"),
                      lambda: manager.write_file(box.id, "a", b"x"), lambda: manager.download_archive(box.id)):
        with pytest.raises(sr.ApiError) as caught:
            operation()
        assert (caught.value.status, caught.value.code) == (410, "sandbox_gone")
    manager.delete(box.id)  # deleting it again is still fine
    # Other ways of losing a sandbox are remembered too; unknown ids stay 404.
    other = manager.create({"session": "t"})
    manager.delete(other.id)
    with pytest.raises(sr.ApiError) as caught:
        manager.get(other.id)
    assert caught.value.status == 410
    with pytest.raises(sr.ApiError) as caught:
        manager.get("0" * 32)
    assert caught.value.status == 404


def test_the_memory_of_removed_sandboxes_is_bounded(manager, monkeypatch):
    monkeypatch.setattr(sr, "REMEMBER_REMOVED", 3)
    ids = []
    for _ in range(5):
        box = manager.create({"session": "s"})
        manager.delete(box.id)
        ids.append(box.id)
    assert len(manager._removed) == 3
    with pytest.raises(sr.ApiError) as caught:
        manager.get(ids[0])
    assert caught.value.status == 404  # forgotten
    with pytest.raises(sr.ApiError) as caught:
        manager.get(ids[-1])
    assert caught.value.status == 410


# ----- archive transfers cannot starve the agents' file writes --------------------------------------------


def _call(server, method, path, raw=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    connection.request(method, path, body=raw, headers={"Authorization": f"Bearer {TOKEN}"})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, json.loads(payload) if payload else None


def test_slow_archive_downloads_do_not_block_file_writes(manager):
    """Two slowly read archive downloads hold both archive slots; every agent's write_file must still work."""
    server = sr.RunnerServer(("127.0.0.1", 0), token=TOKEN, manager=manager)
    start(server)
    try:
        box = create(server)
        manager.engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"%d\n" % len(stdin or b""))
        for _ in range(sr.LARGE_TRANSFERS):
            server.transfers.acquire()
        try:
            status, payload = _call(server, "PUT", f"/v1/sandboxes/{box['id']}/files?path=a.txt", raw=b"hello")
            assert status == 200, payload
            status, payload = _call(server, "GET", f"/v1/sandboxes/{box['id']}/archive")
            assert status == 503 and payload["error"]["code"] == "busy"  # archives stay bounded
        finally:
            for _ in range(sr.LARGE_TRANSFERS):
                server.transfers.release()
        # File writes have their own (bounded) pool.
        for _ in range(sr.FILE_TRANSFERS):
            server.file_transfers.acquire()
        try:
            status, payload = _call(server, "PUT", f"/v1/sandboxes/{box['id']}/files?path=b.txt", raw=b"x")
            assert status == 503 and payload["error"]["code"] == "busy"
        finally:
            for _ in range(sr.FILE_TRANSFERS):
                server.file_transfers.release()
    finally:
        server.shutdown()
        server.server_close()


# ----- the unauthenticated health check cannot fan out engine calls ---------------------------------------


def test_concurrent_health_checks_share_one_engine_call(manager):
    engine = manager.engine
    real_answer = engine.answer
    calls = []

    def slow(args, stdin, timeout):
        if args[0] == "ps":
            calls.append(time.monotonic())
            time.sleep(0.3)
        return real_answer(args, stdin, timeout)
    engine.answer = slow
    manager._health["at"] = -1e9
    results = []
    threads = [threading.Thread(target=lambda: results.append(manager.healthy())) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == [True] * 12
    assert len(calls) == 1


# ----- Docker: interrupt kills processes that outlived the command that started them ----------------------

DOCKER = shutil.which("docker")


def _docker_problem() -> str:
    if not DOCKER:
        return "docker is not installed"
    try:
        if subprocess.run([DOCKER, "image", "inspect", sr.DEFAULT_IMAGE], capture_output=True,
                          timeout=30).returncode != 0:
            return "the image is missing or the daemon is unreachable"
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    return ""


@pytest.fixture(scope="module")
def docker_runner(tmp_path_factory):
    problem = _docker_problem()
    if problem:
        if os.environ.get("BC_SANDBOX_REQUIRE_DOCKER") == "1":
            pytest.fail(problem)
        pytest.skip(problem)
    instance = "review3-" + secrets.token_hex(4)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token_file = tmp_path_factory.mktemp("review3") / "token"
    environ = {"BC_SANDBOX_TOKEN_FILE": str(token_file), "BC_SANDBOX_PORT": str(port), "BC_SANDBOX_ENGINE": "docker",
               "BC_SANDBOX_IMAGES": sr.DEFAULT_IMAGE, "BC_SANDBOX_ALLOW_ROOTFUL": "1",
               "BC_SANDBOX_INSTANCE": instance, "BC_SANDBOX_MAX": "2", "BC_SANDBOX_MEMORY_MB": "256",
               "BC_SANDBOX_PIDS": "64", "BC_SANDBOX_WORKSPACE_MB": "64", "BC_SANDBOX_EXEC_TIMEOUT": "30"}
    server = sr.create_server(environ)
    server.token_value = token_file.read_text().strip()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.manager.shutdown()
        listed = subprocess.run([DOCKER, "ps", "-aq", "--filter", f"label={sr.LABEL_RUNNER}={instance}"],
                                capture_output=True, text=True, timeout=60).stdout.split()
        if listed:
            subprocess.run([DOCKER, "rm", "-f", *listed], capture_output=True, timeout=120)


def _docker_call(server, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=90)
    connection.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                       headers={"Authorization": f"Bearer {server.token_value}"})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, json.loads(payload) if payload else None


def test_detached_processes_survive_a_command_until_interrupted(docker_runner):
    """The web server relies on interrupt to stop agent code left running after a run ends (Stop, kill switch)."""
    status, box = _docker_call(docker_runner, "POST", "/v1/sandboxes", {"session": "review3"})
    assert status == 201, box
    path = f"/v1/sandboxes/{box['id']}"
    try:
        status, result = _docker_call(docker_runner, "POST", f"{path}/exec", {
            "command": "nohup sh -c 'while :; do :; done' >/dev/null 2>&1 & setsid sleep 999 >/dev/null 2>&1 & "
                       "echo started", "timeout": 10})
        assert status == 200 and result["stdout"] == "started\n" and not result["timed_out"]
        listing = "for d in /proc/[0-9]*; do tr '\\0' ' ' < $d/cmdline 2>/dev/null; echo; done"
        status, result = _docker_call(docker_runner, "POST", f"{path}/exec", {"command": listing, "timeout": 10})
        assert "while :" in result["stdout"] and "sleep 999" in result["stdout"]  # they outlived their command
        status, result = _docker_call(docker_runner, "POST", f"{path}/interrupt", {})
        assert status == 200 and result == {"ok": True, "sandbox_removed": False}
        status, result = _docker_call(docker_runner, "POST", f"{path}/exec", {"command": listing, "timeout": 10})
        assert "while :" not in result["stdout"] and "sleep 999" not in result["stdout"]
        assert "sleep infinity" in result["stdout"]
    finally:
        _docker_call(docker_runner, "DELETE", path)


# ----- connection slots: one untrusted peer cannot take them all ------------------------------------------


def _hold(server, count):
    held = []
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        sock.sendall(b"GET /v1/sandboxes HTTP/1.1\r\n")  # headers never finished (slowloris)
        held.append(sock)
    time.sleep(0.3)
    return held


def test_one_untrusted_peer_cannot_take_every_connection(manager):
    server = sr.RunnerServer(("127.0.0.1", 0), token=TOKEN, manager=manager, max_connections=8, max_per_peer=2,
                             trusted_peers=(), header_deadline=5)
    start(server)
    held = _hold(server, 2)
    try:
        status, payload = _call(server, "GET", "/v1/sandboxes")
        assert status == 503 and payload["error"]["code"] == "busy"
    finally:
        for sock in held:
            sock.close()
        server.shutdown()
        server.server_close()


def test_trusted_peers_are_not_capped_per_address_and_keep_a_reserve(manager):
    server = sr.RunnerServer(("127.0.0.1", 0), token=TOKEN, manager=manager, max_connections=8, max_per_peer=2,
                             header_deadline=5)
    start(server)
    held = _hold(server, 4)  # loopback (a local proxy or SSH tunnel) is trusted by default
    try:
        assert _call(server, "GET", "/v1/sandboxes")[0] == 200
    finally:
        for sock in held:
            sock.close()
        server.shutdown()
        server.server_close()
    # Untrusted peers together get at most max_connections minus a reserve that only trusted peers may use.
    assert server.untrusted_limit == 6
    admitted = [server.admit(f"203.0.113.{n}") for n in range(10)]
    assert admitted.count(True) == 6
    assert server.admit("127.0.0.1") and server.admit("::ffff:127.0.0.1")
    assert server.admit("203.0.113.200") is False
    for n in range(6):
        server.leave(f"203.0.113.{n}")
    assert server.admit("203.0.113.200")


def test_connection_settings_are_validated():
    config = sr.Config.from_env({"BC_SANDBOX_TOKEN_FILE": "x"})
    assert config.max_per_peer == 4 and config.trusted_peers == ("127.0.0.1/32", "::1/128")
    config = sr.Config.from_env({"BC_SANDBOX_TOKEN_FILE": "x", "BC_SANDBOX_TRUSTED_PEERS": "100.64.0.0/10, 10.0.0.5",
                                 "BC_SANDBOX_MAX_CONNECTIONS_PER_PEER": "2"})
    assert config.trusted_peers == ("100.64.0.0/10", "10.0.0.5/32") and config.max_per_peer == 2
    assert sr.Config.from_env({"BC_SANDBOX_TOKEN_FILE": "x", "BC_SANDBOX_TRUSTED_PEERS": "none"}).trusted_peers == ()
    with pytest.raises(ValueError):
        sr.Config.from_env({"BC_SANDBOX_TOKEN_FILE": "x", "BC_SANDBOX_TRUSTED_PEERS": "example.org"})
    assert sr.HEADER_DEADLINE <= 10


# ----- a stalled archive download gives its slot back quickly ---------------------------------------------


def test_stalled_archive_downloads_release_their_slot(manager, monkeypatch):
    monkeypatch.setattr(sr, "SEND_GRACE", 1.0)
    monkeypatch.setattr(sr, "SEND_IDLE_TIMEOUT", 1.0)
    monkeypatch.setattr(manager, "download_archive", lambda sandbox_id: b"\x1f\x8b" + b"z" * (64 * 1024 * 1024))
    server = sr.RunnerServer(("127.0.0.1", 0), token=TOKEN, manager=manager)
    start(server)
    try:
        box = create(server)
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.sendall(f"GET /v1/sandboxes/{box['id']}/archive HTTP/1.1\r\nHost: x\r\n"
                     f"Authorization: Bearer {TOKEN}\r\n\r\n".encode())
        try:
            time.sleep(0.5)
            deadline = time.monotonic() + 8
            released = False
            while time.monotonic() < deadline and not released:
                taken = [server.transfers.acquire(blocking=False) for _ in range(sr.LARGE_TRANSFERS)]
                released = all(taken)
                for ok in taken:
                    if ok:
                        server.transfers.release()
                time.sleep(0.2)
            assert released, "the stalled download kept its archive slot"
        finally:
            sock.close()
    finally:
        server.shutdown()
        server.server_close()


# ----- file operations inside the container never follow a swapped symlink out --------------------------


def _helper(tmp_path, *args, stdin=b""):
    root = tmp_path / "ws"
    code = sr.SAFE_FILE_PY.replace('ROOT = "/workspace"', f"ROOT = {str(root)!r}")
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, *[a.replace("/workspace", str(root))
                                                                       for a in args]],
                            input=stdin, capture_output=True, timeout=30)
    return result.returncode, result.stdout


def test_the_python_file_helper_resolves_inside_the_workspace_only(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hello")
    outside = tmp_path / "secret"
    outside.write_bytes(b"top secret")
    os.symlink("sub", root / "inner")                    # stays inside: followed
    os.symlink(str(outside), root / "abs_out")           # absolute, outside
    os.symlink("../secret", root / "rel_out")            # relative, outside
    os.symlink(str(root / "sub"), root / "abs_in")       # absolute inside the workspace
    os.symlink("loop", root / "loop")
    os.mkfifo(root / "fifo")
    read = ("read",)
    limits = (str(sr.MAX_READ_BYTES), "10")
    assert _helper(tmp_path, *read, "/workspace/sub/a.txt", *limits) == (0, b"F\t5\nhello")
    assert _helper(tmp_path, *read, "/workspace/inner/a.txt", *limits) == (0, b"F\t5\nhello")
    assert _helper(tmp_path, *read, "/workspace/abs_in/a.txt", *limits)[0] == 0
    code, out = _helper(tmp_path, *read, "/workspace/sub", *limits)
    assert code == 0 and out == b"D\nf\t5\ta.txt\0"
    code, out = _helper(tmp_path, *read, "/workspace", *limits)
    assert code == 0 and out.startswith(b"D\n") and b"l\t" in out and b"p\t0\tfifo\0" in out
    for path in ("/workspace/abs_out", "/workspace/rel_out", "/workspace/inner/../../secret"):
        assert _helper(tmp_path, *read, path, *limits)[0] == 91, path
    assert _helper(tmp_path, *read, "/workspace/missing", *limits)[0] == 92
    assert _helper(tmp_path, *read, "/workspace/sub/a.txt/x", *limits)[0] == 92
    assert _helper(tmp_path, *read, "/workspace/fifo", *limits)[0] == 93
    assert _helper(tmp_path, *read, "/workspace/loop", *limits)[0] == 90
    # Writes: parents are created, symlinks inside are followed, nothing is written outside.
    assert _helper(tmp_path, "write", "/workspace/new/deep/b.txt", stdin=b"data") == (0, b"4\n")
    assert (root / "new" / "deep" / "b.txt").read_bytes() == b"data"
    assert _helper(tmp_path, "write", "/workspace/inner/c.txt", stdin=b"xy") == (0, b"2\n")
    assert (root / "sub" / "c.txt").read_bytes() == b"xy"
    for path in ("/workspace/abs_out", "/workspace/rel_out"):
        assert _helper(tmp_path, "write", path, stdin=b"pwned")[0] == 91
    assert outside.read_bytes() == b"top secret"
    assert _helper(tmp_path, "write", "/workspace/sub", stdin=b"x")[0] == 95
    assert _helper(tmp_path, "write", "/workspace", stdin=b"x")[0] == 95
    assert _helper(tmp_path, "write", "/workspace/fifo", stdin=b"x")[0] == 93
    assert _helper(tmp_path, "write", "/workspace/sub/a.txt/x", stdin=b"x")[0] == 94


def test_file_scripts_prefer_the_helper_and_keep_a_shell_fallback():
    assert "'" not in sr.SAFE_FILE_PY  # it is embedded in single quotes
    for script in (sr.READ_SCRIPT, sr.WRITE_SCRIPT):
        assert script.startswith("if command -v python3 ")
        assert "readlink -m" in script  # images without python3 keep the previous behaviour


def test_swapped_symlinks_never_lead_out_of_the_workspace(docker_runner):
    status, box = _docker_call(docker_runner, "POST", "/v1/sandboxes", {"session": "review3-race"})
    assert status == 201, box
    path = f"/v1/sandboxes/{box['id']}"
    try:
        # A racing agent swaps /workspace/d between a real folder and a symlink to /etc.
        status, result = _docker_call(docker_runner, "POST", f"{path}/exec", {"command": (
            "mkdir -p real && echo inside > real/passwd && "
            "nohup sh -c 'while :; do ln -sfn /etc d.tmp && mv -T d.tmp d; ln -sfn real d.tmp && mv -T d.tmp d; "
            "done' >/dev/null 2>&1 & echo ok"), "timeout": 10})
        assert status == 200 and result["stdout"] == "ok\n", result
        leaks = 0
        seen_inside = 0
        for _ in range(60):
            status, result = _docker_call(docker_runner, "GET", f"{path}/files?path=/workspace/d/passwd")
            if status == 200 and "root:" in result.get("content", ""):
                leaks += 1
            elif status == 200:
                seen_inside += 1
        assert leaks == 0
        # The fallback (no python3 on PATH) still works.
        status, result = _docker_call(docker_runner, "POST", f"{path}/interrupt", {})
        assert status == 200
        fallback = ("PATH=/usr/bin:/bin sh -c " + shlex.quote(sr.READ_SCRIPT) +
                    f" sh /workspace/real/passwd {sr.MAX_READ_BYTES} 10")
        status, result = _docker_call(docker_runner, "POST", f"{path}/exec", {"command": fallback, "timeout": 10})
        assert status == 200 and result["stdout"] == "F\t7\ninside\n", result
    finally:
        _docker_call(docker_runner, "DELETE", path)
