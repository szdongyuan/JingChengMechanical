from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import threading

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import (
    MAX_ALLOCATABLE_FRAMES,
    MAX_CAPTURE_BUFFER_BYTES,
    MAX_FRAMES_PER_BUFFER,
    MAX_INPUT_CHANNELS,
    MAX_RING_BYTES_PER_STREAM,
    MAX_SAMPLE_RATE_HZ,
    MAX_TIMEOUT_SECONDS,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CapturedAudio,
    MaintenanceReadiness,
    MaintenanceStatus,
    ResidentAudioPool,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ShutdownReport,
    ShutdownStatus,
)
from app.service.analysis.resident_audio_mvp import ring as resident_ring
from app.service import managed_input_resident_adapter as adapter_module
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    ManagedHardwareId,
    ManagedInputCloseResult,
    ManagedInputCloseStatus,
    ManagedInputErrorCode,
    OfflineMaintenanceBlockerSnapshot,
    ResidentCalibrationMeasurement,
    ResidentStreamGenerationStamp,
)
from app.service.managed_input_resident_adapter import (
    ResidentManagedInputAdapter,
)
from tests.resident_audio_mvp.fakes import FakeBackend, ManualMonotonicClock


def endpoint(
    hardware_value: int,
    *,
    sample_rate_hz: float = 48_000.0,
    input_channels: int = 2,
) -> HardwareEndpointIdentity:
    return HardwareEndpointIdentity(
        hardware_id=ManagedHardwareId(hardware_value),
        scan_id=BackendScanId("adapter-test", 1),
        device_index=hardware_value,
        hostapi_index=0,
        stable_backend_uid=f"uid-{hardware_value}",
        device_name=f"mic-{hardware_value}",
        hostapi_name="fake-api",
        sample_rate_hz=sample_rate_hz,
        input_channels=input_channels,
    )


def make_adapter(
    backend: FakeBackend | None = None,
    **overrides: object,
) -> ResidentManagedInputAdapter:
    values: dict[str, object] = {
        "backend": backend or FakeBackend(),
        "level_meter": lambda samples, sample_rate_hz: 0.0,
        "frames_per_buffer": 256,
        "max_capture_frames": 48_000,
        "startup_stream_timeout_s": 1.0,
        "fault_close_timeout_s": 1.0,
        "shutdown_timeout_s": 1.0,
        "calibration_wait_margin_s": 0.25,
    }
    values.update(overrides)
    return ResidentManagedInputAdapter(**values)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("frames_per_buffer", True),
        ("frames_per_buffer", 0),
        ("frames_per_buffer", MAX_FRAMES_PER_BUFFER + 1),
        ("max_capture_frames", False),
        ("max_capture_frames", 0),
        ("max_capture_frames", MAX_ALLOCATABLE_FRAMES + 1),
        ("startup_stream_timeout_s", True),
        ("startup_stream_timeout_s", 0.0),
        ("startup_stream_timeout_s", math.inf),
        ("startup_stream_timeout_s", MAX_TIMEOUT_SECONDS + 0.001),
        ("fault_close_timeout_s", False),
        ("fault_close_timeout_s", 0),
        ("fault_close_timeout_s", math.nan),
        ("fault_close_timeout_s", MAX_TIMEOUT_SECONDS + 1.0),
        ("shutdown_timeout_s", True),
        ("shutdown_timeout_s", -1.0),
        ("shutdown_timeout_s", -math.inf),
        ("shutdown_timeout_s", MAX_TIMEOUT_SECONDS + 0.001),
        ("calibration_wait_margin_s", True),
        ("calibration_wait_margin_s", 1),
        ("calibration_wait_margin_s", 0.0),
        ("calibration_wait_margin_s", math.nan),
        ("calibration_wait_margin_s", MAX_TIMEOUT_SECONDS + 0.001),
    ],
)
def test_constructor_rejects_invalid_limits(
    field: str,
    invalid: object,
) -> None:
    backend = FakeBackend()

    with pytest.raises((TypeError, ValueError)):
        make_adapter(backend, **{field: invalid})

    assert backend.create_calls == []


@pytest.mark.parametrize(
    "submitted",
    [
        [],
        [endpoint(1)],
        iter((endpoint(1),)),
        {"endpoint": endpoint(1)},
    ],
)
def test_open_requires_an_exact_tuple(submitted: object) -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)

    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams(submitted)  # type: ignore[arg-type]

    assert backend.create_calls == []


class EndpointSubclass(HardwareEndpointIdentity):
    pass


class EndpointLookalike:
    def __init__(self, original: HardwareEndpointIdentity) -> None:
        for field_name in original.__dataclass_fields__:
            setattr(self, field_name, getattr(original, field_name))


def endpoint_subclass() -> EndpointSubclass:
    original = endpoint(1)
    return EndpointSubclass(
        hardware_id=original.hardware_id,
        scan_id=original.scan_id,
        device_index=original.device_index,
        hostapi_index=original.hostapi_index,
        stable_backend_uid=original.stable_backend_uid,
        device_name=original.device_name,
        hostapi_name=original.hostapi_name,
        sample_rate_hz=original.sample_rate_hz,
        input_channels=original.input_channels,
    )


@pytest.mark.parametrize(
    "invalid_item",
    [
        endpoint_subclass(),
        EndpointLookalike(endpoint(1)),
        asdict(endpoint(1)),
        object(),
    ],
)
def test_open_requires_exact_endpoint_items(invalid_item: object) -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)

    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams((invalid_item,))  # type: ignore[arg-type]

    assert backend.create_calls == []


def test_open_rejects_duplicate_managed_ids_before_backend_calls() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)

    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams((endpoint(1), endpoint(1)))

    assert backend.create_calls == []


def test_open_rejects_seventh_endpoint_before_backend_calls() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)

    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams(tuple(endpoint(value) for value in range(1, 8)))

    assert backend.create_calls == []


def failure_codes(result: object) -> dict[int, str]:
    return {
        item.hardware_id.value: item.error_code
        for item in result.failures  # type: ignore[attr-defined]
    }


def ready_ids(result: object) -> tuple[int, ...]:
    return tuple(
        item.endpoint.hardware_id.value
        for item in result.ready_streams  # type: ignore[attr-defined]
    )


def test_allocation_accepts_exact_integer_sample_rate_boundaries() -> None:
    adapter = make_adapter(frames_per_buffer=1)

    result = adapter.open_startup_streams(
        (
            endpoint(1, sample_rate_hz=1.0, input_channels=1),
            endpoint(
                2,
                sample_rate_hz=float(MAX_SAMPLE_RATE_HZ),
                input_channels=1,
            ),
        )
    )

    assert ready_ids(result) == (1, 2)
    assert result.failures == ()


@pytest.mark.parametrize(
    "invalid_rate",
    [48_000.5, float(MAX_SAMPLE_RATE_HZ + 1)],
)
def test_allocation_isolates_unsupported_sample_rates(
    invalid_rate: float,
) -> None:
    backend = FakeBackend(
        clock=ManualMonotonicClock(initial_ns=10_000_000_000)
    )
    result = make_adapter(backend).open_startup_streams(
        (endpoint(1, sample_rate_hz=invalid_rate), endpoint(2))
    )

    assert ready_ids(result) == (2,)
    assert failure_codes(result) == {1: "unsupported_sample_rate"}
    assert [spec.hardware_id for spec in backend.create_calls] == [
        "managed-input-2"
    ]


def test_allocation_accepts_channel_boundaries_and_isolates_sixty_five() -> None:
    backend = FakeBackend()
    result = make_adapter(backend).open_startup_streams(
        (
            endpoint(1, input_channels=1),
            endpoint(2, input_channels=MAX_INPUT_CHANNELS),
            endpoint(3, input_channels=MAX_INPUT_CHANNELS + 1),
        )
    )

    assert ready_ids(result) == (1, 2)
    assert failure_codes(result) == {3: "unsupported_channel_count"}
    assert [spec.hardware_id for spec in backend.create_calls] == [
        "managed-input-1",
        "managed-input-2",
    ]


