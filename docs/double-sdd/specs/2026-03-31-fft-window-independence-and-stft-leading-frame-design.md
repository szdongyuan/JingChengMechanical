# FFT Window Independence And STFT Leading-Frame Design

## Summary

Two operator-visible analysis defects must be corrected:

- the committed FFT plot changes point count when `stft_nfft` changes, even when the selected analysis window is unchanged
- the live/review STFT image leaves a blank leading gap before the first visible column, and that gap grows as the STFT window size grows

This pass fixes both defects at the analysis-data source rather than compensating in the UI.

## Problem

### Incorrect FFT Point-Count Coupling

The committed FFT chart is built from the selected analysis window, but its FFT length is still derived from `channel_config["stft_nfft"]`.

As a result:

- changing the STFT window length changes the FFT point count
- the committed FFT is not strictly the FFT of the selected analysis window

This is incorrect. The committed FFT must depend only on the selected analysis-window samples, not on the STFT frame size.

### Incorrect STFT Leading Blank Region

The live STFT path computes STFT columns on the rolling signal buffer, but it emits only the last STFT column from each chunk.

Because earlier newly available columns are dropped:

- the rendered STFT image does not begin at the earliest valid STFT time
- the first visible column appears late
- the blank leading gap grows when `stft_nfft` grows

This is also incorrect. The STFT surface should render the full sequence of newly available STFT columns from the start of the run.

## Scope

In scope:

- committed FFT chart generation for review payloads
- live STFT frame emission from the streaming analysis pipeline
- session-level propagation of live STFT frames to the bus
- regression tests for FFT window independence and STFT leading-frame coverage

Out of scope:

- STFT selection-box geometry
- review-window business rules
- controller states or workflow
- SPL and octave behavior
- UI-only visual compensation for missing STFT columns
- localization or unrelated chart styling changes

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - factory review workflow
  - existing bus signal signatures for `stft_updated` and `fft_updated`
  - current manual reanalysis contract using `(start_sec, end_sec)`
  - chart widget payload shapes already consumed by the UI
- Allowed breakage:
  - current incorrect dependency of committed FFT point count on `stft_nfft`
  - current incorrect live STFT leading blank region caused by dropping early columns
  - current implicit assumption that at most one STFT frame is emitted per chunk
- Migration strategy: none

## Approved Decisions

1. The committed FFT must be computed from the exact selected analysis-window samples.
2. The committed FFT must not depend on `stft_nfft`.
3. If the selected analysis window contains `N` samples, the committed FFT length is exactly `N`.
4. Therefore the committed one-sided FFT plot length is `floor(N / 2) + 1`.
5. Changing the STFT window size must not change the committed FFT point count when the selected analysis window is unchanged.
6. The live STFT path must emit every newly available STFT column instead of only the last column from each chunk.
7. The earliest valid emitted STFT column for a run must be rendered at `0.0 s` when the first chunk already contains enough data for a `0.0 s` STFT column.
8. Larger `stft_nfft` values must not create additional leading blank region by dropping earlier valid STFT columns.
9. No compatibility mode is required for the old incorrect behavior.

## Root Cause Summary

### FFT Root Cause

`build_chart_payload()` slices the selected analysis window correctly, but `_build_fft_chart()` still reads `channel_config["stft_nfft"]` and passes that value to `compute_fft_spectrum()`.

That incorrectly couples the committed FFT resolution to the STFT analysis configuration.

### STFT Root Cause

`AnalysisPipeline.process_chunk()` computes an STFT matrix on the rolling buffer, but after the STFT call it constructs only one `StftFrame` from the last STFT column and discards the earlier columns returned for that chunk.

`compute_stft()` already yields an initial `0.0 s` column under the current SciPy configuration, so the leading blank region is not caused by missing data from the DSP helper. It is caused by the pipeline dropping early valid STFT columns before they reach the UI cache.

## Design Summary

Keep the committed FFT and live STFT fixes local to the analysis pipeline / payload builders.

For committed FFT:

