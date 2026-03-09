from __future__ import annotations

from PyQt6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ui.popups.analysis_overview import open_windows
from ui.modules.octave import OctaveModule
from ui.modules.spectrum import SpectrumModule
from ui.modules.spl import SplModule
from ui.modules.stft import StftModule


class WorkOrderModule(QWidget):
    module_id = "workorder"

    def __init__(self, services):
        super().__init__()
        self.services = services

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.ed_sn = QLineEdit()
        self.ed_sn.setPlaceholderText("请输入条码")
        self.ed_model = QLineEdit()
        self.ed_model.setPlaceholderText("请输入产品型号")
        self.ed_cfg = QLineEdit()
        self.ed_cfg.setPlaceholderText("Default Configuration")

        form = QFormLayout()
        form.addRow("条码 SN", self.ed_sn)
        form.addRow("产品型号 Model", self.ed_model)
        form.addRow("测试配置 Config", self.ed_cfg)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setObjectName("Primary")

        # self.btn_analysis = QPushButton("显示所有分析项")

        row.addWidget(self.btn_apply)
        # row.addWidget(self.btn_analysis)
        row.addStretch(1)
        lay.addLayout(row)

        self.btn_apply.clicked.connect(self.apply)
        # self.btn_analysis.clicked.connect(self.open_analysis_overview)

        self._analysis_windows = []

    def value(self) -> dict:
        return {
            "sn": self.ed_sn.text().strip(),
            "model": self.ed_model.text().strip(),
            "config": (self.ed_cfg.text().strip() or "Default Configuration"),
        }

    def apply(self):
        v = self.value()
        self.services.bus.workorder_changed.emit(v)
        self.services.bus.log.emit(f"[WorkOrder] applied {v}")

    def open_analysis_overview(self):
        items = [
            ("频谱 (Spectrum)", SpectrumModule),
            ("STFT 频谱图 (Spectrogram)", StftModule),
            ("SPL 时间曲线 (A计权)", SplModule),
            ("倍频程 (Octave)", OctaveModule),
        ]
        self._analysis_windows = open_windows(self.services, items, prev=self._analysis_windows)