def test_allocation_enforces_exact_ring_frame_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate = 48_000
    frames_per_buffer = 256
    ring_frames = rate * 2 + frames_per_buffer
    monkeypatch.setattr(
        adapter_module,
        "MAX_ALLOCATABLE_FRAMES",
        ring_frames,
    )

    exact = make_adapter(
        frames_per_buffer=frames_per_buffer
    ).open_startup_streams((endpoint(1),))
    over_backend = FakeBackend()
    over = make_adapter(
        over_backend,
        frames_per_buffer=frames_per_buffer + 1,
    ).open_startup_streams((endpoint(2),))

    assert ready_ids(exact) == (1,)
    assert failure_codes(over) == {2: "unsupported_ring_layout"}
    assert over_backend.create_calls == []


def test_allocation_enforces_exact_ring_byte_boundary_without_large_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = MAX_INPUT_CHANNELS
    bytes_per_frame = channels * np.dtype(np.float32).itemsize
    boundary_frames = MAX_RING_BYTES_PER_STREAM // bytes_per_frame
    rate = MAX_SAMPLE_RATE_HZ
    frames_per_buffer = boundary_frames - rate * 2
    allocation_shapes: list[tuple[int, int]] = []

    def reject_large_allocation(
        shape: tuple[int, int],
        *,
        dtype: object,
    ) -> np.ndarray:
        allocation_shapes.append(shape)
        raise MemoryError("test avoids allocating the public byte boundary")

    monkeypatch.setattr(resident_ring.np, "empty", reject_large_allocation)
    exact = make_adapter(
        frames_per_buffer=frames_per_buffer
    ).open_startup_streams(
        (
            endpoint(
                1,
                sample_rate_hz=float(rate),
                input_channels=channels,
            ),
        )
    )

    assert failure_codes(exact) == {1: "resident_open_failed"}
    assert allocation_shapes == [(boundary_frames, channels)]

    allocation_shapes.clear()
    over_backend = FakeBackend()
    over = make_adapter(
        over_backend,
        frames_per_buffer=frames_per_buffer + 1,
    ).open_startup_streams(
        (
            endpoint(
                2,
                sample_rate_hz=float(rate),
                input_channels=channels,
            ),
        )
    )

    assert failure_codes(over) == {2: "unsupported_ring_layout"}
    assert allocation_shapes == []
    assert over_backend.create_calls == []


def test_allocation_enforces_exact_capture_byte_boundary() -> None:
    channels = MAX_INPUT_CHANNELS
    bytes_per_frame = channels * np.dtype(np.float32).itemsize
    boundary_frames = MAX_CAPTURE_BUFFER_BYTES // bytes_per_frame

    exact = make_adapter(
        max_capture_frames=boundary_frames
    ).open_startup_streams((endpoint(1, input_channels=channels),))
    over_backend = FakeBackend()
    over = make_adapter(
        over_backend,
        max_capture_frames=boundary_frames + 1,
    ).open_startup_streams((endpoint(2, input_channels=channels),))

    assert ready_ids(exact) == (1,)
    assert failure_codes(over) == {2: "unsupported_capture_layout"}
    assert over_backend.create_calls == []


def test_allocation_filters_one_invalid_layout_before_backend_or_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels = MAX_INPUT_CHANNELS
    bytes_per_frame = channels * np.dtype(np.float32).itemsize
    boundary_frames = MAX_RING_BYTES_PER_STREAM // bytes_per_frame
    rate = MAX_SAMPLE_RATE_HZ
    frames_per_buffer = boundary_frames - rate * 2 + 1
    invalid_shape = (boundary_frames + 1, channels)
    allocation_shapes: list[tuple[int, int]] = []
    real_empty = np.empty

    def recording_empty(
        shape: tuple[int, int],
        *,
        dtype: object,
    ) -> np.ndarray:
        allocation_shapes.append(shape)
        return real_empty(shape, dtype=dtype)

    monkeypatch.setattr(resident_ring.np, "empty", recording_empty)
    backend = FakeBackend(
        clock=ManualMonotonicClock(initial_ns=10_000_000_000)
    )
    submitted = tuple(endpoint(value, input_channels=1) for value in range(1, 6))
    submitted += (
        endpoint(
            6,
            sample_rate_hz=float(rate),
            input_channels=channels,
        ),
    )

    result = make_adapter(
        backend,
        frames_per_buffer=frames_per_buffer,
    ).open_startup_streams(submitted)

    assert ready_ids(result) == (1, 2, 3, 4, 5)
    assert failure_codes(result) == {6: "unsupported_ring_layout"}
    assert [spec.hardware_id for spec in backend.create_calls] == [
        f"managed-input-{value}" for value in range(1, 6)
    ]
    assert invalid_shape not in allocation_shapes


@pytest.mark.parametrize("count", [0, 1, 6])
def test_startup_opens_zero_one_or_six_submitted_endpoints(count: int) -> None:
    backend = FakeBackend()
    submitted = tuple(endpoint(value) for value in range(1, count + 1))

    result = make_adapter(backend).open_startup_streams(submitted)

    assert ready_ids(result) == tuple(range(1, count + 1))
    assert result.failures == ()
    assert len(backend.create_calls) == count
    for spec in backend.create_calls:
        assert len(backend.handles[spec.hardware_id].start_deadlines) == 1


@pytest.mark.parametrize("fault_phase", ["create", "start"])
def test_startup_isolates_one_backend_fault_from_ready_siblings(
    fault_phase: str,
) -> None:
    resident_id = "managed-input-2"
    if fault_phase == "create":
        backend = FakeBackend(
            create_errors={resident_id: RuntimeError("create failed")}
        )
    else:
        def fail_start(_handle: object) -> None:
            raise RuntimeError("start failed")

        backend = FakeBackend(start_actions={resident_id: fail_start})

    result = make_adapter(backend).open_startup_streams(
        tuple(endpoint(value) for value in range(1, 7))
    )

    assert ready_ids(result) == (1, 3, 4, 5, 6)
    assert failure_codes(result) == {2: "resident_open_failed"}
    assert len(backend.create_calls) == 6


def test_startup_returns_original_endpoint_and_fixed_generation() -> None:
    original = endpoint(7)

    result = make_adapter().open_startup_streams((original,))

    ready = result.ready_streams[0]
    assert ready.endpoint is original
    assert ready.generation.hardware_id is original.hardware_id
    assert ready.generation.pool_epoch == 1
    assert ready.generation.stream_generation == 1


def test_startup_partition_is_unique_and_numeric_stable_sorted() -> None:
    backend = FakeBackend(
        create_errors={
            "managed-input-2": RuntimeError("two failed"),
            "managed-input-11": RuntimeError("eleven failed"),
        }
    )
    submitted = (endpoint(11), endpoint(10), endpoint(2), endpoint(1))

    result = make_adapter(backend).open_startup_streams(submitted)

    assert ready_ids(result) == (1, 10)
    assert tuple(item.hardware_id.value for item in result.failures) == (2, 11)
    partition = ready_ids(result) + tuple(
        item.hardware_id.value for item in result.failures
    )
    assert sorted(partition) == [1, 2, 10, 11]
    assert len(set(partition)) == len(submitted)


def test_startup_builds_exact_resident_specs() -> None:
    backend = FakeBackend()
    original = endpoint(
        4,
        sample_rate_hz=96_000.0,
        input_channels=3,
    )

    make_adapter(
        backend,
        frames_per_buffer=512,
    ).open_startup_streams((original,))

    assert len(backend.create_calls) == 1
    spec = backend.create_calls[0]
    assert spec.hardware_id == "managed-input-4"
    assert spec.device_index == original.device_index
    assert spec.device_name == original.device_name
    assert spec.hostapi_index == original.hostapi_index
    assert spec.hostapi_name == original.hostapi_name
    assert spec.sample_rate_hz == 96_000
    assert spec.input_channel_count == 3
    assert spec.capture_channel_indices == (0, 1, 2)
    assert spec.sample_format == "float32"
    assert spec.frames_per_buffer == 512


