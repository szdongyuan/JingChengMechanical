from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.service.multi_run_coordinator_mvp.contracts import (
    ABSENT,
    FrozenPayload,
    HistoryFactKind,
    ProjectedHistoryEvent,
    ResultProjection,
    ResultProjectionPhase,
    TerminalConclusion,
    TerminalErrorCategory,
)
from ui.modules.today_statistics import TodayStatisticsModule


_LOCAL_TIMEZONE = timezone(timedelta(hours=8))
_RUN_ID = "00000000-0000-4000-8000-000000000001"
_OPERATION_ID = "00000000-0000-4000-8000-000000000002"
_HISTORY_LABELS = FrozenPayload("history-labels", b"{}")
_RESULT_PAYLOAD = FrozenPayload("result", b"{}")


class _SignalStub:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)


class _RunExportReaderStub:
    def __init__(self, rows) -> None:
        self.rows = tuple(rows)
        self.calls = 0

    def list_durable(self):
        self.calls += 1
        return self.rows


class _LegacyAudioDataServiceStub:
    def __init__(self) -> None:
        self.calls = 0

    def today_statistics(self):
        self.calls += 1
        return {"total": 99, "ok": 98, "ng": 1}


class _BusStub:
    def __init__(self) -> None:
        self.result_changed = _SignalStub()
        self.test_finished = _SignalStub()
        self.factory_display_reset = _SignalStub()
        self.run_records_deleted = _SignalStub()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _terminal_at(local_value: datetime) -> str:
    return local_value.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _durable_row(
    *,
    run_id: str,
    state: str,
    judgment: str | None,
    terminal_at: str | None,
):
    return SimpleNamespace(
        run_id=run_id,
        state=state,
        judgment=judgment,
        terminal_at=terminal_at,
    )


def _history_event(
    kind: HistoryFactKind,
    *,
    preaccept: bool = False,
) -> ProjectedHistoryEvent:
    if kind is HistoryFactKind.RECORDING:
        projection = ResultProjection(
            phase=ResultProjectionPhase.RECORDING,
            conclusion=None,
            error_category=None,
            reason="",
            history_labels=_HISTORY_LABELS,
            result_payload=None,
        )
    elif preaccept:
        projection = ResultProjection(
            phase=ResultProjectionPhase.TERMINAL,
            conclusion=TerminalConclusion.CAPACITY_REJECTED,
            error_category=TerminalErrorCategory.CAPTURE_CAPACITY,
            reason="capacity unavailable",
            history_labels=_HISTORY_LABELS,
            result_payload=None,
        )
    else:
        projection = ResultProjection(
            phase=ResultProjectionPhase.TERMINAL,
            conclusion=TerminalConclusion.OK,
            error_category=None,
            reason="",
            history_labels=_HISTORY_LABELS,
            result_payload=_RESULT_PAYLOAD,
        )
    expected_revision = ABSENT if preaccept or kind is HistoryFactKind.RECORDING else 1
    return ProjectedHistoryEvent(
        kind=kind,
        run_id=_RUN_ID,
        expected_revision=expected_revision,
        operation_id=None if expected_revision is ABSENT else _OPERATION_ID,
        proposed_revision=1 if expected_revision is ABSENT else 2,
        projection=projection,
    )


def test_today_statistics_reads_durable_run_v2_terminal_rows(qapp):
    now = datetime.now(_LOCAL_TIMEZONE)
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    reader = _RunExportReaderStub(
        (
            _durable_row(
                run_id="pass",
                state="SUCCEEDED",
                judgment="PASS",
                terminal_at=_terminal_at(today),
            ),
            _durable_row(
                run_id="fail",
                state="SUCCEEDED",
                judgment="FAIL",
                terminal_at=_terminal_at(today),
            ),
            _durable_row(
                run_id="no-result",
                state="NO_RESULT",
                judgment=None,
                terminal_at=_terminal_at(today),
            ),
            _durable_row(
                run_id="active",
                state="ACTIVE",
                judgment=None,
                terminal_at=None,
            ),
            _durable_row(
                run_id="old-pass",
                state="SUCCEEDED",
                judgment="PASS",
                terminal_at=_terminal_at(yesterday),
            ),
        )
    )
    legacy = _LegacyAudioDataServiceStub()
    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=_BusStub(),
            run_export_reader=reader,
            audio_data_service=legacy,
        )
    )

    assert reader.calls == 1
    assert legacy.calls == 0
    assert module.lb_total.text() == "3"
    assert module.lb_ok.text() == "1"
    assert module.lb_ng.text() == "1"

    module.deleteLater()


