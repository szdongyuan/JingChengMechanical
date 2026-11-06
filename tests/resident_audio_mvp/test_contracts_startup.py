from __future__ import annotations

import dataclasses
import inspect
import math
import threading

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import pool as pool_module
from app.service.analysis.resident_audio_mvp import ring as ring_module
from app.service.analysis.resident_audio_mvp import stream as stream_module
from app.service.analysis.resident_audio_mvp.contracts import (
    FLOAT32_BYTES,
    MAX_ALLOCATABLE_FRAMES,
    MAX_BOUNDED_TEXT_CHARS,
    MAX_CAPTURE_BUFFER_BYTES,
    MAX_FRAMES_PER_BUFFER,
    MAX_INDEX,
    MAX_INPUT_CHANNELS,
    MAX_RING_BYTES_PER_STREAM,
    MAX_SAMPLE_RATE_HZ,
    MAX_TIMEOUT_SECONDS,
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    BackendFault,
    CancelResult,
    CancelStatus,
    CaptureBinding,
    CaptureCompletion,
    CaptureOutcome,
    CaptureOutcomeStatus,
    CapturedAudio,
    MaintenanceReadiness,
    MaintenanceStatus,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
    ShutdownReport,
    ShutdownStatus,
    StreamOpenResult,
    StreamOpenStatus,
)
from app.service.analysis.resident_audio_mvp.pool import ResidentAudioPool

from .fakes import FakeBackend, ManualMonotonicClock


def assert_invalid(call) -> None:
    with pytest.raises(ResidentRequestError) as raised:
        call()
    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert 0 < len(raised.value.error.summary) <= MAX_BOUNDED_TEXT_CHARS


def valid_spec(**overrides: object) -> ResidentStreamSpec:
    values: dict[str, object] = {
        "hardware_id": "mic-1",
        "device_index": 3,
        "device_name": "Exact USB Input",
        "hostapi_index": 2,
        "hostapi_name": "WASAPI",
        "sample_rate_hz": 48_000,
        "input_channel_count": 4,
        "capture_channel_indices": (0, 2),
        "sample_format": "float32",
        "frames_per_buffer": 480,
    }
    values.update(overrides)
    return ResidentStreamSpec(**values)


def test_frozen_safety_constants_are_exact() -> None:
    assert MAX_BOUNDED_TEXT_CHARS == 256
    assert MAX_INDEX == (1 << 31) - 1
    assert MAX_SAMPLE_RATE_HZ == 384_000
    assert MAX_INPUT_CHANNELS == 64
    assert MAX_FRAMES_PER_BUFFER == 1_048_576
    assert MAX_ALLOCATABLE_FRAMES == (1 << 31) - 1
    assert MAX_RING_BYTES_PER_STREAM == 256 * 1024 * 1024
    assert MAX_CAPTURE_BUFFER_BYTES == 256 * 1024 * 1024
    assert MAX_TIMEOUT_SECONDS == 300.0


def test_stream_spec_is_frozen_and_has_only_the_reviewed_fields() -> None:
    spec = valid_spec()
    assert dataclasses.is_dataclass(spec)
    assert [field.name for field in dataclasses.fields(spec)] == [
        "hardware_id",
        "device_index",
        "device_name",
        "hostapi_index",
        "hostapi_name",
        "sample_rate_hz",
        "input_channel_count",
        "capture_channel_indices",
        "sample_format",
        "frames_per_buffer",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.device_index = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("hardware_id", ""),
        ("hardware_id", "x" * (MAX_BOUNDED_TEXT_CHARS + 1)),
        ("device_name", ""),
        ("device_name", "x" * (MAX_BOUNDED_TEXT_CHARS + 1)),
        ("hostapi_name", ""),
        ("hostapi_name", "x" * (MAX_BOUNDED_TEXT_CHARS + 1)),
        ("device_index", True),
        ("device_index", -1),
        ("device_index", MAX_INDEX + 1),
        ("hostapi_index", True),
        ("hostapi_index", -1),
        ("hostapi_index", MAX_INDEX + 1),
        ("sample_rate_hz", True),
        ("sample_rate_hz", 0),
        ("sample_rate_hz", MAX_SAMPLE_RATE_HZ + 1),
        ("input_channel_count", True),
        ("input_channel_count", 0),
        ("input_channel_count", MAX_INPUT_CHANNELS + 1),
        ("frames_per_buffer", True),
        ("frames_per_buffer", 0),
        ("frames_per_buffer", MAX_FRAMES_PER_BUFFER + 1),
        ("sample_format", "int16"),
        ("capture_channel_indices", ()),
        ("capture_channel_indices", (0, 0)),
        ("capture_channel_indices", (1, 0)),
        ("capture_channel_indices", (0, 4)),
        ("capture_channel_indices", (True,)),
    ],
)
def test_stream_spec_validation_rejects_each_malformed_field(
    field_name: str, value: object
) -> None:
    assert_invalid(lambda: valid_spec(**{field_name: value}))


def test_stream_spec_accepts_every_exact_boundary() -> None:
    spec = valid_spec(
        hardware_id="h" * MAX_BOUNDED_TEXT_CHARS,
        device_index=MAX_INDEX,
        device_name="d" * MAX_BOUNDED_TEXT_CHARS,
        hostapi_index=MAX_INDEX,
        hostapi_name="a" * MAX_BOUNDED_TEXT_CHARS,
        sample_rate_hz=MAX_SAMPLE_RATE_HZ,
        input_channel_count=MAX_INPUT_CHANNELS,
        capture_channel_indices=(0, MAX_INPUT_CHANNELS - 1),
        frames_per_buffer=MAX_FRAMES_PER_BUFFER,
    )
    assert spec.sample_format == "float32"


class SpyCompletionEvent:
    def __init__(self) -> None:
        self.wait_calls: list[float | None] = []
        self.set_calls = 0

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        return False

    def is_set(self) -> bool:
        return False

    def set(self) -> None:
        self.set_calls += 1


@pytest.mark.parametrize(
    "timeout", [False, "0", -1.0, math.nan, math.inf, -math.inf]
)
def test_completion_wait_rejects_malformed_timeout_without_state_change(
    timeout: object,
) -> None:
    completion = CaptureCompletion()
    event = SpyCompletionEvent()
    completion._event = event  # type: ignore[assignment]

    assert_invalid(lambda: completion.wait(timeout))  # type: ignore[arg-type]

    assert event.wait_calls == []
    assert event.set_calls == 0
    assert completion._outcome is None


def test_completion_wait_passes_exact_threading_timeout_max_unchanged() -> None:
    completion = CaptureCompletion()
    event = SpyCompletionEvent()
    completion._event = event  # type: ignore[assignment]

    assert completion.wait(threading.TIMEOUT_MAX) is None

    assert event.wait_calls == [threading.TIMEOUT_MAX]
    assert event.set_calls == 0
    assert completion._outcome is None


