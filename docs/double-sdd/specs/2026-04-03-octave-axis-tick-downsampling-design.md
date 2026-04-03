# Octave Axis Tick Downsampling Design

## Summary

The octave chart currently renders an x-axis label for every octave-band center frequency, which causes severe label crowding and overlap in the main analysis UI. This pass reduces only the visible octave x-axis labels to a fixed operator-approved subset while leaving the plotted bars, data payloads, overlays, markers, and axis geometry otherwise unchanged.

The user-approved product rules for this pass are:

- only modify the octave x-axis labels
- display only the labels `40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 16000`
- keep all other octave-band positions present but unlabeled
- do not change bar positions, payload structure, overlays, limits, or any other chart behavior

## Problem

The octave chart uses one x-axis label per band center, which makes the axis unreadable when the full IEC center-frequency set is shown. The labels visually collide and reduce usability even though the plotted bar data itself is correct.

## Scope

In scope:

- downsampling the visible octave x-axis tick labels to the approved subset
- keeping the underlying bar positions and axis indexing unchanged
- focused regression coverage for the rendered octave tick labels

Out of scope:

- changing octave payload contents
- changing octave bar positions or widths
- changing golden overlay behavior
- changing limit-band markers
- changing FFT, STFT, SPL, loudness, or other chart modules
- changing axis scaling or introducing logarithmic placement

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - octave chart payload keys and values
  - octave bar x-positions and rendering order
  - limit-band marker placement
  - golden overlay rendering
  - review/live module wiring
- Allowed breakage:
  - the exact set of visible octave x-axis label strings
  - unlabeled octave ticks between the approved displayed labels
- Migration strategy: none

## Approved Decisions

1. Keep the octave chart on its existing zero-based categorical/indexed x-axis.
2. Show visible tick labels only for the band centers `40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 16000`.
3. Keep all non-selected band centers at their current positions but render them with empty labels.
4. Do not change the octave payload contract or the module API for this pass.
5. Do not change any chart other than the octave chart.

## Design Summary

The change stays inside `OctaveBarChartWidget` tick generation.

Current behavior:

- `set_bands(...)` creates one bar per center frequency at index positions `0..N-1`
- the bottom axis labels every index using the center frequency string

New behavior:

- `set_bands(...)` still creates one bar per center frequency at the same index positions
- the bottom axis still uses the same index positions
- only indices whose center frequency is one of the approved visible labels receive a text label
- all other indices receive an empty string so the axis keeps the same geometry while becoming readable

This is intentionally a text-only axis change, not an axis-layout or scale change.

## Rendering Rules

For a plotted octave frequency list:

- if a center frequency equals one of `40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 16000`, render its integer label
- otherwise render an empty label at that tick position

Observable requirements:

- bars stay at the same x positions as before
- overlays and markers stay aligned with the same bars as before
- the x-axis remains readable on dense octave payloads
- no additional labels beyond the approved subset appear

## Testing Strategy

Add or update focused widget tests for:

- octave axis tick generation with a dense center-frequency payload
- the visible label list matching exactly `40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 16000`
- non-selected octave centers remaining present in the payload and bar rendering while using empty labels

Verification should include:

- targeted octave chart widget tests
- any directly affected committed octave module tests if needed

## Files Likely Affected

- `ui/widgets/analysis_charts.py`
- `tests/test_analysis_chart_modules/test_widget_payload_and_geometry.py`

## Non-Goals

This design does not introduce:

- logarithmic octave-axis spacing
- payload filtering of octave bands
- changes to octave analysis math
- changes to chart colors, fonts, or layout
