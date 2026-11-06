from __future__ import annotations

import dataclasses
import builtins
import gc
import hmac
import json
import threading
import tracemalloc
import types
import weakref
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from app.service.analysis.resident_audio import (
    CapturedAudioBlock,
    CaptureErrorCode,
    CaptureOwner,
    CaptureRequest,
    LeaseState,
    PoolState,
    RawCallbackBlock,
    ResidentStream,
    StreamReadyToken,
    TimestampedBlockRing,
    production_block_frames,
)
from app.service.analysis.resident_audio.ingress import (
    GenerationLatch,
    PortAudioCallbackAdapter,
    SpscBlockIngress,
)
from app.service.analysis.resident_audio.owner_finality import (
    FinalityDurableProgress,
    OwnerFinalityLedger,
)
from app.service.analysis.resident_audio.owner_finality_types import (
    OwnerFinalityState,
)
from app.service.analysis.resident_audio.owner_identity import (
    FinalityAckReceipt,
    FinalityPruneOutcome,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerRemovalOutcome,
    StreamInstanceId,
)
from app.service.analysis.resident_audio.owner_types import (
    DequeLiveReferenceQueue,
)
from app.service.analysis.resident_audio.artifact_writer import (
    CaptureArtifactWriter,
)
from app.service.analysis.resident_audio.owner_settlement_types import (
    SettlementClaim,
)
from tests.resident_audio.fakes import FakeInputHandle
from tests.resident_audio import (
    test_owner_settlement as settlement_support,
    test_resident_stream as stream_support,
)
from tests.resident_audio.test_resident_pool import (
    desired,
    make_pool,
    make_ready,
    request as pool_request,
)


OWNER_COUNT = 1_000
TERMINAL_DIAGNOSTIC_LIMIT = 32


def _stream_id(*, nonce: str = "stress-stream") -> StreamInstanceId:
    return StreamInstanceId(
        pool_epoch="stress-epoch",
        hardware_id="stress-mic",
        stream_generation=7,
        instance_nonce=nonce,
    )


def _owner_id(
    index: int,
    *,
    stream: StreamInstanceId | None = None,
) -> OwnerInstanceId:
    stream = stream or _stream_id()
    return OwnerInstanceId(
        pool_epoch=stream.pool_epoch,
        hardware_id=stream.hardware_id,
        stream_generation=stream.stream_generation,
        run_id=f"stress-run-{index}",
        commit_attempt_id=f"attempt-{index}",
        owner_capability=f"capability-{index}",
        instance_nonce=f"owner-nonce-{index}",
    )


def _settle_ledger_entry(
    ledger: OwnerFinalityLedger,
    instance_id: OwnerInstanceId,
) -> FinalityAckReceipt:
    admission = ledger.reserve_prepare(instance_id)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(instance_id)
    claim = ledger.claim_prepare(instance_id)
    assert claim.activation_allowed
    ledger.resolve_prepare(instance_id, activated=True)
    removal = ledger.request_removal(
        instance_id, registration_present=True
    )
    assert removal.outcome is OwnerRemovalOutcome.REMOVED
    durable = FinalityDurableProgress(
        removal_receipt=removal,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )
    acknowledgement = ledger._acknowledge_entry(
        instance_id, removal, durable
    )
    assert acknowledgement is not None
    pruned = ledger._prune_entry(instance_id, acknowledgement)
    assert pruned.outcome is FinalityPruneOutcome.PRUNED
    return acknowledgement


class _StressWriter:
    def __init__(self, index: int) -> None:
        self.artifact_id = f"artifact-{index}"

    def bind_capture_start(self, **_kwargs) -> None:
        return None

    def write(self, _audio_block) -> int:
        return 0

    def freeze(self):
        raise AssertionError("canceled stress owners must not freeze")

    def discard(self) -> bool:
        return True


class _TerminalHandler:
    def __call__(self, _event) -> None:
        return None


class _PerOwnerCallback:
    def __call__(self, _completion) -> None:
        return None


def _capture_request(index: int) -> CaptureRequest:
    return CaptureRequest(
        run_id=f"stress-run-{index}",
        trigger_monotonic=10.0,
        record_duration=0.1,
        stream_token=StreamReadyToken(
            hardware_id="stress-mic",
            pool_epoch="stress-epoch",
            stream_generation=7,
            sample_rate=10.0,
            channels=1,
            earliest_mappable_monotonic=9.0,
            latest_mappable_monotonic=11.0,
            earliest_sample=0,
            latest_sample=1,
        ),
    )


def _resident_infrastructure():
    ring = TimestampedBlockRing(
        pool_epoch="stress-epoch",
        hardware_id="stress-mic",
        stream_generation=7,
        sample_rate=10.0,
    )
    latch = GenerationLatch()
    ingress = SpscBlockIngress(latch)
    callback = PortAudioCallbackAdapter(
        pool_epoch="stress-epoch",
        hardware_id="stress-mic",
        stream_generation=7,
        generation_latch=latch,
        ingress=ingress,
        monotonic_clock=lambda: 10.0,
    )
    handle = FakeInputHandle()
    handle.callback = callback
    return ring, latch, ingress, callback, handle


def _new_collectible_owner(index: int, ring: TimestampedBlockRing):
    writer = _StressWriter(index)
    owner_queue = DequeLiveReferenceQueue()
    terminal_handler = _TerminalHandler()

    def control_handler(_event) -> None:
        return None

    owner = CaptureOwner(
        request=_capture_request(index),
        target_frames=1,
        commit_attempt_id=f"attempt-{index}",
        owner_capability=f"capability-{index}",
        ring=ring,
        writer=writer,
        terminal_sink=terminal_handler,
        control_sink=control_handler,
        monotonic_clock=lambda: 10.0,
        live_queue=owner_queue,
    )
    return owner, writer, owner_queue, terminal_handler, control_handler


def _settle_collectible_owner(
    index: int,
    ring: TimestampedBlockRing,
    ledger: OwnerFinalityLedger,
):
    owner, writer, owner_queue, handler, closure = _new_collectible_owner(
        index, ring
    )
    references = tuple(
        weakref.ref(value)
        for value in (owner, writer, owner_queue, handler, closure)
    )
    instance_id = _owner_id(index)
    admission = ledger.reserve_prepare(instance_id)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.claim_prepare(instance_id)
    ledger.resolve_prepare(instance_id, activated=True)
    removal = ledger.request_removal(
        instance_id, registration_present=True
    )
    assert owner.cancel("stress settlement")
    assert owner.join(timeout=1.0)
    durable = FinalityDurableProgress(
        removal_receipt=removal,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )
    acknowledgement = ledger._acknowledge_entry(
        instance_id, removal, durable
    )
    assert acknowledgement is not None
    assert (
        ledger._prune_entry(instance_id, acknowledgement).outcome
        is FinalityPruneOutcome.PRUNED
    )
    del owner, writer, owner_queue, handler, closure
    return references


def _collect_twice() -> None:
    gc.collect()
    gc.collect()


def _resident_audio_traced_bytes() -> int:
    snapshot = tracemalloc.take_snapshot()
    return sum(
        trace.size
        for trace in snapshot.traces
        if "/app/service/analysis/resident_audio/"
        in trace.traceback[0].filename.replace("\\", "/")
    )


def _live_type_counts() -> dict[str, int]:
    tracked = (
        CaptureOwner,
        CaptureArtifactWriter,
        _StressWriter,
        DequeLiveReferenceQueue,
        _TerminalHandler,
        _PerOwnerCallback,
        SettlementClaim,
        types.MethodType,
    )
    counts = {kind.__name__: 0 for kind in tracked}
    for value in gc.get_objects():
        value_type = type(value)
        if value_type in tracked:
            counts[value_type.__name__] += 1
    return counts


def _settle_real_pool_owner(pool, token, stream, index):
    run_id = f"real-stress-{index}"
    handler = _PerOwnerCallback()
    pool.set_completion_handler(handler)
    assert pool.reserve(run_id).state is LeaseState.RESERVED
    committed = pool.commit(
        run_id,
        token,
        pool_request(token, run_id, duration=0.5),
    )
    assert committed.state is LeaseState.COMMITTED
    record = pool._leases[run_id]
    owner = record.owner
    writer = record.writer
    assert isinstance(owner, CaptureOwner)
    assert isinstance(writer, CaptureArtifactWriter)
    assert record.owner_instance_id in stream._registrations
    references = (
        weakref.ref(owner),
        weakref.ref(writer),
        weakref.ref(owner._live_queue),
        weakref.ref(handler),
        weakref.ref(owner._terminal_sink),
        weakref.ref(owner._control_sink),
    )
    finality_during_active = stream.finality_snapshot()
    canceled = pool.cancel(run_id, "real stress settlement")
    assert canceled.state is LeaseState.CANCELED
    terminal = pool._leases[run_id]
    assert terminal.settled
    assert terminal.owner_instance_id not in stream._registrations
    assert stream.finality_snapshot().entries == ()
    assert terminal.settlement_claim is None
    for field in (
        "owner",
        "writer",
        "stream",
        "artifact",
    ):
        assert getattr(terminal, field) is None
    return references, len(finality_during_active.entries)


