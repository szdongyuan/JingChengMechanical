# Result Dashboard Layout Design

## Request

The factory test page right-side result dashboard must become configurable for
the expected customer shape of at most two selected channels. The fixed left
column remains unchanged. The configurable area should let users choose which
result panels are shown, how many rows/columns the dashboard uses, and whether a
panel spans rows or columns.

The configuration should be edited from the configuration page, not by dragging
panels directly during production testing.

## Current Context

`MainWindow.build_test_page()` currently builds the right side as a hard-coded
three-column grid containing the result summary, STFT, spectrum, loudness/SPL or
vibration replacement stacks, and octave panels.

The existing chart modules consume live signals and `FactoryReviewPayload`
signals. STFT display also relies on live in-memory cache for normal runtime
tests, with imported-audio payload support for imported WAV analysis.

The data-management page currently lists, filters, plays, exports, and deletes
saved audio records. It does not open historical records back into the test-page
chart dashboard.

## Goals

- Keep the left test-page column fixed.
- Make the right-side dashboard layout configurable per test configuration.
- Support one, two, or three dashboard columns.
- Support user-added rows.
- Support panel row span and column span.
- Support hiding panels by leaving them out of the layout.
- Require exactly one `结果摘要` panel, while still allowing it to move and span.
- Allow duplicate chart panels except for `结果摘要`, for example two spectrum
  panels bound to different channels.
- Support channel binding for acoustic and vibration panels.
- Keep panel positions stable when data is missing; missing data shows an empty
  state inside the configured card.
- Limit selected channels to at most two.
- Preserve the current config-page save model: dialog confirmation updates the
  editor draft, and the configuration page save writes to the database.

## Non-Goals

- Do not change the fixed left test-page column.
- Do not add runtime drag editing on the production test page.
- Do not add historical-result opening or historical STFT persistence to the
  data-management page.
- Do not change PLC code or PLC wiring.
- Do not change hardware modules or hardware configuration behavior.
- Do not redesign individual chart drawing semantics beyond what is necessary
  to host them in configurable panel instances.
- Do not keep compatibility with old layout configuration shapes.

## Panel Catalog

The layout editor supports these panel types:

| Stored type key | Visible title | Panel family |
| --- | --- | --- |
| `result_summary` | `结果摘要` | summary |
| `stft` | `时频图` | acoustic |
| `spectrum` | `频谱` | acoustic |
| `octave` | `倍频程` | acoustic |
| `spl` | `声压级` | acoustic |
| `loudness` | `响度` | acoustic |
| `vibration_stft` | `时频图（振动）` | vibration |
| `vibration_spectrum` | `频谱（振动）` | vibration |
| `vibration_octave` | `倍频程（振动）` | vibration |
| `vibration_frequency_stability` | `频率稳定性（振动）` | vibration |
| `vibration_pulse` | `脉冲强度（振动）` | vibration |

`结果摘要` is required, unique, movable, and resizable. All other panel types are
optional and may appear multiple times.

## Channel Binding

Acoustic panels support these bindings:

| Stored channel key | Visible label | Meaning |
| --- | --- | --- |
| `primary_acoustic` | `主声学通道` | First selected channel that is not the configured vibration channel |
| `selected_1` | `通道1` | First entry in the current config's `selected_channels` list |
| `selected_2` | `通道2` | Second entry in the current config's `selected_channels` list |

`主声学通道` resolves to the first selected channel that is not the configured
vibration channel. If no acoustic channel exists, the panel shows an empty state.

`通道1` and `通道2` are selected-channel positions, not physical hardware channel
numbers. For example, if `selected_channels` is `[2, 5]`, `通道1` resolves to
channel index `2` and `通道2` resolves to channel index `5`.

Vibration panels support these bindings:

| Stored channel key | Visible label | Meaning |
| --- | --- | --- |
| `vibration` | `振动通道` | The configured `vibration_view_channel_index` |
| `selected_1` | `通道1` | First entry in the current config's `selected_channels` list |
| `selected_2` | `通道2` | Second entry in the current config's `selected_channels` list |

A vibration panel only renders vibration data when the resolved channel is the
configured `vibration_view_channel_index`; otherwise it shows an empty state.

Summary panels use `none` as the stored channel key and ignore channel binding.

The dashboard does not auto-hide or reflow panels when a bound channel is
unavailable.

## Layout Data Model

Add a `result_dashboard_layout` text field to `test_config_table`. The field
stores JSON.

The normalized layout shape is:

```json
{
  "version": 1,
  "columns": 3,
  "rows": 2,
  "panels": [
    {
      "id": "summary",
      "type": "result_summary",
      "row": 0,
      "column": 0,
      "row_span": 1,
      "column_span": 1,
      "channel": "none"
    }
  ]
}
```

Rules:

- `version` is `1`.
- `columns` is an integer from `1` to `3`.
- `rows` is an integer greater than or equal to `1`.
- `row` and `column` are zero-based.
- `row_span` and `column_span` are positive integers.
- Each panel must fit within the configured grid.
- Panel rectangles must not overlap.
- Panel IDs must be unique.
- Exactly one panel must have type `result_summary`.
- Unknown panel types or invalid channels are validation errors when saving or
  importing a config.
- Missing, blank, or null layout data is replaced by the default layout during
  config load and new config creation.
- Malformed non-empty layout data is rejected during save/import validation. If
  malformed data is already present in the database, the runtime display falls
  back to the default layout and config save continues to reject it until the
  editor writes a valid layout.

Default layout:

```json
{
  "version": 1,
  "columns": 3,
  "rows": 2,
  "panels": [
    {"id": "summary", "type": "result_summary", "row": 0, "column": 0, "row_span": 1, "column_span": 1, "channel": "none"},
    {"id": "stft_acoustic", "type": "stft", "row": 0, "column": 1, "row_span": 1, "column_span": 1, "channel": "primary_acoustic"},
    {"id": "spectrum_acoustic", "type": "spectrum", "row": 0, "column": 2, "row_span": 1, "column_span": 1, "channel": "primary_acoustic"},
    {"id": "octave_acoustic", "type": "octave", "row": 1, "column": 0, "row_span": 1, "column_span": 1, "channel": "primary_acoustic"},
    {"id": "vibration_frequency", "type": "vibration_frequency_stability", "row": 1, "column": 1, "row_span": 1, "column_span": 1, "channel": "vibration"},
    {"id": "vibration_pulse", "type": "vibration_pulse", "row": 1, "column": 2, "row_span": 1, "column_span": 1, "channel": "vibration"}
  ]
}
```

## Configuration UI

Add a top-level configuration launcher for `结果展示布局`.

The launcher opens a centered dialog/card owned by the configuration page. The
dialog uses the existing config-card behavior:

- The top-right close button is `x`.
- `x` closes the dialog and discards unconfirmed changes made inside that
  dialog session.
- The bottom-right `确定` button applies the dialog changes to the current config
  page draft only.
- `确定` does not save to the database.
- The configuration page `保存配置` button remains the only database save point.
- Leaving or switching away from a dirty config continues to use the existing
  unsaved-change prompt.

The editor shows a grid preview and a panel list. Users can:

- choose column count: `1`, `2`, or `3`
- add rows
- remove rows only when no panel occupies the removed row
- reduce columns only when no panel would be out of bounds
- add a panel from the catalog
- remove optional panels
- drag panels between grid cells
- edit row span and column span
- edit channel binding for panel types that need a channel

If a drag or span edit would overlap another panel or leave the grid, the editor
rejects the edit and keeps the previous valid layout.

For row deletion, a panel occupies every row in its span range:
`row <= removed_row < row + row_span`. The editor blocks deleting any occupied
row, including rows that are occupied only by a spanning panel. Column reduction
uses the same rectangle-fit rule.

## Runtime Dashboard Rendering

Replace the hard-coded right-side grid in `MainWindow.build_test_page()` with a
dashboard host that renders cards from `result_dashboard_layout`.

The host responsibilities:

- Load the displayed config payload and normalize its layout.
- Build a `QGridLayout` with the configured column and row counts.
- Instantiate panel widgets from a registry keyed by panel type.
- Wrap each panel in a `Card` using the required visible title.
- Apply row and column spans.
- Rebuild the dashboard when the displayed configuration changes.
- Leave the left test-page column untouched.

Rows and columns use equal stretch inside the right dashboard grid. Each card
keeps the existing card minimum sizing where available. If the configured row
count exceeds the available vertical space, the right dashboard area scrolls
vertically instead of compressing cards into unusable heights. Horizontal
scrolling is not introduced because the maximum column count is three.

The renderer should preserve existing chart-module behavior where possible. To
support duplicate panel instances, chart modules or lightweight wrappers must be
able to receive a fixed panel channel binding instead of relying only on the
single global analysis-channel toolbar state.

The existing global analysis channel toolbar can be removed from the new
dashboard rendering path if panel-local bindings fully replace it for the right
dashboard. If retained for other behavior, it must not override explicit panel
bindings.

