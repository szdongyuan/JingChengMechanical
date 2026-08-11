from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading
from collections import deque
from uuid import uuid4
import wave
import json

import numpy as np
import pytest
from app.service.current_meter_service import CurrentSample
import app.service.multi_run_coordinator_mvp as package

from app.service.analysis.scheduler import (
    AnalysisCancelOrigin,
    AnalysisExecutionResult,
    AnalysisExecutionStatus,
    FrozenResultObject,
)
from app.service.managed_input_contracts import ManagedHardwareId
from app.service.multi_run_coordinator_mvp import (
    AudioMetadataConstraints,
    AudioSaveOutcome,
    AudioSaveSourceKind,
    AudioSaveStatus,
    CommitResult,
    CapturedAudioSaveRequest,
    CapturedStartRequest,
    FinalPathRequest,
    FrozenCapturedRun,
    FrozenImportedRun,
    HistoryEvictionNotice,
    ImportedAudioSaveRequest,
    ImportedSourceFormat,
    ImportedSourceReference,
    ImportedStartRequest,
    ManagedResidentCaptureCloseStatus,
    ManagedResidentCapturePort,
    ManagedResidentCaptureRoute,
    MultiRunCoordinator,
    OperationStatus,
    PersistedAudioFormat,
    PersistedAudioReference,
    ResidentCancelResult,
    ResidentCancelStatus,
    SaveCancelResult,
    SaveCancelStatus,
    SaveDispatcherShutdownReport,
    SaveDispatcherShutdownStatus,
    SaveSubmission,
    SaveSubmitStatus,
    StartStatus,
    TerminalErrorCategory,
    TriggerSource,
)
from app.service.plc_result_sequencer import PlcResultSequencer
from app.service.run_persistence import (
    AudioPublication,
    AudioRepository,
    AudioPersistenceService,
    BridgeDatabaseCloseStatus,
    BridgeSchedulerShutdownStatus,
    CapturedAudioSaveRequest as PersistenceCapturedAudioSaveRequest,
    ImportedAudioSaveRequest as PersistenceImportedAudioSaveRequest,
    IngressKind,
    ProductionAudioStatus,
    ProductionRunAudioAdapter,
    RunInsert,
    RunPersistenceDatabase,
    RunRepository,
    TransactionState,
    TriggerSource as PersistenceTriggerSource,
    freeze_run_inputs,
    settle_startup,
)
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from tests.multi_run_coordinator_mvp._support import (
    ATTEMPT_IDS,
    RUN_IDS,
    FakeCapturedFreezer,
    FakeDiagnostics,
    FakeHistoryProjector,
    FakeHistorySink,
    FakeCurrentCollector,
    captured_request,
    completed_capture,
    frozen_captured,
    saved_outcome,
    run_config_payload,
    analysis_input_payload,
    payload,
    Harness,
)
from tests.multi_run_coordinator_mvp.test_result_operations import (
    manual_request,
    reanalysis_report,
    reanalysis_request,
    result_facts,
)
from tests.resident_audio_mvp.fakes import FakeBackend
from tests.run_persistence.conftest import make_analysis_envelope
from tests.run_persistence.test_scheduler_bridge_end_to_end import (
    ACCEPTED_AT,
    SAVED_AT,
    _build_real_stack,
)
from tests.test_managed_input_resident_adapter import endpoint, make_adapter


def _write_import(path: Path) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setframerate(8_000)
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.writeframes(b"\x00\x00\x01\x00\x02\x00\x03\x00")


def _start_enabled_current(
    harness: Harness,
    *,
    run_id: str = RUN_IDS[0],
    samples: list[CurrentSample] | None = None,
    channel_order: tuple[int, ...] = (0,),
) -> tuple[MultiRunCoordinator, FakeCurrentCollector]:
    collector = FakeCurrentCollector(harness.trace, samples=samples)
    harness.current_factory.collectors.append(collector)
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
        channel_order=channel_order,
    )
    coordinator = harness.coordinator()
    assert coordinator.start_captured(captured_request(run_id)).accepted
    return coordinator, collector


def test_failed_or_canceled_current_cleanup_preserves_terminal_and_latches_restart() -> None:
    for index, (status, category, failing_stage) in enumerate(
        (
            (
                package.CaptureOutcomeStatus.FAILED,
                TerminalErrorCategory.CAPTURE_FAILED,
                "stop",
            ),
            (
                package.CaptureOutcomeStatus.CANCELLED,
                TerminalErrorCategory.CAPTURE_CANCELLED,
                "samples",
            ),
        )
    ):
        harness = Harness()
        coordinator, collector = _start_enabled_current(
            harness,
            run_id=RUN_IDS[index],
            samples=[CurrentSample(0.0, 1.0)],
        )
        if failing_stage == "stop":
            collector.stop_exception = TimeoutError("stop timed out")
        else:
            collector.samples_exception = RuntimeError("samples failed")
        reservation = harness.resident.register_calls[0]
        harness.resident.complete(
            package.CaptureOutcome(
                run_id=RUN_IDS[index],
                reservation=reservation,
                status=status,
                audio=None,
                error_category=category,
                reason="capture terminal",
            )
        )

        assert harness.persistence.terminal_calls[-1].error_category is category
        assert harness.persistence.terminal_calls[-1].reason == "capture terminal"
        assert len(collector.stop_calls) == 1
        assert collector.samples_calls == 1
        assert harness.persistence.current_calls == []
        assert harness.save_dispatcher.calls == []
        assert harness.scheduler.initial_calls == []
        assert coordinator.snapshot().restart_required is True
        assert (
            coordinator.start_captured(captured_request(RUN_IDS[index + 8])).status
            is StartStatus.INGRESS_CLOSED
        )
        cleanup_diagnostics = [
            fact
            for fact in harness.diagnostics.calls
            if fact.stage == "current cleanup"
        ]
        assert len(cleanup_diagnostics) == 1


