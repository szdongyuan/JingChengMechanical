from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget


class ControlModule(QWidget):
    module_id = "control"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self._workorder: dict = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        row1 = QHBoxLayout()
        self.btn_start = QPushButton("▷ 自动开始")
        self.btn_start.setObjectName("Primary")
        self.btn_stop = QPushButton("□ 停止测试")
        self.btn_stop.setObjectName("Danger")
        row1.addWidget(self.btn_start)
        row1.addWidget(self.btn_stop)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_pass = QPushButton("√ 手工合格")
        self.btn_pass.setObjectName("Success")
        self.btn_fail = QPushButton("× 手工不合格")
        self.btn_fail.setObjectName("Fail")
        row2.addWidget(self.btn_pass)
        row2.addWidget(self.btn_fail)
        lay.addLayout(row2)

        self.services.bus.workorder_changed.connect(self._on_workorder)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self.services.test.stop)
        self.btn_pass.clicked.connect(lambda: self.services.bus.log.emit("Manual judgment: PASS"))
        self.btn_fail.clicked.connect(lambda: self.services.bus.log.emit("Manual judgment: FAIL"))

    def _on_workorder(self, wo: dict):
        self._workorder = wo or {}

    def _start(self):
        self.services.test.start(self._workorder)

