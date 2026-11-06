"""A balanced COM apartment lifetime for a Windows owner thread."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def com_apartment() -> Iterator[None]:
    """Keep this thread COM-ready, releasing only our initialization reference."""
    if sys.platform != "win32":
        yield
        return

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_int32
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    hresult = ole32.CoInitializeEx(None, 0) & 0xFFFFFFFF
    owned = hresult in (0, 1)  # S_OK and S_FALSE both acquire a reference.
    if not owned and hresult != 0x80010106:  # RPC_E_CHANGED_MODE: existing apartment.
        raise OSError(f"CoInitializeEx failed with HRESULT 0x{hresult:08X}")
    try:
        yield
    finally:
        if owned:
            ole32.CoUninitialize()
