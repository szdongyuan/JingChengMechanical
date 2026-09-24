from __future__ import annotations

from dataclasses import fields
from enum import Enum
import json

import pytest

from app.service.managed_input_contracts import (
    MAX_CALIBRATABLE_INPUT_CHANNELS,
    MAX_MANAGED_AUDIO_INPUTS,
    MAX_RESIDENT_INPUT_STREAMS,
    SIGNED63_MAX,
    AddManagedInputCommand,
    BackendDiscoveryResult,
    BackendInputScan,
    BackendScanId,
    CalibrateManagedInputCommand,
    FrozenRunCalibrationSnapshot,
    FrozenRunHardwareSnapshot,
    FrozenRunInputProjection,
    FrozenRunProjectionResult,
    HardwareEndpointIdentity,
    InputEndpointDescriptor,
    InputEndpointObservation,
    ManagedHardwareId,
    ManagedInputAvailability,
    ManagedInputBlocker,
    ManagedInputBootstrapResult,
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
    ManagedInputCommandKind,
    ManagedInputError,
    ManagedInputErrorCode,
    ManagedInputInventorySnapshot,
    ManagedInputProcessState,
    ManagedInputResult,
    ManagedInputResultStatus,
    ManagedInputShutdownReport,
    ManagedInputSnapshot,
    MapStartupEndpointCommand,
    OfflineMaintenanceBlockerSnapshot,
    RawBackendInputScan,
    RawHostApiIdentity,
    RefreshManagedInputsCommand,
    RemoveManagedInputCommand,
    ResidentCalibrationMeasurement,
    ResidentInputFailureNotice,
    ResidentOpenFailure,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
    SelectOrReplaceManagedInputCommand,
    StableUidCapability,
    StableUidContract,
    normalize_managed_input_alias,
    parse_calibration_json,
    serialize_calibration_json,
    validate_shutdown_timeout,
)


def descriptor(channels: int = 2) -> InputEndpointDescriptor:
    return InputEndpointDescriptor("Device", "Host API", 48_000.0, channels)


def scan_id(sequence: int = 1) -> BackendScanId:
    return BackendScanId("process-1", sequence)


def observation(
    *,
    uid: str | None = "uid-1",
    observation_id: str = "observation-1",
) -> InputEndpointObservation:
    return InputEndpointObservation(
        scan_id(),
        observation_id,
        3,
        1,
        uid,
        descriptor(),
    )


def endpoint(
    hardware_id: int = 1,
    device_index: int = 3,
) -> HardwareEndpointIdentity:
    return HardwareEndpointIdentity(
        ManagedHardwareId(hardware_id),
        scan_id(),
        device_index,
        1,
        "uid-1",
        "Device",
        "Host API",
        48_000.0,
        2,
    )


def generation(hardware_id: int = 1) -> ResidentStreamGenerationStamp:
    return ResidentStreamGenerationStamp(ManagedHardwareId(hardware_id), 1, 1)


def ready(hardware_id: int = 1, device_index: int = 3) -> ResidentReadyStream:
    return ResidentReadyStream(
        endpoint(hardware_id, device_index), generation(hardware_id)
    )


def error(
    code: ManagedInputErrorCode = ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
) -> ManagedInputError:
    return ManagedInputError(code, "bootstrap", "endpoint unavailable")


def inventory(
    *,
    state: ManagedInputProcessState = ManagedInputProcessState.ACCEPTING_RUNS,
    active_capture_count: int | None = None,
    rows: tuple[ManagedInputSnapshot, ...] = (),
) -> ManagedInputInventorySnapshot:
    return ManagedInputInventorySnapshot(
        state,
        state is ManagedInputProcessState.RESTART_REQUIRED,
        active_capture_count,
        scan_id(),
        None,
        rows,
    )


def row_snapshot(
    hardware_id: int,
    alias: str,
    availability: ManagedInputAvailability,
    *,
    device_index: int = 3,
    error_code: ManagedInputErrorCode | None = None,
) -> ManagedInputSnapshot:
    ready_endpoint = (
        endpoint(hardware_id, device_index)
        if availability is ManagedInputAvailability.AVAILABLE
        else None
    )
    ready_generation = (
        generation(hardware_id)
        if availability is ManagedInputAvailability.AVAILABLE
        else None
    )
    return ManagedInputSnapshot(
        ManagedHardwareId(hardware_id),
        alias,
        1,
        descriptor(),
        0,
        availability,
        ready_endpoint,
        ready_generation,
        error_code,
    )


def test_constants_are_the_exact_limits():
    assert SIGNED63_MAX == 2**63 - 1
    assert MAX_CALIBRATABLE_INPUT_CHANNELS == 508
    assert MAX_MANAGED_AUDIO_INPUTS == 6
    assert MAX_RESIDENT_INPUT_STREAMS == 6


def test_max_calibratable_channels_matches_canonical_worst_state():
    worst_correction = -2.2250738585072014e-308
    last_supported = tuple(
        (channel_index, True, SIGNED63_MAX, worst_correction)
        for channel_index in range(MAX_CALIBRATABLE_INPUT_CHANNELS)
    )
    assert len(
        serialize_calibration_json(
            last_supported,
            expected_channels=MAX_CALIBRATABLE_INPUT_CHANNELS,
        ).encode("utf-8")
    ) <= 64 * 1024

    first_unsupported = last_supported + (
        (
            MAX_CALIBRATABLE_INPUT_CHANNELS,
            True,
            SIGNED63_MAX,
            worst_correction,
        ),
    )
    with pytest.raises(ValueError):
        serialize_calibration_json(
            first_unsupported,
            expected_channels=MAX_CALIBRATABLE_INPUT_CHANNELS + 1,
        )


@pytest.mark.parametrize(
    "input_channels",
    (MAX_CALIBRATABLE_INPUT_CHANNELS + 1, 2**31 - 1),
)
def test_endpoint_observation_channel_contract_remains_wide(input_channels):
    wide_descriptor = InputEndpointDescriptor(
        "Device",
        "Host API",
        48_000.0,
        input_channels,
    )
    wide_observation = InputEndpointObservation(
        scan_id(),
        "wide-observation",
        3,
        1,
        None,
        wide_descriptor,
    )
    assert wide_observation.descriptor.input_channels == input_channels


def test_canonical_calibration_rejects_compact_unsupported_channel_count():
    unsupported_channels = MAX_CALIBRATABLE_INPUT_CHANNELS + 1
    channels = tuple(
        (channel_index, False, 0, None)
        for channel_index in range(unsupported_channels)
    )
    with pytest.raises(ValueError):
        serialize_calibration_json(
            channels,
            expected_channels=unsupported_channels,
        )

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
    assert len(compact.encode("utf-8")) < 64 * 1024
    with pytest.raises(ValueError):
        parse_calibration_json(
            compact,
            expected_channels=unsupported_channels,
        )


