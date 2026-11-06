from __future__ import annotations

from datetime import datetime, timezone
import os
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from scipy.io import wavfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTableView,
)

from app.service.run_persistence.data_management import (
    DeleteBatchReport,
    DeleteRunResult,
    DeleteRunStatus,
    ManagementJudgment,
    RunManagementPage,
    RunFaultTypeOptions,
    RunManagementRow,
)
from app.service.run_record_export import (
    AudioExportFacts,
    CurrentExportFacts,
    RunDataExportCounts,
    RunDataExportOptions,
    RunDataExportReadReport,
    RunDataExportRecord,
    RunDataExportResult,
)
from app.service.run_record_export.package import write_run_data_export
from app.service.run_scoped_playback import (
    PlaybackCode,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)
from ui.modules.data_management import DataManagementModule, _ExportWorker


RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"
RUN_C = "33333333-3333-4333-8333-333333333333"


def _row(
    run_id: str,
    *,
    sn: str = "SN-001",
    model: str = "MODEL-A",
    state: str = "SUCCEEDED",
    accepted_fault_types: tuple[str, ...] = (),
) -> RunManagementRow:
    return RunManagementRow(
        run_id=run_id,
        accepted_at="2026-07-29T01:02:03.000000Z",
        sn=sn,
        model=model,
        ingress_kind="CAPTURED",
        trigger_source="PLC",
        config_id=7,
        hardware_id="12",
        state=state,
        judgment="PASS" if state == "SUCCEEDED" else None,
        judgment_source="AUTOMATIC" if state == "SUCCEEDED" else "NONE",
        fault_type=None,
        manual_judgment_revision=None,
        terminal_code="ANALYSIS_SUCCEEDED" if state == "SUCCEEDED" else None,
        terminal_at=(
            "2026-07-29T01:02:04.000000Z"
            if state == "SUCCEEDED"
            else None
        ),
        accepted_fault_types=accepted_fault_types,
    )


