"""The agent sandbox runner (``python -m compute.sandbox_runner``) against a recording fake engine.

Real-container behaviour (network, read-only root, limits, timeouts...) is
proven in ``test_sandbox_runner_docker.py``.
"""

import base64
import http.client
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

from compute import sandbox_runner as sr

TOKEN = "k" * 64
ROOTLESS_INFO = {"SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless"], "MemoryLimit": True,
                 "PidsLimit": True, "CpuCfsQuota": True, "Runtimes": {"runc": {}, "runsc": {}}}
ROOTFUL_INFO = dict(ROOTLESS_INFO, SecurityOptions=["name=seccomp,profile=builtin"])
FORBIDDEN_FLAGS = ("--privileged", "-v", "--volume", "--mount", "--device", "--cap-add", "--pid", "--userns",
                   "--network=host", "--uts", "--cgroupns", "--volumes-from", "--security-opt=seccomp=unconfined")


class FakeEngine(sr.Engine):
    """Records every engine invocation and imitates Docker's answers."""

    def __init__(self, kind="docker", info=None):
        super().__init__(kind, binary="/fake/" + kind, environ={})
        self.info = ROOTLESS_INFO if info is None else info
        self.calls = []
        self.containers = {}  # name -> {"state", "args"}
        self.images = {sr.DEFAULT_IMAGE}
        self.exec_handler = lambda argv, stdin: sr.RunResult(0)
        self.kill_ok = True
        self.fail_create = False
        self.lock = threading.Lock()

    def check_binary(self):
        pass

    def run(self, args, *, stdin=None, timeout=60, limit=1 << 20, drain_limit=None):
        args = [str(arg) for arg in args]
        captured = None
        if callable(stdin):
            buffer = io.BytesIO()
            try:
                stdin(buffer)
            except sr.ApiError as error:
                return sr.RunResult(1, error=error)
            captured = buffer.getvalue()
        elif stdin is not None:
            captured = bytes(stdin)
        with self.lock:
            self.calls.append((args, captured))
        return self.answer(args, captured, timeout)

    def answer(self, args, stdin, timeout):
        verb = args[0]
        if verb == "info":
            return sr.RunResult(0, json.dumps(self.info).encode())
        if verb == "image":
            return sr.RunResult(0 if args[-1] in self.images else 1, b"sha256:x\n")
        if verb == "ps":
            lines = "".join(f"{name} {item['state']}\n" for name, item in self.containers.items())
            return sr.RunResult(0, lines.encode())
        if verb == "run":
            if self.fail_create:
                return sr.RunResult(125, b"", b"boom")
            name = args[args.index("--name") + 1]
            self.containers[name] = {"state": "running", "args": args}
            return sr.RunResult(0, b"c0ffee\n")
        if verb == "rm":
            self.containers.pop(args[-1], None)
            return sr.RunResult(0)
        if verb == "inspect":
            item = self.containers.get(args[-1])
            if item is None:
                return sr.RunResult(1, b"", b"No such container")
            if "--format" in args:
                return sr.RunResult(0, b"true\n" if item["state"] == "running" else b"false\n")
            return sr.RunResult(0, json.dumps([self.inspect(item["args"])]).encode())
        if verb == "exec":
            name_index = args.index("--env") + 2
            argv = args[name_index + 1:]
            if argv[:3] == ["sh", "-c", sr.FIND_KEEPER]:
                return sr.RunResult(0, b"7\n")
            if argv[:3] == ["sh", "-c", sr.KILL_SCRIPT]:
                return sr.RunResult(0 if self.kill_ok else 1)
            return self.exec_handler(argv, stdin)
        raise AssertionError(f"unexpected engine call {args}")

    @staticmethod
    def inspect(args):
        def value(flag):
            return args[args.index(flag) + 1]
        memory = int(value("--memory").rstrip("m")) * 1024 * 1024
        return {"HostConfig": {"Privileged": "--privileged" in args, "ReadonlyRootfs": "--read-only" in args,
                               "Memory": memory, "PidsLimit": int(value("--pids-limit")),
                               "NetworkMode": value("--network"), "Binds": None, "Devices": [],
                               "CapDrop": [value("--cap-drop")], "CapAdd": None,
                               "SecurityOpt": [value("--security-opt")]},
                "Config": {"User": value("--user")}}

    def commands(self, verb):
        return [args for args, _ in self.calls if args[0] == verb]


def make_config(**overrides):
    options = dict(engine="docker", token_file="unused", instance="unit", max_sandboxes=2, max_execs=4,
                   exec_timeout=30)
    options.update(overrides)
    return sr.Config(**options)


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def manager(engine):
    manager = sr.SandboxManager(make_config(), engine)
    manager.preflight()
    return manager