@pytest.mark.parametrize("invalid", [True, 0, -1, 2**63, 1.0, "1"])
def test_managed_hardware_id_rejects_invalid_exact_integer(invalid):
    with pytest.raises(ValueError):
        ManagedHardwareId(invalid)


def test_nested_public_values_reject_subclasses_and_structural_copies():
    class HardwareIdSubclass(ManagedHardwareId):
        pass

    with pytest.raises(ValueError):
        ResidentStreamGenerationStamp(HardwareIdSubclass(1), 1, 1)
    with pytest.raises(ValueError):
        InputEndpointObservation(
            scan_id(),
            "o",
            0,
            0,
            None,
            {
                "device_name": "Device",
                "hostapi_name": "Host API",
                "sample_rate_hz": 48_000.0,
                "input_channels": 2,
            },
        )


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a", True),
        ("A_z.9-", True),
        ("x" * 64, True),
        ("", False),
        ("x" * 65, False),
        ("with space", False),
        ("é", False),
    ],
)
def test_backend_scan_id_validates_process_id(value, valid):
    if valid:
        assert BackendScanId(value, 1).process_instance_id == value
    else:
        with pytest.raises(ValueError):
            BackendScanId(value, 1)


@pytest.mark.parametrize("invalid", [True, 0, -1, SIGNED63_MAX + 1])
def test_backend_scan_id_rejects_invalid_sequence(invalid):
    with pytest.raises(ValueError):
        BackendScanId("p", invalid)


def test_descriptor_requires_normalized_bounded_names_exact_float_and_channels():
    assert descriptor().sample_rate_hz == 48_000.0
    invalid_values = (
        ("e\u0301", "Host", 1.0, 1),
        ("D", "", 1.0, 1),
        ("D", "H", 1, 1),
        ("D", "H", float("nan"), 1),
        ("D", "H", 0.0, 1),
        ("D", "H", 1.0, True),
        ("D", "H", 1.0, 0),
        ("D", "H", 1.0, 2**31),
    )
    for values in invalid_values:
        with pytest.raises(ValueError):
            InputEndpointDescriptor(*values)


def test_observation_validates_opaque_ids_indexes_and_exact_nested_values():
    assert observation().stable_backend_uid == "uid-1"
    for invalid_id in ("", " leading", "trailing ", "has\ncontrol", "x" * 257):
        with pytest.raises(ValueError):
            InputEndpointObservation(scan_id(), invalid_id, 0, 0, None, descriptor())
    for invalid_uid in ("", " uid", "uid ", "bad\x00uid", "ü" * 129):
        with pytest.raises(ValueError):
            InputEndpointObservation(scan_id(), "o", 0, 0, invalid_uid, descriptor())
    for invalid_index in (True, -1, 2**31, 1.0):
        with pytest.raises(ValueError):
            InputEndpointObservation(
                scan_id(), "o", invalid_index, 0, None, descriptor()
            )


def test_endpoint_identity_validates_all_fields_like_an_observation():
    value = endpoint()
    assert value.hardware_id == ManagedHardwareId(1)
    with pytest.raises(ValueError):
        HardwareEndpointIdentity(
            ManagedHardwareId(1),
            scan_id(),
            0,
            0,
            None,
            "D",
            "H",
            48_000,
            1,
        )


def test_resident_values_enforce_exact_generation_and_partition_types():
    stream = ready()
    assert stream.generation.hardware_id == stream.endpoint.hardware_id
    with pytest.raises(ValueError):
        ResidentReadyStream(endpoint(), generation(2))
    with pytest.raises(ValueError):
        ResidentStreamGenerationStamp(ManagedHardwareId(1), True, 1)
    with pytest.raises(ValueError):
        ResidentInputFailureNotice(generation(), "UPPER")
    with pytest.raises(ValueError):
        ResidentOpenFailure(ManagedHardwareId(1), "")
    result = ResidentStartupOpenResult((stream,), ())
    assert result.ready_streams == (stream,)
    with pytest.raises(ValueError):
        ResidentStartupOpenResult((stream,), (ResidentOpenFailure(ManagedHardwareId(1), "failed"),))


def test_resident_open_failure_preserves_legacy_construction_and_optional_detail():
    legacy = ResidentOpenFailure(ManagedHardwareId(1), "resident_open_failed")
    assert legacy.detail == ""
    detailed = ResidentOpenFailure(
        ManagedHardwareId(1), "resident_open_failed", "synthetic open failure"
    )
    assert detailed.detail == "synthetic open failure"
    assert detailed.error_code == legacy.error_code
    assert ResidentOpenFailure(ManagedHardwareId(1), "failed", "音" * 256).detail == "音" * 256


@pytest.mark.parametrize("detail", [None, 1, "x" * 257, "line\nbreak", "nul\0byte", "\ud800"])
def test_resident_open_failure_rejects_unbounded_or_unsafe_detail(detail):
    with pytest.raises(ValueError):
        ResidentOpenFailure(ManagedHardwareId(1), "resident_open_failed", detail)


def test_calibration_measurement_requires_exact_finite_values():
    value = ResidentCalibrationMeasurement(generation(), 0, -12.5, 480)
    assert value.captured_frames == 480
    for values in (
        (generation(), True, -1.0, 1),
        (generation(), 0, float("inf"), 1),
        (generation(), 0, -1.0, True),
        (generation(), 0, -1.0, 0),
    ):
        with pytest.raises(ValueError):
            ResidentCalibrationMeasurement(*values)


def test_blocker_snapshot_requires_both_facts_or_neither():
    OfflineMaintenanceBlockerSnapshot(None, None)
    OfflineMaintenanceBlockerSnapshot(0, False)
    with pytest.raises(ValueError):
        OfflineMaintenanceBlockerSnapshot(None, False)
    with pytest.raises(ValueError):
        OfflineMaintenanceBlockerSnapshot(True, False)
    with pytest.raises(ValueError):
        OfflineMaintenanceBlockerSnapshot(0, 0)


