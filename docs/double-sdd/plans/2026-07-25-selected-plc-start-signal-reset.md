# Selected PLC Start Signal Reset Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a clean `False` baseline for the start bit of PLCs bound to startup-selected or newly checked configurations, without changing the existing rising-edge trigger rule.

**Architecture:** Add a process-local reset coordinator to `ServiceContainer`: it maps configuration IDs to deduplicated PLC IDs, retains pending IDs even before a PLC service exists, publishes only to registered services, and removes an ID only after a success acknowledgement. `PlcService` performs the Snap7 write in its worker thread and retains local requests across disconnects; `WorkOrderModule` creates startup/new-selection requests and `PlcSettingsModule` wires and replays pending requests after service registration.

**Tech Stack:** Python 3.14, PyQt6 signals/slots and worker threads, python-snap7, pytest

---

## File Structure

- Modify `app/services_container.py`: own reset request/result signals, process-local pending IDs, configuration-to-PLC routing, and acknowledgement state.
- Modify `app/service/plc_service.py`: execute matching reset requests, retry after reconnect, acknowledge outcomes, and establish a `False` polling baseline.
- Modify `ui/modules/workorder.py`: create requests for restored startup selection and only newly checked configuration IDs.
- Modify `ui/modules/plc_settings.py`: register PLC reset receivers and replay retained requests after service registration.
- Modify `tests/test_service_container.py`: verify the reset coordinator independently.
- Modify `tests/test_plc_service_protocol.py`: verify the PLC-thread reset protocol independently.
- Modify `tests/test_workorder_module.py`: verify startup and user-selection request timing.
- Modify `tests/test_plc_settings.py`: verify service wiring and retained-request replay.

### Task 1: Add the Process-Local Reset Coordinator

**Files:**
- Modify: `app/services_container.py`
- Test: `tests/test_service_container.py`

- [ ] **Step 1: Write failing EventBus and coordinator tests**

Extend the EventBus signal-surface assertions for:

```python
request_plc_start_signal_reset = pyqtSignal(int)
plc_start_signal_reset_result = pyqtSignal(int, bool, str)
```

Create a lightweight `ServiceContainer` test fixture without heavyweight
runtime services. Give it:

```python
services.bus = EventBus()
services.config_service = ConfigServiceStub(...)
services.plc_services = {}
services.pending_plc_start_signal_reset_ids = set()
services.bus.plc_start_signal_reset_result.connect(
    services._on_plc_start_signal_reset_result
)
```

Test the public contract with configs `101` and `202` both binding PLC `7`,
config `303` binding PLC `8`, plus unbound/invalid/load-failure configs:

```python
plc_ids = services.request_plc_start_signal_resets_for_configs(
    (101, 202, 303, 404, 505)
)

assert plc_ids == (7, 8)
assert services.pending_plc_start_signal_reset_ids == {7, 8}
assert requested_plc_ids == []
```

The empty emission list is required because neither PLC service is registered.
Then register PLC `7` in `services.plc_services` and verify:

```python
assert services.dispatch_pending_plc_start_signal_reset(7) is True
assert requested_plc_ids == [7]
```

Also verify:

- a repeated configuration request while PLC `7` remains pending does not
  publish a duplicate;
- failed acknowledgement retains `7`;
- successful acknowledgement removes `7`;
- a later fresh request for PLC `7` publishes again when its service is still
  registered;
- PLC `8` remains retained without emission until it is registered and
  explicitly replayed;
- skipped/failed configuration loads reach `bus.log`.

- [ ] **Step 2: Run the coordinator tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_service_container.py
```

Expected: failures because the signals and coordinator methods do not exist.

- [ ] **Step 3: Implement coordinator state and routing**

In `EventBus`, add the two typed signals.

In `ServiceContainer.__post_init__()` initialize and connect:

```python
self.pending_plc_start_signal_reset_ids: set[int] = set()
self.bus.plc_start_signal_reset_result.connect(
    self._on_plc_start_signal_reset_result
)
```

Add:

```python
def request_plc_start_signal_resets_for_configs(
    self,
    config_ids: tuple[int, ...] | list[int],
) -> tuple[int, ...]:
    ...