OVER_MAX_TIMEOUTS = [
    pytest.param(
        math.nextafter(threading.TIMEOUT_MAX, math.inf),
        id="next-representable-float",
    )
]
timeout_max_plus_one = threading.TIMEOUT_MAX + 1.0
if timeout_max_plus_one != threading.TIMEOUT_MAX:
    OVER_MAX_TIMEOUTS.append(
        pytest.param(timeout_max_plus_one, id="timeout-max-plus-one")
    )


@pytest.mark.parametrize("timeout", OVER_MAX_TIMEOUTS)
def test_completion_wait_rejects_over_threading_timeout_max_before_event_wait(
    timeout: float,
) -> None:
    completion = CaptureCompletion()
    event = SpyCompletionEvent()
    completion._event = event  # type: ignore[assignment]

    assert_invalid(lambda: completion.wait(timeout))

    assert event.wait_calls == []
    assert event.set_calls == 0
    assert completion._outcome is None


class ExplodingFloat(float):
    def __float__(self) -> float:
        raise ValueError("conversion failed")


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(10**10_000, id="huge-int"),
        pytest.param(ExplodingFloat(1.0), id="failing-float-subclass"),
    ],
)
def test_completion_wait_contains_numeric_conversion_failures(
    timeout: object,
) -> None:
    completion = CaptureCompletion()

    assert_invalid(lambda: completion.wait(timeout))  # type: ignore[arg-type]
    assert completion.result_if_ready() is None


def test_pending_completion_zero_wait_and_poll_are_nonblocking() -> None:
    completion = CaptureCompletion()
    assert completion.wait(0) is None
    assert completion.result_if_ready() is None


def test_error_and_backend_contract_invariants() -> None:
    error = ResidentError(ResidentErrorCode.FAULT, "mic-1 failed")
    assert error == ResidentError(ResidentErrorCode.FAULT, "mic-1 failed")
    assert dataclasses.is_dataclass(error)
    with pytest.raises(dataclasses.FrozenInstanceError):
        error.summary = "changed"  # type: ignore[misc]

    assert BackendCloseResult(BackendCloseStatus.CLOSED).error is None
    non_clean = ResidentError(ResidentErrorCode.NON_CLEAN, "close not confirmed")
    assert (
        BackendCloseResult(BackendCloseStatus.NOT_CONFIRMED, non_clean).error
        is non_clean
    )
    assert BackendCallbackStatus.OK.value == "OK"
    assert BackendCallbackDirective.STOP.value == "STOP"
    assert BackendFault(
        BackendCallbackStatus.DEVICE_FAULT, "device disconnected"
    ).summary == "device disconnected"

    assert_invalid(
        lambda: BackendCloseResult(
            BackendCloseStatus.CLOSED,
            ResidentError(ResidentErrorCode.NON_CLEAN, "unexpected"),
        )
    )
    assert_invalid(lambda: BackendCloseResult(BackendCloseStatus.NOT_CONFIRMED))
    assert_invalid(
        lambda: BackendFault(BackendCallbackStatus.OK, "not a fault status")
    )


@pytest.mark.parametrize(
    "status",
    [
        "INPUT_OVERFLOW",
        "DEVICE_FAULT",
        "BACKEND_FAULT",
        "OK",
        True,
        1,
        object(),
    ],
)
def test_backend_fault_requires_exact_fatal_status_instance(status: object) -> None:
    assert_invalid(
        lambda: BackendFault(status, "backend fault")  # type: ignore[arg-type]
    )


def shutdown_report(
    status: ShutdownStatus,
    **overrides: object,
) -> ShutdownReport:
    values: dict[str, object] = {
        "status": status,
        "started_monotonic_ns": 10,
        "finished_monotonic_ns": 11,
        "unclosed_hardware_ids": (),
        "unjoined_worker_ids": (),
        "non_terminal_binding_ids": (),
        "errors": (),
        "deadline_monotonic_ns": 20,
        "operator_action": "",
    }
    values.update(overrides)
    return ShutdownReport(**values)


def test_shutdown_report_accepts_only_exact_valid_status_shapes() -> None:
    closed = shutdown_report(ShutdownStatus.CLOSED)
    assert closed.operator_action == ""

    in_progress = shutdown_report(
        ShutdownStatus.IN_PROGRESS,
        finished_monotonic_ns=None,
        unclosed_hardware_ids=("mic-1",),
        operator_action="等待 owning shutdown caller 的最终报告",
    )
    assert in_progress.finished_monotonic_ns is None

    non_clean = shutdown_report(
        ShutdownStatus.NON_CLEAN,
        unclosed_hardware_ids=("mic-1",),
        errors=(ResidentError(ResidentErrorCode.NON_CLEAN, "not closed"),),
        operator_action="不要做相关 maintenance；停止并重启进程",
    )
    assert non_clean.status is ShutdownStatus.NON_CLEAN


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        (ShutdownStatus.CLOSED, {"finished_monotonic_ns": None}),
        (ShutdownStatus.CLOSED, {"unclosed_hardware_ids": ("mic-1",)}),
        (ShutdownStatus.CLOSED, {"unjoined_worker_ids": ("worker-1",)}),
        (ShutdownStatus.CLOSED, {"non_terminal_binding_ids": ("binding-1",)}),
        (
            ShutdownStatus.CLOSED,
            {"errors": (ResidentError(ResidentErrorCode.FAULT, "fault"),)},
        ),
        (ShutdownStatus.CLOSED, {"operator_action": "none"}),
        (
            ShutdownStatus.IN_PROGRESS,
            {
                "finished_monotonic_ns": None,
                "operator_action": "wait",
            },
        ),
        (
            ShutdownStatus.IN_PROGRESS,
            {
                "finished_monotonic_ns": 11,
                "operator_action": "等待 owning shutdown caller 的最终报告",
            },
        ),
        (
            ShutdownStatus.NON_CLEAN,
            {"operator_action": "不要做相关 maintenance；停止并重启进程"},
        ),
        (
            ShutdownStatus.NON_CLEAN,
            {
                "unclosed_hardware_ids": ("mic-1",),
                "operator_action": "restart",
            },
        ),
    ],
)
def test_shutdown_report_rejects_inconsistent_status_shapes(
    status: ShutdownStatus,
    overrides: dict[str, object],
) -> None:
    assert_invalid(lambda: shutdown_report(status, **overrides))