def test_capture_failure_claims_terminal_before_blocking_current_cleanup() -> None:
    harness = Harness()
    coordinator, collector = _start_enabled_current(
        harness,
        samples=[CurrentSample(0.0, 1.0)],
    )
    stop_entered = threading.Event()
    stop_release = threading.Event()
    collector.hooks["stop"].entered = stop_entered
    collector.hooks["stop"].release = stop_release
    reservation = harness.resident.register_calls[0]
    callback = threading.Thread(
        target=lambda: harness.resident.complete(
            package.CaptureOutcome(
                run_id=RUN_IDS[0],
                reservation=reservation,
                status=package.CaptureOutcomeStatus.FAILED,
                audio=None,
                error_category=TerminalErrorCategory.CAPTURE_FAILED,
                reason="capture failed exactly",
            )
        )
    )

    callback.start()
    assert stop_entered.wait(2)
    with coordinator._lock:
        context = coordinator._runs[RUN_IDS[0]]
        assert context.terminal_claimed is True

    coordinator.stop_all(0.0)
    stop_release.set()
    callback.join(2)

    assert not callback.is_alive()
    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert terminal.reason == "capture failed exactly"
    assert len(collector.stop_calls) == 1
    assert collector.samples_calls == 1
    assert harness.persistence.current_calls == []
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.initial_calls == []


def test_current_cleanup_failure_is_isolated_to_its_claimed_run() -> None:
    harness = Harness()
    first = FakeCurrentCollector(
        harness.trace,
        samples=[CurrentSample(0.0, 1.0)],
    )
    second = FakeCurrentCollector(
        harness.trace,
        samples=[CurrentSample(0.0, 2.0)],
    )
    first.stop_exception = TimeoutError("one-shot stop timed out")
    harness.current_factory.collectors.extend((first, second))
    harness.captured_freezer.result_factory = lambda request: frozen_captured(
        request,
        current_enabled=True,
    )
    coordinator = harness.coordinator()
    assert coordinator.start_captured(captured_request(RUN_IDS[0])).accepted
    assert coordinator.start_captured(captured_request(RUN_IDS[1])).accepted

    first_reservation = harness.resident.register_calls[0]
    harness.resident.complete(
        package.CaptureOutcome(
            run_id=RUN_IDS[0],
            reservation=first_reservation,
            status=package.CaptureOutcomeStatus.FAILED,
            audio=None,
            error_category=TerminalErrorCategory.CAPTURE_FAILED,
            reason="first capture failed",
        )
    )

    assert len(harness.persistence.terminal_calls) == 1
    terminal = harness.persistence.terminal_calls[0]
    assert terminal.run_id == RUN_IDS[0]
    assert terminal.error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert terminal.reason == "first capture failed"
    assert len(first.stop_calls) == 1
    assert first.samples_calls == 1
    assert second.stop_calls == []
    assert second.samples_calls == 0
    assert RUN_IDS[1] in coordinator.snapshot().accepted_active_run_ids


def test_completed_capture_settles_current_before_audio_and_scheduler() -> None:
    harness = Harness()
    harness.save_dispatcher.synchronous_outcome = saved_outcome
    _, collector = _start_enabled_current(
        harness,
        samples=[
            CurrentSample(0.0, 1.25),
            CurrentSample(0.1, 1.5),
        ],
    )
    reservation = harness.resident.register_calls[0]

    harness.resident.complete(completed_capture(reservation))

    assert len(collector.stop_calls) == 1
    assert collector.samples_calls == 1
    assert len(harness.persistence.current_calls) == 1
    trace = harness.persistence.current_calls[0][1]
    assert trace.samples == ((0.0, 1.25), (0.1, 1.5))
    assert harness.trace.index("current_stop") < harness.trace.index(
        "current_samples"
    )
    assert harness.trace.index("current_samples") < harness.trace.index(
        "attach_current"
    )
    assert harness.trace.index("attach_current") < harness.trace.index(
        "save_submit"
    )
    assert harness.trace.index("save_submit") < harness.trace.index(
        "bridge_submit_initial"
    )

    empty = Harness()
    empty.save_dispatcher.synchronous_outcome = saved_outcome
    _, empty_collector = _start_enabled_current(empty, samples=[])
    empty.resident.complete(completed_capture(empty.resident.register_calls[0]))
    assert len(empty_collector.stop_calls) == 1
    assert empty_collector.samples_calls == 1
    assert empty.persistence.current_calls == []
    assert len(empty.save_dispatcher.calls) == 1
    assert len(empty.scheduler.initial_calls) == 1


def test_channel_order_mismatch_fails_before_attach_audio_or_scheduler() -> None:
    harness = Harness()
    _, collector = _start_enabled_current(
        harness,
        samples=[CurrentSample(0.0, 1.25)],
        channel_order=(1,),
    )
    reservation = harness.resident.register_calls[0]

    harness.resident.complete(completed_capture(reservation))

    terminal = harness.persistence.terminal_calls[-1]
    assert terminal.error_category is TerminalErrorCategory.CAPTURE_FAILED
    assert terminal.reason == "channel-order mismatch"
    assert len(collector.stop_calls) == 1
    assert harness.persistence.current_calls == []
    assert harness.save_dispatcher.calls == []
    assert harness.scheduler.initial_calls == []