def dispatch_pending_plc_start_signal_reset(self, plc_id: int) -> bool:
    ...

def _on_plc_start_signal_reset_result(
    self,
    plc_id: int,
    success: bool,
    message: str,
) -> None:
    ...
```

Use `config_service.load_config()`, accept only `int` `plc_id` values that are
not `bool`, and preserve first-seen order. Add valid targets to the pending set.
On first addition, publish immediately only when the PLC ID is a key in
`getattr(self, "plc_services", {})`; otherwise retain it without publishing.
`dispatch_pending...()` is the explicit replay path after service registration.
On success discard the ID; on failure retain it. Log skipped configs and
failure messages through `bus.log.emit()` without aborting the batch.

- [ ] **Step 4: Run Task 1 tests and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_service_container.py
git diff --check -- app/services_container.py tests/test_service_container.py
git add app/services_container.py tests/test_service_container.py
git commit -m "feat: coordinate pending PLC start resets"
```

Expected: coordinator tests pass and only Task 1 files are committed.

### Task 2: Execute Reset Requests in the PLC Worker

**Files:**
- Modify: `app/service/plc_service.py`
- Test: `tests/test_plc_service_protocol.py`

- [ ] **Step 1: Write failing PLC reset protocol tests**

Extend `BusStub` with `plc_start_signal_reset_result`. Using
`FakeS7Client` and `start_signal_offset="0.0"`, verify:

```python
service._last_value = True
service.request_start_signal_reset(22)

assert snap7.util.get_bool(client.memory, 0, 0) is False
assert service._last_value is False
assert service._start_signal_reset_pending is False
assert bus.plc_start_signal_reset_result.calls[-1] == (22, True, "")
assert any("启动信号已归位" in call[0] for call in bus.log.calls)
```

Add tests for:

- another PLC ID causing no write, acknowledgement, or reset log;
- a disconnected client retaining the request;
- `_connect_once()` applying a retained request before normal polling;
- a Snap7 write failure emitting `(plc_id, False, message)`, logging the
  failure, retaining the request, disconnecting, and succeeding after reconnect;
- missing/invalid `start_signal_offset` following the same logged failure and
  retained-request path;
- after success, an immediate high read being a genuine `False -> True` edge;
- existing first-read-high behavior still not triggering without a reset.

- [ ] **Step 2: Run PLC protocol tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_service_protocol.py
```

Expected: failures because the PLC reset slot/state do not exist.

- [ ] **Step 3: Implement PLC-thread reset handling**

Initialize:

```python
self._start_signal_reset_pending = False
```

Add a `@pyqtSlot(int)` method that ignores foreign/boolean IDs, sets the local
pending flag, and attempts the write. The private apply method must:

- return when there is no pending request or client;
- write `False` through `write_field_value("start_signal_offset", False)`;
- treat a false return or missing/invalid offset as failure;
- on success set `_last_value = False`, clear local pending, log
  `PLC <id> 启动信号已归位`, and emit `(plc_id, True, "")`;
- on failure retain pending, emit `(plc_id, False, message)`, log the message,
  and call `_disconnect()` for the existing reconnect loop.

Call the private apply method from the successful `_connect_once()` path before
normal polling continues. Do not change `poll_once()` first-read/rising-edge
conditions or the existing post-trigger automatic reset.

- [ ] **Step 4: Run Task 2 tests and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_service_protocol.py tests/test_plc_service_current.py
git diff --check -- app/service/plc_service.py tests/test_plc_service_protocol.py
git add app/service/plc_service.py tests/test_plc_service_protocol.py
git commit -m "feat: reset PLC start bit on request"
```

Expected: PLC protocol/current tests pass and both success and failure logging
assertions are green.