def test_startup_forwards_exact_pool_open_dependencies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    real_open = ResidentAudioPool.open
    calls: list[dict[str, object]] = []

    def recording_open(
        cls: type[ResidentAudioPool],
        specs: tuple[object, ...],
        **kwargs: object,
    ) -> object:
        calls.append({"specs": specs, **kwargs})
        return real_open(specs, **kwargs)

    monkeypatch.setattr(
        ResidentAudioPool,
        "open",
        classmethod(recording_open),
    )
    result = make_adapter(
        backend,
        max_capture_frames=12_345,
        startup_stream_timeout_s=2.5,
        fault_close_timeout_s=3.5,
        shutdown_timeout_s=4.5,
    ).open_startup_streams((endpoint(1),))

    assert ready_ids(result) == (1,)
    assert len(calls) == 1
    assert type(calls[0]["specs"]) is tuple
    assert calls[0]["backend"] is backend
    assert calls[0]["max_capture_frames"] == 12_345
    assert calls[0]["startup_stream_timeout_s"] == 2.5
    assert calls[0]["fault_close_timeout_s"] == 3.5
    assert calls[0]["shutdown_timeout_s"] == 4.5


def test_repeated_open_returns_identical_frozen_startup_result() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)
    submitted = (endpoint(1), endpoint(2))

    first = adapter.open_startup_streams(submitted)
    second = adapter.open_startup_streams(submitted)

    assert second is first
    assert len(backend.create_calls) == 2
    assert all(
        len(handle.start_deadlines) == 1
        for handle in backend.handles.values()
    )


def test_repeated_open_with_changed_membership_has_no_backend_side_effect() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)
    adapter.open_startup_streams((endpoint(1),))
    calls_before = tuple(backend.create_calls)

    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams((endpoint(1), endpoint(2)))

    assert tuple(backend.create_calls) == calls_before


def test_startup_top_level_pool_exception_partitions_once_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def fail_open(
        cls: type[ResidentAudioPool],
        specs: tuple[object, ...],
        **_kwargs: object,
    ) -> object:
        calls.append(specs)
        raise RuntimeError("top-level open failed")

    monkeypatch.setattr(
        ResidentAudioPool,
        "open",
        classmethod(fail_open),
    )
    adapter = make_adapter()
    submitted = (
        endpoint(3),
        endpoint(1, sample_rate_hz=48_000.5),
        endpoint(2),
    )

    result = adapter.open_startup_streams(submitted)

    assert result.ready_streams == ()
    assert tuple(item.hardware_id.value for item in result.failures) == (1, 2, 3)
    assert failure_codes(result) == {
        1: "unsupported_sample_rate",
        2: "resident_open_failed",
        3: "resident_open_failed",
    }
    assert len(calls) == 1
    assert tuple(spec.hardware_id for spec in calls[0]) == (
        "managed-input-2",
        "managed-input-3",
    )


def test_snapshot_readiness_uses_exact_empty_target_for_six_idle_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    real_readiness = ResidentAudioPool.maintenance_readiness

    def recording_readiness(
        pool: ResidentAudioPool,
        targets: object,
    ) -> MaintenanceReadiness:
        calls.append(targets)
        return real_readiness(pool, targets)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "maintenance_readiness",
        recording_readiness,
    )
    adapter = make_adapter()
    adapter.open_startup_streams(
        tuple(endpoint(value) for value in range(1, 7))
    )

    snapshot = adapter.snapshot(None)

    assert type(snapshot) is OfflineMaintenanceBlockerSnapshot
    assert snapshot == OfflineMaintenanceBlockerSnapshot(0, False)
    assert calls == [()]


def test_snapshot_readiness_empty_target_counts_global_reserved_binding() -> None:
    adapter = make_adapter()
    startup = adapter.open_startup_streams((endpoint(1), endpoint(2)))
    target = adapter.capture_target(
        ManagedHardwareId(2),
        startup.ready_streams[1].generation,
    )
    binding = target.pool.reserve_capture("readiness-run", target.resident_hardware_id)
    try:
        assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
            1,
            False,
        )
    finally:
        target.pool.cancel_capture(binding)


def test_snapshot_readiness_uses_one_exact_known_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    real_readiness = ResidentAudioPool.maintenance_readiness

    def recording_readiness(
        pool: ResidentAudioPool,
        targets: object,
    ) -> MaintenanceReadiness:
        calls.append(targets)
        return real_readiness(pool, targets)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "maintenance_readiness",
        recording_readiness,
    )
    adapter = make_adapter()
    adapter.open_startup_streams((endpoint(1), endpoint(2)))

    snapshot = adapter.snapshot(ManagedHardwareId(2))

    assert snapshot == OfflineMaintenanceBlockerSnapshot(0, True)
    assert calls == [("managed-input-2",)]


@pytest.mark.parametrize(
    "requested_hardware_id",
    [ManagedHardwareId(2), ManagedHardwareId(999)],
)
def test_snapshot_readiness_unknown_target_still_counts_global_active_binding(
    monkeypatch: pytest.MonkeyPatch,
    requested_hardware_id: ManagedHardwareId,
) -> None:
    calls: list[object] = []
    real_readiness = ResidentAudioPool.maintenance_readiness

    def recording_readiness(
        pool: ResidentAudioPool,
        targets: object,
    ) -> MaintenanceReadiness:
        calls.append(targets)
        return real_readiness(pool, targets)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "maintenance_readiness",
        recording_readiness,
    )
    adapter = make_adapter()
    startup = adapter.open_startup_streams(
        (endpoint(1), endpoint(2, sample_rate_hz=48_000.5))
    )
    target = adapter.capture_target(
        ManagedHardwareId(1),
        startup.ready_streams[0].generation,
    )
    binding = target.pool.reserve_capture(
        "global-readiness-run",
        target.resident_hardware_id,
    )
    try:
        snapshot = adapter.snapshot(requested_hardware_id)
    finally:
        target.pool.cancel_capture(binding)

    assert snapshot == OfflineMaintenanceBlockerSnapshot(1, False)
    assert calls == [()]


@pytest.mark.parametrize(
    "readiness",
    [
        MaintenanceReadiness(MaintenanceStatus.CLOSING, (), (), "closing"),
        MaintenanceReadiness(MaintenanceStatus.CLOSED, (), (), "closed"),
        object(),
        RuntimeError("readiness failed"),
    ],
)
def test_snapshot_readiness_maps_unknown_public_results(
    monkeypatch: pytest.MonkeyPatch,
    readiness: object,
) -> None:
    adapter = make_adapter()
    adapter.open_startup_streams((endpoint(1),))

    def selected_readiness(
        _pool: ResidentAudioPool,
        _targets: object,
    ) -> object:
        if isinstance(readiness, BaseException):
            raise readiness
        return readiness

    monkeypatch.setattr(
        ResidentAudioPool,
        "maintenance_readiness",
        selected_readiness,
    )

    assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
        None,
        None,
    )


def test_snapshot_readiness_without_pool_is_known_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(
        cls: type[ResidentAudioPool],
        specs: tuple[object, ...],
        **_kwargs: object,
    ) -> object:
        raise RuntimeError("pool open failed")

    monkeypatch.setattr(
        ResidentAudioPool,
        "open",
        classmethod(fail_open),
    )
    adapter = make_adapter()
    adapter.open_startup_streams((endpoint(1),))

    assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
        0,
        False,
    )


