"""Sandbox runner: disposable, locked-down containers for BananaChat agents (runs on the cluster).

Run from the repository root with ``python -m compute.sandbox_runner``; it uses
only the standard library, like the inference gateway. It is the only
BananaChat component that talks to the container engine, and every limit is
enforced here, so a compromised web server cannot loosen them. The operator
guide and threat model are in ``docs/agents.md``.

Environment (all optional except the token file):

* ``BC_SANDBOX_HOST`` / ``BC_SANDBOX_PORT`` - listen address (127.0.0.1:11436);
* ``BC_SANDBOX_TOKEN_FILE`` - the bearer token, a private (0600) regular file
  holding at least 32 characters; created with a random token if missing;
* ``BC_SANDBOX_ENGINE`` (``podman`` or ``docker``), ``BC_SANDBOX_ENGINE_BINARY``
  (absolute path), ``BC_SANDBOX_RUNTIME`` (OCI runtime such as ``runsc``);
* ``BC_SANDBOX_IMAGES`` - comma-separated image allowlist (pulled by the
  operator; the runner never pulls);
* ``BC_SANDBOX_NETWORK`` - ``none`` (default) or ``bridge``;
* ``BC_SANDBOX_MAX`` (4 sandboxes), ``BC_SANDBOX_MAX_EXECS`` (8 engine
  operations at once), ``BC_SANDBOX_MEMORY_MB`` (1024), ``BC_SANDBOX_CPUS``
  (1.0), ``BC_SANDBOX_PIDS`` (256), ``BC_SANDBOX_WORKSPACE_MB`` (512),
  ``BC_SANDBOX_EXEC_TIMEOUT`` (300 s), ``BC_SANDBOX_IDLE_TTL`` (1800 s),
  ``BC_SANDBOX_MAX_AGE`` (21600 s), ``BC_SANDBOX_OUTPUT_KB`` (64 per stream);
* ``BC_SANDBOX_ALLOW_ROOTFUL`` - ``1`` to accept a rootful engine;
* ``BC_SANDBOX_INSTANCE`` - name of this runner, stored as a container label
  so several runners (or a test suite) on one host never reap each other's
  sandboxes; ``BC_SANDBOX_MAX_CONNECTIONS`` (32);
* ``BC_SANDBOX_TRUSTED_PEERS`` (loopback: the local proxy or SSH tunnel) and
  ``BC_SANDBOX_MAX_CONNECTIONS_PER_PEER`` (4 connections per other address).

Nothing user-controlled ever reaches a shell on the host: the engine is always
started with an argument list, identifiers are generated here or validated
against strict patterns, and file operations run *inside* the container with
fixed scripts that receive paths as positional arguments. Importing this module
has no side effects.
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import io
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
import zlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("compute.sandbox_runner")

DEFAULT_IMAGE = "mirror.gcr.io/library/python:3.12-slim"
WORKSPACE = "/workspace"
SANDBOX_USER = "1000:1000"
LABEL = "io.bananachat.sandbox"
LABEL_RUNNER = "io.bananachat.runner"
LABEL_SESSION = "io.bananachat.session"
LABEL_ID = "io.bananachat.sandbox-id"
NAME_PREFIX = "bc-sandbox-"

MIN_TOKEN_LENGTH = 32
MAX_JSON_BODY = 256 * 1024
MAX_FILE_BODY = 10 * 1024 * 1024
MAX_ARCHIVE_BODY = 50 * 1024 * 1024
MAX_READ_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 1000
MAX_ARCHIVE_DOWNLOAD = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000
MAX_ARCHIVE_META = 64 * 1024  # pax / GNU long-name headers
MAX_COMMAND = 64 * 1024
MAX_PATH = 4096
MAX_COMPONENT = 255
# Output kept per stream is BC_SANDBOX_OUTPUT_KB; beyond this much in one
# stream the command is stopped, so `yes` cannot keep the engine busy.
DRAIN_LIMIT = 16 * 1024 * 1024
TMP_MB = 64
NOFILE = 1024
LARGE_TRANSFERS = 2  # uploads and archive downloads held in memory at once
FILE_TRANSFERS = 4  # file writes (at most 10 MB each) at once; separate, so archives cannot starve them
REMEMBER_REMOVED = 4096  # ids of removed sandboxes that keep answering 410 instead of 404

CLIENT_TIMEOUT = 30.0  # per socket operation
HEADER_DEADLINE = 10.0  # to send the request line and headers (before any authentication)
REQUEST_DEADLINE = 300.0  # whole request, except exec (its timeout + EXEC_SLACK)
EXEC_SLACK = 90.0
# Sending a response body (archives): it must move at SEND_MIN_RATE on average after SEND_GRACE, and no
# single write may stall longer than SEND_IDLE_TIMEOUT, so a stalled reader cannot keep a transfer slot.
SEND_MIN_RATE = 1024 * 1024
SEND_GRACE = 15.0
SEND_IDLE_TIMEOUT = 10.0
SEND_CHUNK = 256 * 1024
ENGINE_TIMEOUT = 60.0
FILE_OP_TIMEOUT = 60
ARCHIVE_TIMEOUT = 300
KILL_TIMEOUT = 10.0
REAP_INTERVAL = 15.0
HEALTH_CACHE_SECONDS = 5.0
HEALTH_DEADLINE = 60.0
AUTH_FAILURES_PER_MINUTE = 20
DRAIN_SECONDS = 5.0

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SANDBOX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RUNTIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$")
ROUTE_RE = re.compile(r"^/v1/sandboxes(?:/([0-9a-f]{32})(?:/(exec|interrupt|files|archive))?)?$")

# ----- scripts run inside the container ----------------------------------------------------------
# They are constants: user input only ever arrives as positional arguments ("$1"...), never as
# part of the script text. The container, not these checks, is the security boundary; the
# checks keep the API's promise that paths stay under /workspace even through symlinks.
_INSIDE = 'r=$(readlink -m -- "$1") || exit 90; case $r in /workspace|/workspace/*) ;; *) exit 91;; esac; '

# Reads and writes prefer this helper when the image has python3 (3.7+): it walks the path one component at
# a time with directory file descriptors and O_NOFOLLOW, resolving symlinks itself and refusing any that
# lead out of /workspace, so a symlink the agent swaps in between a check and the open cannot redirect the
# operation (the shell fallback below checks with readlink first and then opens: a small race). It answers
# with the same exit codes and output as the shell scripts. It must not contain single quotes.
SAFE_FILE_PY = r'''import errno, os, stat, sys
if sys.version_info < (3, 7):
    sys.exit(97)
ROOT = "/workspace"
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def parts(text):
    return [part for part in text.split("/") if part not in ("", ".")]


def walk(path, create):
    if path != ROOT and not path.startswith(ROOT + "/"):
        sys.exit(91)
    stack = [os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)]
    pending = parts(path[len(ROOT):])
    hops = 0
    while pending:
        name = pending.pop(0)
        if name == "..":
            if len(stack) == 1:
                sys.exit(91)
            os.close(stack.pop())
            continue
        try:
            info = os.stat(name, dir_fd=stack[-1], follow_symlinks=False)
        except FileNotFoundError:
            if not pending:
                return stack, name
            if not create:
                sys.exit(92)
            try:
                os.mkdir(name, 0o777, dir_fd=stack[-1])
            except FileExistsError:
                pass
            except OSError:
                sys.exit(94)
            hops += 1
            if hops > 200:
                sys.exit(90)
            pending.insert(0, name)
            continue
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > 40:
                sys.exit(90)
            target = os.readlink(name, dir_fd=stack[-1])
            if target.startswith("/"):
                if target != ROOT and not target.startswith(ROOT + "/"):
                    sys.exit(91)
                while len(stack) > 1:
                    os.close(stack.pop())
                target = target[len(ROOT):]
            pending[:0] = parts(target)
            continue
        if not pending:
            return stack, name
        if not stat.S_ISDIR(info.st_mode):
            sys.exit(94 if create else 92)
        try:
            stack.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=stack[-1]))
        except OSError:
            hops += 1
            if hops > 40:
                sys.exit(90)
            pending.insert(0, name)
    return stack, None


def read(path, limit, entries):
    stack, name = walk(path, False)
    if name is None:
        fd = os.dup(stack[-1])
    else:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | NOFOLLOW, dir_fd=stack[-1])
        except FileNotFoundError:
            sys.exit(92)
        except OSError as error:
            sys.exit(90 if error.errno == errno.ELOOP else 93)
    info = os.fstat(fd)
    out = sys.stdout.buffer
    if stat.S_ISDIR(info.st_mode):
        out.write(b"D\n")
        count = 0
        with os.scandir(fd) as items:
            for item in items:
                if count >= entries:
                    break
                try:
                    mode = item.stat(follow_symlinks=False).st_mode
                    size = item.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                kind = (b"d" if stat.S_ISDIR(mode) else b"f" if stat.S_ISREG(mode) else
                        b"l" if stat.S_ISLNK(mode) else b"p")
                out.write(kind + b"\t%d\t" % size + os.fsencode(item.name) + b"\0")
                count += 1
        return
    if not stat.S_ISREG(info.st_mode):
        sys.exit(93)
    out.write(b"F\t%d\n" % info.st_size)
    left = limit
    while left > 0:
        chunk = os.read(fd, min(65536, left))
        if not chunk:
            break
        out.write(chunk)
        left -= len(chunk)


def write(path):
    stack, name = walk(path, True)
    if name is None:
        sys.exit(95)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOCTTY | NOFOLLOW, 0o666,
                     dir_fd=stack[-1])
    except IsADirectoryError:
        sys.exit(95)
    except OSError as error:
        sys.exit(96 if error.errno in (errno.ENOSPC, errno.EDQUOT) else 90 if error.errno == errno.ELOOP else 93)
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        sys.exit(95)
    if not stat.S_ISREG(info.st_mode):
        sys.exit(93)
    try:
        os.ftruncate(fd, 0)
        while True:
            chunk = sys.stdin.buffer.read(65536)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
    except OSError:
        sys.exit(96)
    sys.stdout.write("%d\n" % os.fstat(fd).st_size)


if sys.argv[1] == "read":
    read(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
elif sys.argv[1] == "write":
    write(sys.argv[2])
else:
    sys.exit(90)
'''


def _prefer_python(mode: str, fallback: str) -> str:
    """Run SAFE_FILE_PY when python3 exists (it answers 97 when too old), else the shell script."""
    return ("if command -v python3 >/dev/null 2>&1; then python3 -I -S -c '" + SAFE_FILE_PY + "' " + mode +
            ' "$@"; s=$?; [ "$s" -eq 97 ] || exit "$s"; fi; ' + fallback)


READ_SCRIPT = _prefer_python("read", _INSIDE + (
    'if [ -d "$r" ]; then printf "D\\n"; '
    'find "$r" -mindepth 1 -maxdepth 1 -printf "%y\\t%s\\t%f\\0" | head -z -n "$3"; exit 0; fi; '
    '[ -e "$r" ] || exit 92; [ -f "$r" ] || exit 93; '
    'printf "F\\t%s\\n" "$(stat -c %s -- "$r")"; exec head -c "$2" -- "$r"'))

WRITE_SCRIPT = _prefer_python("write", _INSIDE + (
    '[ "$r" = /workspace ] && exit 95; [ -d "$r" ] && exit 95; [ -e "$r" ] && [ ! -f "$r" ] && exit 93; '
    'mkdir -p -- "$(dirname -- "$r")" || exit 94; cat > "$r" || exit 96; stat -c %s -- "$r"'))

UNPACK_SCRIPT = _INSIDE + (
    'mkdir -p -- "$r" || exit 94; cd -- "$r" || exit 94; '
    'exec tar -x -f - --no-same-owner --no-same-permissions')

# Runs the user's command in the requested directory. $1 = cwd, $2 = command.
EXEC_WRAPPER = ('cd -- "$1" 2>/dev/null || { echo "sandbox: cannot enter $1" >&2; exit 126; }; '
                'exec sh -c "$2"')

# Finds the idle main process (the child of the init process) right after start.
FIND_KEEPER = ('for d in /proc/[0-9]*; do p=${d#/proc/}; [ "$p" = 1 ] && continue; '
               '{ read -r s < "$d/stat"; } 2>/dev/null || continue; s=${s##*) }; set -- $s; '
               '[ "$2" = 1 ] && { echo "$p"; exit 0; }; done; echo 1')

# Kills every process except init, the idle main process ($1) and itself. The killing uses only
# shell builtins so it still works when a fork bomb exhausted the pids limit; the short pause
# between rounds (which may fail to fork, harmlessly) lets killed processes finish exiting. If the
# exec itself cannot start, or processes survive every round, the runner removes the sandbox.
KILL_SCRIPT = ('keep=$1; me=$$; i=0; while [ $i -lt 40 ]; do found=0; '
               'for d in /proc/[0-9]*; do p=${d#/proc/}; case $p in 1|"$keep"|"$me") continue;; esac; '
               '{ read -r s < "$d/stat"; } 2>/dev/null || continue; s=${s##*) }; s=${s%% *}; '
               'case $s in Z|X|x) continue;; esac; kill -9 "$p" 2>/dev/null && found=1; done; '
               '[ $found = 0 ] && exit 0; i=$((i+1)); sleep 0.1 2>/dev/null; done; exit 1')


class ApiError(Exception):
    """An error answered as ``{"error": {"code", "message"}}``."""

    def __init__(self, status: int, code: str, message: str, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class ArchiveError(ApiError):
    def __init__(self, message: str):
        super().__init__(400, "invalid_archive", message)


def _no_constants(name):
    raise ValueError(f"{name} is not allowed")


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- configuration -------------------------------------------------------------------------------------

def check_token(token: str) -> str:
    token = (token or "").strip()
    if len(token) < MIN_TOKEN_LENGTH or any(char.isspace() for char in token) or not token.isascii():
        raise ValueError(f"The sandbox runner token must be at least {MIN_TOKEN_LENGTH} ASCII characters "
                         "without spaces.")
    return token


def read_token_file(path) -> str:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"The sandbox token file {path} cannot be read: {error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("The sandbox token file must be a regular file (not a symlink).")
    if os.name != "nt" and info.st_mode & 0o077:
        raise ValueError(f"The sandbox token file is readable by others; run: chmod 600 {path}")
    if info.st_size > 4096:
        raise ValueError("The sandbox token file is too large.")
    return check_token(path.read_text(encoding="utf-8"))


def load_or_create_token(path) -> str:
    """Read the token file, creating it (0600, 256 random bits) on first start."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        parent = path.parent
        try:
            parent_info = parent.stat()
        except OSError as error:
            raise ValueError(f"The directory for the sandbox token file does not exist: {error}") from None
        if os.name != "nt" and parent_info.st_mode & 0o002:
            raise ValueError(f"Refusing to create the token file in a world-writable directory ({parent}).")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        LOG.warning("Created a new sandbox runner token in %s; copy it to the web server "
                    "(BC_AGENTS_RUNNER_TOKEN_FILE).", path)
    return read_token_file(path)