def start(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def make_server(manager):
    servers = []

    def factory(**options):
        server = sr.RunnerServer(("127.0.0.1", 0), token=TOKEN, manager=options.pop("manager", manager), **options)
        start(server)
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def server(make_server):
    return make_server()


def call(server, method, path, body=None, raw=None, token=TOKEN, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    if response.getheader("Content-Type") == "application/json" and payload:
        payload = json.loads(payload)
    return response.status, payload, response


def raw_request(server, data: bytes, timeout=5) -> bytes:
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=timeout) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def create(server, **body):
    body.setdefault("session", "task-1")
    status, payload, _ = call(server, "POST", "/v1/sandboxes", body)
    assert status == 201, payload
    return payload


# ----- configuration and start-up ---------------------------------------------------------------------------

def test_configuration_defaults_and_validation(tmp_path):
    config = sr.Config.from_env({"BC_SANDBOX_TOKEN_FILE": str(tmp_path / "t")})
    assert (config.host, config.port, config.engine, config.network) == ("127.0.0.1", 11436, "podman", "none")
    assert (config.max_sandboxes, config.memory_mb, config.cpus, config.pids, config.workspace_mb) == (4, 1024, 1.0,
                                                                                                        256, 512)
    assert (config.exec_timeout, config.idle_ttl, config.max_age, config.output_kb) == (300, 1800, 21600, 64)
    assert config.images == (sr.DEFAULT_IMAGE,) and not config.allow_rootful
    with pytest.raises(ValueError, match="BC_SANDBOX_TOKEN_FILE"):
        sr.Config.from_env({})
    base = {"BC_SANDBOX_TOKEN_FILE": "t"}
    bad = [("BC_SANDBOX_ENGINE", "lxc"), ("BC_SANDBOX_NETWORK", "host"), ("BC_SANDBOX_NETWORK", "container:x"),
           ("BC_SANDBOX_RUNTIME", "runsc --privileged"), ("BC_SANDBOX_RUNTIME", "--privileged"),
           ("BC_SANDBOX_IMAGES", "-v/:/host"), ("BC_SANDBOX_IMAGES", "python;reboot"),
           ("BC_SANDBOX_IMAGES", "python:3 --privileged"), ("BC_SANDBOX_ENGINE_BINARY", "docker"),
           ("BC_SANDBOX_MEMORY_MB", "10"), ("BC_SANDBOX_PIDS", "0"), ("BC_SANDBOX_CPUS", "nan"),
           ("BC_SANDBOX_EXEC_TIMEOUT", "99999"), ("BC_SANDBOX_ALLOW_ROOTFUL", "maybe"), ("BC_SANDBOX_MAX", "x"),
           ("BC_SANDBOX_INSTANCE", "Prod Runner")]
    for name, value in bad:
        with pytest.raises(ValueError):
            sr.Config.from_env(dict(base, **{name: value}))
    for options in ({"runtime": "runsc\n"}, {"instance": "x\n"}, {"images": ("python\n",)}):
        with pytest.raises(ValueError):  # a trailing newline must not slip past a pattern
            make_config(**options)
    config = sr.Config.from_env(dict(base, BC_SANDBOX_IMAGES="a/b:1, c:2", BC_SANDBOX_ALLOW_ROOTFUL="1",
                                     BC_SANDBOX_ENGINE="Docker"))
    assert config.images == ("a/b:1", "c:2") and config.allow_rootful and config.engine == "docker"


def test_token_file_is_created_private_and_checked(tmp_path):
    path = tmp_path / "runner.token"
    token = sr.load_or_create_token(path)
    assert len(token) == 64 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sr.load_or_create_token(path) == token  # not regenerated
    path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        sr.load_or_create_token(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular file"):
        sr.read_token_file(link)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        sr.load_or_create_token(dangling)
    assert not (tmp_path / "elsewhere").exists()  # never follows a symlink when creating
    short = tmp_path / "short"
    short.write_text("abc")
    short.chmod(0o600)
    with pytest.raises(ValueError, match="32"):
        sr.read_token_file(short)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(ValueError, match="world-writable"):
        sr.load_or_create_token(shared / "token")


def test_rootful_engine_is_refused_unless_explicitly_allowed():
    engine = FakeEngine(info=ROOTFUL_INFO)
    with pytest.raises(ValueError, match="rootful.*BC_SANDBOX_ALLOW_ROOTFUL"):
        sr.SandboxManager(make_config(), engine).preflight()
    manager = sr.SandboxManager(make_config(allow_rootful=True), engine)
    manager.preflight()
    assert manager.rootless is False and manager.status()["rootless"] is False
    podman = FakeEngine("podman", info={"host": {"security": {"rootless": False},
                                                 "cgroupControllers": ["cpu", "memory", "pids"]}})
    with pytest.raises(ValueError, match="rootful"):
        sr.SandboxManager(make_config(engine="podman"), podman).preflight()
    podman.info["host"]["security"]["rootless"] = True
    sr.SandboxManager(make_config(engine="podman"), podman).preflight()


def test_engines_that_cannot_enforce_limits_are_refused():
    engine = FakeEngine(info=dict(ROOTLESS_INFO, MemoryLimit=False))
    with pytest.raises(ValueError, match="cannot enforce resource limits"):
        sr.SandboxManager(make_config(), engine).preflight()
    podman = FakeEngine("podman", info={"host": {"security": {"rootless": True}, "cgroupControllers": ["cpu"]}})
    with pytest.raises(ValueError, match="memory, pids"):
        sr.SandboxManager(make_config(engine="podman"), podman).preflight()


def test_images_are_never_pulled_and_runtime_must_exist(engine):
    engine.images = set()
    with pytest.raises(ValueError, match="never pulls"):
        sr.SandboxManager(make_config(), engine).preflight()
    assert not any("pull" == args[0] for args, _ in engine.calls)
    engine.images = {sr.DEFAULT_IMAGE}
    with pytest.raises(ValueError, match="runtime 'kata'"):
        sr.SandboxManager(make_config(runtime="kata"), engine).preflight()
    sr.SandboxManager(make_config(runtime="runsc"), engine).preflight()


def test_start_up_removes_leftover_sandboxes_of_this_instance_only(engine):
    engine.containers = {"bc-sandbox-old": {"state": "running", "args": []}}
    manager = sr.SandboxManager(make_config(), engine)
    manager.preflight()
    assert engine.containers == {}
    ps = engine.commands("ps")[0]
    assert f"label={sr.LABEL}=1" in ps and f"label={sr.LABEL_RUNNER}=unit" in ps


def test_bridge_network_is_logged_loudly(engine, caplog):
    with caplog.at_level("WARNING"):
        sr.SandboxManager(make_config(network="bridge"), engine).preflight()
    assert "BC_SANDBOX_NETWORK=bridge" in caplog.text


def test_module_imports_without_side_effects_or_web_packages():
    root = Path(__file__).resolve().parents[1]
    code = ("import sys, threading, compute.sandbox_runner as r; "
            "assert not any(n.split('.')[0] in ('flask', 'werkzeug', 'bananachat') for n in sys.modules); "
            "assert threading.active_count() == 1; print('ok')")
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60,
                            env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


def test_main_refuses_bad_configuration(monkeypatch):
    monkeypatch.delenv("BC_SANDBOX_TOKEN_FILE", raising=False)
    monkeypatch.setattr(sr.logging, "basicConfig", lambda **_: None)
    assert sr.main() == 2


# ----- container hardening ------------------------------------------------------------------------------------

def test_every_sandbox_gets_every_hardening_flag(manager, engine):
    box = manager.create({"session": "task-1"})
    args = engine.commands("run")[0]
    joined = " ".join(args)

    def value(flag):
        return args[args.index(flag) + 1]
    assert value("--network") == "none" and "--read-only" in args and "--init" in args
    assert value("--user") == "1000:1000" and value("--cap-drop") == "ALL"
    assert value("--security-opt") == "no-new-privileges"
    assert value("--pids-limit") == "256" and value("--memory") == value("--memory-swap") == "1024m"
    assert value("--cpus") == "1" and "nofile=1024:1024" in args and "core=0" in args
    assert value("--pull") == "never" and value("--restart") == "no" and value("--log-driver") == "none"
    assert "/workspace:rw,exec,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700" in args
    assert "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777" in args
    assert f"{sr.LABEL}=1" in args and f"{sr.LABEL_SESSION}=task-1" in args and f"{sr.LABEL_ID}={box.id}" in args
    assert args[-2:] == [sr.DEFAULT_IMAGE, "infinity"]
    assert value("--entrypoint") == "sleep"
    for flag in FORBIDDEN_FLAGS:
        assert flag not in args, flag
    assert "seccomp" not in joined and "unconfined" not in joined and ":/" not in joined.replace("/workspace:", "")
    # The container was verified and its idle process recorded for kill-all.
    assert box.keeper == "7" and engine.commands("inspect")


def test_podman_and_gvisor_flags():
    engine = FakeEngine("podman", info={"host": {"security": {"rootless": True},
                                                 "cgroupControllers": ["cpu", "memory", "pids"]}})
    manager = sr.SandboxManager(make_config(engine="podman", runtime="runsc"), engine)
    manager.preflight()
    args = manager.create_args("0" * 32, "bc-sandbox-x", "s", sr.DEFAULT_IMAGE, manager.default_limits())
    assert "--read-only-tmpfs=false" in args and args[args.index("--runtime") + 1] == "runsc"


def test_verification_rejects_an_engine_that_ignores_flags(manager, engine):
    original = engine.inspect
    engine.inspect = lambda args: dict(original(args), HostConfig=dict(original(args)["HostConfig"],
                                                                        ReadonlyRootfs=False))
    with pytest.raises(sr.ApiError) as caught:
        manager.create({"session": "s"})
    assert caught.value.code == "engine_error" and engine.containers == {}
    assert manager.list() == []


def test_create_validates_session_image_and_limits(manager, engine):
    for request in ({}, {"session": ""}, {"session": "a" * 65}, {"session": "x;rm -rf /"}, {"session": "a=b"},
                    {"session": 5}, {"session": "s\n"}, {"session": "s\x00"}, {"session": "sé"}):
        with pytest.raises(sr.ApiError) as caught:
            manager.create(request)
        assert caught.value.status == 400
    with pytest.raises(sr.ApiError, match="allowlist"):
        manager.create({"session": "s", "image": "alpine"})
    for limits in ({"memory_mb": "512"}, {"memory_mb": True}, {"memory_mb": 10}, {"cpus": 0},
                   {"workspace_mb": 1.5}, {"memory_mb": float("nan")}):
        with pytest.raises(sr.ApiError) as caught:
            manager.create(dict(session="s", **limits))
        assert caught.value.code == "invalid_limits"
    assert not engine.commands("run")
    # Requests can lower the caps but never raise them.
    box = manager.create({"session": "s", "memory_mb": 99999, "cpus": 32, "workspace_mb": 64})
    assert box.limits["memory_mb"] == 1024 and box.limits["cpus"] == 1.0 and box.limits["workspace_mb"] == 64
    box = manager.create({"session": "s", "memory_mb": 256, "cpus": 0.5})
    args = engine.commands("run")[-1]
    assert args[args.index("--memory") + 1] == "256m" and args[args.index("--cpus") + 1] == "0.5"


def test_capacity_is_enforced(manager, engine):
    manager.create({"session": "a"})
    manager.create({"session": "b"})
    with pytest.raises(sr.ApiError) as caught:
        manager.create({"session": "c"})
    assert caught.value.status == 429 and caught.value.code == "capacity"
    assert len(engine.commands("run")) == 2


def test_failed_creation_cleans_up_and_frees_the_slot(manager, engine):
    engine.fail_create = True
    for _ in range(3):
        with pytest.raises(sr.ApiError):
            manager.create({"session": "a"})
    engine.fail_create = False
    manager.create({"session": "a"})


def test_failed_removals_keep_counting_against_capacity(manager, engine):
    box = manager.create({"session": "a"})
    real = engine.answer

    def stuck(args, stdin, timeout):
        if args[0] == "rm":
            return sr.RunResult(1, b"", b"device busy")
        return real(args, stdin, timeout)
    engine.answer = stuck
    manager.delete(box.id)
    manager.create({"session": "b"})
    with pytest.raises(sr.ApiError, match="All sandboxes"):
        manager.create({"session": "c"})
    engine.answer = real
    manager.reap()  # retries the removal
    manager.create({"session": "c"})


def test_missing_and_already_removed_containers_release_capacity(manager, engine):
    box = manager.create({"session": "a"})
    engine.containers.pop(box.name)
    real = engine.answer

    def missing(args, stdin, timeout):
        if args[0] == "rm":
            return sr.RunResult(1, b"", b"Error response from daemon: No such container")
        return real(args, stdin, timeout)

    engine.answer = missing
    manager.delete(box.id)
    manager.delete(box.id)
    manager.remove(box, "already removed")
    assert len(engine.commands("rm")) == 1
    engine.answer = real
    assert manager.create({"session": "b"})
    assert manager.create({"session": "c"})


def test_a_container_being_removed_still_counts_against_capacity(engine):
    manager = sr.SandboxManager(make_config(max_sandboxes=1), engine)
    manager.preflight()
    box = manager.create({"session": "a"})
    entered, release = threading.Event(), threading.Event()
    real = engine.answer

    def slow_remove(args, stdin, timeout):
        if args[0] == "rm":
            entered.set()
            assert release.wait(5)
        return real(args, stdin, timeout)

    engine.answer = slow_remove
    worker = threading.Thread(target=manager.delete, args=(box.id,))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(sr.ApiError, match="All sandboxes"):
            manager.create({"session": "b"})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert engine.containers == {}
    assert manager.create({"session": "b"})


# ----- paths ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("/workspace", "/workspace"), ("/workspace/", "/workspace"), ("a/b.txt", "/workspace/a/b.txt"),
    ("/workspace/a/../b", "/workspace/b"), ("./x", "/workspace/x"), ("//workspace//x", "/workspace/x"),
    ("-rf", "/workspace/-rf"), ("/workspace/ünïcode ok", "/workspace/ünïcode ok"),
])
def test_paths_are_normalised_under_the_workspace(value, expected):
    assert sr.clean_path(value) == expected


