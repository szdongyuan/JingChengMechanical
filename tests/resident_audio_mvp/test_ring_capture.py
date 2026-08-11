from __future__ import annotations

import gc
import inspect
import threading
import weakref
from dataclasses import fields

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp.contracts import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    CaptureOutcomeStatus,
    CancelStatus,
    ResidentError,
    CapturedAudio,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
)
from app.service.analysis.resident_audio_mvp.pool import ResidentAudioPool
from app.service.analysis.resident_audio_mvp.ring import (
    _MappingStatus,
    _ResidentRing,
)
from app.service.analysis.resident_audio_mvp.stream import _ResidentStreamSlot
from app.service.managed_input_sounddevice_backend import (
    MonotonicSoundDeviceAudioBackend,
)

from .fakes import FakeBackend, ManualMonotonicClock


def stream_spec(
    *,
    sample_rate_hz: int = 100,
    frames_per_buffer: int = 10,
    input_channel_count: int = 2,
    capture_channel_indices: tuple[int, ...] = (0,),
) -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id="mic-1",
        device_index=1,
        device_name="Exact Input",
        hostapi_index=0,
        hostapi_name="WASAPI",
        sample_rate_hz=sample_rate_hz,
        input_channel_count=input_channel_count,
        capture_channel_indices=capture_channel_indices,
        sample_format="float32",
        frames_per_buffer=frames_per_buffer,
    )


def make_ring(
    *,
    sample_rate_hz: int = 100,
    frames_per_buffer: int = 10,
    channel_count: int = 1,
) -> _ResidentRing:
    ring = _ResidentRing(
        sample_rate_hz * 2 + frames_per_buffer,
        channel_count,
    )
    ring.configure_timeline(
        sample_rate_hz=sample_rate_hz,
        frames_per_buffer=frames_per_buffer,
    )
    return ring


def block(first: int, *, frames: int = 10, channels: int = 1) -> np.ndarray:
    values = np.arange(first, first + frames, dtype=np.float32)
    return np.repeat(values[:, None], channels, axis=1)


def append_block(
    ring: _ResidentRing,
    samples: np.ndarray,
    *,
    callback_monotonic_ns: int,
) -> None:
    ring.append(samples, callback_monotonic_ns=callback_monotonic_ns)


def ring_snapshot(ring: _ResidentRing) -> tuple[object, ...]:
    return (
        ring.oldest_cursor,
        ring.write_cursor,
        ring.retained_frames,
        ring.block_metadata,
        ring._storage.copy(),
    )


def assert_same_ring_snapshot(
    before: tuple[object, ...],
    after: tuple[object, ...],
) -> None:
    assert after[:4] == before[:4]
    np.testing.assert_array_equal(after[4], before[4])


def test_ten_thousand_appends_keep_fixed_storage() -> None:
    ring = make_ring()
    storage_id = id(ring._storage)

    for index in range(10_000):
        append_block(
            ring,
            block(index * 10),
            callback_monotonic_ns=(index + 1) * 100_000_000,
        )

    assert id(ring._storage) == storage_id
    assert ring.capacity_frames == 210
    assert ring.write_cursor == 100_000
    assert ring.retained_frames <= ring.capacity_frames


def test_exact_half_open_read_and_block_timestamp_metadata() -> None:
    ring = make_ring()
    append_block(ring, block(0), callback_monotonic_ns=100_000_000)
    append_block(ring, block(10), callback_monotonic_ns=200_000_000)

    np.testing.assert_array_equal(ring.read(3, 10)[:, 0], np.arange(3, 13))
    metadata = ring.block_metadata
    assert [
        (
            item.start_cursor,
            item.end_cursor,
            item.frame_count,
            item.callback_monotonic_ns,
            item.estimated_start_ns,
            item.estimated_end_ns,
        )
        for item in metadata
    ] == [
        (0, 10, 10, 100_000_000, 0, 100_000_000),
        (10, 20, 10, 200_000_000, 100_000_000, 200_000_000),
    ]


def test_whole_block_eviction_updates_oldest_and_write_cursors() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=4)

    for index, frames in enumerate((3, 4, 2, 4, 4, 4, 4)):
        append_block(
            ring,
            block(sum((3, 4, 2, 4, 4, 4, 4)[:index]), frames=frames),
            callback_monotonic_ns=(index + 1) * 400_000_000,
        )

    assert ring.capacity_frames == 24
    assert ring.write_cursor == 25
    assert ring.oldest_cursor == 3
    assert ring.retained_frames == 22
    assert ring.block_metadata[0].start_cursor == 3


