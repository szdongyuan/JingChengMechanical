"""Fixed-capacity ring, generation, callback lane, and seeded seam tests."""

from __future__ import annotations

import linecache
import sys
from dataclasses import FrozenInstanceError, fields
from threading import Barrier, Event, Lock, Thread
from types import new_class

import numpy as np
import pytest

import app.service.analysis.resident_audio._leaf_kernel as leaf
from app.service.analysis.resident_audio._leaf_kernel import (
    generation as generation_module,
)
from app.service.analysis.resident_audio._leaf_kernel import (
    ledger as ledger_module,
)
from app.service.analysis.resident_audio._leaf_kernel.generation import (
    _GenerationIssuerSeal,
    _ResidentGeneration,
)
from app.service.analysis.resident_audio._leaf_kernel.ledger import (
    _SeededGenerationConstruction,
    _SeededGenerationConstructionStage,
    _issue_ledger,
)
from app.service.analysis.resident_audio._leaf_kernel.ring import (
    RingAppendRejected,
    RingOperationRejectionCode,
    RingReadAvailable,
    RingReadBusy,
    RingReadEvicted,
    RingReadFaulted,
    RingReadUnavailable,
    RingStateFaulted,
    TimestampedBlockRing,
)
from app.service.analysis.resident_audio._leaf_kernel.rows import (
    CallbackLaneRejected,
    CallbackLaneSnapshot,
    CallbackLaneState,
    CallbackQuiescenceOutcome,
    CallbackRejectionCode,
    GenerationRegistrationRowSnapshot,
    GenerationState,
    LedgerSnapshot,
    SeededSlotState,
)
from app.service.analysis.resident_audio._leaf_kernel.seeded import (
    _SeededSlotOutcome,
    _issue_seeded_slots,
)
from app.service.analysis.resident_audio._leaf_kernel.values import (
    CallbackGeneration,
    CallbackSequence,
    CallbackTicket,
    GenerationId,
    LedgerRevision,
    MAX_SIGNED_63,
    RedactedIdentifier,
    RedactedIdentifierKind,
    SampleCursor,
    SeededSlotId,
)

from .fakes import (
    AmbiguousAdvanceRing,
    MaliciousLookalike,
    FailingPublicationRing,
    DoubleFailurePublicationRing,
    FaultQuarantineReadRing,
    InterruptingPublicationRing,
    PreclaimPausingPublicationRing,
    PausingCommitRing,
    PausingReadValidationRing,
    PausingWriteRing,
    float32_frames,
)


EXACT_ENUM_TYPES = (
    SeededSlotState,
    GenerationState,
    CallbackLaneState,
    CallbackRejectionCode,
    RingOperationRejectionCode,
    _SeededGenerationConstructionStage,
)

EXACT_TYPES = (
    SeededSlotId,
    GenerationId,
    SampleCursor,
    CallbackGeneration,
    CallbackSequence,
    CallbackTicket,
    CallbackLaneRejected,
    _SeededSlotOutcome,
    CallbackLaneSnapshot,
    CallbackQuiescenceOutcome,
    GenerationRegistrationRowSnapshot,
    RingReadAvailable,
    RingReadBusy,
    RingReadEvicted,
    RingReadFaulted,
    RingReadUnavailable,
    RingAppendRejected,
    RingStateFaulted,
)


def _identities(slot_number: int = 1):
    slot = SeededSlotId(slot_number)
    generation = GenerationId(slot, 1)
    callback_generation = CallbackGeneration(generation, 1)
    sequence = CallbackSequence(1)
    ticket = CallbackTicket(callback_generation, sequence)
    return slot, generation, callback_generation, sequence, ticket


def _valid_exact(exact_type):
    slot, generation, callback_generation, sequence, ticket = _identities()
    if exact_type is SeededSlotId:
        return slot
    if exact_type is GenerationId:
        return generation
    if exact_type is SampleCursor:
        return SampleCursor(1)
    if exact_type is CallbackGeneration:
        return callback_generation
    if exact_type is CallbackSequence:
        return sequence
    if exact_type is CallbackTicket:
        return ticket
    if exact_type is CallbackLaneRejected:
        return CallbackLaneRejected(
            callback_generation, CallbackRejectionCode.BUSY
        )
    if exact_type is _SeededSlotOutcome:
        return _SeededSlotOutcome(slot, SeededSlotState.ISSUED)
    if exact_type is CallbackLaneSnapshot:
        return CallbackLaneSnapshot(
            callback_generation,
            CallbackLaneState.ADMITTING,
            1,
            1,
            0,
            sequence,
            CallbackSequence(0),
            CallbackSequence(0),
        )
    if exact_type is CallbackQuiescenceOutcome:
        return CallbackQuiescenceOutcome(
            generation,
            callback_generation,
            callback_generation,
            0,
            sequence,
            sequence,
            False,
        )
    if exact_type is GenerationRegistrationRowSnapshot:
        return GenerationRegistrationRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.SEEDED_SLOT, 1),
            RedactedIdentifier(RedactedIdentifierKind.GENERATION, 1),
            GenerationState.ACTIVE,
            LedgerRevision(1),
        )
    if exact_type is RingReadAvailable:
        return RingReadAvailable(
            SampleCursor(0),
            SampleCursor(1),
            np.zeros((1, 1), dtype=np.float32),
            np.zeros((1,), dtype=np.int64),
        )
    if exact_type is RingReadBusy:
        return RingReadBusy(SampleCursor(0), SampleCursor(1))
    if exact_type is RingReadFaulted:
        return RingReadFaulted(SampleCursor(0), SampleCursor(1))
    if exact_type is RingReadEvicted:
        return RingReadEvicted(
            SampleCursor(0), SampleCursor(1), SampleCursor(1)
        )
    if exact_type is RingReadUnavailable:
        return RingReadUnavailable(
            SampleCursor(1), SampleCursor(2), SampleCursor(1)
        )
    if exact_type is RingAppendRejected:
        return RingAppendRejected(
            RingOperationRejectionCode.BUSY, SampleCursor(0)
        )
    if exact_type is RingStateFaulted:
        return RingStateFaulted(SampleCursor(0))
    raise AssertionError(f"missing exact fixture for {exact_type.__name__}")


def _new_generation(capacity_frames: int = 16, channels: int = 1):
    ledger, authority = _issue_ledger()
    seeded = _issue_seeded_slots()[0]
    generation = ledger.reserve_seeded_generation(
        authority, seeded, capacity_frames, channels
    )
    return ledger, authority, generation


def _ring_publication(ring):
    return ring._publication


def _raise_ring_process_control_once(ring, placement, interruption):
    target_code = TimestampedBlockRing.append.__code__
    fired = False

    def trace(frame, event, arg):
        nonlocal fired
        if fired or frame.f_code is not target_code:
            return trace
        version, state = _ring_publication(ring)
        source = linecache.getline(
            frame.f_code.co_filename, frame.f_lineno
        ).strip()
        if (
            placement == "odd_publication"
            and event == "line"
            and version & 1
        ):
            fired = True
            raise interruption
        if (
            placement == "metadata"
            and event == "line"
            and version & 1
            and source
            == "self._block_lengths[block_tail] = frame_count"
        ):
            fired = True
            raise interruption
        next_state = frame.f_locals.get("next_committed_state")
        if (
            placement == "after_commit"
            and event == "line"
            and next_state is not None
            and state == next_state
        ):
            fired = True
            raise interruption
        if (
            placement == "before_return"
            and event == "return"
            and type(arg) is SampleCursor
        ):
            fired = True
            raise interruption
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(type(interruption)) as raised:
            ring.append(float32_frames(4, 4), 200)
    finally:
        sys.settrace(None)
    assert fired
    assert raised.value is interruption


@pytest.mark.parametrize(
    "interruption_type", (KeyboardInterrupt, SystemExit)
)
@pytest.mark.parametrize(
    "placement",
    (
        "odd_publication",
        "metadata",
        "after_commit",
        "before_return",
    ),
)
def test_ring_trace_interruption_has_one_atomic_publication_record(
    interruption_type,
    placement,
):
    ring = TimestampedBlockRing(16, 1)
    assert ring.append(float32_frames(0, 4), 100) == SampleCursor(4)

    _raise_ring_process_control_once(
        ring, placement, interruption_type()
    )

    version, committed_state = _ring_publication(ring)
    committed = placement in ("after_commit", "before_return")
    expected_cursor = 8 if committed else 4
    assert version % 2 == 0
    assert committed_state[3] == expected_cursor
    assert ring.next_cursor == SampleCursor(expected_cursor)
    assert type(
        ring.read(
            SampleCursor(expected_cursor - 4),
            SampleCursor(expected_cursor),
        )
    ) is RingReadAvailable
    assert ring.append(
        float32_frames(expected_cursor, 1), 300
    ) == SampleCursor(expected_cursor + 1)
    assert type(
        ring.read(
            SampleCursor(expected_cursor),
            SampleCursor(expected_cursor + 1),
        )
    ) is RingReadAvailable


