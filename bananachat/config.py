"""Runtime configuration read from ``BC_*`` environment variables.

Every setting has a safe default, so an installation keeps working when an
update introduces a new option. Invalid values never crash the server: they
are replaced by the default (or clamped into range) and reported through
``Config.warnings`` so the operator sees them in the log.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_URL = "https://github.com/BananaSuite/BananaChat"
SUPPORTED_LANGUAGES = ("it", "en")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class _Reader:
    """Parse environment values, collecting a warning for each invalid one."""

    def __init__(self, environ):
        self.environ = environ
        self.warnings: list[str] = []

    def raw(self, name, default=""):
        value = self.environ.get(name)
        return default if value is None else value.strip()

    def text(self, name, default=""):
        return self.raw(name, default)

    def flag(self, name, default=False):
        value = self.raw(name, "").lower()
        if value == "":
            return default
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        self.warnings.append(f"{name}={value!r} is not a boolean (use 1/0, true/false, yes/no, on/off); using {int(default)}.")
        return default

    def integer(self, name, default, minimum=None, maximum=None):
        text = self.raw(name, "")
        if text == "":
            return default
        try:
            value = int(text)
        except ValueError:
            self.warnings.append(f"{name}={text!r} is not an integer; using {default}.")
            return default
        return self._clamp(name, value, minimum, maximum)

    def number(self, name, default, minimum=None, maximum=None):
        text = self.raw(name, "")
        if text == "":
            return float(default)
        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            self.warnings.append(f"{name}={text!r} is not a finite number; using {default}.")
            return float(default)
        return float(self._clamp(name, value, minimum, maximum))

    def optional_flag(self, name):
        """A boolean that may be left unset (``None``)."""
        value = self.raw(name, "").lower()
        if value == "":
            return None
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        self.warnings.append(f"{name}={value!r} is not a boolean (use 1/0, true/false, yes/no, on/off); ignoring it.")
        return None

    def choice(self, name, default, choices):
        value = self.raw(name, default).lower() or default
        if value not in choices:
            self.warnings.append(f"{name}={value!r} must be one of {', '.join(choices)}; using {default!r}.")
            return default
        return value

    def _clamp(self, name, value, minimum, maximum):
        if minimum is not None and value < minimum:
            self.warnings.append(f"{name}={value} is below the minimum {minimum}; using {minimum}.")
            return minimum
        if maximum is not None and value > maximum:
            self.warnings.append(f"{name}={value} is above the maximum {maximum}; using {maximum}.")
            return maximum
        return value


@dataclass(frozen=True)
class Config:
    # Networking
    host: str = "127.0.0.1"
    port: int = 8000
    proxy_mode: bool = False
    proxy_hops: int = 1
    environment: str = "production"
    debug: bool = False

    # Storage
    instance_dir: Path = REPO_ROOT / "instance"
    database_path: Path = REPO_ROOT / "instance" / "bananachat.db"
    log_file: Path | None = None
    logging_level: str = "verbose"
    maintenance_file: Path | None = None

    # Security
    secret_key: str = ""
    setup_token: str = ""
    session_cookie_name: str = "bc_session"
    secure_cookies: bool = False
    session_days: int = 7
    password_hash_method: str = "auto"
    min_form_seconds: float = 0.4

    # Interface
    default_language: str = "it"
    source_url: str = DEFAULT_SOURCE_URL

    # Customization uploads
    background_max_bytes: int = 4 * 1024 * 1024
    background_max_pixels: int = 16_000_000
    background_max_dimension: int = 2560

    # Chat input limits
    chat_max_files: int = 4
    chat_max_request_bytes: int = 8 * 1024 * 1024
    chat_max_image_bytes: int = 5 * 1024 * 1024
    chat_max_document_bytes: int = 5 * 1024 * 1024
    chat_max_text_bytes: int = 1024 * 1024
    chat_max_extracted_chars: int = 200_000
    chat_max_image_pixels: int = 20_000_000
    chat_max_session_attachment_bytes: int = 40 * 1024 * 1024
    chat_max_context_chars: int = 300_000
    chat_max_context_images: int = 8
    chat_max_message_bytes: int = 100 * 1024
    chat_max_response_bytes: int = 1024 * 1024
    chat_max_history_messages: int = 100

    # Inference
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_api_key: str = ""
    inference_local: bool | None = None
    ollama_sync_interval: int = 60
    keep_alive: int = 0
    ollama_model_dir: str = ""
    min_free_disk_gb: float = 2.0
    http_threads: int = 16
    max_concurrent: int = 4
    max_queue_depth: int = 50
    queue_timeout: int = 120
    generation_timeout: int = 300
    inference_read_timeout: int = 30
    first_token_timeout: int = 180
    max_output_tokens: int = 8192
    max_num_ctx: int = 32768
    min_free_memory_mb: int = 256
    compute_snapshot_interval: int = 30
    inference_outage_mode: str = "shutdown"
    inference_health_interval: int = 15
    inference_health_failures: int = 3
    inference_fallback_url: str = ""
    inference_fallback_api_key: str = ""
    workers_enabled: bool = False
    worker_claim_timeout: int = 20
    # Operator-installed provider adapter; unset keeps Claude disconnected.
    claude_extension: str = ""
    claude_code_config: str = ""

    # Retention
    no_history_ttl_hours: int = 24
    deleted_chat_retention_days: int = 30
    metrics_retention_days: int = 30

    # Image generation
    image_backend: str = "disabled"
    image_generation_rpm: int = 6
    image_credits_per_generation: float = 5.0  # the previous release's setting; image_tokens_per_generation is used
    image_tokens_per_generation: int = 5000
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_timeout: float = 30.0
    comfyui_generation_timeout: float = 600.0
    comfyui_queue_timeout: float = 300.0
    comfyui_poll_interval: float = 1.0
    comfyui_sampler: str = "euler"
    comfyui_scheduler: str = "normal"
    comfyui_steps: int = 20
    comfyui_cfg: float = 7.0
    image_credit_reservation_ttl: float = 1200.0
    image_credit_reservation_heartbeat: float = 30.0
    checkpoint_agent_url: str = ""
    checkpoint_agent_token_file: str = ""
    checkpoint_agent_allow_insecure_tailscale: bool = False

    # Agents (sandbox runner on the compute host; see docs/agents.md)
    agents_runner_url: str = ""
    agents_runner_token_file: str = ""
    agents_runner_token: str = ""
    agents_runner_allow_insecure_tailscale: bool = False
    agents_max_upload_bytes: int = 20 * 1024 * 1024

    warnings: tuple[str, ...] = field(default=(), compare=False)

    # ----- derived values -------------------------------------------------
    @property
    def is_development(self) -> bool:
        return self.environment in {"development", "dev", "test", "testing"}

    @property
    def images_enabled(self) -> bool:
        return self.image_backend == "comfyui"

    @property
    def image_request_timeout(self) -> float:
        """End-to-end budget of one synchronous image request."""
        return self.comfyui_queue_timeout + self.comfyui_generation_timeout + self.comfyui_timeout * 5 + 120.0

    @property
    def background_upload_dir(self) -> Path:
        return self.instance_dir / "customization" / "backgrounds"

    @property
    def audio_dir(self) -> Path:
        return self.instance_dir / "audio"

    @property
    def error_log(self) -> Path:
        return self.instance_dir / "errors.log"

    @property
    def model_recovery_file(self) -> Path:
        return self.instance_dir / ".model-recovery.json"

    @property
    def ollama_is_local(self) -> bool:
        """Whether Ollama runs on this machine (its memory, disk and GPU are ours).

        ``BC_INFERENCE_LOCAL`` decides when set. Otherwise a bearer token means
        a compute gateway (the documented SSH tunnel is a loopback URL with a
        token) and any other host is remote; only a loopback URL without a
        token is a local Ollama.
        """
        if self.inference_local is not None:
            return self.inference_local
        if self.ollama_api_key:
            return False
        return (urlsplit(self.ollama_url).hostname or "").lower() in LOOPBACK_HOSTS

    @property
    def fallback_enabled(self) -> bool:
        return self.inference_outage_mode == "fallback" and bool(self.inference_fallback_url)

    def replace(self, **changes) -> "Config":
        return dataclasses.replace(self, **changes)


def _fallback_url(r: _Reader, primary: str, mode: str) -> str:
    """``BC_INFERENCE_FALLBACK_URL`` checked; empty when fallback cannot be used.

    There is deliberately no default: a web server must never start sending
    requests (and model downloads) to whatever happens to listen on its own
    port 11434 because the compute server went away.
    """
    value = r.text("BC_INFERENCE_FALLBACK_URL").rstrip("/")
    if value:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password \
                or parts.query or parts.fragment:
            r.warnings.append("BC_INFERENCE_FALLBACK_URL must be an http(s) URL without credentials; "
                              "fallback is disabled.")
            value = ""
        elif value == primary and mode == "fallback":
            r.warnings.append("BC_INFERENCE_FALLBACK_URL is the same server as BC_OLLAMA_URL; fallback is disabled.")
            value = ""
    if mode == "fallback" and not value:
        r.warnings.append("BC_INFERENCE_OUTAGE_MODE=fallback needs BC_INFERENCE_FALLBACK_URL; "
                          "using shutdown (new answers pause while the AI server is unreachable).")
    return value


def _bearer_token(r: _Reader, name: str, url: str) -> str:
    """The token for *url*; never sent in clear text to another machine (HTTPS, or loopback such as an SSH tunnel)."""
    value = r.text(name)
    parts = urlsplit(url)
    if value and parts.scheme == "http" and (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        r.warnings.append(f"{name} is not sent over plain HTTP to another machine ({url}); use https:// or an SSH "
                          "tunnel to 127.0.0.1. That server will reject requests until this is fixed.")
        return ""
    return value


def _path(value, base):
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def load_config(environ=None, *, load_secret=True) -> Config:
    """Build a :class:`Config` from *environ* (defaults to ``os.environ``)."""
    env = os.environ if environ is None else environ
    r = _Reader(env)
    cwd = Path.cwd()

    instance_dir = _path(r.text("BC_INSTANCE_DIR") or str(REPO_ROOT / "instance"), cwd)
    database_path = _path(r.text("BC_DATABASE_PATH") or str(instance_dir / "bananachat.db"), cwd)
    log_file_text = r.text("BC_LOG_FILE")
    log_file = _path(log_file_text, cwd) if log_file_text else instance_dir / "bananachat.log"

    proxy_mode = r.flag("BC_PROXY_MODE", False)
    environment = r.text("BC_ENV", "production").lower() or "production"

    source_url = r.text("BC_SOURCE_URL") or DEFAULT_SOURCE_URL
    parts = urlsplit(source_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        r.warnings.append("BC_SOURCE_URL must be an absolute HTTP(S) URL without credentials; using the default.")
        source_url = DEFAULT_SOURCE_URL

    max_concurrent = r.integer("BC_MAX_CONCURRENT", 4, 1, 128)
    generation_timeout = r.integer("BC_GENERATION_TIMEOUT", 300, 10, 3600)
    comfyui_generation_timeout = r.number("BC_COMFYUI_GENERATION_TIMEOUT", 600, 1, 2400)
    image_credits = r.number("BC_IMAGE_CREDITS_PER_GENERATION", 5, 0, 100000)
    reservation_ttl = max(comfyui_generation_timeout + 360.0, r.number("BC_IMAGE_CREDIT_RESERVATION_TTL", 1200, 60, 86400))
    chat_max_request_bytes = r.integer("BC_CHAT_MAX_REQUEST_MB", 8, 1, 512) * 1024 * 1024
    maintenance = r.text("BANANA_MAINTENANCE_FILE") or r.text("BW_MAINTENANCE_FILE")
    default_language = r.choice("BC_DEFAULT_LANGUAGE", "it", SUPPORTED_LANGUAGES)
    ollama_url = (r.text("BC_OLLAMA_URL", DEFAULT_OLLAMA_URL) or DEFAULT_OLLAMA_URL).rstrip("/")
    outage_mode = r.choice("BC_INFERENCE_OUTAGE_MODE", "shutdown", ("shutdown", "fallback"))
    fallback_url = _fallback_url(r, ollama_url, outage_mode)
    if outage_mode == "fallback" and not fallback_url:
        outage_mode = "shutdown"

    claude_extension = r.text("BC_CLAUDE_EXTENSION")
    if claude_extension and not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", claude_extension,
                                             flags=re.ASCII):
        r.warnings.append("BC_CLAUDE_EXTENSION must be an installed Python module name; Claude is disabled.")
        claude_extension = ""

    config = Config(
        host=r.text("BC_HOST", "127.0.0.1") or "127.0.0.1",
        port=r.integer("BC_PORT", 8000, 1, 65535),
        proxy_mode=proxy_mode,
        proxy_hops=r.integer("BC_PROXY_HOPS", 1, 1, 5),
        environment=environment,
        debug=r.flag("BC_DEBUG", False),
        instance_dir=instance_dir,
        database_path=database_path,
        log_file=log_file,
        logging_level=r.choice("BC_LOGGING_LEVEL", "verbose", ("off", "minimal", "medium", "verbose", "debug")),
        maintenance_file=Path(maintenance) if maintenance else None,
        session_cookie_name=r.text("BC_SESSION_COOKIE_NAME", "bc_session") or "bc_session",
        secure_cookies=r.flag("BC_SECURE_COOKIES", proxy_mode),
        session_days=r.integer("BC_SESSION_DAYS", 7, 1, 365),
        password_hash_method=r.text("BC_PASSWORD_HASH_METHOD", "auto") or "auto",
        min_form_seconds=r.number("BC_MIN_FORM_SECONDS", 0.4, 0, 30),
        default_language=default_language,
        source_url=source_url,
        background_max_bytes=r.integer("BC_BACKGROUND_IMAGE_MAX_MB", 4, 1, 64) * 1024 * 1024,
        background_max_pixels=r.integer("BC_BACKGROUND_IMAGE_MAX_PIXELS", 16_000_000, 100_000, 100_000_000),
        background_max_dimension=r.integer("BC_BACKGROUND_IMAGE_MAX_DIMENSION", 2560, 320, 8192),
        chat_max_files=r.integer("BC_CHAT_MAX_FILES", 4, 1, 32),
        chat_max_request_bytes=chat_max_request_bytes,
        chat_max_image_bytes=r.integer("BC_CHAT_MAX_IMAGE_MB", 5, 1, 256) * 1024 * 1024,
        chat_max_document_bytes=r.integer("BC_CHAT_MAX_DOCUMENT_MB", 5, 1, 256) * 1024 * 1024,
        chat_max_text_bytes=r.integer("BC_CHAT_MAX_TEXT_KB", 1024, 1, 65536) * 1024,
        chat_max_extracted_chars=r.integer("BC_CHAT_MAX_EXTRACTED_CHARS", 200_000, 1000, 5_000_000),
        chat_max_image_pixels=r.integer("BC_CHAT_MAX_IMAGE_PIXELS", 20_000_000, 1_000_000, 200_000_000),
        chat_max_session_attachment_bytes=max(
            chat_max_request_bytes, r.integer("BC_CHAT_MAX_SESSION_ATTACHMENT_MB", 40, 1, 4096) * 1024 * 1024),
        chat_max_context_chars=r.integer("BC_CHAT_MAX_CONTEXT_CHARS", 300_000, 1000, 5_000_000),
        chat_max_context_images=r.integer("BC_CHAT_MAX_CONTEXT_IMAGES", 8, 1, 64),
        chat_max_response_bytes=r.integer("BC_CHAT_MAX_RESPONSE_KB", 1024, 16, 8192) * 1024,
        chat_max_history_messages=r.integer("BC_CHAT_MAX_HISTORY_MESSAGES", 100, 2, 500),
        ollama_url=ollama_url,
        ollama_api_key=_bearer_token(r, "BC_OLLAMA_API_KEY", ollama_url),
        inference_local=r.optional_flag("BC_INFERENCE_LOCAL"),
        ollama_sync_interval=r.integer("BC_OLLAMA_SYNC_INTERVAL", 60, 10, 86400),
        keep_alive=r.integer("BC_KEEP_ALIVE", 0, -1, 86400 * 7),
        # A managed single server shares app.env with its Ollama unit, so
        # OLLAMA_MODELS names the model folder when nothing else is set.
        ollama_model_dir=r.text("BC_OLLAMA_MODEL_DIR", r.text("OLLAMA_MODELS")
                                or str(Path("~/.ollama/models").expanduser())),
        min_free_disk_gb=r.number("BC_MIN_FREE_DISK_GB", 2, 0, 100_000),
        http_threads=r.integer("BC_HTTP_THREADS", 16, 8, 64),
        max_concurrent=max_concurrent,
        max_queue_depth=r.integer("BC_MAX_QUEUE_DEPTH", 50, max_concurrent, 1000),
        queue_timeout=r.integer("BC_QUEUE_TIMEOUT", 120, 5, 3600),
        generation_timeout=generation_timeout,
        inference_read_timeout=r.integer("BC_INFERENCE_READ_TIMEOUT", 30, 5, 600),
        first_token_timeout=r.integer("BC_FIRST_TOKEN_TIMEOUT", 180, 10, 3600),
        max_output_tokens=r.integer("BC_MAX_OUTPUT_TOKENS", 8192, 128, 65536),
        max_num_ctx=r.integer("BC_MAX_NUM_CTX", 32768, 512, 1_048_576),
        min_free_memory_mb=r.integer("BC_MIN_FREE_MEMORY_MB", 256, 0, 1_048_576),
        compute_snapshot_interval=r.integer("BC_COMPUTE_SNAPSHOT_INTERVAL", 30, 5, 3600),
        inference_outage_mode=outage_mode,
        inference_health_interval=r.integer("BC_INFERENCE_HEALTH_INTERVAL", 15, 5, 3600),
        inference_health_failures=r.integer("BC_INFERENCE_HEALTH_FAILURES", 3, 1, 100),
        inference_fallback_url=fallback_url,
        inference_fallback_api_key=_bearer_token(r, "BC_INFERENCE_FALLBACK_API_KEY", fallback_url) if fallback_url else "",
        workers_enabled=r.flag("BC_WORKERS_ENABLED", False),
        worker_claim_timeout=r.integer("BC_WORKER_CLAIM_TIMEOUT", 20, 2, 600),
        claude_extension=claude_extension,
        claude_code_config=r.text("BC_CLAUDE_CODE_CONFIG"),
        no_history_ttl_hours=r.integer("BC_NO_HISTORY_TTL_HOURS", 24, 1, 24 * 365),
        deleted_chat_retention_days=r.integer("BC_DELETED_CHAT_RETENTION_DAYS", 30, 0, 36500),
        metrics_retention_days=r.integer("BC_METRICS_RETENTION_DAYS", 30, 1, 36500),
        image_backend=r.choice("BC_IMAGE_BACKEND", "disabled", ("disabled", "comfyui")),
        image_generation_rpm=r.integer("BC_IMAGE_GENERATION_RPM", 6, 0, 10000),
        image_credits_per_generation=image_credits,
        # Tokens charged per image; the previous release's setting in credits (1 credit = 1,000 tokens) is the default.
        image_tokens_per_generation=r.integer("BC_IMAGE_TOKENS_PER_GENERATION", int(round(image_credits * 1000)), 0,
                                              100_000_000),
        comfyui_url=(r.text("BC_COMFYUI_URL", "http://127.0.0.1:8188") or "http://127.0.0.1:8188").rstrip("/"),
        comfyui_timeout=r.number("BC_COMFYUI_TIMEOUT", 30, 1, 60),
        comfyui_generation_timeout=comfyui_generation_timeout,
        comfyui_queue_timeout=r.number("BC_COMFYUI_QUEUE_TIMEOUT", 300, 1, 600),
        comfyui_poll_interval=r.number("BC_COMFYUI_POLL_INTERVAL", 1, 0.05, 30),
        comfyui_sampler=r.text("BC_COMFYUI_SAMPLER", "euler") or "euler",
        comfyui_scheduler=r.text("BC_COMFYUI_SCHEDULER", "normal") or "normal",
        comfyui_steps=r.integer("BC_COMFYUI_STEPS", 20, 1, 150),
        comfyui_cfg=r.number("BC_COMFYUI_CFG", 7, 0, 30),
        image_credit_reservation_ttl=reservation_ttl,
        image_credit_reservation_heartbeat=max(
            5.0, min(reservation_ttl / 3.0, r.number("BC_IMAGE_CREDIT_RESERVATION_HEARTBEAT", 30, 1, 3600))),
        checkpoint_agent_url=r.text("BC_CHECKPOINT_AGENT_URL"),
        checkpoint_agent_token_file=r.text("BC_CHECKPOINT_AGENT_TOKEN_FILE"),
        checkpoint_agent_allow_insecure_tailscale=r.flag("BC_CHECKPOINT_AGENT_ALLOW_INSECURE_TAILSCALE", False),
        agents_runner_url=r.text("BC_AGENTS_RUNNER_URL"),
        agents_runner_token_file=r.text("BC_AGENTS_RUNNER_TOKEN_FILE"),
        agents_runner_token=r.text("BC_AGENTS_RUNNER_TOKEN"),
        agents_runner_allow_insecure_tailscale=r.flag("BC_AGENTS_RUNNER_ALLOW_INSECURE_TAILSCALE", False),
        agents_max_upload_bytes=r.integer("BC_AGENTS_MAX_UPLOAD_MB", 20, 1, 200) * 1024 * 1024,
    )
    if load_secret:
        secret = load_secret_key(config.instance_dir, env, development=config.is_development, warnings=r.warnings)
        setup_token = r.text("BC_SETUP_TOKEN") or derive_setup_token(secret)
        config = config.replace(secret_key=secret, setup_token=setup_token)
    return config.replace(warnings=tuple(r.warnings))


# ----- secret key -----------------------------------------------------------

def derive_setup_token(secret_key: str) -> str:
    """The installation token used to create the first administrator.

    Managed installations set ``BC_SETUP_TOKEN``; otherwise it is derived from
    the persistent secret so every worker process agrees on it.
    """
    return hmac.new(secret_key.encode("utf-8"), b"initial-admin-setup", hashlib.sha256).hexdigest()


def load_secret_key(instance_dir: Path, environ, *, development=False, warnings=None) -> str:
    """Return ``SECRET_KEY`` from the environment or the instance key file.

    The key file (``<instance>/.secret_key``, mode 0600) is created on first
    start under a lock so concurrently starting workers agree on one key.
    """
    for name in ("BC_SECRET_KEY", "SECRET_KEY"):
        value = (environ.get(name) or "").strip()
        if value:
            if len(value) < 32 and warnings is not None:
                warnings.append(f"{name} is shorter than 32 characters; use a long random value.")
            return value

    from filelock import FileLock

    instance_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_file = instance_dir / ".secret_key"
    with FileLock(str(key_file) + ".lock", timeout=20, mode=0o600):
        existing = _read_key_file(key_file, development=development, warnings=warnings)
        if existing:
            return existing
        return _write_key_file(key_file)


def _read_key_file(key_file: Path, *, development, warnings):
    try:
        info = key_file.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"{key_file} must be a regular file.")
    if os.name != "nt" and info.st_mode & 0o077:
        message = f"{key_file} is readable by other users (mode {info.st_mode & 0o777:o}); run: chmod 600 {key_file}"
        if not development:
            raise SystemExit("FATAL: " + message)
        if warnings is not None:
            warnings.append(message)
    text = key_file.read_text(encoding="utf-8").strip()
    if not text or len(text) > 4096:
        raise SystemExit(f"{key_file} is empty or invalid; restore the original key from a backup.")
    return text


def _write_key_file(key_file: Path) -> str:
    key = secrets.token_hex(32)
    descriptor, temporary = tempfile.mkstemp(dir=key_file.parent, prefix=".secret_key_")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, key_file)
        if os.name == "posix":
            directory = os.open(key_file.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return key


def log_warnings(config: Config, logger: logging.Logger | None = None) -> None:
    logger = logger or logging.getLogger("bananachat")
    for message in config.warnings:
        logger.warning("Configuration: %s", message)
