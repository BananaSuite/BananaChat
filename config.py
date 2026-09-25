"""BananaChat Configuration.

Override settings via environment variables prefixed with BC_.
The internal product identifier remains "BananaChat" for compatibility.
The user-facing display name is "BananaChat".
"""

import os
import hashlib
import hmac
import secrets
import tempfile

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Networking
PORT = int(os.environ.get("BC_PORT", "8000"))
HOST = os.environ.get("BC_HOST", "127.0.0.1")
PROXY_MODE = os.environ.get("BC_PROXY_MODE", "0").strip().lower() not in ("0", "false", "no", "")
# Trust one proxy by default; increase only for a verified proxy chain.
PROXY_HOPS = max(1, int(os.environ.get("BC_PROXY_HOPS", "1")))

# Security
INSTANCE_DIR = os.environ.get("BC_INSTANCE_DIR", os.path.join(BASE_DIR, "instance"))
SECRET_KEY_FILE = os.path.join(INSTANCE_DIR, ".secret_key")
BACKGROUND_UPLOAD_DIR = os.path.join(INSTANCE_DIR, "customization", "backgrounds")


def _read_secret_key_from_env():
    env_key = os.environ.get("SECRET_KEY", "").strip()
    return env_key or None


def _verify_key_file_permissions():
    if os.name == "nt":
        return
    try:
        mode = os.stat(SECRET_KEY_FILE).st_mode & 0o777
        if mode & 0o077:
            import logging
            msg = (
                f"Secret key file {SECRET_KEY_FILE} has overly permissive permissions "
                f"(mode {mode:o}). Expected 0600. Run: chmod 600 {SECRET_KEY_FILE}"
            )
            env = os.environ.get("BC_ENV", "production").strip().lower()
            if env in ("development", "dev", "test", "testing"):
                logging.getLogger("bananachat").warning(msg)
            else:
                raise SystemExit(
                    f"FATAL: {msg}\nSet BC_ENV=development to bypass during local hacking."
                )
    except OSError:
        pass


def _read_secret_key_from_file():
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            file_key = f.read(4097).strip()
        if len(file_key) > 4096 or not file_key:
            raise ValueError("The application key file is empty or invalid; restore its original key")
        if file_key:
            _verify_key_file_permissions()
            return file_key
    except FileNotFoundError:
        pass
    return None


def _generate_and_write_secret_key(instance_dir):
    new_key = secrets.token_hex(32)
    fd, tmp_path = tempfile.mkstemp(dir=instance_dir, prefix=".secret_key_")
    try:
        os.write(fd, new_key.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SECRET_KEY_FILE)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return new_key


def _load_secret_key():
    from filelock import FileLock
    key = _read_secret_key_from_env()
    if key:
        return key
    instance_dir = os.path.dirname(SECRET_KEY_FILE)
    os.makedirs(instance_dir, mode=0o700, exist_ok=True)
    # Keep all workers on the same key during a concurrent first start.
    with FileLock(SECRET_KEY_FILE + ".lock", timeout=20, mode=0o600):
        key = _read_secret_key_from_file()
        if key:
            return key
        return _generate_and_write_secret_key(instance_dir)


SECRET_KEY = _load_secret_key()
SETUP_TOKEN = os.environ.get("BC_SETUP_TOKEN", "").strip() or hmac.new(
    SECRET_KEY.encode("utf-8"), b"initial-admin-setup", hashlib.sha256,
).hexdigest()