def test_calibration_success_uses_exact_resident_capture_and_meter_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(emit_valid_by_default=True)
    trigger_calls: list[int] = []
    reserved: list[tuple[str, str]] = []
    started: list[tuple[object, int, int]] = []
    meter_calls: list[tuple[np.ndarray, int]] = []
    capture_started = threading.Event()
    duration_frames = 300
    adapter: ResidentManagedInputAdapter

    def monotonic_ns() -> int:
        value = backend.clock()
        trigger_calls.append(value)
        return value

    def meter(samples: np.ndarray, sample_rate_hz: int) -> float:
        assert adapter._lock.acquire(blocking=False) is True
        adapter._lock.release()
        meter_calls.append((samples, sample_rate_hz))
        return -17.25

    real_reserve = ResidentAudioPool.reserve_capture
    real_start = ResidentAudioPool.start_capture

    def recording_reserve(
        pool: ResidentAudioPool,
        run_id: str,
        hardware_id: str,
    ) -> object:
        reserved.append((run_id, hardware_id))
        return real_reserve(pool, run_id, hardware_id)

    def recording_start(
        pool: ResidentAudioPool,
        binding: object,
        *,
        trigger_monotonic_ns: int,
        target_frame_count: int,
    ) -> object:
        completion = real_start(
            pool,
            binding,  # type: ignore[arg-type]
            trigger_monotonic_ns=trigger_monotonic_ns,
            target_frame_count=target_frame_count,
        )
        started.append(
            (binding, trigger_monotonic_ns, target_frame_count)
        )
        capture_started.set()
        return completion

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        recording_reserve,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        recording_start,
    )
    adapter = make_adapter(
        backend,
        level_meter=meter,
        frames_per_buffer=64,
        monotonic_ns=monotonic_ns,
    )
    startup = adapter.open_startup_streams((endpoint(1), endpoint(2)))
    generation = startup.ready_streams[0].generation

    def emit_future_frames() -> None:
        assert capture_started.wait(5)
        backend.handles["managed-input-1"].emit_numbered_frames(
            first=0,
            count=duration_frames,
        )

    emitter = threading.Thread(target=emit_future_frames)
    emitter.start()
    try:
        measurement = adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            1,
            duration_frames,
        )
    finally:
        capture_started.set()
        emitter.join(5)

    assert emitter.is_alive() is False
    assert reserved == [
        ("resident-calibration:1:1", "managed-input-1")
    ]
    assert len(started) == 1
    assert started[0][1:] == (trigger_calls[0], duration_frames)
    assert len(trigger_calls) == 1
    assert trigger_calls[0] > 0
    assert len(meter_calls) == 1
    channel_view, sample_rate_hz = meter_calls[0]
    assert channel_view.ndim == 1
    assert channel_view.shape == (duration_frames,)
    assert channel_view.dtype == np.dtype(np.float32)
    assert channel_view.flags.writeable is False
    assert channel_view.base is not None
    assert sample_rate_hz == 48_000
    assert np.array_equal(
        channel_view,
        np.arange(duration_frames, dtype=np.float32) + 1_000,
    )
    assert type(measurement) is ResidentCalibrationMeasurement
    assert measurement == ResidentCalibrationMeasurement(
        generation,
        1,
        -17.25,
        duration_frames,
    )
    sibling_target = adapter.capture_target(
        ManagedHardwareId(2),
        startup.ready_streams[1].generation,
    )
    assert sibling_target.resident_hardware_id == "managed-input-2"
    assert len(backend.create_calls) == 2
    assert len(backend.handles["managed-input-2"].start_deadlines) == 1


class StaticCalibrationCompletion:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.wait_calls: list[float] = []

    def wait(self, timeout_seconds: float) -> object:
        self.wait_calls.append(timeout_seconds)
        return self.outcome


def synthetic_completed_outcome(
    binding: object,
    *,
    outcome_binding: object | None = None,
    audio_binding: object | None = None,
    hardware_id: str = "managed-input-1",
    sample_rate_hz: int = 48_000,
    frame_count: int = 100,
    channel_indices: tuple[int, ...] = (0, 1),
) -> CaptureOutcome:
    selected_outcome_binding = (
        binding if outcome_binding is None else outcome_binding
    )
    selected_audio_binding = (
        selected_outcome_binding
        if audio_binding is None
        else audio_binding
    )
    samples = np.zeros((100, 2), dtype=np.float32)
    samples.setflags(write=False)
    audio = object.__new__(CapturedAudio)
    object.__setattr__(audio, "binding", selected_audio_binding)
    object.__setattr__(audio, "run_id", "resident-calibration:1:1")
    object.__setattr__(audio, "hardware_id", hardware_id)
    object.__setattr__(audio, "trigger_monotonic_ns", 1)
    object.__setattr__(audio, "start_frame_cursor", 0)
    object.__setattr__(audio, "frame_count", frame_count)
    object.__setattr__(audio, "sample_rate_hz", sample_rate_hz)
    object.__setattr__(audio, "channel_indices", channel_indices)
    object.__setattr__(audio, "samples", samples)
    object.__setattr__(audio, "completion_monotonic_ns", 2)
    outcome = object.__new__(CaptureOutcome)
    object.__setattr__(
        outcome,
        "status",
        CaptureOutcomeStatus.COMPLETED,
    )
    object.__setattr__(outcome, "binding", selected_outcome_binding)
    object.__setattr__(outcome, "audio", audio)
    object.__setattr__(outcome, "error", None)
    return outcome


def opened_calibration_adapter(
    *,
    level_meter: object = None,
) -> tuple[
    ResidentManagedInputAdapter,
    FakeBackend,
    ResidentStreamGenerationStamp,
]:
    backend = FakeBackend()
    adapter = make_adapter(
        backend,
        level_meter=(
            (lambda _samples, _sample_rate_hz: -10.0)
            if level_meter is None
            else level_meter
        ),
        monotonic_ns=lambda: 123,
    )
    startup = adapter.open_startup_streams((endpoint(1),))
    return adapter, backend, startup.ready_streams[0].generation


@pytest.mark.parametrize("state_case", ["new", "not_ready", "stale"])
def test_calibration_rejects_invalid_target_before_reserve(
    monkeypatch: pytest.MonkeyPatch,
    state_case: str,
) -> None:
    reserve_calls: list[object] = []

    def forbidden_reserve(*args: object, **_kwargs: object) -> object:
        reserve_calls.append(args)
        raise AssertionError("invalid calibration must not reserve")

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        forbidden_reserve,
    )
    hardware_id = ManagedHardwareId(1)
    if state_case == "new":
        adapter = make_adapter()
        generation = ResidentStreamGenerationStamp(hardware_id, 1, 1)
    elif state_case == "not_ready":
        adapter = make_adapter()
        adapter.open_startup_streams(
            (endpoint(1, sample_rate_hz=48_000.5),)
        )
        generation = ResidentStreamGenerationStamp(hardware_id, 1, 1)
    else:
        adapter, _, ready_generation = opened_calibration_adapter()
        generation = ResidentStreamGenerationStamp(
            hardware_id,
            ready_generation.pool_epoch,
            ready_generation.stream_generation + 1,
        )

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        adapter.measure_calibration(
            hardware_id,
            generation,
            0,
            100,
        )

    assert reserve_calls == []


@pytest.mark.parametrize(
    ("channel_index", "duration_frames"),
    [(2, 100), (0, 101)],
)
def test_calibration_rejects_channel_or_duration_before_reserve(
    monkeypatch: pytest.MonkeyPatch,
    channel_index: int,
    duration_frames: int,
) -> None:
    reserve_calls: list[object] = []

    def forbidden_reserve(*args: object, **_kwargs: object) -> object:
        reserve_calls.append(args)
        raise AssertionError("invalid calibration must not reserve")

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        forbidden_reserve,
    )
    backend = FakeBackend()
    adapter = make_adapter(backend, max_capture_frames=100)
    startup = adapter.open_startup_streams((endpoint(1),))

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            startup.ready_streams[0].generation,
            channel_index,
            duration_frames,
        )

    assert reserve_calls == []


def test_calibration_rejects_computed_wait_over_timeout_before_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve_calls: list[object] = []

    def forbidden_reserve(*args: object, **_kwargs: object) -> object:
        reserve_calls.append(args)
        raise AssertionError("invalid wait must not reserve")

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        forbidden_reserve,
    )
    adapter = make_adapter(
        calibration_wait_margin_s=0.25,
        max_capture_frames=100,
    )
    startup = adapter.open_startup_streams((endpoint(1),))
    monkeypatch.setattr(adapter_module.threading, "TIMEOUT_MAX", 0.001)

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            startup.ready_streams[0].generation,
            0,
            1,
        )

    assert reserve_calls == []