def test_invalid_or_oversized_append_has_zero_state_change() -> None:
    ring = make_ring()
    append_block(ring, block(0), callback_monotonic_ns=100_000_000)

    invalid_blocks = (
        np.zeros((10, 1), dtype=np.float64),
        np.zeros((10,), dtype=np.float32),
        np.zeros((10, 2), dtype=np.float32),
        np.zeros((11, 1), dtype=np.float32),
    )
    for invalid in invalid_blocks:
        before = ring_snapshot(ring)
        try:
            append_block(
                ring,
                invalid,
                callback_monotonic_ns=200_000_000,
            )
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("invalid append unexpectedly succeeded")
        assert_same_ring_snapshot(before, ring_snapshot(ring))


def make_slot() -> tuple[_ResidentStreamSlot, ManualMonotonicClock]:
    clock = ManualMonotonicClock(initial_ns=1_000_000_000)
    spec = stream_spec()
    slot = _ResidentStreamSlot(
        spec,
        ring_capacity_frames=210,
        monotonic_ns=clock,
    )
    slot.publish_drain_owner()
    return slot, clock


def callback_block() -> np.ndarray:
    return np.arange(20, dtype=np.float32).reshape(10, 2)


def test_stale_generation_callback_has_zero_audio_state_change() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    slot._generation = object()
    before = ring_snapshot(slot._ring)

    directive = slot._callback_for_generation(
        generation,
        callback_block(),
        10,
        clock(),
        BackendCallbackStatus.OK,
    )

    assert directive is BackendCallbackDirective.STOP
    assert_same_ring_snapshot(before, ring_snapshot(slot._ring))


def test_two_second_history_uses_at_most_one_boundary_block_extra() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=4)

    for index in range(20):
        append_block(
            ring,
            block(index * 3, frames=3),
            callback_monotonic_ns=(index + 1) * 300_000_000,
        )

    assert ring.retained_frames <= 20 + 4
    assert ring.capacity_frames == 24


def test_stale_or_shutdown_callback_only_changes_bounded_drop_diagnostic() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    slot._accepting = False
    slot._generation = None
    slot._dropped_callback_count = (1 << 63) - 2
    assert slot._work_event is not None
    slot._work_event.clear()
    before = ring_snapshot(slot._ring)

    first = slot._callback_for_generation(
        generation,
        callback_block(),
        10,
        clock(),
        BackendCallbackStatus.OK,
    )
    second = slot._callback_for_generation(
        generation,
        callback_block(),
        10,
        clock(),
        BackendCallbackStatus.OK,
    )

    assert first is second is BackendCallbackDirective.STOP
    assert slot._dropped_callback_count == (1 << 63) - 1
    assert not slot._work_event.is_set()
    assert_same_ring_snapshot(before, ring_snapshot(slot._ring))


def test_fatal_backend_status_latches_fault_without_appending_audio() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    assert slot._work_event is not None
    slot._work_event.clear()

    directive = slot._callback_for_generation(
        generation,
        callback_block(),
        10,
        clock(),
        BackendCallbackStatus.DEVICE_FAULT,
    )

    assert directive is BackendCallbackDirective.STOP
    assert slot._fault is not None
    assert slot._state == "FAULTED"
    assert slot._ring.write_cursor == 0
    assert not slot._work_event.is_set()


def test_trigger_inside_block_maps_to_first_sample_not_earlier_than_trigger() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)

    mapped = ring.map_trigger(850_000_000)

    assert mapped.status is _MappingStatus.AVAILABLE
    assert mapped.cursor == 1


def test_exact_half_open_time_boundary_belongs_only_to_later_block() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)
    append_block(ring, block(2, frames=2), callback_monotonic_ns=1_200_000_000)

    mapped = ring.map_trigger(1_000_000_000)

    assert mapped.status is _MappingStatus.AVAILABLE
    assert mapped.cursor == 2


def test_trigger_at_or_newer_than_newest_anchor_waits_for_later_block() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)

    assert ring.map_trigger(1_000_000_000).status is _MappingStatus.NOT_YET_AVAILABLE
    assert ring.map_trigger(1_050_000_000).status is _MappingStatus.NOT_YET_AVAILABLE