SESSION_COOKIE_NAME = os.environ.get("BC_SESSION_COOKIE_NAME", "bc_session")
# Default to secure cookies when running behind a reverse proxy (implies HTTPS).
# Set BC_SECURE_COOKIES=0 to disable (e.g. local dev without TLS).
SECURE_COOKIES = os.environ.get("BC_SECURE_COOKIES", "1" if PROXY_MODE else "0").strip().lower() not in ("0", "false", "no", "")
PASSWORD_HASH_METHOD = os.environ.get("BC_PASSWORD_HASH_METHOD", "auto")
MAX_BACKUP_UPLOAD_MB = max(1, int(os.environ.get("BC_MAX_BACKUP_UPLOAD_MB", "512")))
BACKGROUND_IMAGE_MAX_UPLOAD_SIZE = max(
    1024, int(os.environ.get("BC_BACKGROUND_IMAGE_MAX_MB", "4")) * 1024 * 1024
)
BACKGROUND_IMAGE_MAX_PIXELS = max(
    1, int(os.environ.get("BC_BACKGROUND_IMAGE_MAX_PIXELS", "16000000"))
)
BACKGROUND_IMAGE_MAX_DIMENSION = max(
    320, int(os.environ.get("BC_BACKGROUND_IMAGE_MAX_DIMENSION", "2560"))
)
CHAT_MAX_FILES = max(1, int(os.environ.get("BC_CHAT_MAX_FILES", "4")))
CHAT_MAX_REQUEST_BYTES = max(
    1024 * 1024, int(os.environ.get("BC_CHAT_MAX_REQUEST_MB", "8")) * 1024 * 1024
)
CHAT_MAX_IMAGE_BYTES = max(
    1024, int(os.environ.get("BC_CHAT_MAX_IMAGE_MB", "5")) * 1024 * 1024
)
CHAT_MAX_DOCUMENT_BYTES = max(
    1024, int(os.environ.get("BC_CHAT_MAX_DOCUMENT_MB", "5")) * 1024 * 1024
)
CHAT_MAX_TEXT_BYTES = max(
    1024, int(os.environ.get("BC_CHAT_MAX_TEXT_KB", "1024")) * 1024
)
CHAT_MAX_EXTRACTED_CHARS = max(
    1000, int(os.environ.get("BC_CHAT_MAX_EXTRACTED_CHARS", "200000"))
)
CHAT_MAX_IMAGE_PIXELS = max(
    1_000_000, int(os.environ.get("BC_CHAT_MAX_IMAGE_PIXELS", "20000000"))
)
CHAT_MAX_SESSION_ATTACHMENT_BYTES = max(
    CHAT_MAX_REQUEST_BYTES,
    int(os.environ.get("BC_CHAT_MAX_SESSION_ATTACHMENT_MB", "40")) * 1024 * 1024,
)
CHAT_MAX_CONTEXT_CHARS = max(
    1000, int(os.environ.get("BC_CHAT_MAX_CONTEXT_CHARS", "300000"))
)
CHAT_MAX_CONTEXT_IMAGES = max(
    1, int(os.environ.get("BC_CHAT_MAX_CONTEXT_IMAGES", "8"))
)

# Database
DATABASE_PATH = os.environ.get(
    "BC_DATABASE_PATH",
    os.path.join(INSTANCE_DIR, "bananachat.db"),
)

# Ollama
OLLAMA_BASE_URL = os.environ.get("BC_OLLAMA_URL", "http://127.0.0.1:11434")
# Bearer credential for an authenticated Ollama-compatible proxy or cloud API.
# A local Ollama server does not enforce authentication itself.
OLLAMA_API_KEY = os.environ.get("BC_OLLAMA_API_KEY", "").strip()
# Interval in seconds between Ollama model sync polls
OLLAMA_SYNC_INTERVAL = int(os.environ.get("BC_OLLAMA_SYNC_INTERVAL", "60"))
# Compute snapshot collection interval (seconds)
COMPUTE_SNAPSHOT_INTERVAL = int(os.environ.get("BC_COMPUTE_SNAPSHOT_INTERVAL", "30"))
# How long to keep models loaded in Ollama after inference (seconds).
# 0 = unload immediately, -1 = keep forever, positive = seconds to keep alive.
KEEP_ALIVE = int(os.environ.get("BC_KEEP_ALIVE", "0"))
# Directory where Ollama stores model weights (for disk space checks).
# Set to empty string to disable the disk space guard.
OLLAMA_MODEL_DIR = os.environ.get("BC_OLLAMA_MODEL_DIR", os.path.expanduser("~/.ollama/models"))
# Minimum free disk space (GB) required before a pull is accepted. 0 = no check.
MIN_FREE_DISK_GB = float(os.environ.get("BC_MIN_FREE_DISK_GB", "2"))
# Image generation controls.
IMAGE_GENERATION_RPM = max(0, int(os.environ.get("BC_IMAGE_GENERATION_RPM", "6")))
IMAGE_CREDITS_PER_GENERATION = max(
    0.0, float(os.environ.get("BC_IMAGE_CREDITS_PER_GENERATION", "5"))
)

