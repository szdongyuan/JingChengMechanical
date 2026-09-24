# Configurable Manual Judgment Buttons Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-configuration, default-off switch that freezes at run acceptance and controls whether the homepage result shows the complete manual OK/NG judgment button group.

**Architecture:** Persist one exact boolean on each test configuration, expose it beside fault-type selection in a dedicated “人工标注” configuration area, and freeze it in a versioned history-label payload. Project the frozen value into each result card and gate both the homepage action widget attachment and published manual-judgment target without changing backend judgment capabilities or AI fault-type semantics.

**Tech Stack:** Python 3, Peewee/SQLite, PyQt6, canonical JSON payloads, pytest

---

## File Structure

- Modify `model/test_config_table.py`: add the persisted boolean with a false default.
- Modify `app/service/db_connection_service.py`: require and validate the exact runtime column definition.
- Create `scripts/migrate_manual_judgment_buttons_schema.py`: backed-up, transactional, idempotent schema migration.
- Create `tests/test_migrate_manual_judgment_buttons_schema.py`: migration behavior and rollback coverage.
- Modify `tests/test_db_connection_service.py`: runtime-schema contract coverage.
- Modify `tests/test_test_config_table.py`: exact model-field contract coverage.
- Modify `app/service/config_service.py`: normalize, validate, save, load, copy, and default the field.
- Modify `app/service/config_package_service.py`: accept old packages as false and preserve the field in new packages.
- Modify `ui/modules/config.py`: add the “人工标注” area, checkbox, and grouped fault-type controls.
- Modify `app/service/fault_types.py`: add a new history-label payload version while preserving canonical legacy decoding.
- Modify `app/services_container.py`: freeze the switch at run acceptance.
- Modify `app/service/multi_run_integration/production_composition.py`: preserve the switch when rebuilding durable terminal history labels.
- Modify `scripts/benchmark_two_stage_analysis.py`: pass the semantic switch through its direct history-label encoder call.
- Modify `app/service/result_history.py`: carry the frozen flag in `FrozenConfigSummary` and clone it through transitions.
- Modify `app/service/multi_run_integration/history_qt_adapter.py`: map both payload versions into the frozen card value.
- Modify `tests/multi_run_integration/fakes.py`: let shared history-label fixtures encode the new field explicitly.
- Modify `ui/modules/result_history.py`: gate shared action-widget attachment by the frozen value.
- Modify `ui/modules/result_dashboard.py`: publish no homepage manual-judgment target when the frozen value is false.
- Modify focused tests in `tests/test_config_service.py`, `tests/test_config_package_service.py`, `tests/test_config_module.py`, `tests/test_fault_types.py`, `tests/multi_run_integration/test_composition_and_ingress.py`, `tests/multi_run_integration/test_history_qt_adapter.py`, `tests/test_result_history.py`, `tests/test_result_history_module.py`, `tests/test_result_dashboard_module.py`, `tests/test_result_dashboard_history_projection.py`, and `tests/test_control_module.py` as required by the existing ownership boundaries.
- Modify `tests/multi_run_integration/test_production_composition.py` for durable terminal label encoding and `tests/test_review_payloads.py` for unchanged automatic/AI judgment semantics.

## Test Command Convention

The repository virtual environment contains the working PyQt6 runtime but not pytest. For Qt tests, set `QT_QPA_PLATFORM=offscreen`, append `D:\Program Files\Python\Python312\Lib\site-packages`, and import `pytest, PyQt6.QtCore` before calling `pytest.main(...)` from `C:\Users\DY\Documents\ChatGPT\New project\.venv\Scripts\python.exe`.

### Task 1: Persisted Schema and Safe Migration

