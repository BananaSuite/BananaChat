"""The agent loop against the real sandbox runner (``compute/sandbox_runner.py``) with a recording engine.

The runner's HTTP server, sandbox manager and file scripts are the real ones;
only the container engine is imitated (``tests/test_sandbox_runner.FakeEngine``)
with a small in-memory workspace, so this checks the web server's client
against the runner's actual API: create, exec, read/write files, missing
files, interrupt on Stop and deletion.
"""

from __future__ import annotations

import os
import posixpath
import threading

import pytest

from tests.app.conftest import Browser
from tests.app.test_agents import (  # noqa: F401 - fast_loop is an autouse fixture
    add_user, call, configure, error_key, fast_loop, start_ok, steps, wait_for, wait_status)

sr = pytest.importorskip("compute.sandbox_runner")
runner_tests = pytest.importorskip("tests.test_sandbox_runner")


class WorkspaceEngine(runner_tests.FakeEngine):
    """The recording engine plus an in-memory /workspace and interruptible commands."""

    def __init__(self):
        super().__init__()
        self.files: dict[str, bytes] = {}
        self.shell_commands: list[str] = []
        self.hang = False
        self.release = threading.Event()
        self.kills = 0
        self.exec_handler = self.handle

    def answer(self, args, stdin, timeout):
        if args[0] == "exec" and sr.KILL_SCRIPT in args:
            self.kills += 1
            self.release.set()
        return super().answer(args, stdin, timeout)

    def _is_dir(self, path):
        return path == "/workspace" or any(name.startswith(path + "/") for name in self.files)

    def handle(self, argv, stdin):
        if sr.EXEC_WRAPPER in argv:
            command = argv[-1]
            self.shell_commands.append(command)
            if self.hang:
                self.release.wait(20)
                return sr.RunResult(137, b"", b"", 0, 0)
            out = f"ran: {command}\n".encode()
            return sr.RunResult(0, out, b"", len(out), 0)
        if sr.READ_SCRIPT in argv:
            path = posixpath.normpath(argv[argv.index(sr.READ_SCRIPT) + 2])
            if path in self.files:
                data = self.files[path]
                return sr.RunResult(0, b"F\t%d\n" % len(data) + data)
            if self._is_dir(path):
                names = {}
                for name, data in self.files.items():
                    if name.startswith(path + "/"):
                        rest = name[len(path) + 1:].split("/")
                        names[rest[0]] = ("d", 0) if len(rest) > 1 else ("f", len(data))
                body = b"".join(f"{kind}\t{size}\t{name}".encode() + b"\0" for name, (kind, size) in names.items())
                return sr.RunResult(0, b"D\n" + body)
            return sr.RunResult(92)
        if sr.WRITE_SCRIPT in argv:
            path = posixpath.normpath(argv[argv.index(sr.WRITE_SCRIPT) + 2])
            self.files[path] = stdin or b""
            return sr.RunResult(0, str(len(stdin or b"")).encode())
        return sr.RunResult(0)


@pytest.fixture
def real_runner():
    engine = WorkspaceEngine()
    manager = sr.SandboxManager(runner_tests.make_config(), engine)
    manager.preflight()
    server = sr.RunnerServer(("127.0.0.1", 0), token=runner_tests.TOKEN, manager=manager)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield engine, server
    engine.release.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_env(make_app, real_runner, tmp_path):
    engine, server = real_runner
    token = tmp_path / "runner.token"
    token.write_text(runner_tests.TOKEN)
    os.chmod(token, 0o600)
    app = make_app(AGENTS_RUNNER_URL=f"http://127.0.0.1:{server.server_port}", AGENTS_RUNNER_TOKEN_FILE=str(token))
    configure(app)
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    return app, browser, engine


def test_agent_task_against_the_real_runner(real_env, fake_ollama):
    from bananachat.services.agents import runner as runner_mod

    app, browser, engine = real_env
    with app.app_context():
        health = runner_mod.health(fresh=True)
    assert health["ok"] and health["network"] == "none" and health["rootless"] is True
    fake_ollama.tool_script = [
        call("bash", command="python -V"),
        call("write_file", path="pkg/app.py", content="print('hi')\n"),
        call("read_file", path="missing.txt"),
        call("edit_file", path="pkg/app.py", old="hi", new="there"),
        call("read_file", path="pkg/app.py"),
        call("list_files"),
        call("finish", summary="done"),
    ]
    task_id = start_ok(browser)
    row = wait_status(app, task_id, "finished")
    assert row["summary"] == "done"
    assert engine.shell_commands == ["python -V"]
    assert engine.files["/workspace/pkg/app.py"] == b"print('there')\n"
    results = {step["tool_name"] + str(index): step for index, step in enumerate(
        step for step in steps(app, task_id) if step["kind"] == "tool")}
    assert "ran: python -V" in results["bash0"]["tool_result"]
    assert "No such file or directory" in results["read_file2"]["tool_result"]
    assert "print('there')" in results["read_file4"]["tool_result"]
    assert "pkg/" in results["list_files5"]["tool_result"]
    runs = engine.commands("run")
    assert len(runs) == 1 and "--network" in runs[0] and runs[0][runs[0].index("--network") + 1] == "none"

    data = browser.get(f"/agents/{task_id}/workspace?path=/workspace/pkg").get_json()
    assert data["entries"] == [{"name": "app.py", "type": "file", "size": 15}]
    assert browser.get(f"/agents/{task_id}/workspace/file?path=/workspace/pkg/app.py").data == b"print('there')\n"
    assert browser.get(f"/agents/{task_id}/workspace?path=/workspace/nope").status_code == 404
    assert browser.fetch(f"/agents/{task_id}/delete", method="POST").status_code == 200
    assert engine.commands("rm")


def test_stop_interrupts_the_command_through_the_real_runner(real_env, fake_ollama):
    app, browser, engine = real_env
    engine.hang = True
    fake_ollama.tool_script = [call("bash", command="sleep 600")]
    task_id = start_ok(browser)
    wait_for(lambda: engine.shell_commands, message="the command")
    assert browser.fetch(f"/agents/{task_id}/stop", method="POST").status_code == 200
    row = wait_status(app, task_id, "stopped", timeout=8)
    assert error_key(app, row["error"]) == "agents.stopped"
    assert engine.kills >= 1
    assert row["sandbox_id"] and not engine.commands("rm")  # the workspace survives the interrupt
