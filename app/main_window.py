from __future__ import annotations

from PyQt6.QtCore import Qt, QMetaObject
from PyQt6.QtGui import QAction, QCloseEvent
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

from app.services_container import FactoryControllerState, FactoryReviewPayload
from consts.style import APP_QSS
from ui.widgets.cards import Card
from ui.widgets.analysis_channel_toolbar import AnalysisChannelToolbar
from ui.modules.workorder import WorkOrderModule
from ui.modules.control import ControlModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.stft import StftModule
from ui.modules.spl import SplModule
from ui.modules.octave import OctaveModule
from ui.modules.result import ResultModule
from ui.modules.video import VideoModule
from ui.modules.log import LogModule
from ui.modules.hardware import HardwareModule
from ui.modules.data_management import DataManagementModule
from ui.modules.input_calibration import InputCalibrationModule
from ui.modules.config import ConfigModule
from ui.modules.channel_select import ChannelSelectModule
from ui.modules.plc_settings import PlcSettingsModule
from ui.modules.login import LoginWindow
from ui.modules.change_password import ChangePasswordWindow
from ui.modules.add_user import AddUserWindow
from ui.modules.delete_user import DeleteUserWindow

ROLE_PERMISSIONS = {
    "管理员": {
        "tabs": {"测试", "数据管理", "配置", "硬件"},
        "actions": {"切换用户", "添加用户", "删除用户", "修改密码"},
    },
    "工程师": {
        "tabs": {"测试", "配置", "硬件"},
        "actions": {"切换用户", "修改密码"},
    },
    "操作员": {
        "tabs": {"测试", "硬件"},
        "actions": {"切换用户", "修改密码"},
    },
}