def test_current_attach_requires_committed_before_audio_and_scheduler() -> None:
    for index, failure in enumerate(
        (CommitResult.REJECTED, CommitResult.UNAVAILABLE, RuntimeError("attach failed"))
    ):
        harness = Harness()
        if isinstance(failure, Exception):
            harness.persistence.current_exception = failure
        else:
            harness.persistence.current_results.append(failure)
        _, collector = _start_enabled_current(
            harness,
            run_id=RUN_IDS[index],
            samples=[CurrentSample(0.0, 1.25)],
        )
        reservation = harness.resident.register_calls[0]

        harness.resident.complete(completed_capture(reservation))

        terminal = harness.persistence.terminal_calls[-1]
        assert (
            terminal.error_category
            is TerminalErrorCategory.CURRENT_TRACE_PERSIST_FAILED
        )
        assert len(collector.stop_calls) == 1
        assert len(harness.persistence.current_calls) == 1
        assert harness.save_dispatcher.calls == []
        assert harness.scheduler.initial_calls == []


class _ImportedFreezer:
    def __init__(self, source: Path, final: Path) -> None:
        self.source = source
        self.final = final

    def freeze(self, request: ImportedStartRequest) -> FrozenImportedRun:
        return FrozenImportedRun(
            run_id=request.run_id,
            trigger_monotonic_ns=request.trigger_monotonic_ns,
            source=request.source,
            config_id="composite-config",
            source_reference=ImportedSourceReference(
                source_identity="composite-import",
                absolute_path=str(self.source.resolve()),
                sample_rate_hz=8_000,
                channel_count=1,
                frame_count=4,
                format=ImportedSourceFormat.PCM16_WAV,
            ),
            config_payload=run_config_payload(),
            calibration_payload=payload("calibration"),
            work_order_payload=payload("work-order"),
            history_labels=payload("history"),
            analysis_input=analysis_input_payload((0,)),
            final_path_request=FinalPathRequest(str(self.final.resolve())),
            metadata=AudioMetadataConstraints(
                expected_sample_rate_hz=8_000,
                expected_channel_count=1,
                expected_frame_count=4,
                format=PersistedAudioFormat.PCM16_WAV,
            ),
        )


class _CapturedFreezer:
    def __init__(self, paths_by_run: dict[str, tuple[str, Path]]) -> None:
        self.paths_by_run = paths_by_run

    def freeze(self, request: CapturedStartRequest) -> FrozenCapturedRun:
        hardware_id, final = self.paths_by_run[request.run_id]
        return FrozenCapturedRun(
            run_id=request.run_id,
            trigger_monotonic_ns=request.trigger_monotonic_ns,
            source=request.source,
            config_id="composite-config",
            hardware_id=hardware_id,
            target_frame_count=2,
            config_payload=run_config_payload(),
            hardware_payload=payload("hardware"),
            calibration_payload=payload("calibration"),
            work_order_payload=payload("work-order"),
            history_labels=payload("history"),
            analysis_input=analysis_input_payload((0, 1)),
            final_path_request=FinalPathRequest(str(final.resolve())),
            metadata=AudioMetadataConstraints(
                expected_sample_rate_hz=48_000,
                expected_channel_count=2,
                expected_frame_count=2,
                format=PersistedAudioFormat.PCM16_WAV,
            ),
        )


class _CoordinatorPersistence:
    def __init__(self, runtime: RunPersistenceDatabase) -> None:
        self.runtime = runtime
        self.active_calls = []

    def create_active(self, frozen_run) -> CommitResult:
        self.active_calls.append(frozen_run)
        captured = isinstance(frozen_run, FrozenCapturedRun)
        result = RunRepository(self.runtime).insert_active(
            RunInsert(
                run_id=frozen_run.run_id,
                ingress_kind=(
                    IngressKind.CAPTURED if captured else IngressKind.IMPORTED
                ),
                trigger_source=(
                    PersistenceTriggerSource.MANUAL
                    if captured
                    else PersistenceTriggerSource.IMPORT
                ),
                config_id=7,
                hardware_id=(
                    int(frozen_run.hardware_id.rsplit("-", 1)[1])
                    if captured
                    else None
                ),
                snapshots=freeze_run_inputs(
                    json.loads(
                        frozen_run.analysis_input.canonical_bytes.decode("utf-8")
                    ),
                    (
                        {"hardware_id": frozen_run.hardware_id}
                        if captured
                        else None
                    ),
                ),
                accepted_at=ACCEPTED_AT,
                imported_source_identity=(
                    None
                    if captured
                    else frozen_run.source_reference.source_identity
                ),
                imported_source_path=(
                    None
                    if captured
                    else frozen_run.source_reference.absolute_path
                ),
                imported_expected_frame_count=(
                    None
                    if captured
                    else frozen_run.source_reference.frame_count
                ),
            )
        )
        return (
            CommitResult.COMMITTED
            if (
                result.state is TransactionState.COMMITTED
                and result.value is True
            )
            else CommitResult.REJECTED
        )

    def commit_initial_terminal(self, terminal_fact) -> CommitResult:
        raise AssertionError(
            "acquired real bridge attempts must not use Coordinator terminal persistence"
        )

    def apply_manual_judgment(self, request, facts):
        raise AssertionError("manual judgment is outside this real initial flow")

    def load_result_control_facts(self, run_id):
        return None

    def attach_current_trace(self, run_id, trace) -> CommitResult:
        result = RunRepository(self.runtime).attach_current_trace(run_id, trace)
        return (
            CommitResult.COMMITTED
            if (
                result.state is TransactionState.COMMITTED
                and result.value is True
            )
            else CommitResult.REJECTED
        )


