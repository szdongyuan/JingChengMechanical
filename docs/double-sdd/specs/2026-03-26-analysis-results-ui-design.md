# Analysis Results UI Design

## Context

The March 20 factory-mode analysis UI spec already defined the factory runtime contract:

- one active config at a time
- one displayed config at a time
- STFT as the canonical review surface
- a fixed-duration post-recording analysis box
- explicit `分析` action for manual re-analysis

The test page still does not satisfy the operator-facing part of that design:

- `STFT` / `SPL` / `FFT` / `倍频程` are still placeholder cards
- the result card is AI-detail-heavy instead of operator-summary-oriented
- there is no shared display-channel control for multi-channel configs
- there is no committed chart payload for post-analysis rendering

This spec upgrades the existing test-page cards into real result views without changing the March 20 factory controller model.

Control-timing clarification: the later spec `2026-03-27-factory-post-analysis-controls-design.md` supersedes this document for when manual `分析` becomes eligible and when the STFT selection box is visible or movable after automatic analysis.

## Scope

This spec covers only the existing test-page analysis/result cards and the runtime data contract they need.

In scope:

- replace placeholder `STFT` / `SPL` / `FFT` / `倍频程` cards with real result views
- keep `STFT` live during recording for one selected display channel
- keep manual re-analysis anchored to the full-timeline `STFT` view and fixed-duration box
- add one shared analysis-channel selector for the four analysis cards
- redesign the result summary card into a compact operator summary
- define the committed post-analysis payload needed to render charts after default analysis or manual re-analysis

Out of scope:

- factory pair selection or trigger routing changes
- PLC protocol or hardware work
- separate popup analysis windows or a separate analysis page
- history browsing, comparison view, or multiple simultaneous result panes
- changing the configured meaning of `analysis_duration` or `analysis_window_offset_ms`

## Compatibility / Migration

- Backward compatibility: not required
- Protected surfaces:
  - the March 20 factory-mode workflow semantics
  - saved config meaning for `analysis_duration`, `analysis_window_offset_ms`, and `selected_channels`
  - the existing single-active-session factory runtime model
- Allowed breakage:
  - placeholder-chart internals
  - current result-card row layout and AI-detail presentation
  - factory review payload shape may be extended for summary/chart rendering
- Migration strategy: none

This is a new project branch. No compatibility layer or schema-preservation work is required just to preserve placeholder UI behavior.

## Approved Decisions

1. This work upgrades the existing test-page cards. It does not introduce a separate analysis page or popup workflow.
2. Only `STFT` needs live display during recording.
3. After automatic analysis finishes and the page enters `reviewing`, the operator may move an analysis box on the `STFT` timeline and click `分析` for re-analysis, subject to the later March 27 control-timing clarification.
4. The analysis-box duration remains fixed by config `analysis_duration`; the operator may move it but may not resize it.
5. During box dragging, committed `SPL` / `FFT` / `倍频程` results do not update immediately.
6. Manual re-analysis replaces the committed result set only after the operator clicks `分析`.
7. `STFT` must keep showing the full recording timeline in review; it must not be cropped to only the committed window.
8. A shared analysis-channel selector is allowed because it is cost-effective in the current architecture.
9. The shared selector applies only to the four analysis cards.
10. The shared selector remembers the last selected display channel per config.
11. After the displayed config changes, the UI restores that config's remembered display channel when possible; otherwise it falls back to the first selected channel.
12. The result summary card is not channel-selected. It stays config-level worst-case across all selected channels.
13. The result summary card becomes a compact operator summary instead of an AI-detail table.
14. The summary table columns are `项目 | 偏差值 | 通道 | 结果`.
15. The summary rows are `SPL`, `FFT`, `倍频程`, and `AI分析`.
16. `FFT` and `倍频程` show only the maximum deviation, not one row per configured band.
17. Passing rows show remaining margin as a negative deviation, for example `-3 dB` or `-10`.
18. Failing rows show exceeded deviation as a positive value, for example `6 dB` or `10`.
19. If an analysis type has no configured constraint, it still appears as qualified, for example `AI分析 | -- | -- | OK`.
20. `SPL` / `FFT` / `倍频程` summary rows show the channel that caused the worst-case deviation using the same canonical naming used on the home page: `CH1`, `CH2`, etc.
21. `AI分析` may show a channel only when the AI backend explicitly returns one; the UI must not invent an AI channel.
22. If the AI backend does not provide channel identity, the AI row shows `--` in the `通道` column.
23. `STFT` review data is owned only by cached live `stft_updated` frames plus review-window metadata. The committed payload must not carry a second competing `STFT` data source.
24. If rule analysis succeeds but AI is enabled and AI execution fails, times out, returns invalid output, or provides no usable score, `SPL` / `FFT` / `倍频程` rows still render normally, `AI分析` shows `无结果`, and the top final-result badge shows `无结果`.
25. The result card display is driven by `summary_payload`, not re-derived ad hoc from `judgment_payload`.