def test_all_enum_members_and_serialized_values_are_closed():
    expected = {
        ManagedInputAvailability: {
            "AVAILABLE": "available",
            "MAPPED_PENDING_OPEN": "mapped_pending_open",
            "UNAVAILABLE": "unavailable",
            "NEEDS_USER_SELECTION": "needs_user_selection",
        },
        ManagedInputProcessState: {
            "BOOTSTRAPPING": "bootstrapping",
            "ACCEPTING_RUNS": "accepting_runs",
            "RESTART_REQUIRED": "restart_required",
            "SHUTTING_DOWN": "shutting_down",
            "CLOSED": "closed",
            "SHUTDOWN_INCOMPLETE": "shutdown_incomplete",
        },
        ManagedInputCommandKind: {
            "ADD": "add",
            "SELECT_OR_REPLACE": "select_or_replace",
            "REMOVE": "remove",
            "REFRESH": "refresh",
            "CALIBRATE": "calibrate",
        },
        ManagedInputResultStatus: {
            "SUCCEEDED": "succeeded",
            "SUCCEEDED_RESTART_REQUIRED": "succeeded_restart_required",
            "BUSY": "busy",
            "CAPACITY_REJECTED": "capacity_rejected",
            "REJECTED": "rejected",
            "FAILED": "failed",
        },
        ManagedInputBlocker: {
            "MAINTENANCE_IN_PROGRESS": "maintenance_in_progress",
            "OWNERSHIP_STATE_UNKNOWN": "ownership_state_unknown",
            "ACTIVE_CAPTURE_EXISTS": "active_capture_exists",
            "TARGET_RESOURCE_BUSY": "target_resource_busy",
        },
        StableUidCapability: {"NONE": "none", "SUPPORTED": "supported"},
        StableUidContract: {
            "ABSENT": "absent",
            "REVIEWED_OPAQUE_UID": "reviewed_opaque_uid",
        },
        ManagedInputCloseStatus: {
            "CLOSED": "closed",
            "NOT_OPEN": "not_open",
            "TIMED_OUT": "timed_out",
            "FAILED": "failed",
        },
    }
    for enum_type, members in expected.items():
        assert issubclass(enum_type, Enum)
        assert {member.name: member.value for member in enum_type} == members
    assert {member.name: member.value for member in ManagedInputErrorCode} == {
        name.upper(): name
        for name in (
            "capacity_reached",
            "alias_already_exists",
            "backend_identity_conflict",
            "hardware_not_found",
            "stale_hardware_revision",
            "stale_calibration_revision",
            "stale_scan",
            "stale_endpoint_observation",
            "endpoint_unavailable",
            "endpoint_needs_user_selection",
            "endpoint_already_bound",
            "endpoint_validation_failed",
            "config_reference_exists",
            "resident_lease_exists",
            "resident_open_failed",
            "resident_stream_failed",
            "calibration_required",
            "calibration_invalid",
            "calibration_capture_failed",
            "database_transaction_failed",
            "refresh_requires_restart",
            "restart_required",
            "shutting_down",
            "shutdown_timeout",
            "invariant_violation",
        )
    }


def test_snapshot_available_requires_matching_endpoint_and_generation():
    value = ManagedInputSnapshot(
        ManagedHardwareId(1),
        "Alias",
        1,
        descriptor(),
        0,
        ManagedInputAvailability.AVAILABLE,
        endpoint(),
        generation(),
        None,
    )
    assert value.endpoint is not None
    with pytest.raises(ValueError):
        ManagedInputSnapshot(
            ManagedHardwareId(1),
            "Alias",
            1,
            descriptor(),
            0,
            ManagedInputAvailability.AVAILABLE,
            endpoint(),
            None,
            None,
        )
    with pytest.raises(ValueError):
        ManagedInputSnapshot(
            ManagedHardwareId(1),
            "Alias",
            1,
            descriptor(),
            0,
            ManagedInputAvailability.UNAVAILABLE,
            endpoint(),
            generation(),
            error(),
        )


def test_available_snapshot_descriptor_matches_endpoint_exactly():
    with pytest.raises(ValueError):
        ManagedInputSnapshot(
            ManagedHardwareId(1),
            "Alias",
            1,
            InputEndpointDescriptor("Device", "Host API", 44_100.0, 2),
            0,
            ManagedInputAvailability.AVAILABLE,
            endpoint(),
            generation(),
            None,
        )


@pytest.mark.parametrize(
    ("availability", "error_code"),
    [
        (ManagedInputAvailability.AVAILABLE, None),
        (ManagedInputAvailability.MAPPED_PENDING_OPEN, None),
        (
            ManagedInputAvailability.NEEDS_USER_SELECTION,
            ManagedInputErrorCode.ENDPOINT_NEEDS_USER_SELECTION,
        ),
        (ManagedInputAvailability.UNAVAILABLE, None),
        (
            ManagedInputAvailability.UNAVAILABLE,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ),
        (
            ManagedInputAvailability.UNAVAILABLE,
            ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED,
        ),
        (
            ManagedInputAvailability.UNAVAILABLE,
            ManagedInputErrorCode.RESIDENT_OPEN_FAILED,
        ),
        (
            ManagedInputAvailability.UNAVAILABLE,
            ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
        ),
    ],
)
def test_snapshot_accepts_exact_availability_error_combinations(
    availability,
    error_code,
):
    value = row_snapshot(1, "Alias", availability, error_code=error_code)
    assert value.error_code is error_code


@pytest.mark.parametrize(
    ("availability", "error_code"),
    [
        (
            ManagedInputAvailability.AVAILABLE,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ),
        (
            ManagedInputAvailability.MAPPED_PENDING_OPEN,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ),
        (ManagedInputAvailability.NEEDS_USER_SELECTION, None),
        (
            ManagedInputAvailability.NEEDS_USER_SELECTION,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ),
        (
            ManagedInputAvailability.UNAVAILABLE,
            ManagedInputErrorCode.STALE_SCAN,
        ),
    ],
)
def test_snapshot_rejects_other_availability_error_combinations(
    availability,
    error_code,
):
    with pytest.raises(ValueError):
        row_snapshot(1, "Alias", availability, error_code=error_code)


def test_inventory_requires_consistent_state_bounded_sorted_rows():
    first = ManagedInputSnapshot(
        ManagedHardwareId(1),
        "A",
        1,
        descriptor(),
        0,
        ManagedInputAvailability.UNAVAILABLE,
        None,
        None,
        ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
    )
    assert inventory(rows=(first,)).rows == (first,)
    with pytest.raises(ValueError):
        ManagedInputInventorySnapshot(
            ManagedInputProcessState.ACCEPTING_RUNS,
            True,
            None,
            scan_id(),
            None,
            (),
        )
    with pytest.raises(ValueError):
        ManagedInputInventorySnapshot(
            ManagedInputProcessState.ACCEPTING_RUNS,
            False,
            True,
            scan_id(),
            None,
            (),
        )


