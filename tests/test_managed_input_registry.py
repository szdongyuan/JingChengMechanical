from __future__ import annotations

import _thread
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from app.service.managed_input_contracts import (
    MAX_CALIBRATABLE_INPUT_CHANNELS,
    AddManagedInputCommand,
    BackendDiscoveryResult,
    BackendInputScan,
    BackendScanId,
    CalibrateManagedInputCommand,
    HardwareEndpointIdentity,
    InputEndpointDescriptor,
    InputEndpointObservation,
    ManagedHardwareId,
    ManagedInputAvailability,
    ManagedInputBlocker,
    ManagedInputCommandKind,
    ManagedInputError,
    ManagedInputErrorCode,
    ManagedInputProcessState,
    ManagedInputResultStatus,
    OfflineMaintenanceBlockerSnapshot,
    RefreshManagedInputsCommand,
    RemoveManagedInputCommand,
    ResidentCalibrationMeasurement,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
    SelectOrReplaceManagedInputCommand,
    StableUidCapability,
    parse_calibration_json,
    serialize_calibration_json,
)
from app.service.managed_input_registry import ManagedInputRegistry
from model.managed_audio_input_table import ManagedAudioInputTable


class FakeDiscovery:
    def __init__(self) -> None:
        self.refresh_requires_reinitialize = False
        self.results: list[BackendDiscoveryResult] = []
        self.calls: list[BackendScanId] = []
        self.on_discover = None

    def discover(self, scan_id: BackendScanId) -> BackendDiscoveryResult:
        self.calls.append(scan_id)
        if self.on_discover is not None:
            self.on_discover()
        return self.results.pop(0)

    def requery_validated_input_endpoint(
        self,
        scan_id: BackendScanId,
        device_index: int,
    ) -> InputEndpointObservation | None:
        raise AssertionError("management must not preflight an endpoint")


class FakeBlocker:
    def __init__(
        self,
        snapshot: OfflineMaintenanceBlockerSnapshot | object | None = None,
    ) -> None:
        self.current = snapshot or OfflineMaintenanceBlockerSnapshot(0, False)
        self.calls: list[ManagedHardwareId | None] = []

    def snapshot(
        self,
        hardware_id: ManagedHardwareId | None,
    ) -> OfflineMaintenanceBlockerSnapshot:
        self.calls.append(hardware_id)
        if isinstance(self.current, BaseException):
            raise self.current
        return self.current  # type: ignore[return-value]


class FakeReferences:
    def __init__(self) -> None:
        self.referenced: set[ManagedHardwareId] = set()
        self.calls: list[ManagedHardwareId] = []

    def configuration_references(self, hardware_id: ManagedHardwareId) -> bool:
        self.calls.append(hardware_id)
        return hardware_id in self.referenced


class FakeResident:
    def __init__(self) -> None:
        self.measurements: list[ResidentCalibrationMeasurement | BaseException] = []
        self.on_measure = None
        self.measure_calls: list[
            tuple[
                ManagedHardwareId,
                ResidentStreamGenerationStamp,
                int,
                int,
            ]
        ] = []

    def open_startup_streams(
        self,
        endpoints: tuple[HardwareEndpointIdentity, ...],
    ) -> ResidentStartupOpenResult:
        raise AssertionError("Task 2 management must not open streams")

    def measure_calibration(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
        channel_index: int,
        duration_frames: int,
    ) -> ResidentCalibrationMeasurement:
        self.measure_calls.append(
            (hardware_id, generation, channel_index, duration_frames)
        )
        if self.on_measure is not None:
            self.on_measure()
        result = self.measurements.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close_all(self, timeout_seconds: float):
        raise AssertionError("Task 2 management must not close streams")


@pytest.fixture
def managed_table(setup_test_db):
    setup_test_db.bind([ManagedAudioInputTable])
    setup_test_db.create_tables([ManagedAudioInputTable])
    try:
        yield setup_test_db
    finally:
        setup_test_db.drop_tables([ManagedAudioInputTable])


def descriptor(
    *,
    device_name: str = "Input",
    hostapi_name: str = "Host API",
    sample_rate_hz: float = 48_000.0,
    input_channels: int = 2,
) -> InputEndpointDescriptor:
    return InputEndpointDescriptor(
        device_name,
        hostapi_name,
        sample_rate_hz,
        input_channels,
    )


def calibration_json(channels: int = 2) -> str:
    return serialize_calibration_json(
        tuple((index, False, 0, None) for index in range(channels)),
        expected_channels=channels,
    )


def make_row(
    alias: str,
    *,
    uid: str | None = None,
    endpoint_descriptor: InputEndpointDescriptor | None = None,
):
    selected = endpoint_descriptor or descriptor()
    return ManagedAudioInputTable.create(
        alias=alias,
        stable_backend_uid=uid,
        descriptor_device_name=selected.device_name,
        descriptor_hostapi_name=selected.hostapi_name,
        descriptor_sample_rate_hz=selected.sample_rate_hz,
        descriptor_input_channels=selected.input_channels,
        calibration_json=calibration_json(selected.input_channels),
    )


def make_observation(
    scan_id: BackendScanId,
    observation_id: str,
    *,
    device_index: int = 0,
    uid: str | None = None,
    endpoint_descriptor: InputEndpointDescriptor | None = None,
) -> InputEndpointObservation:
    return InputEndpointObservation(
        scan_id=scan_id,
        observation_id=observation_id,
        device_index=device_index,
        hostapi_index=0,
        stable_backend_uid=uid,
        descriptor=endpoint_descriptor or descriptor(),
    )


def make_scan(
    scan_id: BackendScanId,
    *observations: InputEndpointObservation,
    capability: StableUidCapability = StableUidCapability.NONE,
) -> BackendInputScan:
    return BackendInputScan(scan_id, capability, observations)


def new_registry(
    *,
    discovery: FakeDiscovery | None = None,
    blocker: FakeBlocker | None = None,
    references: FakeReferences | None = None,
    resident: FakeResident | None = None,
    process_id_factory=lambda: "process-1",
) -> ManagedInputRegistry:
    return ManagedInputRegistry(
        table=ManagedAudioInputTable,
        discovery=discovery or FakeDiscovery(),
        blocker=blocker or FakeBlocker(),
        references=references or FakeReferences(),
        resident=resident or FakeResident(),
        monotonic=lambda: 1.0,
        process_instance_id_factory=process_id_factory,
    )


def seed_ready(
    registry: ManagedInputRegistry,
    *,
    observations: tuple[InputEndpointObservation, ...] = (),
    capability: StableUidCapability = StableUidCapability.NONE,
    ready_streams: tuple[ResidentReadyStream, ...] = (),
) -> None:
    scan_id = (
        observations[0].scan_id
        if observations
        else BackendScanId("process-1", 1)
    )
    scan = BackendInputScan(scan_id, capability, observations)
    assert registry._lock.acquire(blocking=False)
    try:
        registry._process_state = ManagedInputProcessState.ACCEPTING_RUNS
        registry._current_scan = scan
        registry._observations = observations
        registry._startup_stable_uid_capability = capability
        registry._ready_streams = ready_streams
        ready_by_id = {
            stream.endpoint.hardware_id: stream for stream in ready_streams
        }
        registry._row_snapshots = tuple(
            replace(
                row,
                availability=ManagedInputAvailability.AVAILABLE,
                endpoint=ready_by_id[row.hardware_id].endpoint,
                resident_generation=ready_by_id[row.hardware_id].generation,
                error_code=None,
            )
            if row.hardware_id in ready_by_id
            else row
            for row in registry._row_snapshots
        )
        registry._next_scan_sequence = scan_id.sequence + 1
    finally:
        registry._lock.release()