IMAGE_BACKEND = os.environ.get("BC_IMAGE_BACKEND", "disabled").strip().lower()
if IMAGE_BACKEND not in ("disabled", "comfyui"):
    IMAGE_BACKEND = "disabled"
COMFYUI_URL = os.environ.get("BC_COMFYUI_URL", "http://127.0.0.1:8188").strip()
COMFYUI_TIMEOUT = max(
    1.0, min(60.0, float(os.environ.get("BC_COMFYUI_TIMEOUT", "30")))
)
COMFYUI_GENERATION_TIMEOUT = max(
    1.0, min(2400.0, float(os.environ.get("BC_COMFYUI_GENERATION_TIMEOUT", "600")))
)
COMFYUI_QUEUE_TIMEOUT = max(
    1.0, min(600.0, float(os.environ.get("BC_COMFYUI_QUEUE_TIMEOUT", "300")))
)
COMFYUI_POLL_INTERVAL = max(
    0.05, float(os.environ.get("BC_COMFYUI_POLL_INTERVAL", "1"))
)
COMFYUI_SAMPLER = os.environ.get("BC_COMFYUI_SAMPLER", "euler").strip() or "euler"
COMFYUI_SCHEDULER = os.environ.get("BC_COMFYUI_SCHEDULER", "normal").strip() or "normal"
COMFYUI_STEPS = min(150, max(1, int(os.environ.get("BC_COMFYUI_STEPS", "20"))))
COMFYUI_CFG = min(30.0, max(0.0, float(os.environ.get("BC_COMFYUI_CFG", "7"))))
# End-to-end synchronous request budget used by Gunicorn. It includes queueing,
# generation, bounded network operations, cancellation/cleanup, and margin.
IMAGE_REQUEST_TIMEOUT = (
    COMFYUI_QUEUE_TIMEOUT
    + COMFYUI_GENERATION_TIMEOUT
    + (COMFYUI_TIMEOUT * 5)
    + 120.0
)

# Reservations must outlive the longest valid queue + generation request. The
# configured value can extend cleanup retention but cannot shorten this floor.
IMAGE_CREDIT_RESERVATION_TTL = max(
    COMFYUI_GENERATION_TIMEOUT + 360.0,
    float(os.environ.get("BC_IMAGE_CREDIT_RESERVATION_TTL", "1200")),
)
IMAGE_CREDIT_RESERVATION_HEARTBEAT = max(
    5.0,
    min(
        IMAGE_CREDIT_RESERVATION_TTL / 3.0,
        float(os.environ.get("BC_IMAGE_CREDIT_RESERVATION_HEARTBEAT", "30")),
    ),
)

# Optional web-to-compute checkpoint download agent. Both values must be set;
# services.checkpoint_agent performs strict URL and mode-0600 token validation.
CHECKPOINT_AGENT_URL = os.environ.get("BC_CHECKPOINT_AGENT_URL", "").strip()
CHECKPOINT_AGENT_TOKEN_FILE = os.environ.get(
    "BC_CHECKPOINT_AGENT_TOKEN_FILE", ""
).strip()
CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE = os.environ.get(
    "BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE", "0"
).strip().lower() in ("1", "true", "yes")