@pytest.mark.parametrize(
    "interruption_type", (KeyboardInterrupt, SystemExit)
)
def test_generation_reconciles_trace_interruption_after_ring_commit(
    interruption_type,
):
    _, _, generation = _new_generation()
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    target_code = _ResidentGeneration.append_callback.__code__
    interruption = interruption_type()
    fired = False

    def trace(frame, event, arg):
        nonlocal fired
        if (
            not fired
            and frame.f_code is target_code
            and event == "line"
            and generation.ring_next_cursor == SampleCursor(4)
            and generation._last_ingress_sequence == 0
        ):
            fired = True
            raise interruption
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(interruption_type) as raised:
            generation.append_callback(
                ticket, float32_frames(0, 4), 100
            )
    finally:
        sys.settrace(None)

    assert fired
    assert raised.value is interruption
    assert generation.ring_next_cursor == SampleCursor(4)
    with pytest.raises(
        ValueError, match="stale callback ingress sequence"
    ):
        generation.append_callback(
            ticket, float32_frames(4, 1), 200
        )
    generation.close_callback_admission(
        generation.callback_generation
    )
    assert generation.return_callback(ticket) is None
    outcome = generation.quiescence_outcome()
    assert type(outcome) is CallbackQuiescenceOutcome
    assert outcome.last_admitted_sequence == CallbackSequence(1)
    assert outcome.last_drained_ingress_sequence == CallbackSequence(1)


@pytest.mark.parametrize(
    "interruption_type", (KeyboardInterrupt, SystemExit)
)
def test_generation_leaves_ingress_uncommitted_when_interrupted_ring_is_unchanged(
    interruption_type,
):
    _, _, generation = _new_generation()
    ring = InterruptingPublicationRing(16, 1)
    generation._ring = ring
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    ring.failure_stage = "before"
    ring.interruption = interruption_type()

    with pytest.raises(interruption_type):
        generation.append_callback(
            ticket, float32_frames(0, 1), 100
        )

    snapshot = generation.callback_snapshot()
    assert snapshot.last_ingress_sequence == CallbackSequence(0)
    assert generation.ring_next_cursor == SampleCursor(0)
    ring.failure_stage = ""
    assert generation.append_callback(
        ticket, float32_frames(0, 1), 200
    ) == SampleCursor(1)


def test_generation_faults_closed_on_ambiguous_interrupted_ring_advance():
    _, _, generation = _new_generation()
    generation._ring = AmbiguousAdvanceRing(16, 1)
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )

    with pytest.raises(KeyboardInterrupt, match="ambiguous"):
        generation.append_callback(
            ticket, float32_frames(0, 1), 100
        )

    assert generation.ring_next_cursor == SampleCursor(2)
    assert generation.append_callback(
        ticket, float32_frames(2, 1), 200
    ) == RingAppendRejected(
        RingOperationRejectionCode.FAULTED, SampleCursor(2)
    )
    generation.close_callback_admission(
        generation.callback_generation
    )
    assert generation.return_callback(ticket) == CallbackLaneRejected(
        generation.callback_generation,
        CallbackRejectionCode.BUSY,
    )
    assert generation.quiescence_outcome() is None


def test_fixed_capacity_never_reallocates_or_grows_across_10000_appends():
    ring = TimestampedBlockRing(32, 1)
    allocation = ring._allocation_fingerprint()

    for index in range(10_000):
        ring.append(float32_frames(index * 4, 4), index)

    assert ring._allocation_fingerprint() == allocation
    assert ring.capacity_frames == 32
    assert ring.retained_frames == 32
    assert ring.oldest_cursor == SampleCursor(39_968)
    assert ring.next_cursor == SampleCursor(40_000)


