# SPL Boundary Tail Design

## Summary

The committed SPL review chart can produce an extreme low outlier at the end of the plotted series when the selected analysis window ends with a partial SPL chart chunk. The current chart builder samples SPL on a fixed 50 ms chart grid and computes one SPL value per chunk. If the final chunk is much shorter than 50 ms, the weighted SPL calculation on that undersized tail can collapse to a numerically invalid boundary point, which then distorts chart auto-ranging and produces the visible vertical plunge at the right edge.

This pass fixes the bug by omitting any trailing SPL chart chunk shorter than the committed chart step size. The configured SPL time constant remains unchanged and continues to control the SPL detector behavior inside each computed point.

## Problem

Current committed-chart behavior:

- the analysis window is sliced first
- `_build_spl_chart(...)` then walks that sliced signal in fixed 50 ms chunks
- every chunk, including a final partial tail chunk, is passed to `dsp.compute_spl(...)`

Observed failure:

- for `A` or `C` weighting, a very short final chunk can produce an extreme negative SPL value
- that invalid boundary value is serialized into `values_db`
- the line chart then auto-ranges around the outlier and renders a sharp drop at the right boundary

The bug is in the committed SPL chart payload generation, not in the selected analysis window itself and not in the line-chart widget geometry.

## Scope

In scope:

- committed SPL chart payload generation in the review payload path
- suppressing a trailing chunk whose sample count is smaller than the fixed SPL chart chunk size
- focused regression coverage for the omitted tail-point behavior

Out of scope:

- changing the configured SPL time-constant meaning or UI
- redesigning SPL computation into a continuous detector trace
- changing the chart step size from 50 ms
- changing live SPL frame emission
- changing FFT, STFT, octave, loudness, or chart widget auto-range logic
- adding new performance optimizations or caching behavior

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - SPL chart payload keys and overall structure
  - configured `spl_time_constant` behavior
  - SPL lower/upper limit serialization
  - analysis-window slicing rules
  - live SPL pipeline behavior
- Allowed breakage:
  - the presence of a final plotted SPL point when the selected analysis window ends with less than one full 50 ms chart chunk remaining
  - an SPL chart payload with empty `times_sec` and `values_db` when the selected analysis window contains no complete 50 ms chunk
- Migration strategy: none

## Approved Decisions

1. Keep the committed SPL chart on its current fixed 50 ms plotting grid.
2. Keep using the configured `spl_time_constant` inside each computed SPL value.
3. Drop any trailing chunk shorter than the fixed 50 ms chart chunk size instead of computing a boundary point from it.
4. Do not synthesize, pad, or clamp a replacement tail value.
5. Keep the change localized to committed SPL chart payload generation and regression coverage.

## Design Summary

Current implementation:

- `review_payloads._build_spl_chart(...)` computes `chunk_samples` from `_SPL_CHUNK_DURATION_SEC`
- the function iterates over the entire sliced signal with `range(0, signal.size, chunk_samples)`
- the final iteration may produce a partial chunk with fewer than `chunk_samples` samples
- that partial chunk is still sent to `dsp.compute_spl(...)`

New implementation:

- compute the same `chunk_samples`
- iterate only over complete chunks, or equivalently skip any chunk whose sample count is smaller than `chunk_samples`
- keep each emitted SPL point timestamped at the end of its complete 50 ms chunk
- leave all other SPL chart fields unchanged

This change intentionally fixes only the invalid trailing point. It does not reinterpret the SPL detector or alter the chart cadence.

## Rendering Rules

For a selected analysis window:

- each full 50 ms SPL chart chunk produces one plotted SPL point
- the x-value remains the end time of that full chunk relative to the sliced analysis window
- the y-value remains the weighted SPL computed with the configured `spl_time_constant`
- if the remaining tail is shorter than one full 50 ms chunk, no additional SPL point is emitted for that tail

Observable requirements:

- no extreme negative SPL boundary point should appear solely because the final tail is shorter than 50 ms
- a window that ends exactly on a 50 ms chunk boundary should keep its full point count
- a window shorter than one full 50 ms chart chunk should serialize no SPL data points for the committed chart
- when the committed SPL payload contains no data points, the existing blank/no-data SPL chart behavior remains in effect
- when committed SPL data points exist, SPL lower and upper limit lines remain unchanged

## Testing Strategy

Add focused review-payload regression coverage for:

- a signal whose selected analysis slice contains one or more full 50 ms chunks plus a very short tail
- `A`-weighted committed SPL chart output omitting the final partial-tail point
- `C`-weighted committed SPL chart output omitting the final partial-tail point
- a selected analysis window shorter than one full 50 ms chunk returning empty `times_sec` and `values_db`
- exact-boundary behavior where a signal length equal to an integer multiple of the chart chunk size still emits all expected points

Verification should include:

- targeted `tests/test_review_payloads.py` coverage for the committed SPL chart payload
- focused committed SPL module coverage confirming that an empty committed SPL payload keeps the chart blank/no-data
- the directly affected SPL committed-module smoke coverage if needed after implementation review

## Files Likely Affected

- `app/service/analysis/review_payloads.py`
- `tests/test_review_payloads.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`

## Non-Goals

This design does not introduce:

- a new SPL resolution setting
- SPL chart caching changes
- changes to live analysis updates
- detector-state carryover across chart chunks
- new UI controls or labels