@pytest.mark.parametrize("value", [
    "", "/", "/etc/passwd", "../etc/passwd", "/workspace/../etc", "/workspace/../../x", "/workspacex",
    "/workspace2/x", "a\x00b", "a\nb", "a\rb", "\x1b[31m", "/workspace/" + "a" * 256, "a/" * 3000, None, 5,
    "/tmp/x", "\udc80",
])
def test_paths_outside_the_workspace_or_malformed_are_refused(value):
    with pytest.raises(sr.ApiError) as caught:
        sr.clean_path(value)
    assert caught.value.status == 400


def test_file_paths_cannot_be_the_workspace_itself():
    with pytest.raises(sr.ApiError):
        sr.clean_path("/workspace", allow_root=False)


# ----- exec ---------------------------------------------------------------------------------------------------------

def test_commands_reach_the_container_as_one_argument_never_through_a_host_shell(manager, engine):
    seen = []
    engine.exec_handler = lambda argv, stdin: seen.append(argv) or sr.RunResult(0, b"out", b"err")
    box = manager.create({"session": "s"})
    evil = "'; touch /tmp/pwned; echo $(id) `id` && rm -rf / #"
    result = manager.execute(box.id, {"command": evil, "cwd": "sub dir"})
    assert result == {"exit_code": 0, "stdout": "out", "stderr": "err", "truncated": False, "timed_out": False,
                      "duration_ms": result["duration_ms"], "interrupted": False, "sandbox_removed": False}
    assert seen == [["sh", "-c", sr.EXEC_WRAPPER, "sh", "/workspace/sub dir", evil]]
    args = engine.commands("exec")[-1]
    assert args[:8] == ["exec", "--user", "1000:1000", "--workdir", "/workspace", "--env", "HOME=/workspace",
                        box.name]
    assert "--privileged" not in args and "-i" not in args


def test_exec_validates_its_input(manager):
    box = manager.create({"session": "s"})
    for request in ({}, {"command": ""}, {"command": "   "}, {"command": 5}, {"command": "a\x00b"},
                    {"command": "x" * (sr.MAX_COMMAND + 1)}, {"command": "ls", "timeout": 0},
                    {"command": "ls", "timeout": -1}, {"command": "ls", "timeout": "5"},
                    {"command": "ls", "timeout": True}, {"command": "ls", "timeout": float("nan")},
                    {"command": "ls", "cwd": "/etc"}, {"command": "ls", "cwd": "../.."}):
        with pytest.raises(sr.ApiError) as caught:
            manager.execute(box.id, request)
        assert caught.value.status == 400, request
    with pytest.raises(sr.ApiError) as caught:
        manager.execute("f" * 32, {"command": "ls"})
    assert caught.value.status == 404


