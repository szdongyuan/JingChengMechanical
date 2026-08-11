from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.service.analysis.resident_audio import (
    ArtifactState,
    BlockTimeQuality,
    CaptureArtifactStore,
    CaptureErrorCode,
    CaptureOwner,
    CaptureOwnerState,
    CaptureRequest,
    CapturedAudioBlock,
    MappingStatus,
    OwnerControlKind,
    OwnerTerminalEvent,
    StreamReadyToken,
    TimelineMapping,
    TimestampedBlockRing,
)


GIB = 1024**3


class MappingTimeline:
    def __init__(
        self,
        mapping: TimelineMapping,
        *,
        mappings: list[TimelineMapping] | None = None,
    ) -> None:
        self.mapping = mapping
        self.mappings = list(mappings or [])
        self.triggers: list[float] = []

    def map_trigger(self, trigger_monotonic: float) -> TimelineMapping:
        self.triggers.append(trigger_monotonic)
        if self.mappings:
            return self.mappings.pop(0)
        return self.mapping


def mapped(sample_index: int, sample_time: float | None = None) -> TimelineMapping:
    return TimelineMapping(
        MappingStatus.MAPPED,
        sample_index,
        10.0 + sample_index / 10.0 if sample_time is None else sample_time,
    )


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


def make_store(tmp_path: Path) -> CaptureArtifactStore:
    return CaptureArtifactStore(
        tmp_path / "staging",
        quota_bytes=1_000_000,
        minimum_free_bytes=GIB,
        free_space_bytes=lambda _: GIB + 1_000_000,
    )


def token(*, earliest_sample: int = 0, latest_sample: int = 100) -> StreamReadyToken:
    return StreamReadyToken(
        hardware_id="mic-1",
        pool_epoch="epoch-1",
        stream_generation=3,
        sample_rate=10.0,
        channels=2,
        earliest_mappable_monotonic=10.0,
        latest_mappable_monotonic=20.0,
        earliest_sample=earliest_sample,
        latest_sample=latest_sample,
    )


def request(
    run_id: str,
    *,
    trigger: float = 10.0,
    duration: float = 1.0,
) -> CaptureRequest:
    return CaptureRequest(
        run_id=run_id,
        trigger_monotonic=trigger,
        record_duration=duration,
        stream_token=token(),
    )


def block(start: int, frames: int = 4) -> CapturedAudioBlock:
    values = np.column_stack(
        (
            np.arange(start, start + frames, dtype=np.float32),
            np.arange(start, start + frames, dtype=np.float32) + 1_000,
        )
    )
    return CapturedAudioBlock(
        pool_epoch="epoch-1",
        hardware_id="mic-1",
        stream_generation=3,
        sample_start=start,
        sample_end=start + frames,
        data=np.ascontiguousarray(values),
        time_quality=BlockTimeQuality.CONFIRMED,
        monotonic_start=10.0 + start / 10.0,
        monotonic_end=10.0 + (start + frames) / 10.0,
    )


def make_ring(*blocks: CapturedAudioBlock) -> TimestampedBlockRing:
    ring = TimestampedBlockRing(
        pool_epoch="epoch-1",
        hardware_id="mic-1",
        stream_generation=3,
        sample_rate=10.0,
    )
    for item in blocks:
        ring.append(item)
    return ring


def make_owner(
    tmp_path: Path,
    ring: TimestampedBlockRing,
    *,
    run_id: str,
    start: int,
    target_frames: int,
    trigger: float = 10.0,
    terminal_events: list[OwnerTerminalEvent] | None = None,
    auto_switch: bool = True,
    writer=None,
    live_queue=None,
    monotonic_clock=lambda: 10.0,
):
    events = terminal_events if terminal_events is not None else []
    store = make_store(tmp_path)
    artifact_writer = writer or store.allocate_writer(
        run_id=run_id,
        target_frames=target_frames,
        channels=2,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=trigger,
    )
    controls = []
    owner_ref: list[CaptureOwner] = []

    def on_control(event) -> None:
        controls.append(event)
        if (
            auto_switch
            and event.kind is OwnerControlKind.REQUEST_LIVE_SWITCH
        ):
            owner_ref[0].switch_to_live(ring.latest_cursor or 0)

    owner_kwargs = {}
    if live_queue is not None:
        owner_kwargs["live_queue"] = live_queue
    owner = CaptureOwner(
        request=request(
            run_id,
            trigger=trigger,
            duration=target_frames / 10.0,
        ),
        target_frames=target_frames,
        commit_attempt_id=f"attempt-{run_id}",
        owner_capability=f"capability-{run_id}",
        ring=ring,
        writer=artifact_writer,
        terminal_sink=events.append,
        control_sink=on_control,
        monotonic_clock=monotonic_clock,
        **owner_kwargs,
    )
    owner_ref.append(owner)
    return owner, store, events, controls


