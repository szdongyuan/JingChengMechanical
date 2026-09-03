# Restart Button PLC Shutdown Fix Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the “重启软件” flow by making the real `PlcService` satisfy the staged application-shutdown contract without weakening strict shutdown safety.

**Architecture:** Add two thread-affine, idempotent Qt slots to `PlcService`: one stops ingress timers while preserving the PLC client, and the second disconnects after PLC result shutdown. Keep the existing `stop_plc_service` slot as the backward-compatible composition of both stages, and cover the real service rather than only shutdown fakes.

**Tech Stack:** Python 3.12, PyQt6 `QObject`/`pyqtSlot`, pytest 8

---

## File Structure

- Create `tests/test_plc_service_shutdown.py`: focused regression tests for the real `PlcService` Qt slot contract, staged resource behavior, and legacy combined stop entry point.
- Modify `tests/multi_run_integration/test_shutdown_order.py`: exercise the production `PlcServiceThreadShutdownOwner` and `_invoke_plc_thread_slot` boundary with a real `PlcService` on a real `QThread`, and require a clean aggregate shutdown report.
- Modify `app/service/plc_service.py`: implement staged shutdown slots and delegate the legacy `stop_plc_service` slot to them.
- Modify `app/services_container.py`: after the blocking queued disconnect call returns, consume the real service's immutable disconnect acknowledgement and raise on failure so the application shutdown owner records a non-clean stage.

No other production modules should change. In particular, preserve the existing ordering in `app/service/multi_run_integration/shutdown.py`, the strict report check in `app/main_window.py`, and the restart command construction.

### Task 1: Implement the real PLC staged shutdown contract

**Files:**
- Create: `tests/test_plc_service_shutdown.py`
- Modify: `tests/multi_run_integration/test_shutdown_order.py`
- Modify: `app/service/plc_service.py:119-135`
- Modify: `app/services_container.py:202-215`
- Test: `tests/test_plc_service_shutdown.py`, `tests/multi_run_integration/test_shutdown_order.py`

- [ ] **Step 1: Write the failing Qt-slot contract test**

Create `tests/test_plc_service_shutdown.py` with minimal bus/logger/timer/client stubs and a real `PlcService`. Start with a test that checks the exact methods expected by `PlcServiceThreadShutdownOwner` are exported through the Qt meta-object:

```python
from __future__ import annotations

from unittest.mock import MagicMock

from app.service.plc_service import PlcService


class _SignalStub:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _BusStub:
    def __init__(self):
        self.log = _SignalStub()


def _service():
    bus = _BusStub()
    service = PlcService(
        bus,
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"start_signal_offset": "0.0"},
        plc_id=7,
    )
    service.logger = MagicMock()
    return service, bus


def test_staged_shutdown_methods_are_qt_slots():
    meta = PlcService.staticMetaObject

    assert meta.indexOfMethod(b"stop_ingress_polling()") >= 0
    assert meta.indexOfMethod(b"disconnect_plc_service()") >= 0
```

- [ ] **Step 2: Run the contract test to verify it fails**

Run:

```powershell
py -3 -m pytest tests/test_plc_service_shutdown.py::test_staged_shutdown_methods_are_qt_slots -v
```

Expected: FAIL because both `indexOfMethod` results are `-1`.

- [ ] **Step 3: Add failing behavior tests for both stages and the compatibility entry point**

Extend the test file with a timer stub whose `stop()` and `deleteLater()` calls are recorded, then add these tests:

```python
class _TimerStub:
    def __init__(self, trace, name):
        self._trace = trace
        self._name = name

    def stop(self):
        self._trace.append(f"{self._name}.stop")

    def deleteLater(self):
        self._trace.append(f"{self._name}.delete")


def test_stop_ingress_polling_stops_timers_without_disconnecting():
    service, bus = _service()
    trace = []
    client = MagicMock()
    service.timer = _TimerStub(trace, "poll")
    service.value_timer = _TimerStub(trace, "value")
    service.plc_client = client
    service._last_value = True

    service.stop_ingress_polling()

    assert trace == [
        "poll.stop",
        "poll.delete",
        "value.stop",
        "value.delete",
    ]
    assert service.timer is None
    assert service.value_timer is None
    assert service._last_value is None
    assert service.plc_client is client
    client.disconnect.assert_not_called()
    assert bus.log.calls == []


def test_disconnect_stage_disconnects_and_reports_stopped():
    service, bus = _service()
    client = MagicMock()
    service.plc_client = client
    states = []
    service.state_changed.connect(states.append)

    service.disconnect_plc_service()

    client.disconnect.assert_called_once_with()
    assert service.plc_client is None
    assert states[-1] == "未连接"
    assert bus.log.calls == [("PLC7停止监听",)]
    service.logger.info.assert_called_with("PLC7停止监听")


def test_legacy_stop_composes_staged_shutdown_in_order(monkeypatch):
    service, _bus = _service()
    trace = []
    monkeypatch.setattr(
        service,
        "stop_ingress_polling",
        lambda: trace.append("ingress"),
    )
    monkeypatch.setattr(
        service,
        "disconnect_plc_service",
        lambda: trace.append("disconnect"),
    )

    service.stop_plc_service()

    assert trace == ["ingress", "disconnect"]
```

Also add an idempotency test that calls both new stages twice with absent timers and no connected client, asserting no exception, all resource attributes remain `None`, and the final state is “未连接”.

- [ ] **Step 4: Add a failing production-boundary integration test**

In `tests/multi_run_integration/test_shutdown_order.py`, import `MagicMock`, `QCoreApplication`, `QThread`, the real `PlcService`, and the production `_invoke_plc_thread_slot` helper. Add this test next to the existing PLC split-stop tests:

