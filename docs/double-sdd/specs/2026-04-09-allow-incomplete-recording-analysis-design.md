# Allow Incomplete Recording Analysis Design

## Context

The current automatic judgment flow requires the recorded buffer to fully cover the configured default analysis window. If the captured audio ends before `analysis_window_offset_ms + analysis_duration`, judgment fails with an "analysis window not fully captured" no-result path.

Manual stop currently does not branch on that boundary. `FactoryModeController.stop_active_run()` delegates to `TestManager.stop_active_run()` and then immediately resets factory review context with `active_run_stopped`, which clears the display state and returns the page to the armed/not-ready idle path.

The requested behavior changes manual stop in two ways:

- add a config option named `允许分析不完整录音`
- when the user stops after the recording cursor has reached the right edge of the default analysis window, optionally analyze the partial recording instead of treating the stop as a display reset

The user also requested that manual stop must no longer clear the current charts or result area. If stop does not transition into analysis, the factory page should stay visually frozen at the stop moment and the workorder phase text should show `已停止`.

## Goals

- Add a persisted config toggle `允许分析不完整录音`, default `False`.
- Keep existing config validity rules unchanged: the configured default analysis window must still fit within the configured record duration.
- On manual stop, if the toggle is enabled and captured audio has reached the default analysis-window right edge, analyze the partial recording with the same automatic analysis flow used today.
- On manual stop, if the toggle is disabled or the captured audio has not reached the default analysis-window right edge, do not analyze and do not clear the existing display; move the factory page into an idle display-preserving `stopped` phase.
- Open manual drag-selection and the `分析` button only when stop-triggered automatic analysis has actually produced review context, not for plain stopped state.

## Non-Goals

- No change to natural full-duration completion behavior.
- No change to the meaning of `analysis_window_offset_ms`, `analysis_duration`, or record-duration validation.
- No change to manual reanalysis behavior after a normal review payload already exists.
- No new support for stopping an in-flight manual reanalysis pass.
- No synthesized, padded, or extrapolated audio for uncaptured portions of the configured default analysis window.

## Approved Product Decisions

1. The config label is `允许分析不完整录音`.
2. Old configs and newly created configs both default this toggle to disabled.
3. The right-edge eligibility check is inclusive: reaching or exceeding the default analysis-window end time is sufficient.
4. If manual stop is not eligible for analysis, the workorder phase must display `已停止`.
5. Stop without analysis must preserve the current visible chart/result state instead of clearing the page.
6. Stop-triggered analysis keeps the original run `trigger_source`; the stop action is not modeled as a new trigger source.

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces:
  - config persistence and validation
  - automatic full-length recording analysis
  - manual stop error handling
  - manual reanalysis contract after a review payload exists
  - workorder start/stop lifecycle and selection readiness rules
- Allowed breakage:
  - manual stop no longer always returns the factory page to `armed`
  - a new controller phase `stopped` becomes observable in state-driven UI/tests
  - manual stop may now produce standard automatic analysis review payloads when the new config toggle is enabled and the default window is fully captured
- Migration strategy: add a new boolean config column with DB default `0` and treat missing legacy values as `False`

## Design

### Configuration Surface

Add a boolean field named `allow_incomplete_recording_analysis` to the test config model and database schema.

Required behavior:

- `TestConfigTable` stores the field as a non-null boolean with default `False`
- `DbConnectionService._migrate()` adds the column for existing databases
- `ConfigService.load_config()` includes the field in returned config payloads
- `ConfigService.save_config()` persists the field
- `ConfigService.validate()` rejects non-boolean values for the field
- `ConfigModule` renders a checkbox labeled `允许分析不完整录音` in the global-parameter section near the existing analysis-window controls

The new toggle changes only manual-stop handling. It does not relax current save-time or start-time validation of the configured default analysis window.

### Stop-Time Eligibility Rule

Manual stop eligibility is phase-bounded:

- if the controller/runtime is in `running`, evaluate the stop-triggered analysis rule below
- if the controller/runtime is already in `analyzing`, manual stop is a cancel-to-`stopped` path and does not start or restart analysis

While in `running`, manual stop is eligible for automatic analysis only when all of the following are true:

- `allow_incomplete_recording_analysis` is `True`
- the captured duration is known
- `captured_duration_sec >= analysis_window_offset_ms / 1000 + analysis_duration`
- the partial buffer was successfully saved to a WAV path usable by the existing judgment/AI pipeline

This rule is the code-level meaning of "录音的时间节点已经超过分析框最右侧". The boundary is inclusive so stopping exactly on the right edge is eligible.

Failure classification for manual stop:

- if post-attach buffer acquisition fails but orderly stop cleanup still succeeds, treat the stop as a normal non-eligible stop and fall back to `factory_runtime_stopped`
- if partial-buffer capture succeeds but captured duration is unknown, or the partial WAV save fails, treat the stop as a normal non-eligible stop and fall back to `factory_runtime_stopped`
- if recorder/monitor/runtime cleanup fails in a way that prevents orderly stop ownership release, treat it as a true runtime failure and keep using `factory_runtime_aborted`
- if the runtime is already in `analyzing`, stopping never attempts partial-analysis recovery; it only requests orderly stop and then transitions to `stopped` if cleanup succeeds

