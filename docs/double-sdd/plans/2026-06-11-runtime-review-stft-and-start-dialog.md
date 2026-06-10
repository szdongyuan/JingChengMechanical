# Runtime Review STFT And Start Dialog Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the final STFT image visible after ordinary runtime completion and stop transient busy blocked-start dialogs from flashing during later run cycles.

**Architecture:** Track a run-bound display channel in `MainWindow` using `(config_id, run_id)` from runtime lifecycle signals, and preserve that channel only for the matching ordinary hardware review. Suppress non-actionable busy start-blocked reasons in `ControlModule` while preserving existing real failure dialogs.

**Tech Stack:** Python 3.12, PyQt6 widgets/signals, existing JingChengMechanical service dataclasses, pytest/pytest-qt.

---

## Spec

- `docs/double-sdd/specs/2026-06-11-runtime-review-stft-and-start-dialog-design.md`

## File Structure

- Modify `app/main_window.py`
  - Add run-bound display-channel tracking for ordinary runtime reviews.
  - Connect to `factory_runtime_start_accepted` and `factory_runtime_analyzing_entered`.
  - Preserve the run-bound channel before review-channel resolution only when `(config_id, run_id)` matches.
  - Clear run-bound tracking on display reset.
- Modify `ui/modules/control.py`
  - Suppress modal display for busy blocked-start reasons already defined by `_BUSY_START_READY_REASONS`.
- Modify `tests/test_main_window.py`
  - Add regression tests for runtime-to-review channel preservation with vibration channel configured.
  - Add stale/different-run regression coverage so direct-review behavior is unchanged.
- Modify `tests/test_control_module.py`
  - Add busy blocked-start modal suppression coverage.

## Task 1: Preserve Runtime STFT Display Channel Across Completion

**Files:**
- Modify: `app/main_window.py`
- Test: `tests/test_main_window.py`

- [ ] **Step 1: Add a failing main-window regression test for ordinary runtime completion**

Add a test near the existing display-channel/STFT main-window tests:

```python
def test_main_window_runtime_review_keeps_acoustic_channel_when_vibration_channel_configured(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_armed_state(),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
    }
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-1")
    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 0)
    services.bus.stft_updated.emit(
        101,
        "run-101-1",
        0,
        _make_stft_frame(time=0.0, magnitudes=(1.0, 2.0)),
    )
    qapp.processEvents()

    assert window.current_display_channel_index() == 0
    assert window.stft.current_render_cache_key() == (101, "run-101-1", 0)

    services.bus.factory_review_changed.emit(
        _review_payload_with_vibration(
            101,
            vibration_channel=1,
            run_id="run-101-1",
        )
    )
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    qapp.processEvents()

    assert window.current_display_channel_index() == 0
    assert window.analysis_toolbar.current_channel_index() == 0
    assert window.stft.current_render_cache_key() == (101, "run-101-1", 0)
    assert window.stft.current_image_payload() is not None
    assert _card_title(_test_page_grid_card(window, 1, 1)) == "时频图（频率Hz，幅值dB）"
```

Important: make the review payload `run_id` match the runtime run id. If
`_review_payload_with_vibration()` does not accept `run_id`, extend that test
helper only. Explicitly selecting channel `0` during the running phase is part
of the test setup: it proves a valid live acoustic STFT selection is lost only
when the completion review arrives.

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```powershell
python -m pytest tests/test_main_window.py::test_main_window_runtime_review_keeps_acoustic_channel_when_vibration_channel_configured -q
```

Expected: FAIL because review completion switches to channel `1` / vibration
mode or clears the STFT render cache.

- [ ] **Step 3: Add stale-run guard coverage**

Add a test near the same group:

```python
def test_main_window_review_with_unmatched_run_id_does_not_preserve_stale_runtime_channel(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
        "vibration_view_channel_index": 1,
    }
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-live")
    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 0)

    assert window.current_display_channel_index() == 0

    services.bus.factory_review_changed.emit(
        _review_payload_with_vibration(
            101,
            vibration_channel=1,
            run_id="run-101-other",
        )
    )
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    qapp.processEvents()

    assert window.current_display_channel_index() == 1
    assert window.analysis_toolbar.current_channel_index() == 1
```