def test_registry_loads_zero_through_six_rows_in_canonical_order(managed_table):
    assert new_registry().inventory_snapshot().rows == ()

    for alias in ("Zulu", "Alpha", "Echo", "Bravo", "Delta", "Charlie"):
        make_row(alias)

    rows = new_registry().inventory_snapshot().rows
    assert [row.alias for row in rows] == sorted(
        ["Zulu", "Alpha", "Echo", "Bravo", "Delta", "Charlie"]
    )
    assert all(type(row.hardware_id) is ManagedHardwareId for row in rows)
    assert all(row.availability is ManagedInputAvailability.UNAVAILABLE for row in rows)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("alias", " not-canonical "),
        ("hardware_revision", 0),
        ("descriptor_sample_rate_hz", float("inf")),
        ("descriptor_input_channels", 0),
        ("calibration_revision", -1),
        ("calibration_json", "{}"),
    ],
)
def test_registry_fails_closed_on_malformed_row(managed_table, column, value):
    row = make_row("valid")
    managed_table.execute_sql("PRAGMA ignore_check_constraints = ON")
    ManagedAudioInputTable.update({column: value}).where(
        ManagedAudioInputTable.hardware_id == row.hardware_id
    ).execute()
    managed_table.execute_sql("PRAGMA ignore_check_constraints = OFF")

    discovery = FakeDiscovery()
    registry = new_registry(discovery=discovery)

    result = registry.bootstrap()

    assert result.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert result.inventory.scan_id is None
    assert result.inventory.scan_error_code is (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    )
    assert result.ready_streams == ()
    assert result.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert discovery.calls == []


def test_registry_fails_closed_on_seventh_row(managed_table):
    for index in range(7):
        make_row(f"row-{index}")
    discovery = FakeDiscovery()
    result = new_registry(discovery=discovery).bootstrap()

    assert result.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert result.inventory.scan_error_code is (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    )
    assert result.ready_streams == ()
    assert discovery.calls == []


@pytest.mark.parametrize("valid_prefix_count", (0, 1))
def test_registry_load_fails_atomically_on_persisted_uncalibratable_row(
    managed_table,
    valid_prefix_count,
):
    for index in range(valid_prefix_count):
        make_row(f"valid-{index}")
    unsupported_channels = MAX_CALIBRATABLE_INPUT_CHANNELS + 1
    compact = json.dumps(
        {
            "version": 1,
            "channels": [
                {
                    "channel_index": channel_index,
                    "trusted": False,
                    "channel_calibration_revision": 0,
                    "correction_db": None,
                }
                for channel_index in range(unsupported_channels)
            ],
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    managed_table.execute_sql("PRAGMA ignore_check_constraints = ON")
    try:
        ManagedAudioInputTable.create(
            alias="invalid",
            stable_backend_uid=None,
            descriptor_device_name="Device",
            descriptor_hostapi_name="Host API",
            descriptor_sample_rate_hz=48_000.0,
            descriptor_input_channels=unsupported_channels,
            calibration_json=compact,
        )
    finally:
        managed_table.execute_sql("PRAGMA ignore_check_constraints = OFF")

    discovery = FakeDiscovery()
    owner = new_registry(discovery=discovery)

    result = owner.bootstrap()

    assert result.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert result.inventory.scan_error_code is (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    )
    assert result.ready_streams == ()
    assert discovery.calls == []


def test_query_returns_frozen_values_and_never_orm_rows(managed_table):
    orm_row = make_row("Input A")
    registry = new_registry()

    inventory = registry.inventory_snapshot()
    assert inventory.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert inventory.active_capture_count is None
    assert inventory.rows[0].hardware_id == ManagedHardwareId(orm_row.hardware_id)
    assert not isinstance(inventory.rows[0], ManagedAudioInputTable)
    with pytest.raises(FrozenInstanceError):
        inventory.rows[0].alias = "changed"  # type: ignore[misc]


def test_query_projects_process_scan_and_error_state_exactly(managed_table):
    registry = new_registry()
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "observation")
    seed_ready(registry, observations=(observation,))
    assert registry._lock.acquire(blocking=False)
    try:
        registry._scan_error_code = ManagedInputErrorCode.INVARIANT_VIOLATION
    finally:
        registry._lock.release()

    inventory = registry.inventory_snapshot()
    assert inventory.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert inventory.scan_id == scan_id
    assert inventory.scan_error_code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert registry.endpoint_snapshot() == (observation,)


def test_process_instance_id_factory_is_called_once_and_stable(managed_table):
    calls = 0

    def factory() -> str:
        nonlocal calls
        calls += 1
        return "stable-process"

    registry = new_registry(process_id_factory=factory)
    assert calls == 1
    assert registry._process_instance_id == "stable-process"
    assert (
        registry.inventory_snapshot().process_state
        is ManagedInputProcessState.BOOTSTRAPPING
    )
    assert calls == 1


def test_registry_owns_exactly_one_thread_lock(managed_table):
    registry = new_registry()
    locks = [
        value
        for value in vars(registry).values()
        if type(value) is _thread.LockType
    ]
    assert locks == [registry._lock]
    assert registry._lock.acquire(blocking=False) is True
    assert registry._lock.acquire(blocking=False) is False
    registry._lock.release()


def command_for(
    kind: ManagedInputCommandKind,
    hardware_id: ManagedHardwareId,
    scan_id: BackendScanId,
    observation_id: str,
):
    if kind is ManagedInputCommandKind.ADD:
        return AddManagedInputCommand("new input", scan_id, observation_id)
    if kind is ManagedInputCommandKind.SELECT_OR_REPLACE:
        return SelectOrReplaceManagedInputCommand(
            hardware_id,
            1,
            scan_id,
            observation_id,
        )
    if kind is ManagedInputCommandKind.REMOVE:
        return RemoveManagedInputCommand(hardware_id, 1)
    if kind is ManagedInputCommandKind.REFRESH:
        return RefreshManagedInputsCommand()
    return CalibrateManagedInputCommand(
        hardware_id,
        1,
        0,
        0,
        -20.0,
        100,
    )


def invoke(registry: ManagedInputRegistry, kind: ManagedInputCommandKind, command):
    return {
        ManagedInputCommandKind.ADD: registry.add,
        ManagedInputCommandKind.SELECT_OR_REPLACE: registry.select_or_replace,
        ManagedInputCommandKind.REMOVE: registry.remove,
        ManagedInputCommandKind.REFRESH: registry.refresh,
        ManagedInputCommandKind.CALIBRATE: registry.calibrate,
    }[kind](command)


@pytest.fixture
def guarded_registry(managed_table):
    row = make_row("target")
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "target-observation")
    discovery = FakeDiscovery()
    blocker = FakeBlocker()
    references = FakeReferences()
    resident = FakeResident()
    registry = new_registry(
        discovery=discovery,
        blocker=blocker,
        references=references,
        resident=resident,
    )
    seed_ready(registry, observations=(observation,))
    return (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    )