def test_timeouts_are_capped_and_stop_the_command(manager, engine):
    box = manager.create({"session": "s"})
    timeouts = []

    def slow(argv, stdin):
        return sr.RunResult(None, b"partial", b"", 7, 0, timed_out=True)
    engine.exec_handler = slow
    real_run = engine.run

    def spy(args, **kwargs):
        if args[0] == "exec" and sr.EXEC_WRAPPER in args:
            timeouts.append(kwargs["timeout"])
        return real_run(args, **kwargs)
    engine.run = spy
    result = manager.execute(box.id, {"command": "sleep 1000", "timeout": 10 ** 9})
    assert timeouts == [30.0]  # BC_SANDBOX_EXEC_TIMEOUT caps what the caller asks for
    assert result["timed_out"] and result["exit_code"] == 124 and result["stdout"] == "partial"
    assert "timed out" in result["stderr"] and not result["sandbox_removed"]
    kills = [args for args in engine.commands("exec") if sr.KILL_SCRIPT in args]
    assert kills and kills[-1][-1] == "7"  # every process but the idle keeper is killed
    manager.execute(box.id, {"command": "ls"})
    assert timeouts[-1] == 30.0  # the default, min(120 s, cap)


def test_a_command_that_cannot_be_killed_takes_its_sandbox_with_it(manager, engine):
    box = manager.create({"session": "s"})
    engine.exec_handler = lambda argv, stdin: sr.RunResult(None, timed_out=True)
    engine.kill_ok = False
    result = manager.execute(box.id, {"command": ":(){ :|:& };:", "timeout": 1})
    assert result["timed_out"] and result["sandbox_removed"]
    assert box.name not in engine.containers and manager.list() == []
    with pytest.raises(sr.ApiError) as caught:
        manager.execute(box.id, {"command": "ls"})
    assert caught.value.status == 410  # a removed sandbox is reported gone, not unknown


def test_output_is_capped_with_a_marker(manager, engine):
    box = manager.create({"session": "s"})
    cap = manager.config.output_kb * 1024
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"a" * cap, b"\xff\xfe", cap + 5000, 2)
    result = manager.execute(box.id, {"command": "yes"})
    assert result["truncated"] and result["stdout"].startswith("a" * cap)
    assert result["stdout"].endswith("[output truncated: 5000 more bytes]")
    assert result["stderr"] == "��"  # undecodable bytes never break the JSON
    engine.exec_handler = lambda argv, stdin: sr.RunResult(None, b"y" * cap, b"", sr.DRAIN_LIMIT + 1, 0,
                                                           overflow=True)
    result = manager.execute(box.id, {"command": "yes"})
    assert result["truncated"] and "too much output" in result["stderr"]
    assert any(sr.KILL_SCRIPT in args for args in engine.commands("exec"))


def test_one_command_per_sandbox_and_a_global_cap(manager, engine):
    first = manager.create({"session": "a"})
    second = manager.create({"session": "b"})
    release = threading.Event()
    entered = threading.Event()

    def blocking(argv, stdin):
        entered.set()
        release.wait(5)
        return sr.RunResult(0)
    engine.exec_handler = blocking
    worker = threading.Thread(target=manager.execute, args=(first.id, {"command": "sleep 1"}))
    worker.start()
    assert entered.wait(5)
    with pytest.raises(sr.ApiError) as caught:
        manager.execute(first.id, {"command": "ls"})
    assert caught.value.status == 409 and caught.value.code == "busy"
    # A busy sandbox is not idle.
    manager.config.idle_ttl = 0
    assert manager.reap() == [second.id]
    release.set()
    worker.join(5)
    manager.config.idle_ttl = 1800
    # The global cap on engine operations answers 503 when exhausted.
    third = manager.create({"session": "c"})
    for _ in range(manager.config.max_execs):
        manager._slots.acquire()
    try:
        with pytest.raises(sr.ApiError) as caught:
            manager.execute(third.id, {"command": "ls"})
        assert caught.value.status == 503
    finally:
        for _ in range(manager.config.max_execs):
            manager._slots.release()


def test_a_sandbox_whose_main_process_was_killed_is_reported_gone(manager, engine):
    box = manager.create({"session": "s"})

    def kill_everything(argv, stdin):
        engine.containers[box.name]["state"] = "exited"
        return sr.RunResult(137)
    engine.exec_handler = kill_everything
    with pytest.raises(sr.ApiError) as caught:
        manager.execute(box.id, {"command": "kill -9 -1"})
    assert caught.value.status == 410 and caught.value.code == "sandbox_gone"
    assert manager.list() == [] and box.name not in engine.containers
    # A plain failing command does not remove the sandbox.
    other = manager.create({"session": "t"})
    engine.exec_handler = lambda argv, stdin: sr.RunResult(1, b"", b"Error response from daemon: fake")
    assert manager.execute(other.id, {"command": "false"})["exit_code"] == 1
    assert [item["id"] for item in manager.list()] == [other.id]


# ----- reaper -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("include_new_container", [False, True])
def test_reconcile_does_not_remove_a_sandbox_created_during_its_listing(manager, engine, include_new_container):
    listed, release = threading.Event(), threading.Event()
    real = engine.answer

    def slow_listing(args, stdin, timeout):
        result = None if include_new_container else real(args, stdin, timeout)
        if args[0] == "ps":
            listed.set()
            assert release.wait(5)
        return real(args, stdin, timeout) if result is None else result

    engine.answer = slow_listing
    worker = threading.Thread(target=manager.reconcile)
    worker.start()
    try:
        assert listed.wait(5)
        box = manager.create({"session": "new"})
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert manager.get(box.id) is box
    assert box.name in engine.containers
    assert not engine.commands("rm")


def test_reconcile_and_delete_do_not_remove_the_same_container_concurrently(manager, engine):
    box = manager.create({"session": "s"})
    entered, release, overlap = threading.Event(), threading.Event(), threading.Event()
    real = engine.answer

    def slow_remove(args, stdin, timeout):
        if args[0] == "rm":
            if entered.is_set() and not release.is_set():
                overlap.set()
                return sr.RunResult(1, b"", b"removal already in progress")
            entered.set()
            assert release.wait(5)
        return real(args, stdin, timeout)

    engine.answer = slow_remove
    deleting = threading.Thread(target=manager.delete, args=(box.id,))
    reconciling = threading.Thread(target=manager.reconcile)
    deleting.start()
    try:
        assert entered.wait(5)
        reconciling.start()
        assert not overlap.wait(0.2)
    finally:
        release.set()
        deleting.join(5)
        if reconciling.ident is not None:
            reconciling.join(5)
    assert not deleting.is_alive() and not reconciling.is_alive()
    assert engine.containers == {}
    assert manager.list() == []