class _SynchronousProductionSaveDispatcher:
    def __init__(self, adapter: ProductionRunAudioAdapter) -> None:
        self.adapter = adapter
        self.calls = []
        self.shutdown_calls: list[float] = []

    def try_submit(
        self,
        request,
        sink,
    ) -> SaveSubmission:
        self.calls.append(request)
        current_content_revision = 1
        if isinstance(request, CapturedAudioSaveRequest):
            samples = np.frombuffer(
                request.audio.interleaved_bytes,
                dtype=np.dtype("<f4"),
            ).reshape(request.audio.frame_count, request.audio.channel_count).copy()
            result = self.adapter.persist_captured(
                PersistenceCapturedAudioSaveRequest(
                    save_job_id=request.save_job_id,
                    run_id=request.run_id,
                    source_identity=request.source_identity,
                    content_revision=current_content_revision,
                    samples=samples,
                    sample_rate_hz=request.audio.sample_rate_hz,
                    channel_count=request.audio.channel_count,
                    frame_count=request.audio.frame_count,
                    final_path=request.final_path_request.final_path,
                    now=SAVED_AT,
                )
            )
            source_kind = AudioSaveSourceKind.CAPTURED_AUDIO
        else:
            result = self.adapter.persist_imported(
                PersistenceImportedAudioSaveRequest(
                    save_job_id=request.save_job_id,
                    run_id=request.run_id,
                    source_identity=request.source_identity,
                    content_revision=current_content_revision,
                    source_path=request.source.absolute_path,
                    final_path=request.final_path_request.final_path,
                    expected_sample_rate_hz=request.metadata.expected_sample_rate_hz,
                    expected_channel_count=request.metadata.expected_channel_count,
                    expected_frame_count=request.metadata.expected_frame_count,
                    now=SAVED_AT,
                )
            )
            source_kind = AudioSaveSourceKind.IMPORTED_SOURCE
        assert result.status is ProductionAudioStatus.PERSISTED
        assert result.reference is not None
        persisted = PersistedAudioReference(
            audio_id=result.reference.reference_id,
            save_job_id=request.save_job_id,
            run_id=request.run_id,
            source_kind=source_kind,
            source_identity=request.source_identity,
            final_path=result.reference.absolute_path,
            sample_rate_hz=result.reference.sample_rate_hz,
            channel_count=result.reference.channel_count,
            frame_count=result.reference.frame_count,
            byte_size=int(result.reference.content_revision.rsplit(":", 1)[1]),
            format=PersistedAudioFormat.PCM16_WAV,
            content_revision=current_content_revision,
        )
        sink(
            AudioSaveOutcome(
                save_job_id=request.save_job_id,
                run_id=request.run_id,
                source_kind=request.source_kind,
                source_identity=request.source_identity,
                status=AudioSaveStatus.SAVED,
                persisted_audio=persisted,
                error_category=None,
                reason="",
            )
        )
        return SaveSubmission(
            status=SaveSubmitStatus.ACCEPTED,
            run_id=request.run_id,
            save_job_id=request.save_job_id,
            source_kind=request.source_kind,
            source_identity=request.source_identity,
            reason="",
        )

    def cancel(self, run_id: str, save_job_id: str) -> SaveCancelResult:
        return SaveCancelResult(
            status=SaveCancelStatus.ALREADY_TERMINAL,
            run_id=run_id,
            save_job_id=save_job_id,
            reason="",
        )

    def shutdown(
        self,
        deadline_monotonic: float,
    ) -> SaveDispatcherShutdownReport:
        self.shutdown_calls.append(deadline_monotonic)
        return SaveDispatcherShutdownReport(
            status=SaveDispatcherShutdownStatus.CLOSED,
            accepted_count=len(self.calls),
            canceled_count=0,
            completed_count=len(self.calls),
            remaining_save_job_ids=(),
            live_worker_ids=(),
            errors=(),
        )


class _GatedExecutor:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()

    def execute(self, job, audio, cancel_token) -> AnalysisExecutionResult:
        self.calls.append((job, audio, cancel_token))
        self.trace.append("executor_entered")
        self.entered.set()
        assert self.release.wait(10.0), "gated executor was not released"
        self.trace.append("executor_returned")
        self.returned.set()
        return AnalysisExecutionResult(
            status=AnalysisExecutionStatus.SUCCEEDED,
            result_payload=FrozenResultObject(
                items=(
                    ("result", FrozenResultObject(items=(("score", 7),))),
                    ("judgment", "PASS"),
                )
            ),
        )

    def wait_for_calls(self, count: int) -> None:
        assert count == 1
        assert self.entered.wait(5.0), "executor was not entered"


class _TracedBridge:
    def __init__(
        self,
        bridge,
        trace: list[str],
        sink_returned: threading.Event,
    ) -> None:
        self.bridge = bridge
        self.trace = trace
        self.sink_returned = sink_returned

    def submit_initial(self, request, terminal_sink):
        def traced_sink(report) -> None:
            self.trace.append("bridge_sink_entered")
            terminal_sink(report)
            self.trace.append("bridge_sink_returned")
            self.sink_returned.set()

        return self.bridge.submit_initial(request, traced_sink)

    def submit_reanalysis(self, request, terminal_sink):
        return self.bridge.submit_reanalysis(request, terminal_sink)

    def cancel(self, request):
        return self.bridge.cancel(request)


