# Resident AI Execution Mode Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit `resident_process | oneshot_process` support for AI toolchains, make `resident_process` the default and recommended mode, implement resident stdio execution without auto-fallback, and expose execution-mode selection in `AI工具管理`.

**Architecture:** Split the change into five slices. First make `execution_mode` a real supported registry field across the model, migrations, and service validation. Then update the AI tool management page to edit and display supported modes. After that add a dedicated resident-runtime helper that owns stdio JSON Lines transport, exact launch semantics, locks, stderr draining, and cache invalidation. Next route `AiService.run_ai_analysis()` by execution mode and make resident-toolchain mutation invalidation explicit for save / rename / delete. Finally wire application shutdown to resident runtime cleanup and run the full verification pass.

**Tech Stack:** Python, PyQt6, Peewee ORM, SQLite, subprocess, threading, pytest

---

**Spec:** `docs/double-sdd/specs/2026-04-07-resident-ai-execution-mode-design.md`

## File Map

- `model/ai_toolchain_table.py`
  Change the default persisted `execution_mode` from `oneshot_process` to `resident_process`.

- `app/service/db_connection_service.py`
  Update bootstrap / migration column definitions so newly added `execution_mode` columns declare the new supported default without introducing a separate historical-row rewrite step.

- `app/service/ai_service.py`
  Replace the fixed execution-mode normalization with explicit supported-mode validation, integrate the resident runtime, route `run_ai_analysis()` by toolchain mode, expose shutdown, and make resident mutation invalidation explicit.

- `app/service/ai_resident_runtime.py`
  New focused helper that owns resident subprocess lifecycle, exact entrypoint launch semantics, per-toolchain locks, stdio JSONL transport, stderr draining, idle-stdout invalidation, and cache invalidation.

- `app/services_container.py`
  Add an app-level shutdown boundary that stops resident AI processes before other services are torn down.

- `app/main_window.py`
  Route window close cleanup through the shared shutdown boundary so resident toolchains are stopped on application exit.

- `ui/modules/ai_tool_management.py`
  Replace the fixed `execution_mode` helper text with an editable supported-value selector and resident-mode helper copy.

- `tests/test_ai_toolchain_table.py`
  Update model-default and explicit-value coverage for `resident_process` / `oneshot_process`.

- `tests/test_db_connection_service.py`
  Update runtime migration expectations for the new `execution_mode` default.

- `tests/test_ai_service.py`
  Cover supported execution-mode validation, mutation invalidation, busy rejection, and no-unsupported-value behavior.

- `tests/test_ai_resident_runtime.py`
  New unit-level coverage for resident subprocess lifecycle, JSONL transport, exact launch semantics, stderr draining, timeouts, idle-stdout invalidation, and cache invalidation.

- `tests/test_ai_service_integration.py`
  Add resident-mode end-to-end tests using repo-local fake resident toolchain scripts.

- `tests/test_ai_tool_management_module.py`
  Update the widget tests for execution-mode defaults, selection, validation, and busy save/delete errors.

- `tests/test_service_container.py`
  Verify app shutdown delegates to `AiService` resident cleanup.

- `tests/test_main_window.py`
  Verify main-window close uses the shared shutdown boundary after leave guards pass.

## Task 1: Make `execution_mode` A Real Supported Registry Field

**Files:**
- Modify: `model/ai_toolchain_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_toolchain_table.py`
- Modify: `tests/test_db_connection_service.py`
- Modify: `tests/test_ai_service.py`

- [ ] **Step 1: Write the failing model / migration / registry validation tests**

Add or update focused tests:

```python
def test_defaults_to_resident_process():
    row = AiToolchainTable.create(
        toolchain_id="minimal-tc",
        display_name="Minimal",
        entrypoint="echo ok",
    )
    assert row.execution_mode == "resident_process"


def test_save_toolchain_accepts_supported_execution_modes(ai_service):
    ai_service.save_toolchain(
        None,
        _toolchain_payload(
            toolchain_id="resident-ai",
            execution_mode="resident_process",
        ),
    )
    ai_service.save_toolchain(
        None,
        _toolchain_payload(
            toolchain_id="oneshot-ai",
            execution_mode="oneshot_process",
        ),
    )


def test_save_toolchain_rejects_unsupported_execution_mode(ai_service):
    with pytest.raises(ValueError, match="execution_mode"):
        ai_service.save_toolchain(
            None,
            _toolchain_payload(execution_mode="daemon"),
        )
```