def activate(owner: CaptureOwner, start: int, *, now: float = 10.0) -> None:
    owner.open_commit_token()
    owner.activate(
        MappingTimeline(mapped(start)),
        commit_monotonic=now,
    )


def read_artifact(event: OwnerTerminalEvent, frames: int) -> np.ndarray:
    assert event.artifact is not None
    return np.array(
        np.memmap(
            event.artifact.path,
            dtype=np.float32,
            mode="r",
            shape=(frames, 2),
        )
    )


def test_owner_snapshot_reports_value_only_previous_to_current_transitions(
    tmp_path: Path,
) -> None:
    owner, _, _, _ = make_owner(
        tmp_path / "owner-transition-diagnostics",
        make_ring(block(0)),
        run_id="owner-transition",
        start=0,
        target_frames=100,
    )
    prepared = owner.snapshot()
    assert prepared.previous_state is None
    assert prepared.state is CaptureOwnerState.PREPARED

    activate(owner, 0)
    wait_until(lambda: owner.snapshot().state is CaptureOwnerState.LIVE)
    live = owner.snapshot()
    assert live.previous_state is CaptureOwnerState.CATCHING_UP
    assert live.state is CaptureOwnerState.LIVE

    assert owner.cancel("transition diagnostics")
    owner.join(1.0)
    canceled = owner.snapshot()
    assert canceled.previous_state is CaptureOwnerState.LIVE
    assert canceled.state is CaptureOwnerState.CANCELED
    serialized = json.dumps(dataclasses.asdict(canceled), default=str)
    assert "capability-owner-transition" not in serialized
    assert "attempt-owner-transition" not in serialized
    assert "OwnerTerminalEvent" not in serialized
    assert "Exception" not in serialized


def test_overlapping_owners_persist_exact_independent_ranges_and_metadata(
    tmp_path: Path,
) -> None:
    ring = make_ring(*(block(start) for start in range(0, 20, 4)))
    first, _, first_events, _ = make_owner(
        tmp_path / "first",
        ring,
        run_id="same-config-run-1",
        start=3,
        target_frames=6,
    )
    second, _, second_events, _ = make_owner(
        tmp_path / "second",
        ring,
        run_id="same-config-run-2",
        start=5,
        target_frames=8,
    )

    activate(first, 3)
    activate(second, 5)
    wait_until(lambda: len(first_events) == len(second_events) == 1)

    np.testing.assert_array_equal(
        read_artifact(first_events[0], 6)[:, 0],
        np.arange(3, 9, dtype=np.float32),
    )
    np.testing.assert_array_equal(
        read_artifact(second_events[0], 8)[:, 0],
        np.arange(5, 13, dtype=np.float32),
    )
    assert first_events[0].state is CaptureOwnerState.COMPLETED
    assert second_events[0].state is CaptureOwnerState.COMPLETED
    assert first_events[0].artifact is not None
    assert first_events[0].artifact.sample_start == 3
    assert first_events[0].artifact.sample_end == 9
    assert first_events[0].artifact.captured_frames == 6
    assert first_events[0].artifact.channels == 2
    assert first_events[0].artifact.dtype == "float32"
    assert first_events[0].artifact.run_id != second_events[0].artifact.run_id
    assert first.snapshot().credits_occupied == 0
    assert first.snapshot().catch_up_references == 0
    assert second.snapshot().credits_occupied == 0
    first.join(1)
    second.join(1)


