from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app.service.analysis.resident_audio_mvp import (
    BackendCallbackDirective,
    BackendCallbackStatus,
    BackendCloseResult,
    BackendCloseStatus,
    CaptureOutcomeStatus,
    DiagnosticReason,
    DiagnosticStage,
    LifecycleStatus,
    ResidentAudioPool,
    ResidentError,
    ResidentErrorCode,
    ResidentRequestError,
    ResidentStreamSpec,
)
from app.service.managed_input_contracts import (
    BackendScanId,
    HardwareEndpointIdentity,
    ManagedHardwareId,
)
from tests.resident_audio_mvp.fakes import FakeInputHandle, ManualMonotonicClock


def spec(hardware_id: str = "mic-1", *, device_index: int = 2) -> ResidentStreamSpec:
    return ResidentStreamSpec(
        hardware_id=hardware_id,
        device_index=device_index,
        device_name=f"Exact {hardware_id}",
        hostapi_index=0,
        hostapi_name="WASAPI",
        sample_rate_hz=1_000,
        input_channel_count=1,
        capture_channel_indices=(0,),
        sample_format="float32",
        frames_per_buffer=10,
    )


def endpoint(hardware_value: int = 1, *, device_index: int = 2) -> HardwareEndpointIdentity:
    return HardwareEndpointIdentity(
        hardware_id=ManagedHardwareId(hardware_value),
        scan_id=BackendScanId("recovery-test", 1),
        device_index=device_index,
        hostapi_index=0,
        stable_backend_uid=f"uid-{hardware_value}",
        device_name=f"Exact mic-{hardware_value}",
        hostapi_name="WASAPI",
        sample_rate_hz=1_000.0,
        input_channels=1,
    )


class StaticResolver:
    def __init__(self) -> None:
        self.calls: list[HardwareEndpointIdentity] = []

    def resolve(self, frozen: HardwareEndpointIdentity) -> HardwareEndpointIdentity:
        self.calls.append(frozen)
        return HardwareEndpointIdentity(
            hardware_id=frozen.hardware_id,
            scan_id=BackendScanId(
                frozen.scan_id.process_instance_id,
                frozen.scan_id.sequence + len(self.calls),
            ),
            device_index=frozen.device_index + len(self.calls),
            hostapi_index=frozen.hostapi_index,
            stable_backend_uid=frozen.stable_backend_uid,
            device_name=frozen.device_name,
            hostapi_name=frozen.hostapi_name,
            sample_rate_hz=frozen.sample_rate_hz,
            input_channels=frozen.input_channels,
        )


class SequencedBackend:
    def __init__(self, clock: ManualMonotonicClock) -> None:
        self.clock = clock
        self.handles: list[FakeInputHandle] = []
        self.start_actions: list[object] = []
        self.close_results: list[BackendCloseResult] = []

    def create_input(self, stream_spec, callback, on_fault):
        position = len(self.handles)
        action = (
            self.start_actions[position]
            if position < len(self.start_actions)
            else lambda handle: handle.emit_valid()
        )
        close_result = (
            self.close_results[position]
            if position < len(self.close_results)
            else BackendCloseResult(BackendCloseStatus.CLOSED)
        )
        handle = FakeInputHandle(
            stream_spec,
            callback,
            on_fault,
            clock=self.clock,
            start_action=action,
            close_result=close_result,
        )
        self.handles.append(handle)
        return handle


def open_recoverable(
    backend: SequencedBackend,
    resolver: StaticResolver,
    clock: ManualMonotonicClock,
    specs: tuple[ResidentStreamSpec, ...] = (spec(),),
    drain_observer=None,
):
    identities = {
        item.hardware_id: endpoint(index + 1, device_index=item.device_index)
        for index, item in enumerate(specs)
    }
    return ResidentAudioPool.open(
        specs,
        backend=backend,
        max_capture_frames=100,
        startup_stream_timeout_s=0.2,
        fault_close_timeout_s=0.2,
        shutdown_timeout_s=0.5,
        endpoint_identities=identities,
        endpoint_resolver=resolver,
        drain_observer=drain_observer,
        monotonic_ns=clock,
    ).pool


def wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.001)


def test_healthy_readiness_is_immediate_and_does_not_start_lifecycle() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)

    ticket = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    outcome = ticket.future.result_if_ready()

    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
    assert len(backend.handles) == 1
    assert resolver.calls == []


def test_reserve_can_pin_the_continuous_generation_without_racing_replacement() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.result_if_ready()

    with pytest.raises(ResidentRequestError) as stale:
        pool.reserve_capture(
            "run-stale",
            "mic-1",
            generation_id="mic-1:generation-999",
        )
    assert stale.value.error.code is ResidentErrorCode.NOT_READY

    binding = pool.reserve_capture(
        "run-current",
        "mic-1",
        generation_id=outcome.generation_id,
    )
    assert binding.hardware_id == "mic-1"


def test_stale_generation_can_prove_continuity_within_observation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    original = backend.handles[0]
    clock.advance_seconds(2)
    observation_started = threading.Event()
    slot = pool._slots["mic-1"]
    wait_for_callback_after = type(slot).wait_for_callback_after

    def observed_wait(owner, sequence: int, timeout_seconds: float) -> bool:
        if owner is slot:
            observation_started.set()
        return wait_for_callback_after(owner, sequence, timeout_seconds)

    monkeypatch.setattr(type(slot), "wait_for_callback_after", observed_wait)

    ticket = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    assert observation_started.wait(0.5)
    original.emit_valid()
    outcome = ticket.future.wait(0.5)

    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
    assert len(backend.handles) == 1


def test_stale_continuity_uses_one_shared_observation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service.analysis.resident_audio_mvp.stream import (
        _ResidentStreamSlot,
    )

    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    clock.advance_seconds(2)
    observation_started_ns = clock()
    observed_waits: list[tuple[str, float]] = []

    def wait_for_callback(
        _slot: _ResidentStreamSlot,
        _sequence: int,
        timeout_s: float,
    ) -> bool:
        observed_waits.append(("callback", timeout_s))
        clock.advance_seconds(0.75)
        return True

    def wait_for_drain(
        _slot: _ResidentStreamSlot,
        _sequence: int,
        timeout_s: float,
    ) -> bool:
        observed_waits.append(("drain", timeout_s))
        clock.advance_seconds(0.20)
        return True

    monkeypatch.setattr(
        _ResidentStreamSlot,
        "wait_for_callback_after",
        wait_for_callback,
    )
    monkeypatch.setattr(
        _ResidentStreamSlot,
        "wait_for_drain_after",
        wait_for_drain,
    )

    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
    assert observed_waits[0] == ("callback", 1.0)
    assert observed_waits[1][0] == "drain"
    assert observed_waits[1][1] == pytest.approx(0.25)
    assert clock() - observation_started_ns <= 1_000_000_000
    assert len(backend.handles) == 1


def test_stale_callback_progress_is_not_continuity_until_ring_accepts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    original = backend.handles[0]
    clock.advance_seconds(2)
    observation_started = threading.Event()
    slot_type = type(pool._slots["mic-1"])
    original_wait = slot_type.wait_for_callback_after

    def observed_wait(slot, sequence: int, timeout_seconds: float) -> bool:
        observation_started.set()
        return original_wait(slot, sequence, timeout_seconds)

    monkeypatch.setattr(slot_type, "wait_for_callback_after", observed_wait)

    ticket = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    assert observation_started.wait(0.5)
    original.emit(
        np.zeros((10, 1), dtype=np.float32),
        callback_monotonic_ns=900_000_000,
    )
    outcome = ticket.future.wait(1.5)

    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_REPLACED
    assert len(backend.handles) == 2


