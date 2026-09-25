"""Cross-platform system activity and GPU utilisation monitoring.

Supports Windows, all common Linux distributions (X11, Wayland, Mir,
headless) and macOS on both Intel and Apple Silicon.  Each platform uses the
best available mechanism and falls back gracefully when optional tools are
absent.

macOS has no NVIDIA telemetry to read: Apple Silicon has no supported
per-process GPU counter outside root-only powermetrics, and Intel Macs run
AMD or Intel parts that nvidia-smi knows nothing about.  So on a Mac the GPU
gate is simply absent and the idle-time reading carries the decision, which
is why idle detection there is not optional.

Activity states
---------------
  idle: user idle ≥ IDLE_THRESHOLD_SECONDS AND gpu_util < 30 %
            → run inference at full priority, keep Ollama loaded
  light: user idle ≥ LIGHT_THRESHOLD_SECONDS OR gpu_util < GPU_ACTIVE_THRESHOLD
            → run inference at normal / below-normal priority
  active: user recently active AND gpu_util < GPU_GAMING_THRESHOLD
            → run inference at below-normal priority
  gaming: gpu_util ≥ GPU_GAMING_THRESHOLD
            → do NOT start new inference; wait for GPU to settle
"""

import logging
import platform
import subprocess
import time

import config

_logger = logging.getLogger("bananachat.worker.activity")

_OS = platform.system()  # 'Windows' | 'Linux' | 'Darwin'


# GPU utilisation

def _gpu_util_pynvml() -> float | None:
    """Query GPU utilisation via pynvml (nvidia-ml-py3).  Returns % or None."""
    try:
        import pynvml  # type: ignore
        if not _gpu_util_pynvml._init:
            pynvml.nvmlInit()
            _gpu_util_pynvml._init = True
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            return None
        total_util = 0.0
        total_mem  = 0
        for i in range(count):
            h   = pynvml.nvmlDeviceGetHandleByIndex(i)
            u   = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            total_util += u.gpu * mem.total
            total_mem  += mem.total
        return total_util / total_mem if total_mem > 0 else 0.0
    except Exception:
        return None

_gpu_util_pynvml._init = False  # type: ignore[attr-defined]


def _gpu_util_smi() -> float | None:
    """Query GPU utilisation via nvidia-smi subprocess.  Returns % or None."""
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode != 0:
            return None
        rows = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2:
                rows.append((float(parts[0]), float(parts[1])))
        if not rows:
            return None
        total_mem  = sum(r[0] for r in rows)
        weighted   = sum(r[1] * r[0] for r in rows) / total_mem if total_mem else 0.0
        return weighted
    except Exception:
        return None


def get_gpu_utilisation() -> float | None:
    """Return the weighted-average GPU utilisation % across all NVIDIA GPUs,
    or None if no GPU telemetry is available."""
    result = _gpu_util_pynvml()
    if result is not None:
        return result
    return _gpu_util_smi()


def _gpu_name_macos() -> str | None:
    """Name the Apple Silicon SoC, or the discrete GPU on an Intel Mac.

    Cached: system_profiler takes about a second, and the answer cannot
    change while the worker is running.
    """
    if _gpu_name_macos._cached is not _UNSET:
        return _gpu_name_macos._cached
    name = None
    if platform.machine() == "arm64":
        try:
            proc = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True, timeout=3)
            if proc.returncode == 0 and proc.stdout.strip():
                name = proc.stdout.strip()
        except Exception:
            name = None
    if name is None:
        try:
            proc = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                  capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                models = [line.split(":", 1)[1].strip()
                          for line in proc.stdout.splitlines()
                          if "Chipset Model:" in line]
                name = " + ".join(m for m in models if m) or None
        except Exception:
            name = None
    _gpu_name_macos._cached = name
    return name

_UNSET = object()
_gpu_name_macos._cached = _UNSET  # type: ignore[attr-defined]


def get_gpu_name() -> str | None:
    """Return a string describing the GPU(s), or None."""
    if _OS == "Darwin":
        return _gpu_name_macos()
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        names = []
        for i in range(count):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            names.append(pynvml.nvmlDeviceGetName(h))
        return " + ".join(names) if names else None
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if proc.returncode == 0:
            names = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
            return " + ".join(names) if names else None
    except Exception:
        pass
    return None


# User idle time

def _idle_seconds_windows() -> float:
    """Milliseconds since last keyboard/mouse event via Win32 GetLastInputInfo."""
    import ctypes
    import ctypes.wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint),
        ]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))  # type: ignore
    tick_ms = ctypes.windll.kernel32.GetTickCount()           # type: ignore
    idle_ms = tick_ms - lii.dwTime
    return max(0, idle_ms) / 1000.0