def open_ready_pool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sample_rate_hz: int = 10,
    frames_per_buffer: int = 2,
) -> tuple[ResidentAudioPool, FakeBackend, ManualMonotonicClock]:
    clock = ManualMonotonicClock(initial_ns=1_000_000_000)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns",
        clock,
    )
    backend = FakeBackend(clock=clock)
    opened = ResidentAudioPool.open(
        [
            stream_spec(
                sample_rate_hz=sample_rate_hz,
                frames_per_buffer=frames_per_buffer,
            )
        ],
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    assert opened.ready_count == 1
    return opened.pool, backend, clock


def test_start_rejects_trigger_beyond_one_block_clock_race_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    record = pool._bindings[binding.binding_id]
    before = (
        record.state,
        record.buffer,
        pool._capture_permits,
        dict(pool._bindings),
        dict(pool._run_ids),
        binding._completion.result_if_ready(),
    )

    with pytest.raises(ResidentRequestError) as raised:
        pool.start_capture(
            binding,
            trigger_monotonic_ns=clock() + 200_000_001,
            target_frame_count=2,
        )

    assert raised.value.error.code is ResidentErrorCode.INVALID
    after = (
        record.state,
        record.buffer,
        pool._capture_permits,
        pool._bindings,
        pool._run_ids,
        binding._completion.result_if_ready(),
    )
    assert after == before


def test_trigger_within_one_block_clock_race_waits_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")

    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock() + 200_000_000,
        target_frame_count=2,
    )

    record = pool._bindings[binding.binding_id]
    assert completion is binding._completion
    assert record.state == "ACTIVE"
    assert record.start_cursor is None
    assert completion.result_if_ready() is None


def test_trigger_older_than_logical_retention_is_expired() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    for index in range(12):
        append_block(
            ring,
            block(index * 2, frames=2),
            callback_monotonic_ns=1_000_000_000 + index * 200_000_000,
        )

    mapped = ring.map_trigger(950_000_000)

    assert mapped.status is _MappingStatus.EXPIRED
    assert mapped.cursor is None


def test_one_block_overlap_selects_the_smaller_candidate_cursor() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)
    append_block(ring, block(2, frames=2), callback_monotonic_ns=1_100_000_000)

    mapped = ring.map_trigger(950_000_000)

    assert mapped.status is _MappingStatus.AVAILABLE
    assert mapped.cursor == 2


def test_callback_delivery_gap_maps_to_next_continuous_frame_cursor() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)
    append_block(ring, block(2, frames=2), callback_monotonic_ns=1_300_000_000)

    mapped = ring.map_trigger(1_050_000_000)

    assert mapped.status is _MappingStatus.AVAILABLE
    assert mapped.cursor == 2


def test_positive_callback_delay_keeps_stream_available_for_later_runs() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    first = slot._callback_for_generation(
        generation,
        callback_block()[:2],
        2,
        1_000_000_000,
        BackendCallbackStatus.OK,
    )
    clock.advance_ns(1_000_000_000)
    before_cursor = slot._ring.write_cursor

    second = slot._callback_for_generation(
        generation,
        callback_block()[:2],
        2,
        1_500_000_001,
        BackendCallbackStatus.OK,
    )

    assert first is BackendCallbackDirective.CONTINUE
    assert second is BackendCallbackDirective.CONTINUE
    assert slot._fault is None
    assert slot._ring.write_cursor == before_cursor + 2
    delayed = slot._ring.map_trigger(1_050_000_000)
    assert delayed.status is _MappingStatus.AVAILABLE
    assert delayed.cursor == 2
    assert (
        slot._ring.map_trigger(1_490_000_001).status
        is _MappingStatus.AVAILABLE
    )


def test_delayed_production_callbacks_map_one_continuous_frame_timeline() -> None:
    class Clock:
        monotonic_domain = "python/system-monotonic"

        def __init__(self) -> None:
            self.values = iter(
                (1_000_000_000, 1_300_000_000, 1_800_000_000)
            )

        @staticmethod
        def monotonic() -> float:
            return 1.0

        def monotonic_ns(self) -> int:
            return next(self.values)

    class SoundDevice:
        @staticmethod
        def InputStream(**_kwargs):
            raise AssertionError("native stream need not open")

    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    backend = MonotonicSoundDeviceAudioBackend(SoundDevice(), Clock())
    handle = backend.create_input(
        stream_spec(sample_rate_hz=10, frames_per_buffer=2),
        lambda samples, _frames, timestamp, _status: (
            ring.append(samples, callback_monotonic_ns=timestamp)
            or BackendCallbackDirective.CONTINUE
        ),
        lambda _fault: None,
    )
    samples = np.zeros((2, 1), dtype=np.float32)

    handle._native_callback(
        samples,
        2,
        object(),
        BackendCallbackStatus.OK,
    )
    handle._native_callback(
        samples,
        2,
        object(),
        BackendCallbackStatus.OK,
    )
    handle._native_callback(
        samples,
        2,
        object(),
        BackendCallbackStatus.OK,
    )

    mapped = ring.map_trigger(1_450_000_000)
    assert [
        (item.start_cursor, item.end_cursor)
        for item in ring.block_metadata
    ] == [(0, 2), (2, 4), (4, 6)]
    assert mapped.status is not _MappingStatus.TIMELINE_GAP
    assert mapped.status is _MappingStatus.AVAILABLE


