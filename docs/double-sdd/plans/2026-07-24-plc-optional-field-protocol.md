# PLC Optional Field Protocol Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ordinary test-PLC field except `StartSignal` optional while matching the authoritative Siemens field types, dynamically handling `STRING[N]`, and reporting the accepted-test lifecycle through the optional `ResTestingSignal`.

**Architecture:** Introduce one protocol metadata module that owns field names, labels, types, direction, requiredness, and callback groups. Make the UI and `PlcService` consume that metadata, add two nullable persisted offsets through an additive migration, and route accepted PLC-run lifecycle state through the existing event bus. Keep process-PLC behavior and non-PLC factory triggers unchanged.

**Tech Stack:** Python 3.11+, PyQt6 signals/slots, python-snap7, Peewee, SQLite, pytest

---

## Authoritative Context

- Spec: `docs/double-sdd/specs/2026-07-24-plc-optional-field-protocol-design.md`
- Work against the current repository state; do not copy values from the developer's ignored
  `data/jingchen.db` into source code.
- The target PLC protocol is authoritative. Do not add compatibility for the old string
  error-code protocol.
- `StartSignal` is the only required offset.
- `RotationSpeed` is read into the PLC workorder but is not consumed by analysis.
- `ResTestingSignal` is set only after a PLC-triggered backend start is accepted.
- Siemens string sizes are read from the two-byte PLC string header.
- Do not modify `PlcProcessParamOffsetTable` or `PlcProcessParamService`.
- Preserve unrelated existing worktree changes, especially `base/utils.py`.

## File/Responsibility Map

- Create `app/service/plc_protocol.py`
  - Own `PlcValueType`, field direction/group metadata, all ordinary PLC field
    specifications, and offset parsing/validation.
- Modify `model/plc_field_offset_table.py`
  - Persist the nullable `rotation_speed_offset` and `res_testing_signal_offset`.
- Create `scripts/migrate_plc_optional_field_schema.py`
  - Back up an external prepared database and add only the two missing nullable columns.
- Modify `app/service/db_connection_service.py`
  - Require the two new columns at startup; never mutate schema at startup.
- Modify `ui/modules/plc_settings.py`
  - Build the field dialog from protocol metadata, require only `StartSignal`, and connect
    the testing-state event-bus signal to each PLC service.
- Modify `app/service/plc_service.py`
  - Use metadata-driven type-safe codecs, dynamic Siemens strings, optional workorder
    reads, testing-state writes, and ordered result/config callbacks.
- Modify `app/services_container.py`
  - Add the testing-state signal and carry optional `plc_id` on accepted runtime payloads.
- Modify `app/service/factory_mode_controller.py`
  - Associate an accepted PLC run with its PLC and clear/set the testing state at the
    correct lifecycle boundaries.
- Modify `app/service/analysis/test_manager.py`
  - Emit integer PLC error codes.
- Create `tests/test_plc_optional_field_schema.py`
  - Verify model fields, migration backup, idempotency, preservation, and rollback.
- Create `tests/test_plc_protocol.py`
  - Verify metadata coverage and type-aware offset validation.
- Create `tests/test_plc_service_protocol.py`
  - Verify codecs, optional reads/writes, dynamic strings, ordering, and failure behavior.
- Modify `tests/test_db_connection_service.py`
  - Verify the startup schema gate requires both new columns.
- Modify `tests/test_plc_settings.py`
  - Verify UI optionality, validation, persistence, and signal wiring.
- Modify `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
  - Verify PLC testing-state True is emitted only on accepted PLC starts.
- Modify `tests/test_factory_mode_controller/test_stop_and_active_run.py`
  - Verify success/stop/abort cleanup and non-PLC isolation.
- Modify `tests/test_factory_mode_controller/_support.py`
  - Allow test helpers to construct accepted runtime payloads with an optional PLC id.
- Modify `tests/test_test_manager/test_review_payload_emission.py`
  - Verify the result callback uses an integer error code.

## Task 1: Persist the Two New Optional Offsets Safely

**Files:**
- Create: `scripts/migrate_plc_optional_field_schema.py`
- Create: `tests/test_plc_optional_field_schema.py`
- Modify: `model/plc_field_offset_table.py`
- Modify: `app/service/db_connection_service.py`
- Modify: `tests/test_db_connection_service.py`

- [ ] **Step 1: Write failing model and migration tests**

Create `tests/test_plc_optional_field_schema.py` using the structure of
`tests/test_plc_current_schema.py`. Include:

```python
def test_plc_field_offset_model_exposes_nullable_optional_protocol_offsets():
    for name in ("rotation_speed_offset", "res_testing_signal_offset"):
        field = PlcFieldOffsetTable._meta.fields[name]
        assert field.null is True
        assert field.default is None