def test_prepared_owner_is_dormant_and_late_history_switches_to_live_without_gap(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0), block(4))
    owner, _, events, controls = make_owner(
        tmp_path,
        ring,
        run_id="late",
        start=2,
        target_frames=12,
    )

    assert owner.offer_live(block(8)) is False
    assert owner.snapshot().state is CaptureOwnerState.PREPARED
    with pytest.raises(RuntimeError, match="commit token"):
        owner.activate(
            MappingTimeline(mapped(2)),
            commit_monotonic=10.0,
        )

    activate(owner, 2)
    wait_until(lambda: owner.snapshot().state is CaptureOwnerState.LIVE)
    assert any(
        event.kind is OwnerControlKind.REQUEST_LIVE_SWITCH for event in controls
    )
    assert owner.offer_live(block(8)) is True
    assert owner.offer_live(block(12)) is True
    wait_until(lambda: len(events) == 1)

    np.testing.assert_array_equal(
        read_artifact(events[0], 12)[:, 0],
        np.arange(2, 14, dtype=np.float32),
    )
    assert owner.snapshot().written_frames == 12
    owner.join(1)


def test_pending_mapping_keeps_original_trigger_and_uses_two_second_deadline(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0))
    now = [100.0]
    owner, _, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="pending",
        start=4,
        target_frames=4,
        trigger=101.0,
        monotonic_clock=lambda: now[0],
    )
    timeline = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE),
        mappings=[
            TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE),
            TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE),
            mapped(4, 101.0),
        ],
    )
    owner.open_commit_token()
    owner.activate(timeline, commit_monotonic=100.0)

    snapshot = owner.snapshot()
    assert snapshot.state is CaptureOwnerState.WAITING_FOR_MAPPING
    assert snapshot.capture_start_cursor is None
    assert snapshot.next_write_cursor is None
    assert snapshot.mapping_deadline == 103.0
    now[0] = 102.9
    assert owner.advance_pending_mapping(timeline) is False
    assert timeline.triggers == [101.0, 101.0]
    ring.append(block(4))
    assert owner.advance_pending_mapping(timeline) is True
    wait_until(lambda: len(events) == 1)
    np.testing.assert_array_equal(
        read_artifact(events[0], 4)[:, 0],
        np.arange(4, 8, dtype=np.float32),
    )
    assert events[0].artifact is not None
    assert events[0].artifact.sample_start == 4
    assert events[0].artifact.capture_start_monotonic == 101.0
    assert events[0].artifact.time_quality == "CONFIRMED"
    assert timeline.triggers == [101.0, 101.0, 101.0]
    owner.join(1)

    timed_out, _, timeout_events, _ = make_owner(
        tmp_path / "timeout",
        ring,
        run_id="timeout",
        start=8,
        target_frames=2,
        trigger=101.0,
        monotonic_clock=lambda: 103.0,
    )
    waiting = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)
    )
    timed_out.open_commit_token()
    timed_out.activate(waiting, commit_monotonic=100.0)
    assert timed_out.advance_pending_mapping(waiting) is False
    wait_until(lambda: len(timeout_events) == 1)
    assert timeout_events[0].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    timed_out.join(1)


def test_between_sample_trigger_binds_distinct_precise_mapped_start(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0))
    owner, store, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="between-sample",
        start=1,
        target_frames=2,
        trigger=10.00001,
    )
    owner.open_commit_token()
    owner.activate(
        MappingTimeline(
            mapped(1, 10.1)
        ),
        commit_monotonic=10.01,
    )
    wait_until(lambda: len(events) == 1)

    artifact = events[0].artifact
    assert artifact is not None
    assert artifact.trigger_monotonic == pytest.approx(10.00001)
    assert artifact.capture_start_monotonic == pytest.approx(10.1)
    assert artifact.sample_start == 1
    assert artifact.sample_end == 3
    assert store.publication_pending(artifact.artifact_id) is True
    owner.join(1)


class SequenceClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self._last = values[-1]
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            try:
                self._last = next(self._values)
            except StopIteration:
                pass
            return self._last