### Task 3: Create Reset Requests From Startup and User Selection

**Files:**
- Modify: `ui/modules/workorder.py`
- Test: `tests/test_workorder_module.py`

- [ ] **Step 1: Write failing work-order lifecycle tests**

Extend the work-order services stub with:

```python
def request_plc_start_signal_resets_for_configs(self, config_ids):
    self.operation_order.append(("reset", tuple(config_ids)))
```

Verify construction with restored IDs `(303, 101)` requests that final
validated/deduplicated tuple once after `restore_selection()`.

Starting from `(101,)`, check `202` and assert:

```python
assert ("reset", (202,)) in operation_order
assert operation_order.index(("reset", (202,))) < operation_order.index(
    ("set_selection", (101, 202))
)
```

Verify unchecking, same-selection synchronization, and drag-only reordering
create no reset request. Existing test doubles without the coordinator method
must continue to work via a callable check.

- [ ] **Step 2: Run work-order tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_workorder_module.py
```

Expected: reset-request assertions fail.

- [ ] **Step 3: Integrate startup and newly checked IDs**

In `_restore_startup_selection()`, after the controller accepts the validated
`restored_selection`, call the coordinator once with exactly that tuple.

In `_on_config_selection_changed()`:

1. snapshot `previous_selected` from `self._factory_state.selected_config_ids`;
2. preserve UI order while collecting IDs in the new tuple but absent from the
   previous set;
3. call the coordinator with only non-empty added IDs;
4. then continue with current `set_selection()`, UI sync, publish, and persist.

Do not request for removal, unchanged selection, controller sync, or reorder.

- [ ] **Step 4: Run Task 3 tests and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_workorder_module.py tests/test_control_module.py
git diff --check -- ui/modules/workorder.py tests/test_workorder_module.py
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "feat: request resets for selected PLC configs"
```

Expected: work-order/control tests pass.

### Task 4: Register PLC Reset Receivers and Replay Retained Requests

**Files:**
- Modify: `ui/modules/plc_settings.py`
- Test: `tests/test_plc_settings.py`

- [ ] **Step 1: Write failing PLC service registration tests**

Extend the PLC service stub with `request_start_signal_reset()`, the bus with a
reset-request signal stub, and services with a
`dispatch_pending_plc_start_signal_reset()` recorder.

After `start_connect(row)`, verify:

```python
assert services.bus.request_plc_start_signal_reset.callbacks == [
    plc_service.request_start_signal_reset
]
assert services.dispatch_pending_calls == [row.id]
```

Record order and prove the service is present in `services.plc_services` and
the worker thread has started before replay dispatch. Preserve all existing
testing-state/result/limit wiring assertions.

- [ ] **Step 2: Run PLC settings tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_settings.py
```

Expected: receiver/replay assertions fail.

- [ ] **Step 3: Wire and replay after registration**

In `PlcSettingsModule.start_connect()`:

1. connect `bus.request_plc_start_signal_reset` to
   `plc_service.request_start_signal_reset`;
2. keep existing result, limit, testing-state, state, and shutdown wiring;
3. register the service/thread and start the thread;
4. after registration/start, call
   `services.dispatch_pending_plc_start_signal_reset(row.id)` when callable.

Do not infer current selection or create a new request here. The coordinator
alone decides whether replay is needed.

- [ ] **Step 4: Run focused and broader regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_settings.py tests/test_service_container.py tests/test_plc_service_protocol.py tests/test_plc_service_current.py tests/test_workorder_module.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_factory_mode_controller tests/test_control_module.py tests/test_main_window.py
git diff --check
```

Expected: all focused and broader regression tests pass with no whitespace
errors.

- [ ] **Step 5: Commit Task 4**

```powershell
git add ui/modules/plc_settings.py tests/test_plc_settings.py
git commit -m "feat: replay pending PLC start resets"
```

Before reporting completion, run the full repository test command used by this
project and report any pre-existing baseline failure separately from new
failures.