def test_migration_backs_up_adds_only_missing_columns_and_preserves_rows(tmp_path):
    database_path = tmp_path / "jingchen.db"
    _create_legacy_database(database_path)

    result = migration.migrate_database(database_path)

    assert result.backup_path.is_file()
    assert result.added_columns == (
        "plc_field_offset_table.rotation_speed_offset",
        "plc_field_offset_table.res_testing_signal_offset",
    )
    assert _read_existing_offsets(database_path) == (3, "0.0", "2.0")
    assert _read_new_offsets(database_path) == (None, None)
```

Also test:

- a second run creates another backup but returns `added_columns == ()`;
- an injected invalid second `ALTER TABLE` rolls back the first;
- a missing `plc_field_offset_table` raises before mutation;
- an existing one of the two columns is preserved and only the other is added.

- [ ] **Step 2: Run the new migration tests and confirm RED**

Run:

```powershell
pytest -q tests/test_plc_optional_field_schema.py
```

Expected: collection/import failure because the model fields and migration module do not
exist.

- [ ] **Step 3: Add nullable Peewee fields**

In `model/plc_field_offset_table.py`, add:

```python
rotation_speed_offset = TextField(null=True)
res_testing_signal_offset = TextField(null=True)
```

Do not add defaults or alter existing columns.

- [ ] **Step 4: Implement the additive migration**

Create `scripts/migrate_plc_optional_field_schema.py` with the same backup-first,
transactional shape as `scripts/migrate_plc_current_schema.py`, scoped to:

```python
PLC_FIELD_OFFSET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rotation_speed_offset", "TEXT DEFAULT NULL"),
    ("res_testing_signal_offset", "TEXT DEFAULT NULL"),
)
```

Required behavior:

```python
required_tables = {"plc_field_offset_table"}
connection.execute("BEGIN IMMEDIATE")
for column_name, definition in PLC_FIELD_OFFSET_COLUMNS:
    if column_name not in existing_columns:
        connection.execute(
            f'ALTER TABLE "plc_field_offset_table" '
            f'ADD COLUMN "{column_name}" {definition}'
        )
connection.commit()
```

Keep a CLI accepting one explicit database path. The script must never search for or
guess a deployment database.

- [ ] **Step 5: Run migration tests and confirm GREEN**

Run:

```powershell
pytest -q tests/test_plc_optional_field_schema.py
```

Expected: all tests pass.

- [ ] **Step 6: Write the failing startup schema-gate test**

In `tests/test_db_connection_service.py`, add a regression that creates the required
runtime tables but recreates `plc_field_offset_table` without
`rotation_speed_offset` and `res_testing_signal_offset`.

```python
with pytest.raises(DatabaseStartupError, match="rotation_speed_offset"):
    DbConnectionService().connect()
```

Also assert the service did not add either missing column.

- [ ] **Step 7: Run the startup test and confirm RED**

Run:

```powershell
pytest -q tests/test_db_connection_service.py -k "optional_plc_protocol"
```

Expected: failure because startup currently requires only `current_value_offset`.

- [ ] **Step 8: Extend the read-only startup gate**

In `app/service/db_connection_service.py`, change:

```python
_REQUIRED_PLC_FIELD_OFFSET_COLUMNS = frozenset(
    {
        "current_value_offset",
        "rotation_speed_offset",
        "res_testing_signal_offset",
    }
)
```

Do not add runtime migration or table creation.

- [ ] **Step 9: Run schema and database-service tests**

Run:

```powershell
pytest -q tests/test_plc_optional_field_schema.py tests/test_db_connection_service.py
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 1**

