from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from app.service.managed_input_contracts import (
    BackendDiscoveryResult,
    BackendInputScan,
    BackendScanId,
    CalibrateManagedInputCommand,
    FrozenRunInputProjection,
    HardwareEndpointIdentity,
    InputEndpointDescriptor,
    InputEndpointObservation,
    ManagedHardwareId,
    ManagedInputAvailability,
    ManagedInputBlocker,
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
    ManagedInputError,
    ManagedInputErrorCode,
    ManagedInputProcessState,
    ManagedInputResultStatus,
    MapStartupEndpointCommand,
    OfflineMaintenanceBlockerSnapshot,
    RawBackendInputScan,
    RawHostApiIdentity,
    ResidentInputFailureNotice,
    ResidentCalibrationMeasurement,
    ResidentOpenFailure,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
    RemoveManagedInputCommand,
    SelectOrReplaceManagedInputCommand,
    StableUidCapability,
    StableUidContract,
    serialize_calibration_json,
)
from app.service.managed_input_registry import ManagedInputRegistry
from app.service.managed_input_runtime import ManagedInputDiscoveryAdapter
from model.managed_audio_input_table import ManagedAudioInputTable


SCAN_ID = BackendScanId("runtime-tests", 1)


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


def observation(
    observation_id: str = "endpoint-0",
    *,
    scan_id: BackendScanId = SCAN_ID,
    device_index: int = 0,
    hostapi_index: int = 0,
    uid: str | None = None,
    endpoint_descriptor: InputEndpointDescriptor | None = None,
) -> InputEndpointObservation:
    return InputEndpointObservation(
        scan_id,
        observation_id,
        device_index,
        hostapi_index,
        uid,
        endpoint_descriptor or descriptor(),
    )


def raw_scan(
    *observations: InputEndpointObservation,
    scan_id: BackendScanId = SCAN_ID,
    complete: bool = True,
    capability: StableUidCapability = StableUidCapability.NONE,
    contract: StableUidContract = StableUidContract.ABSENT,
    hostapis: tuple[RawHostApiIdentity, ...] = (
        RawHostApiIdentity(0, "Host API"),
    ),
) -> RawBackendInputScan:
    return RawBackendInputScan(
        scan_id,
        complete,
        capability,
        contract,
        hostapis,
        observations,
    )


class FakeRawBackend:
    def __init__(self, result: object) -> None:
        self.result = result
        self.query_calls: list[BackendScanId] = []
        self.requery_result: object = None
        self.requery_calls: list[tuple[BackendScanId, int]] = []
        self.requires_reinitialize: object = False

    @property
    def refresh_requires_reinitialize(self) -> bool:
        return self.requires_reinitialize  # type: ignore[return-value]

    def query_raw_input_endpoints(
        self,
        scan_id: BackendScanId,
    ) -> RawBackendInputScan:
        self.query_calls.append(scan_id)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]

    def requery_raw_input_endpoint(
        self,
        scan_id: BackendScanId,
        device_index: int,
    ) -> InputEndpointObservation | None:
        self.requery_calls.append((scan_id, device_index))
        if isinstance(self.requery_result, BaseException):
            raise self.requery_result
        return self.requery_result  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("capability", "contract", "uid"),
    [
        (StableUidCapability.NONE, StableUidContract.ABSENT, None),
        (
            StableUidCapability.SUPPORTED,
            StableUidContract.REVIEWED_OPAQUE_UID,
            "stable-uid",
        ),
    ],
)
def test_discovery_adapter_returns_one_validated_scan(
    capability,
    contract,
    uid,
):
    endpoint = observation(uid=uid)
    backend = FakeRawBackend(
        raw_scan(
            endpoint,
            capability=capability,
            contract=contract,
        )
    )

    result = ManagedInputDiscoveryAdapter(backend).discover(SCAN_ID)

    assert result.scan is not None
    assert result.scan.scan_id == SCAN_ID
    assert result.scan.stable_uid_capability is capability
    assert result.scan.observations == (endpoint,)
    assert result.error is None


@pytest.mark.parametrize(
    "raw_result",
    [
        object(),
        RuntimeError("backend detail"),
        raw_scan(observation(), complete=False),
        raw_scan(
            observation(),
            hostapis=(
                RawHostApiIdentity(0, "Host API"),
                RawHostApiIdentity(0, "Host API"),
            ),
        ),
        raw_scan(observation(hostapi_index=1)),
        raw_scan(
            observation(
                endpoint_descriptor=descriptor(hostapi_name="Other API")
            )
        ),
        raw_scan(
            observation(scan_id=BackendScanId("runtime-tests", 2)),
            scan_id=BackendScanId("runtime-tests", 2),
        ),
    ],
)
def test_discovery_adapter_maps_structural_failures_to_invariant_violation(
    raw_result,
):
    result = ManagedInputDiscoveryAdapter(
        FakeRawBackend(raw_result)
    ).discover(SCAN_ID)

    assert result.scan is None
    assert result.error is not None
    assert result.error.code is ManagedInputErrorCode.INVARIANT_VIOLATION


@pytest.mark.parametrize(
    "raw_result",
    [
        raw_scan(
            observation(),
            capability=StableUidCapability.SUPPORTED,
            contract=StableUidContract.ABSENT,
        ),
        raw_scan(
            observation(uid="uid"),
            capability=StableUidCapability.NONE,
            contract=StableUidContract.ABSENT,
        ),
        raw_scan(
            observation("a", device_index=0, uid="uid"),
            observation("b", device_index=1),
            capability=StableUidCapability.SUPPORTED,
            contract=StableUidContract.REVIEWED_OPAQUE_UID,
        ),
        raw_scan(
            observation("a", device_index=0, uid="uid"),
            observation("b", device_index=1, uid="uid"),
            capability=StableUidCapability.SUPPORTED,
            contract=StableUidContract.REVIEWED_OPAQUE_UID,
        ),
    ],
)
def test_discovery_adapter_maps_identity_failures_without_raw_publication(
    raw_result,
):
    result = ManagedInputDiscoveryAdapter(
        FakeRawBackend(raw_result)
    ).discover(SCAN_ID)

    assert result.scan is None
    assert result.error is not None
    assert (
        result.error.code
        is ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    )


@pytest.mark.parametrize(
    "raw_result",
    [
        raw_scan(
            observation("duplicate"),
            observation("duplicate", device_index=1),
        ),
        raw_scan(
            observation("first", device_index=3),
            observation("second", device_index=3),
        ),
    ],
)
def test_discovery_adapter_rejects_duplicate_observation_identity_or_coordinate(
    raw_result,
):
    result = ManagedInputDiscoveryAdapter(
        FakeRawBackend(raw_result)
    ).discover(SCAN_ID)

    assert result.scan is None
    assert result.error is not None
    assert result.error.code is ManagedInputErrorCode.INVARIANT_VIOLATION