def _page(
    rows=(_row(RUN_A), _row(RUN_B, state="FAILED")),
    *,
    page: int = 1,
    page_size: int = 50,
    total_count: int | None = None,
    total_pages: int = 1,
) -> RunManagementPage:
    return RunManagementPage(
        rows=tuple(rows),
        total_count=len(rows) if total_count is None else total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


class _SignalStub:
    def __init__(self) -> None:
        self.values = []

    def emit(self, value) -> None:
        self.values.append(value)


class _ManagementStub:
    def __init__(self) -> None:
        self.queries = []
        self.next_page = _page()
        self.query_error: Exception | None = None
        self.delete_one_result = DeleteRunResult(
            run_id=RUN_A,
            status=DeleteRunStatus.DELETED,
            reason="deleted",
            residual_path=None,
            database_committed=True,
        )
        self.delete_many_report = DeleteBatchReport(
            results=(),
            unattempted_run_ids=(),
            restart_required=False,
        )
        self.updated_fault_types = []
        self.fault_type_options_by_run_id: dict[str, tuple[str, ...]] = {}

    def query_page(self, request):
        self.queries.append(request)
        if self.query_error is not None:
            raise self.query_error
        return self.next_page

    def delete_one(self, run_id):
        assert run_id == self.delete_one_result.run_id
        return self.delete_one_result

    def delete_many(self, run_ids):
        self.deleted_many = run_ids
        return self.delete_many_report

    def update_fault_type(self, run_id, fault_type):
        self.updated_fault_types.append((run_id, fault_type))
        return True

    def fault_type_options_for_run_ids(self, run_ids):
        return tuple(
            RunFaultTypeOptions(
                run_id=run_id,
                accepted_fault_types=self.fault_type_options_by_run_id.get(
                    run_id,
                    (),
                ),
            )
            for run_id in run_ids
        )


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _module(qapp, archive_service=MagicMock()):
    management = _ManagementStub()
    deleted = _SignalStub()
    services = SimpleNamespace(
        run_data_management_service=management,
        run_archive_service=archive_service,
        run_export_reader=MagicMock(),
        final_composition=SimpleNamespace(playback=MagicMock()),
        historical_metric_report_service=object(),
        bus=SimpleNamespace(run_records_deleted=deleted),
        audio_data_service=SimpleNamespace(
            export_zip=MagicMock(
                side_effect=AssertionError(
                    "legacy SQLite-plus-WAV export must be unreachable"
                )
            )
        ),
    )
    return DataManagementModule(services), management, deleted


def _select_all_rows(module: DataManagementModule) -> None:
    for row_index in range(module.table.rowCount()):
        module.table.item(row_index, 0).setCheckState(Qt.CheckState.Checked)


def test_import_notification_preserves_applied_query_and_selection(qapp, monkeypatch):
    from app.service.offline_audio_import import ImportPhase, ImportStatus
    module, management, _ = _module(qapp)
    module.edit_sn.setText('applied')
    module.btn_search.click()
    query = module._applied_query
    module._selected_run_id_set.add(RUN_A)
    module.edit_sn.setText('unfinished edit')
    opened = []
    monkeypatch.setattr(module, '_show_analysis', opened.append)
    module._on_import_status(ImportStatus(RUN_A, 1, ImportPhase.COMPLETING_CHARTS, result_ready=True))
    assert module._applied_query == query
    assert RUN_A in module._selected_run_id_set
    assert opened == [RUN_A]


def test_import_entry_lists_all_saved_configs_with_no_production_selection(qapp, qtbot):
    module, _, _ = _module(qapp)
    qtbot.addWidget(module)
    module.services.offline_audio_import_service = SimpleNamespace(status=None)
    module.services.config_service = SimpleNamespace(
        list_configs=lambda: [{'config_id': 101}, {'config_id': 202}],
        load_config=lambda config_id: {'config_id': config_id, 'selected_channels': [0], 'analysis_duration': 1.})
    module.btn_import_audio.setEnabled(True)
    module.btn_import_audio.click()
    dialog = module._import_dialog
    assert dialog._config_selector.currentData() is None
    assert [dialog._config_selector.itemData(i) for i in range(1, 3)] == [101, 202]
    dialog.close()


def test_first_persisted_result_refreshes_an_already_open_history(qapp, monkeypatch):
    from app.service.offline_audio_import import ImportPhase, ImportStatus
    module, _, _ = _module(qapp)
    history = SimpleNamespace(refresh_enrichment=MagicMock())
    module._analysis_dialogs[RUN_A] = history
    monkeypatch.setattr(module, '_show_analysis', lambda run_id: None)
    module._on_import_status(ImportStatus(RUN_A, 1, ImportPhase.COMPLETING_CHARTS, result_ready=True))
    history.refresh_enrichment.assert_called_once_with()


def _analysis_button(module, row=0):
    buttons = module.table.findChildren(QPushButton)
    assert any(button.text() == "查看分析结果" for button in buttons)
    return module.table.cellWidget(row, module.table.columnCount() - 3)


def _historical_module(qapp, qtbot):
    from tests.test_historical_analysis_dialog import Reader, snapshot

    module, management, _ = _module(qapp)
    qtbot.addWidget(module)
    values = {run_id: snapshot(run_id=run_id) for run_id in (RUN_A, RUN_B)}
    reader = Reader(values[RUN_A])
    reader.read = lambda run_id: values[run_id]
    module.services.historical_analysis_reader = reader
    return module, management, reader, values


def test_analysis_buttons_open_exact_uuid_and_reuse_only_same_run(qapp, qtbot, monkeypatch):
    from ui.popups.historical_analysis_dialog import HistoricalAnalysisDialog

    module, _, _, _ = _historical_module(qapp, qtbot)
    _analysis_button(module).click()
    first = module._analysis_dialogs[RUN_A]
    qtbot.waitUntil(lambda: first.snapshot is not None)
    assert isinstance(first, HistoricalAnalysisDialog)
    assert first.run_id == first.snapshot.run.run_id == RUN_A
    assert first.services is module.services
    assert first.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    raised, activated = [], []
    monkeypatch.setattr(first, "raise_", lambda: raised.append(True))
    monkeypatch.setattr(first, "activateWindow", lambda: activated.append(True))
    _analysis_button(module).click()
    assert module._analysis_dialogs == {RUN_A: first}
    assert raised == activated == [True]
    _analysis_button(module, 1).click()
    second = module._analysis_dialogs[RUN_B]
    qtbot.waitUntil(lambda: second.snapshot is not None)
    assert second is not first
    assert second.snapshot.run.run_id == RUN_B
    first.close()
    qtbot.waitUntil(lambda: RUN_A not in module._analysis_dialogs)
    assert module._analysis_dialogs == {RUN_B: second}
    _analysis_button(module).click()
    reopened = module._analysis_dialogs[RUN_A]
    qtbot.waitUntil(lambda: reopened.snapshot is not None)
    assert reopened is not first
    reopened.close()
    second.close()


def test_analysis_registry_cleanup_does_not_remove_replacement(qapp, qtbot):
    from PyQt6 import sip

    module, _, _, _ = _historical_module(qapp, qtbot)
    _analysis_button(module).click()
    previous = module._analysis_dialogs[RUN_A]
    qtbot.waitUntil(lambda: previous.snapshot is not None)
    replacement = object()
    module._analysis_dialogs[RUN_A] = replacement
    previous.close()
    qtbot.waitUntil(lambda: sip.isdeleted(previous))
    assert module._analysis_dialogs[RUN_A] is replacement
    module._analysis_dialogs.clear()


@pytest.mark.parametrize("fail_save", [False, True])
def test_analysis_label_save_refreshes_only_on_commit_preserving_list_context(
    qapp, qtbot, fail_save,
):
    from dataclasses import replace
    from model.malfunction_type_table import GOOD_MALFUNCTION_TYPE_NAME

    module, management, _, values = _historical_module(qapp, qtbot)
    module.edit_model.setText("MODEL-A")
    management.next_page = _page(page=2, total_count=60, total_pages=2)
    module._request_and_render(2)
    _select_all_rows(module)
    applied = module._applied_query
    management.queries.clear()

    def save(run_id, label):
        management.updated_fault_types.append((run_id, label))
        if fail_save:
            raise RuntimeError("database busy")
        values[run_id] = replace(values[run_id], run=replace(values[run_id].run,
            judgment="PASS", judgment_source="MANUAL"))
        return True

    management.update_fault_type = save
    _analysis_button(module).click()
    widget = module._analysis_dialogs[RUN_A]
    qtbot.waitUntil(lambda: widget.snapshot is not None)
    widget.label_combo.setCurrentIndex(widget.label_combo.findData(GOOD_MALFUNCTION_TYPE_NAME))
    widget.save_label_button.click()
    qtbot.waitUntil(lambda: widget.save_label_button.isEnabled())
    assert management.updated_fault_types == [(RUN_A, GOOD_MALFUNCTION_TYPE_NAME)]
    assert management.queries == ([] if fail_save else [applied])
    assert widget.snapshot.run.judgment == ("FAIL" if fail_save else "PASS")
    assert module._applied_query == applied
    assert module._page == 2
    assert _visible_checked_run_ids(module) == {RUN_A, RUN_B}
    if fail_save:
        assert "database busy" in widget.status_label.text()
    widget.close()


def test_analysis_text_column_has_space_without_changing_old_action_widths(qapp):
    module, _, _ = _module(qapp)
    button = _analysis_button(module)
    assert button.text() == "查看分析结果"
    assert module.table.columnWidth(module.table.columnCount() - 3) >= 120
    assert module.table.columnWidth(module.table.columnCount() - 2) == 48
    assert module.table.columnWidth(module.table.columnCount() - 1) == 48


def test_analysis_button_text_fits_compact_row_with_application_style(qapp, qtbot, monkeypatch):
    from consts.style import APP_QSS
    from PyQt6.QtWidgets import QStyle, QStyleOptionButton

    module, _, _ = _module(qapp)
    qtbot.addWidget(module)
    monkeypatch.setattr(module, "_refresh_fault_type_options_cache", lambda: None)
    module.setStyleSheet(APP_QSS)
    module.resize(1550, 450)
    module.show()
    QApplication.processEvents()
    button = _analysis_button(module)
    option = QStyleOptionButton()
    option.initFrom(button)
    contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
    assert contents.height() >= button.fontMetrics().height()
    assert button.sizeHint().height() <= module.table.rowHeight(0)
    assert button.sizeHint().width() <= module.table.columnWidth(module._ANALYSIS_COLUMN)


def _visible_checked_run_ids(module: DataManagementModule) -> set[str]:
    checked = set()
    for row_index in range(module.table.rowCount()):
        selector = module.table.item(row_index, 0)
        if selector.checkState() == Qt.CheckState.Checked:
            checked.add(str(selector.data(Qt.ItemDataRole.UserRole)))
    return checked


def _wait_for_operation_idle(
    module: DataManagementModule,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while (
        module._operation_active or module._export_worker is not None
    ) and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()
    assert module._operation_active is False
    assert module._export_worker is None


def _export_result(
    target_path: str,
    *,
    options: RunDataExportOptions,
    selected_runs: int = 2,
    exported_runs: int = 2,
    skipped_runs: int = 0,
    audio_runs_skipped: int = 0,
    current_runs_skipped: int = 0,
) -> RunDataExportResult:
    return RunDataExportResult(
        target_path=target_path,
        output_kind=options.output_kind,
        counts=RunDataExportCounts(
            selected_runs=selected_runs,
            exported_runs=exported_runs,
            skipped_runs=skipped_runs,
            audio_runs_emitted=0,
            audio_runs_skipped=audio_runs_skipped,
            audio_files_emitted=0,
            current_runs_emitted=0,
            current_runs_skipped=current_runs_skipped,
        ),
    )


def test_playback_uses_full_uuid_first_frozen_channel_and_full_duration(qapp):
    module, _management, _deleted = _module(qapp)
    record = SimpleNamespace(
        run_id=RUN_A,
        channel_order=(7, 3),
        frame_count=96000,
        sample_rate_hz=48000,
    )
    module.services.run_export_reader.read_many.return_value = (record,)
    module.services.final_composition.playback.play.return_value = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_A,
        PlaybackCode.STARTED,
        "Playback started.",
    )

    module.table.cellWidget(0, module.table.columnCount() - 2).click()

    module.services.run_export_reader.read_many.assert_called_once_with((RUN_A,))
    request = module.services.final_composition.playback.play.call_args.args[0]
    assert request == RunPlaybackRequest(RUN_A, 7, 0.0, 2.0)


def test_playback_resolves_current_service_and_maps_exact_busy_result(qapp, monkeypatch):
    module, _management, _deleted = _module(qapp)
    module.services.run_export_reader.read_many.return_value = (
        SimpleNamespace(
            run_id=RUN_A,
            channel_order=(2,),
            frame_count=48000,
            sample_rate_hz=48000,
        ),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    replacement = MagicMock()
    replacement.play.return_value = PlaybackResult(
        PlaybackStatus.UNAVAILABLE,
        RUN_A,
        PlaybackCode.BUSY,
        "Playback is busy with another operation.",
    )
    module.services.final_composition = SimpleNamespace(playback=replacement)

    module._play_run(RUN_A)

    replacement.play.assert_called_once()
    assert warnings[-1][2] == "回放服务正在忙，请稍后再试"
    assert module.table.rowCount() == 2
    assert module.table.cellWidget(0, module.table.columnCount() - 1).isEnabled()


@pytest.mark.parametrize(
    ("status", "code", "summary", "message"),
    [
        (PlaybackStatus.UNAVAILABLE, PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE, "Persisted audio is unavailable for the selected run.", "此记录没有可回放的音频"),
        (PlaybackStatus.FAILED, PlaybackCode.PERSISTED_FACT_INVALID, "Persisted playback facts are invalid.", "此记录的音频信息无效"),
        (PlaybackStatus.FAILED, PlaybackCode.CONFIG_SNAPSHOT_INVALID, "Frozen playback configuration is invalid.", "此记录的配置快照无效"),
        (PlaybackStatus.UNAVAILABLE, PlaybackCode.CHANNEL_UNAVAILABLE, "The selected channel is unavailable for persisted playback.", "此记录的回放通道不可用"),
        (PlaybackStatus.UNAVAILABLE, PlaybackCode.WINDOW_UNAVAILABLE, "The selected window is unavailable for persisted playback.", "此记录的音频时长无效"),
        (PlaybackStatus.FAILED, PlaybackCode.AUDIO_LOAD_FAILED, "Persisted audio could not be loaded safely.", "音频读取失败"),
        (PlaybackStatus.FAILED, PlaybackCode.DEADLINE_CLOCK_FAILED, "Playback could not establish a safe deadline.", "无法启动回放计时"),
        (PlaybackStatus.FAILED, PlaybackCode.REPLACEMENT_TIMED_OUT, "The previous playback did not stop before the replacement deadline.", "停止上一条回放超时"),
        (PlaybackStatus.FAILED, PlaybackCode.REPLACEMENT_NON_CLEAN, "The previous playback did not stop cleanly.", "上一条回放未正常停止"),
        (PlaybackStatus.FAILED, PlaybackCode.BACKEND_START_FAILED, "Playback could not start.", "音频回放设备启动失败"),
        (PlaybackStatus.FAILED, PlaybackCode.BACKEND_START_TIMED_OUT, "Playback did not start before the startup deadline.", "音频回放设备启动超时"),
        (PlaybackStatus.CLOSED, PlaybackCode.SERVICE_CLOSED, "Playback is shut down.", "回放服务已关闭，请重新启动软件"),
    ],
)
def test_playback_exact_failures_have_concise_chinese_messages(
    status,
    code,
    summary,
    message,
):
    request = RunPlaybackRequest(RUN_A, 0, 0.0, 1.0)
    result = PlaybackResult(status, RUN_A, code, summary)

    assert DataManagementModule._playback_failure_message(request, result) == message


def test_playback_truthy_non_result_and_mismatched_run_are_invalid():
    request = RunPlaybackRequest(RUN_A, 0, 0.0, 1.0)
    started_for_other_run = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_B,
        PlaybackCode.STARTED,
        "Playback started.",
    )

    assert DataManagementModule._playback_failure_message(request, object()) == "回放结果无效"
    assert DataManagementModule._playback_failure_message(
        request,
        started_for_other_run,
    ) == "回放结果无效"


def test_page_projects_sn_model_filters_paging_and_destructive_controls(qapp):
    module, management, _deleted = _module(qapp)

    assert len(management.queries) == 1
    assert management.queries[0].page_size == 50
    assert [
        module.table.horizontalHeaderItem(index).text()
        for index in range(module.table.columnCount())
    ] == [
        "选择",
        "运行 UUID",
        "接收时间",
        "SN",
        "产品型号",
        "判定",
        "判定来源",
        "故障类型",
        "查看分析结果",
        "回放",
        "删除",
    ]
    assert module.table.item(0, 1).text() == RUN_A
    assert module.table.item(0, 1).toolTip() == RUN_A
    assert module.table.item(0, 1).data(Qt.ItemDataRole.UserRole) == RUN_A
    assert module.table.item(0, 2).text() == "2026-07-29 09:02:03"
    assert module.table.item(0, 3).text() == "SN-001"
    assert module.table.item(0, 4).text() == "MODEL-A"
    assert module.edit_model.placeholderText() == "产品型号"
    assert module.edit_sn.placeholderText() == "SN"
    assert module.edit_fault_type.placeholderText() == "故障类型"
    assert module.edit_start.minimumWidth() >= 168
    assert module.edit_end.minimumWidth() >= 168
    assert module.check_start.text() == "开始"
    assert module.check_end.text() == "结束"
    assert {
        module.check_ok.text(),
        module.check_ng.text(),
        module.check_unjudged.text(),
    } == {"OK", "NG", "未判定"}
    assert [
        module.combo_page_size.itemText(index)
        for index in range(module.combo_page_size.count())
    ] == ["20", "50", "100", "200"]
    assert module.btn_previous.text() == "上一页"
    assert module.btn_next.text() == "下一页"
    assert module.lb_page.text() == "第 1/1 页，共 2 条"
    assert module.btn_select_page.text() == "选当前页"
    assert module.btn_batch_fault_type.text() == "批量修改故障类型"
    assert module.btn_batch_delete.text() == "批量删除"
    assert isinstance(
        module.table.cellWidget(0, module.table.columnCount() - 1),
        QPushButton,
    )
    play = module.table.cellWidget(0, module.table.columnCount() - 2)
    delete = module.table.cellWidget(0, module.table.columnCount() - 1)
    fault_type = module.table.cellWidget(0, module._fault_type_column_index())
    assert isinstance(fault_type, QComboBox)
    assert fault_type.objectName() == "data_management_fault_type_combo"
    assert fault_type.height() == 24
    assert play.objectName() == "data_management_play_button"
    assert delete.objectName() == "data_management_delete_button"
    assert play.toolTip() == "播放完整音频"
    assert delete.toolTip() == "永久删除此记录"
    assert not play.icon().isNull()
    assert not delete.icon().isNull()
    assert play.width() == 22 and play.height() == 22
    assert delete.width() == 22 and delete.height() == 22
    assert play.iconSize().width() == 13
    assert delete.iconSize().width() == 13
    for control in (
        module.btn_refresh,
        module.btn_statistics_report,
        module.btn_select_all,
        module.btn_select_page,
        module.btn_export,
    ):
        assert control.isEnabled()


def test_select_current_page_toggles_only_visible_page(qapp):
    module, management, _deleted = _module(qapp)

    module.btn_select_page.click()

    assert module._selected_run_id_set == {RUN_A, RUN_B}
    assert _visible_checked_run_ids(module) == {RUN_A, RUN_B}
    assert module.btn_select_page.text() == "取消当前页"

    management.next_page = _page(
        (_row(RUN_C, sn="SN-003"),),
        page=2,
        total_count=51,
        total_pages=2,
    )
    assert module._request_and_render(2)

    assert module._selected_run_id_set == {RUN_A, RUN_B}
    assert _visible_checked_run_ids(module) == set()
    assert module.lb_selected.text() == "已选 2 条"
    assert module.btn_select_page.text() == "选当前页"
    assert not module.btn_select_all.isChecked()

    module.btn_select_page.click()

    assert module._selected_run_id_set == {RUN_A, RUN_B, RUN_C}
    assert _visible_checked_run_ids(module) == {RUN_C}
    assert module.btn_select_page.text() == "取消当前页"
    assert not module.btn_select_all.isChecked()

    module.btn_select_page.click()

    assert module._selected_run_id_set == {RUN_A, RUN_B}
    assert _visible_checked_run_ids(module) == set()
    assert module.btn_select_page.text() == "选当前页"
    assert not module.btn_select_all.isChecked()


def test_fault_type_options_are_cached_on_show_and_reused_by_combos(
    qapp,
    monkeypatch,
):
    calls = []

    def fake_load_fault_type_options(self):
        call_index = len(calls)
        calls.append(call_index)
        value = "缓存类型-A" if call_index == 0 else "缓存类型-B"
        return (
            (
                ("请选择", None),
                (f"{value}（FT）", value),
            ),
            frozenset({value}),
        )

    monkeypatch.setattr(
        DataManagementModule,
        "_load_fault_type_options_from_database",
        fake_load_fault_type_options,
    )

    module, management, _deleted = _module(qapp)
    management.next_page = _page(
        (_row(RUN_A, accepted_fault_types=("缓存类型-A",)),)
    )

    assert calls == []
    module.show()
    qapp.processEvents()
    module.refresh_table()

    assert calls == [0]
    fault_combo = module.table.cellWidget(0, module._fault_type_column_index())
    assert fault_combo.findData("缓存类型-A") >= 0

    management.next_page = _page(
        tuple(
            _row(
                f"33333333-3333-4333-8333-33333333333{index}",
                accepted_fault_types=("缓存类型-A",),
            )
            for index in range(5)
        )
    )
    module.refresh_table()

    assert calls == [0]
    fault_combo = module.table.cellWidget(0, module._fault_type_column_index())
    fault_combo.showPopup()
    fault_combo.hidePopup()
    assert calls == [0]
    assert fault_combo.findData("缓存类型-A") >= 0

    module.hide()
    qapp.processEvents()
    module.show()
    qapp.processEvents()

    assert calls == [0, 1]
    management.next_page = _page(
        (_row(RUN_B, accepted_fault_types=("缓存类型-B",)),)
    )
    module.refresh_table()
    fault_combo = module.table.cellWidget(0, module._fault_type_column_index())
    assert fault_combo.findData("缓存类型-B") >= 0
    module.close()


def test_fault_type_options_include_virtual_good_without_database_row(
    qapp,
    monkeypatch,
):
    rows = [
        SimpleNamespace(malfunction_name="良品", malfunction_number="OK"),
        SimpleNamespace(malfunction_name="不良", malfunction_number="NG"),
        SimpleNamespace(malfunction_name="轴伤音", malfunction_number="ZS"),
    ]

    class Query:
        def order_by(self, *_args):
            return rows

    monkeypatch.setattr(
        "ui.modules.data_management.ensure_builtin_malfunction_type",
        lambda: None,
    )
    monkeypatch.setattr(
        "ui.modules.data_management.MalfunctionTypeTable.select",
        lambda: Query(),
    )

    module, _management, _deleted = _module(qapp)
    options, seen = module._load_fault_type_options_from_database()

    assert options[0] == ("请选择", None)
    assert ("合格（OK）", "良品") in options
    assert ("不良（NG）", "不良") in options
    assert ("轴伤音（ZS）", "轴伤音") in options
    assert "良品" in seen
    assert sum(1 for _label, value in options if value == "良品") == 1


def test_fault_type_combo_filters_options_to_record_frozen_fault_types(
    qapp,
):
    module, management, _deleted = _module(qapp)
    module._fault_type_options_cache = (
        ("请选择", None),
        ("合格（OK）", "良品"),
        ("不良（NG）", "不良"),
        ("针响（ZX）", "针响"),
        ("轴真圆度音（ZZ）", "轴真圆度音"),
    )
    module._fault_type_option_names = frozenset(
        {"良品", "不良", "针响", "轴真圆度音"}
    )
    management.next_page = _page(
        (_row(RUN_A, accepted_fault_types=("不良", "针响")),)
    )

    module.refresh_table()

    fault_combo = module.table.cellWidget(0, module._fault_type_column_index())
    assert fault_combo.findData("良品") >= 0
    assert fault_combo.findData("不良") >= 0
    assert fault_combo.findData("针响") >= 0
    assert fault_combo.findData("轴真圆度音") < 0


def test_fault_type_combo_ignores_wheel_to_avoid_accidental_update(qapp):
    class WheelEventStub:
        def __init__(self) -> None:
            self.ignored = False

        def ignore(self) -> None:
            self.ignored = True

    module, management, _deleted = _module(qapp)
    fault_combo = module.table.cellWidget(0, module._fault_type_column_index())
    old_index = fault_combo.currentIndex()
    event = WheelEventStub()

    fault_combo.wheelEvent(event)

    assert event.ignored is True
    assert fault_combo.currentIndex() == old_index
    assert management.updated_fault_types == []


def test_filters_reset_page_and_convert_local_end_second_to_exclusive_utc(
    qapp,
):
    module, management, _deleted = _module(qapp)
    management.queries.clear()
    module._page = 4
    module.edit_model.setText(" Model-X ")
    module.edit_sn.setText(" abc%_ ")
    module.edit_fault_type.setText(" 轴承 ")
    module.check_start.setChecked(True)
    module.edit_start.setDateTime(QDateTime(2026, 7, 30, 8, 0, 0))
    module.check_end.setChecked(True)
    module.edit_end.setDateTime(QDateTime(2026, 7, 30, 8, 0, 5))

    module._apply_filters()

    query = management.queries[-1]
    assert query.page == 1
    assert query.model == "Model-X"
    assert query.sn_contains == "abc%_"
    assert query.fault_type_contains == "轴承"
    assert datetime.fromisoformat(
        query.accepted_start_utc.replace("Z", "+00:00")
    ) == datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(
        query.accepted_end_exclusive_utc.replace("Z", "+00:00")
    ) == datetime(2026, 7, 30, 0, 0, 6, tzinfo=timezone.utc)
    assert query.judgments == frozenset(ManagementJudgment)


def test_first_show_applies_table_widths_without_manual_refresh(qapp):
    module, management, _deleted = _module(qapp)
    query_count = len(management.queries)

    module.resize(1400, 700)
    module.show()
    qapp.processEvents()
    qapp.processEvents()

    assert module._initial_column_widths_applied is True
    assert len(management.queries) == query_count
    assert (
        module.table.horizontalHeader().sectionResizeMode(4)
        is QHeaderView.ResizeMode.Interactive
    )
    model_width = module.table.columnWidth(4)
    assert module.table.columnWidth(1) >= 300
    assert module.table.columnWidth(2) >= 152
    assert module.table.columnWidth(3) >= 96
    assert model_width >= 180
    assert model_width < module.table.viewport().width() // 4
    assert model_width <= module.table.columnWidth(2) + 40
    assert module.table.columnWidth(5) >= 70
    assert module.table.columnWidth(6) >= 92
    assert module.table.columnWidth(7) >= 150
    assert module.table.columnWidth(module.table.columnCount() - 2) == 48
    assert module.table.columnWidth(module.table.columnCount() - 1) == 48
    calendar = module.edit_start.calendarWidget()
    calendar_view = calendar.findChild(
        QTableView,
        "qt_calendar_calendarview",
    )
    assert module.edit_start.minimumWidth() >= 210
    assert calendar.minimumWidth() >= 460
    assert calendar_view is not None
    assert (
        calendar_view.textElideMode()
        is Qt.TextElideMode.ElideNone
    )
    module.close()


def test_replay_dialog_selects_channels_gain_and_time_range(
    qapp,
    tmp_path,
):
    module, _management, _deleted = _module(qapp)
    wav_path = tmp_path / "multi.wav"
    samples = np.column_stack(
        (
            np.arange(800, dtype=np.int16),
            -np.arange(800, dtype=np.int16),
        )
    )
    wavfile.write(wav_path, 8000, samples)
    record = SimpleNamespace(
        run_id=RUN_A,
        source_wav_path=str(wav_path),
        channel_order=(7, 3),
        frame_count=800,
        sample_rate_hz=8000,
    )
    module.services.run_export_reader.read_many.return_value = (record,)
    playback = module.services.final_composition.playback
    playback.play.return_value = PlaybackResult(
        PlaybackStatus.STARTED,
        RUN_A,
        PlaybackCode.STARTED,
        "Playback started.",
    )

    module._play_run(RUN_A)
    dialog = module._replay_dialogs[RUN_A]
    dialog.channel_list.item(1).setSelected(True)
    assert dialog.gain_spin.value() == 20.0
    assert dialog._range() == pytest.approx((0.0, 0.1))
    assert dialog._set_selection_range(0.01, 0.08) is True
    dialog._refresh_stft()
    dialog._play()

    request = playback.play.call_args.args[0]
    assert request.display_channel_index == 7
    assert request.display_channel_indices == (7, 3)
    assert request.window_start_seconds == pytest.approx(0.01)
    assert request.window_end_seconds == pytest.approx(0.08)
    assert request.gain_db_override == 20.0
    assert dialog.region.movable is False
    assert not hasattr(dialog, "spectrum_plot")
    dialog.close()


def test_query_failure_restores_the_previous_coherent_applied_state(
    qapp,
    monkeypatch,
):
    module, management, _deleted = _module(qapp)
    old_records = tuple(module._records)
    old_page_text = module.lb_page.text()
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(args),
    )
    management.query_error = RuntimeError("database unavailable")
    module.edit_model.setText("new-model")

    module._apply_filters()

    assert tuple(module._records) == old_records
    assert module.edit_model.text() == ""
    assert module.lb_page.text() == old_page_text
    assert "查询失败" in messages[0][1]
    assert "database unavailable" in messages[0][2]


