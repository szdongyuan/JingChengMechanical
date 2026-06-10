# Runtime Review STFT And Start Dialog Design

## Request

Fix two runtime-test UI bugs:

- After a factory test completes, the final time-frequency chart can become
  blank and the visible result area flickers.
- On a later automatic start, several transient dialogs can flash before the run
  continues.

The user's clarification is that "the second click" means a second recording
and analysis cycle, not a double-click.

## Current Context

The current test page is built in `app/main_window.py` as a hard-coded grid.
`MainWindow` owns the shared display-channel toolbar and the card-mode switch
between acoustic titles and vibration titles. There is no separate runtime
dashboard host in the current checkout.

`MainWindow._connect_bus_signals()` connects the main-window handlers before
the chart modules are constructed. Therefore, when `factory_review_changed` is
emitted, `MainWindow._on_factory_review_changed()` resolves the current display
channel before `StftModule._on_factory_review_changed()` renders review data.
`MainWindow` does not currently track which display channel belongs to a
specific runtime `run_id`.

The factory controller applies review payloads by emitting
`factory_review_changed` before `factory_state_changed`, after it has already
updated its internal state to `reviewing`. The main window may therefore still
have a stale local `running` or `analyzing` state when the review signal handler
runs. The fix must not depend on the main-window local state already being
`reviewing` during `factory_review_changed`.

For normal hardware runs, `StftModule` keeps the final time-frequency image from
live in-memory STFT cache. Ordinary hardware review payloads do not include a
full STFT heatmap matrix. The STFT module already preserves live cached frames
when the review payload has no full STFT data, as long as the selected display
channel still resolves to a cached channel.

The observed blank/flicker path in the current code is:

- During `running`, the toolbar resolves a default channel, usually the first
  selected input channel.
- When the review payload for the completed run arrives,
  `_clear_display_channel_for_new_review_identity()` can remove that stored
  display channel because the review identity is new.
- `_sync_analysis_toolbar()` then calls `_resolve_current_display_channel()`.
  In committed-review phases that resolver currently prefers the configured
  `vibration_view_channel_index` when it is available.
- For a two-channel config where channel 2 is configured as vibration, this can
  switch the UI from the acoustic channel used during live STFT rendering to the
  vibration channel at completion time.
- If the STFT cache for the vibration channel is absent, `StftModule` clears the
  heatmap because the selected channel has no cached STFT image. The title/stack
  switch to vibration mode is the visible flicker.

The start button flow already has disabled/busy state handling. The transient
busy reasons `当前测试运行中` and `启动请求处理中` can still reach the blocked-start
modal path, producing a flashing dialog during normal run transition/cleanup.
Those reasons are state feedback, not user-actionable failures.

## Goals

- Preserve the display channel selected during a normal running/analyzing cycle
  when the committed review for that same run arrives.
- Keep the final STFT image visible by keeping STFT on the same cached channel
  across the running-to-review transition.
- Avoid acoustic/vibration mode flicker caused by an automatic completion-time
  channel switch.
- Keep startup/direct-review behavior unchanged when there is no prior runtime
  display channel to preserve.
- Keep imported-audio review behavior unchanged.
- Suppress blocked-start modal dialogs for transient busy reasons.
- Preserve modal dialogs for real blocked-start or runtime-abort failures.
- Keep the fix scoped to `MainWindow` display-channel resolution and
  `ControlModule` blocked-start dialog behavior.

## Non-Goals

- Do not add full STFT heatmap persistence to ordinary runtime review payloads.
- Do not change hardware modules, hardware configuration, PLC code, or database
  schema.
- Do not change the result dashboard layout editor or saved layout format.
- Do not redesign chart rendering, STFT scoring, FFT, octave, SPL, loudness, or
  vibration analysis logic.
- Do not remove the existing ability for a direct review/startup review with a
  vibration config to default to the vibration channel.
- Do not change how a real start failure is reported to the operator.

## Design

### Runtime Review Display Channel Preservation

`MainWindow` should track a run-bound display channel during ordinary hardware
runtime. Add main-window state for the active runtime identity and the display
channel selected for that identity:

- Runtime identity: `(config_id, run_id)`.
- Record/update the active runtime identity from
  `factory_runtime_start_accepted` and `factory_runtime_analyzing_entered` when
  both fields are valid.
- Record/update the runtime display channel for the active identity whenever
  `_sync_analysis_toolbar()` or `_sync_selected_display_channel()` resolves a
  channel while the displayed config matches the active runtime config and the
  phase is `running` or `analyzing`.
- Clear run-bound display-channel records on explicit display reset events such
  as next-run acceptance or selection change.

`MainWindow._on_factory_review_changed()` should preserve the run-bound channel
only for the ordinary hardware review that belongs to that same runtime
identity.

The preserved channel is valid when:

- the incoming payload is a `FactoryReviewPayload`;
- `trigger_source` is not `import_audio`;
- `displayed_config_id` is an integer;
- `run_id` is a non-empty string;
- `(displayed_config_id, run_id)` has a recorded run-bound display channel from
  the prior `running`/`analyzing` phase;
