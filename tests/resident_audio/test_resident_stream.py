from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

import pytest

import app.service.analysis.resident_audio.stream as stream_module
from app.service.analysis.resident_audio import (
    CaptureErrorCode,
    CaptureOwnerState,
    OwnerControlEvent,
    OwnerControlKind,
    ResolvedInputDevice,
    RetryBackoff,
    SoundDeviceAudioBackend,
    StreamCommandIndeterminate,
    StreamState,
)
from app.service.analysis.resident_audio.stream import (
    ResidentStream,
    StreamFailureEvent,
)
from app.service.analysis.resident_audio.stream_control import StreamCommand
from app.service.analysis.resident_audio.owner_identity import (
    FinalityPruneOutcome,
    OwnerInstanceId,
    OwnerNonAdmissionProof,
    OwnerRemovalOutcome,
)
from app.service.analysis.resident_audio.owner_finality import (
    FinalityDurableProgress,
)
from tests.resident_audio.fakes import (
    BlockingOpenAudioBackend,
    FakeAudioBackend,
    FakeInputHandle,
    FakeResolver,
    FakeSoundDeviceModule,
    ManualClock,
)


def resolved(*, hardware_id: str = "mic-a", rate: float = 100.0, channels: int = 2):
    return ResolvedInputDevice(
        hardware_id=hardware_id,
        device_index=7,
        name="Resolved microphone",
        host_api="ASIO",
        sample_rate=rate,
        channels=channels,
        dtype="float32",
        resolution_version=3,
    )


def wait_stream(stream: ResidentStream) -> None:
    assert stream.synchronize(timeout=1.0)


def valid_block(
    handle: FakeInputHandle,
    clock: ManualClock,
    *,
    frames: int = 5,
    channels: int = 2,
    value: float = 1.0,
) -> None:
    handle.emit(
        frames=frames,
        channels=channels,
        callback_monotonic=clock(),
        adc_time=clock() - 0.01,
        current_time=clock(),
        value=value,
    )


def test_resolver_protocol_carries_full_attempt_identity():
    device = resolved()
    resolver = FakeResolver(device)
    cancel = threading.Event()
    result = resolver.resolve("mic-a", 11, 13, cancel)
    assert result is device
    assert resolver.calls[0].hardware_id == "mic-a"
    assert resolver.calls[0].desired_revision == 11
    assert resolver.calls[0].resolution_attempt_id == 13
    assert resolver.calls[0].cancel_token is cancel


def test_sounddevice_adapter_uses_only_resolved_index_and_format():
    module = FakeSoundDeviceModule()
    backend = SoundDeviceAudioBackend(module)
    device = resolved(rate=48_000.0, channels=4)
    callback = lambda *_: None
    handle = backend.open_input(device, block_frames=2400, callback=callback)
    handle.start()
    assert module.handle.calls == ["start"]
    assert module.kwargs == {
        "device": 7,
        "samplerate": 48_000.0,
        "channels": 4,
        "dtype": "float32",
        "blocksize": 2400,
        "callback": callback,
    }
    assert not hasattr(module, "query_devices")
    assert not hasattr(module, "query_hostapis")
    backend.force_terminate()
    assert module.terminate_calls == 1


def test_stream_opens_exact_resolution_and_is_ready_only_after_ingest():
    clock = ManualClock()
    backend = FakeAudioBackend()
    ready = []
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
        ready_sink=ready.append,
    )
    try:
        stream.start()
        assert stream.snapshot().state is StreamState.OPENING
        call = backend.open_calls[0]
        assert call.resolved is stream.resolved_device
        assert call.block_frames == 5
        assert backend.handle.calls == ["start"]
        assert ready == []

        valid_block(backend.handle, clock)
        wait_stream(stream)
        snapshot = stream.snapshot()
        assert snapshot.state is StreamState.READY
        assert snapshot.cursor == 5
        assert snapshot.ring_blocks == 1
        assert snapshot.sample_rate == 100.0
        assert snapshot.channels == 2
        assert snapshot.callback_frames == 5
        assert snapshot.ring_earliest_sample == 0
        assert snapshot.ring_latest_sample == 5
        assert snapshot.ring_earliest_monotonic is not None
        assert snapshot.ring_latest_monotonic is not None
        assert snapshot.command_depth == 0
        assert snapshot.old_generation_drops == 0
        assert snapshot.overflow_count == 0
        assert snapshot.recovery_count == 0
        assert snapshot.backlog_count == 0
        assert snapshot.finality_entry_count == 0
        assert len(ready) == 1
        token = ready[0]
        assert (token.hardware_id, token.pool_epoch, token.stream_generation) == (
            "mic-a",
            "epoch-a",
            4,
        )
        assert token.earliest_sample == 0
        assert token.latest_sample == 5
    finally:
        stream.close()


def test_stream_instance_rejects_start_after_close_without_reopening_backend():
    stream, backend, _ = make_ready_stream()
    assert stream.close()
    with pytest.raises(RuntimeError, match="single-use"):
        stream.start()
    assert backend.open_attempts == 1
    assert len(backend.open_calls) == 1


def test_stream_instance_rejects_restart_after_failed_open():
    backend = FakeAudioBackend()
    backend.open_error = OSError("first open failed")
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
    )
    stream.start()
    assert stream.snapshot().state is StreamState.FAILED
    backend.open_error = None
    with pytest.raises(RuntimeError, match="single-use"):
        stream.start()
    assert backend.open_attempts == 1
    assert backend.open_calls == []


def test_close_before_start_permanently_prevents_backend_open():
    backend = FakeAudioBackend()
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
    )
    assert stream.close()
    with pytest.raises(RuntimeError, match="single-use"):
        stream.start()
    assert backend.open_attempts == 0
    assert backend.open_calls == []


def test_close_waits_for_blocked_open_then_late_handle_is_cleaned_once():
    backend = BlockingOpenAudioBackend()
    ready = []
    failures = []
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        ready_sink=ready.append,
        failure_sink=failures.append,
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert backend.open_started.wait(1.0)

    close_results = []
    closer = threading.Thread(
        target=lambda: close_results.append(stream.close(timeout=1.0))
    )
    closer.start()
    assert not stream.generation_latch.alive
    assert closer.is_alive()
    backend.release_open.set()
    starter.join(1.0)
    closer.join(1.0)

    assert close_results == [True]
    assert backend.handle.calls == ["abort", "stop", "close"]
    assert backend.handle.physically_closed
    assert stream.snapshot().state is StreamState.CLOSED
    assert stream.snapshot().owner_count == 0
    assert stream.current_ready_token is None
    assert ready == []
    assert failures == []


