# Configurable FFT Resolution Design

## Summary

FFT analysis currently becomes expensive when the selected analysis window is long because several FFT paths still use the full signal length as the FFT point count. This pass introduces a separate configurable FFT resolution for each channel and makes every FFT consumer use the same bounded-resolution contract.

The user-approved product rules for this pass are:

- add a selectable FFT resolution parameter
- use one shared FFT resolution across live FFT, judgment, review charts, and golden-reference FFT comparisons
- when the analysis window is longer than the configured FFT resolution, the FFT must still represent the full analysis window rather than only the first slice
- do not add a native or unbounded FFT mode in this pass
- backward compatibility is not required

## Spec Relationship

This spec supersedes the committed-FFT contract in:

- `docs/double-sdd/specs/2026-03-31-fft-window-independence-and-stft-leading-frame-design.md`
- `docs/double-sdd/specs/2026-04-01-golden-sample-fft-octave-design.md`
- `docs/double-sdd/specs/2026-04-02-startup-db-bootstrap-repair-design.md`

After approval of this document:

- the March 31 spec remains authoritative for the STFT leading-frame behavior introduced there
- the March 31 rule that committed FFT length equals the selected analysis-window length is replaced by the bounded `fft_nfft` contract in this document
- the April 1 golden-sample spec remains authoritative for the golden-binding feature set, except its rule that golden FFT must use the normatively full-window FFT path is replaced by the bounded `fft_nfft` contract in this document
- the April 2 startup-bootstrap spec remains authoritative for startup migration ownership, but its `channel_config_table` migration set is amended to include `fft_nfft INTEGER NOT NULL DEFAULT 4096` and the matching `DbConnectionService` regression coverage

## Problem

### Performance Failure

Long analysis windows can contain hundreds of thousands of samples. The current committed FFT paths still derive `nfft` from the full signal length, which causes:

- very large FFT allocations
- expensive recomputation during review payload building
- expensive spectrum rendering due to oversized payloads
- visible UI stutter when entering or re-entering analysis/review views

### Inconsistent FFT Contracts

The current codebase does not use one FFT-length rule everywhere:

- the live `AnalysisPipeline` FFT is already bounded by `channel_config["stft_nfft"]`
- judgment FFT band checks use the full selected signal length
- committed review FFT charts use the full selected signal length
- golden-reference FFT caches use the full stored golden sample length

This means performance and FFT axes change depending on which path produced the spectrum, which is incorrect once FFT becomes a user-configured analysis setting.

### Missing Product Control

There is currently no dedicated FFT-resolution setting. Operators cannot choose a predictable FFT cost/accuracy tradeoff independently from STFT settings.

## Scope

In scope:

- adding a dedicated per-channel FFT resolution setting
- using the new setting consistently in all FFT-producing paths
- defining bounded full-window FFT behavior for signals longer than the configured FFT resolution
- updating config persistence, validation, and UI editing
- updating golden-reference FFT generation to share the same bounded contract
- regression coverage for performance-sensitive FFT sizing behavior

Out of scope:

- STFT rendering behavior
- SPL, loudness, and octave behavior
- adding a native/full-resolution FFT mode
- redesigning spectrum chart visuals
- changing FFT bus signal signatures or review payload shapes

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - existing `fft_updated` bus signal signature
  - existing review chart payload field names
  - existing FFT band-limit workflow and result payload shape
  - existing golden-sample binding workflow
- Allowed breakage:
  - current implicit rule that committed FFT uses the full raw analysis-window length
  - current implicit rule that golden FFT references use the full raw golden-sample length
  - current coupling where live FFT uses `stft_nfft` because no dedicated FFT setting exists
- Migration strategy: startup schema migration adds `fft_nfft` to `channel_config_table` with default `4096` and backfills existing rows in place; no compatibility layer is provided for the old full-length FFT behavior

## Approved Decisions

1. Add a dedicated per-channel config field named `fft_nfft`.
2. `fft_nfft` is separate from `stft_nfft`.
3. All FFT consumers must use the same `fft_nfft` setting value for a channel.
4. This includes:
   - live FFT frames from `AnalysisPipeline`
   - judgment FFT band checks
   - committed review FFT chart payloads
   - golden-reference FFT generation and comparison
5. Fixed selectable resolutions are required; no native/full-resolution option is included in this pass.
6. For committed FFT consumers, when the analyzed signal is longer than `fft_nfft`, the FFT must still represent the full signal, not just the first `fft_nfft` samples.
7. Backward compatibility for existing saved configs is not required.

## Root Cause Summary

The current stutter is not primarily caused by STFT redraw alone. The dominant FFT-specific root cause is that several committed FFT paths use `nfft = len(signal)`:

- `JudgmentEngine._measure_fft(...)`
- `review_payloads._measure_fft_levels(...)`
- `GoldenReferenceCache.ensure_fft(...)`