def _integer(environ, name, default, minimum, maximum) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _number(environ, name, default, minimum, maximum) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number.") from None
    if not minimum <= value <= maximum:  # also refuses nan
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _flag(environ, name) -> bool:
    raw = (environ.get(name) or "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"{name} must be 1 or 0.")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 11436
    token_file: str = ""
    engine: str = "podman"
    engine_binary: str = ""
    runtime: str = ""
    images: tuple = (DEFAULT_IMAGE,)
    network: str = "none"
    max_sandboxes: int = 4
    max_execs: int = 8
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 256
    workspace_mb: int = 512
    exec_timeout: int = 300
    idle_ttl: float = 1800
    max_age: float = 21600
    output_kb: int = 64
    allow_rootful: bool = False
    instance: str = "default"
    max_connections: int = 32
    max_per_peer: int = 4
    trusted_peers: tuple = ("127.0.0.1/32", "::1/128")

    def __post_init__(self):
        if self.engine not in ("podman", "docker"):
            raise ValueError("BC_SANDBOX_ENGINE must be podman or docker.")
        if self.network not in ("none", "bridge"):
            raise ValueError("BC_SANDBOX_NETWORK must be none or bridge.")
        if self.runtime and not RUNTIME_RE.fullmatch(self.runtime):
            raise ValueError("BC_SANDBOX_RUNTIME must be a runtime name such as runsc.")
        if self.engine_binary and not os.path.isabs(self.engine_binary):
            raise ValueError("BC_SANDBOX_ENGINE_BINARY must be an absolute path.")
        if not INSTANCE_RE.fullmatch(self.instance):
            raise ValueError("BC_SANDBOX_INSTANCE must be up to 32 lowercase letters, digits and dashes.")
        images = tuple(self.images)
        if not images:
            raise ValueError("BC_SANDBOX_IMAGES must list at least one image.")
        for image in images:
            if not IMAGE_RE.fullmatch(image) or ".." in image or "//" in image:
                raise ValueError(f"BC_SANDBOX_IMAGES has an invalid image reference: {image!r}")
        self.images = images
        self.trusted_peers = tuple(str(ipaddress.ip_network(item, strict=False)) for item in self.trusted_peers)

    @classmethod
    def from_env(cls, environ=None) -> "Config":
        environ = os.environ if environ is None else environ
        token_file = (environ.get("BC_SANDBOX_TOKEN_FILE") or "").strip()
        if not token_file:
            raise ValueError("BC_SANDBOX_TOKEN_FILE is required.")
        images = tuple(item.strip() for item in (environ.get("BC_SANDBOX_IMAGES") or DEFAULT_IMAGE).split(",")
                       if item.strip())
        raw_peers = (environ.get("BC_SANDBOX_TRUSTED_PEERS") or "127.0.0.1,::1").strip()
        peers = () if raw_peers.lower() == "none" else tuple(item.strip() for item in raw_peers.split(",")
                                                             if item.strip())
        try:
            peers = tuple(str(ipaddress.ip_network(item, strict=False)) for item in peers)
        except ValueError:
            raise ValueError("BC_SANDBOX_TRUSTED_PEERS must list IP addresses or networks (or none).") from None
        return cls(
            host=(environ.get("BC_SANDBOX_HOST") or "127.0.0.1").strip(),
            port=_integer(environ, "BC_SANDBOX_PORT", 11436, 1, 65535),
            token_file=token_file,
            engine=(environ.get("BC_SANDBOX_ENGINE") or "podman").strip().lower(),
            engine_binary=(environ.get("BC_SANDBOX_ENGINE_BINARY") or "").strip(),
            runtime=(environ.get("BC_SANDBOX_RUNTIME") or "").strip(),
            images=images,
            network=(environ.get("BC_SANDBOX_NETWORK") or "none").strip().lower(),
            max_sandboxes=_integer(environ, "BC_SANDBOX_MAX", 4, 1, 64),
            max_execs=_integer(environ, "BC_SANDBOX_MAX_EXECS", 8, 1, 256),
            memory_mb=_integer(environ, "BC_SANDBOX_MEMORY_MB", 1024, 64, 65536),
            cpus=_number(environ, "BC_SANDBOX_CPUS", 1.0, 0.1, 64.0),
            pids=_integer(environ, "BC_SANDBOX_PIDS", 256, 16, 4096),
            workspace_mb=_integer(environ, "BC_SANDBOX_WORKSPACE_MB", 512, 16, 8192),
            exec_timeout=_integer(environ, "BC_SANDBOX_EXEC_TIMEOUT", 300, 5, 3600),
            idle_ttl=_integer(environ, "BC_SANDBOX_IDLE_TTL", 1800, 60, 86400),
            max_age=_integer(environ, "BC_SANDBOX_MAX_AGE", 21600, 300, 604800),
            output_kb=_integer(environ, "BC_SANDBOX_OUTPUT_KB", 64, 4, 1024),
            allow_rootful=_flag(environ, "BC_SANDBOX_ALLOW_ROOTFUL"),
            instance=(environ.get("BC_SANDBOX_INSTANCE") or "default").strip(),
            max_connections=_integer(environ, "BC_SANDBOX_MAX_CONNECTIONS", 32, 1, 1024),
            max_per_peer=_integer(environ, "BC_SANDBOX_MAX_CONNECTIONS_PER_PEER", 4, 1, 1024),
            trusted_peers=peers,
        )


# ----- paths -------------------------------------------------------------------------------------------------

def clean_path(value, *, allow_root: bool = True) -> str:
    """Normalise a sandbox path and make sure it stays under /workspace.

    Relative paths are taken relative to /workspace. NUL and control characters,
    over-long paths or components and anything that normalises outside the
    workspace are refused. Symlinks are resolved inside the container.
    """
    if not isinstance(value, str) or not value:
        raise ApiError(400, "invalid_path", "A path under /workspace is required.")
    if len(value.encode("utf-8", "surrogatepass")) > MAX_PATH:
        raise ApiError(400, "invalid_path", "The path is too long.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ApiError(400, "invalid_path", "The path contains control characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(400, "invalid_path", "The path is not valid UTF-8.") from None
    if not value.startswith("/"):
        value = WORKSPACE + "/" + value
    path = posixpath.normpath(value)
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    if path != WORKSPACE and not path.startswith(WORKSPACE + "/"):
        raise ApiError(400, "invalid_path", "The path must stay under /workspace.")
    if path == WORKSPACE and not allow_root:
        raise ApiError(400, "invalid_path", "A file path under /workspace is required.")
    if any(len(part.encode("utf-8")) > MAX_COMPONENT for part in path.split("/")):
        raise ApiError(400, "invalid_path", "A path component is too long.")
    return path


# ----- the container engine -------------------------------------------------------------------------------

@dataclass
class RunResult:
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_total: int = 0
    stderr_total: int = 0
    timed_out: bool = False
    overflow: bool = False
    error: BaseException | None = None  # raised by the stdin writer


class _Capture(threading.Thread):
    """Reads a pipe, keeping the first ``limit`` bytes and counting the rest."""

    def __init__(self, stream, limit, drain_limit, overflow):
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.drain_limit = drain_limit
        self.overflow = overflow
        self.data = bytearray()
        self.total = 0

    def run(self):
        try:
            while True:
                chunk = self.stream.read1(65536) if hasattr(self.stream, "read1") else self.stream.read(65536)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.limit - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if self.drain_limit is not None and self.total > self.drain_limit:
                    self.overflow.set()
        except (OSError, ValueError):
            pass


class Engine:
    """Starts the container engine CLI with argument lists only (never a shell)."""

    def __init__(self, kind: str, binary: str = "", environ=None):
        if kind not in ("podman", "docker"):
            raise ValueError("The engine must be podman or docker.")
        self.kind = kind
        self.binary = binary or shutil.which(kind) or ""
        self.environ = dict(os.environ if environ is None else environ)

    def check_binary(self) -> None:
        if not self.binary or not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            raise ValueError(f"The container engine {self.kind!r} was not found; install it or set "
                             "BC_SANDBOX_ENGINE_BINARY.")

    def run(self, args, *, stdin=None, timeout: float = ENGINE_TIMEOUT, limit: int = 1024 * 1024,
            drain_limit: int | None = None) -> RunResult:
        """Run ``<engine> *args`` and wait at most ``timeout`` seconds.

        ``stdin`` is ``None`` (no input), ``bytes``, or a callable that writes to
        the pipe it is given. Output beyond ``limit`` bytes per stream is counted
        but not kept; beyond ``drain_limit`` the process is killed.
        """
        args = [str(arg) for arg in args]
        for arg in args:
            if "\x00" in arg:
                raise ApiError(400, "bad_request", "Arguments cannot contain NUL characters.")
        try:
            process = subprocess.Popen(
                [self.binary, *args], stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environ, close_fds=True,
                start_new_session=True)
        except OSError as error:
            LOG.error("Cannot start the container engine: %s", error)
            raise ApiError(502, "engine_error", "The container engine could not be started.") from None
        overflow = threading.Event()
        readers = [_Capture(process.stdout, limit, drain_limit, overflow),
                   _Capture(process.stderr, limit, drain_limit, overflow)]
        for reader in readers:
            reader.start()
        failure = []
        writer = None
        if stdin is not None:
            def feed():
                try:
                    if callable(stdin):
                        stdin(process.stdin)
                    else:
                        process.stdin.write(stdin)
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    pass
                except BaseException as error:  # noqa: BLE001 - reported to the caller
                    failure.append(error)
                finally:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        deadline = time.monotonic() + max(0.1, timeout)
        timed_out = False
        while True:
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pass
            if overflow.is_set() or failure:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if writer is not None:
            writer.join(10)
        for reader in readers:
            reader.join(10)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        return RunResult(process.returncode, bytes(readers[0].data), bytes(readers[1].data), readers[0].total,
                         readers[1].total, timed_out, overflow.is_set(), failure[0] if failure else None)


# ----- archives (sanitised on the host, extracted in the container) --------------------------------------

class _SafeTarInfo(tarfile.TarInfo):
    """Refuses oversized metadata headers before tarfile reads them into memory."""

    def _proc_member(self, tarfile_):
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME,
                         tarfile.GNUTYPE_LONGLINK) and self.size > MAX_ARCHIVE_META:
            raise ArchiveError("The archive has an oversized metadata header.")
        return super()._proc_member(tarfile_)


class _Gunzip(io.RawIOBase):
    """Streams a gzip body, raising once more than ``limit`` bytes come out (bomb guard)."""

    def __init__(self, data: bytes, limit: int):
        self._data = data
        self._pos = 0
        self._limit = limit
        self._out = 0
        self._buffer = b""
        self._inflate = zlib.decompressobj(31)
        self._done = False

    def readable(self):
        return True

    def _fill(self) -> None:
        while not self._buffer and not self._done:
            if self._inflate.eof:
                # A further gzip member may follow; restart where the last one ended.
                self._pos -= len(self._inflate.unused_data)
                remaining = len(self._data) - self._pos
                if self._data.count(b"\x00", self._pos) == remaining:
                    self._done = True
                    return
                self._inflate = zlib.decompressobj(31)
            if self._inflate.unconsumed_tail:
                source = self._inflate.unconsumed_tail
            elif self._pos < len(self._data):
                source = self._data[self._pos:self._pos + 65536]
                self._pos += len(source)
            else:
                raise ArchiveError("The gzip data is truncated.")
            try:
                self._buffer = self._inflate.decompress(source, 65536)
            except zlib.error:
                raise ArchiveError("The gzip data is corrupt.") from None
            self._out += len(self._buffer)
            if self._out > self._limit:
                raise ArchiveError("The archive expands to more than the workspace can hold.")

    def readinto(self, target) -> int:
        self._fill()
        if not self._buffer:
            return 0
        count = min(len(target), len(self._buffer))
        target[:count] = self._buffer[:count]
        self._buffer = self._buffer[count:]
        return count


def _member_name(raw) -> str | None:
    """A safe relative name for an archive member, None for the root, or ArchiveError."""
    if not isinstance(raw, str):
        raise ArchiveError("An archive entry has an invalid name.")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise ArchiveError("An archive entry name is not valid UTF-8.") from None
    if "\\" in raw:
        raise ArchiveError(f"The archive entry {raw[:80]!r} contains a backslash.")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ArchiveError("An archive entry name contains control characters.")
    if raw.startswith("/"):
        raise ArchiveError(f"The archive entry {raw[:80]!r} is an absolute path.")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ArchiveError(f"The archive entry {raw[:80]!r} leaves the target directory.")
    if not parts:
        return None
    if any(len(part.encode("utf-8")) > MAX_COMPONENT for part in parts):
        raise ArchiveError("An archive entry name component is too long.")
    name = "/".join(parts)
    if len(name.encode("utf-8")) > MAX_PATH - len(WORKSPACE) - 256:
        raise ArchiveError("An archive entry name is too long.")
    return name


@dataclass
class _Entry:
    kind: str  # "dir" or "file"
    name: str
    size: int = 0
    mode: int = 0o644
    mtime: int = 0
    open: object = None  # callable returning a readable for files


def _clamp_mtime(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(value, int(time.time()) + 86400))


def _sniff(data: bytes) -> str:
    if data[:2] == b"\x1f\x8b":
        return "tar.gz"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if len(data) >= 262 and data[257:262] == b"ustar":
        return "tar"
    raise ApiError(415, "unsupported_media_type", "Upload a .tar.gz, .tar or .zip archive.")


class ArchivePlan:
    """Validated view of an uploaded archive that can be replayed as a clean tar stream.

    Only directories and regular files are kept (symlinks, hard links, devices
    and FIFOs are skipped and counted); names must be relative without ``..``;
    the number of entries, the declared sizes and the actual decompressed bytes
    are capped; owners become 1000:1000 and modes 0644/0755. The upload is never
    written to the host's disk.
    """

    def __init__(self, data: bytes, limit: int):
        self.data = data
        self.limit = int(limit)
        self.kind = _sniff(data)
        self.files = 0
        self.dirs = 0
        self.skipped = 0
        self.size = 0
        # First pass: validate everything (and, for tar, decompress fully) before
        # the container sees a single byte, so a bad archive changes nothing.
        for entry in self.entries():
            if entry.kind == "file":
                stream = entry.open()
                while stream.read(65536):
                    pass

    def _count(self, entry: _Entry) -> None:
        if entry.kind == "skip":
            self.skipped += 1
        elif entry.kind == "dir":
            self.dirs += 1
        else:
            self.files += 1
            self.size += entry.size
            if entry.size > self.limit or self.size > self.limit:
                raise ArchiveError("The archive expands to more than the workspace can hold.")
        if self.files + self.dirs + self.skipped > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError(f"The archive has more than {MAX_ARCHIVE_ENTRIES} entries.")

    def entries(self):
        self.files = self.dirs = self.skipped = self.size = 0
        if self.kind == "zip":
            yield from self._zip_entries()
        else:
            yield from self._tar_entries()

    def _zip_entries(self):
        try:
            archive = zipfile.ZipFile(io.BytesIO(self.data))
            infos = archive.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError):
            raise ArchiveError("The zip file is corrupt.") from None
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError(f"The archive has more than {MAX_ARCHIVE_ENTRIES} entries.")
        declared = 0
        for info in infos:
            declared += max(0, info.file_size)
            if declared > self.limit:
                raise ArchiveError("The archive expands to more than the workspace can hold.")
        for info in infos:
            name = _member_name(info.filename)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if info.flag_bits & 0x1:
                raise ArchiveError("Encrypted zip files are not supported.")
            try:
                mtime = int(datetime(*info.date_time).timestamp())
            except (ValueError, OverflowError, OSError):
                mtime = 0
            if name is None:
                continue
            if info.is_dir():
                entry = _Entry("dir", name, mode=0o755, mtime=_clamp_mtime(mtime))
            elif file_type and file_type != stat.S_IFREG:
                entry = _Entry("skip", name)
            else:
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ArchiveError("Only stored and deflated zip entries are supported.")

                def opener(info=info):
                    return _GuardedZipStream(archive, info)
                entry = _Entry("file", name, size=info.file_size, mode=0o755 if unix_mode & 0o111 else 0o644,
                               mtime=_clamp_mtime(mtime), open=opener)
            self._count(entry)
            yield entry

    def _tar_entries(self):
        slack = MAX_ARCHIVE_ENTRIES * 2048 + 1024 * 1024
        if self.kind == "tar.gz":
            source = io.BufferedReader(_Gunzip(self.data, self.limit + slack), 65536)
        else:
            if len(self.data) > self.limit + slack:
                raise ArchiveError("The archive expands to more than the workspace can hold.")
            source = io.BytesIO(self.data)
        try:
            archive = tarfile.open(fileobj=source, mode="r|", tarinfo=_SafeTarInfo)
        except tarfile.TarError:
            raise ArchiveError("The tar archive is corrupt.") from None
        try:
            while True:
                try:
                    member = archive.next()
                except (tarfile.TarError, EOFError, zlib.error, UnicodeError):
                    raise ArchiveError("The tar archive is corrupt.") from None
                if member is None:
                    break
                name = _member_name(member.name)
                if name is None:
                    continue
                if member.isdir():
                    entry = _Entry("dir", name, mode=0o755, mtime=_clamp_mtime(member.mtime))
                elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and not member.issparse():
                    if member.size < 0:
                        raise ArchiveError("The tar archive is corrupt.")

                    def opener(member=member):
                        return _GuardedTarStream(archive.extractfile(member))
                    entry = _Entry("file", name, size=member.size, mode=0o755 if member.mode & 0o111 else 0o644,
                                   mtime=_clamp_mtime(member.mtime), open=opener)
                else:
                    entry = _Entry("skip", name)
                self._count(entry)
                yield entry
        finally:
            archive.close()

    def write_tar(self, output) -> None:
        """Write the sanitised archive as an uncompressed tar stream to ``output``."""
        with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for entry in self.entries():
                if entry.kind == "skip":
                    continue
                info = tarfile.TarInfo(entry.name)
                info.uid = info.gid = 1000
                info.uname = info.gname = ""
                info.mtime = entry.mtime
                if entry.kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    info.type = tarfile.REGTYPE
                    info.mode = entry.mode
                    info.size = entry.size
                    tar.addfile(info, entry.open())