def test_waiting_owner_autonomously_expires_without_mapping_advance(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0))
    owner, store, events, controls = make_owner(
        tmp_path,
        ring,
        run_id="autonomous-timeout",
        start=4,
        target_frames=2,
        monotonic_clock=SequenceClock(10.0, 10.0, 12.0),
    )
    timeline = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)
    )
    owner.open_commit_token()
    owner.activate(timeline, commit_monotonic=10.0)

    wait_until(lambda: len(events) == 1)
    assert events[0].state is CaptureOwnerState.FAILED
    assert events[0].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    assert sum(
        event.kind is OwnerControlKind.DETACH for event in controls
    ) == 1
    assert owner.snapshot().credits_occupied == 0
    assert owner.snapshot().live_queued == 0
    assert owner.join(1) is True
    assert store.state(events[0].artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0


@pytest.mark.parametrize("timeout_wins", [False, True])
def test_mapping_and_autonomous_timeout_linearize_once(
    tmp_path: Path,
    timeout_wins: bool,
) -> None:
    clock = (
        SequenceClock(10.0, 10.0, 12.0)
        if timeout_wins
        else SequenceClock(10.0)
    )
    ring = make_ring(block(0))
    owner, store, events, controls = make_owner(
        tmp_path / str(timeout_wins),
        ring,
        run_id=f"mapping-timeout-{timeout_wins}",
        start=4,
        target_frames=2,
        monotonic_clock=clock,
    )
    waiting = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)
    )
    owner.open_commit_token()
    owner.activate(waiting, commit_monotonic=10.0)

    if timeout_wins:
        wait_until(lambda: len(events) == 1)
        assert owner.advance_pending_mapping(
            MappingTimeline(mapped(4, 10.4)),
            now_monotonic=10.5,
        ) is False
        expected_state = CaptureOwnerState.FAILED
        expected_code = CaptureErrorCode.TIMING_UNAVAILABLE
    else:
        ring.append(block(4))
        assert owner.advance_pending_mapping(
            MappingTimeline(mapped(4, 10.4)),
            now_monotonic=10.5,
        ) is True
        wait_until(lambda: len(events) == 1)
        expected_state = CaptureOwnerState.COMPLETED
        expected_code = None

    assert events[0].state is expected_state
    assert events[0].error_code is expected_code
    assert len(events) == 1
    assert sum(
        event.kind is OwnerControlKind.DETACH for event in controls
    ) == 1
    assert owner.snapshot().credits_occupied == 0
    assert owner.join(1) is True
    if timeout_wins:
        assert store.state(events[0].artifact_id) is ArtifactState.DELETED
        assert store.reserved_bytes == 0


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
@pytest.mark.parametrize("timeout_wins", [False, True])
def test_cancel_shutdown_and_autonomous_timeout_emit_once(
    tmp_path: Path,
    action: str,
    timeout_wins: bool,
) -> None:
    clock = (
        SequenceClock(10.0, 10.0, 12.0)
        if timeout_wins
        else SequenceClock(10.0)
    )
    owner, store, events, controls = make_owner(
        tmp_path / f"{action}-{timeout_wins}",
        make_ring(block(0)),
        run_id=f"{action}-timeout-{timeout_wins}",
        start=4,
        target_frames=2,
        monotonic_clock=clock,
    )
    owner.open_commit_token()
    owner.activate(
        MappingTimeline(
            TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)
        ),
        commit_monotonic=10.0,
    )
    if timeout_wins:
        wait_until(lambda: len(events) == 1)

    won = owner.cancel("race cancel") if action == "cancel" else owner.shutdown()
    wait_until(lambda: len(events) == 1)
    assert won is (not timeout_wins)
    assert len(events) == 1
    assert events[0].error_code is (
        CaptureErrorCode.TIMING_UNAVAILABLE
        if timeout_wins
        else (
            CaptureErrorCode.CAPTURE_CANCELED
            if action == "cancel"
            else CaptureErrorCode.POOL_SHUTTING_DOWN
        )
    )
    assert sum(
        event.kind is OwnerControlKind.DETACH for event in controls
    ) == 1
    snapshot = owner.snapshot()
    assert snapshot.credits_occupied == 0
    assert snapshot.catch_up_references == 0
    assert snapshot.live_queued == 0
    assert owner.join(1) is True
    assert store.state(events[0].artifact_id) is ArtifactState.DELETED
    assert store.reserved_bytes == 0