def test_fault_replaces_entire_generation_and_old_claim_fails_next_claim_succeeds() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    candidate_start = threading.Event()

    def start_candidate(handle: FakeInputHandle) -> None:
        candidate_start.set()
        handle.emit_valid()

    backend.start_actions = [lambda handle: handle.emit_valid(), start_candidate]
    pool = open_recoverable(backend, resolver, clock)
    original = backend.handles[0]

    original.fault(summary="runtime cable fault")
    ticket = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    outcome = ticket.future.wait(1)

    assert candidate_start.is_set()
    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_REPLACED
    assert outcome.diagnostic.reason_code is DiagnosticReason.GENERATION_REPLACED_FOR_TRIGGER
    assert len(backend.handles) == 2
    replacement = backend.handles[1]
    assert replacement is not original
    assert replacement.callback is not original.callback
    assert replacement.spec.device_index == 3
    assert original.emit_valid() is BackendCallbackDirective.STOP

    next_ticket = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    next_outcome = next_ticket.future.result_if_ready()
    assert next_outcome is not None
    assert next_outcome.status is LifecycleStatus.GENERATION_CONTINUOUS
    assert next_outcome.generation_id == outcome.generation_id


def test_startup_and_replacement_generations_share_one_post_drain_observer() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    observed: list[tuple[str, np.ndarray, int, int, int, str]] = []

    def observer(hardware_id, samples, frame_count, callback_ns, sample_rate):
        observed.append(
            (
                hardware_id,
                np.asarray(samples).copy(),
                frame_count,
                callback_ns,
                sample_rate,
                threading.current_thread().name,
            )
        )

    pool = open_recoverable(
        backend,
        resolver,
        clock,
        drain_observer=observer,
    )
    wait_for(lambda: len(observed) == 1)
    original = backend.handles[0]

    original.fault(summary="replace this generation")
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(1)

    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_REPLACED
    wait_for(lambda: len(observed) == 2)
    assert [item[0] for item in observed] == ["mic-1", "mic-1"]
    assert [item[2] for item in observed] == [10, 10]
    assert [item[4] for item in observed] == [1_000, 1_000]
    assert all(item[5] == "resident-audio-mic-1" for item in observed)
    assert backend.handles[1] is not original
    assert original.emit_valid() is BackendCallbackDirective.STOP


def test_drain_observer_failure_never_faults_the_generation() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    observer_entered = threading.Event()

    def failing_observer(*_args) -> None:
        observer_entered.set()
        raise RuntimeError("display failed")

    pool = open_recoverable(
        backend,
        resolver,
        clock,
        drain_observer=failing_observer,
    )

    assert observer_entered.wait(0.5)
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.result_if_ready()
    assert outcome is not None
    assert outcome.status is LifecycleStatus.GENERATION_CONTINUOUS


def test_runtime_fault_settles_each_affected_capture_owner_exactly_once() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    owners = [pool.reserve_capture(f"run-{index}", "mic-1") for index in range(2)]
    completions = [
        pool.start_capture(owner, trigger_monotonic_ns=clock(), target_frame_count=50)
        for owner in owners
    ]

    backend.handles[0].emit_status(BackendCallbackStatus.INPUT_OVERFLOW)
    outcomes = [completion.wait(0.5) for completion in completions]

    assert all(item is not None for item in outcomes)
    assert [item.status for item in outcomes] == [
        CaptureOutcomeStatus.FAILED,
        CaptureOutcomeStatus.FAILED,
    ]
    assert [completion.result_if_ready() for completion in completions] == outcomes
    wait_for(lambda: len(backend.handles) == 2)


def test_same_hardware_coalesces_recovery_while_other_hardware_stays_immediate() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    release_candidate = threading.Event()

    def blocked_start(handle: FakeInputHandle) -> None:
        release_candidate.wait(0.5)
        handle.emit_valid()

    backend.start_actions = [
        lambda handle: handle.emit_valid(),
        lambda handle: handle.emit_valid(),
        blocked_start,
    ]
    pool = open_recoverable(
        backend,
        resolver,
        clock,
        specs=(spec("mic-1"), spec("mic-2", device_index=4)),
    )
    backend.handles[0].fault()
    first = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    second = pool.begin_readiness("mic-1", clock() + 5_000_000_000)
    healthy = pool.begin_readiness("mic-2", clock() + 5_000_000_000)

    assert first.future is second.future
    assert healthy.future.result_if_ready().status is LifecycleStatus.GENERATION_CONTINUOUS
    release_candidate.set()
    assert first.future.wait(1).status is LifecycleStatus.GENERATION_REPLACED
    assert len(backend.handles) == 3


