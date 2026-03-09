import sys
from PyQt6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.services_container import ServiceContainer

def main():
    app = QApplication(sys.argv)

    services = ServiceContainer()
    w = MainWindow(services)
    w.showMaximized()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()