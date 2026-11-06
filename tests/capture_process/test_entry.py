"""Exercise the real source entry in an isolated spawned capture child."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_source_entry_spawn_loads_no_gui_and_preserves_child_runtime_limits():
    code = '''
import sys, os
import main
assert not any(name.startswith(("PyQt", "ui.", "app.main_window", "app.services_container", "app.service.db_")) for name in sys.modules)
sys.modules["__main__"].__file__ = main.__file__
from tests.capture_process.test_worker import make_adapter
from tests.capture_process.fake_backend import endpoint
adapter = make_adapter()
try:
    assert adapter.open_startup_streams((endpoint(),)).ready_streams
    info = adapter.client.diagnostics
    assert info["pid"] != os.getpid()
    assert info["forbidden_modules"] == (), info
    assert set(info["numeric_environment"].values()) == {"1"}, info
    assert info["numeric_runtime_threads"] and set(info["numeric_runtime_threads"].values()) == {1}, info
    if os.name == "nt":
        assert info["priority_class"] == 0x8000, info
finally:
    adapter.close_all(2.0)
assert not adapter.client.process.is_alive()
'''
    result = subprocess.run([sys.executable, "-W", "error", "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_frozen_dispatch_exits_before_gui_dependency_loader():
    code = '''
import multiprocessing, runpy, sys
class NoGui:
    def find_spec(self, fullname, *args):
        if fullname.startswith(("PyQt", "app.", "ui.", "base.")):
            raise AssertionError("GUI import before frozen dispatch: " + fullname)
sys.meta_path.insert(0, NoGui())
def dispatch():
    raise SystemExit(73)
multiprocessing.freeze_support = dispatch
runpy.run_path("main.py", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 73, result.stdout + result.stderr


def test_package_includes_capture_worker_and_native_library():
    spec = (ROOT / "main.spec").read_text(encoding="utf-8")
    assert "'app.service.capture_process.worker'" in spec
    assert '("app/native/capture_buffer.dll", "app/native")' in spec


def test_frozen_runtime_dispatch_precedes_pyinstaller_qt_hook():
    import ast
    spec = ast.parse((ROOT / "main.spec").read_text(encoding="utf-8"))
    analysis = next(node for node in ast.walk(spec) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "Analysis")
    hooks = ast.literal_eval(next(item.value for item in analysis.keywords if item.arg == "runtime_hooks"))
    assert hooks == ["scripts/capture_spawn_hook.py"]
    code = '''
import multiprocessing.spawn, runpy, sys, os
from pathlib import Path
import PyInstaller
class NoGui:
    def find_spec(self, fullname, *args):
        if fullname.startswith(("PyQt", "app.main_window", "app.services_container")):
            raise AssertionError("GUI import before frozen capture dispatch: " + fullname)
sys.meta_path.insert(0, NoGui())
def spawn_main(**kwargs):
    assert kwargs == {"parent_pid": 123, "pipe_handle": 456}, kwargs
    assert all(os.environ[key] == "1" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"))
    print("spawn dispatched before Qt hooks")
multiprocessing.spawn.spawn_main = spawn_main
sys.frozen = True
sys.argv = ["app.exe", "--multiprocessing-fork", "parent_pid=123", "pipe_handle=456"]
runpy.run_path("scripts/capture_spawn_hook.py")
# PyInstaller invokes its implied Qt hook only after custom runtime hooks.
# Reaching it in this subprocess would violate capture isolation.
runpy.run_path(str(Path(PyInstaller.__file__).parent / "hooks/rthooks/pyi_rth_pyqt6.py"))
raise AssertionError("frozen spawn did not terminate entry execution")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "spawn dispatched before Qt hooks" in result.stdout


def test_frozen_parent_runtime_hook_continues_normal_gui_startup():
    code = '''
import runpy, sys, multiprocessing
sys.frozen = True
sys.argv = ["app.exe"]
runpy.run_path("scripts/capture_spawn_hook.py")
print("parent continues")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "parent continues" in result.stdout