def test_large_timeline_overlap_still_faults_without_partial_append() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    first = slot._callback_for_generation(
        generation,
        callback_block()[:2],
        2,
        1_000_000_000,
        BackendCallbackStatus.OK,
    )
    clock.advance_ns(1_000_000_000)
    before_cursor = slot._ring.write_cursor

    second = slot._callback_for_generation(
        generation,
        callback_block()[:2],
        2,
        899_999_999,
        BackendCallbackStatus.OK,
    )

    assert first is BackendCallbackDirective.CONTINUE
    assert second is BackendCallbackDirective.STOP
    assert slot._fault is not None
    assert slot._ring.write_cursor == before_cursor


def test_timestamp_rollback_and_format_change_fault_without_partial_append() -> None:
    slot, clock = make_slot()
    generation = slot._generation
    assert (
        slot._callback_for_generation(
            generation,
            callback_block(),
            10,
            clock(),
            BackendCallbackStatus.OK,
        )
        is BackendCallbackDirective.CONTINUE
    )
    before_cursor = slot._ring.write_cursor
    clock.advance_ns(1_000_000_000)

    directive = slot._callback_for_generation(
        generation,
        callback_block(),
        10,
        999_999_999,
        BackendCallbackStatus.OK,
    )

    assert directive is BackendCallbackDirective.STOP
    assert slot._fault is not None
    assert slot._ring.write_cursor == before_cursor

    other, other_clock = make_slot()
    other_generation = other._generation
    malformed = np.zeros((10, 1), dtype=np.float32)
    assert (
        other._callback_for_generation(
            other_generation,
            malformed,
            10,
            other_clock(),
            BackendCallbackStatus.OK,
        )
        is BackendCallbackDirective.STOP
    )
    assert other._ring.write_cursor == 0


def test_callback_delay_maps_to_adjacent_frame_without_partial_audio() -> None:
    ring = make_ring(sample_rate_hz=10, frames_per_buffer=2)
    append_block(ring, block(0, frames=2), callback_monotonic_ns=1_000_000_000)
    append_block(ring, block(2, frames=2), callback_monotonic_ns=1_300_000_000)

    delayed = ring.map_trigger(1_050_000_000)
    future = ring.map_trigger(1_300_000_000)

    assert delayed == type(delayed)(_MappingStatus.AVAILABLE, 2)
    assert future == type(future)(_MappingStatus.NOT_YET_AVAILABLE, None)


def test_binding_during_callback_delay_starts_at_next_continuous_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, clock = open_ready_pool(monkeypatch)
    clock.advance_ns(300_000_000)
    backend.handles["mic-1"].emit(
        np.zeros((2, 2), dtype=np.float32),
        callback_monotonic_ns=clock(),
    )
    binding = pool.reserve_capture("run-gap", "mic-1")

    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=1_050_000_000,
        target_frame_count=2,
    )
    outcome = completion.wait(1.0)

    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert outcome.audio is not None
    assert outcome.audio.start_frame_cursor == 2
    assert outcome.audio.frame_count == 2
    assert outcome.error is None
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}


def test_long_active_capture_does_not_revalidate_evicted_copied_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("long-run", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=12,
    )

    for value in range(1, 7):
        clock.advance_ns(200_000_000)
        directive = backend.handles["mic-1"].emit(
            np.full((2, 2), float(value), dtype=np.float32),
            callback_monotonic_ns=clock(),
        )
        assert directive is BackendCallbackDirective.CONTINUE

    completion.wait(0.2)
    outcome = completion.result_if_ready()
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert outcome.audio is not None
    np.testing.assert_array_equal(
        outcome.audio.samples[:, 0],
        np.repeat(np.arange(1.0, 7.0, dtype=np.float32), 2),
    )