def test_close_timeout_during_blocked_open_reports_unfinished_until_settled():
    backend = BlockingOpenAudioBackend()
    failures = []
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=failures.append,
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert backend.open_started.wait(1.0)

    assert not stream.close(timeout=0.01)
    timed_out = stream.snapshot()
    assert timed_out.state is not StreamState.CLOSED
    assert not stream.generation_latch.alive
    assert backend.handle.calls == []

    backend.release_open.set()
    starter.join(1.0)
    assert backend.handle.calls == ["abort", "stop", "close"]
    assert backend.handle.physically_closed
    assert stream.snapshot().state is not StreamState.CLOSED
    assert failures == []

    assert stream.close(timeout=1.0)
    assert stream.snapshot().state is StreamState.CLOSED
    assert backend.open_attempts == 1


@pytest.mark.parametrize("failing_operation", ["abort", "stop", "close"])
def test_late_open_cleanup_tracks_outcome_and_retries_only_definitive_close(
    failing_operation,
):
    backend = BlockingOpenAudioBackend()
    getattr(backend.handle, f"{failing_operation}_errors").append(
        RuntimeError(f"{failing_operation} failed")
    )
    ready = []
    failures = []
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        ready_sink=ready.append,
        failure_sink=failures.append,
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert backend.open_started.wait(1.0)

    close_results = []
    closer = threading.Thread(
        target=lambda: close_results.append(stream.close(timeout=1.0))
    )
    closer.start()
    backend.release_open.set()
    starter.join(1.0)
    closer.join(1.0)

    assert close_results == [False]
    first_calls = list(backend.handle.calls)
    assert first_calls == ["abort", "stop", "close"]
    assert ready == []
    assert failures == []
    assert stream.current_ready_token is None
    assert stream.snapshot().owner_count == 0
    if failing_operation == "close":
        assert stream.snapshot().state is not StreamState.CLOSED
        assert stream.snapshot().handle_open
        assert stream.tracked_cleanup_handle is backend.handle
        assert not backend.handle.physically_closed

        assert stream.close(timeout=1.0)
        assert backend.handle.calls == first_calls + ["close"]
        assert stream.tracked_cleanup_handle is None
        assert backend.handle.physically_closed
    else:
        assert stream.snapshot().state is StreamState.CLOSED
        assert not stream.snapshot().handle_open
        assert stream.tracked_cleanup_handle is None
        assert backend.handle.physically_closed

        assert stream.close(timeout=1.0)
        assert backend.handle.calls == first_calls
    assert stream.snapshot().state is StreamState.CLOSED


def test_repeated_late_definitive_close_failures_never_publish_closed_or_replay_stages():
    backend = BlockingOpenAudioBackend()
    backend.handle.close_errors.extend(
        [
            RuntimeError("initial close failed"),
            RuntimeError("first retry failed"),
            RuntimeError("second retry failed"),
        ]
    )
    ready = []
    failures = []
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        ready_sink=ready.append,
        failure_sink=failures.append,
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert backend.open_started.wait(1.0)
    initial_results = []
    closer = threading.Thread(
        target=lambda: initial_results.append(stream.close(timeout=1.0))
    )
    closer.start()
    backend.release_open.set()
    starter.join(1.0)
    closer.join(1.0)

    assert initial_results == [False]
    assert backend.handle.calls == ["abort", "stop", "close"]
    for expected_calls in (4, 5):
        assert not stream.close(timeout=1.0)
        assert stream.snapshot().state is not StreamState.CLOSED
        assert stream.snapshot().handle_open
        assert stream.tracked_cleanup_handle is backend.handle
        assert backend.handle.calls == [
            "abort",
            "stop",
            *(["close"] * (expected_calls - 2)),
        ]

    assert stream.close(timeout=1.0)
    successful_calls = list(backend.handle.calls)
    assert successful_calls == [
        "abort",
        "stop",
        "close",
        "close",
        "close",
        "close",
    ]
    assert stream.snapshot().state is StreamState.CLOSED
    assert not stream.snapshot().handle_open
    assert stream.tracked_cleanup_handle is None
    assert backend.handle.physically_closed
    assert stream.close(timeout=1.0)
    assert backend.handle.calls == successful_calls
    assert ready == []
    assert failures == []
    assert stream.current_ready_token is None
    assert stream.snapshot().owner_count == 0


def test_commands_after_final_admission_close_reject_immediately_without_residue(
    monkeypatch,
):
    stream, _, _ = make_ready_stream()
    admission_closed = threading.Event()
    release_worker_exit = threading.Event()
    original_finalize = stream._finalize_command_admission

    def paused_after_finalize():
        original_finalize()
        admission_closed.set()
        assert release_worker_exit.wait(2.0)

    monkeypatch.setattr(
        stream, "_finalize_command_admission", paused_after_finalize
    )
    closer = threading.Thread(target=lambda: stream.close(timeout=2.0))
    closer.start()
    assert admission_closed.wait(1.0)

    owner = StubOwner("run-closed", "attempt-closed", "cap-closed")
    owner.commit_open = True
    operations = [
        lambda: stream.prepare_owner(owner, identity(owner), timeout=None),
        lambda: stream.activate_owner(
            owner,
            identity(owner),
            commit_monotonic=10.0,
            timeout=None,
        ),
        lambda: stream.remove_owner(identity(owner), timeout=None),
        lambda: stream.switch_owner_live(
            owner, identity(owner), timeout=None
        ),
        lambda: stream.synchronize(timeout=None),
    ]
    results: list[object] = []
    submitters = [
        threading.Thread(
            target=lambda operation=operation: results.append(operation()),
            daemon=True,
        )
        for operation in operations
    ]
    for submitter in submitters:
        submitter.start()
    for submitter in submitters:
        submitter.join(0.1)

    async_result = stream.submit_owner_control(
        OwnerControlEvent(
            kind=OwnerControlKind.DETACH,
            owner=owner,
            run_id=owner.run_id,
            commit_attempt_id=owner.commit_attempt_id,
            owner_capability=owner.owner_capability,
            pool_epoch=owner.pool_epoch,
            stream_generation=owner.stream_generation,
        )
    )
    all_stopped = all(not submitter.is_alive() for submitter in submitters)
    queue_empty = stream._commands.qsize() == 0
    owner_count = stream.snapshot().owner_count
    release_worker_exit.set()
    closer.join(1.0)

    assert all_stopped
    receipts = [result for result in results if hasattr(result, "outcome")]
    assert len(receipts) == 1
    assert receipts[0].outcome.value == "TOMBSTONED_ABSENT"
    assert sum(result is False for result in results) == 4
    assert async_result is False
    assert queue_empty
    assert owner_count == 0
    assert owner.calls == []