def test_discovery_adapter_is_stateless_and_delegates_refresh_property():
    backend = FakeRawBackend(raw_scan())
    adapter = ManagedInputDiscoveryAdapter(backend)
    backend.requires_reinitialize = True

    assert adapter.refresh_requires_reinitialize is True
    assert vars(adapter) == {"_backend": backend}


@pytest.mark.parametrize(
    ("raw_requery", "expected"),
    [
        (observation(device_index=4), observation(device_index=4)),
        (observation(scan_id=BackendScanId("runtime-tests", 2)), None),
        (observation(device_index=5), None),
        (object(), None),
        (RuntimeError("backend detail"), None),
        (None, None),
    ],
)
def test_discovery_adapter_requery_accepts_only_requested_scan_and_index(
    raw_requery,
    expected,
):
    backend = FakeRawBackend(raw_scan())
    backend.requery_result = raw_requery

    actual = ManagedInputDiscoveryAdapter(
        backend
    ).requery_validated_input_endpoint(SCAN_ID, 4)

    assert actual == expected
    assert backend.requery_calls == [(SCAN_ID, 4)]


class FakeDiscovery:
    def __init__(self) -> None:
        self.results: list[BackendDiscoveryResult | BaseException | object] = []
        self.calls: list[BackendScanId] = []
        self.requeries: dict[
            tuple[BackendScanId, int],
            InputEndpointObservation | None | BaseException | object,
        ] = {}
        self.requery_calls: list[tuple[BackendScanId, int]] = []
        self.on_requery = None
        self.refresh_requires_reinitialize = False

    def discover(self, scan_id: BackendScanId) -> BackendDiscoveryResult:
        self.calls.append(scan_id)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def requery_validated_input_endpoint(
        self,
        scan_id: BackendScanId,
        device_index: int,
    ) -> InputEndpointObservation | None:
        self.requery_calls.append((scan_id, device_index))
        if self.on_requery is not None:
            self.on_requery()
        result = self.requeries.get((scan_id, device_index))
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


class FakeBlocker:
    def __init__(self) -> None:
        self.current: object = OfflineMaintenanceBlockerSnapshot(0, False)
        self.calls: list[ManagedHardwareId | None] = []
        self.on_snapshot = None

    def snapshot(self, hardware_id):
        self.calls.append(hardware_id)
        if self.on_snapshot is not None:
            self.on_snapshot()
        if isinstance(self.current, BaseException):
            raise self.current
        return self.current


class FakeReferences:
    def __init__(self) -> None:
        self.referenced: set[ManagedHardwareId] = set()
        self.calls: list[ManagedHardwareId] = []

    def configuration_references(self, hardware_id):
        self.calls.append(hardware_id)
        return hardware_id in self.referenced


class FakeResident:
    def __init__(self) -> None:
        self.open_results: list[ResidentStartupOpenResult | BaseException | object] = []
        self.open_calls: list[tuple[HardwareEndpointIdentity, ...]] = []
        self.on_open = None
        self.close_results: list[
            tuple[ManagedInputCloseResult, ...] | BaseException | object
        ] = []
        self.close_calls: list[float] = []
        self.on_close = None
        self.measurements: list[object] = []
        self.measure_calls: list[tuple[object, ...]] = []

    def open_startup_streams(self, endpoints):
        self.open_calls.append(endpoints)
        if self.on_open is not None:
            self.on_open()
        result = self.open_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def measure_calibration(self, *args):
        self.measure_calls.append(args)
        result = self.measurements.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close_all(self, timeout_seconds):
        self.close_calls.append(timeout_seconds)
        if self.on_close is not None:
            self.on_close()
        result = self.close_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def managed_table(setup_test_db):
    setup_test_db.bind([ManagedAudioInputTable])
    setup_test_db.create_tables([ManagedAudioInputTable])
    try:
        yield setup_test_db
    finally:
        setup_test_db.drop_tables([ManagedAudioInputTable])


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


def valid_scan(
    scan_id: BackendScanId,
    *items: InputEndpointObservation,
    capability: StableUidCapability = StableUidCapability.NONE,
) -> BackendDiscoveryResult:
    return BackendDiscoveryResult(
        BackendInputScan(scan_id, capability, items),
        None,
    )


def new_registry(
    *,
    discovery: FakeDiscovery | None = None,
    blocker: FakeBlocker | None = None,
    references: FakeReferences | None = None,
    resident: FakeResident | None = None,
    monotonic=lambda: 1.0,
    process_factory=lambda: "runtime-process",
) -> ManagedInputRegistry:
    return ManagedInputRegistry(
        table=ManagedAudioInputTable,
        discovery=discovery or FakeDiscovery(),
        blocker=blocker or FakeBlocker(),
        references=references or FakeReferences(),
        resident=resident or FakeResident(),
        monotonic=monotonic,
        process_instance_id_factory=process_factory,
    )


def ready_for(endpoint: HardwareEndpointIdentity) -> ResidentReadyStream:
    return ResidentReadyStream(
        endpoint,
        ResidentStreamGenerationStamp(endpoint.hardware_id, 1, 1),
    )