def test_reserved_binding_faulted_before_start_returns_stable_failed_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-fault-before-start", "mic-1")
    record = pool._bindings[binding.binding_id]
    allocation_calls = 0
    real_empty = np.empty

    def tracked_empty(*args, **kwargs):
        nonlocal allocation_calls
        allocation_calls += 1
        return real_empty(*args, **kwargs)

    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.np.empty",
        tracked_empty,
    )
    backend.handles["mic-1"].fault(summary="fault after reservation")

    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=2,
    )
    outcome = completion.result_if_ready()

    assert completion is binding._completion
    assert outcome is not None
    assert outcome.status is CaptureOutcomeStatus.FAILED
    assert outcome.audio is None
    assert outcome.error is not None
    assert outcome.error.code is ResidentErrorCode.FAULT
    assert allocation_calls == 0
    assert record.buffer is None
    assert pool._capture_permits == 0
    assert pool._bindings == {}
    assert pool._run_ids == {}
    assert (
        pool.start_capture(
            binding,
            trigger_monotonic_ns=clock(),
            target_frame_count=2,
        )
        is completion
    )
    assert completion.result_if_ready() is outcome


@pytest.mark.parametrize(
    ("trigger_monotonic_ns", "target_frame_count"),
    [
        (True, 2),
        (0, 2),
        (-1, 2),
        (1.0, 2),
        ("1", 2),
        (1_000_000_000, True),
        (1_000_000_000, 0),
        (1_000_000_000, -1),
        (1_000_000_000, 2.0),
        (1_000_000_000, "2"),
    ],
)
def test_start_malformed_arguments_leave_reserved_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    trigger_monotonic_ns: object,
    target_frame_count: object,
) -> None:
    pool, _, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    record = pool._bindings[binding.binding_id]

    with pytest.raises(ResidentRequestError) as raised:
        pool.start_capture(
            binding,
            trigger_monotonic_ns=trigger_monotonic_ns,  # type: ignore[arg-type]
            target_frame_count=target_frame_count,  # type: ignore[arg-type]
        )

    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert record.state == "RESERVED"
    assert record.buffer is None
    assert record.trigger_monotonic_ns is None
    assert record.target_frame_count is None
    assert pool._capture_permits == 1
    assert pool._bindings[binding.binding_id] is record
    assert pool._run_ids[binding.run_id] == binding.binding_id
    assert binding._completion.result_if_ready() is None


def test_identical_active_start_returns_same_completion_and_conflict_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=4,
    )
    record = pool._bindings[binding.binding_id]
    buffer = record.buffer

    assert (
        pool.start_capture(
            binding,
            trigger_monotonic_ns=clock(),
            target_frame_count=4,
        )
        is completion
    )
    with pytest.raises(ResidentRequestError) as raised:
        pool.start_capture(
            binding,
            trigger_monotonic_ns=clock(),
            target_frame_count=3,
        )
    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert record.buffer is buffer
    assert record.target_frame_count == 4


def test_known_terminal_start_returns_same_completion_without_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    cancelled = pool.cancel_capture(binding)
    before = (
        pool._capture_permits,
        dict(pool._bindings),
        dict(pool._run_ids),
        cancelled.outcome,
    )

    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=2,
    )

    assert completion is binding._completion
    assert completion.result_if_ready() is cancelled.outcome
    assert (
        pool._capture_permits,
        pool._bindings,
        pool._run_ids,
        completion.result_if_ready(),
    ) == before


def test_start_rejects_unknown_and_foreign_binding_without_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    foreign_pool, _, _ = open_ready_pool(monkeypatch)
    foreign = foreign_pool.reserve_capture("foreign", "mic-1")

    for token in (object.__new__(type(binding)), foreign):
        with pytest.raises(ResidentRequestError) as raised:
            pool.start_capture(
                token,
                trigger_monotonic_ns=clock(),
                target_frame_count=2,
            )
        assert raised.value.error.code is ResidentErrorCode.INVALID
    assert pool._bindings[binding.binding_id].state == "RESERVED"
    assert pool._capture_permits == 1


def test_target_over_maximum_leaves_reservation_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")

    with pytest.raises(ResidentRequestError) as raised:
        pool.start_capture(
            binding,
            trigger_monotonic_ns=clock(),
            target_frame_count=101,
        )

    assert raised.value.error.code is ResidentErrorCode.INVALID
    assert pool._bindings[binding.binding_id].state == "RESERVED"
    assert pool._bindings[binding.binding_id].buffer is None