def test_one_thousand_sequential_owners_are_collectible_and_ledger_stays_bounded(
    tmp_path: Path,
):
    pool, resolver, backend, _store, _clock = make_pool(
        tmp_path, terminal_diagnostic_limit=TERMINAL_DIAGNOSTIC_LIMIT
    )
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    ring = stream._ring
    callback = stream._callback
    ring_ref = weakref.ref(ring)
    callback_ref = weakref.ref(callback)
    per_owner_references: list[tuple[weakref.ReferenceType, ...]] = []
    maximum_entries = 0
    try:
        for index in range(OWNER_COUNT):
            references, active_entries = _settle_real_pool_owner(
                pool, token, stream, index
            )
            per_owner_references.append(references)
            maximum_entries = max(maximum_entries, active_entries)
            assert len(stream._registrations) == 0
            assert pool.snapshot().terminal_diagnostic_count <= (
                TERMINAL_DIAGNOSTIC_LIMIT
            )

        pool.set_completion_handler(lambda _completion: None)
        _collect_twice()
        snapshot = stream.finality_snapshot()
        assert 0 < maximum_entries <= snapshot.ledger_limit
        assert snapshot.entries == ()
        assert snapshot.pruned_entry_count == OWNER_COUNT
        assert len(pool._leases) == TERMINAL_DIAGNOSTIC_LIMIT
        assert all(
            reference() is None
            for references in per_owner_references
            for reference in references
        )
        assert ring_ref() is ring
        assert callback_ref() is callback
        assert handle.callback is callback
    finally:
        pool.shutdown()
    del (
        handle,
        callback,
        ring,
        stream,
        pool,
        resolver,
        backend,
        _store,
        _clock,
        token,
    )
    _collect_twice()
    assert ring_ref() is None
    assert callback_ref() is None


def test_small_injected_ledger_limit_backpressures_without_unsafe_eviction():
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id, limit=2)
    retained = [_owner_id(index, stream=stream_id) for index in range(3)]

    first = ledger.reserve_prepare(retained[0])
    second = ledger.reserve_prepare(retained[1])
    rejected = ledger.reserve_prepare(retained[2])

    assert not isinstance(first, OwnerNonAdmissionProof)
    assert not isinstance(second, OwnerNonAdmissionProof)
    assert isinstance(rejected, OwnerNonAdmissionProof)
    assert rejected.confirms(retained[2])
    snapshot = ledger.snapshot()
    assert len(snapshot.entries) == snapshot.ledger_limit == 2
    assert snapshot.pending_late_commands == 2
    assert snapshot.backpressure_rejection_count == 1
    serialized = json.dumps(dataclasses.asdict(snapshot), default=str)
    assert retained[0].instance_nonce in serialized
    assert retained[1].instance_nonce in serialized
    assert retained[2].instance_nonce not in serialized
    assert "capability-" not in serialized


def test_real_pool_backpressure_rolls_back_resources_and_reuses_pruned_slot(
    tmp_path,
    monkeypatch,
):
    def one_entry_stream(**kwargs):
        return ResidentStream(owner_finality_limit=1, **kwargs)

    pool, resolver, backend, store, _clock = make_pool(
        tmp_path, stream_factory=one_entry_stream
    )
    _handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    pool.reserve("holder")
    assert pool.commit(
        "holder", token, pool_request(token, "holder", duration=0.5)
    ).state is LeaseState.COMMITTED
    holder_quota = store.reserved_bytes
    original_remove = stream.remove_owner
    removal_calls = []

    def observed_remove(*args, **kwargs):
        removal_calls.append(args[0])
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(stream, "remove_owner", observed_remove)
    pool.reserve("rejected")
    rejected_outcome = pool.commit(
        "rejected", token, pool_request(token, "rejected", duration=0.5)
    )
    rejected = pool._leases["rejected"]
    diagnostic = dict(rejected_outcome.diagnostic_context)

    assert rejected_outcome.state is LeaseState.FAILED
    assert rejected_outcome.error_code is CaptureErrorCode.CAPTURE_CAPACITY_FULL
    assert diagnostic["capacity_domain"] == "owner_finality_ledger"
    assert diagnostic["ledger_limit"] == 1
    assert diagnostic["ledger_active_count"] == 1
    assert diagnostic["backpressure_rejection_count"] == 1
    assert rejected.owner is None
    assert rejected.writer is None
    assert rejected.settlement_claim is None
    assert rejected.terminal_summary.non_admission_confirmed
    assert removal_calls == []
    assert store.reserved_bytes == holder_quota
    assert len(stream.finality_snapshot().entries) == 1

    pool.cancel("holder", "release ledger slot")
    assert stream.finality_snapshot().entries == ()
    pool.reserve("admitted-after-prune")
    admitted = pool.commit(
        "admitted-after-prune",
        token,
        pool_request(token, "admitted-after-prune", duration=0.5),
    )
    assert admitted.state is LeaseState.COMMITTED
    assert len(stream.finality_snapshot().entries) == 1
    pool.cancel("admitted-after-prune", "cleanup")
    pool.shutdown()


def test_acknowledged_unpruned_entry_counts_toward_limit():
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id, limit=1)
    first = _owner_id(1, stream=stream_id)
    second = _owner_id(2, stream=stream_id)

    admission = ledger.reserve_prepare(first)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(first)
    ledger.claim_prepare(first)
    ledger.resolve_prepare(first, activated=True)
    removal = ledger.request_removal(first, registration_present=True)
    durable = FinalityDurableProgress(
        removal_receipt=removal,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )
    acknowledgement = ledger._acknowledge_entry(first, removal, durable)
    assert acknowledgement is not None

    rejected = ledger.reserve_prepare(second)
    assert isinstance(rejected, OwnerNonAdmissionProof)
    assert ledger.snapshot().acknowledged_awaiting_prune == 1
    assert len(ledger.snapshot().entries) == 1

    assert (
        ledger._prune_entry(first, acknowledgement).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert not isinstance(
        ledger.reserve_prepare(second), OwnerNonAdmissionProof
    )


def test_blocked_late_prepare_retains_only_resolution_state():
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id, limit=1)
    instance_id = _owner_id(1, stream=stream_id)
    ledger.reserve_prepare(instance_id)
    ledger.mark_prepare_enqueued(instance_id)
    claim = ledger.claim_prepare(instance_id)
    assert claim.activation_allowed

    receipt = ledger.request_removal(
        instance_id, registration_present=False
    )
    snapshot = ledger.snapshot()
    serialized = json.dumps(dataclasses.asdict(snapshot), default=str)

    assert receipt.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert snapshot.pending_late_commands == 1
    assert len(snapshot.entries) == 1
    assert instance_id.instance_nonce in serialized
    assert instance_id.owner_capability not in serialized
    assert not any(
        isinstance(value, (CaptureOwner, _StressWriter))
        for value in gc.get_referents(ledger)
    )

    ledger.resolve_prepare(instance_id, activated=False)
    exact = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert exact.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT


def test_replacement_stream_rejects_old_ack_receipt():
    old_stream = _stream_id(nonce="old-stream")
    replacement_stream = _stream_id(nonce="replacement-stream")
    old_ledger = OwnerFinalityLedger(old_stream)
    replacement_ledger = OwnerFinalityLedger(replacement_stream)
    instance_id = _owner_id(1, stream=old_stream)
    acknowledgement = _settle_ledger_entry(old_ledger, instance_id)

    outcome = replacement_ledger._prune_entry(
        instance_id, acknowledgement
    )

    assert outcome.outcome is FinalityPruneOutcome.MISMATCH
    assert replacement_ledger.snapshot().entries == ()
    assert replacement_ledger.snapshot().pruned_entry_count == 0


@pytest.mark.parametrize("receipt_kind", ["missing", "altered", "wrong_owner"])
def test_missing_altered_and_wrong_owner_ack_receipts_cannot_prune(
    receipt_kind,
):
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id)
    instance_id = _owner_id(41, stream=stream_id)
    admission = ledger.reserve_prepare(instance_id)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.claim_prepare(instance_id)
    ledger.resolve_prepare(instance_id, activated=True)
    removal = ledger.request_removal(
        instance_id, registration_present=True
    )
    durable = FinalityDurableProgress(
        removal_receipt=removal,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )
    acknowledgement = ledger._acknowledge_entry(
        instance_id, removal, durable
    )
    assert acknowledgement is not None
    if receipt_kind == "missing":
        candidate = None
    elif receipt_kind == "altered":
        candidate = dataclasses.replace(
            acknowledgement, ack_token="altered-token"
        )
    else:
        candidate = dataclasses.replace(
            acknowledgement,
            owner_instance_id=_owner_id(42, stream=stream_id),
        )

    rejected = ledger._prune_entry(instance_id, candidate)

    assert rejected.outcome is FinalityPruneOutcome.MISMATCH
    assert ledger.snapshot().acknowledged_awaiting_prune == 1
    assert len(ledger.snapshot().entries) == 1
    assert (
        ledger._prune_entry(instance_id, acknowledgement).outcome
        is FinalityPruneOutcome.PRUNED
    )