def test_command_admitted_immediately_before_final_close_is_cancelled_and_acked(
    monkeypatch,
):
    stream, _, _ = make_ready_stream()
    finalization_entered = threading.Event()
    allow_finalization = threading.Event()
    original_finalize = stream._finalize_command_admission

    def paused_before_finalize():
        finalization_entered.set()
        assert allow_finalization.wait(2.0)
        original_finalize()

    monkeypatch.setattr(
        stream, "_finalize_command_admission", paused_before_finalize
    )
    closer = threading.Thread(target=lambda: stream.close(timeout=2.0))
    closer.start()
    assert finalization_entered.wait(1.0)

    command_results = []
    submitter = threading.Thread(
        target=lambda: command_results.append(
            stream.synchronize(timeout=None)
        ),
        daemon=True,
    )
    submitter.start()
    deadline = time.monotonic() + 1.0
    while stream._commands.qsize() == 0 and time.monotonic() < deadline:
        threading.Event().wait(0.001)
    assert stream._commands.qsize() == 1

    allow_finalization.set()
    submitter.join(1.0)
    closer.join(1.0)
    assert not submitter.is_alive()
    assert command_results == [False]
    assert stream._commands.qsize() == 0


@dataclass
class StubOwner:
    run_id: str
    commit_attempt_id: str
    owner_capability: str
    pool_epoch: str = "epoch-a"
    stream_generation: int = 4
    commit_open: bool = False
    state: CaptureOwnerState = CaptureOwnerState.PREPARED

    def __post_init__(self):
        self.calls: list[tuple] = []
        self.failure_probe = None

    def snapshot(self):
        return type(
            "Snapshot",
            (),
            {"commit_open": self.commit_open, "state": self.state},
        )()

    def activate(self, timeline, *, commit_monotonic):
        self.calls.append(("activate", timeline.cursor))
        if not self.commit_open:
            raise RuntimeError("commit token is not open")
        self.state = CaptureOwnerState.WAITING_FOR_MAPPING
        return self.state

    def advance_pending_mapping(self, timeline, *, now_monotonic=None):
        self.calls.append(("map", timeline.cursor))
        self.state = CaptureOwnerState.CATCHING_UP
        return True

    def notify_ring_extended(self):
        self.calls.append(("extended",))

    def offer_live(self, block):
        self.calls.append(("offer", block.sample_end))
        return True

    def switch_to_live(self, cursor):
        self.calls.append(("switch", cursor))
        self.state = CaptureOwnerState.LIVE
        return True

    def stream_failed(self, code, message):
        alive = None if self.failure_probe is None else self.failure_probe()
        if self.state is CaptureOwnerState.FREEZING:
            self.calls.append(("ignored-failure", code, alive))
            return False
        self.calls.append(("failed", code, alive))
        self.state = CaptureOwnerState.FAILED
        return True


class BlockingActivateOwner(StubOwner):
    def __post_init__(self):
        super().__post_init__()
        self.activate_started = threading.Event()
        self.release_activate = threading.Event()

    def activate(self, timeline, *, commit_monotonic):
        self.activate_started.set()
        assert self.release_activate.wait(1.0)
        return super().activate(
            timeline, commit_monotonic=commit_monotonic
        )


def identity(owner: StubOwner, *, generation: int | None = None):
    return OwnerInstanceId(
        pool_epoch=owner.pool_epoch,
        hardware_id="mic-a",
        stream_generation=(
            owner.stream_generation if generation is None else generation
        ),
        run_id=owner.run_id,
        commit_attempt_id=owner.commit_attempt_id,
        owner_capability=owner.owner_capability,
        instance_nonce=f"test-owner-{id(owner)}",
    )


def make_ready_stream(*, clock=None, failures=None, backoff=None, generation=4):
    clock = clock or ManualClock()
    backend = FakeAudioBackend()
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=generation,
        monotonic_clock=clock,
        failure_sink=None if failures is None else failures.append,
        retry_backoff=backoff,
    )
    stream.start()
    valid_block(backend.handle, clock)
    wait_stream(stream)
    return stream, backend, clock


def owner_instance(owner: StubOwner, *, nonce: str = "nonce-1"):
    return OwnerInstanceId(
        pool_epoch=owner.pool_epoch,
        hardware_id="mic-a",
        stream_generation=owner.stream_generation,
        run_id=owner.run_id,
        commit_attempt_id=owner.commit_attempt_id,
        owner_capability=owner.owner_capability,
        instance_nonce=nonce,
    )


def test_stream_prepare_reserves_ledger_before_command_enqueue(monkeypatch):
    stream, _, _ = make_ready_stream()
    owner = StubOwner("run-ledger", "attempt-ledger", "cap-ledger")
    instance_id = owner_instance(owner)
    observed = []
    original_submit = stream._command_admission.submit

    def inspect_before_enqueue(command, commands, wake):
        observed.append(stream.finality_snapshot().pending_late_commands)
        return original_submit(command, commands, wake)

    monkeypatch.setattr(stream._command_admission, "submit", inspect_before_enqueue)
    try:
        assert stream.prepare_owner(owner, instance_id, timeout=1.0) is True
        assert observed == [1]
    finally:
        stream.close()


def test_stream_remove_by_value_identity_never_removes_new_owner():
    stream, _, _ = make_ready_stream()
    old = StubOwner("same-run-ledger", "attempt-old", "cap-old")
    replacement = StubOwner("same-run-ledger", "attempt-new", "cap-new")
    old_id = owner_instance(old, nonce="nonce-old")
    replacement_id = owner_instance(replacement, nonce="nonce-new")
    try:
        assert stream.prepare_owner(old, old_id, timeout=1.0) is True
        removed = stream.remove_owner(old_id, timeout=1.0)
        assert removed.confirms(old_id)
        assert stream.prepare_owner(replacement, replacement_id, timeout=1.0)
        mismatch = stream.remove_owner(
            replace(old_id, instance_nonce="wrong"), timeout=1.0
        )
        assert mismatch.outcome is OwnerRemovalOutcome.MISMATCH
        assert stream.snapshot().owner_count == 1
        assert stream.remove_owner(replacement_id, timeout=1.0).confirms(
            replacement_id
        )
    finally:
        stream.close()