@pytest.mark.parametrize("kind", tuple(ManagedInputCommandKind))
def test_held_mutex_returns_immediate_busy_without_query(
    guarded_registry,
    kind,
):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    command = command_for(kind, hardware_id, scan_id, observation.observation_id)
    assert registry._lock.acquire(blocking=False)
    try:
        result = invoke(registry, kind, command)
    finally:
        registry._lock.release()

    assert result.status is ManagedInputResultStatus.BUSY
    assert result.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert result.inventory is None
    assert blocker.calls == []
    assert discovery.calls == []
    assert references.calls == []
    assert resident.measure_calls == []


@pytest.mark.parametrize("kind", tuple(ManagedInputCommandKind))
def test_bootstrapping_rejects_every_management_command_before_external_work(
    guarded_registry,
    kind,
):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    registry._process_state = ManagedInputProcessState.BOOTSTRAPPING
    command = command_for(kind, hardware_id, scan_id, observation.observation_id)

    result = invoke(registry, kind, command)

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors == (
        ManagedInputError(
            ManagedInputErrorCode.INVARIANT_VIOLATION,
            "command_phase",
            "Management is unavailable while input bootstrap is incomplete.",
        ),
    )
    assert blocker.calls == []
    assert discovery.calls == []
    assert references.calls == []
    assert resident.measure_calls == []


@pytest.mark.parametrize(
    "kind",
    (
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    ),
)
def test_restart_required_rejects_persistent_commands_before_external_work(
    guarded_registry,
    kind,
):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    registry._process_state = ManagedInputProcessState.RESTART_REQUIRED
    command = command_for(kind, hardware_id, scan_id, observation.observation_id)

    result = invoke(registry, kind, command)

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.RESTART_REQUIRED
    assert blocker.calls == []
    assert discovery.calls == []
    assert references.calls == []
    assert resident.measure_calls == []


@pytest.mark.parametrize(
    "kind",
    (
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    ),
)
def test_scan_diagnostic_rejects_persistent_commands_before_external_work(
    guarded_registry,
    kind,
):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    registry._scan_error_code = ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    command = command_for(kind, hardware_id, scan_id, observation.observation_id)

    result = invoke(registry, kind, command)

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    assert blocker.calls == []
    assert discovery.calls == []
    assert references.calls == []
    assert resident.measure_calls == []


@pytest.mark.parametrize(
    "latched_code",
    (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ),
)
def test_scan_diagnostic_wins_when_restart_required_is_already_latched(
    managed_table,
    latched_code,
):
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "endpoint")
    discovery = FakeDiscovery()
    discovery.results.append(scan_error(latched_code))
    blocker = FakeBlocker()
    resident = FakeResident()
    registry = new_registry(
        discovery=discovery,
        blocker=blocker,
        resident=resident,
    )
    seed_ready(registry, observations=(observation,))

    mutation = registry.add(
        AddManagedInputCommand("first", scan_id, observation.observation_id)
    )
    invalid_refresh = registry.refresh(RefreshManagedInputsCommand())
    before_count = ManagedAudioInputTable.select().count()

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand("second", scan_id, observation.observation_id)
        ),
    )

    assert mutation.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    assert invalid_refresh.errors[0].code is latched_code
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.process_state is ManagedInputProcessState.RESTART_REQUIRED
    assert result.blocker is None
    assert result.errors[0].code is latched_code
    assert result.observations == ()
    assert result.inventory.restart_required is True
    assert result.inventory.scan_error_code is latched_code
    assert blocker.calls == [None, None]
    assert discovery.calls == [BackendScanId("process-1", 2)]
    assert resident.measure_calls == []
    assert transactions == []
    assert ManagedAudioInputTable.select().count() == before_count


@pytest.mark.parametrize(
    ("snapshot", "expected_blocker", "expected_count"),
    [
        (
            OfflineMaintenanceBlockerSnapshot(None, None),
            ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN,
            None,
        ),
        (
            OfflineMaintenanceBlockerSnapshot(3, True),
            ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS,
            3,
        ),
        (
            OfflineMaintenanceBlockerSnapshot(0, True),
            ManagedInputBlocker.TARGET_RESOURCE_BUSY,
            0,
        ),
    ],
)
@pytest.mark.parametrize("kind", tuple(ManagedInputCommandKind))
def test_blocker_precedence_is_exact_for_every_command(
    guarded_registry,
    snapshot,
    expected_blocker,
    expected_count,
    kind,
):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    blocker.current = snapshot
    command = command_for(kind, hardware_id, scan_id, observation.observation_id)

    result = invoke(registry, kind, command)

    assert result.status is ManagedInputResultStatus.BUSY
    assert result.blocker is expected_blocker
    assert result.inventory.active_capture_count == expected_count
    expected_target = (
        None
        if kind
        in (
            ManagedInputCommandKind.ADD,
            ManagedInputCommandKind.REFRESH,
            ManagedInputCommandKind.CALIBRATE,
        )
        else hardware_id
    )
    assert blocker.calls == [expected_target]
    assert discovery.calls == []
    assert references.calls == []
    assert resident.measure_calls == []


def test_invalid_blocker_response_is_unknown_and_never_zero(guarded_registry):
    (
        registry,
        discovery,
        blocker,
        references,
        resident,
        hardware_id,
        scan_id,
        observation,
    ) = guarded_registry
    blocker.current = object()

    result = registry.add(
        AddManagedInputCommand("new", scan_id, observation.observation_id)
    )

    assert result.status is ManagedInputResultStatus.BUSY
    assert result.blocker is ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN
    assert result.inventory.active_capture_count is None
    assert blocker.calls == [None]


def transaction_statements(database, action):
    statements: list[str] = []
    database.connection().set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        database.connection().set_trace_callback(None)
    transaction_starts = [
        sql for sql in statements if sql.strip().upper().startswith("BEGIN")
    ]
    return result, transaction_starts


@pytest.mark.parametrize("kind", tuple(ManagedInputCommandKind))
def test_clean_guard_success_projects_authoritative_zero(managed_table, kind):
    if kind is ManagedInputCommandKind.ADD:
        scan_id = BackendScanId("process-1", 1)
        observation = make_observation(scan_id, "endpoint")
        registry = new_registry()
        seed_ready(registry, observations=(observation,))
        result = registry.add(
            AddManagedInputCommand("new", scan_id, observation.observation_id)
        )
    elif kind is ManagedInputCommandKind.SELECT_OR_REPLACE:
        row = make_row("target")
        hardware_id = ManagedHardwareId(row.hardware_id)
        scan_id = BackendScanId("process-1", 1)
        observation = make_observation(scan_id, "replacement")
        registry = new_registry()
        seed_ready(registry, observations=(observation,))
        result = registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                1,
                scan_id,
                observation.observation_id,
            )
        )
    elif kind is ManagedInputCommandKind.REMOVE:
        row = make_row("target")
        hardware_id = ManagedHardwareId(row.hardware_id)
        registry = new_registry()
        seed_ready(registry)
        result = registry.remove(RemoveManagedInputCommand(hardware_id, 1))
    elif kind is ManagedInputCommandKind.REFRESH:
        startup_id = BackendScanId("process-1", 1)
        startup = make_observation(startup_id, "startup")
        refresh_id = BackendScanId("process-1", 2)
        refreshed = make_observation(refresh_id, "refreshed")
        discovery = FakeDiscovery()
        discovery.results.append(
            BackendDiscoveryResult(make_scan(refresh_id, refreshed), None)
        )
        registry = new_registry(discovery=discovery)
        seed_ready(registry, observations=(startup,))
        result = registry.refresh(RefreshManagedInputsCommand())
    else:
        make_row("target")
        registry, resident, hardware_id, generation = calibration_registry()
        resident.measurements.append(
            ResidentCalibrationMeasurement(generation, 0, -21.0, 100)
        )
        result = registry.calibrate(
            CalibrateManagedInputCommand(
                hardware_id,
                1,
                0,
                0,
                -20.0,
                100,
            )
        )

    assert result.status in (
        ManagedInputResultStatus.SUCCEEDED,
        ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
    )
    assert result.inventory.active_capture_count == 0