def test_invalid_time_filter_is_reported_without_replacing_applied_state(
    qapp,
    monkeypatch,
):
    module, management, _deleted = _module(qapp)
    management.queries.clear()
    old_records = tuple(module._records)
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(args),
    )
    module.check_start.setChecked(True)
    module.edit_start.setDateTime(QDateTime(2026, 7, 30, 8, 0, 5))
    module.check_end.setChecked(True)
    module.edit_end.setDateTime(QDateTime(2026, 7, 30, 8, 0, 3))

    module._apply_filters()

    assert management.queries == []
    assert tuple(module._records) == old_records
    assert module.check_start.isChecked() is False
    assert module.check_end.isChecked() is False
    assert "查询条件错误" in messages[0][1]


def test_batch_update_fault_type_updates_selected_rows_and_refreshes(
    qapp,
    monkeypatch,
):
    module, management, _deleted = _module(qapp)
    management.queries.clear()
    management.fault_type_options_by_run_id = {
        RUN_A: ("轴伤音",),
        RUN_B: ("轴伤音",),
    }
    _select_all_rows(module)

    def fake_refresh_fault_types(self):
        self._fault_type_options_cache = (
            ("请选择", None),
            ("合格（OK）", "良品"),
            ("轴伤音（ZS）", "轴伤音"),
        )
        self._fault_type_option_names = frozenset({"良品", "轴伤音"})

    confirmations = []
    summaries = []
    monkeypatch.setattr(
        DataManagementModule,
        "_refresh_fault_type_options_cache",
        fake_refresh_fault_types,
    )
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *args: ("轴伤音（ZS）", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (
            confirmations.append(args),
            QMessageBox.StandardButton.Yes,
        )[1],
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: summaries.append(args),
    )

    module._batch_update_fault_type()

    assert management.updated_fault_types == [
        (RUN_A, "轴伤音"),
        (RUN_B, "轴伤音"),
    ]
    assert module._operation_active is False
    assert management.queries[-1].page == 1
    assert "已选的 2 条" in confirmations[0][2]
    assert "轴伤音（ZS）" in confirmations[0][2]
    assert "成功修改 2 条" in summaries[0][2]