## Design Summary

Add a shared analysis-display layer on top of the existing factory review contract:

- keep the existing live `stft_updated` stream for recording-time display
- add a committed review payload that contains row-ready summary data and per-channel chart data for `SPL` / `FFT` / `倍频程` for the final selected analysis window
- let `STFT` own the full recording timeline and review-box interaction
- let `SPL` / `FFT` / `倍频程` render only committed post-analysis results
- keep the result summary card focused on worst-case operator decisions rather than raw AI metadata

The recommended charting approach is to add `pyqtgraph` and use it consistently for all four analysis cards. The repo currently has no charting dependency, and this task now requires real heatmap/curve/bar rendering rather than custom placeholder painting.

## Test Page Layout

Keep the current 3x2 card layout:

- row 1 col 1: `结果摘要`
- row 2 col 1: `视频预览`
- row 1 col 2: `STFT`
- row 1 col 3: `FFT`
- row 2 col 2: `SPL`
- row 2 col 3: `倍频程`

Add one compact analysis-toolbar row above the four analysis cards on the right side. That toolbar owns:

- shared `analysis channel` selector
- optional label text for the currently displayed config/channel context

The toolbar is visible only when there is a displayed config in `running`, `analyzing`, `reviewing`, or `reanalyzing`. If the displayed config has only one selected channel, the selector is hidden or disabled and that channel is used automatically.

## Analysis Channel Selector

### Purpose

The selector controls only which channel the analysis cards render:

- `STFT`
- `SPL`
- `FFT`
- `倍频程`

It does not affect:

- factory summary selection state
- result summary card rows
- overall pass/fail judgment
- the actual channels analyzed by the backend

### Behavior

- During `running`, the selector controls which channel's live `STFT` stream is visible.
- During `analyzing`, the selector may remain visible but only committed charts render after analysis completes.
- During `reviewing` and `reanalyzing`, the selector switches the four analysis cards together.
- Selector choice is remembered per `config_id`.
- If the remembered channel is not present in the newly displayed config's `selected_channels`, fall back to the first selected channel.

### Canonical Channel Label

The display label for channels in this spec is the same canonical home-page naming already used by the workorder summary:

- `CH1`
- `CH2`
- `CH3`
- ...

Channel-summary text and result-card `通道` values must use the same convention to avoid ambiguity.

## Rendering Rules

### `STFT`

`STFT` is the only chart that renders live data during recording.

Rendering contract:

- in `running`, render the chosen display channel's live full-timeline STFT accumulation
- in `analyzing`, keep the run context but do not invent a committed result early
- in `reviewing`, keep showing the full recording timeline and overlay the fixed-duration analysis box when a valid manual review window exists, per the later March 27 control-timing clarification
- in `reanalyzing`, keep the same full timeline and box visible but lock box movement until re-analysis finishes

`STFT` is never cropped to only the committed analysis window. The selected window is shown as an overlay because the operator may re-select and analyze again.

### `SPL`, `FFT`, and `倍频程`

These cards do not try to simulate real-time analysis during recording.

Rendering contract:

- in `running` and `analyzing`, remain empty
- after automatic analysis succeeds, render committed chart data for the selected display channel
- after manual re-analysis succeeds, replace the committed chart data for the selected display channel
- while the operator drags the `STFT` analysis box, keep showing the last committed result until `分析` is clicked
- when re-analysis begins, clear these three cards immediately

### Result Summary Card

The result card becomes a compact decision summary rather than a detail inspector.

Top area:

- keep the large overall badge, and allow these display texts depending on card state:
  - `-`
  - `分析中`
  - `PASS`
  - `FAIL`
  - `无结果`

Table area:

- columns: `项目 | 偏差值 | 通道 | 结果`
- rows: `SPL`, `FFT`, `倍频程`, `AI分析`

The summary card always reflects the config-level worst-case view across all selected channels. It does not follow the shared analysis-channel selector.

The row `结果` column is allowed to show:

- `OK`
- `NG`
- `无结果`

## Result Summary Semantics

### Row Selection

#### `SPL`

Choose one worst-case row from all selected channels that have an SPL limit configured.