### Factory Stop State Model

Introduce a new controller/UI phase named `stopped`.

`stopped` semantics:

- the active recording/analyzing runtime has ended
- the display context is intentionally preserved
- no automatic review payload is implied
- the page is idle enough to allow the next start attempt, subject to normal selection/config readiness

`stopped` is not a blocked-start phase and not an active-runtime phase. It behaves like an idle phase for readiness, but unlike `armed` it preserves the stopped run's visible context until the next accepted run or a real selection change.

A real selection change means the selected-config tuple changes value, including add/remove/reorder operations in the multi-select UI. A background refresh that revalidates the same selected tuple does not clear stopped display context by itself.

### Runtime Signaling

Add a dedicated factory-mode signal and payload for manual stop without analysis:

- `FactoryRuntimeStoppedPayload`
- `EventBus.factory_runtime_stopped`

Payload fields should minimally include:

- `config_id`
- `run_id`
- `trigger_source`

This signal exists to distinguish a normal user stop from a runtime abort/error. Existing `factory_runtime_aborted` behavior remains reserved for true runtime failures such as recorder/monitor errors.

`trigger_source` in both `FactoryRuntimeStoppedPayload` and any stop-triggered automatic-analysis review payload continues to represent the original run origin (`manual`, `plc`, or `scan`).

### TestManager Stop Flow

`TestManager.stop_active_run()` should stop being a pure cleanup path. It becomes a branch point with phase-aware outcomes after capturing the partial post-attach buffer.

Shared steps:

- resolve the active session, recorder, and monitor
- capture the recorder-owned post-attach buffer slice
- copy it into `session._buffer`
- keep `session._sample_rate` in sync with recorder sample rate
- attempt to save the partial WAV exactly as current manual-stop behavior does
- stop/disconnect monitor and recorder safely

Outcome A: stop during `running` and eligible for stop-triggered analysis

- do not emit `factory_runtime_aborted`
- do not emit `factory_runtime_stopped`
- do not drop into controller reset behavior
- reuse the existing automatic analysis lifecycle on the partial buffer:
  - emit `factory_runtime_analyzing_entered`
  - run judgment/AI using the existing default-window path
  - emit the existing success/no-result/error factory review signal
  - emit existing cleanup-settled signaling as needed

This path should be behaviorally equivalent to today's natural recording completion, except that the captured buffer may be shorter than the configured full record duration.

Outcome B: stop during `running` but not eligible for stop-triggered analysis

- finish cleanup without running judgment
- emit `factory_runtime_stopped` after runtime ownership is fully released
- do not emit `factory_runtime_aborted`
- do not emit review-reset signals

This outcome includes the benign fallback cases where captured duration cannot be derived or the partial WAV could not be saved for judgment. Those conditions should be logged, but they should not be upgraded to aborts if orderly stop cleanup still succeeds.

Outcome C: stop during `analyzing`

- cancel any remaining auto-analysis completion path for that active run
- finish cleanup without starting a new judgment pass
- emit `factory_runtime_stopped` after runtime ownership is fully released
- do not emit `factory_runtime_aborted` unless orderly stop cleanup itself fails
- do not emit review-reset signals

Winner rule for stop-versus-completion races:

- once stop is accepted for an `analyzing` run, the stop outcome wins
- backend completion signals for that run should be suppressed after stop acceptance
- controller should still defensively ignore any late auto-analysis completion signal that arrives after `factory_runtime_stopped` has already been consumed for that run

### FactoryModeController Behavior

`FactoryModeController.stop_active_run()` should stop calling `_reset_review_context("active_run_stopped")` on successful manual stop. Instead, successful stop state transitions come from backend signals:

- stop-triggered analysis path continues to use the existing `analyzing -> reviewing` signal flow
- plain stopped path consumes the new `factory_runtime_stopped` signal

On `factory_runtime_stopped`, the controller should:

- verify ownership against the active runtime config/run id
- clear active runtime ownership
- preserve `displayed_config_id` for the stopped config
- preserve any current review payload rather than replacing it with an empty payload
- set `run_phase="stopped"`
- preserve `trigger_source`
- recompute selection/start readiness using current selection validity and backend-idle state
- emit `factory_state_changed`
- not emit `factory_display_reset`
- not emit an empty `factory_review_changed`

The controller should continue to treat `factory_runtime_aborted` as the error-reset path that clears review/display context.

If a user changes selected configs while in `stopped` and the selected tuple actually changes, the controller should clear the preserved stopped display context through the normal selection-change reset path before entering the next idle ready/not-ready state.

### UI Rendering Rules

#### Workorder / Phase Text

Map `stopped` to `当前阶段：已停止`.