def test_batch_update_fault_type_only_shows_common_frozen_options(
    qapp,
    monkeypatch,
):
    module, management, _deleted = _module(qapp)
    management.queries.clear()
    management.fault_type_options_by_run_id = {
        RUN_A: ("不良", "针响"),
        RUN_B: ("不良", "电枢音"),
    }
    _select_all_rows(module)

    def fake_refresh_fault_types(self):
        self._fault_type_options_cache = (
            ("请选择", None),
            ("合格（OK）", "良品"),
            ("不良（NG）", "不良"),
            ("针响（ZX）", "针响"),
            ("电枢音（DS）", "电枢音"),
        )
        self._fault_type_option_names = frozenset(
            {"良品", "不良", "针响", "电枢音"}
        )

    labels_seen = []
    monkeypatch.setattr(
        DataManagementModule,
        "_refresh_fault_type_options_cache",
        fake_refresh_fault_types,
    )

    def fake_get_item(_parent, _title, _prompt, labels, *_args):
        labels_seen.extend(labels)
        return "不良（NG）", True

    monkeypatch.setattr(QInputDialog, "getItem", fake_get_item)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    module._batch_update_fault_type()

    assert labels_seen == ["合格（OK）", "不良（NG）"]
    assert management.updated_fault_types == [
        (RUN_A, "不良"),
        (RUN_B, "不良"),
    ]


