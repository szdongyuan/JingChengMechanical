from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


class VideoModule(QWidget):
    module_id = "video"

    def __init__(self, services):
        super().__init__()
        self.services = services

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        lb = QLabel("暂无画面")
        lb.setObjectName("Muted")
        lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb.setStyleSheet(
            "background:#f8fafc; border:1px dashed #d1d5db; border-radius:10px; padding:14px;"
        )
        lay.addWidget(lb, 1)

