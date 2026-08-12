from __future__ import annotations

import os
import sys
from threading import Thread

from PyQt6.QtCore import QProcess, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from base.utils import asset_path

from app.service.multi_run_coordinator_mvp import (
    ManualJudgmentRequest as CompositeManualJudgmentRequest,
    OperationResult,
    OperationStatus,
    ReanalysisCancelRequest as CompositeReanalysisCancelRequest,
    ReanalysisRequest as CompositeReanalysisRequest,
    TerminalConclusion,
)
from app.service.multi_run_integration.shutdown import ApplicationShutdownReport
from app.service.result_history_contracts import (
    ManualJudgmentRequest as UiManualJudgmentRequest,
    ReanalysisCancelRequest as UiReanalysisCancelRequest,
    ReanalysisRequest as UiReanalysisRequest,
)
from app.services_container import FactoryControllerState
from consts.style import APP_QSS
from ui.widgets.cards import Card
from ui.modules.workorder import WorkOrderModule
from ui.modules.control import ControlModule
from ui.modules.result_dashboard import ResultDashboardModule
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
        "tabs": {"测试", "数据管理", "配置", "AI管理", "硬件"},
        "actions": {"切换用户", "添加用户", "删除用户", "修改密码"},
    },
    "工程师": {
        "tabs": {"测试", "数据管理", "配置", "AI管理", "硬件"},
        "actions": {"切换用户", "修改密码"},
    },
    "操作员": {
        "tabs": {"测试"},
        "actions": {"切换用户"},
    },
}


