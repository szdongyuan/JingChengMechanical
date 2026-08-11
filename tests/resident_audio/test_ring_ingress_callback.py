from __future__ import annotations

import ast
import builtins
import inspect
import math
import queue
import threading
import textwrap
import time
from dataclasses import dataclass

import numpy as np
import pytest
from _pytest.mark.structures import Mark, MarkDecorator

from app.service.analysis import resident_audio
from app.service.analysis.resident_audio.contracts import (
    BlockTimeQuality,
    CaptureErrorCode,
    CaptureLayerError,
    CapturedAudioBlock,
)
from app.service.analysis.resident_audio.ingress import (
    FOUR_BLOCK_INGRESS_CAPACITY,
    GenerationLatch,
    PortAudioCallbackAdapter,
    SpscBlockIngress,
    captured_block_from_raw,
)
import app.service.analysis.resident_audio.ingress as ingress_module
from app.service.analysis.resident_audio.ring import (
    RingReadStatus,
    TimestampedBlockRing,
)
from app.service.analysis.resident_audio.timeline import StreamTimeline


performance_test = MarkDecorator(
    Mark("performance", (), {}, _ispytest=True),
    _ispytest=True,
)


POOL_EPOCH = "pool-a"
HARDWARE_ID = "hw-1"
GENERATION = 3


def _captured(
    sample_start: int,
    frames: int,
    *,
    pool_epoch: str = POOL_EPOCH,
    hardware_id: str = HARDWARE_ID,
    generation: int = GENERATION,
) -> CapturedAudioBlock:
    data = np.arange(frames * 2, dtype=np.float32).reshape(frames, 2)
    return CapturedAudioBlock(
        pool_epoch=pool_epoch,
        hardware_id=hardware_id,
        stream_generation=generation,
        sample_start=sample_start,
        sample_end=sample_start + frames,
        data=data,
        time_quality=BlockTimeQuality.CONFIRMED,
        monotonic_start=10.0 + sample_start / 10.0,
        monotonic_end=10.0 + (sample_start + frames) / 10.0,
    )


class _TimelineTrimSpy:
    def __init__(self) -> None:
        self.boundaries: list[int] = []

    def trim_before(self, sample_cursor: int) -> None:
        self.boundaries.append(sample_cursor)


def _ring(
    *,
    sample_rate: float = 5.0,
    timeline: _TimelineTrimSpy | None = None,
) -> TimestampedBlockRing:
    return TimestampedBlockRing(
        pool_epoch=POOL_EPOCH,
        hardware_id=HARDWARE_ID,
        stream_generation=GENERATION,
        sample_rate=sample_rate,
        timeline=timeline,
    )


def test_ring_retains_two_seconds_with_at_most_one_boundary_block():
    ring = _ring(sample_rate=5.0)
    minimum = math.ceil(5.0 * 2.0)

    for start in range(0, 80, 4):
        ring.append(_captured(start, 4))
        assert ring.retained_frames <= minimum + 4

    assert ring.minimum_frames == minimum
    assert ring.retained_frames >= minimum
    assert ring.earliest_cursor == 68
    assert ring.latest_cursor == 80
    assert ring.block_count == 3


@pytest.mark.parametrize("retention_seconds", [0.5, 3.0])
def test_ring_product_window_cannot_be_configured_below_or_above_two_seconds(
    retention_seconds: float,
):
    with pytest.raises(TypeError, match="retention_seconds"):
        TimestampedBlockRing(
            pool_epoch=POOL_EPOCH,
            hardware_id=HARDWARE_ID,
            stream_generation=GENERATION,
            sample_rate=5.0,
            retention_seconds=retention_seconds,
        )


def test_task_two_components_are_available_from_the_capture_package():
    assert resident_audio.TimestampedBlockRing is TimestampedBlockRing
    assert resident_audio.RingReadStatus is RingReadStatus
    assert resident_audio.GenerationLatch is GenerationLatch
    assert resident_audio.SpscBlockIngress is SpscBlockIngress
    assert resident_audio.PortAudioCallbackAdapter is PortAudioCallbackAdapter


def test_ring_evicts_whole_blocks_and_reads_absolute_cursors_after_wrap():
    ring = _ring(sample_rate=5.0)
    blocks = [_captured(start, 4) for start in range(0, 24, 4)]
    for block in blocks:
        ring.append(block)

    expired = ring.references_from(11, limit=2)
    available = ring.references_from(13, limit=2)
    future = ring.references_from(25, limit=2)

    assert expired.status is RingReadStatus.EXPIRED
    assert expired.references == ()
    assert available.status is RingReadStatus.AVAILABLE
    assert available.references == (blocks[3], blocks[4])
    assert available.references[0].data is blocks[3].data
    assert available.next_cursor == blocks[4].sample_end
    assert future.status is RingReadStatus.NOT_YET_AVAILABLE
    assert future.references == ()