@pytest.mark.parametrize(
    "scan_error_code",
    [
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ],
)
def test_inventory_accepts_only_documented_scan_diagnostics(scan_error_code):
    value = ManagedInputInventorySnapshot(
        ManagedInputProcessState.ACCEPTING_RUNS,
        False,
        None,
        scan_id(),
        scan_error_code,
        (),
    )
    assert value.scan_error_code is scan_error_code


@pytest.mark.parametrize(
    "scan_error_code",
    [
        ManagedInputErrorCode.STALE_SCAN,
        ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
    ],
)
def test_inventory_rejects_non_scan_diagnostics(scan_error_code):
    with pytest.raises(ValueError):
        ManagedInputInventorySnapshot(
            ManagedInputProcessState.ACCEPTING_RUNS,
            False,
            None,
            scan_id(),
            scan_error_code,
            (),
        )


def test_inventory_rejects_process_invalid_mapped_or_available_rows():
    mapped = row_snapshot(
        1, "A", ManagedInputAvailability.MAPPED_PENDING_OPEN
    )
    with pytest.raises(ValueError):
        inventory(rows=(mapped,))
    available = row_snapshot(1, "A", ManagedInputAvailability.AVAILABLE)
    with pytest.raises(ValueError):
        inventory(
            state=ManagedInputProcessState.BOOTSTRAPPING,
            rows=(available,),
        )


def test_frozen_run_projection_requires_matching_snapshots_and_one_result_arm():
    hardware = FrozenRunHardwareSnapshot(
        ManagedHardwareId(1), 2, "Alias", endpoint(), 3
    )
    calibration = FrozenRunCalibrationSnapshot(
        ManagedHardwareId(1), 2, 3, 48_000.0, 2, ((0, 1, -2.5), (1, 2, 1.0))
    )
    projection = FrozenRunInputProjection(hardware, calibration)
    assert FrozenRunProjectionResult(projection, None).projection is projection
    assert FrozenRunProjectionResult(None, error()).error is not None
    with pytest.raises(ValueError):
        FrozenRunProjectionResult(None, None)
    with pytest.raises(ValueError):
        FrozenRunProjectionResult(projection, error())
    with pytest.raises(ValueError):
        FrozenRunCalibrationSnapshot(
            ManagedHardwareId(1), 2, 3, 48_000.0, 2, ((1, 1, 0.0), (0, 1, 0.0))
        )


@pytest.mark.parametrize("correction_db", [-400.0, 400.0])
def test_frozen_calibration_accepts_inclusive_correction_bounds(correction_db):
    value = FrozenRunCalibrationSnapshot(
        ManagedHardwareId(1),
        1,
        1,
        48_000.0,
        1,
        ((0, 1, correction_db),),
    )
    assert value.channel_values[0][2] == correction_db


@pytest.mark.parametrize("correction_db", [-400.000001, 400.000001])
def test_frozen_calibration_rejects_corrections_outside_bounds(correction_db):
    with pytest.raises(ValueError):
        FrozenRunCalibrationSnapshot(
            ManagedHardwareId(1),
            1,
            1,
            48_000.0,
            1,
            ((0, 1, correction_db),),
        )


def test_commands_validate_exact_fields_and_refresh_is_empty():
    AddManagedInputCommand(" Alias ", scan_id(), "o")
    SelectOrReplaceManagedInputCommand(ManagedHardwareId(1), 1, scan_id(), "o")
    RemoveManagedInputCommand(ManagedHardwareId(1), 1)
    MapStartupEndpointCommand(ManagedHardwareId(1), 1, scan_id(), "o")
    RefreshManagedInputsCommand()
    assert fields(RefreshManagedInputsCommand) == ()
    for invalid in (True, 0, SIGNED63_MAX + 1):
        with pytest.raises(ValueError):
            RemoveManagedInputCommand(ManagedHardwareId(1), invalid)
    with pytest.raises(ValueError):
        CalibrateManagedInputCommand(
            ManagedHardwareId(1), 1, 0, 0, -200.0, True
        )
    CalibrateManagedInputCommand(
        ManagedHardwareId(1), 1, 0, 0, -200.0, SIGNED63_MAX
    )


def test_alias_normalization_checks_scalar_and_utf8_limits():
    assert normalize_managed_input_alias("  e\u0301  ") == "é"
    for invalid in ("", "   ", "x" * 97, "💡" * 97, "\ud800"):
        with pytest.raises(ValueError):
            normalize_managed_input_alias(invalid)


def test_calibration_json_is_canonical_exact_version_one():
    channels = ((1, False, 3, None), (0, True, 2, -1.25))
    encoded = serialize_calibration_json(channels, expected_channels=2)
    assert encoded == (
        '{"version":1,"channels":[{"channel_index":0,"trusted":true,'
        '"channel_calibration_revision":2,"correction_db":-1.25},'
        '{"channel_index":1,"trusted":false,'
        '"channel_calibration_revision":3,"correction_db":null}]}'
    )
    assert parse_calibration_json(encoded, expected_channels=2) == (
        (0, True, 2, -1.25),
        (1, False, 3, None),
    )
    invalid = (
        '{"version":2,"channels":[]}',
        '{"version":1,"channels":[],"extra":1}',
        '{"version":1,"channels":[{"channel_index":0,"trusted":false,'
        '"channel_calibration_revision":0,"correction_db":0.0}]}',
        '{"version":1,"channels":[{"channel_index":0,"trusted":true,'
        '"channel_calibration_revision":0,"correction_db":NaN}]}',
    )
    for raw in invalid:
        with pytest.raises(ValueError):
            parse_calibration_json(raw, expected_channels=1)


def test_discovery_values_validate_exact_types_and_result_exclusivity():
    raw_hostapi = RawHostApiIdentity(1, "Host API")
    raw = RawBackendInputScan(
        scan_id(),
        True,
        StableUidCapability.SUPPORTED,
        StableUidContract.REVIEWED_OPAQUE_UID,
        (raw_hostapi,),
        (observation(),),
    )
    validated = BackendInputScan(
        scan_id(), StableUidCapability.SUPPORTED, (observation(),)
    )
    assert raw.observations == validated.observations
    BackendDiscoveryResult(validated, None)
    BackendDiscoveryResult(None, error(ManagedInputErrorCode.INVARIANT_VIOLATION))
    with pytest.raises(ValueError):
        BackendDiscoveryResult(None, None)
    with pytest.raises(ValueError):
        BackendDiscoveryResult(validated, error())


@pytest.mark.parametrize(
    "error_code",
    [
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ],
)
def test_discovery_failure_accepts_only_scan_failure_codes(error_code):
    result = BackendDiscoveryResult(None, error(error_code))
    assert result.error.code is error_code


