"""Spawn target: only standard-library imports before numerical limits are set."""
import os

NUMERIC_ENVIRONMENT = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def run(connection, config, endpoints, preview, backend_factory, backend_options):
    for name in NUMERIC_ENVIRONMENT:
        os.environ[name] = "1"
    import ctypes
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x8000):
            raise OSError("cannot set capture child above-normal priority")
    from .worker import run_worker
    run_worker(connection, config, endpoints, preview, backend_factory, backend_options)


def runtime_diagnostics():
    import ctypes
    import pathlib
    import sys
    import numpy as np
    counts = {}
    # Also enforce already loaded runtimes when the host's spawn entry imported
    # NumPy before invoking this target (source entry is fixed in composition).
    roots = (pathlib.Path(np.__file__).parent.parent / "numpy.libs",)
    for root in roots:
        for path in root.glob("*openblas*"):
            lib = ctypes.CDLL(str(path))
            for suffix in ("64_", "_", ""):
                setter = getattr(lib, "scipy_openblas_set_num_threads" + suffix, None)
                getter = getattr(lib, "scipy_openblas_get_num_threads" + suffix, None)
                if setter is not None and getter is not None:
                    setter.argtypes = (ctypes.c_int,)
                    setter(1)
                    getter.restype = ctypes.c_int
                    getter.argtypes = ()
                    counts[path.name] = getter()
                    break
    priority = None
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.GetPriorityClass.argtypes = (ctypes.c_void_p,)
        priority = kernel.GetPriorityClass(kernel.GetCurrentProcess())
    return {"pid": os.getpid(), "priority_class": priority,
            "numeric_environment": {name: os.environ.get(name) for name in NUMERIC_ENVIRONMENT},
            "numeric_runtime_threads": counts,
            "forbidden_modules": sorted(name for name in sys.modules
                                        if name.startswith(("PyQt", "app.view", "app.ui", "app.dao", "app.database")))}
