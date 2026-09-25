"""Authenticated, single-worker ComfyUI checkpoint download agent."""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import queue
import re
import signal
import stat
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Dict, Mapping, Optional, Tuple


STATE_VERSION = 1
CHUNK_SIZE = 1024 * 1024
MAX_SAFETENSORS_HEADER = 16 * 1024 * 1024
TERMINAL_STATES = {"completed", "failed", "canceled"}
ALLOWED_REDIRECT_HOSTS = frozenset(
    {
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.hf.co",
        "cdn-lfs-eu-1.hf.co",
        "cas-bridge.xethub.hf.co",
    }
)
REPO_PART_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PATH_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AgentError(Exception):
    """An error safe to expose through the API."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class DownloadCanceled(Exception):
    pass


@dataclass(frozen=True)
class AgentConfig:
    host: str
    port: int
    checkpoint_root: Path
    state_dir: Path
    token_file: Path
    hf_token_file: Optional[Path] = None
    queue_size: int = 4
    max_download_bytes: int = 100 * 1024**3
    disk_reserve_bytes: int = 5 * 1024**3
    json_body_limit: int = 32 * 1024
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "AgentConfig":
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError("{} is required".format(name))
            return value

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            raw = env.get(name, str(default))
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError("{} must be an integer".format(name)) from exc
            if not minimum <= value <= maximum:
                raise ValueError("{} is outside the allowed range".format(name))
            return value

        host = env.get("BC_CHECKPOINT_AGENT_HOST", "127.0.0.1").strip()
        if not host:
            raise ValueError("BC_CHECKPOINT_AGENT_HOST must not be empty")
        try:
            timeout = float(env.get("BC_CHECKPOINT_AGENT_REQUEST_TIMEOUT", "30"))
        except ValueError as exc:
            raise ValueError("BC_CHECKPOINT_AGENT_REQUEST_TIMEOUT must be numeric") from exc
        if not 0 < timeout <= 300:
            raise ValueError("BC_CHECKPOINT_AGENT_REQUEST_TIMEOUT is outside the allowed range")
        hf_token = env.get("BC_CHECKPOINT_AGENT_HF_TOKEN_FILE", "").strip()
        return cls(
            host=host,
            port=integer("BC_CHECKPOINT_AGENT_PORT", 8765, 1, 65535),
            checkpoint_root=Path(required("BC_CHECKPOINT_ROOT")),
            state_dir=Path(required("BC_CHECKPOINT_AGENT_STATE_DIR")),
            token_file=Path(required("BC_CHECKPOINT_AGENT_TOKEN_FILE")),
            hf_token_file=Path(hf_token) if hf_token else None,
            queue_size=integer("BC_CHECKPOINT_AGENT_QUEUE_SIZE", 4, 1, 128),
            max_download_bytes=integer(
                "BC_CHECKPOINT_AGENT_MAX_BYTES", 100 * 1024**3, 1024, 1024**5
            ),
            disk_reserve_bytes=integer(
                "BC_CHECKPOINT_AGENT_DISK_RESERVE_BYTES", 5 * 1024**3, 0, 1024**5
            ),
            json_body_limit=integer(
                "BC_CHECKPOINT_AGENT_JSON_BODY_LIMIT", 32 * 1024, 1024, 1024**2
            ),
            request_timeout=timeout,
        )


def _read_secret(path: Path, label: str) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("{} file is unavailable".format(label)) from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("{} file must be a regular mode-0600 file".format(label))
    if path.is_symlink():
        raise ValueError("{} file must not be a symlink".format(label))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600:
            raise ValueError("{} file must be a regular mode-0600 file".format(label))
        data = os.read(fd, 4097)
    finally:
        os.close(fd)
    if not data or len(data) > 4096:
        raise ValueError("{} file has an invalid size".format(label))
    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("{} file is not UTF-8".format(label)) from exc
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("{} file contains an invalid token".format(label))
    return value


def _prepare_directory(path: Path, mode: int) -> Path:
    path = path.absolute()
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError("configured directory must be a non-symlink directory")
    return path


def _validate_repo_id(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 192:
        raise AgentError(400, "invalid_source", "repo_id is invalid")
    parts = value.split("/")
    if len(parts) != 2 or not all(REPO_PART_RE.fullmatch(part) for part in parts):
        raise AgentError(400, "invalid_source", "repo_id is invalid")
    if any(".." in part for part in parts):
        raise AgentError(400, "invalid_source", "repo_id is invalid")
    return value


def _validate_revision(value: Any) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value) or ".." in value:
        raise AgentError(400, "invalid_source", "revision is invalid")
    return value


def _validate_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise AgentError(400, "invalid_request", "{} is invalid".format(field))
    parts = value.split("/")
    if any(
        not part
        or part in {".", ".."}
        or part.startswith(".")
        or not PATH_PART_RE.fullmatch(part)
        for part in parts
    ):
        raise AgentError(400, "invalid_request", "{} is invalid".format(field))
    if not parts[-1].endswith(".safetensors"):
        raise AgentError(400, "invalid_request", "{} must end in .safetensors".format(field))
    return "/".join(parts)


def _parse_download_request(body: Any, maximum_bytes: int) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise AgentError(400, "invalid_request", "JSON body must be an object")
    allowed = {
        "source",
        "target_name",
        "expected_sha256",
        "expected_size",
        "idempotency_key",
    }
    if set(body) - allowed:
        raise AgentError(400, "invalid_request", "request contains unknown fields")
    source = body.get("source")
    if not isinstance(source, dict) or set(source) != {
        "type",
        "repo_id",
        "filename",
        "revision",
    }:
        raise AgentError(400, "invalid_source", "source must contain the required fields")
    if source.get("type") != "huggingface":
        raise AgentError(400, "invalid_source", "source type is not supported")
    expected_hash = body.get("expected_sha256")
    if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
        raise AgentError(400, "invalid_request", "expected_sha256 is required")
    expected_size = body.get("expected_size")
    if expected_size is not None:
        if isinstance(expected_size, bool) or not isinstance(expected_size, int):
            raise AgentError(400, "invalid_request", "expected_size is invalid")
        if expected_size < 1 or expected_size > maximum_bytes:
            raise AgentError(400, "invalid_request", "expected_size is outside the allowed range")
    idempotency_key = body.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise AgentError(400, "invalid_request", "idempotency_key is invalid")
    return {
        "source": {
            "type": "huggingface",
            "repo_id": _validate_repo_id(source.get("repo_id")),
            "filename": _validate_relative_path(source.get("filename"), "source filename"),
            "revision": _validate_revision(source.get("revision")),
        },
        "target_name": _validate_relative_path(body.get("target_name"), "target_name"),
        "expected_sha256": expected_hash.lower(),
        "expected_size": expected_size,
        "idempotency_key": idempotency_key,
    }


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_REDIRECT_HOSTS:
            raise urllib.error.HTTPError(
                newurl, code, "redirect destination is not allowed", headers, fp
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class HuggingFaceFetcher:
    """Construct and open an allowlisted Hugging Face HTTPS source."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_SafeRedirectHandler())

    def open(self, source: Mapping[str, str], hf_token: Optional[str]):
        segments = [
            urllib.parse.quote(part, safe="")
            for part in source["repo_id"].split("/")
        ]
        segments += [
            "resolve",
            urllib.parse.quote(source["revision"], safe=""),
        ]
        segments += [
            urllib.parse.quote(part, safe="")
            for part in source["filename"].split("/")
        ]
        url = "https://huggingface.co/" + "/".join(segments)
        headers = {"User-Agent": "BananaChat-checkpoint-agent/1"}
        if hf_token:
            headers["Authorization"] = "Bearer " + hf_token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return self.opener.open(request, timeout=self.timeout)
        except (urllib.error.URLError, ValueError) as exc:
            raise AgentError(502, "source_unavailable", "checkpoint source is unavailable") from exc


