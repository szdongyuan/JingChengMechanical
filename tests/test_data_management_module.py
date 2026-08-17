from __future__ import annotations

from datetime import datetime, timezone
import os
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
    QFileDialog,
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
    RunManagementRow,
)
from app.service.run_scoped_playback import (
    PlaybackCode,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)
from ui.modules.data_management import DataManagementModule


RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"


def _row(
    run_id: str,
    *,
    sn: str = "SN-001",
    model: str = "MODEL-A",
    state: str = "SUCCEEDED",
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
        "回放",
        "删除",
    ]
    assert module.table.item(0, 1).text() == "111111…111111"
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
    assert module.btn_batch_delete.text() == "批量删除"
    assert isinstance(
        module.table.cellWidget(0, module.table.columnCount() - 1),
        QPushButton,
    )
    play = module.table.cellWidget(0, module.table.columnCount() - 2)
    delete = module.table.cellWidget(0, module.table.columnCount() - 1)
    fault_type = module.table.cellWidget(0, module.table.columnCount() - 3)
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
        module.btn_export,
    ):
        assert control.isEnabled()


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

    assert calls == []
    module.show()
    qapp.processEvents()

    assert calls == [0]
    fault_combo = module.table.cellWidget(0, module.table.columnCount() - 3)
    assert fault_combo.findData("缓存类型-A") >= 0

    management.next_page = _page(
        tuple(
            _row(f"33333333-3333-4333-8333-33333333333{index}")
            for index in range(5)
        )
    )
    module.refresh_table()

    assert calls == [0]
    fault_combo = module.table.cellWidget(0, module.table.columnCount() - 3)
    fault_combo.showPopup()
    fault_combo.hidePopup()
    assert calls == [0]
    assert fault_combo.findData("缓存类型-A") >= 0

    module.hide()
    qapp.processEvents()
    module.show()
    qapp.processEvents()

    assert calls == [0, 1]
    fault_combo = module.table.cellWidget(0, module.table.columnCount() - 3)
    assert fault_combo.findData("缓存类型-B") >= 0
    module.close()


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


def test_export_uses_shared_operation_gate_and_archive_service(
    qapp,
    monkeypatch,
    tmp_path,
):
    chosen = tmp_path / "operator-choice.zip"
    holder = {}

    class ArchiveService:
        def export(self, run_ids, target_path):
            module = holder["module"]
            assert module._operation_active is True
            assert all(not control.isEnabled() for control in module._operation_controls())
            assert run_ids == (RUN_A, RUN_B)
            assert target_path == str(chosen)
            return target_path

    module, _management, _deleted = _module(qapp, ArchiveService())
    holder["module"] = module
    _select_all_rows(module)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(chosen), "Zip files (*.zip)"),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)

    module._export_selected_rows()

    assert module._operation_active is False
    assert module.btn_refresh.isEnabled()
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