def test_reaper_removes_idle_expired_and_unknown_containers(engine):
    now = [1000.0]
    manager = sr.SandboxManager(make_config(max_sandboxes=4), engine, clock=lambda: now[0])
    manager.preflight()
    idle = manager.create({"session": "idle"})
    now[0] += 1000
    fresh = manager.create({"session": "fresh"})
    now[0] += 900  # idle unused for 1900 s > 1800, fresh for 900 s
    assert manager.reap() == [idle.id]
    manager.execute(fresh.id, {"command": "true"})
    now[0] += 21600  # older than BC_SANDBOX_MAX_AGE even though just used
    manager._touch(fresh)
    assert manager.reap() == [fresh.id]
    # Containers this runner does not know are removed; dead ones are forgotten.
    engine.containers["bc-sandbox-stray"] = {"state": "running", "args": []}
    kept = manager.create({"session": "k"})
    dead = manager.create({"session": "d"})
    engine.containers[dead.name]["state"] = "exited"
    manager.reap()
    assert set(engine.containers) == {kept.name}
    assert [item["id"] for item in manager.list()] == [kept.id]
    del engine.containers[kept.name]
    manager.reap()
    assert manager.list() == []


def test_expiry_is_reported(engine):
    now = [0.0]
    manager = sr.SandboxManager(make_config(), engine, clock=lambda: now[0], wall=lambda: 1_700_000_000.0)
    manager.preflight()
    box = manager.create({"session": "s"})
    assert box.public(manager)["expires_at"] == sr._iso(1_700_000_000.0 + 1800)


def test_shutdown_removes_every_sandbox(manager, engine):
    manager.create({"session": "a"})
    manager.create({"session": "b"})
    manager.shutdown()
    assert engine.containers == {} and manager.list() == []


def test_shutdown_retries_a_previously_failed_removal(manager, engine):
    box = manager.create({"session": "s"})
    real = engine.answer
    engine.answer = lambda args, stdin, timeout: (
        sr.RunResult(1, b"", b"device busy") if args[0] == "rm" else real(args, stdin, timeout))
    manager.delete(box.id)
    assert box.name in engine.containers
    engine.answer = real
    manager.shutdown()
    assert engine.containers == {}


def test_shutdown_waits_for_an_inflight_removal(manager, engine):
    box = manager.create({"session": "s"})
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    real = engine.answer

    def slow_remove(args, stdin, timeout):
        if args[0] == "rm":
            entered.set()
            assert release.wait(5)
        return real(args, stdin, timeout)

    def shutdown():
        manager.shutdown()
        stopped.set()

    engine.answer = slow_remove
    deleting = threading.Thread(target=manager.delete, args=(box.id,))
    stopping = threading.Thread(target=shutdown)
    deleting.start()
    try:
        assert entered.wait(5)
        stopping.start()
        assert not stopped.wait(0.2)
    finally:
        release.set()
        deleting.join(5)
        if stopping.ident is not None:
            stopping.join(5)
    assert not deleting.is_alive() and not stopping.is_alive()
    assert stopped.is_set() and engine.containers == {}


@pytest.mark.parametrize("failed_creation", [False, True])
def test_shutdown_waits_for_pending_creation_and_refuses_new_sandboxes(manager, engine, failed_creation):
    entered, release = threading.Event(), threading.Event()
    real = engine.answer
    errors = []

    def slow_create(args, stdin, timeout):
        if args[0] == "run":
            entered.set()
            assert release.wait(5)
        return real(args, stdin, timeout)

    def create():
        try:
            manager.create({"session": "s"})
        except sr.ApiError as error:
            errors.append(error.status)

    engine.answer = slow_create
    engine.fail_create = failed_creation
    creating = threading.Thread(target=create)
    stopping = threading.Thread(target=manager.shutdown)
    creating.start()
    try:
        assert entered.wait(5)
        stopping.start()
        assert manager._stop.wait(5)
    finally:
        release.set()
        creating.join(5)
        if stopping.ident is not None:
            stopping.join(5)
    assert not creating.is_alive() and not stopping.is_alive()
    assert errors == [502 if failed_creation else 503]
    assert engine.containers == {} and manager.list() == []
    with pytest.raises(sr.ApiError) as caught:
        manager.create({"session": "new"})
    assert caught.value.status == 503
    assert len(engine.commands("run")) == 1


def test_shutdown_does_not_wait_forever_for_a_stuck_pending_creation(manager, monkeypatch, caplog):
    manager._pending.add("bc-sandbox-stuck")
    monkeypatch.setattr(sr, "CREATE_SHUTDOWN_TIMEOUT", 0)
    manager.shutdown()
    assert "Timed out waiting for 1 pending sandbox creations during shutdown" in caplog.text


# ----- files --------------------------------------------------------------------------------------------------------------

def test_file_reads_run_inside_the_container_with_fixed_scripts(manager, engine):
    box = manager.create({"session": "s"})
    seen = []

    def reader(argv, stdin):
        seen.append(argv)
        path = argv[-3]
        if path == "/workspace/dir":
            return sr.RunResult(0, b"D\nf\t3\tb.txt\0d\t60\tsub\0l\t4\tlink\0p\t0\tfifo\0")
        if path == "/workspace/bin":
            return sr.RunResult(0, b"F\t4\n\x00\xff\x01\x02")
        if path == "/workspace/big":
            return sr.RunResult(0, b"F\t5000000000\n" + b"z" * sr.MAX_READ_BYTES)
        return sr.RunResult({"/workspace/out": 91, "/workspace/none": 92, "/workspace/fifo": 93}.get(path, 0),
                            b"F\t2\nhi")
    engine.exec_handler = reader
    assert manager.read_path(box.id, "a.txt") == {"path": "/workspace/a.txt", "type": "file", "size": 2,
                                                  "truncated": False, "content": "hi", "encoding": "utf-8"}
    assert seen[0][:7] == ["timeout", "-s", "KILL", "60", "sh", "-c", sr.READ_SCRIPT]
    assert seen[0][7:] == ["sh", "/workspace/a.txt", str(sr.MAX_READ_BYTES), str(sr.MAX_LIST_ENTRIES + 1)]
    listing = manager.read_path(box.id, "dir")
    assert listing["type"] == "dir" and not listing["truncated"]
    assert listing["entries"] == [{"name": "b.txt", "type": "file", "size": 3},
                                  {"name": "fifo", "type": "other", "size": 0},
                                  {"name": "link", "type": "symlink", "size": 4},
                                  {"name": "sub", "type": "dir", "size": 60}]
    binary = manager.read_path(box.id, "bin")
    assert binary["encoding"] == "base64" and base64.b64decode(binary["content"]) == b"\x00\xff\x01\x02"
    big = manager.read_path(box.id, "big")
    assert big["truncated"] and big["size"] == 5000000000 and len(big["content"]) == sr.MAX_READ_BYTES
    for path, status in (("out", 403), ("none", 404), ("fifo", 400)):
        with pytest.raises(sr.ApiError) as caught:
            manager.read_path(box.id, path)
        assert caught.value.status == status
    with pytest.raises(sr.ApiError):
        manager.read_path(box.id, "../../etc/shadow")
    assert len(seen) == 7  # the traversal never reached the engine


