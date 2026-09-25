"""Process priority and Ollama resource throttling.

When the user is actively using the PC, we lower the priority of both the
worker Python process and the Ollama subprocess so inference does not compete
with foreground applications.

When the user is fully idle we restore normal priority so throughput is
maximised.

Priority classes
----------------
  idle    → Normal priority  (same as any foreground app)
  light   → Below-normal priority
  active  → Below-normal priority
  gaming  → Inference is blocked by the daemon; not reached here

The Ollama child process (if managed by the worker) gets the same treatment.

Windows uses priority classes; Linux and macOS both use POSIX nice values. On
those two, lowering a priority needs no privileges but raising it back does,
so a worker that has stepped aside for local activity stays there until it
restarts.
"""

import logging
import os
import platform

_logger = logging.getLogger("bananachat.worker.resources")
_OS = platform.system()

# Current priority level; tracked to avoid redundant syscalls.
_current_priority: str = "normal"
_warned_no_raise: bool = False


# Process priority helpers

def _set_self_priority_posix(level: str):
    """Set the calling process's nice value on Linux or macOS."""
    nice_values = {
        "normal":       0,
        "below-normal": 10,
        "idle":         19,
    }
    target = nice_values.get(level, 0)
    try:
        current = os.nice(0)
        delta   = target - current
        if delta != 0:
            os.nice(delta)
    except OSError as exc:
        # Lowering the nice value needs CAP_SYS_NICE, which the worker service
        # deliberately does not have. Once the worker has stepped down for
        # local activity it stays there, so inference keeps running politely
        # even after the machine goes idle again. Say so once: the alternative
        # symptom is "inference got slow and never sped up" with no cause.
        global _warned_no_raise
        if not _warned_no_raise:
            _warned_no_raise = True
            _logger.info(
                "Cannot raise this process back to %s priority (nice %s, currently %s): %s. "
                "Inference stays at the lower priority until the worker restarts.",
                level, target, current, exc,
            )


def _set_self_priority_windows(level: str):
    """Set the calling process's Windows priority class."""
    import ctypes
    # Priority class constants
    NORMAL_PRIORITY_CLASS       = 0x00000020
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    IDLE_PRIORITY_CLASS         = 0x00000040
    classes = {
        "normal":       NORMAL_PRIORITY_CLASS,
        "below-normal": BELOW_NORMAL_PRIORITY_CLASS,
        "idle":         IDLE_PRIORITY_CLASS,
    }
    target = classes.get(level, NORMAL_PRIORITY_CLASS)
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore
        ctypes.windll.kernel32.SetPriorityClass(handle, target)  # type: ignore
    except Exception as exc:
        _logger.debug("Could not set Windows priority class: %s", exc)


def _set_ollama_priority_psutil(pid: int, level: str):
    """Apply the same priority to the Ollama process via psutil."""
    try:
        import psutil  # type: ignore
        proc = psutil.Process(pid)
        if _OS == "Windows":
            import psutil
            classes = {
                "normal":       psutil.NORMAL_PRIORITY_CLASS,
                "below-normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                "idle":         psutil.IDLE_PRIORITY_CLASS,
            }
            proc.nice(classes.get(level, psutil.NORMAL_PRIORITY_CLASS))
        else:
            nice_values = {"normal": 0, "below-normal": 10, "idle": 19}
            proc.nice(nice_values.get(level, 0))
    except Exception as exc:
        _logger.debug("Could not reprioritise Ollama (pid %d): %s", pid, exc)


# Public API

def apply_for_state(state: str, ollama_pid: int | None = None):
    """Adjust process priority to match the current activity state.

    state: one of 'idle', 'light', 'active', 'gaming'
    ollama_pid: PID of the Ollama process managed by ollama_mgr, if available.
    """
    global _current_priority

    level_map = {
        "idle":   "normal",
        "light":  "below-normal",
        "active": "below-normal",
        "gaming": "below-normal",   # Gaming: inference won't start anyway, but be polite
    }
    level = level_map.get(state, "normal")
    if level == _current_priority:
        return

    _logger.debug("Adjusting process priority: %s → %s (state=%s)", _current_priority, level, state)
    _current_priority = level

    if _OS == "Windows":
        _set_self_priority_windows(level)
    else:
        _set_self_priority_posix(level)

    if ollama_pid:
        _set_ollama_priority_psutil(ollama_pid, level)