def test_close_unconfirmed_is_orphaned_without_reusing_old_handle() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    backend.close_results = [
        BackendCloseResult(
            BackendCloseStatus.NOT_CONFIRMED,
            ResidentError(
                ResidentErrorCode.NON_CLEAN,
                "native close unconfirmed",
            ),
        )
    ]
    pool = open_recoverable(backend, resolver, clock)
    old = backend.handles[0]

    old.fault()
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(1)

    assert outcome.status is LifecycleStatus.GENERATION_REPLACED
    assert len(backend.handles) == 2
    assert backend.handles[1] is not old
    assert pool.orphan_counts() == (1, {"mic-1": 1})


def test_second_fault_rearms_lifecycle_while_prior_orphan_cleanup_owns_worker() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    unresolved = BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(
            ResidentErrorCode.NON_CLEAN,
            "generation one close remains unresolved",
        ),
    )
    backend.close_results = [unresolved]
    pool = open_recoverable(backend, resolver, clock)

    backend.handles[0].fault()
    first_ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )
    first = first_ticket.future.wait(1)
    assert first.status is LifecycleStatus.GENERATION_REPLACED
    assert first.generation_id == "mic-1:generation-2"
    assert pool.orphan_counts() == (1, {"mic-1": 1})
    wait_for(
        lambda: pool._recovery_states["mic-1"].worker is not None
    )

    backend.handles[1].fault()
    second_ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )

    assert second_ticket.future is not first_ticket.future
    assert second_ticket.future.result_if_ready() is None
    second = second_ticket.future.wait(1)
    assert second.status is LifecycleStatus.GENERATION_REPLACED
    assert second.generation_id == "mic-1:generation-3"
    assert len(backend.handles) == 3
    assert pool.orphan_counts() == (1, {"mic-1": 1})


def test_failed_start_unconfirmed_close_is_owned_before_replacement_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service.analysis.resident_audio_mvp import pool as pool_module

    monkeypatch.setattr(pool_module, "retry_delay", lambda _attempt: 0.01)
    clock = ManualMonotonicClock()
    pool_holder: dict[str, ResidentAudioPool] = {}

    class OrphanAssertingBackend(SequencedBackend):
        def create_input(self, stream_spec, callback, on_fault):
            if self.handles:
                assert pool_holder["pool"].orphan_counts() == (
                    1,
                    {"mic-1": 1},
                )
            return super().create_input(stream_spec, callback, on_fault)

    backend = OrphanAssertingBackend(clock)
    resolver = StaticResolver()
    unresolved = BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(ResidentErrorCode.NON_CLEAN, "startup close unresolved"),
    )

    def fail_start(_handle: FakeInputHandle) -> None:
        raise OSError("start failed")

    backend.start_actions = [fail_start, fail_start]
    backend.close_results = [unresolved, unresolved]
    pool = open_recoverable(backend, resolver, clock)
    pool_holder["pool"] = pool

    assert len(backend.handles) == 1
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert outcome.status is LifecycleStatus.RECOVERY_FAILED
    wait_for(lambda: pool.orphan_counts()[0] == 2)
    time.sleep(0.05)
    assert pool.orphan_counts() == (2, {"mic-1": 2})
    assert len(backend.handles) == 2


def test_failed_start_confirmed_close_does_not_create_orphan() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()

    def fail_start(_handle: FakeInputHandle) -> None:
        raise OSError("start failed")

    backend.start_actions = [fail_start]
    pool = open_recoverable(backend, resolver, clock)
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert outcome.status is LifecycleStatus.GENERATION_REPLACED
    assert pool.orphan_counts() == (0, {})