```powershell
git add app/service/db_connection_service.py model/plc_field_offset_table.py scripts/migrate_plc_optional_field_schema.py tests/test_db_connection_service.py tests/test_plc_optional_field_schema.py
git commit -m "feat: add optional PLC protocol offsets"
```

## Task 2: Centralize the Ordinary PLC Field Protocol and UI Validation

**Files:**
- Create: `app/service/plc_protocol.py`
- Create: `tests/test_plc_protocol.py`
- Modify: `ui/modules/plc_settings.py`
- Modify: `tests/test_plc_settings.py`

- [ ] **Step 1: Write failing protocol metadata tests**

Create `tests/test_plc_protocol.py` with assertions equivalent to:

```python
def test_start_is_the_only_required_plc_field():
    required = [spec.key for spec in PLC_FIELD_SPECS if spec.required]
    assert required == ["start_signal_offset"]


def test_authoritative_field_types_and_groups():
    assert PLC_FIELD_BY_KEY["rotation_speed_offset"].value_type is PlcValueType.INT
    assert PLC_FIELD_BY_KEY["res_testing_signal_offset"].value_type is PlcValueType.BOOL
    assert PLC_FIELD_BY_KEY["res_error_code_offset"].value_type is PlcValueType.INT
    assert PLC_FIELD_BY_KEY["sn_code_offset"].value_type is PlcValueType.STRING
    assert PLC_FIELD_BY_KEY["product_model_offset"].value_type is PlcValueType.STRING
    assert PLC_FIELD_BY_KEY["res_finish_signal_offset"].result_role == "finish"
```

Cover every field in the spec table, including the retained optional return-SN and
return-model fields. Assert unique keys.

- [ ] **Step 2: Write failing type-aware offset validation tests**

Test a pure helper:

```python
assert parse_plc_offset("8.1", PlcValueType.BOOL) == (8, 1)
assert parse_plc_offset("62.0", PlcValueType.STRING) == (62, 0)

with pytest.raises(ValueError):
    parse_plc_offset("8.8", PlcValueType.BOOL)
with pytest.raises(ValueError):
    parse_plc_offset("10.1", PlcValueType.INT)
with pytest.raises(ValueError):
    parse_plc_offset("-1.0", PlcValueType.REAL)
```

`Bool` accepts bit `0..7`; `Int`, `Real`, and `STRING` require the `.0` suffix.

- [ ] **Step 3: Run protocol tests and confirm RED**

Run:

```powershell
pytest -q tests/test_plc_protocol.py
```

Expected: import failure because `app.service.plc_protocol` does not exist.

- [ ] **Step 4: Implement protocol metadata and parsing**

Create `app/service/plc_protocol.py` with focused immutable types:

```python
class PlcValueType(Enum):
    BOOL = "bool"
    INT = "int"
    REAL = "real"
    STRING = "string"


class PlcDirection(Enum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class PlcFieldSpec:
    key: str
    label: str
    value_type: PlcValueType
    direction: PlcDirection
    required: bool = False
    workorder_key: str | None = None
    result_role: str | None = None
    result_payload_key: str | None = None
    result_order: int | None = None
    config_return: bool = False
```

Define `PLC_FIELD_SPECS` in the user-facing order approved by the spec and derive:

```python
PLC_FIELD_BY_KEY = {spec.key: spec for spec in PLC_FIELD_SPECS}
PLC_WORKORDER_FIELDS = tuple(
    spec for spec in PLC_FIELD_SPECS if spec.workorder_key is not None
)
PLC_RESULT_FIELDS = tuple(
    sorted(
        (spec for spec in PLC_FIELD_SPECS if spec.result_role == "value"),
        key=lambda spec: spec.result_order,
    )
)
PLC_CONFIG_RETURN_FIELDS = tuple(
    spec for spec in PLC_FIELD_SPECS if spec.config_return
)
```

Keep `res_testing_signal_offset` separate from normal result values and mark
`res_finish_signal_offset` as the finish role. Give every result value an explicit
payload mapping and order:

```python
("res_error_code_offset", "error_code", 10)
("res_sn_code_offset", "sn_code", 20)
("res_result_offset", "test_result", 30)
("res_score_offset", "score", 40)
("res_spl_offset", "spl", 50)
("res_loudness_offset", "loudness", 60)
("res_vibrate_data_offset", "vibrate", 70)
("res_ai_score_offset", "ai_score", 80)
```