class _GuardedZipStream:
    def __init__(self, archive, info):
        try:
            self._stream = archive.open(info)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, ValueError, EOFError):
            raise ArchiveError("The zip file is corrupt.") from None

    def read(self, size=-1):
        try:
            return self._stream.read(65536 if size is None or size < 0 else size)
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError):
            raise ArchiveError("The zip file is corrupt.") from None


class _GuardedTarStream:
    def __init__(self, stream):
        self._stream = stream

    def read(self, size=-1):
        try:
            return self._stream.read(65536 if size is None or size < 0 else size)
        except (tarfile.TarError, zlib.error, OSError, EOFError):
            raise ArchiveError("The tar archive is corrupt.") from None


# ----- sandboxes ------------------------------------------------------------------------------------------------

@dataclass
class Sandbox:
    id: str
    name: str
    session: str
    image: str
    limits: dict
    created_at: float
    created_mono: float
    last_used_mono: float
    last_used_at: float
    keeper: str = "1"
    exec_lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    gone: bool = False
    interrupted: bool = False

    def public(self, manager: "SandboxManager") -> dict:
        return {"id": self.id, "session": self.session, "image": self.image, "created_at": _iso(self.created_at),
                "last_used_at": _iso(self.last_used_at), "expires_at": _iso(manager.expires_at(self)),
                "busy": self.exec_lock.locked(), "limits": dict(self.limits)}