class MainWindow(QMainWindow):
    _TEST_PAGE_INDEX = 0
    _manual_judgment_completed = pyqtSignal(object)

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
        self._selected_plc_bindings: tuple[int, ...] = ()
        self._selected_config_count = 0
        self._selected_unbound_plc_count = 0
        self._selected_config_load_error_count = 0
        self._selected_plc_tooltip = ""
        self._current_main_tab_index = self._TEST_PAGE_INDEX
        self._pending_guarded_tab_index: int | None = None
        self._reverting_main_tab_change = False
        self._restart_in_progress = False

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
        self._reload_selected_plc_bindings()
        self._refresh_plc_state_labels()

        # 页面：测试/数据管理/配置/AI管理/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_data_page())
        self.tab.addWidget(self.build_config_page())
        self.tab.addWidget(self.build_ai_tool_management_page())
        self.tab.addWidget(self.build_hardware_page())
        self.tab.currentChanged.connect(self._on_main_tab_changed)

        self.services.bus.log.emit("[App] 启动程序")
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
        self.tab_names = ["测试", "数据管理", "配置", "AI管理", "硬件"]
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

        plc_state_bar = QWidget()
        plc_state_lay = QHBoxLayout(plc_state_bar)
        plc_state_lay.setContentsMargins(66, 0, 0, 0)
        plc_state_lay.setSpacing(12)
        self.selected_plc_state_label = QLabel("PLC：未选择配置")
        self.process_plc_state_label = QLabel("工艺：未连接")
        for label in (
            self.selected_plc_state_label,
            self.process_plc_state_label,
        ):
            label.setStyleSheet("color: #475569; font-size: 12px;")
            plc_state_lay.addWidget(label)
        lay.addWidget(plc_state_bar)

        self.plc_state_timer = QTimer(self)
        # 2秒刷一下
        self.plc_state_timer.setInterval(2000)
        self.plc_state_timer.timeout.connect(self._refresh_plc_state_labels)
        self.plc_state_timer.start()
        self._refresh_plc_state_labels()

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
        self.control.btn_stop.setText("停止测试")
        self.control.restart_intent.connect(self._restart_application)
        left_lay.addWidget(Card("控制面板", self.control))

        self.today_statistics = TodayStatisticsModule(self.services)
        self.today_statistics_card = Card("今日数据统计", self.today_statistics)
        self.today_statistics_card.setFixedHeight(108)
        left_lay.addWidget(self.today_statistics_card)

        self.video = VideoModule(self.services)
        self.services.register_independent_shutdown_resource(
            self.video.stop_camera
        )
        self.video_card = Card("视频预览", self.video)
        self.video_card.setMinimumHeight(270)
        left_lay.addWidget(self.video_card)

        content_lay.addWidget(left)

        # 右侧主区域：运行时结果展示面板
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        self.result_dashboard = ResultDashboardModule(
            self.services,
            self.services.result_history_store,
            self.control.manual_judgment_widget,
        )
        self.services.attach_committed_history_observer(
            self.today_statistics.on_committed_history_event
        )
        self.history_adapter = self.services.attach_result_dashboard(
            self.result_dashboard
        )
        self._wire_run_scoped_result_operations()
        main_lay.addWidget(self.result_dashboard, 1)
        content_lay.addWidget(main, 1)
        return page

    def _wire_run_scoped_result_operations(self) -> None:
        completion_signal = getattr(
            self,
            "_manual_judgment_completed",
            None,
        )
        if callable(getattr(completion_signal, "connect", None)):
            completion_signal.connect(
                self.control.handle_manual_judgment_result
            )
        self.result_dashboard.manual_judgment_target_changed.connect(
            self.control.set_manual_judgment_target
        )
        self.control.manual_judgment_intent.connect(
            self.result_dashboard.request_manual_judgment
        )
        self.result_dashboard.manual_judgment_requested.connect(
            self._apply_manual_judgment_request
        )
        self.result_dashboard.reanalysis_requested.connect(
            self._start_reanalysis_request
        )
        self.result_dashboard.reanalysis_cancel_requested.connect(
            self._cancel_reanalysis_request
        )
        self.control.stop_intent.connect(self._stop_all_active_runs)

    def _apply_manual_judgment_request(self, request: object) -> None:
        if type(request) is not UiManualJudgmentRequest:
            return
        if request.label == "OK":
            conclusion = TerminalConclusion.OK
        elif request.label == "NG":
            conclusion = TerminalConclusion.NG
        else:
            return
        composite_request = CompositeManualJudgmentRequest(
            run_id=request.run_id,
            expected_revision=request.expected_revision,
            conclusion=conclusion,
            fault_type=request.fault_type,
            bounded_note="",
        )
        completion_signal = getattr(
            self,
            "_manual_judgment_completed",
            None,
        )
        if callable(getattr(completion_signal, "emit", None)):
            Thread(
                target=self._commit_manual_judgment,
                args=(composite_request,),
                name="manual-judgment-save",
                daemon=True,
            ).start()
            return

        result = self.services.multi_run_coordinator.apply_manual(
            composite_request
        )
        self.control.handle_manual_judgment_result(result)

    def _commit_manual_judgment(
        self,
        request: CompositeManualJudgmentRequest,
    ) -> None:
        try:
            result = self.services.multi_run_coordinator.apply_manual(request)
        except Exception as exc:
            result = OperationResult(
                run_id=request.run_id,
                status=OperationStatus.PERSISTENCE_REJECTED,
                attempt_id=None,
                reason=str(exc).strip() or type(exc).__name__,
            )
        self._manual_judgment_completed.emit(result)

    def _start_reanalysis_request(self, request: object) -> None:
        if type(request) is not UiReanalysisRequest:
            return
        result = self.services.multi_run_coordinator.start_reanalysis(
            CompositeReanalysisRequest(
                run_id=request.run_id,
                expected_revision=request.expected_revision,
                start_sec=request.start_sec,
                end_sec=request.end_sec,
            )
        )
        if (
            type(result) is OperationResult
            and result.status is OperationStatus.NOT_FOUND
        ):
            self.services.bus.status.emit(
                "重新分析失败：所选运行记录不存在，可能已被删除"
            )

    def _cancel_reanalysis_request(self, request: object) -> None:
        if type(request) is not UiReanalysisCancelRequest:
            return
        self.services.multi_run_coordinator.cancel_reanalysis(
            CompositeReanalysisCancelRequest(
                run_id=request.run_id,
                attempt_id=request.operation_id,
                expected_revision=request.expected_revision,
            )
        )

    def _stop_all_active_runs(self) -> None:
        self.services.multi_run_coordinator.stop_all(5.0)

    def _refresh_plc_state_labels(self):
        plc_states = getattr(self.services, "plc_state", {})
        if not isinstance(plc_states, dict):
            plc_states = {}

        process_state = getattr(self.services, "process_plc_state", "未连接")
        self.selected_plc_state_label.setText(
            self._plc_status_html(
                self._visible_plc_ids(plc_states, self._selected_plc_bindings),
                self._selected_plc_bindings,
                plc_states,
                selected_config_count=self._selected_config_count,
                unbound_config_count=self._selected_unbound_plc_count,
                load_error_count=self._selected_config_load_error_count,
            )
        )
        self.selected_plc_state_label.setToolTip(self._selected_plc_tooltip)
        self._set_plc_state_label(self.process_plc_state_label, "工艺", process_state)

    def _reload_selected_plc_bindings(self) -> None:
        selected_config_ids = tuple(self._factory_state.selected_config_ids)
        config_service = getattr(self.services, "config_service", None)
        load_config = getattr(config_service, "load_config", None)

        plc_ids: list[int] = []
        seen_plc_ids: set[int] = set()
        unbound_count = 0
        load_error_count = 0
        tooltip_rows: list[str] = []
        for config_id in selected_config_ids:
            try:
                config = load_config(config_id) if callable(load_config) else None
            except Exception:
                config = None
            if not isinstance(config, dict):
                load_error_count += 1
                tooltip_rows.append(f"配置 {config_id}：加载失败")
                continue

            config_name = str(config.get("config_name") or f"配置 {config_id}")
            plc_id = config.get("plc_id")
            if not isinstance(plc_id, int) or isinstance(plc_id, bool):
                unbound_count += 1
                tooltip_rows.append(f"{config_name}：未绑定 PLC")
                continue

            tooltip_rows.append(f"{config_name}：PLC {plc_id}")
            if plc_id not in seen_plc_ids:
                seen_plc_ids.add(plc_id)
                plc_ids.append(plc_id)

        self._selected_plc_bindings = tuple(plc_ids)
        self._selected_config_count = len(selected_config_ids)
        self._selected_unbound_plc_count = unbound_count
        self._selected_config_load_error_count = load_error_count
        self._selected_plc_tooltip = "\n".join(tooltip_rows)

    @classmethod
    def _plc_status_html(
        cls,
        visible_plc_ids: tuple[int, ...],
        plc_ids: tuple[int, ...],
        plc_states: dict,
        *,
        selected_config_count: int,
        unbound_config_count: int,
        load_error_count: int,
    ) -> str:
        muted_color = "#64748b"
        if visible_plc_ids:
            parts = []
            for plc_id in visible_plc_ids:
                state = plc_states.get(plc_id, "未连接")
                color = cls._plc_state_color(state)
                parts.append(
                    f"PLC {plc_id}：<span style='color: {color};'>{state}</span>"
                )
            return "　".join(parts)

        if selected_config_count == 0:
            return f"PLC：<span style='color: {muted_color};'>未选择配置</span>"

        parts = []
        for plc_id in plc_ids:
            state = plc_states.get(plc_id, "未连接")
            color = cls._plc_state_color(state)
            parts.append(
                f"PLC {plc_id}：<span style='color: {color};'>{state}</span>"
            )
        if unbound_config_count:
            parts.append(
                f"<span style='color: {muted_color};'>"
                f"{unbound_config_count} 个配置未绑定 PLC</span>"
            )
        if load_error_count:
            parts.append(
                f"<span style='color: #dc2626;'>"
                f"{load_error_count} 个配置加载失败</span>"
            )
        return "　".join(parts)

    @staticmethod
    def _visible_plc_ids(plc_states: dict, selected_plc_ids: tuple[int, ...]) -> tuple[int, ...]:
        state_ids = sorted(
            plc_id
            for plc_id in plc_states
            if type(plc_id) is int and plc_id > 0
        )
        if state_ids:
            return tuple(state_ids)
        return tuple(selected_plc_ids)

    @staticmethod
    def _plc_state_color(state: str) -> str:
        if state == "已连接":
            return "#16a34a"
        if state in ("连接中", "重连中"):
            return "#d97706"
        return "#dc2626"

    @staticmethod
    def _set_plc_state_label(label, name, state):
        color = MainWindow._plc_state_color(state)
        label.setText(f"{name}：<span style='color: {color};'>{state}</span>")

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
        lay.addWidget(Card("AI管理", self.ai_tool_management_module), 1)
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
        left_lay.addWidget(Card("输入硬件管理", hw), 1)
        content_lay.addWidget(left, 7)

        # 右侧主区域：通道选择 / 输入校准 / PLC 设置作为一个整体垂直排列。
        main = QWidget()
        main_lay = QVBoxLayout(main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        cal = InputCalibrationModule(self.services)
        cal_card = Card("麦克风输入校准", cal)

        channel = ChannelSelectModule(self.services)
        channel_card = Card("硬件通道启用", channel)

        self.plc_settings_module = PlcSettingsModule(self.services)
        self.services.register_independent_shutdown_resource(
            self.plc_settings_module.stop_process_param_service
        )
        self.services.get_process_param_values = self.plc_settings_module.get_process_param_values
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
        if hasattr(self.services.bus, "config_changed"):
            self.services.bus.config_changed.connect(self._on_plc_binding_data_changed)
        if hasattr(self.services.bus, "plc_changed"):
            self.services.bus.plc_changed.connect(self._on_plc_binding_data_changed)

    def _on_plc_binding_data_changed(self) -> None:
        self._reload_selected_plc_bindings()
        self._refresh_plc_state_labels()

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

    def _on_factory_state_changed(self, state) -> None:
        if isinstance(state, FactoryControllerState):
            self._factory_state = state
        else:
            self._factory_state = FactoryControllerState()
        self._reload_selected_plc_bindings()
        self._refresh_plc_state_labels()

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
        guarded_module = self._guarded_main_tab_module(index)
        confirm_leave = getattr(
            guarded_module,
            "confirm_leave",
            None,
        )
        if callable(confirm_leave):
            return bool(confirm_leave())
        return True

    def _guarded_main_tab_module(self, index: int | None):
        if index == self._config_tab_index():
            return getattr(self, "config_module", None)
        if index == self._ai_tool_management_tab_index():
            return getattr(self, "ai_tool_management_module", None)
        return None

    def _config_tab_index(self) -> int | None:
        try:
            return self.tab_names.index("配置")
        except ValueError:
            return None

    def _ai_tool_management_tab_index(self) -> int | None:
        try:
            return self.tab_names.index("AI管理")
        except ValueError:
            return None

    def _restart_application(self) -> None:
        if self._restart_in_progress:
            return

        self._restart_in_progress = True
        self.control.set_restart_pending(True)
        if not self._can_leave_main_tab(self.tab.currentIndex()):
            self._restore_restart_control()
            return

        self.services.bus.log.emit("[App] 重启程序")
        shutdown_clean = True
        try:
            self.control.shutdown()
        except Exception:
            shutdown_clean = False
        try:
            shutdown_report = self.services.shutdown()
            shutdown_clean = (
                shutdown_clean
                and type(shutdown_report) is ApplicationShutdownReport
                and shutdown_report.clean is True
            )
        except Exception:
            shutdown_clean = False

        if not shutdown_clean:
            QMessageBox.critical(
                self,
                "重启软件",
                "软件关闭失败，未启动新实例。请重试或手动重启软件。",
            )
            self._restore_restart_control()
            return

        program, arguments = self._restart_command()
        try:
            detached_result = QProcess.startDetached(program, arguments)
            launched = (
                bool(detached_result[0])
                if isinstance(detached_result, tuple)
                else bool(detached_result)
            )
        except Exception:
            launched = False

        if not launched:
            QMessageBox.critical(
                self,
                "重启软件",
                "软件重启失败，未启动新实例。请手动启动软件。",
            )
            self._restore_restart_control()
            return

        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _restore_restart_control(self) -> None:
        self._restart_in_progress = False
        self.control.set_restart_pending(False)

    @staticmethod
    def _restart_command() -> tuple[str, list[str]]:
        arguments = list(sys.argv[1:])
        if getattr(sys, "frozen", False):
            return sys.executable, arguments
        main_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "main.py")
        )
        return sys.executable, [main_path, *arguments]

    def closeEvent(self, event: QCloseEvent):
        if not self._can_leave_main_tab(self.tab.currentIndex()):
            event.ignore()
            return
        self.services.bus.log.emit("[App] 关闭程序")
        try:
            self.control.shutdown()
        except Exception:
            pass
        try:
            self.services.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
