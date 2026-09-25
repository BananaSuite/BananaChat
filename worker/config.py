"""BananaChat Worker daemon configuration.

All settings are read from environment variables so the daemon can run as a
systemd service, a Windows user task, a macOS launchd agent, or a plain
script with a .env file.
"""

import os
import platform
import socket
from urllib.parse import urlsplit

# Environment file

# systemd reads EnvironmentFile= for us, but launchd has no equivalent and
# would need the token written into the plist, which sits world-readable in
# ~/Library/LaunchAgents. Reading the file here instead keeps the token in one
# 0600 file on every platform. Anything already exported wins, so a systemd
# unit or an exported shell variable still overrides the file.
def default_env_file() -> str:
    """Where the worker keeps its settings, per platform convention.

    service.py builds the same path when it writes the file. They are two
    short functions rather than one shared import because the worker modules
    are imported both as a package and as loose modules on sys.path, and a
    cross-import between them resolves differently in the two cases.
    test_worker_service keeps them honest.
    """
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BananaChat", "bananachat-worker.env")
    return os.path.expanduser("~/.config/bananachat-worker.env")


DEFAULT_ENV_FILE = default_env_file()


def load_env_file(path=None):
    """Populate os.environ from a KEY=VALUE file, without overriding exports.

    Returns the path that was read, or None. A malformed line is skipped
    rather than failing startup: a stray line in a hand-edited file should not
    take the worker down.
    """
    path = path or os.environ.get("BC_WORKER_ENV_FILE") or DEFAULT_ENV_FILE
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)
    return path


ENV_FILE_LOADED = load_env_file()

# Server connection

# Full URL of the BananaChat platform server, e.g. https://ai.example.com
SERVER_URL = os.environ.get("BC_SERVER_URL", "").rstrip("/")

# Worker token issued by the admin panel (Admin → Workers → Add Worker).
WORKER_TOKEN = os.environ.get("BC_WORKER_TOKEN", "").strip()

# Human-readable name shown in the admin panel.  Defaults to the machine
# hostname so multiple workers on the same network are easy to distinguish.
WORKER_NAME = os.environ.get("BC_WORKER_NAME", socket.gethostname())

# Ollama

# Base URL of the local Ollama instance this worker will run jobs on.
OLLAMA_HOST = os.environ.get("BC_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

# Path to the ollama binary (or just "ollama" if it's on PATH).
OLLAMA_BINARY = os.environ.get("BC_OLLAMA_BINARY", "ollama")

# Seconds of job inactivity before the daemon stops Ollama to free VRAM.
# Set to 0 to never stop Ollama automatically.
OLLAMA_IDLE_TIMEOUT = int(os.environ.get("BC_OLLAMA_IDLE_TIMEOUT", "600"))

INFERENCE_READ_TIMEOUT = max(5, min(45, int(os.environ.get("BC_INFERENCE_READ_TIMEOUT", "30"))))
GENERATION_TIMEOUT = max(10, min(1800, int(os.environ.get("BC_GENERATION_TIMEOUT", "300"))))

# keep_alive value sent to Ollama (0 = unload model after each inference).
OLLAMA_KEEP_ALIVE = int(os.environ.get("BC_OLLAMA_KEEP_ALIVE", "0"))

# Activity / resource thresholds

# Seconds of keyboard+mouse inactivity before we consider the user "idle".
# When idle, inference runs at full priority and Ollama is kept resident.
IDLE_THRESHOLD_SECONDS = int(os.environ.get("BC_IDLE_THRESHOLD", "300"))   # 5 min

# Seconds of inactivity that still counts as "light" use (moderate priority).
LIGHT_THRESHOLD_SECONDS = int(os.environ.get("BC_LIGHT_THRESHOLD", "30"))

# GPU utilisation (%) above which we consider the user "gaming / rendering".
# When in this state the daemon will not start new inference jobs.
GPU_GAMING_THRESHOLD = int(os.environ.get("BC_GPU_GAMING_THRESHOLD", "70"))

# GPU utilisation (%) above which we consider the user "actively using GPU"
# (inference still runs but at reduced process priority).
GPU_ACTIVE_THRESHOLD = int(os.environ.get("BC_GPU_ACTIVE_THRESHOLD", "50"))

# Fraction of VRAM the worker may use when the user is actively gaming.
# 0.0 = disable inference entirely while gaming (recommended).
# Positive values let you run small models concurrently (advanced, risky).
GPU_GAMING_VRAM_FRACTION = float(os.environ.get("BC_GPU_GAMING_VRAM_FRACTION", "0.0"))

# Polling / timing

# Seconds between consecutive /jobs/poll requests when idle.
# The actual long-poll window is always ~28 s; this adds a gap between polls.
POLL_GAP_SECONDS = float(os.environ.get("BC_POLL_GAP", "0.5"))

# Seconds between heartbeat POSTs to the server.
HEARTBEAT_INTERVAL = float(os.environ.get("BC_HEARTBEAT_INTERVAL", "10.0"))

# Seconds between activity / GPU reads.
ACTIVITY_CHECK_INTERVAL = float(os.environ.get("BC_ACTIVITY_CHECK_INTERVAL", "5.0"))

# Logging

LOG_LEVEL = os.environ.get("BC_WORKER_LOG_LEVEL", "INFO").upper()

# Optional log file. systemd captures stdout in the journal and the macOS
# agent redirects it, but a Windows Task Scheduler job has nowhere to put it,
# so the worker writes its own file when this is set.
LOG_FILE = os.environ.get("BC_WORKER_LOG_FILE", "").strip()

# Bytes per log file and how many to keep, when LOG_FILE is set.
LOG_MAX_BYTES = max(64 * 1024, int(os.environ.get("BC_WORKER_LOG_MAX_BYTES", str(5 * 1024 * 1024))))
LOG_BACKUPS = max(0, min(20, int(os.environ.get("BC_WORKER_LOG_BACKUPS", "3"))))

# Validation helper

def validate():
    """Raise ValueError if required settings are missing."""
    if not SERVER_URL:
        raise ValueError(
            "BC_SERVER_URL is not set. "
            "Example: BC_SERVER_URL=https://ai.example.com"
        )
    endpoint = urlsplit(SERVER_URL)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("BC_SERVER_URL must be an HTTP(S) server URL without embedded credentials, query, or fragment")
    if endpoint.scheme == "http" and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"} and os.environ.get("BC_WORKER_ALLOW_HTTP") != "1":
        raise ValueError("Use HTTPS for the worker token. On an independently secured private network, explicitly set BC_WORKER_ALLOW_HTTP=1 to use HTTP.")
    if not WORKER_TOKEN:
        raise ValueError(
            "BC_WORKER_TOKEN is not set. "
            "Create a worker in Admin → Workers, then copy the token here."
        )