def test_candidate_start_and_first_sample_failures_keep_precise_stage() -> None:
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()

    def fail_start(_handle: FakeInputHandle) -> None:
        raise OSError("candidate start failed")

    backend.start_actions = [lambda handle: handle.emit_valid(), fail_start]
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    start_failure = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert start_failure.status is LifecycleStatus.RECOVERY_FAILED
    assert start_failure.diagnostic.reason_code is DiagnosticReason.START_FAILED

    clock2 = ManualMonotonicClock()
    backend2 = SequencedBackend(clock2)
    resolver2 = StaticResolver()

    def no_first_sample(_handle: FakeInputHandle) -> None:
        clock2.advance_seconds(5)

    backend2.start_actions = [lambda handle: handle.emit_valid(), no_first_sample]
    pool2 = open_recoverable(backend2, resolver2, clock2)
    backend2.handles[0].fault()
    timeout = pool2.begin_readiness(
        "mic-1", clock2() + 5_000_000_000
    ).future.wait(0.5)

    assert timeout.status is LifecycleStatus.RECOVERY_PENDING
    assert timeout.diagnostic.reason_code is DiagnosticReason.FIRST_SAMPLE_TIMEOUT


def test_candidate_open_failure_is_not_reported_as_no_samples() -> None:
    class OpenFailureBackend(SequencedBackend):
        def create_input(self, stream_spec, callback, on_fault):
            if self.handles:
                raise OSError("candidate open failed")
            return super().create_input(stream_spec, callback, on_fault)

    clock = ManualMonotonicClock()
    backend = OpenFailureBackend(clock)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()

    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert outcome.status is LifecycleStatus.RECOVERY_FAILED
    assert outcome.diagnostic.reason_code is DiagnosticReason.OPEN_FAILED
    assert "sample" not in outcome.diagnostic.detail.lower()


def test_managed_blocked_candidate_open_maps_to_open_timeout() -> None:
    from app.service.analysis.resident_audio_mvp.sounddevice_backend import (
        _NativeLifecycleState,
    )
    from app.service.managed_input_sounddevice_backend import (
        MonotonicSoundDeviceAudioBackend,
    )

    class Native:
        def start(self) -> None:
            raise AssertionError("timed-out open must not start")

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    class BlockingSoundDevice:
        CallbackStop = RuntimeError

        def __init__(self, clock: ManualMonotonicClock) -> None:
            self.clock = clock
            self.native = Native()
            self.construction_release = threading.Event()

        def InputStream(self, **_kwargs):
            self.clock.advance_seconds(5)
            assert self.construction_release.wait(1)
            return self.native

    class HybridBackend(SequencedBackend):
        def __init__(
            self,
            clock: ManualMonotonicClock,
            managed_backend: MonotonicSoundDeviceAudioBackend,
        ) -> None:
            super().__init__(clock)
            self.managed_backend = managed_backend

        def create_input(self, stream_spec, callback, on_fault):
            if not self.handles:
                return super().create_input(stream_spec, callback, on_fault)
            handle = self.managed_backend.create_input(
                stream_spec,
                callback,
                on_fault,
            )
            self.handles.append(handle)
            return handle

    clock = ManualMonotonicClock()
    clock.monotonic_domain = "python/system-monotonic"
    clock.monotonic = lambda: clock() / 1_000_000_000
    clock.monotonic_ns = clock.__call__
    sounddevice = BlockingSoundDevice(clock)
    managed_backend = MonotonicSoundDeviceAudioBackend(sounddevice, clock)
    backend = HybridBackend(clock, managed_backend)
    resolver = StaticResolver()
    pool = open_recoverable(backend, resolver, clock)

    backend.handles[0].fault()
    outcome = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)
    candidate = backend.handles[1]

    assert outcome.status is LifecycleStatus.RECOVERY_PENDING
    assert outcome.diagnostic.reason_code is DiagnosticReason.OPEN_TIMEOUT
    assert outcome.diagnostic.reason_code is not DiagnosticReason.START_TIMEOUT
    assert candidate.start_failure_stage == "open"
    assert candidate.lifecycle_state is _NativeLifecycleState.CALL_TIMED_OUT
    sounddevice.construction_release.set()
    wait_for(
        lambda: candidate.lifecycle_state is _NativeLifecycleState.CLOSED
    )


