# Loudness Performance And Chart Scaling Design

## Summary

The initial loudness integration is functionally correct enough to expose the feature, but it is not operationally acceptable on the factory review path.

Two operator-visible defects must now be corrected:

- automatic post-analysis and manual STFT reanalysis become very slow after loudness was added
- the loudness chart often renders with a vertical range anchored near `0..60`, which flattens the actual trace

This pass keeps loudness as an ISO 532 judged metric with the existing per-channel unit and limit contract, but narrows the implementation to a cheaper analysis backend and a data-focused chart-range policy.

## Problem

### Loudness Backend Cost Is Dominating Review Latency

Root-cause measurements on the current code path show:

- `app/service/analysis/dsp.compute_loudness_trace()` takes about `8 s` for one `3 s` mono signal on this machine
- `app/service/analysis/judgment.py` calls that path once per selected channel for scalar pass/fail
- `app/service/analysis/review_payloads.py` calls it again per selected channel for the committed loudness chart
- the duplicate call pattern worsens total latency, but the underlying single-call cost is already large enough to explain the operator-visible stall

The current implementation uses MoSQITo `loudness_zwtv()`, the fully time-varying ISO 532-1 path. That route is too expensive for this UI workflow.

### Loudness Chart Y Range Is Being Distorted By Default Limits

The loudness chart reuses `LineChartWidget` with visible horizontal limit lines.

Because the default loudness limits are `0..60`, the plotted view often expands to include those limits even when the actual trace occupies a much narrower band. The result is a chart whose left axis starts near zero and makes the trace look artificially flat.

This is not a data problem. It is a rendering-range policy problem.

## Scope

In scope:

- loudness backend selection and adapter behavior in `app/service/analysis/dsp.py`
- loudness scalar and committed-chart generation behavior that depends on that adapter
- loudness chart Y-range behavior in the review UI
- regression tests for backend choice, payload shape, and chart scaling

Out of scope:

- database schema or config-surface changes
- loudness unit or limit semantics
- STFT, FFT, octave, SPL, AI, or layout behavior unrelated to the loudness regression
- new caching or background-job infrastructure for loudness reuse

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - loudness remains an ISO 532 metric sourced through MoSQITo
  - per-channel `sone` / `phon` config, display, and pass/fail semantics remain unchanged
  - loudness remains a committed post-analysis chart and a judged item that affects pass/fail
- Allowed breakage:
  - the exact loudness time-series and scalar values may change from the prior experimental `loudness_zwtv()` implementation because the backend algorithm changes
  - the loudness chart no longer guarantees that far-away default limit lines remain inside the visible Y range
- Migration strategy: none

## Approved Decisions

1. Loudness will continue to use MoSQITo and remain within the ISO 532 family.
2. The current `loudness_zwtv()` path is replaced by a segmented ISO 532 MoSQITo path because it is operationally too slow for this workflow.
3. This pass does not add a new loudness cache or a new chart-generation reuse layer because the measured root cause is the backend cost, not just duplicate invocation.
4. The loudness adapter will use `mosqito.sq_metrics.loudness_zwst_perseg()` with a fixed internal segment contract rather than the current fully time-varying `loudness_zwtv()` path.
5. The adapter-owned output contract remains the same: time axis, canonical `sone` trace, and canonical `sone` maximum.
6. `LineChartWidget` keeps its current default behavior for existing consumers such as SPL.
7. The loudness chart gets a dedicated opt-in Y-range mode exposed as an optional `set_series(..., y_axis_mode="data_focus")` keyword rather than a constructor-wide behavior change.
8. `set_series(...)` accepts only `y_axis_mode="auto"` and `y_axis_mode="data_focus"`; any other mode raises `ValueError`.
9. `LineChartWidget` initializes with stored `y_axis_mode="auto"`.
10. `LineChartWidget` stores the last requested `y_axis_mode` on the widget instance and updates that stored mode on every `set_series(...)` call.
11. A later `set_series(..., y_axis_mode="auto")` call must clear any prior data-focused Y override and restore the existing default Y autorange behavior for subsequent renders on that widget instance.
12. `series_payload()` remains backward-compatible and does not include `y_axis_mode`; the existing payload shape stays unchanged.
13. In the opt-in mode, the visible Y range must be derived from the plotted series, not from far-away limit lines.
14. `LineChartWidget.set_series(..., y_axis_mode="data_focus")` rejects non-finite plotted values rather than attempting to apply `data_focus` math to `NaN` or `inf`; `y_axis_mode="auto"` keeps the current broader numeric acceptance behavior unchanged.
15. The segmented loudness adapter uses a fixed `nperseg=2048` and `noverlap=1024` contract in this pass.
16. That fixed segment contract must retain about `139` chart samples for a `3 s` / `48 kHz` analysis window, which is sufficient chart density for the current review UI.
17. If the normalized loudness input is shorter than `2048` samples, the adapter must right-pad with zeros to `2048` before the backend call and then clamp any returned time coordinates to the actual pre-pad signal duration.
18. If the MoSQITo backend returns a scalar loudness value for the one-segment case, the adapter must normalize it to a one-point loudness trace before length and finite-value validation.
19. Existing loudness chart-authority warnings in `TestManager` remain part of the contract and must continue to be validated after the backend change.
20. Loudness limit lines may still be rendered, but they must not force the loudness chart to appear anchored near zero when the data lives elsewhere.
21. Verification for this pass includes reproducible warm-start timing probes: after one untimed warmup loudness call has loaded the backend, seeded `3 s` / `2-channel` NumPy buffers at both `48 kHz` and `44.1 kHz` and four sequential loudness adapter calls (judgment plus chart recompute for both channels) must stay below `2.0 s` in the same local Python environment used to run the targeted tests.