def test_ring_reference_result_is_strictly_bounded_without_data_copy():
    ring = _ring(sample_rate=50.0)
    blocks = [_captured(start, 4) for start in range(0, 20, 4)]
    for block in blocks:
        ring.append(block)

    result = ring.references_from(0, limit=3)

    assert len(result.references) == 3
    assert result.references == tuple(blocks[:3])
    assert all(returned is original for returned, original in zip(result.references, blocks))
    assert all(
        returned.data is original.data
        for returned, original in zip(result.references, blocks)
    )


@pytest.mark.parametrize(
    ("block", "expected_code"),
    [
        (_captured(5, 4), CaptureErrorCode.INPUT_OVERFLOW_OR_GAP),
        (
            _captured(4, 4, pool_epoch="other"),
            CaptureErrorCode.STREAM_GENERATION_CHANGED,
        ),
        (
            _captured(4, 4, generation=4),
            CaptureErrorCode.STREAM_GENERATION_CHANGED,
        ),
        (
            _captured(4, 4, hardware_id="other"),
            CaptureErrorCode.STREAM_GENERATION_CHANGED,
        ),
    ],
)
def test_ring_rejects_discontinuity_or_different_stream_identity(
    block: CapturedAudioBlock,
    expected_code: CaptureErrorCode,
):
    ring = _ring()
    ring.append(_captured(0, 4))

    with pytest.raises(CaptureLayerError) as captured:
        ring.append(block)

    assert captured.value.code is expected_code
    assert ring.latest_cursor == 4
    assert ring.block_count == 1


def test_ring_trims_timeline_at_the_same_whole_block_eviction_boundary():
    timeline = _TimelineTrimSpy()
    ring = _ring(timeline=timeline)

    for start in range(0, 20, 4):
        ring.append(_captured(start, 4))

    assert timeline.boundaries == [4, 8]
    assert timeline.boundaries[-1] == ring.earliest_cursor


@dataclass
class _TimeInfo:
    inputBufferAdcTime: float = 20.0
    currentTime: float = 20.0


class _FatalStatus:
    input_overflow = True

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return "input overflow"


def _callback_boundary(
    *,
    clock=lambda: 10.0,
) -> tuple[GenerationLatch, SpscBlockIngress, PortAudioCallbackAdapter]:
    latch = GenerationLatch()
    ingress = SpscBlockIngress(latch)
    callback = PortAudioCallbackAdapter(
        pool_epoch=POOL_EPOCH,
        hardware_id=HARDWARE_ID,
        stream_generation=GENERATION,
        generation_latch=latch,
        ingress=ingress,
        monotonic_clock=clock,
    )
    return latch, ingress, callback


def _invoke_callback(
    callback: PortAudioCallbackAdapter,
    *,
    frames: int = 4,
    status: object = "",
) -> None:
    callback(
        np.arange(frames * 2, dtype=np.float32).reshape(frames, 2),
        frames,
        _TimeInfo(),
        status,
    )


def test_four_block_ingress_never_overwrites_or_expands_and_latches_fifth():
    latch, ingress, callback = _callback_boundary()

    for _ in range(5):
        _invoke_callback(callback)

    retained = [ingress.try_pop() for _ in range(4)]
    assert FOUR_BLOCK_INGRESS_CAPACITY == 4
    assert ingress.capacity == 4
    assert ingress.size == 0
    assert [block.callback_sequence for block in retained if block] == [0, 1, 2, 3]
    assert latch.alive is False
    assert latch.failure_code is CaptureErrorCode.INPUT_INGRESS_BACKLOG
    assert ingress.try_pop() is None


def test_stale_and_shutdown_callbacks_only_increment_the_drop_counter():
    latch, ingress, callback = _callback_boundary()
    latch.deactivate()

    _invoke_callback(callback, status=_FatalStatus())
    _invoke_callback(callback)

    assert latch.alive is False
    assert latch.failure_code is None
    assert latch.dropped_callbacks == 2
    assert ingress.size == 0
    assert callback.next_sequence == 0


def test_fatal_portaudio_status_latches_generation_without_pushing_audio():
    latch, ingress, callback = _callback_boundary()

    _invoke_callback(callback, status=_FatalStatus())

    assert latch.alive is False
    assert latch.failure_code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP
    assert ingress.size == 0
    assert ingress.wake_event.is_set()