@pytest.mark.parametrize("kind", tuple(ManagedInputCommandKind))
def test_clean_guard_post_validation_result_projects_authoritative_zero(
    managed_table,
    kind,
):
    if kind is ManagedInputCommandKind.ADD:
        make_row("duplicate")
        scan_id = BackendScanId("process-1", 1)
        observation = make_observation(scan_id, "endpoint")
        registry = new_registry()
        seed_ready(registry, observations=(observation,))
        result = registry.add(
            AddManagedInputCommand(
                "duplicate",
                scan_id,
                observation.observation_id,
            )
        )
    elif kind is ManagedInputCommandKind.SELECT_OR_REPLACE:
        row = make_row("target")
        hardware_id = ManagedHardwareId(row.hardware_id)
        scan_id = BackendScanId("process-1", 1)
        observation = make_observation(scan_id, "replacement")
        registry = new_registry()
        seed_ready(registry, observations=(observation,))
        result = registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                2,
                scan_id,
                observation.observation_id,
            )
        )
    elif kind is ManagedInputCommandKind.REMOVE:
        row = make_row("target")
        hardware_id = ManagedHardwareId(row.hardware_id)
        references = FakeReferences()
        references.referenced.add(hardware_id)
        registry = new_registry(references=references)
        seed_ready(registry)
        result = registry.remove(RemoveManagedInputCommand(hardware_id, 1))
    elif kind is ManagedInputCommandKind.REFRESH:
        startup_id = BackendScanId("process-1", 1)
        startup = make_observation(startup_id, "startup")
        discovery = FakeDiscovery()
        discovery.results.append(
            scan_error(ManagedInputErrorCode.INVARIANT_VIOLATION)
        )
        registry = new_registry(discovery=discovery)
        seed_ready(registry, observations=(startup,))
        result = registry.refresh(RefreshManagedInputsCommand())
    else:
        make_row("target")
        registry, resident, hardware_id, generation = calibration_registry(
            available=False
        )
        result = registry.calibrate(
            CalibrateManagedInputCommand(
                hardware_id,
                1,
                0,
                0,
                -20.0,
                100,
            )
        )

    assert result.status in (
        ManagedInputResultStatus.REJECTED,
        ManagedInputResultStatus.FAILED,
        ManagedInputResultStatus.CAPACITY_REJECTED,
    )
    assert result.inventory.active_capture_count == 0


def test_no_query_and_unknown_count_controls_remain_unknown(managed_table):
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "endpoint")
    blocker = FakeBlocker(OfflineMaintenanceBlockerSnapshot(None, None))
    registry = new_registry(blocker=blocker)
    assert registry.inventory_snapshot().active_capture_count is None
    seed_ready(registry, observations=(observation,))

    unknown = registry.add(
        AddManagedInputCommand("new", scan_id, observation.observation_id)
    )
    assert unknown.inventory.active_capture_count is None

    registry._process_state = ManagedInputProcessState.RESTART_REQUIRED
    blocker.calls.clear()
    early = registry.add(
        AddManagedInputCommand("new", scan_id, observation.observation_id)
    )
    assert early.inventory.active_capture_count is None
    assert blocker.calls == []


def test_add_normalizes_alias_persists_once_and_latches_restart(managed_table):
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "new-endpoint")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand(
                "  Cafe\u0301  ",
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    assert result.process_state is ManagedInputProcessState.RESTART_REQUIRED
    assert result.hardware_snapshot is not None
    assert result.hardware_snapshot.alias == "Café"
    assert result.hardware_snapshot.hardware_revision == 1
    assert result.hardware_snapshot.calibration_revision == 0
    row = ManagedAudioInputTable.get()
    assert row.alias == "Café"
    assert row.stable_backend_uid is None
    assert parse_calibration_json(
        row.calibration_json,
        expected_channels=2,
    ) == ((0, False, 0, None), (1, False, 0, None))


def test_add_duplicate_normalized_alias_rejects_before_transaction(managed_table):
    make_row("Café")
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "new-endpoint")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand(
                " Cafe\u0301 ",
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert transactions == []
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.ALIAS_ALREADY_EXISTS
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert ManagedAudioInputTable.select().count() == 1


def test_add_seventh_is_capacity_rejected_in_one_transaction(managed_table):
    for index in range(6):
        make_row(f"input-{index}")
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "new-endpoint")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand("seventh", scan_id, observation.observation_id)
        ),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.CAPACITY_REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.CAPACITY_REACHED
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert ManagedAudioInputTable.select().count() == 6


def test_add_rejects_foreign_stable_uid_in_one_transaction(managed_table):
    make_row("existing", uid="uid-1")
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(
        scan_id,
        "new-endpoint",
        uid="uid-1",
    )
    registry = new_registry()
    seed_ready(
        registry,
        observations=(observation,),
        capability=StableUidCapability.SUPPORTED,
    )

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand("new", scan_id, observation.observation_id)
        ),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND
    assert ManagedAudioInputTable.select().count() == 1


def test_add_allows_equal_null_uid_descriptors(managed_table):
    make_row("existing")
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "new-endpoint")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result = registry.add(
        AddManagedInputCommand("new", scan_id, observation.observation_id)
    )

    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    assert ManagedAudioInputTable.select().count() == 2


@pytest.mark.parametrize(
    ("kind", "input_channels"),
    [
        (
            ManagedInputCommandKind.ADD,
            MAX_CALIBRATABLE_INPUT_CHANNELS + 1,
        ),
        (ManagedInputCommandKind.ADD, 2**31 - 1),
        (
            ManagedInputCommandKind.SELECT_OR_REPLACE,
            MAX_CALIBRATABLE_INPUT_CHANNELS + 1,
        ),
        (ManagedInputCommandKind.SELECT_OR_REPLACE, 2**31 - 1),
    ],
)
def test_add_and_select_reject_uncalibratable_channel_count_before_transaction(
    managed_table,
    monkeypatch,
    kind,
    input_channels,
):
    row = (
        make_row("target")
        if kind is ManagedInputCommandKind.SELECT_OR_REPLACE
        else None
    )
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(
        scan_id,
        "wide-endpoint",
        endpoint_descriptor=descriptor(input_channels=input_channels),
    )
    registry = new_registry()
    seed_ready(registry, observations=(observation,))
    initial_serialization_calls = 0

    def forbidden_initial_serialization(channels):
        nonlocal initial_serialization_calls
        initial_serialization_calls += 1
        raise AssertionError("unrepresentable payload must be rejected first")

    monkeypatch.setattr(
        registry,
        "_initial_calibration_json",
        forbidden_initial_serialization,
    )
    if kind is ManagedInputCommandKind.ADD:
        action = lambda: registry.add(
            AddManagedInputCommand("wide", scan_id, observation.observation_id)
        )
    else:
        hardware_id = ManagedHardwareId(row.hardware_id)
        action = lambda: registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                1,
                scan_id,
                observation.observation_id,
            )
        )

    result, transactions = transaction_statements(managed_table, action)

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert result.inventory.active_capture_count == 0
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert initial_serialization_calls == 0
    assert transactions == []
    assert ManagedAudioInputTable.select().count() == (1 if row else 0)