@pytest.mark.parametrize("row_count", range(7))
def test_bootstrap_loads_zero_through_six_rows_and_publishes_sequence_one(
    managed_table,
    row_count,
):
    for index in range(row_count):
        make_row(f"row-{index}")
    discovery = FakeDiscovery()
    scan_id = BackendScanId("runtime-process", 1)
    items = tuple(
        observation(
            f"endpoint-{index}",
            scan_id=scan_id,
            device_index=index,
        )
        for index in range(row_count)
    )
    discovery.results.append(valid_scan(scan_id, *items))
    registry = new_registry(discovery=discovery)

    result = registry.bootstrap()

    assert discovery.calls == [scan_id]
    assert result.inventory.scan_id == scan_id
    assert registry.endpoint_snapshot() == items
    assert registry._next_scan_sequence == 2
    assert len(result.inventory.rows) == row_count
    assert all(
        row.availability is ManagedInputAvailability.NEEDS_USER_SELECTION
        for row in result.inventory.rows
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_bootstrap_row_fail_close_precedes_discovery(
    managed_table,
    malformed,
):
    count = 1 if malformed else 7
    for index in range(count):
        row = make_row(f"row-{index}")
    if malformed:
        managed_table.execute_sql("PRAGMA ignore_check_constraints = ON")
        ManagedAudioInputTable.update(alias=" not-canonical ").where(
            ManagedAudioInputTable.hardware_id == row.hardware_id
        ).execute()
        managed_table.execute_sql("PRAGMA ignore_check_constraints = OFF")
    discovery = FakeDiscovery()
    registry = new_registry(discovery=discovery)

    first = registry.bootstrap()
    second = registry.bootstrap()

    assert first == second
    assert discovery.calls == []
    assert first.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert first.inventory.scan_id is None
    assert first.ready_streams == ()
    assert first.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION


def test_bootstrap_caches_invalid_discovery_and_never_advances_sequence(
    managed_table,
):
    discovery = FakeDiscovery()
    discovery.results.extend(
        [
            BackendDiscoveryResult(
                None,
                ManagedInputError(
                    ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                    "backend_scan",
                    "The backend identity is invalid.",
                ),
            ),
            valid_scan(BackendScanId("runtime-process", 1)),
        ]
    )
    registry = new_registry(discovery=discovery)

    first = registry.bootstrap()
    second = registry.finalize_bootstrap()
    third = registry.bootstrap()

    assert first == second == third
    assert discovery.calls == [BackendScanId("runtime-process", 1)]
    assert registry._next_scan_sequence == 1
    assert first.inventory.scan_error_code is (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    )


def test_bootstrap_exact_stable_uid_resolution_and_descriptor_mismatch(
    managed_table,
):
    first = make_row("available", uid="uid-1")
    second = make_row("mismatch", uid="uid-2")
    scan_id = BackendScanId("runtime-process", 1)
    exact = observation(
        "exact",
        scan_id=scan_id,
        device_index=1,
        uid="uid-1",
    )
    mismatch = observation(
        "mismatch",
        scan_id=scan_id,
        device_index=2,
        uid="uid-2",
        endpoint_descriptor=descriptor(device_name="Changed"),
    )
    discovery = FakeDiscovery()
    discovery.results.append(
        valid_scan(
            scan_id,
            exact,
            mismatch,
            capability=StableUidCapability.SUPPORTED,
        )
    )

    result = new_registry(discovery=discovery).bootstrap()
    rows = {row.hardware_id.value: row for row in result.inventory.rows}

    assert rows[first.hardware_id].availability is (
        ManagedInputAvailability.MAPPED_PENDING_OPEN
    )
    assert rows[first.hardware_id].error_code is None
    assert rows[second.hardware_id].availability is (
        ManagedInputAvailability.UNAVAILABLE
    )
    assert rows[second.hardware_id].error_code is (
        ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED
    )


def test_bootstrap_row_capability_conflict_invalidates_whole_scan(
    managed_table,
):
    make_row("no uid")
    scan_id = BackendScanId("runtime-process", 1)
    endpoint = observation(
        scan_id=scan_id,
        uid="uid",
    )
    discovery = FakeDiscovery()
    discovery.results.append(
        valid_scan(
            scan_id,
            endpoint,
            capability=StableUidCapability.SUPPORTED,
        )
    )

    result = new_registry(discovery=discovery).bootstrap()

    assert result.inventory.scan_id is None
    assert result.inventory.scan_error_code is (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    )
    assert result.errors[0].code is (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    )


def test_null_uid_mapping_is_explicit_process_local_and_one_to_one(
    managed_table,
):
    first = make_row("first")
    second = make_row("second")
    scan_id = BackendScanId("runtime-process", 1)
    candidate = observation("candidate", scan_id=scan_id)
    other = observation("other", scan_id=scan_id, device_index=1)
    discovery = FakeDiscovery()
    discovery.results.append(valid_scan(scan_id, candidate, other))
    resident = FakeResident()
    registry = new_registry(discovery=discovery, resident=resident)
    initial = registry.bootstrap()

    mapped = registry.map_startup_endpoint(
        MapStartupEndpointCommand(
            ManagedHardwareId(first.hardware_id),
            1,
            scan_id,
            "candidate",
        )
    )
    collision = registry.map_startup_endpoint(
        MapStartupEndpointCommand(
            ManagedHardwareId(second.hardware_id),
            1,
            scan_id,
            "candidate",
        )
    )

    assert initial.inventory.rows[0].availability is (
        ManagedInputAvailability.NEEDS_USER_SELECTION
    )
    assert mapped.inventory.rows[0].availability is (
        ManagedInputAvailability.MAPPED_PENDING_OPEN
    )
    assert collision.errors[0].code is (
        ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND
    )
    assert ManagedAudioInputTable.get_by_id(first.hardware_id).hardware_revision == 1
    assert resident.open_calls == []


@pytest.mark.parametrize(
    ("command_factory", "expected"),
    [
        (
            lambda row, scan: MapStartupEndpointCommand(
                ManagedHardwareId(999), 1, scan, "candidate"
            ),
            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
        ),
        (
            lambda row, scan: MapStartupEndpointCommand(
                ManagedHardwareId(row.hardware_id), 2, scan, "candidate"
            ),
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
        ),
        (
            lambda row, scan: MapStartupEndpointCommand(
                ManagedHardwareId(row.hardware_id),
                1,
                BackendScanId("runtime-process", 2),
                "candidate",
            ),
            ManagedInputErrorCode.STALE_SCAN,
        ),
        (
            lambda row, scan: MapStartupEndpointCommand(
                ManagedHardwareId(row.hardware_id), 1, scan, "missing"
            ),
            ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION,
        ),
    ],
)
def test_startup_mapping_exact_rejections(
    managed_table,
    command_factory,
    expected,
):
    row = make_row("row")
    scan_id = BackendScanId("runtime-process", 1)
    candidate = observation("candidate", scan_id=scan_id)
    discovery = FakeDiscovery()
    discovery.results.append(valid_scan(scan_id, candidate))
    registry = new_registry(discovery=discovery)
    registry.bootstrap()

    result = registry.map_startup_endpoint(command_factory(row, scan_id))

    assert result.errors[0].code is expected
    assert result.inventory.rows[0].availability is (
        ManagedInputAvailability.NEEDS_USER_SELECTION
    )


def test_startup_mapping_mutex_contention_has_no_side_effect(managed_table):
    row = make_row("row")
    scan_id = BackendScanId("runtime-process", 1)
    candidate = observation("candidate", scan_id=scan_id)
    discovery = FakeDiscovery()
    discovery.results.append(valid_scan(scan_id, candidate))
    registry = new_registry(discovery=discovery)
    before = registry.bootstrap()
    assert registry._lock.acquire(blocking=False)
    try:
        result = registry.map_startup_endpoint(
            MapStartupEndpointCommand(
                ManagedHardwareId(row.hardware_id),
                1,
                scan_id,
                "candidate",
            )
        )
    finally:
        registry._lock.release()

    assert result.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert result.inventory == before.inventory
    assert result.errors == ()


def test_finalize_requeries_without_lock_opens_once_and_isolates_failure(
    managed_table,
):
    stable_row = make_row("stable", uid="uid")
    null_row = make_row("unmapped", uid="missing")
    scan_id = BackendScanId("runtime-process", 1)
    stable = observation(
        "stable",
        scan_id=scan_id,
        device_index=1,
        uid="uid",
    )
    discovery = FakeDiscovery()
    discovery.results.append(
        valid_scan(
            scan_id,
            stable,
            capability=StableUidCapability.SUPPORTED,
        )
    )
    resident = FakeResident()
    registry = new_registry(discovery=discovery, resident=resident)
    registry.bootstrap()
    discovery.requeries[(scan_id, 1)] = stable

    def prove_requery_lock_released():
        assert registry._lock.acquire(blocking=False)
        registry._lock.release()

    discovery.on_requery = prove_requery_lock_released
    endpoint = HardwareEndpointIdentity(
        ManagedHardwareId(stable_row.hardware_id),
        scan_id,
        1,
        0,
        "uid",
        "Input",
        "Host API",
        48_000.0,
        2,
    )
    ready = ready_for(endpoint)
    resident.open_results.append(
        ResidentStartupOpenResult((ready,), ())
    )

    def prove_open_lock_released():
        assert registry._lock.acquire(blocking=False)
        registry._lock.release()

    resident.on_open = prove_open_lock_released
    result = registry.finalize_bootstrap()
    repeated = registry.finalize_bootstrap()

    assert result == repeated
    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert result.ready_streams == (ready,)
    assert result.unresolved_hardware_ids == (
        ManagedHardwareId(null_row.hardware_id),
    )
    assert discovery.requery_calls == [(scan_id, 1)]
    assert resident.open_calls == [(endpoint,)]


def test_finalize_preflight_mismatch_isolated_and_empty_open_still_occurs(
    managed_table,
):
    row = make_row("stable", uid="uid")
    scan_id = BackendScanId("runtime-process", 1)
    selected = observation(scan_id=scan_id, uid="uid")
    discovery = FakeDiscovery()
    discovery.results.append(
        valid_scan(
            scan_id,
            selected,
            capability=StableUidCapability.SUPPORTED,
        )
    )
    discovery.requeries[(scan_id, 0)] = replace(
        selected,
        descriptor=descriptor(device_name="Changed"),
    )
    resident = FakeResident()
    resident.open_results.append(ResidentStartupOpenResult((), ()))
    registry = new_registry(discovery=discovery, resident=resident)
    registry.bootstrap()

    result = registry.finalize_bootstrap()

    assert resident.open_calls == [()]
    assert result.inventory.rows[0].hardware_id == ManagedHardwareId(row.hardware_id)
    assert result.inventory.rows[0].availability is (
        ManagedInputAvailability.UNAVAILABLE
    )
    assert result.inventory.rows[0].error_code is (
        ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED
    )


def test_concurrent_map_and_finalize_are_fenced_during_external_open(
    managed_table,
):
    row = make_row("row")
    scan_id = BackendScanId("runtime-process", 1)
    selected = observation(scan_id=scan_id)
    discovery = FakeDiscovery()
    discovery.results.append(valid_scan(scan_id, selected))
    discovery.requeries[(scan_id, 0)] = selected
    resident = FakeResident()
    registry = new_registry(discovery=discovery, resident=resident)
    registry.bootstrap()
    registry.map_startup_endpoint(
        MapStartupEndpointCommand(
            ManagedHardwareId(row.hardware_id), 1, scan_id, "endpoint-0"
        )
    )
    entered = threading.Event()
    release = threading.Event()

    def pause_open():
        entered.set()
        assert release.wait(5)

    resident.on_open = pause_open
    endpoint = HardwareEndpointIdentity(
        ManagedHardwareId(row.hardware_id),
        scan_id,
        0,
        0,
        None,
        "Input",
        "Host API",
        48_000.0,
        2,
    )
    resident.open_results.append(
        ResidentStartupOpenResult((ready_for(endpoint),), ())
    )
    thread = threading.Thread(target=registry.finalize_bootstrap)
    thread.start()
    assert entered.wait(5)

    mapped = registry.map_startup_endpoint(
        MapStartupEndpointCommand(
            ManagedHardwareId(row.hardware_id), 1, scan_id, "endpoint-0"
        )
    )
    finalized = registry.finalize_bootstrap()
    release.set()
    thread.join(5)

    assert mapped.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert finalized.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert len(resident.open_calls) == 1


def finalized_registry(
    *,
    discovery: FakeDiscovery | None = None,
    blocker: FakeBlocker | None = None,
    resident: FakeResident | None = None,
    monotonic=lambda: 1.0,
):
    row = ManagedAudioInputTable.get()
    hardware_id = ManagedHardwareId(row.hardware_id)
    scan_id = BackendScanId("runtime-process", 1)
    selected = observation(
        scan_id=scan_id,
        uid=row.stable_backend_uid,
        endpoint_descriptor=descriptor(
            device_name=row.descriptor_device_name,
            hostapi_name=row.descriptor_hostapi_name,
            sample_rate_hz=row.descriptor_sample_rate_hz,
            input_channels=row.descriptor_input_channels,
        ),
    )
    actual_discovery = discovery or FakeDiscovery()
    actual_discovery.results.append(
        valid_scan(
            scan_id,
            selected,
            capability=(
                StableUidCapability.SUPPORTED
                if row.stable_backend_uid is not None
                else StableUidCapability.NONE
            ),
        )
    )
    actual_discovery.requeries[(scan_id, 0)] = selected
    actual_resident = resident or FakeResident()
    endpoint = HardwareEndpointIdentity(
        hardware_id,
        scan_id,
        0,
        0,
        row.stable_backend_uid,
        row.descriptor_device_name,
        row.descriptor_hostapi_name,
        row.descriptor_sample_rate_hz,
        row.descriptor_input_channels,
    )
    ready = ready_for(endpoint)
    actual_resident.open_results.append(
        ResidentStartupOpenResult((ready,), ())
    )
    registry = new_registry(
        discovery=actual_discovery,
        blocker=blocker,
        resident=actual_resident,
        monotonic=monotonic,
    )
    registry.bootstrap()
    if row.stable_backend_uid is None:
        registry.map_startup_endpoint(
            MapStartupEndpointCommand(
                hardware_id,
                row.hardware_revision,
                scan_id,
                selected.observation_id,
            )
        )
    registry.finalize_bootstrap()
    return (
        registry,
        actual_discovery,
        actual_resident,
        hardware_id,
        endpoint,
        ready.generation,
    )


def calibration_command(
    hardware_id: ManagedHardwareId,
    *,
    hardware_revision: int = 1,
    calibration_revision: int = 0,
    channel_index: int = 0,
) -> CalibrateManagedInputCommand:
    return CalibrateManagedInputCommand(
        hardware_id,
        hardware_revision,
        calibration_revision,
        channel_index,
        -20.0,
        100,
    )


def test_calibrate_blocker_uses_global_readiness_and_idle_target_does_not_block(
    managed_table,
) -> None:
    row = make_row("ready", uid="uid")
    blocker = FakeBlocker()
    blocker.current = OfflineMaintenanceBlockerSnapshot(0, True)
    blocker.on_snapshot = lambda: setattr(
        blocker,
        "current",
        OfflineMaintenanceBlockerSnapshot(
            0,
            blocker.calls[-1] is not None,
        ),
    )
    registry, _, resident, hardware_id, _, generation = finalized_registry(
        blocker=blocker
    )
    resident.measurements.append(
        ResidentCalibrationMeasurement(generation, 0, -21.0, 100)
    )

    result = registry.calibrate(calibration_command(hardware_id))

    assert blocker.calls == [None]
    assert resident.measure_calls == [(hardware_id, generation, 0, 100)]
    assert result.status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
    persisted = ManagedAudioInputTable.get_by_id(row.hardware_id)
    assert persisted.hardware_revision == 2
    assert persisted.calibration_revision == 1


@pytest.mark.parametrize(
    ("snapshot", "expected_blocker"),
    [
        (
            OfflineMaintenanceBlockerSnapshot(2, False),
            ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS,
        ),
        (
            OfflineMaintenanceBlockerSnapshot(None, None),
            ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN,
        ),
    ],
)
def test_calibrate_blocker_remains_fail_closed_for_global_capture_facts(
    managed_table,
    snapshot: OfflineMaintenanceBlockerSnapshot,
    expected_blocker: ManagedInputBlocker,
) -> None:
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    blocker.current = snapshot
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        blocker=blocker
    )

    result = registry.calibrate(calibration_command(hardware_id))

    assert blocker.calls == [None]
    assert result.blocker is expected_blocker
    assert resident.measure_calls == []