def wait_outcome(binding, timeout: float = 1.0):
    outcome = binding._completion.wait(timeout)
    assert outcome is not None
    return outcome


def test_backlog_already_in_ring_is_copied_from_event_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=6)
    binding = pool.reserve_capture("run-1", "mic-1")

    pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=4,
    )
    outcome = wait_outcome(binding)

    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert outcome.audio is not None
    assert outcome.audio.samples[:, 0].tolist() == [0.0, 1.0, 2.0, 3.0]


def test_two_overlapping_bindings_receive_independent_exact_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    first = pool.reserve_capture("run-1", "mic-1")
    second = pool.reserve_capture("run-2", "mic-1")

    pool.start_capture(
        first,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=4,
    )
    pool.start_capture(
        second,
        trigger_monotonic_ns=1_200_000_000,
        target_frame_count=2,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=6)

    first_outcome = wait_outcome(first)
    second_outcome = wait_outcome(second)
    assert first_outcome.audio is not None
    assert second_outcome.audio is not None
    assert first_outcome.audio.samples[:, 0].tolist() == [0.0, 1.0, 2.0, 3.0]
    assert second_outcome.audio.samples[:, 0].tolist() == [2.0, 3.0]


def test_six_overlapping_bindings_keep_independent_lengths_and_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=4)
    bindings = [
        pool.reserve_capture(f"run-{index}", "mic-1")
        for index in range(6)
    ]
    for index, binding in enumerate(bindings):
        pool.start_capture(
            binding,
            trigger_monotonic_ns=1_000_000_000 + index * 100_000_000,
            target_frame_count=index + 1,
        )

    backend.handles["mic-1"].emit_numbered_frames(first=4, count=10)

    for index, binding in enumerate(bindings):
        outcome = wait_outcome(binding)
        assert outcome.audio is not None
        expected_first = index
        assert outcome.audio.samples[:, 0].tolist() == [
            float(value)
            for value in range(expected_first, expected_first + index + 1)
        ]


def test_completion_order_can_differ_from_reservation_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    long = pool.reserve_capture("run-long", "mic-1")
    short = pool.reserve_capture("run-short", "mic-1")
    pool.start_capture(
        long,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=6,
    )
    pool.start_capture(
        short,
        trigger_monotonic_ns=1_200_000_000,
        target_frame_count=2,
    )

    backend.handles["mic-1"].emit_numbered_frames(first=0, count=4)
    assert short._completion.wait(1.0) is not None
    assert long._completion.result_if_ready() is None
    backend.handles["mic-1"].emit_numbered_frames(first=4, count=2)
    assert long._completion.wait(1.0) is not None


def test_completed_audio_has_exact_correlation_shape_and_immutable_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, clock = open_ready_pool(
        monkeypatch,
        sample_rate_hz=10,
        frames_per_buffer=2,
    )
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=3,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=4)

    outcome = wait_outcome(binding)

    assert outcome.binding is binding
    assert outcome.status is CaptureOutcomeStatus.COMPLETED
    assert outcome.error is None
    assert isinstance(outcome.audio, CapturedAudio)
    audio = outcome.audio
    assert audio.binding is binding
    assert audio.run_id == "run-1"
    assert audio.hardware_id == "mic-1"
    assert audio.trigger_monotonic_ns == 1_000_000_000
    assert audio.start_frame_cursor == 2
    assert audio.frame_count == 3
    assert audio.sample_rate_hz == 10
    assert audio.channel_indices == (0,)
    assert audio.samples.shape == (3, 1)
    assert audio.samples.dtype == np.dtype(np.float32)
    assert audio.samples.flags.c_contiguous
    assert not audio.samples.flags.writeable
    assert type(audio.samples.base) is bytes
    assert audio.completion_monotonic_ns == clock()
    assert completion is binding._completion
    with pytest.raises(ValueError):
        audio.samples.setflags(write=True)


def test_completed_audio_preserves_selected_channel_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock(initial_ns=1_000_000_000)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.time.monotonic_ns",
        clock,
    )
    backend = FakeBackend(clock=clock)
    opened = ResidentAudioPool.open(
        [
            stream_spec(
                sample_rate_hz=10,
                frames_per_buffer=2,
                input_channel_count=4,
                capture_channel_indices=(0, 2),
            )
        ],
        backend=backend,
        max_capture_frames=10,
        startup_stream_timeout_s=0.1,
        fault_close_timeout_s=0.1,
        shutdown_timeout_s=0.1,
    )
    binding = opened.pool.reserve_capture("run-channels", "mic-1")
    opened.pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=2,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=10, count=2)

    outcome = wait_outcome(binding)

    assert outcome.audio is not None
    assert outcome.audio.channel_indices == (0, 2)
    assert outcome.audio.samples.tolist() == [
        [10.0, 2010.0],
        [11.0, 2011.0],
    ]


