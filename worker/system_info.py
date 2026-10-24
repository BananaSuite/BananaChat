"""Hardware figures for the administrators' Workers page: memory, video memory, disk, processors.

Everything is optional: a figure that cannot be read is left out. ``psutil``
is used when installed; otherwise ``/proc/meminfo`` (Linux), ``sysctl``
(macOS) or ``GlobalMemoryStatusEx`` (Windows). Video memory comes from
``nvidia-smi`` (NVIDIA only). Readings are cached for a minute.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from typing import Dict, Optional, Tuple

CACHE_SECONDS = 60
_cache: Dict[str, Tuple[float, object]] = {}


def _cached(key: str, compute):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        value = compute()
    except Exception:  # noqa: BLE001 - a missing reading is left out
        value = None
    _cache[key] = (time.monotonic(), value)
    return value


def _memory() -> Optional[Tuple[int, int]]:
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    except ImportError:
        pass
    system = platform.system()
    if system == "Linux":
        values = {}
        with open("/proc/meminfo", encoding="ascii") as source:
            for line in source:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    values[name] = int(rest.split()[0]) * 1024
        return values["MemTotal"], values.get("MemAvailable", 0)
    if system == "Darwin":
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
                                   check=True).stdout.strip())
        return total, 0
    if system == "Windows":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys), int(status.ullAvailPhys)
    return None


def _video_memory() -> Optional[Tuple[int, int]]:
    if not shutil.which("nvidia-smi"):
        return None
    options = {"capture_output": True, "text": True, "timeout": 8, "check": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    output = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
                            **options).stdout
    total = free = 0
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) * 1024 * 1024
            free += int(parts[1]) * 1024 * 1024
    return (total, free) if total else None


def models_directory() -> str:
    """Where Ollama keeps its models (``OLLAMA_MODELS``, else its default folder, else the home folder)."""
    configured = os.environ.get("OLLAMA_MODELS")
    if configured and os.path.isdir(configured):
        return configured
    default = os.path.join(os.path.expanduser("~"), ".ollama", "models")
    return default if os.path.isdir(default) else os.path.expanduser("~")


def free_disk() -> Optional[int]:
    try:
        return int(shutil.disk_usage(models_directory()).free)
    except OSError:
        return None


def collect(features, version: str) -> dict:
    memory = _cached("memory", _memory)
    video = _cached("video", _video_memory)
    try:
        disk = shutil.disk_usage(models_directory())
    except OSError:
        disk = None
    result = {"cpu_count": os.cpu_count(), "features": list(features), "version": version}
    if memory:
        result["ram_total"], result["ram_free"] = memory[0], memory[1] or None
    if video:
        result["vram_total"], result["vram_free"] = video
    if disk:
        result["disk_total"], result["disk_free"] = int(disk.total), int(disk.free)
    return {key: value for key, value in result.items() if value is not None}
