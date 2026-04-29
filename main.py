import os
import sys
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.services_container import ServiceContainer
from base.utils import asset_path

def main():
    app = QApplication(sys.argv)

    icon_path = asset_path("logo.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    services = ServiceContainer()
    w = MainWindow(services)
    w.showMaximized()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()