def test_result_value_invariants_reject_invalid_combinations() -> None:
    fault = ResidentError(ResidentErrorCode.FAULT, "fault")
    non_clean = ResidentError(ResidentErrorCode.NON_CLEAN, "unclean")

    assert StreamOpenResult("mic-1", StreamOpenStatus.READY).error is None
    assert StreamOpenResult("mic-1", StreamOpenStatus.FAULTED, fault).error is fault
    assert_invalid(
        lambda: StreamOpenResult("mic-1", StreamOpenStatus.READY, fault)
    )
    assert_invalid(
        lambda: StreamOpenResult("mic-1", StreamOpenStatus.FAULTED)
    )

    assert_invalid(
        lambda: CancelResult(CancelStatus.INVALID, outcome=None, error=None)
    )
    assert_invalid(
        lambda: MaintenanceReadiness(
            MaintenanceStatus.READY,
            active_binding_ids=("b-1",),
            owned_hardware_ids=(),
            reason="ready",
        )
    )
    assert_invalid(
        lambda: ShutdownReport(
            status=ShutdownStatus.IN_PROGRESS,
            started_monotonic_ns=10,
            finished_monotonic_ns=11,
            unclosed_hardware_ids=(),
            unjoined_worker_ids=(),
            non_terminal_binding_ids=(),
            errors=(),
            deadline_monotonic_ns=20,
            operator_action="wait",
        )
    )
    assert_invalid(
        lambda: ShutdownReport(
            status=ShutdownStatus.NON_CLEAN,
            started_monotonic_ns=10,
            finished_monotonic_ns=11,
            unclosed_hardware_ids=("mic-1",),
            unjoined_worker_ids=(),
            non_terminal_binding_ids=(),
            errors=(non_clean,),
            deadline_monotonic_ns=20,
            operator_action="",
        )
    )


def test_capture_outcome_requires_exact_status_payload_pair() -> None:
    # CaptureBinding is intentionally pool-created, so the valid construction cases
    # are covered through reserve/cancel below. These malformed cases must fail before
    # the binding can be observed.
    assert_invalid(
        lambda: CaptureOutcome(
            status=CaptureOutcomeStatus.CANCELLED,
            binding=None,  # type: ignore[arg-type]
            audio=None,
            error=ResidentError(ResidentErrorCode.CANCELLED, "cancelled"),
        )
    )


def fabricated_binding(
    *,
    binding_id: str,
    run_id: str,
    hardware_id: str,
    owner_nonce: object,
    completion: CaptureCompletion | None = None,
) -> CaptureBinding:
    binding = object.__new__(CaptureBinding)
    object.__setattr__(binding, "binding_id", binding_id)
    object.__setattr__(binding, "run_id", run_id)
    object.__setattr__(binding, "hardware_id", hardware_id)
    object.__setattr__(binding, "_owner_nonce", owner_nonce)
    object.__setattr__(
        binding,
        "_completion",
        completion if completion is not None else CaptureCompletion(),
    )
    return binding


def audio_binding() -> CaptureBinding:
    return fabricated_binding(
        binding_id="binding-audio",
        run_id="run-audio",
        hardware_id="mic-1",
        owner_nonce=object(),
    )


def captured_audio(
    *,
    channel_indices: tuple[object, ...] = (0, 1),
    samples: np.ndarray | None = None,
) -> CapturedAudio:
    binding = audio_binding()
    if samples is None:
        samples = np.zeros((2, len(channel_indices)), dtype=np.float32)
        samples.setflags(write=False)
    return CapturedAudio(
        binding=binding,
        run_id=binding.run_id,
        hardware_id=binding.hardware_id,
        trigger_monotonic_ns=1,
        start_frame_cursor=0,
        frame_count=2,
        sample_rate_hz=48_000,
        channel_indices=channel_indices,  # type: ignore[arg-type]
        samples=samples,
        completion_monotonic_ns=2,
    )


def test_captured_audio_owns_exact_immutable_bytes_backed_samples() -> None:
    audio = captured_audio()

    assert type(audio.samples.base) is bytes
    assert not audio.samples.flags.owndata
    assert not audio.samples.flags.writeable


def test_captured_audio_handoff_breaks_source_alias_and_is_physically_read_only() -> None:
    source = np.arange(4, dtype=np.float32).reshape(2, 2).copy()
    source.setflags(write=False)

    audio = captured_audio(samples=source)

    source.setflags(write=True)
    source[:] = -1
    np.testing.assert_array_equal(
        audio.samples,
        np.arange(4, dtype=np.float32).reshape(2, 2),
    )
    with pytest.raises(ValueError):
        audio.samples.setflags(write=True)


@pytest.mark.parametrize(
    "channel_indices",
    [
        (True,),
        (-1,),
        (0.0,),
        ("0",),
        (MAX_INPUT_CHANNELS,),
        (0, 0),
        (1, 0),
    ],
)
def test_captured_audio_rejects_invalid_channel_indices(
    channel_indices: tuple[object, ...],
) -> None:
    assert_invalid(lambda: captured_audio(channel_indices=channel_indices))


def test_captured_audio_rejects_read_only_view_over_writable_storage() -> None:
    writable_owner = np.zeros((2, 2), dtype=np.float32)
    read_only_view = writable_owner.view()
    read_only_view.setflags(write=False)

    assert read_only_view.base is writable_owner
    assert not read_only_view.flags.owndata
    assert_invalid(lambda: captured_audio(samples=read_only_view))


def malformed_bindings() -> tuple[CaptureBinding, CaptureBinding]:
    uninitialized = object.__new__(CaptureBinding)
    malformed = fabricated_binding(
        binding_id="",
        run_id="run-audio",
        hardware_id="mic-1",
        owner_nonce=object(),
    )
    return uninitialized, malformed


@pytest.mark.parametrize("binding", malformed_bindings())
def test_captured_audio_rejects_malformed_binding_without_attribute_leak(
    binding: CaptureBinding,
) -> None:
    samples = np.zeros((2, 1), dtype=np.float32)
    samples.setflags(write=False)

    assert_invalid(
        lambda: CapturedAudio(
            binding=binding,
            run_id="run-audio",
            hardware_id="mic-1",
            trigger_monotonic_ns=1,
            start_frame_cursor=0,
            frame_count=2,
            sample_rate_hz=48_000,
            channel_indices=(0,),
            samples=samples,
            completion_monotonic_ns=2,
        )
    )


@pytest.mark.parametrize("binding", malformed_bindings())
def test_capture_outcome_rejects_malformed_binding(
    binding: CaptureBinding,
) -> None:
    assert_invalid(
        lambda: CaptureOutcome(
            status=CaptureOutcomeStatus.CANCELLED,
            binding=binding,
            audio=None,
            error=ResidentError(ResidentErrorCode.CANCELLED, "cancelled"),
        )
    )


def test_capture_binding_cannot_be_constructed_directly() -> None:
    with pytest.raises(TypeError):
        CaptureBinding()
    assert not hasattr(CaptureBinding, "_create")
    assert not hasattr(CaptureCompletion, "_store_outcome")
    assert not hasattr(CaptureCompletion, "_publish")


def open_pool(
    backend: FakeBackend,
    specs: list[ResidentStreamSpec],
    *,
    max_capture_frames: int = 48_000,
    startup_stream_timeout_s: float = 0.01,
    fault_close_timeout_s: float = 0.02,
    shutdown_timeout_s: float = 0.03,
):
    return ResidentAudioPool.open(
        specs,
        backend=backend,
        max_capture_frames=max_capture_frames,
        startup_stream_timeout_s=startup_stream_timeout_s,
        fault_close_timeout_s=fault_close_timeout_s,
        shutdown_timeout_s=shutdown_timeout_s,
    )