class _ShutdownEntryScheduler:
    def __init__(self, scheduler, trace: list[str]) -> None:
        self.scheduler = scheduler
        self.trace = trace
        self.shutdown_entered = threading.Event()

    def submit(self, job):
        return self.scheduler.submit(job)

    def cancel(
        self,
        run_id,
        analysis_attempt_id,
        origin,
        reason,
    ):
        return self.scheduler.cancel(
            run_id,
            analysis_attempt_id,
            origin,
            reason,
        )

    def shutdown(self, timeout_seconds=None):
        self.trace.append("scheduler_shutdown_entered")
        self.shutdown_entered.set()
        return self.scheduler.shutdown(timeout_seconds)


class _SharedOwnerClock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value
        self.reads: list[str] = []
        self.remaining_calls: list[tuple[str, float, float]] = []

    def __call__(self) -> float:
        self.reads.append("read")
        return self.value

    def remaining(self, label: str, deadline: float) -> float:
        now = self()
        remaining = max(0.0, deadline - now)
        self.remaining_calls.append((label, now, remaining))
        return remaining


def _real_resident_port(route_count: int = 6):
    backend = FakeBackend()
    managed_input = make_adapter(
        backend,
        frames_per_buffer=8,
        max_capture_frames=64,
    )
    startup = managed_input.open_startup_streams(
        tuple(endpoint(index) for index in range(1, route_count + 1))
    )
    assert len(startup.ready_streams) == route_count
    routes = tuple(
        ManagedResidentCaptureRoute(
            coordinator_hardware_id=(
                f"hardware-{ready.endpoint.hardware_id.value}"
            ),
            managed_hardware_id=ready.endpoint.hardware_id,
            generation=ready.generation,
        )
        for ready in startup.ready_streams
    )
    return (
        ManagedResidentCapturePort(managed_input, routes),
        managed_input,
        backend,
        routes,
    )


def test_real_managed_input_resident_port_completes_six_and_rejects_seventh() -> None:
    port, managed_input, backend, routes = _real_resident_port()
    reservations = []
    outcomes = []
    condition = threading.Condition()
    try:
        for index, route in enumerate(routes):
            run_id = str(uuid4())
            reserved = port.reserve(
                run_id,
                route.coordinator_hardware_id,
            )
            assert reserved.reservation is not None
            reservations.append(reserved.reservation)

            def sink(outcome, *, expected_run=run_id) -> None:
                assert outcome.run_id == expected_run
                with condition:
                    outcomes.append(outcome)
                    condition.notify_all()

            port.register_completion(reserved.reservation, sink)
            port.start(
                reserved.reservation,
                trigger_monotonic_ns=backend.clock() + 1,
                target_frame_count=2,
            )

        rejected = port.reserve(
            str(uuid4()),
            routes[0].coordinator_hardware_id,
        )
        assert rejected.status.name == "CAPACITY"
        assert rejected.reservation is None

        backend.clock.advance_ns(round(8 * 1_000_000_000 / 48_000))
        samples = np.arange(16, dtype=np.float32).reshape(8, 2)
        emitters = [
            threading.Thread(
                target=backend.handles[f"managed-input-{index}"].emit,
                args=(samples.copy(),),
                name=f"composite-resident-emitter-{index}",
            )
            for index in range(1, 7)
        ]
        for emitter in emitters:
            emitter.start()
        for emitter in emitters:
            emitter.join(5)
            assert not emitter.is_alive()

        with condition:
            assert condition.wait_for(lambda: len(outcomes) == 6, timeout=5.0)
        assert {outcome.run_id for outcome in outcomes} == {
            reservation.run_id for reservation in reservations
        }
        observed = [(outcome.status.name, outcome.reason) for outcome in outcomes]
        assert all(
            outcome.status.name == "COMPLETED" for outcome in outcomes
        ), observed
        assert all(
            outcome.audio.source_identity == outcome.reservation.reservation_id
            for outcome in outcomes
        )
    finally:
        for reservation in reservations:
            try:
                port.cancel(reservation)
            except Exception:
                pass
        assert (
            port.close_and_join(5.0).status
            is ManagedResidentCaptureCloseStatus.CLOSED
        )
        managed_input.close_all(5.0)