@pytest.mark.parametrize(
    "error_code",
    [
        ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        ManagedInputErrorCode.SHUTTING_DOWN,
    ],
)
def test_discovery_failure_rejects_non_scan_failure_codes(error_code):
    with pytest.raises(ValueError):
        BackendDiscoveryResult(None, error(error_code))


def invalid_scan_inventory(
    *,
    rows: tuple[ManagedInputSnapshot, ...] = (),
    error_code: ManagedInputErrorCode = (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    ),
    state: ManagedInputProcessState = ManagedInputProcessState.BOOTSTRAPPING,
    published_scan_id: BackendScanId | None = None,
) -> ManagedInputInventorySnapshot:
    return ManagedInputInventorySnapshot(
        state,
        False,
        None,
        published_scan_id,
        error_code,
        rows,
    )


@pytest.mark.parametrize(
    "error_code",
    [
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ],
)
def test_invalid_startup_accepts_exact_zero_and_nonzero_row_shapes(error_code):
    global_error = error(error_code)
    zero_row = ManagedInputBootstrapResult(
        ManagedInputProcessState.BOOTSTRAPPING,
        invalid_scan_inventory(error_code=error_code),
        (),
        (),
        None,
        (global_error,),
    )
    assert zero_row.errors == (global_error,)

    unavailable = row_snapshot(
        1, "A", ManagedInputAvailability.UNAVAILABLE
    )
    one_row = ManagedInputBootstrapResult(
        ManagedInputProcessState.BOOTSTRAPPING,
        invalid_scan_inventory(rows=(unavailable,), error_code=error_code),
        (),
        (ManagedHardwareId(1),),
        None,
        (global_error,),
    )
    assert one_row.unresolved_hardware_ids == (ManagedHardwareId(1),)


def test_invalid_startup_rejects_every_incoherent_aggregate_shape():
    unavailable = row_snapshot(
        1, "A", ManagedInputAvailability.UNAVAILABLE
    )
    needs_selection = row_snapshot(
        1,
        "A",
        ManagedInputAvailability.NEEDS_USER_SELECTION,
        error_code=ManagedInputErrorCode.ENDPOINT_NEEDS_USER_SELECTION,
    )
    invariant_error = error(ManagedInputErrorCode.INVARIANT_VIOLATION)
    conflict_error = error(ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT)
    cases = (
        (
            ManagedInputProcessState.ACCEPTING_RUNS,
            invalid_scan_inventory(
                state=ManagedInputProcessState.ACCEPTING_RUNS
            ),
            (),
            (),
            (invariant_error,),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(published_scan_id=scan_id()),
            (),
            (),
            (invariant_error,),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(rows=(needs_selection,)),
            (),
            (ManagedHardwareId(1),),
            (invariant_error,),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(rows=(unavailable,)),
            (),
            (),
            (invariant_error,),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(),
            (),
            (),
            (),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(),
            (),
            (),
            (conflict_error,),
        ),
        (
            ManagedInputProcessState.BOOTSTRAPPING,
            invalid_scan_inventory(),
            (),
            (),
            (invariant_error, invariant_error),
        ),
    )
    for process_state, inv, ready_streams, unresolved_ids, errors in cases:
        with pytest.raises(ValueError):
            ManagedInputBootstrapResult(
                process_state,
                inv,
                ready_streams,
                unresolved_ids,
                None,
                errors,
            )


def valid_scan_bootstrap(
    row_count: int,
    error_count: int,
) -> ManagedInputBootstrapResult:
    rows = tuple(
        row_snapshot(
            index + 1,
            chr(ord("A") + index),
            ManagedInputAvailability.UNAVAILABLE,
            error_code=ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        )
        for index in range(row_count)
    )
    return ManagedInputBootstrapResult(
        ManagedInputProcessState.BOOTSTRAPPING,
        inventory(
            state=ManagedInputProcessState.BOOTSTRAPPING,
            rows=rows,
        ),
        (),
        tuple(row.hardware_id for row in rows),
        None,
        tuple(
            error(ManagedInputErrorCode.ENDPOINT_UNAVAILABLE)
            for _ in range(error_count)
        ),
    )


@pytest.mark.parametrize(
    ("row_count", "error_count"),
    [(0, 0), (1, 1), (2, 2)],
)
def test_valid_scan_bootstrap_bounds_errors_by_inventory_rows(
    row_count,
    error_count,
):
    result = valid_scan_bootstrap(row_count, error_count)
    assert len(result.errors) == error_count


@pytest.mark.parametrize(
    ("row_count", "error_count"),
    [(0, 1), (1, 2)],
)
def test_valid_scan_bootstrap_rejects_more_errors_than_rows(
    row_count,
    error_count,
):
    with pytest.raises(ValueError):
        valid_scan_bootstrap(row_count, error_count)


def test_bootstrap_and_managed_results_enforce_closed_invariants():
    inv = inventory()
    bootstrap = ManagedInputBootstrapResult(
        ManagedInputProcessState.ACCEPTING_RUNS, inv, (), (), None, ()
    )
    assert bootstrap.inventory is inv
    success = ManagedInputResult(
        ManagedInputResultStatus.SUCCEEDED,
        ManagedInputCommandKind.REFRESH,
        ManagedInputProcessState.ACCEPTING_RUNS,
        None,
        inv,
        None,
        (),
        (),
    )
    assert success.inventory is inv
    with pytest.raises(ValueError):
        ManagedInputResult(
            ManagedInputResultStatus.BUSY,
            ManagedInputCommandKind.ADD,
            ManagedInputProcessState.ACCEPTING_RUNS,
            None,
            None,
            None,
            (),
            (),
        )
    with pytest.raises(ValueError):
        ManagedInputResult(
            ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
            ManagedInputCommandKind.ADD,
            ManagedInputProcessState.ACCEPTING_RUNS,
            None,
            inv,
            None,
            (),
            (),
        )


def managed_result(
    status: ManagedInputResultStatus,
    command_kind: ManagedInputCommandKind,
    error_code: ManagedInputErrorCode | None = None,
) -> ManagedInputResult:
    process_state = (
        ManagedInputProcessState.RESTART_REQUIRED
        if status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
        else ManagedInputProcessState.ACCEPTING_RUNS
    )
    blocker = (
        ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
        if status is ManagedInputResultStatus.BUSY
        else None
    )
    result_inventory = None if blocker is not None else inventory(state=process_state)
    errors = () if error_code is None else (error(error_code),)
    return ManagedInputResult(
        status,
        command_kind,
        process_state,
        blocker,
        result_inventory,
        None,
        (),
        errors,
    )


