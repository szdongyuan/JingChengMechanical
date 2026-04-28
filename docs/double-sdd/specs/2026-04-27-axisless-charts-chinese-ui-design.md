# Homepage Left Axis Removal And Chinese UI Design

## Context

The test homepage uses a fixed-width left sidebar that contains work order input,
controls, and video preview. The video preview is visually too small. The right
analysis grid still spends horizontal space on visible left-side chart axes,
including y-axis titles, tick marks, and tick labels. The requested change is to
remove those vertical-axis labels from the homepage charts so the chart area can
be narrower and more space can be reserved for the left sidebar.

The UI should also be more consistently Chinese. This is a UI-only text cleanup:
`PASS`, `FAIL`, `OK`, and `NG` must stay unchanged, and established technical
abbreviations and units may remain visible.

## Goals

1. Hide only the left-side y-axis visual content for analysis charts on the test
   homepage.
2. Keep bottom x-axes visible so users can still read horizontal time/frequency
   coordinates.
3. Preserve chart data, ranges, overlays, selection geometry, and judgment logic.
4. Move y-axis quantity and unit meaning into the homepage chart card titles.
5. Replace customer-facing English words on the homepage and result summary with
   Chinese, except approved abbreviations and status labels.
6. Leave non-UI data contracts unchanged.

## Non-Goals

- Do not hide bottom, top, or right axes as part of this request.
- Do not change analysis calculations, limit evaluation, AI judgment behavior, or
  review payload production.
- Do not change AI protocol fields, summary payload field names, database schema,
  configuration files, or import/export formats.
- Do not translate internal enum values, status codes, style properties, logs, or
  debug-only strings.
- Do not refactor unrelated layout or business logic.
- Do not add compatibility migrations; this is a UI-only presentation change.

## User-Visible Scope

### Homepage Layout

The implementation may use the space recovered from hidden left y-axes to enlarge
the homepage left sidebar, primarily benefiting the `视频预览` card. The current
left sidebar is fixed-width in `app/main_window.py`; the implementation should
increase that width conservatively without changing the overall page structure.

Acceptance criteria:

- The left sidebar is wider than before.
- `视频预览` has more horizontal space than before.
- The right chart grid remains visible and usable.
- No new page, tab, or workflow is introduced.

### Chart Card Titles

Because y-axis labels are no longer visible, homepage chart titles must carry the
y quantity and unit. The title text should stay concise to avoid consuming the
space just recovered.

Required homepage chart titles:

- `FFT频谱（声压级dB）`
- `STFT时频图（频率Hz，幅值dB）`
- `响度（sone）` when the displayed loudness payload uses `sone`
- `响度（phon）` when the displayed loudness payload uses `phon`
- `响度` when no loudness payload unit is available
- `声压级（dB）`
- `倍频程（声压级dB）`

Title semantics:

- FFT uses `levels_db`; describe the vertical quantity as `声压级dB`.
- STFT keeps the bottom time axis visible and hides the left frequency axis, so
  the title identifies the hidden vertical quantity as `频率Hz` and the color
  scale as `幅值dB`.
- Loudness title must update to the single unit of the rendered payload when
  that unit is `sone` or `phon`.
- SPL uses `values_db`; describe the vertical quantity as `声压级（dB）`.
- Octave uses `levels_db`; describe the vertical quantity as `声压级dB`.

Approved abbreviations and units that may remain visible:

- `AI`
- `PLC`
- `FFT`
- `STFT`
- `Hz`
- `dB`
- `sone`
- `phon`

Status/result labels that must remain unchanged:

- `PASS`
- `FAIL`
- `OK`
- `NG`

### Chart Axes

Only chart widgets used on the test homepage should hide their left y-axis
visual content.

Acceptance criteria:

- The left axis title is not visible.
- Left-axis tick text is not visible.
- Left-axis tick marks and axis line are not visible.
- The bottom axis remains visible.
- Existing bottom-axis labels/ticks remain usable.
- Existing range policy and viewbox coordinate mapping remain unchanged.
- Existing plotted data, limit overlays, golden overlays, markers, and STFT
  selection behavior remain unchanged.

Affected homepage chart classes/modules:

- `StftImageChartWidget` as used by `StftModule`
- `FftLineChartWidget` as used by `SpectrumModule`
- `LineChartWidget` as used by `SplModule` and `LoudnessModule`
- `OctaveBarChartWidget` as used by `OctaveModule`

The implementation must be explicit opt-in. Do not change the default visual
behavior of generic chart widgets globally. Acceptable implementation patterns
include:

- adding a `hide_left_axis: bool = False` constructor option and passing
  `hide_left_axis=True` only from homepage chart modules; or
- adding an `apply_homepage_left_axis_hidden_style()` method and calling it only
  for chart widgets used on the homepage.

The default chart widget behavior must remain unchanged unless a caller opts in.
This prevents accidental changes to unrelated pages or future chart usages.

Grid lines may remain if they do not depend on visible left-axis labels and do
not reduce readability. If hidden-axis grid behavior is visually confusing, the
implementation may disable y-grid lines for opted-in homepage charts while
keeping x-axis visibility.