def slot_captured_by(callback):
    bound_slot = getattr(callback, "__self__", None)
    if bound_slot is not None:
        return bound_slot
    nonlocals = inspect.getclosurevars(callback).nonlocals
    return next(
        value
        for value in nonlocals.values()
        if isinstance(value, stream_module._ResidentStreamSlot)
    )


@pytest.mark.parametrize("count", [0, 1, 6])
def test_open_accepts_zero_one_and_six_distinct_specs(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(clock=clock)
    specs = [valid_spec(hardware_id=f"mic-{index}") for index in range(count)]

    opened = open_pool(backend, specs)

    assert tuple(item.hardware_id for item in opened.streams) == tuple(
        sorted(spec.hardware_id for spec in specs)
    )
    assert opened.ready_count == count


def test_equal_duplicate_specs_are_deduplicated_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(clock=clock)
    spec = valid_spec()

    opened = open_pool(backend, [spec, spec])

    assert len(opened.streams) == 1
    assert backend.create_calls == [spec]


def test_conflicting_duplicate_rejects_before_backend_or_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    allocations: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        ring_module.np,
        "empty",
        lambda *args, **kwargs: allocations.append(args) or np.zeros(*args, **kwargs),
    )

    with pytest.raises(ResidentRequestError) as raised:
        open_pool(
            backend,
            [
                valid_spec(hardware_id="mic-1", device_index=3),
                valid_spec(hardware_id="mic-1", device_index=4),
            ],
        )

    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert "device_index" in raised.value.error.summary
    assert "mic-1" in raised.value.error.summary
    assert backend.create_calls == []
    assert allocations == []


def test_seventh_distinct_hardware_rejects_before_backend_or_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    allocations: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        ring_module.np,
        "empty",
        lambda *args, **kwargs: allocations.append(args) or np.zeros(*args, **kwargs),
    )
    specs = [valid_spec(hardware_id=f"mic-{index}") for index in range(7)]

    with pytest.raises(ResidentRequestError) as raised:
        open_pool(backend, specs)

    assert raised.value.error.code is ResidentErrorCode.CAPACITY
    assert len(raised.value.error.summary) <= MAX_BOUNDED_TEXT_CHARS
    assert backend.create_calls == []
    assert allocations == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_capture_frames": False},
        {"max_capture_frames": 0},
        {"max_capture_frames": MAX_ALLOCATABLE_FRAMES + 1},
        {"startup_stream_timeout_s": False},
        {"startup_stream_timeout_s": 0},
        {"startup_stream_timeout_s": MAX_TIMEOUT_SECONDS + 0.001},
        {"startup_stream_timeout_s": math.inf},
        {"fault_close_timeout_s": 0},
        {"fault_close_timeout_s": math.nan},
        {"shutdown_timeout_s": -1},
        {"shutdown_timeout_s": "1"},
    ],
)
def test_open_limit_validation_precedes_backend_and_allocation(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    backend = FakeBackend()
    allocations: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        ring_module.np,
        "empty",
        lambda *args, **kwargs: allocations.append(args) or np.zeros(*args, **kwargs),
    )
    arguments = {
        "max_capture_frames": 48_000,
        "startup_stream_timeout_s": 0.01,
        "fault_close_timeout_s": 0.02,
        "shutdown_timeout_s": 0.03,
    }
    arguments.update(overrides)

    assert_invalid(
        lambda: ResidentAudioPool.open(
            [valid_spec()],
            backend=backend,
            **arguments,
        )
    )
    assert backend.create_calls == []
    assert allocations == []


@pytest.mark.parametrize(
    "field_name",
    [
        "startup_stream_timeout_s",
        "fault_close_timeout_s",
        "shutdown_timeout_s",
    ],
)
@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param(10**10_000, id="huge-int"),
        pytest.param(ExplodingFloat(1.0), id="failing-float-subclass"),
    ],
)
def test_open_contains_timeout_conversion_failures_before_side_effects(
    field_name: str,
    timeout: object,
) -> None:
    backend = FakeBackend()
    arguments = {
        "startup_stream_timeout_s": 0.01,
        "fault_close_timeout_s": 0.02,
        "shutdown_timeout_s": 0.03,
    }
    arguments[field_name] = timeout

    assert_invalid(
        lambda: ResidentAudioPool.open(
            [valid_spec()],
            backend=backend,
            max_capture_frames=48_000,
            **arguments,
        )
    )
    assert backend.create_calls == []
    assert backend.handles == {}


def test_exact_ring_and_capture_byte_boundaries_validate_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    allocations: list[tuple[object, ...]] = []
    real_empty = np.empty

    def recording_empty(*args, **kwargs):
        allocations.append(args)
        return real_empty(*args, **kwargs)

    monkeypatch.setattr(ring_module.np, "empty", recording_empty)
    channels = tuple(range(MAX_INPUT_CHANNELS))
    exact_ring = valid_spec(
        sample_rate_hz=1,
        input_channel_count=MAX_INPUT_CHANNELS,
        capture_channel_indices=channels,
        frames_per_buffer=1_048_574,
    )
    too_large_ring = valid_spec(
        sample_rate_hz=1,
        input_channel_count=MAX_INPUT_CHANNELS,
        capture_channel_indices=channels,
        frames_per_buffer=1_048_575,
    )

    assert_invalid(lambda: open_pool(backend, [too_large_ring]))
    assert backend.create_calls == []
    assert allocations == []

    assert_invalid(
        lambda: open_pool(
            backend,
            [exact_ring],
            max_capture_frames=1_048_577,
        )
    )
    assert backend.create_calls == []
    assert allocations == []


def test_exact_global_timeout_and_frame_boundaries_are_accepted() -> None:
    opened = ResidentAudioPool.open(
        [],
        backend=FakeBackend(),
        max_capture_frames=MAX_ALLOCATABLE_FRAMES,
        startup_stream_timeout_s=MAX_TIMEOUT_SECONDS,
        fault_close_timeout_s=MAX_TIMEOUT_SECONDS,
        shutdown_timeout_s=MAX_TIMEOUT_SECONDS,
    )

    assert opened.streams == ()
    assert opened.ready_count == 0


def test_exact_capture_byte_boundary_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    channels = tuple(range(MAX_INPUT_CHANNELS))
    backend = FakeBackend(clock=clock)

    opened = open_pool(
        backend,
        [
            valid_spec(
                sample_rate_hz=1,
                input_channel_count=MAX_INPUT_CHANNELS,
                capture_channel_indices=channels,
                frames_per_buffer=1,
            )
        ],
        max_capture_frames=MAX_CAPTURE_BUFFER_BYTES
        // (MAX_INPUT_CHANNELS * np.dtype(np.float32).itemsize),
    )

    assert opened.ready_count == 1