@pytest.mark.parametrize(
    ("status", "command_kind", "error_code"),
    [
        (
            ManagedInputResultStatus.SUCCEEDED,
            ManagedInputCommandKind.REFRESH,
            None,
        ),
        *[
            (
                ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
                command_kind,
                None,
            )
            for command_kind in (
                ManagedInputCommandKind.ADD,
                ManagedInputCommandKind.SELECT_OR_REPLACE,
                ManagedInputCommandKind.REMOVE,
                ManagedInputCommandKind.CALIBRATE,
            )
        ],
        *[
            (ManagedInputResultStatus.BUSY, command_kind, None)
            for command_kind in ManagedInputCommandKind
        ],
        (
            ManagedInputResultStatus.CAPACITY_REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.CAPACITY_REACHED,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.ALIAS_ALREADY_EXISTS,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.SELECT_OR_REPLACE,
            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.REMOVE,
            ManagedInputErrorCode.CONFIG_REFERENCE_EXISTS,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.REFRESH,
            ManagedInputErrorCode.REFRESH_REQUIRES_RESTART,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.CALIBRATE,
            ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.SELECT_OR_REPLACE,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.REMOVE,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.CALIBRATE,
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
        ),
    ],
)
def test_managed_result_accepts_closed_status_command_error_table(
    status,
    command_kind,
    error_code,
):
    value = managed_result(status, command_kind, error_code)
    assert value.status is status