def test_half_open_reads_preserve_exact_frames_and_block_timestamps():
    ring = TimestampedBlockRing(10, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.append(float32_frames(4, 4), 200)
    ring.append(float32_frames(8, 3), 300)

    result = ring.read(SampleCursor(4), SampleCursor(11))

    assert type(result) is RingReadAvailable
    assert result.start == SampleCursor(4)
    assert result.end == SampleCursor(11)
    np.testing.assert_array_equal(
        result.frames[:, 0], np.arange(4, 11, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        result.timestamps_ns,
        np.array([200] * 4 + [300] * 3, dtype=np.int64),
    )
    assert result.frames.flags.writeable is False
    assert result.timestamps_ns.flags.writeable is False


def test_whole_oldest_block_eviction_has_explicit_evicted_and_unavailable_results():
    ring = TimestampedBlockRing(10, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.append(float32_frames(4, 4), 200)
    ring.append(float32_frames(8, 3), 300)

    assert ring.oldest_cursor == SampleCursor(4)
    evicted = ring.read(SampleCursor(0), SampleCursor(1))
    unavailable = ring.read(SampleCursor(10), SampleCursor(12))

    assert evicted == RingReadEvicted(
        SampleCursor(0), SampleCursor(1), SampleCursor(4)
    )
    assert unavailable == RingReadUnavailable(
        SampleCursor(10), SampleCursor(12), SampleCursor(11)
    )


def test_invalid_or_oversized_append_and_read_are_zero_mutation():
    ring = TimestampedBlockRing(4, 1)
    ring.append(float32_frames(0, 2), 1)
    before = ring.snapshot()

    for frames_value, timestamp in (
        (float32_frames(0, 5), 2),
        (np.zeros((1, 1), dtype=np.float64), 2),
        (np.zeros((1,), dtype=np.float32), 2),
        (float32_frames(0, 1), True),
    ):
        with pytest.raises((TypeError, ValueError)):
            ring.append(frames_value, timestamp)
        assert ring.snapshot() == before

    with pytest.raises(ValueError):
        ring.read(SampleCursor(2), SampleCursor(2))
    assert ring.snapshot() == before


def test_reader_returns_bounded_busy_during_wraparound_publication_and_never_returns_torn_available():
    ring = PausingWriteRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.pause_writes = True
    writer = Thread(
        target=lambda: ring.append(float32_frames(4, 4), 200)
    )
    writer.start()
    paused = ring.writer_paused.wait(5)
    claim_guard_was_free = ring._writer_claim_lock.acquire(
        blocking=False
    )
    if claim_guard_was_free:
        ring._writer_claim_lock.release()
    busy = ring.read(SampleCursor(4), SampleCursor(8))
    snapshot_during = ring.snapshot()
    competing_append = ring.append(
        float32_frames(8, 1), 300
    )
    observed = ring.reader_saw_in_progress.wait(5)
    ring.allow_writer.set()
    writer.join(5)

    assert paused
    assert claim_guard_was_free
    assert observed
    assert not writer.is_alive()
    assert busy == RingReadBusy(SampleCursor(4), SampleCursor(8))
    assert snapshot_during == (0, 4, 4, 1)
    assert competing_append == RingAppendRejected(
        RingOperationRejectionCode.BUSY, SampleCursor(4)
    )
    result = ring.read(SampleCursor(4), SampleCursor(8))
    assert type(result) is RingReadAvailable
    assert result.start == SampleCursor(4)
    assert result.end == SampleCursor(8)
    np.testing.assert_array_equal(
        result.frames[:, 0], np.arange(4, 8, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        result.timestamps_ns, np.array([200] * 4, dtype=np.int64)
    )
    assert ring.oldest_cursor == SampleCursor(4)
    assert ring.next_cursor == SampleCursor(8)


@pytest.mark.parametrize("fail_before_commit", (True, False))
def test_paused_commit_exposes_only_the_last_irrevocable_cursor(
    fail_before_commit,
):
    ring = PausingCommitRing(6, 1)
    assert ring.append(float32_frames(0, 4), 100) == SampleCursor(4)
    ring.pause_next_commit = True
    ring.fail_next_commit = fail_before_commit
    writer_result = []
    writer_error = []

    def append_second_block():
        try:
            writer_result.append(
                ring.append(float32_frames(4, 4), 200)
            )
        except BaseException as error:
            writer_error.append(error)

    writer = Thread(target=append_second_block)
    writer.start()
    paused = ring.commit_paused.wait(5)

    snapshot_during = ring.snapshot()
    retained_during = ring.retained_frames
    oldest_during = ring.oldest_cursor
    next_during = ring.next_cursor
    read_during = ring.read(SampleCursor(4), SampleCursor(8))
    competing_append = ring.append(
        float32_frames(8, 1), 300
    )

    ring.allow_commit.set()
    writer.join(5)

    assert paused
    assert not writer.is_alive()
    assert writer_error == []
    assert snapshot_during == (0, 4, 4, 1)
    assert retained_during == 4
    assert oldest_during == SampleCursor(0)
    assert next_during == SampleCursor(4)
    assert read_during == RingReadBusy(
        SampleCursor(4), SampleCursor(8)
    )
    assert competing_append == RingAppendRejected(
        RingOperationRejectionCode.BUSY, SampleCursor(4)
    )

    if fail_before_commit:
        assert writer_result == [
            RingAppendRejected(
                RingOperationRejectionCode.FAILED, SampleCursor(4)
            )
        ]
        assert ring.snapshot() == (0, 4, 4, 1)
        assert ring.next_cursor == SampleCursor(4)
    else:
        assert writer_result == [SampleCursor(8)]
        assert ring.snapshot() == (4, 8, 4, 1)
        assert ring.oldest_cursor == SampleCursor(4)
        assert ring.next_cursor == SampleCursor(8)


@pytest.mark.parametrize(
    "failure_stage", ("before", "during", "after", "publish", "state")
)
def test_ring_write_failure_rolls_back_and_never_leaves_odd_publication(
    failure_stage,
):
    ring = FailingPublicationRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    before = ring.snapshot()
    before_read = ring.read(SampleCursor(0), SampleCursor(4))
    ring.failure_stage = failure_stage

    rejected = ring.append(float32_frames(4, 4), 200)

    assert rejected == RingAppendRejected(
        RingOperationRejectionCode.FAILED, SampleCursor(4)
    )
    assert ring._publication[0] % 2 == 0
    assert ring.snapshot() == before
    after_read = ring.read(SampleCursor(0), SampleCursor(4))
    assert after_read == before_read
    np.testing.assert_array_equal(
        after_read.frames[:, 0], np.arange(4, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        after_read.timestamps_ns,
        np.array([100] * 4, dtype=np.int64),
    )
    ring.failure_stage = ""
    committed = ring.append(float32_frames(4, 2), 200)
    assert committed == SampleCursor(6)
    assert ring.next_cursor == SampleCursor(6)


class _CustomProcessControl(BaseException):
    pass


@pytest.mark.parametrize(
    "interruption_type",
    (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
        _CustomProcessControl,
    ),
)
@pytest.mark.parametrize(
    "failure_stage", ("before", "during", "after", "publish", "state")
)
def test_process_control_interruption_restores_commit_settles_parity_and_reraises(
    interruption_type,
    failure_stage,
):
    ring = InterruptingPublicationRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    before = ring.snapshot()
    before_read = ring.read(SampleCursor(0), SampleCursor(4))
    interruption = interruption_type()
    ring.failure_stage = failure_stage
    ring.interruption = interruption

    with pytest.raises(interruption_type) as raised:
        ring.append(float32_frames(4, 4), 200)

    assert raised.value is interruption
    assert ring._publication[0] % 2 == 0
    assert ring.snapshot() == before
    restored = ring.read(SampleCursor(0), SampleCursor(4))
    assert restored == before_read
    np.testing.assert_array_equal(
        restored.frames[:, 0], np.arange(4, dtype=np.float32)
    )
    np.testing.assert_array_equal(
        restored.timestamps_ns,
        np.array([100] * 4, dtype=np.int64),
    )
    ring.failure_stage = ""
    assert ring.append(float32_frames(4, 2), 200) == SampleCursor(6)


@pytest.mark.parametrize("rollback_stage", ("before", "during"))
@pytest.mark.parametrize(
    "failure_stage", ("before", "during", "after", "publish", "state")
)
def test_interruption_with_failed_restoration_permanently_faults_bounded_ring(
    rollback_stage,
    failure_stage,
):
    ring = DoubleFailurePublicationRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    interruption = KeyboardInterrupt()
    ring.failure_stage = failure_stage
    ring.interruption = interruption
    ring.rollback_failure_stage = rollback_stage
    ring.rollback_failure = RuntimeError("rollback failed")

    with pytest.raises(KeyboardInterrupt) as raised:
        ring.append(float32_frames(4, 4), 200)

    assert raised.value is interruption
    assert ring._publication[0] % 2 == 0
    assert ring.snapshot() == RingStateFaulted(SampleCursor(4))
    assert ring.oldest_cursor == SampleCursor(0)
    assert ring.next_cursor == SampleCursor(4)
    assert ring.retained_frames == 4
    assert ring._publication[1] == (0, 1, 4, 4)
    assert ring.read(
        SampleCursor(0), SampleCursor(4)
    ) == RingReadFaulted(SampleCursor(0), SampleCursor(4))
    assert ring.append(
        float32_frames(8, 1), 300
    ) == RingAppendRejected(
        RingOperationRejectionCode.FAULTED, SampleCursor(4)
    )


def test_contender_cannot_append_after_fault_published_between_precheck_and_claim():
    ring = PreclaimPausingPublicationRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.pause_next_preclaim = True
    contender_result = []
    contender = Thread(
        target=lambda: contender_result.append(
            ring.append(float32_frames(8, 1), 300)
        )
    )
    contender.start()
    paused = ring.preclaim_paused.wait(5)

    ring.failure_stage = "state"
    ring.interruption = KeyboardInterrupt()
    ring.rollback_failure_stage = "during"
    ring.rollback_failure = RuntimeError("rollback failed")
    with pytest.raises(KeyboardInterrupt):
        ring.append(float32_frames(4, 4), 200)

    publication_after_fault = ring._publication
    frames_after_fault = ring._frames.copy()
    timestamps_after_fault = ring._timestamps_ns.copy()
    ring.allow_preclaim.set()
    contender.join(5)

    assert paused
    assert not contender.is_alive()
    assert contender_result == [
        RingAppendRejected(
            RingOperationRejectionCode.FAULTED, SampleCursor(4)
        )
    ]
    assert ring._publication is publication_after_fault
    np.testing.assert_array_equal(ring._frames, frames_after_fault)
    np.testing.assert_array_equal(
        ring._timestamps_ns, timestamps_after_fault
    )


def test_read_checks_fault_before_retry_state_or_storage_after_wraparound_failure():
    ring = FaultQuarantineReadRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.pause_copy_once = True
    ring.pause_attempt = 1
    result_box = []
    reader = Thread(
        target=lambda: result_box.append(
            ring.read(SampleCursor(0), SampleCursor(4))
        )
    )
    reader.start()
    copied = ring.copy_paused.wait(5)
    assert ring.append(float32_frames(4, 1), 200) == SampleCursor(5)
    ring.allow_copy.set()
    retry_paused = ring.attempt_paused.wait(5)

    ring.failure_stage = "state"
    ring.interruption = KeyboardInterrupt()
    ring.rollback_failure_stage = "during"
    ring.rollback_failure = RuntimeError("rollback failed")
    with pytest.raises(KeyboardInterrupt):
        ring.append(float32_frames(5, 4), 300)
    ring.allow_attempt.set()
    reader.join(5)

    assert copied
    assert retry_paused
    assert not reader.is_alive()
    assert result_box == [
        RingReadFaulted(SampleCursor(0), SampleCursor(4))
    ]


def test_read_rechecks_fault_after_validated_copy_before_available_return():
    ring = FaultQuarantineReadRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.pause_validated_once = True
    result_box = []
    reader = Thread(
        target=lambda: result_box.append(
            ring.read(SampleCursor(0), SampleCursor(4))
        )
    )
    reader.start()
    validated = ring.validated_paused.wait(5)

    ring.failure_stage = "state"
    ring.interruption = KeyboardInterrupt()
    ring.rollback_failure_stage = "during"
    ring.rollback_failure = RuntimeError("rollback failed")
    with pytest.raises(KeyboardInterrupt):
        ring.append(float32_frames(4, 4), 200)
    ring.allow_validated.set()
    reader.join(5)

    assert validated
    assert not reader.is_alive()
    assert result_box == [
        RingReadFaulted(SampleCursor(0), SampleCursor(4))
    ]


def test_ordinary_failure_with_failed_restoration_returns_faulted_rejection():
    ring = DoubleFailurePublicationRing(6, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.failure_stage = "after"
    ring.interruption = RuntimeError("write failed")
    ring.rollback_failure_stage = "during"
    ring.rollback_failure = RuntimeError("rollback failed")

    result = ring.append(float32_frames(4, 4), 200)

    assert result == RingAppendRejected(
        RingOperationRejectionCode.FAULTED, SampleCursor(4)
    )
    assert ring._publication[0] % 2 == 0
    assert type(ring.snapshot()) is RingStateFaulted
    assert type(
        ring.read(SampleCursor(0), SampleCursor(4))
    ) is RingReadFaulted


def test_reader_retries_when_writer_commits_after_buffer_copy_before_validation():
    ring = PausingReadValidationRing(8, 1)
    ring.append(float32_frames(0, 4), 100)
    ring.pause_next_read = True
    result_box = []
    reader = Thread(
        target=lambda: result_box.append(
            ring.read(SampleCursor(0), SampleCursor(4))
        )
    )
    reader.start()
    copied = ring.reader_copied.wait(5)

    ring.append(float32_frames(4, 6), 200)
    ring.allow_reader_validation.set()
    reader.join(5)

    assert copied
    assert not reader.is_alive()
    assert len(result_box) == 1
    result = result_box[0]
    assert type(result) is RingReadEvicted
    assert result.start == SampleCursor(0)
    assert result.end == SampleCursor(4)
    assert result.oldest_available == SampleCursor(4)
    assert ring.oldest_cursor == SampleCursor(4)
    assert ring.next_cursor == SampleCursor(10)


def test_callback_lane_accounts_admitted_in_flight_returned_and_ingress():
    _, _, generation = _new_generation()
    callback_generation = generation.callback_generation
    ticket = generation.admit_callback(
        callback_generation, CallbackSequence(1)
    )

    admitted = generation.callback_snapshot()
    assert admitted.admitted_count == 1
    assert admitted.in_flight_count == 1
    assert admitted.returned_count == 0

    generation.append_callback(ticket, float32_frames(0, 4), 100)
    generation.return_callback(ticket)

    returned = generation.callback_snapshot()
    assert returned.admitted_count == 1
    assert returned.in_flight_count == 0
    assert returned.returned_count == 1
    assert returned.last_ingress_sequence == CallbackSequence(1)
    assert returned.last_returned_sequence == CallbackSequence(1)
    assert generation.ring_next_cursor == SampleCursor(4)
    result = generation.read_ring(SampleCursor(0), SampleCursor(4))
    assert type(result) is RingReadAvailable
    assert result.frames.flags.writeable is False
    assert result.timestamps_ns.flags.writeable is False


class _IntSubclass(int):
    pass


@pytest.mark.parametrize(
    ("bad_in_flight", "admitted", "returned"),
    (
        (True, 1, 0),
        (_IntSubclass(1), 1, 0),
        (MaliciousLookalike(), 1, 0),
        (2, 2, 0),
        (-1, 0, 1),
    ),
)
def test_callback_lane_snapshot_rejects_non_exact_single_in_flight_count(
    bad_in_flight,
    admitted,
    returned,
):
    _, _, callback_generation, _, _ = _identities()

    with pytest.raises((TypeError, ValueError)):
        CallbackLaneSnapshot(
            callback_generation,
            CallbackLaneState.ADMITTING,
            admitted,
            bad_in_flight,
            returned,
            CallbackSequence(admitted),
            CallbackSequence(0),
            CallbackSequence(returned),
        )


def test_single_in_flight_overlap_rejects_before_any_counter_or_sequence_mutation():
    _, _, generation = _new_generation()
    first = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    before = generation.callback_snapshot()

    rejected = generation.admit_callback(
        generation.callback_generation, CallbackSequence(2)
    )

    assert rejected == CallbackLaneRejected(
        generation.callback_generation,
        CallbackRejectionCode.OVERLAP,
    )
    assert generation.callback_snapshot() == before
    generation.return_callback(first)
    second = generation.admit_callback(
        generation.callback_generation, CallbackSequence(2)
    )
    assert type(second) is CallbackTicket


def test_stale_generation_and_callback_sequence_reject_with_zero_mutation():
    _, _, generation = _new_generation()
    stale_generation = CallbackGeneration(
        generation.generation_id, 2
    )
    before_lane = generation.callback_snapshot()
    before_ring = generation.ring_snapshot()

    with pytest.raises(ValueError, match="stale callback generation"):
        generation.admit_callback(
            stale_generation, CallbackSequence(1)
        )
    with pytest.raises(ValueError, match="stale callback sequence"):
        generation.admit_callback(
            generation.callback_generation, CallbackSequence(2)
        )

    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    admitted = generation.callback_snapshot()
    with pytest.raises(ValueError, match="stale callback"):
        generation.append_callback(
            CallbackTicket(
                generation.callback_generation, CallbackSequence(2)
            ),
            float32_frames(0, 1),
            1,
        )
    assert generation.callback_snapshot() == admitted
    assert generation.ring_snapshot() == before_ring
    generation.return_callback(ticket)
    with pytest.raises(ValueError, match="stale callback"):
        generation.return_callback(ticket)
    assert generation.callback_snapshot().returned_count == 1
    assert before_lane.admitted_count == 0


def test_close_admission_waits_for_exact_return_before_quiescence():
    _, _, generation = _new_generation()
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    generation.close_callback_admission(generation.callback_generation)

    assert generation.callback_snapshot().state is CallbackLaneState.ADMISSION_CLOSED
    assert generation.quiescence_outcome() is None
    with pytest.raises(PermissionError):
        generation.admit_callback(
            generation.callback_generation, CallbackSequence(2)
        )

    generation.return_callback(ticket)
    outcome = generation.quiescence_outcome()
    assert type(outcome) is CallbackQuiescenceOutcome
    assert outcome.in_flight_count == 0
    assert outcome.last_admitted_sequence == CallbackSequence(1)
    assert outcome.last_drained_ingress_sequence == CallbackSequence(0)
    assert outcome.can_admit_again is False
    assert generation.callback_snapshot().state is CallbackLaneState.QUIESCENT


def test_close_racing_admission_is_linearizable_and_never_quiescent_in_flight(
    monkeypatch,
):
    _, _, generation = _new_generation()
    hook_entered = Event()
    allow_admission = Event()

    def pause_admission(self):
        hook_entered.set()
        assert allow_admission.wait(5)

    monkeypatch.setattr(
        generation_module._ResidentGeneration,
        "_after_admission_check_before_publish",
        pause_admission,
    )
    admitted = []
    admission_thread = Thread(
        target=lambda: admitted.append(
            generation.admit_callback(
                generation.callback_generation, CallbackSequence(1)
            )
        )
    )
    admission_thread.start()
    entered = hook_entered.wait(5)
    close_results = []
    close_thread = Thread(
        target=lambda: close_results.append(
            generation.close_callback_admission(
                generation.callback_generation
            )
        )
    )
    close_thread.start()
    allow_admission.set()
    admission_thread.join(5)
    close_thread.join(5)

    assert entered
    assert not admission_thread.is_alive()
    assert not close_thread.is_alive()
    assert len(admitted) == 1
    assert close_results == [
        CallbackLaneRejected(
            generation.callback_generation,
            CallbackRejectionCode.BUSY,
        )
    ]
    closed = generation.close_callback_admission(
        generation.callback_generation
    )
    assert type(closed) is CallbackLaneSnapshot
    snapshot = generation.callback_snapshot()
    assert snapshot.state is CallbackLaneState.ADMISSION_CLOSED
    assert snapshot.admitted_count == 1
    assert snapshot.in_flight_count == 1
    assert snapshot.returned_count == 0
    assert generation.quiescence_outcome() is None


def test_close_racing_return_publishes_quiescence_only_after_return(
    monkeypatch,
):
    _, _, generation = _new_generation()
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    hook_entered = Event()
    allow_close = Event()

    def pause_close(self):
        hook_entered.set()
        assert allow_close.wait(5)

    monkeypatch.setattr(
        generation_module._ResidentGeneration,
        "_after_close_check_before_publish",
        pause_close,
    )
    close_thread = Thread(
        target=lambda: generation.close_callback_admission(
            generation.callback_generation
        )
    )
    close_thread.start()
    entered = hook_entered.wait(5)
    return_results = []
    return_thread = Thread(
        target=lambda: return_results.append(
            generation.return_callback(ticket)
        )
    )
    return_thread.start()
    allow_close.set()
    close_thread.join(5)
    return_thread.join(5)

    assert entered
    assert not close_thread.is_alive()
    assert not return_thread.is_alive()
    assert return_results == [
        CallbackLaneRejected(
            generation.callback_generation,
            CallbackRejectionCode.BUSY,
        )
    ]
    generation.return_callback(ticket)
    snapshot = generation.callback_snapshot()
    assert snapshot.state is CallbackLaneState.QUIESCENT
    assert snapshot.in_flight_count == 0
    assert snapshot.returned_count == 1
    assert generation.quiescence_outcome() is not None


def test_concurrent_admission_and_return_preserve_single_in_flight_equation():
    _, _, generation = _new_generation()
    first = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    barrier = Barrier(3)
    results = []
    errors = []

    def admit_second():
        barrier.wait()
        try:
            results.append(
                generation.admit_callback(
                    generation.callback_generation,
                    CallbackSequence(2),
                )
            )
        except Exception as exc:
            errors.append(exc)

    def return_first():
        barrier.wait()
        try:
            generation.return_callback(first)
        except Exception as exc:
            errors.append(exc)

    threads = (Thread(target=admit_second), Thread(target=return_first))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    snapshot = generation.callback_snapshot()
    assert snapshot.admitted_count in (1, 2)
    assert snapshot.in_flight_count in (0, 1)
    assert snapshot.returned_count == 1
    assert (
        snapshot.admitted_count
        == snapshot.in_flight_count + snapshot.returned_count
    )
    if results and type(results[0]) is CallbackLaneRejected:
        assert results[0].code is CallbackRejectionCode.OVERLAP
        assert snapshot.admitted_count == 1
        assert snapshot.in_flight_count == 0
    else:
        assert type(results[0]) is CallbackTicket
        assert snapshot.admitted_count == 2
        assert snapshot.in_flight_count == 1


def test_reverse_order_return_and_ingress_cannot_strand_single_in_flight_lane():
    _, _, generation = _new_generation()
    first = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    overlap = generation.admit_callback(
        generation.callback_generation, CallbackSequence(2)
    )
    forged_second = CallbackTicket(
        generation.callback_generation, CallbackSequence(2)
    )
    before = generation.callback_snapshot()

    with pytest.raises(ValueError, match="stale callback"):
        generation.return_callback(forged_second)
    with pytest.raises(ValueError, match="stale callback"):
        generation.append_callback(
            forged_second, float32_frames(10, 1), 10
        )
    assert overlap.code is CallbackRejectionCode.OVERLAP
    assert generation.callback_snapshot() == before

    generation.return_callback(first)
    second = generation.admit_callback(
        generation.callback_generation, CallbackSequence(2)
    )
    generation.append_callback(
        second, float32_frames(20, 1), 20
    )
    generation.close_callback_admission(
        generation.callback_generation
    )
    generation.return_callback(second)
    assert generation.callback_snapshot().state is CallbackLaneState.QUIESCENT
    assert generation.quiescence_outcome() is not None


def test_no_ingress_callback_settles_and_next_callback_can_ingress_and_quiesce():
    _, _, generation = _new_generation()
    first = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    generation.return_callback(first)
    second = generation.admit_callback(
        generation.callback_generation, CallbackSequence(2)
    )

    generation.append_callback(
        second, float32_frames(30, 2), 30
    )
    generation.close_callback_admission(
        generation.callback_generation
    )
    generation.return_callback(second)

    snapshot = generation.callback_snapshot()
    assert snapshot.last_ingress_sequence == CallbackSequence(2)
    assert snapshot.returned_count == 2
    assert snapshot.in_flight_count == 0
    assert snapshot.state is CallbackLaneState.QUIESCENT


def test_paused_control_path_never_makes_callback_operations_wait(
    monkeypatch,
):
    _, _, generation = _new_generation()
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    control_entered = Event()
    allow_control = Event()

    def pause_close(self):
        control_entered.set()
        assert allow_control.wait(5)

    monkeypatch.setattr(
        generation_module._ResidentGeneration,
        "_after_close_check_before_publish",
        pause_close,
    )
    close_thread = Thread(
        target=lambda: generation.close_callback_admission(
            generation.callback_generation
        )
    )
    close_thread.start()
    entered = control_entered.wait(5)

    callback_results = []
    callbacks_finished = Event()

    def run_callback_operations():
        callback_results.extend(
            (
                generation.admit_callback(
                    generation.callback_generation,
                    CallbackSequence(2),
                ),
                generation.append_callback(
                    ticket, float32_frames(0, 1), 1
                ),
                generation.return_callback(ticket),
            )
        )
        callbacks_finished.set()

    callback_thread = Thread(target=run_callback_operations)
    callback_thread.start()
    finished_before_control_release = callbacks_finished.wait(2)

    allow_control.set()
    callback_thread.join(5)
    close_thread.join(5)
    assert entered
    assert finished_before_control_release
    admission, ingress, returned = callback_results
    assert admission.code is CallbackRejectionCode.BUSY
    assert ingress.code is CallbackRejectionCode.BUSY
    assert returned.code is CallbackRejectionCode.BUSY
    assert not callback_thread.is_alive()
    assert not close_thread.is_alive()
    generation.return_callback(ticket)
    assert generation.callback_snapshot().state is CallbackLaneState.QUIESCENT


def test_outcome_racing_last_return_is_none_or_exact_quiescence():
    _, _, generation = _new_generation()
    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    generation.close_callback_admission(
        generation.callback_generation
    )
    barrier = Barrier(3)
    outcome_box = []

    def read_outcome():
        barrier.wait()
        outcome_box.append(generation.quiescence_outcome())

    def return_last():
        barrier.wait()
        generation.return_callback(ticket)

    threads = (Thread(target=read_outcome), Thread(target=return_last))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(outcome_box) == 1
    observed = outcome_box[0]
    assert (
        observed is None
        or (
            type(observed) is CallbackLaneRejected
            and observed.code is CallbackRejectionCode.BUSY
        )
        or (
            observed.in_flight_count == 0
            and observed.can_admit_again is False
        )
    )
    final = generation.quiescence_outcome()
    assert final is not None
    assert final.in_flight_count == 0
    assert (
        generation.callback_snapshot().state
        is CallbackLaneState.QUIESCENT
    )


def test_snapshot_racing_admission_is_one_complete_counter_state():
    _, _, generation = _new_generation()
    barrier = Barrier(3)
    snapshot_box = []

    def read_snapshot():
        barrier.wait()
        snapshot_box.append(generation.callback_snapshot())

    def admit_first():
        barrier.wait()
        generation.admit_callback(
            generation.callback_generation, CallbackSequence(1)
        )

    threads = (Thread(target=read_snapshot), Thread(target=admit_first))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(snapshot_box) == 1
    observed = snapshot_box[0]
    if type(observed) is CallbackLaneRejected:
        assert observed.code is CallbackRejectionCode.BUSY
    else:
        assert (
            observed.admitted_count,
            observed.in_flight_count,
            observed.returned_count,
        ) in ((0, 0, 0), (1, 1, 0))
        assert (
            observed.admitted_count
            == observed.in_flight_count + observed.returned_count
        )


def test_owner_churn_never_closes_recreates_or_reallocates_generation():
    _, _, generation = _new_generation()
    generation_id = generation.generation_id
    callback_generation = generation.callback_generation

    for index in range(1_000):
        generation.record_seeded_owner_joined()
        ticket = generation.admit_callback(
            callback_generation, CallbackSequence(index + 1)
        )
        assert generation.append_callback(
            ticket, float32_frames(index, 1), index
        ) == SampleCursor(index + 1)
        assert generation.return_callback(ticket) is None
        generation.record_seeded_owner_left()

    assert generation.generation_id == generation_id
    assert generation.callback_generation == callback_generation
    assert generation.ring_capacity_frames == 16
    assert generation.ring_next_cursor == SampleCursor(1_000)
    assert generation.ring_retained_frames == 16
    assert generation.owner_count == 0
    assert generation.state is GenerationState.ACTIVE
    assert not hasattr(generation, "close_for_last_owner")


def test_generation_exposes_value_only_ring_reads_and_no_direct_append_escape():
    _, _, generation = _new_generation()

    assert not hasattr(generation, "ring")
    assert generation.ring_capacity_frames == 16
    assert generation.ring_snapshot() == (0, 0, 0, 0)
    assert generation.ring_oldest_cursor == SampleCursor(0)
    assert generation.ring_next_cursor == SampleCursor(0)
    assert generation.ring_retained_frames == 0

    ticket = generation.admit_callback(
        generation.callback_generation, CallbackSequence(1)
    )
    assert generation.append_callback(
        ticket, float32_frames(0, 4), 100
    ) == SampleCursor(4)
    generation.close_callback_admission(
        generation.callback_generation
    )
    assert generation.return_callback(ticket) is None
    outcome_before_read = generation.quiescence_outcome()

    read = generation.read_ring(SampleCursor(0), SampleCursor(4))

    assert type(read) is RingReadAvailable
    assert read.frames.flags.writeable is False
    assert read.timestamps_ns.flags.writeable is False
    with pytest.raises(ValueError):
        read.frames[0, 0] = 999
    with pytest.raises(ValueError):
        read.timestamps_ns[0] = 999
    assert generation.ring_snapshot() == (0, 4, 4, 1)
    assert generation.ring_next_cursor == SampleCursor(4)
    assert generation.quiescence_outcome() == outcome_before_read
    assert all(
        type(getattr(generation, name)) is not TimestampedBlockRing
        for name in dir(generation)
        if not name.startswith("_")
    )


def test_six_independent_seeded_slots_register_once_without_backend():
    ledger, authority = _issue_ledger()
    outcomes = _issue_seeded_slots()

    generations = tuple(
        ledger.reserve_seeded_generation(authority, outcome, 8, 1)
        for outcome in outcomes
    )

    assert len(outcomes) == 6
    assert len({generation.generation_id for generation in generations}) == 6
    assert all(
        generation.ring_snapshot() == (0, 0, 0, 0)
        for generation in generations
    )
    snapshot = ledger.snapshot()
    assert snapshot.revision == LedgerRevision(12)
    assert len(snapshot.generation_slots) == 6
    assert all(
        row.state is GenerationState.ACTIVE
        for row in snapshot.generation_slots
    )
    assert not any(
        name in dir(_SeededSlotOutcome)
        for name in (
            "enumerate",
            "preflight",
            "open",
            "close",
            "reset",
            "identity",
        )
    )


def test_seeded_outcome_is_private_final_unexported_field_closed_and_identity_checked():
    assert not hasattr(leaf, "_SeededSlotOutcome")
    assert {field.name for field in fields(_SeededSlotOutcome)} == {
        "slot_id",
        "state",
    }
    assert not any(
        word in field.name.lower()
        for field in fields(_SeededSlotOutcome)
        for word in (
            "hardware",
            "endpoint",
            "device",
            "backend",
            "payload",
            "object",
            "callable",
        )
    )
    with pytest.raises(TypeError):
        type("ForbiddenSeededSubclass", (_SeededSlotOutcome,), {})

    issued = _issue_seeded_slots()[0]
    structural_copy = _SeededSlotOutcome(issued.slot_id, issued.state)
    ledger, authority = _issue_ledger()
    before = ledger.snapshot()
    with pytest.raises(PermissionError, match="issuer"):
        ledger.reserve_seeded_generation(
            authority, structural_copy, 8, 1
        )
    assert ledger.snapshot() == before


def test_seeded_outcome_cannot_be_reused_or_supply_arbitrary_payload():
    issued = _issue_seeded_slots()[0]
    ledger, authority = _issue_ledger()
    ledger.reserve_seeded_generation(authority, issued, 8, 1)
    before = ledger.snapshot()

    with pytest.raises(PermissionError):
        ledger.reserve_seeded_generation(authority, issued, 8, 1)
    with pytest.raises(TypeError):
        _SeededSlotOutcome(issued.slot_id, issued.state, object())
    assert ledger.snapshot() == before


def test_seeded_reservation_requires_exact_ledger_authority_and_is_transactional():
    ledger, authority = _issue_ledger()
    other_ledger, other_authority = _issue_ledger()
    del other_ledger
    issued = _issue_seeded_slots()[0]
    before = ledger.snapshot()

    with pytest.raises(PermissionError):
        ledger.reserve_seeded_generation(
            other_authority, issued, 8, 1
        )
    assert ledger.snapshot() == before

    with pytest.raises(ValueError):
        ledger.reserve_seeded_generation(authority, issued, 0, 1)
    assert ledger.snapshot() == before

    generation = ledger.reserve_seeded_generation(
        authority, issued, 8, 1
    )
    assert generation.generation_id.slot_id == SeededSlotId(1)
    assert ledger.snapshot().revision == LedgerRevision(2)


def test_same_slot_concurrency_reserves_row_before_construction_and_returns_only_one(
    monkeypatch,
):
    ledger, authority = _issue_ledger()
    first = _issue_seeded_slots()[0]
    second = _issue_seeded_slots()[0]
    factory_entered = Event()
    allow_factory = Event()
    factory_calls = []
    factory_lock = Lock()
    real_factory = ledger_module._create_seeded_generation

    def pausing_factory(slot_id, capacity_frames, channels):
        with factory_lock:
            factory_calls.append(slot_id)
        factory_entered.set()
        assert allow_factory.wait(5)
        return real_factory(slot_id, capacity_frames, channels)

    monkeypatch.setattr(
        ledger_module, "_create_seeded_generation", pausing_factory
    )
    results = []
    errors = []

    def reserve(outcome):
        try:
            results.append(
                ledger.reserve_seeded_generation(
                    authority, outcome, 8, 1
                )
            )
        except Exception as exc:
            errors.append(exc)

    first_thread = Thread(target=reserve, args=(first,))
    first_thread.start()
    entered = factory_entered.wait(5)
    reserved = ledger.snapshot()
    second_thread = Thread(target=reserve, args=(second,))
    second_thread.start()
    second_thread.join(5)
    allow_factory.set()
    first_thread.join(5)

    assert entered
    assert reserved.revision == LedgerRevision(1)
    assert len(reserved.generation_slots) == 1
    assert (
        reserved.generation_slots[0].state
        is GenerationState.RESERVED
    )
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(factory_calls) == 1
    assert len(results) == 1
    assert len(errors) == 1
    assert type(errors[0]) is ValueError
    final = ledger.snapshot()
    assert final.revision == LedgerRevision(2)
    assert len(final.generation_slots) == 1
    assert final.generation_slots[0].state is GenerationState.ACTIVE


def test_sixth_construction_reservation_blocks_concurrent_capacity_boundary(
    monkeypatch,
):
    ledger, authority = _issue_ledger()
    initial = _issue_seeded_slots()
    for outcome in initial[:5]:
        ledger.reserve_seeded_generation(authority, outcome, 8, 1)
    sixth = initial[5]
    competing_sixth = _issue_seeded_slots()[5]
    factory_entered = Event()
    allow_factory = Event()
    real_factory = ledger_module._create_seeded_generation

    def pausing_sixth(slot_id, capacity_frames, channels):
        if slot_id == SeededSlotId(6):
            factory_entered.set()
            assert allow_factory.wait(5)
        return real_factory(slot_id, capacity_frames, channels)

    monkeypatch.setattr(
        ledger_module, "_create_seeded_generation", pausing_sixth
    )
    result_box = []
    sixth_thread = Thread(
        target=lambda: result_box.append(
            ledger.reserve_seeded_generation(
                authority, sixth, 8, 1
            )
        )
    )
    sixth_thread.start()
    entered = factory_entered.wait(5)
    during = ledger.snapshot()
    with pytest.raises(ValueError):
        ledger.reserve_seeded_generation(
            authority, competing_sixth, 8, 1
        )
    after_rejection = ledger.snapshot()
    allow_factory.set()
    sixth_thread.join(5)

    assert entered
    assert during == after_rejection
    assert during.revision == LedgerRevision(11)
    assert len(during.generation_slots) == 6
    assert (
        during.generation_slots[-1].state
        is GenerationState.RESERVED
    )
    assert not sixth_thread.is_alive()
    assert len(result_box) == 1
    final = ledger.snapshot()
    assert final.revision == LedgerRevision(12)
    assert len(final.generation_slots) == 6
    assert final.generation_slots[-1].state is GenerationState.ACTIVE


def test_generation_construction_failure_remains_exact_accounted_row_evidence(
    monkeypatch,
):
    ledger, authority = _issue_ledger()
    issued = _issue_seeded_slots()[0]

    def failing_factory(slot_id, capacity_frames, channels):
        raise RuntimeError("seeded construction failed")

    monkeypatch.setattr(
        ledger_module, "_create_seeded_generation", failing_factory
    )
    with pytest.raises(RuntimeError, match="construction failed"):
        ledger.reserve_seeded_generation(authority, issued, 8, 1)

    snapshot = ledger.snapshot()
    assert snapshot.revision == LedgerRevision(2)
    assert len(snapshot.generation_slots) == 1
    assert (
        snapshot.generation_slots[0].state
        is GenerationState.FAILED_CONSTRUCTION
    )


@pytest.mark.parametrize(
    ("failure_stage", "expected_state"),
    (
        ("factory", GenerationState.FAILED_CONSTRUCTION),
        ("before_bind", GenerationState.FAILED_CONSTRUCTION),
        ("bind_row", GenerationState.ACTIVE),
        ("bind_revision", GenerationState.ACTIVE),
        ("after_bind", GenerationState.ACTIVE),
    ),
)
@pytest.mark.parametrize(
    "exception_type",
    (RuntimeError, KeyboardInterrupt, SystemExit, _CustomProcessControl),
)
def test_seeded_reservation_reconciles_every_construction_and_bind_boundary(
    monkeypatch,
    failure_stage,
    expected_state,
    exception_type,
):
    ledger, authority = _issue_ledger()
    issued = _issue_seeded_slots()[0]
    interruption = exception_type("injected construction boundary")
    real_factory = ledger_module._create_seeded_generation

    if failure_stage == "factory":
        def failing_factory(slot_id, capacity_frames, channels):
            raise interruption

        monkeypatch.setattr(
            ledger_module, "_create_seeded_generation", failing_factory
        )
    else:
        monkeypatch.setattr(
            ledger_module,
            "_create_seeded_generation",
            real_factory,
        )
        hook_names = {
            "before_bind": "_after_seeded_generation_factory",
            "bind_row": "_after_seeded_generation_bind_row_replaced",
            "bind_revision": "_after_seeded_generation_bind_revision",
            "after_bind": "_after_seeded_generation_bind",
        }

        def interrupt_boundary(self):
            raise interruption

        monkeypatch.setattr(
            ledger_module._ResidentLifecycleLedger,
            hook_names[failure_stage],
            interrupt_boundary,
            raising=False,
        )

    with pytest.raises(exception_type) as raised:
        ledger.reserve_seeded_generation(authority, issued, 8, 1)

    assert raised.value is interruption
    snapshot = ledger.snapshot()
    assert snapshot.revision == LedgerRevision(2)
    assert len(snapshot.generation_slots) == 1
    assert snapshot.generation_slots[0].state is expected_state
    assert snapshot.generation_slots[0].revision == LedgerRevision(2)


@pytest.mark.parametrize(
    "exception_type",
    (RuntimeError, KeyboardInterrupt, SystemExit, _CustomProcessControl),
)
def test_seeded_failure_cleanup_never_masks_original_base_exception(
    monkeypatch,
    exception_type,
):
    ledger, authority = _issue_ledger()
    issued = _issue_seeded_slots()[0]
    original = exception_type("original factory failure")
    cleanup_failure = SystemExit("cleanup failure")
    cleanup_calls = []

    def failing_factory(slot_id, capacity_frames, channels):
        raise original

    def interrupt_failure_settlement(self):
        cleanup_calls.append(cleanup_failure)
        raise cleanup_failure

    monkeypatch.setattr(
        ledger_module, "_create_seeded_generation", failing_factory
    )
    monkeypatch.setattr(
        ledger_module._ResidentLifecycleLedger,
        "_after_seeded_generation_failure_row_replaced",
        interrupt_failure_settlement,
        raising=False,
    )

    with pytest.raises(exception_type) as raised:
        ledger.reserve_seeded_generation(authority, issued, 8, 1)

    assert raised.value is original
    assert cleanup_calls == [cleanup_failure]
    snapshot = ledger.snapshot()
    assert snapshot.revision == LedgerRevision(2)
    assert len(snapshot.generation_slots) == 1
    assert (
        snapshot.generation_slots[0].state
        is GenerationState.FAILED_CONSTRUCTION
    )
    assert snapshot.generation_slots[0].revision == LedgerRevision(2)


def test_seeded_bind_row_replacement_is_never_visible_before_revision_publication(
    monkeypatch,
):
    ledger, authority = _issue_ledger()
    issued = _issue_seeded_slots()[0]
    row_replaced = Event()
    allow_revision = Event()
    snapshot_done = Event()
    result_box = []
    snapshot_box = []

    def pause_after_row_replacement(self):
        row_replaced.set()
        assert allow_revision.wait(5)

    monkeypatch.setattr(
        ledger_module._ResidentLifecycleLedger,
        "_after_seeded_generation_bind_row_replaced",
        pause_after_row_replacement,
        raising=False,
    )
    reservation = Thread(
        target=lambda: result_box.append(
            ledger.reserve_seeded_generation(
                authority, issued, 8, 1
            )
        )
    )

    def snapshot_ledger():
        snapshot_box.append(ledger.snapshot())
        snapshot_done.set()

    reservation.start()
    paused = row_replaced.wait(5)
    observer = Thread(target=snapshot_ledger)
    observer.start()
    snapshot_blocked = not snapshot_done.wait(0.1)
    allow_revision.set()
    reservation.join(5)
    observer.join(5)

    assert paused
    assert snapshot_blocked
    assert not reservation.is_alive()
    assert not observer.is_alive()
    assert len(result_box) == 1
    assert len(snapshot_box) == 1
    observed = snapshot_box[0]
    assert observed.revision == LedgerRevision(2)
    assert len(observed.generation_slots) == 1
    assert observed.generation_slots[0].state is GenerationState.ACTIVE
    assert (
        observed.generation_slots[0].revision
        == observed.revision
    )


def test_post_bind_interruption_after_second_generation_never_rewrites_history(
    monkeypatch,
):
    ledger, authority = _issue_ledger()
    first, second = _issue_seeded_slots()[:2]
    first_hook_entered = Event()
    allow_first_hook = Event()
    hook_lock = Lock()
    should_pause = [True]
    interruption = KeyboardInterrupt("post-bind interruption")
    first_result = []
    first_errors = []

    def pause_first_post_bind(self):
        with hook_lock:
            pause = should_pause[0]
            should_pause[0] = False
        if pause:
            first_hook_entered.set()
            assert allow_first_hook.wait(5)
            raise interruption

    monkeypatch.setattr(
        ledger_module._ResidentLifecycleLedger,
        "_after_seeded_generation_bind",
        pause_first_post_bind,
    )

    def reserve_first():
        try:
            first_result.append(
                ledger.reserve_seeded_generation(
                    authority, first, 8, 1
                )
            )
        except BaseException as error:
            first_errors.append(error)

    thread = Thread(target=reserve_first)
    thread.start()
    paused = first_hook_entered.wait(5)
    second_generation = ledger.reserve_seeded_generation(
        authority, second, 8, 1
    )
    before_resume = ledger.snapshot()
    allow_first_hook.set()
    thread.join(5)
    after_resume = ledger.snapshot()

    assert paused
    assert not thread.is_alive()
    assert first_result == []
    assert first_errors == [interruption]
    assert second_generation.generation_id.slot_id == SeededSlotId(2)
    assert before_resume.revision == LedgerRevision(4)
    assert after_resume == before_resume
    assert tuple(
        row.revision for row in after_resume.generation_slots
    ) == (LedgerRevision(2), LedgerRevision(4))


def test_legal_partial_active_reconciliation_publishes_exact_missing_revision():
    ledger, authority = _issue_ledger()
    generation = ledger.reserve_seeded_generation(
        authority, _issue_seeded_slots()[0], 8, 1
    )
    slot_id = generation.generation_id.slot_id
    rows = ledger._ResidentLifecycleLedger__generation_rows
    row = rows[slot_id]
    ledger._ResidentLifecycleLedger__revision = 1
    construction = _SeededGenerationConstruction(
        _SeededGenerationConstructionStage.BIND_ROW_REPLACED,
        generation,
    )

    ledger._reconcile_seeded_generation_construction(
        slot_id, construction
    )

    assert ledger._ResidentLifecycleLedger__revision == 2
    assert rows[slot_id] is row
    assert row.revision == 2
    assert construction.stage is _SeededGenerationConstructionStage.ACTIVE


@pytest.mark.parametrize(
    "hostile_stage",
    (
        _SeededGenerationConstructionStage.RESERVED,
        _SeededGenerationConstructionStage.FACTORY_RETURNED,
        _SeededGenerationConstructionStage.ACTIVE,
        _SeededGenerationConstructionStage.FAILED_CONSTRUCTION,
        "BIND_ROW_REPLACED",
        GenerationState.RESERVED,
        0,
        None,
        MaliciousLookalike(),
    ),
)
def test_missing_revision_reconciliation_rejects_non_bind_stage_without_mutation(
    hostile_stage,
):
    ledger, authority = _issue_ledger()
    generation = ledger.reserve_seeded_generation(
        authority, _issue_seeded_slots()[0], 8, 1
    )
    slot_id = generation.generation_id.slot_id
    rows = ledger._ResidentLifecycleLedger__generation_rows
    row = rows[slot_id]
    ledger._ResidentLifecycleLedger__revision = 1
    construction = _SeededGenerationConstruction(
        hostile_stage,
        generation,
    )
    before_stage = construction.stage
    before = (
        ledger._ResidentLifecycleLedger__revision,
        id(rows),
        tuple(rows.keys()),
        id(rows[slot_id]),
        row.slot_ordinal,
        row.generation_ordinal,
        row.state,
        row.revision,
        row.generation,
        construction.generation,
        construction.reconciliation_failures,
    )

    with pytest.raises(RuntimeError, match="conflicting evidence"):
        ledger._reconcile_seeded_generation_construction(
            slot_id, construction
        )

    assert construction.stage is before_stage
    assert (
        ledger._ResidentLifecycleLedger__revision,
        id(rows),
        tuple(rows.keys()),
        id(rows[slot_id]),
        row.slot_ordinal,
        row.generation_ordinal,
        row.state,
        row.revision,
        row.generation,
        construction.generation,
        construction.reconciliation_failures,
    ) == before


def test_exact_complete_active_reconciliation_is_pure_idempotent_zero_write():
    ledger, authority = _issue_ledger()
    generation = ledger.reserve_seeded_generation(
        authority, _issue_seeded_slots()[0], 8, 1
    )
    slot_id = generation.generation_id.slot_id
    rows = ledger._ResidentLifecycleLedger__generation_rows
    row = rows[slot_id]
    construction = _SeededGenerationConstruction(
        _SeededGenerationConstructionStage.ACTIVE,
        generation,
    )
    before = (
        ledger._ResidentLifecycleLedger__revision,
        id(rows[slot_id]),
        row.revision,
        construction.stage,
        construction.reconciliation_failures,
    )

    ledger._reconcile_seeded_generation_construction(
        slot_id, construction
    )

    assert (
        ledger._ResidentLifecycleLedger__revision,
        id(rows[slot_id]),
        row.revision,
        construction.stage,
        construction.reconciliation_failures,
    ) == before


@pytest.mark.parametrize(
    "invalid_case", ("same_revision", "revision_gap", "identity_mismatch")
)
def test_illegal_partial_active_reconciliation_never_rewrites_revision(
    invalid_case,
):
    ledger, authority = _issue_ledger()
    first, second = _issue_seeded_slots()[:2]
    generation = ledger.reserve_seeded_generation(
        authority, first, 8, 1
    )
    other_generation = ledger.reserve_seeded_generation(
        authority, second, 8, 1
    )
    slot_id = generation.generation_id.slot_id
    rows = ledger._ResidentLifecycleLedger__generation_rows
    row = rows[slot_id]
    ledger._ResidentLifecycleLedger__revision = 2
    if invalid_case == "revision_gap":
        row.revision = 4
    candidate = (
        other_generation
        if invalid_case == "identity_mismatch"
        else generation
    )
    construction = _SeededGenerationConstruction(
        _SeededGenerationConstructionStage.BIND_ROW_REPLACED,
        candidate,
    )
    before = (
        ledger._ResidentLifecycleLedger__revision,
        row.revision,
        row.generation,
        row.state,
    )

    with pytest.raises(RuntimeError, match="conflicting evidence"):
        ledger._reconcile_seeded_generation_construction(
            slot_id, construction
        )

    assert (
        ledger._ResidentLifecycleLedger__revision,
        row.revision,
        row.generation,
        row.state,
    ) == before


def test_same_seeded_outcome_cannot_be_consumed_by_two_ledgers_concurrently():
    first_ledger, first_authority = _issue_ledger()
    second_ledger, second_authority = _issue_ledger()
    issued = _issue_seeded_slots()[0]
    barrier = Barrier(3)
    results = []
    errors = []

    def reserve(ledger, authority):
        barrier.wait()
        try:
            results.append(
                ledger.reserve_seeded_generation(
                    authority, issued, 8, 1
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = (
        Thread(target=reserve, args=(first_ledger, first_authority)),
        Thread(target=reserve, args=(second_ledger, second_authority)),
    )
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 1
    assert len(errors) == 1
    assert type(errors[0]) is PermissionError
    snapshots = (first_ledger.snapshot(), second_ledger.snapshot())
    assert sorted(snapshot.revision.value for snapshot in snapshots) == [
        0,
        2,
    ]
    assert sum(
        len(snapshot.generation_slots) for snapshot in snapshots
    ) == 1


def test_altered_issuer_outcome_is_rejected_without_ledger_mutation():
    issued = _issue_seeded_slots()[0]
    object.__setattr__(issued, "slot_id", SeededSlotId(2))
    ledger, authority = _issue_ledger()
    before = ledger.snapshot()

    with pytest.raises(PermissionError, match="altered"):
        ledger.reserve_seeded_generation(authority, issued, 8, 1)

    assert ledger.snapshot() == before


@pytest.mark.parametrize("exact_type", EXACT_TYPES)
def test_every_new_exact_type_is_frozen_slotted_runtime_final_and_forgery_safe(
    exact_type,
):
    valid = _valid_exact(exact_type)
    assert hasattr(exact_type, "__slots__")
    with pytest.raises(TypeError):
        type("ForbiddenSubclass", (exact_type,), {})
    if fields(exact_type):
        first = fields(exact_type)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(valid, first, getattr(valid, first))

    missing = object.__new__(exact_type)
    with pytest.raises(TypeError):
        exact_type.__post_init__(missing)

    forged = object.__new__(exact_type)
    for field in fields(exact_type):
        object.__setattr__(forged, field.name, getattr(valid, field.name))
    with pytest.raises(TypeError, match="constructor"):
        exact_type.__post_init__(forged)


@pytest.mark.parametrize("enum_type", EXACT_ENUM_TYPES)
def test_every_new_enum_is_closed_and_rejects_raw_string_lookalikes(enum_type):
    assert tuple(enum_type)
    with pytest.raises(TypeError):
        new_class("ForbiddenEnumSubclass", (enum_type,))


@pytest.mark.parametrize(
    "bounded_type",
    (SeededSlotId, SampleCursor, CallbackSequence),
)
@pytest.mark.parametrize(
    "bad_value", (True, False, -1, MAX_SIGNED_63 + 1, "1")
)
def test_new_integer_values_reject_bool_wrong_class_and_out_of_range(
    bounded_type, bad_value
):
    with pytest.raises((TypeError, ValueError)):
        bounded_type(bad_value)


def test_slot_and_sequences_enforce_positive_bounds():
    with pytest.raises(ValueError):
        SeededSlotId(0)
    with pytest.raises(ValueError):
        SeededSlotId(7)
    with pytest.raises(ValueError):
        CallbackTicket(
            CallbackGeneration(GenerationId(SeededSlotId(1), 1), 1),
            CallbackSequence(0),
        )
    assert CallbackSequence(0).value == 0
    assert SampleCursor(0).value == 0


def test_nested_exact_types_reject_structural_and_malicious_lookalikes():
    malicious = MaliciousLookalike()
    _, generation, callback_generation, sequence, _ = _identities()

    constructors = (
        lambda: GenerationId(malicious, 1),
        lambda: CallbackGeneration(malicious, 1),
        lambda: CallbackTicket(malicious, sequence),
        lambda: CallbackTicket(callback_generation, malicious),
        lambda: _SeededSlotOutcome(malicious, SeededSlotState.ISSUED),
        lambda: CallbackQuiescenceOutcome(
            malicious,
            callback_generation,
            callback_generation,
            0,
            sequence,
            sequence,
            False,
        ),
        lambda: CallbackQuiescenceOutcome(
            generation,
            malicious,
            callback_generation,
            0,
            sequence,
            sequence,
            False,
        ),
        lambda: RingReadEvicted(
            malicious, SampleCursor(1), SampleCursor(1)
        ),
        lambda: RingReadUnavailable(
            SampleCursor(1), malicious, SampleCursor(1)
        ),
        lambda: GenerationRegistrationRowSnapshot(
            malicious,
            RedactedIdentifier(RedactedIdentifierKind.GENERATION, 1),
            GenerationState.ACTIVE,
            LedgerRevision(1),
        ),
    )
    for construct in constructors:
        with pytest.raises(TypeError):
            construct()


@pytest.mark.parametrize("exact_type", EXACT_TYPES)
def test_every_new_exact_type_rejects_a_malicious_wrong_class(exact_type):
    malicious = MaliciousLookalike()
    slot, generation, callback_generation, sequence, _ = _identities()
    constructors = {
        SeededSlotId: lambda: SeededSlotId(malicious),
        GenerationId: lambda: GenerationId(malicious, 1),
        SampleCursor: lambda: SampleCursor(malicious),
        CallbackGeneration: lambda: CallbackGeneration(malicious, 1),
        CallbackSequence: lambda: CallbackSequence(malicious),
        CallbackTicket: lambda: CallbackTicket(malicious, sequence),
        CallbackLaneRejected: lambda: CallbackLaneRejected(
            malicious, CallbackRejectionCode.BUSY
        ),
        _SeededSlotOutcome: lambda: _SeededSlotOutcome(
            malicious, SeededSlotState.ISSUED
        ),
        CallbackLaneSnapshot: lambda: CallbackLaneSnapshot(
            malicious,
            CallbackLaneState.ADMITTING,
            0,
            0,
            0,
            CallbackSequence(0),
            CallbackSequence(0),
            CallbackSequence(0),
        ),
        CallbackQuiescenceOutcome: lambda: CallbackQuiescenceOutcome(
            malicious,
            callback_generation,
            callback_generation,
            0,
            CallbackSequence(0),
            CallbackSequence(0),
            False,
        ),
        GenerationRegistrationRowSnapshot: lambda: (
            GenerationRegistrationRowSnapshot(
                malicious,
                RedactedIdentifier(
                    RedactedIdentifierKind.GENERATION, 1
                ),
                GenerationState.ACTIVE,
                LedgerRevision(1),
            )
        ),
        RingReadAvailable: lambda: RingReadAvailable(
            malicious,
            SampleCursor(1),
            np.zeros((1, 1), dtype=np.float32),
            np.zeros((1,), dtype=np.int64),
        ),
        RingReadBusy: lambda: RingReadBusy(
            malicious, SampleCursor(1)
        ),
        RingReadFaulted: lambda: RingReadFaulted(
            malicious, SampleCursor(1)
        ),
        RingReadEvicted: lambda: RingReadEvicted(
            malicious, SampleCursor(1), SampleCursor(1)
        ),
        RingReadUnavailable: lambda: RingReadUnavailable(
            malicious, SampleCursor(2), SampleCursor(1)
        ),
        RingAppendRejected: lambda: RingAppendRejected(
            malicious, SampleCursor(0)
        ),
        RingStateFaulted: lambda: RingStateFaulted(malicious),
    }
    with pytest.raises(TypeError):
        constructors[exact_type]()


def test_wrong_nested_identity_and_bool_counts_are_rejected():
    slot, generation, callback_generation, sequence, _ = _identities()
    other_generation = GenerationId(SeededSlotId(2), 1)
    other_callback = CallbackGeneration(other_generation, 1)

    with pytest.raises(ValueError):
        CallbackTicket(other_callback, sequence).__class__(
            callback_generation, CallbackSequence(0)
        )
    with pytest.raises(ValueError, match="same generation"):
        CallbackQuiescenceOutcome(
            generation,
            other_callback,
            other_callback,
            0,
            sequence,
            sequence,
            False,
        )
    with pytest.raises(TypeError):
        CallbackLaneSnapshot(
            callback_generation,
            CallbackLaneState.ADMITTING,
            True,
            0,
            0,
            CallbackSequence(0),
            CallbackSequence(0),
            CallbackSequence(0),
        )
    with pytest.raises(TypeError):
        _SeededSlotOutcome(slot, "ISSUED")
    with pytest.raises(TypeError):
        CallbackLaneRejected(callback_generation, "BUSY")
    with pytest.raises(TypeError):
        RingAppendRejected("BUSY", SampleCursor(0))
    with pytest.raises(TypeError):
        GenerationId(slot, True)
    with pytest.raises((TypeError, ValueError)):
        GenerationId(slot, MAX_SIGNED_63 + 1)
    with pytest.raises(TypeError):
        CallbackGeneration(generation, True)
    with pytest.raises(TypeError):
        CallbackLaneSnapshot(
            callback_generation,
            "ADMITTING",
            0,
            0,
            0,
            CallbackSequence(0),
            CallbackSequence(0),
            CallbackSequence(0),
        )
    with pytest.raises(TypeError):
        CallbackQuiescenceOutcome(
            generation,
            callback_generation,
            callback_generation,
            0,
            sequence,
            sequence,
            0,
        )


def test_available_ring_outcome_rejects_unsafe_or_inconsistent_payloads():
    start = SampleCursor(0)
    end = SampleCursor(1)
    valid_frames = np.zeros((1, 1), dtype=np.float32)
    valid_timestamps = np.zeros((1,), dtype=np.int64)

    bad_payloads = (
        (
            np.zeros((1, 1), dtype=np.float64),
            valid_timestamps,
        ),
        (
            np.array([[np.nan]], dtype=np.float32),
            valid_timestamps,
        ),
        (
            valid_frames,
            np.array([-1], dtype=np.int64),
        ),
        (
            np.zeros((2, 1), dtype=np.float32),
            valid_timestamps,
        ),
    )
    for frames_value, timestamps_value in bad_payloads:
        with pytest.raises((TypeError, ValueError)):
            RingReadAvailable(
                start, end, frames_value, timestamps_value
            )


def test_generation_snapshot_rejects_wrong_redacted_identifier_kinds():
    with pytest.raises(TypeError):
        GenerationRegistrationRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.GENERATION, 1),
            RedactedIdentifier(RedactedIdentifierKind.GENERATION, 1),
            GenerationState.ACTIVE,
            LedgerRevision(1),
        )
    with pytest.raises(TypeError):
        GenerationRegistrationRowSnapshot(
            RedactedIdentifier(RedactedIdentifierKind.SEEDED_SLOT, 1),
            RedactedIdentifier(RedactedIdentifierKind.SEEDED_SLOT, 1),
            GenerationState.ACTIVE,
            LedgerRevision(1),
        )


def test_ring_and_generation_constructors_reject_unsafe_values_and_direct_generation():
    for capacity, channels in (
        (True, 1),
        (0, 1),
        (MAX_SIGNED_63 + 1, 1),
        (8, True),
        (8, 0),
        (8, 65),
    ):
        with pytest.raises((TypeError, ValueError)):
            TimestampedBlockRing(capacity, channels)

    with pytest.raises(PermissionError):
        _ResidentGeneration(object(), _issue_seeded_slots()[0], 8, 1)


def test_generation_issuer_seal_is_final_identity_only_and_never_authorizes_direct_construction():
    assert not hasattr(generation_module, "_GENERATION_ISSUER_SEAL")
    with pytest.raises(TypeError):
        type("ForbiddenSealSubclass", (_GenerationIssuerSeal,), {})

    structural_copy = _GenerationIssuerSeal()
    missing = object.__new__(_GenerationIssuerSeal)
    forged = object.__new__(_GenerationIssuerSeal)
    forged._marker = object()
    assert structural_copy._marker is None
    with pytest.raises(AttributeError):
        getattr(missing, "_marker")
    copies = (
        structural_copy,
        missing,
        forged,
        object(),
        MaliciousLookalike(),
    )
    for candidate in copies:
        with pytest.raises(PermissionError):
            _ResidentGeneration(
                candidate, SeededSlotId(1), 8, 1
            )

    with pytest.raises(PermissionError):
        _ResidentGeneration(
            _GenerationIssuerSeal(), SeededSlotId(1), 8, 1
        )


def test_reprs_do_not_expose_seeded_or_generation_identity_values():
    slot, generation, callback_generation, sequence, ticket = _identities(6)
    outcome = _SeededSlotOutcome(slot, SeededSlotState.ISSUED)

    for value in (
        slot,
        generation,
        callback_generation,
        ticket,
        outcome,
    ):
        assert "6" not in repr(value)
    assert "payload" not in repr(outcome).lower()


def test_generation_registration_extends_ledger_snapshot_without_aliasing():
    ledger, authority, generation = _new_generation()
    first = ledger.snapshot()
    second = ledger.snapshot()

    assert type(first) is LedgerSnapshot
    assert first.revision == LedgerRevision(2)
    assert first.generation_slots[0] is not second.generation_slots[0]
    assert first.generation_slots[0].revision is not second.generation_slots[0].revision
    assert generation.generation_id.slot_id == SeededSlotId(1)

    object.__setattr__(
        first.generation_slots[0], "state", "CORRUPTED"
    )
    object.__setattr__(
        first.generation_slots[0].revision, "value", 99
    )
    fresh = ledger.snapshot()
    assert fresh.generation_slots[0].state is GenerationState.ACTIVE
    assert fresh.generation_slots[0].revision == LedgerRevision(2)