@pytest.mark.parametrize(
    "invalid_kind",
    ["missing", "altered", "wrong_owner", "replacement_stream"],
)
def test_real_lease_public_prune_rejects_invalid_ack_until_exact_retry(
    invalid_kind, tmp_path, monkeypatch
):
    pool, resolver, backend, store, _clock = make_pool(tmp_path)
    _handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    original_prune = stream.prune_finality
    terminal_calls = 0
    original_terminal = pool._settlement_adapter.terminal_cas

    def terminal_spy(*args, **kwargs):
        nonlocal terminal_calls
        terminal_calls += 1
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(
        pool._settlement_adapter, "terminal_cas", terminal_spy
    )
    exact_enabled = False
    public_outcomes = []

    def invalidating_public_prune(instance_id, acknowledgement, **kwargs):
        if exact_enabled:
            return original_prune(instance_id, acknowledgement, **kwargs)
        if invalid_kind == "missing":
            invalid = None
        elif invalid_kind == "altered":
            invalid = dataclasses.replace(
                acknowledgement, ack_token="altered-public-token"
            )
        elif invalid_kind == "wrong_owner":
            invalid = dataclasses.replace(
                acknowledgement,
                owner_instance_id=dataclasses.replace(
                    instance_id, instance_nonce="wrong-public-owner"
                ),
            )
        else:
            invalid = dataclasses.replace(
                acknowledgement,
                stream_instance_id=StreamInstanceId(
                    instance_id.pool_epoch,
                    instance_id.hardware_id,
                    instance_id.stream_generation,
                    "replacement-stream-instance",
                ),
            )
        outcome = original_prune(instance_id, invalid, **kwargs)
        public_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(
        stream, "prune_finality", invalidating_public_prune
    )
    run_id = f"invalid-public-{invalid_kind}"
    pool.reserve(run_id)
    assert pool.commit(
        run_id, token, pool_request(token, run_id, duration=0.5)
    ).state is LeaseState.COMMITTED
    quota_before = store.reserved_bytes

    paused = pool.cancel(run_id, "invalid ack gate")
    record = pool._leases[run_id]

    assert paused.state is LeaseState.COMMITTED
    assert not record.settled
    assert pool.snapshot().active_lease_count == 1
    assert store.reserved_bytes == quota_before
    assert terminal_calls == 0
    assert public_outcomes
    assert all(
        result.outcome is FinalityPruneOutcome.MISMATCH
        for result in public_outcomes
    )
    assert record.settlement_claim.progress.finality_acknowledged
    assert not record.settlement_claim.progress.finality_pruned
    assert not record.settlement_claim.progress.artifact_disposition_confirmed

    exact_enabled = True
    pool.retry_settlements()

    assert pool.lease_snapshot(run_id).state is LeaseState.CANCELED
    assert pool.lease_snapshot(run_id).settled
    assert pool.snapshot().active_lease_count == 0
    assert store.reserved_bytes == 0
    assert terminal_calls == 1
    pool.shutdown()


@pytest.mark.parametrize(
    "changed_field",
    [
        "pool_epoch",
        "hardware_id",
        "stream_generation",
        "run_id",
        "commit_attempt_id",
        "owner_capability",
        "instance_nonce",
        "complete_identity",
    ],
)
def test_identity_isolation_matrix_remains_exact_under_repeated_retry(
    changed_field: str,
):
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id)
    active = _owner_id(1, stream=stream_id)
    ledger.reserve_prepare(active)
    ledger.mark_prepare_enqueued(active)
    ledger.claim_prepare(active)
    ledger.resolve_prepare(active, activated=True)
    before = ledger.snapshot()
    if changed_field == "complete_identity":
        stale = _owner_id(999, stream=stream_id)
    else:
        replacement = (
            999
            if changed_field == "stream_generation"
            else f"wrong-{changed_field}"
        )
        stale = dataclasses.replace(active, **{changed_field: replacement})

    for _ in range(10):
        receipt = ledger.request_removal(
            stale, registration_present=False
        )
        assert receipt.outcome is OwnerRemovalOutcome.MISMATCH
        assert ledger.snapshot() == before

    exact = ledger.request_removal(active, registration_present=True)
    assert exact.confirms(active)


def test_owner_queues_callbacks_and_closures_are_collectible_after_prune():
    ledger = OwnerFinalityLedger(_stream_id())
    ring, latch, ingress, callback, handle = _resident_infrastructure()
    references = _settle_collectible_owner(1, ring, ledger)

    _collect_twice()

    assert all(reference() is None for reference in references)
    assert ledger.snapshot().entries == ()
    assert handle.callback is callback
    assert latch.alive
    del handle, callback, ingress, latch, ring


def test_terminal_diagnostics_remain_at_existing_configured_bound(tmp_path: Path):
    pool, *_ = make_pool(
        tmp_path,
        terminal_diagnostic_limit=TERMINAL_DIAGNOSTIC_LIMIT,
    )
    try:
        for index in range(TERMINAL_DIAGNOSTIC_LIMIT + 100):
            run_id = f"terminal-stress-{index}"
            pool.reserve(run_id)
            pool.cancel(run_id, "bounded terminal diagnostic")

        snapshot = pool.snapshot()
        assert snapshot.terminal_diagnostic_count == (
            TERMINAL_DIAGNOSTIC_LIMIT
        )
        assert len(pool._leases) == TERMINAL_DIAGNOSTIC_LIMIT
        retained = tuple(pool._leases.values())
        assert all(record.run_id.startswith("terminal-stress-") for record in retained)
        serialized = repr(retained)
        assert "terminal-stress-0" not in serialized
        assert "bounded terminal diagnostic" in serialized
        assert all(record.owner_capability is None for record in retained)
        assert all(record.publication_token is None for record in retained)
        assert all(record.owner is None for record in retained)
        assert all(record.writer is None for record in retained)
        assert all(record.settlement_claim is None for record in retained)
        assert "capability-" not in serialized
        assert "ack_token=" not in serialized
    finally:
        pool.shutdown()


def test_nonvacuum_retry_handler_and_finality_diagnostics_are_redacted():
    ports = settlement_support.FakePorts()
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(settlement_support.candidate())
    ports.removal_outcome = OwnerRemovalOutcome.MISMATCH
    assert not settlement_support.run_to_pause(engine).settled
    ports.removal_outcome = OwnerRemovalOutcome.REMOVED
    ports.join_complete = False
    assert not settlement_support.run_to_pause(engine).settled
    ports.join_complete = True
    ports.artifact_result = (
        settlement_support.ArtifactSettlementResult(False, False)
    )
    assert not settlement_support.run_to_pause(engine).settled
    assert not settlement_support.run_to_pause(engine).settled
    snapshot = engine.diagnostics()
    encoded = json.dumps(
        dataclasses.asdict(snapshot), default=lambda value: value.value
    )

    assert snapshot.unsettled_leases == 1
    assert snapshot.removal_retries == 1
    assert snapshot.owner_join_retries == 1
    assert snapshot.artifact_disposition_retries == 1
    assert snapshot.publication_retries == 1
    assert "hw-1" in encoded
    assert "owner-nonce" in encoded
    assert "ARTIFACT_SETTLEMENT" in encoded
    for secret in (
        "owner-secret",
        "publication-secret",
        "ack-secret",
        "secret arbitrary payload",
        "traceback",
    ):
        assert secret not in encoded