@pytest.mark.parametrize(
    ("command_overrides", "expected_error"),
    [
        (
            {"hardware_revision": 2},
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
        ),
        (
            {"calibration_revision": 1},
            ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
        ),
        (
            {"channel_index": 2},
            ManagedInputErrorCode.CALIBRATION_INVALID,
        ),
    ],
)
def test_calibrate_blocker_preserves_revision_and_channel_validation(
    managed_table,
    command_overrides: dict[str, int],
    expected_error: ManagedInputErrorCode,
) -> None:
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        blocker=blocker
    )

    result = registry.calibrate(
        calibration_command(hardware_id, **command_overrides)
    )

    assert blocker.calls == [None]
    assert result.errors[0].code is expected_error
    assert resident.measure_calls == []


def test_calibrate_blocker_preserves_availability_validation(
    managed_table,
) -> None:
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, _, resident, hardware_id, _, generation = finalized_registry(
        blocker=blocker
    )
    registry.report_stream_failure(
        ResidentInputFailureNotice(
            generation,
            "resident_stream_failed",
        )
    )
    blocker.calls.clear()

    result = registry.calibrate(calibration_command(hardware_id))

    assert blocker.calls == [None]
    assert result.errors[0].code is ManagedInputErrorCode.ENDPOINT_UNAVAILABLE
    assert resident.measure_calls == []