## Root Cause Summary

### Backend Comparison

Measured on the same `3 s`, `48 kHz` mono signal:

- `loudness_zwtv()` took about `8.2 s`
- `loudness_zwst_perseg()` took about `0.15 s` to `0.20 s` depending on segment size

That means the dominant operator-visible regression comes from the chosen backend path itself.

### Duplicate Invocation Is Real But Secondary

The current review flow computes loudness twice per selected channel:

- once for scalar judgment
- once for committed chart payload construction

That duplication increases total latency, but even a single `loudness_zwtv()` call is already far too slow for the intended workflow. Reuse alone is therefore not the primary fix.

### Chart Scaling Failure Mode

The loudness payload commonly carries default limits `0.0` and `60.0`.

Those limits are valid business constraints, but they are not necessarily useful view bounds. When the chart view includes them unconditionally, the visible loudness trace becomes compressed even though the underlying data is correct.

## Design Summary

Keep the existing loudness feature contract but replace the expensive backend and tighten the loudness chart range policy.

For analysis:

- move the loudness adapter to MoSQITo `loudness_zwst_perseg()`
- keep the adapter return shape unchanged for callers
- let the adapter own any backend-specific sample-rate accommodation and segment configuration

For rendering:

- extend `LineChartWidget` with an explicit Y-range policy based on plotted data
- keep the existing default mode untouched for SPL and any other current `LineChartWidget` consumers
- let `LoudnessModule` opt into the data-focused policy by passing an explicit `set_series(..., y_axis_mode=\"data_focus\")` style option
- do not broaden the change to unrelated chart modules unless a local regression forces it

This keeps the fix narrow, removes the dominant latency source, and preserves the feature surfaces the operator already uses.

## Loudness Adapter Contract

### Backend Selection

`compute_loudness_trace()` in `app/service/analysis/dsp.py` must switch from:

- MoSQITo `loudness_zwtv()`

to:

- MoSQITo `loudness_zwst_perseg()`

The adapter remains the only loudness backend entry point used by judgment and review-payload code.

### Segment Policy

The adapter uses a fixed internal segment length and overlap. The contract is:

- segment length is fixed to `nperseg=2048` in this pass and is not operator-configurable
- overlap is fixed to `noverlap=1024` in this pass
- the chosen segment policy must preserve a visually useful loudness-over-time chart while keeping the large performance win

Why `nperseg=2048` / `noverlap=1024` is fixed here:

- with `50%` overlap it yields about `139` samples over a `3 s` / `48 kHz` window, which is sufficient density for the current committed review chart
- local investigation showed that this path remains in the same sub-second performance tier as the other tested segmented options while avoiding the current `~8 s` single-call cost of `loudness_zwtv()`
- this fix does not introduce a user-facing tuning surface; structural regression tests validate the backend contract and a manual timing probe validates the operational target