def test_exact_ring_byte_boundary_is_accepted_without_real_large_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()

    class SizingRing:
        def __init__(self, capacity_frames: int, channel_count: int) -> None:
            self.capacity_frames = capacity_frames
            self.channel_count = channel_count
            self.write_cursor = 0

        def append(self, samples) -> None:
            self.write_cursor += samples.shape[0]

    def return_after_deadline(handle) -> None:
        clock.advance_seconds(0.01)

    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    monkeypatch.setattr(stream_module, "_ResidentRing", SizingRing)
    channels = tuple(range(MAX_INPUT_CHANNELS))
    exact_ring_frames = MAX_RING_BYTES_PER_STREAM // (
        len(channels) * FLOAT32_BYTES
    )
    spec = valid_spec(
        sample_rate_hz=1,
        input_channel_count=MAX_INPUT_CHANNELS,
        capture_channel_indices=channels,
        frames_per_buffer=exact_ring_frames - 10,
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": return_after_deadline},
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [spec])

    assert backend.create_calls == [spec]
    assert opened.streams[0].status is StreamOpenStatus.FAULTED


def test_create_receives_exact_spec_and_start_sees_published_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    observations: list[tuple[bool, bool, bool, bool]] = []

    def inspect_publication(handle) -> None:
        slot = slot_captured_by(handle.callback)
        observations.append(
            (
                slot._handle is handle,
                slot._first_block_event is not None,
                slot._work_event is not None,
                slot._drain_owner is not None,
            )
        )
        handle.emit_valid()

    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": inspect_publication},
        emit_valid_by_default=False,
    )
    spec = valid_spec()

    opened = open_pool(backend, [spec])

    assert backend.create_calls == [spec]
    assert observations == [(True, True, True, True)]
    assert opened.streams[0].status is StreamOpenStatus.READY


def test_startup_publication_order_is_create_then_handle_then_drain_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    observations: list[tuple[str, bool, bool, bool, bool, bool]] = []

    class InspectingBackend(FakeBackend):
        def create_input(self, spec, callback, on_fault):
            slot = slot_captured_by(callback)
            observations.append(
                (
                    "create",
                    slot._ring is not None,
                    slot._first_block_event is not None,
                    slot._handle is None,
                    slot._work_event is None,
                    slot._drain_owner is None,
                )
            )
            return super().create_input(spec, callback, on_fault)

    def inspect_start(handle) -> None:
        slot = slot_captured_by(handle.callback)
        observations.append(
            (
                "start",
                slot._ring is not None,
                slot._first_block_event is not None,
                slot._handle is handle,
                slot._work_event is not None,
                slot._drain_owner is not None,
            )
        )
        handle.emit_valid()

    backend = InspectingBackend(
        clock=clock,
        start_actions={"mic-1": inspect_start},
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [valid_spec()])

    assert observations == [
        ("create", True, True, True, True, True),
        ("start", True, True, True, True, True),
    ]
    assert opened.ready_count == 1


def test_noncurrent_generation_callback_and_fault_are_stale_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = ready_pool(monkeypatch)
    slot = pool._slots["mic-1"]
    handle = backend.handles["mic-1"]
    reports_before = tuple(
        (hardware_id, candidate.ready)
        for hardware_id, candidate in sorted(pool._slots.items())
    )
    cursor_before = slot._ring.write_cursor
    slot._work_event.clear()
    with slot._lock:
        slot._generation = object()

    directive = handle.emit_valid()
    handle.fault(summary="late retired generation fault")

    assert directive is BackendCallbackDirective.STOP
    assert slot._ring.write_cursor == cursor_before
    assert not slot._work_event.is_set()
    assert slot._fault is None
    assert slot.ready
    assert tuple(
        (hardware_id, candidate.ready)
        for hardware_id, candidate in sorted(pool._slots.items())
    ) == reports_before
    assert slot._dropped_callback_count == 2


def test_startup_fault_invalidates_generation_and_late_entries_only_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={
            "mic-1": lambda handle: handle.emit_status(
                BackendCallbackStatus.DEVICE_FAULT
            )
        },
        emit_valid_by_default=False,
    )
    opened = open_pool(backend, [valid_spec()])
    slot = opened.pool._slots["mic-1"]
    handle = backend.handles["mic-1"]
    winner = slot._fault
    slot._work_event.clear()

    directive = handle.emit_valid()
    handle.fault(summary="late fault must not replace winner")

    assert opened.streams[0].status is StreamOpenStatus.FAULTED
    assert slot._generation is None
    assert directive is BackendCallbackDirective.STOP
    assert slot._ring.write_cursor == 0
    assert not slot._work_event.is_set()
    assert slot._fault is winner
    assert slot._dropped_callback_count == 2

    slot._dropped_callback_count = (1 << 63) - 1
    handle.emit_valid()
    handle.fault()
    assert slot._dropped_callback_count == (1 << 63) - 1


def test_ready_stream_contains_throwing_malformed_callback_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ThrowingStatus:
        def __init__(self) -> None:
            self.value_accesses = 0

        @property
        def value(self) -> str:
            self.value_accesses += 1
            raise RuntimeError("status value conversion failed")

    pool, backend = ready_pool(monkeypatch)
    slot = pool._slots["mic-1"]
    handle = backend.handles["mic-1"]
    status = ThrowingStatus()
    samples = np.zeros(
        (
            handle.spec.frames_per_buffer,
            handle.spec.input_channel_count,
        ),
        dtype=np.float32,
    )
    cursor_before = slot._ring.write_cursor
    slot._work_event.clear()
    backend.clock.advance_ns(123)

    directive = handle.emit(samples, status=status)  # type: ignore[arg-type]

    assert directive is BackendCallbackDirective.STOP
    assert status.value_accesses == 0
    assert slot._ring.write_cursor == cursor_before
    assert not slot._work_event.is_set()
    assert slot._generation is None
    assert slot._fault is not None
    assert slot._fault.code is ResidentErrorCode.FAULT
    assert slot._fault_monotonic_ns == backend.clock()
    winner = slot._fault

    assert (
        handle.emit(samples, status=status)  # type: ignore[arg-type]
        is BackendCallbackDirective.STOP
    )
    assert slot._fault is winner
    assert slot._dropped_callback_count == 1


@pytest.mark.parametrize(
    ("callback_monotonic_ns", "status"),
    [
        (10**30, BackendCallbackStatus.DEVICE_FAULT),
        (-1, BackendCallbackStatus.OK),
        (True, BackendCallbackStatus.OK),
        (10**30, BackendCallbackStatus.OK),
    ],
)
def test_callback_timestamp_cannot_control_startup_cleanup_deadline(
    monkeypatch: pytest.MonkeyPatch,
    callback_monotonic_ns: object,
    status: BackendCallbackStatus,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )

    def fail_from_callback(handle) -> None:
        samples = np.zeros(
            (
                handle.spec.frames_per_buffer,
                handle.spec.input_channel_count,
            ),
            dtype=np.float32,
        )
        handle.emit(
            samples,
            callback_monotonic_ns=callback_monotonic_ns,
            status=status,
        )
        clock.advance_ns(777)
        handle.fault(summary="later fault cannot replace first receipt")

    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fail_from_callback},
        emit_valid_by_default=False,
    )

    opened = open_pool(
        backend,
        [valid_spec()],
        shutdown_timeout_s=0.03,
    )

    assert opened.streams[0].status is StreamOpenStatus.FAULTED
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [1_030_000_000]