Mark workorder mappings in the same metadata:

```python
("sn_code_offset", "sn")
("product_model_offset", "model")
("rotation_speed_offset", "rotation_speed")
```

Add tests asserting both the exact `PLC_WORKORDER_FIELDS` mapping and the exact sorted
`PLC_RESULT_FIELDS` mapping. UI display order may differ from callback order, but neither
consumer may reconstruct either mapping itself.

Implement `parse_plc_offset` without importing PyQt or snap7.

- [ ] **Step 5: Run protocol tests and confirm GREEN**

Run:

```powershell
pytest -q tests/test_plc_protocol.py
```

Expected: all tests pass.

- [ ] **Step 6: Write failing dialog optionality tests**

In `tests/test_plc_settings.py`, replace the current current-only optional test with
parameterized coverage:

```python
@pytest.mark.parametrize(
    "optional_key",
    [spec.key for spec in PLC_FIELD_SPECS if not spec.required],
)
def test_every_non_start_plc_offset_can_be_blank(qapp, optional_key):
    values = {spec.key: "0.0" for spec in PLC_FIELD_SPECS}
    values[optional_key] = None
    dialog = PlcFieldOffsetDialog(1, values)
    assert dialog.validate_values() is True
```

Add explicit tests that:

- blank start fails;
- `Bool` bit `8` fails;
- `Int`, `Real`, and `STRING` with `.1` fail;
- `RotationSpeed` and `ResTestingSignal` are present in `dialog.edits`;
- accepted blank values become `None` in `get_values()`.

- [ ] **Step 7: Run dialog tests and confirm RED**

Run:

```powershell
pytest -q tests/test_plc_settings.py -k "offset"
```

Expected: failures for the new fields and for non-current optional fields.

- [ ] **Step 8: Build the dialog from protocol metadata**

In `ui/modules/plc_settings.py`:

- import `PLC_FIELD_SPECS` and `parse_plc_offset`;
- remove the handwritten `PLC_FIELD_OFFSETS` type contract;
- if existing tests or local call sites need the tuple shape, expose only a derived view:

```python
PLC_FIELD_OFFSETS = tuple((spec.key, spec.label) for spec in PLC_FIELD_SPECS)
```

- create each edit from `PLC_FIELD_SPECS`;
- in `validate_values`, reject empty text only when `spec.required`;
- call `parse_plc_offset(value, spec.value_type)` for non-empty values;
- use a type-specific, user-readable warning and focus the invalid editor.

All existing `get_values`, `open_field_offset_dialog`, and `get_field_offsets` loops
must derive keys from the same metadata.

- [ ] **Step 9: Run protocol and UI tests**

Run:

```powershell
pytest -q tests/test_plc_protocol.py tests/test_plc_settings.py
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 2**

```powershell
git add app/service/plc_protocol.py ui/modules/plc_settings.py tests/test_plc_protocol.py tests/test_plc_settings.py
git commit -m "feat: centralize optional PLC field metadata"
```

## Task 3: Add Type-Safe Codecs, Dynamic Strings, and Optional Workorder Reads

**Files:**
- Create: `tests/test_plc_service_protocol.py`
- Modify: `app/service/plc_service.py`
- Modify: `tests/test_plc_service_current.py`

- [ ] **Step 1: Add a reusable fake S7 client in the new service test**

Create `tests/test_plc_service_protocol.py` with a byte-addressable fake:

```python
class FakeS7Client:
    def __init__(self, size=512):
        self.memory = bytearray(size)
        self.reads = []
        self.writes = []

    def db_read(self, db_number, start, size):
        self.reads.append((db_number, start, size))
        return bytearray(self.memory[start:start + size])

    def db_write(self, db_number, start, data):
        payload = bytes(data)
        self.writes.append((db_number, start, payload))
        self.memory[start:start + len(payload)] = payload
