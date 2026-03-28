# Workorder Layout And AI Binding Legacy Save Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. If that skill is unavailable in the execution environment, follow the same fresh-worktree, fresh-implementer, paired-review workflow manually. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up the workorder factory-config UI, keep config-drag previews visually bounded to the actual row size, and restore config-save compatibility for legacy AI binding schemas that still require `analysis_window_offset_ms`.

**Architecture:** Keep the workorder cleanup local to `WorkOrderModule` and the selector widget. Add a small drag-preview helper in the selector widget so drag visuals are controlled without changing reorder semantics. Add legacy-schema compatibility to `AiService.save_binding(...)` rather than rebuilding user databases, and cover that behavior with focused tests that simulate the confirmed live-schema shape.

**Tech Stack:** Python 3.12, PyQt6, Peewee, pytest

---

## File Structure

### Workorder UI

- Modify: `ui/modules/workorder.py`
  - Remove the redundant `测试配置 Config` field and the factory summary block.
  - Render the selector under the exact section label `工厂配置 Factory Configs` as a full-width section.
- Modify: `tests/test_workorder_module.py`
  - Update layout-focused expectations while preserving selection payload and persistence assertions.

### Selector drag preview

- Modify: `ui/widgets/factory_config_list.py`
  - Add a targeted drag-preview helper/start-drag path that renders the dragged row widget at its actual size.
- Modify: `tests/test_factory_config_list.py`
  - Add focused assertions around drag-preview sizing and preserve existing reorder tests.

### AI binding legacy save compatibility

- Modify: `app/service/ai_service.py`
  - Detect legacy binding-table columns and keep `save_binding(...)` compatible with schemas that still require `analysis_window_offset_ms`.
- Modify: `tests/test_ai_service.py`
  - Add coverage for create/update saves against a legacy binding table schema with `analysis_window_offset_ms NOT NULL`.

## Task 1: Simplify The Workorder Factory Selection Layout

**Files:**
- Modify: `ui/modules/workorder.py`
- Modify: `tests/test_workorder_module.py`

- [ ] **Step 1: Write the failing workorder layout tests**

Add or update tests that assert:

```python
def test_workorder_module_uses_full_width_factory_config_section(qapp):
    module, _ = _make_module()
    assert module.lbl_factory_configs.text() == "工厂配置 Factory Configs"
    assert hasattr(module, "ed_cfg") is False
    assert module.form_layout.labelForField(module.config_selector) is None
    assert module.factory_config_section.layout().indexOf(module.config_selector) >= 0


def test_workorder_module_does_not_render_factory_summary_block(qapp):
    module, services = _make_module(saved_selected_ids=(303, 101))
    assert hasattr(module, "lbl_factory_summary") is False
    assert "Factory summary:" not in _workorder_rendered_text(module)
```

- [ ] **Step 2: Run the focused workorder tests to verify they fail**

Run: `pytest tests/test_workorder_module.py -k "full_width_factory_config_section or factory_summary_block" -v`

Expected: FAIL because the module still creates `ed_cfg` and `lbl_factory_summary` and still renders the selector in a form row.

- [ ] **Step 3: Implement the layout cleanup**

Implementation requirements:

- keep `条码 SN` and `产品型号 Model` in the form layout
- expose the form layout as `self.form_layout` so the new layout contract is testable
- remove `self.ed_cfg`
- remove `self.lbl_factory_summary`
- add a dedicated container such as `self.factory_config_section`
- add a dedicated `QLabel` with exact text `工厂配置 Factory Configs`
- place `self.config_selector` directly below that label as a full-width section in the module layout
- preserve the existing workorder payload:

```python
{
    "sn": ...,
    "model": ...,
    "selected_config_ids": ...,
}
```

- keep persistence notice and phase/trigger summary behavior unchanged unless the removed summary widget requires a helper update

- [ ] **Step 4: Run the focused workorder tests to verify they pass**

Run: `pytest tests/test_workorder_module.py -k "full_width_factory_config_section or factory_summary_block" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/modules/workorder.py tests/test_workorder_module.py
git commit -m "refactor(workorder): simplify factory config section"
```