@pytest.mark.parametrize(
    ("status", "command_kind", "error_code"),
    [
        (
            ManagedInputResultStatus.SUCCEEDED,
            ManagedInputCommandKind.REFRESH,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        (
            ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        (
            ManagedInputResultStatus.BUSY,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        (
            ManagedInputResultStatus.CAPACITY_REJECTED,
            ManagedInputCommandKind.REFRESH,
            ManagedInputErrorCode.CAPACITY_REACHED,
        ),
        (
            ManagedInputResultStatus.CAPACITY_REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.ALIAS_ALREADY_EXISTS,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            None,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.SELECT_OR_REPLACE,
            ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
        ),
        (
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.CALIBRATE,
            ManagedInputErrorCode.CALIBRATION_REQUIRED,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.ADD,
            None,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.REFRESH,
            ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        (
            ManagedInputResultStatus.FAILED,
            ManagedInputCommandKind.ADD,
            ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
        ),
    ],
)
def test_managed_result_rejects_other_status_command_error_combinations(
    status,
    command_kind,
    error_code,
):
    with pytest.raises(ValueError):
        managed_result(status, command_kind, error_code)


def test_managed_result_rejects_multiple_action_errors():
    with pytest.raises(ValueError):
        ManagedInputResult(
            ManagedInputResultStatus.REJECTED,
            ManagedInputCommandKind.ADD,
            ManagedInputProcessState.ACCEPTING_RUNS,
            None,
            inventory(),
            None,
            (),
            (
                error(ManagedInputErrorCode.ALIAS_ALREADY_EXISTS),
                error(ManagedInputErrorCode.STALE_SCAN),
            ),
        )


def latched_result(
    command_kind: ManagedInputCommandKind,
    *,
    status: ManagedInputResultStatus = ManagedInputResultStatus.REJECTED,
    latch_code: ManagedInputErrorCode = (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    ),
    result_error_code: ManagedInputErrorCode | None = (
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT
    ),
    observations: tuple[InputEndpointObservation, ...] = (),
) -> ManagedInputResult:
    latched_inventory = ManagedInputInventorySnapshot(
        ManagedInputProcessState.ACCEPTING_RUNS,
        False,
        None,
        scan_id(),
        latch_code,
        (),
    )
    errors = (
        ()
        if result_error_code is None
        else (error(result_error_code),)
    )
    return ManagedInputResult(
        status,
        command_kind,
        ManagedInputProcessState.ACCEPTING_RUNS,
        None,
        latched_inventory,
        None,
        observations,
        errors,
    )


def persistent_latched_non_rejected_result(
    command_kind: ManagedInputCommandKind,
    status: ManagedInputResultStatus,
) -> ManagedInputResult:
    process_state = (
        ManagedInputProcessState.RESTART_REQUIRED
        if status is ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED
        else ManagedInputProcessState.ACCEPTING_RUNS
    )
    blocker = (
        ManagedInputBlocker.TARGET_RESOURCE_BUSY
        if status is ManagedInputResultStatus.BUSY
        else None
    )
    active_capture_count = 0 if blocker is not None else None
    latched_inventory = ManagedInputInventorySnapshot(
        process_state,
        process_state is ManagedInputProcessState.RESTART_REQUIRED,
        active_capture_count,
        scan_id(),
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        (),
    )
    if status in (
        ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
        ManagedInputResultStatus.BUSY,
    ):
        errors = ()
    elif status is ManagedInputResultStatus.CAPACITY_REJECTED:
        errors = (error(ManagedInputErrorCode.CAPACITY_REACHED),)
    else:
        errors = (error(ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED),)
    return ManagedInputResult(
        status,
        command_kind,
        process_state,
        blocker,
        latched_inventory,
        None,
        (),
        errors,
    )


@pytest.mark.parametrize(
    ("command_kind", "status"),
    [
        *[
            (command_kind, ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED)
            for command_kind in (
                ManagedInputCommandKind.ADD,
                ManagedInputCommandKind.SELECT_OR_REPLACE,
                ManagedInputCommandKind.REMOVE,
                ManagedInputCommandKind.CALIBRATE,
            )
        ],
        *[
            (command_kind, ManagedInputResultStatus.BUSY)
            for command_kind in (
                ManagedInputCommandKind.ADD,
                ManagedInputCommandKind.SELECT_OR_REPLACE,
                ManagedInputCommandKind.REMOVE,
                ManagedInputCommandKind.CALIBRATE,
            )
        ],
        (
            ManagedInputCommandKind.ADD,
            ManagedInputResultStatus.CAPACITY_REJECTED,
        ),
        *[
            (command_kind, ManagedInputResultStatus.FAILED)
            for command_kind in (
                ManagedInputCommandKind.ADD,
                ManagedInputCommandKind.SELECT_OR_REPLACE,
                ManagedInputCommandKind.REMOVE,
                ManagedInputCommandKind.CALIBRATE,
            )
        ],
    ],
)
def test_persistent_latch_rejects_every_non_rejected_status(
    command_kind,
    status,
):
    with pytest.raises(ValueError):
        persistent_latched_non_rejected_result(command_kind, status)


@pytest.mark.parametrize(
    "error_code",
    [
        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
        ManagedInputErrorCode.INVARIANT_VIOLATION,
    ],
)
def test_latched_refresh_rejection_requires_matching_scan_error(error_code):
    result = latched_result(
        ManagedInputCommandKind.REFRESH,
        latch_code=error_code,
        result_error_code=error_code,
    )
    assert result.errors[0].code is error_code


@pytest.mark.parametrize(
    "command_kind",
    [
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    ],
)
def test_latched_persistent_rejection_requires_matching_scan_error(
    command_kind,
):
    result = latched_result(command_kind)
    assert result.errors[0].code is result.inventory.scan_error_code


def test_latched_refresh_and_persistent_results_reject_incoherent_shapes():
    cases = (
        lambda: latched_result(
            ManagedInputCommandKind.REFRESH,
            status=ManagedInputResultStatus.SUCCEEDED,
            result_error_code=None,
        ),
        lambda: latched_result(
            ManagedInputCommandKind.REFRESH,
            result_error_code=ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        lambda: latched_result(
            ManagedInputCommandKind.REFRESH,
            observations=(observation(),),
        ),
        lambda: latched_result(
            ManagedInputCommandKind.ADD,
            result_error_code=ManagedInputErrorCode.INVARIANT_VIOLATION,
        ),
        lambda: latched_result(
            ManagedInputCommandKind.ADD,
            observations=(observation(),),
        ),
    )
    for build in cases:
        with pytest.raises(ValueError):
            build()


def test_unlatched_refresh_requires_restart_remains_a_valid_rejection():
    result = managed_result(
        ManagedInputResultStatus.REJECTED,
        ManagedInputCommandKind.REFRESH,
        ManagedInputErrorCode.REFRESH_REQUIRES_RESTART,
    )
    assert result.errors[0].code is ManagedInputErrorCode.REFRESH_REQUIRES_RESTART


def test_bootstrap_rejects_duplicate_endpoint_coordinates_across_hardware():
    unique_streams = (ready(1, 3), ready(2, 4))
    unique_rows = (
        row_snapshot(1, "A", ManagedInputAvailability.AVAILABLE, device_index=3),
        row_snapshot(2, "B", ManagedInputAvailability.AVAILABLE, device_index=4),
    )
    result = ManagedInputBootstrapResult(
        ManagedInputProcessState.ACCEPTING_RUNS,
        inventory(rows=unique_rows),
        unique_streams,
        (),
        None,
        (),
    )
    assert result.ready_streams == unique_streams

    with pytest.raises(ValueError):
        duplicate_rows = (
            row_snapshot(
                1, "A", ManagedInputAvailability.AVAILABLE, device_index=3
            ),
            row_snapshot(
                2, "B", ManagedInputAvailability.AVAILABLE, device_index=3
            ),
        )
        ManagedInputBootstrapResult(
            ManagedInputProcessState.ACCEPTING_RUNS,
            inventory(rows=duplicate_rows),
            (ready(1, 3), ready(2, 3)),
            (),
            None,
            (),
        )


def test_bootstrap_sets_exactly_partition_inventory_rows():
    available = row_snapshot(
        1, "A", ManagedInputAvailability.AVAILABLE, device_index=3
    )
    unavailable = row_snapshot(
        2,
        "B",
        ManagedInputAvailability.UNAVAILABLE,
        error_code=ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
    )
    inv = inventory(rows=(available, unavailable))
    coherent = ManagedInputBootstrapResult(
        ManagedInputProcessState.ACCEPTING_RUNS,
        inv,
        (ready(1, 3),),
        (ManagedHardwareId(2),),
        None,
        (),
    )
    assert coherent.unresolved_hardware_ids == (ManagedHardwareId(2),)

    invalid_partitions = (
        ((ready(1, 3),), ()),
        ((ready(1, 3),), (ManagedHardwareId(3),)),
        ((), (ManagedHardwareId(1), ManagedHardwareId(2))),
        ((ready(2, 3),), (ManagedHardwareId(1),)),
    )
    for ready_streams, unresolved_ids in invalid_partitions:
        with pytest.raises(ValueError):
            ManagedInputBootstrapResult(
                ManagedInputProcessState.ACCEPTING_RUNS,
                inv,
                ready_streams,
                unresolved_ids,
                None,
                (),
            )


def test_bootstrap_mapped_pending_row_is_neither_ready_nor_unresolved():
    mapped = row_snapshot(
        1, "A", ManagedInputAvailability.MAPPED_PENDING_OPEN
    )
    bootstrapping_inventory = inventory(
        state=ManagedInputProcessState.BOOTSTRAPPING,
        rows=(mapped,),
    )
    result = ManagedInputBootstrapResult(
        ManagedInputProcessState.BOOTSTRAPPING,
        bootstrapping_inventory,
        (),
        (),
        None,
        (),
    )
    assert result.ready_streams == ()
    assert result.unresolved_hardware_ids == ()


def test_bootstrap_ready_stream_must_equal_inventory_endpoint_and_generation():
    available = row_snapshot(
        1, "A", ManagedInputAvailability.AVAILABLE, device_index=3
    )
    with pytest.raises(ValueError):
        ManagedInputBootstrapResult(
            ManagedInputProcessState.ACCEPTING_RUNS,
            inventory(rows=(available,)),
            (ready(1, 4),),
            (),
            None,
            (),
        )
    mismatched_generation = ResidentReadyStream(
        endpoint(1, 3),
        ResidentStreamGenerationStamp(ManagedHardwareId(1), 2, 1),
    )
    with pytest.raises(ValueError):
        ManagedInputBootstrapResult(
            ManagedInputProcessState.ACCEPTING_RUNS,
            inventory(rows=(available,)),
            (mismatched_generation,),
            (),
            None,
            (),
        )


def busy_result(
    blocker: ManagedInputBlocker,
    blocker_inventory: ManagedInputInventorySnapshot | None,
) -> ManagedInputResult:
    return ManagedInputResult(
        ManagedInputResultStatus.BUSY,
        ManagedInputCommandKind.ADD,
        ManagedInputProcessState.ACCEPTING_RUNS,
        blocker,
        blocker_inventory,
        None,
        (),
        (),
    )


def test_busy_blockers_accept_only_their_exact_inventory_shapes():
    assert (
        busy_result(ManagedInputBlocker.MAINTENANCE_IN_PROGRESS, None).inventory
        is None
    )
    assert (
        busy_result(ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN, None).inventory
        is None
    )
    unknown_inventory = inventory(active_capture_count=None)
    assert (
        busy_result(
            ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN, unknown_inventory
        ).inventory
        is unknown_inventory
    )
    active_inventory = inventory(active_capture_count=2)
    assert (
        busy_result(
            ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS, active_inventory
        ).inventory
        is active_inventory
    )
    idle_inventory = inventory(active_capture_count=0)
    assert (
        busy_result(
            ManagedInputBlocker.TARGET_RESOURCE_BUSY, idle_inventory
        ).inventory
        is idle_inventory
    )


@pytest.mark.parametrize(
    ("blocker", "blocker_inventory"),
    [
        (ManagedInputBlocker.MAINTENANCE_IN_PROGRESS, inventory()),
        (
            ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN,
            inventory(active_capture_count=0),
        ),
        (ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS, None),
        (
            ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS,
            inventory(active_capture_count=0),
        ),
        (ManagedInputBlocker.TARGET_RESOURCE_BUSY, None),
        (
            ManagedInputBlocker.TARGET_RESOURCE_BUSY,
            inventory(active_capture_count=1),
        ),
    ],
)
def test_busy_blockers_reject_other_inventory_shapes(
    blocker,
    blocker_inventory,
):
    with pytest.raises(ValueError):
        busy_result(blocker, blocker_inventory)


@pytest.mark.parametrize(
    "command_kind",
    [
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    ],
)
def test_persistent_command_kinds_reject_plain_succeeded(command_kind):
    with pytest.raises(ValueError):
        ManagedInputResult(
            ManagedInputResultStatus.SUCCEEDED,
            command_kind,
            ManagedInputProcessState.ACCEPTING_RUNS,
            None,
            inventory(),
            None,
            (),
            (),
        )


@pytest.mark.parametrize(
    "command_kind",
    [
        ManagedInputCommandKind.ADD,
        ManagedInputCommandKind.SELECT_OR_REPLACE,
        ManagedInputCommandKind.REMOVE,
        ManagedInputCommandKind.CALIBRATE,
    ],
)
def test_restart_required_success_accepts_persistent_kinds(command_kind):
    restart_inventory = inventory(
        state=ManagedInputProcessState.RESTART_REQUIRED
    )
    result = ManagedInputResult(
        ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
        command_kind,
        ManagedInputProcessState.RESTART_REQUIRED,
        None,
        restart_inventory,
        None,
        (),
        (),
    )
    assert result.command_kind is command_kind


def test_restart_required_success_rejects_refresh():
    restart_inventory = inventory(
        state=ManagedInputProcessState.RESTART_REQUIRED
    )
    with pytest.raises(ValueError):
        ManagedInputResult(
            ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
            ManagedInputCommandKind.REFRESH,
            ManagedInputProcessState.RESTART_REQUIRED,
            None,
            restart_inventory,
            None,
            (),
            (),
        )


def test_timeout_accepts_exact_bounds_only():
    validate_shutdown_timeout(0.0000001)
    validate_shutdown_timeout(0.000001)
    validate_shutdown_timeout(60.0)
    for invalid in (True, 0.0, 60.000001, float("nan"), float("inf"), 1):
        with pytest.raises(ValueError):
            validate_shutdown_timeout(invalid)


def test_shutdown_values_enforce_clean_blocked_and_dirty_shapes():
    closed = ManagedInputCloseResult(
        ManagedHardwareId(1), ManagedInputCloseStatus.CLOSED, None
    )
    clean = ManagedInputShutdownReport(
        True, ManagedInputProcessState.CLOSED, None, (closed,), None
    )
    assert clean.clean is True
    zero_stream_clean = ManagedInputShutdownReport(
        True, ManagedInputProcessState.CLOSED, None, (), None
    )
    assert zero_stream_clean.close_results == ()
    blocked = ManagedInputShutdownReport(
        False,
        ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
        ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS,
        (),
        None,
    )
    assert blocked.blocker is ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS
    timeout = ManagedInputShutdownReport(
        False,
        ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
        None,
        (),
        error(ManagedInputErrorCode.SHUTDOWN_TIMEOUT),
    )
    assert timeout.error.code is ManagedInputErrorCode.SHUTDOWN_TIMEOUT
    failed = ManagedInputCloseResult(
        ManagedHardwareId(2),
        ManagedInputCloseStatus.FAILED,
        error(ManagedInputErrorCode.INVARIANT_VIOLATION),
    )
    dirty = ManagedInputShutdownReport(
        False,
        ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
        None,
        (closed, failed),
        None,
    )
    assert dirty.close_results == (closed, failed)
    with pytest.raises(ValueError):
        ManagedInputCloseResult(
            ManagedHardwareId(1), ManagedInputCloseStatus.FAILED, None
        )
    with pytest.raises(ValueError):
        ManagedInputShutdownReport(
            True,
            ManagedInputProcessState.CLOSED,
            None,
            (
                ManagedInputCloseResult(
                    ManagedHardwareId(1),
                    ManagedInputCloseStatus.TIMED_OUT,
                    error(ManagedInputErrorCode.SHUTDOWN_TIMEOUT),
                ),
            ),
            None,
        )
    with pytest.raises(ValueError):
        ManagedInputShutdownReport(
            False,
            ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
            None,
            (),
            None,
        )
    invalid_empty_partition = ManagedInputShutdownReport(
        False,
        ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
        None,
        (),
        ManagedInputError(
            ManagedInputErrorCode.INVARIANT_VIOLATION,
            "resident_close",
            "Resident returned an invalid close partition.",
        ),
    )
    assert invalid_empty_partition.error.code is (
        ManagedInputErrorCode.INVARIANT_VIOLATION
    )
    with pytest.raises(ValueError):
        ManagedInputShutdownReport(
            False,
            ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
            None,
            (),
            error(ManagedInputErrorCode.CALIBRATION_INVALID),
        )
    not_open = ManagedInputCloseResult(
        ManagedHardwareId(2), ManagedInputCloseStatus.NOT_OPEN, None
    )
    with pytest.raises(ValueError):
        ManagedInputShutdownReport(
            False,
            ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
            None,
            (closed, not_open),
            None,
        )


def test_contracts_have_no_non_mvp_delivery_recovery_or_playback_fields():
    forbidden = {
        "delivery_key",
        "hardware_delivery_key",
        "command_digest",
        "capability",
        "sequence_hwm",
        "operation_id",
        "playback_key",
        "epoch",
    }
    public_values = (
        AddManagedInputCommand,
        SelectOrReplaceManagedInputCommand,
        RemoveManagedInputCommand,
        RefreshManagedInputsCommand,
        CalibrateManagedInputCommand,
        MapStartupEndpointCommand,
        ManagedInputResult,
    )
    for value_type in public_values:
        assert forbidden.isdisjoint(field.name for field in fields(value_type))
