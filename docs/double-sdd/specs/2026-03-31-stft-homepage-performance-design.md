# STFT Homepage Performance Design

## Summary

The recent STFT correctness work fixed missing leading frames and exact time alignment, but it also made the homepage STFT surface much heavier.

Two operator-visible performance defects must now be corrected:

- the homepage becomes laggy during recording because the STFT widget repaints too often
- the homepage remains laggy when switching away and back after recording because the STFT widget rebuilds a large cached heatmap too expensively

This pass keeps the exact full-resolution STFT timeline while redesigning only the homepage display/cache path.

## Problem

### Excessive Live Repaint Frequency

The live STFT path now emits every newly available STFT column instead of only the newest column from each chunk.

That fixed correctness, but it also means the homepage STFT widget may receive multiple `stft_updated` frames for one 50 ms recorder chunk. Depending on `stft_nfft`, this can produce dozens or hundreds of full heatmap redraws per second.

This is acceptable for data correctness but not for homepage responsiveness.

### Expensive Revisit / Refresh Path

The homepage STFT cache currently stores many separate magnitude columns and rebuilds the full image matrix with `np.column_stack(...)` when rendering.

As a result:

- returning to the homepage can trigger a heavy matrix rebuild
- refreshing the selected display channel can trigger the same rebuild again
- the widget also reapplies `setImage(..., autoLevels=True)` on each render path

This is also incorrect for the intended UX. STFT is now only a display and selection surface, so post-recording and revisit behavior must be responsive even if live display is somewhat less immediate.

## Scope

In scope:

- homepage STFT cache representation in `ui/modules/stft.py`
- homepage STFT repaint policy in running/analyzing/reviewing/reanalyzing phases
- homepage STFT revisit / channel-switch refresh behavior
- regression tests for cache reuse and repaint coalescing

Out of scope:

- STFT analysis correctness in the analysis pipeline
- FFT / SPL / octave / AI results
- STFT selection-box geometry rules
- review-window business rules
- display downsampling or lossy STFT decimation
- unrelated homepage layout or styling changes

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - exact full-resolution STFT data available for observation and box selection after recording
  - existing `stft_updated` bus signal signature
  - existing review-window and reanalysis semantics
  - existing time-axis correctness for the STFT view
- Allowed breakage:
  - current immediate repaint-on-every-frame behavior
  - current reliance on rebuilding the STFT image matrix from separate cached columns on refresh
  - current repeated auto-level recalculation on every live refresh
- Migration strategy: none

## Approved Decisions

1. The homepage STFT view may optimize display independently from analysis as long as final full-resolution STFT data remains exact.
2. STFT display data must not be downsampled.
3. During recording, the STFT widget may skip intermediate visible paints if needed.
4. The exact full-resolution STFT timeline must still be retained.
5. After recording, switching away from the homepage and back must render from existing cached STFT matrix data without matrix reconstruction from per-column history.
6. The STFT selection box must continue to map against the true full recording timeline.
7. No compatibility mode is required for the old repaint-heavy behavior.
8. Live frame-driven STFT repaint for an unchanged render key must be capped at one visible refresh per 50 ms at most.
9. The STFT module, not the chart widget, owns live refresh scheduling and pending-render state.
10. Reviewing and reanalyzing must render immediately from the exact cached matrix rather than using live repaint throttling.

## Root Cause Summary

### Data Retention Shape

`_StftCacheEntry` in `ui/modules/stft.py` currently stores:

- `times_sec` as a growing Python list
- `magnitude_columns` as a growing list of 1-D NumPy arrays

When rendering, `image_payload()` rebuilds the full image matrix using `np.column_stack(self.magnitude_columns)`.

That makes refresh and revisit cost grow with the full run history.

### Render Trigger Policy

`StftModule._on_stft_updated(...)` currently refreshes the visible STFT image immediately for each accepted frame.

After the March 31 STFT correctness fix, one recorder chunk may yield multiple `stft_updated` emissions. The UI therefore performs many more full refreshes than before.

### Image Update Cost

`StftImageChartWidget.set_image(...)` currently calls `ImageItem.setImage(..., autoLevels=True)` on each render path. That forces repeated image-level work even when only a small amount of new STFT data arrived.

## Design Summary