**Files:**
- Modify: `model/test_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Create: `scripts/migrate_manual_judgment_buttons_schema.py`
- Create: `tests/test_migrate_manual_judgment_buttons_schema.py`
- Modify: `tests/test_db_connection_service.py`
- Modify: `tests/test_test_config_table.py`

- [ ] **Step 1: Write failing model and runtime-schema tests**

Update the exact `TestConfigTable._meta.fields` expectation and assert `TestConfigTable.manual_judgment_buttons_visible` is a `BooleanField` with false default and the expected SQLite schema mapping. Extend test database schemas with the exact definition `INTEGER NOT NULL DEFAULT 0`. Add cases proving startup rejects a missing, nullable, non-integer, or nonzero-default definition without mutating the database.

- [ ] **Step 2: Write failing migration tests**

Following the patterns in `scripts/migrate_plc_current_schema.py`, test that the new migration:

- validates the exact resolved file and required `test_config_table` before changes;
- creates a timestamped backup;
- adds `manual_judgment_buttons_visible INTEGER NOT NULL DEFAULT 0` transactionally;
- preserves all existing rows and backfills `0`;
- is idempotent when rerun against an already-correct column;
- rejects an incompatible existing definition and rolls back.

- [ ] **Step 3: Run focused tests and verify RED**

Run `pytest tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_migrate_manual_judgment_buttons_schema.py -v` and expect missing field/script failures.

- [ ] **Step 4: Implement the model and migration**

Add:

```python
manual_judgment_buttons_visible = BooleanField(default=False)
```

Add the column name to `_REQUIRED_TEST_CONFIG_COLUMNS` and validate the exact SQLite tuple `("INTEGER", True, "0", 0)` using the existing fail-closed startup style. Implement the migration with `BEGIN IMMEDIATE`, `ALTER TABLE`, commit/rollback, and recoverable backup creation.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the same focused tests; expect all PASS. Run `compileall` and `git diff --check`.

- [ ] **Step 6: Commit the checkpoint**

```powershell
git add model/test_config_table.py app/service/db_connection_service.py scripts/migrate_manual_judgment_buttons_schema.py tests/test_test_config_table.py tests/test_db_connection_service.py tests/test_migrate_manual_judgment_buttons_schema.py
git commit -m "tmp/sdd: add manual judgment visibility schema"
```

### Task 2: Configuration Service and Package Compatibility

**Files:**
- Modify: `app/service/config_service.py`
- Modify: `app/service/config_package_service.py`
- Modify: `tests/test_config_service.py`
- Modify: `tests/test_config_package_service.py`

- [ ] **Step 1: Write failing service tests**

Cover new-config false default, load/save/copy round-trip, omitted legacy input defaulting to false, exact `True` persistence, and rejection of non-boolean values such as `0`, `1`, `"true"`, and `None` when the key is explicitly supplied.

- [ ] **Step 2: Write failing package tests**

Verify new exports contain the boolean, new imports preserve it, and accepted old package payloads missing the key import as false without changing the package format version.

- [ ] **Step 3: Run focused tests and verify RED**

Run `pytest tests/test_config_service.py tests/test_config_package_service.py -k "manual_judgment_buttons or export_writes_manifest or legacy" -v`.

- [ ] **Step 4: Implement normalization and persistence**

At the config-service boundary use:

```python
if "manual_judgment_buttons_visible" in data:
    value = data["manual_judgment_buttons_visible"]
    if type(value) is not bool:
        raise ValueError("manual_judgment_buttons_visible must be a boolean")
else:
    value = False
```

Include the normalized value in create/update row values and loaded semantic config. Normalize missing imported-package values to false before validation/save; let new exports naturally serialize the loaded field.

- [ ] **Step 5: Run focused and full service tests**

Run both full test files. Expect PASS. Run `compileall` and `git diff --check`.

- [ ] **Step 6: Commit the checkpoint**

```powershell
git add app/service/config_service.py app/service/config_package_service.py tests/test_config_service.py tests/test_config_package_service.py
git commit -m "tmp/sdd: persist manual judgment visibility config"
```

### Task 3: Configuration Page “人工标注” Area

**Files:**
- Modify: `ui/modules/config.py`
- Modify: `tests/test_config_module.py`

- [ ] **Step 1: Write failing UI structure and behavior tests**

Assert the configuration page contains a clearly labeled `人工标注` group with checkbox object name `manual_judgment_buttons_visible`, label `在主页结果中显示人工标注按钮`, and the existing `fault_types_editor` plus `故障类型管理` button inside the same group.

Test default unchecked, authoritative load of true/false, exact boolean collection, dirty-state changes, save/reload/discard behavior, switching between one true configuration and one false configuration without state leakage, and that the fault-type editor remains enabled and editable while the checkbox is off. Add an invalid service-payload case proving explicit `0`, `1`, `"false"`, or `None` is rejected rather than coerced.

- [ ] **Step 2: Run focused UI tests and verify RED**

Run `pytest tests/test_config_module.py -k "manual_judgment_buttons or fault_types_editor" -v` and expect missing UI/state failures.

- [ ] **Step 3: Implement the grouped UI**

Create a `QGroupBox("人工标注")` or equivalently named existing card pattern. Place the checkbox first and the existing fault-type row beneath it. Do not connect the checkbox to fault-type enabled state. Wire checkbox changes into existing dirty tracking and collect `isChecked()`.

At the UI load boundary, default only a missing key to false and reject every explicit non-boolean value:

```python
value = config.get("manual_judgment_buttons_visible", False)
if type(value) is not bool:
    raise ValueError("manual_judgment_buttons_visible must be a boolean")
