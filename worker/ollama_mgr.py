"""Ollama process manager for the worker daemon.

Handles starting, stopping, and health-checking the local Ollama instance.
On Windows and Linux, Ollama is launched as a subprocess and its PID is
tracked so we can reprioritise it alongside the worker process.

If Ollama is already running when the worker starts (e.g. the user has it
open in a system tray), we adopt it without spawning a second copy.
"""

import json
import logging
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request

import config

_logger = logging.getLogger("bananachat.worker.ollama")
_OS = platform.system()

_ollama_proc: subprocess.Popen | None = None   # Our managed child process


# Health check

def is_alive() -> bool:
    """Return True if the local Ollama instance is reachable."""
    try:
        req = urllib.request.Request(
            config.OLLAMA_HOST + "/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_version() -> str | None:
    """Return the Ollama version string, or None."""
    try:
        req = urllib.request.Request(
            config.OLLAMA_HOST + "/api/version",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read(4 * 1024 * 1024 + 1))
            return data.get("version")
    except Exception:
        return None


# Model listing

def list_models() -> list[str]:
    """Return the ollama_names of all locally installed models."""
    try:
        req = urllib.request.Request(
            config.OLLAMA_HOST + "/api/tags",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(4 * 1024 * 1024 + 1))
            return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception as exc:
        _logger.debug("Could not list Ollama models: %s", exc)
        return []


# Start / stop

def get_managed_pid() -> int | None:
    """Return the PID of the Ollama process we launched, if any."""
    if _ollama_proc is not None and _ollama_proc.poll() is None:
        return _ollama_proc.pid
    return None


def ensure_running() -> bool:
    """Make sure Ollama is up.  Start it if needed.  Returns True on success."""
    global _ollama_proc

    if is_alive():
        return True

    # Our managed child may have died: clean up the reference
    if _ollama_proc is not None and _ollama_proc.poll() is not None:
        _logger.info("Managed Ollama process exited (rc=%d), restarting", _ollama_proc.returncode)
        _ollama_proc = None

    if _ollama_proc is None:
        _logger.info("Starting Ollama via '%s serve'", config.OLLAMA_BINARY)
        env = os.environ.copy()
        # Point Ollama at the configured host/port
        if ":" in config.OLLAMA_HOST.rsplit(":", 1)[-1]:
            host_part  = config.OLLAMA_HOST.split("//", 1)[-1]   # strip scheme
            env["OLLAMA_HOST"] = host_part
        try:
            if _OS == "Windows":
                # Don't create a console window
                _ollama_proc = subprocess.Popen(
                    [config.OLLAMA_BINARY, "serve"],
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                _ollama_proc = subprocess.Popen(
                    [config.OLLAMA_BINARY, "serve"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except FileNotFoundError:
            _logger.error(
                "Ollama binary '%s' not found. "
                "Install Ollama or set BC_OLLAMA_BINARY to the full path.",
                config.OLLAMA_BINARY,
            )
            return False

    # Wait for Ollama to become responsive (up to 30 s)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if is_alive():
            _logger.info("Ollama is ready (version=%s)", get_version())
            return True
        time.sleep(1)

    _logger.error("Ollama did not become responsive within 30 s")
    return False


def stop_managed():
    """Stop the Ollama process we launched (if any).  Does not kill user-started instances."""
    global _ollama_proc
    if _ollama_proc is None:
        return
    if _ollama_proc.poll() is None:
        _logger.info("Stopping managed Ollama process (pid %d)", _ollama_proc.pid)
        try:
            if _OS == "Windows":
                _ollama_proc.terminate()
            else:
                import signal as _sig
                _ollama_proc.send_signal(_sig.SIGTERM)
            _ollama_proc.wait(timeout=10)
        except Exception as exc:
            _logger.warning("Could not stop Ollama cleanly: %s", exc)
            _ollama_proc.kill()
    _ollama_proc = None


# Inference helpers

def generate_stream(model: str, messages: list, options: dict | None = None):
    """Yield (content_delta, done, usage) from Ollama's /api/chat endpoint.

    Wire-compatible with services/ollama.generate_chat_stream() so the caller
    can treat both identically.
    """
    url  = config.OLLAMA_HOST + "/api/chat"
    body = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    if options:
        body["options"] = options

    data = json.dumps(body).encode("utf-8")
    req  = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=config.INFERENCE_READ_TIMEOUT)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot connect to local Ollama: {exc.reason}") from exc

    deadline = time.monotonic() + config.GENERATION_TIMEOUT
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Inference reached its time limit")
            raw_line = resp.readline(128 * 1024 + 1)
            if not raw_line:
                raise RuntimeError("Ollama ended without a completion marker")
            if len(raw_line) > 128 * 1024:
                raise RuntimeError("Ollama returned an oversized stream record")
            line = raw_line.strip().decode("utf-8", errors="replace")
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("Ollama returned an invalid stream record") from error
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            done    = chunk.get("done", False)
            content = chunk.get("message", {}).get("content", "")
            usage   = {}
            if done:
                usage = {
                    "prompt_tokens":     chunk.get("prompt_eval_count", 0),
                    "completion_tokens": chunk.get("eval_count", 0),
                    "finish_reason": chunk.get("done_reason", "stop"),
                }
            yield content, done, usage
            if done:
                return
    finally:
        try:
            resp.close()
        except Exception:
            pass