- [ ] **Step 4: Run the stale-run guard test and record current behavior**

Run:

```powershell
python -m pytest tests/test_main_window.py::test_main_window_review_with_unmatched_run_id_does_not_preserve_stale_runtime_channel -q
```

Expected before implementation: likely PASS today. Keep it as guard coverage so
the fix does not over-preserve stale channels.

- [ ] **Step 5: Add chart-payload-missing-channel guard coverage**

Add a test proving a matching runtime identity does not preserve a channel that
is absent from the committed chart payload:

```python
def test_main_window_matching_runtime_channel_missing_from_chart_payload_falls_back(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-live")
    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 1)

    assert window.current_display_channel_index() == 1

    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            101,
            run_id="run-101-live",
            channels=(0,),
        )
    )
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    qapp.processEvents()

    assert window.current_display_channel_index() == 0
    assert window.analysis_toolbar.current_channel_index() == 0
    _assert_committed_chart_channel(window, channel_index=0)
```

- [ ] **Step 6: Run the chart-payload guard test and record current behavior**

Run:

```powershell
python -m pytest tests/test_main_window.py::test_main_window_matching_runtime_channel_missing_from_chart_payload_falls_back -q
```

Expected before implementation: likely PASS today. Keep it as guard coverage for
the chart-payload membership check.

- [ ] **Step 7: Add preserve-once guard coverage**

Add a test proving the run-bound preservation record is consumed after the first
matching review, so later same-run review payloads do not overwrite a user
selection made while already reviewing:

```python
def test_main_window_runtime_channel_preservation_is_consumed_after_first_review(
    qapp,
    monkeypatch,
):
    window, services = _make_window(
        monkeypatch,
        initial_state=_running_state(101),
    )
    services.config_service.load_config.return_value = {
        "config_id": 101,
        "selected_channels": [0, 1],
    }
    _show(window, qapp)

    _emit_runtime_started(services, config_id=101, run_id="run-101-live")
    services.bus.factory_state_changed.emit(_running_state(101))
    qapp.processEvents()
    _select_toolbar_channel(window, qapp, 0)

    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            101,
            run_id="run-101-live",
            channels=(0, 1),
        )
    )
    services.bus.factory_state_changed.emit(_reviewing_state(101))
    qapp.processEvents()

    assert window.current_display_channel_index() == 0

    _select_toolbar_channel(window, qapp, 1)
    assert window.current_display_channel_index() == 1

    services.bus.factory_review_changed.emit(
        _review_payload_with_chart_payload(
            101,
            run_id="run-101-live",
            channels=(0, 1),
        )
    )
    qapp.processEvents()

    assert window.current_display_channel_index() == 1
    assert window.analysis_toolbar.current_channel_index() == 1
    _assert_committed_chart_channel(window, channel_index=1)
```

- [ ] **Step 8: Run the preserve-once guard test and record current behavior**

Run:

```powershell
python -m pytest tests/test_main_window.py::test_main_window_runtime_channel_preservation_is_consumed_after_first_review -q
```

Expected before implementation: likely PASS today. Keep it as guard coverage so
the fix does not reapply run-bound preservation after the first matching review.

- [ ] **Step 9: Implement runtime identity tracking in `MainWindow`**

In `MainWindow.__init__`, add focused state:

```python
self._active_runtime_identity: tuple[int, str] | None = None
self._runtime_display_channel_by_identity: dict[tuple[int, str], int] = {}
self._handling_factory_review_changed = False
```

In `_connect_bus_signals()`, connect existing runtime lifecycle signals:

```python
if hasattr(self.services.bus, "factory_runtime_start_accepted"):
    self.services.bus.factory_runtime_start_accepted.connect(
        self._on_factory_runtime_context
    )
if hasattr(self.services.bus, "factory_runtime_analyzing_entered"):
    self.services.bus.factory_runtime_analyzing_entered.connect(
        self._on_factory_runtime_context
    )
```

Add helpers:

```python
def _runtime_identity_from_payload(self, payload) -> tuple[int, str] | None:
    config_id = getattr(payload, "config_id", None)
    run_id = getattr(payload, "run_id", None)
    if isinstance(config_id, bool) or not isinstance(config_id, int):
        return None
    if not isinstance(run_id, str) or not run_id:
        return None
    return config_id, run_id

def _on_factory_runtime_context(self, payload) -> None:
    identity = self._runtime_identity_from_payload(payload)
    if identity is None:
        return
    self._active_runtime_identity = identity
    self._record_runtime_display_channel_for_identity(identity)

def _record_runtime_display_channel_for_identity(
    self,
    identity: tuple[int, str] | None = None,
) -> None:
    if self._handling_factory_review_changed:
        return
    if identity is None:
        identity = self._active_runtime_identity
    if identity is None:
        return
    config_id, _run_id = identity
    if self._factory_state.displayed_config_id != config_id:
        return
    if self._factory_state.run_phase not in {"running", "analyzing"}:
        return
    channel = self.display_channel_by_config_id.get(config_id)
    if isinstance(channel, int) and not isinstance(channel, bool):
        self._runtime_display_channel_by_identity[identity] = channel
```

Add review preservation helpers. These helpers consume the run-bound record on
the first matching review identity so it cannot overwrite later review-time user
selections:

```python
def _review_runtime_identity(self, review: FactoryReviewPayload) -> tuple[int, str] | None:
    config_id = getattr(review, "displayed_config_id", None)
    run_id = getattr(review, "run_id", None)
    if isinstance(config_id, bool) or not isinstance(config_id, int):
        return None
    if not isinstance(run_id, str) or not run_id:
        return None
    return config_id, run_id

def _consume_preserved_runtime_display_channel_for_review(
    self,
    review: FactoryReviewPayload,
) -> int | None:
    if getattr(review, "trigger_source", None) == "import_audio":
        return None
    identity = self._review_runtime_identity(review)
    if identity is None:
        return None
    missing = object()
    channel = self._runtime_display_channel_by_identity.pop(identity, missing)
    if channel is missing:
        return None
    if not isinstance(channel, int) or isinstance(channel, bool):
        return None
    config_id, _run_id = identity
    if channel not in self._selected_channels_for_config(config_id):
        return None
    chart_payload = getattr(review, "chart_payload", None)
    if isinstance(chart_payload, dict) and channel not in self._normalize_channels(
        chart_payload.keys()
    ):
        return None
    return channel
```

Update `_on_factory_review_changed()` so it computes the preserved channel
before `_clear_display_channel_for_new_review_identity()` and restores it after
the clear. Wrap review handling in a guard so the real signal order cannot
re-record the just-consumed runtime identity while `_sync_analysis_toolbar()`
runs with a still-local `running`/`analyzing` state:

```python
preserved_channel = None
self._handling_factory_review_changed = True
try:
    if isinstance(review, FactoryReviewPayload):
        preserved_channel = self._consume_preserved_runtime_display_channel_for_review(review)
    ...
    self._clear_display_channel_for_new_review_identity(self._review_payload)
    if preserved_channel is not None and isinstance(self._review_payload.displayed_config_id, int):
        self.display_channel_by_config_id[self._review_payload.displayed_config_id] = preserved_channel
    self._sync_analysis_toolbar()
finally:
    self._handling_factory_review_changed = False
```

In `_sync_analysis_toolbar()` and `_sync_selected_display_channel()`, after a
valid channel is stored, call `_record_runtime_display_channel_for_identity()`.
This captures both automatic default resolution and user toolbar selection
during `running`/`analyzing`.

In `_on_factory_display_reset()`, clear runtime identity state:

```python
self._active_runtime_identity = None
self._runtime_display_channel_by_identity.clear()
```

- [ ] **Step 10: Run Task 1 focused tests**

Run:

```powershell
python -m pytest tests/test_main_window.py::test_main_window_runtime_review_keeps_acoustic_channel_when_vibration_channel_configured tests/test_main_window.py::test_main_window_review_with_unmatched_run_id_does_not_preserve_stale_runtime_channel tests/test_main_window.py::test_main_window_matching_runtime_channel_missing_from_chart_payload_falls_back tests/test_main_window.py::test_main_window_runtime_channel_preservation_is_consumed_after_first_review tests/test_main_window.py::test_analysis_defaults_to_vibration_channel_from_review_config tests/test_main_window.py::test_imported_review_defaults_to_vibration_channel_from_payload_config tests/test_stft_module/test_live_review_rendering.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit Task 1**

```powershell
git add app/main_window.py tests/test_main_window.py
git commit -m "fix: preserve runtime display channel on review"
```

## Task 2: Suppress Transient Busy Start Dialogs

**Files:**
- Modify: `ui/modules/control.py`
- Test: `tests/test_control_module.py`

- [ ] **Step 1: Add a failing control-module test for busy blocked-start reasons**

Add this test near the existing blocked-start modal tests:

```python
@pytest.mark.parametrize("reason", ["当前测试运行中", "启动请求处理中"])
def test_busy_blocked_start_reasons_do_not_open_modal(qapp, monkeypatch, reason):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason=reason, trigger_source="manual")
    )

    assert recorder.open_count == 0
    assert recorder.latest_text == ""
    assert module._blocked_start_dialog is None
```

- [ ] **Step 2: Add guard coverage for existing real failure dialog**

Add a test near the busy suppression test:

```python
@pytest.mark.parametrize("reason", ["当前测试运行中", "启动请求处理中"])
def test_busy_blocked_start_reason_does_not_replace_existing_failure_modal(
    qapp,
    monkeypatch,
    reason,
):
    module, services, recorder = _make_module_with_message_box_spy(monkeypatch)

    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason="配置加载失败", trigger_source="manual")
    )
    services.bus.factory_start_blocked.emit(
        FactoryStartBlockedPayload(reason=reason, trigger_source="manual")
    )

    assert recorder.open_count == 1
    assert recorder.latest_text == "配置加载失败"
    assert module._blocked_start_dialog is not None
```

- [ ] **Step 3: Run the new control tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_control_module.py::test_busy_blocked_start_reasons_do_not_open_modal tests/test_control_module.py::test_busy_blocked_start_reason_does_not_replace_existing_failure_modal -q
```

Expected: FAIL because the current handler opens the blocked-start modal for
every non-empty reason and updates an existing failure dialog with busy text.

- [ ] **Step 4: Implement busy-reason suppression**

In `ControlModule._on_factory_start_blocked()` after trimming `reason`, add:

```python
if reason in self._BUSY_START_READY_REASONS:
    return
```

Do not change `_on_factory_runtime_aborted()`.

- [ ] **Step 5: Run Task 2 focused tests**

Run:

```powershell
python -m pytest tests/test_control_module.py::test_busy_blocked_start_reasons_do_not_open_modal tests/test_control_module.py::test_busy_blocked_start_reason_does_not_replace_existing_failure_modal tests/test_control_module.py::test_blocked_start_signal_opens_and_updates_single_modal tests/test_control_module.py::test_runtime_abort_with_error_opens_start_failure_dialog -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add ui/modules/control.py tests/test_control_module.py
git commit -m "fix: suppress transient busy start dialogs"
```

## Final Verification

- [ ] **Step 1: Run combined focused regression**

```powershell
python -m pytest tests/test_main_window.py::test_main_window_runtime_review_keeps_acoustic_channel_when_vibration_channel_configured tests/test_main_window.py::test_main_window_review_with_unmatched_run_id_does_not_preserve_stale_runtime_channel tests/test_main_window.py::test_main_window_matching_runtime_channel_missing_from_chart_payload_falls_back tests/test_main_window.py::test_main_window_runtime_channel_preservation_is_consumed_after_first_review tests/test_main_window.py::test_analysis_defaults_to_vibration_channel_from_review_config tests/test_main_window.py::test_imported_review_defaults_to_vibration_channel_from_payload_config tests/test_control_module.py::test_busy_blocked_start_reasons_do_not_open_modal tests/test_control_module.py::test_busy_blocked_start_reason_does_not_replace_existing_failure_modal tests/test_control_module.py::test_blocked_start_signal_opens_and_updates_single_modal tests/test_control_module.py::test_runtime_abort_with_error_opens_start_failure_dialog tests/test_stft_module/test_live_review_rendering.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader related suites**

```powershell
python -m pytest tests/test_main_window.py tests/test_control_module.py tests/test_stft_module/test_live_review_rendering.py -q
```

Expected: PASS or report unrelated pre-existing failures with exact test names.