Update DB migration expectations so:

- fresh/default toolchain rows now use `resident_process`
- legacy schemas that are missing the `execution_mode` column gain that column with a declared default of `resident_process`
- no extra migration step rewrites already persisted supported `execution_mode` values

- [ ] **Step 2: Run the focused registry tests to confirm they fail**

Run:

```bash
pytest tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_ai_service.py -k "execution_mode or resident_process or oneshot_process" -v
```

Expected: FAIL because the current model, migrations, and service still hard-wire `oneshot_process`.

- [ ] **Step 3: Implement supported execution-mode validation**

In `app/service/ai_service.py`, replace the single fixed-mode constant with an explicit supported set:

```python
_DEFAULT_EXECUTION_MODE = "resident_process"
_SUPPORTED_EXECUTION_MODES = {
    "resident_process",
    "oneshot_process",
}
```

Implement payload normalization rules:

- create defaults to `resident_process` when callers omit `execution_mode`
- update preserves caller-selected supported values
- unsupported values raise `ValueError`
- `protocol_version` remains fixed to `1`
- DB migration work updates the schema default only; do not add a separate `UPDATE` pass that rewrites already persisted supported execution modes

Update:

- `model/ai_toolchain_table.py` default
- `app/service/db_connection_service.py` migration default
- `app/service/ai_service.py` normalization / save paths

- [ ] **Step 4: Re-run the focused registry tests**

Run:

```bash
pytest tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_ai_service.py -k "execution_mode or resident_process or oneshot_process" -v
```

Expected: PASS

- [ ] **Step 5: Commit the registry-contract slice**

Run:

```bash
git add model/ai_toolchain_table.py app/service/db_connection_service.py app/service/ai_service.py tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_ai_service.py
git commit -m "feat: support explicit ai execution modes"
```

## Task 2: Update `AI工具管理` To Edit `execution_mode`

**Files:**
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_tool_management_module.py`

- [ ] **Step 1: Write the failing widget tests for execution-mode editing**

Extend `tests/test_ai_tool_management_module.py` with coverage like:

```python
def test_new_toolchain_defaults_execution_mode_to_resident_process(qapp):
    module, _service, _bus = _make_module(toolchains=[])
    module.btn_new.click()
    assert module.combo_execution_mode.currentData() == "resident_process"


def test_selecting_row_populates_execution_mode(qapp):
    module, _service, _bus = _make_module(
        toolchains=[_toolchain("motor-ai", execution_mode="oneshot_process")]
    )
    module._select_toolchain("motor-ai")
    assert module.combo_execution_mode.currentData() == "oneshot_process"


def test_save_persists_selected_execution_mode(qapp):
    module, service, _bus = _make_module(toolchains=[])
    module.btn_new.click()
    module.ed_toolchain_id.setText("motor-ai")
    module.ed_display_name.setText("Motor AI")
    module.ed_entrypoint.setPlainText("python motor_ai.py")
    module.combo_execution_mode.setCurrentIndex(
        module.combo_execution_mode.findData("oneshot_process")
    )
    module.btn_save.click()
    assert service.saved_requests[-1][1]["execution_mode"] == "oneshot_process"