def test_on_fault_freezes_trusted_receipt_time_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )

    def fault_then_advance(handle) -> None:
        clock.advance_ns(123)
        handle.fault(summary="first fault")
        clock.advance_ns(999)

    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fault_then_advance},
        emit_valid_by_default=False,
    )

    opened = open_pool(
        backend,
        [valid_spec()],
        shutdown_timeout_s=0.03,
    )

    assert opened.ready_count == 0
    assert backend.handles["mic-1"].close_deadlines == [1_030_000_123]


def test_startup_reporting_contains_exception_with_failing_string_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnprintableStartError(Exception):
        def __str__(self) -> str:
            raise ValueError("message conversion failed")

    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={
            "mic-1": lambda handle: (_ for _ in ()).throw(
                UnprintableStartError()
            )
        },
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [valid_spec()])

    result = opened.streams[0]
    assert result.status is StreamOpenStatus.FAULTED
    assert result.error is not None
    assert "UnprintableStartError" in result.error.summary
    assert len(result.error.summary) <= MAX_BOUNDED_TEXT_CHARS
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert len(handle.close_deadlines) == 1


def test_valid_first_block_after_start_return_before_deadline_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    wait_entered = threading.Event()

    class ObservableFirstBlockEvent:
        def __init__(self) -> None:
            self._event = threading.Event()

        def is_set(self) -> bool:
            return self._event.is_set()

        def set(self) -> None:
            self._event.set()

        def wait(self, timeout: float | None = None) -> bool:
            wait_entered.set()
            return self._event.wait(timeout)

    first_block_event = ObservableFirstBlockEvent()
    original_slot = stream_module._ResidentStreamSlot

    def slot_factory(*args, **kwargs):
        slot = original_slot(*args, **kwargs)
        slot._first_block_event = first_block_event
        return slot

    emitter_threads: list[threading.Thread] = []

    def start_then_emit_from_barrier(handle) -> None:
        def emit_after_start_owner_waits() -> None:
            wait_entered.wait()
            handle.emit_valid()

        emitter = threading.Thread(target=emit_after_start_owner_waits)
        emitter.start()
        emitter_threads.append(emitter)

    monkeypatch.setattr(pool_module, "_ResidentStreamSlot", slot_factory)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": start_then_emit_from_barrier},
        emit_valid_by_default=False,
    )

    opened = open_pool(
        backend,
        [valid_spec()],
        startup_stream_timeout_s=1.0,
    )

    for emitter in emitter_threads:
        emitter.join(timeout=1.0)
        assert not emitter.is_alive()
    assert wait_entered.is_set()
    assert opened.streams[0].status is StreamOpenStatus.READY


def test_no_first_block_before_deadline_is_faulted_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()

    class AdvancingFirstBlockEvent:
        def __init__(self) -> None:
            self._set = False
            self.wait_calls = 0

        def is_set(self) -> bool:
            return self._set

        def set(self) -> None:
            self._set = True

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_calls += 1
            assert timeout is not None
            clock.advance_seconds(timeout)
            return False

    first_block_event = AdvancingFirstBlockEvent()
    original_slot = stream_module._ResidentStreamSlot

    def slot_factory(*args, **kwargs):
        slot = original_slot(*args, **kwargs)
        slot._first_block_event = first_block_event
        return slot

    monkeypatch.setattr(pool_module, "_ResidentStreamSlot", slot_factory)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(clock=clock, emit_valid_by_default=False)

    opened = open_pool(
        backend,
        [valid_spec()],
        startup_stream_timeout_s=0.01,
    )

    assert first_block_event.wait_calls == 1
    assert opened.streams[0].status is StreamOpenStatus.FAULTED


def test_start_return_without_first_block_does_not_mark_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()

    def return_after_deadline(handle) -> None:
        clock.advance_seconds(0.011)

    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": return_after_deadline},
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [valid_spec()])

    assert opened.streams[0].status is StreamOpenStatus.FAULTED
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert len(handle.close_deadlines) == 1


def test_first_valid_block_is_owned_by_ingress_at_ready_then_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(clock=clock)

    opened = open_pool(backend, [valid_spec()])
    slot = opened.pool._slots["mic-1"]

    assert opened.streams[0].status is StreamOpenStatus.READY
    assert slot.callback_sequence == 1
    assert slot.callback_heartbeat_monotonic_ns is not None
    assert slot._first_block_event.is_set()
    assert slot._first_block_drained_event.wait(1)
    assert slot._ring.write_cursor == 480
    np.testing.assert_array_equal(
        slot._ring.read(0, 1),
        np.array([[0.0, 2.0]], dtype=np.float32),
    )


def test_sync_fault_inside_start_wins_over_normal_start_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={
            "mic-1": lambda handle: handle.emit_status(
                BackendCallbackStatus.DEVICE_FAULT
            )
        },
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [valid_spec()])

    assert opened.streams[0].status is StreamOpenStatus.FAULTED
    handle = backend.handles["mic-1"]
    assert handle.request_stop_calls == 1
    assert len(handle.close_deadlines) == 1
    assert opened.pool._slots["mic-1"]._ring.write_cursor == 0


def test_sync_on_fault_inside_start_prevents_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": lambda handle: handle.fault()},
        emit_valid_by_default=False,
    )

    opened = open_pool(backend, [valid_spec()])

    assert opened.streams[0].status is StreamOpenStatus.FAULTED


@pytest.mark.parametrize(
    "action",
    [
        lambda handle: handle.emit(
            np.zeros(
                (
                    handle.spec.frames_per_buffer,
                    handle.spec.input_channel_count,
                ),
                dtype=np.float64,
            )
        ),
        lambda handle: handle.emit(
            np.zeros((handle.spec.frames_per_buffer, 1), dtype=np.float32)
        ),
        lambda handle: handle.emit_valid(frames=handle.spec.frames_per_buffer + 1),
        lambda handle: handle.emit_status(BackendCallbackStatus.INPUT_OVERFLOW),
    ],
)
def test_invalid_first_block_faults_only_that_hardware(
    monkeypatch: pytest.MonkeyPatch, action
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"bad": action},
        emit_valid_by_default=True,
    )

    opened = open_pool(
        backend,
        [valid_spec(hardware_id="good"), valid_spec(hardware_id="bad")],
    )

    assert [(item.hardware_id, item.status) for item in opened.streams] == [
        ("bad", StreamOpenStatus.FAULTED),
        ("good", StreamOpenStatus.READY),
    ]
    assert [spec.hardware_id for spec in backend.create_calls] == ["bad", "good"]


