from __future__ import annotations

import os

from PyQt6.QtCore import QMetaObject, QSize, Qt
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from base.utils import asset_path

from app.services_container import FactoryControllerState, FactoryReviewPayload
from consts.style import APP_QSS
from ui.widgets.cards import Card
from ui.widgets.analysis_channel_toolbar import AnalysisChannelToolbar
from ui.modules.workorder import WorkOrderModule
from ui.modules.control import ControlModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.stft import StftModule
from ui.modules.loudness import LoudnessModule
from ui.modules.spl import SplModule
from ui.modules.octave import OctaveModule
from ui.modules.vibration import VibrationAxisModule, VibrationTrendModule
from ui.modules.result import ResultModule
from ui.modules.video import VideoModule
from ui.modules.today_statistics import TodayStatisticsModule
from ui.modules.hardware import HardwareModule
from ui.modules.data_management import DataManagementModule
from ui.modules.input_calibration import InputCalibrationModule
from ui.modules.config import ConfigModule
from ui.modules.ai_tool_management import AiToolManagementModule
from ui.modules.channel_select import ChannelSelectModule
from ui.modules.plc_settings import PlcSettingsModule
from ui.modules.login import LoginWindow
from ui.modules.change_password import ChangePasswordWindow
from ui.modules.add_user import AddUserWindow
from ui.modules.delete_user import DeleteUserWindow

ROLE_PERMISSIONS = {
    "管理员": {
        "tabs": {"测试", "数据管理", "配置", "AI工具管理", "硬件"},
        "actions": {"切换用户", "添加用户", "删除用户", "修改密码"},
    },
    "工程师": {
        "tabs": {"测试", "数据管理", "配置", "AI工具管理", "硬件"},
        "actions": {"切换用户", "修改密码"},
    },
    "操作员": {
        "tabs": {"测试"},
        "actions": {"切换用户", "修改密码"},
    },
}


