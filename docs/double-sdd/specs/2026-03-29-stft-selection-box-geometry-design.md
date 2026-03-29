# STFT Selection Box Geometry Design

## Context

The current factory STFT review surface renders the movable manual-analysis box with geometry derived from a fixed rectangle inside the widget canvas rather than from the actual pyqtgraph plot area.

Observed operator-facing failures:

- a configured `3 s` review window renders visibly longer than `3 s`
- the box can appear before the visible `0 s` position on the x-axis
- the box bottom can extend below the plotted y-axis region

The underlying selected review window is still clamped logically for analysis execution, so this is primarily a rendering and interaction-geometry defect on the STFT review surface.

## Scope

This spec covers only the STFT manual-review box geometry and drag bounds.

In scope:

- align the selection-box geometry to the actual rendered STFT plot area
- make the rendered box width match the selected review duration
- clamp the box so it cannot render before `0 s`
- clamp the box so it stays fully inside the plotted y-range
- ensure drag behavior respects the same plotted bounds used for rendering
- add regression tests for plot-area alignment and time-width tolerance

Out of scope:

- changing automatic/manual review-window business rules
- changing controller phase transitions
- changing FFT / SPL / octave modules
- redesigning the STFT chart implementation

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - the existing STFT review workflow
  - current `default_window` / `fallback_window` / `manual_reanalysis_allowed` semantics
  - current manual re-analysis call contract using `(start_sec, end_sec)`
- Allowed breakage:
  - current incorrect overlay placement based on fixed canvas margins
  - current visual behavior where the selection box can render outside the plotted axes region
- Migration strategy: none

## Approved Decisions

1. The STFT selection box must be positioned and sized relative to the actual pyqtgraph plot/view area, not a hard-coded canvas rectangle.
2. The box must stay fully inside the visible STFT plot area.
3. The box must never render to the left of the visible `0 s` axis position.
4. The box must never render below the plotted frequency-axis region.
5. The box height should match the visible STFT plot height, not the outer widget height.
6. A selected review duration of `3.0 s` should render as `3.0 s` on the plot, with at most one rendered STFT frame of tolerance at each edge.
7. Dragging must use the same geometry source as rendering so visual position and logical selected window remain consistent.
8. Clamping must use the intersection of valid captured-audio bounds and the plotted STFT time range.
9. If the plot rectangle or rendered STFT payload is temporarily unavailable, the selection box must stay hidden and drag operations must be no-ops until valid geometry is available again.
10. No backward-compatibility mode is required for the old incorrect overlay behavior.

## Root Cause Summary

The current implementation in `ui/modules/stft.py` computes the overlay against `_track_rect()`, which is just the canvas rectangle minus a fixed margin. The actual STFT image is rendered inside the pyqtgraph view box, whose position and size differ materially from that fixed-margin rectangle because of axis labels, tick labels, and internal plot layout.

As a result:

- x-position and x-width are scaled against the wrong pixel width
- the left box edge can appear before the visible `0 s` axis location
- the box height extends into non-plot UI space below the y-axis region

## Design Summary

Treat the rendered STFT plot area as the single geometry authority for the review box.

The STFT module should:

- derive the visible plot rectangle from the pyqtgraph view box
- render the selection box only within that rectangle
- map selected-window start/end times to x positions within that rectangle
- map drag movements back through that same rectangle to selected-window times

This removes the current split between logical bounds and visual bounds.

## Geometry Contract

### Plot-Area Alignment

The selection box geometry must use the actual visible plot rectangle of the STFT chart.

Required behavior:

- box `left` and `width` come from time-to-pixel mapping inside the plot rectangle
- box `top` equals the plot rectangle top
- box `height` equals the plot rectangle height
- button placement may remain outside the box, but the box itself must remain fully inside the plot rectangle

### X-Axis Bounds

The box must not render outside the plotted STFT time range.

Required behavior:

- left edge must not render before the visible `0 s` position
- right edge must not render after the latest full-duration position inside the intersection of:
  - captured-audio bounds
  - rendered STFT x-range
- dragging left clamps at the earliest valid start
- dragging right clamps at the latest valid start that still preserves full duration
- if no full-duration interval exists inside that intersection, the selection box must be hidden and dragging must do nothing

### Duration Rendering

The visual width of the box must reflect the selected review duration accurately.

Required behavior:

- if the selected window duration is `D`, the rendered width corresponds to `D` within the displayed STFT time scale
- STFT payload timestamps must be treated as rendered column positions, not reinterpreted into a different STFT timing model in this fix
- the rendered left edge may differ from the ideal left edge by at most one rendered column width
- the rendered right edge may differ from the ideal right edge by at most one rendered column width
- equivalently, the total rendered-width error may not exceed two rendered column widths
- the implementation must not introduce additional width error from unrelated widget margins or axis layout

### Y-Axis Bounds

The box must be constrained to the plotted STFT frequency region.

Required behavior:

- top edge must align with the plotted STFT region top
- bottom edge must align with the plotted STFT region bottom
- the box must not extend below the visible y-axis plotting region

## Rendering / Interaction Model

The overlay geometry source should be unified.

Recommended flow:

1. resolve the current visible STFT plot rectangle from the chart widget
2. resolve the current plotted STFT time extent from the rendered image payload
3. map `selected_window.start_sec` and `selected_window.end_sec` into that plot rectangle
4. clamp the result to the valid plotted bounds
5. use the same rectangle and time extent to convert drag x positions back to `start_sec`

This ensures the rendered box and dragged box follow the same coordinate system.

### Transient Geometry-Unavailable States

During review-state updates, the plot widget may briefly have no valid view rectangle or no usable rendered image payload.

Required behavior:

- if the plot rectangle is empty or zero-sized, hide the selection box for that paint cycle
- if no rendered STFT image payload is available, hide the selection box for that paint cycle
- in those states, do not mutate `selected_window`
- in those states, drag requests must return without changing the logical selection
- once valid geometry returns, the same logical `selected_window` should render again using the corrected plot-area mapping

## Testing Strategy

Add or update tests for:

- the selection box left edge stays at or to the right of the plotted `0 s` position
- the selection box right edge matches the selected duration within at most one rendered STFT frame at each edge
- the selection box height matches the STFT plot height rather than the outer canvas height
- the selection box bottom does not extend below the plotted y-axis region
- dragging left cannot move the selected window before `0 s`
- dragging right cannot move the selected window past the latest valid full-duration position
- when no valid full-duration interval exists inside the captured/plot intersection, the selection box stays hidden
- when plot geometry or rendered payload is temporarily unavailable, the selection box stays hidden and drag attempts do not change `selected_window`
- after valid geometry returns, the same logical selected window re-renders inside the plot bounds

## Files Likely Affected

- `ui/modules/stft.py`
- `ui/widgets/analysis_charts.py`
- `tests/test_stft_module.py`

## Non-Goals

This design does not introduce:

- new controller states
- new review-window semantics
- new STFT data contracts
- changes to the analysis result payload

## Recommendation

Fix the STFT selection box by making the rendered STFT plot area the only geometry source for both overlay drawing and drag mapping.

This is the smallest correct fix because it addresses the real source of the mismatch rather than tuning the existing fixed-margin approximation.
