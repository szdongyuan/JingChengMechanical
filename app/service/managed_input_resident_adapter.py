from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import threading
import time
from types import MappingProxyType
from typing import Protocol, final

import numpy as np

from app.service.startup_diagnostics import bounded_startup_text

from app.service.analysis.resident_audio_mvp import (
    MAX_ALLOCATABLE_FRAMES,
    MAX_CAPTURE_BUFFER_BYTES,
    MAX_FRAMES_PER_BUFFER,
    MAX_INPUT_CHANNELS,
    MAX_RESIDENT_STREAMS,
    MAX_RING_BYTES_PER_STREAM,
    MAX_SAMPLE_RATE_HZ,
    MAX_TIMEOUT_SECONDS,
    AudioInputBackend,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CapturedAudio,
    MaintenanceReadiness,
    MaintenanceStatus,
    ResidentAudioPool,
    ResidentStreamSpec,
    ShutdownReport,
    ShutdownStatus,
    StreamOpenStatus,
)
from app.service.managed_input_contracts import (
    HardwareEndpointIdentity,
    ManagedHardwareId,
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
    ManagedInputError,
    ManagedInputErrorCode,
    OfflineMaintenanceBlockerSnapshot,
    ResidentCalibrationMeasurement,
    ResidentOpenFailure,
    ResidentReadyStream,
    ResidentStartupOpenResult,
    ResidentStreamGenerationStamp,
    SIGNED63_MAX,
    validate_shutdown_timeout,
)


class CalibrationLevelMeter(Protocol):
    def __call__(
        self,
        channel_samples: np.ndarray,
        sample_rate_hz: int,
    ) -> float: ...


@final
@dataclass(frozen=True, slots=True)
class ResidentCaptureTarget:
    pool: ResidentAudioPool
    resident_hardware_id: str
    generation: ResidentStreamGenerationStamp


def resident_stream_spec_for_endpoint(
    endpoint: HardwareEndpointIdentity,
    resident_hardware_id: str,
    *,
    frames_per_buffer: int,
    max_capture_frames: int,
) -> ResidentStreamSpec | str:
    """Pure stream eligibility shared by Resident and its process transport."""
    rate = endpoint.sample_rate_hz
    if (
        type(rate) is not float
        or not math.isfinite(rate)
        or rate <= 0.0
        or not rate.is_integer()
        or rate > MAX_SAMPLE_RATE_HZ
    ):
        return "unsupported_sample_rate"
    if not 1 <= endpoint.input_channels <= MAX_INPUT_CHANNELS:
        return "unsupported_channel_count"
    channel_count = endpoint.input_channels
    bytes_per_frame = channel_count * np.dtype(np.float32).itemsize
    # The Ring keeps a two-second logical mapping window inside a larger
    # ten-second physical buffer used to isolate downstream scheduling.
    ring_frames = int(rate) * 10 + frames_per_buffer
    if (
        ring_frames > MAX_ALLOCATABLE_FRAMES
        or ring_frames > MAX_RING_BYTES_PER_STREAM // bytes_per_frame
    ):
        return "unsupported_ring_layout"
    if max_capture_frames > MAX_CAPTURE_BUFFER_BYTES // bytes_per_frame:
        return "unsupported_capture_layout"
    return ResidentStreamSpec(
        hardware_id=resident_hardware_id,
        device_index=endpoint.device_index,
        device_name=endpoint.device_name,
        hostapi_index=endpoint.hostapi_index,
        hostapi_name=endpoint.hostapi_name,
        sample_rate_hz=int(rate),
        input_channel_count=channel_count,
        capture_channel_indices=tuple(range(channel_count)),
        sample_format="float32",
        frames_per_buffer=frames_per_buffer,
    )