def test_no_global_completion_handler_or_replaceable_callback_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")

    names = set(vars(pool)) | set(dir(pool._slots["mic-1"]))

    assert not any("handler" in name for name in names)
    assert not hasattr(binding._completion, "set_callback")
    assert not hasattr(binding._completion, "publish")


def test_terminal_caller_waits_only_for_existing_pool_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, _, clock = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    pool.start_capture(
        binding,
        trigger_monotonic_ns=clock(),
        target_frame_count=2,
    )
    started = threading.Event()
    finished = threading.Event()
    result: dict[str, object] = {}

    def cancel() -> None:
        started.set()
        result["value"] = pool.cancel_capture(binding)
        finished.set()

    with pool._lock:
        thread = threading.Thread(target=cancel)
        thread.start()
        assert started.wait(1.0)
        assert not finished.wait(0.05)
        assert binding._completion.result_if_ready() is None
        assert pool._capture_permits == 1
        assert binding.binding_id in pool._bindings

    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert finished.is_set()
    cancelled = result["value"]
    assert cancelled.status is CancelStatus.CANCELLED
    source = inspect.getsource(pool._commit_terminal_locked)
    assert ".wait(" not in source
    assert "Condition" not in source
    assert "sleep(" not in source


def test_completed_handoff_publishes_then_retires_under_same_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=2,
    )
    record = pool._bindings[binding.binding_id]
    underlying_event = completion._event
    event_visible = threading.Event()
    release_set = threading.Event()
    state_at_set: dict[str, object] = {}

    class BarrierEvent:
        def is_set(self) -> bool:
            return underlying_event.is_set()

        def wait(self, timeout: float | None = None) -> bool:
            return underlying_event.wait(timeout)

        def set(self) -> None:
            state_at_set["outcome"] = completion._outcome
            state_at_set["proof"] = completion._terminal_proof
            state_at_set["buffer"] = record.buffer
            state_at_set["permit"] = pool._capture_permits
            state_at_set["binding"] = pool._bindings.get(binding.binding_id)
            state_at_set["run"] = pool._run_ids.get(binding.run_id)
            underlying_event.set()
            event_visible.set()
            assert release_set.wait(1.0)

    completion._event = BarrierEvent()  # type: ignore[assignment]
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=2)
    assert event_visible.wait(1.0)

    outcome = completion.result_if_ready()
    assert outcome is not None
    assert completion.wait(0) is outcome
    operation_started = threading.Event()
    operation_done = threading.Event()
    reserved: dict[str, object] = {}

    def reserve_after_handoff() -> None:
        operation_started.set()
        reserved["binding"] = pool.reserve_capture("run-2", "mic-1")
        operation_done.set()

    operation_thread = threading.Thread(target=reserve_after_handoff)
    operation_thread.start()
    assert operation_started.wait(1.0)
    assert not operation_done.wait(0.05)

    assert state_at_set["outcome"] is outcome
    assert state_at_set["proof"] is completion._terminal_proof
    assert state_at_set["buffer"] is None
    assert state_at_set["permit"] == 1
    assert state_at_set["binding"] is record
    assert state_at_set["run"] == binding.binding_id

    release_set.set()
    operation_thread.join(timeout=1.0)
    assert not operation_thread.is_alive()
    replacement = reserved["binding"]
    assert replacement.run_id == "run-2"
    assert pool._capture_permits == 1
    assert binding.binding_id not in pool._bindings
    assert binding.run_id not in pool._run_ids
    pool.cancel_capture(replacement)


def test_completed_builder_has_no_mutable_reference_after_worker_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=2,
    )
    record = pool._bindings[binding.binding_id]
    assert record.buffer is not None
    builder_ref = weakref.ref(record.buffer)
    owner = pool._slots["mic-1"]._drain_owner
    assert owner is not None

    backend.handles["mic-1"].emit_numbered_frames(first=0, count=2)
    outcome = wait_outcome(binding)
    for _ in range(20):
        gc.collect()
        if builder_ref() is None:
            break
        threading.Event().wait(0.005)

    assert record.buffer is None
    assert builder_ref() is None
    assert outcome.audio is not None
    assert not outcome.audio.samples.flags.writeable
    assert type(outcome.audio.samples.base) is bytes
    assert not any(
        isinstance(getattr(owner, field.name), np.ndarray)
        for field in fields(owner)
    )
    target = owner.thread._target
    assert target is not None
    assert not any(
        isinstance(cell.cell_contents, np.ndarray)
        for cell in (target.__closure__ or ())
    )