```

Also add a failing test for a busy save/delete error being surfaced as a user-visible warning.

- [ ] **Step 2: Run the widget tests to confirm they fail**

Run:

```bash
pytest tests/test_ai_tool_management_module.py -k "execution_mode or resident_process or busy" -v
```

Expected: FAIL because the current module still treats `execution_mode` as fixed helper text.

- [ ] **Step 3: Implement the editable execution-mode selector**

Replace the fixed runtime helper with:

- editable `QComboBox` for `execution_mode`
- helper text that explains:
  - `protocol_version = 1`
  - `resident_process` is recommended
  - `oneshot_process` remains available for file-based CLI toolchains

Suggested data mapping:

```python
self.combo_execution_mode.addItem("常驻进程（推荐）", "resident_process")
self.combo_execution_mode.addItem("单次进程", "oneshot_process")
```

Update editor payload collection / normalization so execution mode round-trips through save and selection reload.

- [ ] **Step 4: Re-run the widget tests**

Run:

```bash
pytest tests/test_ai_tool_management_module.py -k "execution_mode or resident_process or busy" -v
```

Expected: PASS

- [ ] **Step 5: Commit the AI tool management slice**

Run:

```bash
git add ui/modules/ai_tool_management.py tests/test_ai_tool_management_module.py
git commit -m "feat: expose ai execution mode in tool management"
```

## Task 3: Add The Resident StdIO Runtime Helper

**Files:**
- Create: `app/service/ai_resident_runtime.py`
- Create: `tests/test_ai_resident_runtime.py`

- [ ] **Step 1: Write the failing resident-runtime unit tests**

Create `tests/test_ai_resident_runtime.py` with repo-local fake resident scripts that speak JSON Lines over stdio.

Cover at least:

```python
def test_reuses_cached_process_for_repeated_requests(...):
    first = runtime.analyze(toolchain_id="tc-1", ...)
    second = runtime.analyze(toolchain_id="tc-1", ...)
    assert second["process_instance_id"] == first["process_instance_id"]


def test_busy_second_request_fails_fast(...):
    # start one request that blocks in the child
    # second overlapping analyze call returns process_error / busy


def test_message_type_error_maps_to_process_error_and_discards_cache(...):
    result = runtime.analyze(...)
    assert result["module_status"] == "process_error"
    assert runtime.has_cached_process("tc-1") is False
```

Also add failing tests for:

- launches the resident entrypoint exactly as stored, with no host-appended flags or per-request argv, while inheriting parent cwd/env
- malformed JSON line -> `protocol_error`
- unexpected `message_type` -> `protocol_error`
- missing / mismatched `request_id` -> `protocol_error`
- timeout waiting for response -> `timeout`
- idle extra stdout after a successful result invalidates cache but does not retroactively fail the completed run
- protocol stdout frames never become `stdout_excerpt`
- stderr draining produces bounded per-run excerpts and does not grow without limit
- invalidate / shutdown sends best-effort shutdown and then terminates after the 2000 ms grace period when needed

- [ ] **Step 2: Run the resident-runtime tests to confirm they fail**

Run:

```bash
pytest tests/test_ai_resident_runtime.py -v
```

Expected: FAIL because the resident runtime helper does not exist yet.

- [ ] **Step 3: Implement the focused resident runtime helper**

Create `app/service/ai_resident_runtime.py` with a small focused API, for example:

```python
class ResidentToolchainRuntime:
    def analyze(self, *, toolchain_id: str, entrypoint: str, request_id: str, request_payload: dict, artifacts_dir: str, timeout_ms: int) -> dict:
        ...

    def invalidate(self, toolchain_id: str) -> None:
        ...

    def ensure_mutation_allowed(self, toolchain_id: str) -> None:
        ...

    def shutdown_all(self) -> None:
        ...
```

Implementation responsibilities:

- cache by persisted `toolchain_id`
- non-blocking per-toolchain lease for analyze vs mutation
- launch the configured `entrypoint` exactly as stored after shell-style splitting; do not append resident-mode argv, and inherit current cwd/env by leaving them unset on `Popen`
- background stderr draining with a 64 KiB rolling buffer
- one JSON object per line over stdio
- flush after every request line
- assume the child flushes every response line
- never copy resident protocol frames from `stdout` into `stdout_excerpt`; instead attribute only newly drained `stderr` text to the current run and truncate it using the existing excerpt policy
- if an extra unsolicited stdout line appears after a successful run while the process is idle, invalidate the cached process before the next reuse
- 2000 ms shutdown grace period before force terminate

- [ ] **Step 4: Re-run the resident-runtime tests**

Run:

```bash
pytest tests/test_ai_resident_runtime.py -v
```

Expected: PASS

- [ ] **Step 5: Commit the resident-runtime slice**

Run:

```bash
git add app/service/ai_resident_runtime.py tests/test_ai_resident_runtime.py
git commit -m "feat: add resident ai stdio runtime"
```

## Task 4: Route `AiService.run_ai_analysis()` By Execution Mode

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_service.py`
- Modify: `tests/test_ai_service_integration.py`