def test_late_claimed_prepare_cannot_escape_tombstone(monkeypatch):
    stream, _, _ = make_ready_stream()
    owner = StubOwner("late-prepare", "attempt-late", "cap-late")
    instance_id = owner_instance(owner)
    claimed = threading.Event()
    release = threading.Event()
    original = stream._prepare

    def blocked(candidate, candidate_id):
        claimed.set()
        assert release.wait(2.0)
        return original(candidate, candidate_id)

    monkeypatch.setattr(stream, "_prepare", blocked)
    try:
        with pytest.raises(StreamCommandIndeterminate):
            stream.prepare_owner(owner, instance_id, timeout=0.01)
        assert claimed.wait(1.0)
        first = stream.remove_owner(instance_id, timeout=0.01)
        assert first.outcome is OwnerRemovalOutcome.INDETERMINATE
        release.set()
        wait_stream(stream)
        final = stream.remove_owner(instance_id, timeout=1.0)
        assert final.outcome is OwnerRemovalOutcome.TOMBSTONED_ABSENT
        assert stream.snapshot().owner_count == 0
    finally:
        release.set()
        stream.close()


def test_stream_acknowledge_and_prune_route_exact_value_finality():
    stream, _, _ = make_ready_stream()
    owner = StubOwner("ack-prune", "attempt-ack", "cap-ack")
    instance_id = owner_instance(owner)
    try:
        assert stream.prepare_owner(owner, instance_id, timeout=1.0)
        removal = stream.remove_owner(instance_id, timeout=1.0)
        durable = FinalityDurableProgress(
            removal_receipt=removal,
            owner_signaled=True,
            owner_joined=True,
            late_owner_commands_resolved=True,
            remove_retry_disabled=True,
        )
        ack = stream.acknowledge_finality(
            instance_id, removal, durable, timeout=1.0
        )
        assert ack is not None
        pruned = stream.prune_finality(instance_id, ack, timeout=1.0)
        assert pruned.outcome is FinalityPruneOutcome.PRUNED
        assert stream.finality_snapshot().entries == ()
    finally:
        stream.close()


def test_ledger_full_prepare_rejects_before_enqueue():
    clock = ManualClock()
    backend = FakeAudioBackend()
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
        owner_finality_limit=1,
    )
    stream.start()
    valid_block(backend.handle, clock)
    wait_stream(stream)
    first = StubOwner("ledger-first", "attempt-first", "cap-first")
    second = StubOwner("ledger-second", "attempt-second", "cap-second")
    try:
        assert stream.prepare_owner(
            first, owner_instance(first), timeout=1.0
        )
        result = stream.prepare_owner(
            second, owner_instance(second), timeout=1.0
        )
        assert isinstance(result, OwnerNonAdmissionProof)
        assert result.confirms(owner_instance(second))
        assert stream.snapshot().owner_count == 1
    finally:
        stream.close()


def test_prepare_is_dormant_activation_is_acknowledged_and_ordered_after_ring():
    stream, backend, clock = make_ready_stream()
    owner = StubOwner("run-1", "attempt-1", "cap-1")
    try:
        assert stream.prepare_owner(owner, identity(owner), timeout=1.0)
        clock.advance(0.05)
        valid_block(backend.handle, clock)
        wait_stream(stream)
        assert owner.calls == []

        assert not stream.activate_owner(
            owner, identity(owner), commit_monotonic=clock(), timeout=1.0
        )
        owner.commit_open = True
        assert stream.activate_owner(
            owner, identity(owner), commit_monotonic=clock(), timeout=1.0
        )
        assert owner.calls[0] == ("activate", 10)

        clock.advance(0.05)
        valid_block(backend.handle, clock)
        wait_stream(stream)
        assert ("map", 15) in owner.calls
        assert owner.calls.index(("map", 15)) < owner.calls.index(("extended",))
    finally:
        stream.close()


def test_identity_mismatched_remove_cannot_delete_replacement():
    stream, _, _ = make_ready_stream()
    old = StubOwner("same-run", "old-attempt", "old-cap")
    replacement = StubOwner("same-run", "new-attempt", "new-cap")
    try:
        assert stream.prepare_owner(old, identity(old), timeout=1.0)
        removed = stream.remove_owner(identity(old), timeout=1.0)
        assert removed.outcome.value == "REMOVED"
        assert stream.prepare_owner(replacement, identity(replacement), timeout=1.0)
        assert stream.remove_owner(identity(old), timeout=1.0) == removed
        assert stream.snapshot().owner_count == 1
        replacement.commit_open = True
        assert stream.activate_owner(
            replacement,
            identity(replacement),
            commit_monotonic=10.0,
            timeout=1.0,
        )
    finally:
        stream.close()


@pytest.mark.parametrize(
    "mismatch",
    [
        "pool_epoch",
        "hardware_id",
        "stream_generation",
        "run_id",
        "commit_attempt_id",
        "owner_capability",
        "instance_nonce",
        "alternate_identity",
    ],
)
def test_each_stale_identity_mismatch_preserves_active_replacement_and_routing(
    mismatch,
):
    stream, backend, clock = make_ready_stream()
    stale = StubOwner("run-a", "attempt-a", "cap-a", commit_open=True)
    replacement = StubOwner(
        "run-b", "attempt-b", "cap-b", commit_open=True
    )
    stale_id = owner_instance(stale, nonce="nonce-a")
    replacement_id = owner_instance(replacement, nonce="nonce-b")
    candidate_identity = stale_id
    if mismatch == "alternate_identity":
        alternate = StubOwner(
            "run-other", "attempt-other", "cap-other"
        )
        candidate_identity = owner_instance(
            alternate, nonce="nonce-other"
        )
    else:
        candidate_identity = replace(
            stale_id,
            **{
                mismatch: (
                    999
                    if mismatch == "stream_generation"
                    else f"wrong-{mismatch}"
                )
            },
        )
    replacement.artifact = object()
    try:
        assert stream.prepare_owner(stale, stale_id, timeout=1.0)
        assert stream.prepare_owner(
            replacement, replacement_id, timeout=1.0
        )
        assert stream.activate_owner(
            stale,
            stale_id,
            commit_monotonic=clock(),
            timeout=1.0,
        )
        assert stream.activate_owner(
            replacement,
            replacement_id,
            commit_monotonic=clock(),
            timeout=1.0,
        )
        registration_before = stream._registrations[replacement_id]
        ring_before = stream._ring
        callback_before = stream._callback
        artifact_before = replacement.artifact
        calls_before = list(replacement.calls)
        stream_before = stream.snapshot()
        finality_before = stream.finality_snapshot()

        receipt = stream.remove_owner(
            candidate_identity, timeout=1.0
        )
        assert receipt.outcome.value == "MISMATCH"
        assert stream._registrations[replacement_id] is registration_before
        assert stream._ring is ring_before
        assert stream._callback is callback_before
        assert replacement.artifact is artifact_before
        assert replacement.calls == calls_before
        assert stream.snapshot() == stream_before
        assert stream.finality_snapshot() == finality_before

        clock.advance(0.05)
        valid_block(backend.handle, clock)
        wait_stream(stream)
        assert len(replacement.calls) > len(calls_before)
        assert stream.remove_owner(stale_id, timeout=1.0).confirms(
            stale_id
        )
        assert replacement_id in stream._registrations

        later = StubOwner(
            "run-a", "attempt-a-later", "cap-a-later", commit_open=True
        )
        later_id = owner_instance(later, nonce="nonce-a-later")
        assert stream.prepare_owner(later, later_id, timeout=1.0)
        assert stream.activate_owner(
            later,
            later_id,
            commit_monotonic=clock(),
            timeout=1.0,
        )
        assert replacement_id in stream._registrations
        assert later_id in stream._registrations
        assert stream.remove_owner(later_id, timeout=1.0).confirms(later_id)
        assert stream.remove_owner(
            replacement_id, timeout=1.0
        ).confirms(replacement_id)
    finally:
        stream.close()


