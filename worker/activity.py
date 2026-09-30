"""What the PC's owner is doing: idle time and GPU utilisation, on Linux, macOS and Windows.

States (thresholds from the settings; defaults in brackets):

* ``gaming`` - GPU utilisation at or above ``BC_GPU_GAMING_THRESHOLD`` [70 %]:
  the worker takes no new job and returns jobs it has not started;
* ``idle`` - no input for ``BC_IDLE_THRESHOLD`` [300 s] and a calm GPU (< 30 %):
  inference runs at normal priority;
* ``light`` - no input for ``BC_LIGHT_THRESHOLD`` [30 s]: below-normal priority;
* ``active`` - someone is using the PC: below-normal priority.

Without an idle reading (headless, some Wayland sessions) the GPU decides:
below ``BC_GPU_ACTIVE_THRESHOLD`` [50 %] is idle, above it active. Without any
reading the PC counts as idle. macOS has no GPU reading (Apple Silicon offers
no unprivileged counter and ``nvidia-smi`` does not exist there), so the idle
reading from IOKit decides alone.

While the worker itself runs a job its own inference loads the GPU, so the GPU
alone then means ``gaming`` only when someone is also at the keyboard.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("bananachat.worker.activity")

_OS = platform.system()  # 'Windows' | 'Linux' | 'Darwin'
STATES = ("idle", "light", "active", "gaming")
CALM_GPU = 30.0
_UNSET = object()


def _run(command, timeout: float = 3.0) -> Optional[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result.stdout if result.returncode == 0 else None


# ----- GPU -------------------------------------------------------------------------------------

_nvml = {"ready": None}


def _nvml_module():
    if _nvml["ready"] is False:
        return None
    try:
        import pynvml  # type: ignore
        if not _nvml["ready"]:
            pynvml.nvmlInit()
            _nvml["ready"] = True
        return pynvml
    except Exception:  # noqa: BLE001 - optional dependency, missing driver
        _nvml["ready"] = False
        return None


def _gpu_util_nvml() -> Optional[float]:
    pynvml = _nvml_module()
    if pynvml is None:
        return None
    try:
        weighted, memory = 0.0, 0
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            total = pynvml.nvmlDeviceGetMemoryInfo(handle).total
            weighted += pynvml.nvmlDeviceGetUtilizationRates(handle).gpu * total
            memory += total
        return weighted / memory if memory else None
    except Exception:  # noqa: BLE001
        return None


def parse_smi_utilisation(output: str) -> Optional[float]:
    """Memory-weighted utilisation from ``nvidia-smi --query-gpu=memory.total,utilization.gpu`` CSV."""
    rows = []
    for line in (output or "").strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    memory = sum(row[0] for row in rows)
    if not rows or memory <= 0:
        return None
    return sum(total * util for total, util in rows) / memory


def get_gpu_utilisation() -> Optional[float]:
    """Utilisation (%) across NVIDIA GPUs, or None when unknown."""
    if _OS == "Darwin":
        return None
    value = _gpu_util_nvml()
    if value is not None:
        return value
    output = _run(["nvidia-smi", "--query-gpu=memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
    return parse_smi_utilisation(output) if output else None


def _gpu_name_macos() -> Optional[str]:
    if _gpu_name_macos._cached is not _UNSET:  # type: ignore[attr-defined]
        return _gpu_name_macos._cached  # type: ignore[attr-defined]
    name = None
    if platform.machine() == "arm64":
        output = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        name = output.strip() if output and output.strip() else None
    if name is None:
        output = _run(["system_profiler", "SPDisplaysDataType"], timeout=15)
        if output:
            models = [line.split(":", 1)[1].strip() for line in output.splitlines() if "Chipset Model:" in line]
            name = " + ".join(model for model in models if model) or None
    _gpu_name_macos._cached = name  # type: ignore[attr-defined]
    return name


_gpu_name_macos._cached = _UNSET  # type: ignore[attr-defined]


def get_gpu_name() -> Optional[str]:
    if _OS == "Darwin":
        return _gpu_name_macos()
    pynvml = _nvml_module()
    if pynvml is not None:
        try:
            names = []
            for index in range(pynvml.nvmlDeviceGetCount()):
                name = pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(index))
                names.append(name.decode() if isinstance(name, bytes) else str(name))
            if names:
                return " + ".join(names)
        except Exception:  # noqa: BLE001
            pass
    output = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if output:
        names = [line.strip() for line in output.splitlines() if line.strip()]
        return " + ".join(names) or None
    return None


# ----- idle time -------------------------------------------------------------------------------

def _idle_seconds_windows() -> Optional[float]:
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
            return None
        ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF  # type: ignore[attr-defined]
        return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0  # wraps after 49.7 days
    except Exception:  # noqa: BLE001
        return None


def parse_dbus_integer(output: Optional[str]) -> Optional[int]:
    """The value of a ``dbus-send --print-reply`` answer such as ``   uint64 12345``."""
    for line in (output or "").splitlines():
        tokens = line.split()
        if len(tokens) >= 2 and tokens[0] in ("uint32", "uint64", "int32", "int64"):
            try:
                return int(tokens[1])
            except ValueError:
                return None
    return None


def _idle_seconds_linux() -> Optional[float]:
    output = _run(["xprintidle"], timeout=2)
    if output and output.strip().isdigit():
        return int(output.strip()) / 1000.0
    for command in (
        ["dbus-send", "--session", "--print-reply", "--dest=org.gnome.Mutter.IdleMonitor",
         "/org/gnome/Mutter/IdleMonitor/Core", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
        ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.ScreenSaver", "/ScreenSaver",
         "org.freedesktop.ScreenSaver.GetSessionIdleTime"],
    ):
        value = parse_dbus_integer(_run(command, timeout=2))
        if value is not None:
            return value / 1000.0
    return None


def parse_ioreg_idle(output: Optional[str]) -> Optional[float]:
    """Seconds from IOKit's ``HIDIdleTime`` (nanoseconds) in ``ioreg -c IOHIDSystem`` output."""
    for line in (output or "").splitlines():
        if "HIDIdleTime" not in line:
            continue
        value = line.partition("=")[2].strip().strip("<>").strip()
        try:
            nanoseconds = int(value)
        except ValueError:
            continue
        return nanoseconds / 1e9 if nanoseconds >= 0 else None
    return None


