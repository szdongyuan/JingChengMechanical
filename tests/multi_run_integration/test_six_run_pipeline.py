from __future__ import annotations

from collections import deque
from dataclasses import replace
import os
from pathlib import Path
import threading
import time
import wave

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from app.service.current_meter_service import CurrentSample
from app.service.analysis import process_executor as process_executor_module
from app.service.analysis.process_executor import ProcessIsolatedAnalysisExecutor
from app.service.analysis.scheduler import (
    AnalysisScheduler,
    AnalysisSchedulerConfig,
    AudioStorageKind,
    PersistedAudioReference as SchedulerAudioReference,
    SubmitStatus,
)
from app.service.multi_run_coordinator_mvp import (
    AudioSaveOutcome,
    AudioSaveStatus,
    CapturedStartRequest,
    MultiRunCoordinator,
    PersistedAudioFormat,
    PersistedAudioReference,
    PlcIngressTicket,
    SaveCancelToken,
    StartStatus,
    TriggerSource,
)
from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter
from app.service.result_history import ResultHistoryStore
from tests.multi_run_coordinator_mvp._support import (
    ATTEMPT_IDS,
    AUDIO_IDS,
    RUN_IDS,
    SAVE_JOB_IDS,
    FakeCurrentCollector,
    Harness,
    run_config_payload,
    payload,
)
from tests.multi_run_coordinator_mvp.test_bridge_initial import terminal_report
from tests.multi_run_coordinator_mvp.test_composite_end_to_end import (
    _CapturedFreezer,
    _real_resident_port,
)
from tests.multi_run_integration.fakes import history_labels
from tests.analysis_scheduler.fakes import (
    ManualClock,
    PassThroughStartGate,
    RecordingAudioLoader,
    make_job,
)
from tests.analysis_scheduler.test_staged_process_executor import (
    _BlockingDecisionContext,
    _FakeCaptureClock,
)
from tests.multi_run_integration.test_production_audio_saver import (
    _insert_active,
    _real_saver,
)
from app.service.run_persistence.prepare_database import (
    prepare_run_persistence_database,
)
from tests.run_persistence.test_audio_repository import ready_runtime


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Dashboard:
    def __init__(self) -> None:
        self.store = ResultHistoryStore()
        self.apply_threads = []

    def thread(self):
        return QThread.currentThread()

    def apply_history_event(self, event):
        self.apply_threads.append(threading.current_thread().name)
        return self.store.apply(event)


class _BarrierSaveDispatcher:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._submitted: dict[str, threading.Event] = {}

    @property
    def calls(self):
        return self._delegate.calls

    def try_submit(self, request, sink):
        event = self._submitted.setdefault(request.run_id, threading.Event())
        result = self._delegate.try_submit(request, sink)
        event.set()
        return result

    def wait_submitted(self, run_id: str) -> None:
        event = self._submitted.setdefault(run_id, threading.Event())
        assert event.wait(5.0), f"save submission did not arrive for {run_id}"

    def cancel(self, run_id, save_job_id):
        return self._delegate.cancel(run_id, save_job_id)

    def shutdown(self, deadline_monotonic):
        return self._delegate.shutdown(deadline_monotonic)


class _AcceptanceFreezer:
    def __init__(self, paths_by_run: dict[str, tuple[str, Path]]) -> None:
        self._delegate = _CapturedFreezer(paths_by_run)

    def freeze(self, request):
        frozen = self._delegate.freeze(request)
        hardware_number = int(frozen.hardware_id.rsplit("-", 1)[1])
        return replace(
            frozen,
            config_payload=run_config_payload(current_enabled=True),
            history_labels=history_labels(
                config_id=hardware_number,
                fault_types=("bearing",),
                sn=f"SN-{hardware_number}",
            ),
        )