def test_timed_out_endpoint_scan_is_bounded_coalesced_and_reenumerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service.managed_input_contracts import (
        InputEndpointDescriptor,
        InputEndpointObservation,
        RawBackendInputScan,
        RawHostApiIdentity,
        StableUidCapability,
        StableUidContract,
    )
    from app.service.managed_input_endpoint_resolver import (
        ManagedInputEndpointResolver,
    )

    clock = ManualMonotonicClock()

    class BlockingScans:
        def __init__(self) -> None:
            self.calls: list[BackendScanId] = []
            self.first_entered = threading.Event()
            self.advance_to_timeout = threading.Event()
            self.first_release = threading.Event()
            self.first_returned = threading.Event()

        def query_raw_input_endpoints(
            self,
            scan_id: BackendScanId,
        ) -> RawBackendInputScan:
            self.calls.append(scan_id)
            if len(self.calls) == 1:
                self.first_entered.set()
                assert self.advance_to_timeout.wait(1)
                clock.advance_seconds(5)
                assert self.first_release.wait(1)
                self.first_returned.set()
            descriptor = InputEndpointDescriptor(
                "Exact mic-1",
                "WASAPI",
                1_000.0,
                1,
            )
            return RawBackendInputScan(
                scan_id=scan_id,
                complete=True,
                stable_uid_capability=StableUidCapability.SUPPORTED,
                stable_uid_contract=StableUidContract.REVIEWED_OPAQUE_UID,
                hostapis=(RawHostApiIdentity(0, "WASAPI"),),
                observations=(
                    InputEndpointObservation(
                        scan_id=scan_id,
                        observation_id=(
                            f"{scan_id.process_instance_id}:"
                            f"{scan_id.sequence}:3"
                        ),
                        device_index=2 + len(self.calls),
                        hostapi_index=0,
                        stable_backend_uid="uid-1",
                        descriptor=descriptor,
                    ),
                ),
            )

    scans = BlockingScans()
    resolver = ManagedInputEndpointResolver(scans, monotonic_ns=clock)
    monkeypatch.setattr(
        "app.service.analysis.resident_audio_mvp.pool.retry_delay",
        lambda _attempt: 10.0,
    )
    backend = SequencedBackend(clock)
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    assert scans.first_entered.wait(0.5)
    ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )
    scans.advance_to_timeout.set()
    outcome = ticket.future.wait(0.5)

    assert outcome.status is LifecycleStatus.RECOVERY_PENDING
    assert outcome.diagnostic.reason_code is DiagnosticReason.ENDPOINT_SCAN_FAILED
    assert "timed out" in outcome.diagnostic.detail
    background_futures = {
        pool.begin_readiness(
            "mic-1", clock() + 5_000_000_000
        ).future
        for _ in range(100)
    }
    assert len(background_futures) == 1
    background = background_futures.pop()
    assert background.result_if_ready() is None
    assert len(scans.calls) == 1

    scans.first_release.set()
    assert scans.first_returned.wait(0.5)
    pool._recovery_states["mic-1"].wake.set()
    recovered = background.wait(1)

    assert recovered.status is LifecycleStatus.GENERATION_REPLACED
    assert len(scans.calls) == 2
    assert scans.calls[1].sequence == scans.calls[0].sequence + 1
    assert backend.handles[1].spec.device_index == 4


