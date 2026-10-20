"""Process priority: step aside while the owner uses the PC.

``idle`` runs at normal priority; ``light``, ``active`` and ``gaming`` run the
worker and the Ollama it started (with its model runners) at below-normal
priority. Windows uses priority classes. Linux and macOS use nice values:
lowering needs no privilege but raising back does, so there the worker stays
at the lower priority until it restarts (it logs this once) unless the service
has ``CAP_SYS_NICE``.
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
        if level != self.level:
            log.debug("Priority %s -> %s (%s)", self.level, level, state)
            self.level = level
            if _OS == "Windows":
                self._windows(level)
            else:
                self._posix(level)
        if ollama_pid:
            # Every time: Ollama may have been started, or have started a model runner, since the last change.
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
        """Set *level* on Ollama and its children: it runs each loaded model in a child process, and on Linux a
        nice value belongs to one thread, so there every thread is changed (threads and runners started later
        inherit it)."""
        try:
            import psutil  # type: ignore
            root = psutil.Process(pid)
            processes = [root, *root.children(recursive=True)]
        except Exception:  # noqa: BLE001 - psutil is optional
            log.debug("Could not list the processes of Ollama (pid %s)", pid, exc_info=True)
            return
        for process in processes:
            try:
                if _OS == "Windows":
                    process.nice(psutil.NORMAL_PRIORITY_CLASS if level == "normal"
                                 else psutil.BELOW_NORMAL_PRIORITY_CLASS)
                elif _OS == "Linux":
                    for thread in process.threads():
                        _set_thread_nice(thread.id, NICE[level])
                else:
                    process.nice(NICE[level])
            except Exception:  # noqa: BLE001 - the process may have exited; raising back may be refused
                log.debug("Could not change the priority of Ollama (pid %s)", process.pid, exc_info=True)


def _set_thread_nice(tid: int, value: int) -> None:
    try:
        if os.getpriority(os.PRIO_PROCESS, tid) != value:
            os.setpriority(os.PRIO_PROCESS, tid, value)
    except ProcessLookupError:
        pass  # the thread ended meanwhile