@pytest.mark.parametrize("command_kind", ["prepare", "activate", "remove", "switch"])
def test_timed_out_queued_owner_command_never_mutates_later(command_kind):
    stream, _, _ = make_ready_stream()
    blocker = BlockingActivateOwner(
        "run-blocker", "attempt-blocker", "cap-blocker"
    )
    blocker.commit_open = True
    target = StubOwner("run-target", "attempt-target", "cap-target")
    target.commit_open = True
    assert stream.prepare_owner(blocker, identity(blocker), timeout=1.0)
    if command_kind != "prepare":
        assert stream.prepare_owner(target, identity(target), timeout=1.0)
    if command_kind == "switch":
        assert stream.activate_owner(
            target,
            identity(target),
            commit_monotonic=10.0,
            timeout=1.0,
        )
        target.state = CaptureOwnerState.CATCHING_UP

    blocker_result = []
    blocker_thread = threading.Thread(
        target=lambda: blocker_result.append(
            stream.activate_owner(
                blocker,
                identity(blocker),
                commit_monotonic=10.0,
                timeout=1.0,
            )
        )
    )
    blocker_thread.start()
    assert blocker.activate_started.wait(1.0)

    if command_kind == "prepare":
        result = stream.prepare_owner(target, identity(target), timeout=0.01)
    elif command_kind == "activate":
        result = stream.activate_owner(
            target,
            identity(target),
            commit_monotonic=10.0,
            timeout=0.01,
        )
    elif command_kind == "remove":
        result = stream.remove_owner(identity(target), timeout=0.01)
    else:
        result = stream.switch_owner_live(
            target, identity(target), timeout=0.01
        )
    if command_kind == "remove":
        assert result.outcome.value == "INDETERMINATE"
    else:
        assert result is False

    blocker.release_activate.set()
    blocker_thread.join(1.0)
    assert blocker_result == [True]
    wait_stream(stream)
    if command_kind == "prepare":
        assert stream.snapshot().owner_count == 1
        assert target.calls == []
    elif command_kind == "activate":
        assert target.calls == []
        assert stream.snapshot().owner_count == 2
    elif command_kind == "remove":
        assert stream.snapshot().owner_count == 2
    else:
        assert not any(call[0] == "switch" for call in target.calls)
        assert stream.snapshot().owner_count == 2
    stream.close()


def test_claimed_command_timeout_reports_identity_rollback_requirement():
    stream, _, _ = make_ready_stream()
    owner = BlockingActivateOwner("run-claimed", "attempt-claimed", "cap-claimed")
    owner.commit_open = True
    assert stream.prepare_owner(owner, identity(owner), timeout=1.0)
    try:
        with pytest.raises(StreamCommandIndeterminate) as caught:
            stream.activate_owner(
                owner,
                identity(owner),
                commit_monotonic=10.0,
                timeout=0.01,
            )
        error = caught.value
        assert error.command_kind == "activate"
        assert error.identity == identity(owner)
        assert error.owner is owner
        assert error.rollback_required
    finally:
        owner.release_activate.set()
        assert stream.remove_owner(identity(owner), timeout=1.0)
        stream.close()


@pytest.mark.parametrize("prepare_final", ["true", "false", "raise"])
@pytest.mark.parametrize("ack_timing", ["before", "after"])
def test_prepare_remove_tombstone_matrix_is_exact_and_negative_final(
    monkeypatch, prepare_final, ack_timing
):
    stream, _, _ = make_ready_stream()
    owner = StubOwner("run-matrix", "attempt-matrix", "cap-matrix")
    replacement = StubOwner(
        "run-matrix", "attempt-replacement", "cap-replacement"
    )
    original_prepare = stream._prepare
    prepare_claimed = threading.Event()
    prepare_continue = threading.Event()

    def controlled_prepare(candidate, candidate_identity):
        prepare_claimed.set()
        if ack_timing == "after":
            assert prepare_continue.wait(2.0)
        if prepare_final == "true":
            return original_prepare(candidate, candidate_identity)
        if prepare_final == "false":
            return False
        raise RuntimeError("injected prepare failure")

    monkeypatch.setattr(stream, "_prepare", controlled_prepare)
    try:
        if ack_timing == "before":
            result = stream.prepare_owner(
                owner, identity(owner), timeout=0.1
            )
            assert result is (prepare_final == "true")
            if result:
                receipt = stream.remove_owner(
                    identity(owner), timeout=1.0
                )
                assert receipt.outcome.value == "REMOVED"
                assert (
                    stream.remove_owner(
                        identity(owner), timeout=1.0
                    )
                    == receipt
                )
        else:
            with pytest.raises(StreamCommandIndeterminate):
                stream.prepare_owner(
                    owner, identity(owner), timeout=0.01
                )
            assert prepare_claimed.wait(1.0)
            indeterminate = stream.remove_owner(
                identity(owner), timeout=0.01
            )
            assert indeterminate.outcome.value == "INDETERMINATE"
            prepare_continue.set()
            wait_stream(stream)
            assert stream.snapshot().owner_count == 0
            receipt = stream.remove_owner(
                identity(owner), timeout=1.0
            )
            assert receipt.outcome.value == "TOMBSTONED_ABSENT"
            assert (
                stream.remove_owner(
                    identity(owner), timeout=1.0
                )
                    == receipt
            )

        monkeypatch.setattr(stream, "_prepare", original_prepare)
        assert stream.prepare_owner(
            replacement, identity(replacement), timeout=1.0
        )
        assert stream.remove_owner(
            identity(replacement), timeout=1.0
        )
    finally:
        prepare_continue.set()
        stream.close()