def _captured_request(
    run_id: str,
    *,
    plc_id: int,
    trigger_monotonic_ns: int,
) -> CapturedStartRequest:
    return CapturedStartRequest(
        run_id=run_id,
        trigger_monotonic_ns=trigger_monotonic_ns,
        source=TriggerSource.PLC,
        ingress_payload=payload(f"ingress:{run_id}"),
        fallback_history_labels=history_labels(
            config_id=plc_id,
            fault_types=("bearing",),
            sn=f"SN-{plc_id}",
        ),
        plc_id=plc_id,
    )


def _saved_outcome(request, *, audio_id: str) -> AudioSaveOutcome:
    audio = request.audio
    reference = PersistedAudioReference(
        audio_id=audio_id,
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        final_path=request.final_path_request.final_path,
        sample_rate_hz=audio.sample_rate_hz,
        channel_count=audio.channel_count,
        frame_count=audio.frame_count,
        byte_size=len(audio.interleaved_bytes),
        format=PersistedAudioFormat.PCM16_WAV,
        content_revision=1,
    )
    return AudioSaveOutcome(
        save_job_id=request.save_job_id,
        run_id=request.run_id,
        source_kind=request.source_kind,
        source_identity=request.source_identity,
        status=AudioSaveStatus.SAVED,
        persisted_audio=reference,
        error_category=None,
        reason="",
    )


def _drain_until(qapp: QApplication, predicate) -> None:
    for _ in range(100):
        qapp.processEvents()
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate()