@pytest.mark.parametrize(
    "kind",
    (
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
    ),
)
def test_add_and_select_accept_max_calibratable_channel_count(
    managed_table,
    kind,
):
    row = (
        make_row("target")
        if kind is ManagedInputCommandKind.SELECT_OR_REPLACE
        else None
    )
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(
        scan_id,
        "wide-endpoint",
        endpoint_descriptor=descriptor(
            input_channels=MAX_CALIBRATABLE_INPUT_CHANNELS
        ),
    )
    registry = new_registry()
    seed_ready(registry, observations=(observation,))
    if kind is ManagedInputCommandKind.ADD:
        result = registry.add(
            AddManagedInputCommand("wide", scan_id, observation.observation_id)
        )
    else:
        result = registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                ManagedHardwareId(row.hardware_id),
                1,
                scan_id,
                observation.observation_id,
            )
        )

    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    persisted = ManagedAudioInputTable.get_by_id(
        result.hardware_snapshot.hardware_id.value
    )
    assert (
        persisted.descriptor_input_channels
        == MAX_CALIBRATABLE_INPUT_CHANNELS
    )
    assert len(persisted.calibration_json.encode("utf-8")) <= 64 * 1024


def test_max_channel_row_supports_successive_full_precision_calibrations(
    managed_table,
):
    scan_id = BackendScanId("process-1", 1)
    wide_descriptor = descriptor(
        input_channels=MAX_CALIBRATABLE_INPUT_CHANNELS
    )
    observation = make_observation(
        scan_id,
        "wide-endpoint",
        endpoint_descriptor=wide_descriptor,
    )
    add_registry = new_registry()
    seed_ready(add_registry, observations=(observation,))
    added = add_registry.add(
        AddManagedInputCommand("wide", scan_id, observation.observation_id)
    )
    assert added.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED

    first_registry, first_resident, hardware_id, first_generation = (
        calibration_registry()
    )
    first_correction = -2.2250738585072014e-308
    first_resident.measurements.append(
        ResidentCalibrationMeasurement(
            first_generation,
            0,
            -first_correction,
            100,
        )
    )
    first = first_registry.calibrate(
        CalibrateManagedInputCommand(
            hardware_id,
            1,
            0,
            0,
            0.0,
            100,
        )
    )
    assert first.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED

    second_registry, second_resident, _, second_generation = (
        calibration_registry()
    )
    second_channel = MAX_CALIBRATABLE_INPUT_CHANNELS - 1
    second_correction = 199.12345678901235
    second_resident.measurements.append(
        ResidentCalibrationMeasurement(
            second_generation,
            second_channel,
            -second_correction,
            100,
        )
    )
    second = second_registry.calibrate(
        CalibrateManagedInputCommand(
            hardware_id,
            2,
            1,
            second_channel,
            0.0,
            100,
        )
    )

    assert second.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    persisted = ManagedAudioInputTable.get_by_id(hardware_id.value)
    channels = parse_calibration_json(
        persisted.calibration_json,
        expected_channels=MAX_CALIBRATABLE_INPUT_CHANNELS,
    )
    assert channels[0] == (0, True, 1, first_correction)
    assert channels[second_channel] == (
        second_channel,
        True,
        1,
        second_correction,
    )
    assert persisted.hardware_revision == 3
    assert persisted.calibration_revision == 2


@pytest.mark.parametrize(
    ("scan_sequence", "observation_id", "expected"),
    [
        (2, "new-endpoint", ManagedInputErrorCode.STALE_SCAN),
        (1, "missing", ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION),
    ],
)
def test_add_rejects_stale_scan_or_observation_without_transaction(
    managed_table,
    scan_sequence,
    observation_id,
    expected,
):
    current_scan_id = BackendScanId("process-1", 1)
    observation = make_observation(current_scan_id, "new-endpoint")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))
    command_scan_id = BackendScanId("process-1", scan_sequence)

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.add(
            AddManagedInputCommand("new", command_scan_id, observation_id)
        ),
    )

    assert transactions == []
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is expected
    assert ManagedAudioInputTable.select().count() == 0


def test_select_replaces_endpoint_in_one_transaction_and_clears_calibration(
    managed_table,
):
    row = make_row("target")
    row.hardware_revision = 4
    row.calibration_revision = 3
    row.calibration_json = serialize_calibration_json(
        ((0, True, 2, 1.25), (1, True, 1, -0.5)),
        expected_channels=2,
    )
    row.save()
    hardware_id = ManagedHardwareId(row.hardware_id)
    selected_descriptor = descriptor(
        device_name="Replacement",
        sample_rate_hz=44_100.0,
        input_channels=1,
    )
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(
        scan_id,
        "replacement",
        endpoint_descriptor=selected_descriptor,
    )
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                4,
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    updated = ManagedAudioInputTable.get_by_id(row.hardware_id)
    assert updated.hardware_revision == 5
    assert updated.calibration_revision == 0
    assert updated.descriptor_device_name == "Replacement"
    assert updated.descriptor_sample_rate_hz == 44_100.0
    assert updated.descriptor_input_channels == 1
    assert parse_calibration_json(
        updated.calibration_json,
        expected_channels=1,
    ) == ((0, False, 0, None),)


def test_select_rejects_stale_revision_without_transaction(managed_table):
    row = make_row("target")
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "replacement")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                2,
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert transactions == []
    assert result.errors[0].code is ManagedInputErrorCode.STALE_HARDWARE_REVISION
    assert ManagedAudioInputTable.get_by_id(row.hardware_id).hardware_revision == 1


def test_select_rejects_a_foreign_stable_uid_in_one_transaction(managed_table):
    target = make_row("target", uid="uid-target")
    make_row("owner", uid="uid-owned")
    hardware_id = ManagedHardwareId(target.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(
        scan_id,
        "owned-endpoint",
        uid="uid-owned",
    )
    registry = new_registry()
    seed_ready(
        registry,
        observations=(observation,),
        capability=StableUidCapability.SUPPORTED,
    )

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                1,
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND
    assert ManagedAudioInputTable.get_by_id(target.hardware_id).stable_backend_uid == (
        "uid-target"
    )


@pytest.mark.parametrize(
    ("starting_revision", "expected_status"),
    [
        (2**63 - 1, ManagedInputResultStatus.REJECTED),
        (2**63 - 2, ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED),
    ],
)
def test_select_hardware_revision_exhaustion_is_checked_before_transaction(
    managed_table,
    starting_revision,
    expected_status,
):
    row = make_row("target")
    row.hardware_revision = starting_revision
    row.save()
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "replacement")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.select_or_replace(
            SelectOrReplaceManagedInputCommand(
                hardware_id,
                starting_revision,
                scan_id,
                observation.observation_id,
            )
        ),
    )

    assert result.status is expected_status
    persisted = ManagedAudioInputTable.get_by_id(row.hardware_id)
    if starting_revision == 2**63 - 1:
        assert result.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
        assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
        assert transactions == []
        assert persisted.hardware_revision == starting_revision
    else:
        assert len(transactions) == 1
        assert persisted.hardware_revision == 2**63 - 1


