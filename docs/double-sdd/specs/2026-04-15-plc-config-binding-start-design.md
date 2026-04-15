# PLC Config Binding And Start Routing Design

## Context

The current PLC integration is split across two incomplete pieces:

- `plc_table` already stores real PLC connection records and `ui/modules/plc_settings.py` can connect/listen to them.
- test configs still expose placeholder text fields (`plc_device_key`, `plc_device_label`) instead of binding to a real PLC record.
- `PlcService` detects a rising edge, but `ServiceContainer._start_from_plc()` is still only a logging stub and does not route the signal into the existing factory-mode start flow.

The existing runtime behavior that must remain stable is:

- manual start still launches the primary enabled config
- factory-mode selection, pending-start handling, runtime transitions, stop behavior, auto-analysis, and review/reanalysis flow remain unchanged after a start is accepted
- PLC is only responsible for emitting a start request

Product decisions already confirmed for this change:

- configs bind to a real PLC chosen from `plc_table`
- multiple configs may bind the same PLC
- when the same PLC matches multiple enabled configs, the PLC trigger starts the first match in the current enabled order
- old persisted text-based PLC bindings do not need automatic migration
- if a PLC record is deleted, affected config bindings may be cleared instead of preserving an invalid source record

## Goals

- Replace placeholder PLC config text entry with a real PLC selection sourced from `plc_table`.
- Introduce a real config-to-PLC relationship via `test_config_table.plc_id`.
- Route PLC rising-edge events into the existing factory-mode start pipeline using the triggering PLC record id.
- Preserve current manual-start and post-start behavior.
- Keep the implementation narrowly scoped to PLC binding and start ingress only.

## Non-Goals

- No redesign of factory-mode state transitions, pending-start semantics, stop semantics, or review/reanalysis behavior.
- No changes to scan-trigger behavior.
- No requirement to migrate legacy `plc_device_key` / `plc_device_label` data into `plc_id`.
- No requirement to preserve deleted PLC identities in config records.

## Design

### 1. Persist config-to-PLC binding with `plc_id`

`test_config_table` gains a nullable `plc_id` integer foreign-key column referencing `plc_table(id)` with `ON DELETE SET NULL`.

Behavioral rules:

- `plc_id = NULL` means the config is not bound to a PLC.
- `plc_id` is the only PLC field used for runtime PLC-trigger matching.
- existing `plc_device_key` / `plc_device_label` columns are no longer authoritative for binding or PLC-trigger routing.
- runtime code should tolerate the legacy columns still existing in the table, but new binding logic must not depend on them.
- deleting a PLC row clears `test_config_table.plc_id` automatically through the database foreign-key rule rather than preserving an invalid binding state

The startup schema bootstrap/migration path will ensure the nullable foreign-key column exists for newly created or updated runtime databases.

For newly created databases, `create_tables(...)` must create `test_config_table.plc_id` with the real foreign-key definition.

For already-created SQLite databases, startup cannot rely on a simple `ALTER TABLE ... ADD COLUMN` to retrofit `ON DELETE SET NULL`. The migration contract is therefore:

- if `test_config_table.plc_id` is missing, or if `plc_id` exists but the table does not yet provide the required foreign-key `ON DELETE SET NULL` semantics, rebuild `test_config_table` into a replacement table that includes nullable `plc_id INTEGER REFERENCES plc_table(id) ON DELETE SET NULL`
- preserve stable `config_id` primary-key values during the rebuild so inbound relationships from `channel_config_table` and `test_config_ai_binding_table` remain valid against the rebuilt `test_config_table`
- preserve every existing `test_config_table` row and every existing non-PLC column value during the rebuild
- preserve existing runtime-owned constraints and schema shape on the rebuilt table, including `config_id` primary-key semantics and `config_name` uniqueness
- preserve the legacy PLC text columns if they already exist, even though runtime logic no longer depends on them
- initialize rebuilt rows with `plc_id = NULL` because legacy text bindings do not need automatic backfill

This keeps schema semantics correct without requiring legacy binding migration.

