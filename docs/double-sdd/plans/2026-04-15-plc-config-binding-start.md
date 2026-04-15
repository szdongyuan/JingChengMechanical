# PLC Config Binding And Start Routing Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace placeholder PLC text bindings with real `plc_id` config bindings, route PLC rising-edge starts through that real id, and preserve the existing manual-start and post-start runtime flow.

**Architecture:** Add `test_config_table.plc_id` as the authoritative config-to-PLC relationship, rebuild/repair old SQLite schemas so the column has real `ON DELETE SET NULL` semantics, move config editing to a PLC selector backed by `plc_table`, and change PLC ingress from text-key matching to `plc_id` matching. Keep downstream factory-mode runtime behavior unchanged after a start is accepted.

**Tech Stack:** Python, Peewee, SQLite, PyQt6, pytest

---

## File Map

- `model/test_config_table.py`
  Adds the authoritative nullable `plc_id` foreign-key field on the config model while leaving legacy text columns in place for schema tolerance only.
- `app/service/db_connection_service.py`
  Owns startup schema repair for `test_config_table.plc_id`, including SQLite table rebuild when the runtime DB lacks the required FK semantics.
- `app/service/config_service.py`
  Changes the config payload contract from text PLC binding to `plc_id`, validates referenced PLC rows, and persists/loads the new field.
- `ui/modules/config.py`
  Replaces PLC free-text inputs with a real PLC selector, collects `plc_id`, and refreshes selector state when PLC rows change.
- `app/services_container.py`
  Defines the updated factory PLC ingress payload contract and the new `plc_changed` event-bus signal, and emits PLC ingress by id.
- `app/service/factory_mode_controller.py`
  Resolves PLC starts by `plc_id` and refreshes cached selected-config payloads when PLC rows change.
- `ui/modules/plc_settings.py`
  Wires each PLC connection to the owning PLC id, emits `plc_changed` after committed PLC table updates, and refreshes its list on PLC-table change notifications.
- `app/service/plc_service.py`
  Keeps PLC rising-edge detection intact while supporting the id-carrying start callback signature.
- `tests/test_test_config_table.py`
  Covers the model field contract.
- `tests/test_db_connection_service.py`
  Covers fresh bootstrap FK creation, startup schema rebuild/repair, foreign-key delete semantics, and legacy-column preservation.
- `tests/test_config_service.py`
  Covers config payload/load/save/validate behavior for `plc_id`.
- `tests/test_config_module.py`
  Covers selector rendering, payload collection, stale-binding fallback, and in-session refresh.
- `tests/test_service_container.py`
  Covers `FactoryPlcIngressPayload`, `plc_changed`, and the id-carrying PLC start hook.
- `tests/test_plc_service.py`
  Covers the id-carrying callback invocation contract.
- `tests/test_plc_settings_module.py`
  Covers PLC-id callback wiring and PLC-table change notifications.
- `tests/test_factory_mode_controller/_support.py`
  Updates factory-mode fixtures/helpers to use `plc_id`.
- `tests/test_factory_mode_controller/test_trigger_routing.py`
  Covers PLC routing by `plc_id`, repeated binding precedence, and cache refresh after PLC-table-driven nulling.
- `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
  Updates PLC payload fixtures/assertions affected by the `FactoryPlcIngressPayload(plc_id)` contract.
- `tests/test_factory_mode_controller/test_stop_and_active_run.py`
  Updates PLC payload fixtures/assertions affected by the `FactoryPlcIngressPayload(plc_id)` contract.

### Task 1: Add The Authoritative `plc_id` Schema And Startup Repair

**Files:**
- Modify: `model/test_config_table.py`
- Modify: `app/service/db_connection_service.py`
- Test: `tests/test_test_config_table.py`
- Test: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write the failing schema and model tests**

Add/adjust tests in `tests/test_test_config_table.py` and `tests/test_db_connection_service.py` to prove:

```python
def test_test_config_table_exposes_nullable_plc_id_field():
    ...

def test_connect_repairs_test_config_table_with_real_plc_fk_delete_semantics(monkeypatch):
    ...

def test_deleting_plc_row_sets_bound_config_plc_id_to_null(monkeypatch):
    ...
