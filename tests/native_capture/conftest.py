"""Keep synchronous native callback tests from elevating the pytest thread."""
import ctypes as ct
import os

import pytest


@pytest.fixture(autouse=True)
def restore_calling_thread_priority():
    if os.name != 'nt':
        yield
        return
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentThread.argtypes, kernel.GetCurrentThread.restype = [], ct.c_void_p
    kernel.GetThreadPriority.argtypes, kernel.GetThreadPriority.restype = [ct.c_void_p], ct.c_int
    kernel.SetThreadPriority.argtypes, kernel.SetThreadPriority.restype = [ct.c_void_p, ct.c_int], ct.c_int
    thread = kernel.GetCurrentThread()
    previous = kernel.GetThreadPriority(thread)
    assert previous != 0x7fffffff
    try:
        yield
    finally:
        # invoke() calls the actual producer on this test thread. Production
        # correctly elevates its owner, so restore only the test environment.
        assert kernel.SetThreadPriority(thread, previous)
        assert kernel.GetThreadPriority(thread) == previous