def test_calibration_reserve_failure_has_no_start_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    calls: list[str] = []

    def fail_reserve(*_args: object, **_kwargs: object) -> object:
        calls.append("reserve")
        raise RuntimeError("reserve failed")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("forbidden")
        raise AssertionError("reserve failure must stop calibration")

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        fail_reserve,
    )
    monkeypatch.setattr(ResidentAudioPool, "start_capture", forbidden)
    monkeypatch.setattr(ResidentAudioPool, "cancel_capture", forbidden)

    with pytest.raises(RuntimeError, match="reserve failed"):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert calls == ["reserve"]


def test_calibration_start_failure_cancels_once_and_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    cancel_calls: list[object] = []
    start_calls = 0
    real_cancel = ResidentAudioPool.cancel_capture

    def fail_start(*_args: object, **_kwargs: object) -> object:
        nonlocal start_calls
        start_calls += 1
        raise RuntimeError("original start failure")

    def recording_cancel(
        pool: ResidentAudioPool,
        binding: object,
    ) -> object:
        cancel_calls.append(binding)
        return real_cancel(pool, binding)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        fail_start,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "cancel_capture",
        recording_cancel,
    )

    with pytest.raises(RuntimeError, match="original start failure"):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert start_calls == 1
    assert len(cancel_calls) == 1


def test_calibration_cancel_exception_is_not_retried_or_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    cancel_calls: list[object] = []

    def fail_start(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("original start failure")

    def fail_cancel(
        _pool: ResidentAudioPool,
        binding: object,
    ) -> object:
        cancel_calls.append(binding)
        raise RuntimeError("cancel failure")

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        fail_start,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "cancel_capture",
        fail_cancel,
    )

    with pytest.raises(RuntimeError, match="original start failure"):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert len(cancel_calls) == 1


def test_calibration_wait_timeout_cancels_once_without_second_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    completion = StaticCalibrationCompletion(None)
    cancel_calls: list[object] = []
    start_calls = 0
    real_cancel = ResidentAudioPool.cancel_capture

    def timeout_start(*_args: object, **_kwargs: object) -> object:
        nonlocal start_calls
        start_calls += 1
        return completion

    def recording_cancel(
        pool: ResidentAudioPool,
        binding: object,
    ) -> object:
        cancel_calls.append(binding)
        return real_cancel(pool, binding)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        timeout_start,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "cancel_capture",
        recording_cancel,
    )

    with pytest.raises(RuntimeError):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert start_calls == 1
    assert len(completion.wait_calls) == 1
    assert len(cancel_calls) == 1


@pytest.mark.parametrize(
    "invalid_kind",
    ["malformed", "mismatched_binding"],
)
def test_calibration_invalid_wait_result_settles_owned_binding(
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    target = adapter.capture_target(ManagedHardwareId(1), generation)
    foreign_binding = target.pool.reserve_capture(
        "foreign-terminal-binding",
        target.resident_hardware_id,
    )
    target.pool.cancel_capture(foreign_binding)
    owned_bindings: list[object] = []
    cancel_calls: list[object] = []
    real_cancel = ResidentAudioPool.cancel_capture

    def invalid_start(
        _pool: ResidentAudioPool,
        binding: object,
        **_kwargs: object,
    ) -> StaticCalibrationCompletion:
        owned_bindings.append(binding)
        outcome = (
            object()
            if invalid_kind == "malformed"
            else synthetic_completed_outcome(
                binding,
                outcome_binding=foreign_binding,
            )
        )
        return StaticCalibrationCompletion(outcome)

    def recording_cancel(
        pool: ResidentAudioPool,
        binding: object,
    ) -> object:
        cancel_calls.append(binding)
        return real_cancel(pool, binding)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        invalid_start,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "cancel_capture",
        recording_cancel,
    )

    with pytest.raises(
        RuntimeError,
        match="resident calibration capture failed",
    ):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert cancel_calls == owned_bindings
    assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
        0,
        False,
    )
    next_binding = target.pool.reserve_capture(
        f"after-{invalid_kind}",
        target.resident_hardware_id,
    )
    try:
        assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
            1,
            False,
        )
    finally:
        target.pool.cancel_capture(next_binding)


@pytest.mark.parametrize(
    "invalid_kind",
    ["malformed", "mismatched_binding"],
)
def test_calibration_invalid_wait_result_preserves_failure_if_cancel_fails(
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    target = adapter.capture_target(ManagedHardwareId(1), generation)
    foreign_binding = target.pool.reserve_capture(
        "foreign-cancel-failure-binding",
        target.resident_hardware_id,
    )
    target.pool.cancel_capture(foreign_binding)
    reserved_bindings: list[object] = []
    cancel_calls: list[object] = []
    real_cancel = ResidentAudioPool.cancel_capture

    def invalid_start(
        _pool: ResidentAudioPool,
        binding: object,
        **_kwargs: object,
    ) -> StaticCalibrationCompletion:
        reserved_bindings.append(binding)
        outcome = (
            object()
            if invalid_kind == "malformed"
            else synthetic_completed_outcome(
                binding,
                outcome_binding=foreign_binding,
            )
        )
        return StaticCalibrationCompletion(outcome)

    def fail_cancel(
        _pool: ResidentAudioPool,
        binding: object,
    ) -> object:
        cancel_calls.append(binding)
        raise RuntimeError("cancel failure must not replace validation")

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        invalid_start,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "cancel_capture",
        fail_cancel,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="resident calibration capture failed",
        ):
            adapter.measure_calibration(
                ManagedHardwareId(1),
                generation,
                0,
                100,
            )
        assert cancel_calls == reserved_bindings
    finally:
        monkeypatch.setattr(
            ResidentAudioPool,
            "cancel_capture",
            real_cancel,
        )
        for binding in reserved_bindings:
            real_cancel(target.pool, binding)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "status",
    [CaptureOutcomeStatus.CANCELLED, CaptureOutcomeStatus.FAILED],
)
def test_calibration_rejects_terminal_non_completed_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    status: CaptureOutcomeStatus,
) -> None:
    adapter, _, generation = opened_calibration_adapter()

    def terminal_start(
        _pool: ResidentAudioPool,
        binding: object,
        **_kwargs: object,
    ) -> StaticCalibrationCompletion:
        code = (
            ResidentErrorCode.CANCELLED
            if status is CaptureOutcomeStatus.CANCELLED
            else ResidentErrorCode.FAULT
        )
        outcome = CaptureOutcome(
            status,
            binding,  # type: ignore[arg-type]
            None,
            ResidentError(code, "terminal calibration outcome"),
        )
        return StaticCalibrationCompletion(outcome)

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        terminal_start,
    )

    with pytest.raises(RuntimeError):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )


@pytest.mark.parametrize(
    ("mismatch", "value"),
    [
        ("binding", object()),
        ("hardware_id", "managed-input-2"),
        ("sample_rate_hz", 44_100),
        ("frame_count", 99),
        ("channel_indices", (0,)),
    ],
)
def test_calibration_rejects_mismatched_completed_audio(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
    value: object,
) -> None:
    adapter, _, generation = opened_calibration_adapter()
    meter_calls = 0

    def forbidden_meter(
        _samples: np.ndarray,
        _sample_rate_hz: int,
    ) -> float:
        nonlocal meter_calls
        meter_calls += 1
        return 0.0

    adapter._level_meter = forbidden_meter

    def mismatched_start(
        _pool: ResidentAudioPool,
        binding: object,
        **_kwargs: object,
    ) -> StaticCalibrationCompletion:
        kwargs: dict[str, object] = {}
        if mismatch == "binding":
            kwargs["outcome_binding"] = value
        else:
            kwargs[mismatch] = value
        outcome = synthetic_completed_outcome(binding, **kwargs)
        return StaticCalibrationCompletion(outcome)

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        mismatched_start,
    )

    with pytest.raises(RuntimeError):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert meter_calls == 0


