"""The built-in terminal of remote access: a shell on a pseudo-terminal, run in a process of its own.

The worker starts ``python bananachat_worker.py shell-host`` for each terminal
session (only when its owner set ``BC_WORKER_REMOTE_SHELL=1``) and talks to it
over standard input and output in frames: one type byte, a 4-byte length and
the payload. Type 1 carries terminal bytes, type 2 a JSON control message
(``{"type": "resize", "cols": 80, "rows": 24}``) and type 3 ends the session.
Running it apart keeps the worker safe whatever the shell or the platform's
terminal API does.

Linux and macOS use a pseudo-terminal (``pty``). Windows 10 1809 and later use
a pseudo console (ConPTY) through ``ctypes``; older Windows get ``cmd.exe`` on
plain pipes, where full-screen programs do not work. The shell runs as the
account the worker runs as, in its home folder.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import sys
import threading

DATA, CONTROL, CLOSE = 1, 2, 3
_HEADER = struct.Struct("!BI")
MAX_FRAME = 1024 * 1024


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError()
        data.extend(chunk)
    return bytes(data)


def read_frame(stream):
    kind, length = _HEADER.unpack(_read_exact(stream, _HEADER.size))
    if length > MAX_FRAME:
        raise ValueError("frame too large")
    return kind, (_read_exact(stream, length) if length else b"")


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def frame(self, kind: int, payload: bytes = b"") -> None:
        with self.lock:
            self.stream.write(_HEADER.pack(kind, len(payload)) + payload)
            self.stream.flush()


def _size(message) -> tuple:
    try:
        cols = int(message.get("cols", 80))
        rows = int(message.get("rows", 24))
    except (TypeError, ValueError):
        return 80, 24
    return max(10, min(cols, 1000)), max(2, min(rows, 500))


def _shell_command():
    if platform.system() == "Windows":
        for name in ("pwsh.exe", "powershell.exe"):
            found = shutil.which(name)
            if found:
                return [found, "-NoLogo"]
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    shell = os.environ.get("SHELL") or ""
    if not shell or not os.path.exists(shell):
        shell = shutil.which("bash") or "/bin/sh"
    return [shell, "-l"]


# ----- Linux and macOS ------------------------------------------------------------------------------------------

def _posix(stdin, writer) -> int:
    import fcntl
    import pty
    import select
    import signal
    import termios

    command = _shell_command()
    environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    environment.pop("BC_WORKER_TOKEN", None)
    pid, fd = pty.fork()
    if pid == 0:  # the child: become the shell
        try:
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass
        os.execvpe(command[0], command, environment)  # noqa: S606 - the shell of the remote terminal itself
    stop = threading.Event()

    def from_worker():
        try:
            while not stop.is_set():
                kind, payload = read_frame(stdin)
                if kind == DATA:
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                elif kind == CONTROL:
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("type") == "resize":
                        cols, rows = _size(message)
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                elif kind == CLOSE:
                    break
        except (EOFError, OSError, ValueError):
            pass
        finally:
            stop.set()
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass

    threading.Thread(target=from_worker, daemon=True).start()
    try:
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            writer.frame(DATA, data)
    finally:
        stop.set()
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            _, status = os.waitpid(pid, 0)
        except OSError:
            status = 0
        try:
            os.close(fd)
        except OSError:
            pass
    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0


# ----- Windows ----------------------------------------------------------------------------------------------------

def _windows_conpty(stdin, writer) -> int:
    """A pseudo console (Windows 10 1809+). Raises OSError when it cannot be created."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not hasattr(kernel32, "CreatePseudoConsole"):
        raise OSError("ConPTY is not available")

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    kernel32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p,
                                    wintypes.DWORD]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
                                             ctypes.POINTER(ctypes.c_void_p)]
    kernel32.CreatePseudoConsole.restype = ctypes.c_long
    kernel32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, COORD]
    kernel32.ResizePseudoConsole.restype = ctypes.c_long
    kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    kernel32.ClosePseudoConsole.restype = None
    kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                           ctypes.POINTER(ctypes.c_size_t)]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                   ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    def check(ok, what):
        if not ok:
            raise OSError(f"{what} failed ({ctypes.get_last_error()})")

    console_in, our_in = wintypes.HANDLE(), wintypes.HANDLE()
    our_out, console_out = wintypes.HANDLE(), wintypes.HANDLE()
    check(kernel32.CreatePipe(ctypes.byref(console_in), ctypes.byref(our_in), None, 0), "CreatePipe")
    check(kernel32.CreatePipe(ctypes.byref(our_out), ctypes.byref(console_out), None, 0), "CreatePipe")
    console = ctypes.c_void_p()
    result = kernel32.CreatePseudoConsole(COORD(80, 24), console_in, console_out, 0, ctypes.byref(console))
    if result != 0:
        raise OSError(f"CreatePseudoConsole failed ({result})")
    kernel32.CloseHandle(console_in)
    kernel32.CloseHandle(console_out)
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    attributes = ctypes.cast(buffer, ctypes.c_void_p)
    check(kernel32.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)), "attribute list")
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    check(kernel32.UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, console,
                                             ctypes.sizeof(ctypes.c_void_p), None, None), "attribute")
    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    startup.lpAttributeList = attributes
    information = PROCESS_INFORMATION()
    command = ctypes.create_unicode_buffer(" ".join(f'"{part}"' for part in _shell_command()))
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    home = os.path.expanduser("~")
    check(kernel32.CreateProcessW(None, command, None, None, False, EXTENDED_STARTUPINFO_PRESENT, None, home,
                                  ctypes.cast(ctypes.byref(startup), ctypes.POINTER(STARTUPINFOW)),
                                  ctypes.byref(information)), "CreateProcess")
    kernel32.CloseHandle(information.hThread)
    output = os.fdopen(msvcrt.open_osfhandle(our_out.value, os.O_RDONLY | os.O_BINARY), "rb", buffering=0)
    input_ = os.fdopen(msvcrt.open_osfhandle(our_in.value, os.O_WRONLY | os.O_BINARY), "wb", buffering=0)
    stop = threading.Event()

    def from_worker():
        try:
            while not stop.is_set():
                kind, payload = read_frame(stdin)
                if kind == DATA:
                    input_.write(payload)
                elif kind == CONTROL:
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("type") == "resize":
                        cols, rows = _size(message)
                        kernel32.ResizePseudoConsole(console, COORD(cols, rows))
                elif kind == CLOSE:
                    break
        except (EOFError, OSError, ValueError):
            pass
        finally:
            stop.set()
            kernel32.TerminateProcess(information.hProcess, 0)

    def watch():
        kernel32.WaitForSingleObject(information.hProcess, 0xFFFFFFFF)
        stop.set()
        kernel32.ClosePseudoConsole(console)  # ends the output pipe, so the reader below returns

    threading.Thread(target=from_worker, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    try:
        while True:
            data = output.read(65536)
            if not data:
                break
            writer.frame(DATA, data)
    except OSError:
        pass
    finally:
        stop.set()
        kernel32.TerminateProcess(information.hProcess, 0)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(information.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(information.hProcess)
        kernel32.DeleteProcThreadAttributeList(attributes)
        for stream in (output, input_):
            try:
                stream.close()
            except OSError:
                pass
    return int(code.value)


def _windows_pipes(stdin, writer) -> int:
    """Old Windows without ConPTY: cmd.exe on plain pipes (line by line; no full-screen programs)."""
    import subprocess

    process = subprocess.Popen([os.environ.get("COMSPEC") or "cmd.exe", "/Q"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.path.expanduser("~"),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def from_worker():
        try:
            while True:
                kind, payload = read_frame(stdin)
                if kind == DATA:
                    process.stdin.write(payload.replace(b"\r", b"\r\n"))
                    process.stdin.flush()
                    writer.frame(DATA, payload.replace(b"\r", b"\r\n"))  # no terminal echoes for us
                elif kind == CLOSE:
                    break
        except (EOFError, OSError, ValueError):
            pass
        finally:
            process.kill()

    threading.Thread(target=from_worker, daemon=True).start()
    writer.frame(DATA, b"(Basic terminal: this Windows has no pseudo console, so full-screen programs do not work.)\r\n")
    while True:
        data = process.stdout.read1(65536) if hasattr(process.stdout, "read1") else process.stdout.read(1)
        if not data:
            break
        writer.frame(DATA, data.replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n"))
    return process.wait()


def main() -> int:
    stdin = sys.stdin.buffer
    writer = Writer(sys.stdout.buffer)
    try:
        if platform.system() == "Windows":
            try:
                code = _windows_conpty(stdin, writer)
            except OSError:
                code = _windows_pipes(stdin, writer)
        else:
            code = _posix(stdin, writer)
    finally:
        try:
            writer.frame(CLOSE)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