For long analysis windows, that creates very large spectra and large chart payloads every time the review payload is rebuilt or re-rendered.

The live FFT path is already bounded, but it is bounded by the wrong setting: `stft_nfft` instead of a dedicated FFT-resolution parameter.

## Design Summary

Introduce a dedicated `fft_nfft` channel setting and centralize committed FFT-length behavior inside the DSP FFT helper.

The FFT contract becomes:

- if `len(signal) <= fft_nfft`, keep the current one-sided FFT behavior using pad-to-length when needed
- if `len(signal) > fft_nfft`, compute a bounded-resolution spectrum that still uses the full signal by aggregating fixed-size FFT windows across the whole signal
- always return a one-sided spectrum with `fft_nfft // 2 + 1` bins
- always preserve the original runtime sample-rate axis up to the original Nyquist frequency

This makes FFT cost predictable while ensuring the full selected analysis window still contributes to the committed result set.

## Configuration Model

### New Field

Add a new per-channel field:

- `fft_nfft: int`

Default:

- `4096`

Allowed values:

- `256`
- `512`
- `1024`
- `2048`
- `4096`
- `8192`

### Persistence and Validation

Required behavior:

- store `fft_nfft` in `channel_config_table`
- extend startup DB migration to add `fft_nfft INTEGER NOT NULL DEFAULT 4096` when missing
- backfill existing rows to `4096` as part of that migration if needed
- include it in `ConfigService._CHANNEL_DEFAULTS`
- include it in `ConfigService.load_config(...)`
- include it in `ConfigService._channel_db_values(...)`
- validate it against the allowed FFT-resolution list
- no compatibility mode is required for reproducing the old full-length FFT behavior

### UI Placement

Required behavior:

- expose `fft_nfft` in the FFT section of the config UI, not the STFT section
- use a fixed-select combo box with the allowed values above
- label it clearly as FFT resolution / FFT point count for the FFT analyzer
- do not expose a native/full option in this pass

## FFT Computation Contract

### Shared Helper Contract

`dsp.compute_fft_spectrum(...)` becomes the single source of truth for bounded FFT behavior.

Inputs:

- `signal`
- `sample_rate`
- `nfft`

Outputs:

- one-sided frequency axis
- one-sided magnitude spectrum in dB

Committed FFT callers listed in this spec must use this helper with `channel_config["fft_nfft"]`.

Live FFT uses the same `fft_nfft` value and the same helper, but the helper input for live FFT is the rolling live-FFT buffer defined in `Consumer Rules > Live FFT`, not the entire run history.

If `len(signal) == 0`:

- `compute_fft_spectrum(...)` must raise a `ValueError` describing the empty FFT input
- committed/judgment/golden callers continue handling that through their existing unavailable/error paths
- live FFT does not call the helper until its live buffer contains enough samples to produce a spectrum

If `sample_rate` is not a finite positive number or `nfft` is not a positive integer:

- `compute_fft_spectrum(...)` must raise `ValueError`
- callers must not silently coerce invalid inputs into fallback FFT behavior

### Short-Signal Behavior

If `len(signal) < nfft`:

- keep the current zero-padding behavior
- keep the current Hann-window usage
- keep the current one-sided axis and scaling contract

If `len(signal) == nfft`:

- keep the current single-window FFT behavior

### Long-Signal Behavior

If `len(signal) > nfft`, do not truncate to the first `nfft` samples and do not compute a native-length FFT.

Instead, compute a bounded full-window FFT as follows:

1. set `hop = max(1, nfft // 2)`
2. enumerate full-window start positions as `0, hop, 2*hop, ...` while `start + nfft <= len(signal)`
3. if the last enumerated full window does not include the final sample, append exactly one additional tail window starting at `len(signal) - nfft`
4. deduplicate start positions while preserving order so the extra tail window is omitted when it would repeat the last full-window start
5. for each retained start position, slice exactly `signal[start:start + nfft]`
6. use Hann windowing for each analysis window
7. compute a one-sided `nfft` FFT for each window at the original sample rate
8. convert each window FFT to the same linear-magnitude scale used by the current helper before dB conversion
9. average the linear magnitudes across all windows
10. convert the averaged magnitudes to dB for the final return value

This defines one deterministic window-start sequence and one deterministic tail-inclusion rule for both measured and golden FFT generation.

Observable requirements:

- the full signal contributes to the result
- the returned spectrum length is capped by `nfft`
- the returned frequency axis still spans `0 .. sample_rate / 2`
- two long signals with different durations but the same sample rate and `fft_nfft` produce the same axis length

This is intentionally a bounded full-window aggregate spectrum, not a first-slice FFT.

## Consumer Rules

### Live FFT

`AnalysisPipeline` must stop using `stft_nfft` for live FFT.

Required behavior:

- STFT continues using `stft_nfft`
- live FFT uses `fft_nfft`
- add a dedicated rolling live-FFT buffer that accumulates mono samples across chunks independently of the STFT buffer
- the live-FFT buffer keeps the most recent `fft_nfft` samples
- while the live-FFT buffer contains fewer than `fft_nfft` samples, `AnalysisPipeline` emits no FFT frame yet
- once the live-FFT buffer reaches `fft_nfft` samples, each subsequent chunk with extractable mono samples for that channel must emit exactly one live FFT frame computed from the rolling live-FFT buffer
- live FFT therefore remains functional even when `fft_nfft` is larger than the recorder chunk size
- `AnalysisPipeline.reset()` and any new pipeline instance must clear the live-FFT buffer before the next run begins
- `fft_updated` signal shape remains unchanged

### Judgment FFT

`JudgmentEngine._measure_fft(...)` must use `fft_nfft`.

Required behavior:

- make the `nfft` dependency explicit at the boundary, for example `_measure_fft(sig, sample_rate, *, nfft: int)`
- call sites pass `channel_config["fft_nfft"]` into that boundary
- if measured FFT generation raises `ValueError` or another spectrum-generation error, judgment must set `fft_available = False`, populate `fft_error_message`, and treat FFT band results as unavailable rather than hard-failing the entire judgment path
- FFT band checks use the bounded full-window FFT result
- long analysis windows no longer expand the FFT axis size
- FFT band-limit evaluation remains on the returned bounded axis

### Review Payload FFT

`review_payloads._measure_fft_levels(...)` must use `fft_nfft`.

Required behavior:

- make the `nfft` dependency explicit at the boundary, for example `_measure_fft_levels(signal, sample_rate, *, nfft: int)`
- call sites pass `channel_config["fft_nfft"]` into that boundary
- if measured FFT chart recomputation raises `ValueError` or another spectrum-generation error, the committed chart builder must omit the affected `fft` payload item for that channel and log a warning instead of re-raising and aborting the full chart-payload build
- committed FFT chart payloads are capped to `fft_nfft // 2 + 1` plotted points
- repeated access to the review/homepage analysis views no longer requires building a full-window native FFT payload
- payload shape stays unchanged aside from the bounded axis length

### Golden FFT

`GoldenReferenceCache.ensure_fft(...)` must use the same `fft_nfft` as the measured channel.

Required behavior:

- make the `nfft` dependency explicit at the interface boundary, for example `ensure_fft(bundle, *, nfft: int)`
- cache golden FFT results by both bundle identity and `nfft`, rather than assuming one cached FFT per bundle
- measured FFT and golden FFT use the same bounded axis construction
- FFT golden absolute/delta comparisons continue to require matching axes
- long golden samples no longer force native-length FFT axes

## Behavioral Consequences

Accepted behavior changes in this pass:

- long-window FFT results are no longer native-resolution single-shot FFTs
- long-window FFT results become bounded aggregate spectra
- chart point count and FFT comparison cost are now determined by `fft_nfft`, not by raw signal length

Required consistency:

- for a given sample rate and `fft_nfft`, all FFT consumers must agree on axis construction
- when measured and golden FFT are computed for the same signal window, they must agree on both axis length and axis values

## Testing Strategy

Add or update regression tests for:

- config persistence and validation of `fft_nfft`
- startup DB bootstrap/backfill adds `channel_config_table.fft_nfft` for older DBs and preserves existing rows
- config UI collection and population of `fft_nfft`
- live pipeline uses `fft_nfft` instead of `stft_nfft`
- live pipeline still emits FFT after enough rolling samples accumulate even when `fft_nfft` is larger than one recorder chunk
- judgment FFT uses `fft_nfft` instead of `len(signal)`
- review payload FFT uses `fft_nfft` instead of `len(signal)`
- golden-reference FFT uses `fft_nfft` instead of golden signal length
- long-signal FFT uses the full signal rather than truncating to the first `nfft` samples
- long-signal FFT output length is capped to `fft_nfft // 2 + 1`
- FFT axis matching still works for measured/golden comparisons under the new bounded contract

Verification should include:

- targeted FFT DSP tests
- targeted config-service/config-module tests
- targeted judgment/review-payload/golden-reference tests
- targeted pipeline/session tests where needed
- full `pytest` suite before completion

## Files Likely Affected

- `model/channel_config_table.py`
- `app/service/db_connection_service.py`
- `app/service/config_service.py`
- `ui/modules/config.py`
- `app/service/analysis/dsp.py`
- `app/service/analysis/pipeline.py`
- `app/service/analysis/judgment.py`
- `app/service/analysis/review_payloads.py`
- `app/service/analysis/golden_reference.py`
- relevant FFT/config test files under `tests/`

## Non-Goals

This design does not introduce:

- an unbounded native FFT mode
- FFT/STFT setting unification
- spectrum-chart caching redesign
- STFT, SPL, loudness, or octave algorithm changes
- a migration layer for legacy saved configs
