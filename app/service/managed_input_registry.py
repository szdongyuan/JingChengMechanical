from __future__ import annotations

from dataclasses import dataclass, replace
import math
import threading
import unicodedata
from typing import Callable

from app.service.managed_input_contracts import (
    MAX_CALIBRATABLE_INPUT_CHANNELS,
    MAX_MANAGED_AUDIO_INPUTS,
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
    ManagedInputDiscoveryBoundary,
    ManagedInputError,
    ManagedInputErrorCode,
    ManagedInputInventorySnapshot,
    ManagedInputProcessState,
    ManagedInputReferenceBoundary,
    ManagedInputResult,
    ManagedInputResultStatus,
    ManagedInputSnapshot,
    ManagedInputShutdownReport,
    MapStartupEndpointCommand,
    OfflineMaintenanceBlockerSnapshot,
    OfflineMaintenanceBlockerPort,
    RefreshManagedInputsCommand,
    RemoveManagedInputCommand,
    ResidentManagedInputBoundary,
    ResidentCalibrationMeasurement,
    ResidentInputFailureNotice,
    ResidentOpenFailure,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
    SelectOrReplaceManagedInputCommand,
    StableUidCapability,
    normalize_managed_input_alias,
    parse_calibration_json,
    serialize_calibration_json,
    validate_shutdown_timeout,
)
from consts.time_consts import now_str


@dataclass(frozen=True, slots=True)
class _ManagedRow:
    hardware_id: ManagedHardwareId
    alias: str
    hardware_revision: int
    stable_backend_uid: str | None
    descriptor: InputEndpointDescriptor
    calibration_revision: int
    calibration_channels: tuple[tuple[int, bool, int, float | None], ...]