```python
def test_real_plc_service_completes_thread_affine_application_shutdown():
    app = QCoreApplication.instance() or QCoreApplication([])
    _trace, _calls, resources = _harness()
    bus = SimpleNamespace(log=SimpleNamespace(emit=lambda *_args: None))
    client = MagicMock()
    service = PlcService(
        bus,
        lambda *_args: None,
        "127.0.0.1",
        field_offsets_dict={"start_signal_offset": "0.0"},
        plc_id=7,
    )
    service.plc_client = client
    thread = QThread()
    service.moveToThread(thread)
    thread.finished.connect(service.deleteLater)
    thread.start()
    binding = PlcServiceThreadShutdownOwner(
        service,
        thread,
        _invoke_plc_thread_slot,
    )
    resources = replace(
        resources,
        external_producers=(binding,),
        plc_owners=(binding,),
    )

    try:
        report = ApplicationShutdownOwner(
            resources,
            monotonic=_Clock(),
            timeout_seconds=5.0,
        ).shutdown()

        assert report.clean is True
        assert report.restart_required is False
        client.disconnect.assert_called_once_with()
        assert thread.isRunning() is False
    finally:
        if thread.isRunning():
            thread.quit()
            thread.wait(1000)
        del app
```

The imports must be:

```python
from unittest.mock import MagicMock

from PyQt6.QtCore import QCoreApplication, QThread

from app.service.plc_service import PlcService
from app.services_container import _invoke_plc_thread_slot
```

This test must use the existing `_harness()` exact-clean resources for every non-PLC stage. Do not replace the production binding or invocation helper with test doubles.

- [ ] **Step 5: Run all new tests to verify they fail for the missing methods**

Run:

```powershell
py -3 -m pytest tests/test_plc_service_shutdown.py tests/multi_run_integration/test_shutdown_order.py::test_real_plc_service_completes_thread_affine_application_shutdown -v
```

Expected: FAIL because `PlcService` does not yet implement the two staged Qt slots, so the production blocking invocation fails and the aggregate shutdown report is non-clean.

- [ ] **Step 6: Implement the minimal staged shutdown slots**

Replace the body of `stop_plc_service` in `app/service/plc_service.py` with delegation, and move its existing cleanup into two new slots:

```python
    @pyqtSlot()
    def stop_ingress_polling(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.value_timer is not None:
            self.value_timer.stop()
            self.value_timer.deleteLater()
            self.value_timer = None
        self._last_value = None

    @pyqtSlot()
    def disconnect_plc_service(self):
        self._disconnect()
        self.state_changed.emit("未连接")
        self.bus.log.emit(f"PLC{self._plc_id}停止监听")
        self.logger.info(f"PLC{self._plc_id}停止监听")

    @pyqtSlot()
    def stop_plc_service(self):
        self.logger.info("停止 PLC 服务")
        self.stop_ingress_polling()
        self.disconnect_plc_service()
```

Do not change `PlcServiceThreadShutdownOwner`, shutdown stage ordering, or restart safety checks. A minimal internal change to `_disconnect` is allowed only to return or record the already-caught disconnect failure while preserving all existing callers and side effects.

The Qt slot boundary must also explicitly acknowledge physical disconnect failure. Python exceptions raised by a Qt slot invoked through `BlockingQueuedConnection` are not a reliable caller-side result, and the existing `_disconnect()` intentionally swallows client errors for legacy runtime/manual behavior. Therefore:

- `disconnect_plc_service()` must record an immutable success/failure acknowledgement for its most recent invocation while preserving the legacy combined-stop behavior.
- `_invoke_plc_thread_slot()` must check that acknowledgement immediately after the blocking `disconnect_plc_service` invocation and raise in the shutdown caller on failure.
- The acknowledgement must be reset at the start of every disconnect invocation so stale failures cannot contaminate later calls.
- Add a real-thread integration regression in `tests/multi_run_integration/test_shutdown_order.py` with `client.disconnect.side_effect = RuntimeError("disconnect failed")`; require `report.clean is False`, `report.restart_required is True`, and a `plc_owners[0]` error containing the failure detail.
- Do not change manual-disconnect UI behavior or weaken the aggregate exact-clean gate.

- [ ] **Step 7: Run the focused tests and verify they pass**

Run:

```powershell
py -3 -m pytest tests/test_plc_service_shutdown.py tests/test_plc_service_current.py tests/multi_run_integration/test_shutdown_order.py::test_real_plc_service_completes_thread_affine_application_shutdown -q
```

Expected: all tests PASS.

- [ ] **Step 8: Run the shutdown and restart regression suites**

Run:

```powershell
py -3 -m pytest tests/multi_run_integration/test_shutdown_order.py tests/test_service_container.py tests/test_main_window.py -q
```

Expected: all tests PASS with no new warnings or Qt thread-lifecycle errors attributable to this change.

- [ ] **Step 9: Run diff validation**

Run:

```powershell
git diff --check
git status --short
```

Expected: `git diff --check` produces no output. Only `app/service/plc_service.py`, `tests/test_plc_service_shutdown.py`, `tests/multi_run_integration/test_shutdown_order.py`, and the tracked plan checklist may be changed by this task; pre-existing unrelated worktree changes must remain untouched.

- [ ] **Step 10: Commit the implementation**

```powershell
git add -- app/service/plc_service.py tests/test_plc_service_shutdown.py tests/multi_run_integration/test_shutdown_order.py
git commit -m "fix: restore restart PLC shutdown contract"
```
