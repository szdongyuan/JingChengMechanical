# Octave Bar Edge Padding Design

## Purpose

The octave frequency bar chart currently sets its horizontal view range to the first and last bar centers. Because each bar has visible width, the first and last bars are clipped at the chart edge. The chart must show both outer bars completely by leaving a small amount of blank space before the first bar and after the last bar.

## Scope

In scope:

- Update `OctaveBarChartWidget` in `ui/widgets/analysis_charts.py`.
- Add or update chart widget tests under `tests/test_analysis_chart_modules/`.
- Preserve existing payload structure, tick labels, bar width, golden overlay rendering, limit marker rendering, y-axis behavior, and empty-state fallback axes.

Out of scope:

- FFT line chart behavior.
- STFT image chart behavior.
- DSP or analysis payload generation.
- Operator configuration, saved data, or database changes.

## Required Behavior

- When octave bars are rendered, the x-axis range must include the full visual width of the first and last bars.
- The x-axis range must also leave a small blank margin outside those bars.
- For the current octave bar width of `0.6`, use a total edge padding of `0.5` axis units on each side. This means a three-bar chart with centers `0.0, 1.0, 2.0` uses x range `-0.5 .. 2.5`.
- A single-bar chart uses x range `-0.5 .. 0.5`.
- Clearing data still resets the chart to the existing empty fallback x range `0.0 .. 1.0`.
- Other chart widgets keep their existing x-axis behavior.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces: chart payload format, signal signatures, non-octave chart behavior, empty-state chart behavior.
- Allowed breakage: octave bar chart visible x-axis range changes to include edge padding.
- Migration strategy: none.

## Verification

- Add or update tests proving octave bar charts use padded edge ranges for multiple bars, single bar, and limit-marker cases.
- Run focused chart widget tests:

```powershell
pytest tests/test_analysis_chart_modules/test_zero_based_axes.py tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py -k "octave_bar_chart_widget" -q
```