Keep the exact STFT timeline but redesign the homepage cache and repaint contract.

For cache representation:

- the STFT module should keep one exact render-ready matrix per `(config_id, run_id, channel_index)` instead of a list of separate columns that must be rebuilt later
- appending new STFT frames should update that matrix incrementally
- revisit and channel-switch refresh paths should reuse the existing matrix directly

For repaint behavior:

- incoming frames still update the exact cache immediately
- visible repaint should be coalesced to a capped refresh cadence during live phases
- after recording, the full exact matrix should be displayed directly without reconstruction churn

This keeps the corrected STFT timeline while removing the current UI-side rebuild storm.

## Definitions

- `render key`: the currently displayed `(config_id, run_id, channel_index)` tuple for the homepage STFT widget
- `active STFT surface`: the one visible homepage STFT widget instance
- `live frame-driven refresh`: a repaint caused only by incoming `stft_updated` frames for the current render key while the phase is `running` or `analyzing`
- `refresh boundary`: an explicit non-frame event that may force an immediate render, limited to selected-channel change, page re-entry to the test/home page, phase transition into `reviewing` or `reanalyzing`, runtime context change to a new run, or display reset

## Homepage STFT Cache Contract

### Exact Data Retention

Required behavior:

- the cache for one `(config_id, run_id, channel_index)` must still represent the full exact STFT timeline that reached the UI
- no STFT columns may be dropped from the retained cache
- no time-axis decimation may be introduced

Allowed optimization:

- the cache may change internal storage shape from `list[np.ndarray]` to an appendable 2-D matrix or equivalent render-ready representation

Frequency-axis invariant:

- under normal operation, one cache entry keeps one stable frequency axis for its whole lifetime
- the current STFT pipeline/config contract is expected to satisfy that invariant for one `(config_id, run_id, channel_index)`
- defensive fallback for violated invariant: the module must reject the mismatched frame, keep the last valid cache entry intact, and avoid mixing different frequency axes into one matrix
- the exact full-timeline guarantee applies to valid runs that satisfy the stable-axis invariant above

### Render-Ready Representation

Required behavior:

- the STFT cache must expose a render-ready image payload without rebuilding the full matrix from per-column history on every refresh
- channel switch, homepage revisit, and explicit refresh paths must reuse the stored matrix directly
- matrix growth may allocate new arrays when needed, but refresh-only code paths must not rebuild the matrix from raw column lists
- refresh/revisit paths must not deep-copy the full retained STFT matrix before handing it to the image-widget render path

Implementation rule:

- the cache entry used by refresh/revisit paths must expose one stored render-ready exact matrix for the retained timeline
- internal over-allocation is allowed, but refresh/revisit paths must consume a direct matrix view from that stored cache entry rather than assembling from segmented historical blocks
- the observable contract is no reconstruction from per-column history and no full-matrix deep copy on refresh/revisit paths

### Cache Reuse

Required behavior:

- `refresh_selected_display_channel()` must render from an already-built cache entry when one exists
- returning to the homepage after viewing another page must render from the existing exact cache for the active run/channel
- cached STFT data must still clear on the existing display-reset / run-reset paths
- one refresh boundary may cause at most one image-widget update for the active STFT surface
- if the current render key has no cache entry or no valid cached data, the active STFT surface must clear instead of preserving an old image or falling back to a different channel

## Live Repaint Contract

### Coalesced Refresh

Required behavior:

- `stft_updated` frames may arrive more often than the widget repaints
- incoming frames must update the exact cache immediately
- visible repaint during `running` and `analyzing` must be coalesced to a capped refresh policy rather than forcing one redraw per frame
- the cap is one visible refresh per 50 ms at most
- the 50 ms cap applies only to live frame-driven refreshes for an unchanged render key

Allowed behavior:

- intermediate live paints may be skipped
- the next visible repaint may include multiple accumulated new STFT frames at once

Scheduling rule:

- `StftModule` should maintain a pending-refresh flag and a single scheduled live flush for the active render key
- if more frames arrive before that scheduled flush runs, they must be merged into the same eventual repaint
- the design does not require one specific Qt primitive, but the scheduling contract must be externally observable through one explicit module-owned flush path
- pending live flush state must be keyed to the active render key; if the render key changes before the flush runs, the stale flush must be canceled or ignored
- display/run reset must clear pending live flush state so no stale render occurs after cache reset
- immediate renders caused by refresh boundaries are explicit exceptions to the 50 ms live frame-driven cap and may render synchronously once per boundary
- if a refresh boundary forces an immediate render for the same render key while a live flush is still pending, that pending live flush must be canceled or consumed by the boundary render so the active STFT surface does not render twice for the same accumulated state

### Live Phase Priority

Required behavior:

- performance priority applies mainly to `running` and `analyzing`
- lower live smoothness is acceptable if it prevents the homepage from becoming obviously laggy
- `reviewing` and `reanalyzing` are not live-throttled phases; they must render from the exact cached matrix immediately
- switching the selected display channel during live phases may bypass the pending live flush and render the newly selected channel immediately from its current exact cache

### Catch-Up Behavior

Required behavior:

- when recording/analysis finishes, the widget must be able to show the exact full-resolution cached STFT timeline
- the final displayed STFT must include all retained frames, not only the last visibly repainted subset from live mode
- entering `reviewing`
  or showing a completed cached run on homepage revisit must force an immediate flush of any pending live refresh so the exact final matrix is shown without waiting for the live timer
- changing the selected display channel outside live throttled phases must also render immediately from the cached matrix

### Homepage Revisit Integration

Required behavior:

- `MainWindow` must treat re-entry to the test/home page as an explicit refresh boundary
- when the stacked main page changes back to the test page, `MainWindow` must call the existing analysis-display refresh path so `StftModule` can render from cache immediately
- that refresh path must reuse the exact cached matrix for the active run/channel rather than rebuilding history from raw frame lists
- if no valid cache exists for the current render key at page re-entry time, the STFT widget must clear immediately

## Image-Level Rendering Contract

Required behavior:

- repeated renders of the same cache entry should avoid unnecessary expensive image work
- auto-level recalculation must not run on every live update
- if color-level recalculation is needed, it may happen only on first render of a cache entry, cache reset, or render-key change instead of every incoming frame

Ownership:

- `StftModule` owns cache state, pending-refresh state, and the decision of when a render should occur
- `StftImageChartWidget` owns only image-widget rendering details for a payload it is given
- color-level reuse may live in the chart widget, but the refresh cadence decision must stay in `StftModule`

Non-goal:

- preserving the current exact live auto-level behavior frame-by-frame is not required

## Selection / Review Integrity

Required behavior:

- STFT time-axis values used for rendering must remain exact
- selection-box positioning and review-window mapping must continue to use the true full timeline
- this optimization must not change the already-fixed March 31 selection/timeline correctness

## Testing Strategy

Add or update regression tests for:

- STFT cache entries expose a render-ready exact matrix without requiring `column_stack(...)` reconstruction on refresh paths
- multiple `stft_updated` emissions can accumulate before one visible refresh while the cache still retains all exact columns
- live repaint is capped to one scheduled flush per 50 ms window, using a deterministic module-level flush seam instead of wall-clock timing assertions
- `refresh_selected_display_channel()` reuses an existing render-ready cache instead of rebuilding from raw frame history
- homepage revisit paths render from the existing cache and keep the exact full timeline
- final review STFT still contains the complete exact timeline after live coalescing
- frequency-axis mismatch rejects the bad frame and leaves the last valid cache entry/render state intact instead of attempting mixed-axis matrix reuse
- revisit and selected-channel refresh boundaries cause at most one image-widget update for the active STFT surface
- live updates do not trigger image-widget auto-level recalculation on every frame
- existing March 31 STFT correctness regressions remain green

Verification should include:

- targeted `tests/test_stft_module/test_live_review_rendering.py`
- any related `tests/test_main_window.py` or chart-widget regressions affected by the refresh contract
- the full `pytest` suite before completion

## Files Likely Affected

- `ui/modules/stft.py`
- `ui/widgets/analysis_charts.py`
- `app/main_window.py`
- `tests/test_stft_module/test_live_review_rendering.py`
- `tests/test_main_window.py`

## Non-Goals

This design does not introduce:

- STFT display downsampling
- changes to analysis outputs or judgment logic
- changes to FFT / SPL / octave cards
- changes to STFT selection-box business rules
- new external settings for STFT refresh rate or cache mode
