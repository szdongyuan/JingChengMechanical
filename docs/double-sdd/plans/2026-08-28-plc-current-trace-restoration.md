# PLC Current Trace Restoration Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复普通 PLC 电流从 Snap7 轮询到 STFT 时频图右轴曲线的完整运行链路，同时保留当前 `PlcService` 的所有后续业务改动和内部错误可发现性。

**Architecture:** 只在当前 `PlcService.poll_once()` 中恢复一个独立的可选电流读取/发布边界；继续由现有 `ProductionCurrentCollectorFactory`、`PlcCurrentCollector` 和 `StftModule` 完成运行过滤、时间重基准与绘制。历史代码只提供行为参考，不回滚服务结构。生产实现仅捕获明确的 Snap7/REAL 外部读取错误和读取结果转换错误，不增加内部总线、PLC id 或对象形状兜底。

**Tech Stack:** Python 3.11、PyQt6、python-snap7 3.0.0、pytest、pyqtgraph

---

## File Structure

- Modify: `app/service/plc_service.py`
  - 恢复当前 PLC 轮询中的可选电流读取与 `plc_current_sampled` 发布。
  - 不修改启动、工单、测试中写回、结果写回、自动复位、连接或线程结构。
- Create: `tests/test_plc_service_current.py`
  - 锁定电流样本、错误边界以及现有 `poll_once()` 启动顺序。
- Create: `tests/test_stft_module/test_plc_current_live_path.py`
  - 使用同一事件总线覆盖启动沿前样本、运行订阅、下一轮样本和 STFT 展示。
- No UI production files should change.
  - `ui/modules/stft.py`、`ui/widgets/analysis_charts.py` 和
    `app/service/current_meter_service.py` 已与回归前版本一致。

## Task 1: Restore the PLC Current Publication Boundary with TDD

**Files:**
- Modify: `app/service/plc_service.py`
- Create: `tests/test_plc_service_current.py`
- Create: `tests/test_stft_module/test_plc_current_live_path.py`

- [ ] **Step 1: Record the task base and inspect the exact current interfaces**

Run:

```powershell
git rev-parse HEAD
git status --short
rg -n "def poll_once|def read_offset_value|plc_current_sampled|class PlcCurrentCollector|class ProductionCurrentCollectorFactory" app tests
```

Record this `HEAD` as `TaskBase`. Confirm the only unrelated main-worktree items are the
pre-existing untracked `.agents/` and `.codex/` directories; they must not be copied,
staged, or modified by this task.

- [ ] **Step 2: Add focused PLC service test support**

Create `tests/test_plc_service_current.py` with small behavior-oriented stubs. Do not
recreate the old service architecture.

Use a signal stub that records calls and a bus with only the production interface used by
`PlcService`:

```python
class _SignalStub:
    def __init__(self, *, error: BaseException | None = None):
        self.calls = []
        self.error = error

    def emit(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error


class _BusStub:
    def __init__(self):
        self.log = _SignalStub()
        self.plc_current_sampled = _SignalStub()
        self.plc_workorder_changed = _SignalStub()
```

Provide one `_build_service(...)` helper that constructs the current `PlcService` with:

```python
service = PlcService(
    bus,
    on_start_requested,
    "127.0.0.1",
    db_number=3,
    auto_reset_bit=auto_reset_bit,
    field_offsets_dict=field_offsets,
    plc_id=plc_id,
)
service.plc_client = client
```

Keep helpers narrow. Do not add a generic fake framework or production-style validation
inside tests.

- [ ] **Step 3: Write failing sample publication tests**

Add tests that call `poll_once()` with `_read_start_bit()` fixed to a valid value and
`read_offset_value()` returning controlled data. Cover these exact contracts, using
parameterization where it keeps the file smaller:

```python
@pytest.mark.parametrize("current_a", [0.0, 1.25, -0.5])
def test_poll_once_publishes_finite_plc_current_before_start_processing(...):
    ...
    assert bus.plc_current_sampled.calls == [(7, 123.5, current_a)]
```

Also assert:

- `time.monotonic()` is sampled before `read_offset_value(offset, "real")`;
- offset `0`, string `"0"`, and a normal byte offset are accepted;
- `None`, `""`, and whitespace offsets perform no current read, emission, or error log;
- `NaN`, `Inf`, and `-Inf` do not emit and do not modify
  `_last_current_read_error`;
- a finite successful emission clears `_last_current_read_error` only after
  `emit()` succeeds.

Do not assert extra type coercions or fallback behavior that the spec does not require.

- [ ] **Step 4: Write failing external-error and internal-propagation tests**

Import `struct` and the actual `snap7.error.S7Error`. Add compact tests for the exact
exception boundary:

```python
@pytest.mark.parametrize(
    "error",
    [S7Error("offline"), struct.error("short REAL")],
)
def test_expected_plc_current_read_errors_are_logged_once_and_do_not_block_start(...):
    ...
```

For the expected external errors, assert:

- the current signal is not emitted;
- the same message is logged once across repeated failures;
- the message contains the PLC id, `current_value_offset`, and reason;
- the existing rising-edge start callback still executes;
- a later finite success clears the dedup state, allowing a later failure to log again.