def test_calibrate_blocker_preserves_measurement_validation_without_db_write(
    managed_table,
) -> None:
    row = make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        blocker=blocker
    )
    resident.measurements.append(object())

    result = registry.calibrate(calibration_command(hardware_id))

    assert blocker.calls == [None]
    assert result.errors[0].code is (
        ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED
    )
    persisted = ManagedAudioInputTable.get_by_id(row.hardware_id)
    assert persisted.hardware_revision == 1
    assert persisted.calibration_revision == 0


@pytest.mark.parametrize("command_kind", ["select", "remove"])
def test_calibrate_blocker_keeps_target_guard_for_select_and_remove(
    managed_table,
    command_kind: str,
) -> None:
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    blocker.current = OfflineMaintenanceBlockerSnapshot(0, True)
    registry, discovery, _, hardware_id, _, _ = finalized_registry(
        blocker=blocker
    )
    current_scan = discovery.calls[0]
    if command_kind == "select":
        command = SelectOrReplaceManagedInputCommand(
            hardware_id,
            1,
            current_scan,
            "endpoint-0",
        )
        result = registry.select_or_replace(command)
    else:
        result = registry.remove(RemoveManagedInputCommand(hardware_id, 1))

    assert blocker.calls == [hardware_id]
    assert result.blocker is ManagedInputBlocker.TARGET_RESOURCE_BUSY


def test_frozen_run_projection_returns_only_exact_trusted_referenced_channels(
    managed_table,
):
    row = make_row("ready", uid="uid")
    row.hardware_revision = 4
    row.calibration_revision = 3
    row.calibration_json = serialize_calibration_json(
        (
            (0, True, 2, 1.25),
            (1, False, 0, None),
        ),
        expected_channels=2,
    )
    row.save()
    registry, _, _, hardware_id, endpoint, _ = finalized_registry()

    result = registry.frozen_run_projection(hardware_id, (0,))

    assert type(result.projection) is FrozenRunInputProjection
    assert result.error is None
    assert result.projection.hardware.hardware_id == hardware_id
    assert result.projection.hardware.hardware_revision == 4
    assert result.projection.hardware.alias_at_trigger == "ready"
    assert result.projection.hardware.endpoint == endpoint
    assert result.projection.hardware.calibration_revision == 3
    assert result.projection.calibration.channel_values == ((0, 2, 1.25),)


@pytest.mark.parametrize(
    "channels",
    [
        (),
        [0],
        (True,),
        (-1,),
        (2,),
        (0, 0),
        (1, 0),
    ],
)
def test_frozen_run_projection_rejects_malformed_channel_tuple(
    managed_table,
    channels,
):
    make_row("ready", uid="uid")
    registry, _, _, hardware_id, _, _ = finalized_registry()

    result = registry.frozen_run_projection(hardware_id, channels)

    assert result.projection is None
    assert result.error.code is ManagedInputErrorCode.CALIBRATION_INVALID