def test_remove_rejects_configuration_reference_without_transaction(
    managed_table,
):
    row = make_row("target")
    hardware_id = ManagedHardwareId(row.hardware_id)
    references = FakeReferences()
    references.referenced.add(hardware_id)
    registry = new_registry(references=references)
    seed_ready(registry)

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.remove(RemoveManagedInputCommand(hardware_id, 1)),
    )

    assert transactions == []
    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.CONFIG_REFERENCE_EXISTS
    assert ManagedAudioInputTable.select().count() == 1


def test_remove_deletes_once_and_latches_restart(managed_table):
    kept = make_row("kept")
    removed = make_row("removed")
    hardware_id = ManagedHardwareId(removed.hardware_id)
    registry = new_registry()
    seed_ready(registry)

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.remove(RemoveManagedInputCommand(hardware_id, 1)),
    )

    assert len(transactions) == 1
    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    assert result.hardware_snapshot.hardware_id == hardware_id
    assert [row.hardware_id for row in result.inventory.rows] == [
        ManagedHardwareId(kept.hardware_id)
    ]
    assert ManagedAudioInputTable.select().count() == 1


@pytest.mark.parametrize(
    ("method_name", "command_factory"),
    [
        (
            "add",
            lambda hardware_id, scan_id, observation: AddManagedInputCommand(
                "new", scan_id, observation.observation_id
            ),
        ),
        (
            "select_or_replace",
            lambda hardware_id, scan_id, observation:
            SelectOrReplaceManagedInputCommand(
                hardware_id, 1, scan_id, observation.observation_id
            ),
        ),
        (
            "remove",
            lambda hardware_id, scan_id, observation: RemoveManagedInputCommand(
                hardware_id, 1
            ),
        ),
    ],
)
def test_database_failure_rolls_back_without_retry_or_restart(
    managed_table,
    monkeypatch,
    method_name,
    command_factory,
):
    row = make_row("target")
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    observation = make_observation(scan_id, "replacement")
    registry = new_registry()
    seed_ready(registry, observations=(observation,))
    calls = 0
    def failing_atomic(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("database detail must not escape")

    monkeypatch.setattr(managed_table, "atomic", failing_atomic)
    command = command_factory(hardware_id, scan_id, observation)

    result = getattr(registry, method_name)(command)

    assert calls == 1
    assert result.status is ManagedInputResultStatus.FAILED
    assert result.errors[0].code is ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED
    assert "database detail" not in result.errors[0].summary
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert ManagedAudioInputTable.select().count() == 1


def scan_error(code: ManagedInputErrorCode) -> BackendDiscoveryResult:
    return BackendDiscoveryResult(
        scan=None,
        error=ManagedInputError(
            code,
            "backend_scan",
            "The backend scan was invalid.",
        ),
    )


def test_clean_refresh_publishes_next_scan_without_restart_latch(managed_table):
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    refresh_id = BackendScanId("process-1", 2)
    refreshed = make_observation(refresh_id, "refreshed", device_index=3)
    discovery = FakeDiscovery()
    discovery.results.append(
        BackendDiscoveryResult(
            scan=make_scan(refresh_id, refreshed),
            error=None,
        )
    )
    blocker = FakeBlocker()
    registry = new_registry(discovery=discovery, blocker=blocker)
    seed_ready(registry, observations=(startup_observation,))

    def prove_lock_is_held():
        assert registry._lock.acquire(blocking=False) is False

    discovery.on_discover = prove_lock_is_held

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.SUCCEEDED
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert result.inventory.scan_id == refresh_id
    assert result.observations == (refreshed,)
    assert registry.endpoint_snapshot() == (refreshed,)
    assert discovery.calls == [refresh_id]
    assert blocker.calls == [None]
    assert registry._next_scan_sequence == 3


def test_refresh_remains_available_and_preserves_restart_required(
    managed_table,
):
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    refresh_id = BackendScanId("process-1", 2)
    refreshed = make_observation(refresh_id, "refreshed")
    discovery = FakeDiscovery()
    discovery.results.append(
        BackendDiscoveryResult(make_scan(refresh_id, refreshed), None)
    )
    registry = new_registry(discovery=discovery)
    seed_ready(registry, observations=(startup_observation,))
    registry._process_state = ManagedInputProcessState.RESTART_REQUIRED

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.SUCCEEDED
    assert result.process_state is ManagedInputProcessState.RESTART_REQUIRED
    assert result.inventory.restart_required is True
    assert result.inventory.scan_id == refresh_id


def test_refresh_never_changes_frozen_runtime_projection(managed_table):
    row = make_row("target")
    hardware_id = ManagedHardwareId(row.hardware_id)
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    endpoint = HardwareEndpointIdentity(
        hardware_id,
        startup_id,
        startup_observation.device_index,
        startup_observation.hostapi_index,
        startup_observation.stable_backend_uid,
        startup_observation.descriptor.device_name,
        startup_observation.descriptor.hostapi_name,
        startup_observation.descriptor.sample_rate_hz,
        startup_observation.descriptor.input_channels,
    )
    generation = ResidentStreamGenerationStamp(hardware_id, 1, 1)
    ready = ResidentReadyStream(endpoint, generation)
    refresh_id = BackendScanId("process-1", 2)
    discovery = FakeDiscovery()
    discovery.results.append(
        BackendDiscoveryResult(make_scan(refresh_id), None)
    )
    resident = FakeResident()
    registry = new_registry(discovery=discovery, resident=resident)
    seed_ready(
        registry,
        observations=(startup_observation,),
        ready_streams=(ready,),
    )
    before = registry.inventory_snapshot().rows

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.inventory.rows == before
    assert result.inventory.rows[0].availability is ManagedInputAvailability.AVAILABLE
    assert result.inventory.rows[0].endpoint is endpoint
    assert result.inventory.rows[0].resident_generation is generation
    assert resident.measure_calls == []


def test_refresh_requiring_reinitialize_rejects_without_query_or_latch(
    managed_table,
):
    discovery = FakeDiscovery()
    discovery.refresh_requires_reinitialize = True
    registry = new_registry(discovery=discovery)
    startup_id = BackendScanId("process-1", 1)
    seed_ready(
        registry,
        observations=(make_observation(startup_id, "startup"),),
    )

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.REFRESH_REQUIRES_RESTART
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert result.inventory.restart_required is False
    assert result.inventory.scan_id == startup_id
    assert discovery.calls == []


@pytest.mark.parametrize(
    "latched_code",
    (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ),
)
def test_reinitialize_requirement_cannot_replace_latched_scan_diagnostic(
    managed_table,
    latched_code,
):
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    discovery = FakeDiscovery()
    discovery.results.append(scan_error(latched_code))
    registry = new_registry(discovery=discovery)
    seed_ready(registry, observations=(startup_observation,))

    first = registry.refresh(RefreshManagedInputsCommand())
    preserved = first.inventory
    discovery.refresh_requires_reinitialize = True

    second = registry.refresh(RefreshManagedInputsCommand())

    assert second.status is ManagedInputResultStatus.REJECTED
    assert second.blocker is None
    assert second.errors[0].code is latched_code
    assert second.observations == ()
    assert second.inventory == preserved
    assert registry.endpoint_snapshot() == (startup_observation,)
    assert discovery.calls == [BackendScanId("process-1", 2)]


@pytest.mark.parametrize(
    "code",
    (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ),
)
def test_invalid_refresh_latches_error_and_preserves_valid_projection(
    managed_table,
    code,
):
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    discovery = FakeDiscovery()
    discovery.results.append(scan_error(code))
    registry = new_registry(discovery=discovery)
    seed_ready(registry, observations=(startup_observation,))
    before = registry.inventory_snapshot()

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is code
    assert result.observations == ()
    assert result.inventory.scan_id == before.scan_id
    assert result.inventory.rows == before.rows
    assert result.inventory.scan_error_code is code
    assert registry.endpoint_snapshot() == (startup_observation,)
    assert registry._next_scan_sequence == 2


def test_latched_refresh_may_diagnose_once_but_cannot_publish_or_clear(
    managed_table,
):
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    diagnostic_id = BackendScanId("process-1", 2)
    diagnostic_observation = make_observation(diagnostic_id, "diagnostic")
    discovery = FakeDiscovery()
    discovery.results.extend(
        [
            scan_error(ManagedInputErrorCode.INVARIANT_VIOLATION),
            BackendDiscoveryResult(
                make_scan(diagnostic_id, diagnostic_observation),
                None,
            ),
        ]
    )
    registry = new_registry(discovery=discovery)
    seed_ready(registry, observations=(startup_observation,))

    first = registry.refresh(RefreshManagedInputsCommand())
    second = registry.refresh(RefreshManagedInputsCommand())

    assert first.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert second.status is ManagedInputResultStatus.REJECTED
    assert second.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert second.observations == ()
    assert second.inventory.scan_id == startup_id
    assert registry.endpoint_snapshot() == (startup_observation,)
    assert discovery.calls == [diagnostic_id, diagnostic_id]
    assert registry._next_scan_sequence == 2


def test_refresh_rejects_changed_stable_uid_capability(managed_table):
    make_row("target")
    startup_id = BackendScanId("process-1", 1)
    startup_observation = make_observation(startup_id, "startup")
    refresh_id = BackendScanId("process-1", 2)
    refreshed = make_observation(refresh_id, "refreshed", uid="uid-1")
    discovery = FakeDiscovery()
    discovery.results.append(
        BackendDiscoveryResult(
            make_scan(
                refresh_id,
                refreshed,
                capability=StableUidCapability.SUPPORTED,
            ),
            None,
        )
    )
    registry = new_registry(discovery=discovery)
    seed_ready(registry, observations=(startup_observation,))

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    assert (
        result.inventory.scan_error_code
        is ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    )
    assert result.inventory.scan_id == startup_id
    assert registry.endpoint_snapshot() == (startup_observation,)


@pytest.mark.parametrize(
    ("capability", "uid_values", "expected_code"),
    [
        (
            StableUidCapability.SUPPORTED,
            ("uid-1", None),
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ),
        (
            StableUidCapability.SUPPORTED,
            ("uid-1", "uid-1"),
            ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ),
        (
            StableUidCapability.NONE,
            (None, None),
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
    ],
)
def test_refresh_rejects_mixed_duplicate_or_structurally_conflicting_scan(
    managed_table,
    capability,
    uid_values,
    expected_code,
):
    startup_id = BackendScanId("process-1", 1)
    startup = make_observation(startup_id, "startup")
    refresh_id = BackendScanId("process-1", 2)
    first = make_observation(
        refresh_id,
        "first",
        device_index=0,
        uid=uid_values[0],
    )
    second = make_observation(
        refresh_id,
        "second",
        device_index=(
            0
            if expected_code is ManagedInputErrorCode.INVARIANT_VIOLATION
            else 1
        ),
        uid=uid_values[1],
    )
    discovery = FakeDiscovery()
    discovery.results.append(
        BackendDiscoveryResult(
            make_scan(refresh_id, first, second, capability=capability),
            None,
        )
    )
    registry = new_registry(discovery=discovery)
    seed_ready(
        registry,
        observations=(startup,),
        capability=capability,
    )

    result = registry.refresh(RefreshManagedInputsCommand())

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is expected_code
    assert result.observations == ()
    assert result.inventory.scan_id == startup_id


def calibration_registry(
    *,
    resident: FakeResident | None = None,
    available: bool = True,
):
    row = ManagedAudioInputTable.get()
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("process-1", 1)
    endpoint_descriptor = descriptor(
        device_name=row.descriptor_device_name,
        hostapi_name=row.descriptor_hostapi_name,
        sample_rate_hz=row.descriptor_sample_rate_hz,
        input_channels=row.descriptor_input_channels,
    )
    observation = make_observation(
        scan_id,
        "calibration-endpoint",
        uid=row.stable_backend_uid,
        endpoint_descriptor=endpoint_descriptor,
    )
    endpoint = HardwareEndpointIdentity(
        hardware_id,
        scan_id,
        observation.device_index,
        observation.hostapi_index,
        observation.stable_backend_uid,
        endpoint_descriptor.device_name,
        endpoint_descriptor.hostapi_name,
        endpoint_descriptor.sample_rate_hz,
        endpoint_descriptor.input_channels,
    )
    generation = ResidentStreamGenerationStamp(hardware_id, 10, 20)
    ready = ResidentReadyStream(endpoint, generation)
    actual_resident = resident or FakeResident()
    registry = new_registry(resident=actual_resident)
    seed_ready(
        registry,
        observations=(observation,),
        capability=(
            StableUidCapability.SUPPORTED
            if row.stable_backend_uid is not None
            else StableUidCapability.NONE
        ),
        ready_streams=(ready,) if available else (),
    )
    return registry, actual_resident, hardware_id, generation


def test_calibration_rejects_unavailable_row_before_measurement(managed_table):
    row = make_row("target")
    registry, resident, hardware_id, generation = calibration_registry(
        available=False
    )

    result = registry.calibrate(
        CalibrateManagedInputCommand(hardware_id, 1, 0, 0, -20.0, 100)
    )

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.ENDPOINT_UNAVAILABLE
    assert resident.measure_calls == []
    assert ManagedAudioInputTable.get_by_id(row.hardware_id).hardware_revision == 1


@pytest.mark.parametrize(
    ("hardware_revision", "calibration_revision", "expected"),
    [
        (2, 0, ManagedInputErrorCode.STALE_HARDWARE_REVISION),
        (1, 1, ManagedInputErrorCode.STALE_CALIBRATION_REVISION),
    ],
)
def test_calibration_rejects_stale_revisions_before_measurement(
    managed_table,
    hardware_revision,
    calibration_revision,
    expected,
):
    make_row("target")
    registry, resident, hardware_id, generation = calibration_registry()

    result = registry.calibrate(
        CalibrateManagedInputCommand(
            hardware_id,
            hardware_revision,
            calibration_revision,
            0,
            -20.0,
            100,
        )
    )

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is expected
    assert resident.measure_calls == []


@pytest.mark.parametrize(
    ("channel_index", "duration_frames"),
    [
        (2, 100),
        (0, 48_000 * 60 + 1),
    ],
)
def test_calibration_rejects_channel_or_duration_bounds_before_measurement(
    managed_table,
    channel_index,
    duration_frames,
):
    make_row("target")
    registry, resident, hardware_id, generation = calibration_registry()

    result = registry.calibrate(
        CalibrateManagedInputCommand(
            hardware_id,
            1,
            0,
            channel_index,
            -20.0,
            duration_frames,
        )
    )

    assert result.status is ManagedInputResultStatus.REJECTED
    assert result.errors[0].code is ManagedInputErrorCode.CALIBRATION_INVALID
    assert resident.measure_calls == []


def test_calibration_measures_once_under_same_lock_and_updates_one_channel(
    managed_table,
):
    row = make_row("target")
    row.hardware_revision = 4
    row.calibration_revision = 3
    row.calibration_json = serialize_calibration_json(
        ((0, True, 2, 1.25), (1, False, 0, None)),
        expected_channels=2,
    )
    row.save()
    resident = FakeResident()
    registry, resident, hardware_id, generation = calibration_registry(
        resident=resident
    )
    resident.measurements.append(
        ResidentCalibrationMeasurement(generation, 1, -23.0, 48_000)
    )

    def prove_lock_is_held():
        assert registry._lock.acquire(blocking=False) is False

    resident.on_measure = prove_lock_is_held
    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.calibrate(
            CalibrateManagedInputCommand(
                hardware_id,
                4,
                3,
                1,
                -20.0,
                48_000,
            )
        ),
    )

    assert len(transactions) == 1
    assert resident.measure_calls == [(hardware_id, generation, 1, 48_000)]
    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    assert result.process_state is ManagedInputProcessState.RESTART_REQUIRED
    updated = ManagedAudioInputTable.get_by_id(row.hardware_id)
    assert updated.hardware_revision == 5
    assert updated.calibration_revision == 4
    assert parse_calibration_json(
        updated.calibration_json,
        expected_channels=2,
    ) == ((0, True, 2, 1.25), (1, True, 1, 3.0))


@pytest.mark.parametrize(
    ("measurement_factory", "expected_status", "expected_code"),
    [
        (
            lambda hardware_id, generation:
            ResidentCalibrationMeasurement(
                ResidentStreamGenerationStamp(hardware_id, 10, 21),
                0,
                -20.0,
                100,
            ),
            ManagedInputResultStatus.FAILED,
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
        ),
        (
            lambda hardware_id, generation:
            ResidentCalibrationMeasurement(generation, 1, -20.0, 100),
            ManagedInputResultStatus.FAILED,
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
        ),
        (
            lambda hardware_id, generation:
            ResidentCalibrationMeasurement(generation, 0, -20.0, 99),
            ManagedInputResultStatus.FAILED,
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
        ),
        (
            lambda hardware_id, generation:
            ResidentCalibrationMeasurement(generation, 0, 200.5, 100),
            ManagedInputResultStatus.REJECTED,
            ManagedInputErrorCode.CALIBRATION_INVALID,
        ),
    ],
)
def test_invalid_calibration_measurement_never_persists(
    managed_table,
    measurement_factory,
    expected_status,
    expected_code,
):
    row = make_row("target")
    registry, resident, hardware_id, generation = calibration_registry()
    resident.measurements.append(measurement_factory(hardware_id, generation))

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.calibrate(
            CalibrateManagedInputCommand(
                hardware_id,
                1,
                0,
                0,
                -200.0,
                100,
            )
        ),
    )

    assert transactions == []
    assert result.status is expected_status
    assert result.errors[0].code is expected_code
    updated = ManagedAudioInputTable.get_by_id(row.hardware_id)
    assert updated.hardware_revision == 1
    assert updated.calibration_revision == 0