class ManagedInputRegistry:
    def __init__(
        self,
        *,
        table: type,
        discovery: ManagedInputDiscoveryBoundary,
        blocker: OfflineMaintenanceBlockerPort,
        references: ManagedInputReferenceBoundary,
        resident: ResidentManagedInputBoundary,
        monotonic: Callable[[], float],
        process_instance_id_factory: Callable[[], str],
    ) -> None:
        self._table = table
        self._discovery = discovery
        self._blocker = blocker
        self._references = references
        self._resident = resident
        self._monotonic = monotonic
        self._lock = threading.Lock()

        process_instance_id = process_instance_id_factory()
        BackendScanId(process_instance_id, 1)
        self._process_instance_id = process_instance_id
        self._next_scan_sequence = 1

        self._process_state = ManagedInputProcessState.BOOTSTRAPPING
        self._current_scan: BackendInputScan | None = None
        self._observations: tuple[InputEndpointObservation, ...] = ()
        self._scan_error_code: ManagedInputErrorCode | None = None
        self._startup_stable_uid_capability: StableUidCapability | None = None
        self._startup_owner_map: tuple[
            tuple[ManagedHardwareId, str], ...
        ] = ()
        self._ready_streams: tuple[ResidentReadyStream, ...] = ()
        self._cached_bootstrap_result: ManagedInputBootstrapResult | None = None
        self._finalization_in_progress = False
        self._frozen_startup_hardware_ids: tuple[ManagedHardwareId, ...] = ()
        self._cached_shutdown_report: ManagedInputShutdownReport | None = None

        try:
            self._row_records = self._load_rows()
        except Exception:
            self._row_records = ()
        self._row_snapshots = tuple(
            self._snapshot_from_record(row) for row in self._row_records
        )

    def _load_rows(self) -> tuple[_ManagedRow, ...]:
        orm_rows = tuple(self._table.select())
        if len(orm_rows) > MAX_MANAGED_AUDIO_INPUTS:
            raise ValueError("managed input row count exceeds capacity")
        records = tuple(self._record_from_orm(row) for row in orm_rows)
        aliases = [record.alias for record in records]
        hardware_ids = [record.hardware_id for record in records]
        stable_uids = [
            record.stable_backend_uid
            for record in records
            if record.stable_backend_uid is not None
        ]
        if (
            len(set(aliases)) != len(aliases)
            or len(set(hardware_ids)) != len(hardware_ids)
            or len(set(stable_uids)) != len(stable_uids)
        ):
            raise ValueError("managed input rows are not unique")
        return tuple(
            sorted(records, key=lambda row: (row.alias, row.hardware_id.value))
        )

    @staticmethod
    def _record_from_orm(row: object) -> _ManagedRow:
        hardware_id = ManagedHardwareId(row.hardware_id)
        alias = normalize_managed_input_alias(row.alias)
        if alias != row.alias:
            raise ValueError("stored alias is not canonical")
        if type(row.hardware_revision) is not int or not (
            1 <= row.hardware_revision <= 2**63 - 1
        ):
            raise ValueError("stored hardware revision is invalid")
        stable_uid = row.stable_backend_uid
        if stable_uid is not None:
            if type(stable_uid) is not str:
                raise ValueError("stored stable UID is invalid")
            try:
                stable_uid_size = len(stable_uid.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("stored stable UID is invalid") from exc
            if (
                not 1 <= stable_uid_size <= 256
                or stable_uid != stable_uid.strip()
                or any(unicodedata.category(char) == "Cc" for char in stable_uid)
            ):
                raise ValueError("stored stable UID is invalid")
        descriptor = InputEndpointDescriptor(
            row.descriptor_device_name,
            row.descriptor_hostapi_name,
            row.descriptor_sample_rate_hz,
            row.descriptor_input_channels,
        )
        if type(row.calibration_revision) is not int or not (
            0 <= row.calibration_revision <= 2**63 - 1
        ):
            raise ValueError("stored calibration revision is invalid")
        calibration_channels = parse_calibration_json(
            row.calibration_json,
            expected_channels=descriptor.input_channels,
        )
        return _ManagedRow(
            hardware_id=hardware_id,
            alias=alias,
            hardware_revision=row.hardware_revision,
            stable_backend_uid=stable_uid,
            descriptor=descriptor,
            calibration_revision=row.calibration_revision,
            calibration_channels=calibration_channels,
        )

    @staticmethod
    def _snapshot_from_record(record: _ManagedRow) -> ManagedInputSnapshot:
        return ManagedInputSnapshot(
            hardware_id=record.hardware_id,
            alias=record.alias,
            hardware_revision=record.hardware_revision,
            descriptor=record.descriptor,
            calibration_revision=record.calibration_revision,
            availability=ManagedInputAvailability.UNAVAILABLE,
            endpoint=None,
            resident_generation=None,
            error_code=None,
        )

    def _inventory_locked(
        self,
        active_capture_count: int | None = None,
    ) -> ManagedInputInventorySnapshot:
        return ManagedInputInventorySnapshot(
            process_state=self._process_state,
            restart_required=(
                self._process_state is ManagedInputProcessState.RESTART_REQUIRED
            ),
            active_capture_count=active_capture_count,
            scan_id=(
                None if self._current_scan is None else self._current_scan.scan_id
            ),
            scan_error_code=self._scan_error_code,
            rows=self._row_snapshots,
        )

    def inventory_snapshot(self) -> ManagedInputInventorySnapshot:
        with self._lock:
            return self._inventory_locked()

    def endpoint_snapshot(self) -> tuple[InputEndpointObservation, ...]:
        with self._lock:
            return self._observations

    def _try_lock(self) -> bool:
        return self._lock.acquire(blocking=False)

    def _latch_restart_required_locked(self) -> None:
        self._process_state = ManagedInputProcessState.RESTART_REQUIRED

    def _result_locked(
        self,
        *,
        kind: ManagedInputCommandKind,
        status: ManagedInputResultStatus,
        active_capture_count: int | None = None,
        blocker: ManagedInputBlocker | None = None,
        hardware_snapshot: ManagedInputSnapshot | None = None,
        observations: tuple[InputEndpointObservation, ...] = (),
        errors: tuple[ManagedInputError, ...] = (),
        include_inventory: bool = True,
    ) -> ManagedInputResult:
        return ManagedInputResult(
            status=status,
            command_kind=kind,
            process_state=self._process_state,
            blocker=blocker,
            inventory=(
                self._inventory_locked(active_capture_count)
                if include_inventory
                else None
            ),
            hardware_snapshot=hardware_snapshot,
            observations=observations,
            errors=errors,
        )

    @staticmethod
    def _error(
        code: ManagedInputErrorCode,
        stage: str,
        summary: str,
    ) -> ManagedInputError:
        return ManagedInputError(code, stage, summary)

    def _rejected_locked(
        self,
        kind: ManagedInputCommandKind,
        code: ManagedInputErrorCode,
        stage: str,
        summary: str,
        *,
        active_capture_count: int | None = None,
        hardware_snapshot: ManagedInputSnapshot | None = None,
    ) -> ManagedInputResult:
        return self._result_locked(
            kind=kind,
            status=ManagedInputResultStatus.REJECTED,
            active_capture_count=active_capture_count,
            hardware_snapshot=hardware_snapshot,
            errors=(self._error(code, stage, summary),),
        )

    def _failed_locked(
        self,
        kind: ManagedInputCommandKind,
        code: ManagedInputErrorCode,
        stage: str,
        summary: str,
        *,
        active_capture_count: int | None = None,
        hardware_snapshot: ManagedInputSnapshot | None = None,
    ) -> ManagedInputResult:
        return self._result_locked(
            kind=kind,
            status=ManagedInputResultStatus.FAILED,
            active_capture_count=active_capture_count,
            hardware_snapshot=hardware_snapshot,
            errors=(self._error(code, stage, summary),),
        )

    def _busy_unlocked(
        self,
        kind: ManagedInputCommandKind,
    ) -> ManagedInputResult:
        return ManagedInputResult(
            status=ManagedInputResultStatus.BUSY,
            command_kind=kind,
            process_state=self._process_state,
            blocker=ManagedInputBlocker.MAINTENANCE_IN_PROGRESS,
            inventory=None,
            hardware_snapshot=None,
            observations=(),
            errors=(),
        )

    def _guard_locked(
        self,
        kind: ManagedInputCommandKind,
        hardware_id: ManagedHardwareId | None,
    ) -> ManagedInputResult | int:
        if self._process_state is ManagedInputProcessState.BOOTSTRAPPING:
            return self._rejected_locked(
                kind,
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "command_phase",
                "Management is unavailable while input bootstrap is incomplete.",
            )
        if self._process_state in (
            ManagedInputProcessState.SHUTTING_DOWN,
            ManagedInputProcessState.CLOSED,
            ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
        ):
            return self._rejected_locked(
                kind,
                ManagedInputErrorCode.SHUTTING_DOWN,
                "command_phase",
                "Managed input shutdown has started.",
            )
        persistent = kind is not ManagedInputCommandKind.REFRESH
        if persistent and self._scan_error_code is not None:
            return self._rejected_locked(
                kind,
                self._scan_error_code,
                "scan_validation",
                "The current process has a latched input scan diagnostic.",
            )
        if (
            persistent
            and self._process_state is ManagedInputProcessState.RESTART_REQUIRED
        ):
            return self._rejected_locked(
                kind,
                ManagedInputErrorCode.RESTART_REQUIRED,
                "command_phase",
                "A process restart is required before another persistent command.",
            )

        try:
            blocker_snapshot = self._blocker.snapshot(hardware_id)
        except Exception:
            blocker_snapshot = None
        if type(blocker_snapshot) is not OfflineMaintenanceBlockerSnapshot:
            active_capture_count = None
            blocker = ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN
        elif (
            blocker_snapshot.active_capture_count is None
            or blocker_snapshot.target_resource_owned is None
        ):
            active_capture_count = None
            blocker = ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN
        elif blocker_snapshot.active_capture_count > 0:
            active_capture_count = blocker_snapshot.active_capture_count
            blocker = ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS
        elif blocker_snapshot.target_resource_owned:
            active_capture_count = 0
            blocker = ManagedInputBlocker.TARGET_RESOURCE_BUSY
        else:
            return 0
        return self._result_locked(
            kind=kind,
            status=ManagedInputResultStatus.BUSY,
            active_capture_count=active_capture_count,
            blocker=blocker,
        )

    def _record_locked(
        self,
        hardware_id: ManagedHardwareId,
    ) -> _ManagedRow | None:
        return next(
            (
                record
                for record in self._row_records
                if record.hardware_id == hardware_id
            ),
            None,
        )

    def _snapshot_locked(
        self,
        hardware_id: ManagedHardwareId,
    ) -> ManagedInputSnapshot | None:
        return next(
            (
                snapshot
                for snapshot in self._row_snapshots
                if snapshot.hardware_id == hardware_id
            ),
            None,
        )

    def _selected_observation_locked(
        self,
        *,
        kind: ManagedInputCommandKind,
        scan_id: BackendScanId,
        observation_id: str,
        active_capture_count: int,
    ) -> InputEndpointObservation | ManagedInputResult:
        if self._current_scan is None or scan_id != self._current_scan.scan_id:
            return self._rejected_locked(
                kind,
                ManagedInputErrorCode.STALE_SCAN,
                "scan_selection",
                "The selected scan is not the current published scan.",
                active_capture_count=active_capture_count,
            )
        matches = tuple(
            observation
            for observation in self._observations
            if observation.observation_id == observation_id
        )
        if len(matches) != 1:
            return self._rejected_locked(
                kind,
                ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION,
                "endpoint_selection",
                "The selected endpoint observation is not current.",
                active_capture_count=active_capture_count,
            )
        return matches[0]

    def _publish_records_locked(
        self,
        records: tuple[_ManagedRow, ...],
    ) -> None:
        old_snapshots = {
            snapshot.hardware_id: snapshot for snapshot in self._row_snapshots
        }
        snapshots: list[ManagedInputSnapshot] = []
        for record in records:
            old = old_snapshots.get(record.hardware_id)
            if old is not None and old.descriptor == record.descriptor:
                snapshots.append(
                    replace(
                        old,
                        alias=record.alias,
                        hardware_revision=record.hardware_revision,
                        descriptor=record.descriptor,
                        calibration_revision=record.calibration_revision,
                    )
                )
            else:
                snapshots.append(self._snapshot_from_record(record))
        self._row_records = records
        self._row_snapshots = tuple(snapshots)

    @staticmethod
    def _initial_calibration_json(input_channels: int) -> str:
        return serialize_calibration_json(
            tuple(
                (channel_index, False, 0, None)
                for channel_index in range(input_channels)
            ),
            expected_channels=input_channels,
        )

    def _database_failure_locked(
        self,
        kind: ManagedInputCommandKind,
        *,
        active_capture_count: int,
        hardware_snapshot: ManagedInputSnapshot | None = None,
    ) -> ManagedInputResult:
        return self._failed_locked(
            kind,
            ManagedInputErrorCode.DATABASE_TRANSACTION_FAILED,
            "database_transaction",
            "The managed input database transaction failed.",
            active_capture_count=active_capture_count,
            hardware_snapshot=hardware_snapshot,
        )

    def _latch_scan_error_locked(
        self,
        code: ManagedInputErrorCode,
    ) -> ManagedInputErrorCode:
        if self._scan_error_code is None:
            self._scan_error_code = code
        return self._scan_error_code

    def _refresh_rejection_locked(
        self,
        code: ManagedInputErrorCode,
        summary: str,
        *,
        active_capture_count: int,
    ) -> ManagedInputResult:
        latched = self._latch_scan_error_locked(code)
        return self._rejected_locked(
            ManagedInputCommandKind.REFRESH,
            latched,
            "backend_scan",
            summary,
            active_capture_count=active_capture_count,
        )

    def _validate_refresh_scan_locked(
        self,
        scan: BackendInputScan,
        expected_scan_id: BackendScanId,
    ) -> tuple[ManagedInputErrorCode, str] | None:
        if scan.scan_id != expected_scan_id:
            return (
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "The discovery boundary returned an unexpected scan identity.",
            )
        startup_capability = self._startup_stable_uid_capability
        if (
            startup_capability is not None
            and scan.stable_uid_capability is not startup_capability
        ):
            return (
                ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                "The backend stable UID capability changed in this process.",
            )
        row_has_uids = [
            record.stable_backend_uid is not None for record in self._row_records
        ]
        if (
            scan.stable_uid_capability is StableUidCapability.SUPPORTED
            and any(not has_uid for has_uid in row_has_uids)
        ) or (
            scan.stable_uid_capability is StableUidCapability.NONE
            and any(row_has_uids)
        ):
            return (
                ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                "Managed rows conflict with the backend stable UID declaration.",
            )
        observation_ids = [
            observation.observation_id for observation in scan.observations
        ]
        device_indexes = [
            observation.device_index for observation in scan.observations
        ]
        if (
            len(set(observation_ids)) != len(observation_ids)
            or len(set(device_indexes)) != len(device_indexes)
        ):
            return (
                ManagedInputErrorCode.INVARIANT_VIOLATION,
                "The validated scan contains conflicting endpoint coordinates.",
            )
        observed_uids = [
            observation.stable_backend_uid for observation in scan.observations
        ]
        if scan.stable_uid_capability is StableUidCapability.SUPPORTED:
            if any(uid is None for uid in observed_uids):
                return (
                    ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                    "The scan mixes stable UID presence.",
                )
            non_null_uids = [uid for uid in observed_uids if uid is not None]
            if len(set(non_null_uids)) != len(non_null_uids):
                return (
                    ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                    "The scan contains a duplicate stable UID.",
                )
        elif any(uid is not None for uid in observed_uids):
            return (
                ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                "The scan conflicts with the absent stable UID declaration.",
            )
        return None

    def _bootstrap_result_locked(
        self,
        *,
        errors: tuple[ManagedInputError, ...] = (),
        blocker: ManagedInputBlocker | None = None,
    ) -> ManagedInputBootstrapResult:
        ready_streams = tuple(
            sorted(
                self._ready_streams,
                key=lambda stream: stream.endpoint.hardware_id.value,
            )
        )
        unresolved = tuple(
            sorted(
                (
                    row.hardware_id
                    for row in self._row_snapshots
                    if row.availability
                    in (
                        ManagedInputAvailability.UNAVAILABLE,
                        ManagedInputAvailability.NEEDS_USER_SELECTION,
                    )
                ),
                key=lambda hardware_id: hardware_id.value,
            )
        )
        return ManagedInputBootstrapResult(
            self._process_state,
            self._inventory_locked(),
            ready_streams,
            unresolved,
            blocker,
            errors,
        )

    def _bootstrap_busy_unlocked(self) -> ManagedInputBootstrapResult:
        current = self._cached_bootstrap_result
        if current is None:
            inventory = ManagedInputInventorySnapshot(
                self._process_state,
                False,
                None,
                None,
                self._scan_error_code,
                self._row_snapshots,
            )
            current = ManagedInputBootstrapResult(
                self._process_state,
                inventory,
                (),
                tuple(
                    row.hardware_id
                    for row in self._row_snapshots
                    if row.availability
                    in (
                        ManagedInputAvailability.UNAVAILABLE,
                        ManagedInputAvailability.NEEDS_USER_SELECTION,
                    )
                ),
                None,
                (),
            )
        return replace(
            current,
            blocker=ManagedInputBlocker.MAINTENANCE_IN_PROGRESS,
            errors=(),
        )

    def _fail_bootstrap_locked(
        self,
        code: ManagedInputErrorCode,
        summary: str,
    ) -> ManagedInputBootstrapResult:
        self._process_state = ManagedInputProcessState.BOOTSTRAPPING
        self._current_scan = None
        self._observations = ()
        self._scan_error_code = code
        self._startup_stable_uid_capability = None
        self._startup_owner_map = ()
        self._ready_streams = ()
        self._row_snapshots = tuple(
            replace(
                snapshot,
                availability=ManagedInputAvailability.UNAVAILABLE,
                endpoint=None,
                resident_generation=None,
                error_code=None,
            )
            for snapshot in self._row_snapshots
        )
        result = self._bootstrap_result_locked(
            errors=(self._error(code, "backend_scan", summary),)
        )
        self._cached_bootstrap_result = result
        return result

    @staticmethod
    def _endpoint_from_observation(
        hardware_id: ManagedHardwareId,
        item: InputEndpointObservation,
    ) -> HardwareEndpointIdentity:
        return HardwareEndpointIdentity(
            hardware_id,
            item.scan_id,
            item.device_index,
            item.hostapi_index,
            item.stable_backend_uid,
            item.descriptor.device_name,
            item.descriptor.hostapi_name,
            item.descriptor.sample_rate_hz,
            item.descriptor.input_channels,
        )

    def bootstrap(self) -> ManagedInputBootstrapResult:
        with self._lock:
            if self._cached_bootstrap_result is not None:
                return self._cached_bootstrap_result

            try:
                records = self._load_rows()
            except Exception:
                self._row_records = ()
                self._row_snapshots = ()
                return self._fail_bootstrap_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The managed input row set is malformed or exceeds capacity.",
                )
            self._row_records = records
            self._row_snapshots = tuple(
                self._snapshot_from_record(record) for record in records
            )

            scan_id = BackendScanId(
                self._process_instance_id,
                self._next_scan_sequence,
            )
            try:
                discovery_result = self._discovery.discover(scan_id)
            except Exception:
                discovery_result = None
            if type(discovery_result) is not BackendDiscoveryResult:
                return self._fail_bootstrap_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The discovery boundary returned an invalid result.",
                )
            if discovery_result.error is not None:
                return self._fail_bootstrap_locked(
                    discovery_result.error.code,
                    discovery_result.error.summary,
                )
            scan = discovery_result.scan
            if scan is None:
                return self._fail_bootstrap_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The discovery boundary returned no scan.",
                )
            validation_error = self._validate_refresh_scan_locked(scan, scan_id)
            if validation_error is not None:
                return self._fail_bootstrap_locked(*validation_error)

            self._current_scan = scan
            self._observations = scan.observations
            self._startup_stable_uid_capability = scan.stable_uid_capability
            self._next_scan_sequence += 1

            owner_map: list[tuple[ManagedHardwareId, str]] = []
            snapshots: list[ManagedInputSnapshot] = []
            errors: list[ManagedInputError] = []
            for record in records:
                candidates = tuple(
                    item
                    for item in scan.observations
                    if (
                        item.stable_backend_uid == record.stable_backend_uid
                        if record.stable_backend_uid is not None
                        else item.descriptor == record.descriptor
                    )
                )
                if record.stable_backend_uid is None:
                    if candidates:
                        availability = ManagedInputAvailability.NEEDS_USER_SELECTION
                        code = ManagedInputErrorCode.ENDPOINT_NEEDS_USER_SELECTION
                        summary = "The endpoint requires explicit startup selection."
                    else:
                        availability = ManagedInputAvailability.UNAVAILABLE
                        code = ManagedInputErrorCode.ENDPOINT_UNAVAILABLE
                        summary = "No matching input endpoint is available."
                elif not candidates:
                    availability = ManagedInputAvailability.UNAVAILABLE
                    code = ManagedInputErrorCode.ENDPOINT_UNAVAILABLE
                    summary = "No matching input endpoint is available."
                elif len(candidates) != 1:
                    return self._fail_bootstrap_locked(
                        ManagedInputErrorCode.BACKEND_IDENTITY_CONFLICT,
                        "A stable UID resolved to more than one endpoint.",
                    )
                elif candidates[0].descriptor != record.descriptor:
                    availability = ManagedInputAvailability.UNAVAILABLE
                    code = ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED
                    summary = "The stored endpoint descriptor no longer matches."
                else:
                    availability = ManagedInputAvailability.MAPPED_PENDING_OPEN
                    code = None
                    summary = ""
                    owner_map.append(
                        (record.hardware_id, candidates[0].observation_id)
                    )
                snapshots.append(
                    replace(
                        self._snapshot_from_record(record),
                        availability=availability,
                        error_code=code,
                    )
                )
                if code is not None:
                    errors.append(
                        self._error(code, "endpoint_resolution", summary)
                    )
            self._startup_owner_map = tuple(owner_map)
            self._row_snapshots = tuple(snapshots)
            result = self._bootstrap_result_locked(errors=tuple(errors))
            self._cached_bootstrap_result = result
            return result

    def map_startup_endpoint(
        self,
        command: MapStartupEndpointCommand,
    ) -> ManagedInputBootstrapResult:
        if not self._try_lock():
            return self._bootstrap_busy_unlocked()
        try:
            if self._finalization_in_progress:
                return self._bootstrap_busy_unlocked()
            if self._cached_bootstrap_result is None:
                return self._bootstrap_result_locked(
                    errors=(
                        self._error(
                            ManagedInputErrorCode.INVARIANT_VIOLATION,
                            "command_phase",
                            "Input bootstrap has not started.",
                        ),
                    )
                )
            if (
                self._process_state is not ManagedInputProcessState.BOOTSTRAPPING
                or self._current_scan is None
                or self._scan_error_code is not None
            ):
                assert self._cached_bootstrap_result is not None
                return self._cached_bootstrap_result
            record = self._record_locked(command.hardware_id)
            if record is None:
                code = ManagedInputErrorCode.HARDWARE_NOT_FOUND
                summary = "The managed input does not exist."
            elif record.hardware_revision != command.expected_hardware_revision:
                code = ManagedInputErrorCode.STALE_HARDWARE_REVISION
                summary = "The managed input revision is stale."
            elif record.stable_backend_uid is not None:
                code = ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED
                summary = "Stable-UID rows cannot be explicitly mapped."
            elif command.scan_id != self._current_scan.scan_id:
                code = ManagedInputErrorCode.STALE_SCAN
                summary = "The selected startup scan is stale."
            else:
                selected = next(
                    (
                        item
                        for item in self._observations
                        if item.observation_id == command.observation_id
                    ),
                    None,
                )
                if selected is None:
                    code = ManagedInputErrorCode.STALE_ENDPOINT_OBSERVATION
                    summary = "The selected endpoint observation is stale."
                elif any(
                    owner != command.hardware_id
                    and observation_id == command.observation_id
                    for owner, observation_id in self._startup_owner_map
                ):
                    code = ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND
                    summary = "The selected endpoint is already mapped."
                elif (
                    selected.stable_backend_uid is not None
                    or selected.descriptor != record.descriptor
                ):
                    code = ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED
                    summary = "The selected endpoint does not match the managed row."
                else:
                    self._startup_owner_map = tuple(
                        (owner, observation_id)
                        for owner, observation_id in self._startup_owner_map
                        if owner != command.hardware_id
                    ) + ((command.hardware_id, command.observation_id),)
                    self._row_snapshots = tuple(
                        replace(
                            snapshot,
                            availability=ManagedInputAvailability.MAPPED_PENDING_OPEN,
                            endpoint=None,
                            resident_generation=None,
                            error_code=None,
                        )
                        if snapshot.hardware_id == command.hardware_id
                        else snapshot
                        for snapshot in self._row_snapshots
                    )
                    result = self._bootstrap_result_locked()
                    self._cached_bootstrap_result = result
                    return result
            result = self._bootstrap_result_locked(
                errors=(self._error(code, "startup_mapping", summary),)
            )
            return result
        finally:
            self._lock.release()

    def finalize_bootstrap(self) -> ManagedInputBootstrapResult:
        if not self._try_lock():
            return self._bootstrap_busy_unlocked()
        try:
            if self._finalization_in_progress:
                return self._bootstrap_busy_unlocked()
            if self._cached_bootstrap_result is None:
                return self._bootstrap_result_locked(
                    errors=(
                        self._error(
                            ManagedInputErrorCode.INVARIANT_VIOLATION,
                            "command_phase",
                            "Input bootstrap has not started.",
                        ),
                    )
                )
            assert self._cached_bootstrap_result is not None
            if (
                self._process_state
                is not ManagedInputProcessState.BOOTSTRAPPING
                or self._scan_error_code is not None
            ):
                return self._cached_bootstrap_result
            if self._current_scan is None:
                return self._cached_bootstrap_result
            observations = {
                item.observation_id: item for item in self._observations
            }
            frozen_mapping = tuple(
                sorted(
                    (
                        (hardware_id, observations[observation_id])
                        for hardware_id, observation_id
                        in self._startup_owner_map
                    ),
                    key=lambda pair: pair[0].value,
                )
            )
            frozen_ids = tuple(
                sorted(
                    (record.hardware_id for record in self._row_records),
                    key=lambda hardware_id: hardware_id.value,
                )
            )
            self._finalization_in_progress = True
        finally:
            self._lock.release()

        endpoints: list[HardwareEndpointIdentity] = []
        preflight_failed: set[ManagedHardwareId] = set()
        for hardware_id, selected in frozen_mapping:
            try:
                current = self._discovery.requery_validated_input_endpoint(
                    selected.scan_id,
                    selected.device_index,
                )
            except Exception:
                current = None
            if type(current) is not InputEndpointObservation or current != selected:
                preflight_failed.add(hardware_id)
            else:
                endpoints.append(
                    self._endpoint_from_observation(hardware_id, selected)
                )
        endpoint_tuple = tuple(endpoints)
        try:
            resident_result = self._resident.open_startup_streams(endpoint_tuple)
        except Exception:
            resident_result = None

        self._lock.acquire()
        try:
            submitted = {
                endpoint.hardware_id: endpoint for endpoint in endpoint_tuple
            }
            self._frozen_startup_hardware_ids = frozen_ids
            partition_valid = False
            ready_by_id: dict[ManagedHardwareId, ResidentReadyStream] = {}
            failed_ids: set[ManagedHardwareId] = set()
            if type(resident_result) is ResidentStartupOpenResult:
                try:
                    ready_streams = resident_result.ready_streams
                    failures = resident_result.failures
                except AttributeError:
                    ready_streams = None
                    failures = None
                exact_items = (
                    type(ready_streams) is tuple
                    and type(failures) is tuple
                    and all(
                        type(stream) is ResidentReadyStream
                        for stream in ready_streams
                    )
                    and all(
                        type(failure) is ResidentOpenFailure
                        for failure in failures
                    )
                )
                if exact_items:
                    ready_ids = [
                        stream.endpoint.hardware_id
                        for stream in ready_streams
                    ]
                    failure_ids = [
                        failure.hardware_id for failure in failures
                    ]
                    all_ids = ready_ids + failure_ids
                    ready_by_id = {
                        stream.endpoint.hardware_id: stream
                        for stream in ready_streams
                    }
                    failed_ids = set(failure_ids)
                    partition_valid = (
                        len(all_ids) == len(set(all_ids))
                        and set(all_ids) == set(submitted)
                        and all(
                            stream.endpoint
                            == submitted.get(stream.endpoint.hardware_id)
                            and type(stream.generation)
                            is ResidentStreamGenerationStamp
                            and stream.generation.hardware_id
                            == stream.endpoint.hardware_id
                            for stream in ready_streams
                        )
                    )
            if not partition_valid:
                self._process_state = ManagedInputProcessState.BOOTSTRAPPING
                self._current_scan = None
                self._observations = ()
                self._ready_streams = ()
                self._row_snapshots = tuple(
                    replace(
                        snapshot,
                        availability=ManagedInputAvailability.UNAVAILABLE,
                        endpoint=None,
                        resident_generation=None,
                        error_code=None,
                    )
                    for snapshot in self._row_snapshots
                )
                result = self._bootstrap_result_locked(
                    errors=(
                        self._error(
                            ManagedInputErrorCode.INVARIANT_VIOLATION,
                            "resident_open",
                            "Resident returned an invalid startup open partition.",
                        ),
                    )
                )
                self._cached_bootstrap_result = result
                return result

            snapshots: list[ManagedInputSnapshot] = []
            errors: list[ManagedInputError] = []
            for snapshot in self._row_snapshots:
                hardware_id = snapshot.hardware_id
                if hardware_id in ready_by_id:
                    stream = ready_by_id[hardware_id]
                    snapshots.append(
                        replace(
                            snapshot,
                            availability=ManagedInputAvailability.AVAILABLE,
                            endpoint=stream.endpoint,
                            resident_generation=stream.generation,
                            error_code=None,
                        )
                    )
                elif hardware_id in preflight_failed:
                    snapshots.append(
                        replace(
                            snapshot,
                            availability=ManagedInputAvailability.UNAVAILABLE,
                            endpoint=None,
                            resident_generation=None,
                            error_code=ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED,
                        )
                    )
                    errors.append(
                        self._error(
                            ManagedInputErrorCode.ENDPOINT_VALIDATION_FAILED,
                            "endpoint_preflight",
                            "The selected endpoint changed before opening.",
                        )
                    )
                elif hardware_id in failed_ids:
                    snapshots.append(
                        replace(
                            snapshot,
                            availability=ManagedInputAvailability.UNAVAILABLE,
                            endpoint=None,
                            resident_generation=None,
                            error_code=ManagedInputErrorCode.RESIDENT_OPEN_FAILED,
                        )
                    )
                    errors.append(
                        self._error(
                            ManagedInputErrorCode.RESIDENT_OPEN_FAILED,
                            "resident_open",
                            "Resident could not open the managed input stream.",
                        )
                    )
                else:
                    snapshots.append(snapshot)
            self._row_snapshots = tuple(snapshots)
            self._ready_streams = tuple(
                sorted(
                    ready_by_id.values(),
                    key=lambda stream: stream.endpoint.hardware_id.value,
                )
            )
            self._process_state = ManagedInputProcessState.ACCEPTING_RUNS
            result = self._bootstrap_result_locked(errors=tuple(errors))
            self._cached_bootstrap_result = result
            return result
        finally:
            self._finalization_in_progress = False
            self._lock.release()

    @staticmethod
    def _projection_error(
        code: ManagedInputErrorCode,
        stage: str,
        summary: str,
    ) -> FrozenRunProjectionResult:
        return FrozenRunProjectionResult(
            None,
            ManagedInputError(code, stage, summary),
        )

    def frozen_run_projection(
        self,
        hardware_id: ManagedHardwareId,
        referenced_channels: tuple[int, ...],
    ) -> FrozenRunProjectionResult:
        with self._lock:
            if type(hardware_id) is not ManagedHardwareId:
                return self._projection_error(
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "run_projection",
                    "The managed hardware identity is invalid.",
                )
            record = self._record_locked(hardware_id)
            snapshot = self._snapshot_locked(hardware_id)
            if record is None or snapshot is None:
                return self._projection_error(
                    ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                    "hardware_lookup",
                    "The managed input does not exist.",
                )
            if self._process_state in (
                ManagedInputProcessState.SHUTTING_DOWN,
                ManagedInputProcessState.CLOSED,
                ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
            ):
                return self._projection_error(
                    ManagedInputErrorCode.SHUTTING_DOWN,
                    "run_projection",
                    "Managed input shutdown has started.",
                )
            if self._process_state is ManagedInputProcessState.RESTART_REQUIRED:
                return self._projection_error(
                    ManagedInputErrorCode.RESTART_REQUIRED,
                    "run_projection",
                    "A process restart is required before starting a run.",
                )
            if (
                self._process_state
                is not ManagedInputProcessState.ACCEPTING_RUNS
                or snapshot.availability
                is not ManagedInputAvailability.AVAILABLE
                or snapshot.endpoint is None
                or snapshot.resident_generation is None
            ):
                return self._projection_error(
                    ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
                    "run_projection",
                    "The managed input endpoint is unavailable.",
                )
            if (
                type(referenced_channels) is not tuple
                or not referenced_channels
                or any(type(index) is not int for index in referenced_channels)
                or tuple(sorted(set(referenced_channels)))
                != referenced_channels
                or referenced_channels[0] < 0
                or referenced_channels[-1] >= record.descriptor.input_channels
            ):
                return self._projection_error(
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "run_projection",
                    "The referenced input channels are invalid.",
                )
            channels_by_index = {
                channel[0]: channel for channel in record.calibration_channels
            }
            selected_channels = tuple(
                channels_by_index.get(index) for index in referenced_channels
            )
            if any(
                channel is None
                or not channel[1]
                or channel[3] is None
                for channel in selected_channels
            ):
                return self._projection_error(
                    ManagedInputErrorCode.CALIBRATION_REQUIRED,
                    "run_projection",
                    "Every referenced input channel must be calibrated.",
                )
            projection = FrozenRunInputProjection(
                FrozenRunHardwareSnapshot(
                    record.hardware_id,
                    record.hardware_revision,
                    record.alias,
                    snapshot.endpoint,
                    record.calibration_revision,
                ),
                FrozenRunCalibrationSnapshot(
                    record.hardware_id,
                    record.hardware_revision,
                    record.calibration_revision,
                    record.descriptor.sample_rate_hz,
                    record.descriptor.input_channels,
                    tuple(
                        (channel[0], channel[2], channel[3])
                        for channel in selected_channels
                    ),
                ),
            )
            return FrozenRunProjectionResult(projection, None)

    def report_stream_failure(
        self,
        notice: ResidentInputFailureNotice,
    ) -> ManagedInputSnapshot:
        if type(notice) is not ResidentInputFailureNotice:
            raise ValueError("notice must be an exact ResidentInputFailureNotice")
        with self._lock:
            hardware_id = notice.generation.hardware_id
            snapshot = self._snapshot_locked(hardware_id)
            if snapshot is None:
                raise ValueError("failure notice hardware is not managed")
            if (
                snapshot.availability is not ManagedInputAvailability.AVAILABLE
                or snapshot.resident_generation != notice.generation
            ):
                return snapshot
            failed = replace(
                snapshot,
                availability=ManagedInputAvailability.UNAVAILABLE,
                endpoint=None,
                resident_generation=None,
                error_code=ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
            )
            self._row_snapshots = tuple(
                failed if row.hardware_id == hardware_id else row
                for row in self._row_snapshots
            )
            self._ready_streams = tuple(
                stream
                for stream in self._ready_streams
                if stream.endpoint.hardware_id != hardware_id
            )
            if self._cached_bootstrap_result is not None:
                self._cached_bootstrap_result = self._bootstrap_result_locked()
            return failed

    @staticmethod
    def _blocked_shutdown_report(
        blocker: ManagedInputBlocker,
    ) -> ManagedInputShutdownReport:
        return ManagedInputShutdownReport(
            False,
            ManagedInputProcessState.SHUTDOWN_INCOMPLETE,
            blocker,
            (),
            None,
        )

    def shutdown(self, timeout_seconds: float) -> ManagedInputShutdownReport:
        validate_shutdown_timeout(timeout_seconds)
        cached = self._cached_shutdown_report
        if cached is not None:
            return cached
        started = self._monotonic()
        deadline = started + timeout_seconds
        if not self._try_lock():
            return self._blocked_shutdown_report(
                ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
            )
        try:
            if self._finalization_in_progress:
                return self._blocked_shutdown_report(
                    ManagedInputBlocker.MAINTENANCE_IN_PROGRESS
                )
            if self._cached_shutdown_report is not None:
                return self._cached_shutdown_report
            try:
                blocker_snapshot = self._blocker.snapshot(None)
            except Exception:
                blocker_snapshot = None
            if (
                type(blocker_snapshot) is not OfflineMaintenanceBlockerSnapshot
                or blocker_snapshot.active_capture_count is None
                or blocker_snapshot.target_resource_owned is None
            ):
                return self._blocked_shutdown_report(
                    ManagedInputBlocker.OWNERSHIP_STATE_UNKNOWN
                )
            if blocker_snapshot.active_capture_count > 0:
                return self._blocked_shutdown_report(
                    ManagedInputBlocker.ACTIVE_CAPTURE_EXISTS
                )

            self._process_state = ManagedInputProcessState.SHUTTING_DOWN
            remaining = deadline - self._monotonic()
            if remaining <= 0.0:
                self._process_state = ManagedInputProcessState.SHUTDOWN_INCOMPLETE
                return ManagedInputShutdownReport(
                    False,
                    self._process_state,
                    None,
                    (),
                    self._error(
                        ManagedInputErrorCode.SHUTDOWN_TIMEOUT,
                        "shutdown_deadline",
                        "The shutdown deadline expired before Resident close.",
                    ),
                )

            expected_ids = self._frozen_startup_hardware_ids
            try:
                raw_close_results = self._resident.close_all(remaining)
            except Exception:
                raw_close_results = None
            valid_partition = (
                type(raw_close_results) is tuple
                and len(raw_close_results) == len(expected_ids)
                and all(
                    type(item) is ManagedInputCloseResult
                    for item in raw_close_results
                )
                and tuple(
                    sorted(
                        (item.hardware_id for item in raw_close_results),
                        key=lambda hardware_id: hardware_id.value,
                    )
                )
                == expected_ids
            )
            if valid_partition:
                close_results = tuple(
                    sorted(
                        raw_close_results,
                        key=lambda item: item.hardware_id.value,
                    )
                )
            else:
                partition_error = self._error(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "resident_close",
                    "Resident returned an invalid managed input close partition.",
                )
                close_results = tuple(
                    ManagedInputCloseResult(
                        hardware_id,
                        ManagedInputCloseStatus.FAILED,
                        partition_error,
                    )
                    for hardware_id in expected_ids
                )
            clean = valid_partition and all(
                item.status
                in (
                    ManagedInputCloseStatus.CLOSED,
                    ManagedInputCloseStatus.NOT_OPEN,
                )
                for item in close_results
            )
            self._process_state = (
                ManagedInputProcessState.CLOSED
                if clean
                else ManagedInputProcessState.SHUTDOWN_INCOMPLETE
            )
            report_error = None if valid_partition else partition_error
            if not clean and report_error is None:
                report_error = next(
                    (
                        item.error
                        for item in close_results
                        if item.error is not None
                    ),
                    None,
                )
            report = ManagedInputShutdownReport(
                clean,
                self._process_state,
                None,
                close_results,
                report_error,
            )
            self._cached_shutdown_report = report
            return report
        finally:
            self._lock.release()

    def add(self, command: AddManagedInputCommand) -> ManagedInputResult:
        kind = ManagedInputCommandKind.ADD
        if not self._try_lock():
            return self._busy_unlocked(kind)
        try:
            guarded = self._guard_locked(kind, None)
            if type(guarded) is ManagedInputResult:
                return guarded
            active_capture_count = guarded
            alias = normalize_managed_input_alias(command.alias)
            if any(record.alias == alias for record in self._row_records):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.ALIAS_ALREADY_EXISTS,
                    "alias_validation",
                    "A managed input already uses that normalized alias.",
                    active_capture_count=active_capture_count,
                )
            selected = self._selected_observation_locked(
                kind=kind,
                scan_id=command.scan_id,
                observation_id=command.observation_id,
                active_capture_count=active_capture_count,
            )
            if type(selected) is ManagedInputResult:
                return selected
            if (
                selected.descriptor.input_channels
                > MAX_CALIBRATABLE_INPUT_CHANNELS
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "calibration_schema",
                    "The endpoint channel count cannot be represented by "
                    "canonical calibration JSON.",
                    active_capture_count=active_capture_count,
                )
            try:
                with self._table._meta.database.atomic():
                    if self._table.select().count() >= MAX_MANAGED_AUDIO_INPUTS:
                        capacity_reached = True
                        created = None
                    else:
                        capacity_reached = False
                        if (
                            selected.stable_backend_uid is not None
                            and self._table.select()
                            .where(
                                self._table.stable_backend_uid
                                == selected.stable_backend_uid
                            )
                            .exists()
                        ):
                            return self._rejected_locked(
                                kind,
                                ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND,
                                "endpoint_ownership",
                                "The selected stable endpoint is already managed.",
                                active_capture_count=active_capture_count,
                            )
                        created = self._table.create(
                            alias=alias,
                            hardware_revision=1,
                            stable_backend_uid=selected.stable_backend_uid,
                            descriptor_device_name=selected.descriptor.device_name,
                            descriptor_hostapi_name=selected.descriptor.hostapi_name,
                            descriptor_sample_rate_hz=(
                                selected.descriptor.sample_rate_hz
                            ),
                            descriptor_input_channels=(
                                selected.descriptor.input_channels
                            ),
                            calibration_revision=0,
                            calibration_json=self._initial_calibration_json(
                                selected.descriptor.input_channels
                            ),
                        )
                    records = (
                        self._row_records
                        if capacity_reached
                        else self._load_rows()
                    )
            except Exception:
                return self._database_failure_locked(
                    kind,
                    active_capture_count=active_capture_count,
                )
            if capacity_reached:
                return self._result_locked(
                    kind=kind,
                    status=ManagedInputResultStatus.CAPACITY_REJECTED,
                    active_capture_count=active_capture_count,
                    errors=(
                        self._error(
                            ManagedInputErrorCode.CAPACITY_REACHED,
                            "capacity",
                            "At most six managed audio inputs are permitted.",
                        ),
                    ),
                )
            self._publish_records_locked(records)
            created_hardware_id = ManagedHardwareId(created.hardware_id)
            hardware_snapshot = self._snapshot_locked(created_hardware_id)
            self._latch_restart_required_locked()
            return self._result_locked(
                kind=kind,
                status=ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
                active_capture_count=active_capture_count,
                hardware_snapshot=hardware_snapshot,
                observations=(selected,),
            )
        finally:
            self._lock.release()

    def select_or_replace(
        self,
        command: SelectOrReplaceManagedInputCommand,
    ) -> ManagedInputResult:
        kind = ManagedInputCommandKind.SELECT_OR_REPLACE
        if not self._try_lock():
            return self._busy_unlocked(kind)
        try:
            guarded = self._guard_locked(kind, command.hardware_id)
            if type(guarded) is ManagedInputResult:
                return guarded
            active_capture_count = guarded
            current = self._record_locked(command.hardware_id)
            current_snapshot = self._snapshot_locked(command.hardware_id)
            if current is None:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                    "hardware_lookup",
                    "The managed input does not exist.",
                    active_capture_count=active_capture_count,
                )
            if current.hardware_revision != command.expected_hardware_revision:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                    "hardware_revision",
                    "The managed input revision is stale.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            selected = self._selected_observation_locked(
                kind=kind,
                scan_id=command.scan_id,
                observation_id=command.observation_id,
                active_capture_count=active_capture_count,
            )
            if type(selected) is ManagedInputResult:
                return selected
            if current.hardware_revision >= SIGNED63_MAX:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "hardware_revision",
                    "The managed input hardware revision cannot be incremented.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if (
                selected.descriptor.input_channels
                > MAX_CALIBRATABLE_INPUT_CHANNELS
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "calibration_schema",
                    "The endpoint channel count cannot be represented by "
                    "canonical calibration JSON.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            try:
                with self._table._meta.database.atomic():
                    row = self._table.get_or_none(
                        self._table.hardware_id == command.hardware_id.value
                    )
                    if row is None:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                            "hardware_lookup",
                            "The managed input does not exist.",
                            active_capture_count=active_capture_count,
                        )
                    if row.hardware_revision != command.expected_hardware_revision:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                            "hardware_revision",
                            "The managed input revision is stale.",
                            active_capture_count=active_capture_count,
                            hardware_snapshot=current_snapshot,
                        )
                    if selected.stable_backend_uid is not None:
                        foreign_owner = (
                            self._table.select()
                            .where(
                                (self._table.stable_backend_uid
                                 == selected.stable_backend_uid)
                                & (self._table.hardware_id != command.hardware_id.value)
                            )
                            .exists()
                        )
                        if foreign_owner:
                            return self._rejected_locked(
                                kind,
                                ManagedInputErrorCode.ENDPOINT_ALREADY_BOUND,
                                "endpoint_ownership",
                                "The selected stable endpoint is already managed.",
                                active_capture_count=active_capture_count,
                                hardware_snapshot=current_snapshot,
                            )
                    row.hardware_revision += 1
                    row.stable_backend_uid = selected.stable_backend_uid
                    row.descriptor_device_name = selected.descriptor.device_name
                    row.descriptor_hostapi_name = selected.descriptor.hostapi_name
                    row.descriptor_sample_rate_hz = (
                        selected.descriptor.sample_rate_hz
                    )
                    row.descriptor_input_channels = (
                        selected.descriptor.input_channels
                    )
                    row.calibration_revision = 0
                    row.calibration_json = self._initial_calibration_json(
                        selected.descriptor.input_channels
                    )
                    row.updated_time = now_str()
                    row.save()
                    records = self._load_rows()
            except Exception:
                return self._database_failure_locked(
                    kind,
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            self._publish_records_locked(records)
            hardware_snapshot = self._snapshot_locked(command.hardware_id)
            self._latch_restart_required_locked()
            return self._result_locked(
                kind=kind,
                status=ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
                active_capture_count=active_capture_count,
                hardware_snapshot=hardware_snapshot,
                observations=(selected,),
            )
        finally:
            self._lock.release()

    def remove(self, command: RemoveManagedInputCommand) -> ManagedInputResult:
        kind = ManagedInputCommandKind.REMOVE
        if not self._try_lock():
            return self._busy_unlocked(kind)
        try:
            guarded = self._guard_locked(kind, command.hardware_id)
            if type(guarded) is ManagedInputResult:
                return guarded
            active_capture_count = guarded
            current = self._record_locked(command.hardware_id)
            current_snapshot = self._snapshot_locked(command.hardware_id)
            if current is None:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                    "hardware_lookup",
                    "The managed input does not exist.",
                    active_capture_count=active_capture_count,
                )
            if current.hardware_revision != command.expected_hardware_revision:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                    "hardware_revision",
                    "The managed input revision is stale.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            try:
                referenced = self._references.configuration_references(
                    command.hardware_id
                )
            except Exception:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "configuration_reference",
                    "Configuration references could not be verified.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if type(referenced) is not bool:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "configuration_reference",
                    "Configuration references could not be verified.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if referenced:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.CONFIG_REFERENCE_EXISTS,
                    "configuration_reference",
                    "A saved configuration still references this managed input.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            try:
                with self._table._meta.database.atomic():
                    row = self._table.get_or_none(
                        self._table.hardware_id == command.hardware_id.value
                    )
                    if row is None:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                            "hardware_lookup",
                            "The managed input does not exist.",
                            active_capture_count=active_capture_count,
                        )
                    if row.hardware_revision != command.expected_hardware_revision:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                            "hardware_revision",
                            "The managed input revision is stale.",
                            active_capture_count=active_capture_count,
                            hardware_snapshot=current_snapshot,
                        )
                    row.delete_instance()
                    records = self._load_rows()
            except Exception:
                return self._database_failure_locked(
                    kind,
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            self._publish_records_locked(records)
            self._latch_restart_required_locked()
            return self._result_locked(
                kind=kind,
                status=ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
                active_capture_count=active_capture_count,
                hardware_snapshot=current_snapshot,
            )
        finally:
            self._lock.release()

    def refresh(
        self,
        command: RefreshManagedInputsCommand,
    ) -> ManagedInputResult:
        kind = ManagedInputCommandKind.REFRESH
        if not self._try_lock():
            return self._busy_unlocked(kind)
        try:
            guarded = self._guard_locked(kind, None)
            if type(guarded) is ManagedInputResult:
                return guarded
            active_capture_count = guarded
            try:
                requires_reinitialize = (
                    self._discovery.refresh_requires_reinitialize
                )
            except Exception:
                return self._refresh_rejection_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The backend refresh capability could not be read.",
                    active_capture_count=active_capture_count,
                )
            if type(requires_reinitialize) is not bool:
                return self._refresh_rejection_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The backend refresh capability is invalid.",
                    active_capture_count=active_capture_count,
                )
            if requires_reinitialize:
                if self._scan_error_code is not None:
                    return self._rejected_locked(
                        kind,
                        self._scan_error_code,
                        "backend_scan",
                        "The current process has a latched input scan diagnostic.",
                        active_capture_count=active_capture_count,
                    )
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.REFRESH_REQUIRES_RESTART,
                    "refresh_capability",
                    "The backend requires a process restart to refresh inputs.",
                    active_capture_count=active_capture_count,
                )
            scan_id = BackendScanId(
                self._process_instance_id,
                self._next_scan_sequence,
            )
            try:
                discovery_result = self._discovery.discover(scan_id)
            except Exception:
                discovery_result = None
            if type(discovery_result) is not BackendDiscoveryResult:
                return self._refresh_rejection_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The discovery boundary returned an invalid result.",
                    active_capture_count=active_capture_count,
                )
            if discovery_result.error is not None:
                return self._refresh_rejection_locked(
                    discovery_result.error.code,
                    discovery_result.error.summary,
                    active_capture_count=active_capture_count,
                )
            scan = discovery_result.scan
            if scan is None:
                return self._refresh_rejection_locked(
                    ManagedInputErrorCode.INVARIANT_VIOLATION,
                    "The discovery boundary returned no scan.",
                    active_capture_count=active_capture_count,
                )
            validation_error = self._validate_refresh_scan_locked(scan, scan_id)
            if validation_error is not None:
                return self._refresh_rejection_locked(
                    *validation_error,
                    active_capture_count=active_capture_count,
                )
            if self._scan_error_code is not None:
                return self._rejected_locked(
                    kind,
                    self._scan_error_code,
                    "backend_scan",
                    "The current process has a latched input scan diagnostic.",
                    active_capture_count=active_capture_count,
                )
            self._current_scan = scan
            self._observations = scan.observations
            if self._startup_stable_uid_capability is None:
                self._startup_stable_uid_capability = scan.stable_uid_capability
            self._next_scan_sequence += 1
            return self._result_locked(
                kind=kind,
                status=ManagedInputResultStatus.SUCCEEDED,
                active_capture_count=active_capture_count,
                observations=scan.observations[:MAX_MANAGED_AUDIO_INPUTS],
            )
        finally:
            self._lock.release()

    def calibrate(
        self,
        command: CalibrateManagedInputCommand,
    ) -> ManagedInputResult:
        kind = ManagedInputCommandKind.CALIBRATE
        if not self._try_lock():
            return self._busy_unlocked(kind)
        try:
            guarded = self._guard_locked(kind, None)
            if type(guarded) is ManagedInputResult:
                return guarded
            active_capture_count = guarded
            current = self._record_locked(command.hardware_id)
            current_snapshot = self._snapshot_locked(command.hardware_id)
            if current is None or current_snapshot is None:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                    "hardware_lookup",
                    "The managed input does not exist.",
                    active_capture_count=active_capture_count,
                )
            if current.hardware_revision != command.expected_hardware_revision:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                    "hardware_revision",
                    "The managed input revision is stale.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if (
                current.calibration_revision
                != command.expected_calibration_revision
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
                    "calibration_revision",
                    "The managed input calibration revision is stale.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if (
                current_snapshot.availability
                is not ManagedInputAvailability.AVAILABLE
                or current_snapshot.resident_generation is None
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.ENDPOINT_UNAVAILABLE,
                    "calibration_stream",
                    "The managed input has no available Resident stream.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if (
                command.channel_index >= current.descriptor.input_channels
                or current.hardware_revision >= SIGNED63_MAX
                or current.calibration_revision >= SIGNED63_MAX
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "calibration_validation",
                    "The calibration request is outside the supported bounds.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            duration_product = current.descriptor.sample_rate_hz * 60.0
            maximum_duration_frames = (
                SIGNED63_MAX
                if not math.isfinite(duration_product)
                or duration_product >= SIGNED63_MAX
                else math.floor(duration_product)
            )
            if command.duration_frames > maximum_duration_frames:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "calibration_validation",
                    "The calibration duration exceeds sixty seconds.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            selected_channel = current.calibration_channels[
                command.channel_index
            ]
            if selected_channel[2] >= SIGNED63_MAX:
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "calibration_validation",
                    "The calibration channel revision cannot be incremented.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            generation = current_snapshot.resident_generation
            try:
                measurement = self._resident.measure_calibration(
                    command.hardware_id,
                    generation,
                    command.channel_index,
                    command.duration_frames,
                )
            except Exception:
                return self._failed_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
                    "calibration_capture",
                    "Resident could not capture the calibration interval.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if type(measurement) is not ResidentCalibrationMeasurement:
                return self._failed_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
                    "calibration_capture",
                    "Resident returned an invalid calibration measurement.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            correction_db = (
                command.standard_level_db - measurement.measured_level_db
            )
            if (
                measurement.generation != generation
                or measurement.channel_index != command.channel_index
                or measurement.captured_frames != command.duration_frames
            ):
                return self._failed_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_CAPTURE_FAILED,
                    "calibration_capture",
                    "Resident returned a mismatched calibration measurement.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            if (
                not math.isfinite(correction_db)
                or not -400.0 <= correction_db <= 400.0
            ):
                return self._rejected_locked(
                    kind,
                    ManagedInputErrorCode.CALIBRATION_INVALID,
                    "calibration_calculation",
                    "The calculated calibration correction is outside the "
                    "supported bounds.",
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            try:
                with self._table._meta.database.atomic():
                    row = self._table.get_or_none(
                        self._table.hardware_id == command.hardware_id.value
                    )
                    if row is None:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.HARDWARE_NOT_FOUND,
                            "hardware_lookup",
                            "The managed input does not exist.",
                            active_capture_count=active_capture_count,
                        )
                    if row.hardware_revision != command.expected_hardware_revision:
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.STALE_HARDWARE_REVISION,
                            "hardware_revision",
                            "The managed input revision is stale.",
                            active_capture_count=active_capture_count,
                            hardware_snapshot=current_snapshot,
                        )
                    if (
                        row.calibration_revision
                        != command.expected_calibration_revision
                    ):
                        return self._rejected_locked(
                            kind,
                            ManagedInputErrorCode.STALE_CALIBRATION_REVISION,
                            "calibration_revision",
                            "The managed input calibration revision is stale.",
                            active_capture_count=active_capture_count,
                            hardware_snapshot=current_snapshot,
                        )
                    channels = list(
                        parse_calibration_json(
                            row.calibration_json,
                            expected_channels=row.descriptor_input_channels,
                        )
                    )
                    previous = channels[command.channel_index]
                    channels[command.channel_index] = (
                        command.channel_index,
                        True,
                        previous[2] + 1,
                        correction_db,
                    )
                    row.calibration_json = serialize_calibration_json(
                        tuple(channels),
                        expected_channels=row.descriptor_input_channels,
                    )
                    row.calibration_revision += 1
                    row.hardware_revision += 1
                    row.updated_time = now_str()
                    row.save()
                    records = self._load_rows()
            except Exception:
                return self._database_failure_locked(
                    kind,
                    active_capture_count=active_capture_count,
                    hardware_snapshot=current_snapshot,
                )
            self._publish_records_locked(records)
            hardware_snapshot = self._snapshot_locked(command.hardware_id)
            self._latch_restart_required_locked()
            return self._result_locked(
                kind=kind,
                status=ManagedInputResultStatus.SUCCEEDED_RESTART_REQUIRED,
                active_capture_count=active_capture_count,
                hardware_snapshot=hardware_snapshot,
            )
        finally:
            self._lock.release()