- If any selected channel fails SPL, choose the channel with the largest positive SPL deviation.
- If all selected channels pass SPL, choose the channel with the smallest remaining margin to failure.
- If no SPL constraint is configured anywhere relevant, show `SPL | -- | -- | OK`.

#### `FFT`

Choose one worst-case row from all configured FFT bands across all selected channels.

- If any FFT band fails, choose the band/channel pair with the largest positive deviation.
- If all configured FFT bands pass, choose the band/channel pair with the smallest remaining margin.
- If no FFT band limit is configured, show `FFT | -- | -- | OK`.

Only the maximum deviation is displayed. The table does not list every FFT band.

#### `倍频程`

Choose one worst-case row from all configured octave bands across all selected channels.

- If any octave band fails, choose the band/channel pair with the largest positive deviation.
- If all configured octave bands pass, choose the band/channel pair with the smallest remaining margin.
- If no octave band limit is configured, show `倍频程 | -- | -- | OK`.

Only the maximum deviation is displayed. The table does not list every octave band.

#### `AI分析`

If AI is unconstrained, not configured, or not enabled:

- show `AI分析 | -- | -- | OK`

If AI has a configured threshold and an actual score:

- compare score against threshold
- show the difference as the deviation
- mark `NG` when below threshold
- mark `OK` when above or equal to threshold

The AI `通道` column is backend-driven:

- if the AI backend returns one channel identity for the result and that identity maps cleanly to a selected channel, show the canonical `CHn` label
- otherwise show `--`

The UI must not infer an AI channel by itself.

If AI is enabled but execution fails, times out, returns invalid output, or does not produce a usable score:

- show `AI分析 | -- | -- | 无结果` unless the backend explicitly provides a usable channel identity
- if a usable backend-provided channel identity exists, map it to canonical `CHn` naming and show it in the `通道` column
- force the top overall badge to `无结果`
- keep successful rule-analysis rows visible

### Deviation Calculation

Deviation is normalized centrally, not separately inside each widget.

#### Lower-bound check

For checks like `actual >= threshold`:

- failing case: `threshold - actual`, positive
- passing case: `actual - threshold`, then display as a negative margin

Example:

- threshold `60`, actual `50` -> `10`, `NG`
- threshold `60`, actual `63` -> `-3`, `OK`

#### Bounded check

For checks like `lower <= actual <= upper`:

- below lower -> `lower - actual`, positive
- above upper -> `actual - upper`, positive
- within range -> `-min(actual - lower, upper - actual)`

This makes passing rows directly communicate remaining safety margin and failing rows directly communicate how far the result exceeded the limit.

### Units

- `SPL`: deviation display uses `dB`
- `FFT`: deviation display uses `dB`
- `倍频程`: deviation display uses `dB`
- `AI分析`: no forced unit; display the numeric threshold difference directly

If a deviation is exactly on the constraint boundary, `0` is valid and still counts as `OK`.

## Runtime Data Contract

The runtime needs two different data flows:

1. live stream for recording-time `STFT`
2. committed result payload for post-analysis rendering

### Existing Live Stream

Keep the existing per-chunk `stft_updated` signal as the source of live `STFT` frames during recording.

The `STFT` module preserves these frames into review state so the full timeline remains visible after recording ends.

This is the only source of truth for rendered `STFT` review data. The committed review payload must not duplicate full `STFT` matrices.

### Committed Review Payload

Extend the factory review contract with two additional payloads:

- `summary_payload`
- `chart_payload`

Keep the existing `judgment_payload` as the raw judgment result contract.

Recommended split:

- `judgment_payload`: raw analysis/judgment result for application logic
- `summary_payload`: row-ready operator summary and overall display authority for the result card
- `chart_payload`: per-channel committed chart data for `SPL` / `FFT` / `倍频程`

This keeps display logic out of the UI modules that should only render already-normalized data.

### `summary_payload`

Recommended shape:

- `overall_status`: `pass`, `fail`, or `no_result`
- `overall_label`: `PASS`, `FAIL`, or `无结果`
- `error_message`: operator-facing error text when `overall_status == no_result`; otherwise `None`
- `rows`: exactly four rows in fixed order: `SPL`, `FFT`, `倍频程`, `AI分析`

Each row should include enough information for direct rendering:

- `item_key`
- `label`
- `deviation_value`
- `deviation_display`
- `channel_index`
- `channel_label`
- `status` (`OK` / `NG` / `无结果`)
- `constrained`

`deviation_display` may be `--` when unconstrained.

`summary_payload` is always the authoritative source for the result-card badge and row table. The result card must not decide its own top-level status by re-interpreting `judgment_payload`.

`summary_payload` is also required in review-time no-result states:

- if rule-analysis rows are available but final-result display is `无结果` because AI failed, include those rule rows plus an `AI分析` row with `无结果`
- if no usable rule-analysis result exists at all, include four `无结果` rows so the card can distinguish this state from the normal pre-run empty state

For any `summary_payload` whose `overall_status == no_result`, `error_message` must be non-empty. If the upstream runtime does not provide one, the normalizer must fill a fallback operator-facing message such as `分析结果不可用`.

### `chart_payload`

Recommended shape:

- keyed by channel index
- each channel entry contains render-ready datasets for:
  - `spl`
  - `fft`
  - `octave`

Per-channel chart content:

- `spl`
  - `times_sec: list[float]`
  - `values_db: list[float]`
  - `lower_limit_db: float | None`
  - `upper_limit_db: float | None`
- `fft`
  - `frequencies_hz: list[float]`
  - `levels_db: list[float]`
  - `limit_bands: list[dict]`
  - each limit-band dict includes:
    - `freq_hz`
    - `lower_db`
    - `upper_db`
    - `actual_db`
    - `passed`
- `octave`
  - `center_freqs_hz: list[float]`
  - `levels_db: list[float]`
  - `limit_bands: list[dict]`
  - each limit-band dict includes:
    - `freq_hz`
    - `lower_db`
    - `upper_db`
    - `actual_db`
    - `passed`

Widget responsibilities are intentionally limited:

- widgets may choose scales, colors, axes, and marker styles
- widgets do not recompute limits, worst-case selection, or pass/fail semantics
- widgets consume named arrays and limit-band records directly from `chart_payload`

`chart_payload` may be absent in no-result review states. `STFT` review remains available independently from cached live frames.

### Re-analysis Replacement Rules

- After automatic analysis succeeds, emit committed `summary_payload` and `chart_payload`.
- After manual re-analysis succeeds, replace the committed `summary_payload` and `chart_payload`.
- While the operator moves the `STFT` box before clicking `分析`, do not mutate the committed payload.
- When manual re-analysis starts, clear the committed summary and committed non-`STFT` charts immediately.
- If manual re-analysis fails or returns no usable result, keep the page in no-result review state and do not restore the previous committed payload.

While a run or re-analysis is actively in progress and no committed `summary_payload` has been emitted yet, the result card is not in a no-result state. It is in an in-progress state:

- badge text: `分析中`
- no operator error message
- table rows use the normal cleared placeholder state

## UI State Model

This spec does not replace the March 20 factory controller. It only adds analysis-display state on top of it.

Recommended UI-owned state:

- `display_channel_by_config_id: dict[int, int]`
- `active_chart_payload`: the latest committed `SPL` / `FFT` / `倍频程` chart payload for the displayed config/run
- `active_summary_payload`: the latest committed summary payload for the displayed config/run
- `live_stft_cache`: preserved full-timeline STFT frames for the displayed run, keyed by config/run/channel, and used as the single `STFT` review source

The UI should continue using the existing factory controller state to decide:

- which config is active
- which config is displayed
- whether the page is `running`, `analyzing`, `reviewing`, or `reanalyzing`

## Error Handling

### Result Card State Contract

The result card has five distinct display states:

#### 1. Empty pre-run state

Used before any accepted run is displayed or after display reset for the next run.

- top badge shows no result text, e.g. `-`
- no operator error message is shown
- the table is in its normal empty placeholder state

#### 2. In-progress state

Used after a run has been accepted or re-analysis has been requested, but before a committed `summary_payload` or no-result `summary_payload` has been emitted.

- top badge shows `分析中`
- no operator error message is shown
- the table is in its normal cleared placeholder state

This state applies during:

- recording before automatic analysis finishes
- automatic analysis in progress
- manual re-analysis in progress after the old committed result has been cleared

#### 3. Completed result state

Used when committed `summary_payload.overall_status` is `pass` or `fail`.

- top badge shows `PASS` or `FAIL`
- no operator error message is shown
- the four rows render from committed `summary_payload.rows`

#### 4. Partial no-result state

Used when some rule-analysis data exists but the final displayed result is `无结果`, such as enabled AI failure after successful rule analysis.

- top badge shows `无结果`
- `error_message` is shown in a visible operator-facing text area on the card
- `SPL` / `FFT` / `倍频程` rows render their available values normally
- `AI分析` renders as `无结果`

#### 5. Full no-result state

Used when no usable committed rule-analysis result exists, such as automatic window coverage failure or re-analysis failure before a result is produced.

- top badge shows `无结果`
- `error_message` is shown in a visible operator-facing text area on the card
- all four rows render as `-- | -- | 无结果`

