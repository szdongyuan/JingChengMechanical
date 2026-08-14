from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os

from PyQt6.QtCore import QDateTime, QSignalBlocker, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.service.run_persistence.data_management import (
    DeleteBatchReport,
    DeleteRunResult,
    DeleteRunStatus,
    ManagementJudgment,
    RunManagementQuery,
)
from app.service.run_scoped_playback import (
    PlaybackCode,
    PlaybackResult,
    PlaybackStatus,
    RunPlaybackRequest,
)
from consts.running_consts import base_dir
from model.malfunction_type_table import (
    BAD_MALFUNCTION_TYPE_NAME,
    GOOD_MALFUNCTION_TYPE_NAME,
    MalfunctionTypeTable,
    ensure_builtin_malfunction_type,
)
from ui.popups.historical_metric_report_dialog import HistoricalMetricReportDialog
from ui.popups.run_audio_replay_dialog import RunAudioReplayDialog


class DataManagementModule(QWidget):
    """Server-paged run-v2 listing, archive export, and irreversible deletion."""

    module_id = "data_management"
    _columns = (
        ("run_id", "运行 UUID"),
        ("accepted_at", "接收时间"),
        ("sn", "SN"),
        ("model", "产品型号"),
        ("judgment", "判定"),
        ("judgment_source", "判定来源"),
        ("fault_type", "故障类型"),
    )
    _PLAYBACK_FAILURE_MESSAGES = {
        PlaybackCode.BUSY: "回放服务正在忙，请稍后再试",
        PlaybackCode.PERSISTED_AUDIO_UNAVAILABLE: "此记录没有可回放的音频",
        PlaybackCode.PERSISTED_FACT_INVALID: "此记录的音频信息无效",
        PlaybackCode.CONFIG_SNAPSHOT_INVALID: "此记录的配置快照无效",
        PlaybackCode.CHANNEL_UNAVAILABLE: "此记录的回放通道不可用",
        PlaybackCode.WINDOW_UNAVAILABLE: "此记录的音频时长无效",
        PlaybackCode.AUDIO_LOAD_FAILED: "音频读取失败",
        PlaybackCode.DEADLINE_CLOCK_FAILED: "无法启动回放计时",
        PlaybackCode.REPLACEMENT_TIMED_OUT: "停止上一条回放超时",
        PlaybackCode.REPLACEMENT_NON_CLEAN: "上一条回放未正常停止",
        PlaybackCode.BACKEND_START_FAILED: "音频回放设备启动失败",
        PlaybackCode.BACKEND_START_TIMED_OUT: "音频回放设备启动超时",
        PlaybackCode.SERVICE_CLOSED: "回放服务已关闭，请重新启动软件",
    }

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.data_dir = os.path.join(base_dir, "data")
        self._records: list[object] = []
        self._page = 1
        self._total_pages = 1
        self._total_count = 0
        self._applied_query: RunManagementQuery | None = None
        self._operation_active = False
        self._operation_control_state: tuple[
            tuple[tuple[QWidget, bool], ...],
            tuple[Qt.ItemFlag, ...],
            tuple[bool, ...],
        ] | None = None
        self._report_dialog = None
        self._replay_dialogs: dict[str, RunAudioReplayDialog] = {}
        self._initial_column_widths_applied = False

        self._build_ui()
        self.refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)

        self.btn_refresh = QPushButton("刷新")
        self.btn_statistics_report = QPushButton("统计报告")
        self.btn_select_all = QPushButton("全选")
        self.btn_select_all.setCheckable(True)
        self.btn_export = QPushButton("导出")
        self.btn_batch_delete = QPushButton("批量删除")
        self.lb_selected = QLabel("已选 0 条")

        toolbar_layout.addWidget(self.btn_refresh)
        toolbar_layout.addWidget(self.btn_statistics_report)
        toolbar_layout.addWidget(self.btn_select_all)
        toolbar_layout.addWidget(self.btn_export)
        toolbar_layout.addWidget(self.btn_batch_delete)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self.lb_selected)
        layout.addWidget(toolbar)

        filters = QWidget(self)
        filter_layout = QHBoxLayout(filters)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)
        self.edit_model = QLineEdit()
        self.edit_model.setPlaceholderText("产品型号")
        self.edit_sn = QLineEdit()
        self.edit_sn.setPlaceholderText("SN")
        self.edit_fault_type = QLineEdit()
        self.edit_fault_type.setPlaceholderText("故障类型")
        self.check_start = QCheckBox("开始")
        self.edit_start = self._date_time_edit()
        self.edit_start.setEnabled(False)
        self.check_end = QCheckBox("结束")
        self.edit_end = self._date_time_edit()
        self.edit_end.setEnabled(False)
        self.check_ok = QCheckBox("OK")
        self.check_ng = QCheckBox("NG")
        self.check_unjudged = QCheckBox("未判定")
        for checkbox in (self.check_ok, self.check_ng, self.check_unjudged):
            checkbox.setChecked(True)
        self.btn_search = QPushButton("查询")

        self.edit_model.setMinimumWidth(120)
        self.edit_sn.setMinimumWidth(120)
        self.edit_fault_type.setMinimumWidth(120)
        self.edit_start.setMinimumWidth(210)
        self.edit_end.setMinimumWidth(210)

        for widget in (
            self.edit_model,
            self.edit_sn,
            self.edit_fault_type,
            self.check_start,
            self.edit_start,
            self.check_end,
            self.edit_end,
            QLabel("本地判定"),
            self.check_ok,
            self.check_ng,
            self.check_unjudged,
            self.btn_search,
        ):
            filter_layout.addWidget(widget)
        filter_layout.addStretch(1)
        layout.addWidget(filters)

        self.table = QTableWidget(0, len(self._columns) + 3, self)
        self.table.setHorizontalHeaderLabels(
            [
                "选择",
                *(label for _name, label in self._columns),
                "回放",
                "删除",
            ]
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.table, 1)

        paging = QWidget(self)
        paging_layout = QHBoxLayout(paging)
        paging_layout.setContentsMargins(0, 0, 0, 0)
        paging_layout.setSpacing(8)
        paging_layout.addStretch(1)
        paging_layout.addWidget(QLabel("每页"))
        self.combo_page_size = QComboBox()
        self.combo_page_size.addItems(["20", "50", "100", "200"])
        self.combo_page_size.setCurrentText("50")
        self.btn_previous = QPushButton("上一页")
        self.btn_next = QPushButton("下一页")
        self.lb_page = QLabel("第 1/1 页，共 0 条")
        paging_layout.addWidget(self.combo_page_size)
        paging_layout.addWidget(self.btn_previous)
        paging_layout.addWidget(self.btn_next)
        paging_layout.addWidget(self.lb_page)
        layout.addWidget(paging)

        self.btn_refresh.clicked.connect(self.refresh_table)
        self.btn_statistics_report.clicked.connect(self._show_statistics_report)
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_export.clicked.connect(self._export_selected_rows)
        self.btn_batch_delete.clicked.connect(self._delete_selected_rows)
        self.btn_search.clicked.connect(self._apply_filters)
        self.edit_model.returnPressed.connect(self._apply_filters)
        self.edit_sn.returnPressed.connect(self._apply_filters)
        self.edit_fault_type.returnPressed.connect(self._apply_filters)
        self.check_start.toggled.connect(self.edit_start.setEnabled)
        self.check_end.toggled.connect(self.edit_end.setEnabled)
        self.combo_page_size.currentTextChanged.connect(
            self._page_size_changed
        )
        self.btn_previous.clicked.connect(self._previous_page)
        self.btn_next.clicked.connect(self._next_page)
        self.table.itemChanged.connect(self._on_item_changed)

    @staticmethod
    def _date_time_edit() -> QDateTimeEdit:
        edit = QDateTimeEdit(QDateTime.currentDateTime())
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        calendar = edit.calendarWidget()
        calendar.setMinimumSize(460, 340)
        calendar_view = calendar.findChild(
            QTableView,
            "qt_calendar_calendarview",
        )
        if calendar_view is not None:
            calendar_view.setMinimumWidth(440)
            calendar_view.setTextElideMode(Qt.TextElideMode.ElideNone)
            calendar_view.horizontalHeader().setMinimumSectionSize(48)
            calendar_view.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch
            )
        return edit

    def _query_from_widgets(self, page: int) -> RunManagementQuery:
        judgments = frozenset(
            judgment
            for checkbox, judgment in (
                (self.check_ok, ManagementJudgment.OK),
                (self.check_ng, ManagementJudgment.NG),
                (self.check_unjudged, ManagementJudgment.UNJUDGED),
            )
            if checkbox.isChecked()
        )
        start = (
            self._to_utc(self.edit_start.dateTime())
            if self.check_start.isChecked()
            else None
        )
        end = (
            self._to_utc(
                self.edit_end.dateTime(),
                end_exclusive=True,
            )
            if self.check_end.isChecked()
            else None
        )
        return RunManagementQuery(
            model=self.edit_model.text().strip(),
            sn_contains=self.edit_sn.text().strip(),
            accepted_start_utc=start,
            accepted_end_exclusive_utc=end,
            judgments=judgments,
            page=page,
            page_size=int(self.combo_page_size.currentText()),
            fault_type_contains=self.edit_fault_type.text().strip(),
        )

    @staticmethod
    def _to_utc(value: QDateTime, *, end_exclusive: bool = False) -> str:
        local_value = value.toPyDateTime()
        if local_value.tzinfo is None:
            local_value = local_value.replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        if end_exclusive:
            local_value += timedelta(seconds=1)
        return local_value.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    def refresh_table(self) -> None:
        if self._operation_active:
            return
        self._request_and_render(self._page)

    def _apply_filters(self) -> None:
        if self._operation_active:
            return
        self._request_and_render(1)

    def _page_size_changed(self, _value: str) -> None:
        if not self._operation_active:
            self._request_and_render(1)

    def _previous_page(self) -> None:
        if not self._operation_active and self._page > 1:
            self._request_and_render(self._page - 1)

    def _next_page(self) -> None:
        if not self._operation_active and self._page < self._total_pages:
            self._request_and_render(self._page + 1)

    def _request_and_render(self, page: int) -> bool:
        try:
            request = self._query_from_widgets(page)
        except Exception as exc:
            self._restore_applied_widgets()
            QMessageBox.warning(
                self,
                "查询条件错误",
                f"请检查筛选条件：{exc}",
            )
            return False
        return self._query_and_render(request)

    def _query_and_render(self, request: RunManagementQuery) -> bool:
        try:
            page = self.services.run_data_management_service.query_page(request)
        except Exception as exc:
            self._restore_applied_widgets()
            QMessageBox.warning(
                self,
                "查询失败",
                f"查询运行记录失败：{exc}",
            )
            return False

        self._records = list(page.rows)
        self._page = page.page
        self._total_pages = page.total_pages
        self._total_count = page.total_count
        self._applied_query = replace(request, page=page.page)
        self._render_rows()
        return True

    def _render_rows(self) -> None:
        self.table.blockSignals(True)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(self._records))
            for row_index, record in enumerate(self._records):
                selector = QTableWidgetItem()
                selector.setData(Qt.ItemDataRole.UserRole, record.run_id)
                selector.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                selector.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(row_index, 0, selector)
                for column_index, (field_name, _label) in enumerate(
                    self._columns,
                    start=1,
                ):
                    value = getattr(record, field_name)
                    if field_name == "fault_type":
                        item = QTableWidgetItem("")
                        item.setFlags(
                            Qt.ItemFlag.ItemIsEnabled
                            | Qt.ItemFlag.ItemIsSelectable
                        )
                        self.table.setItem(row_index, column_index, item)
                        self.table.setCellWidget(
                            row_index,
                            column_index,
                            self._create_fault_type_combo(record),
                        )
                        continue
                    if field_name == "run_id":
                        display = self._display_run_id(value)
                    else:
                        display = "" if value is None else str(value)
                    item = QTableWidgetItem(
                        display
                    )
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                    )
                    if field_name == "run_id":
                        item.setData(
                            Qt.ItemDataRole.UserRole,
                            record.run_id,
                        )
                        item.setToolTip(record.run_id)
                    self.table.setItem(row_index, column_index, item)
                play_button = self._create_table_action_button(
                    QStyle.StandardPixmap.SP_MediaPlay,
                    "data_management_play_button",
                    "播放完整音频",
                )
                play_button.clicked.connect(
                    lambda _checked=False, run_id=record.run_id: self._play_run(
                        run_id
                    )
                )
                self.table.setCellWidget(
                    row_index,
                    self.table.columnCount() - 2,
                    play_button,
                )
                delete_button = self._create_table_action_button(
                    QStyle.StandardPixmap.SP_TrashIcon,
                    "data_management_delete_button",
                    "永久删除此记录",
                )
                delete_button.clicked.connect(
                    lambda _checked=False, run_id=record.run_id: self._delete_run(
                        run_id
                    )
                )
                self.table.setCellWidget(
                    row_index,
                    self.table.columnCount() - 1,
                    delete_button,
                )
        finally:
            self.table.blockSignals(False)

        self._apply_table_column_widths()
        for row_index in range(self.table.rowCount()):
            self.table.setRowHeight(row_index, 30)
        self.lb_page.setText(
            f"第 {self._page}/{self._total_pages} 页，共 {self._total_count} 条"
        )
        self.btn_previous.setEnabled(self._page > 1)
        self.btn_next.setEnabled(self._page < self._total_pages)
        self._update_selected_count()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._initial_column_widths_applied:
            self._initial_column_widths_applied = True
            QTimer.singleShot(0, self._apply_table_column_widths)

    def _apply_table_column_widths(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Interactive,
            )
        if self.table.columnCount() > 4:
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, max(54, self.table.columnWidth(0)))
        fault_column = self._fault_type_column_index()
        if fault_column is not None:
            self.table.setColumnWidth(
                fault_column,
                max(128, self.table.columnWidth(fault_column)),
            )
        for column in (
            self.table.columnCount() - 2,
            self.table.columnCount() - 1,
        ):
            self.table.setColumnWidth(column, 48)

    def _fault_type_column_index(self) -> int | None:
        for index, (field_name, _label) in enumerate(self._columns, start=1):
            if field_name == "fault_type":
                return index
        return None

    def _fault_type_options(
        self,
        current_value: str | None = None,
    ) -> list[tuple[str, str | None]]:
        options: list[tuple[str, str | None]] = [("请选择", None)]
        seen: set[str] = set()
        try:
            ensure_builtin_malfunction_type()
            rows = list(
                MalfunctionTypeTable.select().order_by(MalfunctionTypeTable.id)
            )
        except Exception:
            rows = []
        for row in rows:
            name = str(row.malfunction_name or "").strip()
            if not name:
                continue
            number = str(row.malfunction_number or "").strip()
            label = f"{name}（{number}）" if number else name
            options.append((label, name))
            seen.add(name)
        current = str(current_value or "").strip()
        if current and current not in seen:
            options.append((f"{current}（当前值）", current))
        return options

    def _create_fault_type_combo(self, record) -> QComboBox:
        combo = QComboBox(self.table)
        combo.setMinimumHeight(26)
        combo.setEditable(False)
        for label, value in self._fault_type_options(record.fault_type):
            combo.addItem(label, value)
        current = self._record_fault_type_selection(record)
        target_index = combo.findData(current) if current else 0
        combo.blockSignals(True)
        combo.setCurrentIndex(target_index if target_index >= 0 else 0)
        combo.blockSignals(False)
        combo.setProperty("last_committed_index", combo.currentIndex())
        editable = getattr(record, "state", None) != "ACTIVE"
        combo.setEnabled(editable)
        if editable:
            combo.setToolTip("选择后会同步更新判定和数据库")
            combo.currentIndexChanged.connect(
                lambda _index, run_id=record.run_id, source=combo: (
                    self._on_fault_type_changed(run_id, source)
                )
            )
        else:
            combo.setToolTip("运行中的记录不能修改")
        return combo

    @staticmethod
    def _record_fault_type_selection(record) -> str:
        judgment = getattr(record, "judgment", None)
        fault_type = str(getattr(record, "fault_type", None) or "").strip()
        if judgment == "PASS":
            return GOOD_MALFUNCTION_TYPE_NAME
        if judgment == "FAIL":
            return fault_type or BAD_MALFUNCTION_TYPE_NAME
        return fault_type

    def _on_fault_type_changed(self, run_id: str, combo: QComboBox) -> None:
        fault_type = combo.currentData()
        previous_index = combo.property("last_committed_index")
        if previous_index is None:
            previous_index = 0
        if not fault_type:
            combo.blockSignals(True)
            combo.setCurrentIndex(int(previous_index))
            combo.blockSignals(False)
            return
        try:
            self.services.run_data_management_service.update_fault_type(
                run_id,
                str(fault_type),
            )
        except Exception as exc:
            combo.blockSignals(True)
            combo.setCurrentIndex(int(previous_index))
            combo.blockSignals(False)
            QMessageBox.warning(self, "修改失败", f"故障类型更新失败：{exc}")
            return
        combo.setProperty("last_committed_index", combo.currentIndex())
        self.refresh_table()

    @staticmethod
    def _display_run_id(run_id: object) -> str:
        text = str(run_id)
        if len(text) <= 12:
            return text
        return f"{text[:6]}…{text[-6:]}"

    def _create_table_action_button(
        self,
        standard_pixmap: QStyle.StandardPixmap,
        object_name: str,
        tooltip: str,
    ) -> QPushButton:
        button = QPushButton(self.table)
        button.setObjectName(object_name)
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setToolTip(tooltip)
        button.setFixedSize(28, 26)
        button.setStyleSheet("padding: 0; margin: 0;")
        return button

    def _resolve_full_run_playback_request(
        self,
        run_id: str,
    ) -> RunPlaybackRequest:
        records = self.services.run_export_reader.read_many((run_id,))
        if type(records) is not tuple or len(records) != 1:
            raise ValueError("one export record is required")
        record = records[0]
        if getattr(record, "run_id", None) != run_id:
            raise ValueError("export record run ID mismatch")
        return self._full_run_playback_request_from_record(record)

    @staticmethod
    def _full_run_playback_request_from_record(record) -> RunPlaybackRequest:
        run_id = getattr(record, "run_id", None)
        channel_order = getattr(record, "channel_order", None)
        frame_count = getattr(record, "frame_count", None)
        sample_rate_hz = getattr(record, "sample_rate_hz", None)
        if (
            type(channel_order) is not tuple
            or not channel_order
            or type(channel_order[0]) is not int
            or type(frame_count) is not int
            or frame_count <= 0
            or type(sample_rate_hz) is not int
            or sample_rate_hz <= 0
        ):
            raise ValueError("export audio facts are invalid")
        return RunPlaybackRequest(
            run_id,
            channel_order[0],
            0.0,
            float(frame_count) / float(sample_rate_hz),
        )

    def _play_run(self, run_id: str) -> None:
        try:
            records = self.services.run_export_reader.read_many((run_id,))
            record = (
                records[0]
                if len(records) == 1
                and getattr(records[0], "run_id", None) == run_id
                else None
            )
        except Exception:
            record = None
        composition = getattr(self.services, "final_composition", None)
        playback = getattr(composition, "playback", None)
        if (
            record is not None
            and isinstance(getattr(record, "source_wav_path", None), str)
            and callable(getattr(playback, "play", None))
        ):
            try:
                dialog = RunAudioReplayDialog(
                    record=record,
                    playback=playback,
                    parent=self,
                )
            except Exception:
                QMessageBox.warning(self, "回放", "此记录的音频无法打开")
                return
            self._replay_dialogs[run_id] = dialog
            dialog.destroyed.connect(
                lambda *_args, rid=run_id: self._replay_dialogs.pop(
                    rid,
                    None,
                )
            )
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        try:
            request = (
                self._full_run_playback_request_from_record(record)
                if record is not None
                else self._resolve_full_run_playback_request(run_id)
            )
        except Exception:
            QMessageBox.warning(
                self,
                "回放",
                "此记录没有可回放的完整音频",
            )
            return
        play = getattr(playback, "play", None)
        if not callable(play):
            QMessageBox.warning(self, "回放", "回放服务不可用")
            return
        try:
            result = play(request)
        except Exception:
            QMessageBox.warning(self, "回放", "回放启动失败")
            return
        message = self._playback_failure_message(request, result)
        if message is not None:
            QMessageBox.warning(self, "回放", message)

    @classmethod
    def _playback_failure_message(
        cls,
        request: RunPlaybackRequest,
        result: object,
    ) -> str | None:
        if (
            type(result) is not PlaybackResult
            or result.run_id != request.run_id
        ):
            return "回放结果无效"
        if (
            result.status is PlaybackStatus.STARTED
            and result.code is PlaybackCode.STARTED
        ):
            return None
        return cls._PLAYBACK_FAILURE_MESSAGES.get(
            result.code,
            "回放结果无效",
        )

    def _restore_applied_widgets(self) -> None:
        query = self._applied_query
        if query is None:
            return
        blockers = [
            QSignalBlocker(widget)
            for widget in (
                self.edit_model,
                self.edit_sn,
                self.edit_fault_type,
                self.check_start,
                self.edit_start,
                self.check_end,
                self.edit_end,
                self.check_ok,
                self.check_ng,
                self.check_unjudged,
                self.combo_page_size,
            )
        ]
        self.edit_model.setText(query.model)
        self.edit_sn.setText(query.sn_contains)
        self.edit_fault_type.setText(query.fault_type_contains)
        self.check_start.setChecked(query.accepted_start_utc is not None)
        self.check_end.setChecked(
            query.accepted_end_exclusive_utc is not None
        )
        self.edit_start.setEnabled(query.accepted_start_utc is not None)
        self.edit_end.setEnabled(
            query.accepted_end_exclusive_utc is not None
        )
        if query.accepted_start_utc is not None:
            self.edit_start.setDateTime(
                self._utc_to_local_qdatetime(query.accepted_start_utc)
            )
        if query.accepted_end_exclusive_utc is not None:
            exclusive = self._parse_utc(query.accepted_end_exclusive_utc)
            inclusive = exclusive - timedelta(seconds=1)
            self.edit_end.setDateTime(
                QDateTime.fromPyDateTime(inclusive.astimezone())
            )
        self.check_ok.setChecked(
            ManagementJudgment.OK in query.judgments
        )
        self.check_ng.setChecked(
            ManagementJudgment.NG in query.judgments
        )
        self.check_unjudged.setChecked(
            ManagementJudgment.UNJUDGED in query.judgments
        )
        self.combo_page_size.setCurrentText(str(query.page_size))
        del blockers

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @classmethod
    def _utc_to_local_qdatetime(cls, value: str) -> QDateTime:
        return QDateTime.fromPyDateTime(cls._parse_utc(value).astimezone())

    def _show_statistics_report(self) -> None:
        if self._report_dialog is None:
            self._report_dialog = HistoricalMetricReportDialog(
                services=self.services,
                parent=self,
            )
            self._report_dialog.destroyed.connect(
                lambda *_: setattr(self, "_report_dialog", None)
            )
        self._report_dialog.show()
        self._report_dialog.raise_()
        self._report_dialog.activateWindow()

    def _toggle_select_all(self, checked: bool) -> None:
        if self._operation_active:
            return
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        try:
            for row_index in range(self.table.rowCount()):
                selector = self.table.item(row_index, 0)
                if selector is not None:
                    selector.setCheckState(state)
        finally:
            self.table.blockSignals(False)
        self._update_selected_count()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_selected_count()

    def _selected_run_ids(self) -> tuple[str, ...]:
        selected = []
        for row_index in range(self.table.rowCount()):
            selector = self.table.item(row_index, 0)
            if (
                selector is not None
                and selector.checkState() == Qt.CheckState.Checked
            ):
                selected.append(
                    str(selector.data(Qt.ItemDataRole.UserRole))
                )
        return tuple(selected)

    def _update_selected_count(self) -> None:
        selected_count = len(self._selected_run_ids())
        total_count = self.table.rowCount()
        all_selected = total_count > 0 and selected_count == total_count
        self.lb_selected.setText(f"已选 {selected_count} 条")
        self.btn_select_all.blockSignals(True)
        try:
            self.btn_select_all.setChecked(all_selected)
            self.btn_select_all.setText(
                "取消全选" if all_selected else "全选"
            )
        finally:
            self.btn_select_all.blockSignals(False)

    def _export_selected_rows(self) -> None:
        if self._operation_active:
            return
        run_ids = self._selected_run_ids()
        if not run_ids:
            QMessageBox.warning(self, "提示", "请先勾选要导出的运行记录")
            return

        os.makedirs(self.data_dir, exist_ok=True)
        target_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出运行记录",
            os.path.join(self.data_dir, "run-records.zip"),
            "Zip 文件 (*.zip);;所有文件 (*)",
        )
        if not target_path:
            return

        self._set_operation_active(True)
        QApplication.processEvents()
        resolved_target = None
        error_message = None
        try:
            resolved_target = self.services.run_archive_service.export(
                run_ids,
                target_path,
            )
        except Exception as exc:
            error_message = str(exc)
        finally:
            self._set_operation_active(False)

        if error_message is None:
            QMessageBox.information(
                self,
                "导出完成",
                f"已导出 {len(run_ids)} 条运行记录到：\n{resolved_target}",
            )
        else:
            QMessageBox.warning(
                self,
                "导出失败",
                f"导出失败：{error_message}",
            )

    def _delete_run(self, run_id: str) -> None:
        if self._operation_active:
            return
        answer = QMessageBox.question(
            self,
            "确认永久删除",
            f"确定永久删除运行 {run_id} 吗？\n此操作无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        report = self._perform_delete(
            lambda: self._single_delete_report(
                self.services.run_data_management_service.delete_one(run_id)
            )
        )
        if report is not None:
            self._finish_delete(report)

    def _delete_selected_rows(self) -> None:
        if self._operation_active:
            return
        run_ids = self._selected_run_ids()
        if not run_ids:
            QMessageBox.warning(self, "提示", "请先勾选要删除的运行记录")
            return
        answer = QMessageBox.question(
            self,
            "确认批量永久删除",
            f"确定永久删除已选的 {len(run_ids)} 条运行记录吗？\n"
            "此操作无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        report = self._perform_delete(
            lambda: self.services.run_data_management_service.delete_many(
                run_ids
            )
        )
        if report is not None:
            self._finish_delete(report)

    @staticmethod
    def _single_delete_report(result: DeleteRunResult) -> DeleteBatchReport:
        return DeleteBatchReport(
            results=(result,),
            unattempted_run_ids=(),
            restart_required=result.status is DeleteRunStatus.UNKNOWN,
        )

    def _perform_delete(self, operation) -> DeleteBatchReport | None:
        self._set_operation_active(True)
        QApplication.processEvents()
        try:
            return operation()
        except Exception as exc:
            QMessageBox.warning(
                self,
                "删除失败",
                f"删除运行记录失败：{exc}",
            )
            return None
        finally:
            self._set_operation_active(False)

    def _finish_delete(self, report: DeleteBatchReport) -> None:
        committed = any(
            result.database_committed for result in report.results
        )
        if committed:
            signal = getattr(
                getattr(self.services, "bus", None),
                "run_records_deleted",
                None,
            )
            if signal is not None:
                signal.emit(report)
        self.refresh_table()
        text, has_problem = self._delete_summary(report)
        message = QMessageBox.warning if has_problem else QMessageBox.information
        message(self, "删除结果", text)

    @staticmethod
    def _delete_summary(
        report: DeleteBatchReport,
    ) -> tuple[str, bool]:
        deleted = sum(
            result.status is DeleteRunStatus.DELETED
            for result in report.results
        )
        partial = sum(
            result.status is DeleteRunStatus.PARTIAL_FILE_REMAINS
            for result in report.results
        )
        failed = sum(
            not result.database_committed for result in report.results
        )
        unattempted = len(report.unattempted_run_ids)
        lines = [
            f"成功删除 {deleted} 条",
            f"数据库失败或拒绝 {failed} 条",
            f"文件残留 {partial} 条",
            f"未尝试 {unattempted} 条",
        ]
        details = tuple(
            detail
            for result in report.results
            if (detail := DataManagementModule._delete_result_detail(result))
        )
        if details:
            lines.append("明细：")
            lines.extend(details)
        residual_paths = tuple(
            result.residual_path
            for result in report.results
            if result.residual_path
        )
        if residual_paths:
            lines.append("残留文件：")
            lines.extend(residual_paths)
        if report.restart_required:
            lines.append("数据库状态未知，需要重启软件。")
        return "\n".join(lines), bool(
            partial or failed or unattempted or report.restart_required
        )

    @staticmethod
    def _delete_result_detail(result: DeleteRunResult) -> str | None:
        prefix = f"运行 {result.run_id}"
        if result.status is DeleteRunStatus.REJECTED_ACTIVE:
            return f"{prefix} 正在运行，不能删除。"
        if result.status is DeleteRunStatus.REJECTED_ATTEMPT:
            return f"{prefix} 正在等待或执行重新分析，不能删除。"
        if result.status is DeleteRunStatus.NOT_FOUND:
            return f"{prefix} 不存在或已被删除。"
        if result.status is DeleteRunStatus.ROLLED_BACK:
            return f"{prefix} 数据库删除失败：{result.reason}"
        if result.status is DeleteRunStatus.UNKNOWN:
            return f"{prefix} 数据库状态未知，需要重启软件。"
        if result.status is DeleteRunStatus.PARTIAL_FILE_REMAINS:
            return f"{prefix} 数据库已删除，但音频文件仍有残留。"
        return None

    def _operation_controls(self) -> tuple[QWidget, ...]:
        return (
            self.btn_refresh,
            self.edit_model,
            self.edit_sn,
            self.edit_fault_type,
            self.check_start,
            self.edit_start,
            self.check_end,
            self.edit_end,
            self.check_ok,
            self.check_ng,
            self.check_unjudged,
            self.btn_search,
            self.combo_page_size,
            self.btn_previous,
            self.btn_next,
            self.btn_select_all,
            self.btn_export,
            self.btn_batch_delete,
        )

    def _set_operation_active(self, active: bool) -> None:
        if active == self._operation_active:
            return
        if active:
            control_state = tuple(
                (control, control.isEnabled())
                for control in self._operation_controls()
            )
            selector_flags = tuple(
                self.table.item(row_index, 0).flags()
                for row_index in range(self.table.rowCount())
            )
            delete_enabled = tuple(
                self.table.cellWidget(
                    row_index,
                    self.table.columnCount() - 1,
                ).isEnabled()
                for row_index in range(self.table.rowCount())
            )
            self._operation_control_state = (
                control_state,
                selector_flags,
                delete_enabled,
            )
            self._operation_active = True
            for control, _enabled in control_state:
                control.setEnabled(False)
            for row_index, flags in enumerate(selector_flags):
                self.table.item(row_index, 0).setFlags(
                    flags & ~Qt.ItemFlag.ItemIsEnabled
                )
                self.table.cellWidget(
                    row_index,
                    self.table.columnCount() - 1,
                ).setEnabled(False)
            return

        state = self._operation_control_state
        self._operation_active = False
        self._operation_control_state = None
        if state is None:
            return
        control_state, selector_flags, delete_enabled = state
        for control, enabled in control_state:
            control.setEnabled(enabled)
        for row_index, flags in enumerate(selector_flags):
            selector = self.table.item(row_index, 0)
            delete_button = self.table.cellWidget(
                row_index,
                self.table.columnCount() - 1,
            )
            if selector is not None:
                selector.setFlags(flags)
            if delete_button is not None:
                delete_button.setEnabled(delete_enabled[row_index])