### Short-Window Handling

`mosqito==1.2.1` does not safely handle all shorter-than-`nperseg` inputs through `loudness_zwst_perseg(..., nperseg=2048)`.

Required behavior:

- after sample-rate normalization, if the signal length is below `2048` samples, the adapter pads the tail with zeros up to `2048`
- the adapter records the actual pre-pad duration
- after the backend call, the adapter clamps returned `time_axis` values to that actual duration so the loudness payload never claims time beyond the selected analysis window
- if the backend returns a scalar loudness value for the one-segment case, the adapter converts it into a one-element loudness vector before validating output shape
- if the normalized input length is exactly `2048` samples, the backend's one-point time/value output is accepted as a valid loudness trace
- the adapter contract therefore explicitly allows a non-empty one-point trace for short or single-segment analysis windows, while preserving the existing list/list/float return shape

This preserves loudness availability for short manual reanalysis windows without reopening the expensive `loudness_zwtv()` path.

### Sample-Rate Handling

The adapter keeps ownership of sample-rate normalization to an exact `48 kHz` backend input.

Required behavior:

- if the calibrated input signal is already `48 kHz`, pass it through unchanged
- if the calibrated input signal is not `48 kHz`, resample it to `48 kHz` inside the app adapter before the MoSQITo call
- the app-owned normalization suppresses MoSQITo's internal warning-print path for `fs < 48 kHz`
- the backend should always receive `48000` as its `fs` argument after adapter normalization

This intentionally preserves the current adapter-owned rate-control boundary while changing only the loudness algorithm behind it.

### Output Contract

The adapter must still return:

- loudness times in seconds
- loudness values in canonical `sone`
- maximum loudness in canonical `sone`

Existing consumers in judgment and review payloads should not need a contract change.

## Judgment And Review-Payload Contract

Required behavior:

- `JudgmentEngine` still uses `compute_loudness_trace()` for the authoritative loudness scalar
- `review_payloads.py` still uses `compute_loudness_trace()` for the committed chart payload
- existing `sone` / `phon` conversion helpers continue to own all unit conversion outside the backend
- no new serialization or config fields are added in this pass
- backend exceptions still surface through the existing loudness failure contract:
  - `loudness_available=False`
  - `loudness_error_message` populated from the backend failure
  - summary payload remains partial-no-result with the existing loudness no-result behavior
- if loudness chart recomputation later fails, the existing chart-omission and serialized-authority behavior remains unchanged

Allowed behavior:

- the loudness scalar and chart trace values may differ numerically from the previous `loudness_zwtv()` implementation because the backend changes

Non-goal:

- this pass does not add cross-call caching between judgment and chart generation

### Existing Chart-Authority Warning Contract

Required behavior:

- `TestManager` must continue treating serialized loudness judgment as authoritative
- if recomputed chart payload omits loudness for a channel whose serialized loudness result is available, the existing warning log remains required
- if recomputed chart payload produces a peak that disagrees with serialized `loudness_max_sone`, the existing mismatch warning remains required

This pass changes the shared backend used by both judgment and chart recomputation, so those warnings are expected to remain rare, but the existing authority contract still stands and must be revalidated.

## Loudness Chart Y-Range Contract

Required behavior:

- the visible loudness chart Y range must be derived from the plotted loudness values rather than from distant default limit lines
- in the new `y_axis_mode="data_focus"` path, `LineChartWidget.set_series(...)` computes a data-only Y range from the plotted `y_values`
- `LineChartWidget` starts in stored `y_axis_mode="auto"`, stores the most recently requested Y-axis mode, and `clear_data()` consults that stored mode
- `set_series(..., y_axis_mode="data_focus")` requires a non-empty finite series; an empty series or non-finite values raise `ValueError`
- a `data_focus` validation failure is atomic: it leaves the prior stored mode, payload, and rendered range unchanged
- if the stored mode is `data_focus`, `clear_data()` resets the visible Y range to the explicit empty-state fallback `0.0 .. 1.0`
- if the stored mode is `auto`, `clear_data()` preserves the current backward-compatible behavior with no explicit Y fallback management
- for non-zero-span data, the visible range is:
  - `y_min = min(data) - padding`
  - `y_max = max(data) + padding`
  - `padding = max(0.1 * (max(data) - min(data)), 0.1)`