def test_callback_makes_one_owned_copy_one_raw_block_and_one_try_push(monkeypatch):
    latch, ingress, callback = _callback_boundary(clock=lambda: 12.5)
    calls = {"ascontiguousarray": 0, "copy": 0, "raw": 0, "push": 0}
    real_ascontiguousarray = np.ascontiguousarray
    real_raw = ingress_module.RawCallbackBlock
    real_push = ingress.try_push

    class _CopyProbe:
        def __init__(self, array: np.ndarray) -> None:
            self._array = array

        def copy(self) -> np.ndarray:
            calls["copy"] += 1
            return self._array.copy()

    def observed_ascontiguousarray(data, *, dtype):
        calls["ascontiguousarray"] += 1
        assert dtype is np.float32
        return _CopyProbe(real_ascontiguousarray(data, dtype=dtype))

    class ObservedRaw(real_raw):
        def __new__(cls, **kwargs):
            del kwargs
            calls["raw"] += 1
            return super().__new__(cls)

    def observed_push(block):
        calls["push"] += 1
        return real_push(block)

    monkeypatch.setattr(ingress_module.np, "ascontiguousarray", observed_ascontiguousarray)
    monkeypatch.setattr(ingress_module, "RawCallbackBlock", ObservedRaw)
    monkeypatch.setattr(ingress, "try_push", observed_push)

    source = np.arange(8, dtype=np.float32).reshape(4, 2)
    callback(source, 4, _TimeInfo(30.0, 30.25), "")
    raw = ingress.try_pop()

    assert calls == {"ascontiguousarray": 1, "copy": 1, "raw": 1, "push": 1}
    assert raw is not None
    assert raw.callback_monotonic == 12.5
    assert raw.input_buffer_adc_time == 30.0
    assert raw.current_time == 30.25
    assert raw.data is not source
    np.testing.assert_array_equal(raw.data, source)


def test_callback_structure_and_instrumentation_exclude_forbidden_operations(
    monkeypatch,
):
    _, ingress, callback = _callback_boundary()

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("forbidden callback operation")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    monkeypatch.setattr(threading.Event, "wait", forbidden)
    monkeypatch.setattr(threading, "Lock", forbidden)
    monkeypatch.setattr(threading, "RLock", forbidden)
    monkeypatch.setattr(queue.Queue, "put", forbidden)
    monkeypatch.setattr(queue.Queue, "get", forbidden)

    _invoke_callback(callback)
    assert ingress.size == 1

    callback_tree = ast.parse(
        textwrap.dedent(inspect.getsource(PortAudioCallbackAdapter.__call__))
    )
    forbidden_names = {
        "open",
        "write",
        "flush",
        "rename",
        "unlink",
        "execute",
        "emit",
        "analyze",
        "sleep",
        "wait",
        "put",
        "get",
        "acquire",
        "join",
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(callback_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(forbidden_names)

    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(inspect.getsource(ingress_module)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported_roots.isdisjoint(
        {"peewee", "PyQt5", "PySide6", "artifact_store", "owner", "pool", "stream"}
    )


def test_paused_consumer_does_not_make_callback_wait():
    latch, ingress, callback = _callback_boundary()
    producer_done = threading.Event()

    def produce() -> None:
        for _ in range(5):
            _invoke_callback(callback)
        producer_done.set()

    producer = threading.Thread(target=produce)
    producer.start()

    assert producer_done.wait(1.0)
    producer.join(1.0)
    assert not producer.is_alive()
    assert ingress.size == 4
    assert latch.failure_code is CaptureErrorCode.INPUT_INGRESS_BACKLOG


def test_ingest_conversion_ring_and_six_owner_references_share_one_copy():
    latch, ingress, callback = _callback_boundary()
    _invoke_callback(callback)
    raw = ingress.try_pop()
    assert raw is not None

    timeline = StreamTimeline(sample_rate=10.0, monotonic_clock=lambda: 10.0)
    update = timeline.ingest(raw)
    captured = captured_block_from_raw(raw, update, sample_rate=10.0)
    ring = _ring(sample_rate=10.0)
    ring.append(captured)
    owners = [captured for _ in range(6)]
    ring_reference = ring.references_from(0, limit=1).references[0]

    assert all(owner is captured for owner in owners)
    assert ring_reference is captured
    assert captured.data is raw.data
    assert all(owner.data is raw.data for owner in owners)


@performance_test
def test_callback_p99_is_below_half_of_production_block_duration():
    clock_value = 100.0

    def clock() -> float:
        nonlocal clock_value
        clock_value += 0.05
        return clock_value

    latch, ingress, callback = _callback_boundary(clock=clock)
    source = np.zeros((2_400, 2), dtype=np.float32)
    time_info = _TimeInfo()
    durations_ms: list[float] = []

    for index in range(10_200):
        started = time.perf_counter_ns()
        callback(source, 2_400, time_info, "")
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        raw = ingress.try_pop()
        assert raw is not None
        if index >= 200:
            durations_ms.append(elapsed_ms)

    p99_ms = float(np.percentile(durations_ms, 99))
    assert latch.alive
    assert p99_ms < 25.0, f"callback p99 was {p99_ms:.3f} ms"