@pytest.mark.parametrize(
    "meter_result",
    [
        True,
        1,
        np.float32(1.0),
        math.nan,
        math.inf,
        -math.inf,
        RuntimeError("meter failed"),
    ],
)
def test_calibration_rejects_invalid_meter_result(
    monkeypatch: pytest.MonkeyPatch,
    meter_result: object,
) -> None:
    meter_calls = 0

    def meter(_samples: np.ndarray, _sample_rate_hz: int) -> object:
        nonlocal meter_calls
        meter_calls += 1
        if isinstance(meter_result, BaseException):
            raise meter_result
        return meter_result

    adapter, _, generation = opened_calibration_adapter(
        level_meter=meter,
    )

    def completed_start(
        _pool: ResidentAudioPool,
        binding: object,
        **_kwargs: object,
    ) -> StaticCalibrationCompletion:
        return StaticCalibrationCompletion(
            synthetic_completed_outcome(binding)
        )

    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        completed_start,
    )

    with pytest.raises((TypeError, ValueError, RuntimeError)):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )

    assert meter_calls == 1


def test_calibration_does_not_retain_mutable_audio_or_create_worker() -> None:
    adapter, _, _ = opened_calibration_adapter()

    assert not any(
        isinstance(value, np.ndarray) for value in vars(adapter).values()
    )
    assert not any(
        isinstance(value, threading.Thread)
        for value in vars(adapter).values()
    )


def test_capture_target_returns_exact_opened_pool_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = ResidentAudioPool.open
    opened_pools: list[ResidentAudioPool] = []

    def recording_open(
        cls: type[ResidentAudioPool],
        specs: tuple[object, ...],
        **kwargs: object,
    ) -> object:
        result = real_open(specs, **kwargs)
        opened_pools.append(result.pool)
        return result

    monkeypatch.setattr(
        ResidentAudioPool,
        "open",
        classmethod(recording_open),
    )
    adapter = make_adapter()
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation

    target = adapter.capture_target(ManagedHardwareId(1), generation)

    assert target.pool is opened_pools[0]
    assert target.resident_hardware_id == "managed-input-1"
    assert target.generation is generation


@pytest.mark.parametrize("failure_kind", ["unsupported", "startup_failed"])
def test_capture_target_rejects_non_ready_hardware(
    failure_kind: str,
) -> None:
    backend = (
        FakeBackend(
            create_errors={
                "managed-input-1": RuntimeError("startup failed")
            }
        )
        if failure_kind == "startup_failed"
        else FakeBackend()
    )
    original = (
        endpoint(1)
        if failure_kind == "startup_failed"
        else endpoint(1, sample_rate_hz=48_000.5)
    )
    adapter = make_adapter(backend)
    adapter.open_startup_streams((original,))
    claimed_generation = ResidentStreamGenerationStamp(
        hardware_id=original.hardware_id,
        pool_epoch=1,
        stream_generation=1,
    )

    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(original.hardware_id, claimed_generation)


def test_capture_target_rejects_stale_or_foreign_generation() -> None:
    adapter = make_adapter()
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation
    stale = ResidentStreamGenerationStamp(
        hardware_id=ManagedHardwareId(1),
        pool_epoch=1,
        stream_generation=2,
    )
    foreign = ResidentStreamGenerationStamp(
        hardware_id=ManagedHardwareId(2),
        pool_epoch=1,
        stream_generation=1,
    )

    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(ManagedHardwareId(1), stale)
    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(ManagedHardwareId(2), foreign)
    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(ManagedHardwareId(1), foreign)
    assert generation != stale


def test_capture_target_rejects_new_adapter() -> None:
    adapter = make_adapter()
    generation = ResidentStreamGenerationStamp(
        hardware_id=ManagedHardwareId(1),
        pool_epoch=1,
        stream_generation=1,
    )

    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(ManagedHardwareId(1), generation)


def test_capture_target_does_not_reserve_or_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter()
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation
    calls: list[str] = []

    def forbidden_reserve(*_args: object, **_kwargs: object) -> object:
        calls.append("reserve")
        raise AssertionError("capture_target must not reserve")

    def forbidden_start(*_args: object, **_kwargs: object) -> object:
        calls.append("start")
        raise AssertionError("capture_target must not start")

    monkeypatch.setattr(
        ResidentAudioPool,
        "reserve_capture",
        forbidden_reserve,
    )
    monkeypatch.setattr(
        ResidentAudioPool,
        "start_capture",
        forbidden_start,
    )

    target = adapter.capture_target(ManagedHardwareId(1), generation)

    assert target.resident_hardware_id == "managed-input-1"
    assert calls == []


@dataclass(frozen=True)
class FrozenRunBindingFacts:
    generation: ResidentStreamGenerationStamp
    binding: object


def test_capture_target_generation_stays_next_to_later_binding() -> None:
    adapter = make_adapter()
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation
    target = adapter.capture_target(ManagedHardwareId(1), generation)
    binding = target.pool.reserve_capture(
        "normal-run-1",
        target.resident_hardware_id,
    )
    try:
        facts = FrozenRunBindingFacts(target.generation, binding)

        assert facts.generation is generation
        assert facts.binding is binding
    finally:
        target.pool.cancel_capture(binding)


def test_capture_target_reuses_one_pool_for_two_later_run_bindings() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation
    first_target = adapter.capture_target(ManagedHardwareId(1), generation)
    second_target = adapter.capture_target(ManagedHardwareId(1), generation)
    first = first_target.pool.reserve_capture(
        "normal-run-1",
        first_target.resident_hardware_id,
    )
    second = second_target.pool.reserve_capture(
        "normal-run-2",
        second_target.resident_hardware_id,
    )
    try:
        assert first_target.pool is second_target.pool
        assert first.hardware_id == second.hardware_id == "managed-input-1"
        assert first.binding_id != second.binding_id
        assert len(backend.create_calls) == 1
        assert len(backend.handles["managed-input-1"].start_deadlines) == 1
    finally:
        first_target.pool.cancel_capture(first)
        second_target.pool.cancel_capture(second)


def close_statuses(
    results: tuple[ManagedInputCloseResult, ...],
) -> dict[int, ManagedInputCloseStatus]:
    return {
        item.hardware_id.value: item.status
        for item in results
    }


def shutdown_report(
    status: ShutdownStatus,
    deadline_monotonic_ns: int,
    *,
    unclosed_hardware_ids: tuple[str, ...] = (),
) -> ShutdownReport:
    if status is ShutdownStatus.CLOSED:
        finished_monotonic_ns = 11
        errors = ()
        operator_action = ""
    elif status is ShutdownStatus.IN_PROGRESS:
        finished_monotonic_ns = None
        errors = ()
        operator_action = "等待 owning shutdown caller 的最终报告"
    else:
        finished_monotonic_ns = 11
        errors = (
            ResidentError(
                ResidentErrorCode.NON_CLEAN,
                "free text must not affect managed close mapping",
            ),
        )
        operator_action = "不要做相关 maintenance；停止并重启进程"
    return ShutdownReport(
        status=status,
        started_monotonic_ns=10,
        finished_monotonic_ns=finished_monotonic_ns,
        unclosed_hardware_ids=unclosed_hardware_ids,
        unjoined_worker_ids=(),
        non_terminal_binding_ids=(),
        errors=errors,
        deadline_monotonic_ns=deadline_monotonic_ns,
        operator_action=operator_action,
    )


def test_close_contains_complete_frozen_startup_membership_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "MAX_ALLOCATABLE_FRAMES", 2_000)
    monkeypatch.setattr(adapter_module, "MAX_CAPTURE_BUFFER_BYTES", 400)
    backend = FakeBackend(
        create_errors={
            "managed-input-1": RuntimeError("startup fault"),
        },
    )
    adapter = make_adapter(
        backend,
        max_capture_frames=100,
        frames_per_buffer=1,
    )
    startup = adapter.open_startup_streams(
        (
            endpoint(6, sample_rate_hz=1.0, input_channels=1),
            endpoint(1, sample_rate_hz=1.0, input_channels=1),
            endpoint(2, sample_rate_hz=1.5, input_channels=1),
            endpoint(3, sample_rate_hz=1.0, input_channels=65),
            endpoint(4, sample_rate_hz=1_000.0, input_channels=1),
            endpoint(5, sample_rate_hz=1.0, input_channels=64),
        )
    )

    results = adapter.close_all(1.0)

    assert ready_ids(startup) == (6,)
    assert failure_codes(startup) == {
        1: "resident_open_failed",
        2: "unsupported_sample_rate",
        3: "unsupported_channel_count",
        4: "unsupported_ring_layout",
        5: "unsupported_capture_layout",
    }
    assert tuple(item.hardware_id.value for item in results) == (1, 2, 3, 4, 5, 6)
    assert close_statuses(results) == {
        1: ManagedInputCloseStatus.NOT_OPEN,
        2: ManagedInputCloseStatus.NOT_OPEN,
        3: ManagedInputCloseStatus.NOT_OPEN,
        4: ManagedInputCloseStatus.NOT_OPEN,
        5: ManagedInputCloseStatus.NOT_OPEN,
        6: ManagedInputCloseStatus.CLOSED,
    }