## Task 2: Bound The Selector Drag Preview To The Row Size

**Files:**
- Modify: `ui/widgets/factory_config_list.py`
- Modify: `tests/test_factory_config_list.py`

- [ ] **Step 1: Write the failing drag-preview tests**

Add tests that verify a helper or start-drag path uses the current row widget size:

```python
def test_factory_config_list_drag_preview_matches_current_row_widget_size(qapp):
    widget = FactoryConfigListWidget()
    widget.set_rows(
        rows=[_row(101, "Config A"), _row(202, "Config B")],
        enabled_config_ids=(101, 202),
    )
    _show_widget(widget, qapp)
    widget.setCurrentItem(widget.item(0))

    preview = widget._drag_preview_pixmap()
    row_widget = widget.itemWidget(widget.item(0))

    assert preview.width() == row_widget.size().width()
    assert preview.height() == row_widget.size().height()
```

- [ ] **Step 2: Run the focused selector tests to verify they fail**

Run: `pytest tests/test_factory_config_list.py -k "drag_preview_matches_current_row_widget_size" -v`

Expected: FAIL because no drag-preview helper/start-drag customization exists yet.

- [ ] **Step 3: Implement the drag-preview helper/start-drag path**

Implementation requirements:

- keep existing `dropEvent(...)` reorder logic intact
- add a small helper that renders the dragged row widget to a pixmap sized from the row widget itself
- wire `startDrag(...)` or an equivalent local helper so the drag preview uses that pixmap
- do not change:
  - checked-order semantics
  - drag eligibility rules
  - persistence or emitted order changes

- [ ] **Step 4: Run the focused selector tests to verify they pass**

Run: `pytest tests/test_factory_config_list.py -k "drag_preview_matches_current_row_widget_size or drop_event" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ui/widgets/factory_config_list.py tests/test_factory_config_list.py
git commit -m "fix(factory-ui): bound selector drag preview size"
```

## Task 3: Restore AI Binding Save Compatibility For Legacy Schemas

**Files:**
- Modify: `app/service/ai_service.py`
- Modify: `tests/test_ai_service.py`

- [ ] **Step 1: Write the failing legacy-schema save tests**

Add focused tests that simulate a binding table with the legacy column present and required:

```python
def test_save_binding_creates_row_when_legacy_binding_offset_column_is_not_null(ai_service, config, toolchain):
    db = AiBindingTable._meta.database
    db.execute_sql("DROP TABLE test_config_ai_binding_table")
    db.execute_sql(
        '''
        CREATE TABLE test_config_ai_binding_table (
            binding_id INTEGER PRIMARY KEY,
            config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
            timeout_ms INTEGER,
            analysis_window_offset_ms INTEGER NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            score_threshold REAL DEFAULT NULL,
            updated_time TEXT NOT NULL DEFAULT ''
        )
        '''
    )

    binding_id = ai_service.save_binding(config.config_id, {
        "enabled": True,
        "toolchain_id": "motor-ai",
    })

    row = db.execute_sql(
        "SELECT analysis_window_offset_ms FROM test_config_ai_binding_table WHERE binding_id = ?",
        (binding_id,),
    ).fetchone()
    assert row == (0,)


def test_save_binding_updates_existing_row_when_legacy_binding_offset_column_is_not_null(ai_service, config, toolchain):
    db = AiBindingTable._meta.database
    db.execute_sql("DROP TABLE test_config_ai_binding_table")
    db.execute_sql(
        '''
        CREATE TABLE test_config_ai_binding_table (
            binding_id INTEGER PRIMARY KEY,
            config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
            timeout_ms INTEGER,
            analysis_window_offset_ms INTEGER NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            score_threshold REAL DEFAULT NULL,
            updated_time TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    db.execute_sql(
        '''
        INSERT INTO test_config_ai_binding_table (
            binding_id, config_id, enabled, toolchain_id, timeout_ms,
            analysis_window_offset_ms, params_json, score_threshold, updated_time
        ) VALUES (1, ?, 0, NULL, NULL, 250, '{}', NULL, '')
        ''',
        (config.config_id,),
    )

    ai_service.save_binding(config.config_id, {
        "enabled": True,
        "toolchain_id": "motor-ai",
        "timeout_ms": 5000,
    })

    binding = ai_service.resolve_binding(config.config_id)
    assert binding["enabled"] is True
    assert binding["timeout_ms"] == 5000
    assert "analysis_window_offset_ms" not in binding


def test_save_binding_legacy_compatibility_does_not_mask_unrelated_insert_failures(
    ai_service, config, toolchain, monkeypatch
):
    db = AiBindingTable._meta.database
    db.execute_sql("DROP TABLE test_config_ai_binding_table")
    db.execute_sql(
        '''
        CREATE TABLE test_config_ai_binding_table (
            binding_id INTEGER PRIMARY KEY,
            config_id INTEGER NOT NULL UNIQUE REFERENCES test_config_table(config_id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0,
            toolchain_id TEXT REFERENCES ai_toolchain_table(toolchain_id) ON DELETE SET NULL,
            timeout_ms INTEGER,
            analysis_window_offset_ms INTEGER NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            score_threshold REAL DEFAULT NULL,
            updated_time TEXT NOT NULL DEFAULT ''
        )
        '''
    )

    original_execute_sql = db.execute_sql

    def _raising_execute_sql(sql, params=None):
        normalized_sql = str(sql).replace('"', "").replace("'", "").lower()
        if "insert into test_config_ai_binding_table" in normalized_sql:
            raise RuntimeError("different failure")
        return original_execute_sql(sql, params)

    monkeypatch.setattr(db, "execute_sql", _raising_execute_sql)

    with pytest.raises(RuntimeError, match="different failure"):
        ai_service.save_binding(config.config_id, {
            "enabled": True,
            "toolchain_id": "motor-ai",
        })
```