def test_repeated_completion_reads_return_exact_same_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    completion = pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=1,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=1)
    outcome = wait_outcome(binding)

    assert completion.wait(0) is outcome
    assert completion.wait(None) is outcome
    assert completion.result_if_ready() is outcome
    assert pool.capture_completion(binding) is completion


def test_slow_downstream_does_not_hold_capture_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    completed = pool.reserve_capture("completed", "mic-1")
    pool.start_capture(
        completed,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=1,
    )
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=1)
    outcome = wait_outcome(completed)
    consumer_started = threading.Event()
    release_consumer = threading.Event()

    def slow_consumer() -> None:
        assert outcome.audio is not None
        consumer_started.set()
        release_consumer.wait(1.0)

    consumer = threading.Thread(target=slow_consumer)
    consumer.start()
    assert consumer_started.wait(1.0)

    replacements = [
        pool.reserve_capture(f"replacement-{index}", "mic-1")
        for index in range(6)
    ]
    assert pool._capture_permits == 6
    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("seventh", "mic-1")
    assert raised.value.error.code is ResidentErrorCode.CAPACITY

    release_consumer.set()
    consumer.join(timeout=1.0)
    for replacement in replacements:
        pool.cancel_capture(replacement)


def test_seventh_reservation_succeeds_immediately_after_handoff_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    completing = pool.reserve_capture("completing", "mic-1")
    pool.start_capture(
        completing,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=1,
    )
    held = [
        pool.reserve_capture(f"held-{index}", "mic-1")
        for index in range(5)
    ]
    with pytest.raises(ResidentRequestError) as raised:
        pool.reserve_capture("blocked", "mic-1")
    assert raised.value.error.code is ResidentErrorCode.CAPACITY

    backend.handles["mic-1"].emit_numbered_frames(first=0, count=1)
    assert wait_outcome(completing).status is CaptureOutcomeStatus.COMPLETED
    replacement = pool.reserve_capture("replacement", "mic-1")

    assert pool._capture_permits == 6
    pool.cancel_capture(replacement)
    for binding in held:
        pool.cancel_capture(binding)


def test_losing_terminal_candidate_reuses_winner_without_second_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, _ = open_ready_pool(monkeypatch)
    binding = pool.reserve_capture("run-1", "mic-1")
    pool.start_capture(
        binding,
        trigger_monotonic_ns=1_000_000_000,
        target_frame_count=1,
    )
    record = pool._bindings[binding.binding_id]
    backend.handles["mic-1"].emit_numbered_frames(first=0, count=1)
    winner = wait_outcome(binding)

    with pool._lock:
        observed, won = pool._commit_terminal_locked(
            record,
            CaptureOutcomeStatus.CANCELLED,
            ResidentError(ResidentErrorCode.CANCELLED, "loser"),
        )

    assert not won
    assert observed is winner
    assert pool._capture_permits == 0
    assert record.buffer is None


def test_hundreds_of_retained_callers_do_not_create_terminal_pool_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool, backend, clock = open_ready_pool(monkeypatch)
    retained: list[tuple[object, object, object]] = []

    for index in range(200):
        binding = pool.reserve_capture(f"run-{index}", "mic-1")
        completion = pool.start_capture(
            binding,
            trigger_monotonic_ns=clock(),
            target_frame_count=1,
        )
        backend.handles["mic-1"].emit_numbered_frames(first=index, count=1)
        outcome = completion.wait(1.0)
        assert outcome is not None
        retained.append((binding, completion, outcome))
        assert pool._bindings == {}
        assert pool._run_ids == {}
        assert pool._capture_permits == 0

    assert len(retained) == 200
    for binding, completion, outcome in retained:
        assert pool.capture_completion(binding) is completion
        assert completion.result_if_ready() is outcome
    forbidden_fragments = ("terminal", "fifo", "registry", "weak", "queue")
    assert not any(
        fragment in name.lower()
        for name in vars(pool)
        for fragment in forbidden_fragments
    )
