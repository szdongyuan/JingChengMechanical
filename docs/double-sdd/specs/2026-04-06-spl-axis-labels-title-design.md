# SPL Axis Labels And Title Design

## Summary

The SPL time-curve chart currently renders without axis labels, which makes the chart harder to read during review. This pass adds explicit axis labels to the SPL module and renames the visible chart card title from `SPL 时间曲线 (A计权)` to `声压级 (SPL)`.

The user-approved rules for this pass are:

- add axis labels to the SPL time curve
- change the visible chart title to `声压级 (SPL)`
- apply the change automatically without compatibility work

## Scope

In scope:

- setting the SPL chart bottom axis label
- setting the SPL chart left axis label
- updating the main analysis card title for the SPL chart
- adding focused regression coverage for the new labels and title

Out of scope:

- changing SPL payload generation
- changing SPL chart rendering data, limits, or colors
- changing loudness, FFT, STFT, octave, or result-table labels
- adding compatibility shims or migration behavior

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - SPL chart data payload shape
  - SPL review rendering behavior
  - main analysis grid layout and card positions
- Allowed breakage:
  - the user-visible SPL card title text
  - the previously blank SPL axis-label state
- Migration strategy: none

## Approved Decisions

1. Keep the SPL module on the existing shared `LineChartWidget`.
2. Set the bottom axis label to `Time (s)` to match the existing chart-labeling pattern used by loudness.
3. Set the left axis label to `SPL (dB)` so the plotted quantity and unit are explicit.
4. Rename the main-window SPL card title to `声压级 (SPL)`.
5. Keep all other SPL behavior unchanged.

## Design Summary

The change stays localized to the SPL module and the main analysis page composition.

Current behavior:

- `SplModule` instantiates `LineChartWidget` without assigning axis labels
- `app/main_window.py` titles the card `SPL 时间曲线 (A计权)`

New behavior:

- `SplModule` assigns `Time (s)` on the bottom axis and `SPL (dB)` on the left axis
- the labels remain present both when the chart is empty and after committed payloads render
- `app/main_window.py` titles the card `声压级 (SPL)`

## Testing Strategy

Add or update focused UI tests for:

- SPL committed-chart rendering asserting the bottom and left axis labels
- SPL empty-chart behavior asserting the bottom and left axis labels remain visible after clearing
- main-window card-title coverage asserting the SPL card title text

Verification should include:

- the focused committed chart module tests for SPL
- the focused main-window card-title test

## Files Likely Affected

- `ui/modules/spl.py`
- `app/main_window.py`
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
- `tests/test_main_window.py`