@pytest.mark.parametrize("now", [103.0, 103.001])
def test_mapping_that_first_becomes_available_at_or_after_deadline_is_rejected(
    tmp_path: Path,
    now: float,
) -> None:
    ring = make_ring(block(0), block(4))
    owner, store, events, _ = make_owner(
        tmp_path / str(now),
        ring,
        run_id=f"deadline-{now}",
        start=4,
        target_frames=2,
        trigger=101.0,
        monotonic_clock=lambda: now,
    )
    timeline = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE),
        mappings=[
            TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE),
            mapped(4, 101.0),
        ],
    )
    owner.open_commit_token()
    owner.activate(timeline, commit_monotonic=100.0)

    assert owner.advance_pending_mapping(timeline) is False
    wait_until(lambda: len(events) == 1)
    assert events[0].state is CaptureOwnerState.FAILED
    assert events[0].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    assert events[0].artifact is None
    assert owner.snapshot().capture_start_cursor is None
    assert store.state(events[0].artifact_id).value in {
        "DELETED",
        "DELETE_PENDING",
    }
    owner.join(1)


def test_initial_activation_checks_deadline_before_mapping(tmp_path: Path) -> None:
    ring = make_ring(block(0))
    owner, store, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="initial-deadline",
        start=0,
        target_frames=2,
        trigger=101.0,
        monotonic_clock=lambda: 103.0,
    )
    timeline = MappingTimeline(mapped(0, 101.0))
    owner.open_commit_token()

    assert (
        owner.activate(timeline, commit_monotonic=100.0)
        is CaptureOwnerState.FAILED
    )
    wait_until(lambda: len(events) == 1)
    assert timeline.triggers == []
    assert events[0].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    assert store.state(events[0].artifact_id) is ArtifactState.DELETED
    owner.join(1)


def test_initial_activation_rechecks_deadline_before_accepting_mapping(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0))
    clock_values = iter((102.9, 103.0))
    owner, store, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="activation-crosses-deadline",
        start=0,
        target_frames=2,
        trigger=101.0,
        monotonic_clock=lambda: next(clock_values),
    )
    timeline = MappingTimeline(mapped(0, 101.0))
    owner.open_commit_token()

    assert (
        owner.activate(timeline, commit_monotonic=100.0)
        is CaptureOwnerState.FAILED
    )
    wait_until(lambda: len(events) == 1)
    assert timeline.triggers == [101.0]
    assert owner.snapshot().capture_start_cursor is None
    assert events[0].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    assert store.state(events[0].artifact_id) is ArtifactState.DELETED
    owner.join(1)


@pytest.mark.parametrize(
    ("action", "code", "state"),
    [
        ("generation", CaptureErrorCode.STREAM_GENERATION_CHANGED, CaptureOwnerState.FAILED),
        ("cancel", CaptureErrorCode.CAPTURE_CANCELED, CaptureOwnerState.CANCELED),
        ("shutdown", CaptureErrorCode.POOL_SHUTTING_DOWN, CaptureOwnerState.CANCELED),
    ],
)
def test_pending_mapping_terminal_categories(
    tmp_path: Path,
    action: str,
    code: CaptureErrorCode,
    state: CaptureOwnerState,
) -> None:
    ring = make_ring(block(0))
    owner, _, events, _ = make_owner(
        tmp_path / action,
        ring,
        run_id=action,
        start=4,
        target_frames=2,
    )
    timeline = MappingTimeline(
        TimelineMapping(MappingStatus.AFTER_CONFIRMED_RANGE)
    )
    owner.open_commit_token()
    owner.activate(timeline, commit_monotonic=10.0)

    if action == "generation":
        owner.stream_generation_changed()
    elif action == "cancel":
        owner.cancel("operator stop")
    else:
        owner.shutdown()

    wait_until(lambda: len(events) == 1)
    assert events[0].error_code is code
    assert events[0].state is state
    owner.join(1)