Selection locking should remain disabled only for active/busy phases. `stopped` behaves like an idle phase, so normal selection changes are allowed.

#### Control Panel

No new visible status row is introduced. The existing stop button continues to call factory-mode stop. The start button should be enabled in `stopped` whenever normal start readiness is satisfied.

The stop button remains meaningful only for active runtime phases already supported by controller/backend stop (`running` and `analyzing`). Plain stopped state is not itself a stoppable phase.

#### Main Window Analysis Toolbar

`MainWindow._DISPLAY_CHANNEL_PHASES` should include `stopped` so the channel toolbar can remain visible for the preserved stopped display context.

#### STFT Module

`StftModule._DISPLAY_PHASES` should include `stopped` so the live STFT image from the just-stopped run remains visible.

`stopped` is not a review phase:

- the selection box stays hidden unless a real review payload exists and the controller is in `reviewing` or `reanalyzing`
- the `分析` button stays hidden/disabled in plain stopped state

If stop-triggered automatic analysis succeeds or no-results into review, the existing `reviewing` behavior remains the only way to expose manual drag-selection and the `分析` button.

#### Result Module

Manual stop without analysis must not clear the result card to its empty placeholder.

Required behavior:

- if a summary payload already exists, keep rendering it
- if no summary payload exists and the user stops from a live/in-progress state, keep the in-progress visual state instead of reverting to empty
- no `factory_display_reset` should be emitted for a normal user stop

This keeps the result area visually frozen at the stop moment, matching the user request.

#### Review-Only Chart Modules

Modules that only render committed review payloads (`SpectrumModule`, `SplModule`, `LoudnessModule`, `OctaveModule`) should not receive a stop-time reset. If they already have committed review payload content, it remains visible. If they were blank before stop because no committed review payload existed, they remain blank.

### Next-Run Reset Behavior

A later accepted run should continue to clear prior review/display context through the existing next-run acceptance path. The stopped display is intentionally sticky only until the next authoritative state transition replaces it.

The authoritative transitions that clear stopped display context are:

- a newly accepted run for any config
- a real selection-tuple change initiated through the workorder selection UI
- an explicit error/abort path

Selection refreshes that keep the same selected tuple should preserve stopped display context.

## Testing Strategy

Add focused coverage for the following:

- model / DB migration / config service persistence for `allow_incomplete_recording_analysis`
- config-module checkbox load/save wiring and default state
- config validation rejects non-boolean values for the new field
- manual stop below the default-window right edge emits `factory_runtime_stopped`, enters controller `stopped`, and does not clear display context
- manual stop with the toggle disabled emits `factory_runtime_stopped` even if the default-window right edge is already captured
- manual stop with the toggle enabled and the default-window right edge captured reuses the normal auto-analysis path and reaches `reviewing`
- manual stop during `analyzing` cancels into `stopped` instead of launching another analysis pass
- stop acceptance during `analyzing` wins over any late completion signal for the same run
- manual stop eligible-for-analysis path does not emit `factory_runtime_aborted`
- post-attach buffer acquisition failure falls back to normal `stopped` when cleanup succeeds
- partial WAV save failure falls back to normal `stopped` when cleanup succeeds
- cleanup failure during stop still uses abort/error handling
- controller `stopped` phase is start-ready when selection/runtime conditions allow it
- stop-triggered analysis preserves the original runtime `trigger_source`
- selection tuple changes from `stopped` clear preserved display context, while same-tuple refreshes do not
- workorder phase text renders `当前阶段：已停止`
- control-module start button remains enabled in `stopped`
- main-window analysis toolbar remains available in `stopped`
- STFT keeps rendering the stopped run while manual selection controls remain hidden in plain stopped state
- result module does not clear to empty on plain stopped transition
- existing runtime-abort tests continue to prove that true errors still reset the page

## Files Likely Affected

- `model/test_config_table.py`
- `app/service/db_connection_service.py`
- `app/service/config_service.py`
- `ui/modules/config.py`
- `app/services_container.py`
- `app/service/analysis/test_manager.py`
- `app/service/factory_mode_controller.py`
- `ui/modules/workorder.py`
- `ui/modules/control.py`
- `ui/modules/stft.py`
- `ui/modules/result.py`
- `app/main_window.py`
- focused tests under `tests/test_config_service.py`
- focused tests under `tests/test_config_module.py`
- focused tests under `tests/test_test_manager/`
- focused tests under `tests/test_factory_mode_controller/`
- focused tests under `tests/test_workorder_module.py`
- focused tests under `tests/test_control_module.py`
- focused tests under `tests/test_result_module.py`
- focused tests under `tests/test_stft_module/`
- focused tests under `tests/test_main_window.py`

## Verification

- review the new config field end-to-end through DB migration, load, edit, save, and reload
- verify stop below threshold leaves the page in `已停止` without clearing the current display
- verify stop at or beyond threshold with the toggle enabled enters the normal analyzing/reviewing path
- verify manual selection and the `分析` button are available only after a real review payload exists