def test_export_warns_before_opening_settings_when_nothing_is_selected(
    qapp,
    monkeypatch,
):
    module, _management, _deleted = _module(qapp)
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *args, **kwargs: pytest.fail("settings must not open"),
    )

    module._export_selected_rows()

    assert warnings[0][1:] == ("提示", "请先勾选要导出的运行记录")
    assert module._operation_active is False


def test_export_settings_cancel_stops_before_path_selection(qapp, monkeypatch):
    module, _management, _deleted = _module(qapp)
    _select_all_rows(module)
    selected_counts = []
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda _parent, *, selected_count: (
            selected_counts.append(selected_count),
            None,
        )[1],
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *args, **kwargs: pytest.fail("path chooser must not open"),
    )

    module._export_selected_rows()

    assert selected_counts == [2]
    assert module._operation_active is False


@pytest.mark.parametrize(
    "options",
    (
        RunDataExportOptions(True, False, False, False),
        RunDataExportOptions(False, True, False, False),
        RunDataExportOptions(False, False, True, False),
        RunDataExportOptions(False, False, False, True),
        RunDataExportOptions(True, True, True, True),
    ),
)
def test_export_routes_output_kind_and_passes_immutable_options_to_service(
    qapp,
    monkeypatch,
    tmp_path,
    options,
):
    chosen = tmp_path / f"operator-choice.{options.output_kind}"
    calls = []

    class ArchiveService:
        def inspect_selection(self, _run_ids):
            raise AssertionError("legacy strict preflight must be unreachable")

        def export(
            self,
            run_ids,
            target_path,
            *,
            options,
            progress_callback,
            cancel_check,
        ):
            calls.append(
                (run_ids, target_path, options, progress_callback, cancel_check)
            )
            cancel_check()
            progress_callback(0, 0, "正在读取导出数据...")
            cancel_check()
            progress_callback(1, 1, "正在写入导出文件...")
            return _export_result(target_path, options=options)

    module, _management, _deleted = _module(qapp, ArchiveService())
    _select_all_rows(module)
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: options,
    )
    path_kinds = []
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda _parent, *, output_kind, initial_dir: (
            path_kinds.append((output_kind, initial_dir)),
            str(chosen),
        )[1],
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    module._export_selected_rows()

    assert module._operation_active is True
    assert all(not control.isEnabled() for control in module._operation_controls())
    _wait_for_operation_idle(module)

    assert len(calls) == 1
    assert set(calls[0][0]) == {RUN_A, RUN_B}
    assert calls[0][1] == str(chosen)
    assert calls[0][2] is options
    assert path_kinds == [(options.output_kind, module.data_dir)]
    assert module._operation_active is False
    assert module.btn_refresh.isEnabled()
    module.services.audio_data_service.export_zip.assert_not_called()