@pytest.mark.parametrize("handler_final", ["true", "false", "raise"])
@pytest.mark.parametrize("ack_timing", ["before", "after"])
def test_remove_receipt_survives_handler_result_and_ack_order(
    monkeypatch, handler_final, ack_timing
):
    stream, _, _ = make_ready_stream()
    owner = StubOwner("run-remove", "attempt-remove", "cap-remove")
    original_remove = stream._remove
    remove_claimed = threading.Event()
    remove_continue = threading.Event()

    def controlled_remove(candidate_identity):
        receipt = original_remove(candidate_identity)
        remove_claimed.set()
        if ack_timing == "after":
            assert remove_continue.wait(2.0)
        if handler_final == "true":
            return receipt
        if handler_final == "false":
            return False
        raise RuntimeError("injected remove handler failure")

    monkeypatch.setattr(stream, "_remove", controlled_remove)
    try:
        assert stream.prepare_owner(owner, identity(owner), timeout=1.0)
        first = stream.remove_owner(
            identity(owner),
            timeout=1.0 if ack_timing == "before" else 0.01,
        )
        assert remove_claimed.wait(1.0)
        if handler_final == "true" and ack_timing == "before":
            assert first.outcome.value == "REMOVED"
        else:
            assert first.outcome.value == "INDETERMINATE"
        remove_continue.set()
        wait_stream(stream)
        assert stream.snapshot().owner_count == 0

        confirmed = stream.remove_owner(
            identity(owner), timeout=1.0
        )
        assert confirmed.outcome.value == "REMOVED"
        assert (
            stream.remove_owner(
                identity(owner), timeout=1.0
            )
            == confirmed
        )
    finally:
        remove_continue.set()
        stream.close()


@pytest.mark.parametrize(
    "kind",
    [OwnerControlKind.DETACH, OwnerControlKind.REQUEST_LIVE_SWITCH],
)
def test_stale_owner_control_object_cannot_mutate_identical_replacement(kind):
    stream, _, _ = make_ready_stream()
    stale = StubOwner("same-run", "same-attempt", "same-cap")
    replacement = StubOwner("same-run", "same-attempt", "same-cap")
    replacement.commit_open = True
    try:
        assert stream.prepare_owner(stale, identity(stale), timeout=1.0)
        assert stream.remove_owner(identity(stale), timeout=1.0)
        assert stream.prepare_owner(
            replacement, identity(replacement), timeout=1.0
        )
        assert stream.activate_owner(
            replacement,
            identity(replacement),
            commit_monotonic=10.0,
            timeout=1.0,
        )
        replacement.state = CaptureOwnerState.CATCHING_UP
        before = list(replacement.calls)

        stream.submit_owner_control(
            OwnerControlEvent(
                kind=kind,
                owner=stale,
                run_id=stale.run_id,
                commit_attempt_id=stale.commit_attempt_id,
                owner_capability=stale.owner_capability,
                pool_epoch=stale.pool_epoch,
                stream_generation=stale.stream_generation,
            )
        )
        wait_stream(stream)
        assert stream.snapshot().owner_count == 1
        assert replacement.calls == before

        stream.submit_owner_control(
            OwnerControlEvent(
                kind=kind,
                owner=replacement,
                run_id=replacement.run_id,
                commit_attempt_id=replacement.commit_attempt_id,
                owner_capability=replacement.owner_capability,
                pool_epoch=replacement.pool_epoch,
                stream_generation=replacement.stream_generation,
            )
        )
        wait_stream(stream)
        if kind is OwnerControlKind.DETACH:
            assert stream.snapshot().owner_count == 0
        else:
            assert replacement.calls[-1] == ("switch", 5)
    finally:
        stream.close()


@pytest.mark.parametrize(
    ("failure_kind", "expected"),
    [
        ("second-invalid", CaptureErrorCode.TIMING_UNAVAILABLE),
        ("overflow", CaptureErrorCode.INPUT_OVERFLOW_OR_GAP),
        ("ingress-full", CaptureErrorCode.INPUT_INGRESS_BACKLOG),
        ("no-data", CaptureErrorCode.INPUT_DATA_TIMEOUT),
        ("backend", CaptureErrorCode.STREAM_OPEN_FAILED),
        ("disconnect", CaptureErrorCode.STREAM_OPEN_FAILED),
    ],
)
def test_generation_faults_clear_alive_fail_owner_and_emit_retry(
    failure_kind, expected
):
    failures: list[StreamFailureEvent] = []
    stream, backend, clock = make_ready_stream(failures=failures)
    owner = StubOwner("run-fault", "attempt-fault", "cap-fault")
    owner.commit_open = True
    assert stream.prepare_owner(owner, identity(owner), timeout=1.0)
    assert stream.activate_owner(
        owner, identity(owner), commit_monotonic=clock(), timeout=1.0
    )

    if failure_kind == "second-invalid":
        for _ in range(2):
            clock.advance(0.05)
            backend.handle.emit(
                frames=5,
                channels=2,
                callback_monotonic=clock(),
                adc_time=float("nan"),
                current_time=clock(),
            )
            wait_stream(stream)
    elif failure_kind == "overflow":
        clock.advance(0.05)
        backend.handle.emit(
            frames=5,
            channels=2,
            callback_monotonic=clock(),
            status="overflow",
        )
        wait_stream(stream)
    elif failure_kind == "ingress-full":
        stream.pause_ingest()
        for _ in range(5):
            clock.advance(0.05)
            valid_block(backend.handle, clock)
        stream.resume_ingest()
        wait_stream(stream)
    elif failure_kind == "no-data":
        clock.advance(2.01)
        stream.check_deadlines(clock(), timeout=1.0)
    else:
        stream.report_backend_failure(
            RuntimeError(failure_kind),
            disconnected=failure_kind == "disconnect",
            timeout=1.0,
        )

    assert not stream.generation_latch.alive
    assert owner.calls[-1][:2] == ("failed", expected)
    assert failures[-1].error_code is expected
    assert failures[-1].hardware_id == "mic-a"
    assert failures[-1].retry_delay == 0.5
    assert stream.snapshot().state is StreamState.FAILED
    stream.control_close_handle()
    assert backend.handle.calls[-3:] == ["abort", "stop", "close"]
    assert stream.close()


def test_first_valid_block_timeout_and_invalid_first_block_never_publish_ready():
    failures = []
    clock = ManualClock()
    backend = FakeAudioBackend()
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
        ready_sink=lambda token: pytest.fail(f"unexpected token {token}"),
        failure_sink=failures.append,
    )
    stream.start()
    backend.handle.emit(
        frames=5,
        channels=2,
        callback_monotonic=clock(),
        adc_time=float("nan"),
        current_time=clock(),
    )
    wait_stream(stream)
    assert stream.snapshot().state is StreamState.OPENING
    clock.advance(2.01)
    assert stream.check_deadlines(clock(), timeout=1.0)
    assert failures[-1].error_code is CaptureErrorCode.FIRST_VALID_BLOCK_TIMEOUT
    stream.control_close_handle()
    assert stream.close()