def test_trigger_expiry_and_mapped_cursor_eviction_have_distinct_categories(
    tmp_path: Path,
) -> None:
    ring = make_ring(*(block(start) for start in range(0, 32, 4)))
    assert ring.earliest_cursor is not None and ring.earliest_cursor > 0
    expired, _, expired_events, _ = make_owner(
        tmp_path / "expired",
        ring,
        run_id="expired",
        start=0,
        target_frames=2,
    )
    expired.open_commit_token()
    expired.activate(
        MappingTimeline(TimelineMapping(MappingStatus.EXPIRED)),
        commit_monotonic=10.0,
    )
    wait_until(lambda: len(expired_events) == 1)
    assert expired_events[0].error_code is CaptureErrorCode.TRIGGER_AUDIO_EXPIRED
    expired.join(1)

    backlog_ring = make_ring(*(block(start) for start in range(0, 20, 4)))
    backlog, _, backlog_events, _ = make_owner(
        tmp_path / "backlog",
        backlog_ring,
        run_id="backlog",
        start=0,
        target_frames=30,
        auto_switch=False,
    )
    backlog.open_commit_token()
    backlog.activate(
        MappingTimeline(mapped(0)),
        commit_monotonic=10.0,
        start_drain=False,
    )
    for start in range(20, 48, 4):
        backlog_ring.append(block(start))
    backlog.start_drain()
    wait_until(lambda: len(backlog_events) == 1)
    assert (
        backlog_events[0].error_code
        is CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG
    )
    backlog.join(1)


class PausingWriter:
    def __init__(self, delegate, pause_start: int) -> None:
        self.delegate = delegate
        self.pause_start = pause_start
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def artifact_id(self):
        return self.delegate.artifact_id

    @property
    def written_frames(self):
        return self.delegate.written_frames

    def bind_capture_start(self, **kwargs):
        return self.delegate.bind_capture_start(**kwargs)

    def write(self, audio_block):
        if audio_block.sample_start >= self.pause_start:
            self.entered.set()
            assert self.release.wait(2)
        return self.delegate.write(audio_block)

    def freeze(self):
        return self.delegate.freeze()

    def discard(self):
        return self.delegate.discard()


def test_eight_credits_include_inflight_and_ninth_live_reference_never_retained(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0, 1))
    store = make_store(tmp_path)
    delegate = store.allocate_writer(
        run_id="credits",
        target_frames=20,
        channels=2,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
    )
    writer = PausingWriter(delegate, pause_start=1)
    owner, _, events, controls = make_owner(
        tmp_path / "owner",
        ring,
        run_id="credits",
        start=0,
        target_frames=20,
        writer=writer,
    )
    activate(owner, 0)
    wait_until(lambda: owner.snapshot().state is CaptureOwnerState.LIVE)

    assert owner.offer_live(block(1, 1)) is True
    assert writer.entered.wait(1)
    for start in range(2, 9):
        assert owner.offer_live(block(start, 1)) is True
    snapshot = owner.snapshot()
    assert snapshot.credits_occupied == 8
    assert snapshot.in_flight is True
    assert snapshot.live_queued == 7
    assert snapshot.peak_credits == 8

    ninth = block(9, 1)
    assert owner.offer_live(ninth) is False
    assert owner.snapshot().peak_credits == 8
    assert any(event.kind is OwnerControlKind.DETACH for event in controls)
    writer.release.set()
    wait_until(lambda: len(events) == 1)
    assert events[0].error_code is CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG
    assert store.state(delegate.artifact_id).value in {"DELETED", "DELETE_PENDING"}
    owner.join(1)


class RejectingLiveQueue:
    def __init__(self, *, raises: bool) -> None:
        self.raises = raises
        self.push_calls = 0
        self.owner = None
        self.observed_credits: list[int] = []

    def try_push(self, _audio_block) -> bool:
        self.push_calls += 1
        assert self.owner is not None
        self.observed_credits.append(self.owner.snapshot().credits_occupied)
        if self.raises:
            raise RuntimeError("injected queue push failure")
        return False

    def popleft(self):
        raise AssertionError("rejected reference must never be dequeued")

    def clear(self) -> None:
        return None

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False


