from __future__ import annotations

import os

from PyQt6.QtWidgets import QWidget

from consts.running_consts import base_dir


class DataManagementModule(QWidget):
    module_id = "data_management"

    def __init__(self, services):
        super().__init__()
        self.services = services
        self.data_dir = os.path.join(base_dir, "data")
