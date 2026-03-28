# Workorder Layout And AI Binding Legacy Save Design

## Context

The current workorder page still carries UI from the earlier factory selection migration that no longer matches operator needs:

- the ordered factory config selector sits in the right-side field column of a `QFormLayout`
- the page still shows a read-only `测试配置 Config` field even though primary priority is already visible in the selector via the `Primary` badge
- the workorder panel still renders a multiline factory summary that becomes noisy and unusable when many configs are enabled
- dragging configs in the selector can show an oversized drag preview

Separately, saving a config can fail in the live app when AI binding data is saved for a config that does not yet have a binding row. Investigation against the live database showed:

- `test_config_ai_binding_table` still contains a legacy `analysis_window_offset_ms` column
- that legacy column is `NOT NULL`
- the column has no default value in the live database
- the current Peewee model intentionally removed that field
- `AiService.save_binding(...)` inserts through the reduced model, so inserts for new configs fail with `NOT NULL constraint failed: test_config_ai_binding_table.analysis_window_offset_ms`

This design keeps the current ownership model where analysis-window offset belongs to the main test config, not the AI binding row, while restoring compatibility with legacy user databases.

## Scope

In scope:

- simplify the workorder factory-config area layout
- remove the redundant workorder `测试配置 Config` field
- remove the workorder factory summary block
- keep the ordered config selector as the visible source of primary priority
- reduce the oversized drag-preview behavior for config reorder
- restore AI binding saves for legacy databases whose binding table still requires `analysis_window_offset_ms`
- add focused regression coverage for both UI and save compatibility

Out of scope:

- redesigning factory selection semantics
- changing controller start-resolution logic
- changing analysis-window ownership back to the AI binding table
- broad database rebuilds or destructive schema cleanup of user databases

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - factory selection behavior and ordered priority semantics
  - existing live SQLite databases, including legacy `test_config_ai_binding_table` schemas
  - current config-module AI binding payload contract where `analysis_window_offset_ms` is not part of AI binding data
- Allowed breakage:
  - remove the workorder `测试配置 Config` display row
  - remove the workorder factory summary block
  - adjust the workorder layout for the config selector
- Migration strategy: compatibility layer in save path, no mandatory database rebuild

The save fix must work even when the user database still has the legacy binding-side offset column. The app should not require a manual DB migration before config save works again.

## Approaches Considered

### Approach A: Local UI cleanup + save-path compatibility layer

- move the selector out of the `QFormLayout` field column into its own full-width section
- remove the redundant workorder config field and summary block
- keep the legacy binding column unsupported at the model layer, but detect it during save and write a compatibility value only when needed

Pros:

- smallest behavioral surface
- restores save behavior on the real user database
- avoids destructive schema work

Cons:

- keeps a small amount of legacy-schema handling in `AiService`

### Approach B: Rebuild legacy binding tables during startup migration

- rewrite `test_config_ai_binding_table` so the legacy column is removed or given a default

Pros:

- removes the need for runtime compatibility logic

Cons:

- higher risk against live user databases
- broader migration/testing burden
- unnecessary to restore save behavior

### Approach C: Reintroduce binding-side offset into the AI payload and model

Pros:

- minimal code in the save path

Cons:

- re-couples offset ownership to the wrong table
- contradicts current architecture and tests
- reintroduces the data split that earlier work intentionally removed

## Recommended Design

Use Approach A.

### Workorder Layout

The workorder module keeps `SN` and `Model` as simple form rows, but the factory selector becomes its own section below them:

- show the exact section label `工厂配置 Factory Configs`
- render the selector directly below that label, full-width in the workorder content area
- remove the read-only `测试配置 Config` field entirely
- remove the factory summary label entirely

The ordered selector remains the only place where the operator sees enabled configs and priority. The top enabled row continues to use the existing `Primary` visual marker.

### Drag Preview Behavior

Keep the existing reorder semantics and manual `dropEvent(...)` logic. Add a targeted drag-start customization so the drag preview is derived from the dragged row widget itself instead of Qt's oversized default behavior for this embedded-widget list.

Acceptance criteria:

- the drag preview pixmap is created from the current row widget render
- the drag preview width and height match the dragged row widget's current rendered size
- the change does not alter enabled-order calculation, drag eligibility, or persistence behavior

This is a UI-only change. It must not change:

- which rows are draggable
- how enabled order is computed
- how reorder persistence works

### AI Binding Save Compatibility

`analysis_window_offset_ms` remains owned by `test_config_table` and remains absent from the AI binding UI payload and binding model.

`AiService.save_binding(...)` gains legacy-schema awareness:

- inspect `test_config_ai_binding_table` columns at save time
- if the legacy `analysis_window_offset_ms` column exists, treat it as a compatibility-only field
- on binding-row creation, insert an explicit legacy value of `0`
- on binding-row update, preserve normal behavior and do not read any offset value from config UI state
- if implementation later uses a unified upsert path instead of separate create/update branches, it must remain behaviorally equivalent to the above:
  - newly created rows receive legacy `analysis_window_offset_ms = 0` when the column exists
  - existing rows do not require any UI-provided offset value to save successfully

The returned binding ViewModel must continue to exclude `analysis_window_offset_ms`.

### Error Handling

- if the database schema is legacy but otherwise writable, config save should succeed without extra operator action
- if save still fails for another reason, surface the real error instead of masking it as an offset issue
- the Qt font-size warning is not part of the functional save failure unless a concrete code path in this scope proves otherwise

## Testing Requirements

Add or update tests for:

- workorder module no longer exposing the `测试配置 Config` row/widget
- workorder module no longer rendering the factory summary block
- workorder module using the exact section label `工厂配置 Factory Configs` above the selector
- workorder config selector rendered as its own section while preserving selection payload behavior
- config-list drag-preview helper or drag-start path producing a pixmap whose dimensions match the dragged row widget size
- AI binding save succeeds when the live binding table still has legacy `analysis_window_offset_ms NOT NULL`
- AI binding update succeeds when the live binding table still has legacy `analysis_window_offset_ms NOT NULL`
- AI binding save compatibility does not reintroduce `analysis_window_offset_ms` into resolved binding payloads
- normal save behavior stays unchanged for fresh schemas without the legacy column

## Non-Goals

This design does not:

- reintroduce config summaries elsewhere in the workorder panel
- move factory selection out of the workorder module
- rewrite the user's database to drop the legacy binding column as part of this task