@pytest.mark.parametrize("raises", [False, True])
def test_live_queue_push_failure_returns_reserved_credit_and_is_owner_local(
    tmp_path: Path,
    raises: bool,
) -> None:
    ring = make_ring(block(0, 1))
    rejecting_queue = RejectingLiveQueue(raises=raises)
    failed, failed_store, failed_events, failed_controls = make_owner(
        tmp_path / "failed",
        ring,
        run_id=f"queue-failure-{raises}",
        start=0,
        target_frames=10,
        live_queue=rejecting_queue,
    )
    sibling, _, sibling_events, _ = make_owner(
        tmp_path / "sibling",
        ring,
        run_id=f"queue-sibling-{raises}",
        start=0,
        target_frames=10,
    )
    rejecting_queue.owner = failed
    activate(failed, 0)
    activate(sibling, 0)
    wait_until(
        lambda: failed.snapshot().state is CaptureOwnerState.LIVE
        and sibling.snapshot().state is CaptureOwnerState.LIVE
    )

    shared = block(1, 1)
    assert failed.offer_live(shared) is False
    assert failed.snapshot().credits_occupied == 0
    assert failed.snapshot().live_queued == 0
    assert sibling.offer_live(shared) is True
    wait_until(lambda: len(failed_events) == 1)

    snapshot = failed.snapshot()
    assert rejecting_queue.push_calls == 1
    assert snapshot.credits_occupied == 0
    assert snapshot.live_queued == 0
    assert snapshot.in_flight is False
    assert rejecting_queue.observed_credits == [1]
    assert snapshot.peak_credits <= 8
    assert failed_events[0].error_code is CaptureErrorCode.CAPTURE_PERSISTENCE_BACKLOG
    assert sum(
        event.kind is OwnerControlKind.DETACH for event in failed_controls
    ) == 1
    assert failed_store.state(failed_events[0].artifact_id).value in {
        "DELETED",
        "DELETE_PENDING",
    }
    assert sibling.snapshot().state is CaptureOwnerState.LIVE
    assert sibling_events == []

    sibling.cancel("test cleanup")
    wait_until(lambda: len(sibling_events) == 1)
    time.sleep(0.02)
    assert len(failed_events) == 1
    failed.join(1)
    sibling.join(1)


def test_catch_up_ring_reads_use_available_credits_and_never_coexist_with_live(
    tmp_path: Path,
) -> None:
    ring = make_ring(*(block(start, 1) for start in range(9)))
    store = make_store(tmp_path)
    delegate = store.allocate_writer(
        run_id="catch-up-credits",
        target_frames=20,
        channels=2,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
    )
    writer = PausingWriter(delegate, pause_start=0)
    owner, _, events, _ = make_owner(
        tmp_path / "owner",
        ring,
        run_id="catch-up-credits",
        start=0,
        target_frames=20,
        writer=writer,
    )
    activate(owner, 0)
    assert writer.entered.wait(1)

    snapshot = owner.snapshot()
    assert snapshot.state is CaptureOwnerState.CATCHING_UP
    assert snapshot.credits_occupied == 8
    assert snapshot.in_flight is True
    assert snapshot.catch_up_references == 7
    assert snapshot.live_queued == 0
    assert snapshot.peak_credits == 8
    assert ring.block_count == 9
    ring.append(block(9, 1))
    owner.notify_ring_extended()
    assert owner.snapshot().credits_occupied == 8

    owner.cancel("test cleanup")
    writer.release.set()
    wait_until(lambda: len(events) == 1)
    assert events[0].state is CaptureOwnerState.CANCELED
    assert owner.snapshot().credits_occupied == 0
    owner.join(1)


def test_artifact_io_failure_is_owner_local(tmp_path: Path) -> None:
    class FailingWriter:
        artifact_id = "failed-artifact"
        written_frames = 0

        def bind_capture_start(self, **_kwargs):
            return None

        def write(self, _block):
            raise OSError("disk failed")

        def freeze(self):
            raise AssertionError("must not freeze")

        def discard(self):
            return True

    ring = make_ring(block(0))
    failed, _, failed_events, _ = make_owner(
        tmp_path / "failed",
        ring,
        run_id="failed",
        start=0,
        target_frames=4,
        writer=FailingWriter(),
    )
    sibling, _, sibling_events, _ = make_owner(
        tmp_path / "sibling",
        ring,
        run_id="sibling",
        start=0,
        target_frames=4,
    )
    activate(failed, 0)
    activate(sibling, 0)

    wait_until(lambda: len(failed_events) == len(sibling_events) == 1)
    assert (
        failed_events[0].error_code
        is CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
    )
    assert sibling_events[0].state is CaptureOwnerState.COMPLETED
    failed.join(1)
    sibling.join(1)