def test_foreground_timeout_continues_background_retry_and_success_resets(
    monkeypatch,
) -> None:
    from app.service.analysis.resident_audio_mvp import pool as pool_module

    retry_attempts: list[int] = []

    def immediate_retry(attempt: int) -> float:
        retry_attempts.append(attempt)
        return 0.01

    monkeypatch.setattr(pool_module, "retry_delay", immediate_retry)
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()

    def no_first_sample(_handle: FakeInputHandle) -> None:
        clock.advance_seconds(5)

    backend.start_actions = [
        lambda handle: handle.emit_valid(),
        no_first_sample,
        lambda handle: handle.emit_valid(),
    ]
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    foreground = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert foreground.status is LifecycleStatus.RECOVERY_PENDING
    wait_for(lambda: len(backend.handles) == 3)
    next_outcome = None
    deadline = time.monotonic() + 1.0
    while next_outcome is None or next_outcome.status is not LifecycleStatus.GENERATION_CONTINUOUS:
        next_outcome = pool.begin_readiness(
            "mic-1", clock() + 5_000_000_000
        ).future.result_if_ready()
        if time.monotonic() >= deadline:
            raise AssertionError("background recovery did not publish")
        time.sleep(0.001)
    assert retry_attempts[0] == 0


def test_background_retry_gets_fresh_attempt_deadline_after_long_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service.analysis.resident_audio_mvp import pool as pool_module

    clock = ManualMonotonicClock()

    class RecoveringResolver(StaticResolver):
        def __init__(self) -> None:
            super().__init__()
            self.healthy = False
            self.unhealthy_entered = threading.Event()
            self.release_unhealthy = threading.Event()

        def resolve(
            self,
            frozen: HardwareEndpointIdentity,
        ) -> HardwareEndpointIdentity:
            if not self.healthy:
                self.calls.append(frozen)
                self.unhealthy_entered.set()
                assert self.release_unhealthy.wait(1)
                raise OSError("endpoint is temporarily unavailable")
            return super().resolve(frozen)

    resolver = RecoveringResolver()
    retry_attempts: list[int] = []

    def accelerated_long_retry(attempt: int) -> float:
        retry_attempts.append(attempt)
        resolver.healthy = True
        clock.advance_seconds(6)
        return 0.05

    monkeypatch.setattr(pool_module, "retry_delay", accelerated_long_retry)
    backend = SequencedBackend(clock)
    start_windows: list[tuple[int, int]] = []

    def reject_expired_attempt(handle: FakeInputHandle) -> None:
        deadline_ns = handle.start_deadlines[-1]
        start_windows.append((clock(), deadline_ns))
        if deadline_ns <= clock():
            raise ValueError("production handle rejected expired deadline")
        handle.emit_valid()

    backend.start_actions = [
        lambda handle: handle.emit_valid(),
        reject_expired_attempt,
        reject_expired_attempt,
        reject_expired_attempt,
    ]
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    assert resolver.unhealthy_entered.wait(0.5)
    first_ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )
    resolver.release_unhealthy.set()
    first = first_ticket.future.wait(0.5)

    assert first.status is LifecycleStatus.RECOVERY_FAILED
    assert first.diagnostic.reason_code is DiagnosticReason.ENDPOINT_SCAN_FAILED
    background = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future
    assert background.result_if_ready() is None
    assert background.wait(0.005) is None
    recovered = background.wait(1)

    assert recovered.status is LifecycleStatus.GENERATION_REPLACED
    assert retry_attempts == [0]
    assert len(start_windows) == 1
    attempt_started_ns, attempt_deadline_ns = start_windows[0]
    assert attempt_deadline_ns == attempt_started_ns + 5_000_000_000