def test_export_path_cancel_stops_before_operation_starts(qapp, monkeypatch):
    module, _management, _deleted = _module(qapp)
    _select_all_rows(module)
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: RunDataExportOptions.defaults(),
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *_args, **_kwargs: None,
    )

    module._export_selected_rows()

    assert module._operation_active is False
    module.services.run_archive_service.export.assert_not_called()


def test_export_worker_returned_result_is_authoritative_when_cancel_is_requested(
    qapp,
):
    result = _export_result(
        "C:/exports/runs.zip",
        options=RunDataExportOptions.defaults(),
    )
    worker_ref = {}
    succeeded = []
    failed = []
    canceled = []

    def runner(_progress_callback, _cancel_check):
        worker_ref["worker"].request_cancel()
        return result

    worker = _ExportWorker(runner)
    worker_ref["worker"] = worker
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.canceled.connect(lambda: canceled.append(True))

    worker.run()

    assert succeeded == [result]
    assert failed == []
    assert canceled == []


def test_export_worker_reports_non_cancel_failure_even_when_cancel_is_requested(
    qapp,
):
    worker_ref = {}
    succeeded = []
    failed = []
    canceled = []

    def runner(_progress_callback, _cancel_check):
        worker_ref["worker"].request_cancel()
        raise RuntimeError("database write failed")

    worker = _ExportWorker(runner)
    worker_ref["worker"] = worker
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.canceled.connect(lambda: canceled.append(True))

    worker.run()

    assert succeeded == []
    assert failed == ["database write failed"]
    assert canceled == []


