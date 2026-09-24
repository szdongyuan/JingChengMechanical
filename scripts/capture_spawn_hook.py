"""Dispatch Windows frozen workers before PyInstaller's implied Qt hooks.

Custom runtime hooks execute first. Use the standard multiprocessing protocol;
normal application startup continues through the remaining runtime hooks.
"""
import os
import sys

for _capture_thread_variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"
):
    os.environ[_capture_thread_variable] = "1"

if sys.platform == "win32" and getattr(sys, "frozen", False):
    import multiprocessing

    multiprocessing.freeze_support()