self.chk_manual_judgment_buttons_visible.setChecked(value)
```

- [ ] **Step 4: Run focused and full config-module tests**

Expect PASS. Run `compileall` and `git diff --check`.

- [ ] **Step 5: Commit the checkpoint**

```powershell
git add ui/modules/config.py tests/test_config_module.py
git commit -m "tmp/sdd: group manual judgment configuration"
```

### Task 4: Versioned Run-Time Freezing and History Projection

**Files:**
- Modify: `app/service/fault_types.py`
- Modify: `app/services_container.py`
- Modify: `app/service/multi_run_integration/production_composition.py`
- Modify: `scripts/benchmark_two_stage_analysis.py`
- Modify: `app/service/result_history.py`
- Modify: `app/service/multi_run_integration/history_qt_adapter.py`
- Modify: `tests/multi_run_integration/fakes.py`
- Modify: `tests/test_fault_types.py`
- Modify: `tests/multi_run_integration/test_composition_and_ingress.py`
- Modify: `tests/multi_run_integration/test_history_qt_adapter.py`
- Modify: `tests/multi_run_integration/test_production_composition.py`
- Modify: `tests/test_result_history.py`

- [ ] **Step 1: Write failing history-payload tests**

Test that new encoding emits a new schema version and requires an exact boolean under `config`; round-trip true/false values; malformed new payloads fail; legacy canonical bytes/hashes still decode unchanged and expose no stored true value, with consumers defaulting to false.

- [ ] **Step 2: Write failing freeze/projection tests**

Test captured and imported start requests freeze the prepared configuration value, unresolved imported config freezes false, `FrozenConfigSummary` defaults false for legacy constructors, and true/false survive clone, terminal, manual judgment, and reanalysis transitions. Add a durable production-composition test proving terminal label reconstruction reads the frozen config value and encodes it into the new history-label payload instead of reverting to false.

- [ ] **Step 3: Run focused tests and verify RED**

Run `pytest tests/test_fault_types.py tests/multi_run_integration/test_composition_and_ingress.py tests/multi_run_integration/test_history_qt_adapter.py tests/multi_run_integration/test_production_composition.py tests/test_result_history.py -k "manual_judgment_buttons or history_labels or frozen_config" -v` and expect the new assertions to fail.

- [ ] **Step 4: Implement dual-version history labels**

Keep a legacy schema constant and add a new current schema. Encoding always writes the current shape:

```python
"config": {
    "config_id": config_id,
    "name": name,
    "fault_types": fault_types,
    "manual_judgment_buttons_visible": visible,
    "details": details,
}
```

Normalize and canonical-check each version against its own exact shape. Legacy decoding must not rewrite or rehash bytes; the adapter reads `config_value.get("manual_judgment_buttons_visible", False)`.

Update every encoder call, including `PreparedProductionComposition` terminal projection rebuilding, `scripts/benchmark_two_stage_analysis.py`, and `tests/multi_run_integration/fakes.py`. The production path must validate/read the already frozen semantic config boolean rather than the current mutable database row. The benchmark path may default a missing legacy config key to false but must pass the resulting value explicitly so the encoder itself has no ambiguous compatibility default.

- [ ] **Step 5: Freeze and project the value**

Return `(name, fault_types, visible)` from the service-container configuration lookup, pass the value to history-label encoding, add `manual_judgment_buttons_visible: bool = False` to `FrozenConfigSummary`, validate exact bool, and preserve it through `_clone_config_summary` and adapter mapping.

- [ ] **Step 6: Run focused and full history tests**

Expect PASS. Run `compileall` and `git diff --check`.

- [ ] **Step 7: Commit the checkpoint**

```powershell
git add app/service/fault_types.py app/services_container.py app/service/multi_run_integration/production_composition.py app/service/result_history.py app/service/multi_run_integration/history_qt_adapter.py scripts/benchmark_two_stage_analysis.py tests/multi_run_integration/fakes.py tests/test_fault_types.py tests/multi_run_integration/test_composition_and_ingress.py tests/multi_run_integration/test_history_qt_adapter.py tests/multi_run_integration/test_production_composition.py tests/test_result_history.py
git commit -m "tmp/sdd: freeze manual judgment button visibility"
```

### Task 5: Homepage All-or-Nothing Visibility

**Files:**
- Modify: `ui/modules/result_history.py`
- Modify: `ui/modules/result_dashboard.py`
- Modify: `tests/test_result_history_module.py`
- Modify: `tests/test_result_dashboard_module.py`
- Modify: `tests/test_result_dashboard_history_projection.py`
- Modify: `tests/test_control_module.py`
- Modify: `tests/test_review_payloads.py`

- [ ] **Step 1: Write failing homepage visibility tests**

For selected terminal cards with operational manual judgment allowed, verify false keeps the shared widget detached/hidden/disabled and publishes no target; true attaches and shows it. Verify a true card renders `合格` plus all frozen NG fault types, and a true card with no fault types renders `合格` plus generic `不合格`. Update the existing projection helper/cards in `tests/test_result_dashboard_history_projection.py` to set true where a manual target is expected, and add a false case proving no target is published.

Add a run-freezing regression: changing the current configuration after a card is created does not alter that card’s visibility; a new card from the updated configuration uses the new value.

Add an automatic/AI semantics regression that builds otherwise identical judgment summaries with the visibility flag false and true and proves the summary rows, AI status/score/failure label, and overall conclusion are identical. The switch must affect only homepage control visibility.

- [ ] **Step 2: Run focused tests and verify RED**

Run `pytest tests/test_result_history_module.py tests/test_result_dashboard_module.py tests/test_result_dashboard_history_projection.py tests/test_control_module.py tests/test_review_payloads.py -k "manual_judgment_buttons or manual_target or manual_judgment or visibility_flag_does_not_change_analysis" -v` and expect the new visibility assertions to fail while the semantics-equivalence test documents the protected analysis boundary.

- [ ] **Step 3: Gate the history widget attachment**

Extend `legal_selected` in `ResultHistoryModule._render_snapshot_now()` with:

```python
and selected_snapshot.config.manual_judgment_buttons_visible
```

Keep the existing all-or-nothing rendering in `ControlModule`: visible configurations show `btn_pass` plus every NG type, or the generic fail fallback when empty.

- [ ] **Step 4: Gate the published homepage target**

In `ResultDashboardModule._publish_manual_judgment_target()`, require the selected frozen config flag before constructing `ManualJudgmentTarget`; otherwise publish/retain `None`. Do not alter backend request validation or reanalysis targets.

- [ ] **Step 5: Run focused and full homepage tests**

Run the exact focused command from Step 2 and then the five complete test files without `-k`; expect PASS. Run `compileall` and `git diff --check`.

- [ ] **Step 6: Commit the checkpoint**

```powershell
git add ui/modules/result_history.py ui/modules/result_dashboard.py tests/test_result_history_module.py tests/test_result_dashboard_module.py tests/test_result_dashboard_history_projection.py tests/test_control_module.py tests/test_review_payloads.py
git commit -m "tmp/sdd: gate homepage manual judgment buttons"
```

### Task 6: Integration Verification and Workspace Migration Readiness

**Files:**
- Verify all files above.
- Verify: `data/jingchen.db` only after local squash integration; do not mutate the shared workspace database from the temporary worktree.

- [ ] **Step 1: Run the directly affected suites**

Run full tests for database connection/migration, config service/package/UI, fault types, composition/history adapters, result history/store/module/dashboard, control module, and main-window wiring. Expect zero failures in the selected scope.

- [ ] **Step 2: Run syntax and diff checks**

Run `python -m compileall -q` on modified Python source/test files and `git diff --check`. Expect exit 0.

- [ ] **Step 3: Inspect migration target without writing**

From the main workspace, resolve `data/jingchen.db`, verify it is inside the intended workspace data directory, confirm `test_config_table` exists, and report whether the new column is absent or already correct. Do not migrate before the user selects local integration.

- [ ] **Step 4: Document post-integration migration command**

After squash integration and before launching the updated application, run:

```powershell
python scripts/migrate_manual_judgment_buttons_schema.py data/jingchen.db
```

Verify a timestamped backup exists and every existing row has `manual_judgment_buttons_visible = 0`. Then run the focused schema/config tests against the integrated tree.