- [ ] **Step 1: Write the failing service and integration tests for runtime routing**

Add focused tests in `tests/test_ai_service.py` for service-level boundaries:

```python
def test_save_toolchain_rejects_mutation_while_resident_request_in_flight(ai_service, toolchain, monkeypatch):
    monkeypatch.setattr(ai_service._resident_runtime, "ensure_mutation_allowed", lambda toolchain_id: (_ for _ in ()).throw(ValueError("Resident toolchain is busy")))
    with pytest.raises(ValueError, match="busy"):
        ai_service.save_toolchain("motor-ai", _toolchain_payload(execution_mode="resident_process"))
```

Also add explicit service tests for:

- resident save/update invalidates the cached process after a successful mutation
- resident rename checks the old `toolchain_id` lease, invalidates the old cache slot after success, and does not silently remap in-flight work
- resident delete checks the toolchain lease and invalidates the cache only after a successful delete transaction
- busy delete / rename propagate user-visible errors instead of mutating partially

Extend `tests/test_ai_service_integration.py` with resident-mode fake scripts:

- success path that reuses one resident process across two runs
- `message_type = error` -> `process_error`, no `response.json`, cache discard
- malformed JSON line -> `protocol_error`
- timeout -> `timeout`
- overlapping second request -> `process_error` with busy summary
- resident failure does not retry as one-shot
- resident success still writes `request.json`, `response.json`, and artifacts

- [ ] **Step 2: Run the focused routing tests to confirm they fail**

Run:

```bash
pytest tests/test_ai_service.py tests/test_ai_service_integration.py -k "resident or execution_mode or oneshot_process or busy" -v
```

Expected: FAIL because `AiService` still only executes the one-shot path.

- [ ] **Step 3: Integrate resident routing into `AiService`**

Add `ResidentToolchainRuntime` ownership in `AiService.__init__()` and update:

- `save_toolchain()`
- `delete_toolchain()`
- any rename path

so resident mutations:

- call `ensure_mutation_allowed(toolchain_id)` before mutating
- invalidate cached resident processes after successful save / rename / delete
- for rename, treat the persisted old `toolchain_id` as the mutation/lease key, then invalidate the old cache slot after the transaction commits
- for delete, do not invalidate or stop the cached process until the delete transaction has succeeded

In `run_ai_analysis()`:

- build `request.json` before transport
- branch by `tc.execution_mode`
- for `oneshot_process`, keep the current subprocess file path
- for `resident_process`, send:

```python
{
    "message_type": "analyze",
    "request_id": request_id,
    "request": request_data,
    "artifacts_dir": artifacts_dir,
}
```

- write `response.json` only when resident transport returns a valid logical response payload
- reuse the current validation / envelope persistence path after transport
- do not retry resident failures through the one-shot path

- [ ] **Step 4: Re-run the focused routing tests**

Run:

```bash
pytest tests/test_ai_service.py tests/test_ai_service_integration.py -k "resident or execution_mode or oneshot_process or busy" -v
```

Expected: PASS

- [ ] **Step 5: Commit the runtime-routing slice**

Run:

```bash
git add app/service/ai_service.py tests/test_ai_service.py tests/test_ai_service_integration.py
git commit -m "feat: route ai analysis by execution mode"
```