def test_failure_clears_generation_before_dormant_owner_and_ignores_freezing():
    stream, _, _ = make_ready_stream()
    dormant = StubOwner("run-dormant", "attempt-dormant", "cap-dormant")
    dormant.failure_probe = lambda: stream.generation_latch.alive
    freezing = StubOwner("run-freezing", "attempt-freezing", "cap-freezing")
    freezing.commit_open = True
    freezing.failure_probe = lambda: stream.generation_latch.alive
    assert stream.prepare_owner(dormant, identity(dormant), timeout=1.0)
    assert stream.prepare_owner(freezing, identity(freezing), timeout=1.0)
    assert stream.activate_owner(
        freezing, identity(freezing), commit_monotonic=10.0, timeout=1.0
    )
    freezing.state = CaptureOwnerState.FREEZING

    stream.report_backend_failure(RuntimeError("lost"), timeout=1.0)
    assert dormant.calls[-1] == (
        "failed",
        CaptureErrorCode.STREAM_OPEN_FAILED,
        False,
    )
    assert freezing.calls[-1] == (
        "ignored-failure",
        CaptureErrorCode.STREAM_OPEN_FAILED,
        False,
    )
    assert freezing.state is CaptureOwnerState.FREEZING
    assert stream.close()


def test_open_exception_is_structured_and_never_starts_a_generation():
    failures = []
    backend = FakeAudioBackend()
    backend.open_error = OSError("device unavailable")
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=failures.append,
    )
    stream.start()
    assert stream.snapshot().state is StreamState.FAILED
    assert not stream.generation_latch.alive
    assert failures[-1].error_code is CaptureErrorCode.STREAM_OPEN_FAILED
    assert failures[-1].retry_delay == 0.5
    assert backend.handle.calls == []


def test_handle_start_exception_is_failed_by_ingest_and_closed_on_control_path():
    failures = []
    handle = FakeInputHandle()
    handle.raise_on_start = OSError("start rejected")
    backend = FakeAudioBackend(handle)
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=failures.append,
    )
    stream.start()
    assert stream.snapshot().state is StreamState.FAILED
    assert failures[-1].error_code is CaptureErrorCode.STREAM_OPEN_FAILED
    assert handle.calls == ["start", "abort", "stop", "close"]
    assert stream.close()


@pytest.mark.parametrize("failure_point", ["latency", "start"])
def test_startup_closes_handle_before_blocked_failure_sink(failure_point):
    handle = FakeInputHandle()
    if failure_point == "latency":
        handle.latency_error = RuntimeError("latency failed")
    else:
        handle.raise_on_start = RuntimeError("start failed")
    backend = FakeAudioBackend(handle)
    sink_started = threading.Event()
    release_sink = threading.Event()

    def blocked_sink(_event):
        sink_started.set()
        assert release_sink.wait(1.0)

    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=blocked_sink,
    )
    starter = threading.Thread(target=stream.start)
    starter.start()
    assert sink_started.wait(1.0)
    assert handle.calls[-3:] == ["abort", "stop", "close"]
    assert handle.physically_closed
    release_sink.set()
    starter.join(1.0)
    assert not starter.is_alive()
    assert not stream.snapshot().handle_open
    stream.close()


def test_startup_cleanup_survives_indeterminate_failure_notification():
    handle = FakeInputHandle()
    handle.raise_on_start = RuntimeError("start failed")
    backend = FakeAudioBackend(handle)

    def indeterminate_sink(_event):
        raise StreamCommandIndeterminate(
            StreamCommand("startup-failure-notification")
        )

    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=indeterminate_sink,
    )
    stream.start()
    assert handle.physically_closed
    assert handle.calls == ["start", "abort", "stop", "close"]
    assert not stream.snapshot().handle_open


def test_latency_failure_after_open_closes_owned_handle():
    failures = []
    handle = FakeInputHandle()
    handle.latency_error = RuntimeError("latency unavailable")
    backend = FakeAudioBackend(handle)
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        failure_sink=failures.append,
    )
    stream.start()
    snapshot = stream.snapshot()
    assert snapshot.state is StreamState.FAILED
    assert snapshot.handle_close_attempted
    assert not snapshot.handle_open
    assert handle.calls == ["abort", "stop", "close"]
    assert failures[-1].error_code is CaptureErrorCode.STREAM_OPEN_FAILED


@pytest.mark.parametrize("failing_operation", ["abort", "stop", "close"])
def test_close_failure_stays_open_and_can_be_retried(failing_operation):
    stream, backend, _ = make_ready_stream()
    errors = getattr(backend.handle, f"{failing_operation}_errors")
    errors.append(RuntimeError(f"{failing_operation} failed"))

    assert not stream.close()
    failed = stream.snapshot()
    assert failed.handle_close_attempted
    assert backend.handle.calls[-3:] == ["abort", "stop", "close"]
    calls_after_first = list(backend.handle.calls)
    assert failed.handle_abort_succeeded is (failing_operation != "abort")
    assert failed.handle_stop_succeeded is (failing_operation != "stop")
    if failing_operation == "close":
        assert failed.state is not StreamState.CLOSED
        assert failed.handle_open
        assert stream.close()
        assert backend.handle.calls[-3:] == ["abort", "stop", "close"]
    else:
        assert failed.state is StreamState.CLOSED
        assert not failed.handle_open
        assert backend.handle.physically_closed
        assert stream.close()
        assert backend.handle.calls == calls_after_first
    recovered = stream.snapshot()
    assert recovered.state is StreamState.CLOSED
    assert not recovered.handle_open


def test_indeterminate_close_still_closes_handle(monkeypatch):
    stream, backend, _ = make_ready_stream()
    close_started = threading.Event()
    release_close_command = threading.Event()
    original_begin_close = stream._begin_close

    def blocked_begin_close():
        close_started.set()
        assert release_close_command.wait(1.0)
        original_begin_close()

    monkeypatch.setattr(stream, "_begin_close", blocked_begin_close)
    with pytest.raises(StreamCommandIndeterminate):
        stream.close(timeout=0.01)
    assert close_started.is_set()
    assert backend.handle.physically_closed
    assert backend.handle.calls[-3:] == ["abort", "stop", "close"]
    release_close_command.set()
    assert stream.close(timeout=1.0)