def test_export_worker_classifies_in_kernel_progress_cancellation_and_cleans_up(
    qapp,
    tmp_path,
):
    target = tmp_path / "progress-cancel.xlsx"
    target.write_bytes(b"existing")
    record = RunDataExportRecord(
        run_id=RUN_A,
        selection_index=0,
        accepted_at="2026-07-29T01:02:03.000000Z",
        terminal_at="2026-07-29T01:02:04.000000Z",
        sn="SN-001",
        model="MODEL-A",
        state="SUCCEEDED",
        terminal_code="ANALYSIS_SUCCEEDED",
        judgment="PASS",
        judgment_source="AUTOMATIC",
        fault_type=None,
        audio=AudioExportFacts(None, None, None, None, None, "not selected"),
        current=CurrentExportFacts(None, "not selected"),
    )
    report = RunDataExportReadReport((RUN_A,), (record,), ())
    options = RunDataExportOptions(True, False, False, False)
    worker_ref = {}
    progress_events = []
    succeeded = []
    failed = []
    canceled = []

    def runner(progress_callback, cancel_check):
        def arm_before_kernel_progress(completed, total, message):
            if message == "正在写入 Excel...":
                worker_ref["worker"].request_cancel()
            progress_callback(completed, total, message)

        return write_run_data_export(
            report,
            str(target),
            ai_values={},
            options=options,
            progress_callback=arm_before_kernel_progress,
            cancel_check=cancel_check,
        )

    worker = _ExportWorker(runner)
    worker_ref["worker"] = worker
    worker.progress_changed.connect(
        lambda *event: progress_events.append(event)
    )
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.canceled.connect(lambda: canceled.append(True))

    worker.run()

    assert any(event[2] == "正在写入 Excel..." for event in progress_events)
    assert succeeded == []
    assert failed == []
    assert canceled == [True]
    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".run-export-*.tmp")) == []
    assert list(tmp_path.glob(".run-export-stage-*")) == []


def test_export_progress_starts_indeterminate_then_is_monotonic(qapp, monkeypatch):
    options = RunDataExportOptions.defaults()

    class ProgressStub:
        def __init__(self):
            self.ranges = []
            self.values = []
            self.labels = []
            self.canceled = SimpleNamespace(connect=lambda _slot: None)

        def setRange(self, minimum, maximum):
            self.ranges.append((minimum, maximum))

        def setValue(self, value):
            self.values.append(value)

        def setLabelText(self, text):
            self.labels.append(text)

        def show(self):
            pass

        def close(self):
            pass

    progress = ProgressStub()

    class ArchiveService:
        def export(
            self,
            run_ids,
            target_path,
            *,
            options,
            progress_callback,
            cancel_check,
        ):
            cancel_check()
            progress_callback(0, 0, "正在读取...")
            progress_callback(2, 5, "2/5")
            progress_callback(1, 5, "late 1/5")
            progress_callback(4, 5, "4/5")
            return _export_result(target_path, options=options)

    module, _management, _deleted = _module(qapp, ArchiveService())
    _select_all_rows(module)
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: options,
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *_args, **_kwargs: "C:/exports/runs.zip",
    )
    monkeypatch.setattr(module, "_export_progress_dialog", lambda _count: progress)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    module._export_selected_rows()
    _wait_for_operation_idle(module)

    assert progress.ranges[0] == (0, 0)
    assert (0, 5) in progress.ranges
    assert progress.values == sorted(progress.values)
    assert progress.values[-1] == 4


@pytest.fixture
def captured_export_progress(qapp, monkeypatch):
    """Capture the real UI callback while controlling widget reentrant calls."""
    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot, *_args):
            self.slots.append(slot)

    class Worker:
        def __init__(self, *_args):
            for name in ("progress_changed", "succeeded", "failed", "canceled", "finished"):
                setattr(self, name, Signal())

        def start(self):
            pass

        def deleteLater(self):
            pass

    class Progress:
        def __init__(self):
            self.canceled = Signal()
            self.writes = []
            self.hook = None

        def write(self, method, value):
            self.writes.append((method, value))
            if self.hook is not None:
                self.hook(method)

        def setRange(self, minimum, maximum):
            self.write("range", (minimum, maximum))

        def setValue(self, value):
            self.write("value", value)

        def setLabelText(self, text):
            self.write("label", text)

        def show(self):
            pass

        def close(self):
            pass

    module, _, _ = _module(qapp)
    _select_all_rows(module)
    options = RunDataExportOptions.defaults()
    progress = Progress()
    monkeypatch.setattr("ui.modules.data_management._ExportWorker", Worker)
    monkeypatch.setattr("ui.modules.data_management.choose_run_export_options", lambda *a, **k: options)
    monkeypatch.setattr("ui.modules.data_management.choose_run_export_path", lambda *a, **k: "unused.zip")
    monkeypatch.setattr(module, "_export_progress_dialog", lambda _count: progress)
    monkeypatch.setattr(QMessageBox, "information", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    module._export_selected_rows()
    update = module._export_worker.progress_changed.slots[0]
    yield module, progress, update, options
    module._export_worker = None
    module.close()


@pytest.mark.parametrize("terminal", ["success", "failure", "cancel"])
@pytest.mark.parametrize("during", [None, "range", "value"])
def test_export_progress_ignores_terminal_and_replaced_dialogs(captured_export_progress, terminal, during):
    module, progress, update, options = captured_export_progress
    terminal_writes = []

    def finish():
        module._finish_export(
            2, _export_result("unused.zip", options=options) if terminal == "success" else None,
            "failure" if terminal == "failure" else None, canceled=terminal == "cancel",
        )
        terminal_writes[:] = progress.writes

    if during is None:
        finish()
    else:
        def hook(method):
            if method == during:
                progress.hook = None
                finish()
        progress.hook = hook
    update(1, 5, "stale")
    assert progress.writes == terminal_writes
    assert module._operation_active is False
    # Starting a subsequent operation must not reactivate this closure.
    module._export_worker = None
    replacement = type(progress)()
    module._export_progress_dialog = lambda _count: replacement
    module._export_selected_rows()
    before = list(replacement.writes)
    update(5, 5, "old operation")
    assert replacement.writes == before
    assert progress.writes == terminal_writes
    assert module._export_progress is replacement
    assert module._operation_active


@pytest.mark.parametrize("during", ["range", "value"])
def test_export_older_nested_progress_does_not_overwrite_newer_progress(captured_export_progress, during):
    module, progress, update, _ = captured_export_progress

    def hook(method):
        if method == during:
            progress.hook = None
            update(4, 5, "newer")

    progress.hook = hook
    update(1, 5, "older")
    assert [value for method, value in progress.writes if method == "value"][-1] == 4
    assert [value for method, value in progress.writes if method == "label"][-1] == "newer"


def test_export_success_reports_exported_and_nonzero_attachment_skip_counts(
    qapp,
    monkeypatch,
):
    options = RunDataExportOptions(True, True, True, True)

    class ArchiveService:
        def export(self, run_ids, target_path, **kwargs):
            kwargs["cancel_check"]()
            return _export_result(
                target_path,
                options=options,
                selected_runs=2,
                exported_runs=2,
                audio_runs_skipped=1,
                current_runs_skipped=2,
            )

    module, _management, _deleted = _module(qapp, ArchiveService())
    _select_all_rows(module)
    messages = []
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: options,
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *_args, **_kwargs: "C:/exports/runs.zip",
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: messages.append(args),
    )

    module._export_selected_rows()
    _wait_for_operation_idle(module)

    assert messages[0][1] == "导出完成"
    assert "已导出 2 条运行记录" in messages[0][2]
    assert "音频文件缺失或跳过 1 条" in messages[0][2]
    assert "电流数据缺失或跳过 2 条" in messages[0][2]
    assert "C:/exports/runs.zip" in messages[0][2]


