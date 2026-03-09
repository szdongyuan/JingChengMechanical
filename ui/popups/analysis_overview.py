from __future__ import annotations

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from consts.style import APP_QSS
from ui.widgets.cards import Card


class _AnalysisWindow(QWidget):
    def __init__(self, services, title: str, module_cls, parent=None):
        super().__init__(parent)
        self.services = services
        self._settings = QSettings("JingChen", "JingChenMechanical")

        mid = getattr(module_cls, "module_id", title)
        self._geo_key = f"analysis_windows/{mid}/geometry"

        self.setWindowTitle(title)
        self.resize(900, 600)
        self.setStyleSheet(APP_QSS)
        self.setWindowFlag(Qt.WindowType.Window, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(Card(title, module_cls(services)), 1)

        # 恢复上次位置/大小
        geo = self._settings.value(self._geo_key)
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event):
        # 保存当前窗口位置/大小
        self._settings.setValue(self._geo_key, self.saveGeometry())
        super().closeEvent(event)


def open_windows(services, items: list[tuple[str, type]], prev: list[QWidget] | None = None) -> list[QWidget]:
    """
    按 items 配置批量弹出多个独立窗口。

    - **services**: ServiceContainer（或至少包含 bus 的对象）
    - **items**: [(title, ModuleClass), ...]
    - **prev**: 上一次弹出的窗口列表（会先关闭），用于避免越点越多
    """
    if prev:
        for w in prev:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass

    wins: list[QWidget] = []
    for title, cls in items:
        w = _AnalysisWindow(services, title, cls, parent=None)
        w.show()
        wins.append(w)
    return wins