class SandboxManager:
    def __init__(self, config: Config, engine: Engine, *, clock=time.monotonic, wall=time.time):
        self.config = config
        self.engine = engine
        self.clock = clock
        self.wall = wall
        self.rootless = None
        self._lock = threading.Lock()
        self._sandboxes: dict[str, Sandbox] = {}
        self._pending: set[str] = set()
        self._doomed: set[str] = set()  # container names whose removal failed; retried by the reaper
        self._removed: OrderedDict[str, None] = OrderedDict()  # recently removed ids (answered with 410)
        self._slots = threading.BoundedSemaphore(config.max_execs)
        self._stop = threading.Event()
        self._reaper = None
        self._health = {"at": -1e9, "ok": False}
        self._health_lock = threading.Lock()
        self._health_refresh = threading.Lock()  # one engine check at a time; concurrent callers share it

    # ----- start-up ------------------------------------------------------------------------------------------
    def _info(self) -> dict:
        args = ["info", "--format", "json"] if self.engine.kind == "podman" else ["info", "--format", "{{json .}}"]
        result = self.engine.run(args, timeout=ENGINE_TIMEOUT, limit=4 * 1024 * 1024)
        if result.returncode != 0 or result.timed_out:
            raise ValueError(f"The container engine does not answer ({self.engine.kind} info failed: "
                             f"{result.stderr.decode('utf-8', 'replace').strip()[:300]}).")
        try:
            info = json.loads(result.stdout)
        except ValueError:
            raise ValueError("The container engine returned unreadable information.") from None
        if not isinstance(info, dict):
            raise ValueError("The container engine returned unreadable information.")
        return info

    def preflight(self) -> None:
        """Check the engine and refuse unsafe setups, then remove leftover sandboxes."""
        config = self.config
        self.engine.check_binary()
        info = self._info()
        if self.engine.kind == "docker":
            options = [str(item) for item in info.get("SecurityOptions") or []]
            rootless = any("rootless" in item for item in options)
            missing = [name for name, key in (("memory", "MemoryLimit"), ("pids", "PidsLimit"),
                                              ("cpu", "CpuCfsQuota")) if info.get(key) is False]
            if not isinstance(info.get("MemoryLimit"), bool) or not isinstance(info.get("PidsLimit"), bool):
                missing.append("memory/pids (not reported)")
            if config.runtime and config.runtime not in (info.get("Runtimes") or {}):
                raise ValueError(f"The Docker runtime {config.runtime!r} is not installed (docker info Runtimes).")
        else:
            host = info.get("host") or {}
            rootless = bool((host.get("security") or {}).get("rootless"))
            controllers = host.get("cgroupControllers")
            if not isinstance(controllers, list):
                controllers = []
            missing = [name for name in ("memory", "pids", "cpu") if name not in controllers]
        if missing:
            raise ValueError("The container engine cannot enforce resource limits (" + ", ".join(missing) + "). "
                             "Use cgroup v2 with delegated controllers (see docs/agents.md).")
        self.rootless = rootless
        if not rootless:
            if not config.allow_rootful:
                raise ValueError(
                    f"{self.engine.kind} runs rootful: a container escape would give root on this machine. "
                    "Use rootless Podman or rootless Docker (recommended, ideally with gVisor: "
                    "BC_SANDBOX_RUNTIME=runsc), or set BC_SANDBOX_ALLOW_ROOTFUL=1 to accept the risk.")
            LOG.warning("SECURITY: %s runs rootful and BC_SANDBOX_ALLOW_ROOTFUL=1 is set; a container escape "
                        "would be root on this machine.%s", self.engine.kind,
                        "" if config.runtime else " Consider gVisor (BC_SANDBOX_RUNTIME=runsc).")
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            LOG.warning("SECURITY: the sandbox runner runs as root; run it as a dedicated unprivileged account.")
        for image in config.images:
            result = self.engine.run(["image", "inspect", "--format", "{{.Id}}", image], timeout=ENGINE_TIMEOUT)
            if result.returncode != 0:
                raise ValueError(f"The image {image} is not present; the runner never pulls images. "
                                 f"Pull it first: {self.engine.kind} pull {image}")
        if config.network != "none":
            LOG.warning("SECURITY: BC_SANDBOX_NETWORK=%s gives every sandbox network access. Agents can then "
                        "reach the internet and possibly your internal network; see docs/agents.md.",
                        config.network)
        self.reconcile()

    def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap_loop, name="sandbox-reaper", daemon=True)
            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(REAP_INTERVAL):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - the reaper must survive anything
                LOG.exception("The sandbox reaper failed")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            boxes = list(self._sandboxes.values())
        for box in boxes:
            self.remove(box, "runner stopping")
        if self._reaper is not None:
            self._reaper.join(5)

    # ----- engine helpers -----------------------------------------------------------------------------------
    def _labels(self) -> list:
        return ["--filter", f"label={LABEL}=1", "--filter", f"label={LABEL_RUNNER}={self.config.instance}"]

    def _list_containers(self) -> dict | None:
        result = self.engine.run(["ps", "-a", "--no-trunc", *self._labels(), "--format", "{{.Names}} {{.State}}"],
                                 timeout=30)
        if result.returncode != 0 or result.timed_out:
            return None
        containers = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split()
            if parts:
                containers[parts[0]] = parts[1].lower() if len(parts) > 1 else ""
        return containers

    def _remove_container(self, name: str) -> bool:
        result = self.engine.run(["rm", "-f", "-v", name], timeout=30)
        text = result.stderr.decode("utf-8", "replace").lower()
        if result.returncode == 0 or "no such container" in text or "no container with name" in text:
            with self._lock:
                self._doomed.discard(name)
            return True
        LOG.error("Removing sandbox container %s failed; will retry", name)
        with self._lock:
            self._doomed.add(name)
        return False

    def _running(self, box: Sandbox) -> bool:
        result = self.engine.run(["inspect", "--format", "{{.State.Running}}", box.name], timeout=15)
        return result.returncode == 0 and result.stdout.strip() == b"true"

    def healthy(self) -> bool:
        # /healthz needs no token: however many requests arrive, the engine is asked at most once per
        # HEALTH_CACHE_SECONDS (callers that arrive during a check wait for its answer).
        with self._health_refresh:
            with self._health_lock:
                if self.clock() - self._health["at"] < HEALTH_CACHE_SECONDS:
                    return self._health["ok"]
            ok = self._list_containers() is not None
            with self._health_lock:
                self._health.update(at=self.clock(), ok=ok)
            return ok

    def reconcile(self) -> None:
        """Remove labelled containers this runner does not know, and forget dead sandboxes."""
        containers = self._list_containers()
        if containers is None:
            LOG.error("Cannot list sandbox containers")
            return
        with self._lock:
            known = {box.name: box for box in self._sandboxes.values()}
            pending = set(self._pending)
        for name, state in containers.items():
            if name in pending:
                continue
            box = known.get(name)
            if box is None:
                LOG.warning("Removing unknown sandbox container %s", name)
                self._remove_container(name)
            elif state != "running":
                self.remove(box, f"container {state or 'stopped'}")
        for name, box in known.items():
            if name not in containers and name not in pending:
                self.remove(box, "container missing")

    def reap(self) -> list:
        """Remove idle and expired sandboxes; returns the removed ids."""
        now = self.clock()
        removed = []
        with self._lock:
            boxes = list(self._sandboxes.values())
            doomed = list(self._doomed)
        for box in boxes:
            idle = not box.active and now - box.last_used_mono > self.config.idle_ttl
            if now - box.created_mono > self.config.max_age or idle:
                self.remove(box, "expired" if now - box.created_mono > self.config.max_age else "idle")
                removed.append(box.id)
        for name in doomed:
            self._remove_container(name)
        self.reconcile()
        return removed

    def expires_at(self, box: Sandbox) -> float:
        idle_deadline = box.last_used_at + self.config.idle_ttl
        return min(box.created_at + self.config.max_age, idle_deadline)

    # ----- lifecycle -------------------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            count = len(self._sandboxes)
        return {"ok": self.healthy(), "engine": self.engine.kind, "rootless": bool(self.rootless),
                "runtime": self.config.runtime or "default", "sandboxes": count, "max": self.config.max_sandboxes,
                "images": list(self.config.images), "network": self.config.network,
                "limits": self.default_limits()}

    def default_limits(self) -> dict:
        config = self.config
        return {"memory_mb": config.memory_mb, "cpus": config.cpus, "workspace_mb": config.workspace_mb,
                "pids": config.pids, "exec_timeout": config.exec_timeout, "output_kb": config.output_kb,
                "network": config.network, "idle_ttl": int(config.idle_ttl), "max_age": int(config.max_age)}

    def _limits(self, request: dict) -> dict:
        limits = self.default_limits()
        for key, minimum, kind in (("memory_mb", 64, int), ("cpus", 0.1, float), ("workspace_mb", 16, int)):
            if key not in request or request[key] is None:
                continue
            value = request[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise ApiError(400, "invalid_limits", f"{key} must be a number.")
            if kind is int and value != int(value):
                raise ApiError(400, "invalid_limits", f"{key} must be a whole number.")
            if value < minimum:
                raise ApiError(400, "invalid_limits", f"{key} must be at least {minimum}.")
            limits[key] = min(kind(value), limits[key])  # requests can only lower the caps
        return limits

    def create_args(self, sandbox_id: str, name: str, session: str, image: str, limits: dict) -> list:
        """The complete ``run`` argument list; every sandbox gets every one of these flags."""
        memory = f"{limits['memory_mb']}m"
        args = [
            "run", "--detach", "--name", name, "--hostname", "sandbox",
            "--label", f"{LABEL}=1", "--label", f"{LABEL_RUNNER}={self.config.instance}",
            "--label", f"{LABEL_SESSION}={session}", "--label", f"{LABEL_ID}={sandbox_id}",
            "--pull", "never", "--restart", "no", "--log-driver", "none", "--no-healthcheck",
            "--network", self.config.network,
            "--read-only",
            "--tmpfs", f"{WORKSPACE}:rw,exec,nosuid,nodev,size={limits['workspace_mb']}m,uid=1000,gid=1000,mode=0700",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={TMP_MB}m,mode=1777",
            "--user", SANDBOX_USER, "--workdir", WORKSPACE, "--env", f"HOME={WORKSPACE}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--ipc", "private",
            "--pids-limit", str(limits["pids"]),
            "--memory", memory, "--memory-swap", memory,
            "--cpus", f"{limits['cpus']:g}",
            "--ulimit", f"nofile={NOFILE}:{NOFILE}", "--ulimit", "core=0",
            "--oom-score-adj", "500",
            "--init",
        ]
        if self.engine.kind == "podman":
            args.append("--read-only-tmpfs=false")
        if self.config.runtime:
            args += ["--runtime", self.config.runtime]
        args += ["--entrypoint", "sleep", image, "infinity"]
        return args

    def _verify(self, box: Sandbox) -> None:
        """Check with ``inspect`` that the engine really applied the hardening flags."""
        result = self.engine.run(["inspect", box.name], timeout=15, limit=4 * 1024 * 1024)
        try:
            data = json.loads(result.stdout)
            data = data[0] if isinstance(data, list) else data
            host = data["HostConfig"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ApiError(502, "engine_error", "The sandbox could not be verified.") from None
        problems = []
        if host.get("Privileged") is not False:
            problems.append("privileged")
        if host.get("ReadonlyRootfs") is not True:
            problems.append("root filesystem not read-only")
        if host.get("Memory") != box.limits["memory_mb"] * 1024 * 1024:
            problems.append("memory limit")
        if host.get("PidsLimit") != box.limits["pids"]:
            problems.append("pids limit")
        if str(host.get("NetworkMode") or "") != self.config.network:
            problems.append("network mode")
        if host.get("Binds") or host.get("Devices"):
            problems.append("host mounts or devices")
        if self.engine.kind == "docker":
            if [str(item).upper() for item in host.get("CapDrop") or []] != ["ALL"] or host.get("CapAdd"):
                problems.append("capabilities")
            if not any("no-new-privileges" in str(item) for item in host.get("SecurityOpt") or []):
                problems.append("no-new-privileges")
            if (data.get("Config") or {}).get("User") != SANDBOX_USER:
                problems.append("user")
        if problems:
            LOG.error("Sandbox %s failed verification: %s", box.id, ", ".join(problems))
            raise ApiError(502, "engine_error", "The container engine did not apply the sandbox restrictions.")

    def create(self, request: dict) -> Sandbox:
        if not isinstance(request, dict):
            raise ApiError(400, "bad_request", "Send a JSON object.")
        session = request.get("session")
        if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
            raise ApiError(400, "invalid_session", "session must be 1-64 characters: letters, digits, _ and -.")
        image = request.get("image") or self.config.images[0]
        if not isinstance(image, str) or image not in self.config.images:
            raise ApiError(400, "image_not_allowed", "This image is not in the runner's allowlist.")
        limits = self._limits(request)
        sandbox_id = secrets.token_hex(16)
        name = NAME_PREFIX + sandbox_id
        with self._lock:
            if len(self._sandboxes) + len(self._pending) + len(self._doomed) >= self.config.max_sandboxes:
                raise ApiError(429, "capacity", "All sandboxes are in use. Retry later.", {"Retry-After": "30"})
            self._pending.add(name)
        try:
            with self._slot():
                result = self.engine.run(self.create_args(sandbox_id, name, session, image, limits),
                                         timeout=ENGINE_TIMEOUT)
                if result.returncode != 0 or result.timed_out:
                    LOG.error("Creating sandbox %s failed: %s", sandbox_id,
                              result.stderr.decode("utf-8", "replace").strip()[:500])
                    self._remove_container(name)
                    raise ApiError(502, "engine_error", "The sandbox could not be created.")
                now_mono, now = self.clock(), self.wall()
                box = Sandbox(sandbox_id, name, session, image, limits, now, now_mono, now_mono, now)
                try:
                    self._verify(box)
                    keeper = self.engine.run(self._exec_args(box, ["sh", "-c", FIND_KEEPER]), timeout=15)
                    value = keeper.stdout.decode("ascii", "replace").strip()
                    if keeper.returncode != 0 or not value.isdigit():
                        raise ApiError(502, "engine_error", "The sandbox did not start correctly.")
                    box.keeper = value
                except BaseException:
                    self._remove_container(name)
                    raise
            with self._lock:  # registered before it stops being pending, so the reaper never sees a gap
                self._sandboxes[sandbox_id] = box
                self._pending.discard(name)
        finally:
            with self._lock:
                self._pending.discard(name)
        LOG.info("Created sandbox %s for session %s (%s)", sandbox_id, session, image)
        return box

    def list(self, session=None) -> list:
        with self._lock:
            boxes = sorted(self._sandboxes.values(), key=lambda item: item.created_at)
        return [box.public(self) for box in boxes if session is None or box.session == session]

    def get(self, sandbox_id: str) -> Sandbox:
        with self._lock:
            box = self._sandboxes.get(sandbox_id)
            removed = sandbox_id in self._removed
        if box is None:
            if removed:  # e.g. a command that could not be killed took it with it: say so, consistently
                raise ApiError(410, "sandbox_gone", "The sandbox was removed.")
            raise ApiError(404, "not_found", "No such sandbox.")
        return box

    def remove(self, box: Sandbox, reason: str) -> None:
        with self._lock:
            if self._sandboxes.get(box.id) is box:
                del self._sandboxes[box.id]
            box.gone = True
            self._removed[box.id] = None
            self._removed.move_to_end(box.id)
            while len(self._removed) > REMEMBER_REMOVED:
                self._removed.popitem(last=False)
        self._remove_container(box.name)
        LOG.info("Removed sandbox %s (%s)", box.id, reason)

    def delete(self, sandbox_id: str) -> None:
        with self._lock:
            box = self._sandboxes.get(sandbox_id)
        if box is not None:
            self.remove(box, "deleted")

    # ----- operations ----------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _slot(self):
        """One of the BC_SANDBOX_MAX_EXECS engine operations allowed at once (503 when all are taken)."""
        if not self._slots.acquire(blocking=False):
            raise ApiError(503, "busy", "The sandbox runner is busy. Retry shortly.", {"Retry-After": "5"})
        try:
            yield
        finally:
            self._slots.release()

    def _exec_args(self, box: Sandbox, argv, *, interactive=False) -> list:
        args = ["exec"]
        if interactive:
            args.append("-i")
        return [*args, "--user", SANDBOX_USER, "--workdir", WORKSPACE, "--env", f"HOME={WORKSPACE}", box.name,
                *argv]

    def _touch(self, box: Sandbox) -> None:
        box.last_used_mono, box.last_used_at = self.clock(), self.wall()

    def _begin(self, box: Sandbox) -> None:
        with self._lock:
            if box.gone:
                raise ApiError(410, "sandbox_gone", "The sandbox no longer exists.")
            box.active += 1
        self._touch(box)

    def _end(self, box: Sandbox) -> None:
        with self._lock:
            box.active -= 1
        self._touch(box)

    def _gone_check(self, box: Sandbox, result: RunResult) -> None:
        """After a failure, find out whether the sandbox itself died (e.g. `kill -9 -1`)."""
        if box.gone:
            raise ApiError(410, "sandbox_gone", "The sandbox was removed.")
        if result.returncode not in (0, None) and not self._running(box):
            self.remove(box, "container stopped")
            raise ApiError(410, "sandbox_gone", "The sandbox stopped (its main process was killed).")

    def kill_all(self, box: Sandbox) -> bool:
        """Kill every process in the sandbox except its idle main process."""
        for attempt in range(2):
            if attempt:
                time.sleep(0.5)
            result = self.engine.run(self._exec_args(box, ["sh", "-c", KILL_SCRIPT, "sh", box.keeper]),
                                     timeout=KILL_TIMEOUT)
            if result.returncode == 0 and not result.timed_out:
                return True
        return False

    def interrupt(self, sandbox_id: str) -> dict:
        box = self.get(sandbox_id)
        box.interrupted = True
        ok = self.kill_all(box)
        if not ok:
            self.remove(box, "interrupt failed")
        return {"ok": True, "sandbox_removed": not ok}

    def execute(self, sandbox_id: str, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ApiError(400, "bad_request", "Send a JSON object.")
        command = request.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ApiError(400, "invalid_command", "command must be a non-empty string.")
        if "\x00" in command or len(command.encode("utf-8", "surrogatepass")) > MAX_COMMAND:
            raise ApiError(400, "invalid_command", f"command must be at most {MAX_COMMAND} bytes without NUL.")
        timeout = request.get("timeout")
        if timeout is None:
            timeout = min(120, self.config.exec_timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
            raise ApiError(400, "invalid_timeout", "timeout must be a positive number of seconds.")
        timeout = min(float(timeout), float(self.config.exec_timeout))
        cwd = clean_path(request.get("cwd") or WORKSPACE)
        box = self.get(sandbox_id)
        if not box.exec_lock.acquire(blocking=False):
            raise ApiError(409, "busy", "Another command is running in this sandbox.")
        try:
            with self._slot():
                self._begin(box)
                try:
                    box.interrupted = False
                    cap = self.config.output_kb * 1024
                    started = time.monotonic()
                    result = self.engine.run(self._exec_args(box, ["sh", "-c", EXEC_WRAPPER, "sh", cwd, command]),
                                             timeout=timeout, limit=cap, drain_limit=DRAIN_LIMIT)
                    duration = int((time.monotonic() - started) * 1000)
                    removed = False
                    if result.timed_out or result.overflow:
                        if not self.kill_all(box):
                            self.remove(box, "could not stop a command")
                            removed = True
                    elif not box.interrupted:
                        self._gone_check(box, result)
                    if box.gone and not removed:
                        raise ApiError(410, "sandbox_gone", "The sandbox was removed while the command ran.")
                finally:
                    self._end(box)
        finally:
            box.exec_lock.release()
        stdout, cut_out = _render(result.stdout, result.stdout_total, cap, result.overflow)
        stderr, cut_err = _render(result.stderr, result.stderr_total, cap, result.overflow)
        if result.overflow:
            stderr += "\n[sandbox: the command produced too much output and was stopped]"
        if result.timed_out:
            stderr += f"\n[sandbox: the command timed out after {timeout:g} s and was stopped]"
        exit_code = result.returncode
        if result.timed_out:
            exit_code = 124
        elif exit_code is None or exit_code < 0:
            exit_code = 137
        LOG.info("sandbox %s exec: exit %s, %d ms%s", box.id, exit_code, duration,
                 " (timed out)" if result.timed_out else "")
        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                "truncated": cut_out or cut_err or result.overflow, "timed_out": result.timed_out,
                "duration_ms": duration, "interrupted": box.interrupted, "sandbox_removed": removed}

    def _file_op(self, box: Sandbox, script: str, *args, stdin=None, timeout=FILE_OP_TIMEOUT, limit=1024 * 1024,
                 drain_limit=None) -> RunResult:
        argv = ["timeout", "-s", "KILL", str(int(timeout)), "sh", "-c", script, "sh", *args]
        self._begin(box)
        try:
            with self._slot():
                result = self.engine.run(self._exec_args(box, argv, interactive=stdin is not None), stdin=stdin,
                                         timeout=timeout + 10, limit=limit, drain_limit=drain_limit)
        finally:
            self._end(box)
        if result.error is not None:
            if isinstance(result.error, ApiError):
                raise result.error
            raise ApiError(500, "internal", "The upload failed.")
        if result.timed_out or result.returncode in (137, -9):
            raise ApiError(504, "timeout", "The sandbox did not answer in time.")
        if result.returncode not in (0, 90, 91, 92, 93, 94, 95, 96, None) or result.returncode is None:
            self._gone_check(box, result)
        return result

    @staticmethod
    def _path_error(code) -> None:
        if code == 91:
            raise ApiError(403, "outside_workspace", "The path resolves outside /workspace.")
        if code == 92:
            raise ApiError(404, "not_found", "No such file or directory.")
        if code == 93:
            raise ApiError(400, "not_regular", "The path is not a regular file or directory.")
        if code == 90:
            raise ApiError(400, "invalid_path", "The path cannot be resolved.")
        if code == 94:
            raise ApiError(409, "not_a_directory", "A parent of the path is not a directory.")
        if code == 95:
            raise ApiError(409, "is_a_directory", "The path is a directory.")
        if code == 96:
            raise ApiError(507, "write_failed", "The file could not be written (is the workspace full?).")

    def read_path(self, sandbox_id: str, path: str) -> dict:
        path = clean_path(path)
        box = self.get(sandbox_id)
        limit = MAX_READ_BYTES + (MAX_LIST_ENTRIES + 1) * (MAX_COMPONENT * 4 + 64) + 64
        result = self._file_op(box, READ_SCRIPT, path, str(MAX_READ_BYTES), str(MAX_LIST_ENTRIES + 1),
                               limit=limit, drain_limit=limit)
        self._path_error(result.returncode)
        if result.returncode != 0 or result.overflow:
            raise ApiError(502, "engine_error", "The path could not be read.")
        head, _, rest = result.stdout.partition(b"\n")
        if head == b"D":
            entries = []
            for record in rest.split(b"\0"):
                if not record:
                    continue
                kind, _, remainder = record.partition(b"\t")
                size, _, name = remainder.partition(b"\t")
                entries.append({"name": name.decode("utf-8", "replace"),
                                "type": {b"f": "file", b"d": "dir", b"l": "symlink"}.get(kind, "other"),
                                "size": int(size) if size.isdigit() else 0})
            truncated = len(entries) > MAX_LIST_ENTRIES
            entries = sorted(entries[:MAX_LIST_ENTRIES], key=lambda item: item["name"])
            return {"path": path, "type": "dir", "entries": entries, "truncated": truncated}
        if head.startswith(b"F\t") and head[2:].isdigit():
            size = int(head[2:])
            content = rest[:MAX_READ_BYTES]
            payload = {"path": path, "type": "file", "size": size,
                       "truncated": size > len(content) or len(rest) > MAX_READ_BYTES}
            try:
                payload.update(content=content.decode("utf-8"), encoding="utf-8")
            except UnicodeDecodeError:
                payload.update(content=base64.b64encode(content).decode("ascii"), encoding="base64")
            return payload
        raise ApiError(502, "engine_error", "The path could not be read.")

    def write_file(self, sandbox_id: str, path: str, data: bytes) -> dict:
        path = clean_path(path, allow_root=False)
        box = self.get(sandbox_id)
        result = self._file_op(box, WRITE_SCRIPT, path, stdin=bytes(data))
        self._path_error(result.returncode)
        value = result.stdout.strip()
        if result.returncode != 0 or not value.isdigit():
            raise ApiError(502, "engine_error", "The file could not be written.")
        return {"path": path, "size": int(value)}

    def download_archive(self, sandbox_id: str) -> bytes:
        box = self.get(sandbox_id)
        argv = ["timeout", "-s", "KILL", str(ARCHIVE_TIMEOUT), "tar", "-c", "-z", "-f", "-", "-C", WORKSPACE, "."]
        self._begin(box)
        try:
            with self._slot():
                result = self.engine.run(self._exec_args(box, argv), timeout=ARCHIVE_TIMEOUT + 10,
                                         limit=MAX_ARCHIVE_DOWNLOAD + 1, drain_limit=MAX_ARCHIVE_DOWNLOAD + 1)
        finally:
            self._end(box)
        if result.overflow or result.stdout_total > MAX_ARCHIVE_DOWNLOAD:
            raise ApiError(413, "too_large", f"The workspace archive is larger than "
                                             f"{MAX_ARCHIVE_DOWNLOAD // (1024 * 1024)} MB.")
        if result.timed_out or result.returncode in (137, -9):
            raise ApiError(504, "timeout", "Archiving the workspace took too long.")
        if result.returncode not in (0, 1):  # GNU tar: 1 = a file changed while being read
            self._gone_check(box, result)
            raise ApiError(502, "engine_error", "The workspace could not be archived.")
        return result.stdout

    def upload_archive(self, sandbox_id: str, data: bytes, path: str = WORKSPACE) -> dict:
        path = clean_path(path)
        box = self.get(sandbox_id)
        plan = ArchivePlan(data, box.limits["workspace_mb"] * 1024 * 1024)
        result = self._file_op(box, UNPACK_SCRIPT, path, stdin=plan.write_tar, timeout=ARCHIVE_TIMEOUT)
        self._path_error(result.returncode)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace")
            if "No space" in message or "Disk quota" in message:
                raise ApiError(507, "write_failed", "The workspace is full; some files may have been written.")
            raise ApiError(502, "engine_error", "The archive could not be extracted; some files may have been "
                                                "written.")
        return {"path": path, "files": plan.files, "directories": plan.dirs, "skipped": plan.skipped,
                "size": plan.size}


def _render(data: bytes, total: int, cap: int, overflow: bool) -> tuple:
    text = data[:cap].decode("utf-8", "replace")
    if total > cap:
        more = "at least " if overflow else ""
        return text + f"\n[output truncated: {more}{total - cap} more bytes]", True
    return text, False


# ----- HTTP -----------------------------------------------------------------------------------------------------

class FailureLimiter:
    """Counts failed authentications per peer; after too many, bad tokens get 429 without a check.

    Valid tokens are still accepted, so a noisy neighbour on the same proxy cannot
    lock the web server out. With a 256-bit token online guessing is hopeless;
    this keeps log noise and wasted work down.
    """

    def __init__(self, limit=AUTH_FAILURES_PER_MINUTE, window=60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque] = {}
        self._logged = -1e9

    def blocked(self, peer: str) -> bool:
        with self._lock:
            events = self._failures.get(peer)
            if not events:
                return False
            now = self.clock()
            while events and now - events[0] > self.window:
                events.popleft()
            return len(events) >= self.limit

    def failed(self, peer: str) -> None:
        with self._lock:
            now = self.clock()
            if peer not in self._failures and len(self._failures) >= 1024:
                self._failures.pop(next(iter(self._failures)))
            events = self._failures.setdefault(peer, deque(maxlen=self.limit * 2))
            events.append(now)
            if now - self._logged > 60:
                self._logged = now
                LOG.warning("Rejected a request with a missing or wrong token from %s", peer)


class RunnerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, address, *, token: str, manager: SandboxManager, max_connections=32, max_per_peer=4,
                 trusted_peers=("127.0.0.1", "::1"), client_timeout=CLIENT_TIMEOUT, header_deadline=HEADER_DEADLINE):
        self.token = check_token(token).encode("ascii")
        self.manager = manager
        self.client_timeout = float(client_timeout)
        self.header_deadline = float(header_deadline)
        max_connections = max(1, int(max_connections))
        self.slots = threading.BoundedSemaphore(max_connections)
        # Connections are admitted before anyone is authenticated: peers other than the trusted ones (the
        # local proxy or SSH tunnel the web server comes through) get a few connections per address and,
        # together, never the last quarter of the slots, so a flood of idle connections cannot lock out
        # the web server.
        self.max_per_peer = max(1, int(max_per_peer))
        self.untrusted_limit = max(1, max_connections - max(1, max_connections // 4))
        self.trusted = tuple(ipaddress.ip_network(item, strict=False) for item in trusted_peers)
        self._peers: dict[str, int] = {}
        self._untrusted = 0
        self._peer_lock = threading.Lock()
        self.transfers = threading.BoundedSemaphore(LARGE_TRANSFERS)
        self.file_transfers = threading.BoundedSemaphore(FILE_TRANSFERS)
        self.limiter = FailureLimiter()
        self._deadlines: dict = {}
        self._deadline_lock = threading.Lock()
        self._closing = threading.Event()
        host = str(address[0])
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)
        self._watchdog = threading.Thread(target=self._watch, name="sandbox-http-watchdog", daemon=True)
        self._watchdog.start()

    # Every connection has a hard deadline (headers first, then the whole request),
    # so slow clients (slowloris) cannot hold a slot forever.
    def set_deadline(self, sock, seconds: float) -> None:
        with self._deadline_lock:
            self._deadlines[sock] = time.monotonic() + seconds

    def clear_deadline(self, sock) -> None:
        with self._deadline_lock:
            self._deadlines.pop(sock, None)

    def _watch(self) -> None:
        while not self._closing.wait(0.25):
            now = time.monotonic()
            with self._deadline_lock:
                expired = [sock for sock, deadline in self._deadlines.items() if deadline <= now]
                for sock in expired:
                    self._deadlines.pop(sock, None)
            for sock in expired:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def server_close(self):
        self._closing.set()
        super().server_close()

    def is_trusted(self, peer: str) -> bool:
        try:
            address = ipaddress.ip_address(str(peer).split("%", 1)[0])
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        address = mapped or address
        return any(address.version == network.version and address in network for network in self.trusted)

    def admit(self, peer: str) -> bool:
        if self.is_trusted(peer):
            return True
        with self._peer_lock:
            if self._peers.get(peer, 0) >= self.max_per_peer or self._untrusted >= self.untrusted_limit:
                return False
            self._peers[peer] = self._peers.get(peer, 0) + 1
            self._untrusted += 1
            return True

    def leave(self, peer: str) -> None:
        if self.is_trusted(peer):
            return
        with self._peer_lock:
            count = self._peers.get(peer, 0) - 1
            if count > 0:
                self._peers[peer] = count
            else:
                self._peers.pop(peer, None)
            self._untrusted = max(0, self._untrusted - 1)

    def process_request(self, request, client_address):
        peer = str(client_address[0])
        if self.admit(peer):
            if self.slots.acquire(blocking=False):
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    self.leave(peer)
                    raise
                return
            self.leave(peer)
        try:
            request.settimeout(1.0)
            body = _json_bytes({"error": {"code": "busy", "message": "The sandbox runner is busy."}})
            request.sendall(("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                             f"Content-Length: {len(body)}\r\nRetry-After: 5\r\nConnection: close\r\n\r\n")
                            .encode("ascii") + body)
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
            self.leave(str(client_address[0]))

    def authorized(self, header) -> bool:
        scheme, _, credential = (header or "").strip().partition(" ")
        if scheme.lower() != "bearer":
            return False
        try:
            supplied = credential.strip().encode("latin-1")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(supplied, self.token)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BananaChat-sandbox"
    sys_version = ""

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.client_timeout)
        self.server.set_deadline(self.connection, self.server.header_deadline)

    def finish(self):
        self.server.clear_deadline(self.connection)
        try:
            super().finish()
        except OSError:
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (socket.timeout, TimeoutError, ConnectionError, OSError):
            self.close_connection = True

    # ----- responses -------------------------------------------------------------------------------------------
    def _headers(self, status, content_type, length, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def reply(self, status: int, payload=None, headers=None) -> None:
        self.close_connection = True
        if status == 204:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        body = _json_bytes(payload)
        self._headers(status, "application/json", len(body), headers)
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, error: ApiError) -> None:
        self.reply(error.status, {"error": {"code": error.code, "message": error.message}}, error.headers)

    def send_bytes(self, data: bytes, content_type: str, headers=None) -> None:
        self.close_connection = True
        # A reader that stalls (or crawls) is dropped: the transfer slot this answer holds is released.
        self.connection.settimeout(SEND_IDLE_TIMEOUT)
        self.server.set_deadline(self.connection, SEND_GRACE + len(data) / SEND_MIN_RATE)
        self._headers(200, content_type, len(data), headers)
        view = memoryview(data)
        for offset in range(0, len(view), SEND_CHUNK):
            self.wfile.write(view[offset:offset + SEND_CHUNK])

    # ----- requests -------------------------------------------------------------------------------------------------
    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_PATCH(self):
        self.route()

    def do_OPTIONS(self):
        self.route()

    def discard_body(self) -> None:
        """Read and drop a small unread body so the client sees the answer, not a reset."""
        if getattr(self, "_body_started", False) or self.headers is None or self.headers.get("Transfer-Encoding"):
            return
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if not 0 < remaining <= MAX_JSON_BODY:
            return
        deadline = time.monotonic() + DRAIN_SECONDS
        try:
            while remaining > 0 and time.monotonic() < deadline:
                data = self.rfile.read1(min(remaining, 65536))
                if not data:
                    break
                remaining -= len(data)
        except (OSError, ValueError):
            pass

    def read_body(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "Send the request body with a Content-Length.")
        raw = self.headers.get("Content-Length")
        if raw is None:
            if self.command in ("PUT", "POST"):
                raise ApiError(411, "length_required", "Send the request body with a Content-Length.")
            return b""
        if not raw.strip().isdigit():
            raise ApiError(400, "bad_request", "Invalid Content-Length.")
        length = int(raw)
        if length > limit:
            raise ApiError(413, "too_large", f"The request body is larger than {limit} bytes.")
        self._body_started = True
        chunks = []
        remaining = length
        while remaining > 0:
            data = self.rfile.read1(min(remaining, 1024 * 1024))
            if not data:
                raise ApiError(400, "bad_request", "Incomplete request body.")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def read_json(self) -> dict:
        body = self.read_body(MAX_JSON_BODY)
        if not body.strip():
            return {}
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_no_constants)
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_request", "The body must be JSON.") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "bad_request", "The body must be a JSON object.")
        return payload

    def query(self) -> dict:
        raw = urlsplit(self.path).query
        if len(raw) > MAX_PATH * 3 + 64:
            raise ApiError(400, "bad_request", "The query string is too long.")
        try:
            values = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=8,
                              errors="strict")
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_request", "Invalid query string.") from None
        result = {}
        for key, items in values.items():
            if len(items) != 1:
                raise ApiError(400, "bad_request", f"Give {key} once.")
            result[key] = items[0]
        return result

    def route(self) -> None:
        self.close_connection = True
        try:
            self._route()
        except ApiError as error:
            self.discard_body()
            try:
                self.fail(error)
            except OSError:
                pass
        except (OSError, socket.timeout):
            pass
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            LOG.exception("Unexpected error handling %s %s", self.command, urlsplit(self.path).path)
            try:
                self.fail(ApiError(500, "internal", "Internal error."))
            except OSError:
                pass

    def _route(self) -> None:
        server = self.server
        manager = server.manager
        path = urlsplit(self.path).path
        if path == "/healthz":
            server.set_deadline(self.connection, HEALTH_DEADLINE)  # the engine check may take a while
            if self.command not in ("GET", "HEAD"):
                raise ApiError(405, "method_not_allowed", "Use GET.", {"Allow": "GET, HEAD"})
            header = self.headers.get("Authorization")
            if header and server.authorized(header):
                status = manager.status()
            else:
                status = {"ok": manager.healthy(), "engine": manager.engine.kind}
            return self.reply(200 if status["ok"] else 503, status)
        peer = str(self.client_address[0])
        if not server.authorized(self.headers.get("Authorization")):
            if server.limiter.blocked(peer):
                raise ApiError(429, "rate_limited", "Too many failed authentications.", {"Retry-After": "60"})
            server.limiter.failed(peer)
            raise ApiError(401, "unauthorized", "A valid sandbox runner token is required.",
                           {"WWW-Authenticate": 'Bearer realm="bananachat-sandbox"'})
        server.set_deadline(self.connection, REQUEST_DEADLINE)  # only authenticated requests get long
        match = ROUTE_RE.fullmatch(path)
        if not match:
            raise ApiError(404, "not_found", "Unknown path.")
        sandbox_id, action = match.group(1), match.group(2)
        method = self.command
        allowed = {None: ("GET", "POST"), "exec": ("POST",), "interrupt": ("POST",), "files": ("GET", "PUT"),
                   "archive": ("GET", "PUT")}
        methods = ("GET", "DELETE") if sandbox_id and not action else allowed[action]
        if method not in methods:
            raise ApiError(405, "method_not_allowed", "Method not allowed.", {"Allow": ", ".join(methods)})
        query = self.query()
        if sandbox_id is None:
            if method == "GET":
                session = query.get("session")
                return self.reply(200, {"sandboxes": manager.list(session)})
            box = manager.create(self.read_json())
            payload = box.public(manager)
            return self.reply(201, {key: payload[key] for key in ("id", "session", "image", "limits", "created_at",
                                                                  "expires_at")})
        if action is None:
            if method == "DELETE":
                manager.delete(sandbox_id)
                return self.reply(204)
            return self.reply(200, manager.get(sandbox_id).public(manager))
        if action == "exec":
            request = self.read_json()
            server.set_deadline(self.connection, manager.config.exec_timeout + EXEC_SLACK)
            return self.reply(200, manager.execute(sandbox_id, request))
        if action == "interrupt":
            self.read_json()
            return self.reply(200, manager.interrupt(sandbox_id))
        if action == "files":
            if method == "GET":
                return self.reply(200, manager.read_path(sandbox_id, query.get("path", WORKSPACE)))
            target = clean_path(query.get("path", ""), allow_root=False)
            manager.get(sandbox_id)
            with self._transfer(self.server.file_transfers):
                data = self.read_body(MAX_FILE_BODY)
                result = manager.write_file(sandbox_id, target, data)
            return self.reply(200, result)
        if method == "GET":
            with self._transfer():
                data = manager.download_archive(sandbox_id)
                return self.send_bytes(data, "application/gzip",
                                   {"Content-Disposition": 'attachment; filename="workspace.tar.gz"'})
        target = clean_path(query.get("path", WORKSPACE))
        manager.get(sandbox_id)
        with self._transfer():
            data = self.read_body(MAX_ARCHIVE_BODY)
            result = manager.upload_archive(sandbox_id, data, target)
        return self.reply(200, result)

    @contextlib.contextmanager
    def _transfer(self, pool=None):
        """Large bodies and archives are held in memory (and decompressed): only a few at once.

        File writes (the agents' write_file/edit_file) use their own pool: an archive download
        that a slow client reads for minutes holds an archive slot, and must not stall them.
        """
        pool = pool or self.server.transfers
        if not pool.acquire(blocking=False):
            raise ApiError(503, "busy", "Too many transfers at once. Retry shortly.", {"Retry-After": "5"})
        try:
            yield
        finally:
            pool.release()


# ----- entry point ----------------------------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_server(environ=None, *, engine: Engine | None = None) -> RunnerServer:
    environ = os.environ if environ is None else environ
    config = Config.from_env(environ)
    token = load_or_create_token(config.token_file)
    engine = engine or Engine(config.engine, config.engine_binary)
    manager = SandboxManager(config, engine)
    manager.preflight()
    if not _is_loopback(config.host):
        LOG.warning("SECURITY: the sandbox runner listens on %s without TLS; put it behind HTTPS (Caddy) or an "
                    "SSH tunnel and keep it on loopback.", config.host)
    server = RunnerServer((config.host, config.port), token=token, manager=manager,
                          max_connections=config.max_connections, max_per_peer=config.max_per_peer,
                          trusted_peers=config.trusted_peers)
    manager.start_reaper()
    return server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        server = create_server()
    except (OSError, ValueError) as error:
        LOG.error("sandbox runner: %s", error)
        return 2

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("Sandbox runner listening on %s:%s (%s, network %s)", *server.server_address[:2],
             server.manager.engine.kind, server.manager.config.network)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        server.manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
