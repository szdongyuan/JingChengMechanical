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
from ui.modules.input_calibration import InputCalibrationModule
from ui.modules.config import ConfigModule
from ui.modules.channel_select import ChannelSelectModule


class MainWindow(QMainWindow):
    _DISPLAY_CHANNEL_PHASES = frozenset(
        {"running", "analyzing", "reviewing", "reanalyzing"}
    )

    def __init__(self, services):
        super().__init__()
        self.services = services
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
        outer.addWidget(self._build_topbar())
        outer.addWidget(self.tab, 1)

        # 页面：测试/数据管理/配置/硬件
        self.tab.addWidget(self.build_test_page())
        self.tab.addWidget(self.build_placeholder_page("数据管理"))
        self.tab.addWidget(self.build_config_page())
        self.tab.addWidget(self.build_hardware_page())

        self.services.bus.status.connect(self.statusBar().showMessage)
        if hasattr(self.services.bus, "factory_state_changed"):
            self.services.bus.factory_state_changed.connect(self._on_factory_state_changed)
        if hasattr(self.services.bus, "factory_review_changed"):
            self.services.bus.factory_review_changed.connect(self._on_factory_review_changed)
        if hasattr(self.services.bus, "factory_display_reset"):
            self.services.bus.factory_display_reset.connect(self._on_factory_display_reset)
        self.statusBar().showMessage("Idle")

        self.services.bus.log.emit("[App] 启动程序")
        self._sync_analysis_toolbar()

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

    def current_display_channel_index(self) -> int | None:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return None
        return self.display_channel_by_config_id.get(config_id)

    def _attach_analysis_channel_providers(self) -> None:
        for module in (self.stft, self.spl, self.spectrum, self.octave):
            module.selected_display_channel_provider = self.current_display_channel_index

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

    def _on_analysis_channel_changed(self, channel_index: int) -> None:
        config_id = self._factory_state.displayed_config_id
        if config_id is None:
            return
        self.display_channel_by_config_id[config_id] = channel_index
        self.analysis_toolbar.set_context_label_text(
            self._build_analysis_toolbar_context(config_id, channel_index)
        )

    def _sync_analysis_toolbar(self) -> None:
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
        if self._factory_state.run_phase in {"running", "analyzing"}:
            return self._selected_channels_for_config(config_id)

        review_channels = self._chart_payload_channels_for_config(config_id)
        if review_channels:
            return review_channels

        return self._selected_channels_for_config(config_id)

    def _resolve_current_display_channel(
        self,
        config_id: int,
        channels: list[int],
    ) -> int | None:
        if not channels:
            return None

        remembered_channel = self.display_channel_by_config_id.get(config_id)
        review_channels = self._chart_payload_channels_for_config(config_id)
        if review_channels and remembered_channel not in review_channels:
            return review_channels[0]

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