These no-result states are explicitly different from the normal empty pre-run state.

### Unconstrained Analysis Types

If an analysis type has no configured constraint:

- still show the row in the summary card
- use `--` for deviation
- use `--` for channel
- mark the row `OK`
- do not let that row contribute to overall failure

### Automatic Analysis Window Not Covered

If automatic analysis fails because the configured default window is not fully captured:

- emit a full no-result `summary_payload`
- do not emit committed `chart_payload`
- keep the full-timeline `STFT` review context visible
- allow manual re-analysis only when one full valid fixed-duration window still fits in captured audio
- keep `SPL` / `FFT` / `倍频程` and the result summary in no-result state until a successful manual re-analysis occurs

### Manual Re-analysis Failure

If manual re-analysis fails or produces no usable result:

- emit a full no-result `summary_payload`
- do not emit committed `chart_payload`
- keep the page in no-result review state
- keep the `STFT` full timeline and selection context visible
- do not restore the previous committed summary/chart set

### Enabled AI Failure With Valid Rule Analysis

If rule analysis succeeds but enabled AI execution fails, times out, returns invalid output, or produces no usable score:

- emit a partial no-result `summary_payload`
- keep committed `SPL` / `FFT` / `倍频程` chart data available
- render `SPL` / `FFT` / `倍频程` rows normally
- render `AI分析` as `无结果`
- render the top final-result badge as `无结果`
- surface the AI failure message in `summary_payload.error_message`

### Channel Selector Edge Cases

If the displayed config changes and the remembered channel is no longer valid:

- fall back to the first selected channel

If committed chart data does not contain the remembered channel:

- fall back to the first channel present in committed payload

If the AI backend returns channel metadata that cannot be mapped cleanly to one selected channel:

- show `--` for the AI `通道` cell

## Files Likely Affected

The exact breakdown belongs in the implementation plan, but this design points to these areas:

- `app/services_container.py`
- `app/service/analysis/test_session.py`
- `app/service/analysis/test_manager.py`
- `ui/modules/result.py`
- `ui/modules/stft.py`
- `ui/modules/spl.py`
- `ui/modules/spectrum.py`
- `ui/modules/octave.py`
- `app/main_window.py`
- `tests/test_result_module.py`
- `tests/test_stft_module.py`
- `tests/test_main_window.py`
- new targeted tests for committed payload normalization and chart rendering contracts

The implementation may introduce one or more focused helper classes or dataclasses for:

- summary-row normalization
- chart-payload transport
- shared analysis-channel display state

## Testing Strategy

### Summary Normalization Tests

- lower-bound deviation math for pass/fail cases
- bounded-range deviation math for pass/fail cases
- exact-boundary behavior (`0` still `OK`)
- worst-case channel selection for `SPL`
- worst-case band/channel selection for `FFT`
- worst-case band/channel selection for `倍频程`
- unconstrained-row rendering as `-- / -- / OK`
- AI row channel shown only when backend explicitly provides one

### UI State Tests

- selector visibility for one-channel vs multi-channel configs
- remembered display channel restored per config
- invalid remembered channel falls back safely
- result card ignores selector and stays config-level worst-case
- `STFT` remains full timeline in review and re-analysis states
- `SPL` / `FFT` / `倍频程` remain empty during recording

### Re-analysis Flow Tests

- moving the `STFT` box does not mutate committed result views
- clicking `分析` clears committed non-`STFT` views immediately
- successful re-analysis replaces committed summary/charts
- failed re-analysis leaves the page in no-result review state and does not restore prior committed results

### Integration Tests

- automatic analysis emits committed summary/chart payloads
- manual re-analysis emits replacement summary/chart payloads
- channel selector switches committed charts together
- overall badge follows `summary_payload.overall_status` correctly, including partial and full no-result states
- result-summary channel labels use canonical `CHn` naming

## Non-Goals

This design does not introduce:

- concurrent dual-config chart display
- side-by-side result comparison
- a second analysis workflow outside the `STFT` review box
- config-owned "display channel" persistence
- compatibility branches for the old placeholder result card

## Recommendation

Implement the analysis results UI as a focused upgrade to the existing test-page cards:

- use `pyqtgraph` for all four analysis cards
- keep `STFT` as the only live chart and as the canonical review surface
- add a shared per-config-remembered display-channel selector for the four analysis cards
- redesign the result card into a worst-case operator summary
- extend the factory review payload with explicit summary/chart data instead of forcing each widget to reverse-engineer display state from raw judgment data

This keeps the March 20 factory-mode contract intact while finally making the operator-facing results UI real.