@pytest.mark.parametrize(
    ("status", "unclosed_ids", "expected"),
    [
        (
            ShutdownStatus.CLOSED,
            (),
            {
                1: ManagedInputCloseStatus.CLOSED,
                2: ManagedInputCloseStatus.NOT_OPEN,
                3: ManagedInputCloseStatus.NOT_OPEN,
            },
        ),
        (
            ShutdownStatus.NON_CLEAN,
            ("managed-input-1",),
            {
                1: ManagedInputCloseStatus.FAILED,
                2: ManagedInputCloseStatus.NOT_OPEN,
                3: ManagedInputCloseStatus.NOT_OPEN,
            },
        ),
        (
            ShutdownStatus.NON_CLEAN,
            (),
            {
                1: ManagedInputCloseStatus.FAILED,
                2: ManagedInputCloseStatus.NOT_OPEN,
                3: ManagedInputCloseStatus.NOT_OPEN,
            },
        ),
        (
            ShutdownStatus.IN_PROGRESS,
            (),
            {
                1: ManagedInputCloseStatus.FAILED,
                2: ManagedInputCloseStatus.NOT_OPEN,
                3: ManagedInputCloseStatus.NOT_OPEN,
            },
        ),
        (
            ShutdownStatus.NON_CLEAN,
            ("managed-input-2",),
            {
                1: ManagedInputCloseStatus.FAILED,
                2: ManagedInputCloseStatus.FAILED,
                3: ManagedInputCloseStatus.NOT_OPEN,
            },
        ),
    ],
)
def test_close_maps_exact_typed_shutdown_facts(
    monkeypatch: pytest.MonkeyPatch,
    status: ShutdownStatus,
    unclosed_ids: tuple[str, ...],
    expected: dict[int, ManagedInputCloseStatus],
) -> None:
    backend = FakeBackend(
        create_errors={
            "managed-input-2": RuntimeError("startup fault"),
        },
    )
    adapter = make_adapter(backend, monotonic_ns=lambda: 100)
    adapter.open_startup_streams(
        (
            endpoint(3, sample_rate_hz=48_000.5),
            endpoint(1),
            endpoint(2),
        )
    )
    shutdown_calls: list[int] = []

    def fixed_shutdown(
        _pool: ResidentAudioPool,
        *,
        deadline_monotonic_ns: int,
    ) -> ShutdownReport:
        shutdown_calls.append(deadline_monotonic_ns)
        return shutdown_report(
            status,
            deadline_monotonic_ns,
            unclosed_hardware_ids=unclosed_ids,
        )

    monkeypatch.setattr(ResidentAudioPool, "shutdown", fixed_shutdown)

    results = adapter.close_all(1.0)

    assert close_statuses(results) == expected
    assert shutdown_calls == [1_000_000_100]
    failed = [
        item
        for item in results
        if item.status is ManagedInputCloseStatus.FAILED
    ]
    for item in failed:
        assert item.error is not None
        assert item.error.code is ManagedInputErrorCode.RESIDENT_STREAM_FAILED
        assert item.error.stage == "resident_close"
        assert item.error.summary == (
            "Resident shutdown did not finish cleanly; "
            "stop and restart the process."
        )


@pytest.mark.parametrize("failure", [RuntimeError("do not parse me"), object()])
def test_close_shutdown_exception_or_invalid_result_fails_ready_only(
    monkeypatch: pytest.MonkeyPatch,
    failure: object,
) -> None:
    backend = FakeBackend(
        create_errors={
            "managed-input-2": RuntimeError("startup fault"),
        },
    )
    adapter = make_adapter(backend, monotonic_ns=lambda: 100)
    adapter.open_startup_streams(
        (
            endpoint(1),
            endpoint(2),
            endpoint(3, sample_rate_hz=48_000.5),
        )
    )

    def invalid_shutdown(
        _pool: ResidentAudioPool,
        *,
        deadline_monotonic_ns: int,
    ) -> object:
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(ResidentAudioPool, "shutdown", invalid_shutdown)

    results = adapter.close_all(1.0)

    assert close_statuses(results) == {
        1: ManagedInputCloseStatus.FAILED,
        2: ManagedInputCloseStatus.NOT_OPEN,
        3: ManagedInputCloseStatus.NOT_OPEN,
    }
    assert results[0].error is not None
    assert results[0].error.summary == (
        "Resident shutdown did not finish cleanly; "
        "stop and restart the process."
    )


def test_close_after_top_level_open_failure_maps_all_members_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(
        cls: type[ResidentAudioPool],
        specs: tuple[object, ...],
        **_kwargs: object,
    ) -> object:
        raise RuntimeError(f"top-level failure for {len(specs)} specs")

    clock_calls = 0

    def clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        return 100

    monkeypatch.setattr(
        ResidentAudioPool,
        "open",
        classmethod(fail_open),
    )
    adapter = make_adapter(monotonic_ns=clock)
    adapter.open_startup_streams((endpoint(2), endpoint(1)))

    results = adapter.close_all(1.0)

    assert close_statuses(results) == {
        1: ManagedInputCloseStatus.NOT_OPEN,
        2: ManagedInputCloseStatus.NOT_OPEN,
    }
    assert clock_calls == 0


@pytest.mark.parametrize(
    "invalid_timeout",
    [
        True,
        0,
        0.0,
        -1.0,
        math.nan,
        math.inf,
        -math.inf,
        60.000_000_1,
    ],
)
def test_close_rejects_invalid_timeout_before_clock_or_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timeout: object,
) -> None:
    clock_calls = 0
    shutdown_calls = 0

    def clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        return 100

    def forbidden_shutdown(*_args: object, **_kwargs: object) -> object:
        nonlocal shutdown_calls
        shutdown_calls += 1
        raise AssertionError("invalid timeout must stop before shutdown")

    adapter = make_adapter(monotonic_ns=clock)
    adapter.open_startup_streams((endpoint(1),))
    monkeypatch.setattr(
        ResidentAudioPool,
        "shutdown",
        forbidden_shutdown,
    )

    with pytest.raises((TypeError, ValueError)):
        adapter.close_all(invalid_timeout)  # type: ignore[arg-type]

    assert clock_calls == 0
    assert shutdown_calls == 0


@pytest.mark.parametrize(
    ("timeout_seconds", "now_ns", "expected_deadline"),
    [
        (0.000_000_001, 100, 101),
        (60.0, (2**63 - 1) - 60_000_000_000, 2**63 - 1),
    ],
)
def test_close_uses_one_clock_read_and_one_future_signed63_deadline(
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: float,
    now_ns: int,
    expected_deadline: int,
) -> None:
    clock_calls = 0
    deadlines: list[int] = []

    def clock() -> int:
        nonlocal clock_calls
        clock_calls += 1
        return now_ns

    def recording_shutdown(
        _pool: ResidentAudioPool,
        *,
        deadline_monotonic_ns: int,
    ) -> ShutdownReport:
        deadlines.append(deadline_monotonic_ns)
        return shutdown_report(
            ShutdownStatus.CLOSED,
            deadline_monotonic_ns,
        )

    adapter = make_adapter(monotonic_ns=clock)
    adapter.open_startup_streams((endpoint(1), endpoint(2)))
    monkeypatch.setattr(
        ResidentAudioPool,
        "shutdown",
        recording_shutdown,
    )

    results = adapter.close_all(timeout_seconds)

    assert clock_calls == 1
    assert deadlines == [expected_deadline]
    assert close_statuses(results) == {
        1: ManagedInputCloseStatus.CLOSED,
        2: ManagedInputCloseStatus.CLOSED,
    }