## Task 5: Wire Application Shutdown To Resident Runtime Cleanup

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `app/services_container.py`
- Modify: `app/main_window.py`
- Modify: `tests/test_service_container.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Write the failing shutdown tests**

Add focused tests that prove resident cleanup is triggered on application shutdown:

```python
def test_ai_service_shutdown_delegates_to_resident_runtime(ai_service, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_service._resident_runtime, "shutdown_all", lambda: calls.append("shutdown"))
    ai_service.shutdown()
    assert calls == ["shutdown"]


def test_service_container_shutdown_stops_ai_before_closing_db(monkeypatch):
    ...


def test_main_window_close_event_uses_services_shutdown_after_leave_guard(qapp, monkeypatch):
    ...
```

The service-container / main-window assertions must prove:

- resident cleanup is invoked on the normal close path
- the existing AI-tool-management unsaved-changes guard still blocks close before shutdown starts
- DB close still happens through the shared shutdown path

- [ ] **Step 2: Run the shutdown-focused tests to confirm they fail**

Run:

```bash
pytest tests/test_service_container.py tests/test_main_window.py -k "shutdown or close_event" -v
```

Expected: FAIL because the app currently closes the DB directly and has no resident-runtime shutdown boundary.

- [ ] **Step 3: Implement the shared shutdown boundary**

Implement:

- `AiService.shutdown()` -> delegate to the resident runtime
- `ServiceContainer.shutdown()` -> stop resident AI first, then close the DB connection
- `MainWindow.closeEvent()` -> call `services.shutdown()` after leave guards pass instead of closing the DB directly

Keep the existing window-close behavior for camera cleanup and error swallowing consistent with current app patterns.

- [ ] **Step 4: Re-run the shutdown-focused tests**

Run:

```bash
pytest tests/test_service_container.py tests/test_main_window.py -k "shutdown or close_event" -v
```

Expected: PASS

- [ ] **Step 5: Commit the shutdown slice**

Run:

```bash
git add app/service/ai_service.py app/services_container.py app/main_window.py tests/test_service_container.py tests/test_main_window.py
git commit -m "feat: stop resident ai runtimes on shutdown"
```

## Task 6: Full Verification And Scope Check

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `app/service/ai_resident_runtime.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `app/services_container.py`
- Modify: `app/main_window.py`
- Modify: `model/ai_toolchain_table.py`
- Modify: `ui/modules/ai_tool_management.py`
- Modify: `tests/test_ai_service.py`
- Modify: `tests/test_ai_service_integration.py`
- Modify: `tests/test_ai_resident_runtime.py`
- Modify: `tests/test_ai_tool_management_module.py`
- Modify: `tests/test_ai_toolchain_table.py`
- Modify: `tests/test_db_connection_service.py`
- Modify: `tests/test_service_container.py`
- Modify: `tests/test_main_window.py`

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
pytest tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_ai_service.py tests/test_ai_resident_runtime.py tests/test_ai_service_integration.py tests/test_ai_tool_management_module.py tests/test_service_container.py tests/test_main_window.py -v
```

Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run:

```bash
pytest -v
```

Expected: PASS

- [ ] **Step 3: Inspect the final diff for scope control**

Run:

```bash
git status --short --untracked-files=all
git diff -- app/service/ai_service.py app/service/ai_resident_runtime.py app/service/db_connection_service.py app/services_container.py app/main_window.py model/ai_toolchain_table.py ui/modules/ai_tool_management.py tests/test_ai_service.py tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_ai_tool_management_module.py tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_service_container.py tests/test_main_window.py
```

Expected: only the planned runtime, UI, and regression files are changed.

- [ ] **Step 4: Commit the verification pass**

Run:

```bash
git add app/service/ai_service.py app/service/ai_resident_runtime.py app/service/db_connection_service.py app/services_container.py app/main_window.py model/ai_toolchain_table.py ui/modules/ai_tool_management.py tests/test_ai_service.py tests/test_ai_service_integration.py tests/test_ai_resident_runtime.py tests/test_ai_tool_management_module.py tests/test_ai_toolchain_table.py tests/test_db_connection_service.py tests/test_service_container.py tests/test_main_window.py
git commit -m "test: verify resident ai execution mode"
```