def test_create_exception_performs_no_cleanup_and_sibling_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        create_errors={"bad": OSError("create failed")},
    )

    opened = open_pool(
        backend,
        [valid_spec(hardware_id="good"), valid_spec(hardware_id="bad")],
    )

    assert "bad" not in backend.handles
    bad_slot = opened.pool._slots["bad"]
    assert bad_slot._handle is None
    assert bad_slot._work_event is None
    assert bad_slot._drain_owner is None
    assert backend.handles["good"].request_stop_calls == 0
    assert backend.handles["good"].close_deadlines == []
    assert opened.ready_count == 1


def test_post_handle_failure_uses_one_shutdown_timeout_cleanup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()

    def fail_start(handle) -> None:
        clock.advance_ns(123)
        raise OSError("start failed")

    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        start_actions={"mic-1": fail_start},
        emit_valid_by_default=False,
    )

    opened = open_pool(
        backend,
        [valid_spec()],
        fault_close_timeout_s=0.02,
        shutdown_timeout_s=0.03,
    )

    assert opened.ready_count == 0
    handle = backend.handles["mic-1"]
    fault_ns = 1_000_000_123
    assert handle.request_stop_calls == 1
    assert handle.close_deadlines == [fault_ns + 30_000_000]


def test_six_serial_failures_stay_within_startup_plus_cleanup_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()

    def time_out_start(handle) -> None:
        clock.advance_seconds(0.01)

    class AdvancingBackend(FakeBackend):
        def create_input(self, spec, callback, on_fault):
            handle = super().create_input(spec, callback, on_fault)
            original_close = handle.close

            def close(deadline):
                result = original_close(deadline)
                clock.advance_seconds(0.03)
                return result

            handle.close = close
            return handle

    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = AdvancingBackend(
        clock=clock,
        start_actions={f"mic-{index}": time_out_start for index in range(6)},
        emit_valid_by_default=False,
    )
    start_ns = clock()

    opened = open_pool(
        backend,
        [valid_spec(hardware_id=f"mic-{index}") for index in range(6)],
        startup_stream_timeout_s=0.01,
        shutdown_timeout_s=0.03,
    )

    assert opened.ready_count == 0
    assert clock() - start_ns <= 6 * 40_000_000
    assert all(handle.request_stop_calls == 1 for handle in backend.handles.values())
    assert all(len(handle.close_deadlines) == 1 for handle in backend.handles.values())


def test_zero_ready_streams_return_pool_and_explicit_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        create_errors={"mic-1": OSError("not present")},
    )

    opened = open_pool(backend, [valid_spec()])

    assert isinstance(opened.pool, ResidentAudioPool)
    assert opened.ready_count == 0
    assert opened.streams[0].status is StreamOpenStatus.FAULTED


def ready_pool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hardware_ids: tuple[str, ...] = ("mic-1",),
):
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(clock=clock)
    opened = open_pool(
        backend,
        [valid_spec(hardware_id=hardware_id) for hardware_id in hardware_ids],
    )
    return opened.pool, backend


def test_six_ready_streams_consume_zero_capture_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(
        monkeypatch,
        hardware_ids=tuple(f"mic-{index}" for index in range(6)),
    )

    assert len(pool._slots) == 6
    assert pool._capture_permits == 0
    assert pool._bindings == {}


def test_one_ready_stream_accepts_six_reserved_bindings_then_rejects_seventh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)

    bindings = [
        pool.reserve_capture(f"run-{index}", "mic-1") for index in range(6)
    ]

    assert len(bindings) == 6
    assert pool._capture_permits == 6
    assert len(pool._bindings) == 6
    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("run-6", "mic-1")
    assert raised.value.error.code is ResidentErrorCode.CAPACITY
    assert pool._capture_permits == 6


def test_six_streams_and_six_capture_permits_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardware_ids = tuple(f"mic-{index}" for index in range(6))
    pool, _ = ready_pool(monkeypatch, hardware_ids=hardware_ids)

    bindings = [
        pool.reserve_capture(f"run-{index}", hardware_id)
        for index, hardware_id in enumerate(hardware_ids)
    ]

    assert len(pool._slots) == 6
    assert len(bindings) == 6
    assert pool._capture_permits == 6


def test_faulted_stream_does_not_block_ready_sibling_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        create_errors={"bad": OSError("missing")},
    )
    opened = open_pool(
        backend,
        [valid_spec(hardware_id="bad"), valid_spec(hardware_id="good")],
    )

    binding = opened.pool.reserve_capture("run-good", "good")

    assert binding.hardware_id == "good"
    with pytest.raises(ResidentRequestError) as raised:
        opened.pool.reserve_capture("run-bad", "bad")
    assert raised.value.error.code is ResidentErrorCode.NOT_READY


def test_same_run_id_cannot_reserve_twice_across_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch, hardware_ids=("mic-1", "mic-2"))
    first = pool.reserve_capture("run-1", "mic-1")

    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("run-1", "mic-2")

    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert pool.capture_completion(first) is first._completion
    assert pool._capture_permits == 1


@pytest.mark.parametrize(
    ("run_id", "hardware_id"),
    [
        ("", "mic-1"),
        ("x" * (MAX_BOUNDED_TEXT_CHARS + 1), "mic-1"),
        (True, "mic-1"),
        ("run-1", ""),
        ("run-1", b"mic-1"),
        ("run-1", "x" * (MAX_BOUNDED_TEXT_CHARS + 1)),
    ],
)
def test_reserve_rejects_malformed_values_without_state_change(
    monkeypatch: pytest.MonkeyPatch,
    run_id: object,
    hardware_id: object,
) -> None:
    pool, _ = ready_pool(monkeypatch)

    assert_invalid(
        lambda: pool.reserve_capture(run_id, hardware_id)  # type: ignore[arg-type]
    )
    assert pool._capture_permits == 0
    assert pool._bindings == {}


def test_capture_completion_is_stable_and_rejects_invalid_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    before = (pool._capture_permits, dict(pool._bindings))

    assert pool.capture_completion(binding) is binding._completion
    assert pool.capture_completion(binding) is binding._completion
    assert_invalid(lambda: pool.capture_completion(None))  # type: ignore[arg-type]

    foreign, _ = ready_pool(monkeypatch)
    foreign_binding = foreign.reserve_capture("foreign-run", "mic-1")
    assert_invalid(lambda: pool.capture_completion(foreign_binding))
    foreign_cancel = pool.cancel_capture(foreign_binding)
    assert foreign_cancel.status is CancelStatus.INVALID
    assert foreign_cancel.error is not None
    assert foreign_cancel.error.code is ResidentErrorCode.INVALID

    stale = pool.reserve_capture("stale-run", "mic-1")
    with pool._lock:
        del pool._bindings[stale.binding_id]
        del pool._run_ids[stale.run_id]
        pool._capture_permits -= 1
    assert_invalid(lambda: pool.capture_completion(stale))
    stale_cancel = pool.cancel_capture(stale)
    assert stale_cancel.status is CancelStatus.INVALID
    assert stale_cancel.error is not None
    assert stale_cancel.error.code is ResidentErrorCode.INVALID
    assert (pool._capture_permits, pool._bindings) == before