def test_repeated_close_returns_same_tuple_and_calls_shutdown_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(monotonic_ns=lambda: 100)
    startup = adapter.open_startup_streams((endpoint(1),))
    generation = startup.ready_streams[0].generation
    calls = 0

    def recording_shutdown(
        _pool: ResidentAudioPool,
        *,
        deadline_monotonic_ns: int,
    ) -> ShutdownReport:
        nonlocal calls
        calls += 1
        return shutdown_report(
            ShutdownStatus.CLOSED,
            deadline_monotonic_ns,
        )

    monkeypatch.setattr(
        ResidentAudioPool,
        "shutdown",
        recording_shutdown,
    )

    first = adapter.close_all(1.0)
    second = adapter.close_all(1.0)

    assert second is first
    assert calls == 1
    with pytest.raises((TypeError, ValueError)):
        adapter.open_startup_streams((endpoint(1),))
    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(ManagedHardwareId(1), generation)
    with pytest.raises((TypeError, ValueError)):
        adapter.measure_calibration(
            ManagedHardwareId(1),
            generation,
            0,
            100,
        )


def test_close_after_no_endpoints_returns_empty_tuple() -> None:
    adapter = make_adapter()
    adapter.open_startup_streams(())

    results = adapter.close_all(1.0)

    assert results == ()
    assert not hasattr(adapter, "close_one")
    assert not hasattr(adapter, "reopen")


def test_idle_fault_is_delayed_until_next_public_reserve() -> None:
    backend = FakeBackend()
    adapter = make_adapter(backend)
    startup = adapter.open_startup_streams((endpoint(1),))
    ready = startup.ready_streams[0]

    backend.handles["managed-input-1"].fault(
        summary="idle device detached",
    )

    assert startup.failures == ()
    target = adapter.capture_target(
        ready.endpoint.hardware_id,
        ready.generation,
    )
    with pytest.raises(ResidentRequestError) as raised:
        target.pool.reserve_capture(
            "normal-run-after-idle-fault",
            target.resident_hardware_id,
        )

    assert raised.value.error.code is ResidentErrorCode.NOT_READY
    assert len(backend.create_calls) == 1
    assert not hasattr(adapter, "reopen")
    assert not hasattr(adapter, "reconnect")


def test_six_ready_streams_share_one_adapter_pool() -> None:
    preexisting_thread_ids = {
        thread.ident for thread in threading.enumerate()
    }
    backend = FakeBackend()
    adapter = make_adapter(backend)
    startup = adapter.open_startup_streams(
        tuple(endpoint(value) for value in range(1, 7))
    )

    assert ready_ids(startup) == (1, 2, 3, 4, 5, 6)
    assert startup.failures == ()
    assert [spec.hardware_id for spec in backend.create_calls] == [
        f"managed-input-{value}" for value in range(1, 7)
    ]
    assert all(
        len(backend.handles[f"managed-input-{value}"].start_deadlines) == 1
        for value in range(1, 7)
    )
    assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
        0,
        False,
    )

    targets = tuple(
        adapter.capture_target(
            ready.endpoint.hardware_id,
            ready.generation,
        )
        for ready in startup.ready_streams
    )
    assert all(target.pool is targets[0].pool for target in targets)
    assert all(
        adapter.snapshot(ManagedHardwareId(value))
        == OfflineMaintenanceBlockerSnapshot(0, True)
        for value in range(1, 7)
    )

    closed = adapter.close_all(1.0)

    assert tuple(item.hardware_id.value for item in closed) == (
        1,
        2,
        3,
        4,
        5,
        6,
    )
    assert all(
        item.status is ManagedInputCloseStatus.CLOSED
        for item in closed
    )
    close_deadlines = {
        backend.handles[f"managed-input-{value}"].close_deadlines[0]
        for value in range(1, 7)
    }
    assert len(close_deadlines) == 1
    assert all(
        len(backend.handles[f"managed-input-{value}"].close_deadlines) == 1
        for value in range(1, 7)
    )
    assert not any(
        thread.is_alive()
        and thread.ident not in preexisting_thread_ids
        and thread.name.startswith("resident-audio-managed-input-")
        for thread in threading.enumerate()
    )


def test_one_startup_fault_isolated_with_complete_six_member_close() -> None:
    preexisting_thread_ids = {
        thread.ident for thread in threading.enumerate()
    }
    backend = FakeBackend(
        create_errors={
            "managed-input-3": RuntimeError("isolated startup fault"),
        },
    )
    adapter = make_adapter(backend)
    startup = adapter.open_startup_streams(
        tuple(endpoint(value) for value in range(1, 7))
    )

    assert ready_ids(startup) == (1, 2, 4, 5, 6)
    assert failure_codes(startup) == {3: "resident_open_failed"}
    assert len(backend.create_calls) == 6
    assert adapter.snapshot(None) == OfflineMaintenanceBlockerSnapshot(
        0,
        False,
    )
    targets = tuple(
        adapter.capture_target(
            ready.endpoint.hardware_id,
            ready.generation,
        )
        for ready in startup.ready_streams
    )
    assert all(target.pool is targets[0].pool for target in targets)
    assert all(
        adapter.snapshot(ready.endpoint.hardware_id)
        == OfflineMaintenanceBlockerSnapshot(0, True)
        for ready in startup.ready_streams
    )
    failed_generation = ResidentStreamGenerationStamp(
        hardware_id=ManagedHardwareId(3),
        pool_epoch=1,
        stream_generation=1,
    )
    with pytest.raises((TypeError, ValueError)):
        adapter.capture_target(
            ManagedHardwareId(3),
            failed_generation,
        )

    closed = adapter.close_all(1.0)

    assert tuple(item.hardware_id.value for item in closed) == (
        1,
        2,
        3,
        4,
        5,
        6,
    )
    assert close_statuses(closed) == {
        1: ManagedInputCloseStatus.CLOSED,
        2: ManagedInputCloseStatus.CLOSED,
        3: ManagedInputCloseStatus.NOT_OPEN,
        4: ManagedInputCloseStatus.CLOSED,
        5: ManagedInputCloseStatus.CLOSED,
        6: ManagedInputCloseStatus.CLOSED,
    }
    assert not any(
        thread.is_alive()
        and thread.ident not in preexisting_thread_ids
        and thread.name.startswith("resident-audio-managed-input-")
        for thread in threading.enumerate()
    )


def test_adapter_source_has_only_approved_leaf_dependencies_and_operations() -> None:
    source_path = Path(adapter_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported_names.extend(alias.name for alias in node.names)

    module_parts = {
        part.lower()
        for module in imported_modules
        for part in module.split(".")
    }
    assert not any(
        part.startswith("pyqt") or part.startswith("pyside")
        for part in module_parts
    )
    assert module_parts.isdisjoint(
        {
            "application",
            "container",
            "audio_recorder",
            "test_manager",
        }
    )
    assert set(imported_names).isdisjoint({"AudioRecorder", "TestManager"})

    external_private_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )
    }
    assert external_private_attributes == set()

    created_or_called = {
        node.func.id
        if isinstance(node.func, ast.Name)
        else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert created_or_called.isdisjoint(
        {
            "Thread",
            "ThreadPoolExecutor",
            "ProcessPoolExecutor",
            "Executor",
            "Queue",
            "SimpleQueue",
            "LifoQueue",
            "PriorityQueue",
        }
    )

    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        )
    }
    semantic_names = definitions | created_or_called | {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    forbidden_operations = {
        "retry",
        "backoff",
        "reopen",
        "reconnect",
        "poll",
    }
    assert all(
        forbidden_operations.isdisjoint(name.lower().split("_"))
        for name in semantic_names
    )
    assert all(
        "ledger" not in name.lower().split("_")
        for name in semantic_names
    )
    assert {
        name
        for name in definitions
        if "close" in name.lower().split("_")
    } == {"close_all"}
    accessed_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    assert accessed_attributes.isdisjoint(
        {
            "operator_action",
            "errors",
            "unjoined_worker_ids",
            "non_terminal_binding_ids",
        }
    )