def test_frozen_run_projection_exact_state_and_trust_errors(managed_table):
    row = make_row("ready", uid="uid")
    registry, _, _, hardware_id, _, _ = finalized_registry()

    untrusted = registry.frozen_run_projection(hardware_id, (0,))
    missing = registry.frozen_run_projection(ManagedHardwareId(999), (0,))
    registry._process_state = ManagedInputProcessState.RESTART_REQUIRED
    restart = registry.frozen_run_projection(hardware_id, (0,))
    registry._process_state = ManagedInputProcessState.SHUTTING_DOWN
    shutting = registry.frozen_run_projection(hardware_id, (0,))

    assert untrusted.error.code is ManagedInputErrorCode.CALIBRATION_REQUIRED
    assert missing.error.code is ManagedInputErrorCode.HARDWARE_NOT_FOUND
    assert restart.error.code is ManagedInputErrorCode.RESTART_REQUIRED
    assert shutting.error.code is ManagedInputErrorCode.SHUTTING_DOWN


def test_current_generation_failure_fences_only_exact_slot_without_external_work(
    managed_table,
):
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, discovery, resident, hardware_id, _, generation = (
        finalized_registry(blocker=blocker)
    )
    discovery_calls = list(discovery.calls)
    open_calls = list(resident.open_calls)

    stale = registry.report_stream_failure(
        ResidentInputFailureNotice(
            ResidentStreamGenerationStamp(hardware_id, 1, 2),
            "callback_failed",
        )
    )
    current = registry.report_stream_failure(
        ResidentInputFailureNotice(generation, "callback_failed")
    )

    assert stale.availability is ManagedInputAvailability.AVAILABLE
    assert current.availability is ManagedInputAvailability.UNAVAILABLE
    assert current.error_code is ManagedInputErrorCode.RESIDENT_STREAM_FAILED
    assert registry.frozen_run_projection(hardware_id, (0,)).error.code is (
        ManagedInputErrorCode.ENDPOINT_UNAVAILABLE
    )
    assert discovery.calls == discovery_calls
    assert resident.open_calls == open_calls
    assert blocker.calls == []


@pytest.mark.parametrize(
    "timeout",
    [0.0, -0.1, 60.0001, float("nan"), float("inf"), 1, True],
)
def test_shutdown_validates_exact_timeout_before_work(
    managed_table,
    timeout,
):
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, _, resident, _, _, _ = finalized_registry(blocker=blocker)

    with pytest.raises(ValueError):
        registry.shutdown(timeout)

    assert blocker.calls == []
    assert resident.close_calls == []


def test_shutdown_mutex_contention_returns_uncached_incomplete(managed_table):
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    registry, _, resident, _, _, _ = finalized_registry(blocker=blocker)
    assert registry._lock.acquire(blocking=False)
    try:
        report = registry.shutdown(1.0)
    finally:
        registry._lock.release()

    assert report.clean is False
    assert report.process_state is ManagedInputProcessState.SHUTDOWN_INCOMPLETE
    assert report.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert blocker.calls == []
    assert resident.close_calls == []
    assert registry._cached_shutdown_report is None


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            OfflineMaintenanceBlockerSnapshot(None, None),
            ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN,
        ),
        (
            OfflineMaintenanceBlockerSnapshot(1, False),
            ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS,
        ),
    ],
)
def test_shutdown_blocker_precondition_checked_once_without_close(
    managed_table,
    snapshot,
    expected,
):
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    blocker.current = snapshot
    registry, _, resident, _, _, _ = finalized_registry(blocker=blocker)

    report = registry.shutdown(1.0)

    assert report.blocker is expected
    assert blocker.calls == [None]
    assert resident.close_calls == []
    assert registry._cached_shutdown_report is None


def test_shutdown_zero_captures_ignores_target_ownership_and_closes(
    managed_table,
):
    make_row("ready", uid="uid")
    blocker = FakeBlocker()
    blocker.current = OfflineMaintenanceBlockerSnapshot(0, True)
    times = iter((10.0, 10.25))
    resident = FakeResident()
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        blocker=blocker,
        resident=resident,
        monotonic=lambda: next(times),
    )
    states_at_close = []
    resident.on_close = lambda: states_at_close.append(registry._process_state)
    resident.close_results.append(
        (ManagedInputCloseResult(hardware_id, ManagedInputCloseStatus.CLOSED, None),)
    )

    report = registry.shutdown(1.0)

    assert blocker.calls == [None]
    assert states_at_close == [ManagedInputProcessState.SHUTTING_DOWN]
    assert resident.close_calls == [0.75]
    assert report.clean is True
    assert report.process_state is ManagedInputProcessState.CLOSED


def test_shutdown_sets_shutting_down_and_passes_only_positive_remainder(
    managed_table,
):
    make_row("ready", uid="uid")
    times = iter((10.0, 10.25))
    resident = FakeResident()
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        resident=resident,
        monotonic=lambda: next(times),
    )
    resident.close_results.append(
        (ManagedInputCloseResult(hardware_id, ManagedInputCloseStatus.CLOSED, None),)
    )

    report = registry.shutdown(1.0)
    repeated = registry.shutdown(60.0)

    assert report is repeated
    assert report.clean is True
    assert report.process_state is ManagedInputProcessState.CLOSED
    assert resident.close_calls == [0.75]


def test_shutdown_preclose_deadline_expiry_is_uncached_and_retryable(
    managed_table,
):
    make_row("ready", uid="uid")
    times = iter((10.0, 11.0, 20.0, 20.1))
    resident = FakeResident()
    registry, _, resident, hardware_id, _, _ = finalized_registry(
        resident=resident,
        monotonic=lambda: next(times),
    )

    first = registry.shutdown(0.5)
    resident.close_results.append(
        (ManagedInputCloseResult(hardware_id, ManagedInputCloseStatus.NOT_OPEN, None),)
    )
    second = registry.shutdown(1.0)

    assert first.error.code is ManagedInputErrorCode.SHUTDOWN_TIMEOUT
    assert first.process_state is ManagedInputProcessState.SHUTDOWN_INCOMPLETE
    assert second.clean is True
    assert resident.close_calls == [pytest.approx(0.9)]


def test_shutdown_dirty_close_is_cached_and_covers_frozen_deleted_row(
    managed_table,
):
    row = make_row("ready", uid="uid")
    registry, _, resident, hardware_id, _, _ = finalized_registry()
    ManagedAudioInputTable.delete_by_id(row.hardware_id)
    close_error = ManagedInputError(
        ManagedInputErrorCode.SHUTDOWN_TIMEOUT,
        "resident_close",
        "The Resident input stream did not close before the deadline.",
    )
    resident.close_results.append(
        (
            ManagedInputCloseResult(
                hardware_id,
                ManagedInputCloseStatus.TIMED_OUT,
                close_error,
            ),
        )
    )

    first = registry.shutdown(1.0)
    second = registry.shutdown(2.0)

    assert first is second
    assert first.clean is False
    assert first.process_state is ManagedInputProcessState.SHUTDOWN_INCOMPLETE
    assert first.close_results[0].hardware_id == hardware_id
    assert resident.close_calls == [1.0]