Add one conversion test where `read_offset_value()` returns non-numeric external data and
`float(raw_value)` raises `TypeError` or `ValueError`; it follows the same deduped log path.

Add one parameterized internal-contract test. The cases are malformed offset parsing,
invalid PLC id, missing `plc_current_sampled`, `emit()` raising, and an unexpected
non-`S7Error`/non-`struct.error` exception from `read_offset_value()`. Each case must:

```python
with pytest.raises(expected_exception):
    service.poll_once()
assert bus.log.calls == []
assert service._last_current_read_error == original_error_state
```

These assertions protect propagation only. Do not add production guards for these cases.

- [ ] **Step 5: Write failing protected `poll_once()` sequence tests**

Use method spies/call recorders around the current service to lock existing behavior without
testing old private architecture. Assert:

```text
first False poll:
  read_start -> poll_current -> establish baseline -> no start callback

False-to-True poll:
  read_start -> poll_current -> write_testing(True) -> sample trigger clock
  -> read workorder -> on_start_requested(plc_id, workorder, trigger_ns)
  -> optional auto-reset
```

Add variants for:

- no current offset;
- finite current;
- expected current read error;
- external current conversion error;
- disconnected service, which only calls the current reconnect path;
- failed required start-bit read, which does not call current polling or start processing.

For current success/failure variants, assert testing-state write, callback and auto-reset
still execute once. Do not broaden this into result-writing or connection implementation
tests unrelated to the touched control flow.

- [ ] **Step 6: Write the failing PLC-start-to-STFT integration test**

Create `tests/test_stft_module/test_plc_current_live_path.py` using a real `EventBus`, the
existing STFT test helpers, `ProductionCurrentCollectorFactory`, and a minimal fake Snap7
client.

Set the fake start states and current values so the sequence is deterministic:

```python
start_states = iter([False, True, True])
current_values = iter([0.5, 1.0, 2.0])
current_times = iter([1.4, 1.5, 1.7])
trigger_monotonic_ns = 1_500_000_000
```

Construct `PlcService` with `auto_reset_bit=False`, and replace only
`service._emit_plc_workorder` with a deterministic `lambda: {}` plus
`service._write_testing_signal` with a call recorder. Task 1's sequence tests already
protect those current production behaviors; this display-chain test must isolate the three
poll reads so `_emit_plc_workorder()` does not consume extra start/current iterator values.

The `on_start_requested` callback must:

1. assert the PLC id and trigger timestamp;
2. create a frozen config payload with `config_id=101`, `plc_id=7`, and
   `current_meter={"enabled": True, "source": "plc"}`;
3. build and start the real collector through
   `ProductionCurrentCollectorFactory(bus=bus, monotonic=time.monotonic)`;
4. emit `FactoryControllerState(displayed_config_id=101, run_phase="running",
   trigger_source="plc")` and
   `FactoryRuntimeStartPayload(config_id=101, run_id="run-101",
   trigger_source="plc", plc_id=7)`;
5. emit one valid STFT frame for channel `0` so the heatmap exists.

Drive three real `PlcService.poll_once()` calls:

```python
service.poll_once()  # False baseline; 0.5 A occurs before subscription
service.poll_once()  # rising edge; 1.0 A still occurs before subscription
service.poll_once()  # subscribed run consumes 2.0 A at t=0.2 s
qapp.processEvents()
```

Assert:

```python
payload = module.canvas.heatmap.current_trace_payload()
assert payload["times_sec"] == [0.0, 0.2]
assert payload["values_a"] == [2.0, 2.0]
assert payload["unit"] == "A"
```

The leading `0.0` is the existing display extension in `build_current_trace_payload`, not
an extra PLC sample. Assert `0.5` and `1.0` do not appear. Emit a sample for PLC id `8` and
assert the payload remains unchanged. Stop the collector and close the module in `finally`.

Do not modify STFT or chart production code to make this test pass.

- [ ] **Step 7: Run the new tests and confirm RED on the recorded task base**

Run:

```powershell
python -m pytest tests/test_plc_service_current.py tests/test_stft_module/test_plc_current_live_path.py -q
```

Expected: failures attributable to the missing `_poll_current_value()` and absent call from
`poll_once()`. Existing downstream collector/UI code must import successfully; unrelated
failures must be investigated before implementation.

- [ ] **Step 8: Implement only the missing current boundary**

Modify `app/service/plc_service.py`.

Add imports:

```python
import math
import struct
from snap7.error import S7Error
```

Initialize one dedup state in `PlcService.__init__()`:

```python
self._last_current_read_error = ""
```

Add one small reporting helper; it must not catch its own logger or signal errors:

```python
def _report_current_read_error(self, exc: BaseException) -> None:
    message = (
        f"PLC {self._plc_id} 读取电流失败 "
        f"(current_value_offset): {exc}"
    )
    if message == self._last_current_read_error:
        return
    self._last_current_read_error = message
    self.bus.log.emit(message)
    self.logger.error(message)
```

Add the current boundary using exact exception scopes:

```python
def _poll_current_value(self) -> None:
    if self.plc_client is None:
        return
    offset = self._field_offsets_dict.get("current_value_offset")
    if offset is None or not str(offset).strip():
        return

    sampled_monotonic = time.monotonic()
    try:
        raw_value = self.read_offset_value(offset, "real")
    except (S7Error, struct.error) as exc:
        self._report_current_read_error(exc)
        return

    try:
        current_a = float(raw_value)
    except (TypeError, ValueError) as exc:
        self._report_current_read_error(exc)
        return

    if not math.isfinite(current_a):
        return

    self.bus.plc_current_sampled.emit(
        int(self._plc_id),
        sampled_monotonic,
        current_a,
    )
    self._last_current_read_error = ""
```

In `poll_once()`, immediately after a non-`None` start-bit read and before the initial
baseline return/rising-edge calculation, add only:

```python
self._poll_current_value()
```

Do not add `getattr`, `hasattr`, `callable`, fallback signals, default PLC ids, retries,
extra configuration validation, or broad `except Exception`. Do not edit any UI file.

- [ ] **Step 9: Run focused tests and confirm GREEN**

Run:

```powershell
python -m pytest tests/test_plc_service_current.py tests/test_current_meter_service.py tests/multi_run_integration/test_production_composition.py::test_production_current_factory_publishes_live_trace_for_the_run tests/test_stft_module/test_live_current_trace.py tests/test_stft_module/test_plc_current_live_path.py -q
```

Expected: all selected tests pass.

- [ ] **Step 10: Prove the new integration test detects the regression**

Temporarily remove only the `self._poll_current_value()` call from `poll_once()` in the
isolated worktree, run:

```powershell
python -m pytest tests/test_stft_module/test_plc_current_live_path.py -q
```

Expected: FAIL because no PLC current reaches the STFT payload. Restore the call using the
task diff, rerun the same command, and expect PASS. Do not use `git checkout --` or overwrite
unrelated worktree changes.

- [ ] **Step 11: Run protected PLC behavior regression tests**

Run:

```powershell
python -m pytest tests/test_plc_service_current.py tests/test_factory_mode_controller tests/test_workorder_module.py -q
```

Expected: all collected tests pass. The new protected `poll_once()` sequence tests are the
mandatory direct coverage for behavior that currently has no surviving PLC-service test
file on this branch.

- [ ] **Step 12: Inspect and checkpoint Task 1**

Run:

```powershell
git diff --check
git diff -- app/service/plc_service.py tests/test_plc_service_current.py tests/test_stft_module/test_plc_current_live_path.py
git status --short
```

Confirm the production diff is limited to imports, one dedup field, one reporting helper,
one current polling helper, and one `poll_once()` call. Confirm no UI, database, config,
`.agents/`, or `.codex/` file changed.

Create a temporary checkpoint commit for review:

```powershell
git add app/service/plc_service.py tests/test_plc_service_current.py tests/test_stft_module/test_plc_current_live_path.py
git commit -m "tmp/sdd: restore PLC current trace publication"
```

## Task 2: Verify the Complete Change

**Files:**
- No source changes expected
- If a failure reveals a defect, modify only the three Task 1 files and add or tighten a
  regression assertion before the fix

- [ ] **Step 1: Run the complete focused regression set**

Run:

```powershell
python -m pytest tests/test_plc_service_current.py tests/test_current_meter_service.py tests/multi_run_integration/test_production_composition.py tests/test_stft_module/test_live_current_trace.py tests/test_stft_module/test_plc_current_live_path.py tests/test_factory_mode_controller tests/test_workorder_module.py -q
```

Expected: all collected tests pass with zero failures.

- [ ] **Step 2: Run the full repository suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass. Existing warnings may remain, but there must be no new failure,
unhandled Qt exception, lingering signal subscription, or thread leak.

- [ ] **Step 3: Verify scope and reliability constraints**

Run:

```powershell
$taskBase = "<the exact TaskBase SHA recorded in Task 1 Step 1>"
git diff --check "$taskBase..HEAD"
git diff --stat "$taskBase..HEAD"
git status --short
rg -n "getattr|hasattr|callable|except Exception" app/service/plc_service.py
```

Before these commands, any fix made during Task 2 must have a reviewed temporary checkpoint
commit so `TaskBase..HEAD` includes it. Never use `HEAD~1..HEAD` for final scope checks; it
would omit earlier task commits and any still-uncommitted verification fix.

The final review must confirm:

- no new `getattr`, `hasattr`, `callable`, broad `except Exception`, fallback, retry, or
  default-value logic was added for this current path;
- internal contract errors are tested to propagate;
- only the specified PLC external errors are deduplicated;
- the STFT curve style and UI production code are untouched;
- no database/config migration exists;
- `.agents/` and `.codex/` remain untouched and unstaged.

- [ ] **Step 4: Record final verification evidence**

Record the exact focused and full-suite pass counts, the mutation red/green result,
`git diff --check` result, and final changed-file list for the completion handoff. If any
verification fix is needed, rerun the Task 1 paired reviews before repeating this task.
