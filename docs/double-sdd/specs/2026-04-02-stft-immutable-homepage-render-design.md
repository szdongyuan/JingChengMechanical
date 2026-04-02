# STFT Immutable Homepage Render Design

## Summary

The homepage STFT image should be treated as immutable for one `(config_id, run_id, channel_index)` after a run's STFT data has been captured. Returning to the homepage, entering review again, or triggering manual reanalysis for the same run must not resubmit the same large STFT matrix to the heatmap widget.

This pass keeps the current cached STFT data model and overlay behavior, but removes redundant image redraws for unchanged STFT content.

## Context

The March 31 STFT homepage performance pass already fixed two expensive behaviors:

- refresh paths no longer rebuild the full matrix from per-column history
- live updates are coalesced instead of repainting on every frame

However, one operator-visible lag source still remains:

- `StftModule` still calls the heatmap render path on page re-entry, review-state churn, and reanalysis-state churn even when the displayed STFT image is identical

The user clarified the intended product rule:

- STFT is not recomputed for an existing run
- unless a brand new recording session starts, or the image is explicitly cleared/reset, the STFT image should remain unchanged

That means repeated homepage access should reuse the already rendered image, not redraw it from cached data.

## Scope

In scope:

- homepage STFT redraw policy for unchanged cached STFT content
- page re-entry refresh behavior on the main test/home page
- review and reanalysis state transitions for the same run
- regression tests that prove redundant redraws no longer happen

Out of scope:

- STFT analysis correctness or recomputation rules in the analysis pipeline
- live STFT cache retention shape
- STFT selection-box geometry rules
- FFT / SPL / octave / loudness cards
- unrelated main-window layout or navigation changes

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces:
  - visible STFT image content for a given `(config_id, run_id, channel_index)`
  - existing page re-entry refresh flow in `MainWindow`
  - existing STFT review selection-box and analyze-button behavior
  - existing live STFT update behavior when new frames actually arrive
  - existing display-reset and run-reset semantics
- Allowed breakage:
  - redundant `set_image(...)` calls for an unchanged STFT payload
  - incidental color-level recalculation that only happened because of those redundant redraws
- Migration strategy: none

## Root Cause Summary

`StftModule` caches STFT data by render key, but it does not currently distinguish between:

- "same render key, new STFT data arrived"
- "same render key, no STFT data changed, but a UI refresh boundary fired"

As a result, several non-data events still call the heavy image-widget render path for the same already displayed STFT image:

- `refresh_selected_display_channel()` during homepage re-entry
- `_on_factory_state_changed(...)` when review/reanalysis state changes but the run stays the same
- `_on_factory_review_changed(...)` when review metadata changes but the cached STFT content stays the same

For a large STFT matrix, resubmitting the image to the heatmap widget is still expensive enough to cause visible lag even though the matrix is already cached.

## Approaches Considered

### Approach A: Skip homepage re-entry refreshes in `MainWindow`

Pros:

- small patch surface
- directly addresses one reported lag trigger

Cons:

- does not solve redundant redraws caused by review/reanalysis state churn
- spreads STFT-specific performance behavior into `MainWindow`
- makes the optimization dependent on one caller instead of the STFT module contract

### Approach B: Make `StftModule` render idempotent for unchanged STFT revisions

Pros:

- solves page re-entry and state-churn redraws in one place
- keeps `MainWindow` free to keep calling the existing refresh path
- matches the product rule that STFT is immutable for an existing run unless cleared or replaced

Cons:

- requires explicit revision/signature tracking inside the STFT module

### Approach C: Snapshot the rendered heatmap into another image cache

Pros:

- could avoid all redraw work

Cons:

- duplicates data ownership
- adds complexity without need
- risks desynchronizing overlays and render state

## Recommended Design

Use Approach B.

The STFT module should treat redraw as a data-change operation, not as a generic UI refresh operation. If the resolved render target is the same `(config_id, run_id, channel_index)` and the cached STFT revision has not changed since the last successful render, then refresh boundaries must update overlays only and skip `heatmap.set_image(...)`.

## Design Details

### Render Revision Contract

Each `_StftCacheEntry` should expose a monotonic render revision that changes only when accepted STFT frame data changes the cached image content.

Required behavior:

- appending a valid STFT frame increments the cached revision
- rejecting an invalid/mismatched frame does not increment the revision
- the revision represents STFT image-content change, not UI-state change

### Render Signature Contract

`StftModule` should track the last successfully rendered STFT signature:

- render key: `(config_id, run_id, channel_index)`
- render revision: the cache entry revision used for the visible heatmap image

Required behavior:

- if the current render target resolves to the same key and revision, and the heatmap still has an image, the module must not call `set_image(...)` again
- if the key changes, normal render must occur
- if the revision changes, normal render must occur
- if the heatmap image has been cleared, normal render must occur even when the key and revision are unchanged

### Refresh Boundary Behavior

The existing refresh boundaries should remain in place, but their effect changes:

- homepage re-entry may still call `refresh_selected_display_channel()`
- review/reanalysis state handlers may still call the cached refresh path
- channel changes may still call the cached refresh path

But for unchanged STFT content they must become image no-ops:

- page re-entry with the same rendered key/revision should only preserve the current heatmap image and update overlay geometry
- review metadata changes for the same run should only update overlay geometry and button state unless STFT data changed
- entering `reanalyzing` and returning to `reviewing` for the same run should not redraw the unchanged STFT image

### Reset / Clear Behavior

The no-redraw optimization must not block legitimate rerender paths.

Required behavior:

- `factory_display_reset` and any explicit STFT clear path must invalidate the last-rendered signature
- starting a brand new run with a new `run_id` must force a render when data becomes available
- if the heatmap widget has no current image, refresh from cache must still restore it

### Ownership

`StftModule` owns:

- cache-entry revision tracking
- last-rendered signature tracking
- the decision of whether a redraw is necessary

`StftImageChartWidget` continues to own:

- actual heatmap rendering
- stored widget-side payload state
- geometry helpers used by selection overlays

No new render-caching layer should be introduced in `MainWindow`.

## Testing Strategy

Add or update regression tests proving:

- `refresh_selected_display_channel()` does not call `set_image(...)` again when the same key/revision is already visible
- returning to the test/home page with the same rendered STFT does not trigger another STFT heatmap redraw
- review/reanalysis state churn for the same run does not trigger another STFT heatmap redraw when no new STFT data arrived
- clearing the heatmap still allows cached STFT data to be restored on refresh
- receiving a new valid STFT frame for the same key still triggers the expected render path

Verification should include:

- targeted `tests/test_stft_module/test_homepage_performance.py`
- targeted `tests/test_main_window.py`
- any focused STFT regression tests needed for reanalysis/review transitions
- a regression proving rejected/mismatched STFT frames do not advance the cached render revision

## Files Likely Affected

- `ui/modules/stft.py`
- `tests/test_stft_module/test_homepage_performance.py`
- `tests/test_main_window.py`

## Non-Goals

This design does not introduce:

- STFT recomputation changes
- STFT display downsampling
- changes to analysis outputs or judgment logic
- special-case main-window logic that knows STFT internals