def test_only_committed_run_terminated_history_refreshes_statistics(qapp):
    reader = _RunExportReaderStub(())
    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=_BusStub(),
            run_export_reader=reader,
            audio_data_service=_LegacyAudioDataServiceStub(),
        )
    )

    ignored_events = (
        _history_event(HistoryFactKind.RECORDING),
        _history_event(HistoryFactKind.RUN_TERMINATED, preaccept=True),
        _history_event(HistoryFactKind.TERMINAL_RESULT_REPLACED),
        _history_event(HistoryFactKind.REANALYSIS_COMPLETED),
        {"kind": "RUN_TERMINATED", "run_id": _RUN_ID},
    )
    for event in ignored_events:
        module.on_committed_history_event(event)

    assert reader.calls == 1

    now = datetime.now(_LOCAL_TIMEZONE)
    reader.rows = (
        _durable_row(
            run_id=_RUN_ID,
            state="SUCCEEDED",
            judgment="PASS",
            terminal_at=_terminal_at(now),
        ),
    )
    module.on_committed_history_event(
        _history_event(HistoryFactKind.RUN_TERMINATED)
    )

    assert reader.calls == 2
    assert module.lb_total.text() == "1"
    assert module.lb_ok.text() == "1"
    assert module.lb_ng.text() == "0"

    module.deleteLater()


def test_terminal_trigger_does_not_refresh_when_run_is_not_durably_terminal(qapp):
    reader = _RunExportReaderStub(
        (
            _durable_row(
                run_id=_RUN_ID,
                state="ACTIVE",
                judgment=None,
                terminal_at=None,
            ),
        )
    )
    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=_BusStub(),
            run_export_reader=reader,
            audio_data_service=_LegacyAudioDataServiceStub(),
        )
    )
    module._set_values("unchanged", "unchanged", "unchanged")

    module.on_committed_history_event(
        _history_event(HistoryFactKind.RUN_TERMINATED)
    )

    assert reader.calls == 2
    assert module.lb_total.text() == "unchanged"
    assert module.lb_ok.text() == "unchanged"
    assert module.lb_ng.text() == "unchanged"

    module.deleteLater()


def test_today_statistics_has_no_legacy_or_periodic_refresh_source(qapp):
    bus = _BusStub()
    reader = _RunExportReaderStub(())
    legacy = _LegacyAudioDataServiceStub()

    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=bus,
            run_export_reader=reader,
            audio_data_service=legacy,
        )
    )

    assert bus.result_changed.callbacks == []
    assert bus.test_finished.callbacks == []
    assert bus.factory_display_reset.callbacks == []
    assert not hasattr(module, "_timer")

    bus.result_changed.emit({})
    bus.test_finished.emit(1, {})
    bus.factory_display_reset.emit("manual_judgment_ok")
    assert reader.calls == 1

    module.deleteLater()


@pytest.mark.parametrize("status", ["DELETED", "PARTIAL_FILE_REMAINS"])
def test_committed_deletion_signal_refreshes_today_statistics(qapp, status):
    bus = _BusStub()
    reader = _RunExportReaderStub(())
    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=bus,
            run_export_reader=reader,
            audio_data_service=_LegacyAudioDataServiceStub(),
        )
    )
    report = SimpleNamespace(
        results=(
            SimpleNamespace(
                status=status,
                database_committed=True,
            ),
        )
    )

    bus.run_records_deleted.emit(report)

    assert reader.calls == 2
    module.deleteLater()


@pytest.mark.parametrize("status", ["ROLLED_BACK", "UNKNOWN"])
def test_uncommitted_deletion_signal_does_not_refresh_today_statistics(
    qapp,
    status,
):
    bus = _BusStub()
    reader = _RunExportReaderStub(())
    module = TodayStatisticsModule(
        SimpleNamespace(
            bus=bus,
            run_export_reader=reader,
            audio_data_service=_LegacyAudioDataServiceStub(),
        )
    )
    report = SimpleNamespace(
        results=(
            SimpleNamespace(
                status=status,
                database_committed=False,
            ),
        )
    )

    bus.run_records_deleted.emit(report)

    assert reader.calls == 1
    module.deleteLater()