def _json_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def validate_safetensors(handle: BinaryIO, total_size: int) -> None:
    handle.seek(0)
    prefix = handle.read(8)
    if len(prefix) != 8:
        raise AgentError(422, "invalid_safetensors", "checkpoint is not a valid safetensors file")
    header_size = struct.unpack("<Q", prefix)[0]
    if header_size < 2 or header_size > MAX_SAFETENSORS_HEADER or 8 + header_size > total_size:
        raise AgentError(422, "invalid_safetensors", "checkpoint has an invalid safetensors header")
    try:
        header = json.loads(
            handle.read(header_size).decode("utf-8"), object_pairs_hook=_json_no_duplicates
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentError(422, "invalid_safetensors", "checkpoint has an invalid safetensors header") from exc
    if not isinstance(header, dict):
        raise AgentError(422, "invalid_safetensors", "checkpoint has an invalid safetensors header")
    data_size = total_size - 8 - header_size
    for name, tensor in header.items():
        if name == "__metadata__":
            if not isinstance(tensor, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in tensor.items()
            ):
                raise AgentError(422, "invalid_safetensors", "checkpoint metadata is invalid")
            continue
        if not isinstance(name, str) or not name or not isinstance(tensor, dict):
            raise AgentError(422, "invalid_safetensors", "checkpoint tensor metadata is invalid")
        if set(tensor) != {"dtype", "shape", "data_offsets"}:
            raise AgentError(422, "invalid_safetensors", "checkpoint tensor metadata is invalid")
        offsets = tensor.get("data_offsets")
        shape = tensor.get("shape")
        if (
            not isinstance(tensor.get("dtype"), str)
            or not tensor["dtype"]
            or not isinstance(shape, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in shape)
            or not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in offsets)
            or offsets[0] < 0
            or offsets[0] > offsets[1]
            or offsets[1] > data_size
        ):
            raise AgentError(422, "invalid_safetensors", "checkpoint tensor metadata is invalid")