- continue slicing the selected analysis window exactly as today
- stop using `stft_nfft` to determine FFT length
- compute the FFT over the exact selected-window sample count

For live STFT:

- let the pipeline emit all newly available STFT columns
- preserve the existing `StftFrame` dataclass and `stft_updated` signal shape
- allow multiple `stft_updated` emissions for one input chunk when multiple new STFT columns become available

This fixes the actual data contracts seen by the UI instead of faking the missing columns in rendering code.

## Committed FFT Contract

### Input Window

The selected analysis-window sample slice remains the sole FFT input for committed review charts.

Required behavior:

- `build_chart_payload()` continues to slice the full audio buffer using the selected analysis window
- `_build_fft_chart()` receives the calibrated mono signal from that exact slice
- no additional truncation or zero-padding is introduced merely because of `stft_nfft`

### FFT Length Rule

Required behavior:

- FFT length equals `len(signal)`
- if `signal` has `N` samples, the frequency axis length is `floor(N / 2) + 1`
- two otherwise identical review payloads with the same selected analysis-window samples but different `stft_nfft` values must produce the same FFT frequency-axis length

The implementation may continue using the existing DSP helper if the helper is updated to support exact-signal-length FFTs, or it may bypass the helper if that is the smaller and clearer change. The observable requirement is the committed FFT contract above.

## Live STFT Contract

### Emission Model

The pipeline must stop collapsing each STFT update to the last returned column.

Required behavior:

- after computing STFT on the rolling buffer, inspect every returned STFT column
- convert each returned STFT column into a `StftFrame`
- emit every column that is newly available for this run

### Duplicate Suppression

Because overlapping rolling-buffer recomputation will revisit earlier STFT columns, the pipeline must suppress duplicates.

Required behavior:

- track the latest emitted STFT time per pipeline instance
- emit only STFT columns whose absolute time is strictly newer than the last emitted time

### Chunk-End Bound

The pipeline must not emit padded future columns beyond the audio currently available for the chunk boundary being processed.

Required behavior:

- derive the current chunk-end time from the chunk boundary already represented by `elapsed_time`
- only emit STFT columns whose absolute time is at or before the current chunk-end time

### Time Axis

`StftFrame.time` remains the authoritative x-axis value used by the STFT cache and renderer.

Required behavior:

- emitted STFT times must represent actual absolute STFT column times for the run
- the first emitted valid STFT column must therefore appear at `0.0 s` when such a column exists in the returned STFT output
- the UI should not need to infer or backfill missing leading columns

## Session / Bus Behavior

`TestSession` must preserve the existing `stft_updated.emit(config_id, run_id, channel_idx, frame)` signal signature.

Allowed change:

- one incoming audio chunk may now cause zero, one, or multiple `stft_updated` emissions for a given channel

No other signal shapes change in this design.

## Testing Strategy

Add or update regression tests for:

- committed FFT payload length depends on selected-window sample length rather than `stft_nfft`
- same selected analysis window with different `stft_nfft` values produces the same committed FFT point count
- committed FFT still uses the selected analysis-window slice rather than the full recording buffer
- first emitted STFT frame time is `0.0 s` when the first sufficiently large chunk yields a `0.0 s` STFT column
- larger `stft_nfft` values do not create additional leading blank region from dropped STFT columns
- session-level `stft_updated` propagation works when multiple STFT frames are emitted for one chunk
- STFT module cached image payload begins at the earliest emitted STFT time rather than a delayed final-column time

Verification should include:

- targeted `review_payloads`, `pipeline`, `test_session`, and `stft_module` tests
- the full `pytest` suite before completion

## Files Likely Affected

- `app/service/analysis/review_payloads.py`
- `app/service/analysis/dsp.py`
- `app/service/analysis/pipeline.py`
- `app/service/analysis/test_session.py`
- `tests/test_review_payloads.py`
- `tests/test_pipeline.py`
- `tests/test_test_session.py`
- `tests/test_stft_module/...`

## Non-Goals

This design does not introduce:

- new review payload fields
- new UI translation layers
- changes to manual review-window selection behavior
- changes to SPL or octave calculations
