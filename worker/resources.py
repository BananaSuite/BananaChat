"""Process priority: step aside while the owner uses the PC.

``idle`` runs at normal priority; ``light``, ``active`` and ``gaming`` run the
worker and the Ollama process it started at below-normal priority. Windows
uses priority classes. Linux and macOS use nice values: lowering needs no
privilege but raising back does, so there the worker stays at the lower
priority until it restarts (it logs this once) unless the service has
``CAP_SYS_NICE``.
"""

from __future__ import annotations

import logging
import os
import platform
from typing import Optional

log = logging.getLogger("bananachat.worker.resources")
_OS = platform.system()

NICE = {"normal": 0, "below-normal": 10}
LEVEL_FOR_STATE = {"idle": "normal", "light": "below-normal", "active": "below-normal", "gaming": "below-normal"}


class Priority:
    def __init__(self):
        self.level = "normal"
        self._warned = False

    def apply(self, state: str, ollama_pid: Optional[int] = None) -> str:
        level = LEVEL_FOR_STATE.get(state, "normal")
        if level == self.level:
            return level
        log.debug("Priority %s -> %s (%s)", self.level, level, state)
        self.level = level
        if _OS == "Windows":
            self._windows(level)
        else:
            self._posix(level)
        if ollama_pid:
            self._other_process(ollama_pid, level)
        return level

    def _posix(self, level: str) -> None:
        target = NICE[level]
        try:
            current = os.nice(0)
            if target != current:
                os.nice(target - current)
        except OSError as error:
            if not self._warned:
                self._warned = True
                log.info("Cannot raise the worker back to normal priority (%s); it stays lower until it restarts.",
                         error)

    @staticmethod
    def _windows(level: str) -> None:
        try:
            import ctypes
            classes = {"normal": 0x00000020, "below-normal": 0x00004000}
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), classes[level])
        except Exception:  # noqa: BLE001
            log.debug("Could not set the Windows priority class", exc_info=True)

    @staticmethod
    def _other_process(pid: int, level: str) -> None:
        try:
            import psutil  # type: ignore
            process = psutil.Process(pid)
            if _OS == "Windows":
                process.nice(psutil.NORMAL_PRIORITY_CLASS if level == "normal" else psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                process.nice(NICE[level])
        except Exception:  # noqa: BLE001 - psutil is optional; raising back may be refused
            log.debug("Could not change the priority of Ollama (pid %s)", pid, exc_info=True)