- [ ] **Step 2: Run the focused AI-service tests to verify they fail**

Run: `pytest tests/test_ai_service.py -k "legacy_binding_offset_column_is_not_null" -v`

Expected: FAIL because `save_binding(...)` currently inserts through the reduced Peewee model and does not satisfy the legacy required column.

- [ ] **Step 3: Implement the legacy-schema compatibility layer**

Implementation requirements:

- add a helper in `AiService` that inspects `PRAGMA table_info(test_config_ai_binding_table)`
- keep `analysis_window_offset_ms` out of:
  - `resolve_binding(...)`
  - `_collect_ai_binding(...)`
  - the `TestConfigAiBindingTable` model
- if the legacy column exists:
  - create-path saves must explicitly provide `analysis_window_offset_ms = 0`
  - update-path saves must continue to work without requiring any UI-provided offset value
- if the legacy column does not exist:
  - preserve the current save behavior
- keep `score_threshold` validation/persistence semantics unchanged

- [ ] **Step 4: Run the focused AI-service tests to verify they pass**

Run: `pytest tests/test_ai_service.py -k "legacy_binding_offset_column_is_not_null or save_binding_ignores_analysis_window_offset_ms or does_not_mask_unrelated_insert_failures" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/service/ai_service.py tests/test_ai_service.py
git commit -m "fix(ai-service): support legacy binding offset schemas"
```

## Task 4: Run The Focused Verification Slice

**Files:**
- Modify: `tests/test_workorder_module.py`
- Modify: `tests/test_factory_config_list.py`
- Modify: `tests/test_ai_service.py`
- Modify: any production file only if the focused suite reveals a real remaining gap

- [ ] **Step 1: Run the combined focused verification suite**

Run: `pytest tests/test_workorder_module.py tests/test_factory_config_list.py tests/test_ai_service.py -v`

Expected: PASS after the previous tasks are complete.

- [ ] **Step 2: If any failures remain, fix only the validated gap and rerun the same suite**

Keep fixes local to:

- `ui/modules/workorder.py`
- `ui/widgets/factory_config_list.py`
- `app/service/ai_service.py`
- the three focused test files

- [ ] **Step 3: Commit**

```bash
git add ui/modules/workorder.py ui/widgets/factory_config_list.py app/service/ai_service.py tests/test_workorder_module.py tests/test_factory_config_list.py tests/test_ai_service.py
git commit -m "test(factory-ui): verify workorder and ai save cleanup"
```
