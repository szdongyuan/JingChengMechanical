"""Small best-effort thread-priority helpers for the Windows runtime."""

from __future__ import annotations

import os


def set_current_thread_priority(priority: int) -> None:
    """Set the current Windows thread priority without affecting portability."""

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_thread = kernel32.GetCurrentThread
        get_current_thread.argtypes = ()
        get_current_thread.restype = ctypes.c_void_p
        set_thread_priority = kernel32.SetThreadPriority
        set_thread_priority.argtypes = (ctypes.c_void_p, ctypes.c_int)
        set_thread_priority.restype = ctypes.c_int
        set_thread_priority(get_current_thread(), int(priority))
    except Exception:
        pass


def set_current_process_priority(priority_class: int) -> None:
    """Set the current Windows process priority class on a best-effort basis."""

    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = ()
        get_current_process.restype = ctypes.c_void_p
        set_priority_class = kernel32.SetPriorityClass
        set_priority_class.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        set_priority_class.restype = ctypes.c_int
        set_priority_class(
            get_current_process(),
            int(priority_class),
        )
    except Exception:
        pass


THREAD_PRIORITY_BELOW_NORMAL = -1
THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
