# AI Tool Management UI Polish Design

## Summary

Polish the `AI工具管理` detail form layout after adding `package_dir`.

This is a UI-only change. It must not change saved AI toolchain fields, validation rules, `AiService`, runtime behavior, or `.dy` import/export behavior.

Confirmed product decisions:

- align `AI 包目录` with the other form rows
- show the default timeout value left-aligned instead of right-aligned
- show timeout units as a fixed `ms` label to the right of the timeout editor
- remove the `运行时说明` row entirely

## Goals

- Make the `AI 包目录` row visually align with adjacent `QFormLayout` rows.
- Make the default timeout editor read like a normal left-aligned field.
- Make timeout units visible as `ms` next to the editor.
- Remove the runtime helper text and its label from the form.

## Non-Goals

- No AI service or database changes.
- No changes to persisted field names or values.
- No changes to AI runtime protocol, execution mode, or default timeout semantics.
- No changes to `.dy` import/export.
- No broader redesign of the AI management page.

## Design

### 1. Package Directory Row

Keep `AI 包目录` as a single form row with:

- a path `QLineEdit`
- a `浏览` button

The row container must align with normal `QFormLayout` fields and avoid the current visual drop caused by row sizing or vertical alignment.

Concrete acceptance criteria:

- the package directory row widget must not create extra top/bottom spacing beyond the normal form row spacing
- the row widget and its internal layout must align child widgets vertically to the top or center of the same single-line row, not let the path editor sink to the bottom of a tall form row
- the path editor and `浏览` button must have compatible single-line heights
- the implementation should prefer explicit layout alignment and bounded row height over broad form-layout changes that could disturb other rows

### 2. Timeout Row

The timeout row remains bound to `default_timeout_ms`.

Required visual behavior:

- the numeric editor is left-aligned
- the numeric value remains an integer
- a fixed `ms` label appears to the right of the editor
- saving still sends the same integer `default_timeout_ms`
- large timeout values continue to load and save without clamping to `QSpinBox`-style limits

### 3. Runtime Helper Removal

Remove the runtime helper label and the `运行时说明` form row.

Tests should no longer require `protocol_version`, `resident_process`, or `oneshot_process` helper copy to appear in this UI.

## Testing

Update `tests/test_ai_tool_management_module.py` to cover:

- the package directory row exposes the editor and browse button in a bounded single-line row/container
- the package directory row/container uses explicit vertical alignment or height constraints that prevent the editor from dropping below adjacent rows
- the timeout editor alignment is left-aligned
- the timeout row shows a sibling `ms` unit label
- selecting a row still loads and saves `default_timeout_ms`, including values larger than `QSpinBox` limits
- the runtime helper row/text is not present

Required verification:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

Recommended adjacent verification:

```powershell
pytest tests/test_ai_service.py tests/test_ai_tool_management_module.py -q
```

## Compatibility / Migration

- Backward compatibility: required for persisted AI toolchain data
- Protected surfaces: `AiService`, database schema, runtime execution behavior, `.dy` import/export, saved `default_timeout_ms` values
- Allowed breakage: visual layout and test expectations for the removed runtime helper copy
- Migration strategy: none
