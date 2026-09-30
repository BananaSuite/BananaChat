"""Worker settings, read from environment variables and an optional settings file.

Nothing happens at import time: :func:`load` reads the settings file (without
overriding variables that are already exported) and returns a
:class:`Settings`. Invalid numbers fall back to their defaults with a warning,
so a typo in a hand-edited file never stops the worker.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import socket
from dataclasses import dataclass, field
from typing import List, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost"}


def default_env_file() -> str:
    """Where the worker keeps its settings (``service.py`` writes the same path)."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BananaChat", "bananachat-worker.env")
    return os.path.expanduser("~/.config/bananachat-worker.env")


def parse_env_text(text: str) -> List[Tuple[str, str]]:
    """``KEY=VALUE`` lines; comments, blank and malformed lines are skipped; quotes are removed."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((name, value))
    return pairs


def load_env_file(path: Optional[str] = None, environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """Fill *environ* (default ``os.environ``) from the settings file without overriding exports.

    Returns the path that was read, or None when there is no file.
    """
    environ = os.environ if environ is None else environ
    path = path or environ.get("BC_WORKER_ENV_FILE") or default_env_file()
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(1024 * 1024)
    except OSError:
        return None
    for name, value in parse_env_text(text):
        environ.setdefault(name, value)
    return path


def is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    server_url: str = ""
    token: str = ""
    name: str = ""
    allow_http: bool = False
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_binary: str = "ollama"
    ollama_idle_timeout: int = 600
    ollama_keep_alive: int = 0
    first_token_timeout: int = 180
    read_timeout: int = 30
    generation_timeout: int = 300
    idle_threshold: int = 300
    light_threshold: int = 30
    gpu_gaming_threshold: float = 70.0
    gpu_active_threshold: float = 50.0
    poll_gap: float = 0.5
    heartbeat_interval: float = 10.0
    activity_interval: float = 5.0
    log_level: str = "INFO"
    log_file: str = ""
    log_max_bytes: int = 5 * 1024 * 1024
    log_backups: int = 3
    env_file: Optional[str] = None
    warnings: Tuple[str, ...] = field(default=(), compare=False)

    def problems(self) -> List[str]:
        """What prevents the worker from starting (empty when it can run)."""
        found = []
        if not self.server_url:
            found.append("BC_SERVER_URL is not set (for example BC_SERVER_URL=https://chat.example.org).")
        else:
            parts = urlsplit(self.server_url)
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                    or parts.query or parts.fragment):
                found.append("BC_SERVER_URL must be an http(s) address without a password, query or fragment.")
            elif parts.scheme == "http" and not is_loopback(parts.hostname) and not self.allow_http:
                found.append("BC_SERVER_URL must use https:// so the worker token and prompts are encrypted. "
                             "Only on a network you secure yourself, set BC_WORKER_ALLOW_HTTP=1 to allow http://.")
        if not self.token:
            found.append("BC_WORKER_TOKEN is not set. Register the worker on the server's Admin → Workers page "
                         "and copy its token.")
        elif any(char.isspace() for char in self.token) or len(self.token) > 512:
            found.append("BC_WORKER_TOKEN contains spaces or is too long; copy it again.")
        parts = urlsplit(self.ollama_host)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            found.append("BC_OLLAMA_HOST must be an http:// address such as http://127.0.0.1:11434.")
        return found

    @property
    def ollama_is_local(self) -> bool:
        return is_loopback(urlsplit(self.ollama_host).hostname)

    @property
    def ollama_listen_address(self) -> str:
        """``host:port`` for the ``OLLAMA_HOST`` variable of a managed ``ollama serve``."""
        parts = urlsplit(self.ollama_host)
        host = parts.hostname or "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{parts.port or 11434}"


class _Reader:
    def __init__(self, environ: Mapping[str, str]):
        self.environ = environ
        self.warnings: List[str] = []

    def text(self, name: str, default: str = "") -> str:
        value = self.environ.get(name)
        return default if value is None else value.strip()

    def number(self, name: str, default, minimum, maximum, kind=int):
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = kind(raw)
        except ValueError:
            self.warnings.append(f"{name}={raw!r} is not a number; using {default}.")
            return default
        if value != value or not minimum <= value <= maximum:  # NaN or out of range
            clamped = default if value != value else min(max(value, minimum), maximum)
            self.warnings.append(f"{name}={raw} is outside {minimum}..{maximum}; using {clamped}.")
            return clamped
        return value


def from_environ(environ: Mapping[str, str], env_file: Optional[str] = None) -> Settings:
    r = _Reader(environ)
    level = r.text("BC_WORKER_LOG_LEVEL", "INFO").upper() or "INFO"
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        r.warnings.append(f"BC_WORKER_LOG_LEVEL={level!r} is not a logging level; using INFO.")
        level = "INFO"
    return Settings(
        server_url=r.text("BC_SERVER_URL").rstrip("/"),
        token=r.text("BC_WORKER_TOKEN"),
        name=r.text("BC_WORKER_NAME") or socket.gethostname(),
        allow_http=r.text("BC_WORKER_ALLOW_HTTP") == "1",
        ollama_host=(r.text("BC_OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/"),
        ollama_binary=r.text("BC_OLLAMA_BINARY") or "ollama",
        ollama_idle_timeout=r.number("BC_OLLAMA_IDLE_TIMEOUT", 600, 0, 7 * 86400),
        ollama_keep_alive=r.number("BC_OLLAMA_KEEP_ALIVE", 0, -1, 7 * 86400),
        first_token_timeout=r.number("BC_FIRST_TOKEN_TIMEOUT", 180, 10, 3600),
        read_timeout=r.number("BC_INFERENCE_READ_TIMEOUT", 30, 5, 600),
        generation_timeout=r.number("BC_GENERATION_TIMEOUT", 300, 10, 3600),
        idle_threshold=r.number("BC_IDLE_THRESHOLD", 300, 1, 86400),
        light_threshold=r.number("BC_LIGHT_THRESHOLD", 30, 1, 86400),
        gpu_gaming_threshold=r.number("BC_GPU_GAMING_THRESHOLD", 70.0, 1.0, 101.0, float),
        gpu_active_threshold=r.number("BC_GPU_ACTIVE_THRESHOLD", 50.0, 0.0, 100.0, float),
        poll_gap=r.number("BC_POLL_GAP", 0.5, 0.0, 60.0, float),
        heartbeat_interval=r.number("BC_HEARTBEAT_INTERVAL", 10.0, 2.0, 30.0, float),
        activity_interval=r.number("BC_ACTIVITY_CHECK_INTERVAL", 5.0, 1.0, 60.0, float),
        log_level=level,
        log_file=r.text("BC_WORKER_LOG_FILE"),
        log_max_bytes=r.number("BC_WORKER_LOG_MAX_BYTES", 5 * 1024 * 1024, 64 * 1024, 1024 ** 3),
        log_backups=r.number("BC_WORKER_LOG_BACKUPS", 3, 0, 20),
        env_file=env_file,
        warnings=tuple(r.warnings),
    )


def load(environ: Optional[MutableMapping[str, str]] = None) -> Settings:
    """Read the settings file into *environ* (exports win) and build the settings."""
    environ = os.environ if environ is None else environ
    loaded = load_env_file(environ=environ)
    return from_environ(environ, loaded)