### Chinese UI Display Mapping

This task includes a bounded UI-only Chinese display pass for customer-visible
homepage and result-summary text. These mappings are display normalization only;
they must not change payload producers, exported data, stored data, or protocol
field values.

Required display mappings:

- `Spectrum` -> remove from the homepage title; use `FFT频谱`
- `Spectrogram` -> remove from the homepage title; use `STFT时频图`
- `Loudness` -> `响度`
- `Octave` -> remove from the homepage title; use `倍频程`
- `SPL` when used as an explanatory row/title -> `声压级`
- `平均SPL` -> `平均声压级`
- `平均Loudness` -> `平均响度`
- `FFT` when used as a result row/title -> `FFT频谱`
- `CH1`, `CH2`, etc. in homepage controls or result-table channel cells ->
  `通道1`, `通道2`, etc.

`AI分析` may stay unchanged. The request is to reduce explanatory English, not to
rename every already-Chinese label.

## Compatibility / Migration

- Backward compatibility: partial.
- Protected surfaces:
  - AI request and response protocol fields.
  - Summary payload field names and internal status values.
  - Database schema and stored data.
  - Configuration package formats.
  - Analysis and judgment behavior.
  - Logs and debug-only strings.
- Allowed breakage:
  - Customer-visible UI text may change.
  - Homepage layout dimensions may change.
  - UI tests that assert visible labels, card titles, channel display text, or
    left-axis visibility may need updated expectations.
- Migration strategy: none. This is a UI-only presentation change.

## Design

### Left-Axis Hiding

Use an explicit opt-in chart-level helper in `ui/widgets/analysis_charts.py` so
only homepage analysis charts hide the left y-axis.

The helper should:

- hide the left axis line, tick marks, tick text, and axis title;
- leave the bottom axis visible;
- preserve all viewbox range logic and plot items;
- avoid changing payload APIs or chart data structures;
- be idempotent so modules can safely call it after clearing or re-rendering.

SPL and loudness modules should stop relying on visible left-axis label text for
homepage unit display. If internal label text remains in pyqtgraph objects,
tests and acceptance should rely on actual left-axis visibility and card titles,
not hidden label text.

### Titles And Dynamic Loudness Unit

Use the outer `Card` title as the single visible place for y quantity and unit.
This avoids adding an in-plot title that would consume plot height.

Most chart titles are static. The loudness card title must be updateable from
the rendered loudness unit so it displays exactly one unit (`sone` or `phon`) for
the current data. A minimal implementation may expose a title update method on
`Card` and let `LoudnessModule` notify `MainWindow`, or otherwise update the card
title through an existing UI-layer reference. This title update must remain
UI-only.

### UI Display Mapping

For result summary rows and channel labels, add UI-layer display normalization in
the UI modules rather than changing service payloads.

Likely touch points:

- `app/main_window.py` for homepage card titles, left sidebar width, and toolbar
  context text.
- `ui/widgets/cards.py` if a card title update API is needed.
- `ui/widgets/analysis_channel_toolbar.py` for combo-box channel labels.
- `ui/modules/result.py` for result table labels and channel display values.
- `ui/modules/loudness.py` if the rendered loudness unit must notify the owning
  homepage card.

The display mapper must not translate `PASS`, `FAIL`, `OK`, or `NG`.

## Testing Strategy

Update UI-focused tests to assert the new presentation:

- `tests/test_main_window.py`
  - homepage card titles are the new Chinese titles with y quantity and units;
  - left sidebar width is larger than the previous fixed width;
  - channel toolbar/context display uses `通道N` where applicable;
  - `PASS`, `FAIL`, `OK`, and `NG` remain unchanged where displayed;
  - loudness card title updates between `响度（sone）` and `响度（phon）`.
- `tests/test_analysis_chart_modules/test_committed_chart_modules.py`
  - SPL/loudness no longer rely on visible left-axis labels;
  - homepage chart module instances hide the left axis while preserving bottom
    axes.
- `tests/test_analysis_chart_modules/test_zero_based_axes.py`
  - default chart widgets keep their existing axis behavior without opt-in;
  - range and mapping behavior remains unchanged when left-axis hiding is
    enabled.
- `tests/test_result_module.py`
  - customer-visible explanatory row labels and channel labels are Chinese;
  - `PASS`, `FAIL`, `OK`, and `NG` remain unchanged.

Run targeted tests for the affected UI modules after implementation.

## Risks And Mitigations

- Risk: hiding the left axis changes plot geometry and breaks STFT selection
  mapping.
  - Mitigation: keep viewbox coordinate ranges unchanged and cover STFT geometry
    tests.
- Risk: translating payload producers causes protocol churn.
  - Mitigation: translate only at UI display boundaries.
- Risk: longer Chinese titles consume horizontal space.
  - Mitigation: keep titles concise and include only the y quantity/unit required
    by this spec.
- Risk: increasing the left sidebar too much makes the chart grid cramped.
  - Mitigation: increase the fixed width conservatively and verify the right grid
    remains usable.