```

Use a simple bus stub that captures `log`, `plc_workorder_changed`, and
`plc_current_sampled`.

- [ ] **Step 2: Write failing Int codec tests**

Test through field-name-based service methods:

```python
service.write_field_value("res_error_code_offset", -321)
assert snap7.util.get_int(client.memory, 10) == -321
assert service.read_field_value("rotation_speed_offset") == -321
```

Include `-32768`, `32767`, out-of-range, non-integer, and no configured offset.
Missing optional fields must return a distinct skipped result or `None` without warning.

- [ ] **Step 3: Write failing dynamic STRING tests**

Prepare PLC string headers directly:

```python
client.memory[62] = 20  # max length
client.memory[63] = 7   # current length
client.memory[64:71] = b"SN-0001"

assert service.read_field_value("sn_code_offset") == "SN-0001"
assert client.reads == [(1002, 62, 2), (1002, 64, 7)]
```

Also cover:

- `STRING[15]`, `STRING[36]`, and a customer-defined different length;
- current length zero;
- maximum length zero;
- current length larger than maximum;
- a return string that fits;
- a return string longer than the PLC maximum, which raises and performs no data write;
- writing uses exactly `maximum_length + 2` bytes, not 256.

For invalid maximum/current-length headers exercised through `read_plc_values`, assert
the emitted log message includes:

```python
f"PLC {plc_id}"
"sn_code_offset"
"maximum length"  # or the concrete normalized reason used by the implementation
```

- [ ] **Step 4: Write failing optional workorder tests**

Test `_emit_plc_workorder` with combinations of SN, model, and speed:

```python
assert captured_payload == {
    "sn": "SN-0001",
    "model": "MODEL-A",
    "rotation_speed": 1450,
}
```

Make the optional SN read fail while model and speed succeed; assert the start workorder
still emits the successful values. Make all three fields absent; assert no empty
workorder signal is emitted. For an address/client read exception, also assert the log
contains the PLC id, `sn_code_offset`, and the exception reason.

- [ ] **Step 5: Run the focused service tests and confirm RED**

Run:

```powershell
pytest -q tests/test_plc_service_protocol.py
```

Expected: failures because service codecs are stringly typed, Int is unsupported,
strings are fixed-length reads, and speed is not read.

- [ ] **Step 6: Refactor `PlcService` around field metadata**

In `app/service/plc_service.py`:

- import `PLC_FIELD_BY_KEY`, `PlcValueType`, and `parse_plc_offset`;
- replace public/internal `(offset, type, value)` dispatch with field-name-based helpers;
- keep the raw byte operations private;
- implement Bool, Int, Real, and dynamic STRING codecs;
- use `snap7.util.get_int` and `snap7.util.set_int` for Int;
- validate Int values before allocating/writing;
- read the two-byte string header first;
- encode/decode ASCII as specified;
- never truncate a string silently.

Suggested boundary:

```python
def read_field_value(self, field_name: str):
    spec = PLC_FIELD_BY_KEY[field_name]
    offset = self._configured_offset(field_name)
    if offset is None:
        return None
    return self._read_value(offset, spec.value_type)


def write_field_value(self, field_name: str, value) -> bool:
    spec = PLC_FIELD_BY_KEY[field_name]
    offset = self._configured_offset(field_name)
    if offset is None:
        return False
    self._write_value(offset, spec.value_type, value)
    return True
```

Use a private sentinel if empty string must be distinguished from “field not configured.”

- [ ] **Step 7: Make optional reads independent**

Refactor `read_plc_values` so each optional field is attempted independently:

```python
for field_name in field_names:
    if not self._configured_offset(field_name):
        continue
    try:
        value = self.read_field_value(field_name)
    except Exception as exc:
        self._log_field_failure("read", field_name, exc)
        continue
    if value not in (None, ""):
        values[field_name] = value
```

Do not use this catch-and-continue path for `_read_start_bit`; failure of the required
start signal must retain the existing disconnect/reconnect behavior.

- [ ] **Step 8: Add speed to the PLC workorder**

Import and iterate `PLC_WORKORDER_FIELDS`:

```python
payload = {}
for spec in PLC_WORKORDER_FIELDS:
    value = values.get(spec.key)
    if value not in (None, ""):
        payload[spec.workorder_key] = value
