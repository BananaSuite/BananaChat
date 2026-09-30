"""Git import and patch export against the real sandbox runner, Docker and the agent image.

The web server, the agent loop, the runner (``compute/sandbox_runner.py``) and the in-sandbox Git scripts
are the real ones; only the model (a scripted fake) and the repository host (a local HTTPS server) are
imitated. The image is ``bananachat-agent:1`` from ``compute/sandbox-image`` (override with
``BC_SANDBOX_AGENT_IMAGE``). Skipped without Docker or the image unless ``BC_SANDBOX_REQUIRE_AGENT_IMAGE=1``
(the CI ``sandbox`` job builds the image and sets it). Every container carries a per-run instance label and
is removed afterwards.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import shutil
import socket
import subprocess
import tarfile
import threading
import types

import pytest

from tests.app.conftest import Browser
from tests.app.test_agents import add_user, call, configure, start_ok, steps, wait_status  # noqa: F401
from tests.app.test_agents import fast_loop  # noqa: F401 - autouse fixture
from tests.app.test_agents_git import net, tls_files  # noqa: F401 - fixtures

IMAGE = os.environ.get("BC_SANDBOX_AGENT_IMAGE") or "bananachat-agent:1"
DOCKER = shutil.which("docker")
REQUIRED = os.environ.get("BC_SANDBOX_REQUIRE_AGENT_IMAGE") == "1"
INSTANCE = "pytest-git-" + secrets.token_hex(4)


def _ready() -> str:
    if not DOCKER:
        return "docker is not installed"
    try:
        if subprocess.run([DOCKER, "info"], capture_output=True, timeout=30).returncode != 0:
            return "the Docker daemon is not reachable"
        if subprocess.run([DOCKER, "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode != 0:
            return f"{IMAGE} is not built (docker build -t {IMAGE} compute/sandbox-image)"
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"docker failed: {error}"
    return ""


REASON = _ready()
if REASON and not REQUIRED:
    pytest.skip(f"Agent image tests skipped: {REASON}", allow_module_level=True)

sr = pytest.importorskip("compute.sandbox_runner")


def _cleanup():
    listed = subprocess.run([DOCKER, "ps", "-aq", "--filter", f"label={sr.LABEL_RUNNER}={INSTANCE}"],
                            capture_output=True, text=True, timeout=60)
    if listed.stdout.split():
        subprocess.run([DOCKER, "rm", "-f", *listed.stdout.split()], capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def docker_runner(tmp_path_factory):
    if REASON:
        pytest.fail(f"BC_SANDBOX_REQUIRE_AGENT_IMAGE=1 but {REASON}")
    token_file = tmp_path_factory.mktemp("git-runner") / "token"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environ = {
        "BC_SANDBOX_TOKEN_FILE": str(token_file), "BC_SANDBOX_PORT": str(port), "BC_SANDBOX_ENGINE": "docker",
        "BC_SANDBOX_IMAGES": IMAGE,
        # The daemon here is usually rootful; the test opts in explicitly, as an operator would have to.
        "BC_SANDBOX_ALLOW_ROOTFUL": "1", "BC_SANDBOX_INSTANCE": INSTANCE, "BC_SANDBOX_MAX": "2",
        "BC_SANDBOX_MEMORY_MB": "768", "BC_SANDBOX_PIDS": "128", "BC_SANDBOX_WORKSPACE_MB": "128",
        "BC_SANDBOX_EXEC_TIMEOUT": "120", "BC_SANDBOX_OUTPUT_KB": "64",
    }
    server = sr.create_server(environ)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(server=server, url=f"http://127.0.0.1:{port}", token_file=token_file)
    finally:
        server.shutdown()
        server.server_close()
        server.manager.shutdown()
        _cleanup()


def _client(docker_runner):
    from bananachat.services.agents import runner as runner_mod

    config = types.SimpleNamespace(agents_runner_url=docker_runner.url,
                                   agents_runner_token_file=str(docker_runner.token_file), agents_runner_token="",
                                   agents_runner_allow_insecure_tailscale=False)
    return runner_mod.Runner(config)


def test_git_works_under_the_sandbox_restrictions(docker_runner):
    client = _client(docker_runner)
    box = client.create("pytest-git")
    try:
        result = client.exec(box["id"], "git --version; id -u; touch /usr/x 2>&1 || true; "
                                        "cd /workspace && git init -q r && cd r && echo hi > a && git add a && "
                                        "git commit -qm first && git log --format=%s", timeout=60)
        out = result["stdout"]
        assert result["exit_code"] == 0, result
        assert out.startswith("git version ") and "\n1000\n" in out and "Read-only file system" in out
        assert out.rstrip().endswith("first")
    finally:
        client.delete(box["id"])


def _repository() -> tuple[dict, bytes]:
    files = {
        "README.md": b"# Demo\n", "src/app.py": b"print('hi')\n", "keep.log": b"tracked but ignored\n",
        ".gitignore": b"build/\n*.log\n", "data.bin": bytes(range(256)),
        # An archive from a hostile server may carry a .git folder: it must be dropped, never used.
        ".git/config": b"[core]\n\tfsmonitor = touch /workspace/pwned-import\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"demo-main/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return files, buffer.getvalue()


AGENT_CHANGES = (
    "cd /workspace/demo && rm src/app.py && printf 'new file\\n' > NEW.txt && mkdir -p build && echo x > build/out "
    "&& echo y > debug.log && head -c 3000000 /dev/urandom | base64 > big.txt && printf '\\000\\001\\377' > data.bin "
    "&& echo changed >> keep.log && git add -A && git commit -qm 'agent commit' && echo committed")
HOSTILE_CONFIG = (
    "cd /workspace/demo && git config core.fsmonitor 'touch /workspace/pwned-fsmonitor' "
    "&& git config diff.external 'touch /workspace/pwned-diff' "
    "&& git config filter.evil.clean 'touch /workspace/pwned-filter' "
    "&& git config diff.evil.textconv 'touch /workspace/pwned-textconv' "
    "&& printf '* filter=evil diff=evil\\n' > .git/info/attributes "
    "&& mkdir -p /workspace/.config/git && printf '[core]\\n\\tfsmonitor = touch /workspace/pwned-xdg\\n' "
    "> /workspace/.config/git/config "
    "&& printf '[core]\\n\\tfsmonitor = touch /workspace/pwned-global\\n' > /workspace/.gitconfig "
    "&& printf 'unstaged\\n' >> README.md && echo configured")
# A snapshot that runs no Git (Git would run what the agent configured): every file of .git and of the
# working tree, and the workspace's top level.
STATE = ("cd /workspace/demo && find .git -type f | LC_ALL=C sort | xargs sha256sum | sha256sum && "
         "find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum && "
         "ls -a /workspace")


def test_import_and_patch_export_end_to_end(make_app, fake_ollama, docker_runner, net, tmp_path):
    files, archive = _repository()
    net.server.archive("/octo/demo/tar.gz/main", archive)
    app = make_app(AGENTS_RUNNER_URL=docker_runner.url, AGENTS_RUNNER_TOKEN_FILE=str(docker_runner.token_file))
    configure(app, git_enabled=True, command_timeout=120)
    add_user(app, "alice", allowed=True)
    browser = Browser(app)
    browser.login("alice")
    fake_ollama.tool_script = [
        call("write_file", path="demo/README.md", content="# Demo\n\nImproved by the agent.\n"),
        call("bash", command=AGENT_CHANGES, timeout=90),
        call("bash", command=HOSTILE_CONFIG),
        call("finish", summary="Changed the demo."),
    ]
    task_id = start_ok(browser, "Improve the demo", repo_url="https://github.com/octo/demo", repo_ref="main")
    row = wait_status(app, task_id, "finished", timeout=180)
    log = steps(app, task_id)
    results = [step["tool_result"] for step in log if step["kind"] == "tool"]
    assert "committed" in results[1] and "configured" in results[2], results
    events = browser.get(f"/agents/{task_id}/events").get_json()
    assert events["task"]["repository"]["git"] is True, events["task"]
    client = _client(docker_runner)
    sandbox = row["sandbox_id"]
    before = client.exec(sandbox, STATE, timeout=60)["stdout"]
    assert "pwned" not in before and ".bananachat-import-" not in before

    response = browser.get(f"/agents/{task_id}/workspace/patch")
    assert response.status_code == 200, response.get_data(as_text=True)
    patch = response.data
    assert response.headers["Content-Disposition"] == 'attachment; filename="demo-changes.patch"'
    assert len(patch) > 3 * 1024 * 1024  # more than three 1 MB parts

    # Exporting ran no program the agent configured and left the agent's repository exactly as it was.
    after = client.exec(sandbox, STATE, timeout=60)["stdout"]
    assert after == before
    assert "pwned" not in after and ".bananachat-export-" not in after

    # The patch applies to the imported state and reproduces the working tree (untracked files included,
    # ignored ones not; the agent's own commit and the uncommitted edit both count).
    text = patch.decode("utf-8", "replace")
    assert "diff --git a/src/app.py b/src/app.py\ndeleted file mode" in text
    assert "b/NEW.txt" in text and "b/big.txt" in text and "GIT binary patch" in text
    assert "build/out" not in text and "debug.log" not in text and ".git/" not in text
    assert "a/keep.log" in text  # tracked from the import although .gitignore matches it
    work = tmp_path / "check"
    work.mkdir()
    for name, data in files.items():
        if not name.startswith(".git/"):
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_bytes(data)
    environ = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", "init", "-q", "."], cwd=work, check=True, env=environ)
    subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", "-"], cwd=work, input=patch, check=True,
                   env=environ)
    assert (work / "README.md").read_bytes() == b"# Demo\n\nImproved by the agent.\nunstaged\n"
    assert not (work / "src/app.py").exists() and (work / "NEW.txt").read_bytes() == b"new file\n"
    assert (work / "data.bin").read_bytes() == b"\x00\x01\xff"
    assert (work / "keep.log").read_bytes() == b"tracked but ignored\nchanged\n"
    remote = client.exec(sandbox, "sha256sum /workspace/demo/big.txt", timeout=60)["stdout"].split()[0]
    assert hashlib.sha256((work / "big.txt").read_bytes()).hexdigest() == remote
    assert not (work / "build").exists() and not (work / "debug.log").exists()