def _idle_seconds_macos() -> Optional[float]:
    return parse_ioreg_idle(_run(["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r"], timeout=5))


def get_user_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard or mouse input, or None when unknown."""
    if _OS == "Windows":
        return _idle_seconds_windows()
    if _OS == "Darwin":
        return _idle_seconds_macos()
    return _idle_seconds_linux()


# ----- classification ----------------------------------------------------------------------------

def classify(gpu: Optional[float], idle: Optional[float], settings, *, own_job: bool = False) -> str:
    """The activity state for these readings (see the module docstring)."""
    if gpu is not None and gpu >= settings.gpu_gaming_threshold:
        if not own_job or (idle is not None and idle < settings.light_threshold):
            return "gaming"
        gpu = None  # the load is (most likely) our own inference
    if idle is not None:
        if idle >= settings.idle_threshold:
            return "idle" if gpu is None or gpu < CALM_GPU else "light"
        if idle >= settings.light_threshold:
            return "light"
        return "active"
    if gpu is None or gpu < settings.gpu_active_threshold:
        return "idle"
    return "active"


class Monitor:
    """Samples the readings every ``activity_interval`` seconds in a background thread."""

    def __init__(self, settings, *, gpu_reader=get_gpu_utilisation, idle_reader=get_user_idle_seconds):
        self.settings = settings
        self._gpu_reader = gpu_reader
        self._idle_reader = idle_reader
        self._lock = threading.Lock()
        self.state = "idle"
        self.gpu: Optional[float] = None
        self.idle: Optional[float] = None
        self.own_job = False
        self.sampled_at = 0.0

    def sample(self) -> str:
        gpu, idle = self._gpu_reader(), self._idle_reader()
        with self._lock:
            state = classify(gpu, idle, self.settings, own_job=self.own_job)
            if state != self.state:
                log.info("Activity: %s -> %s (GPU %s, idle %s)", self.state, state,
                         "n/a" if gpu is None else f"{gpu:.0f}%", "n/a" if idle is None else f"{idle:.0f}s")
            self.state, self.gpu, self.idle, self.sampled_at = state, gpu, idle, time.monotonic()
            return state

    def run(self, stop: threading.Event, on_change=None) -> None:
        while not stop.is_set():
            before = self.state
            try:
                state = self.sample()
            except Exception:  # noqa: BLE001 - a reading must never stop the worker
                log.debug("Activity sampling failed", exc_info=True)
                state = before
            if on_change is not None and state != before:
                on_change(state)
            stop.wait(self.settings.activity_interval)
