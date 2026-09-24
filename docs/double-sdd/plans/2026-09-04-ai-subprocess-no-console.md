# AI 推理子进程无控制台 Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows 主程序启动单次或常驻 AI 推理 exe 时不创建控制台，同时完整保留现有进程通信和生命周期行为。

**Architecture:** 新增一个无状态平台适配模块，集中提供 AI 子进程的 `creationflags`。两条既有启动路径只消费该函数，不改变命令拆分、管道、超时、工作目录、结果捕获或常驻协议。

**Tech Stack:** Python 3, `subprocess`, Windows `CREATE_NO_WINDOW`, pytest

---

## File Structure

- Create `app/service/ai_subprocess.py`: 唯一负责返回平台相关的 AI 子进程创建标志。
- Create `tests/test_ai_subprocess.py`: 覆盖平台分支，并在 Windows 上真实验证无控制台且 stdout/stderr 管道可用。
- Modify `app/service/ai_service.py`: 将无控制台标志传给一次性 AI 推理的 `subprocess.run`。
- Modify `app/service/ai_resident_runtime.py`: 将无控制台标志传给常驻 AI 推理的 `subprocess.Popen`。
- Modify `tests/test_ai_service.py`: 验证一次性启动参数及返回结果兼容性。
- Modify `tests/test_ai_resident_runtime.py`: 验证常驻启动参数及管道配置兼容性。

### Task 1: 平台创建标志与真实 Windows 语义

**Files:**
- Create: `app/service/ai_subprocess.py`
- Create: `tests/test_ai_subprocess.py`

- [ ] **Step 1: 编写平台分支失败测试**

创建 `tests/test_ai_subprocess.py`，先导入尚不存在的模块，并覆盖 Windows 与非 Windows 分支：

```python
from types import SimpleNamespace

import pytest

import app.service.ai_subprocess as ai_subprocess


def test_creation_flags_use_create_no_window_on_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ai_subprocess,
        "subprocess",
        SimpleNamespace(CREATE_NO_WINDOW=0x08000000),
    )

    assert ai_subprocess.ai_subprocess_creation_flags() == 0x08000000


def test_creation_flags_are_zero_outside_windows(monkeypatch):
    monkeypatch.setattr(ai_subprocess, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ai_subprocess, "subprocess", SimpleNamespace())

    assert ai_subprocess.ai_subprocess_creation_flags() == 0
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_ai_subprocess.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'app.service.ai_subprocess'`.

- [ ] **Step 3: 实现最小平台适配模块**

创建 `app/service/ai_subprocess.py`：

```python
from __future__ import annotations

import os
import subprocess


def ai_subprocess_creation_flags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0
```

- [ ] **Step 4: 增加 Windows 真实行为集成测试**

在 `tests/test_ai_subprocess.py` 增加：

```python
import os
import subprocess
import sys


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics only")
def test_create_no_window_child_has_no_console_and_keeps_output_pipes():
    probe = (
        "import ctypes, sys; "
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
    assert result.stdout.strip() == "0"
    assert result.stderr.strip() == "stderr-ok"
```

- [ ] **Step 5: 运行测试并确认通过**

Run: `python -m pytest tests/test_ai_subprocess.py -v`

Expected on Windows: 3 passed. Expected elsewhere: 2 passed, 1 skipped.

- [ ] **Step 6: 创建临时任务提交**

```bash
git add app/service/ai_subprocess.py tests/test_ai_subprocess.py
git commit -m "tmp/sdd: add AI subprocess creation flags"
```

### Task 2: 接入单次与常驻 AI 推理启动路径

**Files:**
- Modify: `app/service/ai_service.py:16,688-694`
- Modify: `app/service/ai_resident_runtime.py:6,1174-1184`
- Modify: `tests/test_ai_service.py:1247-1269`
- Modify: `tests/test_ai_resident_runtime.py`

- [ ] **Step 1: 扩展一次性启动失败测试**

修改 `test_execute_toolchain_runs_with_package_dir_cwd`，将测试更名为
`test_execute_toolchain_preserves_io_and_uses_no_console_flags`。让 `_run` 捕获完整
`kwargs`，并让结果包含 stdout/stderr：

```python
expected_flags = 0x08000000
monkeypatch.setattr(
    ai_service_module,
    "ai_subprocess_creation_flags",
    lambda: expected_flags,
)

def _run(cmd, **kwargs):
    captured["cmd"] = cmd
    captured["kwargs"] = kwargs
    return subprocess.CompletedProcess(cmd, 7, stdout="out", stderr="err")
```

断言 `creationflags`、`capture_output=True`、`text=True`、`timeout=30.0`、`cwd`
均保持正确，并断言执行 envelope 仍返回退出码 `7`、`out` 和 `err`。

- [ ] **Step 2: 增加常驻启动失败测试**

在 `tests/test_ai_resident_runtime.py` 新增测试，通过 monkeypatch 替换
`ai_resident_runtime.subprocess.Popen`、`ai_resident_runtime.threading.Thread` 和
`ai_resident_runtime.ai_subprocess_creation_flags`。调用
`runtime._start_process("tc-1", "tool.exe --serve", package_dir="C:/AI")`，断言：

```python
assert captured["kwargs"] == {
    "stdin": subprocess.PIPE,
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
    "bufsize": 1,
    "cwd": "C:/AI",
    "creationflags": 0x08000000,
}
```

假进程必须提供非空 `stdin`、`stdout`、`stderr`；假线程的 `start()` 不执行读取
目标，以便测试只隔离构造参数。

- [ ] **Step 3: 运行定向测试并确认失败**

Run: `python -m pytest tests/test_ai_service.py::TestRunAiAnalysisRouting::test_execute_toolchain_preserves_io_and_uses_no_console_flags tests/test_ai_resident_runtime.py::test_start_process_preserves_pipes_and_uses_no_console_flags -v`

Expected: FAIL because launchers do not expose/import `ai_subprocess_creation_flags` and do not pass `creationflags`.

- [ ] **Step 4: 接入一次性启动路径**

在 `app/service/ai_service.py` 导入：

```python
from app.service.ai_subprocess import ai_subprocess_creation_flags
```

在现有 `subprocess.run(...)` 调用中仅增加：

```python
creationflags=ai_subprocess_creation_flags(),
```

- [ ] **Step 5: 接入常驻启动路径**

在 `app/service/ai_resident_runtime.py` 导入同一函数，并在现有
`subprocess.Popen(...)` 调用中仅增加：

```python
creationflags=ai_subprocess_creation_flags(),
```

- [ ] **Step 6: 运行定向测试并确认通过**

Run: `python -m pytest tests/test_ai_service.py::TestRunAiAnalysisRouting::test_execute_toolchain_preserves_io_and_uses_no_console_flags tests/test_ai_resident_runtime.py::test_start_process_preserves_pipes_and_uses_no_console_flags -v`

Expected: 2 passed.

- [ ] **Step 7: 运行 AI 相关回归测试**

Run: `python -m pytest tests/test_ai_subprocess.py tests/test_ai_service.py tests/test_ai_resident_runtime.py -v`

Expected: all collected tests pass; the Windows integration test runs rather than skips on Windows.

- [ ] **Step 8: 创建临时任务提交**

```bash
git add app/service/ai_service.py app/service/ai_resident_runtime.py tests/test_ai_service.py tests/test_ai_resident_runtime.py
git commit -m "tmp/sdd: launch AI inference without a console"
```