def _require_exact_int(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _require_timeout(value: object, field_name: str) -> int | float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


@final
class ResidentManagedInputAdapter:
    def __init__(
        self,
        *,
        backend: AudioInputBackend,
        level_meter: CalibrationLevelMeter,
        frames_per_buffer: int,
        max_capture_frames: int,
        startup_stream_timeout_s: float,
        fault_close_timeout_s: float,
        shutdown_timeout_s: float,
        calibration_wait_margin_s: float,
        endpoint_resolver: object | None = None,
        drain_observer: Callable[
            [str, np.ndarray, int, int, int], None
        ]
        | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._frames_per_buffer = _require_exact_int(
            frames_per_buffer,
            "frames_per_buffer",
            maximum=MAX_FRAMES_PER_BUFFER,
        )
        self._max_capture_frames = _require_exact_int(
            max_capture_frames,
            "max_capture_frames",
            maximum=MAX_ALLOCATABLE_FRAMES,
        )
        self._startup_stream_timeout_s = _require_timeout(
            startup_stream_timeout_s,
            "startup_stream_timeout_s",
        )
        self._fault_close_timeout_s = _require_timeout(
            fault_close_timeout_s,
            "fault_close_timeout_s",
        )
        self._shutdown_timeout_s = _require_timeout(
            shutdown_timeout_s,
            "shutdown_timeout_s",
        )
        if (
            type(calibration_wait_margin_s) is not float
            or not math.isfinite(calibration_wait_margin_s)
            or not 0.0 < calibration_wait_margin_s <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("calibration_wait_margin_s is invalid")

        self._backend = backend
        self._level_meter = level_meter
        self._calibration_wait_margin_s = calibration_wait_margin_s
        self._monotonic_ns = monotonic_ns
        if endpoint_resolver is not None and not callable(
            getattr(endpoint_resolver, "resolve", None)
        ):
            raise TypeError("endpoint_resolver must provide resolve")
        self._endpoint_resolver = endpoint_resolver
        if drain_observer is not None and not callable(drain_observer):
            raise TypeError("drain_observer must be callable or None")
        self._drain_observer = drain_observer

        self._lock = threading.Lock()
        self._state = "NEW"
        self._frozen_endpoints: tuple[HardwareEndpointIdentity, ...] | None = None
        self._resident_id_by_managed = MappingProxyType({})
        self._managed_id_by_resident = MappingProxyType({})
        self._endpoint_by_managed = MappingProxyType({})
        self._generation_by_managed = MappingProxyType({})
        self._spec_by_managed = MappingProxyType({})
        self._pool_member_ids = frozenset()
        self._startup_ready_ids = frozenset()
        self._startup_failed_ids = frozenset()
        self._pool: ResidentAudioPool | None = None
        self._startup_result: ResidentStartupOpenResult | None = None
        self._close_result = None
        self._next_calibration_number = 1

    def open_startup_streams(
        self,
        endpoints: tuple[HardwareEndpointIdentity, ...],
    ) -> ResidentStartupOpenResult:
        submitted = self._validate_endpoint_tuple(endpoints)
        with self._lock:
            if (
                self._state == "OPEN"
                and submitted == self._frozen_endpoints
                and self._startup_result is not None
            ):
                return self._startup_result
            if self._state != "NEW":
                raise ValueError("startup membership is already frozen")

            ordered = tuple(
                sorted(submitted, key=lambda item: item.hardware_id.value)
            )
            resident_id_by_managed = {
                item.hardware_id: f"managed-input-{item.hardware_id.value}"
                for item in ordered
            }
            managed_id_by_resident = {
                resident_id: hardware_id
                for hardware_id, resident_id in resident_id_by_managed.items()
            }
            endpoint_by_managed = {
                item.hardware_id: item for item in ordered
            }

            specs: list[ResidentStreamSpec] = []
            conversion_failures: dict[ManagedHardwareId, str] = {}
            for item in ordered:
                converted = self._to_spec(
                    item,
                    resident_id_by_managed[item.hardware_id],
                )
                if type(converted) is str:
                    conversion_failures[item.hardware_id] = converted
                else:
                    specs.append(converted)

            pool: ResidentAudioPool | None
            stream_status_by_managed = {}
            stream_detail_by_managed = {}
            try:
                open_result = ResidentAudioPool.open(
                    tuple(specs),
                    backend=self._backend,
                    max_capture_frames=self._max_capture_frames,
                    startup_stream_timeout_s=self._startup_stream_timeout_s,
                    fault_close_timeout_s=self._fault_close_timeout_s,
                    shutdown_timeout_s=self._shutdown_timeout_s,
                    endpoint_identities={
                        resident_id_by_managed[item.hardware_id]: item
                        for item in ordered
                        if item.hardware_id not in conversion_failures
                    },
                    endpoint_resolver=self._endpoint_resolver,
                    drain_observer=self._drain_observer,
                )
            except Exception as exc:
                pool = None
                detail = bounded_startup_text(exc)
                for spec in specs:
                    hardware_id = managed_id_by_resident[spec.hardware_id]
                    stream_status_by_managed[hardware_id] = StreamOpenStatus.FAULTED
                    stream_detail_by_managed[hardware_id] = detail
            else:
                pool = open_result.pool
                for stream in open_result.streams:
                    hardware_id = managed_id_by_resident[stream.hardware_id]
                    stream_status_by_managed[hardware_id] = stream.status
                    if stream.error is not None:
                        stream_detail_by_managed[hardware_id] = bounded_startup_text(
                            stream.error.summary
                        )

            ready_streams: list[ResidentReadyStream] = []
            failures: list[ResidentOpenFailure] = []
            generation_by_managed = {}
            for item in ordered:
                hardware_id = item.hardware_id
                conversion_code = conversion_failures.get(hardware_id)
                if conversion_code is not None:
                    failures.append(
                        ResidentOpenFailure(hardware_id, conversion_code)
                    )
                    continue
                if (
                    stream_status_by_managed.get(hardware_id)
                    is not StreamOpenStatus.READY
                ):
                    failures.append(
                        ResidentOpenFailure(
                            hardware_id,
                            "resident_open_failed",
                            detail=stream_detail_by_managed.get(hardware_id, ""),
                        )
                    )
                    continue
                generation = ResidentStreamGenerationStamp(
                    hardware_id=hardware_id,
                    pool_epoch=1,
                    stream_generation=1,
                )
                generation_by_managed[hardware_id] = generation
                ready_streams.append(ResidentReadyStream(item, generation))

            startup_result = ResidentStartupOpenResult(
                tuple(ready_streams),
                tuple(failures),
            )
            self._frozen_endpoints = submitted
            self._resident_id_by_managed = MappingProxyType(
                resident_id_by_managed
            )
            self._managed_id_by_resident = MappingProxyType(
                managed_id_by_resident
            )
            self._endpoint_by_managed = MappingProxyType(endpoint_by_managed)
            self._generation_by_managed = MappingProxyType(
                generation_by_managed
            )
            self._spec_by_managed = MappingProxyType(
                {
                    managed_id_by_resident[spec.hardware_id]: spec
                    for spec in specs
                }
            )
            self._pool_member_ids = frozenset(
                managed_id_by_resident[spec.hardware_id] for spec in specs
            )
            self._startup_ready_ids = frozenset(generation_by_managed)
            self._startup_failed_ids = frozenset(
                item.hardware_id for item in failures
            )
            self._pool = pool
            self._startup_result = startup_result
            self._state = "OPEN"
            return startup_result

    def snapshot(
        self,
        hardware_id: ManagedHardwareId | None,
    ) -> OfflineMaintenanceBlockerSnapshot:
        with self._lock:
            pool = self._pool
            targets: tuple[str, ...] = ()
            requested_resident_id = None
            if hardware_id in self._pool_member_ids:
                requested_resident_id = self._resident_id_by_managed[hardware_id]
                targets = (requested_resident_id,)
        if pool is None:
            return OfflineMaintenanceBlockerSnapshot(0, False)
        try:
            readiness = pool.maintenance_readiness(targets)
        except Exception:
            return OfflineMaintenanceBlockerSnapshot(None, None)
        if (
            type(readiness) is not MaintenanceReadiness
            or readiness.status
            not in (MaintenanceStatus.READY, MaintenanceStatus.BUSY)
        ):
            return OfflineMaintenanceBlockerSnapshot(None, None)
        return OfflineMaintenanceBlockerSnapshot(
            len(readiness.active_binding_ids),
            requested_resident_id is not None
            and requested_resident_id in readiness.owned_hardware_ids,
        )

    def measure_calibration(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
        channel_index: int,
        duration_frames: int,
    ) -> ResidentCalibrationMeasurement:
        if type(hardware_id) is not ManagedHardwareId:
            raise ValueError("hardware_id must be an exact ManagedHardwareId")
        if type(generation) is not ResidentStreamGenerationStamp:
            raise ValueError(
                "generation must be an exact ResidentStreamGenerationStamp"
            )
        if type(channel_index) is not int:
            raise ValueError("channel_index must be an exact integer")
        if type(duration_frames) is not int:
            raise ValueError("duration_frames is invalid")

        with self._lock:
            expected_generation = self._generation_by_managed.get(hardware_id)
            spec = self._spec_by_managed.get(hardware_id)
            pool = self._pool
            if (
                self._state != "OPEN"
                or hardware_id not in self._startup_ready_ids
                or expected_generation is None
                or generation != expected_generation
                or spec is None
                or pool is None
            ):
                raise ValueError("resident calibration target is not ready")
            if channel_index not in spec.capture_channel_indices:
                raise ValueError("calibration channel is invalid")
            if not 1 <= duration_frames <= self._max_capture_frames:
                raise ValueError("duration_frames is invalid")
            wait_timeout_s = (
                duration_frames / spec.sample_rate_hz
                + self._calibration_wait_margin_s
            )
            if (
                not math.isfinite(wait_timeout_s)
                or wait_timeout_s <= 0.0
                or wait_timeout_s > threading.TIMEOUT_MAX
            ):
                raise ValueError("calibration wait timeout is invalid")
            resident_hardware_id = spec.hardware_id
            channel_position = spec.capture_channel_indices.index(
                channel_index
            )
            run_id = (
                f"resident-calibration:{hardware_id.value}:"
                f"{self._next_calibration_number}"
            )
            self._next_calibration_number += 1

        binding = pool.reserve_capture(run_id, resident_hardware_id)
        terminal_outcome_obtained = False
        try:
            trigger_monotonic_ns = self._monotonic_ns()
            if (
                type(trigger_monotonic_ns) is not int
                or trigger_monotonic_ns <= 0
            ):
                raise ValueError("calibration trigger is invalid")
            completion = pool.start_capture(
                binding,
                trigger_monotonic_ns=trigger_monotonic_ns,
                target_frame_count=duration_frames,
            )
            outcome = completion.wait(wait_timeout_s)
            outcome_status = getattr(outcome, "status", None)
            terminal_outcome_obtained = (
                type(outcome) is CaptureOutcome
                and getattr(outcome, "binding", None) is binding
                and (
                    outcome_status is CaptureOutcomeStatus.COMPLETED
                    or outcome_status is CaptureOutcomeStatus.CANCELLED
                    or outcome_status is CaptureOutcomeStatus.FAILED
                )
            )
        except Exception:
            if not terminal_outcome_obtained:
                try:
                    pool.cancel_capture(binding)
                except Exception:
                    pass
            raise
        if not terminal_outcome_obtained:
            try:
                pool.cancel_capture(binding)
            except Exception:
                pass
            if outcome is None:
                raise RuntimeError("resident calibration capture timed out")
            raise RuntimeError("resident calibration capture failed")
        if (
            type(outcome) is not CaptureOutcome
            or outcome.status is not CaptureOutcomeStatus.COMPLETED
            or outcome.binding is not binding
            or outcome.error is not None
            or type(outcome.audio) is not CapturedAudio
        ):
            raise RuntimeError("resident calibration capture failed")
        audio = outcome.audio
        if (
            audio.binding is not binding
            or audio.hardware_id != resident_hardware_id
            or audio.sample_rate_hz != spec.sample_rate_hz
            or audio.frame_count != duration_frames
            or audio.channel_indices != spec.capture_channel_indices
            or audio.samples.dtype != np.dtype(np.float32)
            or audio.samples.ndim != 2
            or audio.samples.flags.writeable
            or audio.samples.shape
            != (duration_frames, len(spec.capture_channel_indices))
        ):
            raise RuntimeError("resident calibration audio is invalid")
        channel_view = audio.samples[:, channel_position]
        channel_view.setflags(write=False)
        measured_level_db = self._level_meter(
            channel_view,
            spec.sample_rate_hz,
        )
        if (
            type(measured_level_db) is not float
            or not math.isfinite(measured_level_db)
        ):
            raise RuntimeError("calibration level meter returned invalid data")
        return ResidentCalibrationMeasurement(
            generation,
            channel_index,
            measured_level_db,
            duration_frames,
        )

    def _to_spec(
        self,
        endpoint: HardwareEndpointIdentity,
        resident_hardware_id: str,
    ) -> ResidentStreamSpec | str:
        return resident_stream_spec_for_endpoint(
            endpoint,
            resident_hardware_id,
            frames_per_buffer=self._frames_per_buffer,
            max_capture_frames=self._max_capture_frames,
        )

    def capture_target(
        self,
        hardware_id: ManagedHardwareId,
        generation: ResidentStreamGenerationStamp,
    ) -> ResidentCaptureTarget:
        if type(hardware_id) is not ManagedHardwareId:
            raise ValueError("hardware_id must be an exact ManagedHardwareId")
        if type(generation) is not ResidentStreamGenerationStamp:
            raise ValueError(
                "generation must be an exact ResidentStreamGenerationStamp"
            )
        with self._lock:
            expected = self._generation_by_managed.get(hardware_id)
            if (
                self._state != "OPEN"
                or expected is None
                or generation != expected
                or self._pool is None
            ):
                raise ValueError("resident capture target is not ready")
            return ResidentCaptureTarget(
                pool=self._pool,
                resident_hardware_id=self._resident_id_by_managed[hardware_id],
                generation=expected,
            )

    def close_all(
        self,
        timeout_seconds: float,
    ) -> tuple[ManagedInputCloseResult, ...]:
        validate_shutdown_timeout(timeout_seconds)
        with self._lock:
            if self._close_result is not None:
                return self._close_result

            ordered_ids = tuple(
                sorted(
                    (
                        endpoint.hardware_id
                        for endpoint in (self._frozen_endpoints or ())
                    ),
                    key=lambda hardware_id: hardware_id.value,
                )
            )
            pool = self._pool
            if pool is None:
                close_result = tuple(
                    ManagedInputCloseResult(
                        hardware_id,
                        ManagedInputCloseStatus.NOT_OPEN,
                        None,
                    )
                    for hardware_id in ordered_ids
                )
                self._close_result = close_result
                self._state = "CLOSED"
                return close_result

            now_ns = self._monotonic_ns()
            timeout_ns = math.ceil(timeout_seconds * 1_000_000_000)
            if (
                type(now_ns) is not int
                or now_ns < 0
                or timeout_ns < 1
                or now_ns > SIGNED63_MAX - timeout_ns
            ):
                raise ValueError("shutdown deadline is invalid")
            deadline_ns = now_ns + timeout_ns
            try:
                report = pool.shutdown(
                    deadline_monotonic_ns=deadline_ns,
                )
            except Exception:
                report = None

            valid_report = type(report) is ShutdownReport
            report_status = report.status if valid_report else None
            unclosed_ids = (
                frozenset(report.unclosed_hardware_ids)
                if valid_report
                else frozenset()
            )
            close_error = ManagedInputError(
                ManagedInputErrorCode.RESIDENT_STREAM_FAILED,
                "resident_close",
                "Resident shutdown did not finish cleanly; "
                "stop and restart the process.",
            )
            close_results: list[ManagedInputCloseResult] = []
            for hardware_id in ordered_ids:
                resident_hardware_id = self._resident_id_by_managed[hardware_id]
                if resident_hardware_id in unclosed_ids:
                    status = ManagedInputCloseStatus.FAILED
                elif hardware_id not in self._startup_ready_ids:
                    status = ManagedInputCloseStatus.NOT_OPEN
                elif report_status is ShutdownStatus.CLOSED:
                    status = ManagedInputCloseStatus.CLOSED
                else:
                    status = ManagedInputCloseStatus.FAILED
                close_results.append(
                    ManagedInputCloseResult(
                        hardware_id,
                        status,
                        close_error
                        if status is ManagedInputCloseStatus.FAILED
                        else None,
                    )
                )

            close_result = tuple(close_results)
            self._close_result = close_result
            self._state = "CLOSED"
            return close_result

    @staticmethod
    def _validate_endpoint_tuple(
        endpoints: object,
    ) -> tuple[HardwareEndpointIdentity, ...]:
        if type(endpoints) is not tuple:
            raise ValueError("endpoints must be an exact tuple")
        if len(endpoints) > MAX_RESIDENT_STREAMS:
            raise ValueError("endpoints exceed resident stream limit")
        for item in endpoints:
            if type(item) is not HardwareEndpointIdentity:
                raise ValueError(
                    "endpoints must contain exact HardwareEndpointIdentity values"
                )
        hardware_ids = tuple(item.hardware_id for item in endpoints)
        if len(set(hardware_ids)) != len(hardware_ids):
            raise ValueError("endpoints contain duplicate managed hardware IDs")
        return endpoints