class MainWindow(QMainWindow):
    _TEST_PAGE_INDEX = 0
    _DISPLAY_CHANNEL_PHASES = frozenset(
        {"running", "analyzing", "reviewing", "reanalyzing"}
    )

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.current_user_name = None  # current_user_name作为是否已登录的唯一判断
        self.current_user_role = None
        self.login_window = None
        self.change_password_window = None
        self.add_user_window = None
        self.delete_user_window = None
        self.login_shown = False
        factory_mode = getattr(self.services, "factory_mode", None)
        self._factory_state = getattr(factory_mode, "state", FactoryControllerState())
        self._review_payload = getattr(factory_mode, "review_payload", FactoryReviewPayload())
        self.display_channel_by_config_id: dict[int, int] = {}

        self.setWindowTitle("精成电机")
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

        # 页面：测试/数据管理/配置/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_data_page())
        self.tab.addWidget(self.build_config_page())
        self.tab.addWidget(self.build_hardware_page())
        self.tab.currentChanged.connect(self._on_main_tab_changed)

        self.statusBar().showMessage("Idle")

        self.services.bus.log.emit("启动程序")
        self._sync_analysis_toolbar()
        self._refresh_analysis_display_modules()
        self.set_role()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.login_shown:
            self.login_shown = True
            self.show_login_window(close_main_window=True, window_title="登录")


    def show_login_window(self, close_main_window=False, window_title="登录"):
        self.setEnabled(False)

        def on_login_success(username: str, role: str):
            self.current_user_name = username
            self.current_user_role = role
            self.setEnabled(True)
            self.set_role()
            self.raise_()
            self.activateWindow()

        def on_login_cancel():
            self.setEnabled(True)
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
        self.tab_names = ["测试", "数据管理", "配置", "硬件"]
        for i, name in enumerate(self.tab_names):
            b = add_tab(name, checked=(i == 0))
            self.top_button_dict[name] = b
            group.setId(b, i)
        # 把group中的按钮一起绑定页面设置
        group.idClicked.connect(self.tab.setCurrentIndex)

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
        role = self.current_user_role
        perms = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["操作员"])
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

        if hasattr(self, "tab") and "测试" in getattr(self, "top_button_dict", {}):
            current_name = self.tab_names[self.tab.currentIndex()]
            current_btn = self.top_button_dict.get(current_name)
            if current_btn and not current_btn.isEnabled():
                self.tab.setCurrentIndex(0)
                self.top_button_dict["测试"].setChecked(True)

        if self.current_user_name and self.current_user_role:
            self.statusBar().showMessage(
                f"当前用户：{self.current_user_name} | 权限：{self.current_user_role}"
            )

    def build_test_page(self) -> QWidget:
        page = QWidget()
        content_lay = QHBoxLayout(page)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(14)

        # 左侧栏
        left = QWidget()
        left.setFixedWidth(320)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(12)

        self.workorder = WorkOrderModule(self.services)
        left_lay.addWidget(Card("工单输入", self.workorder))

        self.control = ControlModule(self.services)
        left_lay.addWidget(Card("控制面板", self.control))

        self.log = LogModule(self.services)
        left_lay.addWidget(Card("系统日志", self.log), 1)

        content_lay.addWidget(left)

        # 右侧主区域：3x2 网格（6 个卡片同尺寸）
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setRowStretch(0, 0)
        grid.setRowStretch(1, 1)
        grid.setRowStretch(2, 1)

        self.result = ResultModule(self.services)
        grid.addWidget(Card("结果摘要", self.result), 1, 0)

        self.video = VideoModule(self.services)
        grid.addWidget(Card("视频预览", self.video), 2, 0)

        self.analysis_toolbar = AnalysisChannelToolbar()
        self.analysis_toolbar.channel_changed.connect(self._on_analysis_channel_changed)
        self.analysis_toolbar.hide()
        grid.addWidget(self.analysis_toolbar, 0, 1, 1, 2)

        self.spectrum = SpectrumModule(self.services)
        grid.addWidget(Card("频谱 (Spectrum)", self.spectrum), 1, 2)

        self.stft = StftModule(self.services)
        grid.addWidget(Card("STFT 频谱图 (Spectrogram)", self.stft), 1, 1)

        self.spl = SplModule(self.services)
        grid.addWidget(Card("SPL 时间曲线 (A计权)", self.spl), 2, 1)

        self.octave = OctaveModule(self.services)
        grid.addWidget(Card("倍频程 (Octave)", self.octave), 2, 2)

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

    def build_hardware_page(self) -> QWidget:
        """
        左侧：设备选择/保存
        右侧：输入校准（后续在这里扩展）
        """
        page = QWidget()
        content_lay = QHBoxLayout(page)
        content_lay.setContentsMargins(0, 0, 0, 0)
        content_lay.setSpacing(14)

        # 左侧栏
        left = QWidget()
        left.setFixedWidth(1500)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(12)

        hw = HardwareModule(self.services)
        left_lay.addWidget(Card("设备选择", hw), 1)
        content_lay.addWidget(left)

        # 右侧主区域（先占位，后续替换为真正的输入校准模块）
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        cal = InputCalibrationModule(self.services)
        cal_card = Card("输入校准", cal)
        # 让右侧校准区域更紧凑（类似参考图）
        cal_card.setMaximumWidth(560)
        cal_card.setFixedHeight(380)

        cal_row = QWidget()
        cal_row_lay = QHBoxLayout(cal_row)
        cal_row_lay.setContentsMargins(0, 0, 0, 0)
        cal_row_lay.setSpacing(0)
        cal_row_lay.addWidget(cal_card, 0)
        cal_row_lay.addStretch(1)

        channel = ChannelSelectModule(self.services)
        channel_card = Card("通道选择", channel)
        channel_card.setMaximumWidth(560)
        channel_card.setFixedHeight(100)

        plc = PlcSettingsModule(self.services)
        plc_card = Card("PLC设置", plc)
        plc_card.setMaximumWidth(560)
        plc_card.setFixedHeight(420)

        main_lay.addWidget(channel_card, 0)
        main_lay.addWidget(cal_row, 0)
        main_lay.addWidget(plc_card, 0)

        main_lay.addStretch(1)
        content_lay.addWidget(main, 1)
        return page

    def _connect_bus_signals(self) -> None:
        self.services.bus.status.connect(self.statusBar().showMessage)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)

    def current_display_channel_index(self) -> int | None:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return None
        return self.display_channel_by_config_id.get(config_id)

    def _attach_analysis_channel_providers(self) -> None:
        for module in (self.stft, self.spl, self.spectrum, self.octave):
            module.selected_display_channel_provider = self.current_display_channel_index
        for module in (self.spl, self.spectrum, self.octave):
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
        self._sync_analysis_toolbar()

    def _on_factory_display_reset(self, _reason: str) -> None:
        self._review_payload = FactoryReviewPayload()
        self._sync_analysis_toolbar()

    def _on_main_tab_changed(self, index: int) -> None:
        if index == self._TEST_PAGE_INDEX:
            self._refresh_analysis_display_modules()

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

        self.display_channel_by_config_id[config_id] = channel_index
        self.analysis_toolbar.set_current_channel(channel_index)
        self.analysis_toolbar.set_context_label_text(
            self._build_analysis_toolbar_context(config_id, channel_index)
        )

        if refresh_modules:
            self._refresh_analysis_display_modules()

    def _refresh_analysis_display_modules(self) -> None:
        for module in (self.stft, self.spl, self.spectrum, self.octave):
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
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            return

        channels = self._resolve_display_channels(config_id)
        if not channels:
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            return

        current_channel = self._resolve_current_display_channel(config_id, channels)
        if current_channel is None:
            self.analysis_toolbar.set_available_channels([])
            self.analysis_toolbar.set_context_label_text("")
            self.analysis_toolbar.hide()
            return

        self.display_channel_by_config_id[config_id] = current_channel
        self.analysis_toolbar.set_available_channels(channels)
        self.analysis_toolbar.set_current_channel(current_channel)
        self.analysis_toolbar.set_context_label_text(
            self._build_analysis_toolbar_context(config_id, current_channel)
        )
        self.analysis_toolbar.show()

    def _resolve_display_channels(self, config_id: int) -> list[int]:
        selected_channels = self._selected_channels_for_config(config_id)
        if selected_channels:
            return selected_channels

        if self._factory_state.run_phase in {"reviewing", "reanalyzing"}:
            return self._chart_payload_channels_for_config(config_id)

        return []

    def _resolve_current_display_channel(
        self,
        config_id: int,
        channels: list[int],
    ) -> int | None:
        if not channels:
            return None

        remembered_channel = self.display_channel_by_config_id.get(config_id)
        review_channels = self._chart_payload_channels_for_config(config_id)
        if (
            remembered_channel is not None
            and review_channels
            and remembered_channel not in review_channels
        ):
            for review_channel in review_channels:
                if review_channel in channels:
                    return review_channel

        if remembered_channel in channels:
            return remembered_channel

        return channels[0]

    def _selected_channels_for_config(self, config_id: int) -> list[int]:
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)
        if not callable(load_config):
            return []

        try:
            config = load_config(config_id)
        except Exception:
            return []

        if not isinstance(config, dict):
            return []

        return self._normalize_channels(config.get("selected_channels", []))

    def _chart_payload_channels_for_config(self, config_id: int) -> list[int]:
        if self._review_payload.displayed_config_id != config_id:
            return []
        chart_payload = getattr(self._review_payload, "chart_payload", None)
        if not isinstance(chart_payload, dict):
            return []
        return self._normalize_channels(chart_payload.keys())

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

    @staticmethod
    def _build_analysis_toolbar_context(config_id: int, channel_index: int) -> str:
        return f"配置 {config_id} / CH{channel_index + 1}"

    def closeEvent(self, event: QCloseEvent):
        self.services.bus.log.emit("关闭程序")
        try:
            # 停止 PLC 监听
            for plc_service in getattr(self.services, "plc_services", {}).values():
                if plc_service is None:
                    continue
                QMetaObject.invokeMethod(
                    plc_service,
                    "stop_plc_service",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
            for plc_thread in getattr(self.services, "plc_threads", {}).values():
                if plc_thread is None:
                    continue
                plc_thread.quit()
                plc_thread.wait(2000)
            # 关闭 DB 长连接
            self.services.db.close()
            # 视频关闭
            self.video.stop_camera()
        except Exception:
            pass
        super().closeEvent(event)