def test_calibration_capture_failure_is_not_retried(managed_table):
    row = make_row("target")
    registry, resident, hardware_id, generation = calibration_registry()
    resident.measurements.append(RuntimeError("raw capture detail"))

    result = registry.calibrate(
        CalibrateManagedInputCommand(hardware_id, 1, 0, 0, -20.0, 100)
    )

    assert len(resident.measure_calls) == 1
    assert result.status is ManagedInputResultStatus.FAILED
    assert result.errors[0].code is ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED
    assert "raw capture detail" not in result.errors[0].summary
    assert ManagedAudioInputTable.get_by_id(row.hardware_id).hardware_revision == 1


@pytest.mark.parametrize(
    ("starting_channel_revision", "expected_status"),
    [
        (2**63 - 1, ManagedInputResultStatus.REJECTED),
        (2**63 - 2, ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED),
    ],
)
def test_calibration_channel_revision_exhaustion_precedes_measurement(
    managed_table,
    starting_channel_revision,
    expected_status,
):
    row = make_row("target")
    row.calibration_json = serialize_calibration_json(
        (
            (0, True, starting_channel_revision, 1.0),
            (1, False, 0, None),
        ),
        expected_channels=2,
    )
    row.save()
    registry, resident, hardware_id, generation = calibration_registry()
    resident.measurements.append(
        ResidentCalibrationMeasurement(generation, 0, -21.0, 100)
    )

    result, transactions = transaction_statements(
        managed_table,
        lambda: registry.calibrate(
            CalibrateManagedInputCommand(
                hardware_id,
                1,
                0,
                0,
                -20.0,
                100,
            )
        ),
    )

    assert result.status is expected_status
    persisted = ManagedAudioInputTable.get_by_id(row.hardware_id)
    channels = parse_calibration_json(
        persisted.calibration_json,
        expected_channels=2,
    )
    if starting_channel_revision == 2**63 - 1:
        assert result.errors[0].code is ManagedInputErrorCode.CALIBRATION_INVALID
        assert resident.measure_calls == []
        assert transactions == []
        assert channels[0][2] == starting_channel_revision
        assert persisted.hardware_revision == 1
    else:
        assert len(resident.measure_calls) == 1
        assert len(transactions) == 1
        assert channels[0][2] == 2**63 - 1
        assert persisted.hardware_revision == 2


def test_calibration_database_failure_occurs_after_one_measurement_without_retry(
    managed_table,
    monkeypatch,
):
    row = make_row("target")
    registry, resident, hardware_id, generation = calibration_registry()
    resident.measurements.append(
        ResidentCalibrationMeasurement(generation, 0, -21.0, 100)
    )
    atomic_calls = 0

    def failing_atomic(*args, **kwargs):
        nonlocal atomic_calls
        atomic_calls += 1
        raise RuntimeError("database detail")

    monkeypatch.setattr(managed_table, "atomic", failing_atomic)

    result = registry.calibrate(
        CalibrateManagedInputCommand(hardware_id, 1, 0, 0, -20.0, 100)
    )

    assert atomic_calls == 1
    assert len(resident.measure_calls) == 1
    assert result.status is ManagedInputResultStatus.FAILED
    assert result.errors[0].code is ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert ManagedAudioInputTable.get_by_id(row.hardware_id).hardware_revision == 1