@pytest.mark.parametrize("loser", ["cancel", "failure", "shutdown"])
def test_freeze_terminal_races_emit_exactly_one_attempt_and_preserve_publication(
    tmp_path: Path,
    loser: str,
) -> None:
    ring = make_ring(block(0))
    store = make_store(tmp_path / loser)
    delegate = store.allocate_writer(
        run_id=loser,
        target_frames=4,
        channels=2,
        sample_rate=10.0,
        pool_epoch="epoch-1",
        stream_generation=3,
        trigger_monotonic=10.0,
    )
    freeze_entered = threading.Event()
    freeze_release = threading.Event()

    class BarrierWriter:
        @property
        def artifact_id(self):
            return delegate.artifact_id

        @property
        def written_frames(self):
            return delegate.written_frames

        def bind_capture_start(self, **kwargs):
            return delegate.bind_capture_start(**kwargs)

        def write(self, audio_block):
            return delegate.write(audio_block)

        def freeze(self):
            freeze_entered.set()
            assert freeze_release.wait(2)
            return delegate.freeze()

        def discard(self):
            return delegate.discard()

    owner, _, events, controls = make_owner(
        tmp_path / f"{loser}-owner",
        ring,
        run_id=loser,
        start=0,
        target_frames=4,
        writer=BarrierWriter(),
    )
    activate(owner, 0)
    assert freeze_entered.wait(1)
    assert owner.snapshot().state is CaptureOwnerState.FREEZING
    assert any(event.kind is OwnerControlKind.DETACH for event in controls)

    if loser == "cancel":
        owner.cancel("race cancel")
    elif loser == "failure":
        owner.fail(CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED, "race failure")
    else:
        owner.shutdown()
    freeze_release.set()
    wait_until(lambda: len(events) == 1)

    assert len({event.terminal_attempt_id for event in events}) == 1
    assert events[0].state is not CaptureOwnerState.COMPLETED
    assert events[0].artifact is not None
    assert events[0].publication_token == events[0].artifact.publication_token
    assert store.publication_pending(delegate.artifact_id) is True
    time.sleep(0.02)
    assert len(events) == 1
    owner.join(1)


def test_stream_fault_after_freezing_detach_cannot_replace_success(tmp_path: Path) -> None:
    ring = make_ring(block(0))
    owner, store, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="freeze-success",
        start=0,
        target_frames=4,
    )
    activate(owner, 0)
    wait_until(
        lambda: owner.snapshot().state
        in {CaptureOwnerState.FREEZING, CaptureOwnerState.COMPLETED}
    )
    owner.stream_failed(CaptureErrorCode.INPUT_DATA_TIMEOUT, "late stream fault")
    wait_until(lambda: len(events) == 1)

    assert events[0].state is CaptureOwnerState.COMPLETED
    assert events[0].artifact is not None
    assert store.publication_pending(events[0].artifact_id) is True
    assert events[0].terminal_attempt_id
    owner.join(1)


def test_success_terminal_attempt_is_stable_against_late_cancel_failure_shutdown(
    tmp_path: Path,
) -> None:
    ring = make_ring(block(0))
    owner, store, events, _ = make_owner(
        tmp_path,
        ring,
        run_id="success-wins",
        start=0,
        target_frames=4,
    )
    activate(owner, 0)
    wait_until(lambda: len(events) == 1)
    attempt = events[0].terminal_attempt_id

    assert owner.cancel("late cancel") is False
    assert owner.fail(
        CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED, "late failure"
    ) is False
    assert owner.shutdown() is False
    assert len(events) == 1
    assert events[0].terminal_attempt_id == attempt
    assert events[0].state is CaptureOwnerState.COMPLETED
    assert store.publication_pending(events[0].artifact_id) is True
    owner.join(1)
