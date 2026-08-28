from __future__ import annotations

from collections import deque
from dataclasses import replace
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from app.service.multi_run_coordinator_mvp import MultiRunCoordinator, StartStatus
from app.service.multi_run_integration.history_qt_adapter import HistoryQtAdapter
from app.service.result_history import ResultHistoryStore
from tests.multi_run_coordinator_mvp._support import (
    RUN_IDS,
    SAVE_JOB_IDS,
    Harness,
)
from tests.multi_run_coordinator_mvp.test_composite_end_to_end import (
    _CapturedFreezer,
    _real_resident_port,
)
from tests.multi_run_integration.fakes import history_labels
from tests.multi_run_integration.test_six_run_pipeline import (
    _captured_request,
    _drain_until,
)


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


class _StopFreezer:
    def __init__(self, paths_by_run) -> None:
        self._delegate = _CapturedFreezer(paths_by_run)

    def freeze(self, request):
        frozen = self._delegate.freeze(request)
        return replace(
            frozen,
            history_labels=history_labels(
                config_id=1,
                fault_types=("bearing",),
                sn=f"SN-{request.run_id[-1]}",
            ),
        )


class _ResidentProbe:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.reserve_calls = []
        self.register_calls = []
        self.start_calls = []
        self.cancel_calls = []

    def reserve(self, run_id, hardware_id):
        self.reserve_calls.append((run_id, hardware_id))
        return self.delegate.reserve(run_id, hardware_id)

    def register_completion(self, reservation, sink):
        self.register_calls.append(reservation)
        return self.delegate.register_completion(reservation, sink)

    def start(self, reservation, trigger_monotonic_ns, target_frame_count):
        self.start_calls.append(
            (reservation, trigger_monotonic_ns, target_frame_count)
        )
        return self.delegate.start(
            reservation,
            trigger_monotonic_ns,
            target_frame_count,
        )

    def cancel(self, reservation):
        self.cancel_calls.append(reservation)
        return self.delegate.cancel(reservation)


def test_stop_all_then_new_signal_reuses_same_resident_handle(qapp, tmp_path):
    resident, managed_input, backend, routes = _real_resident_port(1)
    resident_probe = _ResidentProbe(resident)
    old_run_id, new_run_id = RUN_IDS[:2]
    hardware_id = routes[0].coordinator_hardware_id
    paths_by_run = {
        old_run_id: (hardware_id, tmp_path / "old.wav"),
        new_run_id: (hardware_id, tmp_path / "new.wav"),
    }
    dashboard = _Dashboard()
    adapter_diagnostics = []
    history = HistoryQtAdapter(
        dashboard,
        diagnostic_callback=adapter_diagnostics.append,
    )
    harness = Harness()
    save_job_ids = deque(SAVE_JOB_IDS)
    coordinator = MultiRunCoordinator(
        captured_freezer=_StopFreezer(paths_by_run),
        imported_freezer=harness.imported_freezer,
        resident=resident_probe,
        persistence=harness.persistence,
        save_dispatcher=harness.save_dispatcher,
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
    handle_name, handle = next(iter(backend.handles.items()))
    original_handle_id = id(handle)
    old_trigger_ns = backend.clock() + 1

    try:
        first = coordinator.start_captured(
            _captured_request(
                old_run_id,
                plc_id=1,
                trigger_monotonic_ns=old_trigger_ns,
            )
        )
        assert first.status is StartStatus.ACCEPTED_ACTIVE
        old_reservation = resident_probe.register_calls[0]

        first_stop = coordinator.stop_all(1.0)
        _drain_until(
            qapp,
            lambda: len(dashboard.store.snapshot().terminal_cards) == 1,
        )
        first_snapshot = dashboard.store.snapshot()
        old_card = first_snapshot.terminal_cards[0]
        old_payload = dashboard.store.display_payload(old_run_id)
        assert old_card.run_id == old_run_id
        assert first_snapshot.follow_latest is True
        assert first_snapshot.selected_run_id == old_run_id
        assert resident_probe.cancel_calls == [old_reservation]

        repeated_stop = coordinator.stop_all(1.0)
        assert first_stop.canceled_accepted_run_ids == (old_run_id,)
        assert repeated_stop.canceled_accepted_run_ids == ()
        assert resident_probe.cancel_calls == [old_reservation]

        new_trigger_ns = backend.clock() + 1
        second = coordinator.start_captured(
            _captured_request(
                new_run_id,
                plc_id=1,
                trigger_monotonic_ns=new_trigger_ns,
            )
        )
        assert second.status is StartStatus.ACCEPTED_ACTIVE
        assert list(backend.handles) == [handle_name]
        assert id(backend.handles[handle_name]) == original_handle_id
        assert resident_probe.reserve_calls == [
            (old_run_id, hardware_id),
            (new_run_id, hardware_id),
        ]
        assert resident_probe.start_calls[0][0].hardware_id == hardware_id
        assert resident_probe.start_calls[0][1] == old_trigger_ns
        assert resident_probe.start_calls[1][0].hardware_id == hardware_id
        assert resident_probe.start_calls[1][1] == new_trigger_ns

        _drain_until(
            qapp,
            lambda: len(dashboard.store.snapshot().active_cards) == 1,
        )
        active_snapshot = dashboard.store.snapshot()
        assert active_snapshot.selected_run_id == new_run_id
        assert active_snapshot.follow_latest is True
        assert [card.run_id for card in active_snapshot.active_cards] == [
            new_run_id
        ]
        assert [card.run_id for card in active_snapshot.terminal_cards] == [
            old_run_id
        ]
        assert active_snapshot.terminal_cards[0] == old_card
        assert dashboard.store.display_payload(old_run_id) == old_payload
        selected_old = dashboard.store.select_run(old_run_id)
        assert selected_old.snapshot.follow_latest is True
        assert selected_old.snapshot.selected_run_id == old_run_id

        new_reservation = resident_probe.register_calls[1]
        coordinator.stop_all(1.0)
        _drain_until(
            qapp,
            lambda: len(dashboard.store.snapshot().terminal_cards) == 2,
        )
        final_snapshot = dashboard.store.snapshot()
        assert resident_probe.cancel_calls == [
            old_reservation,
            new_reservation,
        ]
        assert [card.run_id for card in final_snapshot.terminal_cards] == [
            new_run_id,
            old_run_id,
        ]
        assert final_snapshot.active_cards == ()
        assert final_snapshot.follow_latest is True
        assert final_snapshot.selected_run_id == new_run_id
        assert final_snapshot.terminal_cards[1] == old_card
        assert dashboard.store.display_payload(old_run_id) == old_payload
        assert adapter_diagnostics == []
    finally:
        coordinator.stop_all(1.0)
        resident.close_and_join(5.0)
        managed_input.close_all(5.0)
