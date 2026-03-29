# Chart Zero-Axis And STFT Data Alignment Design

## Context

The recent STFT selection-box fix corrected several geometry problems, but one operator-visible defect remains:

- when the analysis window is set to start at `0.0 s`, the rendered selection box still starts noticeably to the left of the plotted `0.0 s` position
- a configured `3.0 s` window still renders materially wider than the plotted `0.0 s -> 3.0 s` span

Direct runtime inspection shows why:

- the STFT view box still includes pyqtgraph auto-range padding on the x-axis
- the selection box is mapped by scaling across the full visible plot rectangle
- the actual plotted data positions for `x=0.0` and `x=3.0` are inset from the plot rectangle edges because of that padding

The user also wants the chart axes themselves corrected:

- all plots should start their x-axis at `0`
- no left or right padding should be shown on the x-axis
- the STFT y-axis should also start at `0`

This is now a shared chart-behavior change plus a final STFT overlay-alignment correction.

## Scope

In scope:

- make all chart x-axes start at `0`
- remove horizontal padding on all chart x-axes
- make the STFT y-axis start at `0`
- remove lower-side y-axis padding on the STFT chart
- map the STFT selection box to exact plotted data coordinates instead of scaling across the full plot rectangle
- preserve the existing STFT review workflow and selection semantics
- add regression tests for zero-based axis behavior and exact STFT box alignment

Out of scope:

- changing review-window business rules
- changing controller states or workflow phases
- changing analysis payload formats
- changing non-axis visual styling
- introducing compatibility shims for old padded-axis behavior

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - existing factory review workflow
  - current `default_window` / `fallback_window` / `manual_reanalysis_allowed` semantics
  - current manual re-analysis call contract using `(start_sec, end_sec)`
- Allowed breakage:
  - existing padded chart ranges
  - existing STFT box placement that is derived from plot-rectangle scaling instead of data-coordinate mapping
- Migration strategy: none

## Approved Decisions

1. All chart x-axes must have a visible minimum of `0`.
2. All chart x-axes must render with no left or right padding.
3. The STFT y-axis must have a visible minimum of `0`.
4. The STFT y-axis must render with no lower-side padding below `0`.
5. The STFT selection box must align to the plotted data-space positions for its `start_sec` and `end_sec`, not to the left and right edges of the full view box.
6. A selected STFT window of `0.0 s -> 3.0 s` must visually align to the plotted `x=0.0` and `x=3.0` positions, with at most one rendered frame of tolerance at each edge.
7. The STFT selection box must remain fully inside the plotted STFT area.
8. Dragging and direct selection moves must use the same data-to-pixel geometry source as rendering.
9. If plot geometry or rendered STFT payload is unavailable, the box must stay hidden and selection-move requests must be no-ops.
10. No backward-compatibility mode is required for the previous padded-axis behavior.

## Root Cause Summary

Two related issues are now confirmed:

### Shared Chart Axis Behavior

The shared chart widgets currently rely on pyqtgraph auto-ranging without an explicit axis policy. That leaves default padding around the visible ranges.

Consequences:

- time plots do not visually begin at `0`
- frequency plots do not visually begin at `0`
- the STFT frequency axis does not visually begin at `0`

### STFT Selection Box Mapping

The current STFT overlay fix uses the rendered time extent and the visible plot rectangle, but it still converts time to pixel position by linear interpolation across the entire visible plot rectangle.

That is only correct if:

- the visible plot rectangle edges correspond exactly to the rendered data-space bounds

With padded axes, that assumption is false. The actual plotted position of `x=0.0` is inset from the left edge of the plot rectangle, so the overlay starts too far left and becomes too wide.

## Design Summary

Make the shared chart widgets responsible for explicit zero-based axis policy, then make the STFT overlay use the chart's actual data-to-pixel transform.

This separates two concerns cleanly:

- chart widgets own visible axis ranges and padding policy
- the STFT module owns review-window selection logic

The STFT module should no longer infer box coordinates by scaling across the plot rectangle width. Instead, it should ask the chart for the plotted pixel positions of the selected `start_sec` and `end_sec` values.

## Chart Axis Policy

### Shared X-Axis Policy

For all charts:

- visible x-axis minimum must be `0`
- no left-side x padding is allowed
- no right-side x padding is allowed

This applies to:

- STFT
- SPL
- FFT
- octave

If a dataset begins above `0`, the chart should still display from `0` to the dataset maximum.

### STFT Y-Axis Policy

For STFT only:

- visible y-axis minimum must be `0`
- no padding is allowed below `0`

The upper STFT y bound may still follow the rendered frequency payload, but the visible range must begin exactly at `0`.

## STFT Overlay Geometry Contract

### Data-Space Alignment

The STFT selection box must be positioned from actual plotted data coordinates.

Required behavior:

- the left edge is the plotted pixel position of `selected_window.start_sec`
- the right edge is the plotted pixel position of `selected_window.end_sec`
- the top edge matches the plotted STFT region top
- the bottom edge matches the plotted STFT region bottom

The implementation must not approximate those x positions by scaling across the full plot rectangle width.

### Duration Rendering

If the logical selected window duration is `D`, the rendered box width must match the plotted span from `start_sec` to `start_sec + D`.

Tolerance:

- at most one rendered STFT frame of error at each edge

The implementation must not introduce additional width error from chart padding or view-box interpolation.

### Bounds / Interaction

Selection normalization rules remain the same as the current approved STFT geometry fix:

- clamp against the intersection of captured-duration bounds and rendered STFT time bounds
- hide the box when no full-duration interval exists in that intersection
- keep `selected_window` unchanged when geometry is unavailable
- keep drag and direct selection moves as no-ops when geometry is unavailable

Dragging and rendering must both use the same data-to-pixel transform source.

## Testing Strategy

Add or update tests for:

- all chart widgets render with x-axis minimum `0`
- all chart widgets render with no horizontal padding
- STFT renders with y-axis minimum `0`
- STFT renders with no lower-side y padding
- the STFT box left edge aligns to the plotted `x=0.0` position when the selected window starts at `0.0 s`
- the STFT box right edge aligns to the plotted `x=3.0` position when the selected window ends at `3.0 s`
- the STFT box width matches the plotted time span within the approved one-frame-per-edge tolerance
- drag and direct-move operations continue to rerender the box at the matching plotted positions
- unavailable-geometry and recovery behaviors from the existing STFT fix remain intact

## Files Likely Affected

- `ui/widgets/analysis_charts.py`
- `ui/modules/stft.py`
- `tests/test_analysis_chart_modules.py`
- `tests/test_stft_module.py`

## Non-Goals

This design does not introduce:

- new controller states
- new chart payload contracts
- nonzero axis padding options
- changes to analysis calculations

## Recommendation

Implement the next fix in two layers:

1. set explicit zero-based, no-padding axis policy in the shared chart widgets
2. change the STFT selection box to map `start_sec` and `end_sec` through the actual chart data-to-pixel transform

This is the smallest correct follow-up because it fixes both the remaining overlay error and the newly approved axis behavior at the proper ownership boundary.