# Inference Server Health
# When the Ollama server is remote (not localhost), the platform can detect
# outages and either shut down entirely or fall back to a local Ollama instance.
#   "shutdown": show an outage page to all users until the server recovers (default)
#   "fallback", redirect inference to http://127.0.0.1:11434 while the primary is down
INFERENCE_OUTAGE_MODE = os.environ.get("BC_INFERENCE_OUTAGE_MODE", "shutdown").strip().lower()
if INFERENCE_OUTAGE_MODE not in ("shutdown", "fallback"):
    INFERENCE_OUTAGE_MODE = "shutdown"
# Seconds between health pings to the inference server
INFERENCE_HEALTH_INTERVAL = int(os.environ.get("BC_INFERENCE_HEALTH_INTERVAL", "15"))
# Number of consecutive failures before declaring an outage
INFERENCE_HEALTH_FAILURES = int(os.environ.get("BC_INFERENCE_HEALTH_FAILURES", "3"))
# Local Ollama URL for fallback mode
INFERENCE_FALLBACK_URL = os.environ.get("BC_INFERENCE_FALLBACK_URL", "http://127.0.0.1:11434")

# Logging
_VALID_LOGGING_LEVELS = {"off", "minimal", "medium", "verbose", "debug"}
LOGGING_LEVEL = os.environ.get("BC_LOGGING_LEVEL", "verbose").strip().lower()
if LOGGING_LEVEL not in _VALID_LOGGING_LEVELS:
    import sys
    print(
        f"WARNING: BC_LOGGING_LEVEL={LOGGING_LEVEL!r} is not valid; falling back to 'verbose'.",
        file=sys.stderr,
    )
    LOGGING_LEVEL = "verbose"

LOG_FILE = os.environ.get(
    "BC_LOG_FILE",
    os.path.join(INSTANCE_DIR, "bananachat.log"),
)

# Queue
def _bounded_integer(name, default, minimum, maximum):
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


HTTP_THREADS = _bounded_integer("BC_HTTP_THREADS", 16, 8, 64)
MAX_CONCURRENT = _bounded_integer("BC_MAX_CONCURRENT", 4, 1, 128)
MAX_QUEUE_DEPTH = _bounded_integer("BC_MAX_QUEUE_DEPTH", 50, MAX_CONCURRENT, 1000)
GENERATION_TIMEOUT = _bounded_integer("BC_GENERATION_TIMEOUT", 300, 10, 1800)
INFERENCE_READ_TIMEOUT = _bounded_integer("BC_INFERENCE_READ_TIMEOUT", 30, 5, 60)
CHAT_MAX_RESPONSE_BYTES = _bounded_integer("BC_CHAT_MAX_RESPONSE_KB", 1024, 16, 8192) * 1024
CHAT_MAX_HISTORY_MESSAGES = _bounded_integer("BC_CHAT_MAX_HISTORY_MESSAGES", 100, 2, 500)
MAX_OUTPUT_TOKENS = _bounded_integer("BC_MAX_OUTPUT_TOKENS", 8192, 128, 65536)
NO_HISTORY_TTL_HOURS = max(1, int(os.environ.get("BC_NO_HISTORY_TTL_HOURS", "24")))

# Remote Worker Pool
# Set to 1/true to allow personal-PC worker daemons to handle inference jobs.
# When disabled (default), all inference runs on the local Ollama instance.
WORKERS_ENABLED = os.environ.get("BC_WORKERS_ENABLED", "0").strip().lower() in (
    "1", "true", "yes",
)

# User-facing display name (internal product identifier remains "BananaChat").
DISPLAY_NAME = "BananaChat"

# Point this at the corresponding source when deploying a modified version.
SOURCE_CODE_URL = os.environ.get("BC_SOURCE_URL", "https://github.com/BananaSuite/BananaChat").strip()
from urllib.parse import urlsplit as _source_urlsplit
_source_parts = _source_urlsplit(SOURCE_CODE_URL)
if _source_parts.scheme not in {"http", "https"} or not _source_parts.netloc or _source_parts.username:
    raise ValueError("BC_SOURCE_URL must be an absolute HTTP(S) source URL")