def test_real_committed_terminal_diagnostics_bound_and_redact_introduced_secrets(
    tmp_path, monkeypatch
):
    pool, resolver, backend, store, clock = make_pool(
        tmp_path, terminal_diagnostic_limit=4
    )
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    ack_tokens = []
    original_ack = stream.acknowledge_finality

    def observed_ack(*args, **kwargs):
        receipt = original_ack(*args, **kwargs)
        if receipt is not None:
            ack_tokens.append(receipt.ack_token)
        return receipt

    monkeypatch.setattr(stream, "acknowledge_finality", observed_ack)
    introduced_capabilities = []
    introduced_artifacts = []
    introduced_runs = []
    original_remove = stream.remove_owner
    remove_mismatch_once = True

    def retrying_remove(instance_id, **kwargs):
        nonlocal remove_mismatch_once
        if remove_mismatch_once:
            remove_mismatch_once = False
            return settlement_support.OwnerRemovalReceipt(
                instance_id, OwnerRemovalOutcome.MISMATCH
            )
        return original_remove(instance_id, **kwargs)

    monkeypatch.setattr(stream, "remove_owner", retrying_remove)
    for index in range(8):
        run_id = f"committed-diagnostic-{index}"
        introduced_runs.append(run_id)
        pool.reserve(run_id)
        pool.commit(
            run_id, token, pool_request(token, run_id, duration=0.5)
        )
        record = pool._leases[run_id]
        owner_ref = weakref.ref(record.owner)
        writer_ref = weakref.ref(record.writer)
        introduced_capabilities.append(record.owner_capability)
        introduced_artifacts.append(record.artifact_id)
        assert record.owner_capability is not None
        assert record.owner_instance_id is not None
        capability = record.owner_capability
        artifact_id = record.artifact_id
        pool.cancel(run_id, "diagnostic retry payload")
        pool.retry_settlements()
        terminal = pool._leases[run_id]
        assert terminal.owner_capability is None
        assert terminal.publication_token is None
        assert terminal.identity is None
        assert terminal.owner_instance_id is None
        assert terminal.non_admission_proof is None
        assert terminal.commit_token is None
        assert pool.lease_snapshot(run_id).settled
        assert pool.release(run_id, capability, artifact_id) == terminal.outcome
        assert pool.cancel(run_id, "repeat terminal") == terminal.outcome
        _collect_twice()
        assert owner_ref() is None
        assert writer_ref() is None

    class PayloadHandler:
        def __init__(self):
            self.publication_tokens = []

        def __call__(self, completion):
            self.publication_tokens.append(
                completion.artifact.publication_token
            )
            raise RuntimeError("arbitrary diagnostic exception payload")

    handler = PayloadHandler()
    handler_ref = weakref.ref(handler)
    pool.set_completion_handler(handler)
    run_id = "committed-handler-diagnostic"
    pool.reserve(run_id)
    pool.commit(run_id, token, pool_request(token, run_id, duration=0.1))
    owner = pool._leases[run_id].owner
    clock.advance(0.05)
    handle.emit(
        frames=10,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert pool.synchronize_stream("mic-a", timeout=1)
    assert owner.join(2)
    pool.retry_settlements()
    publication_tokens = tuple(handler.publication_tokens)
    pool.set_completion_handler(lambda _completion: None)
    del handler
    _collect_twice()

    claimed_tokens = []

    def claiming_handler(completion):
        claimed_tokens.append(completion.artifact.publication_token)
        completion.claim("diagnostic-consumer")

    pool.set_completion_handler(claiming_handler)
    released_id = "committed-released-diagnostic"
    pool.reserve(released_id)
    pool.commit(
        released_id,
        token,
        pool_request(token, released_id, duration=0.1),
    )
    released_owner = pool._leases[released_id].owner
    released_capability = pool._leases[released_id].owner_capability
    released_artifact = pool._leases[released_id].artifact_id
    clock.advance(0.05)
    handle.emit(
        frames=10,
        channels=1,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
    )
    assert pool.synchronize_stream("mic-a", timeout=1)
    assert released_owner.join(2)
    terminal_visibility = pool.retry_settlements()
    assert terminal_visibility.unsettled_transactions == 0
    released = pool._leases[released_id]
    assert released.state is LeaseState.RELEASED
    assert released.owner_capability is None
    assert released.owner_instance_id is None
    assert pool.release(
        released_id, released_capability, released_artifact
    ) == released.outcome
    publication_tokens += tuple(claimed_tokens)

    retained = tuple(pool._leases.values())
    diagnostics = pool._settlement_coordinator.diagnostics()
    encoded = json.dumps(
        {
            "records": [
                repr(record.terminal_summary) for record in retained
            ],
            "diagnostics": dataclasses.asdict(diagnostics),
        },
        default=str,
    )
    assert pool.snapshot().terminal_diagnostic_count == 4
    assert diagnostics.removal_retries >= 1
    assert dict(diagnostics.claims_by_phase)
    assert "committed-handler-diagnostic" in encoded
    assert introduced_runs[0] not in encoded
    assert handler_ref() is None
    for secret in (
        *introduced_capabilities,
        *ack_tokens,
        *publication_tokens,
        "arbitrary diagnostic exception payload",
        "diagnostic retry payload",
        "traceback",
    ):
        assert secret not in encoded
    assert all(
        artifact not in encoded for artifact in introduced_artifacts[:4]
    )
    first_shutdown = pool.shutdown()
    assert first_shutdown.state is PoolState.CLOSED
    assert pool.shutdown() is first_shutdown


def test_retained_object_counts_and_traced_memory_do_not_scale_with_owner_count(
    tmp_path: Path,
):
    pool, resolver, backend, _store, _clock = make_pool(
        tmp_path, terminal_diagnostic_limit=TERMINAL_DIAGNOSTIC_LIMIT
    )
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")

    def settle_range(start, end):
        maximum = 0
        for index in range(start, end):
            _references, active_entries = _settle_real_pool_owner(
                pool, token, stream, 10_000 + index
            )
            maximum = max(maximum, active_entries)
        pool.set_completion_handler(lambda _completion: None)
        _collect_twice()
        assert len(stream._registrations) == 0
        assert stream.finality_snapshot().entries == ()
        assert len(pool._leases) == TERMINAL_DIAGNOSTIC_LIMIT
        assert pool.snapshot().terminal_diagnostic_count == (
            TERMINAL_DIAGNOSTIC_LIMIT
        )
        return maximum

    maximum_entries = settle_range(0, 100)
    tracemalloc.start()
    try:
        _collect_twice()
        warm_bytes = _resident_audio_traced_bytes()
        warm_counts = _live_type_counts()
        maximum_entries = max(maximum_entries, settle_range(100, 350))
        early_bytes = _resident_audio_traced_bytes()
        early_counts = _live_type_counts()
        maximum_entries = max(maximum_entries, settle_range(350, 1_100))
        final_bytes = _resident_audio_traced_bytes()
        final_counts = _live_type_counts()
    finally:
        tracemalloc.stop()

    early_slope = max(0, early_bytes - warm_bytes) / 250
    tail_slope = max(0, final_bytes - early_bytes) / 750
    checkpoints = {
        "warm_bytes": warm_bytes,
        "early_bytes": early_bytes,
        "final_bytes": final_bytes,
        "early_slope": early_slope,
        "tail_slope": tail_slope,
        "warm_counts": warm_counts,
        "early_counts": early_counts,
        "final_counts": final_counts,
    }
    print(f"resident owner memory checkpoints: {checkpoints}")
    assert early_counts == warm_counts, checkpoints
    assert final_counts == warm_counts, checkpoints
    assert 0 < maximum_entries <= stream.finality_snapshot().ledger_limit
    assert stream.finality_snapshot().entries == (), checkpoints
    assert final_bytes <= warm_bytes + 2 * 1024 * 1024, checkpoints
    assert tail_slope <= max(64, early_slope / 4), checkpoints
    assert handle.callback is stream._callback
    pool.shutdown()


SETTLEMENT_INTENTS = tuple(
    settlement_support.SettlementIntent
)
FULL_STAGE_ROWS = (
    "prepare_queued",
    "prepare_claimed",
    "active",
    "removal_lost",
    "removal_mismatch",
    "removal_indeterminate",
    "signal_blocked",
    "join_blocked",
    "ack_lost",
    "ack_mismatch",
    "prune_lost",
    "prune_mismatch",
    "handler_claim",
    "handler_decline",
    "handler_return",
    "handler_raise",
    "delete_pending",
)
REMOVAL_ROWS = (
    OwnerRemovalOutcome.REMOVED,
    OwnerRemovalOutcome.TOMBSTONED_ABSENT,
    OwnerRemovalOutcome.MISMATCH,
    OwnerRemovalOutcome.INDETERMINATE,
    RuntimeError("injected removal failure"),
    TimeoutError("injected removal timeout"),
)


@pytest.mark.parametrize("intent", SETTLEMENT_INTENTS)
@pytest.mark.parametrize("stage", FULL_STAGE_ROWS)
def test_full_six_intent_stage_matrix_recovers_with_one_terminal_cas(
    intent,
    stage,
):
    ports = settlement_support.FakePorts()
    if stage in ("prepare_queued", "prepare_claimed"):
        ports.injected["late-command resolution"] = [False]
    elif stage == "removal_lost":
        ports.injected["remove"] = [TimeoutError("lost removal response")]
    elif stage == "removal_mismatch":
        ports.injected["remove"] = [
            settlement_support.OwnerRemovalReceipt(
                settlement_support.owner_id(),
                OwnerRemovalOutcome.MISMATCH,
            )
        ]
    elif stage == "removal_indeterminate":
        ports.injected["remove"] = [
            settlement_support.OwnerRemovalReceipt(
                settlement_support.owner_id(),
                OwnerRemovalOutcome.INDETERMINATE,
            )
        ]
    elif stage == "signal_blocked":
        ports.injected["signal"] = [False]
    elif stage == "join_blocked":
        ports.injected["join"] = [False]
    elif stage == "ack_lost":
        ports.injected["acknowledge"] = [TimeoutError("lost ack")]
    elif stage == "ack_mismatch":
        ports.ack_matches = False
    elif stage == "prune_lost":
        ports.injected["prune"] = [TimeoutError("lost prune")]
    elif stage == "prune_mismatch":
        ports.prune_outcome = FinalityPruneOutcome.MISMATCH
    elif stage == "delete_pending":
        ports.artifact_result = (
            settlement_support.ArtifactSettlementResult(False, False)
        )
    elif stage.startswith("handler_"):
        resolution = {
            "handler_claim": "CLAIMED",
            "handler_decline": "DECLINED",
            "handler_return": None,
            "handler_raise": None,
        }[stage]
        ports.injected["handler invocation"] = [
            settlement_support.CompletionInvocationResult(
                handler_returned=stage != "handler_raise",
                resolution=resolution,
                exception_type=(
                    "RuntimeError" if stage == "handler_raise" else None
                ),
            )
        ]
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(settlement_support.candidate(intent=intent))

    first = settlement_support.run_to_pause(engine)
    blocking = stage not in (
        "active",
        "handler_claim",
        "handler_decline",
        "handler_return",
        "handler_raise",
    )
    if blocking:
        assert not first.settled
        assert ports.capacity == 1
        assert ports.terminal_cas_count == 0
        before = dict(ports.calls)
        ports.ack_matches = True
        ports.prune_outcome = FinalityPruneOutcome.PRUNED
        ports.artifact_result = (
            settlement_support.ArtifactSettlementResult(True, True)
        )
        recovered = settlement_support.run_to_pause(engine)
        assert recovered.settled
        ordered = (
            "remove",
            "signal",
            "join",
            "freeze/readiness",
            "late-command resolution",
            "acknowledge",
            "prune",
            "artifact/publication",
        )
        blocked_operation = {
            "removal_lost": "remove",
            "removal_mismatch": "remove",
            "removal_indeterminate": "remove",
            "prepare_queued": "late-command resolution",
            "prepare_claimed": "late-command resolution",
            "signal_blocked": "signal",
            "join_blocked": "join",
            "ack_lost": "acknowledge",
            "ack_mismatch": "acknowledge",
            "prune_lost": "prune",
            "prune_mismatch": "prune",
            "delete_pending": "artifact/publication",
        }[stage]
        for operation in ordered[: ordered.index(blocked_operation)]:
            assert ports.calls[operation] == before[operation]
    else:
        assert first.settled

    assert ports.terminal_cas_count == 1
    assert ports.capacity == 0
    assert ports.calls["remove"] in (1, 2)
    assert ports.calls["signal"] in (1, 2)
    assert ports.calls["join"] in (1, 2)
    if intent is settlement_support.SettlementIntent.OWNER_SUCCESS:
        assert ports.calls.get("handler invocation", 0) == (
            1 if stage.startswith("handler_") else 1
        )
    else:
        assert ports.calls.get("handler invocation", 0) == 0


@pytest.mark.parametrize("intent", SETTLEMENT_INTENTS)
@pytest.mark.parametrize("removal_row", REMOVAL_ROWS)
def test_six_intents_by_six_removal_results_never_cross_a_nonconfirming_gate(
    intent,
    removal_row,
):
    ports = settlement_support.FakePorts()
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(
        settlement_support.candidate(intent=intent)
    )
    positive = removal_row in (
        OwnerRemovalOutcome.REMOVED,
        OwnerRemovalOutcome.TOMBSTONED_ABSENT,
    )
    if isinstance(removal_row, BaseException):
        ports.injected["remove"] = [
            type(removal_row)(str(removal_row)) for _ in range(32)
        ]
    else:
        ports.removal_outcome = removal_row

    barrier = threading.Barrier(5)
    results = []

    def retry() -> None:
        barrier.wait()
        for _ in range(4):
            results.append(engine.advance("lease-1"))

    workers = [threading.Thread(target=retry) for _ in range(4)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    if positive:
        if not ports.settled:
            assert settlement_support.run_to_pause(engine).settled
        assert ports.terminal_cas_count == 1
        assert ports.capacity == 0
        assert ports.calls["signal"] == 1
        assert ports.calls["join"] == 1
    else:
        assert not ports.settled
        assert ports.capacity == 1
        assert ports.lease_state in ("COMMITTED", "COMMITTING")
        for forbidden in (
            "signal",
            "join",
            "acknowledge",
            "prune",
            "artifact/publication",
            "quota confirmation",
            "terminal CAS",
        ):
            assert ports.calls.get(forbidden, 0) == 0


def test_concurrent_six_intent_race_has_one_claim_and_one_external_pipeline():
    ports = settlement_support.FakePorts()
    engine = settlement_support.coordinator(ports)
    barrier = threading.Barrier(len(SETTLEMENT_INTENTS) + 1)
    outcomes = []

    def claim(intent) -> None:
        barrier.wait()
        outcomes.append(
            engine.claim_or_resume(
                settlement_support.candidate(intent=intent)
            )
        )

    workers = [
        threading.Thread(target=claim, args=(intent,))
        for intent in SETTLEMENT_INTENTS
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert sum(result.claimed for result in outcomes) == 1
    assert len({result.intent for result in outcomes}) == 1
    assert settlement_support.run_to_pause(engine).settled
    assert ports.calls["remove"] == 1
    assert ports.calls["signal"] == 1
    assert ports.calls["join"] == 1
    assert ports.calls["artifact/publication"] == 1
    assert ports.terminal_cas_count == 1


@pytest.mark.parametrize("intent", SETTLEMENT_INTENTS)
@pytest.mark.parametrize(
    "blocked_stage",
    [
        "signal",
        "join",
        "artifact disposition",
        "publication decline",
    ],
)
def test_every_intent_including_prepare_rollback_resumes_after_blocker_once(
    intent,
    blocked_stage,
):
    ports = settlement_support.FakePorts()
    operation = (
        "artifact/publication"
        if blocked_stage in ("artifact disposition", "publication decline")
        else blocked_stage
    )
    if intent is settlement_support.SettlementIntent.PREPARE_ROLLBACK:
        ports.lease_state = "COMMITTING"
    if operation in ("signal", "join"):
        ports.injected[operation] = [False]
    else:
        ports.artifact_result = (
            settlement_support.ArtifactSettlementResult(
                blocked_stage == "artifact disposition",
                blocked_stage == "publication decline",
            )
        )
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(
        settlement_support.candidate(intent=intent)
    )

    paused = settlement_support.run_to_pause(engine)
    before = dict(ports.calls)

    assert not paused.settled
    assert ports.capacity == 1
    assert ports.terminal_cas_count == 0
    assert ports.calls.get("quota confirmation", 0) == 0
    blocked_progress = ports.claims["lease-1"].progress
    assert blocked_progress.publication_resolved is (
        blocked_stage == "artifact disposition"
        if operation == "artifact/publication"
        else False
    )
    assert blocked_progress.artifact_disposition_confirmed is (
        blocked_stage == "publication decline"
        if operation == "artifact/publication"
        else False
    )
    assert ports.lease_state == (
        "COMMITTING"
        if intent is settlement_support.SettlementIntent.PREPARE_ROLLBACK
        else "COMMITTED"
    )
    ports.artifact_result = (
        settlement_support.ArtifactSettlementResult(True, True)
    )
    recovered = settlement_support.run_to_pause(engine)
    assert recovered.settled
    assert ports.terminal_cas_count == 1
    if operation == "signal":
        earlier_operations = ("remove",)
    elif operation == "join":
        earlier_operations = ("remove", "signal")
    else:
        earlier_operations = (
            "remove",
            "signal",
            "join",
            "freeze/readiness",
            "late-command resolution",
            "acknowledge",
            "prune",
        )
    for earlier in earlier_operations:
        assert ports.calls[earlier] == before[earlier]


@pytest.mark.parametrize(
    "initial_outcome",
    [
        OwnerRemovalOutcome.MISMATCH,
        OwnerRemovalOutcome.INDETERMINATE,
    ],
)
def test_nonconfirming_removal_then_exact_response_resumes_without_early_work(
    initial_outcome,
):
    ports = settlement_support.FakePorts()
    ports.removal_outcome = initial_outcome
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(settlement_support.candidate())

    paused = settlement_support.run_to_pause(engine)
    assert not paused.settled
    for forbidden in (
        "signal",
        "join",
        "acknowledge",
        "prune",
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
    ):
        assert ports.calls.get(forbidden, 0) == 0
    ports.removal_outcome = OwnerRemovalOutcome.REMOVED
    recovered = settlement_support.run_to_pause(engine)

    assert recovered.settled
    assert ports.calls["remove"] == 2
    assert ports.calls["signal"] == 1
    assert ports.calls["join"] == 1
    assert ports.terminal_cas_count == 1


@pytest.mark.parametrize("stage", ["acknowledge", "prune"])
def test_concurrent_ack_and_prune_mismatch_hold_all_later_actions_until_exact(
    stage,
):
    ports = settlement_support.FakePorts()
    if stage == "acknowledge":
        ports.ack_matches = False
    else:
        ports.prune_outcome = FinalityPruneOutcome.MISMATCH
    engine = settlement_support.coordinator(ports)
    engine.claim_or_resume(settlement_support.candidate())
    assert not settlement_support.run_to_pause(engine).settled
    prefix = dict(ports.calls)
    barrier = threading.Barrier(9)

    def retry() -> None:
        barrier.wait()
        engine.advance("lease-1")

    workers = [threading.Thread(target=retry) for _ in range(8)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert ports.capacity == 1
    assert ports.terminal_cas_count == 0
    assert ports.calls["remove"] == prefix["remove"]
    assert ports.calls["signal"] == prefix["signal"]
    assert ports.calls["join"] == prefix["join"]
    if stage == "acknowledge":
        assert ports.calls.get("prune", 0) == 0
        ports.ack_matches = True
    else:
        assert ports.calls["acknowledge"] == prefix["acknowledge"]
        ports.prune_outcome = FinalityPruneOutcome.ALREADY_PRUNED
    for forbidden in (
        "artifact/publication",
        "quota confirmation",
        "terminal CAS",
    ):
        assert ports.calls.get(forbidden, 0) == 0
    assert settlement_support.run_to_pause(engine).settled
    assert ports.terminal_cas_count == 1


def test_lost_ack_and_prune_responses_are_recovered_from_value_only_state():
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id)
    instance_id = _owner_id(1, stream=stream_id)
    admission = ledger.reserve_prepare(instance_id)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(instance_id)
    ledger.claim_prepare(instance_id)
    ledger.resolve_prepare(instance_id, activated=True)
    removal = ledger.request_removal(
        instance_id, registration_present=True
    )
    lost_removal_response = removal
    retried_removal = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert retried_removal == lost_removal_response
    durable = FinalityDurableProgress(
        removal_receipt=removal,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )

    lost_ack_response = ledger._acknowledge_entry(
        instance_id, removal, durable
    )
    retried_ack = ledger._acknowledge_entry(
        instance_id, removal, durable
    )
    assert retried_ack == lost_ack_response
    lost_prune_response = ledger._prune_entry(
        instance_id, retried_ack
    )
    retried_prune = ledger._prune_entry(instance_id, retried_ack)

    assert lost_prune_response.outcome is FinalityPruneOutcome.PRUNED
    assert retried_prune.outcome is FinalityPruneOutcome.ALREADY_PRUNED
    assert ledger.snapshot().entries == ()


def test_exact_non_admission_stress_row_skips_only_finality_operations():
    instance_id = settlement_support.owner_id(nonce="stress-backpressure")
    proof = settlement_support.OwnerNonAdmissionProof(
        instance_id,
        settlement_support.OwnerNonAdmissionReason.LEDGER_FULL,
        command_enqueued=False,
        command_claimed=False,
    )
    ports = settlement_support.FakePorts()
    engine = settlement_support.coordinator(ports)
    claim = settlement_support.candidate(
        proof=proof, owner_instance_id=instance_id
    )
    engine.claim_or_resume(claim)

    result = settlement_support.run_to_pause(engine)

    assert result.settled
    assert ports.calls.get("remove", 0) == 0
    assert ports.calls.get("late-command resolution", 0) == 0
    assert ports.calls.get("acknowledge", 0) == 0
    assert ports.calls.get("prune", 0) == 0
    assert ports.calls["signal"] == 1
    assert ports.calls["join"] == 1
    assert ports.calls["artifact/publication"] == 1
    assert ports.terminal_cas_count == 1


@pytest.mark.parametrize("late_stage", ["queued", "claimed"])
def test_queued_and_claimed_late_prepare_tombstones_resolve_without_escape(
    late_stage,
):
    stream_id = _stream_id()
    ledger = OwnerFinalityLedger(stream_id)
    instance_id = _owner_id(77, stream=stream_id)
    admission = ledger.reserve_prepare(instance_id)
    assert not isinstance(admission, OwnerNonAdmissionProof)
    ledger.mark_prepare_enqueued(instance_id)
    if late_stage == "claimed":
        ledger.claim_prepare(instance_id)

    first = ledger.request_removal(
        instance_id, registration_present=False
    )
    assert first.outcome is OwnerRemovalOutcome.INDETERMINATE
    assert ledger.snapshot().pending_late_commands == 1
    if late_stage == "queued":
        exact = ledger.resolve_queued_prepare_cancellation(
            instance_id, cancelled=True
        )
    else:
        ledger.resolve_prepare(instance_id, activated=False)
        exact = ledger.request_removal(
            instance_id, registration_present=False
        )

    assert exact.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    durable = FinalityDurableProgress(
        removal_receipt=exact,
        owner_signaled=True,
        owner_joined=True,
        late_owner_commands_resolved=True,
        remove_retry_disabled=True,
    )
    acknowledgement = ledger._acknowledge_entry(
        instance_id, exact, durable
    )
    assert acknowledgement is not None
    assert (
        ledger._prune_entry(instance_id, acknowledgement).outcome
        is FinalityPruneOutcome.PRUNED
    )
    assert ledger.snapshot().entries == ()


def test_real_stream_late_claimed_prepare_cannot_outlive_tombstone(
    monkeypatch,
):
    stream, _backend, _clock = stream_support.make_ready_stream()
    owner = stream_support.StubOwner(
        "late-stress", "late-attempt", "late-capability"
    )
    instance_id = stream_support.owner_instance(
        owner, nonce="late-stress-nonce"
    )
    prepare_claimed = threading.Event()
    release_prepare = threading.Event()
    original_prepare = stream._prepare

    def blocked_prepare(candidate, candidate_id):
        prepare_claimed.set()
        assert release_prepare.wait(5)
        return original_prepare(candidate, candidate_id)

    monkeypatch.setattr(stream, "_prepare", blocked_prepare)
    result = []

    def prepare() -> None:
        result.append(
            stream.prepare_owner(owner, instance_id, timeout=None)
        )

    worker = threading.Thread(target=prepare)
    worker.start()
    assert prepare_claimed.wait(2)
    first = stream.remove_owner(instance_id, timeout=0.01)
    assert first.outcome is OwnerRemovalOutcome.INDETERMINATE
    release_prepare.set()
    worker.join(timeout=5)
    stream_support.wait_stream(stream)
    exact = stream.remove_owner(instance_id, timeout=1)

    assert not worker.is_alive()
    assert result == [False]
    assert exact.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
    assert stream.snapshot().owner_count == 0
    stream.close()


@pytest.mark.parametrize(
    ("handler_row", "expected_state"),
    [
        ("claim", "RELEASED"),
        ("decline", "RELEASED"),
        ("return", "FAILED"),
        ("raise", "FAILED"),
    ],
)
def test_handler_race_rows_are_once_only_and_use_the_exact_token(
    handler_row,
    expected_state,
):
    calls = []

    def handler(completion):
        calls.append(completion)
        if handler_row == "claim":
            completion.claim("stress-consumer")
        elif handler_row == "decline":
            completion.decline("stress decline")
        elif handler_row == "raise":
            raise RuntimeError("unretained stress payload")

    engine, ports, store = settlement_support._completion_harness(handler)
    result = settlement_support.run_to_pause(engine)
    for _ in range(10):
        engine.advance("lease-1")

    assert result.settled
    assert ports.lease_state == expected_state
    assert len(calls) == 1
    assert ports.terminal_cas_count == 1
    tokens = store.claim_tokens + store.decline_tokens
    assert tokens == [store.publication_token]
    encoded = repr(ports.claims["lease-1"].terminal_summary)
    assert "unretained stress payload" not in encoded


def test_artifact_delete_pending_retry_keeps_capacity_until_success():
    calls = []
    engine, ports, store = settlement_support._completion_harness(
        calls.append,
        delete_pending=True,
    )

    paused = settlement_support.run_to_pause(engine)
    assert not paused.settled
    assert ports.capacity == 1
    assert ports.terminal_cas_count == 0
    store.delete_pending = False
    recovered = settlement_support.run_to_pause(engine)

    assert recovered.settled
    assert ports.capacity == 0
    assert ports.terminal_cas_count == 1
    assert len(calls) == 1


def test_post_shutdown_management_retry_race_has_zero_business_entry_calls(
    tmp_path: Path,
    monkeypatch,
):
    pool, resolver, backend, _store, _clock = make_pool(tmp_path)
    _handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    original_remove = stream.remove_owner
    exact_retry = False
    retry_entered = threading.Event()
    retry_release = threading.Event()
    retry_remove_calls = 0

    def controlled_remove(*args, **kwargs):
        nonlocal retry_remove_calls
        if not exact_retry:
            instance_id = args[0]
            return settlement_support.OwnerRemovalReceipt(
                instance_id, OwnerRemovalOutcome.MISMATCH
            )
        retry_remove_calls += 1
        retry_entered.set()
        assert retry_release.wait(5)
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(stream, "remove_owner", controlled_remove)
    pool.reserve("management-race")
    committed = pool.commit(
        "management-race",
        token,
        pool_request(
            token,
            "management-race",
            duration=0.5,
        ),
    )
    assert committed.state is LeaseState.COMMITTED
    pool.cancel("management-race", "force unresolved settlement")
    first_shutdown = pool.shutdown()
    assert first_shutdown.state is PoolState.FAILED_CLOSED
    adapter = pool._settlement_adapter
    external_calls = {
        "signal": 0,
        "join": 0,
        "freeze_readiness": 0,
        "late_commands": 0,
        "acknowledge": 0,
        "prune": 0,
        "artifact_publication": 0,
        "quota_confirmation": 0,
        "quota_release": 0,
        "terminal_cas": 0,
    }

    def observed_external(name, original):
        def observed(*args, **kwargs):
            before_quota = _store.reserved_bytes
            external_calls[name] += 1
            result = original(*args, **kwargs)
            if (
                name == "artifact_publication"
                and _store.reserved_bytes < before_quota
            ):
                external_calls["quota_release"] += 1
            return result

        return observed

    monkeypatch.setattr(
        adapter,
        "signal_owner",
        observed_external("signal", adapter.signal_owner),
    )
    monkeypatch.setattr(
        adapter,
        "join_owner",
        observed_external("join", adapter.join_owner),
    )
    monkeypatch.setattr(
        adapter,
        "check_artifact_readiness",
        observed_external(
            "freeze_readiness", adapter.check_artifact_readiness
        ),
    )
    monkeypatch.setattr(
        adapter,
        "late_owner_commands_resolved",
        observed_external(
            "late_commands", adapter.late_owner_commands_resolved
        ),
    )
    monkeypatch.setattr(
        adapter,
        "acknowledge_finality",
        observed_external("acknowledge", adapter.acknowledge_finality),
    )
    monkeypatch.setattr(
        adapter,
        "prune_finality",
        observed_external("prune", adapter.prune_finality),
    )
    monkeypatch.setattr(
        adapter,
        "settle_artifact_and_publication",
        observed_external(
            "artifact_publication",
            adapter.settle_artifact_and_publication,
        ),
    )
    monkeypatch.setattr(
        adapter,
        "terminal_cas",
        observed_external("terminal_cas", adapter.terminal_cas),
    )
    monkeypatch.setattr(
        adapter,
        "confirm_quota_outcome",
        observed_external(
            "quota_confirmation", adapter.confirm_quota_outcome
        ),
    )

    business_calls = {
        "reserve": 0,
        "commit": 0,
        "reconcile": 0,
        "open": 0,
        "prepare": 0,
    }

    def forbidden_business(name: str, original: Callable):
        def observed(*args, **kwargs):
            business_calls[name] += 1
            return original(*args, **kwargs)

        return observed

    monkeypatch.setattr(
        pool, "reserve", forbidden_business("reserve", pool.reserve)
    )
    monkeypatch.setattr(
        pool, "commit", forbidden_business("commit", pool.commit)
    )
    monkeypatch.setattr(
        pool, "reconcile", forbidden_business("reconcile", pool.reconcile)
    )
    monkeypatch.setattr(
        backend,
        "open_input",
        forbidden_business("open", backend.open_input),
    )
    monkeypatch.setattr(
        stream,
        "prepare_owner",
        forbidden_business("prepare", stream.prepare_owner),
    )
    exact_retry = True
    reports = []
    callers_entered = threading.Barrier(3)

    def management_retry():
        callers_entered.wait()
        reports.append(pool.shutdown())

    first = threading.Thread(target=management_retry)
    second = threading.Thread(target=management_retry)
    first.start()
    second.start()
    callers_entered.wait()
    assert retry_entered.wait(2)
    retry_release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(reports) == 2
    assert reports[0] is reports[1]
    assert reports[0].state is PoolState.CLOSED
    assert business_calls == {key: 0 for key in business_calls}
    assert retry_remove_calls == 1
    assert external_calls == {
        "signal": 1,
        "join": 1,
        "freeze_readiness": 1,
        "late_commands": 1,
        "acknowledge": 1,
        "prune": 1,
        "artifact_publication": 1,
        "quota_confirmation": 1,
        "quota_release": 1,
        "terminal_cas": 1,
    }
    assert pool.snapshot().state is PoolState.CLOSED


@pytest.mark.parametrize(
    "changed_field",
    [
        "pool_epoch",
        "hardware_id",
        "stream_generation",
        "run_id",
        "commit_attempt_id",
        "owner_capability",
        "instance_nonce",
        "complete_identity",
    ],
)
def test_real_pool_lease_repeated_stale_identity_isolation(
    changed_field, tmp_path
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    run_id = "replacement"
    try:
        pool.reserve(run_id)
        assert pool.commit(
            run_id, token, pool_request(token, run_id, duration=0.5)
        ).state is LeaseState.COMMITTED
        record = pool._leases[run_id]
        replacement_id = record.owner_instance_id
        registration = stream._registrations[replacement_id]
        before_stream = stream.snapshot()
        before_finality = stream.finality_snapshot()
        before_pool = pool.snapshot()
        before_lease = pool.lease_snapshot(run_id)
        before_progress = record.settlement_claim
        before_ring = stream._ring
        before_callback = stream._callback
        before_artifact_id = record.artifact_id
        before_quota = store.reserved_bytes
        if changed_field == "complete_identity":
            candidate_id = dataclasses.replace(
                replacement_id,
                run_id="entirely-stale",
                instance_nonce="entirely-stale",
            )
        else:
            value = (
                999
                if changed_field == "stream_generation"
                else f"wrong-{changed_field}"
            )
            candidate_id = dataclasses.replace(
                replacement_id, **{changed_field: value}
            )

        for _ in range(10):
            receipt = stream.remove_owner(candidate_id, timeout=1)
            assert receipt.outcome is OwnerRemovalOutcome.MISMATCH
            assert stream._registrations[replacement_id] is registration
            assert stream.snapshot() == before_stream
            assert stream.finality_snapshot() == before_finality
            assert pool.snapshot() == before_pool
            assert pool.lease_snapshot(run_id) == before_lease
            assert pool._leases[run_id].settlement_claim is before_progress
            assert stream._ring is before_ring
            assert stream._callback is before_callback
            assert pool._leases[run_id].artifact_id == before_artifact_id
            assert store.reserved_bytes == before_quota

        clock.advance(0.05)
        handle.emit(
            frames=5,
            channels=1,
            callback_monotonic=clock(),
            adc_time=clock() - 0.01,
            current_time=clock(),
        )
        assert pool.synchronize_stream("mic-a", timeout=1)
        assert pool.lease_snapshot(run_id).state is LeaseState.COMMITTED
    finally:
        pool.cancel(run_id, "identity isolation cleanup")
        pool.shutdown()


def test_ten_thousand_callbacks_exclude_all_forbidden_settlement_work(
    monkeypatch, tmp_path,
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    callback = stream._callback
    assert handle.callback is callback
    assert pool._streams["mic-a"].stream is stream
    callback_thread = threading.get_ident()
    inside_callback = {"value": False}
    guard_counts = {
        name: 0
        for name in (
            "hmac", "filesystem", "wait_join", "finality",
            "settlement", "artifact", "pool_lock",
        )
    }
    fanout_counts: dict[int, int] = {}

    def guarded(category, real):
        def boundary(*args, **kwargs):
            guard_counts[category] += 1
            if (
                threading.get_ident() == callback_thread
                and inside_callback["value"]
            ):
                raise AssertionError(
                    f"forbidden callback operation: {category}"
                )
            return real(*args, **kwargs)
        return boundary

    real_pool_lock = pool._lock
    guarded_acquire = guarded("pool_lock", real_pool_lock.acquire)

    class GuardedPoolRLock:
        def acquire(self, *args, **kwargs):
            return guarded_acquire(*args, **kwargs)

        def release(self):
            return real_pool_lock.release()

        def _is_owned(self):
            return real_pool_lock._is_owned()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

        def __getattr__(self, name):
            return getattr(real_pool_lock, name)

    def fanout_guard(real):
        def observed(owner, *args, **kwargs):
            fanout_counts[id(owner)] = fanout_counts.get(id(owner), 0) + 1
            return real(owner, *args, **kwargs)
        return observed

    start = threading.Barrier(2)
    owner_ready = [threading.Barrier(2) for _ in range(100)]
    callback_delivered = [threading.Barrier(2) for _ in range(100)]
    worker_errors = []
    scheduled_owners = []
    starting_sequence = callback.next_sequence
    starting_cursor = stream.snapshot().cursor
    persistent_id = "callback-persistent-owner"

    with monkeypatch.context() as patch:
        patch.setattr(pool, "_lock", GuardedPoolRLock())
        patch.setattr(hmac, "new", guarded("hmac", hmac.new))
        patch.setattr(
            builtins, "open", guarded("filesystem", builtins.open)
        )
        patch.setattr(
            Path, "unlink", guarded("filesystem", Path.unlink)
        )
        patch.setattr(
            threading.Event,
            "wait",
            guarded("wait_join", threading.Event.wait),
        )
        patch.setattr(
            threading.Thread,
            "join",
            guarded("wait_join", threading.Thread.join),
        )
        for name in (
            "prepare_owner", "activate_owner", "remove_owner",
            "acknowledge_finality", "prune_finality",
        ):
            patch.setattr(
                stream, name, guarded("finality", getattr(stream, name))
            )
        coordinator = pool._settlement_coordinator
        for name in ("claim_or_resume", "advance"):
            patch.setattr(
                coordinator,
                name,
                guarded("settlement", getattr(coordinator, name)),
            )
        adapter = pool._settlement_adapter
        for name in (
            "signal_owner", "join_owner", "check_artifact_readiness",
            "late_owner_commands_resolved", "acknowledge_finality",
            "prune_finality", "settle_artifact_and_publication",
            "confirm_quota_outcome", "terminal_cas",
        ):
            patch.setattr(
                adapter,
                name,
                guarded("settlement", getattr(adapter, name)),
            )
        for name in (
            "allocate_writer", "state", "publication_pending",
            "discard_partial", "retry_delete_pending_artifact",
        ):
            patch.setattr(
                store, name, guarded("artifact", getattr(store, name))
            )
        patch.setattr(
            CaptureOwner,
            "advance_pending_mapping",
            fanout_guard(CaptureOwner.advance_pending_mapping),
        )
        patch.setattr(
            CaptureOwner,
            "offer_live",
            fanout_guard(CaptureOwner.offer_live),
        )
        patch.setattr(
            CaptureOwner,
            "notify_ring_extended",
            fanout_guard(CaptureOwner.notify_ring_extended),
        )

        pool.reserve(persistent_id)
        assert pool.commit(
            persistent_id,
            token,
            pool_request(token, persistent_id, duration=600.0),
        ).state is LeaseState.COMMITTED
        persistent_owner = pool._leases[persistent_id].owner
        persistent_writer = pool._leases[persistent_id].writer
        write_progress = threading.Condition()
        written_blocks = {"count": 0}
        real_write = persistent_writer.write

        def observed_write(*args, **kwargs):
            result = real_write(*args, **kwargs)
            with write_progress:
                written_blocks["count"] += 1
                write_progress.notify_all()
            return result

        patch.setattr(persistent_writer, "write", observed_write)

        def control_work():
            try:
                start.wait()
                for index in range(100):
                    run_id = f"callback-scheduled-{index}"
                    current_token = pool.ready_token("mic-a")
                    assert current_token is not None
                    pool.reserve(run_id)
                    assert pool.commit(
                        run_id,
                        current_token,
                        pool_request(
                            current_token,
                            run_id,
                            duration=600.0,
                            trigger=clock() - 0.01,
                        ),
                    ).state is LeaseState.COMMITTED
                    owner = pool._leases[run_id].owner
                    scheduled_owners.append(owner)
                    owner_ready[index].wait(2)
                    callback_delivered[index].wait(2)
                    assert fanout_counts.get(id(owner), 0) > 0
                    assert pool.cancel(
                        run_id, "callback scheduled settlement"
                    ).state is LeaseState.CANCELED
            except BaseException as error:
                worker_errors.append(error)

        control = threading.Thread(target=control_work)
        control.start()
        start.wait()
        block_frames = production_block_frames(token.sample_rate)
        source = np.ones((block_frames, 1), dtype=np.float32)
        live_object_baseline = None
        for index in range(10_000):
            if index < 100:
                try:
                    owner_ready[index].wait(5)
                except threading.BrokenBarrierError:
                    assert worker_errors == []
                    raise
            clock.advance(block_frames / token.sample_rate)
            inside_callback["value"] = True
            try:
                callback(
                    source,
                    block_frames,
                    {
                        "inputBufferAdcTime": clock() - 0.01,
                        "currentTime": clock(),
                    },
                    "",
                )
            finally:
                inside_callback["value"] = False
            assert pool.synchronize_stream("mic-a", timeout=1)
            with write_progress:
                assert write_progress.wait_for(
                    lambda: written_blocks["count"] >= index + 1,
                    timeout=1,
                )
            if index < 100:
                callback_delivered[index].wait(2)
            if index == 999:
                gc.collect()
                tracked = gc.get_objects()
                live_object_baseline = (
                    sum(
                        isinstance(item, RawCallbackBlock)
                        for item in tracked
                    ),
                    sum(
                        isinstance(item, CapturedAudioBlock)
                        for item in tracked
                    ),
                )
                del tracked
        control.join(10)
        assert not control.is_alive()
        assert worker_errors == []
        assert fanout_counts.get(id(persistent_owner), 0) > 0
        assert len(scheduled_owners) == 100
        assert all(
            fanout_counts.get(id(owner), 0) > 0
            for owner in scheduled_owners
        )
        gc.collect()
        tracked = gc.get_objects()
        live_objects_after = (
            sum(isinstance(item, RawCallbackBlock) for item in tracked),
            sum(isinstance(item, CapturedAudioBlock) for item in tracked),
        )
        stress_snapshot = stream.snapshot()
        assert pool.cancel(
            persistent_id, "callback persistent settlement"
        ).state is LeaseState.CANCELED
        report = pool.shutdown()

    assert callback.next_sequence - starting_sequence == 10_000
    assert stress_snapshot.cursor - starting_cursor == 10_000 * block_frames
    assert stress_snapshot.ingress_depth <= 4
    assert stream._ring.retained_frames >= int(2 * token.sample_rate)
    assert stream._ring.retained_frames < (
        int(2 * token.sample_rate) + block_frames
    )
    assert stream._ring.block_count <= (
        int(2 * token.sample_rate) // block_frames + 1
    )
    assert live_object_baseline is not None
    assert live_objects_after[0] <= live_object_baseline[0] + 4
    assert live_objects_after[1] <= live_object_baseline[1] + 4
    assert stream.generation_latch.failure_code is None
    assert stream._ingress.size <= stream._ingress.capacity
    assert all(value > 0 for value in guard_counts.values())
    assert report.state is PoolState.CLOSED
    assert pool.snapshot().active_lease_count == 0


@pytest.mark.parametrize("intent", SETTLEMENT_INTENTS)
def test_two_owner_shared_stream_non_regression_for_every_terminal_intent(
    intent,
):
    stream, backend, clock = stream_support.make_ready_stream()
    settling = stream_support.StubOwner(
        "settling", "attempt-settling", "cap-settling", commit_open=True
    )
    continuing = stream_support.StubOwner(
        "continuing",
        "attempt-continuing",
        "cap-continuing",
        commit_open=True,
    )
    settling_id = stream_support.owner_instance(
        settling, nonce="settling-nonce"
    )
    continuing_id = stream_support.owner_instance(
        continuing, nonce="continuing-nonce"
    )
    settling.artifact = object()
    continuing.artifact = object()
    try:
        for owner, instance_id in (
            (settling, settling_id),
            (continuing, continuing_id),
        ):
            assert stream.prepare_owner(owner, instance_id, timeout=1)
            assert stream.activate_owner(
                owner,
                instance_id,
                commit_monotonic=clock(),
                timeout=1,
            )
        continuation_registration = stream._registrations[continuing_id]
        continuation_artifact = continuing.artifact
        continuation_calls = list(continuing.calls)

        removed = stream.remove_owner(settling_id, timeout=1)
        assert removed.confirms(settling_id)
        ports = settlement_support.FakePorts()
        ports.removal_outcome = removed.outcome
        engine = settlement_support.coordinator(ports)
        engine.claim_or_resume(
            settlement_support.candidate(intent=intent)
        )
        assert settlement_support.run_to_pause(engine).settled

        assert stream._registrations[continuing_id] is continuation_registration
        assert continuing.artifact is continuation_artifact
        assert stream.snapshot().owner_count == 1
        assert ports.capacity == 0
        clock.advance(0.05)
        stream_support.valid_block(backend.handle, clock)
        stream_support.wait_stream(stream)
        assert len(continuing.calls) > len(continuation_calls)
        assert continuing_id in stream._registrations
    finally:
        stream.remove_owner(continuing_id, timeout=1)
        stream.close()


@pytest.mark.parametrize("intent", SETTLEMENT_INTENTS)
def test_two_real_pool_leases_share_stream_and_settle_independently(
    intent, tmp_path
):
    pool, resolver, backend, store, clock = make_pool(tmp_path)
    handle, token = make_ready(pool, resolver, backend)
    stream = pool.stream_for_testing("mic-a")
    settling_id = f"real-settling-{intent.value}"
    continuing_id = f"real-continuing-{intent.value}"
    settling_duration = (
        0.1
        if intent is settlement_support.SettlementIntent.OWNER_SUCCESS
        else 0.5
    )
    try:
        for run_id, duration in (
            (settling_id, settling_duration),
            (continuing_id, 0.5),
        ):
            pool.reserve(run_id)
            assert pool.commit(
                run_id,
                token,
                pool_request(token, run_id, duration=duration),
            ).state is LeaseState.COMMITTED
        settling = pool._leases[settling_id]
        continuing = pool._leases[continuing_id]
        settling_owner = settling.owner
        continuing_registration = stream._registrations[
            continuing.owner_instance_id
        ]
        continuing_artifact = continuing.artifact_id
        before_ring = stream._ring
        before_callback = stream._callback
        before_capacity = pool.snapshot().active_lease_count
        before_quota = store.reserved_bytes
        assert before_capacity == 2

        if intent is settlement_support.SettlementIntent.OWNER_SUCCESS:
            clock.advance(0.05)
            handle.emit(
                frames=10,
                channels=1,
                callback_monotonic=clock(),
                adc_time=clock() - 0.01,
                current_time=clock(),
            )
            assert pool.synchronize_stream("mic-a", timeout=1)
            assert settling_owner.join(2)
            pool.retry_settlements()
        elif intent is settlement_support.SettlementIntent.OWNER_FAILURE:
            assert settling_owner.fail(
                CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED,
                "two-owner owner failure",
            )
            assert settling_owner.join(2)
            for _ in range(32):
                pool.retry_settlements()
                if pool.lease_snapshot(settling_id).settled:
                    break
        else:
            terminal_state = (
                LeaseState.CANCELED
                if intent
                in (
                    settlement_support.SettlementIntent.CLIENT_CANCEL,
                    settlement_support.SettlementIntent.POOL_SHUTDOWN,
                )
                else LeaseState.FAILED
            )
            code = (
                CaptureErrorCode.CAPTURE_CANCELED
                if terminal_state is LeaseState.CANCELED
                else CaptureErrorCode.CAPTURE_ARTIFACT_IO_FAILED
            )
            result = pool._submit_settlement(
                settling,
                intent,
                terminal_state,
                code,
                "two-owner stress settlement",
            )
            if result is None or not result.settled:
                assert settling_owner.join(2)
                for _ in range(32):
                    pool.retry_settlements()
                    if pool.lease_snapshot(settling_id).settled:
                        break

        assert pool.lease_snapshot(settling_id).settled
        continuing_after = pool._leases[continuing_id]
        assert pool.lease_snapshot(continuing_id).state is LeaseState.COMMITTED
        assert not pool.lease_snapshot(continuing_id).settled
        assert pool.snapshot().active_lease_count == 1
        assert stream._registrations[
            continuing_after.owner_instance_id
        ] is continuing_registration
        assert continuing_after.artifact_id == continuing_artifact
        assert stream._ring is before_ring
        assert stream._callback is before_callback
        assert store.reserved_bytes < before_quota
        assert store.reserved_bytes > 0
    finally:
        pool.cancel(continuing_id, "two-owner cleanup")
        pool.shutdown()
