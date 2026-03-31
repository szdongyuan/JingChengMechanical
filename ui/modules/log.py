from __future__ import annotations

from PyQt6.QtWidgets import QTextEdit, QVBoxLayout, QWidget
from datetime import datetime


class LogModule(QWidget):
    module_id = "log"

    def __init__(self, services):
        super().__init__()
        self.services = services

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        lay.addWidget(self.txt, 1)

        self.services.bus.log.connect(self.append)

    def append(self, s: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.txt.append(f"[{now}] {s}")

