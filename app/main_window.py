from __future__ import annotations

from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QMainWindow,
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


class MainWindow(QMainWindow):
    def __init__(self, services):
        super().__init__()
        self.services = services

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
        outer.addWidget(self._build_topbar())
        outer.addWidget(self.tab, 1)

        # 页面：测试/数据管理/配置/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_placeholder_page("数据管理"))
        self.tab.addWidget(self.build_placeholder_page("配置"))
        self.tab.addWidget(self.build_hardware_page())

        self.services.bus.status.connect(self.statusBar().showMessage)
        self.statusBar().showMessage("Idle")

        self.services.bus.log.emit("[App] 启动程序")

    def closeEvent(self, event: QCloseEvent):
        self.services.bus.log.emit("[App] 关闭程序")
        try:
            # 关闭 DB 长连接
            self.services.db.close()
        except Exception:
            pass
        super().closeEvent(event)

    def _build_topbar(self):
        bar = QWidget()
        bar.setObjectName("TopBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        group = QButtonGroup(self)
        group.setExclusive(True)

        def add_tab(text: str, checked: bool = False):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(checked)
            b.setObjectName("TopTab")
            group.addButton(b)
            lay.addWidget(b)
            return b

        tabs = [
            add_tab("测试", True),
            add_tab("数据管理"),
            add_tab("配置"),
            add_tab("硬件"),
        ]
        for i, b in enumerate(tabs):
            group.setId(b, i)

        group.idClicked.connect(self.tab.setCurrentIndex)

        lay.addStretch(1)
        return bar

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

        main_lay.addWidget(cal_row, 0)
        main_lay.addStretch(1)
        content_lay.addWidget(main, 1)
        return page