def test_export_failure_restores_every_operation_control(qapp, monkeypatch):
    options = RunDataExportOptions.defaults()

    class ArchiveService:
        def export(self, *args, **kwargs):
            kwargs["cancel_check"]()
            raise RuntimeError("database unavailable")

    module, _management, _deleted = _module(qapp, ArchiveService())
    _select_all_rows(module)
    module.btn_statistics_report.setEnabled(False)
    before = tuple(control.isEnabled() for control in module._operation_controls())
    warnings = []
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: options,
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *_args, **_kwargs: "C:/exports/runs.zip",
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )

    module._export_selected_rows()
    _wait_for_operation_idle(module)

    after = tuple(control.isEnabled() for control in module._operation_controls())
    assert after == before
    assert "database unavailable" in warnings[0][2]
    module.services.audio_data_service.export_zip.assert_not_called()


def test_export_cancel_restores_controls_and_reports_canceled(
    qapp,
    monkeypatch,
    tmp_path,
):
    chosen = tmp_path / "operator-choice.zip"
    progress_seen = Event()
    continue_export = Event()
    calls = []
    messages = []
    warnings = []
    options = RunDataExportOptions.defaults()

    class ArchiveService:
        def export(
            self,
            run_ids,
            target_path,
            *,
            options,
            progress_callback,
            cancel_check,
        ):
            calls.append((run_ids, target_path))
            cancel_check()
            progress_callback(0, 0, "正在读取运行记录...")
            progress_seen.set()
            assert continue_export.wait(2)
            cancel_check()
            return _export_result(target_path, options=options)

    module, _management, _deleted = _module(qapp, ArchiveService())
    _select_all_rows(module)
    controls_before = tuple(
        control.isEnabled() for control in module._operation_controls()
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_options",
        lambda *_args, **_kwargs: options,
    )
    monkeypatch.setattr(
        "ui.modules.data_management.choose_run_export_path",
        lambda *_args, **_kwargs: str(chosen),
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: messages.append(args),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )

    module._export_selected_rows()
    assert progress_seen.wait(2)
    QApplication.processEvents()

    module._cancel_export()
    continue_export.set()
    _wait_for_operation_idle(module)

    assert len(calls) == 1
    assert tuple(
        control.isEnabled() for control in module._operation_controls()
    ) == controls_before
    assert warnings == []
    assert messages
    assert messages[0][1] == "导出已取消"
    module.services.audio_data_service.export_zip.assert_not_called()


def test_single_delete_confirms_emits_committed_report_and_refreshes_clamped_page(
    qapp,
    monkeypatch,
):
    module, management, deleted = _module(qapp)
    management.queries.clear()
    module._page = 3
    management.next_page = _page(
        (_row(RUN_B),),
        page=2,
        page_size=50,
        total_count=51,
        total_pages=2,
    )
    confirmations = []
    summaries = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: (
            confirmations.append(args),
            QMessageBox.StandardButton.Yes,
        )[1],
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args: summaries.append(args),
    )

    module._delete_run(RUN_A)

    assert RUN_A in confirmations[0][2]
    assert "永久删除" in confirmations[0][2]
    assert "无法恢复" in confirmations[0][2]
    assert deleted.values
    assert deleted.values[0].results[0].database_committed is True
    assert management.queries[-1].page == 3
    assert module._page == 2
    assert module.lb_page.text() == "第 2/2 页，共 51 条"
    assert "成功删除 1 条" in summaries[0][2]


def test_batch_delete_summary_includes_residual_and_unattempted_counts(
    qapp,
    monkeypatch,
):
    module, management, deleted = _module(qapp)
    _select_all_rows(module)
    management.delete_many_report = DeleteBatchReport(
        results=(
            DeleteRunResult(
                run_id=RUN_A,
                status=DeleteRunStatus.PARTIAL_FILE_REMAINS,
                reason="permission denied",
                residual_path="C:/audio/remains.wav",
                database_committed=True,
            ),
            DeleteRunResult(
                run_id=RUN_B,
                status=DeleteRunStatus.UNKNOWN,
                reason="unknown commit",
                residual_path=None,
                database_committed=False,
            ),
        ),
        unattempted_run_ids=(RUN_A,),
        restart_required=True,
    )
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(args),
    )

    module._delete_selected_rows()

    assert management.deleted_many == (RUN_A, RUN_B)
    assert deleted.values == [management.delete_many_report]
    text = messages[0][2]
    assert "文件残留 1 条" in text
    assert "未尝试 1 条" in text
    assert "C:/audio/remains.wav" in text
    assert "需要重启" in text


def test_active_delete_rejection_is_explained_in_chinese(
    qapp,
    monkeypatch,
):
    module, management, deleted = _module(qapp)
    management.delete_one_result = DeleteRunResult(
        run_id=RUN_A,
        status=DeleteRunStatus.REJECTED_ACTIVE,
        reason="active run cannot be deleted",
        residual_path=None,
        database_committed=False,
    )
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: messages.append(args),
    )

    module._delete_run(RUN_A)

    assert deleted.values == []
    assert RUN_A in messages[0][2]
    assert "正在运行，不能删除" in messages[0][2]