```

The DB repair tests must assert all of these:

- a freshly bootstrapped runtime DB created via `create_tables(...)` exposes `plc_id REFERENCES plc_table(id) ON DELETE SET NULL`
- `test_config_table` ends up with nullable `plc_id`
- existing `config_id` values survive the rebuild
- inbound rows in `channel_config_table` / `test_config_ai_binding_table` still reference the same config rows
- existing non-PLC `test_config_table` values survive the rebuild unchanged
- rebuilt-table constraints survive, including `config_name` uniqueness
- legacy `plc_device_key` / `plc_device_label` columns remain present if they existed before
- deleting a PLC row clears `plc_id`

- [ ] **Step 2: Run the new tests to confirm the current code fails**

Run:

```bash
pytest tests/test_test_config_table.py tests/test_db_connection_service.py -v
```

Expected: FAIL because `TestConfigTable` has no `plc_id` field and `DbConnectionService` does not yet rebuild `test_config_table` with the required foreign key semantics.

- [ ] **Step 3: Implement the minimal schema/model changes**

Update `model/test_config_table.py`:

- add nullable `plc_id` as the real Peewee FK definition used by fresh bootstrap
- keep legacy PLC text columns present in the model for schema tolerance only

Update `app/service/db_connection_service.py`:

- extend startup repair so `test_config_table` gains runtime-owned nullable `plc_id`
- detect both cases that require rebuild:
  - `plc_id` missing
  - `plc_id` present but without the required FK / `ON DELETE SET NULL` semantics
- rebuild `test_config_table` in SQLite while preserving:
  - all rows
  - stable `config_id`
  - existing non-PLC column values
  - runtime-owned uniqueness / primary-key constraints
  - legacy PLC text columns if present
- initialize rebuilt rows with `plc_id = NULL`

- [ ] **Step 4: Re-run the schema/model tests**

Run:

```bash
pytest tests/test_test_config_table.py tests/test_db_connection_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit the schema/model slice**

Run:

```bash
git add model/test_config_table.py app/service/db_connection_service.py tests/test_test_config_table.py tests/test_db_connection_service.py
git commit -m "feat: add authoritative plc config binding schema"
```

Expected: one commit containing only the schema/model slice and its tests.

### Task 2: Move Config Data And UI From PLC Text Fields To `plc_id`

**Files:**
- Modify: `app/service/config_service.py`
- Modify: `ui/modules/config.py`
- Test: `tests/test_config_service.py`
- Test: `tests/test_config_module.py`

- [ ] **Step 1: Write the failing config service and config UI tests**

Add/adjust tests to prove the new payload contract:

```python
def test_load_config_returns_plc_id_and_not_legacy_plc_text_fields():
    ...

def test_save_config_persists_plc_id_and_rejects_missing_plc_rows():
    ...

def test_config_module_renders_plc_selector_from_plc_table():
    ...

def test_config_module_loads_valid_saved_plc_id_into_selector():
    ...

def test_config_module_refreshes_open_selector_after_plc_changed_signal():
    ...
```

The new tests must cover:

- top-level config payload exposes `plc_id` instead of `plc_device_key` / `plc_device_label`
- validation allows `None` and valid PLC ids only
- selector shows:
  - unbound option
  - `ID <id> - <plc_name>` when `plc_name` exists
  - `ID <id>` otherwise
- selector options are loaded from `plc_table` in ascending `id` order
- loading a valid saved `plc_id` selects the matching PLC option
- loading a missing/stale `plc_id` falls back to unbound
- saving collects `plc_id`
- open config UI refreshes on `bus.plc_changed`

- [ ] **Step 2: Run the new tests to confirm the current code fails**

Run:

```bash
pytest tests/test_config_service.py tests/test_config_module.py -v
```

Expected: FAIL because the current payload contract still uses PLC text fields and the config UI still renders free-text PLC inputs.

- [ ] **Step 3: Implement the minimal config payload and UI changes**

Update `app/service/config_service.py`:

- load/save `plc_id`
- stop exposing `plc_device_key` / `plc_device_label` in the top-level config payload
- validate that non-`None` `plc_id` references a real `PlcTable` row

Update `ui/modules/config.py`:

- replace PLC text inputs with a PLC selector widget
- load selector options from `plc_table` in ascending `id` order
- preserve current selection when `bus.plc_changed` fires and the PLC row still exists
- otherwise fall back to unbound
- collect/save `plc_id`

- [ ] **Step 4: Re-run the config service and config UI tests**

Run:

```bash
pytest tests/test_config_service.py tests/test_config_module.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit the config data/UI slice**

Run:

```bash
git add app/service/config_service.py ui/modules/config.py tests/test_config_service.py tests/test_config_module.py
git commit -m "feat: bind configs to real plc ids"
```

Expected: one commit containing only the config service/UI slice and its tests.

### Task 3: Route PLC Starts By `plc_id` And Refresh Runtime Consumers

**Files:**
- Modify: `app/services_container.py`
- Modify: `app/service/factory_mode_controller.py`
- Modify: `ui/modules/plc_settings.py`
- Modify: `app/service/plc_service.py`
- Test: `tests/test_service_container.py`
- Test: `tests/test_plc_service.py`
- Test: `tests/test_plc_settings_module.py`
- Modify: `tests/test_factory_mode_controller/_support.py`
- Test: `tests/test_factory_mode_controller/test_trigger_routing.py`
- Test: `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- Test: `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- Test: `tests/test_factory_mode_controller/test_readiness_and_selection.py`

- [ ] **Step 1: Write the failing PLC ingress/runtime-refresh tests**

Add/adjust tests to prove:

```python
def test_service_container_factory_plc_payload_uses_plc_id():
    ...