def prepared_finalization_registry():
    rows = tuple(
        sorted(
            ManagedAudioInputTable.select(),
            key=lambda row: row.hardware_id,
        )
    )
    scan_id = BackendScanId("runtime-process", 1)
    observations = tuple(
        observation(
            f"endpoint-{index}",
            scan_id=scan_id,
            device_index=index,
            uid=row.stable_backend_uid,
            endpoint_descriptor=descriptor(
                device_name=row.descriptor_device_name,
                hostapi_name=row.descriptor_hostapi_name,
                sample_rate_hz=row.descriptor_sample_rate_hz,
                input_channels=row.descriptor_input_channels,
            ),
        )
        for index, row in enumerate(rows)
    )
    discovery = FakeDiscovery()
    discovery.results.append(
        valid_scan(
            scan_id,
            *observations,
            capability=StableUidCapability.SUPPORTED,
        )
    )
    for item in observations:
        discovery.requeries[(scan_id, item.device_index)] = item
    blocker = FakeBlocker()
    resident = FakeResident()
    registry = new_registry(
        discovery=discovery,
        blocker=blocker,
        resident=resident,
    )
    registry.bootstrap()
    endpoints = tuple(
        HardwareEndpointIdentity(
            ManagedHardwareId(row.hardware_id),
            scan_id,
            item.device_index,
            item.hostapi_index,
            item.stable_backend_uid,
            item.descriptor.device_name,
            item.descriptor.hostapi_name,
            item.descriptor.sample_rate_hz,
            item.descriptor.input_channels,
        )
        for row, item in zip(rows, observations, strict=True)
    )
    return registry, discovery, blocker, resident, endpoints


def forged_startup_result(
    ready_streams: tuple[ResidentReadyStream, ...],
    failures: tuple[ResidentOpenFailure, ...] = (),
) -> ResidentStartupOpenResult:
    result = object.__new__(ResidentStartupOpenResult)
    object.__setattr__(result, "ready_streams", ready_streams)
    object.__setattr__(result, "failures", failures)
    return result


def forged_ready_stream(
    endpoint: HardwareEndpointIdentity,
    generation: ResidentStreamGenerationStamp,
) -> ResidentReadyStream:
    stream = object.__new__(ResidentReadyStream)
    object.__setattr__(stream, "endpoint", endpoint)
    object.__setattr__(stream, "generation", generation)
    return stream


def test_shutdown_is_fenced_while_finalization_external_open_is_in_progress(
    managed_table,
):
    row = make_row("ready", uid="uid")
    registry, discovery, blocker, resident, endpoints = (
        prepared_finalization_registry()
    )
    resident.open_results.append(
        ResidentStartupOpenResult((ready_for(endpoints[0]),), ())
    )
    entered = threading.Event()
    release = threading.Event()

    def pause_open():
        entered.set()
        assert release.wait(5)

    resident.on_open = pause_open
    final_results = []
    thread = threading.Thread(
        target=lambda: final_results.append(registry.finalize_bootstrap())
    )
    thread.start()
    assert entered.wait(5)
    assert registry._finalization_in_progress is True
    before = registry.inventory_snapshot()

    blocked = registry.shutdown(1.0)

    assert blocked.clean is False
    assert blocked.process_state is ManagedInputProcessState.SHUTDOWN_INCOMPLETE
    assert blocked.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert blocker.calls == []
    assert resident.close_calls == []
    assert registry._cached_shutdown_report is None
    assert registry.inventory_snapshot() == before

    release.set()
    thread.join(5)
    assert len(final_results) == 1
    resident.close_results.append(
        (
            ManagedInputCloseResult(
                ManagedHardwareId(row.hardware_id),
                ManagedInputCloseStatus.CLOSED,
                None,
            ),
        )
    )

    clean = registry.shutdown(1.0)

    assert clean.clean is True
    assert blocker.calls == [None]
    assert len(resident.open_calls) == 1
    assert len(resident.close_calls) == 1


def test_finalize_cannot_open_or_publish_once_shutdown_holds_mutex(
    managed_table,
):
    make_row("ready", uid="uid")
    registry, _, blocker, resident, _ = prepared_finalization_registry()
    resident.close_results.append(())
    entered = threading.Event()
    release = threading.Event()

    def pause_blocker():
        entered.set()
        assert release.wait(5)

    blocker.on_snapshot = pause_blocker
    shutdown_results = []
    thread = threading.Thread(
        target=lambda: shutdown_results.append(registry.shutdown(1.0))
    )
    thread.start()
    assert entered.wait(5)

    blocked = registry.finalize_bootstrap()

    assert blocked.blocker is ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
    assert resident.open_calls == []
    release.set()
    thread.join(5)
    assert shutdown_results[0].clean is True

    repeated = registry.finalize_bootstrap()

    assert repeated.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert resident.open_calls == []


def test_valid_resident_open_failure_is_isolated_per_row(managed_table):
    first = make_row("first", uid="uid-1")
    second = make_row("second", uid="uid-2")
    registry, _, _, resident, endpoints = prepared_finalization_registry()
    ready = ready_for(endpoints[0])
    resident.open_results.append(
        ResidentStartupOpenResult(
            (ready,),
            (
                ResidentOpenFailure(
                    ManagedHardwareId(second.hardware_id),
                    "open_failed",
                ),
            ),
        )
    )

    result = registry.finalize_bootstrap()
    rows = {row.hardware_id: row for row in result.inventory.rows}

    assert result.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert result.ready_streams == (ready,)
    assert rows[ManagedHardwareId(first.hardware_id)].availability is (
        ManagedInputAvailability.AVAILABLE
    )
    failed = rows[ManagedHardwareId(second.hardware_id)]
    assert failed.availability is ManagedInputAvailability.UNAVAILABLE
    assert failed.error_code is ManagedInputErrorCode.RESIDENT_OPEN_FAILED


