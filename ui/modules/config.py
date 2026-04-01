from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.popups.golden_sample_import_dialog import GoldenSampleImportDialog
from ui.widgets.cards import Card
from ui.widgets.collapsible_group_box import CollapsibleGroupBox

_GOLDEN_BINDING_FIELDS = (
    "golden_sample_path",
    "golden_sample_label",
    "golden_sample_source_channel",
    "golden_sample_start_sample",
    "golden_sample_num_samples",
    "golden_sample_start_ms",
    "golden_sample_duration_ms",
    "golden_sample_sample_rate",
)


class _NoWheelSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores mouse wheel events."""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class _NoWheelSpinBoxInt(QSpinBox):
    """QSpinBox that ignores mouse wheel events."""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class _NoWheelComboBox(QComboBox):
    """QComboBox that ignores mouse wheel events."""

    def wheelEvent(self, event: QWheelEvent):
        event.ignore()


class _NoWheelPlainTextEdit(QPlainTextEdit):
    """QPlainTextEdit that ignores mouse wheel events."""

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
        self._golden_editor_state: dict[int, dict] = {}

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

        # Scrollable content wrapper
        scroll_content = QWidget()
        scroll_lay = QVBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(10)

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

        self.spin_analysis_window_offset = _NoWheelSpinBoxInt()
        self.spin_analysis_window_offset.setRange(0, 3600000)
        self.spin_analysis_window_offset.setSingleStep(100)
        self.spin_analysis_window_offset.setSuffix(" ms")
        self.spin_analysis_window_offset.setValue(0)
        gf_lay.addRow("默认分析窗起始偏移(ms)", self.spin_analysis_window_offset)

        self.chk_scan = QCheckBox("扫码枪直接触发测试")
        gf_lay.addRow("扫码触发", self.chk_scan)

        scroll_lay.addWidget(Card("全局参数", global_form))

        # Hardware settings card
        hw_form = QWidget()
        hw_lay = QFormLayout(hw_form)
        hw_lay.setContentsMargins(0, 0, 0, 0)
        hw_lay.setHorizontalSpacing(10)
        hw_lay.setVerticalSpacing(8)

        self.ed_plc_device_key = QLineEdit()
        self.ed_plc_device_key.setPlaceholderText("例如: plc-a")
        hw_lay.addRow("PLC 设备标识", self.ed_plc_device_key)

        self.ed_plc_device_label = QLineEdit()
        self.ed_plc_device_label.setPlaceholderText("例如: PLC A")
        hw_lay.addRow("PLC 显示名称", self.ed_plc_device_label)

        self.combo_input_dev = _NoWheelComboBox()
        hw_lay.addRow("输入设备", self.combo_input_dev)

        self.combo_output_dev = _NoWheelComboBox()
        hw_lay.addRow("监听设备", self.combo_output_dev)

        self._channel_check_container = QWidget()
        self._channel_check_layout = QGridLayout(self._channel_check_container)
        self._channel_check_layout.setContentsMargins(0, 0, 0, 0)
        self._channel_check_layout.setSpacing(4)
        self._channel_checkboxes = []
        hw_lay.addRow("通道选择", self._channel_check_container)

        scroll_lay.addWidget(Card("硬件设置", hw_form))

        # AI binding card (test-level, not per-channel)
        ai_bind_form = QWidget()
        ab_lay = QFormLayout(ai_bind_form)
        ab_lay.setContentsMargins(0, 0, 0, 0)
        ab_lay.setHorizontalSpacing(10)
        ab_lay.setVerticalSpacing(8)

        self.chk_ai_enabled = QCheckBox("启用 AI 分析")
        ab_lay.addRow("启用 AI", self.chk_ai_enabled)

        self.combo_ai_toolchain = _NoWheelComboBox()
        ab_lay.addRow("AI 工具链", self.combo_ai_toolchain)

        self.spin_ai_timeout = _NoWheelSpinBoxInt()
        self.spin_ai_timeout.setRange(0, 600000)
        self.spin_ai_timeout.setSingleStep(1000)
        self.spin_ai_timeout.setSuffix(" ms")
        self.spin_ai_timeout.setSpecialValueText("使用工具链默认值")
        self.spin_ai_timeout.setValue(0)
        ab_lay.addRow("超时(ms)", self.spin_ai_timeout)

        self.txt_ai_params = _NoWheelPlainTextEdit()
        self.txt_ai_params.setPlaceholderText('{"key": "value"}')
        self.txt_ai_params.setMaximumHeight(80)
        ab_lay.addRow("参数(JSON)", self.txt_ai_params)

        scroll_lay.addWidget(Card("AI 工具链绑定", ai_bind_form))

        # Channel warning
        self.channel_warning = QLabel("请先选择输入设备")
        self.channel_warning.setObjectName("Muted")
        self.channel_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.channel_warning.setStyleSheet(
            "background: #fefce8; border: 1px solid #fde68a; "
            "border-radius: 8px; padding: 12px;"
        )
        self.channel_warning.setVisible(False)
        scroll_lay.addWidget(self.channel_warning)

        # Channel count mismatch note
        self.channel_mismatch_note = QLabel(
            "当前硬件通道数与已保存配置不同，多余通道配置将在保存时移除"
        )
        self.channel_mismatch_note.setObjectName("Muted")
        self.channel_mismatch_note.setStyleSheet("color: #ca8a04; font-size: 11px;")
        self.channel_mismatch_note.setVisible(False)
        scroll_lay.addWidget(self.channel_mismatch_note)

        # Channel tab-like selector + stacked content
        self.channel_host = QWidget()
        ch_host_lay = QVBoxLayout(self.channel_host)
        ch_host_lay.setContentsMargins(0, 0, 0, 0)
        ch_host_lay.setSpacing(0)

        self.channel_tab_bar = QWidget()
        self.channel_tab_bar.setStyleSheet(
            "QPushButton { padding: 8px 18px; border: 1px solid #e5e7eb; "
            "border-bottom: none; border-radius: 8px 8px 0 0; "
            "background: #f1f5f9; color: #64748b; margin-right: 2px; }"
            "QPushButton:checked { background: white; color: #2563eb; "
            "font-weight: 700; border-bottom: 2px solid #2563eb; }"
            "QPushButton:hover:!checked { background: #e0edff; color: #334155; }"
        )
        self._channel_tab_bar_lay = QHBoxLayout(self.channel_tab_bar)
        self._channel_tab_bar_lay.setContentsMargins(0, 0, 0, 0)
        self._channel_tab_bar_lay.setSpacing(0)
        self._channel_tab_buttons = []
        ch_host_lay.addWidget(self.channel_tab_bar)

        self.channel_stack = QStackedWidget()
        self.channel_stack.setStyleSheet(
            "QStackedWidget { border: 1px solid #e5e7eb; border-radius: 0 0 8px 8px; "
            "background: white; }"
        )
        ch_host_lay.addWidget(self.channel_stack)

        self._channel_containers = []
        scroll_lay.addWidget(self.channel_host)

        scroll_lay.addStretch(1)

        # Scroll area wrapping all content above
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.detail_scroll.setWidget(scroll_content)
        detail_lay.addWidget(self.detail_scroll, 1)

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
        self.combo_input_dev.currentIndexChanged.connect(self._on_input_device_changed)
        self.spin_analysis.valueChanged.connect(
            lambda _value: self._refresh_all_golden_sections()
        )
        self.services.bus.refresh_channels.connect(self._on_hardware_changed)

    def _populate_devices(self):
        """Populate input/output device combos from hss_service."""
        try:
            devices, hostapis = self.services.hss_service.query_devices_and_hostapis(refresh=False)
        except Exception:
            return

        hostapi_names = {}
        for i, api in enumerate(hostapis):
            hostapi_names[i] = api.get("name", "N/A")

        self.combo_input_dev.clear()
        self.combo_input_dev.addItem("（未选择）", None)
        self.combo_output_dev.clear()
        self.combo_output_dev.addItem("（未选择）", None)

        for dev in devices:
            hostapi_idx = dev.get("hostapi", 0)
            api_name = hostapi_names.get(hostapi_idx, "N/A")
            label = f"{dev.get('name', '?')} ({api_name})"
            dev_info = {
                "name": dev.get("name", ""),
                "hostapi": api_name,
                "default_samplerate": dev.get("default_samplerate"),
                "max_input_channels": dev.get("max_input_channels", 0),
                "max_output_channels": dev.get("max_output_channels", 0),
            }
            if dev.get("max_input_channels", 0) > 0:
                self.combo_input_dev.addItem(label, dev_info)
            if dev.get("max_output_channels", 0) > 0:
                self.combo_output_dev.addItem(label, dev_info)

    def _on_input_device_changed(self, index):
        """Rebuild channel checkboxes and tabs when input device changes."""
        existing_channel_data = self._current_channel_editor_data()
        for cb in self._channel_checkboxes:
            cb.deleteLater()
        self._channel_checkboxes = []

        while self._channel_check_layout.count():
            self._channel_check_layout.takeAt(0)

        dev_info = self.combo_input_dev.currentData()
        if not dev_info or not isinstance(dev_info, dict):
            self._prune_staged_bindings([])
            self._build_channel_tabs([], None)
            return

        max_ch = dev_info.get("max_input_channels", 0)
        for i in range(max_ch):
            cb = QCheckBox(f"In{i + 1}")
            cb.toggled.connect(self._on_channel_selection_changed)
            row, col = divmod(i, 6)
            self._channel_check_layout.addWidget(cb, row, col)
            self._channel_checkboxes.append(cb)

        # Rebuild tabs for ALL channels, not just checked ones
        all_indices = list(range(max_ch))
        self._prune_staged_bindings(all_indices)
        self._build_channel_tabs(all_indices, existing_channel_data)
        self._refresh_all_golden_sections()

    def _on_channel_selection_changed(self):
        """Channel checkbox toggled — tabs are built by _on_input_device_changed."""
        pass

    @staticmethod
    def _empty_golden_binding() -> dict:
        return {field: None for field in _GOLDEN_BINDING_FIELDS}

    @staticmethod
    def _normalize_golden_binding(data: dict | None) -> dict:
        if not isinstance(data, dict):
            return ConfigModule._empty_golden_binding()
        return {
            field: data.get(field)
            for field in _GOLDEN_BINDING_FIELDS
        }

    def _current_channel_editor_data(self) -> list[dict] | None:
        if not self._channel_containers:
            return None
        return [self._collect_channel_data(container) for container in self._channel_containers]

    def _current_input_sample_rate(self):
        input_dev = self.combo_input_dev.currentData()
        if isinstance(input_dev, dict):
            return input_dev.get("default_samplerate")
        return None

    def _golden_sample_service(self):
        return getattr(self.services.config_service, "golden_sample_service", None)

    def _discard_staged_paths(self, staged_paths: list[str]) -> None:
        if not staged_paths:
            return
        golden_service = self._golden_sample_service()
        if golden_service is None:
            return
        golden_service.discard_staged_samples(staged_paths)

    def _discard_channel_staged_binding(self, channel_index: int) -> None:
        staged_binding = self._golden_editor_state.pop(channel_index, None)
        if staged_binding is None:
            return
        staged_path = staged_binding.get("staged_path")
        if staged_path:
            self._discard_staged_paths([str(staged_path)])

    def _discard_all_staged_bindings(self) -> None:
        staged_paths = [
            str(binding.get("staged_path"))
            for binding in self._golden_editor_state.values()
            if binding.get("staged_path")
        ]
        self._golden_editor_state = {}
        self._discard_staged_paths(staged_paths)

    def _prune_staged_bindings(self, valid_channel_indices: list[int]) -> None:
        valid = set(valid_channel_indices)
        removed = [
            channel_index
            for channel_index in list(self._golden_editor_state)
            if channel_index not in valid
        ]
        for channel_index in removed:
            self._discard_channel_staged_binding(channel_index)

    @staticmethod
    def _set_channel_binding(container: QWidget, binding: dict) -> None:
        normalized = ConfigModule._normalize_golden_binding(binding)
        container.setProperty("golden_binding", dict(normalized))

    @staticmethod
    def _channel_binding(container: QWidget) -> dict:
        return ConfigModule._normalize_golden_binding(
            container.property("golden_binding")
        )

    @staticmethod
    def _set_committed_channel_binding(container: QWidget, binding: dict) -> None:
        normalized = ConfigModule._normalize_golden_binding(binding)
        container.setProperty("golden_committed_binding", dict(normalized))

    @staticmethod
    def _committed_channel_binding(container: QWidget) -> dict:
        return ConfigModule._normalize_golden_binding(
            container.property("golden_committed_binding")
        )

    def _restore_committed_golden_bindings(self) -> None:
        for container in self._channel_containers:
            self._set_channel_binding(
                container,
                self._committed_channel_binding(container),
            )
        self._refresh_all_golden_sections()

    def _apply_authoritative_golden_bindings(self, bindings_by_channel) -> None:
        binding_map: dict[int, dict] = {}
        for entry in bindings_by_channel or ():
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                continue
            channel_index, binding = entry
            if not isinstance(channel_index, int) or isinstance(channel_index, bool):
                continue
            binding_map[channel_index] = self._normalize_golden_binding(binding)
        if not binding_map:
            return
        for container in self._channel_containers:
            channel_index = container.property("channel_index")
            if channel_index not in binding_map:
                continue
            binding = binding_map[channel_index]
            self._set_channel_binding(container, binding)
            self._set_committed_channel_binding(container, binding)
        self._refresh_all_golden_sections()

    def _reconcile_golden_bindings_after_save_failure(
        self,
        pending_save,
        *,
        pending_save_committed: bool,
    ) -> None:
        self._discard_all_staged_bindings()
        if pending_save_committed:
            self._apply_authoritative_golden_bindings(
                getattr(pending_save, "golden_bindings_by_channel", ()),
            )
            return
        self._restore_committed_golden_bindings()

    def _effective_golden_binding(self, container: QWidget) -> dict | None:
        channel_index = container.property("channel_index")
        staged_binding = self._golden_editor_state.get(channel_index)
        if staged_binding is not None:
            return {
                "golden_sample_path": staged_binding.get("staged_path"),
                "golden_sample_label": staged_binding.get("label"),
                "golden_sample_source_channel": staged_binding.get("source_channel"),
                "golden_sample_start_sample": staged_binding.get("start_sample"),
                "golden_sample_num_samples": staged_binding.get("num_samples"),
                "golden_sample_start_ms": staged_binding.get("start_ms"),
                "golden_sample_duration_ms": staged_binding.get("duration_ms"),
                "golden_sample_sample_rate": staged_binding.get("sample_rate"),
            }
        binding = self._channel_binding(container)
        if any(binding.get(field) is not None for field in _GOLDEN_BINDING_FIELDS):
            return binding
        return None

    @staticmethod
    def _human_source_channel(value) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value + 1)
        return "-"

    @staticmethod
    def _format_ms(value) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value} ms"
        return "-"

    @staticmethod
    def _format_sample_rate(value) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{float(value):g} Hz"
        return "-"

    @staticmethod
    def _set_text(container: QWidget, object_name: str, text: str) -> None:
        label = container.findChild(QLabel, object_name)
        if label is not None:
            label.setText(text)

    def _refresh_channel_golden_section(self, container: QWidget) -> None:
        effective_binding = self._effective_golden_binding(container)
        if effective_binding is None:
            label_text = "-"
            sample_rate_text = "-"
            source_channel_text = "-"
            start_text = "-"
            duration_text = "-"
            status_text = "未绑定"
        else:
            label_text = str(effective_binding.get("golden_sample_label") or "-")
            sample_rate_text = self._format_sample_rate(
                effective_binding.get("golden_sample_sample_rate")
            )
            source_channel_text = self._human_source_channel(
                effective_binding.get("golden_sample_source_channel")
            )
            start_text = self._format_ms(
                effective_binding.get("golden_sample_start_ms")
            )
            duration_text = self._format_ms(
                effective_binding.get("golden_sample_duration_ms")
            )
            channel_data = self._collect_channel_data(container)
            staged_binding = self._golden_editor_state.get(
                container.property("channel_index")
            )
            validator = getattr(self.services.config_service, "validate_golden_binding", None)
            errors = []
            if callable(validator):
                errors = validator(
                    channel=channel_data,
                    staged_binding=staged_binding,
                    analysis_duration=self.spin_analysis.value(),
                    sample_rate=self._current_input_sample_rate(),
                )
            if errors:
                status_text = f"无效: {errors[0]}"
            elif staged_binding is not None:
                status_text = "有效 (待保存)"
            else:
                status_text = "有效"

        self._set_text(container, "golden_sample_label_value", label_text)
        self._set_text(container, "golden_sample_sample_rate_value", sample_rate_text)
        self._set_text(
            container, "golden_sample_source_channel_value", source_channel_text
        )
        self._set_text(container, "golden_sample_start_ms_value", start_text)
        self._set_text(
            container, "golden_sample_duration_ms_value", duration_text
        )
        self._set_text(container, "golden_sample_status_value", status_text)

    def _refresh_all_golden_sections(self) -> None:
        for container in self._channel_containers:
            self._refresh_channel_golden_section(container)

    def _apply_golden_mode_state(
        self,
        combo: QComboBox | None,
        checkbox: QCheckBox | None,
    ) -> None:
        if combo is None or checkbox is None:
            return
        if combo.currentData() == "delta":
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
            checkbox.setEnabled(False)
        else:
            checkbox.setEnabled(True)

    def _on_golden_mode_changed(
        self,
        combo: QComboBox,
        checkbox: QCheckBox,
        container: QWidget,
    ) -> None:
        self._apply_golden_mode_state(combo, checkbox)
        self._refresh_channel_golden_section(container)

    def _on_import_golden_sample(self, channel_index: int) -> None:
        source_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Golden Sample WAV",
            "",
            "WAV Files (*.wav)",
        )
        if not source_path:
            return

        golden_service = self._golden_sample_service()
        if golden_service is None:
            return

        dialog = GoldenSampleImportDialog(
            source_path=source_path,
            analysis_duration=self.spin_analysis.value(),
            config_sample_rate=self._current_input_sample_rate(),
            golden_sample_service=golden_service,
            parent=self,
        )
        if dialog.exec() != int(dialog.DialogCode.Accepted):
            return

        staged_metadata = dialog.staged_metadata()
        if staged_metadata is None:
            return

        previous = self._golden_editor_state.get(channel_index)
        if previous and previous.get("staged_path"):
            self._discard_staged_paths([str(previous["staged_path"])])
        self._golden_editor_state[channel_index] = staged_metadata
        for container in self._channel_containers:
            if container.property("channel_index") == channel_index:
                self._refresh_channel_golden_section(container)
                break

    def _on_clear_golden_binding(self, container: QWidget) -> None:
        channel_index = container.property("channel_index")
        self._discard_channel_staged_binding(channel_index)
        self._set_channel_binding(container, self._empty_golden_binding())
        self._refresh_channel_golden_section(container)

    # ── Channel tab building ────────────────────────────────

    def _build_channel_tabs(self, channel_indices: list[int], channel_data: list[dict] | None = None):
        # Clear previous tab buttons and stretch
        while self._channel_tab_bar_lay.count():
            item = self._channel_tab_bar_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._channel_tab_buttons = []
        # Clear stack
        while self.channel_stack.count():
            w = self.channel_stack.widget(0)
            self.channel_stack.removeWidget(w)
            w.deleteLater()
        self._channel_containers = []

        if not channel_indices:
            self.channel_warning.setVisible(True)
            self.channel_host.setVisible(False)
            return

        self.channel_warning.setVisible(False)
        self.channel_host.setVisible(True)

        ch_data_map = {}
        if channel_data:
            for ch in channel_data:
                ch_data_map[ch["channel_index"]] = ch

        self.channel_mismatch_note.setVisible(False)

        for page_idx, ch_idx in enumerate(channel_indices):
            ch = ch_data_map.get(ch_idx)
            btn = QPushButton(f"In{ch_idx + 1} (通道 {ch_idx + 1})")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, idx=page_idx: self._on_channel_tab_clicked(idx))
            self._channel_tab_bar_lay.addWidget(btn)
            self._channel_tab_buttons.append(btn)

            container = self._build_channel_tab(ch_idx, ch)
            self.channel_stack.addWidget(container)
            self._channel_containers.append(container)

        self._channel_tab_bar_lay.addStretch(1)
        if self._channel_tab_buttons:
            self._channel_tab_buttons[0].setChecked(True)
            self.channel_stack.setCurrentIndex(0)

    def _on_channel_tab_clicked(self, idx: int):
        for i, btn in enumerate(self._channel_tab_buttons):
            btn.setChecked(i == idx)
        self.channel_stack.setCurrentIndex(idx)

    def _build_channel_tab(self, ch_index: int, data: dict | None) -> QWidget:
        container = QWidget()
        container.setProperty("channel_index", ch_index)
        golden_binding = self._normalize_golden_binding(data)
        self._set_channel_binding(container, golden_binding)
        self._set_committed_channel_binding(container, golden_binding)
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

        combo_nfft = _NoWheelComboBox()
        for v in [256, 512, 1024, 2048, 4096, 8192]:
            combo_nfft.addItem(str(v), v)
        combo_nfft.setObjectName("stft_nfft")
        val = data.get("stft_nfft", 1024) if data else 1024
        idx = combo_nfft.findData(val)
        combo_nfft.setCurrentIndex(idx if idx >= 0 else 2)
        sf_lay.addRow("FFT 点数", combo_nfft)

        combo_overlap = _NoWheelComboBox()
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

        combo_spl_w = _NoWheelComboBox()
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

        loudness = CollapsibleGroupBox("响度 参数")
        loudness_form = QWidget()
        loud_lay = QFormLayout(loudness_form)
        loud_lay.setContentsMargins(0, 0, 0, 0)
        loud_lay.setHorizontalSpacing(10)
        loud_lay.setVerticalSpacing(6)

        combo_loudness_unit = _NoWheelComboBox()
        combo_loudness_unit.addItems(["sone", "phon"])
        combo_loudness_unit.setObjectName("loudness_unit")
        val = data.get("loudness_unit", "sone") if data else "sone"
        idx = combo_loudness_unit.findText(val)
        combo_loudness_unit.setCurrentIndex(idx if idx >= 0 else 0)
        loud_lay.addRow("单位", combo_loudness_unit)

        spin_loudness_lo = _NoWheelSpinBox()
        spin_loudness_lo.setObjectName("loudness_lower_limit")
        spin_loudness_lo.setRange(0.0, 999999.0)
        spin_loudness_lo.setDecimals(3)
        spin_loudness_lo.setValue(
            data.get("loudness_lower_limit", 0.0) if data else 0.0
        )
        loud_lay.addRow("响度下限", spin_loudness_lo)

        spin_loudness_hi = _NoWheelSpinBox()
        spin_loudness_hi.setObjectName("loudness_upper_limit")
        spin_loudness_hi.setRange(0.0, 999999.0)
        spin_loudness_hi.setDecimals(3)
        spin_loudness_hi.setValue(
            data.get("loudness_upper_limit", 60.0) if data else 60.0
        )
        loud_lay.addRow("响度上限", spin_loudness_hi)

        loudness.add_widget(loudness_form)
        lay.addWidget(loudness)

        golden_section = CollapsibleGroupBox("Golden Sample")
        golden_form = QWidget()
        golden_layout = QVBoxLayout(golden_form)
        golden_layout.setContentsMargins(0, 0, 0, 0)
        golden_layout.setSpacing(8)

        summary_widget = QWidget()
        summary_form = QFormLayout(summary_widget)
        summary_form.setContentsMargins(0, 0, 0, 0)
        summary_form.setHorizontalSpacing(10)
        summary_form.setVerticalSpacing(6)

        for field_label, object_name in (
            ("绑定文件", "golden_sample_label_value"),
            ("采样率", "golden_sample_sample_rate_value"),
            ("源通道", "golden_sample_source_channel_value"),
            ("窗起点", "golden_sample_start_ms_value"),
            ("窗时长", "golden_sample_duration_ms_value"),
            ("状态", "golden_sample_status_value"),
        ):
            label = QLabel("-")
            label.setObjectName(object_name)
            summary_form.addRow(field_label, label)

        golden_layout.addWidget(summary_widget)

        golden_buttons = QWidget()
        golden_buttons_layout = QHBoxLayout(golden_buttons)
        golden_buttons_layout.setContentsMargins(0, 0, 0, 0)
        golden_buttons_layout.setSpacing(6)

        btn_import = QPushButton("Import WAV...")
        btn_import.setObjectName("golden_sample_import_button")
        btn_import.clicked.connect(lambda _checked=False, idx=ch_index: self._on_import_golden_sample(idx))
        golden_buttons_layout.addWidget(btn_import)

        btn_replace = QPushButton("Replace WAV...")
        btn_replace.setObjectName("golden_sample_replace_button")
        btn_replace.clicked.connect(lambda _checked=False, idx=ch_index: self._on_import_golden_sample(idx))
        golden_buttons_layout.addWidget(btn_replace)

        btn_clear = QPushButton("Clear")
        btn_clear.setObjectName("golden_sample_clear_button")
        btn_clear.clicked.connect(lambda _checked=False, current=container: self._on_clear_golden_binding(current))
        golden_buttons_layout.addWidget(btn_clear)
        golden_buttons_layout.addStretch(1)

        golden_layout.addWidget(golden_buttons)
        golden_section.add_widget(golden_form)
        lay.addWidget(golden_section)

        # FFT band limits section
        fft_section = CollapsibleGroupBox("FFT 频带上下限")
        fft_widget = QWidget()
        fft_layout = QVBoxLayout(fft_widget)
        fft_layout.setContentsMargins(0, 0, 0, 0)
        fft_layout.setSpacing(6)

        fft_controls = QWidget()
        fft_controls_form = QFormLayout(fft_controls)
        fft_controls_form.setContentsMargins(0, 0, 0, 0)
        fft_controls_form.setHorizontalSpacing(10)
        fft_controls_form.setVerticalSpacing(6)

        fft_mode = _NoWheelComboBox()
        fft_mode.setObjectName("fft_golden_mode")
        fft_mode.addItem("Off", "off")
        fft_mode.addItem("Absolute", "absolute")
        fft_mode.addItem("Delta", "delta")
        fft_mode_index = fft_mode.findData(data.get("fft_golden_mode", "off") if data else "off")
        fft_mode.setCurrentIndex(fft_mode_index if fft_mode_index >= 0 else 0)
        fft_controls_form.addRow("Golden mode", fft_mode)

        fft_overlay = QCheckBox("Show golden curve")
        fft_overlay.setObjectName("fft_show_golden_overlay")
        fft_overlay.setChecked(bool(data.get("fft_show_golden_overlay", False)) if data else False)
        fft_controls_form.addRow("Golden curve", fft_overlay)
        fft_layout.addWidget(fft_controls)

        fft_mode.currentIndexChanged.connect(
            lambda _index, combo=fft_mode, checkbox=fft_overlay, current=container: self._on_golden_mode_changed(combo, checkbox, current)
        )

        fft_table = self._build_band_table(
            "fft_band_table",
            data.get("fft_band_limits", []) if data else [],
        )
        fft_layout.addWidget(fft_table)
        fft_section.add_widget(fft_widget)
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
        combo_oct_w = _NoWheelComboBox()
        combo_oct_w.addItems(["A", "C", "Z"])
        combo_oct_w.setObjectName("oct_weighting")
        val = data.get("oct_weighting", "A") if data else "A"
        idx = combo_oct_w.findText(val)
        combo_oct_w.setCurrentIndex(idx if idx >= 0 else 0)
        ow_lay.addRow("计权模式", combo_oct_w)
        oct_mode = _NoWheelComboBox()
        oct_mode.setObjectName("oct_golden_mode")
        oct_mode.addItem("Off", "off")
        oct_mode.addItem("Absolute", "absolute")
        oct_mode.addItem("Delta", "delta")
        oct_mode_index = oct_mode.findData(data.get("oct_golden_mode", "off") if data else "off")
        oct_mode.setCurrentIndex(oct_mode_index if oct_mode_index >= 0 else 0)
        ow_lay.addRow("Golden mode", oct_mode)
        oct_overlay = QCheckBox("Show golden curve")
        oct_overlay.setObjectName("oct_show_golden_overlay")
        oct_overlay.setChecked(bool(data.get("oct_show_golden_overlay", False)) if data else False)
        ow_lay.addRow("Golden curve", oct_overlay)
        of_lay.addWidget(oct_w_row)

        oct_mode.currentIndexChanged.connect(
            lambda _index, combo=oct_mode, checkbox=oct_overlay, current=container: self._on_golden_mode_changed(combo, checkbox, current)
        )

        oct_table = self._build_band_table(
            "oct_band_table",
            data.get("oct_band_limits", []) if data else [],
        )
        of_lay.addWidget(oct_table)
        oct_section.add_widget(oct_form)
        lay.addWidget(oct_section)

        self._apply_golden_mode_state(fft_mode, fft_overlay)
        self._apply_golden_mode_state(oct_mode, oct_overlay)
        self._refresh_channel_golden_section(container)

        return container

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

    def _refresh_ai_toolchains(self, bound_toolchain_id: str | None = None):
        """Populate the AI toolchain combobox from enabled toolchains.

        If bound_toolchain_id refers to a disabled/missing toolchain, it is
        still injected into the list (marked as unavailable) so that
        load→save round-trips do not silently erase persisted bindings.
        """
        self.combo_ai_toolchain.clear()
        self.combo_ai_toolchain.addItem("（未选择）", None)
        toolchains = self.services.ai_service.list_enabled_toolchains()
        known_ids = {tc["toolchain_id"] for tc in toolchains}
        for tc in toolchains:
            label = f"{tc['display_name']} (v{tc['version']})"
            self.combo_ai_toolchain.addItem(label, tc["toolchain_id"])

        # If the bound toolchain is not in the enabled list, inject it so
        # the user sees the current selection instead of silently losing it.
        if bound_toolchain_id and bound_toolchain_id not in known_ids:
            tc_row = self.services.ai_service.get_toolchain(bound_toolchain_id)
            if tc_row:
                label = f"{tc_row.display_name} (v{tc_row.version}) [不可用]"
            else:
                label = f"{bound_toolchain_id} [已删除]"
            self.combo_ai_toolchain.addItem(label, bound_toolchain_id)

    def _populate_ai_binding(self, config_id: int):
        """Load AI binding for config_id into the UI card."""
        binding = self.services.ai_service.resolve_binding(config_id)
        bound_tc_id = binding["toolchain_id"] if binding else None
        self._refresh_ai_toolchains(bound_tc_id)
        if binding is None:
            self.chk_ai_enabled.setChecked(False)
            self.combo_ai_toolchain.setCurrentIndex(0)
            self.spin_ai_timeout.setValue(0)
            self.txt_ai_params.setPlainText("{}")
            return

        self.chk_ai_enabled.setChecked(binding["enabled"])
        idx = self.combo_ai_toolchain.findData(binding["toolchain_id"])
        self.combo_ai_toolchain.setCurrentIndex(idx if idx >= 0 else 0)
        self.spin_ai_timeout.setValue(binding["timeout_ms"] or 0)
        self.txt_ai_params.setPlainText(binding["params_json"])

    def _populate_hardware_card(self, data: dict):
        """Load saved device/trigger/channel selections into hardware card widgets."""
        # Block input device signal to prevent _on_input_device_changed during population
        self.combo_input_dev.blockSignals(True)

        self._populate_devices()
        self.ed_plc_device_key.setText(data.get("plc_device_key") or "")
        self.ed_plc_device_label.setText(data.get("plc_device_label") or "")

        saved_input = data.get("input_device_name")
        saved_input_api = data.get("input_device_hostapi")
        saved_input_sr = data.get("input_device_samplerate")
        self._match_device_combo(
            self.combo_input_dev, saved_input, saved_input_api, saved_input_sr
        )

        saved_output = data.get("output_device_name")
        saved_output_api = data.get("output_device_hostapi")
        saved_output_sr = data.get("output_device_samplerate")
        self._match_device_combo(
            self.combo_output_dev, saved_output, saved_output_api, saved_output_sr
        )

        self._rebuild_channel_checkboxes_silent()

        self.combo_input_dev.blockSignals(False)

        selected = data.get("selected_channels", [])
        for i, cb in enumerate(self._channel_checkboxes):
            cb.blockSignals(True)
            cb.setChecked(i in selected)
            cb.blockSignals(False)

    def _match_device_combo(self, combo, name, hostapi, samplerate):
        """Match saved device against combo items. Inject unavailable device if not found."""
        if not name:
            combo.setCurrentIndex(0)
            return

        for i in range(1, combo.count()):
            dev = combo.itemData(i)
            if dev and isinstance(dev, dict):
                if dev.get("name") == name and dev.get("hostapi") == hostapi:
                    combo.setCurrentIndex(i)
                    return

        label = f"{name} ({hostapi}) (设备不可用)"
        dev_info = {
            "name": name,
            "hostapi": hostapi,
            "default_samplerate": samplerate,
            "max_input_channels": 0,
            "max_output_channels": 0,
        }
        combo.addItem(label, dev_info)
        combo.setCurrentIndex(combo.count() - 1)

    def _rebuild_channel_checkboxes_silent(self):
        """Rebuild channel checkboxes without triggering _on_channel_selection_changed."""
        for cb in self._channel_checkboxes:
            cb.deleteLater()
        self._channel_checkboxes = []
        while self._channel_check_layout.count():
            self._channel_check_layout.takeAt(0)

        dev_info = self.combo_input_dev.currentData()
        if not dev_info or not isinstance(dev_info, dict):
            return

        max_ch = dev_info.get("max_input_channels", 0)
        for i in range(max_ch):
            cb = QCheckBox(f"In{i + 1}")
            cb.toggled.connect(self._on_channel_selection_changed)
            row, col = divmod(i, 6)
            self._channel_check_layout.addWidget(cb, row, col)
            self._channel_checkboxes.append(cb)

    # ── Data collection ─────────────────────────────────────

    def _collect_channel_data(self, container: QWidget) -> dict:
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

        combo_loudness_unit = container.findChild(QComboBox, "loudness_unit")
        ch["loudness_unit"] = (
            combo_loudness_unit.currentText()
            if combo_loudness_unit
            else "sone"
        )

        spin_loudness_lo = container.findChild(
            QDoubleSpinBox, "loudness_lower_limit"
        )
        ch["loudness_lower_limit"] = (
            spin_loudness_lo.value() if spin_loudness_lo else 0.0
        )

        spin_loudness_hi = container.findChild(
            QDoubleSpinBox, "loudness_upper_limit"
        )
        ch["loudness_upper_limit"] = (
            spin_loudness_hi.value() if spin_loudness_hi else 60.0
        )

        combo_oct_w = container.findChild(QComboBox, "oct_weighting")
        ch["oct_weighting"] = combo_oct_w.currentText() if combo_oct_w else "A"

        fft_mode = container.findChild(QComboBox, "fft_golden_mode")
        ch["fft_golden_mode"] = fft_mode.currentData() if fft_mode else "off"
        fft_overlay = container.findChild(QCheckBox, "fft_show_golden_overlay")
        ch["fft_show_golden_overlay"] = fft_overlay.isChecked() if fft_overlay else False

        oct_mode = container.findChild(QComboBox, "oct_golden_mode")
        ch["oct_golden_mode"] = oct_mode.currentData() if oct_mode else "off"
        oct_overlay = container.findChild(QCheckBox, "oct_show_golden_overlay")
        ch["oct_show_golden_overlay"] = oct_overlay.isChecked() if oct_overlay else False

        ch.update(self._channel_binding(container))
        ch["fft_band_limits"] = self._read_band_table(container, "fft_band_table")
        ch["oct_band_limits"] = self._read_band_table(container, "oct_band_table")
        ch["ai_config"] = {}
        return ch

    def _collect_data(self) -> dict:
        input_dev = self.combo_input_dev.currentData()
        output_dev = self.combo_output_dev.currentData()

        selected_channels = [
            i for i, cb in enumerate(self._channel_checkboxes) if cb.isChecked()
        ]

        data = {
            "config_id": self._current_config_id,
            "config_name": self.ed_name.text(),
            "record_duration": self.spin_record.value(),
            "analysis_duration": self.spin_analysis.value(),
            "analysis_window_offset_ms": self.spin_analysis_window_offset.value(),
            "scan_triggers_test": self.chk_scan.isChecked(),
            "plc_device_key": self.ed_plc_device_key.text(),
            "plc_device_label": self.ed_plc_device_label.text(),
            "input_device_name": input_dev.get("name") if isinstance(input_dev, dict) else None,
            "input_device_hostapi": input_dev.get("hostapi") if isinstance(input_dev, dict) else None,
            "input_device_samplerate": input_dev.get("default_samplerate") if isinstance(input_dev, dict) else None,
            "output_device_name": output_dev.get("name") if isinstance(output_dev, dict) else None,
            "output_device_hostapi": output_dev.get("hostapi") if isinstance(output_dev, dict) else None,
            "output_device_samplerate": output_dev.get("default_samplerate") if isinstance(output_dev, dict) else None,
            "selected_channels": selected_channels,
            "channels": [],
            "golden_editor_state": {},
        }

        for container in self._channel_containers:
            data["channels"].append(self._collect_channel_data(container))

        valid_channel_indices = {
            channel["channel_index"]
            for channel in data["channels"]
            if isinstance(channel.get("channel_index"), int)
            and not isinstance(channel.get("channel_index"), bool)
        }
        data["golden_editor_state"] = {
            channel_index: dict(binding)
            for channel_index, binding in self._golden_editor_state.items()
            if channel_index in valid_channel_indices
        }

        return data

    def _collect_ai_binding(self) -> dict:
        """Collect AI binding data from the UI card."""
        tc_id = self.combo_ai_toolchain.currentData()
        timeout = self.spin_ai_timeout.value()
        return {
            "enabled": self.chk_ai_enabled.isChecked(),
            "toolchain_id": tc_id,
            "timeout_ms": timeout if timeout > 0 else None,
            "params_json": self.txt_ai_params.toPlainText().strip() or "{}",
        }

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
        self._discard_all_staged_bindings()
        self.ed_name.setText(data["config_name"])
        self.spin_record.setValue(data["record_duration"])
        self.spin_analysis.setValue(data["analysis_duration"])
        self.spin_analysis_window_offset.setValue(
            data.get("analysis_window_offset_ms", 0)
        )
        self.chk_scan.setChecked(data["scan_triggers_test"])

        self._populate_hardware_card(data)

        # Build tabs for ALL channels of the device, not just selected ones
        dev_info = self.combo_input_dev.currentData()
        if dev_info and isinstance(dev_info, dict):
            max_ch = dev_info.get("max_input_channels", 0)
            all_indices = list(range(max_ch))
        else:
            all_indices = []
        # For legacy configs with no device, use channel data indices
        if not all_indices and data.get("channels"):
            all_indices = [ch["channel_index"] for ch in data["channels"]]
        self._build_channel_tabs(all_indices, data.get("channels"))
        self._populate_ai_binding(data["config_id"])
        self._refresh_all_golden_sections()

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
        pending_save = None
        pending_save_committed = False
        try:
            # Validate AI binding before saving anything
            ai_data = self._collect_ai_binding()
            params_text = ai_data.get("params_json", "{}")
            try:
                import json as _json
                parsed = _json.loads(params_text)
                if not isinstance(parsed, dict):
                    raise ValueError("JSON 必须是对象")
            except (ValueError, _json.JSONDecodeError) as e:
                QMessageBox.warning(self, "AI 配置错误", f"参数(JSON) 格式无效：{e}")
                return

            from model.base_mode import db
            with db.atomic():
                begin_save = getattr(self.services.config_service, "begin_save_config", None)
                if callable(begin_save):
                    pending_save = begin_save(data)
                    saved_id = pending_save.config_id
                else:
                    saved_id = self.services.config_service.save_config(data)
                self.services.ai_service.save_binding(saved_id, ai_data)
            commit_pending_save = getattr(
                self.services.config_service,
                "commit_pending_save",
                None,
            )
            if callable(commit_pending_save):
                pending_save_committed = True
                commit_pending_save(pending_save)
            loaded = self.services.config_service.load_config(saved_id)
            self._skip_reload = True
            self._refresh_list()
            self._select_config_by_id(saved_id)
            self._skip_reload = False
            self._current_config_id = saved_id
            self._populate_detail(loaded)
            self.services.bus.status.emit("配置已保存")
        except ValueError as e:
            self._skip_reload = False
            rollback_pending_save = getattr(
                self.services.config_service,
                "rollback_pending_save",
                None,
            )
            if callable(rollback_pending_save) and not pending_save_committed:
                rollback_pending_save(pending_save)
            self._reconcile_golden_bindings_after_save_failure(
                pending_save,
                pending_save_committed=pending_save_committed,
            )
            QMessageBox.warning(self, "保存失败", str(e))
        except Exception as e:
            self._skip_reload = False
            rollback_pending_save = getattr(
                self.services.config_service,
                "rollback_pending_save",
                None,
            )
            if callable(rollback_pending_save) and not pending_save_committed:
                rollback_pending_save(pending_save)
            self._reconcile_golden_bindings_after_save_failure(
                pending_save,
                pending_save_committed=pending_save_committed,
            )
            QMessageBox.warning(self, "保存失败", str(e))

    def _on_hardware_changed(self):
        """Global hardware changed — refresh device combos, preserve current selection."""
        if self._current_config_id is None:
            return

        # Save current selections before repopulating
        cur_input = self.combo_input_dev.currentData()
        cur_output = self.combo_output_dev.currentData()

        # Block signals to prevent _on_input_device_changed from firing
        self.combo_input_dev.blockSignals(True)
        self.combo_output_dev.blockSignals(True)

        self._populate_devices()

        # Restore previous selections
        if cur_input and isinstance(cur_input, dict):
            self._match_device_combo(
                self.combo_input_dev,
                cur_input.get("name"),
                cur_input.get("hostapi"),
                cur_input.get("default_samplerate"),
            )
        if cur_output and isinstance(cur_output, dict):
            self._match_device_combo(
                self.combo_output_dev,
                cur_output.get("name"),
                cur_output.get("hostapi"),
                cur_output.get("default_samplerate"),
            )

        self.combo_input_dev.blockSignals(False)
        self.combo_output_dev.blockSignals(False)