def test_service_container_exposes_plc_changed_signal():
    ...

def test_plc_settings_module_wires_start_callback_with_owning_plc_id():
    ...

def test_factory_mode_controller_routes_plc_events_by_plc_id():
    ...

def test_factory_mode_controller_refreshes_selected_config_cache_after_plc_changed():
    ...
```

The new tests must cover:

- `FactoryPlcIngressPayload(plc_id: int)`
- `EventBus.plc_changed`
- `_start_from_plc(plc_id)` emits PLC ingress payload by id
- `PlcSettingsModule` passes the owning PLC id into each `PlcService` callback
- PLC delete handling stops any live service/thread for that PLC id before deleting the row and before emitting `plc_changed`
- `FactoryModeController` matches `plc_id`
- repeated PLC bindings still resolve by enabled-order precedence
- manual start still uses the primary enabled config after the PLC payload-contract change
- unmatched PLC ingress is ignored without starting a run
- `FactoryModeController` refreshes selected config cache when PLC-linked config rows change in-session
- `PlcSettingsModule` emits and reacts to `plc_changed`

- [ ] **Step 2: Run the new PLC ingress/runtime tests to confirm the current code fails**

Run:

```bash
pytest tests/test_service_container.py tests/test_plc_service.py tests/test_plc_settings_module.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_readiness_and_selection.py -v
```

Expected: FAIL because PLC ingress still uses text-key payloads and the runtime does not yet have PLC change propagation.

- [ ] **Step 3: Implement the minimal runtime ingress and refresh changes**

Update `app/services_container.py`:

- change `FactoryPlcIngressPayload` to `plc_id: int`
- add `EventBus.plc_changed`
- change `_start_from_plc` to accept a PLC id and emit the payload

Update `ui/modules/plc_settings.py`:

- wire each PLC connection callback with the owning `row.id`
- emit `bus.plc_changed` after committed PLC table updates
- listen for `bus.plc_changed` and reload PLC rows in-session
- implement the owned PLC delete path helper so it:
  - stops any live `PlcService` / thread for the target PLC id
  - deletes the PLC row
  - lets FK nulling settle in the same transaction boundary
  - emits `bus.plc_changed` only after the delete has committed

Update `app/service/plc_service.py`:

- keep current rising-edge logic but support the id-carrying callback signature

Update `app/service/factory_mode_controller.py` and `tests/test_factory_mode_controller/_support.py`:

- resolve PLC starts by `plc_id`
- refresh cached selected configs on `plc_changed`
- keep current manual-start behavior and current readiness gates intact
- update PLC ingress fixtures/helpers used by `test_trigger_routing.py`, `test_pending_start_lifecycle.py`, `test_stop_and_active_run.py`, and manual-start coverage in `test_readiness_and_selection.py`

- [ ] **Step 4: Re-run the PLC ingress/runtime tests**

Run:

```bash
pytest tests/test_service_container.py tests/test_plc_service.py tests/test_plc_settings_module.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_readiness_and_selection.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the full targeted verification suite**

Run:

```bash
pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_service_container.py tests/test_plc_service.py tests/test_plc_settings_module.py tests/test_factory_mode_controller/test_trigger_routing.py tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_stop_and_active_run.py tests/test_factory_mode_controller/test_readiness_and_selection.py -v
```

Expected: PASS. If adjacent fixture failures appear because helper payloads still encode removed PLC text fields, update only the directly affected test helpers/fixtures and rerun the same command.

- [ ] **Step 6: Commit the PLC ingress/runtime slice**

Run:

```bash
git add app/services_container.py app/service/factory_mode_controller.py ui/modules/plc_settings.py app/service/plc_service.py tests/test_service_container.py tests/test_plc_service.py tests/test_plc_settings_module.py tests/test_factory_mode_controller/_support.py tests/test_factory_mode_controller/test_trigger_routing.py
git add tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_stop_and_active_run.py
git add tests/test_factory_mode_controller/test_readiness_and_selection.py
git commit -m "feat: route plc starts by authoritative plc id"
```

Expected: one commit containing only the PLC ingress/runtime slice and its tests.