### 2. Config UI selects a real PLC from `plc_table`

`ui/modules/config.py` replaces the free-form PLC text inputs with a selector backed by `plc_table`.

UI behavior:

- the selector includes an explicit unbound option
- bound options are loaded from `plc_table` ordered by PLC id
- each option shows a deterministic user-facing label:
  - `ID <id> - <plc_name>` when `plc_name` is non-empty
  - `ID <id>` when `plc_name` is empty
- loading a config with `plc_id = NULL` shows the unbound option
- loading a config whose `plc_id` no longer exists falls back to the unbound option instead of preserving a stale broken binding
- saving the config writes only the selected `plc_id`
- while the config page is open, PLC option changes are refreshed from `plc_table` without requiring an application restart

This keeps config editing aligned with real PLC records and removes manual text mismatch risk.

### 3. Config service loads and saves `plc_id`

`ConfigService` will treat PLC binding as config-owned metadata similar to the other top-level hardware/runtime fields.

Required behavior:

- `load_config()` includes `plc_id`
- `save_config()` persists `plc_id`
- the top-level config payload contract removes `plc_device_key` and `plc_device_label`
- UI display text for PLC selection is sourced directly from `plc_table`, not from persisted config payload copies
- validation does not require a PLC binding
- validation accepts either `None` or a real integer PLC id
- if a save references a PLC id that no longer exists, the save fails with a validation error rather than silently storing a dangling id

This change is intentionally limited to the config payload contract and does not redefine unrelated top-level config fields.

### 3a. PLC-table change propagation

PLC-table edits must not leave already-open consumers stale.

The runtime change-propagation contract is:

- introduce a PLC-specific change notification on the shared event bus
- the owner of each committed `plc_table` write is responsible for emitting that notification after the write transaction has committed
- `PlcSettingsModule` emits that notification after each of its own committed `plc_table` updates because selector-visible PLC metadata is cheap to refresh and the table is small
- any future PLC create/delete path must emit that notification only after the create/delete transaction and any FK-driven `plc_id` nulling have settled in the database
- `ConfigModule` listens for that notification and refreshes its PLC selector while preserving the current `plc_id` when still valid, otherwise falling back to unbound
- `FactoryModeController` listens for that notification and refreshes its selected-config cache the same way it already reacts to config changes, so PLC-trigger matching observes the latest persisted `plc_id` values
- `PlcSettingsModule` also listens for that notification and reloads its own PLC row list so the currently displayed PLC selector stays aligned with the latest persisted `plc_table` contents

This rule also covers PLC deletion: once the database clears referencing `plc_id` values, open UI/runtime consumers must converge to the cleared state in-session.

### 4. PLC start ingress carries the triggering PLC id

PLC start ingress is upgraded from a no-argument stub to an id-carrying event.

Concrete flow:

1. `PlcSettingsModule.start_connect()` knows which `PlcTable.id` it is wiring.
2. The callback passed into `PlcService` carries that PLC id.
3. On a rising edge, `PlcService` invokes the callback for that PLC id.
4. `ServiceContainer._start_from_plc(plc_id: int)` emits `FactoryPlcIngressPayload(plc_id=int)`.
5. `FactoryModeController` resolves the first enabled config whose `plc_id` matches that ingress id and starts it with `trigger_source="plc"`.

This keeps PLC-specific knowledge at the ingress boundary and reuses the existing factory-mode start acceptance path after the match is resolved.

The factory PLC ingress contract is explicit:

- `FactoryPlcIngressPayload` carries `plc_id: int`
- `EventBus.factory_plc_event` continues carrying that payload object
- `FactoryModeController.handle_plc_event()` resolves by `payload.plc_id`

### 5. Factory-mode resolution uses enabled-order precedence

`FactoryModeController` keeps the existing precedence model used elsewhere for ordered enabled configs.

PLC trigger matching rules:

- only currently enabled configs are eligible
- the first enabled config whose `plc_id` matches the ingress PLC id is selected
- if no enabled config matches, the trigger is ignored and logged
- if the controller is not start-ready, PLC ingress keeps the current blocked/ignored behavior and must not bypass readiness checks

