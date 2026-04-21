# AI Tool Management Layout Alignment Design

## Summary

Fix the AI tool management detail pane so form rows keep normal vertical spacing when the window is tall. The detail pane should follow the configuration page pattern: put editable content inside a scrollable content wrapper with bottom stretch, and keep the save button outside that scroll area.

## Problem

The AI tool management page currently adds the form host directly to a stretchable `QVBoxLayout`. When the window grows vertically, Qt allocates extra height into the form area, which spreads rows apart and makes fields such as `AI 包目录` and `默认超时` look much looser than the configuration page.

The configuration page avoids this by placing cards/forms inside a `QScrollArea` content widget and adding stretch at the bottom of the scroll content, so extra height stays below the content instead of expanding form row spacing.

## Goals

- Keep AI tool management form rows visually compact and aligned with the configuration page.
- Preserve the existing single-line layout for `AI 包目录` and `默认超时`.
- Preserve the existing save button behavior and placement below the editable content.
- Keep the change localized to AI tool management UI and related tests.

## Non-Goals

- Redesigning the AI tool management page.
- Changing AI toolchain persistence, validation, runtime behavior, or database schema.
- Modifying hardware management code or configuration page behavior.
- Adding new product behavior beyond layout correction.

## Design

Update `ui/modules/ai_tool_management.py` so the right-side detail layout mirrors the configuration page structure:

- Keep `self.detail` as the detail container.
- Add a scroll content widget with a vertical layout.
- Add the current form host to the scroll content.
- Add a bottom stretch to the scroll content.
- Wrap the scroll content in a `QScrollArea` exposed as `self.detail_scroll`, with `setWidgetResizable(True)` and `NoFrame`.
- Add the scroll area to `detail_lay` with stretch factor `1`.
- Keep the save row outside the scroll area, below it.

The form's `QFormLayout` spacing remains explicit and compact (`setVerticalSpacing(8)`), and existing fixed single-line row height handling remains intact.

## Validation

Add or update UI tests in `tests/test_ai_tool_management_module.py` to verify:

- The detail pane has a `detail_scroll` scroll area with widget-resizable behavior and no frame.
- The save row/button is outside `detail_scroll`, so it remains a sibling below the scroll area rather than a child of the scroll content.
- The form host does not expand to fill a tall detail pane; extra height should be absorbed by scroll content bottom stretch.
  A concrete geometry check should resize the module to a tall height (for example 1400 px), show a draft or existing toolchain detail, and assert that the form host height remains near its compact `sizeHint()` instead of approaching the full detail height.
- `AI 包目录` and `默认超时` rows remain fixed single-line rows with visible controls.

Run:

```powershell
pytest tests/test_ai_tool_management_module.py -q
```

## Compatibility / Migration

- Backward compatibility: required for AI tool management behavior.
- Protected surfaces: AI toolchain save/load validation, AI runtime behavior, database schema, hardware modules, configuration page behavior.
- Allowed breakage: none.
- Migration strategy: none.