def test_file_writes_pass_content_on_stdin(manager, engine):
    box = manager.create({"session": "s"})
    seen = []
    engine.exec_handler = lambda argv, stdin: seen.append((argv, stdin)) or sr.RunResult(0, b"5\n")
    assert manager.write_file(box.id, "a/b.txt", b"hello") == {"path": "/workspace/a/b.txt", "size": 5}
    argv, stdin = seen[0]
    assert argv[4:7] == ["sh", "-c", sr.WRITE_SCRIPT] and argv[-1] == "/workspace/a/b.txt" and stdin == b"hello"
    assert "-i" in engine.commands("exec")[-1]
    engine.exec_handler = lambda argv, stdin: sr.RunResult(96)
    with pytest.raises(sr.ApiError) as caught:
        manager.write_file(box.id, "full", b"x")
    assert caught.value.status == 507
    with pytest.raises(sr.ApiError):
        manager.write_file(box.id, "/workspace", b"x")


def test_archive_download_is_capped(manager, engine, monkeypatch):
    box = manager.create({"session": "s"})
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"\x1f\x8bdata", b"", 6)
    assert manager.download_archive(box.id) == b"\x1f\x8bdata"
    argv = [args for args in engine.commands("exec") if "tar" in args][-1]
    assert argv[-8:] == ["tar", "-c", "-z", "-f", "-", "-C", "/workspace", "."]
    engine.exec_handler = lambda argv, stdin: sr.RunResult(None, b"x", b"", sr.MAX_ARCHIVE_DOWNLOAD + 1,
                                                           overflow=True)
    with pytest.raises(sr.ApiError) as caught:
        manager.download_archive(box.id)
    assert caught.value.status == 413


# ----- archives -------------------------------------------------------------------------------------------------------------

def make_tar(entries, compress=True, fmt=tarfile.PAX_FORMAT):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if compress else "w", format=fmt) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def tar_file(name, data=b"x", mode=0o644, **extra):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    for key, value in extra.items():
        setattr(info, key, value)
    return info, data


def tar_special(name, kind, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def replay(plan):
    output = io.BytesIO()
    plan.write_tar(output)
    output.seek(0)
    with tarfile.open(fileobj=output) as tar:
        return {member.name: (member, tar.extractfile(member).read() if member.isfile() else None)
                for member in tar.getmembers()}


def test_archives_are_rewritten_with_only_safe_entries():
    data = make_tar([
        tar_file("./project/run.sh", b"#!/bin/sh\necho hi\n", mode=0o4755, uid=0, gid=0, uname="root"),
        tar_file("project/data.txt", b"data"),
        tar_special("project/link", tarfile.SYMTYPE, "/etc/passwd"),
        tar_special("project/hard", tarfile.LNKTYPE, "project/data.txt"),
        tar_special("project/dev", tarfile.CHRTYPE),
        tar_special("project/fifo", tarfile.FIFOTYPE),
        tar_special("project/dir", tarfile.DIRTYPE),
    ])
    plan = sr.ArchivePlan(data, 1 << 20)
    assert (plan.files, plan.dirs, plan.skipped, plan.size) == (2, 1, 4, 22)
    members = replay(plan)
    assert set(members) == {"project/run.sh", "project/data.txt", "project/dir"}
    run, content = members["project/run.sh"]
    assert content == b"#!/bin/sh\necho hi\n" and run.mode == 0o755  # setuid bit dropped
    assert (run.uid, run.gid, run.uname) == (1000, 1000, "")
    assert members["project/data.txt"][0].mode == 0o644
    # Plain tar works too.
    assert sr.ArchivePlan(make_tar([tar_file("a", b"1")], compress=False), 1 << 20).files == 1


@pytest.mark.parametrize("name", ["../escape", "a/../../escape", "/etc/cron.d/x", "a\\..\\b", "bad\nname",
                                  "x" * 300 + "/y"])
def test_archive_traversal_and_bad_names_are_refused(name):
    with pytest.raises(sr.ArchiveError):
        sr.ArchivePlan(make_tar([tar_file(name)]), 1 << 20)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, "x")
    with pytest.raises(sr.ArchiveError):
        sr.ArchivePlan(buffer.getvalue(), 1 << 20)


def test_gzip_bombs_stop_at_the_workspace_size():
    buffer = io.BytesIO()
    zeros = b"\0" * (1 << 20)
    info = tarfile.TarInfo("bomb")
    info.size = 64 << 20
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.addfile(info, io.BytesIO(zeros * 64))
    data = buffer.getvalue()
    assert len(data) < 200_000
    with pytest.raises(sr.ArchiveError, match="more than the workspace"):
        sr.ArchivePlan(data, 8 << 20)
    # A lying header cannot sneak more data through either: the gzip stream itself is capped.
    guard = sr._Gunzip(data, 1 << 20)
    with pytest.raises(sr.ArchiveError):
        while guard.read(65536):
            pass


def test_zip_bombs_are_refused_from_their_declared_sizes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(4):
            archive.writestr(f"part{index}", b"\0" * (4 << 20))
    with pytest.raises(sr.ArchiveError, match="more than the workspace"):
        sr.ArchivePlan(buffer.getvalue(), 8 << 20)


def test_oversized_metadata_headers_are_refused_before_being_read():
    info, data = tar_file("a", b"1")
    info.pax_headers = {"comment": "x" * (sr.MAX_ARCHIVE_META + 10)}
    with pytest.raises(sr.ArchiveError, match="metadata"):
        sr.ArchivePlan(make_tar([(info, data)]), 1 << 20)


def test_archives_with_too_many_entries_are_refused(monkeypatch):
    monkeypatch.setattr(sr, "MAX_ARCHIVE_ENTRIES", 5)
    with pytest.raises(sr.ArchiveError, match="more than 5 entries"):
        sr.ArchivePlan(make_tar([tar_file(f"f{index}") for index in range(6)]), 1 << 20)