## Data Flow

Runtime tests:

- Existing live signals continue to feed chart modules.
- STFT panels use the existing runtime STFT cache for the active run.
- After a runtime test completes, panels display the current review payload and
  available runtime cache for the current session.

Imported WAV analysis:

- Panels render from the imported `FactoryReviewPayload`.
- STFT panels can use imported-audio STFT payload support where available.

Data management:

- No new historical-result open flow is added.
- No new STFT persistence is added for data-management records.

Missing data:

- A configured panel whose payload or channel is unavailable shows a neutral
  empty state inside its card.
- The dashboard does not remove the card and does not reflow other cards.

## Validation

`ConfigService.validate()` validates:

- selected channel count is between one and two
- `vibration_view_channel_index`, when set, is one of the selected channels
- `result_dashboard_layout` is present as a valid non-overlapping grid after
  applying the missing/blank default rule
- exactly one `结果摘要` panel exists
- all panel bindings are valid for the current selected-channel shape

Runtime startup should also reject configs with more than two selected channels
through the same normalized config path, so invalid configs cannot start tests.

User-visible validation behavior:

- A config with no stored layout, blank layout text, or null layout loads with
  the default layout and can be saved normally.
- A config edited or imported with malformed JSON, unknown panel types,
  overlapping panels, out-of-bounds spans, missing summary, duplicate summary,
  or invalid channel keys is rejected with a validation error.
- The layout editor itself should prevent users from producing invalid geometry,
  but service validation remains the source of truth.

## Compatibility / Migration

- Backward compatibility: not required.
- Protected surfaces:
  - fixed left test-page column behavior
  - PLC code and PLC signal wiring
  - hardware modules and hardware configuration behavior
  - existing config page save/unsaved-change model
  - existing chart computation semantics
- Allowed breakage:
  - old rows with blank or null `result_dashboard_layout` are shown with the new
    default layout
  - old rows with malformed non-empty `result_dashboard_layout` are not
    compatibility-supported; display may fall back to default, but saving or
    importing them requires writing a valid layout
  - selected-channel configurations with more than two channels become invalid
  - the old hard-coded right dashboard layout is replaced
  - the old global right-dashboard channel toolbar may no longer control panels
    if panel-local bindings replace it
- Migration strategy: prepared schema update.
  - Add the required `result_dashboard_layout` column to the prepared schema and
    to runtime startup's required test-config column check.
  - Existing prepared database files without the column are unsupported and
    should fail startup with the existing missing-column startup error.
  - On load or creation, normalize missing, blank, or null layout data to the
    new default layout.
  - Malformed non-empty layout data only falls back for runtime display; it is
    still rejected by save/import validation.
  - No compatibility layer for legacy layout JSON is needed.

## Tests

Add or update focused tests for:

1. `ConfigService` loads, saves, and normalizes `result_dashboard_layout`.
2. `ConfigService.validate()` rejects more than two selected channels.
3. Layout validation rejects overlaps, out-of-bounds spans, missing summary, and
   duplicate summary panels.
4. New configs receive the default layout.
5. Config import/export includes `result_dashboard_layout`.
6. The configuration page exposes the `结果展示布局` launcher.
7. The layout dialog stages changes on `确定`, discards dialog-local changes on
   `x`, and only persists through `保存配置`.
8. The layout editor prevents deleting occupied rows or reducing columns when a
   panel would become out of bounds.
9. The main window renders the right dashboard from layout data and keeps the
   left column unchanged.
10. Duplicate chart panels can render with different channel bindings.
11. `结果摘要` is required, unique, and cannot be removed from the editor.
12. Acoustic panels bound to missing acoustic data show an empty state without
    reflow.
13. Vibration panels show an empty state when no vibration channel is configured.
14. The visible vibration titles use parentheses:
    `时频图（振动）`, `频谱（振动）`, `倍频程（振动）`,
    `频率稳定性（振动）`, `脉冲强度（振动）`.
15. Existing runtime and imported-audio review payload tests continue to pass
    with panel-local bindings.

## Acceptance Criteria

- A user can configure the right-side test dashboard from the configuration page.
- The right dashboard supports `1/2/3` columns, added rows, and row/column spans.
- The summary panel is present exactly once and can be positioned by the user.
- Optional panels can be shown, hidden, duplicated, and channel-bound.
- Missing data produces stable empty cards, not automatic layout changes.
- Tests cannot start with more than two selected channels.
- No PLC or hardware-module code is changed for this feature.