def test_real_coordinator_completes_six_captured_runs_and_rejects_seventh(
    tmp_path: Path,
) -> None:
    database_path = prepare_run_persistence_database(
        tmp_path / "six-captured.sqlite3"
    )
    runtime = RunPersistenceDatabase(database_path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    audio_adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            RunRepository(runtime),
        )
    )
    bridge, executor = _build_real_stack(
        runtime,
        audio_adapter,
        now_utc=lambda: "2026-07-27T12:01:00.000000Z",
    )
    resident, managed_input, backend, routes = _real_resident_port()
    run_ids = [str(uuid4()) for _ in range(7)]
    paths = {
        run_id: (
            routes[index].coordinator_hardware_id,
            tmp_path / f"captured-{index}.wav",
        )
        for index, run_id in enumerate(run_ids[:6])
    }
    paths[run_ids[6]] = (
        routes[0].coordinator_hardware_id,
        tmp_path / "captured-seventh.wav",
    )
    terminal_condition = threading.Condition()
    terminal_runs = set()
    trace: list[str] = []

    class RecordingHistory(FakeHistorySink):
        def publish(self, event) -> None:
            super().publish(event)
            if event.projection.phase.name == "TERMINAL":
                with terminal_condition:
                    terminal_runs.add(event.run_id)
                    terminal_condition.notify_all()

    history = RecordingHistory(trace)
    save_ids = deque(str(uuid4()) for _ in range(6))
    plc = PlcResultSequencer(
        write_result=lambda *_args: None,
        write_testing_state=lambda *_args: None,
        report_attempt=lambda _report: None,
    )
    coordinator = MultiRunCoordinator(
        captured_freezer=_CapturedFreezer(paths),
        imported_freezer=_ImportedFreezer(
            tmp_path / "unused-source.wav",
            tmp_path / "unused-final.wav",
        ),
        resident=resident,
        persistence=_CoordinatorPersistence(runtime),
        save_dispatcher=_SynchronousProductionSaveDispatcher(audio_adapter),
        scheduler=bridge,
        history_projector=FakeHistoryProjector(trace),
        history_sink=history,
        plc=plc,
        diagnostics=FakeDiagnostics(),
        current_collector_factory=lambda _run_id, _frozen: None,
        save_job_id_factory=save_ids.popleft,
        wall_clock=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        monotonic_clock=lambda: 10.0,
    )
    phase_b_called = False
    try:
        starts = [
            coordinator.start_captured(
                CapturedStartRequest(
                    run_id=run_id,
                    trigger_monotonic_ns=backend.clock() + 1,
                    source=TriggerSource.MANUAL,
                    ingress_payload=payload("captured"),
                    fallback_history_labels=payload("fallback"),
                    plc_id=None,
                )
            )
            for run_id in run_ids[:6]
        ]
        assert all(result.status is StartStatus.ACCEPTED_ACTIVE for result in starts)
        seventh = coordinator.start_captured(
            CapturedStartRequest(
                run_id=run_ids[6],
                trigger_monotonic_ns=backend.clock() + 1,
                source=TriggerSource.MANUAL,
                ingress_payload=payload("captured"),
                fallback_history_labels=payload("fallback"),
                plc_id=None,
            )
        )
        assert seventh.status is StartStatus.CAPTURE_CAPACITY

        backend.clock.advance_ns(round(8 * 1_000_000_000 / 48_000))
        samples = np.arange(16, dtype=np.float32).reshape(8, 2)
        emitters = [
            threading.Thread(
                target=backend.handles[f"managed-input-{index}"].emit,
                args=(samples.copy(),),
            )
            for index in range(1, 7)
        ]
        for emitter in emitters:
            emitter.start()
        for emitter in emitters:
            emitter.join(5)
            assert not emitter.is_alive()
        with terminal_condition:
            completed = terminal_condition.wait_for(
                lambda: set(run_ids[:6]).issubset(terminal_runs),
                timeout=10.0,
            )
        assert completed, (
            terminal_runs,
            [(event.run_id, event.kind.name) for event in history.calls],
            coordinator.snapshot(),
        )
        executor.wait_for_calls(6)
        with runtime.operation() as database:
            assert database.execute_sql(
                "SELECT COUNT(*) FROM run WHERE state = 'SUCCEEDED'"
            ).fetchone() == (6,)
            assert database.execute_sql(
                "SELECT COUNT(*) FROM analysis_attempt WHERE state = 'SUCCEEDED'"
            ).fetchone() == (6,)
        assert coordinator.snapshot().restart_required is False

        coordinator.begin_shutdown()
        phase_a = bridge.stop_and_join_scheduler(5.0)
        assert phase_a.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
        assert coordinator.finish_shutdown(15.0).status.name == "CLOSED"
        assert (
            resident.close_and_join(5.0).status
            is ManagedResidentCaptureCloseStatus.CLOSED
        )
        managed_input.close_all(5.0)
        assert runtime.closed is False
        assert (
            bridge.close_persistence_after_upstream_stopped().status
            is BridgeDatabaseCloseStatus.DATABASE_CLOSED
        )
        phase_b_called = True
    finally:
        if not phase_b_called:
            try:
                coordinator.begin_shutdown()
            except Exception:
                pass
            phase_a = bridge.stop_and_join_scheduler(5.0)
            try:
                coordinator.finish_shutdown(15.0)
            except Exception:
                pass
            try:
                resident.close_and_join(5.0)
            finally:
                managed_input.close_all(5.0)
            if (
                phase_a.status
                is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
                and not runtime.closed
            ):
                bridge.close_persistence_after_upstream_stopped()