class CheckpointAgent:
    """Persistent job manager with one download worker."""

    def __init__(
        self,
        config: AgentConfig,
        fetcher: Optional[Any] = None,
        start_worker: bool = True,
    ):
        self.config = config
        self.root = _prepare_directory(config.checkpoint_root, 0o755)
        self.state_dir = _prepare_directory(config.state_dir, 0o700)
        self.token = _read_secret(config.token_file, "agent token")
        self.hf_token = (
            _read_secret(config.hf_token_file, "Hugging Face token")
            if config.hf_token_file
            else None
        )
        self.fetcher = fetcher or HuggingFaceFetcher(config.request_timeout)
        self._lock = threading.RLock()
        # One active download plus queue_size pending jobs can exist. Recovery
        # must be able to enqueue all of them after a restart.
        self._work: "queue.Queue[Optional[str]]" = queue.Queue(config.queue_size + 1)
        self._cancel: Dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._state_fd = self._open_directory(self.state_dir)
        self._root_fd = self._open_directory(self.root)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.managed: Dict[str, Dict[str, Any]] = {}
        try:
            self._load_state()
        except BaseException:
            os.close(self._root_fd)
            os.close(self._state_fd)
            raise
        self._worker: Optional[threading.Thread] = None
        if start_worker:
            self._worker = threading.Thread(
                target=self._worker_loop, name="checkpoint-download", daemon=True
            )
            self._worker.start()

    @staticmethod
    def _open_directory(path: Path) -> int:
        return os.open(
            str(path),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    def close(self) -> None:
        self._stop.set()
        for event in self._cancel.values():
            event.set()
        try:
            self._work.put_nowait(None)
        except queue.Full:
            pass
        if self._worker:
            self._worker.join(timeout=5)
        os.close(self._root_fd)
        os.close(self._state_fd)

    def _load_state(self) -> None:
        try:
            fd = os.open(
                "state.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self._state_fd
            )
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("agent state file must be mode 0600")
                raw = handle.read(8 * 1024 * 1024 + 1)
        except OSError as exc:
            raise ValueError("agent state cannot be read") from exc
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("agent state is too large")
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("agent state is invalid") from exc
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ValueError("agent state version is invalid")
        jobs = state.get("jobs")
        managed = state.get("managed")
        if not isinstance(jobs, dict) or not isinstance(managed, dict):
            raise ValueError("agent state is invalid")
        self.jobs = jobs
        self.managed = managed
        changed = False
        for job_id, job in self.jobs.items():
            if (
                not isinstance(job_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", job_id)
                or not isinstance(job, dict)
                or job.get("id") != job_id
                or job.get("status") not in {
                    "queued",
                    "downloading",
                    "completed",
                    "failed",
                    "canceled",
                }
            ):
                raise ValueError("agent job state is invalid")
            try:
                persisted_request = {
                    key: job.get(key)
                    for key in (
                        "source",
                        "target_name",
                        "expected_sha256",
                        "expected_size",
                        "idempotency_key",
                    )
                }
                parsed = _parse_download_request(
                    persisted_request, self.config.max_download_bytes
                )
            except AgentError as exc:
                raise ValueError("agent job state is invalid") from exc
            if any(job.get(key) != value for key, value in parsed.items()):
                raise ValueError("agent job state is invalid")
            if job.get("status") in {"queued", "downloading"}:
                job["status"] = "queued"
                job["bytes_received"] = 0
                job["updated_at"] = _now()
                job["error"] = None
                self._cancel[job_id] = threading.Event()
                try:
                    self._work.put_nowait(job_id)
                except queue.Full:
                    job["status"] = "failed"
                    job["error"] = {"code": "recovery_queue_full", "message": "job could not be recovered"}
                changed = True
        for name, item in self.managed.items():
            try:
                valid_name = _validate_relative_path(name, "managed checkpoint name")
            except AgentError as exc:
                raise ValueError("managed checkpoint state is invalid") from exc
            if (
                valid_name != name
                or not isinstance(item, dict)
                or item.get("name") != name
                or not isinstance(item.get("digest"), str)
                or not SHA256_RE.fullmatch(item["digest"])
                or isinstance(item.get("size"), bool)
                or not isinstance(item.get("size"), int)
                or item["size"] < 1
            ):
                raise ValueError("managed checkpoint state is invalid")
        if changed:
            self._persist_locked()

    def _persist_locked(self) -> None:
        payload = json.dumps(
            {"version": STATE_VERSION, "jobs": self.jobs, "managed": self.managed},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = ".state-{}.tmp".format(uuid.uuid4().hex)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=self._state_fd,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, "state.json", src_dir_fd=self._state_fd, dst_dir_fd=self._state_fd)
            os.fsync(self._state_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self._state_fd)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _public_job(job: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            key: job.get(key)
            for key in (
                "id",
                "status",
                "source",
                "target_name",
                "expected_sha256",
                "expected_size",
                "idempotency_key",
                "bytes_received",
                "digest",
                "size",
                "created_at",
                "updated_at",
                "error",
            )
        }

    def authenticate(self, header: Optional[str]) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        candidate = header[7:]
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = {name: 0 for name in ("queued", "downloading", "completed", "failed", "canceled")}
            for job in self.jobs.values():
                state = job.get("status")
                if state in counts:
                    counts[state] += 1
            return {
                "status": "ok",
                "worker": "running" if self._worker and self._worker.is_alive() else "stopped",
                "queue_depth": counts["queued"],
                "queue_capacity": self.config.queue_size,
                "jobs": counts,
            }

    def submit(self, body: Any) -> Tuple[Dict[str, Any], bool]:
        request = _parse_download_request(body, self.config.max_download_bytes)
        fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            for existing in self.jobs.values():
                if existing.get("idempotency_key") == request["idempotency_key"]:
                    if existing.get("request_fingerprint") != fingerprint:
                        raise AgentError(409, "idempotency_conflict", "idempotency key was already used")
                    return self._public_job(existing), False
            if sum(job.get("status") == "queued" for job in self.jobs.values()) >= self.config.queue_size:
                raise AgentError(429, "queue_full", "download queue is full")
            now = _now()
            job_id = uuid.uuid4().hex
            job = dict(request)
            job.update(
                {
                    "id": job_id,
                    "status": "queued",
                    "bytes_received": 0,
                    "digest": None,
                    "size": None,
                    "created_at": now,
                    "updated_at": now,
                    "error": None,
                    "request_fingerprint": fingerprint,
                }
            )
            self.jobs[job_id] = job
            self._cancel[job_id] = threading.Event()
            self._persist_locked()
            try:
                self._work.put_nowait(job_id)
            except queue.Full:
                self.jobs.pop(job_id, None)
                self._cancel.pop(job_id, None)
                self._persist_locked()
                raise AgentError(429, "queue_full", "download queue is full") from None
            return self._public_job(job), True

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                raise AgentError(404, "not_found", "download job was not found")
            return self._public_job(job)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                raise AgentError(404, "not_found", "download job was not found")
            if job["status"] in TERMINAL_STATES:
                raise AgentError(409, "job_terminal", "download job is already finished")
            self._cancel.setdefault(job_id, threading.Event()).set()
            if job["status"] == "queued":
                job["status"] = "canceled"
                job["updated_at"] = _now()
                job["error"] = {"code": "canceled", "message": "download was canceled"}
                self._persist_locked()
            return self._public_job(job)

    def list_checkpoints(self) -> Dict[str, Any]:
        with self._lock:
            result = []
            for name, item in sorted(self.managed.items()):
                if self._managed_file_exists(name, item.get("size")):
                    result.append(dict(item))
            return {"checkpoints": result}

    def delete_checkpoint(self, name: str, if_match: Optional[str]) -> Dict[str, Any]:
        name = _validate_relative_path(name, "name")
        if not if_match:
            raise AgentError(428, "precondition_required", "If-Match is required")
        supplied = if_match.strip()
        if supplied.startswith('"') and supplied.endswith('"') and len(supplied) >= 2:
            supplied = supplied[1:-1]
        if not SHA256_RE.fullmatch(supplied):
            raise AgentError(412, "precondition_failed", "If-Match does not match")
        supplied = supplied.lower()
        with self._lock:
            item = self.managed.get(name)
            if not item:
                raise AgentError(404, "not_found", "managed checkpoint was not found")
            if not hmac.compare_digest(supplied, str(item.get("digest", ""))):
                raise AgentError(412, "precondition_failed", "If-Match does not match")
            parent_fd, leaf = self._open_parent(name, create=False)
            try:
                fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                try:
                    before = os.fstat(fd)
                    digest = _hash_fd(fd)
                    current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                finally:
                    os.close(fd)
                if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
                    current.st_dev,
                    current.st_ino,
                ):
                    raise AgentError(409, "checkpoint_changed", "checkpoint changed during verification")
                if not hmac.compare_digest(digest, supplied):
                    raise AgentError(412, "precondition_failed", "checkpoint content does not match")
                os.unlink(leaf, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError as exc:
                raise AgentError(404, "not_found", "managed checkpoint was not found") from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise AgentError(409, "checkpoint_changed", "managed checkpoint is not a regular file") from exc
                raise
            finally:
                os.close(parent_fd)
            del self.managed[name]
            self._persist_locked()
            return {"deleted": name, "digest": supplied}

    def _managed_file_exists(self, name: str, expected_size: Any) -> bool:
        try:
            parent_fd, leaf = self._open_parent(name, create=False)
            try:
                info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                return stat.S_ISREG(info.st_mode) and info.st_size == expected_size
            finally:
                os.close(parent_fd)
        except (OSError, AgentError):
            return False

    def _open_parent(self, relative: str, create: bool) -> Tuple[int, str]:
        parts = relative.split("/")
        fd = os.dup(self._root_fd)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise AgentError(409, "unsafe_target", "target path is not safe") from exc
            raise

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._work.get(timeout=0.25)
            except queue.Empty:
                continue
            if job_id is None:
                self._work.task_done()
                return
            try:
                self._run_job(job_id)
            finally:
                self._work.task_done()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] != "queued":
                return
            event = self._cancel.setdefault(job_id, threading.Event())
            if event.is_set():
                return
            job["status"] = "downloading"
            job["updated_at"] = _now()
            self._persist_locked()
            snapshot = dict(job)
        try:
            digest, size = self._download(snapshot, event)
        except DownloadCanceled:
            if self._stop.is_set():
                self._requeue_for_restart(job_id)
            else:
                self._finish_error(job_id, "canceled", "canceled", "download was canceled")
        except AgentError as exc:
            if self._stop.is_set():
                self._requeue_for_restart(job_id)
            else:
                self._finish_error(job_id, "failed", exc.code, exc.message)
        except Exception:
            if self._stop.is_set():
                self._requeue_for_restart(job_id)
            else:
                self._finish_error(job_id, "failed", "download_failed", "download failed")
        else:
            with self._lock:
                job = self.jobs[job_id]
                job.update(
                    {
                        "status": "completed",
                        "bytes_received": size,
                        "digest": digest,
                        "size": size,
                        "updated_at": _now(),
                        "error": None,
                    }
                )
                self.managed[job["target_name"]] = {
                    "name": job["target_name"],
                    "digest": digest,
                    "size": size,
                    "installed_at": job["updated_at"],
                    "job_id": job_id,
                }
                self._persist_locked()

    def _requeue_for_restart(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = "queued"
            job["bytes_received"] = 0
            job["updated_at"] = _now()
            job["error"] = None
            self._persist_locked()

    def _finish_error(self, job_id: str, status_value: str, code: str, message: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = status_value
            job["updated_at"] = _now()
            job["error"] = {"code": code, "message": message}
            self._persist_locked()

    def _download(self, job: Mapping[str, Any], canceled: threading.Event) -> Tuple[str, int]:
        parent_fd, leaf = self._open_parent(job["target_name"], create=True)
        temporary = ".{}.{}.part".format(leaf, job["id"])
        fd = -1
        response = None
        try:
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise AgentError(409, "target_exists", "target checkpoint already exists")
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            fd = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
                dir_fd=parent_fd,
            )
            response = self.fetcher.open(job["source"], self.hf_token)
            content_length = _content_length(getattr(response, "headers", {}))
            expected_size = job.get("expected_size")
            if content_length is not None and content_length > self.config.max_download_bytes:
                raise AgentError(413, "source_too_large", "checkpoint exceeds the configured size limit")
            if expected_size is not None and content_length is not None and content_length != expected_size:
                raise AgentError(422, "size_mismatch", "checkpoint size does not match")
            hasher = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "w+b", closefd=False) as handle:
                while True:
                    if canceled.is_set() or self._stop.is_set():
                        raise DownloadCanceled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise AgentError(502, "source_invalid", "checkpoint source returned invalid data")
                    total += len(chunk)
                    if total > self.config.max_download_bytes:
                        raise AgentError(413, "source_too_large", "checkpoint exceeds the configured size limit")
                    available = os.fstatvfs(parent_fd).f_bavail * os.fstatvfs(parent_fd).f_frsize
                    if available - len(chunk) < self.config.disk_reserve_bytes:
                        raise AgentError(507, "insufficient_storage", "disk reserve would be exceeded")
                    handle.write(chunk)
                    hasher.update(chunk)
                    with self._lock:
                        current = self.jobs.get(job["id"])
                        if current:
                            current["bytes_received"] = total
                            current["updated_at"] = _now()
                handle.flush()
                os.fsync(handle.fileno())
                if expected_size is not None and total != expected_size:
                    raise AgentError(422, "size_mismatch", "checkpoint size does not match")
                digest = hasher.hexdigest()
                if not hmac.compare_digest(digest, job["expected_sha256"]):
                    raise AgentError(422, "hash_mismatch", "checkpoint digest does not match")
                validate_safetensors(handle, total)
            if canceled.is_set() or self._stop.is_set():
                raise DownloadCanceled()
            try:
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise AgentError(409, "target_exists", "target checkpoint already exists") from exc
            if canceled.is_set() or self._stop.is_set():
                os.unlink(leaf, dir_fd=parent_fd)
                raise DownloadCanceled()
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return digest, total
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)