def test_six_resident_streams_run_six_parallel_captures_with_isolated_identity(
    qapp,
    tmp_path,
    monkeypatch,
):
    resident, managed_input, backend, routes = _real_resident_port(6)
    handle_ids = {
        name: id(handle) for name, handle in backend.handles.items()
    }
    assert len(routes) == len(handle_ids) == 6

    original_run_ids = RUN_IDS[:6]
    rejected_run_id = RUN_IDS[6]
    reuse_run_id = RUN_IDS[7]
    paths_by_run = {
        run_id: (
            routes[index].coordinator_hardware_id,
            tmp_path / f"capture-{index}.wav",
        )
        for index, run_id in enumerate(original_run_ids)
    }
    paths_by_run[rejected_run_id] = (
        routes[0].coordinator_hardware_id,
        tmp_path / "rejected.wav",
    )
    capture_order = (2, 5, 0, 4, 1, 3)
    first_released_index = capture_order[0]
    paths_by_run[reuse_run_id] = (
        routes[first_released_index].coordinator_hardware_id,
        tmp_path / "reuse.wav",
    )

    harness = Harness()
    integrity_runtime = ready_runtime(
        prepare_run_persistence_database(tmp_path / "integrity.db")
    )
    integrity_now = "2026-08-31T12:00:00.000000Z"
    for run_id in original_run_ids:
        _insert_active(
            integrity_runtime,
            run_id=run_id,
            now=integrity_now,
        )
    integrity_saver, _integrity_runs = _real_saver(
        integrity_runtime,
        integrity_now,
    )
    persistence_threads = []
    save_dispatcher = _BarrierSaveDispatcher(harness.save_dispatcher)
    collectors = [
        FakeCurrentCollector(
            harness.trace,
            samples=[CurrentSample(0.0, float(index + 1))],
        )
        for index in range(7)
    ]
    harness.current_factory.collectors.extend(collectors)
    dashboard = _Dashboard()
    adapter_diagnostics = []
    history = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=adapter_diagnostics.append,
    )
    save_job_ids = deque(SAVE_JOB_IDS)
    coordinator = MultiRunCoordinator(
        captured_freezer=_AcceptanceFreezer(paths_by_run),
        imported_freezer=harness.imported_freezer,
        resident=resident,
        persistence=harness.persistence,
        save_dispatcher=save_dispatcher,
        scheduler=harness.scheduler,
        history_projector=harness.projector,
        history_sink=history,
        plc=harness.plc,
        diagnostics=harness.diagnostics,
        current_collector_factory=harness.current_factory,
        save_job_id_factory=save_job_ids.popleft,
        wall_clock=harness.clock,
        monotonic_clock=harness.monotonic,
    )

    try:
        starts = [
            coordinator.start_captured(
                _captured_request(
                    run_id,
                    plc_id=index + 1,
                    trigger_monotonic_ns=backend.clock() + 1,
                )
            )
            for index, run_id in enumerate(original_run_ids)
        ]
        assert [result.status for result in starts] == [
            StartStatus.ACCEPTED_ACTIVE
        ] * 6
        assert len(harness.persistence.active_calls) == 6
        assert {
            frozen.run_id: frozen.hardware_id
            for frozen in harness.persistence.active_calls
        } == {
            run_id: routes[index].coordinator_hardware_id
            for index, run_id in enumerate(original_run_ids)
        }

        seventh = coordinator.start_captured(
            _captured_request(
                rejected_run_id,
                plc_id=7,
                trigger_monotonic_ns=backend.clock() + 1,
            )
        )
        assert seventh.status is StartStatus.CAPTURE_CAPACITY
        assert [frozen.run_id for frozen in harness.persistence.active_calls] == list(
            original_run_ids
        )
        assert len(harness.plc.reject_calls) == 1
        assert harness.plc.reject_calls[0][0] == PlcIngressTicket(
            rejected_run_id,
            7,
            1,
        )

        backend.clock.advance_ns(round(8 * 1_000_000_000 / 48_000))
        samples = np.arange(16, dtype=np.float32).reshape(8, 2)
        no_analysis_baseline_bytes = samples[1:3, :].copy().tobytes(order="C")
        save_by_run = {}
        integrity_references = {}
        for position, index in enumerate(capture_order):
            run_id = original_run_ids[index]
            backend.handles[f"managed-input-{index + 1}"].emit(samples.copy())
            save_dispatcher.wait_submitted(run_id)
            save_by_run[run_id] = next(
                pair for pair in save_dispatcher.calls if pair[0].run_id == run_id
            )
            captured_audio = save_by_run[run_id][0].audio
            assert captured_audio.frame_count == 2
            assert captured_audio.sample_rate_hz == 48_000
            assert captured_audio.channel_count == 2
            assert captured_audio.channel_indices == (0, 1)
            assert type(captured_audio.interleaved_bytes) is bytes
            assert captured_audio.interleaved_bytes == no_analysis_baseline_bytes
            captured_array = np.frombuffer(
                captured_audio.interleaved_bytes,
                dtype=np.float32,
            ).reshape(2, 2)
            assert captured_array.dtype == np.dtype(np.float32)
            assert captured_array.tobytes(order="C") == no_analysis_baseline_bytes
            persistence_threads.append(threading.current_thread().name)
            persisted = integrity_saver.save_captured(
                save_by_run[run_id][0],
                SaveCancelToken(),
            )
            assert persisted.status is AudioSaveStatus.SAVED
            assert persisted.persisted_audio is not None
            assert persisted.persisted_audio.content_revision == 1
            integrity_references[run_id] = persisted.persisted_audio
            final_path = Path(persisted.persisted_audio.final_path)
            expected_pcm = np.full((2, 2), 32767, dtype="<i2").tobytes(
                order="C"
            )
            with wave.open(str(final_path), "rb") as reader:
                assert reader.getnframes() == 2
                assert reader.getframerate() == 48_000
                assert reader.getnchannels() == 2
                assert reader.getsampwidth() == 2
                assert reader.readframes(2) == expected_pcm

            if position == 0:
                request, sink = save_by_run[run_id]
                harness.scheduler.bridge_results.append(
                    harness.scheduler.accepted_bridge_result(
                        run_id,
                        ATTEMPT_IDS[index],
                    )
                )
                sink(_saved_outcome(request, audio_id=AUDIO_IDS[index]))
                assert len(harness.scheduler.initial_calls) == 1
                assert coordinator.snapshot().analysis_attempt_ids == (
                    ATTEMPT_IDS[index],
                )

                reuse = coordinator.start_captured(
                    _captured_request(
                        reuse_run_id,
                        plc_id=8,
                        trigger_monotonic_ns=backend.clock() + 1,
                    )
                )
                assert reuse.status is StartStatus.ACCEPTED_ACTIVE
                assert backend.handles.keys() == handle_ids.keys()
                assert {
                    name: id(handle)
                    for name, handle in backend.handles.items()
                } == handle_ids

        analysis_context = _BlockingDecisionContext()
        capture_clock = _FakeCaptureClock()
        enrichment_reports = []
        decision_commit_threads = []
        monkeypatch.setattr(
            process_executor_module.multiprocessing,
            "get_context",
            lambda method: analysis_context if method == "spawn" else None,
        )
        staged_executor = ProcessIsolatedAnalysisExecutor(
            process_count=2,
            capture_active=capture_clock.capture_active,
            monotonic=capture_clock.monotonic,
            quiet_wait=capture_clock.wait,
            enrichment_sink=enrichment_reports.append,
        )

        def commit_durable_decision(outcome) -> None:
            decision_commit_threads.append(threading.current_thread().name)
            assert staged_executor.mark_decision_committed(
                outcome.analysis_attempt_id
            )

        analysis_scheduler = AnalysisScheduler(
            config=AnalysisSchedulerConfig(
                worker_count=2,
                pending_capacity=6,
            ),
            audio_loader=RecordingAudioLoader(),
            analysis_executor=staged_executor,
            start_gate=PassThroughStartGate(),
            outcome_sink=commit_durable_decision,
            clock=ManualClock(1.0),
        )
        try:
            analysis_jobs = []
            for index, run_id in enumerate(original_run_ids):
                base = make_job(index + 101, run_id=run_id)
                persisted = integrity_references[run_id]
                analysis_jobs.append(
                    replace(
                        base,
                        audio=SchedulerAudioReference(
                            reference_id=persisted.audio_id,
                            storage_kind=(
                                AudioStorageKind.CAPTURED_FINAL_WAV
                            ),
                            absolute_path=persisted.final_path,
                            sample_rate_hz=persisted.sample_rate_hz,
                            channel_count=persisted.channel_count,
                            frame_count=persisted.frame_count,
                            dtype="<i2",
                            content_revision=(
                                f"{persisted.audio_id}:"
                                f"{persisted.byte_size}"
                            ),
                            audio_data_id=persisted.audio_id,
                        ),
                    )
                )
            assert [
                analysis_scheduler.submit(job).status for job in analysis_jobs
            ] == [SubmitStatus.ACCEPTED] * 6
            deadline = time.monotonic() + 1.0
            while (
                time.monotonic() < deadline
                and len(analysis_context.connections) < 2
            ):
                threading.Event().wait(0.01)
            assert len(analysis_context.connections) == 2
            assert all(
                connection.decision_entered.wait(1.0)
                for connection in analysis_context.connections
            )
            assert set(staged_executor.attempt_state_snapshot()) == {
                job.analysis_attempt_id for job in analysis_jobs
            }

            capture_clock.set_active(False)
            assert staged_executor.quiet_epoch_id() == 1
            capture_clock.advance(0.249)
            for connection in analysis_context.connections:
                connection.decision_release.set()
            threading.Event().wait(0.05)
            assert enrichment_reports == []

            capture_clock.advance(0.001)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(enrichment_reports) < 6:
                threading.Event().wait(0.01)
            assert len(enrichment_reports) == 6
            assert len(decision_commit_threads) == 6
            assert all(
                name.startswith("analysis-scheduler-worker-")
                for name in decision_commit_threads
            )
            assert all(
                not name.startswith("resident-audio-drain-")
                for name in decision_commit_threads
            )
        finally:
            for connection in analysis_context.connections:
                connection.decision_release.set()
            analysis_scheduler.shutdown(5.0)
            staged_executor.close()

        save_order = (2, 0, 5, 1, 4, 3)
        for index in save_order:
            if index == first_released_index:
                continue
            run_id = original_run_ids[index]
            request, sink = save_by_run[run_id]
            harness.scheduler.bridge_results.append(
                harness.scheduler.accepted_bridge_result(
                    run_id,
                    ATTEMPT_IDS[index],
                )
            )
            sink(_saved_outcome(request, audio_id=AUDIO_IDS[index]))

        assert {
            request.run_id: request.audio_reference.audio_data_id
            for request, _sink in harness.scheduler.initial_calls
        } == {
            run_id: AUDIO_IDS[index]
            for index, run_id in enumerate(original_run_ids)
        }
        scheduler_audio_metadata = {
            request.run_id: (
                request.audio_reference.content_revision,
                request.audio_reference.frame_count,
                request.audio_reference.sample_rate_hz,
                request.audio_reference.channel_count,
            )
            for request, _sink in harness.scheduler.initial_calls
        }
        assert scheduler_audio_metadata == {
            run_id: (f"{AUDIO_IDS[index]}:16", 2, 48_000, 2)
            for index, run_id in enumerate(original_run_ids)
        }
        assert set(coordinator.snapshot().analysis_attempt_ids) == set(
            ATTEMPT_IDS[:6]
        )
        assert {
            run_id: trace.samples
            for run_id, trace in harness.persistence.current_calls
        } == {
            run_id: ((0.0, float(index + 1)),)
            for index, run_id in enumerate(original_run_ids)
        }

        terminal_order = (4, 1, 5, 0, 3, 2)
        for index in terminal_order:
            harness.scheduler.complete_report(
                terminal_report(
                    original_run_ids[index],
                    ATTEMPT_IDS[index],
                )
            )

        _drain_until(
            qapp,
            lambda: len(dashboard.store.snapshot().terminal_cards) == 6,
        )
        snapshot = dashboard.store.snapshot()
        assert [card.run_id for card in snapshot.terminal_cards] == [
            original_run_ids[index] for index in reversed(terminal_order)
        ]
        assert snapshot.selected_run_id == reuse_run_id
        assert snapshot.follow_latest is True
        assert adapter_diagnostics == []
        assert dashboard.apply_threads
        assert set(dashboard.apply_threads) == {threading.current_thread().name}
        assert all(
            not name.startswith("resident-audio-drain-")
            for name in dashboard.apply_threads
        )
        assert persistence_threads == [threading.current_thread().name] * 6
        assert all(
            not name.startswith("resident-audio-drain-")
            for name in persistence_threads
        )
        resident_pool = managed_input._pool
        assert resident_pool is not None
        for slot in resident_pool._slots.values():
            assert slot._ingress.overflow_count == 0
            assert slot._fault is None
            assert slot._dropped_callback_count == 0
            blocks = slot._ring.block_metadata
            assert all(
                left.end_cursor == right.start_cursor
                for left, right in zip(blocks, blocks[1:])
            )

        terminal_tickets = {
            ticket.run_id: ticket
            for ticket, _payload in harness.plc.publish_calls
            if ticket.run_id in original_run_ids
        }
        assert terminal_tickets == {
            run_id: PlcIngressTicket(run_id, index + 1, 1)
            for index, run_id in enumerate(original_run_ids)
        }
        assert len(
            [
                call
                for call in harness.projector.calls
                if call.kind.name == "RUN_TERMINATED"
                and call.run_id in original_run_ids
            ]
        ) == 6
        assert [collector.stop_calls != [] for collector in collectors[:6]] == [
            True
        ] * 6
        assert collectors[6].stop_calls == []
    finally:
        coordinator.stop_all(1.0)
        resident.close_and_join(5.0)
        managed_input.close_all(5.0)
