from __future__ import annotations

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

from consts.style import APP_QSS
from ui.widgets.cards import Card
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
from ui.modules.input_calibration import InputCalibrationModule
from ui.modules.config import ConfigModule
from ui.modules.channel_select import ChannelSelectModule
from ui.modules.login import LoginWindow
from ui.modules.change_password import ChangePasswordWindow
from ui.modules.add_user import AddUserWindow

ROLE_PERMISSIONS = {
    "管理员": {
        "tabs": {"测试", "数据管理", "配置", "硬件"},
        "actions": {"切换用户", "添加用户", "修改密码"},
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
    def __init__(self, services):
        super().__init__()
        self.services = services
        self.current_user_name = None  # current_user_name作为是否已登录的唯一判断
        self.current_user_role = None
        self.login_window = None
        self.change_password_window = None
        self.add_user_window = None
        self.login_shown = False

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

        # 页面：测试/数据管理/配置/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_placeholder_page("数据管理"))
        self.tab.addWidget(self.build_config_page())
        self.tab.addWidget(self.build_hardware_page())

        self.services.bus.status.connect(self.statusBar().showMessage)

        self.services.bus.log.emit("[App] 启动程序")
        self.set_role()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.login_shown:
            self.login_shown = True
            self.show_login_window(close_main_window=True, window_title="登录")

    def show_login_window(self, close_main_window=False, window_title="登录"):
        self.setEnabled(False) # 锁住主界面

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
            self.services.bus.status.emit("请先登录")
            return

        self.change_password_window = ChangePasswordWindow(self.current_user_name)
        self.change_password_window.show()

    def show_add_user_window(self):
        self.add_user_window = AddUserWindow()
        self.add_user_window.show()

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
        self.action_change_pwd = QAction("修改密码", self)

        self.action_switch_user.triggered.connect(
            lambda: self.show_login_window(close_main_window=False, window_title="切换用户")
        )
        self.action_add_user.triggered.connect(self.show_add_user_window)
        self.action_change_pwd.triggered.connect(self.show_change_password_window)

        user_menu.addAction(self.action_switch_user)
        user_menu.addAction(self.action_add_user)
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
            self.action_change_pwd.setEnabled("修改密码" in allowed_actions)

        # 禁用/启用按钮
        if hasattr(self, "control"):
            self.control.btn_start.setEnabled(bool(self.current_user_name))
            self.control.btn_stop.setEnabled(bool(self.current_user_name))

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
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)

        self.result = ResultModule(self.services)
        grid.addWidget(Card("结果摘要", self.result), 0, 0)

        self.video = VideoModule(self.services)
        grid.addWidget(Card("视频预览", self.video), 1, 0)

        self.spectrum = SpectrumModule(self.services)
        grid.addWidget(Card("频谱 (Spectrum)", self.spectrum), 0, 2)

        self.stft = StftModule(self.services)
        grid.addWidget(Card("STFT 频谱图 (Spectrogram)", self.stft), 0, 1)

        self.spl = SplModule(self.services)
        grid.addWidget(Card("SPL 时间曲线 (A计权)", self.spl), 1, 1)

        self.octave = OctaveModule(self.services)
        grid.addWidget(Card("倍频程 (Octave)", self.octave), 1, 2)

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
        left.setFixedWidth(900)
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
        cal_card.setFixedHeight(420)

        cal_row = QWidget()
        cal_row_lay = QHBoxLayout(cal_row)
        cal_row_lay.setContentsMargins(0, 0, 0, 0)
        cal_row_lay.setSpacing(0)
        cal_row_lay.addWidget(cal_card, 0)
        cal_row_lay.addStretch(1)

        channel = ChannelSelectModule(self.services)
        channel_card = Card("通道选择", channel)
        channel_card.setMaximumWidth(380)
        channel_card.setFixedHeight(100)

        main_lay.addWidget(channel_card, 0)
        main_lay.addWidget(cal_row, 0)

        main_lay.addStretch(1)
        content_lay.addWidget(main, 1)
        return page

    def closeEvent(self, event: QCloseEvent):
        self.services.bus.log.emit("[App] 关闭程序")
        try:
            # 关闭 DB 长连接
            self.services.db.close()
            # 视频关闭
            self.video.stop_camera()
        except Exception:
            pass
        super().closeEvent(event)