def _content_length(headers: Any) -> Optional[int]:
    try:
        value = headers.get("Content-Length")
        if value is None:
            return None
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError) as err:
        raise AgentError(502, "source_invalid", "checkpoint source returned invalid metadata") from err


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, agent: CheckpointAgent):
        self.agent = agent
        super().__init__(address, CheckpointRequestHandler)


class CheckpointRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "checkpoint-agent"
    sys_version = ""

    @property
    def agent(self) -> CheckpointAgent:
        return self.server.agent  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if method == "GET" and parsed.path == "/healthz" and not parsed.query:
                self._json(200, {"status": "ok"})
                return
            if not self.agent.authenticate(self.headers.get("Authorization")):
                self._json(
                    401,
                    {"error": {"code": "unauthorized", "message": "authentication required"}},
                    {"WWW-Authenticate": 'Bearer realm="checkpoint-agent"'},
                )
                return
            if method == "GET" and parsed.path == "/v1/status" and not parsed.query:
                self._json(200, self.agent.status())
                return
            if method == "POST" and parsed.path == "/v1/downloads" and not parsed.query:
                job, created = self.agent.submit(self._read_json())
                self._json(202 if created else 200, job)
                return
            match = re.fullmatch(r"/v1/downloads/([0-9a-f]{32})", parsed.path)
            if match and not parsed.query:
                if method == "GET":
                    self._json(200, self.agent.get_job(match.group(1)))
                    return
                if method == "DELETE":
                    self._json(202, self.agent.cancel(match.group(1)))
                    return
            if method == "GET" and parsed.path == "/v1/checkpoints" and not parsed.query:
                self._json(200, self.agent.list_checkpoints())
                return
            if method == "DELETE" and parsed.path == "/v1/checkpoints":
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                if set(query) != {"name"} or len(query["name"]) != 1:
                    raise AgentError(400, "invalid_request", "exactly one name parameter is required")
                self._json(
                    200,
                    self.agent.delete_checkpoint(query["name"][0], self.headers.get("If-Match")),
                )
                return
            raise AgentError(404, "not_found", "endpoint was not found")
        except AgentError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, KeyError):
            self._json(400, {"error": {"code": "invalid_request", "message": "request is invalid"}})
        except Exception:
            self._json(500, {"error": {"code": "internal_error", "message": "internal error"}})

    def _read_json(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AgentError(415, "unsupported_media_type", "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as exc:
            raise AgentError(400, "invalid_request", "Content-Length is invalid") from exc
        if length < 0:
            raise AgentError(411, "length_required", "Content-Length is required")
        if length == 0 or length > self.agent.config.json_body_limit:
            raise AgentError(413, "body_too_large", "JSON body size is invalid")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(400, "invalid_json", "JSON body is invalid") from exc

    def _json(
        self, status_code: int, payload: Any, extra_headers: Optional[Mapping[str, str]] = None
    ) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


def create_server(
    config: Optional[AgentConfig] = None, fetcher: Optional[Any] = None
) -> AgentHTTPServer:
    configured = config or AgentConfig.from_env()
    agent = CheckpointAgent(configured, fetcher=fetcher)
    try:
        return AgentHTTPServer((configured.host, configured.port), agent)
    except BaseException:
        agent.close()
        raise


def main(argv: Optional[Any] = None) -> int:
    parser = argparse.ArgumentParser(description="BananaChat checkpoint download agent")
    parser.parse_args(argv)
    try:
        server = create_server()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    stopping = threading.Event()

    def stop(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        server.agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
