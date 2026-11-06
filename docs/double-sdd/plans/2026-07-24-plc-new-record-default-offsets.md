# New PLC Record Default Offsets Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every ordinary PLC newly added through the PLC settings UI receives the approved field-offset layout while all existing PLC offset rows remain untouched.

**Architecture:** Define the approved new-record defaults once in `app/service/plc_protocol.py`, next to the authoritative ordinary-PLC field metadata. Pass a copy of that mapping explicitly from `PlcSettingsModule.add_plc()` when creating the new `PlcFieldOffsetTable` row; do not add ORM/database defaults or modify the legacy missing-row compatibility path.

**Tech Stack:** Python 3.14, PyQt6, Peewee, pytest

---

## File Structure

- Modify `app/service/plc_protocol.py`: own the authoritative default-offset mapping for newly added ordinary PLCs.
- Modify `ui/modules/plc_settings.py`: use the mapping only when the user invokes `add_plc()`.
- Modify `tests/test_plc_settings.py`: lock the exact default row and prove existing rows are not backfilled or overwritten.

### Task 1: Create New PLCs With the Approved Default Offset Row

**Files:**
- Modify: `app/service/plc_protocol.py`
- Modify: `ui/modules/plc_settings.py`
- Test: `tests/test_plc_settings.py`

- [ ] **Step 1: Write the failing exact-default test**

Import `PLC_NEW_RECORD_DEFAULT_OFFSETS` alongside `PLC_FIELD_SPECS` in
`tests/test_plc_settings.py`. Rename the existing
`test_empty_plc_table_can_create_blank_configuration` test to describe the new
default behavior and, after `module.add_plc()`, compare every offset field:

```python
offset_row = PlcFieldOffsetTable.get_by_id(row.id)
actual_offsets = {
    spec.key: getattr(offset_row, spec.key)
    for spec in PLC_FIELD_SPECS
}
assert actual_offsets == {
    "start_signal_offset": "0.0",
    "current_value_offset": "2.0",
    "rotation_speed_offset": "6.0",
    "res_testing_signal_offset": "8.0",
    "res_finish_signal_offset": "8.1",
    "res_error_code_offset": "10.0",
    "res_result_offset": "12.0",
    "res_score_offset": "14.0",
    "res_spl_offset": "18.0",
    "res_loudness_offset": "22.0",
    "res_vibrate_data_offset": "26.0",
    "res_ai_score_offset": "30.0",
    "res_spl_limit_up_offset": "34.0",
    "res_spl_limit_down_offset": "38.0",
    "res_loudness_limit_up_offset": "42.0",
    "res_loudness_limit_down_offset": "46.0",
    "res_vibrate_limit_up_offset": "50.0",
    "res_vibrate_limit_down_offset": "54.0",
    "res_ai_score_limit_down_offset": "58.0",
    "sn_code_offset": "62.0",
    "product_model_offset": "80.0",
    "res_sn_code_offset": None,
    "res_product_model_offset": None,
}
assert actual_offsets == PLC_NEW_RECORD_DEFAULT_OFFSETS
assert set(PLC_NEW_RECORD_DEFAULT_OFFSETS) == {
    spec.key for spec in PLC_FIELD_SPECS
}
```

Keep the existing UI selection, editability, and `plc_changed` assertions.

- [ ] **Step 2: Write the failing existing-row preservation test**

Add a separate test that creates an existing PLC and a deliberately blank
`PlcFieldOffsetTable` row, constructs the module, invokes `add_plc()`, and
verifies the existing row remains blank:

```python
existing = PlcTable.create(plc_name="Existing", plc_open_state=0)
PlcFieldOffsetTable.create(plc_id=existing.id)
module = _make_module(monkeypatch, services)

module.add_plc()

existing_offsets = PlcFieldOffsetTable.get_by_id(existing.id)
assert all(
    getattr(existing_offsets, spec.key) is None
    for spec in PLC_FIELD_SPECS
)
new_plc = (
    PlcTable.select()
    .where(PlcTable.id != existing.id)
    .get()
)
new_offsets = PlcFieldOffsetTable.get_by_id(new_plc.id)
assert {
    spec.key: getattr(new_offsets, spec.key)
    for spec in PLC_FIELD_SPECS
} == PLC_NEW_RECORD_DEFAULT_OFFSETS
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_settings.py -k "default_offset or existing_row"
```

Expected: collection or assertion failure because
`PLC_NEW_RECORD_DEFAULT_OFFSETS` does not exist and `add_plc()` still creates an
empty offset row.

- [ ] **Step 4: Add the centralized new-record default mapping**

In `app/service/plc_protocol.py`, after `PLC_FIELD_SPECS`, define:

```python
PLC_NEW_RECORD_DEFAULT_OFFSETS: dict[str, str | None] = {
    "start_signal_offset": "0.0",
    "current_value_offset": "2.0",
    "rotation_speed_offset": "6.0",
    "res_testing_signal_offset": "8.0",
    "res_finish_signal_offset": "8.1",
    "res_error_code_offset": "10.0",
    "res_result_offset": "12.0",
    "res_score_offset": "14.0",
    "res_spl_offset": "18.0",
    "res_loudness_offset": "22.0",
    "res_vibrate_data_offset": "26.0",
    "res_ai_score_offset": "30.0",
    "res_spl_limit_up_offset": "34.0",
    "res_spl_limit_down_offset": "38.0",
    "res_loudness_limit_up_offset": "42.0",
    "res_loudness_limit_down_offset": "46.0",
    "res_vibrate_limit_up_offset": "50.0",
    "res_vibrate_limit_down_offset": "54.0",
    "res_ai_score_limit_down_offset": "58.0",
    "sn_code_offset": "62.0",
    "product_model_offset": "80.0",
    "res_sn_code_offset": None,
    "res_product_model_offset": None,
}
```

The mapping must contain exactly one key for every `PLC_FIELD_SPECS` entry.
Do not change `PlcFieldSpec.required`, field types, directions, or parser rules.

- [ ] **Step 5: Use defaults only in the UI new-PLC transaction**

In `ui/modules/plc_settings.py`, import
`PLC_NEW_RECORD_DEFAULT_OFFSETS` from `app.service.plc_protocol`, then change
only the `add_plc()` creation call:

```python
PlcFieldOffsetTable.create(
    plc_id=row.id,
    **PLC_NEW_RECORD_DEFAULT_OFFSETS,
)
```

Do not change `PlcFieldOffsetTable` field declarations. Do not pass the mapping
to `open_field_offset_dialog()` or its `get_or_create()` compatibility path.

- [ ] **Step 6: Run focused PLC settings tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_settings.py
```

Expected: all tests pass.

- [ ] **Step 7: Run the PLC protocol regression suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_plc_service_protocol.py tests/test_plc_service_current.py tests/test_plc_settings.py
```

Expected: all tests pass.

- [ ] **Step 8: Commit the implementation slice**

```powershell
git add app/service/plc_protocol.py ui/modules/plc_settings.py tests/test_plc_settings.py
git commit -m "fix: default offsets for newly added PLCs"
```

