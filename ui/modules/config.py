from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from consts.style import combo_qss
from ui.widgets.cards import Card
from ui.widgets.collapsible_group_box import CollapsibleGroupBox


class _NoWheelSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores mouse wheel events."""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class _FloatDelegate(QStyledItemDelegate):
    """单元格编辑器限制为浮点数输入。"""

    def createEditor(self, parent, option, index):
        editor = _NoWheelSpinBox(parent)
        editor.setRange(-999999.0, 999999.0)
        editor.setDecimals(2)
        editor.setFrame(False)
        editor.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        return editor

    def setEditorData(self, editor, index):
        try:
            value = float(index.data())
        except (TypeError, ValueError):
            value = 0.0
        editor.setValue(value)

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.value()))


class ConfigModule(QWidget):
    module_id = "config"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._current_config_id: int | None = None
        self._skip_reload = False

        self._init_ui()
        self._connect_signals()
        self._refresh_list()

    # ── UI construction ─────────────────────────────────────

    def _init_ui(self):
        self.setStyleSheet("QWidget { font-size: 14px; }")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        # Left panel: config list
        left = QWidget()
        left.setFixedWidth(220)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

        btn_row = QWidget()
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(6)
        self.btn_new = QPushButton("新建")
        self.btn_new.setObjectName("Primary")
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setObjectName("Danger")
        btn_lay.addWidget(self.btn_new)
        btn_lay.addWidget(self.btn_delete)
        left_lay.addWidget(btn_row)

        self.config_list = QListWidget()
        self.config_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.config_list.setStyleSheet(
            "QListWidget { background: #f8fafc; border: 1px solid #e5e7eb; "
            "border-radius: 8px; padding: 4px; }"
            "QListWidget::item { padding: 8px; border-radius: 6px; "
            "border: 1px solid transparent; margin: 2px 0; }"
            "QListWidget::item:selected { background: #e0edff; "
            "border: 2px solid #3b82f6; color: #1e40af; font-weight: 600; }"
            "QListWidget::item:hover:!selected { background: #f1f5f9; }"
        )
        left_lay.addWidget(self.config_list, 1)
        root.addWidget(left)

        # Right panel: detail
        self.right_panel = QWidget()
        self.right_lay = QVBoxLayout(self.right_panel)
        self.right_lay.setContentsMargins(0, 0, 0, 0)
        self.right_lay.setSpacing(10)

        # Empty state
        self.empty_label = QLabel("暂无配置，请点击「新建」创建")
        self.empty_label.setObjectName("Muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.right_lay.addWidget(self.empty_label)

        # Detail container (hidden when empty)
        self.detail = QWidget()
        detail_lay = QVBoxLayout(self.detail)
        detail_lay.setContentsMargins(0, 0, 0, 0)
        detail_lay.setSpacing(10)

        # Global params
        global_form = QWidget()
        gf_lay = QFormLayout(global_form)
        gf_lay.setContentsMargins(0, 0, 0, 0)
        gf_lay.setHorizontalSpacing(10)
        gf_lay.setVerticalSpacing(8)

        self.ed_name = QLineEdit()
        gf_lay.addRow("配置名称", self.ed_name)

        self.spin_record = _NoWheelSpinBox()
        self.spin_record.setRange(0.1, 3600.0)
        self.spin_record.setSingleStep(0.5)
        self.spin_record.setSuffix(" 秒")
        self.spin_record.setValue(5.0)
        gf_lay.addRow("录音时长", self.spin_record)

        self.spin_analysis = _NoWheelSpinBox()
        self.spin_analysis.setRange(0.1, 3600.0)
        self.spin_analysis.setSingleStep(0.5)
        self.spin_analysis.setSuffix(" 秒")
        self.spin_analysis.setValue(3.0)
        gf_lay.addRow("分析时长", self.spin_analysis)

        # 分析时长 <= 录音时长 实时约束
        self.spin_analysis.valueChanged.connect(
            lambda v: self.spin_record.setValue(max(self.spin_record.value(), v))
        )
        self.spin_record.valueChanged.connect(
            lambda v: self.spin_analysis.setValue(min(self.spin_analysis.value(), v))
        )

        self.chk_scan = QCheckBox("扫码枪直接触发测试")
        gf_lay.addRow("扫码触发", self.chk_scan)

        detail_lay.addWidget(Card("全局参数", global_form))

        # Channel warning
        self.channel_warning = QLabel("请先在硬件页面选择并保存输入设备")
        self.channel_warning.setObjectName("Muted")
        self.channel_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channel_warning.setStyleSheet(
            "background: #fefce8; border: 1px solid #fde68a; "
            "border-radius: 8px; padding: 12px;"
        )
        self.channel_warning.setVisible(False)
        detail_lay.addWidget(self.channel_warning)

        # Channel count mismatch note
        self.channel_mismatch_note = QLabel(
            "当前硬件通道数与已保存配置不同，多余通道配置将在保存时移除"
        )
        self.channel_mismatch_note.setObjectName("Muted")
        self.channel_mismatch_note.setStyleSheet("color: #ca8a04; font-size: 11px;")
        self.channel_mismatch_note.setVisible(False)
        detail_lay.addWidget(self.channel_mismatch_note)

        # Channel tabs
        self.tab_channels = QTabWidget()
        self.tab_channels.setStyleSheet(
            "QTabBar::tab { padding: 8px 18px; border: 1px solid #e5e7eb; "
            "border-bottom: none; border-radius: 8px 8px 0 0; "
            "background: #f1f5f9; color: #64748b; margin-right: 2px; }"
            "QTabBar::tab:selected { background: white; color: #2563eb; "
            "font-weight: 700; border-bottom: 2px solid #2563eb; }"
            "QTabBar::tab:hover:!selected { background: #e0edff; color: #334155; }"
            "QTabWidget::pane { border: 1px solid #e5e7eb; border-radius: 0 0 8px 8px; "
            "background: white; }"
        )
        detail_lay.addWidget(self.tab_channels, 1)

        # Save button
        save_row = QWidget()
        save_lay = QHBoxLayout(save_row)
        save_lay.setContentsMargins(0, 0, 0, 0)
        save_lay.addStretch(1)
        self.btn_save = QPushButton("保存配置")
        self.btn_save.setObjectName("Primary")
        save_lay.addWidget(self.btn_save)
        detail_lay.addWidget(save_row)

        self.detail.setVisible(False)
        self.right_lay.addWidget(self.detail, 1)
        root.addWidget(self.right_panel, 1)

    def _connect_signals(self):
        self.btn_new.clicked.connect(self._on_new)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_save.clicked.connect(self._on_save)
        self.config_list.currentItemChanged.connect(self._on_item_changed)
        self.services.bus.refresh_channels.connect(self._on_hardware_changed)

    # ── Channel tab building ────────────────────────────────

    def _build_channel_tabs(self, ch_count: int, channel_data: list[dict] | None = None):
        self.tab_channels.clear()
        if ch_count <= 0:
            self.channel_warning.setVisible(True)
            self.tab_channels.setVisible(False)
            return

        self.channel_warning.setVisible(False)
        self.tab_channels.setVisible(True)

        saved_count = len(channel_data) if channel_data else 0
        self.channel_mismatch_note.setVisible(
            saved_count > 0 and saved_count > ch_count
        )

        for i in range(ch_count):
            ch = (
                channel_data[i]
                if channel_data and i < len(channel_data)
                else None
            )
            tab = self._build_channel_tab(i, ch)
            self.tab_channels.addTab(tab, f"通道 {i + 1}")

    def _build_channel_tab(self, ch_index: int, data: dict | None) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        container.setProperty("channel_index", ch_index)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        # STFT section
        stft = CollapsibleGroupBox("STFT 参数")
        stft_form = QWidget()
        sf_lay = QFormLayout(stft_form)
        sf_lay.setContentsMargins(0, 0, 0, 0)
        sf_lay.setHorizontalSpacing(10)
        sf_lay.setVerticalSpacing(6)

        combo_nfft = QComboBox()
        combo_nfft.setStyleSheet(combo_qss)
        for v in [256, 512, 1024, 2048, 4096, 8192]:
            combo_nfft.addItem(str(v), v)
        combo_nfft.setObjectName("stft_nfft")
        val = data.get("stft_nfft", 1024) if data else 1024
        idx = combo_nfft.findData(val)
        combo_nfft.setCurrentIndex(idx if idx >= 0 else 2)
        sf_lay.addRow("FFT 点数", combo_nfft)

        combo_overlap = QComboBox()
        combo_overlap.setStyleSheet(combo_qss)
        for v in [0.25, 0.5, 0.75]:
            combo_overlap.addItem(str(v), v)
        combo_overlap.setObjectName("stft_overlap")
        val = data.get("stft_overlap", 0.5) if data else 0.5
        idx = combo_overlap.findData(val)
        combo_overlap.setCurrentIndex(idx if idx >= 0 else 1)
        sf_lay.addRow("重叠率", combo_overlap)

        stft.add_widget(stft_form)
        lay.addWidget(stft)

        # SPL section
        spl = CollapsibleGroupBox("SPL 参数")
        spl_form = QWidget()
        sp_lay = QFormLayout(spl_form)
        sp_lay.setContentsMargins(0, 0, 0, 0)
        sp_lay.setHorizontalSpacing(10)
        sp_lay.setVerticalSpacing(6)

        combo_spl_w = QComboBox()
        combo_spl_w.setStyleSheet(combo_qss)
        combo_spl_w.addItems(["A", "C", "Z"])
        combo_spl_w.setObjectName("spl_weighting")
        val = data.get("spl_weighting", "A") if data else "A"
        idx = combo_spl_w.findText(val)
        combo_spl_w.setCurrentIndex(idx if idx >= 0 else 0)
        sp_lay.addRow("计权模式", combo_spl_w)

        spin_tc = _NoWheelSpinBox()
        spin_tc.setObjectName("spl_time_constant")
        spin_tc.setRange(0.001, 10.0)
        spin_tc.setDecimals(3)
        spin_tc.setSingleStep(0.001)
        spin_tc.setSuffix(" 秒")
        spin_tc.setValue(data.get("spl_time_constant", 0.125) if data else 0.125)
        sp_lay.addRow("时间常数", spin_tc)

        spin_lo = _NoWheelSpinBox()
        spin_lo.setObjectName("spl_lower_limit")
        spin_lo.setRange(-200.0, 200.0)
        spin_lo.setSuffix(" dB")
        spin_lo.setValue(data.get("spl_lower_limit", 40.0) if data else 40.0)
        sp_lay.addRow("SPL 下限", spin_lo)

        spin_hi = _NoWheelSpinBox()
        spin_hi.setObjectName("spl_upper_limit")
        spin_hi.setRange(-200.0, 200.0)
        spin_hi.setSuffix(" dB")
        spin_hi.setValue(data.get("spl_upper_limit", 85.0) if data else 85.0)
        sp_lay.addRow("SPL 上限", spin_hi)

        # 下限 < 上限 实时约束
        spin_lo.valueChanged.connect(
            lambda v: spin_hi.setValue(max(spin_hi.value(), v + 0.1))
        )
        spin_hi.valueChanged.connect(
            lambda v: spin_lo.setValue(min(spin_lo.value(), v - 0.1))
        )

        spl.add_widget(spl_form)
        lay.addWidget(spl)

        # FFT band limits section
        fft_section = CollapsibleGroupBox("FFT 频带上下限")
        fft_table = self._build_band_table(
            "fft_band_table",
            data.get("fft_band_limits", []) if data else [],
        )
        fft_section.add_widget(fft_table)
        lay.addWidget(fft_section)

        # Octave section
        oct_section = CollapsibleGroupBox("1/3 倍频程参数")
        oct_form = QWidget()
        of_lay = QVBoxLayout(oct_form)
        of_lay.setContentsMargins(0, 0, 0, 0)
        of_lay.setSpacing(6)

        oct_w_row = QWidget()
        ow_lay = QFormLayout(oct_w_row)
        ow_lay.setContentsMargins(0, 0, 0, 0)
        combo_oct_w = QComboBox()
        combo_oct_w.setStyleSheet(combo_qss)
        combo_oct_w.addItems(["A", "C", "Z"])
        combo_oct_w.setObjectName("oct_weighting")
        val = data.get("oct_weighting", "A") if data else "A"
        idx = combo_oct_w.findText(val)
        combo_oct_w.setCurrentIndex(idx if idx >= 0 else 0)
        ow_lay.addRow("计权模式", combo_oct_w)
        of_lay.addWidget(oct_w_row)

        oct_table = self._build_band_table(
            "oct_band_table",
            data.get("oct_band_limits", []) if data else [],
        )
        of_lay.addWidget(oct_table)
        oct_section.add_widget(oct_form)
        lay.addWidget(oct_section)

        # AI placeholder
        ai_section = CollapsibleGroupBox("AI 分析")
        ai_label = QLabel("待配置，后续扩展")
        ai_label.setObjectName("Muted")
        ai_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ai_label.setStyleSheet("padding: 16px;")
        ai_section.add_widget(ai_label)
        lay.addWidget(ai_section)

        lay.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _build_band_table(self, object_name: str, bands: list[dict]) -> QWidget:
        wrapper = QWidget()
        w_lay = QVBoxLayout(wrapper)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.setSpacing(4)

        table = QTableWidget()
        table.setObjectName(object_name)
        table.setRowCount(3)
        table.setVerticalHeaderLabels(["频率 (Hz)", "下限 (dB)", "上限 (dB)"])
        table.horizontalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectColumns)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.setItemDelegate(_FloatDelegate(table))

        row_h = 42
        table.verticalHeader().setDefaultSectionSize(row_h)
        table.setStyleSheet(
            "QTableView::item { padding: 2px 4px; }"
        )

        for band in bands:
            self._add_band_col(table, band.get("freq", 1000), band.get("lower", 0), band.get("upper", 100))

        # 精确高度 = 3行 + 2px 边框
        table.setFixedHeight(3 * row_h + 2)
        w_lay.addWidget(table)

        btn_row = QWidget()
        br_lay = QHBoxLayout(btn_row)
        br_lay.setContentsMargins(0, 0, 0, 0)
        br_lay.setSpacing(6)
        btn_add = QPushButton("添加频带")
        btn_del = QPushButton("删除选中")
        btn_del.setObjectName("Danger")
        br_lay.addWidget(btn_add)
        br_lay.addWidget(btn_del)
        br_lay.addStretch(1)

        btn_add.clicked.connect(lambda: self._add_band_col(table, 1000, 0, 100))
        btn_del.clicked.connect(lambda: self._delete_selected_cols(table))

        w_lay.addWidget(btn_row)
        return wrapper

    @staticmethod
    def _add_band_col(table: QTableWidget, freq: float, lower: float, upper: float):
        col = table.columnCount()
        table.insertColumn(col)
        for row, val in enumerate([freq, lower, upper]):
            item = QTableWidgetItem(str(val))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
            )
            table.setItem(row, col, item)

    @staticmethod
    def _delete_selected_cols(table: QTableWidget):
        cols = sorted(set(idx.column() for idx in table.selectedIndexes()), reverse=True)
        for c in cols:
            table.removeColumn(c)

    # ── Data collection ─────────────────────────────────────

    def _collect_data(self) -> dict:
        data = {
            "config_id": self._current_config_id,
            "config_name": self.ed_name.text(),
            "record_duration": self.spin_record.value(),
            "analysis_duration": self.spin_analysis.value(),
            "scan_triggers_test": self.chk_scan.isChecked(),
            "channels": [],
        }

        for tab_idx in range(self.tab_channels.count()):
            scroll = self.tab_channels.widget(tab_idx)
            container = scroll.widget()
            ch_index = container.property("channel_index")

            ch = {"channel_index": ch_index}

            combo_nfft = container.findChild(QComboBox, "stft_nfft")
            ch["stft_nfft"] = combo_nfft.currentData() if combo_nfft else 1024

            combo_overlap = container.findChild(QComboBox, "stft_overlap")
            ch["stft_overlap"] = combo_overlap.currentData() if combo_overlap else 0.5

            combo_spl_w = container.findChild(QComboBox, "spl_weighting")
            ch["spl_weighting"] = combo_spl_w.currentText() if combo_spl_w else "A"

            spin_tc = container.findChild(QDoubleSpinBox, "spl_time_constant")
            ch["spl_time_constant"] = spin_tc.value() if spin_tc else 0.125

            spin_lo = container.findChild(QDoubleSpinBox, "spl_lower_limit")
            ch["spl_lower_limit"] = spin_lo.value() if spin_lo else 40.0

            spin_hi = container.findChild(QDoubleSpinBox, "spl_upper_limit")
            ch["spl_upper_limit"] = spin_hi.value() if spin_hi else 85.0

            combo_oct_w = container.findChild(QComboBox, "oct_weighting")
            ch["oct_weighting"] = combo_oct_w.currentText() if combo_oct_w else "A"

            ch["fft_band_limits"] = self._read_band_table(container, "fft_band_table")
            ch["oct_band_limits"] = self._read_band_table(container, "oct_band_table")
            ch["ai_config"] = {}

            data["channels"].append(ch)

        return data

    @staticmethod
    def _read_band_table(container: QWidget, table_name: str) -> list[dict]:
        table = container.findChild(QTableWidget, table_name)
        if not table:
            return []
        bands = []
        for c in range(table.columnCount()):
            try:
                freq = float(table.item(0, c).text()) if table.item(0, c) else 0
                lower = float(table.item(1, c).text()) if table.item(1, c) else 0
                upper = float(table.item(2, c).text()) if table.item(2, c) else 0
            except ValueError:
                # Preserve column with sentinel values — validate() will flag freq <= 0
                freq, lower, upper = -1, 0, 0
            bands.append({"freq": freq, "lower": lower, "upper": upper})
        return bands

    # ── Populate UI from data ───────────────────────────────

    def _populate_detail(self, data: dict):
        self.ed_name.setText(data["config_name"])
        self.spin_record.setValue(data["record_duration"])
        self.spin_analysis.setValue(data["analysis_duration"])
        self.chk_scan.setChecked(data["scan_triggers_test"])

        ch_count = self.services.config_service.get_channel_count()
        self._build_channel_tabs(ch_count, data.get("channels"))

    # ── Interactions ────────────────────────────────────────

    def _refresh_list(self):
        self.config_list.clear()
        configs = self.services.config_service.list_configs()
        for c in configs:
            item = QListWidgetItem(c["config_name"])
            item.setData(Qt.ItemDataRole.UserRole, c["config_id"])
            self.config_list.addItem(item)

        has_items = len(configs) > 0
        self.empty_label.setVisible(not has_items)
        self.detail.setVisible(has_items)
        self.btn_delete.setEnabled(has_items)
        self.btn_save.setEnabled(has_items)

        if has_items:
            self.config_list.setCurrentRow(0)

    def _select_config_by_id(self, config_id: int):
        for i in range(self.config_list.count()):
            item = self.config_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == config_id:
                self.config_list.setCurrentRow(i)
                return
        if self.config_list.count() > 0:
            self.config_list.setCurrentRow(0)

    def _on_item_changed(self, current: QListWidgetItem | None, _prev):
        if current is None:
            return
        if self._skip_reload:
            return
        config_id = current.data(Qt.ItemDataRole.UserRole)
        self._current_config_id = config_id
        try:
            data = self.services.config_service.load_config(config_id)
            self.empty_label.setVisible(False)
            self.detail.setVisible(True)
            self._populate_detail(data)
        except Exception as e:
            QMessageBox.warning(self, "加载失败", f"无法加载配置：{e}")

    def _on_new(self):
        name, ok = QInputDialog.getText(self, "新建配置", "请输入配置名称：")
        if not ok:
            return
        try:
            cid = self.services.config_service.create_config(name)
            self._refresh_list()
            self._select_config_by_id(cid)
        except ValueError as e:
            QMessageBox.warning(self, "创建失败", str(e))

    def _on_delete(self):
        current = self.config_list.currentItem()
        if current is None:
            return
        config_id = current.data(Qt.ItemDataRole.UserRole)
        config_name = current.text()
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除配置「{config_name}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.services.config_service.delete_config(config_id)
        self._current_config_id = None
        self._refresh_list()

    def _on_save(self):
        if self._current_config_id is None:
            return
        data = self._collect_data()
        errors = self.services.config_service.validate(data)
        if errors:
            QMessageBox.warning(self, "验证失败", "\n".join(errors))
            return
        try:
            saved_id = self._current_config_id
            self.services.config_service.save_config(data)
            self._skip_reload = True
            self._refresh_list()
            self._select_config_by_id(saved_id)
            self._skip_reload = False
            self.services.bus.status.emit("配置已保存")
        except ValueError as e:
            self._skip_reload = False
            QMessageBox.warning(self, "保存失败", str(e))

    def _on_hardware_changed(self):
        if self._current_config_id is not None:
            ch_count = self.services.config_service.get_channel_count()
            if ch_count <= 0:
                self._build_channel_tabs(0)
                return
            # Collect current channel data from UI to preserve unsaved edits
            current_data = self._collect_data()
            current_channels = current_data.get("channels", [])
            self._build_channel_tabs(ch_count, current_channels if current_channels else None)
