# Allow Incomplete Recording Analysis Design

## Context

The current automatic judgment flow requires the recorded buffer to fully cover the configured default analysis window. If the captured audio ends before `analysis_window_offset_ms / 1000 + analysis_duration`, judgment fails with an "analysis window not fully captured" no-result path.

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

Manual stop eligibility is phase-bounded, and backend runtime ownership is authoritative for the branch decision:

- `TestManager` decides the stop branch from its own runtime state for the active config, not from the controller's last rendered phase
- authoritative backend runtime model:
  - `running`: the config has an active owned session in `_sessions` and is not present in `_analyzing_sessions`
  - `analyzing`: the config has an active owned session in `_sessions` and is present in `_analyzing_sessions`
  - no active runtime: the config has no owned session in `_sessions`
- `FactoryModeController` mirrors whatever backend signal wins; it does not make an independent stop-branch decision
- if the backend runtime is still in `running`, evaluate the stop-triggered analysis rule below
- if the backend runtime is already in `analyzing`, manual stop is a cancel-to-`stopped` path and does not start or restart analysis

While in `running`, manual stop is eligible for automatic analysis only when all of the following are true:

- `allow_incomplete_recording_analysis` is `True`
- the captured duration derived from the exact partial buffer captured for stop handling is known
- `captured_duration_sec >= analysis_window_offset_ms / 1000 + analysis_duration`
- the partial buffer was successfully saved to the session WAV path used by the existing judgment/AI pipeline

This rule is the code-level meaning of "录音的时间节点已经超过分析框最右侧". The boundary is inclusive so stopping exactly on the right edge is eligible.

The successful partial-WAV save is not just bookkeeping. The stop-triggered path intentionally reuses the same session-backed analysis pipeline as natural completion, including any downstream logic that expects a valid persisted WAV path on the session. There is no separate in-memory-only fallback path for eligible manual stop analysis.

Failure classification for manual stop:

- if post-attach buffer acquisition fails but orderly stop cleanup still succeeds, treat the stop as a normal non-eligible stop and fall back to `factory_runtime_stopped`
- if partial-buffer capture succeeds but captured duration is unknown, or the partial WAV save fails, treat the stop as a normal non-eligible stop and fall back to `factory_runtime_stopped`
- if recorder/monitor/runtime cleanup fails in a way that prevents orderly stop ownership release, treat it as a true runtime failure and keep using `factory_runtime_aborted`
- if the runtime is already in `analyzing`, stopping skips partial-buffer capture and partial-WAV persistence entirely; it only requests orderly stop and then transitions to `stopped` if cleanup succeeds

### Factory Stop State Model

Introduce a new controller/UI phase named `stopped`.

`stopped` semantics:

- the active recording/analyzing runtime has ended
- the display context is intentionally preserved
- no automatic review payload is implied
- the page is idle enough to allow the next start attempt, subject to normal selection/config readiness
- plain `stopped` outcomes do not promote the stopped session into a backend-completed/reanalyzable session record

`stopped` is not a blocked-start phase and not an active-runtime phase. It behaves like an idle phase for readiness, but unlike `armed` it preserves the stopped run's visible context until the next accepted run or a real selection change.

Shared readiness contract:

- `FactoryControllerState.run_phase="stopped"` must be treated like an idle/start-eligible phase for `start_ready` recomputation
- `FactoryControllerState.start_ready` and `start_ready_reason` for `stopped` are derived by the same readiness rules used for other non-busy idle phases
- consumers must not infer that `stopped` is blocked merely because it is a new observable phase

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

`TestManager.stop_active_run()` should stop being a pure cleanup path. It becomes a branch point with phase-aware outcomes, and only the `running` branch performs partial-buffer capture.

Shared stop-entry steps:

- resolve the active session, recorder, and monitor
- mark the active config as an accepted factory stop before branching so later callbacks can detect the stop winner
- disconnect only the stopping session's recorder/monitor callbacks before cleanup so this run stops receiving live-stream updates immediately

Accepted-stop tracking contract:

- accepted-stop tracking is keyed by the active factory runtime slot (`config_id`), because factory mode owns at most one active run per config at a time
- runtime `run_id` remains the public signal identity used by controller-side ownership checks, while backend late-output suppression continues to rely on active-session ownership (the old session object is no longer the owned session once the stop path releases or replaces it)
- the config-keyed accepted-stop marker is created when stop is accepted and must be cleared when the stop path settles into one of its terminal outcomes:
  - immediately after `factory_runtime_stopped`
  - immediately after `factory_runtime_aborted`
  - for Outcome A, only after the stop-triggered analysis has emitted its review/no-result/error result and any `factory_runtime_auto_analysis_cleanup_settled` signal for that run
- a later same-config start is allowed only after that terminal clearing point, so a stale accepted-stop marker must not leak into the next run

Shared-recorder contract:

- a stop request operates on the active config/session, not on the recorder device globally
- if the recorder is shared by multiple live configs, disconnect the stopping config from that recorder but do not stop or discard the recorder while another live owner still exists
- stop/discard the recorder only when the stopping config was the last live owner
- the same ownership rule applies on cleanup failure: preserve the shared recorder for the surviving owner, but discard it when no live owner remains
- this is a backend resource-management contract, not a new multi-active-run factory-controller state model; `FactoryModeController` still derives `run_phase` / `start_ready` only from its own factory-owned runtime slot, while surviving non-controller owners remain outside that single-config controller state

Return contract for `TestManager.stop_active_run()`:

- return `True` when a stop request is accepted and enters Outcome A, B, or C, including benign fallback cases that end in plain `factory_runtime_stopped`
- return `False` when there is no active owned session for the requested config or when stop cleanup fails and no successful stop outcome is accepted
- raising is reserved for unexpected call-site/runtime failures; controller-level handling should treat it the same as a failed stop request

Additional steps for `running` stops only:

- capture the recorder-owned post-attach buffer slice
- copy it into `session._buffer`
- keep `session._sample_rate` in sync with recorder sample rate
- derive `captured_duration_sec` from that captured slice / synced sample rate pair, so the eligibility check and the eventual stop-triggered analysis operate on the same audio extent
- attempt to save the partial WAV exactly as current manual-stop behavior does
- stop/disconnect monitor and recorder safely

Additional steps for `analyzing` stops only:

- do not capture another partial buffer and do not write another partial WAV
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

This path should be behaviorally equivalent to today's natural recording completion, except that the captured buffer may be shorter than the configured full record duration. Although stop acceptance has already been recorded for race control, Outcome A is intentionally allowed to emit the normal completion/review signals because this accepted-stop branch chose analysis rather than cancellation.

Outcome B: stop during `running` but not eligible for stop-triggered analysis

- finish cleanup without running judgment
- emit `factory_runtime_stopped` after runtime ownership is fully released
- do not emit `factory_runtime_aborted`
- do not emit review-reset signals

This outcome includes the benign fallback cases where captured duration cannot be derived or the partial WAV could not be saved for judgment. Those conditions should be logged, but they should not be upgraded to aborts if orderly stop cleanup still succeeds.

Outcome C: stop during `analyzing`

- do not try to interrupt `session._run_judgment()` or other in-flight analysis work synchronously
- finish cleanup without starting a new judgment pass
- emit `factory_runtime_stopped` after runtime ownership is fully released
- do not emit `factory_runtime_aborted` unless orderly stop cleanup itself fails
- do not emit review-reset signals
- allow any already-running analysis worker to finish in the background, but suppress all late completion output for that run after stop acceptance
- treat the stop boundary as a `TestManager` ownership boundary: release `_sessions`, `_recorders`, and `_monitors` immediately, but do not wait for or roll back any work already executing inside the old session object
- after this stop boundary, the old session must not be reattached as the active runtime, must not be promoted into `_completed_sessions`, and must not make plain `stopped` reanalyzable

Winner rule for stop-versus-completion races:

- once stop is accepted, the backend records that fact before branching so the recording-done path can see it
- if stop is accepted while the runtime is still `running`, later `_on_recording_done()` / `_on_session_done()` work for that config must short-circuit instead of starting the natural completion flow
- if natural completion has already moved the runtime into backend `analyzing` before stop is accepted, the stop request follows Outcome C and the stop result wins by suppressing any later completion/review emission for that run
- after stop acceptance on the cancel-to-`stopped` paths, both factory-mode review signals and the generic completion emissions for that run (`test_finished`, `result_changed`, and the terminal idle/status update) must be suppressed
- this suppression rule does not apply to Outcome A stop-triggered analysis, which is the intentional stop-accepted branch that still emits the standard automatic-analysis outputs
- once a cancel-to-`stopped` path releases the old session from `TestManager` ownership, suppression for late outputs continues through active-session identity mismatch even if the config-keyed accepted-stop marker has already been cleared for a later run
- controller should still defensively ignore any late auto-analysis completion signal that arrives after `factory_runtime_stopped` has already been consumed for that run

### FactoryModeController Behavior

`FactoryModeController.stop_active_run()` should stop calling `_reset_review_context("active_run_stopped")` on successful manual stop. Instead, successful stop state transitions come from backend signals:

- pending-start cancellation is the one controller-local exception: if no backend runtime has been accepted yet, `stop_active_run()` may cancel the pending start request immediately and return the existing cancel outcome without waiting for backend stop signals
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
- not emit a synthetic result/reset payload for the result card; modules preserve their current rendered non-summary state by receiving the `stopped` phase transition without any accompanying reset signal

The controller should continue to treat `factory_runtime_aborted` as the error-reset path that clears review/display context.

If the stop request itself fails before any backend success signal is emitted, the controller should not invent a local success or reset transition:

- if `TestManager.stop_active_run()` returns `False` or raises before a backend runtime signal is emitted, `FactoryModeController.stop_active_run()` returns `False`
- the controller leaves the current state/review/display context unchanged
- any later backend abort signal still follows the normal `factory_runtime_aborted` path
- this preserves the existing manual-stop failure surface: no silent reset, no fake `stopped`, and the operator may retry or wait for the backend's real terminal signal

If a user changes selected configs while in `stopped` and the selected tuple actually changes, the controller should clear the preserved stopped display context through the normal selection-change reset path before entering the next idle ready/not-ready state.

### UI Rendering Rules

#### Workorder / Phase Text

Map `stopped` to `当前阶段：已停止`.

Selection locking should remain disabled only for active/busy phases. `stopped` behaves like an idle phase, so normal selection changes are allowed.

#### Control Panel

No new visible status row is introduced. The existing stop button continues to call factory-mode stop. The start button should be enabled in `stopped` whenever normal start readiness is satisfied.

The stop button remains meaningful for the existing pending-start cancel path and for active runtime phases already supported by controller/backend stop (`running` and `analyzing`). Plain stopped state is not itself a stoppable phase.

- during pending start, stop keeps its current meaning: cancel the pending start request
- during `running` / `analyzing`, stop requests backend stop
- during `reanalyzing`, stop remains unsupported and should be disabled; `FactoryModeController.stop_active_run()` returns `False` without delegating to backend reanalysis work
- during plain idle / `stopped` / review-only phases, stop should be disabled

#### Main Window Analysis Toolbar

`MainWindow._DISPLAY_CHANNEL_PHASES` should include `stopped` so the channel toolbar can remain visible for the preserved stopped display context.

#### STFT Module

`StftModule._DISPLAY_PHASES` should include `stopped` so the live STFT image from the just-stopped run remains visible.

Stop acceptance must freeze the preserved live display at the last frame already received. `TestManager` should disconnect the stopped session's recorder/monitor callbacks before stop cleanup completes so late recorder-driven updates (`stft_updated`, SPL/FFT/octave frame emissions, and `recording_progress`) are no longer forwarded for that stopped run.

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
- no new controller field or synthetic result payload is introduced for this frozen in-progress state; the module preserves the already-rendered non-summary result view because stop does not emit a reset

This keeps the result area visually frozen at the stop moment, matching the user request.

#### Review-Only Chart Modules

Modules that only render committed review payloads (`SpectrumModule`, `SplModule`, `LoudnessModule`, `OctaveModule`) should not receive a stop-time reset.

Render contract:

- these modules render committed chart content only when a committed review payload exists
- `stopped` extends the allowed display phase for already-committed review content; it does not synthesize any new chart payload
- if they already have committed review payload content, it remains visible in `stopped`
- if they were blank before stop because no committed review payload existed, they remain blank in `stopped`

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
- stop acceptance freezes the preserved live display by disconnecting further recorder-driven frame/progress updates for the stopped run
- manual stop eligible-for-analysis path does not emit `factory_runtime_aborted`
- post-attach buffer acquisition failure falls back to normal `stopped` when cleanup succeeds
- partial WAV save failure falls back to normal `stopped` when cleanup succeeds
- cleanup failure during stop still uses abort/error handling
- stopping one config that shares a recorder with another live config does not stop/discard the shared recorder prematurely
- controller `stopped` phase is start-ready when selection/runtime conditions allow it
- pending-start stop continues to cancel the pending start request instead of using the new plain-stopped flow
- stop-triggered analysis preserves the original runtime `trigger_source`
- selection tuple changes from `stopped` clear preserved display context, while same-tuple refreshes do not
- workorder phase text renders `当前阶段：已停止`
- control-module start button remains enabled in `stopped`
- main-window analysis toolbar remains available in `stopped`
- STFT keeps rendering the stopped run while manual selection controls remain hidden in plain stopped state
- result module does not clear to empty on plain stopped transition
- review-only chart modules keep committed content visible in `stopped` and remain blank when no committed review payload exists
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
- `ui/modules/spectrum.py`
- `ui/modules/spl.py`
- `ui/modules/loudness.py`
- `ui/modules/octave.py`
- `app/main_window.py`
- focused tests under `tests/test_config_service.py`
- focused tests under `tests/test_config_module.py`
- focused tests under `tests/test_test_manager/`
- focused tests under `tests/test_factory_mode_controller/`
- focused tests under `tests/test_workorder_module.py`
- focused tests under `tests/test_control_module.py`
- focused tests under `tests/test_result_module.py`
- focused tests under `tests/test_stft_module/`
- focused tests under `tests/test_analysis_chart_modules/`
- focused tests under `tests/test_main_window.py`

## Verification

- review the new config field end-to-end through DB migration, load, edit, save, and reload
- verify stop below threshold leaves the page in `已停止` without clearing the current display
- verify stop at or beyond threshold with the toggle enabled enters the normal analyzing/reviewing path
- verify manual selection and the `分析` button are available only after a real review payload exists