- for zero-span data, the visible range is centered on the single value with:
  - `padding = max(0.1 * abs(value), 0.5)`
  - `y_min = value - padding`
  - `y_max = value + padding`
- the `0.0 .. 1.0` empty-state reset is a new `LineChartWidget` contract introduced by this pass and must be asserted in updated line-chart zero-axis tests

Allowed behavior:

- a far-away lower or upper loudness limit line may render outside the visible Y range
- a limit line that is already near the plotted data may remain visible if the chosen policy naturally includes it

Ownership:

- `LineChartWidget` exposes a reusable `set_series(...)` option for Y-range mode while keeping its current default behavior unchanged
- `LoudnessModule` should be the caller that opts into that mechanism

## Testing Strategy

Add or update regression tests for:

- the loudness DSP adapter calling the segmented backend instead of the current time-varying backend
- the loudness adapter continuing to normalize non-`48 kHz` input to `48 kHz` before the backend call
- the loudness adapter padding shorter-than-`2048` inputs and clamping returned times back to the actual duration
- scalar MoSQITo one-segment output being normalized into a one-point trace
- the loudness adapter still returning finite times, values, and max-sone outputs
- existing loudness backend failure behavior remaining unchanged in judgment and summary payload construction
- review-payload loudness chart generation still producing the same payload shape
- existing `TestManager` loudness chart warning behavior remaining intact after the backend change
- `LineChartWidget` default `y_axis_mode="auto"` behavior remaining backward-compatible
- invalid `y_axis_mode` raising `ValueError`
- non-finite line-chart series values raising `ValueError` in `y_axis_mode="data_focus"`
- empty `y_axis_mode="data_focus"` series raising `ValueError` without mutating prior widget state
- single-point / zero-span `y_axis_mode="data_focus"` behavior following the specified range formula and empty-state `0.0 .. 1.0` reset
- the loudness chart Y range following the data band instead of default `0..60` limits
- loudness chart empty-state and selected-channel behavior remaining unchanged

Verification should include:

- `tests/test_dsp.py`
- `tests/test_judgment.py`
- `tests/test_review_payloads.py`
- `tests/test_test_manager/test_review_payload_emission.py`
- `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- `tests/test_main_window.py`
- any targeted loudness-related factory review regressions affected by the backend change
- warm-start timing probes for seeded `3 s` / `2-channel` four-call loudness workloads at both `48 kHz` and `44.1 kHz`, each with a pass threshold of `< 2.0 s`

### Timing Probe Procedure

The manual timing probes use these deterministic fixtures:

- `duration_sec = 3.0`
- `buffer_48k = np.random.default_rng(0).standard_normal((144000, 2)).astype(np.float64) * 0.01`
- `buffer_44k1 = np.random.default_rng(0).standard_normal((132300, 2)).astype(np.float64) * 0.01`
- for each fixture, make one untimed warmup call to `compute_loudness_trace(buffer[:, 0], sample_rate)` before starting the timer
- for each fixture, run `compute_loudness_trace(buffer[:, 0], sample_rate)` twice and `compute_loudness_trace(buffer[:, 1], sample_rate)` twice
- measure the wall-clock time for those four sequential calls only

Pass criterion:

- total elapsed time for those four calls is `< 2.0 s` for the `48 kHz` fixture
- total elapsed time for those four calls is `< 2.0 s` for the `44.1 kHz` fixture

## Files Likely Affected

- `app/service/analysis/dsp.py`
- `app/service/analysis/judgment.py`
- `app/service/analysis/review_payloads.py`
- `app/service/analysis/test_manager.py`
- `ui/widgets/analysis_charts.py`
- `ui/modules/loudness.py`
- `tests/test_dsp.py`
- `tests/test_judgment.py`
- `tests/test_review_payloads.py`
- `tests/test_test_manager/test_review_payload_emission.py`
- `tests/test_analysis_chart_modules/test_zero_based_axes.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`
- `tests/test_main_window.py`

## Non-Goals

This design does not introduce:

- new loudness config controls
- asynchronous background chart generation
- a loudness result cache shared between judgment and chart payloads
- changes to the current review summary row ordering or layout