This preserves the user-approved rule that repeated PLC bindings are allowed and resolved by enabled order.

### 6. Manual-start and post-start flows stay untouched

Manual start remains the owner of “start the primary enabled config” behavior.

Protected behavior:

- `ControlModule` manual start still calls the same factory-mode manual start path
- `FactoryModeController.start_manual()` still starts the first enabled config
- once a start is accepted, the downstream runtime path remains identical regardless of whether the trigger source was `manual` or `plc`
- audio recording, analysis, stop, stopped-phase handling, review payloads, and reanalysis behavior are not redefined by this work

PLC only changes how a start request is ingressed, not what happens after acceptance.

### 7. PLC deletion semantics

The requested user-visible outcome is that deleted PLC bindings do not preserve a historical invalid source or an “invalid but remembered” placeholder state.

Operationally:

- deleting a PLC row clears any referencing config `plc_id` automatically via `ON DELETE SET NULL`
- the owner of a PLC delete operation must stop and tear down any live `PlcService` / thread for that `plc_id` before finalizing the delete, so runtime listener state does not outlive the deleted PLC row
- config records therefore do not keep a meaningful deleted PLC identity
- loading a config after its PLC was deleted shows the unbound option
- open `ConfigModule`, `FactoryModeController`, and `PlcSettingsModule` instances converge to the post-delete state through the committed PLC change notification
- no separate “binding invalidated” persistence flag is introduced

This keeps behavior simple and avoids an extra “invalid binding state” model.

## Testing

Coverage must prove the touched surfaces without rewriting unrelated factory-mode tests.

Required regression coverage:

- startup DB bootstrap/migration adds `test_config_table.plc_id` for runtime databases that do not have it yet
- startup DB bootstrap/migration rebuilds existing `test_config_table` rows so `plc_id` gains real foreign-key `ON DELETE SET NULL` behavior while preserving existing rows and non-PLC columns
- startup DB bootstrap/migration preserves legacy PLC text columns if they already exist, but runtime binding logic no longer depends on them
- deleting a PLC row clears bound config `plc_id` values
- config service loads/saves `plc_id`
- config service rejects saving a missing/nonexistent PLC id
- config UI renders PLC selection from `plc_table`
- config UI loads a saved `plc_id` into the selector
- config UI collects and saves the selected `plc_id`
- config UI falls back to unbound when the stored `plc_id` no longer exists
- PLC selector labels use the defined `ID <id> - <plc_name>` / `ID <id>` format
- committed PLC-table updates emit the PLC change notification and refresh open config selectors in-session
- PLC-table delete/nulling refreshes `FactoryModeController` selected-config cache in-session
- service container exposes a PLC start hook that emits PLC ingress with the triggering PLC id
- PLC service rising-edge callback can invoke the id-carrying hook
- PLC settings module wires each connection callback with the owning PLC id
- PLC delete handling tears down any live service/thread for the deleted PLC before emitting the PLC change notification
- factory-mode controller routes PLC ingress by `plc_id`
- repeated PLC bindings resolve to the first currently enabled config in enabled order
- manual start behavior remains unchanged for the primary enabled config
- unmatched PLC ingress is ignored without starting a run

Targeted verification should run at least:

`pytest tests/test_db_connection_service.py tests/test_config_service.py tests/test_config_module.py tests/test_service_container.py tests/test_plc_service.py tests/test_plc_settings_module.py tests/test_factory_mode_controller/test_trigger_routing.py -v`

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces: manual start behavior, factory-mode readiness and start gating, runtime post-start flow, PLC settings connection/listening behavior, config editing workflow
- Allowed breakage: legacy text-based PLC bindings do not need automatic migration to `plc_id`; internal config payload consumers must move from `plc_device_key` / `plc_device_label` to `plc_id`
- Migration strategy: startup schema update creates `plc_id` as a real nullable foreign key; pre-existing SQLite databases rebuild `test_config_table` to gain `ON DELETE SET NULL` semantics without backfilling legacy bindings