def test_zip_uploads_keep_files_and_executable_bits_and_skip_symlinks():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("src/", "")
        archive.writestr("src/main.py", "print('hi')\n")
        script = zipfile.ZipInfo("src/run.sh")
        script.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(script, "#!/bin/sh\n")
        link = zipfile.ZipInfo("src/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
    plan = sr.ArchivePlan(buffer.getvalue(), 1 << 20)
    assert (plan.files, plan.dirs, plan.skipped) == (2, 1, 1)
    members = replay(plan)
    assert members["src/main.py"][1] == b"print('hi')\n" and members["src/run.sh"][0].mode == 0o755
    assert "src/link" not in members


def test_corrupt_encrypted_and_unknown_archives_are_refused():
    with pytest.raises(sr.ApiError) as caught:
        sr.ArchivePlan(b"not an archive at all", 1 << 20)
    assert caught.value.status == 415
    with pytest.raises(sr.ArchiveError):
        sr.ArchivePlan(b"\x1f\x8b" + os.urandom(200), 1 << 20)
    good = make_tar([tar_file("a", b"1" * 1000)])
    with pytest.raises(sr.ArchiveError):
        sr.ArchivePlan(good[: len(good) // 2], 1 << 20)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "secret")
    encrypted = bytearray(buffer.getvalue())
    encrypted[6] |= 1  # local header flag
    central = encrypted.rfind(b"PK\x01\x02")
    encrypted[central + 8] |= 1
    with pytest.raises(sr.ArchiveError, match="Encrypted"):
        sr.ArchivePlan(bytes(encrypted), 1 << 20)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", "hello world" * 100)
    corrupted = bytearray(buffer.getvalue())
    corrupted[40] ^= 0xFF  # inside the compressed data
    with pytest.raises(sr.ArchiveError):
        sr.ArchivePlan(bytes(corrupted), 1 << 20)


def test_multi_member_gzip_is_read_in_full():
    part = make_tar([tar_file("a", b"1")], compress=False)
    import gzip
    data = gzip.compress(part[:512]) + gzip.compress(part[512:])
    assert sr.ArchivePlan(data, 1 << 20).files == 1


def test_bad_archives_never_reach_the_container(manager, engine):
    box = manager.create({"session": "s"})
    before = len(engine.commands("exec"))
    for data in (make_tar([tar_file("../x")]), b"garbage" * 100):
        with pytest.raises(sr.ApiError):
            manager.upload_archive(box.id, data)
    assert len(engine.commands("exec")) == before
    seen = []
    engine.exec_handler = lambda argv, stdin: seen.append((argv, stdin)) or sr.RunResult(0)
    result = manager.upload_archive(box.id, make_tar([tar_file("a.txt", b"hello")]), "/workspace/in")
    assert result == {"path": "/workspace/in", "files": 1, "directories": 0, "skipped": 0, "size": 5}
    argv, stdin = seen[0]
    assert argv[:7] == ["timeout", "-s", "KILL", str(sr.ARCHIVE_TIMEOUT), "sh", "-c", sr.UNPACK_SCRIPT]
    assert "--no-same-owner" in sr.UNPACK_SCRIPT and argv[-1] == "/workspace/in"
    with tarfile.open(fileobj=io.BytesIO(stdin)) as tar:
        assert tar.getnames() == ["a.txt"]


# ----- the real engine class --------------------------------------------------------------------------------------------------

@pytest.fixture
def stub_engine(tmp_path):
    """An 'engine' binary that is a Python script, to exercise Engine.run for real."""
    script = tmp_path / "engine"
    script.write_text(f"#!{sys.executable}\n" + """
import json, os, sys, time
args = sys.argv[1:]
mode = args[0]
if mode == "argv":
    print(json.dumps(args[1:]))
elif mode == "sleep":
    time.sleep(float(args[1]))
elif mode == "flood":
    chunk = b"y" * 65536
    while True:
        sys.stdout.buffer.write(chunk)
elif mode == "echo-stdin":
    data = sys.stdin.buffer.read()
    sys.stdout.write(str(len(data)))
""")
    script.chmod(0o755)
    return sr.Engine("docker", binary=str(script), environ={"PATH": os.environ.get("PATH", "")})


def test_engine_passes_arguments_verbatim_without_a_shell(stub_engine, tmp_path):
    marker = tmp_path / "pwned"
    evil = [f"$(touch {marker})", f"`touch {marker}`", f"; touch {marker}", "a b", "--privileged", "*", "'\""]
    result = stub_engine.run(["argv", *evil], timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout) == evil and not marker.exists()
    with pytest.raises(sr.ApiError):
        stub_engine.run(["argv", "a\x00b"])


def test_engine_timeouts_output_caps_and_stdin(stub_engine):
    started = time.monotonic()
    result = stub_engine.run(["sleep", "30"], timeout=0.5)
    assert result.timed_out and time.monotonic() - started < 10
    started = time.monotonic()
    result = stub_engine.run(["flood"], timeout=30, limit=1000, drain_limit=1 << 20)
    assert result.overflow and len(result.stdout) == 1000 and result.stdout_total > 1 << 20
    assert time.monotonic() - started < 10
    assert stub_engine.run(["echo-stdin"], stdin=b"x" * 3_000_000, timeout=30).stdout == b"3000000"
    assert stub_engine.run(["echo-stdin"], stdin=lambda pipe: pipe.write(b"abc"), timeout=30).stdout == b"3"

    def broken(pipe):
        pipe.write(b"a")
        raise sr.ArchiveError("bad")
    result = stub_engine.run(["echo-stdin"], stdin=broken, timeout=30)
    assert isinstance(result.error, sr.ArchiveError)


# ----- HTTP -------------------------------------------------------------------------------------------------------------------

def test_health_needs_no_token_but_details_do(server):
    status, payload, _ = call(server, "GET", "/healthz", token=None)
    assert status == 200 and payload == {"ok": True, "engine": "docker"}
    status, payload, _ = call(server, "GET", "/healthz")
    assert payload["rootless"] is True and payload["network"] == "none" and payload["max"] == 2
    assert payload["images"] == [sr.DEFAULT_IMAGE] and payload["sandboxes"] == 0 and payload["runtime"] == "default"


def test_every_api_route_needs_the_token(server, engine):
    routes = [("GET", "/v1/sandboxes"), ("POST", "/v1/sandboxes"), ("DELETE", "/v1/sandboxes/" + "a" * 32),
              ("POST", "/v1/sandboxes/" + "a" * 32 + "/exec"), ("GET", "/v1/sandboxes/" + "a" * 32 + "/files"),
              ("PUT", "/v1/sandboxes/" + "a" * 32 + "/archive"), ("GET", "/nope")]
    for method, path in routes:
        for token in (None, "wrong" * 10, TOKEN[:-1], TOKEN + "x"):
            status, payload, response = call(server, method, path, body={} if method != "GET" else None,
                                             token=token)
            assert status in (401, 429), (method, path)
            if status == 401:
                assert payload["error"]["code"] == "unauthorized"
                assert response.getheader("WWW-Authenticate")
    assert not engine.commands("run")
    reply = raw_request(server, b"GET /v1/sandboxes HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer \xc3\xa9\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 4")


def test_repeated_bad_tokens_are_rate_limited_but_the_real_token_still_works(server):
    for _ in range(sr.AUTH_FAILURES_PER_MINUTE):
        assert call(server, "GET", "/v1/sandboxes", token="bad" * 20)[0] == 401
    status, payload, response = call(server, "GET", "/v1/sandboxes", token="bad" * 20)
    assert status == 429 and payload["error"]["code"] == "rate_limited" and response.getheader("Retry-After")
    assert call(server, "GET", "/v1/sandboxes")[0] == 200


def test_the_documented_api_round_trip(server, engine):
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"hi\n")
    status, payload, _ = call(server, "POST", "/v1/sandboxes", {"session": "task-9", "memory_mb": 512})
    assert status == 201 and set(payload) == {"id", "session", "image", "limits", "created_at", "expires_at"}
    sandbox = payload["id"]
    assert payload["limits"]["memory_mb"] == 512 and payload["limits"]["network"] == "none"
    status, listing, _ = call(server, "GET", "/v1/sandboxes")
    assert status == 200 and [item["id"] for item in listing["sandboxes"]] == [sandbox]
    assert {"id", "session", "created_at", "last_used_at", "image"} <= set(listing["sandboxes"][0])
    assert call(server, "GET", "/v1/sandboxes?session=other")[1] == {"sandboxes": []}
    assert call(server, "GET", f"/v1/sandboxes/{sandbox}")[1]["session"] == "task-9"
    status, result, _ = call(server, "POST", f"/v1/sandboxes/{sandbox}/exec", {"command": "echo hi", "timeout": 5})
    assert status == 200 and result["stdout"] == "hi\n" and result["exit_code"] == 0
    assert {"exit_code", "stdout", "stderr", "truncated", "timed_out", "duration_ms"} <= set(result)
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"F\t2\nok")
    status, result, _ = call(server, "GET", f"/v1/sandboxes/{sandbox}/files?path=/workspace/a%20b.txt")
    assert status == 200 and result["path"] == "/workspace/a b.txt" and result["content"] == "ok"
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, str(len(stdin)).encode())
    status, result, _ = call(server, "PUT", f"/v1/sandboxes/{sandbox}/files?path=x/y.bin", raw=b"\0" * 100)
    assert status == 200 and result == {"path": "/workspace/x/y.bin", "size": 100}
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0, b"\x1f\x8bfake")
    status, body, response = call(server, "GET", f"/v1/sandboxes/{sandbox}/archive")
    assert status == 200 and body == b"\x1f\x8bfake" and response.getheader("Content-Type") == "application/gzip"
    engine.exec_handler = lambda argv, stdin: sr.RunResult(0)
    status, result, _ = call(server, "PUT", f"/v1/sandboxes/{sandbox}/archive",
                             raw=make_tar([tar_file("n.txt", b"new")]))
    assert status == 200 and result["files"] == 1
    status, result, _ = call(server, "POST", f"/v1/sandboxes/{sandbox}/interrupt", {})
    assert status == 200 and result["ok"]
    assert call(server, "DELETE", f"/v1/sandboxes/{sandbox}")[0] == 204
    assert call(server, "DELETE", f"/v1/sandboxes/{sandbox}")[0] == 204  # idempotent
    status, payload, _ = call(server, "POST", f"/v1/sandboxes/{sandbox}/exec", {"command": "ls"})
    assert status == 410 and payload == {"error": {"code": "sandbox_gone", "message": "The sandbox was removed."}}


