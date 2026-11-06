# AI 推理子进程无控制台设计

## 目标

Windows GUI 主程序启动 AI 推理可执行文件时，不创建 Terminal、命令提示符或其他控制台窗口，同时保持 AI 推理进程及现有通信协议正常运行。

## 当前行为与根因

主程序以 PyInstaller `console=False` 方式打包。AI 推理有两条外部进程启动路径：

- `oneshot_process` 通过 `app/service/ai_service.py` 中的 `subprocess.run` 启动。
- `resident_process` 通过 `app/service/ai_resident_runtime.py` 中的 `subprocess.Popen` 启动。

两条路径均未设置 Windows 进程创建标志。GUI 主进程启动控制台子系统的 AI exe 时，Windows 因此可能为子进程创建控制台及其宿主窗口。

## 方案比较

### 采用方案：`CREATE_NO_WINDOW`

在两条启动路径中设置 `creationflags`。Windows 使用 `subprocess.CREATE_NO_WINDOW`，其他平台使用 `0`。该标志要求 Windows 从创建进程开始就不分配控制台，而不是创建后再隐藏。

优点：

- 不创建控制台窗口，符合目标语义。
- AI exe 仍作为正常子进程运行。
- 现有 stdin/stdout/stderr 管道、超时、退出码和日志捕获保持不变。
- 不要求修改第三方或用户提供的 AI 脚本。

### 不采用：`STARTUPINFO` + `SW_HIDE`

该方案主要隐藏已创建窗口，不能清晰保证不创建控制台，因此不符合“不仅不弹窗，而且不要启动终端”的要求。

### 不采用：将 AI exe 打包为 `--noconsole` / `--windowed`

该方案要求每个 AI 工具链改变打包方式，并可能影响依赖 stdin/stdout/stderr 的常驻 JSONL 协议，兼容风险更高。

## 设计

新增 `app/service/ai_subprocess.py`，通过公开函数
`ai_subprocess_creation_flags()` 集中返回外部 AI 进程的创建标志：

```python
def ai_subprocess_creation_flags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0
```

该模块只依赖 `os` 和 `subprocess`，由 `ai_service.py` 和
`ai_resident_runtime.py` 共同调用。函数不得导入 UI 或服务对象，也不得改变命令行拆分逻辑。

两处调用只新增：

```python
creationflags=ai_subprocess_creation_flags()
```

其他 `subprocess.run` / `subprocess.Popen` 参数保持原样。

## 行为边界

- 本任务只控制主程序直接启动的 AI 推理进程。
- 如果 AI exe 主动调用 Windows `AllocConsole()`，主程序无法阻止其后续显式创建控制台。
- 如果 AI exe 自己启动带 `CREATE_NEW_CONSOLE` 的后代进程，该行为也不在本任务控制范围内。
- 不隐藏或抑制 AI 推理进程本身；仅阻止为其创建控制台。

## 错误处理

平台适配函数在非 Windows 平台返回 `0`，保持当前行为。在 Windows 上直接使用 Python `subprocess` 提供的 `CREATE_NO_WINDOW`；若运行环境异常缺少该常量，应显式失败，不得静默退化为会创建控制台的启动方式。

## 测试

增加单元测试验证：

1. 在 `tests/test_ai_subprocess.py` 验证 Windows 条件下平台适配函数返回
   `subprocess.CREATE_NO_WINDOW`，非 Windows 条件下返回 `0`；Windows
   专用集成测试启动一个报告 `GetConsoleWindow()` 状态并写入 stdout/stderr
   的控制台子进程，验证子进程没有控制台且两条输出管道仍可读取。该测试等价验证
   `console=False` GUI 父进程所需的子进程创建语义，无需在单元测试中重新执行完整
   PyInstaller 打包。
2. 在 `tests/test_ai_service.py` 验证 `oneshot_process` 的 `subprocess.run`
   收到该 `creationflags`，并继续传递 `capture_output=True`、`text=True`、
   `timeout` 和 `cwd`；返回值测试继续验证退出码及 stdout/stderr 捕获。
3. 在 `tests/test_ai_resident_runtime.py` 验证 `resident_process` 的
   `subprocess.Popen` 收到该 `creationflags`，且 stdin/stdout/stderr 管道、
   文本模式、UTF-8 编码、错误替换、行缓冲及工作目录等既有参数不变。
4. 运行现有常驻运行时真实子进程测试，确认 stdin/stdout JSONL、stderr、进程复用与
   生命周期行为不回归。

运行 AI 服务及常驻运行时相关测试，确认进程协议与生命周期行为没有回归。

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: [AI toolchain CLI arguments, stdin/stdout/stderr protocol, execution modes, timeout behavior, exit-code handling, package working directory, non-Windows behavior]
- Allowed breakage: [Windows AI console window creation]
- Migration strategy: none