```

Do not hardcode an SN/model/speed field tuple or mapping inside `PlcService`; the metadata
tests from Task 2 are the contract.

Do not modify analysis configuration or scoring code.

- [ ] **Step 9: Preserve optional current behavior**

Update `_poll_current_value` and existing current tests to use the metadata-driven REAL
reader. Confirm an absent or failed current field still does not block start detection.

- [ ] **Step 10: Run codec, current, and settings tests**

Run:

```powershell
pytest -q tests/test_plc_service_protocol.py tests/test_plc_service_current.py tests/test_plc_settings.py
```

Expected: all tests pass.

- [ ] **Step 11: Commit Task 3**

```powershell
git add app/service/plc_service.py tests/test_plc_service_protocol.py tests/test_plc_service_current.py
git commit -m "feat: add type-safe PLC codecs"
```

## Task 4: Report the Accepted PLC Test Lifecycle

**Files:**
- Modify: `app/services_container.py`
- Modify: `app/service/factory_mode_controller.py`
- Modify: `app/service/plc_service.py`
- Modify: `ui/modules/plc_settings.py`
- Modify: `tests/test_factory_mode_controller/_support.py`
- Modify: `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`
- Modify: `tests/test_factory_mode_controller/test_stop_and_active_run.py`
- Modify: `tests/test_plc_service_protocol.py`
- Modify: `tests/test_plc_settings.py`

- [ ] **Step 1: Write failing accepted-start tests**

In `tests/test_factory_mode_controller/test_pending_start_lifecycle.py`, capture the new
testing-state signal and verify:

```python
assert controller.handle_plc_event(FactoryPlcIngressPayload(plc_id=22)) is True
assert testing_states == [(22, True)]
```

The assertion must occur only after the backend invokes its `on_start_accepted`
callback. Add separate tests where:

- the PLC id has no selected config;
- validation rejects the start;
- the backend returns `False`;
- the backend raises before acceptance;
- a manual or scan start succeeds.

All of these must emit no `(plc_id, True)` except the accepted PLC case.

- [ ] **Step 2: Write failing completion/stop/abort tests**

In `tests/test_factory_mode_controller/test_stop_and_active_run.py`, establish an owned
PLC runtime and assert `(plc_id, False)` is emitted on:

- automatic review success;
- automatic no-result;
- automatic analysis error;
- normal stop;
- runtime abort.

Assert manual, scan, reanalysis, and import-audio transitions do not emit PLC testing
state.

- [ ] **Step 3: Run controller lifecycle tests and confirm RED**

Run:

```powershell
pytest -q tests/test_factory_mode_controller/test_pending_start_lifecycle.py tests/test_factory_mode_controller/test_stop_and_active_run.py -k "plc_testing"
```

Expected: failures because the event bus and ownership state do not exist.

- [ ] **Step 4: Extend accepted runtime payload and event bus**

In `app/services_container.py`:

```python
@dataclass(frozen=True)
class FactoryRuntimeStartPayload:
    config_id: int
    run_id: str
    trigger_source: str
    plc_id: int | None = None
```

Add:

```python
write_plc_testing_state_sign = pyqtSignal(int, bool)
```

The default keeps existing payload constructors source-compatible; it is not an old PLC
protocol compatibility layer.

- [ ] **Step 5: Track PLC ownership only after true backend acceptance**

In `FactoryModeController._accept_start`, capture a validated integer `plc_id` only when
`trigger_source == "plc"` and include it in `FactoryRuntimeStartPayload` inside
`_on_start_accepted`.

In `handle_runtime_started`:

```python
self._owned_runtime_plc_id = (
    payload.plc_id if payload.trigger_source == "plc" else None
)
if self._owned_runtime_plc_id is not None:
    self.bus.write_plc_testing_state_sign.emit(self._owned_runtime_plc_id, True)
```

Initialize `_owned_runtime_plc_id = None` in the constructor.

- [ ] **Step 6: Clear PLC testing ownership on every terminal path**

Add one idempotent helper:

```python
def _clear_owned_plc_testing_state(self) -> None:
    plc_id = self._owned_runtime_plc_id
    self._owned_runtime_plc_id = None
    if plc_id is not None:
        self.bus.write_plc_testing_state_sign.emit(plc_id, False)