def test_retirement_budget_exhaustion_does_not_start_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service.analysis.resident_audio_mvp import pool as pool_module

    monkeypatch.setattr(pool_module, "retry_delay", lambda _attempt: 10.0)
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()

    def reject_expired_candidate(handle: FakeInputHandle) -> None:
        if handle.start_deadlines[-1] <= clock():
            raise ValueError("expired candidate deadline")
        handle.emit_valid()

    backend.start_actions = [
        lambda handle: handle.emit_valid(),
        reject_expired_candidate,
    ]
    pool = open_recoverable(backend, resolver, clock)
    old = backend.handles[0]
    close_entered = threading.Event()
    release_close = threading.Event()

    def consume_foreground_budget(deadline_ns: int) -> BackendCloseResult:
        old.close_deadlines.append(deadline_ns)
        close_entered.set()
        assert release_close.wait(1)
        clock.advance_seconds(5)
        return BackendCloseResult(BackendCloseStatus.CLOSED)

    old.close = consume_foreground_budget  # type: ignore[method-assign]
    old.fault()
    assert close_entered.wait(0.5)
    ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )
    release_close.set()
    outcome = ticket.future.wait(0.5)

    assert outcome.status is LifecycleStatus.RECOVERY_PENDING
    assert outcome.diagnostic.stage is DiagnosticStage.RETIRE_OLD
    assert outcome.diagnostic.reason_code is DiagnosticReason.CLOSE_TIMEOUT
    assert len(backend.handles) == 1
    assert resolver.calls == []
    background = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future
    assert background is not ticket.future
    assert background.result_if_ready() is None
    assert pool._recovery_states["mic-1"].worker is not None
    pool.shutdown(deadline_monotonic_ns=clock() + 1_000_000_000)


def test_endpoint_resolution_budget_exhaustion_does_not_create_candidate() -> None:
    clock = ManualMonotonicClock()

    class DeadlineConsumingResolver(StaticResolver):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def resolve(
            self,
            frozen: HardwareEndpointIdentity,
        ) -> HardwareEndpointIdentity:
            self.entered.set()
            assert self.release.wait(1)
            clock.advance_seconds(5)
            return super().resolve(frozen)

    resolver = DeadlineConsumingResolver()
    backend = SequencedBackend(clock)

    def reject_expired_candidate(handle: FakeInputHandle) -> None:
        if handle.start_deadlines[-1] <= clock():
            raise ValueError("expired candidate deadline")
        handle.emit_valid()

    backend.start_actions = [
        lambda handle: handle.emit_valid(),
        reject_expired_candidate,
    ]
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    assert resolver.entered.wait(0.5)
    ticket = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    )
    resolver.release.set()
    outcome = ticket.future.wait(0.5)

    assert outcome.status is LifecycleStatus.RECOVERY_PENDING
    assert outcome.diagnostic.stage is DiagnosticStage.OPEN_CANDIDATE
    assert outcome.diagnostic.reason_code is DiagnosticReason.OPEN_TIMEOUT
    assert len(backend.handles) == 1


def test_retry_policy_is_one_two_five_ten_then_thirty() -> None:
    from app.service.analysis.resident_audio_mvp.recovery import retry_delay

    assert [retry_delay(index) for index in range(7)] == [
        1.0,
        2.0,
        5.0,
        10.0,
        30.0,
        30.0,
        30.0,
    ]


def test_orphan_cap_blocks_a_third_native_handle(monkeypatch) -> None:
    from app.service.analysis.resident_audio_mvp import pool as pool_module

    monkeypatch.setattr(pool_module, "retry_delay", lambda _attempt: 0.01)
    clock = ManualMonotonicClock()
    backend = SequencedBackend(clock)
    resolver = StaticResolver()
    unresolved = BackendCloseResult(
        BackendCloseStatus.NOT_CONFIRMED,
        ResidentError(ResidentErrorCode.NON_CLEAN, "still unresolved"),
    )

    def fail_start(_handle: FakeInputHandle) -> None:
        raise OSError("candidate start failed")

    backend.start_actions = [lambda handle: handle.emit_valid(), fail_start]
    backend.close_results = [unresolved, unresolved]
    pool = open_recoverable(backend, resolver, clock)
    backend.handles[0].fault()
    first = pool.begin_readiness(
        "mic-1", clock() + 5_000_000_000
    ).future.wait(0.5)

    assert first.status is LifecycleStatus.RECOVERY_FAILED
    wait_for(lambda: pool.orphan_counts()[0] == 2)
    time.sleep(0.05)
    assert len(backend.handles) == 2
    assert pool.orphan_counts() == (2, {"mic-1": 2})