class MainWindow(QMainWindow):
    _TEST_PAGE_INDEX = 0
    _DISPLAY_CHANNEL_PHASES = frozenset(
        {"running", "analyzing", "reviewing", "reanalyzing", "stopped"}
    )
    _COMMITTED_CHART_CHANNEL_PHASES = frozenset(
        {"reviewing", "reanalyzing", "stopped"}
    )

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.current_user_name = None  # current_user_name作为是否已登录的唯一判断
        self.current_user_role = None
        self._applied_user_name = None
        self._applied_user_role = None
        self.login_window = None
        self.change_password_window = None
        self.add_user_window = None
        self.delete_user_window = None
        factory_mode = getattr(self.services, "factory_mode", None)
        self._factory_state = getattr(factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(factory_mode, "review_payload", FactoryReviewPayload())
        self.display_channel_by_config_id: dict[int, int] = {}
        self._last_selected_display_channel: int | None = None
        self._review_identity_by_config_id: dict[int, tuple] = {}
        self._current_main_tab_index = self._TEST_PAGE_INDEX
        self._pending_guarded_tab_index: int | None = None
        self._reverting_main_tab_change = False

        self.setWindowTitle("智能电机检测")
        icon_path = asset_path("logo.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setStyleSheet(APP_QSS)

        # root 空白根容器
        root = QWidget()
        self.setCentralWidget(root)
        # 最外层布局out
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        # tab
        self.tab = QStackedWidget()
        outer.addWidget(self.build_topbar())
        outer.addWidget(self.tab, 1)

        self._connect_bus_signals()

        # 页面：测试/数据管理/配置/AI工具管理/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_data_page())
        self.tab.addWidget(self.build_config_page())
        self.tab.addWidget(self.build_ai_tool_management_page())
        self.tab.addWidget(self.build_hardware_page())
        self.tab.currentChanged.connect(self._on_main_tab_changed)

        self.services.bus.log.emit("[App] 启动程序")
        self._record_review_identity(self._review_payload)
        self._sync_analysis_toolbar()
        self._refresh_analysis_display_modules()
        self.set_role()

    def show_login_window(self, close_main_window=False, window_title="登录"):
        def on_login_success(username: str, role: str):
            self.current_user_name = username
            self.current_user_role = role
            self.set_role()
            self.raise_()
            self.activateWindow()

        def on_login_cancel():
            if close_main_window:
                self.close()

        self.login_window = LoginWindow(
            on_login_success=on_login_success,
            on_login_cancel=on_login_cancel,
            window_title=window_title,
        )
        self.login_window.show()

    def show_change_password_window(self):
        if not self.current_user_name:
            self.services.bus.log.emit("请先登录")
            return

        self.change_password_window = ChangePasswordWindow(self.current_user_name)
        self.change_password_window.show()

    def show_add_user_window(self):
        self.add_user_window = AddUserWindow()
        self.add_user_window.show()

    def show_delete_user_window(self):
        self.delete_user_window = DeleteUserWindow()
        self.delete_user_window.show()

    def build_topbar(self):
        topbar = QWidget()
        topbar.setObjectName("TopBar")
        lay = QHBoxLayout(topbar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        brand = QWidget()
        brand_lay = QHBoxLayout(brand)
        brand_lay.setContentsMargins(2, 0, 10, 0)
        brand_lay.setSpacing(8)

        brand_icon = QLabel()
        brand_pixmap = QPixmap(28, 28)
        brand_pixmap.fill(Qt.GlobalColor.transparent)
        svg_path = asset_path("logo.svg")
        if os.path.exists(svg_path):
            renderer = QSvgRenderer(svg_path)
            painter = QPainter(brand_pixmap)
            renderer.render(painter)
            painter.end()
            brand_icon.setPixmap(brand_pixmap)
        brand_icon.setFixedSize(QSize(28, 28))
        brand_lay.addWidget(brand_icon)

        brand_text = QLabel("智能电机检测")
        brand_text.setStyleSheet("font-size: 14px; font-weight: 700; color: #0f172a; letter-spacing: 1px;")
        brand_lay.addWidget(brand_text)
        lay.addWidget(brand)

        sep = QWidget()
        sep.setFixedWidth(1)
        sep.setFixedHeight(22)
        sep.setStyleSheet("background: #cbd5e1;")
        lay.addSpacing(4)
        lay.addWidget(sep)
        lay.addSpacing(8)

        group = QButtonGroup(self)
        group.setExclusive(True)
        self.top_button_dict = {}

        # 创建一个顶部 tab 按钮，并设置选中第0个tab_names
        def add_tab(text, checked: bool = False):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(checked)
            b.setObjectName("TopTab")
            group.addButton(b)
            lay.addWidget(b)
            return b
        self.tab_names = ["测试", "数据管理", "配置", "AI工具管理", "硬件"]
        for i, name in enumerate(self.tab_names):
            b = add_tab(name, checked=(i == 0))
            self.top_button_dict[name] = b
            group.setId(b, i)
        group.idClicked.connect(self._on_topbar_tab_clicked)

        self.user_btn = QToolButton()
        self.user_btn.setText("用户")
        self.user_btn.setObjectName("TopTab")
        self.user_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        user_menu = QMenu(self.user_btn)
        self.action_switch_user = QAction("切换用户", self)
        self.action_add_user = QAction("添加用户", self)
        self.action_delete_user = QAction("删除用户", self)
        self.action_change_pwd = QAction("修改密码", self)

        self.action_switch_user.triggered.connect(
            lambda: self.show_login_window(close_main_window=False, window_title="切换用户")
        )
        self.action_add_user.triggered.connect(self.show_add_user_window)
        self.action_delete_user.triggered.connect(self.show_delete_user_window)
        self.action_change_pwd.triggered.connect(self.show_change_password_window)

        user_menu.addAction(self.action_switch_user)
        user_menu.addAction(self.action_add_user)
        user_menu.addAction(self.action_delete_user)
        user_menu.addAction(self.action_change_pwd)
        self.user_btn.setMenu(user_menu)
        lay.addWidget(self.user_btn)

        lay.addStretch(1)
        return topbar

    def set_role(self):
        role_applied = self._ensure_role_compatible_main_tab(self.current_user_role)
        if not role_applied:
            self.current_user_name = self._applied_user_name
            self.current_user_role = self._applied_user_role
            self._ensure_role_compatible_main_tab(self.current_user_role)

        role = self.current_user_role
        perms = self._role_permissions(role)
        allowed_tabs = perms["tabs"]
        allowed_actions = perms["actions"]
        for name, btn in getattr(self, "top_button_dict", {}).items():
            btn.setEnabled(name in allowed_tabs)

        if hasattr(self, "action_switch_user"):
            self.action_switch_user.setEnabled("切换用户" in allowed_actions)
            self.action_add_user.setEnabled("添加用户" in allowed_actions)
            self.action_delete_user.setEnabled("删除用户" in allowed_actions)
            self.action_change_pwd.setEnabled("修改密码" in allowed_actions)

        # 禁用/启用按钮
        if hasattr(self, "control"):
            start_button = getattr(self.control, "btn_start", None)
            stop_button = getattr(self.control, "btn_stop", None)
            sync_start_state = getattr(self.control, "_sync_start_state", None)
            if callable(sync_start_state):
                sync_start_state()
            elif start_button is not None:
                start_button.setEnabled(bool(self.current_user_name))
            if start_button is not None and not self.current_user_name:
                start_button.setEnabled(False)
            if stop_button is not None:
                stop_button.setEnabled(bool(self.current_user_name))
        # if hasattr(self, "plc_settings_card"):
        #     plc_allowed = self.current_user_role in {"管理员", "工程师"}
        #     self.plc_settings_card.setEnabled(plc_allowed)
        #     self.plc_settings_card.setToolTip("" if plc_allowed else "暂无权限操作 PLC 设置")

        if hasattr(self, "tab"):
            self._sync_topbar_selection(self.tab.currentIndex())

        self._applied_user_name = self.current_user_name
        self._applied_user_role = self.current_user_role
        return role_applied

    def build_test_page(self) -> QWidget:
        page = QWidget()
        content_lay = QHBoxLayout(page)
        content_lay.setContentsMargins(0, 0, 0, 0)
        # 左侧栏 和 右侧主区域 之间的横向间距
        content_lay.setSpacing(5)

        # 左侧栏
        left = QWidget()
        left.setFixedWidth(420)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(5)

        self.workorder = WorkOrderModule(self.services)
        left_lay.addWidget(Card("工单输入", self.workorder), 1)

        self.control = ControlModule(self.services)
        left_lay.addWidget(Card("控制面板", self.control))

        self.today_statistics = TodayStatisticsModule(self.services)
        self.today_statistics_card = Card("今日数据统计", self.today_statistics)
        self.today_statistics_card.setFixedHeight(108)
        left_lay.addWidget(self.today_statistics_card)

        self.video = VideoModule(self.services)
        self.video_card = Card("视频预览", self.video)
        self.video_card.setMinimumHeight(270)
        left_lay.addWidget(self.video_card)

        content_lay.addWidget(left)

        # 右侧主区域：3x2 网格（6 个卡片同尺寸）
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)

        self.result = ResultModule(self.services)
        grid.addWidget(Card("结果摘要", self.result), 1, 0)

        self.analysis_toolbar = AnalysisChannelToolbar()
        self.analysis_toolbar.channel_changed.connect(self._on_analysis_channel_changed)
        self.analysis_toolbar.hide()
        grid.addWidget(self.analysis_toolbar, 0, 1, 1, 2)

        self.stft = StftModule(self.services)
        self.stft_card = Card("时频图", self.stft)
        self._set_stft_score_title(self.stft.current_score_title())
        self.stft.score_title_changed.connect(self._set_stft_score_title)
        grid.addWidget(self.stft_card, 1, 1)

        self.spectrum = SpectrumModule(self.services)
        self.spectrum_card = Card("频谱", self.spectrum)
        grid.addWidget(self.spectrum_card, 1, 2)

        self.loudness = LoudnessModule(self.services)
        self.vibration_axis = VibrationAxisModule(self.services)
        self.loudness_stack = QStackedWidget()
        self.loudness_stack.addWidget(self.loudness)
        self.loudness_stack.addWidget(self.vibration_axis)
        self.loudness_card = Card("响度", self.loudness_stack)
        self.loudness.title_changed.connect(self._on_loudness_title_changed)
        self.loudness_card.set_title(self.loudness.current_title())
        grid.addWidget(self.loudness_card, 2, 0)

        self.spl = SplModule(self.services)
        self.vibration_trend = VibrationTrendModule(self.services)
        self.spl_stack = QStackedWidget()
        self.spl_stack.addWidget(self.spl)
        self.spl_stack.addWidget(self.vibration_trend)
        self.spl_card = Card("声压级（dB）", self.spl_stack)
        grid.addWidget(self.spl_card, 2, 1)

        self.octave = OctaveModule(self.services)
        self.octave_card = Card("倍频程（dB）", self.octave)
        grid.addWidget(self.octave_card, 2, 2)

        self._attach_analysis_channel_providers()

        main_lay.addLayout(grid, 1)
        content_lay.addWidget(main, 1)
        return page

    def build_placeholder_page(self, title: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        body = QLabel(f"{title} 页面（待实现）")
        body.setObjectName("Muted")
        body.setStyleSheet("background: white; border: 1px solid #e5e7eb; border-radius: 10px; padding: 18px;")
        lay.addWidget(body)
        lay.addStretch(1)
        return page

    def build_data_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.data_management_module = DataManagementModule(self.services)
        lay.addWidget(Card("数据管理", self.data_management_module), 1)
        return page

    def build_config_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.config_module = ConfigModule(self.services)
        lay.addWidget(Card("测试配置管理", self.config_module), 1)
        return page

    def build_ai_tool_management_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.ai_tool_management_module = AiToolManagementModule(self.services)
        lay.addWidget(Card("AI工具管理", self.ai_tool_management_module), 1)
        return page

    def build_hardware_page(self) -> QWidget:
        """
        左侧：设备选择/保存
        右侧：输入校准（后续在这里扩展）
        """
        page = QWidget()
        content_lay = QHBoxLayout(page)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(10)

        # 左侧栏
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(12)

        hw = HardwareModule(self.services)
        left_lay.addWidget(Card("设备选择", hw), 1)
        content_lay.addWidget(left, 7)

        # 右侧主区域：通道选择 / 输入校准 / PLC 设置作为一个整体垂直排列。
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        cal = InputCalibrationModule(self.services)
        cal_card = Card("麦克风输入校准", cal)

        channel = ChannelSelectModule(self.services)
        channel_card = Card("通道选择", channel)

        self.plc_settings_module = PlcSettingsModule(self.services)
        self.plc_settings_card = Card("PLC设置", self.plc_settings_module)

        main_lay.addWidget(channel_card, 1)
        main_lay.addWidget(cal_card, 2)
        main_lay.addWidget(self.plc_settings_card, 2)
        content_lay.addWidget(main, 3)
        return page

    def _connect_bus_signals(self) -> None:
        if hasattr(self.services.bus, "log"):
            self.services.bus.log.connect(self._show_latest_log_line)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)

    def _show_latest_log_line(self, message) -> None:
        self.statusBar().showMessage(self._latest_non_empty_log_line(message))

    @staticmethod
    def _latest_non_empty_log_line(message) -> str:
        text = message if isinstance(message, str) else ("" if message is None else str(message))
        for line in reversed(text.splitlines()):
            normalized = line.strip()
            if normalized:
                return normalized
        return ""

    def current_display_channel_index(self) -> int | None:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return None
        return self.display_channel_by_config_id.get(config_id)

    def _attach_analysis_channel_providers(self) -> None:
        for module in (
            self.result,
            self.stft,
            self.loudness,
            self.spl,
            self.spectrum,
            self.octave,
            self.vibration_axis,
            self.vibration_trend,
        ):
            module.selected_display_channel_provider = self.current_display_channel_index
        for module in (
            self.loudness,
            self.spl,
            self.spectrum,
            self.octave,
            self.vibration_axis,
            self.vibration_trend,
        ):
            module.selected_display_channel_sync = self._sync_selected_display_channel

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()
        self._sync_analysis_toolbar()

    def _on_factory_review_changed(self, review) -> None:
        if isinstance(review, FactoryReviewPayload):
            self._review_payload = review
        else:
            self._review_payload = FactoryReviewPayload()
        self._clear_display_channel_for_new_review_identity(self._review_payload)
        self._sync_analysis_toolbar()

    def _review_identity(self, review: FactoryReviewPayload) -> tuple:
        return (
            getattr(review, "displayed_config_id", None),
            getattr(review, "run_id", None),
            getattr(review, "trigger_source", None),
        )

    def _record_review_identity(self, review: FactoryReviewPayload) -> None:
        config_id = getattr(review, "displayed_config_id", None)
        if isinstance(config_id, int):
            self._review_identity_by_config_id[config_id] = self._review_identity(review)

    def _clear_display_channel_for_new_review_identity(
        self,
        review: FactoryReviewPayload,
    ) -> None:
        config_id = getattr(review, "displayed_config_id", None)
        if not isinstance(config_id, int):
            return

        identity = self._review_identity(review)
        if self._review_identity_by_config_id.get(config_id) != identity:
            self.display_channel_by_config_id.pop(config_id, None)
        self._review_identity_by_config_id[config_id] = identity

    def _on_factory_display_reset(self, _reason: str) -> None:
        config_id = self._factory_state.displayed_config_id
        if isinstance(config_id, int):
            self.display_channel_by_config_id.pop(config_id, None)
        self._review_payload = FactoryReviewPayload()
        self._sync_analysis_toolbar()
        self._apply_analysis_display_mode()

    def _on_topbar_tab_clicked(self, index: int) -> None:
        current_index = self.tab.currentIndex()
        if index == current_index:
            self._sync_topbar_selection(index)
            return
        if not self._can_activate_main_tab(index):
            self._sync_topbar_selection(current_index)
            return
        if not self._can_leave_main_tab(current_index):
            self._sync_topbar_selection(current_index)
            return

        self._pending_guarded_tab_index = index
        self.tab.setCurrentIndex(index)
        if self.tab.currentIndex() == current_index:
            self._pending_guarded_tab_index = None
            self._sync_topbar_selection(current_index)

    def _on_main_tab_changed(self, index: int) -> None:
        previous_index = self._current_main_tab_index
        allowed_target = self._pending_guarded_tab_index
        self._pending_guarded_tab_index = None

        if self._reverting_main_tab_change:
            self._reverting_main_tab_change = False
        elif index != previous_index:
            if not self._can_activate_main_tab(index):
                self._revert_main_tab_change(previous_index)
                return
            if index != allowed_target and not self._can_leave_main_tab(previous_index):
                self._revert_main_tab_change(previous_index)
                return

        self._finalize_main_tab_change(index)

    def _finalize_main_tab_change(self, index: int) -> None:
        self._current_main_tab_index = index
        self._sync_topbar_selection(index)
        if index == self._TEST_PAGE_INDEX:
            self._refresh_analysis_display_modules()
            self._apply_analysis_display_mode()

    def _revert_main_tab_change(self, previous_index: int) -> None:
        self._reverting_main_tab_change = True
        self.tab.setCurrentIndex(previous_index)
        self._sync_topbar_selection(previous_index)

    def _sync_topbar_selection(self, index: int) -> None:
        for button_index, name in enumerate(getattr(self, "tab_names", [])):
            button = self.top_button_dict.get(name)
            if button is not None:
                button.setChecked(button_index == index)

    def _role_permissions(self, role):
        return ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["操作员"])

    def _can_activate_main_tab(self, index: int | None, *, role=None) -> bool:
        if index is None or index < 0 or index >= len(getattr(self, "tab_names", [])):
            return False

        effective_role = self.current_user_role if role is None else role
        if effective_role is None:
            return True

        tab_name = self.tab_names[index]
        allowed_tabs = self._role_permissions(effective_role)["tabs"]
        return tab_name in allowed_tabs

    def _ensure_role_compatible_main_tab(self, role) -> bool:
        if not hasattr(self, "tab"):
            return True

        current_index = self.tab.currentIndex()
        if self._can_activate_main_tab(current_index, role=role):
            return True

        target_index = self._first_allowed_main_tab_index(role)
        if target_index is None:
            return False
        if target_index == current_index:
            return True
        if not self._can_leave_main_tab(current_index):
            return False

        self._pending_guarded_tab_index = target_index
        self.tab.setCurrentIndex(target_index)
        if self.tab.currentIndex() != target_index:
            self._pending_guarded_tab_index = None
            return False
        return self._can_activate_main_tab(target_index, role=role)

    def _first_allowed_main_tab_index(self, role) -> int | None:
        allowed_tabs = self._role_permissions(role)["tabs"]
        for index, name in enumerate(getattr(self, "tab_names", [])):
            if name in allowed_tabs:
                return index
        return None

    def _can_leave_main_tab(self, index: int | None) -> bool:
        if index != self._ai_tool_management_tab_index():
            return True

        confirm_leave = getattr(
            getattr(self, "ai_tool_management_module", None),
            "confirm_leave",
            None,
        )
        if callable(confirm_leave):
            return bool(confirm_leave())
        return True

    def _ai_tool_management_tab_index(self) -> int | None:
        try:
            return self.tab_names.index("AI工具管理")
        except ValueError:
            return None

    def _on_analysis_channel_changed(self, channel_index: int) -> None:
        self._sync_selected_display_channel(channel_index, refresh_modules=False)
        self._refresh_analysis_display_modules()

    def _sync_selected_display_channel(
        self,
        channel_index: int,
        *,
        refresh_modules: bool = True,
    ) -> None:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return

        channels = self._resolve_display_channels(config_id)
        if channel_index not in channels:
            return

        self._last_selected_display_channel = channel_index
        self.display_channel_by_config_id[config_id] = channel_index
        self.analysis_toolbar.set_current_channel(channel_index)
        self.analysis_toolbar.set_context_label_text(
            self._build_analysis_toolbar_context(config_id, channel_index)
        )
        self._emit_factory_display_channel_changed(config_id, channel_index)
        self._apply_analysis_display_mode()

        if refresh_modules:
            self._refresh_analysis_display_modules()

    def _refresh_analysis_display_modules(self) -> None:
        for module in (
            self.stft,
            self.loudness,
            self.spl,
            self.spectrum,
            self.octave,
            self.vibration_axis,
            self.vibration_trend,
            self.result,
        ):
            refresh = getattr(module, "refresh_selected_display_channel", None)
            if callable(refresh):
                refresh()

    def _sync_analysis_toolbar(self) -> None:
        if not hasattr(self, "analysis_toolbar"):
            return

        config_id = self._factory_state.displayed_config_id
        if (
            config_id is None
            or self._factory_state.run_phase not in self._DISPLAY_CHANNEL_PHASES
        ):
            if config_id is not None:
                self.display_channel_by_config_id.pop(config_id, None)
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            self._apply_analysis_display_mode()
            return

        channels = self._resolve_display_channels(config_id)
        if not channels:
            self.display_channel_by_config_id.pop(config_id, None)
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            self._apply_analysis_display_mode()
            return

        current_channel = self._resolve_current_display_channel(config_id, channels)
        if current_channel is None:
            self.display_channel_by_config_id.pop(config_id, None)
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            self._apply_analysis_display_mode()
            return

        self.display_channel_by_config_id[config_id] = current_channel
        self.analysis_toolbar.set_available_channels(
            channels,
            self._display_channel_labels_for_config(config_id),
        )
        self.analysis_toolbar.set_current_channel(current_channel)
        self.analysis_toolbar.set_context_label_text(
            self._build_analysis_toolbar_context(config_id, current_channel)
        )
        self.analysis_toolbar.show()
        self._emit_factory_display_channel_changed(config_id, current_channel)
        self._apply_analysis_display_mode()

    def _emit_factory_display_channel_changed(
        self,
        config_id: int,
        channel_index: int,
    ) -> None:
        signal = getattr(self.services.bus, "factory_display_channel_changed", None)
        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit(config_id, channel_index)

    def _resolve_display_channels(self, config_id: int) -> list[int]:
        if self._is_current_imported_review(config_id):
            return self._chart_payload_channels_for_config(config_id)
        if self._is_transient_imported_review_without_chart_payload(config_id):
            channels = self._analysis_toolbar_channel_options()
            stored_channel = self.display_channel_by_config_id.get(config_id)
            if isinstance(stored_channel, int) and stored_channel in channels:
                return channels
        return self._selected_channels_for_config(config_id)

    def _analysis_toolbar_channel_options(self) -> list[int]:
        if not hasattr(self, "analysis_toolbar"):
            return []
        combo = getattr(self.analysis_toolbar, "channel_combo", None)
        if combo is None:
            return []
        channels: list[int] = []
        for index in range(combo.count()):
            channel = combo.itemData(index)
            if isinstance(channel, int) and channel not in channels:
                channels.append(channel)
        return channels

    def _resolve_current_display_channel(
        self,
        config_id: int,
        channels: list[int],
    ) -> int | None:
        if not channels:
            return None

        review_channels: list[int] | None = None
        is_transient_reanalysis_without_chart_payload = False
        if (
            self._factory_state.run_phase in self._COMMITTED_CHART_CHANNEL_PHASES
            and self._review_payload.displayed_config_id == config_id
        ):
            is_transient_reanalysis_without_chart_payload = (
                self._is_transient_reanalysis_review_without_chart_payload(config_id)
            )
            if is_transient_reanalysis_without_chart_payload:
                review_channels = None
            else:
                review_channels = self._chart_payload_channels_for_config(config_id)

        def channel_is_available(channel_index: int | None) -> bool:
            return (
                channel_index in channels
                and (review_channels is None or channel_index in review_channels)
            )

        if is_transient_reanalysis_without_chart_payload:
            stored_channel = self.display_channel_by_config_id.get(config_id)
            if isinstance(stored_channel, int) and stored_channel in channels:
                return stored_channel

        vibration_channel = self._vibration_view_channel_for_config(config_id)
        if channel_is_available(vibration_channel):
            stored_channel = self.display_channel_by_config_id.get(config_id)
            if channel_is_available(stored_channel):
                return stored_channel
            return vibration_channel

        if review_channels is not None:
            stored_channel = self.display_channel_by_config_id.get(config_id)
            if channel_is_available(stored_channel):
                return stored_channel

        remembered_channel = self._last_selected_display_channel
        if channel_is_available(remembered_channel):
            return remembered_channel

        candidate_channel = channels[0]
        if review_channels and candidate_channel not in review_channels:
            for channel in channels:
                if channel in review_channels:
                    return channel
            return None

        return candidate_channel

    def _selected_channels_for_config(self, config_id: int) -> list[int]:
        config = self._load_config_for_display(config_id)
        return self._normalize_channels(config.get("selected_channels", []))

    def _config_payload_for_displayed_review(self, config_id: int) -> dict:
        if self._review_payload.displayed_config_id != config_id:
            return {}
        config_payload = getattr(self._review_payload, "config_payload", None)
        return config_payload if isinstance(config_payload, dict) else {}

    def _load_config_for_display(self, config_id: int) -> dict:
        payload_config = self._config_payload_for_displayed_review(config_id)
        if payload_config:
            return payload_config

        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if not callable(load_config):
            return {}

        try:
            config = load_config(config_id)
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    def _vibration_view_channel_for_config(self, config_id: int) -> int | None:
        config = self._load_config_for_display(config_id)
        value = config.get("vibration_view_channel_index")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        selected = self._normalize_channels(config.get("selected_channels", []))
        return value if value in selected else None

    def _chart_payload_channels_for_config(self, config_id: int) -> list[int]:
        if self._review_payload.displayed_config_id != config_id:
            return []
        chart_payload = getattr(self._review_payload, "chart_payload", None)
        if not isinstance(chart_payload, dict):
            return []
        return self._normalize_channels(chart_payload.keys())

    def _is_transient_reanalysis_review_without_chart_payload(
        self,
        config_id: int,
    ) -> bool:
        stored_channel = self.display_channel_by_config_id.get(config_id)
        return (
            self._factory_state.run_phase in {"reviewing", "reanalyzing"}
            and isinstance(stored_channel, int)
            and self._review_payload.displayed_config_id == config_id
            and getattr(self._review_payload, "chart_payload", None) is None
            and getattr(self._review_payload, "summary_payload", None) is None
            and getattr(self._review_payload, "judgment_payload", None) is None
            and not bool(getattr(self._review_payload, "no_result", False))
            and getattr(self._review_payload, "error_message", None) is None
        )

    def _is_transient_imported_review_without_chart_payload(
        self,
        config_id: int,
    ) -> bool:
        return (
            getattr(self._review_payload, "trigger_source", None) == "import_audio"
            and self._is_transient_reanalysis_review_without_chart_payload(config_id)
        )

    def _display_channel_labels_for_config(self, config_id: int) -> dict[int, str] | None:
        if not self._is_current_imported_review(config_id):
            return None

        labels = getattr(self._review_payload, "display_channel_labels", None)
        if not isinstance(labels, dict):
            return None

        normalized: dict[int, str] = {}
        vibration_channel = self._vibration_view_channel_for_config(config_id)
        for channel, label in labels.items():
            try:
                channel_index = int(channel)
            except (TypeError, ValueError):
                continue
            if isinstance(label, str) and label.strip():
                normalized_label = label.strip()
                if channel_index == vibration_channel:
                    normalized_label = self._append_vibration_label(normalized_label)
                normalized[channel_index] = normalized_label
        return normalized

    def _is_current_imported_review(self, config_id: int) -> bool:
        return (
            getattr(self._review_payload, "trigger_source", None) == "import_audio"
            and self._factory_state.run_phase in self._COMMITTED_CHART_CHANNEL_PHASES
            and self._review_payload.displayed_config_id == config_id
            and isinstance(getattr(self._review_payload, "chart_payload", None), dict)
        )

    @staticmethod
    def _normalize_channels(channels) -> list[int]:
        normalized_channels: list[int] = []
        for channel in channels or ():
            try:
                channel_index = int(channel)
            except (TypeError, ValueError):
                continue
            if channel_index not in normalized_channels:
                normalized_channels.append(channel_index)
        return normalized_channels

    def _build_analysis_toolbar_context(self, config_id: int, channel_index: int) -> str:
        if self._is_current_imported_review(config_id):
            label = (self._display_channel_labels_for_config(config_id) or {}).get(
                channel_index
            )
            suffix = label or f"配置 {config_id} / 通道{channel_index + 1}"
            context = f"导入音频 / {suffix}"
        else:
            context = f"配置 {config_id} / 通道{channel_index + 1}"
        if channel_index == self._vibration_view_channel_for_config(config_id):
            context = self._append_vibration_context(context)
        return context

    @staticmethod
    def _append_vibration_label(label: str) -> str:
        return label if "振动" in label else f"{label}（振动）"

    @staticmethod
    def _append_vibration_context(context: str) -> str:
        return context if "振动" in context else f"{context} / 振动"

    def _on_loudness_title_changed(self, title: str) -> None:
        if not self._is_current_display_vibration():
            self.loudness_card.set_title(title)

    def _set_stft_score_title(self, text: str) -> None:
        self.stft_card.set_title_metric("频谱平均得分：", self._stft_score_value(text))

    @staticmethod
    def _stft_score_value(text: str) -> str:
        normalized = str(text or "").strip()
        for separator in ("：", ":"):
            if separator in normalized:
                return normalized.split(separator, 1)[1].strip() or "--"
        return normalized or "--"

    def _is_current_display_vibration(self) -> bool:
        config_id = self._factory_state.displayed_config_id
        channel = self.current_display_channel_index()
        if config_id is None or channel is None:
            return False
        return channel == self._vibration_view_channel_for_config(config_id)

    def _apply_analysis_display_mode(self) -> None:
        if not hasattr(self, "loudness_card"):
            return

        vibration_mode = self._is_current_display_vibration()
        if vibration_mode:
            self.stft_card.set_title("振动纹理")
            self.spectrum_card.set_title("振动频谱")
            self.loudness_card.set_title("振动频率稳定性")
            self.spl_card.set_title("振动脉冲")
            self.octave_card.set_title("机械频带")
            self.loudness_stack.setCurrentWidget(self.vibration_axis)
            self.spl_stack.setCurrentWidget(self.vibration_trend)
        else:
            self.stft_card.set_title("时频图")
            self.spectrum_card.set_title("频谱")
            self.loudness_card.set_title(self.loudness.current_title())
            self.spl_card.set_title("声压级（dB）")
            self.octave_card.set_title("倍频程（dB）")
            self.loudness_stack.setCurrentWidget(self.loudness)
            self.spl_stack.setCurrentWidget(self.spl)

        self._set_module_vibration_mode(self.stft, vibration_mode)
        self._set_module_vibration_mode(self.spectrum, vibration_mode)
        self._set_module_vibration_mode(self.octave, vibration_mode)

    @staticmethod
    def _set_module_vibration_mode(module, enabled: bool) -> None:
        set_vibration_mode = getattr(module, "set_vibration_mode", None)
        if callable(set_vibration_mode):
            set_vibration_mode(enabled)

    def closeEvent(self, event: QCloseEvent):
        if not self._can_leave_main_tab(self._ai_tool_management_tab_index()):
            event.ignore()
            return
        self.services.bus.log.emit("[App] 关闭程序")
        try:
            for plc_service in getattr(self.services, "plc_services", {}).values():
                if plc_service is None:
                    continue
                try:
                    QMetaObject.invokeMethod(
                        plc_service,
                        "stop_plc_service",
                        Qt.ConnectionType.BlockingQueuedConnection,
                    )
                except Exception:
                    pass
            for plc_thread in getattr(self.services, "plc_threads", {}).values():
                if plc_thread is None:
                    continue
                try:
                    plc_thread.quit()
                    plc_thread.wait(2000)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.services.shutdown()
        except Exception:
            pass
        try:
            stop_camera = getattr(getattr(self, "video", None), "stop_camera", None)
            if callable(stop_camera):
                stop_camera()
        except Exception:
            pass
        super().closeEvent(event)