- the run-bound channel is still in the displayed config's `selected_channels`;
- if the review payload has a committed `chart_payload`, the run-bound channel
  is present in that chart payload.

When those conditions are true:

- Do not remove the stored display channel only because the review identity has
  changed from the runtime state into the review state.
- Restore `display_channel_by_config_id[config_id]` to the run-bound channel
  before `_sync_analysis_toolbar()` resolves the review channel.
- Allow `_sync_analysis_toolbar()` to keep the current channel and emit the
  normal `factory_display_channel_changed` signal for that same channel.
- Allow already-connected chart modules to render the review payload using the
  preserved channel.
- Consume the run-bound preservation record after the first matching review
  signal for that `(config_id, run_id)`, whether the channel was valid or not.
  This prevents a later same-run review/reanalysis payload from overwriting a
  channel the user selected after review became visible.

When the runtime identity is missing, unmatched, stale, or the run-bound channel
is invalid:

- Keep existing fallback behavior, including choosing the first usable committed
  chart channel and defaulting to the vibration channel where current tests
  require that behavior.

For imported-audio reviews:

- Keep the current imported-audio channel behavior because imported-audio review
  payloads can carry full imported STFT data and custom display labels.

For explicit display resets such as next-run acceptance or selection changes:

- Keep the existing reset behavior that clears run-specific cards and display
  channel state before a new run starts.
- Also clear run-bound display-channel records so same-config later reviews
  cannot accidentally reuse a stale runtime channel.

### STFT Rendering Contract

`StftModule` should not need a broad rewrite. The intended contract remains:

- It may render from live cache for ordinary hardware runs.
- It may seed cache from full STFT chart payload only for imported-audio review.
- It may clear when the selected display channel genuinely has no cache.

The main-window fix should prevent the accidental channel switch that makes a
valid live cache unreachable at completion time.

### Blocked Start Dialog Policy

`ControlModule._on_factory_start_blocked()` should classify busy reasons as
non-modal:

- `当前测试运行中`
- `启动请求处理中`

When these reasons arrive, the control module should keep the start-state logic
and button disablement behavior, but it should not open or update the blocked
start message box. Existing blocked-start dialogs caused by real failures should
not be replaced by a busy-state dialog.

All other blocked-start reasons, plus runtime abort payloads, continue to use
the existing dialog behavior.

## Edge Cases

- Running channel 0, configured vibration channel 1, and review payload channels
  `{0, 1}`: completion should keep channel 0 selected and preserve the acoustic
  STFT image.
- A user-selected channel should be kept across completion if it is still valid
  and present in the committed chart payload.
- A stored channel that is not present in the committed chart payload should
  fall back to the existing committed-review channel selection behavior.
- Same config, different `run_id`: a stale channel from an earlier run must not
  be preserved unless that exact `(config_id, run_id)` has a current run-bound
  display-channel record.
- Same config, same `run_id`: after the first matching review consumes the
  runtime preservation record, a later same-run review must not overwrite a
  channel the user selected while already in review.
- Real controller order: `factory_review_changed` may be handled before the
  main window receives the corresponding `factory_state_changed(reviewing)`, so
  the runtime-channel preservation and chart-payload membership checks must work
  with that signal order.
- Startup directly in review with no prior stored runtime channel should still
  use the existing review default behavior, including the current vibration
  default covered by existing tests.
- Imported-audio review should still use imported chart-payload channels and
  labels.
- A real invalid configuration, missing device, or failed start still shows a
  modal operator-facing error.
- A stale or queued busy blocked-start signal should not flash a modal during a
  second run cycle.

## Compatibility / Migration

- Backward compatibility: required.
- Protected surfaces: database schema, saved config layout JSON, hardware and
  PLC behavior, imported-audio review behavior, startup/direct-review vibration
  defaults, real start-failure dialogs, chart module signal contracts.
- Allowed breakage: none intended.
- Migration strategy: none.

## Testing

Add focused tests that fail on the current behavior and pass with the fix:

- `tests/test_main_window.py`
  - A normal hardware run with selected channels `[0, 1]` and
    `vibration_view_channel_index = 1` keeps channel `0` selected when the
    committed review arrives after live STFT frames on channel `0`.
  - The same test asserts the STFT render cache key remains on channel `0`, the
    image payload remains present, and the card title stays acoustic instead of
    switching to vibration mode.
  - A same-config review with a different `run_id` and no matching run-bound
    channel record does not preserve stale runtime channel state.
  - After the first matching review has preserved the runtime channel, a user
    channel selection made in review is not overwritten by a later same-run
    review signal.
  - At least one regression follows the real controller signal order:
    `factory_review_changed` first, then `factory_state_changed(reviewing)`.
  - Existing direct-review/imported-audio vibration-default tests continue to
    pass.

- `tests/test_control_module.py`
  - Busy blocked-start reasons do not open a message box.
  - Existing real blocked-start reasons still open/update the message box.

Run the focused main-window/control/STFT tests after implementation. Run broader
related tests if the focused changes touch shared module wiring.