def test_reserve_rejects_not_ready_and_closing_with_exact_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns", clock
    )
    backend = FakeBackend(
        clock=clock,
        create_errors={"mic-1": OSError("missing")},
    )
    unavailable = open_pool(backend, [valid_spec()]).pool
    with pytest.raises(ResidentRequestError) as raised_not_ready:
        unavailable.reserve_capture("run-1", "mic-1")
    assert raised_not_ready.value.error.code is ResidentErrorCode.NOT_READY

    ready, _ = ready_pool(monkeypatch)
    ready._state = "CLOSING"
    with pytest.raises(ResidentRequestError) as raised_closing:
        ready.reserve_capture("run-2", "mic-1")
    assert raised_closing.value.error.code is ResidentErrorCode.CLOSING
    assert ready._capture_permits == 0


def test_reserved_cancel_removes_record_releases_once_and_publishes_stable_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend = ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)
    assert completion.result_if_ready() is None

    cancelled = pool.cancel_capture(binding)

    assert cancelled.status is CancelStatus.CANCELLED
    outcome = cancelled.outcome
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.CANCELLED
    assert outcome.binding is binding
    assert outcome.audio is None
    assert outcome.error is not None
    assert outcome.error.code is ResidentErrorCode.CANCELLED
    assert completion.wait(0) is outcome
    assert completion.wait(None) is outcome
    assert completion.result_if_ready() is outcome
    assert pool.capture_completion(binding) is completion
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}
    assert backend.handles["mic-1"].request_stop_calls == 0
    assert backend.handles["mic-1"].close_deadlines == []

    repeated = pool.cancel_capture(binding)
    assert repeated.status is CancelStatus.ALREADY_TERMINAL
    assert repeated.outcome is outcome
    assert repeated.error is None
    assert pool._capture_permits == 0


def test_event_visible_before_retirement_keeps_pool_operations_behind_same_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.capture_completion(binding)
    underlying_event = completion._event
    event_visible = threading.Event()
    release_event_set = threading.Event()
    set_calls = 0

    class BarrierEvent:
        def is_set(self) -> bool:
            return underlying_event.is_set()

        def wait(self, timeout: float | None = None) -> bool:
            return underlying_event.wait(timeout)

        def set(self) -> None:
            nonlocal set_calls
            set_calls += 1
            underlying_event.set()
            event_visible.set()
            if not release_event_set.wait(2.0):
                raise AssertionError("terminal Event.set barrier was not released")

    completion._event = BarrierEvent()  # type: ignore[assignment]

    operation_names = ("completion", "cancel")
    lock_attempted = {
        name: threading.Event() for name in operation_names
    }
    operation_observed = {
        name: threading.Event() for name in operation_names
    }
    operation_done = {
        name: threading.Event() for name in operation_names
    }
    underlying_lock = pool._lock

    class ObservableDecisionLock:
        def __enter__(self):
            name = threading.current_thread().name
            if name in lock_attempted:
                lock_attempted[name].set()
                operation_observed[name].set()
            underlying_lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            underlying_lock.release()

    pool._lock = ObservableDecisionLock()  # type: ignore[assignment]
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def run_operation(name: str, operation) -> None:
        try:
            results[name] = operation()
        except BaseException as exc:
            errors[name] = exc
        finally:
            if name in operation_done:
                operation_done[name].set()
                operation_observed[name].set()

    terminal_thread = threading.Thread(
        name="terminal",
        target=run_operation,
        args=("terminal", lambda: pool.cancel_capture(binding)),
    )
    terminal_thread.start()
    assert event_visible.wait(1.0)

    visible_outcome = completion.result_if_ready()
    assert visible_outcome is not None
    assert completion.wait(0) is visible_outcome
    state_at_event = (
        pool._capture_permits,
        pool._bindings.get(binding.binding_id),
        pool._run_ids.get(binding.run_id),
    )

    operation_threads = [
        threading.Thread(
            name="completion",
            target=run_operation,
            args=("completion", lambda: pool.capture_completion(binding)),
        ),
        threading.Thread(
            name="cancel",
            target=run_operation,
            args=("cancel", lambda: pool.cancel_capture(binding)),
        ),
    ]
    for thread in operation_threads:
        thread.start()
    for name in operation_names:
        assert operation_observed[name].wait(1.0)
    before_release = {
        name: (lock_attempted[name].is_set(), operation_done[name].is_set())
        for name in operation_names
    }

    release_event_set.set()
    terminal_thread.join(timeout=1.0)
    for thread in operation_threads:
        thread.join(timeout=1.0)

    assert not terminal_thread.is_alive()
    assert all(not thread.is_alive() for thread in operation_threads)
    assert errors == {}
    assert before_release == {
        "completion": (True, False),
        "cancel": (True, False),
    }
    assert state_at_event[0] == 1
    assert state_at_event[1] is not None
    assert state_at_event[2] == binding.binding_id
    terminal_result = results["terminal"]
    assert isinstance(terminal_result, CancelResult)
    assert terminal_result.status is CancelStatus.CANCELLED
    assert terminal_result.outcome is visible_outcome
    assert results["completion"] is completion
    repeated = results["cancel"]
    assert isinstance(repeated, CancelResult)
    assert repeated.status is CancelStatus.ALREADY_TERMINAL
    assert repeated.outcome is visible_outcome
    assert set_calls == 1
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}


def test_reserved_binding_holds_capacity_until_terminal_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)
    bindings = [
        pool.reserve_capture(f"run-{index}", "mic-1") for index in range(6)
    ]
    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("blocked", "mic-1")
    assert raised.value.error.code is ResidentErrorCode.CAPACITY

    pool.cancel_capture(bindings[0])
    replacement = pool.reserve_capture("replacement", "mic-1")

    assert replacement.run_id == "replacement"
    assert pool._capture_permits == 6


def test_binding_is_opaque_by_identity_and_repr_hides_owner_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")

    @dataclasses.dataclass(frozen=True)
    class BindingLookalike:
        binding_id: str
        run_id: str
        hardware_id: str

    lookalike = BindingLookalike(
        binding.binding_id,
        binding.run_id,
        binding.hardware_id,
    )

    assert binding is not lookalike
    assert binding != lookalike
    assert_invalid(
        lambda: pool.capture_completion(lookalike)  # type: ignore[arg-type]
    )
    assert_invalid(lambda: pool.cancel_capture(lookalike))  # type: ignore[arg-type]
    assert "nonce" not in repr(binding).lower()
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.run_id = "changed"  # type: ignore[misc]


def test_uninitialized_binding_is_malformed_for_completion_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _ = ready_pool(monkeypatch)
    malformed = object.__new__(CaptureBinding)

    assert_invalid(lambda: pool.capture_completion(malformed))
    assert_invalid(lambda: pool.cancel_capture(malformed))