def test_real_coordinator_bridge_persistence_and_owner_shutdown_order(
    tmp_path: Path,
) -> None:
    prepared_database_path = prepare_run_persistence_database(
        tmp_path / "composite.sqlite3"
    )
    runtime = RunPersistenceDatabase(prepared_database_path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    source = tmp_path / "coordinator-source.wav"
    final = tmp_path / "coordinator-final.wav"
    _write_import(source)
    audio_adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            RunRepository(runtime),
        )
    )
    shutdown_trace: list[str] = []
    executor = _GatedExecutor(shutdown_trace)
    scheduler_wrappers = []

    def wrap_scheduler(scheduler):
        wrapper = _ShutdownEntryScheduler(scheduler, shutdown_trace)
        scheduler_wrappers.append(wrapper)
        return wrapper

    bridge, executor = _build_real_stack(
        runtime,
        audio_adapter,
        executor=executor,
        scheduler_wrapper_factory=wrap_scheduler,
    )
    scheduler_wrapper = scheduler_wrappers[0]
    sink_returned = threading.Event()
    traced_bridge = _TracedBridge(bridge, shutdown_trace, sink_returned)
    owner_clock = _SharedOwnerClock()
    resident, managed_input, _backend, _routes = _real_resident_port(1)
    trace: list[str] = []
    history = FakeHistorySink(trace)
    save_dispatcher = _SynchronousProductionSaveDispatcher(audio_adapter)
    plc_writes = []
    plc = PlcResultSequencer(
        write_result=lambda plc_id, value: plc_writes.append(
            ("result", plc_id, value)
        ),
        write_testing_state=lambda plc_id, testing: plc_writes.append(
            ("testing", plc_id, testing)
        ),
        report_attempt=lambda report: None,
    )
    run_id = str(uuid4())
    save_job_id = str(uuid4())
    coordinator = MultiRunCoordinator(
        captured_freezer=FakeCapturedFreezer(trace),
        imported_freezer=_ImportedFreezer(source, final),
        resident=resident,
        persistence=_CoordinatorPersistence(runtime),
        save_dispatcher=save_dispatcher,
        scheduler=traced_bridge,
        history_projector=FakeHistoryProjector(trace),
        history_sink=history,
        plc=plc,
        diagnostics=FakeDiagnostics(),
        current_collector_factory=lambda _run_id, _frozen: None,
        save_job_id_factory=lambda: save_job_id,
        wall_clock=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        monotonic_clock=owner_clock,
    )
    phase_b_called = False
    phase_a_thread: threading.Thread | None = None
    try:
        started = coordinator.start_imported(
            ImportedStartRequest(
                run_id=run_id,
                trigger_monotonic_ns=10,
                source=TriggerSource.IMPORT,
                ingress_payload=payload("imported-ingress"),
                fallback_history_labels=payload("fallback"),
            )
        )
        assert started.status in (
            StartStatus.ACCEPTED_ACTIVE,
            StartStatus.ACCEPTED_TERMINAL,
        )
        executor.wait_for_calls(1)
        assert coordinator.snapshot().restart_required is False
        assert len(save_dispatcher.calls) == 1
        assert plc_writes == []

        absolute_deadline = 15.0
        owner_trace = []
        coordinator.begin_shutdown()
        owner_trace.append("coordinator_begin_returned")
        phase_a_done = threading.Event()
        phase_a_box = []

        def run_phase_a() -> None:
            shutdown_trace.append("phase_a_call")
            phase_a_box.append(
                bridge.stop_and_join_scheduler(
                    owner_clock.remaining("bridge_phase_a", absolute_deadline)
                )
            )
            shutdown_trace.append("phase_a_returned")
            phase_a_done.set()

        phase_a_thread = threading.Thread(
            target=run_phase_a,
            name="composite-owner-phase-a",
        )
        phase_a_thread.start()
        assert scheduler_wrapper.shutdown_entered.wait(5.0)
        assert not phase_a_done.is_set()
        assert not sink_returned.is_set()
        executor.release.set()
        assert executor.returned.wait(5.0)
        assert sink_returned.wait(5.0)
        assert phase_a_done.wait(5.0)
        phase_a_thread.join(5.0)
        assert not phase_a_thread.is_alive()
        phase_a = phase_a_box[0]
        owner_trace.append("bridge_phase_a_returned")
        assert (
            phase_a.status
            is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
        )
        assert (
            shutdown_trace.index("executor_returned")
            < shutdown_trace.index("bridge_sink_entered")
            < shutdown_trace.index("bridge_sink_returned")
            < shutdown_trace.index("phase_a_returned")
        )
        with runtime.operation() as database:
            attempt_row = database.execute_sql(
                """
                SELECT attempt_kind, state, result_payload_json
                FROM analysis_attempt WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        assert attempt_row == ("INITIAL", "CANCELED", None)
        assert any(event.run_id == run_id for event in history.calls)
        shutdown_trace.append("coordinator_finish_entered")
        shutdown_report = coordinator.finish_shutdown(absolute_deadline)
        owner_trace.append("coordinator_finish_returned")
        assert (
            shutdown_trace.index("bridge_sink_returned")
            < shutdown_trace.index("coordinator_finish_entered")
        )
        assert shutdown_report.status.name == "CLOSED"
        assert save_dispatcher.shutdown_calls == [absolute_deadline]
        adapter_close = resident.close_and_join(
            owner_clock.remaining("resident_port", absolute_deadline)
        )
        owner_trace.append("resident_port_closed")
        assert (
            adapter_close.status
            is ManagedResidentCaptureCloseStatus.CLOSED
        )
        managed_input.close_all(
            owner_clock.remaining("managed_input", absolute_deadline)
        )
        owner_trace.append("managed_input_closed")
        assert runtime.closed is False
        phase_b = bridge.close_persistence_after_upstream_stopped()
        phase_b_called = True
        owner_trace.append("bridge_phase_b_database_closed")
        assert phase_b.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED
        assert runtime.closed is True
        assert owner_clock.remaining_calls == [
            ("bridge_phase_a", 10.0, 5.0),
            ("resident_port", 10.0, 5.0),
            ("managed_input", 10.0, 5.0),
        ]
        assert owner_trace == [
            "coordinator_begin_returned",
            "bridge_phase_a_returned",
            "coordinator_finish_returned",
            "resident_port_closed",
            "managed_input_closed",
            "bridge_phase_b_database_closed",
        ]
    finally:
        if not phase_b_called:
            executor.release.set()
            if phase_a_thread is not None:
                phase_a_thread.join(5.0)
            try:
                coordinator.begin_shutdown()
            except Exception:
                pass
            phase_a = bridge.stop_and_join_scheduler(5.0)
            try:
                coordinator.finish_shutdown(15.0)
            except Exception:
                pass
            try:
                resident.close_and_join(5.0)
            finally:
                managed_input.close_all(5.0)
            if (
                phase_a.status
                is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
                and not runtime.closed
            ):
                bridge.close_persistence_after_upstream_stopped()


def test_composite_result_revision_history_exception_early_and_eviction_matrix() -> None:
    manual = Harness()
    manual.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    manual.history.exception = RuntimeError("test-owned history sink failed")
    manual_coordinator = manual.coordinator()
    manual_result = manual_coordinator.apply_manual(
        manual_request(revision=7)
    )
    assert manual_result.status is OperationStatus.COMPLETED
    assert (
        manual.history.calls[0].expected_revision,
        manual.history.calls[0].proposed_revision,
    ) == (7, 8)
    assert len(manual.persistence.manual_calls) == 1
    assert manual.diagnostics.calls
    assert manual.scheduler.cancel_calls == []

    early = Harness()
    early.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    early.scheduler.synchronous_report = reanalysis_report()
    early.scheduler.bridge_results.append(
        early.scheduler.accepted_bridge_result(RUN_IDS[0], early=True)
    )
    early_result = early.coordinator().start_reanalysis(
        reanalysis_request(revision=7, start=0.123456789, end=9.876543211)
    )
    assert early_result.status is OperationStatus.SUBMITTED
    bridge_request, _ = early.scheduler.reanalysis_calls[0]
    assert (
        bridge_request.analysis_window_start_sec.hex(),
        bridge_request.analysis_window_end_sec.hex(),
    ) == (
        float(0.123456789).hex(),
        float(9.876543211).hex(),
    )
    assert [
        (event.expected_revision, event.proposed_revision)
        for event in early.history.calls
    ] == [(7, 8), (8, 9)]

    evicted = Harness()
    evicted.persistence.facts_by_run[RUN_IDS[0]] = result_facts()
    evicted_coordinator = evicted.coordinator()
    submitted = evicted_coordinator.start_reanalysis(
        reanalysis_request(revision=7)
    )
    assert submitted.attempt_id == ATTEMPT_IDS[0]
    assert evicted_coordinator.history_evicted(
        HistoryEvictionNotice(RUN_IDS[0])
    )
    assert len(evicted.scheduler.cancel_requests) == 1
    assert (
        evicted.scheduler.cancel_requests[0].origin
        is AnalysisCancelOrigin.RESULT_EVICTION
    )


def test_non_clean_upstream_skips_owner_phase_b_and_keeps_persistence_open(
    tmp_path: Path,
) -> None:
    database_path = prepare_run_persistence_database(
        tmp_path / "non-clean-upstream.sqlite3"
    )
    runtime = RunPersistenceDatabase(database_path)
    assert settle_startup(runtime, ACCEPTED_AT).ready
    audio_adapter = ProductionRunAudioAdapter(
        AudioPersistenceService(
            AudioPublication(),
            AudioRepository(runtime),
            RunRepository(runtime),
        )
    )
    bridge, _executor = _build_real_stack(runtime, audio_adapter)
    resident, managed_input, backend, routes = _real_resident_port(1)
    owner_clock = _SharedOwnerClock()
    absolute_deadline = 15.0
    owner_phase_b_calls = 0
    sink_entered = threading.Event()
    sink_release = threading.Event()
    reservation = None
    phase_a = None
    clean_observed = False
    try:
        reserved = resident.reserve(
            str(uuid4()),
            routes[0].coordinator_hardware_id,
        )
        assert reserved.reservation is not None
        reservation = reserved.reservation

        def blocked_sink(_outcome) -> None:
            sink_entered.set()
            assert sink_release.wait(10.0), "blocked sink was not released"

        resident.register_completion(reservation, blocked_sink)
        resident.start(
            reservation,
            trigger_monotonic_ns=backend.clock() + 1,
            target_frame_count=2,
        )
        backend.clock.advance_ns(round(8 * 1_000_000_000 / 48_000))
        backend.handles["managed-input-1"].emit(
            np.arange(16, dtype=np.float32).reshape(8, 2)
        )
        assert sink_entered.wait(5.0)

        phase_a = bridge.stop_and_join_scheduler(
            owner_clock.remaining("bridge_phase_a", absolute_deadline)
        )
        assert phase_a.status is BridgeSchedulerShutdownStatus.SCHEDULER_CLOSED
        owner_clock.value = 14.95
        non_clean = resident.close_and_join(
            owner_clock.remaining("resident_port", absolute_deadline)
        )
        if non_clean.status is ManagedResidentCaptureCloseStatus.CLOSED:
            owner_phase_b_calls += 1
            bridge.close_persistence_after_upstream_stopped()

        assert (
            non_clean.status
            is ManagedResidentCaptureCloseStatus.NON_CLEAN
        )
        assert non_clean.remaining_registration_ids == ()
        assert non_clean.remaining_reservation_ids == (
            reservation.reservation_id,
        )
        assert non_clean.remaining_waiter_ids == (
            reservation.reservation_id,
        )
        assert owner_phase_b_calls == 0
        assert runtime.closed is False
        assert owner_clock.remaining_calls == [
            ("bridge_phase_a", 10.0, 5.0),
            ("resident_port", 14.95, pytest.approx(0.05)),
        ]

        sink_release.set()
        owner_clock.value = 14.96
        clean = resident.close_and_join(
            owner_clock.remaining("resident_cleanup", absolute_deadline)
        )
        assert clean.status is ManagedResidentCaptureCloseStatus.CLOSED
        clean_observed = True
        managed_input.close_all(
            owner_clock.remaining("managed_input_cleanup", absolute_deadline)
        )
        cleanup = bridge.close_persistence_after_upstream_stopped()
        assert cleanup.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED
    finally:
        sink_release.set()
        if reservation is not None and not clean_observed:
            try:
                resident.cancel(reservation)
            except Exception:
                pass
        try:
            resident.close_and_join(1.0)
        finally:
            managed_input.close_all(1.0)
        if phase_a is None:
            phase_a = bridge.stop_and_join_scheduler(5.0)
        if not runtime.closed:
            cleanup = bridge.close_persistence_after_upstream_stopped()
            assert cleanup.status is BridgeDatabaseCloseStatus.DATABASE_CLOSED
