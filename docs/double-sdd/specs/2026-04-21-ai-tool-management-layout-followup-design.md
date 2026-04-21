# AI Tool Management Layout Follow-Up Design

## Summary

Fix two follow-up layout regressions on the `AI工具管理` page:

- the `AI 包目录` browse button height is too small and clips its text
- the `默认超时` row expands vertically and consumes multiple lines of form height

This is a UI-only follow-up to the previous AI tool management polish. It must not change any saved values, validation rules, service behavior, runtime behavior, or `.dy` import/export behavior.

Confirmed product decision:

- use the minimal layout-only fix path
- keep the external `ms` label
- keep the current timeout field semantics and save behavior unchanged

## Goals

- Make the `浏览` button tall enough to display its text without clipping.
- Keep the `AI 包目录` row as a bounded single-line form row.
- Make the `默认超时` row render as a single-line form row again.
- Preserve the current timeout editor behavior and saved payload shape.

## Non-Goals

- No AI service changes.
- No database or schema changes.
- No changes to saved field names or meanings.
- No changes to timeout units, timeout semantics, or execution-mode behavior.
- No changes to `.dy` import/export.
- No broader redesign of the AI tool management page.

## Design

### 1. Package Directory Row Height

The `AI 包目录` row keeps:

- one path editor
- one `浏览` button

Required behavior:

- expose the row widget as `self.package_dir_row` for UI inspection
- compute a shared single-line control height as `max(ed_package_dir.sizeHint().height(), btn_browse_package_dir.sizeHint().height())`
- apply that computed height to both the path editor and browse button
- set the row widget vertical size policy to `QSizePolicy.Policy.Fixed`
- set the row widget fixed height to the same computed single-line control height
- do not clip the `浏览` button text at that computed height

Implementation may use the max of the relevant size hints rather than assuming the line edit height is always sufficient.

### 2. Timeout Row Height

The `默认超时` row keeps:

- the existing large-integer timeout editor
- the external `ms` label on the right

Required behavior:

- expose the timeout row widget as `self.timeout_row` for UI inspection
- compute a shared single-line row height as `max(spin_timeout.sizeHint().height(), lb_timeout_unit.sizeHint().height())`
- set the timeout row widget vertical size policy to `QSizePolicy.Policy.Fixed`
- set the timeout row widget fixed height to that computed single-line row height
- the timeout editor and `ms` label stay vertically aligned within that single-line row
- the row must not absorb extra vertical space from the form layout

### 3. Preserved Behavior

This follow-up must preserve:

- `default_timeout_ms` loading and saving as the same integer value
- large timeout values beyond `QSpinBox` limits
- current `execution_mode` save behavior
- current `package_dir` save behavior
- the absence of the removed runtime helper row

## Testing

Update `tests/test_ai_tool_management_module.py` to cover:

- `module.package_dir_row` exists and uses `QSizePolicy.Policy.Fixed` vertically
- `module.btn_browse_package_dir.height() >= module.btn_browse_package_dir.sizeHint().height()`
- `module.ed_package_dir.height() == module.btn_browse_package_dir.height() == module.package_dir_row.height()`
- `module.timeout_row` exists and uses `QSizePolicy.Policy.Fixed` vertically
- `module.timeout_row.height() >= module.spin_timeout.sizeHint().height()`
- the timeout row still contains the timeout editor plus a visible `ms` label
- large timeout values still load and save without clamping
- unchanged save behavior for `execution_mode` and `package_dir` remains intact

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
- Protected surfaces: `AiService`, database schema, runtime behavior, `.dy` import/export, saved `default_timeout_ms`, saved `execution_mode`, saved `package_dir`
- Allowed breakage: only the specific UI layout details for the package directory row and timeout row
- Migration strategy: none