```

Call it before ownership is discarded in review-ready, no-result/error, stop, abort, and
general owned-runtime reset paths. Do not emit if no PLC owns the run.

- [ ] **Step 7: Update test helpers**

In `tests/test_factory_mode_controller/_support.py`, extend `_emit_owned_runtime_start`
with `plc_id: int | None = None` and pass it into `FactoryRuntimeStartPayload`. Existing
callers should continue to default to non-PLC ownership unless they explicitly provide
the id.

- [ ] **Step 8: Write failing PLC service testing-state tests**

In `tests/test_plc_service_protocol.py`, assert:

- a matching PLC id with configured `res_testing_signal_offset` writes the bit;
- a different PLC id writes nothing;
- a missing testing offset writes nothing and logs no missing-offset warning;
- repeated False writes are safe.

- [ ] **Step 9: Implement the PLC service slot and UI wiring**

In `app/service/plc_service.py`, add a `@pyqtSlot(int, bool)` method that filters by
`self._plc_id`, skips an empty offset, and uses `write_field_value` for the Bool.

In `ui/modules/plc_settings.py`, alongside the existing result and limit connections,
connect:

```python
self.services.bus.write_plc_testing_state_sign.connect(
    plc_service.write_testing_state
)
```

Update the settings test bus stub and assert the connection is established without
changing process-PLC wiring.

- [ ] **Step 10: Run lifecycle and service tests**

Run:

```powershell
pytest -q tests/test_factory_mode_controller tests/test_plc_service_protocol.py tests/test_plc_settings.py
```

Expected: all tests pass.

- [ ] **Step 11: Commit Task 4**

```powershell
git add app/services_container.py app/service/factory_mode_controller.py app/service/plc_service.py ui/modules/plc_settings.py tests/test_factory_mode_controller tests/test_plc_service_protocol.py tests/test_plc_settings.py
git commit -m "feat: report PLC testing lifecycle"
```

## Task 5: Make Configuration and Result Returns Optional, Typed, and Ordered

**Files:**
- Modify: `app/service/plc_service.py`
- Modify: `app/service/analysis/test_manager.py`
- Modify: `tests/test_plc_service_protocol.py`
- Modify: `tests/test_test_manager/test_review_payload_emission.py`

- [ ] **Step 1: Write failing result-order and optionality tests**

In `tests/test_plc_service_protocol.py`, configure a subset of result fields and use the
fake client's ordered writes to assert:

```python
service.last_write_test_result_to_plc(
    plc_id,
    {
        "finished": True,
        "error_code": 0,
        "sn_code": "SN-0001",
        "test_result": True,
        "score": 91.5,
    },
)

assert written_field_order == [
    "res_testing_signal_offset",  # False
    "res_error_code_offset",      # Int
    "res_sn_code_offset",         # Dynamic Siemens STRING
    "res_result_offset",          # Bool
    "res_score_offset",           # Real
    "res_finish_signal_offset",   # True, always last
]
```

Initialize the return-SN PLC header and verify its dynamic length before asserting
ordering. Leave all other result offsets blank. Assert no missing-offset warnings and no
writes for them. Also assert `PLC_RESULT_FIELDS` obtains values via each spec's
`result_payload_key`; `PlcService` must not contain a second handwritten result mapping.

- [ ] **Step 2: Write failing partial-failure tests**

Inject a client failure on a configured result field and assert:

- the error log contains PLC id and field key;
- `res_finish_signal_offset` is not written;
- an absent field is not treated as failure;
- an invalid or out-of-range error code prevents finish;
- a result callback for a different PLC id writes nothing.

- [ ] **Step 3: Write failing configuration-return tests**

Configure only two limit fields and assert:

- only those two are written;
- types come from `PLC_CONFIG_RETURN_FIELDS`;
- blank model/limit offsets are skipped silently;
- a dynamically sized configured return-model string uses the PLC header length;
- a conversion/write error names the field and does not produce a false success log.

- [ ] **Step 4: Write the failing TestManager payload test**

In `tests/test_test_manager/test_review_payload_emission.py`, update or add:

```python
payload = bus.write_test_result_sign.emit.call_args.args[1]
assert payload["error_code"] == 0
assert isinstance(payload["error_code"], int)
```

Run:

```powershell
pytest -q tests/test_plc_service_protocol.py tests/test_test_manager/test_review_payload_emission.py
```

Expected: failures because error code is still a string and finish is not guarded by
all configured result writes.

- [ ] **Step 5: Derive callback fields from protocol metadata**

Remove `PlcService.limit_field`, the local result tuple, and the local string/Bool/Real
sets. Iterate `PLC_CONFIG_RETURN_FIELDS` and explicitly ordered `PLC_RESULT_FIELDS`.
For results, obtain values through `spec.result_payload_key`.

Use one helper with an explicit outcome:

```python
def _write_configured_field(self, field_name: str, value) -> bool:
    if self._configured_offset(field_name) is None:
        return True  # intentionally absent, not a failed write
    self.write_field_value(field_name, value)
    return True