def test_ready_state_and_token_are_published_atomically(monkeypatch):
    clock = ManualClock()
    backend = FakeAudioBackend()
    token_construction_started = threading.Event()
    release_token_construction = threading.Event()
    real_token_type = stream_module.StreamReadyToken

    def blocked_token_type(**kwargs):
        token_construction_started.set()
        assert release_token_construction.wait(1.0)
        return real_token_type(**kwargs)

    monkeypatch.setattr(stream_module, "StreamReadyToken", blocked_token_type)
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
    )
    stream.start()
    emitter = threading.Thread(
        target=lambda: valid_block(backend.handle, clock)
    )
    emitter.start()
    assert token_construction_started.wait(1.0)
    assert stream.snapshot().state is StreamState.OPENING
    assert stream.current_ready_token is None
    release_token_construction.set()
    emitter.join(1.0)
    wait_stream(stream)
    token = stream.current_ready_token
    assert stream.snapshot().state is StreamState.READY
    assert token is not None
    assert (
        token.hardware_id,
        token.pool_epoch,
        token.stream_generation,
    ) == ("mic-a", "epoch-a", 4)
    stream.close()


def test_latched_failure_during_first_token_construction_prevents_readiness(
    monkeypatch,
):
    clock = ManualClock()
    backend = FakeAudioBackend()
    ready = []
    failures = []
    token_construction_started = threading.Event()
    release_token_construction = threading.Event()
    failure_published = threading.Event()
    real_token_type = stream_module.StreamReadyToken

    def blocked_token_type(**kwargs):
        token_construction_started.set()
        assert release_token_construction.wait(1.0)
        return real_token_type(**kwargs)

    def failure_sink(event):
        failures.append(event)
        failure_published.set()

    monkeypatch.setattr(stream_module, "StreamReadyToken", blocked_token_type)
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
        ready_sink=ready.append,
        failure_sink=failure_sink,
    )
    stream.start()
    valid_block(backend.handle, clock)
    assert token_construction_started.wait(1.0)

    clock.advance(0.01)
    backend.handle.emit(
        frames=5,
        channels=2,
        callback_monotonic=clock(),
        status="overflow",
    )
    assert not stream.generation_latch.alive
    release_token_construction.set()
    assert failure_published.wait(1.0)

    assert stream.snapshot().state is StreamState.FAILED
    assert stream.current_ready_token is None
    assert ready == []
    assert failures[-1].error_code is CaptureErrorCode.INPUT_OVERFLOW_OR_GAP
    stream.close()


def test_recovery_discontinuity_fails_generation_and_publishes_no_new_token():
    ready = []
    failures = []
    clock = ManualClock()
    backend = FakeAudioBackend()
    stream = ResidentStream(
        resolved_device=resolved(),
        backend=backend,
        pool_epoch="epoch-a",
        stream_generation=4,
        monotonic_clock=clock,
        ready_sink=ready.append,
        failure_sink=failures.append,
    )
    stream.start()
    valid_block(backend.handle, clock)
    wait_stream(stream)
    clock.advance(0.05)
    backend.handle.emit(
        frames=5,
        channels=2,
        callback_monotonic=clock(),
        adc_time=float("nan"),
        current_time=clock(),
    )
    wait_stream(stream)
    assert stream.snapshot().state is StreamState.TIMING_RECOVERY
    assert len(ready) == 1

    clock.advance(1.0)
    valid_block(backend.handle, clock)
    wait_stream(stream)
    assert failures[-1].error_code is CaptureErrorCode.TIMING_UNAVAILABLE
    assert len(ready) == 1
    stream.control_close_handle()
    assert stream.close()


def test_timing_recovery_accepts_same_generation_dormant_prepare():
    stream, backend, clock = make_ready_stream()
    owner = StubOwner("run-recovery", "attempt-recovery", "cap-recovery")
    try:
        clock.advance(0.05)
        backend.handle.emit(
            frames=5,
            channels=2,
            callback_monotonic=clock(),
            adc_time=float("nan"),
            current_time=clock(),
        )
        wait_stream(stream)
        assert stream.snapshot().state is StreamState.TIMING_RECOVERY
        assert stream.prepare_owner(owner, identity(owner), timeout=1.0)
        assert owner.calls == []
    finally:
        stream.close()


def test_one_hardware_failure_does_not_change_sibling_generation():
    failures_a = []
    failures_b = []
    stream_a, backend_a, clock_a = make_ready_stream(failures=failures_a)
    stream_b, backend_b, clock_b = make_ready_stream(
        failures=failures_b, generation=8
    )
    try:
        stream_a.report_backend_failure(RuntimeError("gone"), timeout=1.0)
        assert stream_a.snapshot().state is StreamState.FAILED
        assert stream_b.snapshot().state is StreamState.READY
        assert stream_b.generation_latch.alive
        clock_b.advance(0.05)
        valid_block(backend_b.handle, clock_b)
        wait_stream(stream_b)
        assert stream_b.snapshot().cursor == 10
        assert failures_a
        assert failures_b == []
    finally:
        stream_a.close()
        stream_b.close()


def test_stale_callbacks_raw_blocks_tokens_and_commands_are_inert():
    old_stream, old_backend, old_clock = make_ready_stream()
    old_token = old_stream.current_ready_token
    old_stream.close()
    replacement, new_backend, new_clock = make_ready_stream(generation=5)
    owner = StubOwner("run-new", "attempt-new", "cap-new")
    owner.stream_generation = 5
    try:
        old_clock.advance(0.05)
        valid_block(old_backend.handle, old_clock)
        assert old_stream.generation_latch.dropped_callbacks == 1
        assert not replacement.prepare_owner(
            owner, identity(owner, generation=3), timeout=1.0
        )
        assert not replacement.token_matches(old_token)
        assert replacement.snapshot().cursor == 5
        assert replacement.generation_latch.alive
        assert owner.calls == []
        new_clock.advance(0.05)
        valid_block(new_backend.handle, new_clock)
        wait_stream(replacement)
        assert replacement.snapshot().cursor == 10
    finally:
        replacement.close()


def test_retry_backoff_sequence_caps_and_ready_reset():
    retry = RetryBackoff()
    assert [retry.next_delay() for _ in range(7)] == [
        0.5,
        1.0,
        2.0,
        5.0,
        10.0,
        10.0,
        10.0,
    ]
    retry.reset()
    assert retry.next_delay() == 0.5
    retry.cancel()
    assert retry.next_delay() is None


def test_stream_readiness_resets_shared_per_hardware_backoff():
    retry = RetryBackoff()
    assert retry.next_delay() == 0.5
    assert retry.next_delay() == 1.0
    stream, _, _ = make_ready_stream(backoff=retry)
    try:
        assert retry.next_delay() == 0.5
    finally:
        stream.close()
