"""Process liveness and tree kill, Windows first (SPEC 15 Phase 2: crash resume)."""

from __future__ import annotations

import os
import subprocess
import sys



def has_console() -> bool:
    """False under pythonw (the hub started at logon); True in a terminal and in runs (spawned with a hidden console)."""
    if sys.platform != "win32":
        return True
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]


def no_window() -> int:
    """creationflags for a console child (git, gh, taskkill, a model probe).

    Phase 7: without a console, every such child opens a window of its own, so it gets CREATE_NO_WINDOW. With a
    console the flag is left out: it would give each child a new hidden console, which nearly doubles a git call.
    """
    return 0 if has_console() else subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # on POSIX signal 0 only checks existence
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    """Kill a process and its children. Best effort; the caller re-checks with pid_alive."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=no_window())
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