```

Catch errors at the callback boundary so the log contains the operation, PLC id, and
field key.

- [ ] **Step 6: Implement ordered result completion**

`last_write_test_result_to_plc` must:

1. ignore nonmatching PLC ids or disconnected clients;
2. clear the optional testing-state field;
3. iterate configured result value fields in metadata order;
4. stop on the first configured-field failure;
5. write finish only when `test_result["finished"]` is true and all configured result
   values succeeded.

Do not write finish in a `finally` block. A missing finish offset is an intentional skip.

- [ ] **Step 7: Implement typed, optional config return**

`last_write_limit_to_plc` must iterate metadata, skip absent payload keys and blank
offsets, and use the field's declared type. Emit “写入限值完成” only if every configured,
present field succeeds.

- [ ] **Step 8: Emit integer error codes**

In `app/service/analysis/test_manager.py`, change the PLC result payload boundary from:

```python
"error_code": "0",
```

to:

```python
"error_code": 0,
```

Do not introduce string fallback conversion inside `TestManager`; range/type enforcement
belongs at the PLC write boundary.

- [ ] **Step 9: Run result, controller, and service tests**

Run:

```powershell
pytest -q tests/test_plc_service_protocol.py tests/test_plc_service_current.py tests/test_test_manager/test_review_payload_emission.py tests/test_factory_mode_controller
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 5**

```powershell
git add app/service/plc_service.py app/service/analysis/test_manager.py tests/test_plc_service_protocol.py tests/test_test_manager/test_review_payload_emission.py
git commit -m "fix: honor optional PLC callback fields"
```

## Task 6: Verify the Complete PLC Protocol Change

**Files:**
- No source changes expected
- If a verification failure exposes a bug, modify only the files owned by the failing
  task and add a regression test before fixing it

- [ ] **Step 1: Run the full focused PLC and factory suite**

Run:

```powershell
pytest -q tests/test_plc_optional_field_schema.py tests/test_plc_protocol.py tests/test_plc_service_protocol.py tests/test_plc_service_current.py tests/test_plc_settings.py tests/test_db_connection_service.py tests/test_factory_mode_controller tests/test_test_manager/test_review_payload_emission.py
```

Expected: all tests pass.

- [ ] **Step 2: Run all repository tests**

Run:

```powershell
pytest -q
```

Expected: all tests pass. Existing unrelated warnings may remain, but there must be no
new failures or unhandled Qt-thread warnings.

- [ ] **Step 3: Inspect the final diff and protocol invariants**

Run:

```powershell
git diff --check
git status --short
git log --oneline -6
```

Confirm:

- only `StartSignal` is required;
- all type decisions come from `app/service/plc_protocol.py`;
- no `db_read(..., 256)` or fixed `set_string(..., 254)` remains in ordinary PLC code;
- no string error-code write remains;
- process-PLC files are unchanged;
- no ignored `data/` file or unrelated user edit is staged.

- [ ] **Step 4: Verify migration against a disposable database**

Create a temporary legacy database through the migration test helper or copy a fixture
database into a temporary directory, then run:

```powershell
python scripts/migrate_plc_optional_field_schema.py <absolute-path-to-temp-jingchen.db>
```

Expected:

- the CLI prints the explicit database path;
- one backup file is created beside the temporary database;
- both nullable columns exist afterward;
- existing offset values are unchanged.

Do not run this command against `data/jingchen.db` from an implementation worktree.
Migration of the external authoritative deployment database is an operator handoff and
must use an explicitly verified absolute path.

- [ ] **Step 5: Record final verification evidence**

Capture the exact focused-suite and full-suite pass counts, the migration CLI output,
and `git status --short` for the final handoff. If verification required a fix, add a
regression test, rerun the affected task review, and commit with a scoped message before
repeating this task.