def _idle_seconds_xprintidle() -> float | None:
    """xprintidle returns idle time in milliseconds (X11 only)."""
    try:
        proc = subprocess.run(
            ["xprintidle"], capture_output=True, text=True, timeout=2
        )
        if proc.returncode == 0:
            return int(proc.stdout.strip()) / 1000.0
    except Exception:
        pass
    return None


def _idle_seconds_dbus_screensaver() -> float | None:
    """D-Bus org.freedesktop.ScreenSaver.GetSessionIdleTime (seconds, KDE/GNOME)."""
    try:
        proc = subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=org.freedesktop.ScreenSaver",
                "/ScreenSaver",
                "org.freedesktop.ScreenSaver.GetSessionIdleTime",
            ],
            capture_output=True, text=True, timeout=2,
        )
        if proc.returncode == 0:
            # Output: method return time=... uint32 <idle_ms>
            for token in proc.stdout.split():
                try:
                    return int(token) / 1000.0
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _idle_seconds_gnome_mutter() -> float | None:
    """GNOME Shell / Mutter idle via D-Bus (Wayland + X11)."""
    try:
        proc = subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=org.gnome.Mutter.IdleMonitor",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ],
            capture_output=True, text=True, timeout=2,
        )
        if proc.returncode == 0:
            for token in proc.stdout.split():
                try:
                    return int(token) / 1000.0
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _idle_seconds_macos() -> float | None:
    """Seconds since the last HID event, from IOKit via ioreg.

    HIDIdleTime is published in nanoseconds by IOHIDSystem and is the same
    source Apple's own idle handling uses. It needs no permissions and no
    extra dependency, which matters because the worker must keep working on a
    machine where nobody has installed anything else.
    """
    try:
        proc = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4", "-r"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            if "HIDIdleTime" not in line:
                continue
            _, _, value = line.partition("=")
            value = value.strip().strip("<>").strip()
            try:
                nanoseconds = int(value)
            except ValueError:
                continue
            if nanoseconds < 0:
                return None
            return nanoseconds / 1_000_000_000.0
    except Exception:
        pass
    return None


def _idle_seconds_linux() -> float | None:
    """Try all available Linux idle detection methods in order."""
    # 1. xprintidle (X11, widely available)
    idle = _idle_seconds_xprintidle()
    if idle is not None:
        return idle
    # 2. GNOME Mutter (Wayland GNOME sessions)
    idle = _idle_seconds_gnome_mutter()
    if idle is not None:
        return idle
    # 3. Generic ScreenSaver D-Bus (KDE Plasma)
    idle = _idle_seconds_dbus_screensaver()
    if idle is not None:
        return idle
    return None


_last_idle_check = 0.0
_cached_idle: float | None = None


def get_user_idle_seconds() -> float | None:
    """Return seconds since last user input, or None if unavailable."""
    global _last_idle_check, _cached_idle
    now = time.monotonic()
    if now - _last_idle_check < 1.0:
        return _cached_idle
    _last_idle_check = now

    if _OS == "Windows":
        try:
            _cached_idle = _idle_seconds_windows()
        except Exception:
            _cached_idle = None
    elif _OS == "Darwin":
        _cached_idle = _idle_seconds_macos()
    else:
        _cached_idle = _idle_seconds_linux()

    return _cached_idle


# Activity state

STATES = ("idle", "light", "active", "gaming")


def get_activity_state() -> str:
    """Return one of: 'idle', 'light', 'active', 'gaming'.

    The GPU utilisation gate always takes precedence because it is the most
    reliable cross-platform indicator that the user is doing GPU-heavy work
    (gaming, rendering, ML training, etc.).
    """
    gpu = get_gpu_utilisation()

    # Gaming / heavy GPU work: do not run inference
    if gpu is not None and gpu >= config.GPU_GAMING_THRESHOLD:
        return "gaming"

    idle_secs = get_user_idle_seconds()

    # If we can measure idle time, use it together with GPU util
    if idle_secs is not None:
        if idle_secs >= config.IDLE_THRESHOLD_SECONDS:
            # Also require GPU to be calm
            if gpu is None or gpu < 30:
                return "idle"
            return "light"
        if idle_secs >= config.LIGHT_THRESHOLD_SECONDS:
            return "light"
        return "active"

    # No idle-time measurement available: fall back to GPU-only heuristic
    if gpu is None:
        return "idle"   # No GPU telemetry at all: assume idle
    if gpu < config.GPU_ACTIVE_THRESHOLD:
        return "idle"
    return "active"