@pytest.mark.parametrize(
    "failure_kind",
    (
        "exception",
        "wrong_type",
        "malformed",
        "missing",
        "extra",
        "duplicate",
        "wrong_endpoint",
        "wrong_generation",
    ),
)
def test_invalid_resident_open_partition_fails_finalization_globally(
    managed_table,
    failure_kind,
):
    row = make_row("ready", uid="uid")
    registry, discovery, _, resident, endpoints = (
        prepared_finalization_registry()
    )
    endpoint = endpoints[0]
    ready = ready_for(endpoint)
    if failure_kind == "exception":
        response = RuntimeError("resident detail")
    elif failure_kind == "wrong_type":
        response = object()
    elif failure_kind == "malformed":
        response = object.__new__(ResidentStartupOpenResult)
    elif failure_kind == "missing":
        response = ResidentStartupOpenResult((), ())
    elif failure_kind == "extra":
        extra_endpoint = replace(
            endpoint,
            hardware_id=ManagedHardwareId(999),
        )
        response = ResidentStartupOpenResult(
            (ready_for(extra_endpoint),),
            (),
        )
    elif failure_kind == "duplicate":
        response = forged_startup_result((ready, ready))
    elif failure_kind == "wrong_endpoint":
        wrong_endpoint = replace(endpoint, device_index=endpoint.device_index + 1)
        response = ResidentStartupOpenResult(
            (ready_for(wrong_endpoint),),
            (),
        )
    else:
        wrong_generation = ResidentStreamGenerationStamp(
            ManagedHardwareId(999),
            1,
            1,
        )
        response = ResidentStartupOpenResult(
            (forged_ready_stream(endpoint, wrong_generation),),
            (),
        )
    resident.open_results.append(response)

    first = registry.finalize_bootstrap()
    second = registry.finalize_bootstrap()

    assert first == second
    assert first.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert first.ready_streams == ()
    assert first.unresolved_hardware_ids == (
        ManagedHardwareId(row.hardware_id),
    )
    assert len(first.errors) == 1
    assert first.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert first.errors[0].stage == "resident_open"
    assert all(
        snapshot.availability is ManagedInputAvailability.UNAVAILABLE
        and snapshot.error_code is None
        for snapshot in first.inventory.rows
    )
    assert len(discovery.requery_calls) == 1
    assert len(resident.open_calls) == 1


def test_valid_zero_member_open_and_close_are_clean_and_cached(managed_table):
    registry, discovery, blocker, resident, endpoints = (
        prepared_finalization_registry()
    )
    assert endpoints == ()
    resident.open_results.append(ResidentStartupOpenResult((), ()))

    finalized = registry.finalize_bootstrap()
    finalized_again = registry.finalize_bootstrap()

    assert finalized == finalized_again
    assert finalized.process_state is ManagedInputProcessState.ACCEPTING_RUNS
    assert finalized.ready_streams == ()
    assert resident.open_calls == [()]
    resident.close_results.append(())

    closed = registry.shutdown(1.0)
    closed_again = registry.shutdown(2.0)

    assert closed is closed_again
    assert closed.clean is True
    assert closed.process_state is ManagedInputProcessState.CLOSED
    assert closed.close_results == ()
    assert blocker.calls == [None]
    assert resident.close_calls == [1.0]


@pytest.mark.parametrize("response", (RuntimeError("detail"), object()))
def test_invalid_zero_member_open_is_globally_fail_closed_and_cached(
    managed_table,
    response,
):
    registry, discovery, _, resident, endpoints = prepared_finalization_registry()
    assert endpoints == ()
    resident.open_results.append(response)

    first = registry.finalize_bootstrap()
    second = registry.finalize_bootstrap()

    assert first == second
    assert first.process_state is ManagedInputProcessState.BOOTSTRAPPING
    assert first.ready_streams == ()
    assert first.unresolved_hardware_ids == ()
    assert len(first.errors) == 1
    assert first.errors[0].code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert first.errors[0].stage == "resident_open"
    assert discovery.requery_calls == []
    assert resident.open_calls == [()]


def test_invalid_open_preserves_complete_membership_for_later_shutdown(
    managed_table,
):
    row = make_row("ready", uid="uid")
    registry, _, blocker, resident, _ = prepared_finalization_registry()
    resident.open_results.append(object())
    failed = registry.finalize_bootstrap()
    assert failed.process_state is ManagedInputProcessState.BOOTSTRAPPING
    hardware_id = ManagedHardwareId(row.hardware_id)
    resident.close_results.append(
        (
            ManagedInputCloseResult(
                hardware_id,
                ManagedInputCloseStatus.NOT_OPEN,
                None,
            ),
        )
    )

    report = registry.shutdown(1.0)

    assert report.clean is True
    assert report.close_results[0].hardware_id == hardware_id
    assert blocker.calls == [None]


@pytest.mark.parametrize("response", (RuntimeError("detail"), object()))
def test_invalid_zero_member_close_is_incomplete_and_cached(
    managed_table,
    response,
):
    registry, _, blocker, resident, _ = prepared_finalization_registry()
    resident.open_results.append(ResidentStartupOpenResult((), ()))
    registry.finalize_bootstrap()
    resident.close_results.append(response)

    first = registry.shutdown(1.0)
    second = registry.shutdown(2.0)

    assert first is second
    assert first.clean is False
    assert first.process_state is ManagedInputProcessState.SHUTDOWN_INCOMPLETE
    assert first.blocker is None
    assert first.close_results == ()
    assert first.error.code is ManagedInputErrorCode.INVARIANT_VIOLATION
    assert first.error.stage == "resident_close"
    assert blocker.calls == [None]
    assert resident.close_calls == [1.0]


def test_invalid_nonempty_close_partition_reports_every_frozen_id_failed(
    managed_table,
):
    row = make_row("ready", uid="uid")
    registry, _, blocker, resident, _ = prepared_finalization_registry()
    resident.open_results.append(ResidentStartupOpenResult((), (
        ResidentOpenFailure(ManagedHardwareId(row.hardware_id), "open_failed"),
    )))
    registry.finalize_bootstrap()
    resident.close_results.append(object())

    first = registry.shutdown(1.0)
    second = registry.shutdown(2.0)

    assert first is second
    assert first.clean is False
    assert len(first.close_results) == 1
    assert first.close_results[0].hardware_id == ManagedHardwareId(row.hardware_id)
    assert first.close_results[0].status is ManagedInputCloseStatus.FAILED
    assert first.close_results[0].error.code is (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    )
    assert blocker.calls == [None]
    assert resident.close_calls == [1.0]
def test_production_raw_backend_declares_absent_stable_uid_contract():
    from app.service.managed_input_contracts import (
        BackendScanId,
        StableUidCapability,
        StableUidContract,
    )
    from app.service.managed_input_sounddevice_backend import (
        ManagedInputSoundDeviceBackend,
    )

    class SoundDevice:
        @staticmethod
        def query_devices():
            return ()

        @staticmethod
        def query_hostapis():
            return ()

    scan = ManagedInputSoundDeviceBackend(
        SoundDevice()
    ).query_raw_input_endpoints(BackendScanId("runtime-production", 1))
    assert scan.stable_uid_capability is StableUidCapability.NONE
    assert scan.stable_uid_contract is StableUidContract.ABSENT
