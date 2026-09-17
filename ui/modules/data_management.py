from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from threading import Event

from PyQt6.QtCore import (
    QDateTime,
    QSize,
    QSignalBlocker,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
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
from app.service.run_record_export import RunDataExportResult
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
from ui.popups.historical_analysis_dialog import HistoricalAnalysisDialog
from ui.popups.run_audio_replay_dialog import RunAudioReplayDialog
from ui.popups.run_record_export_dialog import (
    choose_run_export_options,
    choose_run_export_path,
)


_BEIJING_TIMEZONE = timezone(timedelta(hours=8))
_GOOD_MANAGEMENT_OPTION_LABEL = "合格（OK）"


class _ExportCanceledError(RuntimeError):
    pass


class _ExportWorker(QThread):
    progress_changed = pyqtSignal(int, int, str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(
        self,
        export_runner: Callable[
            [Callable[[int, int, str], None], Callable[[], None]],
            RunDataExportResult,
        ],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._export_runner = export_runner
        self._cancel_requested = Event()

    def request_cancel(self) -> None:
        self._cancel_requested.set()

    def run(self) -> None:
        try:
            self._raise_if_cancel_requested()
            result = self._export_runner(
                self._emit_progress,
                self._raise_if_cancel_requested,
            )
        except _ExportCanceledError:
            self.canceled.emit()
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)

    def _emit_progress(self, completed: int, total: int, message: str) -> None:
        self.progress_changed.emit(completed, total, message)

    def _raise_if_cancel_requested(self) -> None:
        if self._cancel_requested.is_set():
            raise _ExportCanceledError("export canceled")


class _CachedFaultTypeCombo(QComboBox):
    def __init__(self, refresh_options, parent=None):
        super().__init__(parent)
        self._refresh_options = refresh_options

    def showPopup(self) -> None:
        self._refresh_options(self)
        super().showPopup()

    def wheelEvent(self, event) -> None:
        event.ignore()


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
    _ANALYSIS_COLUMN = len(_columns) + 1
    _PLAYBACK_COLUMN = _ANALYSIS_COLUMN + 1
    _DELETE_COLUMN = _PLAYBACK_COLUMN + 1
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
        self._selection_scope: tuple[object, ...] | None = None
        self._selected_run_id_set: set[str] = set()
        self._operation_active = False
        self._operation_control_state: tuple[
            tuple[tuple[QWidget, bool], ...],
            tuple[Qt.ItemFlag, ...],
            tuple[bool, ...],
        ] | None = None
        self._export_worker: _ExportWorker | None = None
        self._export_progress: QProgressDialog | None = None
        self._report_dialog = None
        self._replay_dialogs: dict[str, RunAudioReplayDialog] = {}
        self._analysis_dialogs: dict[str, HistoricalAnalysisDialog] = {}
        self._initial_column_widths_applied = False
        self._fault_type_options_cache: tuple[
            tuple[str, str | None],
            ...,
        ] = (("请选择", None),)
        self._fault_type_option_names: frozenset[str] = frozenset()

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
        self.btn_select_page = QPushButton("选当前页")
        self.btn_select_page.setCheckable(True)
        self.btn_export = QPushButton("导出")
        self.btn_batch_fault_type = QPushButton("批量修改故障类型")
        self.btn_batch_delete = QPushButton("批量删除")
        self.lb_selected = QLabel("已选 0 条")

        toolbar_layout.addWidget(self.btn_refresh)
        toolbar_layout.addWidget(self.btn_statistics_report)
        toolbar_layout.addWidget(self.btn_select_all)
        toolbar_layout.addWidget(self.btn_select_page)
        toolbar_layout.addWidget(self.btn_export)
        toolbar_layout.addWidget(self.btn_batch_fault_type)
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

        self.table = QTableWidget(0, self._DELETE_COLUMN + 1, self)
        self.table.setHorizontalHeaderLabels(
            [
                "选择",
                *(label for _name, label in self._columns),
                "查看分析结果",
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
        self.btn_select_page.clicked.connect(self._toggle_select_current_page)
        self.btn_export.clicked.connect(self._export_selected_rows)
        self.btn_batch_fault_type.clicked.connect(
            self._batch_update_fault_type
        )
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
    def _selection_scope_from_query(
        request: RunManagementQuery,
    ) -> tuple[object, ...]:
        return (
            request.model,
            request.sn_contains,
            request.accepted_start_utc,
            request.accepted_end_exclusive_utc,
            tuple(sorted(judgment.value for judgment in request.judgments)),
            request.fault_type_contains,
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

        selection_scope = self._selection_scope_from_query(request)
        if selection_scope != self._selection_scope:
            self._selected_run_id_set.clear()
            self._selection_scope = selection_scope
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
                selector.setCheckState(
                    Qt.CheckState.Checked
                    if record.run_id in self._selected_run_id_set
                    else Qt.CheckState.Unchecked
                )
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
                        display = "" if value is None else str(value)
                    elif field_name == "accepted_at":
                        display = self._display_beijing_time(value)
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
                analysis_button = QPushButton("查看分析结果")
                analysis_button.setFixedHeight(24)
                analysis_button.setStyleSheet("padding: 2px 6px; border-radius: 6px;")
                analysis_button.clicked.connect(
                    lambda _checked=False, run_id=record.run_id: self._show_analysis(run_id)
                )
                self.table.setCellWidget(row_index, self._ANALYSIS_COLUMN, analysis_button)
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
                    self._PLAYBACK_COLUMN,
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
                    self._DELETE_COLUMN,
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
        self._refresh_fault_type_options_cache()
        self._refresh_visible_fault_type_combos()
        if not self._initial_column_widths_applied:
            self._initial_column_widths_applied = True
            QTimer.singleShot(0, self._apply_table_column_widths)

    def _apply_table_column_widths(self) -> None:
        if not hasattr(self, "table"):
            return
        header = self.table.horizontalHeader()
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Interactive,
            )

        minimum_widths = {
            "run_id": 300,
            "accepted_at": 152,
            "sn": 96,
            "model": 180,
            "judgment": 70,
            "judgment_source": 92,
            "fault_type": 150,
        }
        growth_weights = {
            "run_id": 1.25,
            "accepted_at": 0.80,
            "sn": 0.60,
            "model": 0.75,
            "judgment": 0.35,
            "judgment_source": 0.50,
            "fault_type": 0.70,
        }
        action_width = 48
        analysis_width = 120
        fixed_width = 54 + analysis_width + (2 * action_width)
        base_width = fixed_width + sum(minimum_widths.values())
        available_width = max(self.table.viewport().width() - 2, base_width)
        extra_width = max(0, available_width - base_width)
        total_weight = sum(growth_weights.values())

        self.table.setColumnWidth(0, 54)
        applied_widths: dict[int, int] = {}
        for column, (field_name, _label) in enumerate(
            self._columns,
            start=1,
        ):
            width = minimum_widths.get(field_name, 100)
            if field_name in growth_weights and extra_width:
                width += int(
                    extra_width * growth_weights[field_name] / total_weight
                )
            applied_widths[column] = width
            self.table.setColumnWidth(column, width)
        self.table.setColumnWidth(self._ANALYSIS_COLUMN, analysis_width)
        for column in (self._PLAYBACK_COLUMN, self._DELETE_COLUMN):
            self.table.setColumnWidth(column, action_width)
        used_width = fixed_width + sum(applied_widths.values())
        remainder = max(0, available_width - used_width)
        if remainder and applied_widths:
            fault_column = self._fault_type_column_index()
            target_column = (
                fault_column
                if fault_column is not None
                else max(applied_widths)
            )
            self.table.setColumnWidth(
                target_column,
                self.table.columnWidth(target_column) + remainder,
            )

    def _fault_type_column_index(self) -> int | None:
        for index, (field_name, _label) in enumerate(self._columns, start=1):
            if field_name == "fault_type":
                return index
        return None

    def _load_fault_type_options_from_database(
        self,
    ) -> tuple[tuple[tuple[str, str | None], ...], frozenset[str]]:
        options: list[tuple[str, str | None]] = [
            ("请选择", None),
            (_GOOD_MANAGEMENT_OPTION_LABEL, GOOD_MALFUNCTION_TYPE_NAME),
        ]
        seen: set[str] = {GOOD_MALFUNCTION_TYPE_NAME}
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
            if name in seen:
                continue
            number = str(row.malfunction_number or "").strip()
            label = f"{name}（{number}）" if number else name
            options.append((label, name))
            seen.add(name)
        return tuple(options), frozenset(seen)

    def _refresh_fault_type_options_cache(self) -> None:
        options, seen = self._load_fault_type_options_from_database()
        self._fault_type_options_cache = options
        self._fault_type_option_names = seen

    def _refresh_visible_fault_type_combos(self) -> None:
        fault_column = self._fault_type_column_index()
        if fault_column is None:
            return
        for row_index in range(self.table.rowCount()):
            combo = self.table.cellWidget(row_index, fault_column)
            if isinstance(combo, QComboBox):
                self._reload_fault_type_combo_options(combo)

    def _fault_type_options(
        self,
        current_value: str | None = None,
        accepted_fault_types: tuple[str, ...] | None = None,
    ) -> list[tuple[str, str | None]]:
        if accepted_fault_types is None:
            result = list(self._fault_type_options_cache)
            option_names = set(self._fault_type_option_names)
        else:
            result = self._fault_type_options_for_accepted_types(
                accepted_fault_types
            )
            option_names = {value for _label, value in result if value}
        current = str(current_value or "").strip()
        if current and current not in option_names:
            result.append((f"{current}（当前值）", current))
        return result

    def _fault_type_label_by_name(self) -> dict[str, str]:
        return {
            value: label
            for label, value in self._fault_type_options_cache
            if value
        }

    def _fault_type_options_for_accepted_types(
        self,
        accepted_fault_types: tuple[str, ...],
    ) -> list[tuple[str, str | None]]:
        label_by_name = self._fault_type_label_by_name()
        options: list[tuple[str, str | None]] = [
            ("请选择", None),
            (
                label_by_name.get(
                    GOOD_MALFUNCTION_TYPE_NAME,
                    _GOOD_MANAGEMENT_OPTION_LABEL,
                ),
                GOOD_MALFUNCTION_TYPE_NAME,
            ),
        ]
        names = self._normalized_fault_type_names(accepted_fault_types)
        if not names:
            names = (BAD_MALFUNCTION_TYPE_NAME,)
        seen = {GOOD_MALFUNCTION_TYPE_NAME}
        for name in names:
            if name in seen:
                continue
            options.append((label_by_name.get(name, name), name))
            seen.add(name)
        return options

    def _fault_type_choices_for_selected_runs(
        self,
        run_ids: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        accepted_by_run_id = self._accepted_fault_types_by_run_id(run_ids)
        allowed_sets: list[set[str]] = []
        for run_id in run_ids:
            accepted = accepted_by_run_id.get(run_id, ())
            names = set(self._normalized_fault_type_names(accepted))
            allowed = {GOOD_MALFUNCTION_TYPE_NAME}
            if names:
                allowed.update(names)
            else:
                allowed.add(BAD_MALFUNCTION_TYPE_NAME)
            allowed_sets.append(allowed)
        common = set.intersection(*allowed_sets) if allowed_sets else set()
        if GOOD_MALFUNCTION_TYPE_NAME not in common:
            common.add(GOOD_MALFUNCTION_TYPE_NAME)

        label_by_name = self._fault_type_label_by_name()
        choices: list[tuple[str, str]] = [
            (
                label_by_name.get(
                    GOOD_MALFUNCTION_TYPE_NAME,
                    _GOOD_MANAGEMENT_OPTION_LABEL,
                ),
                GOOD_MALFUNCTION_TYPE_NAME,
            )
        ]
        ordered_candidates: list[str] = [
            value
            for _label, value in self._fault_type_options_cache
            if value and value != GOOD_MALFUNCTION_TYPE_NAME
        ]
        for accepted in accepted_by_run_id.values():
            ordered_candidates.extend(
                name
                for name in self._normalized_fault_type_names(accepted)
                if name != GOOD_MALFUNCTION_TYPE_NAME
            )
        if BAD_MALFUNCTION_TYPE_NAME not in ordered_candidates:
            ordered_candidates.append(BAD_MALFUNCTION_TYPE_NAME)
        seen = {GOOD_MALFUNCTION_TYPE_NAME}
        for name in ordered_candidates:
            if name in seen or name not in common:
                continue
            choices.append((label_by_name.get(name, name), name))
            seen.add(name)
        return choices

    def _accepted_fault_types_by_run_id(
        self,
        run_ids: tuple[str, ...],
    ) -> dict[str, tuple[str, ...]]:
        service = self.services.run_data_management_service
        loader = getattr(service, "fault_type_options_for_run_ids", None)
        if callable(loader):
            accepted_by_run_id = {
                options.run_id: self._record_accepted_fault_types(options)
                for options in loader(run_ids)
            }
            missing = [
                run_id for run_id in run_ids if run_id not in accepted_by_run_id
            ]
            if missing:
                raise RuntimeError(f"缺少运行记录 {missing[0]} 的故障类型配置")
            return accepted_by_run_id
        selected = set(run_ids)
        return {
            str(record.run_id): self._record_accepted_fault_types(record)
            for record in self._records
            if str(record.run_id) in selected
        }

    @staticmethod
    def _normalized_fault_type_names(
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = str(value or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return tuple(names)

    def _create_fault_type_combo(self, record) -> QComboBox:
        combo = _CachedFaultTypeCombo(
            self._reload_fault_type_combo_options,
            self.table,
        )
        combo.setObjectName("data_management_fault_type_combo")
        combo.setFixedHeight(24)
        combo.setStyleSheet(
            """
            QComboBox#data_management_fault_type_combo {
                padding: 2px 22px 2px 8px;
                border-radius: 6px;
            }
            QComboBox#data_management_fault_type_combo::drop-down {
                width: 22px;
                border: 0px;
            }
            """
        )
        combo.setEditable(False)
        current = self._record_fault_type_selection(record)
        accepted_fault_types = self._record_accepted_fault_types(record)
        combo.setProperty("accepted_fault_types", accepted_fault_types)
        self._populate_fault_type_combo(combo, current, accepted_fault_types)
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

    def _reload_fault_type_combo_options(self, combo: QComboBox) -> None:
        accepted_fault_types = combo.property("accepted_fault_types")
        if not isinstance(accepted_fault_types, tuple):
            accepted_fault_types = ()
        self._populate_fault_type_combo(
            combo,
            combo.currentData(),
            accepted_fault_types,
        )

    def _populate_fault_type_combo(
        self,
        combo: QComboBox,
        current_value: str | None,
        accepted_fault_types: tuple[str, ...] | None = None,
    ) -> None:
        current = str(current_value or "").strip()
        combo.blockSignals(True)
        combo.clear()
        for label, value in self._fault_type_options(
            current,
            accepted_fault_types,
        ):
            combo.addItem(label, value)
        target_index = combo.findData(current) if current else 0
        combo.setCurrentIndex(target_index if target_index >= 0 else 0)
        combo.setProperty("last_committed_index", combo.currentIndex())
        combo.blockSignals(False)

    @staticmethod
    def _record_accepted_fault_types(record) -> tuple[str, ...]:
        values = getattr(record, "accepted_fault_types", ())
        if not isinstance(values, tuple):
            return ()
        return DataManagementModule._normalized_fault_type_names(values)

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

    @classmethod
    def _display_beijing_time(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = cls._parse_utc(text)
        except Exception:
            return text
        return parsed.astimezone(_BEIJING_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def _create_table_action_button(
        self,
        standard_pixmap: QStyle.StandardPixmap,
        object_name: str,
        tooltip: str,
    ) -> QPushButton:
        button = QPushButton(self.table)
        button.setObjectName(object_name)
        button.setIcon(self.style().standardIcon(standard_pixmap))
        button.setIconSize(QSize(13, 13))
        button.setToolTip(tooltip)
        button.setFixedSize(22, 22)
        button.setStyleSheet("padding: 0; margin: 0; border-radius: 4px;")
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

    def _show_analysis(self, run_id: str) -> None:
        dialog = self._analysis_dialogs.get(run_id)
        if dialog is None:
            dialog = HistoricalAnalysisDialog(run_id=run_id, services=self.services, parent=self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._analysis_dialogs[run_id] = dialog
            dialogs = self._analysis_dialogs

            def forget_dialog(*_args):
                if dialogs.get(run_id) is dialog:
                    dialogs.pop(run_id)

            dialog.destroyed.connect(forget_dialog)
            dialog.label_saved.connect(self.refresh_table)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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
            replay_dialogs = self._replay_dialogs
            dialog.destroyed.connect(
                lambda *_args, rid=run_id, dialogs=replay_dialogs: dialogs.pop(
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
        if checked:
            if self._applied_query is None:
                self._update_selected_count()
                return
            try:
                run_ids = (
                    self.services.run_data_management_service
                    .query_run_ids(replace(self._applied_query, page=1))
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "全选失败",
                    f"读取全部运行记录失败：{exc}",
                )
                self._update_selected_count()
                return
            self._selected_run_id_set = set(run_ids)
        else:
            self._selected_run_id_set.clear()
        self._sync_visible_selection()
        self._update_selected_count()

    def _toggle_select_current_page(self, checked: bool) -> None:
        if self._operation_active:
            return
        run_ids = self._current_page_run_ids()
        if checked:
            self._selected_run_id_set.update(run_ids)
        else:
            self._selected_run_id_set.difference_update(run_ids)
        self._sync_visible_selection()
        self._update_selected_count()

    def _current_page_run_ids(self) -> tuple[str, ...]:
        return tuple(str(record.run_id) for record in self._records)

    def _sync_visible_selection(self) -> None:
        self.table.blockSignals(True)
        try:
            for row_index in range(self.table.rowCount()):
                selector = self.table.item(row_index, 0)
                if selector is not None:
                    run_id = str(selector.data(Qt.ItemDataRole.UserRole))
                    state = (
                        Qt.CheckState.Checked
                        if run_id in self._selected_run_id_set
                        else Qt.CheckState.Unchecked
                    )
                    selector.setCheckState(state)
        finally:
            self.table.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            run_id = item.data(Qt.ItemDataRole.UserRole)
            if run_id is not None:
                if item.checkState() == Qt.CheckState.Checked:
                    self._selected_run_id_set.add(str(run_id))
                else:
                    self._selected_run_id_set.discard(str(run_id))
            self._update_selected_count()

    def _selected_run_ids(self) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for row_index in range(self.table.rowCount()):
            item = self.table.item(row_index, 1)
            if item is None:
                continue
            run_id = item.data(Qt.ItemDataRole.UserRole)
            if run_id is None:
                run_id = item.text()
            run_id = str(run_id)
            if run_id in self._selected_run_id_set and run_id not in seen:
                ordered.append(run_id)
                seen.add(run_id)

        for run_id in sorted(self._selected_run_id_set - seen):
            ordered.append(run_id)
        return tuple(ordered)

    def _update_selected_count(self) -> None:
        selected_count = len(self._selected_run_id_set)
        total_count = self._total_count
        all_selected = total_count > 0 and selected_count == total_count
        current_page_run_ids = self._current_page_run_ids()
        page_selected = bool(current_page_run_ids) and all(
            run_id in self._selected_run_id_set
            for run_id in current_page_run_ids
        )
        self.lb_selected.setText(f"已选 {selected_count} 条")
        self.btn_select_all.blockSignals(True)
        self.btn_select_page.blockSignals(True)
        try:
            self.btn_select_all.setChecked(all_selected)
            self.btn_select_all.setText(
                "取消全选" if all_selected else "全选"
            )
            self.btn_select_page.setChecked(page_selected)
            self.btn_select_page.setText(
                "取消当前页" if page_selected else "选当前页"
            )
        finally:
            self.btn_select_all.blockSignals(False)
            self.btn_select_page.blockSignals(False)

    def _export_selected_rows(self) -> None:
        if self._operation_active:
            return
        run_ids = self._selected_run_ids()
        if not run_ids:
            QMessageBox.warning(self, "提示", "请先勾选要导出的运行记录")
            return

        options = choose_run_export_options(
            self,
            selected_count=len(run_ids),
        )
        if options is None:
            return

        os.makedirs(self.data_dir, exist_ok=True)
        target_path = choose_run_export_path(
            self,
            output_kind=options.output_kind,
            initial_dir=self.data_dir,
        )
        if not target_path:
            return

        progress = self._export_progress_dialog(len(run_ids))
        progress_state = {
            "determinate": False,
            "value": 0,
            "revision": 0,
        }

        def progress_is_current(revision: int) -> bool:
            return (
                self._export_progress is progress
                and self._operation_active
                and progress_state["revision"] == revision
            )

        def update_progress(completed: int, total: int, message: str) -> None:
            if not progress_is_current(progress_state["revision"]):
                return
            progress_state["revision"] += 1
            revision = progress_state["revision"]
            if total <= 0:
                if not progress_state["determinate"]:
                    progress.setRange(0, 0)
            else:
                total = max(int(total), progress_state["value"])
                display_value = max(
                    progress_state["value"],
                    max(0, min(int(completed), total)),
                )
                progress_state["determinate"] = True
                progress_state["value"] = display_value
                progress.setRange(0, total)
                if not progress_is_current(revision):
                    return
                progress.setValue(display_value)
            # Modal widgets can process events internally. A terminal signal or
            # newer nested update must win over this callback's remaining writes.
            if not progress_is_current(revision):
                return
            progress.setLabelText(message)

        self._set_operation_active(True)
        progress.show()
        QApplication.processEvents()

        def run_export(
            progress_callback,
            cancel_check,
        ) -> RunDataExportResult:
            return self.services.run_archive_service.export(
                run_ids,
                target_path,
                options=options,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

        self._export_progress = progress
        worker = _ExportWorker(run_export, self)
        self._export_worker = worker
        progress.canceled.connect(self._cancel_export)
        worker.progress_changed.connect(
            update_progress,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.succeeded.connect(
            lambda result: self._finish_export(
                len(run_ids),
                result,
                None,
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.failed.connect(
            lambda error_message: self._finish_export(
                len(run_ids),
                None,
                error_message,
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.canceled.connect(
            lambda: self._finish_export(
                len(run_ids),
                None,
                None,
                canceled=True,
            ),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(
            self._clear_export_worker,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(
            worker.deleteLater,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.start()

    def _export_progress_dialog(self, run_count: int) -> QProgressDialog:
        progress = QProgressDialog(
            f"正在准备导出 {run_count} 条记录...",
            "取消",
            0,
            0,
            self,
        )
        progress.setWindowTitle("导出进度")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(380)
        return progress

    def _finish_export(
        self,
        run_count: int,
        result: RunDataExportResult | None,
        error_message: str | None,
        *,
        canceled: bool = False,
    ) -> None:
        progress = self._export_progress
        self._export_progress = None
        if progress is not None:
            progress.close()
        self._set_operation_active(False)

        if canceled:
            QMessageBox.information(
                self,
                "导出已取消",
                f"已取消导出 {run_count} 条运行记录",
            )
            return

        if error_message is None and result is not None:
            counts = result.counts
            lines = [f"已导出 {counts.exported_runs} 条运行记录"]
            if counts.audio_runs_skipped:
                lines.append(
                    f"音频文件缺失或跳过 {counts.audio_runs_skipped} 条"
                )
            if counts.current_runs_skipped:
                lines.append(
                    f"电流数据缺失或跳过 {counts.current_runs_skipped} 条"
                )
            lines.extend(("", f"导出位置：\n{result.target_path}"))
            QMessageBox.information(
                self,
                "导出完成",
                "\n".join(lines),
            )
        else:
            QMessageBox.warning(
                self,
                "导出失败",
                f"导出失败：{error_message}",
            )

    def _cancel_export(self) -> None:
        worker = self._export_worker
        if worker is not None:
            worker.request_cancel()
        progress = self._export_progress
        if progress is not None:
            progress.setLabelText("正在取消导出...")
            progress.setRange(0, 0)
            progress.setCancelButton(None)

    def _clear_export_worker(self) -> None:
        self._export_worker = None

    def _batch_update_fault_type(self) -> None:
        if self._operation_active:
            return
        run_ids = self._selected_run_ids()
        if not run_ids:
            QMessageBox.warning(
                self,
                "提示",
                "请先勾选要修改故障类型的运行记录",
            )
            return

        self._refresh_fault_type_options_cache()
        try:
            choices = self._fault_type_choices_for_selected_runs(run_ids)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "读取故障类型失败",
                f"读取已选记录可用故障类型失败：{exc}",
            )
            return
        if not choices:
            QMessageBox.warning(self, "提示", "没有可用的故障类型")
            return

        labels = [label for label, _value in choices]
        selected_label, accepted = QInputDialog.getItem(
            self,
            "批量修改故障类型",
            f"将已选 {len(run_ids)} 条记录修改为：",
            labels,
            0,
            False,
        )
        if not accepted:
            return

        fault_type = next(
            (
                value
                for label, value in choices
                if label == str(selected_label)
            ),
            None,
        )
        if not fault_type:
            return

        answer = QMessageBox.question(
            self,
            "确认批量修改",
            f"确定将已选的 {len(run_ids)} 条记录故障类型修改为"
            f"「{selected_label}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return

        failures: list[tuple[str, str]] = []
        self._set_operation_active(True)
        QApplication.processEvents()
        try:
            for run_id in run_ids:
                try:
                    updated = (
                        self.services.run_data_management_service
                        .update_fault_type(run_id, str(fault_type))
                    )
                except Exception as exc:
                    failures.append((run_id, str(exc)))
                    continue
                if updated is False:
                    failures.append((run_id, "数据库未更新"))
        finally:
            self._set_operation_active(False)

        self._selected_run_id_set.clear()
        self.refresh_table()
        success_count = len(run_ids) - len(failures)
        lines = [
            f"成功修改 {success_count} 条",
            f"失败 {len(failures)} 条",
        ]
        if failures:
            lines.append("明细：")
            lines.extend(
                f"{run_id}: {reason}"
                for run_id, reason in failures[:5]
            )
            remaining = len(failures) - 5
            if remaining > 0:
                lines.append(f"还有 {remaining} 条失败未展示")
        message = QMessageBox.warning if failures else QMessageBox.information
        message(self, "批量修改结果", "\n".join(lines))

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
            self._selected_run_id_set.difference_update(
                result.run_id
                for result in report.results
                if result.database_committed
            )
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
            self.btn_select_page,
            self.btn_export,
            self.btn_batch_fault_type,
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
