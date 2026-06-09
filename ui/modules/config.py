from __future__ import annotations

import copy
from contextlib import contextmanager

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QVBoxLayout,
    QWidget,
)

from app.service.current_meter_service import current_meter_display_unit, normalize_current_meter_config
from model.plc_table import PlcTable
from ui.popups import config_package_dialogs
from ui.popups.golden_sample_import_dialog import GoldenSampleImportDialog
from ui.widgets.cards import Card
from ui.widgets.collapsible_group_box import CollapsibleGroupBox
from ui.widgets.limit_table_editor import LimitTableEditor

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
_PRESERVE_PLC_SELECTION = object()


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


class _ConfigSectionCardDialog(QDialog):
    def __init__(
        self,
        title: str,
        content: QWidget,
        parent: QWidget,
        *,
        object_name: str,
        close_button_object_name: str,
        default_position_anchor: QWidget | None = None,
        confirm_button_object_name: str = "ConfigSectionCardConfirmButton",
        preferred_size: tuple[int, int] | None = None,
    ):
        super().__init__(
            parent,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName(object_name)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._drag_offset: QPoint | None = None
        self._user_moved = False
        self._default_position_anchor = default_position_anchor
        self._preferred_size = preferred_size

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        frame = QFrame()
        frame.setObjectName("ChannelSectionCard")
        frame.setStyleSheet(
            "QFrame#ChannelSectionCard { background: white; border: 1px solid #cbd5e1; "
            "border-radius: 8px; }"
            "QWidget#ChannelSectionCardTitle { background: #f8fafc; "
            "border-bottom: 1px solid #e2e8f0; border-top-left-radius: 8px; "
            "border-top-right-radius: 8px; }"
        )
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        title_bar = QWidget()
        title_bar.setObjectName("ChannelSectionCardTitle")
        title_bar.setCursor(Qt.CursorShape.SizeAllCursor)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(12, 8, 8, 8)
        title_layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("ChannelSectionCardTitleLabel")
        title_label.setStyleSheet("font-weight: 700; color: #334155;")
        title_layout.addWidget(title_label, 1)

        close_button = QPushButton("X")
        close_button.setObjectName(close_button_object_name)
        close_button.clicked.connect(self.hide)
        title_layout.addWidget(close_button, 0)
        frame_layout.addWidget(title_bar)

        body_widget = QWidget()
        body_layout = QVBoxLayout(body_widget)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(0)
        body_layout.addWidget(content)
        body_layout.addStretch(1)

        body_scroll = QScrollArea()
        body_scroll.setObjectName("ConfigSectionCardBodyScroll")
        body_scroll.setWidgetResizable(True)
        body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_scroll.setWidget(body_widget)
        frame_layout.addWidget(body_scroll, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 12)
        footer_layout.setSpacing(8)
        footer_layout.addStretch(1)

        confirm_button = QPushButton("确定")
        confirm_button.setObjectName(confirm_button_object_name)
        confirm_button.clicked.connect(self._confirm)
        footer_layout.addWidget(confirm_button, 0)
        frame_layout.addWidget(footer, 0)
        root.addWidget(frame)

        self._title_bar = title_bar
        self._content = content
        self._title_bar.mousePressEvent = self._on_title_mouse_press
        self._title_bar.mouseMoveEvent = self._on_title_mouse_move
        self._title_bar.mouseReleaseEvent = self._on_title_mouse_release

    def show_card(self) -> None:
        self.show()
        if not self._user_moved:
            self._move_to_default_position()
            QTimer.singleShot(0, self._move_to_default_position_if_needed)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()

    def _confirm(self) -> None:
        self.hide()

    def _move_to_default_position(self) -> None:
        anchor = self._default_position_anchor or self.parentWidget()
        if anchor is None:
            return
        self.adjustSize()
        self._apply_preferred_size(anchor)
        owner = anchor.window()
        owner_top_left = owner.mapToGlobal(QPoint(0, 0))
        self.move(
            QPoint(
                owner_top_left.x() + (owner.width() - self.width()) // 2,
                owner_top_left.y() + (owner.height() - self.height()) // 2,
            )
        )

    def _apply_preferred_size(self, anchor: QWidget) -> None:
        if self._preferred_size is None:
            return
        owner = anchor.window()
        preferred_width, preferred_height = self._preferred_size
        max_width = max(320, owner.width() - 48)
        max_height = max(360, owner.height() - 48)
        self.resize(
            min(max(self.width(), preferred_width), max_width),
            min(max(self.height(), preferred_height), max_height),
        )

    def _move_to_default_position_if_needed(self) -> None:
        if self.isVisible() and not self._user_moved:
            self._move_to_default_position()

    def _on_title_mouse_press(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        event.accept()

    def _on_title_mouse_move(self, event) -> None:
        if self._drag_offset is None:
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        self._user_moved = True
        event.accept()

    def _on_title_mouse_release(self, event) -> None:
        self._drag_offset = None
        event.accept()


class _ChannelSectionCardDialog(_ConfigSectionCardDialog):
    def __init__(
        self,
        title: str,
        section: CollapsibleGroupBox,
        parent: QWidget,
        preferred_size: tuple[int, int] | None = None,
    ):
        super().__init__(
            title,
            section,
            parent,
            object_name="ChannelSectionCardDialog",
            close_button_object_name="ChannelSectionCardCloseButton",
            confirm_button_object_name="ChannelSectionCardConfirmButton",
            preferred_size=preferred_size,
        )


def _numeric_value(data: dict | None, key: str) -> float:
    value = data.get(key, _LIMIT_DEFAULTS[key]) if data else _LIMIT_DEFAULTS[key]
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else _LIMIT_DEFAULTS[key]


def _checked_value(data: dict | None, key: str) -> bool:
    return bool(data.get(key, _LIMIT_DEFAULTS[key])) if data else bool(_LIMIT_DEFAULTS[key])


def _create_limit_spinbox(
    *,
    object_name: str,
    value: float,
    minimum: float,
    maximum: float,
    decimals: int = 1,
    suffix: str = "",
) -> _NoWheelSpinBox:
    spinbox = _NoWheelSpinBox()
    spinbox.setObjectName(object_name)
    spinbox.setRange(minimum, maximum)
    spinbox.setDecimals(decimals)
    if suffix:
        spinbox.setSuffix(suffix)
    spinbox.setValue(float(value))
    return spinbox


def _create_limit_checkbox(*, object_name: str, checked: bool, label: str) -> QCheckBox:
    checkbox = QCheckBox(label)
    checkbox.setObjectName(object_name)
    checkbox.setChecked(bool(checked))
    return checkbox


def _create_limit_row(
    *,
    data: dict | None,
    lower_enabled_key: str,
    lower_value_key: str,
    upper_enabled_key: str,
    upper_value_key: str,
    minimum: float,
    maximum: float,
    decimals: int = 1,
    suffix: str = "",
) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    lower_enabled = _create_limit_checkbox(
        object_name=lower_enabled_key,
        checked=_checked_value(data, lower_enabled_key),
        label="下限",
    )
    lower_value = _create_limit_spinbox(
        object_name=lower_value_key,
        value=_numeric_value(data, lower_value_key),
        minimum=minimum,
        maximum=maximum,
        decimals=decimals,
        suffix=suffix,
    )
    upper_enabled = _create_limit_checkbox(
        object_name=upper_enabled_key,
        checked=_checked_value(data, upper_enabled_key),
        label="上限",
    )
    upper_value = _create_limit_spinbox(
        object_name=upper_value_key,
        value=_numeric_value(data, upper_value_key),
        minimum=minimum,
        maximum=maximum,
        decimals=decimals,
        suffix=suffix,
    )
    for widget in (lower_enabled, lower_value, upper_enabled, upper_value):
        layout.addWidget(widget)
    layout.addStretch(1)
    return row


_LIMIT_DEFAULTS = {
    "spl_lower_enabled": False,
    "spl_lower_limit": 40.0,
    "spl_upper_enabled": False,
    "spl_upper_limit": 85.0,
    "spl_average_lower_enabled": False,
    "spl_average_lower_limit": 40.0,
    "spl_average_upper_enabled": False,
    "spl_average_upper_limit": 85.0,
    "loudness_lower_enabled": False,
    "loudness_lower_limit": 0.0,
    "loudness_upper_enabled": False,
    "loudness_upper_limit": 60.0,
    "loudness_average_lower_enabled": False,
    "loudness_average_lower_limit": 0.0,
    "loudness_average_upper_enabled": False,
    "loudness_average_upper_limit": 60.0,
}


class ConfigModule(QWidget):
    module_id = "config"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._current_config_id: int | None = None
        self._skip_reload = False
        self._golden_editor_state: dict[int, dict] = {}
        self._ai_binding_dirty = False
        self._loaded_ai_binding_state = self._normalize_ai_binding(None)
        self._suspend_ai_binding_dirty_tracking = False
        self._monitor_channel_explicit = False
        self._suspend_monitor_channel_tracking = False
        self._channel_section_dialogs: dict[tuple[int, str], _ChannelSectionCardDialog] = {}
        self._top_level_config_dialogs: dict[str, _ConfigSectionCardDialog] = {}
        self._loaded_config_editor_snapshot: dict | None = None
        self._restoring_config_selection = False

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
        self.btn_import = QPushButton("导入")
        self.btn_export = QPushButton("导出")
        btn_lay.addWidget(self.btn_new)
        btn_lay.addWidget(self.btn_delete)
        btn_lay.addWidget(self.btn_import)
        btn_lay.addWidget(self.btn_export)
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

        self.chk_allow_incomplete_recording_analysis = QCheckBox(
            "允许分析不完整录音"
        )
        gf_lay.addRow(self.chk_allow_incomplete_recording_analysis)

        scroll_lay.addWidget(Card("全局参数", global_form))

        # PLC binding remains visible at the top level.
        plc_form = QWidget()
        plc_lay = QFormLayout(plc_form)
        plc_lay.setContentsMargins(0, 0, 0, 0)
        plc_lay.setHorizontalSpacing(10)
        plc_lay.setVerticalSpacing(8)

        self.combo_plc = _NoWheelComboBox()
        self.combo_plc.setObjectName("combo_plc")
        plc_lay.addRow("PLC", self.combo_plc)

        scroll_lay.addWidget(Card("PLC", plc_form))

        top_launcher_panel = QWidget()
        top_launcher_panel.setObjectName("top_level_config_launcher_panel")
        top_launcher_layout = QGridLayout(top_launcher_panel)
        top_launcher_layout.setContentsMargins(0, 0, 0, 0)
        top_launcher_layout.setHorizontalSpacing(8)
        top_launcher_layout.setVerticalSpacing(8)
        for column in range(3):
            top_launcher_layout.setColumnStretch(column, 1)
        scroll_lay.addWidget(top_launcher_panel)
        top_launcher_index = 0

        audio_form = QWidget()
        audio_lay = QFormLayout(audio_form)
        audio_lay.setContentsMargins(0, 0, 0, 0)
        audio_lay.setHorizontalSpacing(10)
        audio_lay.setVerticalSpacing(8)

        self.combo_input_dev = _NoWheelComboBox()
        audio_lay.addRow("输入设备", self.combo_input_dev)

        self.combo_output_dev = _NoWheelComboBox()
        audio_lay.addRow("监听设备", self.combo_output_dev)

        self._channel_check_container = QWidget()
        self._channel_check_layout = QGridLayout(self._channel_check_container)
        self._channel_check_layout.setContentsMargins(0, 0, 0, 0)
        self._channel_check_layout.setSpacing(4)
        self._channel_checkboxes = []
        audio_lay.addRow("通道选择", self._channel_check_container)

        self.combo_vibration_view_channel = _NoWheelComboBox()
        self.combo_vibration_view_channel.setObjectName("combo_vibration_view_channel")
        audio_lay.addRow("振动视图通道", self.combo_vibration_view_channel)

        self.combo_monitor_channel = _NoWheelComboBox()
        self.combo_monitor_channel.setObjectName("combo_monitor_channel")
        audio_lay.addRow("监听通道", self.combo_monitor_channel)

        self.spin_monitor_gain_db = _NoWheelSpinBox()
        self.spin_monitor_gain_db.setObjectName("spin_monitor_gain_db")
        self.spin_monitor_gain_db.setRange(-60.0, 24.0)
        self.spin_monitor_gain_db.setSingleStep(1.0)
        self.spin_monitor_gain_db.setDecimals(1)
        self.spin_monitor_gain_db.setSuffix(" dB")
        audio_lay.addRow("监听增益", self.spin_monitor_gain_db)

        self._register_top_level_config_card(
            launcher_layout=top_launcher_layout,
            launcher_index=top_launcher_index,
            section_id="audio_vibration_hardware",
            title="音频/振动硬件设置",
            content=audio_form,
            default_position_anchor=top_launcher_panel,
            button_object_name="open_audio_vibration_hardware_card",
            dialog_object_name="top_level_audio_vibration_hardware_card_dialog",
        )
        top_launcher_index += 1

        current_meter_form = QWidget()
        cm_lay = QFormLayout(current_meter_form)
        cm_lay.setContentsMargins(0, 0, 0, 0)
        cm_lay.setHorizontalSpacing(10)
        cm_lay.setVerticalSpacing(8)

        self.chk_current_meter_enabled = QCheckBox("启用电流表")
        cm_lay.addRow("电流表", self.chk_current_meter_enabled)

        self.ed_current_meter_host = QLineEdit()
        self.ed_current_meter_host.setPlaceholderText("例如 192.168.1.20")
        cm_lay.addRow("电流表IP", self.ed_current_meter_host)

        self.spin_current_meter_port = _NoWheelSpinBoxInt()
        self.spin_current_meter_port.setRange(1, 65535)
        self.spin_current_meter_port.setValue(502)
        cm_lay.addRow("电流表端口", self.spin_current_meter_port)

        self.spin_current_meter_unit_id = _NoWheelSpinBoxInt()
        self.spin_current_meter_unit_id.setRange(0, 247)
        self.spin_current_meter_unit_id.setValue(1)
        cm_lay.addRow("电流表站号", self.spin_current_meter_unit_id)

        self.combo_current_meter_register_type = _NoWheelComboBox()
        self.combo_current_meter_register_type.addItem("Holding Register", "holding")
        self.combo_current_meter_register_type.addItem("Input Register", "input")
        self.combo_current_meter_register_type.setCurrentIndex(
            self.combo_current_meter_register_type.findData("input")
        )
        cm_lay.addRow("电流表寄存器", self.combo_current_meter_register_type)

        self.spin_current_meter_register_address = _NoWheelSpinBoxInt()
        self.spin_current_meter_register_address.setRange(0, 65535)
        self.spin_current_meter_register_address.setValue(0)
        cm_lay.addRow("电流表地址", self.spin_current_meter_register_address)

        self.combo_current_meter_data_type = _NoWheelComboBox()
        self.combo_current_meter_data_type.addItem("Float32", "float32")
        self.combo_current_meter_data_type.addItem("UInt16", "uint16")
        self.combo_current_meter_data_type.addItem("Int16", "int16")
        self.combo_current_meter_data_type.addItem("UInt32", "uint32")
        self.combo_current_meter_data_type.addItem("Int32", "int32")
        self.combo_current_meter_data_type.setCurrentIndex(
            self.combo_current_meter_data_type.findData("uint16")
        )
        cm_lay.addRow("电流表数据类型", self.combo_current_meter_data_type)

        self.combo_current_meter_byte_order = _NoWheelComboBox()
        self.combo_current_meter_byte_order.addItem("Big endian", "big")
        self.combo_current_meter_byte_order.addItem("Little endian", "little")
        cm_lay.addRow("电流表字节序", self.combo_current_meter_byte_order)

        self.combo_current_meter_word_order = _NoWheelComboBox()
        self.combo_current_meter_word_order.addItem("Big word", "big")
        self.combo_current_meter_word_order.addItem("Little word", "little")
        cm_lay.addRow("电流表字序", self.combo_current_meter_word_order)

        self.spin_current_meter_scale = _NoWheelSpinBox()
        self.spin_current_meter_scale.setRange(-999999.0, 999999.0)
        self.spin_current_meter_scale.setDecimals(6)
        self.spin_current_meter_scale.setValue(0.001)
        cm_lay.addRow("电流表倍率", self.spin_current_meter_scale)

        self.spin_current_meter_offset = _NoWheelSpinBox()
        self.spin_current_meter_offset.setObjectName("spin_current_meter_offset")
        self.spin_current_meter_offset.setRange(-999999.0, 999999.0)
        self.spin_current_meter_offset.setDecimals(6)
        self.spin_current_meter_offset.setValue(0.0)
        cm_lay.addRow("电流表偏移(mA)", self.spin_current_meter_offset)

        self.spin_current_meter_poll_interval_ms = _NoWheelSpinBoxInt()
        self.spin_current_meter_poll_interval_ms.setRange(20, 60000)
        self.spin_current_meter_poll_interval_ms.setValue(200)
        self.spin_current_meter_poll_interval_ms.setSuffix(" ms")
        cm_lay.addRow("电流表轮询", self.spin_current_meter_poll_interval_ms)

        self.spin_current_meter_timeout_ms = _NoWheelSpinBoxInt()
        self.spin_current_meter_timeout_ms.setRange(50, 60000)
        self.spin_current_meter_timeout_ms.setValue(1000)
        self.spin_current_meter_timeout_ms.setSuffix(" ms")
        cm_lay.addRow("电流表超时", self.spin_current_meter_timeout_ms)

        self._register_top_level_config_card(
            launcher_layout=top_launcher_layout,
            launcher_index=top_launcher_index,
            section_id="current_meter",
            title="电流表设置",
            content=current_meter_form,
            default_position_anchor=top_launcher_panel,
            button_object_name="open_current_meter_card",
            dialog_object_name="top_level_current_meter_card_dialog",
            preferred_size=(380, 640),
        )
        top_launcher_index += 1

        # AI binding (test-level, not per-channel)
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

        self._register_top_level_config_card(
            launcher_layout=top_launcher_layout,
            launcher_index=top_launcher_index,
            section_id="ai_binding",
            title="AI 工具链绑定",
            content=ai_bind_form,
            default_position_anchor=top_launcher_panel,
            button_object_name="open_ai_binding_card",
            dialog_object_name="top_level_ai_binding_card_dialog",
        )

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
        self.btn_import.clicked.connect(self._on_import_package)
        self.btn_export.clicked.connect(self._on_export_package)
        self.btn_save.clicked.connect(self._on_save)
        self.config_list.currentItemChanged.connect(self._on_item_changed)
        self.combo_input_dev.currentIndexChanged.connect(self._on_input_device_changed)
        self.combo_monitor_channel.currentIndexChanged.connect(
            self._on_monitor_channel_changed
        )
        self.spin_analysis.valueChanged.connect(
            lambda _value: self._refresh_all_golden_sections()
        )
        self.chk_ai_enabled.toggled.connect(self._mark_ai_binding_dirty)
        self.combo_ai_toolchain.currentIndexChanged.connect(self._mark_ai_binding_dirty)
        self.spin_ai_timeout.valueChanged.connect(self._mark_ai_binding_dirty)
        self.combo_current_meter_register_type.currentIndexChanged.connect(
            self._refresh_current_meter_offset_label
        )
        self.combo_current_meter_data_type.currentIndexChanged.connect(
            self._refresh_current_meter_offset_label
        )
        self.combo_current_meter_byte_order.currentIndexChanged.connect(
            self._refresh_current_meter_offset_label
        )
        self.combo_current_meter_word_order.currentIndexChanged.connect(
            self._refresh_current_meter_offset_label
        )
        self.spin_current_meter_scale.valueChanged.connect(
            self._refresh_current_meter_offset_label
        )
        self.services.bus.refresh_channels.connect(self._on_hardware_changed)
        ai_toolchains_changed = getattr(
            self.services.bus,
            "ai_toolchains_changed",
            None,
        )
        if ai_toolchains_changed is not None:
            ai_toolchains_changed.connect(self._on_ai_toolchains_changed)
        plc_changed = getattr(self.services.bus, "plc_changed", None)
        if plc_changed is not None:
            plc_changed.connect(self._on_plc_changed)

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
            hostapi_value = hostapi_names.get(hostapi_idx, "N/A")
            hostapi_display = "未知" if hostapi_value == "N/A" else hostapi_value
            label = f"{dev.get('name', '?')} ({hostapi_display})"
            dev_info = {
                "name": dev.get("name", ""),
                "hostapi": hostapi_value,
                "default_samplerate": dev.get("default_samplerate"),
                "max_input_channels": dev.get("max_input_channels", 0),
                "max_output_channels": dev.get("max_output_channels", 0),
            }
            if dev.get("max_input_channels", 0) > 0:
                self.combo_input_dev.addItem(label, dev_info)
            if dev.get("max_output_channels", 0) > 0:
                self.combo_output_dev.addItem(label, dev_info)

    @staticmethod
    def _format_plc_option_label(plc_row: PlcTable) -> str:
        plc_name = (plc_row.plc_name or "").strip()
        if plc_name:
            return f"ID {plc_row.id} - {plc_name}"
        return f"ID {plc_row.id}"

    def _refresh_plc_selector(
        self,
        selected_plc_id: int | None | object = _PRESERVE_PLC_SELECTION,
    ) -> None:
        if (
            selected_plc_id is _PRESERVE_PLC_SELECTION
            and self.combo_plc.count() > 0
        ):
            selected_plc_id = self.combo_plc.currentData()
        self.combo_plc.blockSignals(True)
        self.combo_plc.clear()
        self.combo_plc.addItem("（未绑定）", None)
        for plc_row in PlcTable.select().order_by(PlcTable.id):
            self.combo_plc.addItem(
                self._format_plc_option_label(plc_row),
                plc_row.id,
            )
        index = self.combo_plc.findData(selected_plc_id)
        self.combo_plc.setCurrentIndex(index if index >= 0 else 0)
        self.combo_plc.blockSignals(False)

    def _on_plc_changed(self) -> None:
        self._refresh_plc_selector()

    def _on_input_device_changed(self, index):
        """Rebuild channel checkboxes and tabs when input device changes."""
        existing_channel_data = self._current_channel_editor_data()
        preferred_monitor_channel = self.combo_monitor_channel.currentData()
        preferred_vibration_channel = self.combo_vibration_view_channel.currentData()
        was_monitor_channel_explicit = self._monitor_channel_explicit
        for cb in self._channel_checkboxes:
            cb.deleteLater()
        self._channel_checkboxes = []

        while self._channel_check_layout.count():
            self._channel_check_layout.takeAt(0)

        dev_info = self.combo_input_dev.currentData()
        if not dev_info or not isinstance(dev_info, dict):
            self._prune_staged_bindings([])
            self._rebuild_monitor_channel_options(preferred=None)
            self._rebuild_vibration_view_channel_options(preferred=None)
            self._monitor_channel_explicit = False
            self._build_channel_tabs([], None)
            return

        max_ch = dev_info.get("max_input_channels", 0)
        for i in range(max_ch):
            cb = QCheckBox(f"输入{i + 1}")
            cb.toggled.connect(self._on_channel_selection_changed)
            row, col = divmod(i, 6)
            self._channel_check_layout.addWidget(cb, row, col)
            self._channel_checkboxes.append(cb)

        self._rebuild_monitor_channel_options(preferred=preferred_monitor_channel)
        self._rebuild_vibration_view_channel_options(
            preferred=preferred_vibration_channel
        )
        self._monitor_channel_explicit = (
            was_monitor_channel_explicit
            and self._is_valid_monitor_channel(
                preferred_monitor_channel,
                self._current_input_channel_count(),
            )
        )

        # Rebuild tabs for ALL channels, not just checked ones
        all_indices = list(range(max_ch))
        self._prune_staged_bindings(all_indices)
        self._build_channel_tabs(all_indices, existing_channel_data)
        self._refresh_all_golden_sections()

    def _on_channel_selection_changed(self):
        """Channel checkbox toggled — tabs are built by _on_input_device_changed."""
        current_vibration_channel = self.combo_vibration_view_channel.currentData()
        self._rebuild_vibration_view_channel_options(
            preferred=current_vibration_channel
        )

        current = self.combo_monitor_channel.currentData()
        max_ch = self._current_input_channel_count()
        if not self._is_valid_monitor_channel(current, max_ch):
            self._rebuild_monitor_channel_options(preferred=None)
            return
        if self._monitor_channel_explicit:
            return
        fallback = self._default_monitor_channel(max_ch)
        if fallback is not None and fallback != current:
            self._set_monitor_channel_current_data(fallback)

    def _on_monitor_channel_changed(self, _index):
        if self._suspend_monitor_channel_tracking:
            return
        if self.combo_monitor_channel.currentData() is not None:
            self._monitor_channel_explicit = True

    def _current_input_channel_count(self) -> int:
        dev_info = self.combo_input_dev.currentData()
        if not dev_info or not isinstance(dev_info, dict):
            return 0
        try:
            return max(0, int(dev_info.get("max_input_channels", 0)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_valid_monitor_channel(value, max_ch: int) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value < max_ch
        )

    def _default_monitor_channel(self, max_ch: int) -> int | None:
        for i, cb in enumerate(self._channel_checkboxes):
            if i >= max_ch:
                break
            if cb.isChecked():
                return i
        return 0 if max_ch > 0 else None

    def _set_monitor_channel_current_data(self, value: int | None) -> None:
        self._suspend_monitor_channel_tracking = True
        self.combo_monitor_channel.blockSignals(True)
        if value is None:
            self.combo_monitor_channel.setCurrentIndex(-1)
        else:
            index = self.combo_monitor_channel.findData(value)
            self.combo_monitor_channel.setCurrentIndex(index if index >= 0 else -1)
        self.combo_monitor_channel.blockSignals(False)
        self._suspend_monitor_channel_tracking = False

    def _rebuild_monitor_channel_options(self, preferred: int | None = None) -> int | None:
        max_ch = self._current_input_channel_count()
        target = (
            int(preferred)
            if self._is_valid_monitor_channel(preferred, max_ch)
            else self._default_monitor_channel(max_ch)
        )

        self._suspend_monitor_channel_tracking = True
        self.combo_monitor_channel.blockSignals(True)
        self.combo_monitor_channel.clear()
        for i in range(max_ch):
            self.combo_monitor_channel.addItem(f"输入{i + 1}", i)
        if target is not None:
            index = self.combo_monitor_channel.findData(target)
            self.combo_monitor_channel.setCurrentIndex(index if index >= 0 else -1)
        self.combo_monitor_channel.blockSignals(False)
        self._suspend_monitor_channel_tracking = False
        return target

    @staticmethod
    def _is_valid_vibration_view_channel(value, selected_channels: list[int]) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value in selected_channels
        )

    def _set_vibration_view_channel_current_data(self, value: int | None) -> None:
        self.combo_vibration_view_channel.blockSignals(True)
        try:
            index = self.combo_vibration_view_channel.findData(value)
            self.combo_vibration_view_channel.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.combo_vibration_view_channel.blockSignals(False)

    def _rebuild_vibration_view_channel_options(
        self,
        preferred: int | None = None,
    ) -> int | None:
        selected = [
            index
            for index, checkbox in enumerate(self._channel_checkboxes)
            if checkbox.isChecked()
        ]
        target = (
            int(preferred)
            if self._is_valid_vibration_view_channel(preferred, selected)
            else None
        )
        self.combo_vibration_view_channel.blockSignals(True)
        try:
            self.combo_vibration_view_channel.clear()
            self.combo_vibration_view_channel.addItem("无", None)
            for index in selected:
                self.combo_vibration_view_channel.addItem(f"输入{index + 1}", index)
            combo_index = self.combo_vibration_view_channel.findData(target)
            self.combo_vibration_view_channel.setCurrentIndex(
                combo_index if combo_index >= 0 else 0
            )
        finally:
            self.combo_vibration_view_channel.blockSignals(False)
        return target

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

    @staticmethod
    def _channel_container_index(container: QWidget | None) -> int | None:
        if container is None:
            return None
        channel_index = container.property("channel_index")
        if isinstance(channel_index, int) and not isinstance(channel_index, bool):
            return channel_index
        return None

    def _selected_channel_index(self) -> int | None:
        return self._channel_container_index(self.channel_stack.currentWidget())

    def _capture_collapsible_section_state(self) -> dict[tuple[int, str], bool]:
        section_state: dict[tuple[int, str], bool] = {}
        for container in self._channel_containers:
            channel_index = self._channel_container_index(container)
            if channel_index is None:
                continue
            for box in container.findChildren(CollapsibleGroupBox):
                section_state[(channel_index, box.section_id())] = box.is_expanded()
        return section_state

    def _restore_collapsible_section_state(
        self,
        section_state: dict[tuple[int, str], bool],
    ) -> None:
        if not section_state:
            return
        for container in self._channel_containers:
            channel_index = self._channel_container_index(container)
            if channel_index is None:
                continue
            for box in container.findChildren(CollapsibleGroupBox):
                key = (channel_index, box.section_id())
                if key in section_state:
                    box.set_expanded(section_state[key])

    def _restore_selected_channel_tab(self, selected_channel_index: int | None) -> None:
        if not self._channel_tab_buttons:
            return
        if selected_channel_index is not None:
            for page_idx, container in enumerate(self._channel_containers):
                if self._channel_container_index(container) == selected_channel_index:
                    self._on_channel_tab_clicked(page_idx)
                    return
        self._on_channel_tab_clicked(0)

    def _apply_loaded_config_state(self, config_id: int, loaded: dict) -> None:
        self._skip_reload = True
        try:
            self._refresh_list()
            self._select_config_by_id(config_id)
        finally:
            self._skip_reload = False
        self._current_config_id = config_id
        self._populate_detail(loaded)

    def _reload_authoritative_config_state(self, config_id: int) -> None:
        loaded = self.services.config_service.load_config(config_id)
        self._apply_loaded_config_state(config_id, loaded)

    def _capture_editor_snapshot(self) -> dict | None:
        if self._current_config_id is None:
            return None
        return copy.deepcopy(
            {
                "config": self._collect_data(),
                "ai_binding": self._capture_ai_binding_editor_state(),
            }
        )

    def _mark_editor_snapshot_clean(self) -> None:
        self._loaded_config_editor_snapshot = self._capture_editor_snapshot()

    def _has_unsaved_changes(self) -> bool:
        if self._current_config_id is None:
            return False
        current = self._capture_editor_snapshot()
        if current is None:
            return False
        return current != self._loaded_config_editor_snapshot

    def _prompt_unsaved_config_changes(self, action: str) -> bool:
        action_label = {
            "switch": "切换配置",
            "new": "新建配置",
            "delete": "删除配置",
            "import": "导入配置",
            "leave": "离开配置页面",
        }.get(action, "继续当前操作")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("未保存的更改")
        dialog.setText(f"当前配置有未保存的更改。是否保存后再{action_label}？")
        save_button = dialog.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(save_button)
        dialog.setEscapeButton(cancel_button)
        dialog.exec()
        return dialog.clickedButton() is save_button

    def _resolve_unsaved_changes_before(self, action: str) -> bool:
        if not self._has_unsaved_changes():
            return True
        if not self._prompt_unsaved_config_changes(action):
            return False
        return self._save_current_config(show_success=False)

    def confirm_leave(self) -> bool:
        return self._resolve_unsaved_changes_before("leave")

    def _finalize_failed_save(
        self,
        *,
        message_title: str,
        message_text: str,
        pending_save=None,
        pending_save_committed: bool = False,
    ) -> None:
        rollback_pending_save = getattr(
            self.services.config_service,
            "rollback_pending_save",
            None,
        )
        if callable(rollback_pending_save) and not pending_save_committed:
            rollback_pending_save(pending_save)

        if pending_save_committed:
            config_id = getattr(pending_save, "config_id", self._current_config_id)
            reloaded = False
            if isinstance(config_id, int) and not isinstance(config_id, bool):
                try:
                    self._reload_authoritative_config_state(config_id)
                    reloaded = True
                except Exception:
                    reloaded = False
            if not reloaded:
                self._reconcile_golden_bindings_after_save_failure(
                    pending_save,
                    pending_save_committed=True,
                )
        else:
            self._reconcile_golden_bindings_after_save_failure(
                pending_save,
                pending_save_committed=False,
            )

        QMessageBox.warning(self, message_title, message_text)

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
            "选择黄金样本 WAV",
            "",
            "WAV 文件 (*.wav)",
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
        for dialog in self._channel_section_dialogs.values():
            dialog.hide()
            dialog.deleteLater()
        self._channel_section_dialogs = {}

        # Remove previous tab buttons and stretch
        while self._channel_tab_bar_lay.count():
            item = self._channel_tab_bar_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._channel_tab_buttons = []
        # Remove stack widgets
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
            btn = QPushButton(f"输入{ch_idx + 1}（通道 {ch_idx + 1}）")
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

    def _register_channel_section_card(
        self,
        *,
        container: QWidget,
        launcher_layout: QGridLayout,
        launcher_index: int,
        section: CollapsibleGroupBox,
        title: str,
        preferred_size: tuple[int, int] | None = None,
    ) -> None:
        section_id = section.section_id()
        dialog = _ChannelSectionCardDialog(
            title,
            section,
            container,
            preferred_size=preferred_size,
        )
        dialog.hide()
        channel_index = self._channel_container_index(container)
        if channel_index is not None:
            self._channel_section_dialogs[(channel_index, section_id)] = dialog

        button = QPushButton(title)
        button.setObjectName(f"open_{section_id}_section")
        button.clicked.connect(
            lambda _checked=False, current=dialog: self._show_config_dialog(current)
        )
        row, column = divmod(launcher_index, 3)
        launcher_layout.addWidget(button, row, column)

    def _hide_top_level_config_dialogs(self) -> None:
        for dialog in self._top_level_config_dialogs.values():
            dialog.hide()

    def _hide_other_config_dialogs(self, active_dialog: QDialog) -> None:
        for dialog in (
            *self._top_level_config_dialogs.values(),
            *self._channel_section_dialogs.values(),
        ):
            if dialog is not active_dialog:
                dialog.hide()

    def _show_config_dialog(self, dialog: _ConfigSectionCardDialog) -> None:
        self._hide_other_config_dialogs(dialog)
        dialog.show_card()

    def _register_top_level_config_card(
        self,
        *,
        launcher_layout: QGridLayout,
        launcher_index: int,
        section_id: str,
        title: str,
        content: QWidget,
        default_position_anchor: QWidget,
        button_object_name: str,
        dialog_object_name: str,
        preferred_size: tuple[int, int] | None = None,
    ) -> None:
        dialog = _ConfigSectionCardDialog(
            title,
            content,
            self,
            object_name=dialog_object_name,
            close_button_object_name="TopLevelConfigCardCloseButton",
            default_position_anchor=default_position_anchor,
            confirm_button_object_name="TopLevelConfigCardConfirmButton",
            preferred_size=preferred_size,
        )
        dialog.hide()
        self._top_level_config_dialogs[section_id] = dialog

        button = QPushButton(title)
        button.setObjectName(button_object_name)
        button.clicked.connect(
            lambda _checked=False, current=dialog: self._show_config_dialog(current)
        )
        row, column = divmod(launcher_index, 3)
        launcher_layout.addWidget(button, row, column)

    def _build_channel_tab(self, ch_index: int, data: dict | None) -> QWidget:
        container = QWidget()
        container.setProperty("channel_index", ch_index)
        golden_binding = self._normalize_golden_binding(data)
        self._set_channel_binding(container, golden_binding)
        self._set_committed_channel_binding(container, golden_binding)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        launcher_panel = QWidget()
        launcher_panel.setObjectName("channel_section_launcher_panel")
        launcher_layout = QGridLayout(launcher_panel)
        launcher_layout.setContentsMargins(0, 0, 0, 0)
        launcher_layout.setHorizontalSpacing(8)
        launcher_layout.setVerticalSpacing(8)
        lay.addWidget(launcher_panel)
        launcher_index = 0

        # STFT section
        stft = CollapsibleGroupBox("STFT 参数", section_id="stft")
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

        color_scale_enabled = QCheckBox("固定色阶")
        color_scale_enabled.setObjectName("stft_color_scale_enabled")
        color_scale_enabled.setChecked(
            bool(data.get("stft_color_scale_enabled", False)) if data else False
        )
        sf_lay.addRow(color_scale_enabled)

        color_scale_min = _NoWheelSpinBox()
        color_scale_min.setObjectName("stft_color_scale_min_db")
        color_scale_min.setRange(-300.0, 300.0)
        color_scale_min.setDecimals(1)
        color_scale_min.setSuffix(" dB")
        color_scale_min.setValue(
            data.get("stft_color_scale_min_db", -120.0) if data else -120.0
        )
        sf_lay.addRow("色阶下限", color_scale_min)

        color_scale_max = _NoWheelSpinBox()
        color_scale_max.setObjectName("stft_color_scale_max_db")
        color_scale_max.setRange(-300.0, 300.0)
        color_scale_max.setDecimals(1)
        color_scale_max.setSuffix(" dB")
        color_scale_max.setValue(
            data.get("stft_color_scale_max_db", 0.0) if data else 0.0
        )
        sf_lay.addRow("色阶上限", color_scale_max)

        color_scale_min.setEnabled(color_scale_enabled.isChecked())
        color_scale_max.setEnabled(color_scale_enabled.isChecked())
        color_scale_enabled.toggled.connect(color_scale_min.setEnabled)
        color_scale_enabled.toggled.connect(color_scale_max.setEnabled)

        stft.add_widget(stft_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=stft,
            title="STFT 参数",
        )
        launcher_index += 1

        # STFT score section
        stft_score = CollapsibleGroupBox("STFT 评分", section_id="stft_score")
        stft_score_form = QWidget()
        ss_lay = QFormLayout(stft_score_form)
        ss_lay.setContentsMargins(0, 0, 0, 0)
        ss_lay.setHorizontalSpacing(10)
        ss_lay.setVerticalSpacing(6)

        score_enabled = QCheckBox("启用评分")
        score_enabled.setObjectName("stft_score_enabled")
        score_enabled.setChecked(
            bool(data.get("stft_score_enabled", True)) if data else True
        )
        ss_lay.addRow(score_enabled)

        score_summary = _NoWheelComboBox()
        score_summary.setObjectName("stft_score_summary_mode")
        score_summary.addItem("10%分位数", "percentile")
        score_summary.addItem("平均分", "mean")
        score_summary.addItem("最低分", "min")
        val = (
            data.get("stft_score_summary_mode", "percentile")
            if data
            else "percentile"
        )
        idx = score_summary.findData(val)
        score_summary.setCurrentIndex(idx if idx >= 0 else 0)
        ss_lay.addRow("汇总方式", score_summary)

        score_percentile = _NoWheelSpinBox()
        score_percentile.setObjectName("stft_score_percentile")
        score_percentile.setRange(0.1, 100.0)
        score_percentile.setDecimals(1)
        score_percentile.setValue(
            data.get("stft_score_percentile", 10.0) if data else 10.0
        )
        ss_lay.addRow("分位数", score_percentile)

        score_loudness_reference = _NoWheelSpinBox()
        score_loudness_reference.setObjectName("stft_score_loudness_reference_sone")
        score_loudness_reference.setRange(0.001, 999999.0)
        score_loudness_reference.setDecimals(3)
        score_loudness_reference.setValue(
            data.get("stft_score_loudness_reference_sone", 10.0)
            if data
            else 10.0
        )
        ss_lay.addRow("响度参考值", score_loudness_reference)

        score_modulation_reference = _NoWheelSpinBox()
        score_modulation_reference.setObjectName("stft_score_modulation_reference")
        score_modulation_reference.setRange(0.001, 10.0)
        score_modulation_reference.setDecimals(3)
        score_modulation_reference.setValue(
            data.get("stft_score_modulation_reference", 0.20) if data else 0.20
        )
        ss_lay.addRow("调制参考值", score_modulation_reference)

        score_harmonic_reference = _NoWheelSpinBox()
        score_harmonic_reference.setObjectName(
            "stft_score_harmonic_ratio_reference"
        )
        score_harmonic_reference.setRange(0.001, 1.0)
        score_harmonic_reference.setDecimals(3)
        score_harmonic_reference.setValue(
            data.get("stft_score_harmonic_ratio_reference", 0.25)
            if data
            else 0.25
        )
        ss_lay.addRow("谐波占比参考值", score_harmonic_reference)

        score_frequency_reference = _NoWheelSpinBox()
        score_frequency_reference.setObjectName(
            "stft_score_frequency_instability_reference"
        )
        score_frequency_reference.setRange(0.001, 10.0)
        score_frequency_reference.setDecimals(3)
        score_frequency_reference.setValue(
            data.get("stft_score_frequency_instability_reference", 0.05)
            if data
            else 0.05
        )
        ss_lay.addRow("频率不稳定参考值", score_frequency_reference)

        score_harmonic_start = _NoWheelSpinBoxInt()
        score_harmonic_start.setObjectName("stft_score_harmonic_start_order")
        score_harmonic_start.setRange(2, 10000)
        score_harmonic_start.setValue(
            data.get("stft_score_harmonic_start_order", 2) if data else 2
        )
        ss_lay.addRow("谐波起始阶次", score_harmonic_start)

        score_harmonic_end = _NoWheelSpinBoxInt()
        score_harmonic_end.setObjectName("stft_score_harmonic_end_order")
        score_harmonic_end.setRange(2, 10000)
        score_harmonic_end.setValue(
            data.get("stft_score_harmonic_end_order", 100) if data else 100
        )
        ss_lay.addRow("谐波结束阶次", score_harmonic_end)

        score_controls = (
            score_summary,
            score_percentile,
            score_loudness_reference,
            score_modulation_reference,
            score_harmonic_reference,
            score_frequency_reference,
            score_harmonic_start,
            score_harmonic_end,
        )

        def set_score_controls_enabled(checked: bool) -> None:
            for control in score_controls:
                control.setEnabled(checked)

        set_score_controls_enabled(score_enabled.isChecked())
        score_enabled.toggled.connect(set_score_controls_enabled)

        stft_score.add_widget(stft_score_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=stft_score,
            title="STFT 评分",
        )
        launcher_index += 1

        # Sound pressure section
        spl = CollapsibleGroupBox("声压级参数", section_id="spl")
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

        sp_lay.addRow(
            "声压级",
            _create_limit_row(
                data=data,
                lower_enabled_key="spl_lower_enabled",
                lower_value_key="spl_lower_limit",
                upper_enabled_key="spl_upper_enabled",
                upper_value_key="spl_upper_limit",
                minimum=-200.0,
                maximum=200.0,
                decimals=1,
                suffix=" dB",
            ),
        )
        sp_lay.addRow(
            "平均声压级",
            _create_limit_row(
                data=data,
                lower_enabled_key="spl_average_lower_enabled",
                lower_value_key="spl_average_lower_limit",
                upper_enabled_key="spl_average_upper_enabled",
                upper_value_key="spl_average_upper_limit",
                minimum=-200.0,
                maximum=200.0,
                decimals=1,
                suffix=" dB",
            ),
        )

        spl.add_widget(spl_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=spl,
            title="声压级参数",
        )
        launcher_index += 1

        loudness = CollapsibleGroupBox("响度参数", section_id="loudness")
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

        loud_lay.addRow(
            "响度",
            _create_limit_row(
                data=data,
                lower_enabled_key="loudness_lower_enabled",
                lower_value_key="loudness_lower_limit",
                upper_enabled_key="loudness_upper_enabled",
                upper_value_key="loudness_upper_limit",
                minimum=0.0,
                maximum=999999.0,
                decimals=3,
            ),
        )
        loud_lay.addRow(
            "平均响度",
            _create_limit_row(
                data=data,
                lower_enabled_key="loudness_average_lower_enabled",
                lower_value_key="loudness_average_lower_limit",
                upper_enabled_key="loudness_average_upper_enabled",
                upper_value_key="loudness_average_upper_limit",
                minimum=0.0,
                maximum=999999.0,
                decimals=3,
            ),
        )

        loudness.add_widget(loudness_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=loudness,
            title="响度参数",
        )
        launcher_index += 1

        golden_section = CollapsibleGroupBox(
            "黄金样本",
            section_id="golden_sample",
        )
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

        btn_import = QPushButton("导入 WAV...")
        btn_import.setObjectName("golden_sample_import_button")
        btn_import.clicked.connect(lambda _checked=False, idx=ch_index: self._on_import_golden_sample(idx))
        golden_buttons_layout.addWidget(btn_import)

        btn_replace = QPushButton("替换 WAV...")
        btn_replace.setObjectName("golden_sample_replace_button")
        btn_replace.clicked.connect(lambda _checked=False, idx=ch_index: self._on_import_golden_sample(idx))
        golden_buttons_layout.addWidget(btn_replace)

        btn_clear = QPushButton("清除")
        btn_clear.setObjectName("golden_sample_clear_button")
        btn_clear.clicked.connect(lambda _checked=False, current=container: self._on_clear_golden_binding(current))
        golden_buttons_layout.addWidget(btn_clear)
        golden_buttons_layout.addStretch(1)

        golden_layout.addWidget(golden_buttons)
        golden_section.add_widget(golden_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=golden_section,
            title="黄金样本",
        )
        launcher_index += 1

        # FFT section
        fft_section = CollapsibleGroupBox("FFT 参数", section_id="fft")
        fft_widget = QWidget()
        fft_layout = QVBoxLayout(fft_widget)
        fft_layout.setContentsMargins(0, 0, 0, 0)
        fft_layout.setSpacing(6)

        fft_controls = QWidget()
        fft_controls_form = QFormLayout(fft_controls)
        fft_controls_form.setContentsMargins(0, 0, 0, 0)
        fft_controls_form.setHorizontalSpacing(10)
        fft_controls_form.setVerticalSpacing(6)

        fft_resolution = _NoWheelComboBox()
        for value in [256, 512, 1024, 2048, 4096, 8192]:
            fft_resolution.addItem(str(value), value)
        fft_resolution.setObjectName("fft_nfft")
        fft_resolution_value = data.get("fft_nfft", 4096) if data else 4096
        fft_resolution_index = fft_resolution.findData(fft_resolution_value)
        fft_resolution.setCurrentIndex(
            fft_resolution_index if fft_resolution_index >= 0 else 4
        )
        fft_controls_form.addRow("FFT 分辨率", fft_resolution)

        fft_axis = _NoWheelComboBox()
        fft_axis.setObjectName("fft_x_axis_scale")
        fft_axis.addItem("线性", "linear")
        fft_axis.addItem("对数", "log")
        fft_axis_value = data.get("fft_x_axis_scale", "linear") if data else "linear"
        fft_axis_index = fft_axis.findData(fft_axis_value)
        fft_axis.setCurrentIndex(fft_axis_index if fft_axis_index >= 0 else 0)
        fft_controls_form.addRow("FFT 横轴", fft_axis)

        fft_mode = _NoWheelComboBox()
        fft_mode.setObjectName("fft_golden_mode")
        fft_mode.addItem("关闭", "off")
        fft_mode.addItem("绝对值", "absolute")
        fft_mode.addItem("差值", "delta")
        fft_mode_index = fft_mode.findData(data.get("fft_golden_mode", "off") if data else "off")
        fft_mode.setCurrentIndex(fft_mode_index if fft_mode_index >= 0 else 0)
        fft_controls_form.addRow("黄金样本模式", fft_mode)

        fft_overlay = QCheckBox("显示黄金曲线")
        fft_overlay.setObjectName("fft_show_golden_overlay")
        fft_overlay.setChecked(bool(data.get("fft_show_golden_overlay", False)) if data else False)
        fft_controls_form.addRow("黄金曲线", fft_overlay)
        fft_layout.addWidget(fft_controls)

        fft_mode.currentIndexChanged.connect(
            lambda _index, combo=fft_mode, checkbox=fft_overlay, current=container: self._on_golden_mode_changed(combo, checkbox, current)
        )

        fft_editor = LimitTableEditor(
            object_name="fft_limit_table_editor",
            label="FFT",
        )
        fft_editor.set_limits(data.get("fft_band_limits", []) if data else [])
        fft_layout.addWidget(fft_editor)
        fft_section.add_widget(fft_widget)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=fft_section,
            title="FFT 参数",
            preferred_size=(780, 520),
        )
        launcher_index += 1

        # Octave section
        oct_section = CollapsibleGroupBox("1/3 倍频程参数", section_id="octave")
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
        oct_mode.addItem("关闭", "off")
        oct_mode.addItem("绝对值", "absolute")
        oct_mode.addItem("差值", "delta")
        oct_mode_index = oct_mode.findData(data.get("oct_golden_mode", "off") if data else "off")
        oct_mode.setCurrentIndex(oct_mode_index if oct_mode_index >= 0 else 0)
        ow_lay.addRow("黄金样本模式", oct_mode)
        oct_overlay = QCheckBox("显示黄金曲线")
        oct_overlay.setObjectName("oct_show_golden_overlay")
        oct_overlay.setChecked(bool(data.get("oct_show_golden_overlay", False)) if data else False)
        ow_lay.addRow("黄金曲线", oct_overlay)
        of_lay.addWidget(oct_w_row)

        oct_mode.currentIndexChanged.connect(
            lambda _index, combo=oct_mode, checkbox=oct_overlay, current=container: self._on_golden_mode_changed(combo, checkbox, current)
        )

        oct_editor = LimitTableEditor(
            object_name="oct_limit_table_editor",
            label="倍频程",
        )
        oct_editor.set_limits(data.get("oct_band_limits", []) if data else [])
        of_lay.addWidget(oct_editor)
        oct_section.add_widget(oct_form)
        self._register_channel_section_card(
            container=container,
            launcher_layout=launcher_layout,
            launcher_index=launcher_index,
            section=oct_section,
            title="1/3 倍频程参数",
            preferred_size=(740, 500),
        )
        lay.addStretch(1)

        self._apply_golden_mode_state(fft_mode, fft_overlay)
        self._apply_golden_mode_state(oct_mode, oct_overlay)
        self._refresh_channel_golden_section(container)

        return container

    @contextmanager
    def _suspend_ai_binding_dirty_updates(self):
        previous = self._suspend_ai_binding_dirty_tracking
        self._suspend_ai_binding_dirty_tracking = True
        try:
            yield
        finally:
            self._suspend_ai_binding_dirty_tracking = previous

    def _mark_ai_binding_dirty(self, *_args):
        if self._suspend_ai_binding_dirty_tracking:
            return
        self._ai_binding_dirty = (
            self._capture_ai_binding_editor_state() != self._loaded_ai_binding_state
        )

    @staticmethod
    def _normalize_ai_binding(binding: dict | None) -> dict:
        binding = binding or {}
        toolchain_id = binding.get("toolchain_id")
        timeout_ms = binding.get("timeout_ms")
        if toolchain_id == "":
            toolchain_id = None
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            timeout_ms = None
        return {
            "enabled": bool(binding.get("enabled", False)),
            "toolchain_id": toolchain_id,
            "timeout_ms": timeout_ms,
        }

    def _capture_ai_binding_editor_state(self) -> dict:
        return self._normalize_ai_binding(self._collect_ai_binding())

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

    def _refresh_ai_toolchains_preserving_local_state(self):
        current_toolchain_id = self.combo_ai_toolchain.currentData()
        with self._suspend_ai_binding_dirty_updates():
            self._refresh_ai_toolchains(current_toolchain_id)
            idx = self.combo_ai_toolchain.findData(current_toolchain_id)
            self.combo_ai_toolchain.setCurrentIndex(idx if idx >= 0 else 0)
        self._mark_ai_binding_dirty()

    def _populate_ai_binding(self, config_id: int):
        """Load AI binding for config_id into the UI card."""
        binding = self._normalize_ai_binding(
            self.services.ai_service.resolve_binding(config_id)
        )
        bound_tc_id = binding["toolchain_id"]
        with self._suspend_ai_binding_dirty_updates():
            self._refresh_ai_toolchains(bound_tc_id)
            self.chk_ai_enabled.setChecked(binding["enabled"])
            idx = self.combo_ai_toolchain.findData(binding["toolchain_id"])
            self.combo_ai_toolchain.setCurrentIndex(idx if idx >= 0 else 0)
            self.spin_ai_timeout.setValue(binding["timeout_ms"] or 0)
        self._loaded_ai_binding_state = binding
        self._ai_binding_dirty = False

    def _on_ai_toolchains_changed(self):
        if self._current_config_id is None:
            return
        if self._ai_binding_dirty:
            self._refresh_ai_toolchains_preserving_local_state()
            return
        self._populate_ai_binding(self._current_config_id)

    def _validate_ai_binding_selection(self, ai_data: dict) -> None:
        if not ai_data["enabled"]:
            return
        tc_id = ai_data.get("toolchain_id")
        if tc_id is None:
            raise ValueError("请选择 AI 工具链")
        valid_ids = {
            tc["toolchain_id"]
            for tc in self.services.ai_service.list_enabled_toolchains()
        }
        if tc_id not in valid_ids:
            raise ValueError("AI 工具链不可用，请重新选择或清空绑定")

    def _populate_hardware_card(self, data: dict):
        """Load saved device/trigger/channel selections into hardware card widgets."""
        # Block input device signal to prevent _on_input_device_changed during population
        self.combo_input_dev.blockSignals(True)

        self._populate_devices()
        self._refresh_plc_selector(data.get("plc_id"))

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

        gain_db = data.get("monitor_gain_db", 0.0)
        if not isinstance(gain_db, (int, float)) or isinstance(gain_db, bool):
            gain_db = 0.0
        self.spin_monitor_gain_db.setValue(float(gain_db))

        saved_monitor_channel = data.get("monitor_channel_index")
        self._monitor_channel_explicit = False
        self._rebuild_monitor_channel_options(preferred=saved_monitor_channel)
        self._monitor_channel_explicit = self._is_valid_monitor_channel(
            saved_monitor_channel,
            self._current_input_channel_count(),
        )
        self._rebuild_vibration_view_channel_options(
            preferred=data.get("vibration_view_channel_index")
        )
        self._populate_current_meter(data.get("current_meter"))

    def _populate_current_meter(self, current_meter: dict | None) -> None:
        current_meter = normalize_current_meter_config(current_meter)
        self.chk_current_meter_enabled.setChecked(bool(current_meter.get("enabled")))
        self.ed_current_meter_host.setText(str(current_meter.get("host") or ""))
        self.spin_current_meter_port.setValue(int(current_meter.get("port")))
        self.spin_current_meter_unit_id.setValue(int(current_meter.get("unit_id")))
        self._set_combo_current_data(
            self.combo_current_meter_register_type, current_meter["register_type"]
        )
        self.spin_current_meter_register_address.setValue(
            int(current_meter["register_address"])
        )
        self._set_combo_current_data(
            self.combo_current_meter_data_type, current_meter["data_type"]
        )
        self._set_combo_current_data(
            self.combo_current_meter_byte_order, current_meter["byte_order"]
        )
        self._set_combo_current_data(
            self.combo_current_meter_word_order, current_meter["word_order"]
        )
        self.spin_current_meter_scale.setValue(
            float(current_meter["scale"])
        )
        self.spin_current_meter_offset.setValue(float(current_meter["offset"]))
        self.spin_current_meter_poll_interval_ms.setValue(
            int(current_meter["poll_interval_ms"])
        )
        self.spin_current_meter_timeout_ms.setValue(
            int(current_meter["timeout_ms"])
        )
        self._refresh_current_meter_offset_label()

    def _set_current_meter_offset_label(self, current_meter: dict | None) -> None:
        unit = current_meter_display_unit(current_meter)
        label = "电流表偏移(mA)" if unit == "mA" else "电流表偏移"
        self._set_form_row_label(self.spin_current_meter_offset, label)

    def _refresh_current_meter_offset_label(self, *_args) -> None:
        current_meter = self._build_current_meter_form_config()
        self._set_current_meter_offset_label(current_meter)

    def _build_current_meter_form_config(self) -> dict:
        data_type = self.combo_current_meter_data_type.currentData() or "uint16"
        return {
            "register_type": self.combo_current_meter_register_type.currentData() or "input",
            "data_type": data_type,
            "byte_order": self.combo_current_meter_byte_order.currentData() or "big",
            "word_order": self.combo_current_meter_word_order.currentData() or "big",
            "scale": float(self.spin_current_meter_scale.value()),
            "register_count": 1 if data_type in {"uint16", "int16"} else 2,
        }

    def _set_form_row_label(self, widget: QWidget, text: str) -> None:
        current = widget
        while current is not None:
            layout = current.parentWidget().layout() if current.parentWidget() else None
            if isinstance(layout, QFormLayout):
                row, _ = layout.getWidgetPosition(current)
                if row >= 0:
                    item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
                    label = item.widget() if item else None
                    if isinstance(label, QLabel):
                        label.setText(text)
                        return
                if row < 0:
                    current = current.parentWidget()
                    continue
                return
            current = current.parentWidget()

    @staticmethod
    def _set_combo_current_data(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

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

        hostapi_display = "未知" if hostapi == "N/A" else hostapi
        label = f"{name} ({hostapi_display}) (设备不可用)"
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
            self._rebuild_monitor_channel_options(preferred=None)
            self._rebuild_vibration_view_channel_options(preferred=None)
            return

        max_ch = dev_info.get("max_input_channels", 0)
        for i in range(max_ch):
            cb = QCheckBox(f"输入{i + 1}")
            cb.toggled.connect(self._on_channel_selection_changed)
            row, col = divmod(i, 6)
            self._channel_check_layout.addWidget(cb, row, col)
            self._channel_checkboxes.append(cb)
        self._rebuild_monitor_channel_options(
            preferred=self.combo_monitor_channel.currentData()
        )
        self._rebuild_vibration_view_channel_options(
            preferred=self.combo_vibration_view_channel.currentData()
        )

    # ── Data collection ─────────────────────────────────────

    def _collect_channel_data(self, container: QWidget) -> dict:
        ch_index = container.property("channel_index")
        ch = {"channel_index": ch_index}

        combo_nfft = container.findChild(QComboBox, "stft_nfft")
        ch["stft_nfft"] = combo_nfft.currentData() if combo_nfft else 1024

        combo_fft_nfft = container.findChild(QComboBox, "fft_nfft")
        ch["fft_nfft"] = combo_fft_nfft.currentData() if combo_fft_nfft else 4096

        fft_axis = container.findChild(QComboBox, "fft_x_axis_scale")
        ch["fft_x_axis_scale"] = fft_axis.currentData() if fft_axis else "linear"

        combo_overlap = container.findChild(QComboBox, "stft_overlap")
        ch["stft_overlap"] = combo_overlap.currentData() if combo_overlap else 0.5

        stft_color_scale_enabled = container.findChild(
            QCheckBox, "stft_color_scale_enabled"
        )
        ch["stft_color_scale_enabled"] = (
            stft_color_scale_enabled.isChecked()
            if stft_color_scale_enabled
            else False
        )

        stft_color_scale_min = container.findChild(
            QDoubleSpinBox, "stft_color_scale_min_db"
        )
        ch["stft_color_scale_min_db"] = (
            stft_color_scale_min.value()
            if stft_color_scale_min
            else -120.0
        )

        stft_color_scale_max = container.findChild(
            QDoubleSpinBox, "stft_color_scale_max_db"
        )
        ch["stft_color_scale_max_db"] = (
            stft_color_scale_max.value()
            if stft_color_scale_max
            else 0.0
        )

        stft_score_enabled = container.findChild(QCheckBox, "stft_score_enabled")
        ch["stft_score_enabled"] = (
            stft_score_enabled.isChecked()
            if stft_score_enabled
            else True
        )

        stft_score_summary = container.findChild(QComboBox, "stft_score_summary_mode")
        ch["stft_score_summary_mode"] = (
            stft_score_summary.currentData()
            if stft_score_summary
            else "percentile"
        )

        stft_score_percentile = container.findChild(
            QDoubleSpinBox, "stft_score_percentile"
        )
        ch["stft_score_percentile"] = (
            stft_score_percentile.value()
            if stft_score_percentile
            else 10.0
        )

        stft_score_loudness_reference = container.findChild(
            QDoubleSpinBox, "stft_score_loudness_reference_sone"
        )
        ch["stft_score_loudness_reference_sone"] = (
            stft_score_loudness_reference.value()
            if stft_score_loudness_reference
            else 10.0
        )

        stft_score_modulation_reference = container.findChild(
            QDoubleSpinBox, "stft_score_modulation_reference"
        )
        ch["stft_score_modulation_reference"] = (
            stft_score_modulation_reference.value()
            if stft_score_modulation_reference
            else 0.20
        )

        stft_score_harmonic_reference = container.findChild(
            QDoubleSpinBox, "stft_score_harmonic_ratio_reference"
        )
        ch["stft_score_harmonic_ratio_reference"] = (
            stft_score_harmonic_reference.value()
            if stft_score_harmonic_reference
            else 0.25
        )

        stft_score_frequency_reference = container.findChild(
            QDoubleSpinBox, "stft_score_frequency_instability_reference"
        )
        ch["stft_score_frequency_instability_reference"] = (
            stft_score_frequency_reference.value()
            if stft_score_frequency_reference
            else 0.05
        )

        stft_score_harmonic_start = container.findChild(
            QSpinBox, "stft_score_harmonic_start_order"
        )
        ch["stft_score_harmonic_start_order"] = (
            stft_score_harmonic_start.value()
            if stft_score_harmonic_start
            else 2
        )

        stft_score_harmonic_end = container.findChild(
            QSpinBox, "stft_score_harmonic_end_order"
        )
        ch["stft_score_harmonic_end_order"] = (
            stft_score_harmonic_end.value()
            if stft_score_harmonic_end
            else 100
        )

        combo_spl_w = container.findChild(QComboBox, "spl_weighting")
        ch["spl_weighting"] = combo_spl_w.currentText() if combo_spl_w else "A"

        spin_tc = container.findChild(QDoubleSpinBox, "spl_time_constant")
        ch["spl_time_constant"] = spin_tc.value() if spin_tc else 0.125

        self._collect_limit_fields(
            container,
            ch,
            enabled_fields=(
                "spl_lower_enabled",
                "spl_upper_enabled",
                "spl_average_lower_enabled",
                "spl_average_upper_enabled",
            ),
            value_fields=(
                "spl_lower_limit",
                "spl_upper_limit",
                "spl_average_lower_limit",
                "spl_average_upper_limit",
            ),
        )

        combo_loudness_unit = container.findChild(QComboBox, "loudness_unit")
        ch["loudness_unit"] = (
            combo_loudness_unit.currentText()
            if combo_loudness_unit
            else "sone"
        )

        self._collect_limit_fields(
            container,
            ch,
            enabled_fields=(
                "loudness_lower_enabled",
                "loudness_upper_enabled",
                "loudness_average_lower_enabled",
                "loudness_average_upper_enabled",
            ),
            value_fields=(
                "loudness_lower_limit",
                "loudness_upper_limit",
                "loudness_average_lower_limit",
                "loudness_average_upper_limit",
            ),
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
        fft_editor = container.findChild(LimitTableEditor, "fft_limit_table_editor")
        oct_editor = container.findChild(LimitTableEditor, "oct_limit_table_editor")
        ch["fft_band_limits"] = fft_editor.limits() if fft_editor else []
        ch["oct_band_limits"] = oct_editor.limits() if oct_editor else []
        ch["ai_config"] = {}
        return ch

    def _collect_limit_fields(
        self,
        container: QWidget,
        channel: dict,
        *,
        enabled_fields: tuple[str, ...],
        value_fields: tuple[str, ...],
    ) -> None:
        for field in enabled_fields:
            checkbox = container.findChild(QCheckBox, field)
            channel[field] = checkbox.isChecked() if checkbox else bool(_LIMIT_DEFAULTS[field])
        for field in value_fields:
            spinbox = container.findChild(QDoubleSpinBox, field)
            channel[field] = spinbox.value() if spinbox else float(_LIMIT_DEFAULTS[field])

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
            "allow_incomplete_recording_analysis": (
                self.chk_allow_incomplete_recording_analysis.isChecked()
            ),
            "scan_triggers_test": False,
            "plc_id": self.combo_plc.currentData(),
            "input_device_name": input_dev.get("name") if isinstance(input_dev, dict) else None,
            "input_device_hostapi": input_dev.get("hostapi") if isinstance(input_dev, dict) else None,
            "input_device_samplerate": input_dev.get("default_samplerate") if isinstance(input_dev, dict) else None,
            "output_device_name": output_dev.get("name") if isinstance(output_dev, dict) else None,
            "output_device_hostapi": output_dev.get("hostapi") if isinstance(output_dev, dict) else None,
            "output_device_samplerate": output_dev.get("default_samplerate") if isinstance(output_dev, dict) else None,
            "selected_channels": selected_channels,
            "monitor_channel_index": self.combo_monitor_channel.currentData()
            if self.combo_monitor_channel.count() > 0
            else None,
            "vibration_view_channel_index": self.combo_vibration_view_channel.currentData(),
            "monitor_gain_db": float(self.spin_monitor_gain_db.value()),
            "current_meter": {
                "enabled": self.chk_current_meter_enabled.isChecked(),
                "host": self.ed_current_meter_host.text().strip(),
                "port": self.spin_current_meter_port.value(),
                "unit_id": self.spin_current_meter_unit_id.value(),
                "register_type": self.combo_current_meter_register_type.currentData(),
                "register_address": self.spin_current_meter_register_address.value(),
                "register_count": (
                    1
                    if self.combo_current_meter_data_type.currentData()
                    in {"uint16", "int16"}
                    else 2
                ),
                "data_type": self.combo_current_meter_data_type.currentData(),
                "byte_order": self.combo_current_meter_byte_order.currentData(),
                "word_order": self.combo_current_meter_word_order.currentData(),
                "scale": self.spin_current_meter_scale.value(),
                "offset": self.spin_current_meter_offset.value(),
                "poll_interval_ms": self.spin_current_meter_poll_interval_ms.value(),
                "timeout_ms": self.spin_current_meter_timeout_ms.value(),
            },
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
        }

    # ── Populate UI from data ───────────────────────────────

    def _populate_detail(self, data: dict):
        self._hide_top_level_config_dialogs()
        self._discard_all_staged_bindings()
        self.ed_name.setText(data["config_name"])
        self.spin_record.setValue(data["record_duration"])
        self.spin_analysis.setValue(data["analysis_duration"])
        self.spin_analysis_window_offset.setValue(
            data.get("analysis_window_offset_ms", 0)
        )
        self.chk_allow_incomplete_recording_analysis.setChecked(
            bool(data.get("allow_incomplete_recording_analysis", False))
        )
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
        self._mark_editor_snapshot_clean()

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
        self.btn_export.setEnabled(has_items)
        self.btn_save.setEnabled(has_items)

        if has_items:
            self.config_list.setCurrentRow(0)
        else:
            self._current_config_id = None
            self._loaded_config_editor_snapshot = None
            self._hide_top_level_config_dialogs()

    def _select_config_by_id(self, config_id: int, *, suppress_reload: bool = False):
        previous_skip_reload = self._skip_reload
        if suppress_reload:
            self._skip_reload = True
        try:
            for i in range(self.config_list.count()):
                item = self.config_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == config_id:
                    self.config_list.setCurrentRow(i)
                    return
            if self.config_list.count() > 0:
                self.config_list.setCurrentRow(0)
        finally:
            if suppress_reload:
                self._skip_reload = previous_skip_reload

    def _restore_current_config_selection(self) -> None:
        if self._current_config_id is None:
            return
        self._restoring_config_selection = True
        try:
            self._select_config_by_id(self._current_config_id, suppress_reload=True)
        finally:
            self._restoring_config_selection = False

    def _load_config_into_editor(self, config_id: int) -> None:
        self._current_config_id = config_id
        data = self.services.config_service.load_config(config_id)
        self.empty_label.setVisible(False)
        self.detail.setVisible(True)
        self._populate_detail(data)

    def _on_item_changed(self, current: QListWidgetItem | None, _prev):
        if current is None:
            return
        if self._skip_reload:
            return
        if self._restoring_config_selection:
            return
        config_id = current.data(Qt.ItemDataRole.UserRole)
        if config_id == self._current_config_id:
            return
        if not self._resolve_unsaved_changes_before("switch"):
            self._restore_current_config_selection()
            return
        try:
            self._select_config_by_id(config_id, suppress_reload=True)
            self._load_config_into_editor(config_id)
        except Exception as e:
            QMessageBox.warning(self, "加载失败", f"无法加载配置：{e}")

    def _on_new(self):
        if not self._resolve_unsaved_changes_before("new"):
            return
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
        if not self._resolve_unsaved_changes_before("delete"):
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
        self._hide_top_level_config_dialogs()
        self._refresh_list()

    def _on_export_package(self):
        current = self.config_list.currentItem()
        if current is None:
            QMessageBox.warning(self, "导出失败", "请选择要导出的配置。")
            return

        config_id = current.data(Qt.ItemDataRole.UserRole)
        config_name = current.text()
        options = config_package_dialogs.choose_export_options(self)
        if options is None:
            return
        target_path = config_package_dialogs.choose_dy_export_path(
            self,
            default_name=config_name,
        )
        if not target_path:
            return

        try:
            self.services.config_package_service.export_package(
                config_id=config_id,
                target_path=target_path,
                options=options,
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return

        QMessageBox.information(self, "导出成功", f"配置包已导出：{target_path}")

    def _on_import_package(self):
        if not self._resolve_unsaved_changes_before("import"):
            return
        source_path = config_package_dialogs.choose_dy_import_path(self)
        if not source_path:
            return

        try:
            preview = self.services.config_package_service.inspect_package(source_path)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return

        decision = config_package_dialogs.choose_import_decision(self, preview)
        if decision is None:
            return

        try:
            result = self.services.config_package_service.import_package(
                source_path,
                decision,
            )
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return

        if result.status in {"created", "overwritten"} and result.config_id is not None:
            self._refresh_list()
            self._select_config_by_id(result.config_id)

        if result.warnings:
            QMessageBox.warning(self, "导入提示", "\n".join(result.warnings))
        if result.info_message:
            QMessageBox.information(self, "导入提示", result.info_message)

    def _on_save(self):
        self._save_current_config(show_success=True)

    def _save_current_config(self, *, show_success: bool = True) -> bool:
        if self._current_config_id is None:
            return True
        data = self._collect_data()
        errors = self.services.config_service.validate(data)
        if errors:
            self._finalize_failed_save(
                message_title="验证失败",
                message_text="\n".join(errors),
            )
            return False
        pending_save = None
        pending_save_committed = False
        try:
            # Validate AI binding before saving anything
            ai_data = self._collect_ai_binding()
            try:
                self._validate_ai_binding_selection(ai_data)
            except ValueError as e:
                self._finalize_failed_save(
                    message_title="AI 配置错误",
                    message_text=str(e),
                )
                return False

            selected_channel_index = self._selected_channel_index()
            section_state = self._capture_collapsible_section_state()

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
            self._apply_loaded_config_state(saved_id, loaded)
            self._restore_collapsible_section_state(section_state)
            self._restore_selected_channel_tab(selected_channel_index)
            self.services.bus.status.emit("配置已保存")
            self._mark_editor_snapshot_clean()
            if show_success:
                QMessageBox.information(self, "保存成功", "配置保存成功！")
            return True
        except ValueError as e:
            self._skip_reload = False
            self._finalize_failed_save(
                message_title="保存失败",
                message_text=str(e),
                pending_save=pending_save,
                pending_save_committed=pending_save_committed,
            )
            return False
        except Exception as e:
            self._skip_reload = False
            self._finalize_failed_save(
                message_title="保存失败",
                message_text=str(e),
                pending_save=pending_save,
                pending_save_committed=pending_save_committed,
            )
            return False

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
