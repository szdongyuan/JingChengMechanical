from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ui.widgets.fake_plots import FakeBars


class OctaveModule(QWidget):
    module_id = "octave"

    def __init__(self, services):
        super().__init__()
        self.services = services

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(FakeBars(), 1)