def test_capacity_answers_429_over_http(server):
    create(server)
    create(server)
    status, payload, response = call(server, "POST", "/v1/sandboxes", {"session": "x"})
    assert status == 429 and payload["error"]["code"] == "capacity" and response.getheader("Retry-After")


def test_routes_methods_and_ids_are_strict(server):
    for method, path in (("GET", "/v1/sandboxes/../../etc"), ("GET", "/v1/sandboxes/ABC"),
                         ("GET", "/v1/sandboxes/" + "a" * 31), ("GET", "/v1/sandboxes/" + "a" * 32 + "/shell"),
                         ("GET", "/v1/other"), ("GET", "/")):
        assert call(server, method, path)[0] == 404, path
    status, _, response = call(server, "GET", "/v1/sandboxes/" + "a" * 32 + "/exec")
    assert status == 405 and response.getheader("Allow") == "POST"
    assert call(server, "PATCH", "/v1/sandboxes")[0] == 405
    assert call(server, "POST", "/healthz", body={})[0] == 405


def test_bodies_are_limited_and_validated(server):
    box = create(server)
    base = f"/v1/sandboxes/{box['id']}"
    for raw in (b"not json", b"[1,2]", b'{"command": NaN}', b'{"session": Infinity}', b"\xff\xfe"):
        status, payload, _ = call(server, "POST", base + "/exec", raw=raw)
        assert status == 400 and payload["error"]["code"] == "bad_request"
    head = f"POST {base}/exec HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
    assert raw_request(server, (head + "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n").encode()
                       ).startswith(b"HTTP/1.1 411")
    assert raw_request(server, (head + f"Content-Length: {sr.MAX_JSON_BODY + 1}\r\n\r\n").encode()
                       ).startswith(b"HTTP/1.1 413")
    assert raw_request(server, (head + "Content-Length: -5\r\n\r\n").encode()).startswith(b"HTTP/1.1 400")
    assert raw_request(server, (head + "\r\n").encode()).startswith(b"HTTP/1.1 411")
    put = f"PUT {base}/files?path=a HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
    assert raw_request(server, (put + f"Content-Length: {sr.MAX_FILE_BODY + 1}\r\n\r\n").encode()
                       ).startswith(b"HTTP/1.1 413")
    put = f"PUT {base}/archive HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
    assert raw_request(server, (put + f"Content-Length: {sr.MAX_ARCHIVE_BODY + 1}\r\n\r\n").encode()
                       ).startswith(b"HTTP/1.1 413")


def test_query_strings_are_strict(server):
    box = create(server)
    base = f"/v1/sandboxes/{box['id']}/files"
    for query in ("?path=a&path=b", "?path=%00", "?path=..%2F..%2Fetc%2Fpasswd", "?path=%ff", "?path=/etc/passwd",
                  "?path=" + "a/" * 5000):
        status, payload, _ = call(server, "GET", base + query)
        assert status == 400, query
    status, payload, _ = call(server, "PUT", base, raw=b"x")  # a file path is required
    assert status == 400 and payload["error"]["code"] == "invalid_path"


def test_slow_clients_are_disconnected(make_server):
    server = make_server(header_deadline=0.5)
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as sock:
        started = time.monotonic()
        sock.sendall(b"GET /v1/sandboxes HTTP/1.1\r\n")
        try:
            for _ in range(20):  # dribble a header byte at a time (slowloris)
                sock.sendall(b"X")
                time.sleep(0.2)
            data = sock.recv(1024)
        except OSError:
            data = b""
        assert data == b"" and time.monotonic() - started < 5


def test_excess_connections_get_a_json_503(make_server):
    server = make_server(max_connections=1, header_deadline=3)
    holder = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    try:
        holder.sendall(b"GET /v1/sandboxes HTTP/1.1\r\n")
        time.sleep(0.3)
        status, payload, response = call(server, "GET", "/v1/sandboxes")
        assert status == 503 and payload["error"]["code"] == "busy" and response.getheader("Retry-After")
    finally:
        holder.close()


def test_concurrent_transfers_are_bounded(server):
    box = create(server)
    for _ in range(sr.LARGE_TRANSFERS):
        server.transfers.acquire()
    try:
        for method, path in (("PUT", "archive"), ("GET", "archive")):  # file writes have their own pool
            status, payload, _ = call(server, method, f"/v1/sandboxes/{box['id']}/{path}",
                                      raw=b"x" if method == "PUT" else None)
            assert status == 503 and payload["error"]["code"] == "busy", path
    finally:
        for _ in range(sr.LARGE_TRANSFERS):
            server.transfers.release()


def test_internal_errors_do_not_leak_details(server, manager, monkeypatch):
    def explode(request):
        raise RuntimeError("secret internals /home/user")
    monkeypatch.setattr(manager, "create", explode)
    status, payload, _ = call(server, "POST", "/v1/sandboxes", {"session": "s"})
    assert status == 500 and payload == {"error": {"code": "internal", "message": "Internal error."}}
