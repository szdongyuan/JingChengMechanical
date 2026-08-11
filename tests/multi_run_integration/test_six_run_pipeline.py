from __future__ import annotations

from collections import deque
from dataclasses import replace
import os
from pathlib import Path
import threading

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from app.service.current_meter_service import CurrentSample
from app.service.multi_run_coordinator_mvp import (
    AudioSaveOutcome,
    AudioSaveStatus,
    CapturedStartRequest,
    MultiRunCoordinator,
    PersistedAudioFormat,
    PersistedAudioReference,
    PlcIngressTicket,
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


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Dashboard:
    def __init__(self) -> None:
        self.store = ResultHistoryStore()

    def thread(self):
        return QThread.currentThread()

    def apply_history_event(self, event):
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
        save_by_run = {}
        for position, index in enumerate(capture_order):
            run_id = original_run_ids[index]
            backend.handles[f"managed-input-{index + 1}"].emit(samples.copy())
            save_dispatcher.wait_submitted(run_id)
            save_by_run[run_id] = next(
                pair for pair in save_dispatcher.calls if pair[0].run_id == run_id
            )

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
