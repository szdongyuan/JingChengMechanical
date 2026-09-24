import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import app.service.ai_subprocess as ai_subprocess


@pytest.mark.parametrize("explicit_environment", [False, True])
def test_child_environment_copies_values_and_overrides_numerical_threads(
    monkeypatch, explicit_environment
):
    original = {
        "PATH": "test-bin", "SYSTEMROOT": "test-windows", "CUSTOM_AI": "keep",
        "OMP_NUM_THREADS": "8", "OPENBLAS_NUM_THREADS": "16",
        "MKL_NUM_THREADS": "4", "NUMEXPR_NUM_THREADS": "6",
    }
    before = dict(original)
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(environ=original))

    result = ai_subprocess.ai_subprocess_environment(
        original if explicit_environment else None
    )

    assert result == {
        "PATH": "test-bin", "SYSTEMROOT": "test-windows", "CUSTOM_AI": "keep",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
    }
    assert original == before
    assert result is not original


def test_creation_flags_use_idle_and_create_no_window_on_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ai_subprocess,
        "subprocess",
        SimpleNamespace(CREATE_NO_WINDOW=0x08000000, IDLE_PRIORITY_CLASS=0x40),
    )

    assert ai_subprocess.ai_subprocess_creation_flags() == 0x08000040


def test_creation_flags_are_zero_outside_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ai_subprocess, "subprocess", SimpleNamespace())

    assert ai_subprocess.ai_subprocess_creation_flags() == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics only")
def test_create_no_window_child_has_no_console_and_keeps_output_pipes():
    probe = (
        "import ctypes, sys; "
        "k = ctypes.windll.kernel32; "
        "k.GetCurrentProcess.restype = ctypes.c_void_p; "
        "k.GetPriorityClass.argtypes = [ctypes.c_void_p]; "
        "print(k.GetPriorityClass(k.GetCurrentProcess())); "
        "print(int(bool(ctypes.windll.kernel32.GetConsoleWindow()))); "
        "print('stderr-ok', file=sys.stderr)"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        creationflags=ai_subprocess.ai_subprocess_creation_flags(),
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["64", "0"]
    assert result.stderr.strip() == "stderr-ok"


_RESOURCE_CHILD = r'''
import ctypes
import json
import os
import sys

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetCurrentProcess.restype = ctypes.c_void_p
k.GetPriorityClass.argtypes = (ctypes.c_void_p,)
k.GetConsoleWindow.restype = ctypes.c_void_p
details = {
    "priority": k.GetPriorityClass(k.GetCurrentProcess()),
    "console": bool(k.GetConsoleWindow()),
    "threads": [os.environ.get(key) for key in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    )],
    "custom": os.environ.get("CAPTURE_TEST_CUSTOM"),
    "path_preserved": os.environ.get("PATH") == os.environ.get("CAPTURE_TEST_PATH"),
    "system_preserved": os.environ.get("SYSTEMROOT") == os.environ.get("CAPTURE_TEST_SYSTEM"),
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
}
if "--resident" not in sys.argv:
    print(json.dumps(details))
    print("stderr-ok", file=sys.stderr)
else:
    for line in sys.stdin:
        message = json.loads(line)
        if message["message_type"] == "shutdown":
            break
        if message["message_type"] == "analyze":
            print("stderr-ok", file=sys.stderr, flush=True)
            print(json.dumps({
                "message_type": "result", "request_id": message["request_id"],
                "response": {
                    "protocol_version": 1, "request_id": message["request_id"],
                    "toolchain_name": "probe", "toolchain_version": "1",
                    "ai_result": details,
                },
            }), flush=True)
'''


_RESOURCE_PARENT = r'''
import ctypes
import json
import os
from pathlib import Path
import sys
from app.service.ai_service import AiService
from app.service.ai_resident_runtime import ResidentToolchainRuntime

mode, package_dir = sys.argv[1:]
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
os.environ["CAPTURE_TEST_PATH"] = os.environ["PATH"]
os.environ["CAPTURE_TEST_SYSTEM"] = os.environ["SYSTEMROOT"]
os.environ["CAPTURE_TEST_CUSTOM"] = "custom-value"
for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = "8"
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetCurrentProcess.restype = ctypes.c_void_p
k.GetPriorityClass.argtypes = (ctypes.c_void_p,)
parent_priority = k.GetPriorityClass(k.GetCurrentProcess())
if mode == "oneshot":
    service = AiService()
    try:
        result = service.execute_toolchain(
            entrypoint="python.exe probe.py", package_dir=package_dir,
            request_json_path="request.json", response_json_path="response.json",
            artifacts_dir="artifacts", timeout_ms=10000,
        )
        assert result["exit_code"] == 0, result
        details = json.loads(result["stdout_excerpt"])
        stderr = result["stderr_excerpt"]
    finally:
        service.shutdown()
else:
    runtime = ResidentToolchainRuntime()
    try:
        result = runtime.analyze(
            toolchain_id="probe", entrypoint="python.exe probe.py --resident",
            package_dir=package_dir, request_id="resource-probe",
            request_payload={"protocol_version": 1, "request_id": "resource-probe"},
            artifacts_dir="artifacts", timeout_ms=10000,
        )
        assert result["module_status"] == "success", result
        details = result["response_payload"]["ai_result"]
        stderr = result["stderr_excerpt"]
    finally:
        runtime.shutdown_all()
print(json.dumps({"parent_priority": parent_priority, "child": details, "stderr": stderr}))
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows child priority readback")
@pytest.mark.parametrize("parent_priority", [0x20, 0x40], ids=["normal-parent", "idle-parent"])
@pytest.mark.parametrize("mode", ["oneshot", "resident"])
def test_real_ai_launch_limits_preserve_idle_parent_and_process_io(
    tmp_path, mode, parent_priority
):
    (tmp_path / "probe.py").write_text(_RESOURCE_CHILD, encoding="utf-8")
    parent = subprocess.run(
        [sys.executable, "-c", _RESOURCE_PARENT, mode, str(tmp_path)],
        creationflags=subprocess.CREATE_NO_WINDOW | parent_priority,
        capture_output=True, text=True, timeout=30,
    )
    assert parent.returncode == 0, parent.stderr
    result = json.loads(parent.stdout)
    assert result["parent_priority"] == parent_priority
    expected_args = ["--resident"] if mode == "resident" else [
        "--request", "request.json", "--response", "response.json",
        "--artifacts-dir", "artifacts",
    ]
    assert result["child"] == {
        "priority": 0x40, "console": False, "threads": ["1"] * 4,
        "custom": "custom-value", "path_preserved": True,
        "system_preserved": True, "args": expected_args, "cwd": str(tmp_path),
    }
    assert result["stderr"].strip() == "stderr-ok"
